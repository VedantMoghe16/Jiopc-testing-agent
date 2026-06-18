"""Result taxonomy and the per-test record written to the JSONL log.

These types are the binding contract between the part modules (A/B/C), the
run log, the summary/exit-code logic, and the post-run LLM analysis layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Result(str, Enum):
    """Outcome of a single test.

    ``ERROR`` means the *agent itself* failed to execute the test (an infra
    problem, e.g. browser missing). It counts as a failure for exit-code
    purposes but is kept distinct in the log so the analysis layer can tell
    "the app is broken" apart from "the agent could not check".
    """

    PASS = "PASS"            # test ran, all assertions held
    FAIL = "FAIL"            # test ran, an assertion failed (4xx/5xx, slow load, ...)
    BLOCKED = "BLOCKED"      # bot-detection / CAPTCHA page; logged, never bypassed
    DEGRADED = "DEGRADED"    # native app launched but died before the T+5s sample
    MISSING = "MISSING"      # expected .desktop / shortcut not found anywhere
    MISPLACED = "MISPLACED"  # found, but in the wrong folder / menu category
    ERROR = "ERROR"          # the agent could not execute the test at all


#: Results that drive a non-zero exit code by default. BLOCKED and DEGRADED do
#: NOT fail the run by default; this set is configurable via ``agent.fail_on``
#: in the YAML (see config.py).
REQUIRED_FAIL_RESULTS: frozenset[Result] = frozenset(
    {Result.FAIL, Result.MISSING, Result.MISPLACED, Result.ERROR}
)


def iso_now() -> str:
    """Current local time as ISO-8601 with timezone offset."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class TestRecord:
    """One test outcome; serialises to a single ``"type": "record"`` log line."""

    ts: str                  # ISO-8601 with timezone (use iso_now())
    component: str           # "A" | "B" | "C"
    test: str                # e.g. "web:JioSaavn", "presence:Chess:desktop_folder"
    result: Result
    duration_ms: int
    detail: str              # one line, human readable
    data: dict = field(default_factory=dict)  # structured extras (load_ms, rss_mb, ...)

    def to_json(self) -> str:
        """Serialise as one machine-clean JSON log line (no trailing newline)."""
        return json.dumps(
            {
                "type": "record",
                "ts": self.ts,
                "component": self.component,
                "test": self.test,
                "result": self.result.value,
                "duration_ms": self.duration_ms,
                "detail": self.detail,
                "data": self.data,
            },
            ensure_ascii=False,
        )


def make_record(
    component: str,
    test: str,
    result: Result,
    duration_ms: int,
    detail: str,
    data: dict | None = None,
) -> TestRecord:
    """Convenience constructor that stamps the current timestamp."""
    return TestRecord(
        ts=iso_now(),
        component=component,
        test=test,
        result=result,
        duration_ms=duration_ms,
        detail=detail,
        data=data or {},
    )
