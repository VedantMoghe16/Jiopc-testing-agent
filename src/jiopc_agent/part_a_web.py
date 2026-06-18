"""Part A — web app checks via Playwright (one shared headless Chromium).

Contract (SPEC §Part A / §Part contracts)::

    def run_part_a(cfg: AgentConfig, log: RunLog) -> list[TestRecord]

The shared context presents a realistic desktop-Chrome identity (``_new_context``)
so legitimate sites render instead of false-flagging a headless bot.

Per web app, in order:

1. ``goto(url, wait_until="domcontentloaded")`` — timeout / connection error
   → FAIL (the *site* is unhealthy, not the agent). These *transient* failures
   are retried up to ``agent.web_retries`` times before the FAIL is recorded.
2. Bot-detection scan (title/body markers from ``agent.bot_detection_markers``
   + challenge-frame selectors) → BLOCKED, logged, never bypassed. If the YAML
   marks ``bot_detection_expected: true`` the detail notes "expected" but the
   result stays BLOCKED.
3. HTTP 4xx/5xx → FAIL.  Blank page (empty ``document.body``) → FAIL.
4. Element checks — each is a CSS selector or an accessible role (``_locate``),
   waited to ``el.state`` (attached|visible) up to ``agent.element_timeout_ms``.
5. ``load_ms`` from Playwright navigation timing; ``load_ms >
   load_time_threshold_ms`` → FAIL "slow load" (value still recorded).

Playwright/Chromium missing → one ERROR record per planned web test with an
install hint; the run continues (Parts B/C unaffected).

Failure screenshots go to ``<log_dir>/artifacts/<run_id>/<test>.png`` and the
path is recorded in ``data.screenshot``. Never taken on PASS (disk + time
budget).

All names/URLs/selectors/thresholds come from the YAML config — nothing app-
specific is hardcoded here (brief §5.3).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from jiopc_agent.config import AgentConfig, WebApp
from jiopc_agent.results import Result, TestRecord, make_record
from jiopc_agent.runlog import RunLog

#: Challenge-page elements that indicate bot detection regardless of wording
#: (SPEC default; complements the text markers from agent.bot_detection_markers).
BOT_DETECTION_SELECTORS = (
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
    "#challenge-form, #cf-challenge"
)

#: How much body text to scan for bot-detection markers (keeps eval cheap).
_BODY_SCAN_CHARS = 20_000

#: One actionable line, reused in every ERROR record when Playwright is absent.
INSTALL_HINT = (
    "install with: pip install playwright && playwright install chromium"
)


def _error_records(
    cfg: AgentConfig, log: RunLog, detail: str
) -> list[TestRecord]:
    """Emit one ERROR record per planned web test (infra problem, not app)."""
    records = []
    for app in cfg.web_apps:
        rec = make_record("A", f"web:{app.name}", Result.ERROR, 0, detail, {"url": app.url})
        log.record(rec)
        records.append(rec)
    return records


def _register_browser_with_selfwatch(pw: Any) -> None:
    """Tell selfwatch which subtree is the browser (counted separately).

    The Playwright driver (a node process) is our direct child and the parent
    of Chromium; registering its PID makes selfwatch account that whole
    subtree as ``browser_rss_mb`` instead of agent RSS (SPEC: agent budget
    excludes the browser). Best-effort: the PID lives behind private
    Playwright attributes, and selfwatch itself is a bonus layer, so any
    failure here is silently ignored.
    """
    try:
        from jiopc_agent import selfwatch

        pid = pw._impl_obj._connection._transport._proc.pid  # noqa: SLF001
        selfwatch.register_browser_pid(int(pid))
    except Exception:  # noqa: BLE001 - monitoring must never affect the run
        pass


def _one_line(exc: BaseException) -> str:
    """First line of an exception message (Playwright appends call logs)."""
    return str(exc).splitlines()[0].strip() if str(exc).strip() else type(exc).__name__


def _safe_filename(test: str) -> str:
    """``web:JioSaavn`` → ``web_JioSaavn`` (portable artifact filename)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", test)


def _screenshot(page: Any, log_dir: Path, run_id: str, test: str) -> str | None:
    """Best-effort failure screenshot; returns the path or None, never raises."""
    try:
        artifact_dir = Path(log_dir).expanduser() / "artifacts" / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"{_safe_filename(test)}.png"
        page.screenshot(path=str(path))
        return str(path)
    except Exception:  # noqa: BLE001 - screenshots must never break a test
        return None


def _nav_load_ms(page: Any, fallback_ms: int) -> int:
    """domcontentloaded time from navigation timing; wall-clock fallback."""
    try:
        value = page.evaluate(
            "() => {"
            "  const e = performance.getEntriesByType('navigation')[0];"
            "  if (e && e.domContentLoadedEventEnd > 0)"
            "    return e.domContentLoadedEventEnd;"
            "  const t = performance.timing;"
            "  if (t && t.domContentLoadedEventEnd > 0)"
            "    return t.domContentLoadedEventEnd - t.navigationStart;"
            "  return -1;"
            "}"
        )
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    except Exception:  # noqa: BLE001 - timing is best-effort
        pass
    return fallback_ms


def _detect_bot_block(page: Any, markers: tuple[str, ...]) -> str | None:
    """Return a human reason if the page looks like a bot-detection wall."""
    try:
        title = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        body = page.evaluate(
            f"() => document.body ? document.body.innerText.slice(0, {_BODY_SCAN_CHARS}) : ''"
        )
        body = (body or "").lower()
    except Exception:  # noqa: BLE001
        body = ""
    for marker in markers:
        if marker and (marker in title or marker in body):
            return f"matched marker {marker!r}"
    try:
        if page.query_selector(BOT_DETECTION_SELECTORS) is not None:
            return "challenge element present (captcha/challenge frame)"
    except Exception:  # noqa: BLE001
        pass
    return None


def _retry_suffix(attempts: int) -> str:
    """Note appended to a transient-failure detail when retries were spent."""
    return f" (after {attempts} attempts)" if attempts > 1 else ""


def _new_context(browser: Any, cfg: AgentConfig) -> Any:
    """Browser context that looks like a real desktop Chrome on JioPC.

    A default headless context advertises ``HeadlessChrome`` and
    ``navigator.webdriver = true``, which makes legitimate sites serve a bot
    wall — a *false* BLOCKED that hides whether the page actually renders. We
    present the same identity a JioPC user's browser would (real UA, India
    locale/timezone, desktop viewport) so the element checks validate the real
    page. This never solves or bypasses a CAPTCHA: a genuine challenge page
    still logs BLOCKED and is never circumvented (SPEC §5.2 "No CAPTCHA solving").
    """
    b = cfg.agent.browser
    context = browser.new_context(
        user_agent=b.user_agent,
        locale=b.locale,
        timezone_id=b.timezone_id,
        viewport={"width": b.viewport_width, "height": b.viewport_height},
    )
    if b.mask_webdriver:
        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:  # noqa: BLE001 - cosmetic hardening, never fatal
            pass
    return context


def _locate(page: Any, el: Any) -> Any:
    """Locator for an element check: accessible role (+ optional name) or CSS.

    Mirrors the spec's "CSS selectors or accessible roles". Role checks use
    Playwright's ``get_by_role`` — the closest match to how a user (and
    assistive tech) perceives the page.
    """
    if el.role:
        return page.get_by_role(el.role, name=el.name) if el.name else page.get_by_role(el.role)
    return page.locator(el.selector)


def _is_blank_page(page: Any) -> bool:
    """True when ``document.body`` is missing or empty (SPEC: blank → FAIL)."""
    try:
        return bool(
            page.evaluate(
                "() => !document.body || document.body.innerHTML.trim() === ''"
            )
        )
    except Exception:  # noqa: BLE001 - cannot evaluate ⇒ treat as blank
        return True


def _check_app(
    app: WebApp,
    context: Any,
    cfg: AgentConfig,
    run_id: str,
    timeout_error: type[Exception],
) -> TestRecord:
    """Run every check for one web app and return its single TestRecord."""
    test = f"web:{app.name}"
    started = time.monotonic()
    data: dict[str, Any] = {
        "url": app.url,
        "load_time_threshold_ms": app.load_time_threshold_ms,
    }

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def finish(result: Result, detail: str, page: Any = None) -> TestRecord:
        if result is not Result.PASS and page is not None:
            shot = _screenshot(page, cfg.agent.log_dir, run_id, test)
            if shot:
                data["screenshot"] = shot
        return make_record("A", test, result, elapsed_ms(), detail, data)

    # Navigation timeout: 2x the per-app threshold (floor 15 s) so a
    # slow-but-alive page is measured and FAILed as "slow load" rather
    # than aborted; a truly dead page still times out → FAIL.
    nav_timeout_ms = max(2 * app.load_time_threshold_ms, 15_000)
    attempts = max(1, cfg.agent.web_retries + 1)

    for attempt in range(attempts):
        last = attempt + 1 == attempts
        page = context.new_page()
        try:
            # Only *transient* navigation failures (timeout / connection) are
            # retried — a one-off network blip must not HOLD a healthy image.
            try:
                response = page.goto(
                    app.url, wait_until="domcontentloaded", timeout=nav_timeout_ms
                )
            except timeout_error:
                if not last:
                    continue
                data["attempts"] = attempts
                return finish(
                    Result.FAIL,
                    f"timeout: no domcontentloaded within {nav_timeout_ms}ms"
                    f"{_retry_suffix(attempts)}",
                    page,
                )
            except Exception as exc:  # noqa: BLE001 - DNS/conn refused/TLS etc.
                if not last:
                    continue
                data["attempts"] = attempts
                return finish(
                    Result.FAIL,
                    f"connection error: {type(exc).__name__}: {_one_line(exc)}"
                    f"{_retry_suffix(attempts)}",
                    page,
                )

            if attempt:
                data["attempts"] = attempt + 1
            return _evaluate_page(app, page, response, cfg, data, elapsed_ms, finish)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    # Unreachable (the loop always returns on the last attempt), but keep the
    # type-checker and any future refactor honest.
    return make_record("A", test, Result.FAIL, elapsed_ms(), "navigation failed", data)


def _evaluate_page(
    app: WebApp,
    page: Any,
    response: Any,
    cfg: AgentConfig,
    data: dict[str, Any],
    elapsed_ms: Any,
    finish: Any,
) -> TestRecord:
    """Classify a successfully navigated page (status/bot/blank/elements/load)."""
    status = response.status if response is not None else None
    data["status"] = status
    load_ms = _nav_load_ms(page, elapsed_ms())
    data["load_ms"] = load_ms

    # Bot detection first: a Cloudflare wall often answers 403, and the
    # right verdict there is BLOCKED (logged, never bypassed), not FAIL.
    block_reason = _detect_bot_block(page, cfg.agent.bot_detection_markers)
    if block_reason:
        data["bot_detection"] = block_reason
        data["bot_detection_expected"] = app.bot_detection_expected
        suffix = " (expected per config)" if app.bot_detection_expected else ""
        return finish(Result.BLOCKED, f"bot detection: {block_reason}{suffix}", page)

    if status is None:
        return finish(Result.FAIL, "no HTTP response received", page)
    if status >= 400:
        return finish(Result.FAIL, f"HTTP {status}", page)
    if _is_blank_page(page):
        return finish(Result.FAIL, f"HTTP {status} but blank page (empty body)", page)

    found: list[str] = []
    missing: list[str] = []
    for el in app.elements:
        try:
            _locate(page, el).first.wait_for(
                state=el.state, timeout=cfg.agent.element_timeout_ms
            )
            found.append(el.description)
        except Exception:  # noqa: BLE001 - absent/timeout both mean missing
            missing.append(el.description)
    data["elements_expected"] = len(app.elements)
    data["elements_found"] = len(found)
    if missing:
        data["elements_missing"] = missing
        return finish(
            Result.FAIL,
            f"HTTP {status}, {len(found)}/{len(app.elements)} elements, "
            f"missing: {', '.join(missing)}",
            page,
        )

    if load_ms > app.load_time_threshold_ms:
        return finish(
            Result.FAIL,
            f"slow load: {load_ms}ms > {app.load_time_threshold_ms}ms "
            f"({len(found)}/{len(app.elements)} elements)",
            page,
        )

    status_text = (response.status_text or "OK") if response is not None else "OK"
    return finish(
        Result.PASS,
        f"{status} {status_text}, {len(found)}/{len(app.elements)} elements, "
        f"load {load_ms}ms < {app.load_time_threshold_ms}ms",
    )


def run_part_a(cfg: AgentConfig, log: RunLog) -> list[TestRecord]:
    """Check every configured web app in one shared headless Chromium.

    Appends each record to ``log`` as it is produced (live tee) and returns
    them all. Never raises: per-test exceptions become ERROR records, and a
    missing Playwright/Chromium yields one ERROR per planned test with an
    install hint.
    """
    if not cfg.web_apps:
        return []

    try:
        from playwright.sync_api import (
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ModuleNotFoundError:
        return _error_records(
            cfg, log, f"Playwright not installed; {INSTALL_HINT}"
        )
    except Exception as exc:  # noqa: BLE001 - broken install
        return _error_records(
            cfg, log, f"Playwright import failed ({exc}); {INSTALL_HINT}"
        )

    records: list[TestRecord] = []
    try:
        with sync_playwright() as pw:
            _register_browser_with_selfwatch(pw)
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 - browser binary missing
                return _error_records(
                    cfg,
                    log,
                    f"Chromium unavailable ({type(exc).__name__}); {INSTALL_HINT}",
                )
            try:
                context = _new_context(browser, cfg)
                for app in cfg.web_apps:
                    try:
                        rec = _check_app(
                            app, context, cfg, log.run_id, PlaywrightTimeoutError
                        )
                    except Exception as exc:  # noqa: BLE001 - per-test guard
                        rec = make_record(
                            "A",
                            f"web:{app.name}",
                            Result.ERROR,
                            0,
                            f"agent error during check: {type(exc).__name__}: {_one_line(exc)}",
                            {"url": app.url},
                        )
                    log.record(rec)
                    records.append(rec)
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001 - never leave the run dirty
                    pass
    except Exception as exc:  # noqa: BLE001 - driver process failed to start
        done = {rec.test for rec in records}
        for app in cfg.web_apps:
            if f"web:{app.name}" in done:
                continue
            rec = make_record(
                "A",
                f"web:{app.name}",
                Result.ERROR,
                0,
                f"Playwright driver failed ({type(exc).__name__}: {_one_line(exc)}); {INSTALL_HINT}",
                {"url": app.url},
            )
            log.record(rec)
            records.append(rec)
    return records
