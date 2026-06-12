"""Run orchestration: part scheduling, selfwatch, summary, exit code.

Design notes
------------
* Part modules (part_a_web / part_b_native / part_c_presence) are imported
  **lazily and defensively** via :func:`importlib.import_module`. If a part
  module is absent or broken, the runner emits one ERROR record per planned
  test in that part instead of crashing — the spine therefore runs (and
  ``--dry-run`` works) before any part is implemented.
* selfwatch, history and notify are bonus/innovation layers; they are also
  imported lazily and every call into them is wrapped so a bug there can
  never corrupt a validation run.
* ``--parallel`` (bonus): Part A (network-bound, waits on remote servers) and
  Part C (pure local I/O, < 30 s) share no resources, so they run
  concurrently on two threads. Part B always runs **exclusively afterwards**
  so app launches are not perturbed by browser CPU spikes — launch_ms and
  CPU samples stay honest. Headroom: A+C together stay well under the 20%
  CPU / 150 MB budgets because C is I/O-bound and A spends its time waiting
  on the network; the shared RunLog is lock-protected.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jiopc_agent import __version__
from jiopc_agent.config import AgentConfig
from jiopc_agent.results import Result, TestRecord, make_record
from jiopc_agent.runlog import RunLog

#: part name → (module, entry-point function). Imported lazily.
PART_MODULES: dict[str, tuple[str, str]] = {
    "A": ("jiopc_agent.part_a_web", "run_part_a"),
    "B": ("jiopc_agent.part_b_native", "run_part_b"),
    "C": ("jiopc_agent.part_c_presence", "run_part_c"),
}

PartFunc = Callable[[AgentConfig, RunLog], "list[TestRecord]"]


@dataclass
class RunOutcome:
    """What the CLI needs after a run."""

    exit_code: int
    log_path: Path | None = None
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Planning (shared by --dry-run and the missing-module ERROR fallback)
# ---------------------------------------------------------------------------


def planned_tests(cfg: AgentConfig, part: str) -> list[str]:
    """Names of the tests a part will run, derived purely from the YAML."""
    if part == "A":
        return [f"web:{app.name}" for app in cfg.web_apps]
    if part == "B":
        return [f"native:{app.name}" for app in cfg.native_apps]
    if part == "C":
        return [
            f"presence:{app.name}:{dim}"
            for app in cfg.desktop_presence
            for dim in ("desktop_folder", "start_menu")
        ]
    raise ValueError(f"unknown part {part!r}")


def execution_order(cfg: AgentConfig, requested: list[str] | None) -> list[str]:
    """Parts to run, in ``agent.part_order``, filtered by ``--part`` flags."""
    wanted = set(requested) if requested else set(PART_MODULES)
    return [p for p in cfg.agent.part_order if p in wanted]


# ---------------------------------------------------------------------------
# Defensive integration points (selfwatch / history / notify)
# ---------------------------------------------------------------------------


def _start_selfwatch(cfg: AgentConfig) -> Any | None:
    """Start the resource self-monitor if available; never raises."""
    try:
        mod = importlib.import_module("jiopc_agent.selfwatch")
        watcher = mod.SelfWatch()
        watcher.start()
        return watcher
    except Exception as exc:  # noqa: BLE001 - bonus layer must never break a run
        print(f"selfwatch unavailable ({exc}); resource stats omitted", file=sys.stderr)
        return None


def _stop_selfwatch(watcher: Any | None) -> dict[str, Any]:
    """Stop the monitor and return its stats dict (empty on any failure)."""
    if watcher is None:
        return {}
    try:
        stats = watcher.stop()
        return dict(stats) if stats else {}
    except Exception as exc:  # noqa: BLE001
        print(f"selfwatch stop failed ({exc})", file=sys.stderr)
        return {}


def _detect_regressions(cfg: AgentConfig, results_map: dict[str, str]) -> list[str]:
    """Ask history.py for regressions vs the previous run; never raises."""
    try:
        mod = importlib.import_module("jiopc_agent.history")
        return list(mod.detect_regressions(cfg.agent.log_dir, results_map))
    except Exception:  # noqa: BLE001 - bonus layer
        return []


def _append_history(
    cfg: AgentConfig, run_id: str, summary: dict[str, Any], results_map: dict[str, str]
) -> None:
    """Append this run to history.jsonl; never raises."""
    try:
        mod = importlib.import_module("jiopc_agent.history")
        mod.append_run(cfg.agent.log_dir, run_id, summary, results_map)
    except Exception:  # noqa: BLE001 - bonus layer
        pass


def _notify(cfg: AgentConfig, summary: dict[str, Any], log_path: Path) -> None:
    """Send the opt-in summary email; never raises."""
    if not cfg.agent.email.enabled:
        return
    try:
        mod = importlib.import_module("jiopc_agent.notify")
        mod.send_summary(cfg, summary, log_path)
    except Exception as exc:  # noqa: BLE001 - bonus layer
        print(f"email notification failed ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Part execution
# ---------------------------------------------------------------------------


def _load_part(part: str) -> tuple[PartFunc | None, str]:
    """Import a part module lazily. Returns (func, "") or (None, reason)."""
    module_name, func_name = PART_MODULES[part]
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - module may not exist yet / be broken
        return None, f"cannot import {module_name}: {exc}"
    func = getattr(mod, func_name, None)
    if not callable(func):
        return None, f"{module_name} has no callable {func_name}()"
    return func, ""


def _run_part(cfg: AgentConfig, log: RunLog, part: str) -> list[TestRecord]:
    """Run one part; on any infra failure emit ERROR records, never raise."""
    func, reason = _load_part(part)
    if func is None:
        records = [
            make_record(part, test, Result.ERROR, 0, f"part {part} unavailable: {reason}")
            for test in planned_tests(cfg, part)
        ]
        for rec in records:
            log.record(rec)
        return records
    try:
        return list(func(cfg, log))
    except Exception as exc:  # noqa: BLE001 - a part crash must not kill the run
        rec = make_record(
            part,
            f"part:{part}",
            Result.ERROR,
            0,
            f"part {part} crashed: {type(exc).__name__}: {exc}",
        )
        log.record(rec)
        return [rec]


def _run_parts(
    cfg: AgentConfig, log: RunLog, order: list[str], parallel: bool
) -> list[TestRecord]:
    """Execute parts sequentially, or A∥C then B when ``parallel`` is set."""
    records: list[TestRecord] = []
    if not parallel:
        for part in order:
            records.extend(_run_part(cfg, log, part))
        return records

    concurrent = [p for p in order if p in ("A", "C")]
    threads: list[threading.Thread] = []
    results: dict[str, list[TestRecord]] = {}
    for part in concurrent:
        thread = threading.Thread(
            target=lambda p=part: results.__setitem__(p, _run_part(cfg, log, p)),
            name=f"part-{part}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    for part in concurrent:  # keep part_order in the returned list
        records.extend(results.get(part, []))
    if "B" in order:  # B always exclusive, after A/C, so launches are unperturbed
        records.extend(_run_part(cfg, log, "B"))
    return records


# ---------------------------------------------------------------------------
# Summary / exit code
# ---------------------------------------------------------------------------


def _count_by(records: list[TestRecord]) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    by_component: dict[str, dict[str, int]] = {}
    by_result: dict[str, int] = {}
    for rec in records:
        comp = by_component.setdefault(rec.component, {})
        comp[rec.result.value] = comp.get(rec.result.value, 0) + 1
        by_result[rec.result.value] = by_result.get(rec.result.value, 0) + 1
    return by_component, by_result


def _print_dry_run(cfg: AgentConfig, order: list[str], parallel: bool) -> None:
    """stdout listing of the planned run; touches nothing on disk."""
    plan = [(part, planned_tests(cfg, part)) for part in order]
    total = sum(len(tests) for _, tests in plan)
    print(f"config OK: {cfg.path}")
    print(f"parts: {', '.join(order)}  (parallel: {'on' if parallel else 'off'})")
    print(f"log dir: {cfg.agent.log_dir}")
    print(f"planned tests ({total}):")
    for part, tests in plan:
        for test in tests:
            print(f"  {part}  {test}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    cfg: AgentConfig,
    *,
    parts: list[str] | None = None,
    parallel: bool = False,
    dry_run: bool = False,
    no_email: bool = False,
) -> RunOutcome:
    """Execute the requested parts and return the outcome.

    Exit-code semantics (binding): 0 — all required tests pass; 1 — at least
    one record whose result is in ``agent.fail_on``. (Config/usage errors
    exit 2, handled in cli.py before we get here.)
    """
    use_parallel = parallel or cfg.agent.parallel
    order = execution_order(cfg, parts)
    if not order:
        print("nothing to do: no requested part is in agent.part_order", file=sys.stderr)
        return RunOutcome(exit_code=2)

    if dry_run:
        _print_dry_run(cfg, order, use_parallel)
        return RunOutcome(exit_code=0)

    started = time.monotonic()
    watcher = _start_selfwatch(cfg)
    with RunLog(cfg.agent.log_dir) as log:
        log.header(agent_version=__version__, config_path=cfg.path, parts=order)
        records = _run_parts(cfg, log, order, use_parallel)

        watch_stats = _stop_selfwatch(watcher)
        by_component, by_result = _count_by(records)
        failed = sum(1 for r in records if r.result.value in cfg.agent.fail_on)
        results_map = {r.test: r.result.value for r in records}
        regressions = _detect_regressions(cfg, results_map)
        exit_code = 1 if failed else 0

        summary = log.summary(
            total=len(records),
            passed=by_result.get(Result.PASS.value, 0),
            failed=failed,
            blocked=by_result.get(Result.BLOCKED.value, 0),
            by_component=by_component,
            by_result=by_result,
            duration_s=round(time.monotonic() - started, 1),
            agent_peak_rss_mb=watch_stats.get("peak_rss_mb"),
            agent_avg_cpu_pct=watch_stats.get("avg_cpu_pct"),
            browser_rss_mb=watch_stats.get("browser_rss_mb"),
            regressions=regressions,
            exit_code=exit_code,
        )

    _append_history(cfg, log.run_id, summary, results_map)
    if regressions:
        print(f"regressions vs previous run: {', '.join(regressions)}", file=sys.stderr)
    if not no_email:
        _notify(cfg, summary, log.path)
    return RunOutcome(exit_code=exit_code, log_path=log.path, summary=summary)
