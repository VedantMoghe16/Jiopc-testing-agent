"""JSON Lines run log with a live human-readable tee to stderr.

Log format (documented in README; the LLM prompt mirrors it):

* first line  — ``{"type": "header", ...}``
* one line per test — ``{"type": "record", ...}`` (see results.TestRecord)
* final line  — ``{"type": "summary", ...}``

The log file stays machine-clean (pure JSONL); humans watch the stderr tee:
``[PASS] A web:JioSaavn (1240ms) — 200 OK, 2/2 elements ...``.

Thread-safe: Part A and Part C may write concurrently in ``--parallel`` mode.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from jiopc_agent.results import TestRecord


def new_run_id(now: datetime | None = None) -> str:
    """Run id used in the header and the log filename: ``YYYY-MM-DDTHH-MM-SS``."""
    return (now or datetime.now()).strftime("%Y-%m-%dT%H-%M-%S")


class RunLog:
    """Writer for one run's JSONL log file.

    API: ``RunLog(log_dir)`` → ``.path``, ``.header(...)``,
    ``.record(TestRecord)``, ``.summary(...) -> dict``, ``.close()``.
    """

    def __init__(
        self,
        log_dir: Path,
        run_id: str | None = None,
        tee: TextIO | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        log_dir = Path(log_dir).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path: Path = log_dir / f"test_run_{self.run_id}.log"
        self._fh = self.path.open("w", encoding="utf-8")
        self._tee = tee if tee is not None else sys.stderr
        self._lock = threading.Lock()

    # -- low-level ---------------------------------------------------------

    def _write_line(self, obj: dict[str, Any] | str) -> None:
        line = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()  # live: log is valid/parsable even if the run dies

    def _tee_line(self, text: str) -> None:
        with self._lock:
            print(text, file=self._tee, flush=True)

    # -- public API ---------------------------------------------------------

    def header(
        self,
        *,
        agent_version: str,
        config_path: Path | str,
        parts: list[str] | tuple[str, ...],
        host: str | None = None,
    ) -> None:
        """Write the mandatory first line of the log."""
        self._write_line(
            {
                "type": "header",
                "run_id": self.run_id,
                "agent_version": agent_version,
                "host": host or socket.gethostname(),
                "platform": sys.platform,
                "config_path": str(config_path),
                "parts": list(parts),
            }
        )

    def record(self, rec: TestRecord) -> None:
        """Append one test record and tee a one-liner to stderr."""
        self._write_line(rec.to_json())
        self._tee_line(
            f"[{rec.result.value}] {rec.component} {rec.test} "
            f"({rec.duration_ms}ms) — {rec.detail}"
        )

    def summary(self, **fields: Any) -> dict[str, Any]:
        """Write the final summary line; returns the summary dict."""
        payload: dict[str, Any] = {"type": "summary", **fields}
        self._write_line(payload)
        self._tee_line(
            f"summary: {fields.get('passed', 0)}/{fields.get('total', 0)} passed, "
            f"{fields.get('failed', 0)} failed, exit_code={fields.get('exit_code')} "
            f"→ {self.path}"
        )
        return payload

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    # -- context manager ------------------------------------------------------

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
