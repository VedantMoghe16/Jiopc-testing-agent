"""Plain-function helpers shared by the test modules (not fixtures).

Kept out of conftest.py so tests can ``import _helpers`` explicitly instead of
relying on conftest side-effects.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

from conftest import FAKE_APP, FAKE_PROCESS_NAME, SRC_DIR

if str(SRC_DIR) not in sys.path:  # defensive: direct module import
    sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def write_config(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    """Write YAML text to a tmp file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def load_config_text(tmp_path: Path, text: str):
    """Write YAML text and load it through the real config layer."""
    from jiopc_agent.config import load_config

    return load_config(write_config(tmp_path, text))


def make_fake_app_desktop(
    directory: Path,
    *,
    exec_cmd: str | None = None,
    name: str = "Fixture Fake App",
    filename: str = "fixture-fakeapp.desktop",
) -> Path:
    """Write a .desktop launching fake_app.py with absolute paths."""
    if exec_cmd is None:
        exec_cmd = f"{sys.executable} {FAKE_APP} %U"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec={exec_cmd}\n"
        "Categories=Utility;\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# JSONL log helpers
# ---------------------------------------------------------------------------


def parse_log(log_path: Path) -> tuple[dict, list[dict], dict]:
    """Parse a run log; asserts JSONL shape: header, records, summary."""
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines, f"log {log_path} is empty"
    parsed = [json.loads(line) for line in lines]  # every line must be valid JSON
    header, *middle, summary = parsed
    assert header.get("type") == "header", f"first line is not a header: {header}"
    assert summary.get("type") == "summary", f"last line is not a summary: {summary}"
    assert all(rec.get("type") == "record" for rec in middle), "non-record middle line"
    return header, middle, summary


def records_by_test(records: list) -> dict[str, object]:
    """Index records (dicts or TestRecord objects) by their test name."""
    out: dict[str, object] = {}
    for rec in records:
        key = rec["test"] if isinstance(rec, dict) else rec.test
        out[key] = rec
    return out


def result_of(rec) -> str:
    """Result value from either a parsed dict or a TestRecord."""
    if isinstance(rec, dict):
        return rec["result"]
    return rec.result.value


# ---------------------------------------------------------------------------
# Process helpers (Part B orphan checks)
# ---------------------------------------------------------------------------


def fake_app_procs() -> list:
    """All live processes that are (or run) the fixture fake app."""
    import psutil

    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = proc.info.get("name") or ""
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if name == FAKE_PROCESS_NAME or str(FAKE_APP) in cmdline:
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def wait_no_fake_app(timeout_s: float = 5.0) -> list:
    """Poll until no fake-app process remains; return any survivors."""
    deadline = time.monotonic() + timeout_s
    survivors = fake_app_procs()
    while survivors and time.monotonic() < deadline:
        time.sleep(0.2)
        survivors = fake_app_procs()
    return survivors


def kill_fake_apps() -> None:
    """Best-effort cleanup so a failing test never leaks the fixture app."""
    import psutil

    for proc in fake_app_procs():
        try:
            proc.kill()
        except psutil.Error:
            pass


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def closed_port_url() -> str:
    """URL on a port that was just bound and released (connection refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def call_main(main, argv: list[str]) -> int:
    """Call a CLI main() tolerant of return-code vs SystemExit styles."""
    try:
        rc = main(argv)
    except SystemExit as exc:
        rc = exc.code
    if rc is None:
        return 0
    return int(rc)
