"""Bonus: run history and regression detection.

After each run the runner appends one JSON line to ``<log_dir>/history.jsonl``
(run id, totals, per-test result map). Before writing, it asks
:func:`detect_regressions` to diff the new results against the **previous**
run: any test that was PASS last time and is no longer PASS is a regression,
surfaced in the summary line, the stderr tee, and the LLM analysis.

Both functions are defensive — a corrupt or missing history file degrades to
"no regressions" rather than ever breaking a validation run (the runner
additionally wraps every call here).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jiopc_agent.results import iso_now

#: History file name, kept alongside the run logs inside ``agent.log_dir``.
HISTORY_FILENAME = "history.jsonl"

#: Summary fields copied into each history line (small, trend-friendly).
_TOTALS_KEYS = ("total", "passed", "failed", "blocked", "duration_s", "exit_code")


def history_path(log_dir: Path | str) -> Path:
    """Location of ``history.jsonl`` for a given log directory."""
    return Path(log_dir).expanduser() / HISTORY_FILENAME


def _last_run(path: Path) -> dict[str, Any] | None:
    """Return the most recent valid history line, or None.

    Malformed lines (partial write from a killed run, manual edits) are
    skipped rather than raising.
    """
    if not path.is_file():
        return None
    last: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("results"), dict):
                last = obj
    return last


def detect_regressions(
    log_dir: Path | str, results_map: Mapping[str, str]
) -> list[str]:
    """Diff this run's results against the previous run's history line.

    A regression is a test that was ``PASS`` last run and is anything other
    than ``PASS`` now (FAIL/MISSING/MISPLACED/ERROR/DEGRADED/BLOCKED).
    Returns human-readable entries like ``"web:JioSaavn (PASS->FAIL)"``;
    empty list when there is no usable history. Never raises.
    """
    try:
        previous = _last_run(history_path(log_dir))
    except OSError:
        return []
    if previous is None:
        return []
    prev_results: dict[str, Any] = previous.get("results", {})
    regressions = [
        f"{test} (PASS->{now})"
        for test, now in sorted(results_map.items())
        if prev_results.get(test) == "PASS" and now != "PASS"
    ]
    return regressions


def append_run(
    log_dir: Path | str,
    run_id: str,
    summary: Mapping[str, Any],
    results_map: Mapping[str, str],
) -> None:
    """Append one compact line for this run to ``history.jsonl``.

    Line shape::

        {"run_id": ..., "ts": ..., "totals": {...}, "results": {test: result}}
    """
    path = history_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "run_id": run_id,
        "ts": iso_now(),
        "totals": {k: summary[k] for k in _TOTALS_KEYS if k in summary},
        "results": dict(results_map),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
