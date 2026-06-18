"""part_a_web.py — load / element / bot-detection checks over local fixtures.

Skips cleanly when Playwright or headless Chromium is unavailable (via the
``playwright_ready`` session fixture); the no-playwright ERROR path is tested
by the inverse-gated test at the bottom. All web apps run through ONE
``run_part_a`` call (module-scoped fixture) to honour the shared-browser
design and keep the suite fast.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import _helpers
from conftest import playwright_skip_reason

part_a = pytest.importorskip(
    "jiopc_agent.part_a_web", reason="spine module part_a_web.py not implemented yet"
)

from jiopc_agent.runlog import RunLog  # noqa: E402

SLOW_THRESHOLD_MS = 250  # /slow.html is delayed ~1.5 s server-side


def _web_yaml(base_url: str, log_dir: Path, dead_url: str) -> str:
    return f"""
agent:
  log_dir: {log_dir}
  element_timeout_ms: 1500
  web_retries: 1
web_apps:
  - name: OK
    url: {base_url}/ok.html
    elements:
      - {{selector: "nav", description: top navigation}}
      - {{selector: "input[type=search]", description: search box}}
  - name: RoleChecks
    url: {base_url}/ok.html
    elements:
      - {{role: "searchbox", description: search box by accessible role}}
      - {{role: "heading", name: "Fixture OK page", description: main heading by role+name}}
      - {{selector: "nav", state: visible, description: visible top nav}}
  - name: Slow
    url: {base_url}/slow.html
    load_time_threshold_ms: {SLOW_THRESHOLD_MS}
  - name: CaptchaExpected
    url: {base_url}/captcha.html
    bot_detection_expected: true
  - name: CaptchaUnexpected
    url: {base_url}/captcha.html
  - name: MissingElements
    url: {base_url}/missing-elements.html
    elements:
      - {{selector: "nav", description: top navigation}}
      - {{selector: "#search-box", description: search box}}
  - name: ServerError
    url: {base_url}/500
  - name: Blank
    url: {base_url}/blank.html
  - name: ConnectionRefused
    url: {dead_url}/
"""


@pytest.fixture(scope="module")
def part_a_run(tmp_path_factory, web_server, playwright_ready):
    """One shared run over every fixture page; returns (by_test, cfg, log)."""
    tmp_path = tmp_path_factory.mktemp("part_a")
    log_dir = tmp_path / "logs"
    cfg = _helpers.load_config_text(
        tmp_path, _web_yaml(web_server.base_url, log_dir, _helpers.closed_port_url())
    )
    log = RunLog(log_dir, tee=io.StringIO())
    records = part_a.run_part_a(cfg, log)
    log.close()
    by_test = _helpers.records_by_test(records)
    assert len(records) == len(cfg.web_apps)
    assert all(rec.component == "A" for rec in records)
    return by_test, cfg, log


def test_healthy_page_passes(part_a_run):
    rec = part_a_run[0]["web:OK"]
    assert _helpers.result_of(rec) == "PASS", rec.detail
    assert isinstance(rec.data.get("load_ms"), (int, float)), rec.data
    assert "screenshot" not in rec.data  # never on PASS (disk + time budget)


def test_accessible_role_and_visible_checks_pass(part_a_run):
    """Element checks by ARIA role (+name) and a visible-state CSS check."""
    rec = part_a_run[0]["web:RoleChecks"]
    assert _helpers.result_of(rec) == "PASS", rec.detail
    assert rec.data.get("elements_found") == 3, rec.data


def test_slow_page_fails_but_records_load_ms(part_a_run):
    rec = part_a_run[0]["web:Slow"]
    assert _helpers.result_of(rec) == "FAIL", rec.detail
    assert "slow" in rec.detail.lower()
    assert rec.data.get("load_ms", 0) > SLOW_THRESHOLD_MS  # value still recorded


def test_expected_captcha_is_blocked_and_noted(part_a_run):
    rec = part_a_run[0]["web:CaptchaExpected"]
    assert _helpers.result_of(rec) == "BLOCKED", rec.detail
    assert "expected" in rec.detail.lower()  # SPEC: detail notes "expected"


def test_unexpected_captcha_is_still_blocked_never_bypassed(part_a_run):
    rec = part_a_run[0]["web:CaptchaUnexpected"]
    assert _helpers.result_of(rec) == "BLOCKED", rec.detail


def test_missing_elements_fail(part_a_run):
    rec = part_a_run[0]["web:MissingElements"]
    assert _helpers.result_of(rec) == "FAIL", rec.detail


def test_failure_screenshot_saved_under_log_dir(part_a_run):
    """Screenshots on failure → <log_dir>/artifacts/<run_id>/... (SPEC)."""
    by_test, cfg, log = part_a_run
    rec = by_test["web:MissingElements"]
    shot = rec.data.get("screenshot")
    assert shot, f"expected data.screenshot on FAIL, got data={rec.data}"
    shot_path = Path(shot)
    assert shot_path.is_file(), f"screenshot missing on disk: {shot_path}"
    artifacts_root = Path(cfg.agent.log_dir) / "artifacts" / log.run_id
    assert str(shot_path).startswith(str(artifacts_root)), (shot_path, artifacts_root)


def test_http_500_fails(part_a_run):
    rec = part_a_run[0]["web:ServerError"]
    assert _helpers.result_of(rec) == "FAIL", rec.detail


def test_blank_page_fails(part_a_run):
    rec = part_a_run[0]["web:Blank"]
    assert _helpers.result_of(rec) == "FAIL", rec.detail


def test_connection_error_fails_not_errors(part_a_run):
    """Unreachable app = the APP is broken (FAIL), not agent infra (ERROR)."""
    rec = part_a_run[0]["web:ConnectionRefused"]
    assert _helpers.result_of(rec) == "FAIL", rec.detail


def test_transient_failure_is_retried(part_a_run):
    """web_retries=1 ⇒ a transient connection error is tried twice before FAIL."""
    rec = part_a_run[0]["web:ConnectionRefused"]
    assert rec.data.get("attempts") == 2, rec.data
    assert "after 2 attempts" in rec.detail


def test_passing_page_records_no_retry(part_a_run):
    """A page that loads first time carries no retry bookkeeping."""
    rec = part_a_run[0]["web:OK"]
    assert "attempts" not in rec.data, rec.data


@pytest.mark.skipif(
    playwright_skip_reason() == "",
    reason="playwright is installed; the missing-playwright ERROR path is untestable here",
)
def test_without_playwright_each_test_errors_with_hint(tmp_path, web_server):
    """Playwright/Chromium missing → one ERROR per web test + install hint."""
    cfg = _helpers.load_config_text(
        tmp_path, _web_yaml(web_server.base_url, tmp_path / "logs", _helpers.closed_port_url())
    )
    with RunLog(tmp_path / "logs", tee=io.StringIO()) as log:
        records = part_a.run_part_a(cfg, log)
    assert len(records) == len(cfg.web_apps)
    for rec in records:
        assert _helpers.result_of(rec) == "ERROR", rec.detail
        assert "playwright" in rec.detail.lower()  # actionable install hint
