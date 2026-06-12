"""runlog.py — JSONL shape, stderr tee format, thread safety."""

from __future__ import annotations

import io
import json
import re
import threading

import _helpers
from jiopc_agent.results import Result, make_record
from jiopc_agent.runlog import RunLog, new_run_id


def _make_log(tmp_path, **kwargs):
    return RunLog(tmp_path / "logs", tee=kwargs.pop("tee", io.StringIO()), **kwargs)


def test_filename_and_run_id(tmp_path):
    log = _make_log(tmp_path)
    assert log.path.name == f"test_run_{log.run_id}.log"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", log.run_id)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", new_run_id())
    log.close()


def test_header_record_summary_shape(tmp_path):
    with _make_log(tmp_path) as log:
        log.header(agent_version="1.0.0", config_path="/cfg.yaml", parts=["A", "C"])
        log.record(make_record("A", "web:Fixture", Result.PASS, 12, "200 OK", {"load_ms": 12}))
        summary = log.summary(total=1, passed=1, failed=0, exit_code=0)
        assert summary["type"] == "summary"
        assert summary["exit_code"] == 0

    header, records, summary_line = _helpers.parse_log(log.path)
    assert header["agent_version"] == "1.0.0"
    assert header["config_path"] == "/cfg.yaml"
    assert header["parts"] == ["A", "C"]
    assert header["run_id"] == log.run_id

    assert len(records) == 1
    rec = records[0]
    assert rec["component"] == "A"
    assert rec["test"] == "web:Fixture"
    assert rec["result"] == "PASS"
    assert rec["duration_ms"] == 12
    assert rec["data"] == {"load_ms": 12}
    assert "T" in rec["ts"]  # ISO-8601

    assert summary_line["total"] == 1
    assert summary_line["exit_code"] == 0


def test_human_tee_format(tmp_path):
    tee = io.StringIO()
    with _make_log(tmp_path, tee=tee) as log:
        log.record(
            make_record("A", "web:JioSaavn", Result.PASS, 1240, "200 OK, 2/2 elements")
        )
    text = tee.getvalue()
    assert "[PASS] A web:JioSaavn (1240ms)" in text
    assert "200 OK, 2/2 elements" in text


def test_log_file_stays_machine_clean(tmp_path):
    """The tee goes to stderr only; every file line must parse as JSON."""
    with _make_log(tmp_path) as log:
        log.header(agent_version="1.0.0", config_path="c", parts=["B"])
        log.record(make_record("B", "native:X", Result.DEGRADED, 5000, "died early"))
        log.summary(total=1, passed=0, failed=0, exit_code=0)
    for line in log.path.read_text(encoding="utf-8").splitlines():
        json.loads(line)  # raises on any human-readable contamination


def test_lines_flushed_immediately(tmp_path):
    """Live log: parsable even while the run is still open."""
    log = _make_log(tmp_path)
    log.header(agent_version="1.0.0", config_path="c", parts=["A"])
    log.record(make_record("A", "web:X", Result.FAIL, 1, "boom"))
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["result"] == "FAIL"
    log.close()


def test_thread_safe_concurrent_records(tmp_path):
    """--parallel mode: A and C write concurrently; no interleaved lines."""
    with _make_log(tmp_path) as log:
        log.header(agent_version="1.0.0", config_path="c", parts=["A", "C"])

        def write(component: str) -> None:
            for i in range(50):
                log.record(
                    make_record(component, f"t:{component}:{i}", Result.PASS, i, "ok")
                )

        threads = [threading.Thread(target=write, args=(c,)) for c in ("A", "C")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.summary(total=100, passed=100, failed=0, exit_code=0)

    header, records, summary = _helpers.parse_log(log.path)
    assert len(records) == 100
    assert {r["component"] for r in records} == {"A", "C"}
    assert summary["total"] == 100
