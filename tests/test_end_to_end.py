"""End-to-end: the real CLI (jiopc_agent.py shim) against fixture-config.yaml.

The static fixture-config.yaml is rendered for the live environment (actual
web-server port, absolute fixture paths, tmp log_dir, absolute fake-app
.desktop) and run as a subprocess. Asserts exit code, full JSONL schema
validity, BLOCKED on captcha, MISSING vs MISPLACED, and zero orphans.

Part A behaviour branches on Playwright availability: with it, web tests get
their real results; without it, the SPEC requires one ERROR record per web
test — both are schema-valid and both exit 1 here (C has planted failures).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import _helpers
from conftest import (
    FIXTURE_CONFIG,
    FIXTURES_DIR,
    REPO_ROOT,
    playwright_skip_reason,
)

# The full run needs the part modules; until they land the run would be all
# ERROR records and the distinction assertions meaningless. (part_a_web may
# exist WITHOUT playwright installed — that path is still exercised below.)
pytest.importorskip(
    "jiopc_agent.part_a_web", reason="spine module part_a_web.py not implemented yet"
)
pytest.importorskip(
    "jiopc_agent.part_b_native", reason="spine module part_b_native.py not implemented yet"
)
pytest.importorskip(
    "jiopc_agent.part_c_presence", reason="spine module part_c_presence.py not implemented yet"
)

VALID_RESULTS = {"PASS", "FAIL", "BLOCKED", "DEGRADED", "MISSING", "MISPLACED", "ERROR"}
RECORD_KEYS = {"type", "ts", "component", "test", "result", "duration_ms", "detail", "data"}


def _render_config(tmp_path, port: int):
    """fixture-config.yaml → live config (port, abs paths, tmp log dir)."""
    log_dir = tmp_path / "logs"
    desktop = _helpers.make_fake_app_desktop(tmp_path / "apps")
    text = FIXTURE_CONFIG.read_text(encoding="utf-8")
    text = text.replace(
        "./tests/fixtures/desktop_tree/applications/fixture-fakeapp.desktop", str(desktop)
    )
    text = text.replace("127.0.0.1:8901", f"127.0.0.1:{port}")
    text = text.replace("~/.local/share/jiopc/agent-fixture/", str(log_dir))
    text = text.replace("./tests/fixtures", str(FIXTURES_DIR))
    cfg_path = tmp_path / "fixture-config.yaml"
    cfg_path.write_text(text, encoding="utf-8")
    return cfg_path, log_dir


def _run_cli(cfg_path, tmp_path, *extra_args, timeout=240):
    env = dict(os.environ)
    env["FAKE_APP_LINK_DIR"] = str(tmp_path / "links")
    env.pop("FAKE_APP_SECONDS", None)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "jiopc_agent.py"), "--config", str(cfg_path),
         "--no-email", *extra_args],
        cwd=str(tmp_path),  # nothing may depend on the repo-root cwd
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def e2e(tmp_path_factory, web_server):
    """One full agent run shared by all assertions in this module."""
    tmp_path = tmp_path_factory.mktemp("e2e")
    cfg_path, log_dir = _render_config(tmp_path, web_server.port)
    try:
        proc = _run_cli(cfg_path, tmp_path)
    finally:
        _helpers.kill_fake_apps()
    logs = sorted(log_dir.glob("test_run_*.log"))
    assert len(logs) == 1, f"expected exactly one run log, found {logs}\n{proc.stderr}"
    header, records, summary = _helpers.parse_log(logs[0])
    return {
        "proc": proc,
        "log_path": logs[0],
        "header": header,
        "records": records,
        "summary": summary,
        "by_test": _helpers.records_by_test(records),
        "log_dir": log_dir,
    }


def test_exit_code_is_1_on_required_failures(e2e):
    proc = e2e["proc"]
    assert proc.returncode == 1, f"stderr:\n{proc.stderr}"
    assert e2e["summary"]["exit_code"] == 1


def test_stdout_stays_reserved(e2e):
    """stderr is for humans; a plain run must leave stdout empty."""
    assert e2e["proc"].stdout == ""


def test_stderr_tee_shows_progress(e2e):
    err = e2e["proc"].stderr
    assert "presence:Ghost" in err  # one-liners teed live
    assert "[MISSING]" in err


def test_log_schema_valid(e2e):
    header, records, summary = e2e["header"], e2e["records"], e2e["summary"]
    assert header["run_id"] in str(e2e["log_path"])
    assert header["agent_version"]
    assert header["parts"] == ["A", "B", "C"]
    for rec in records:
        assert RECORD_KEYS <= set(rec), rec
        assert rec["component"] in {"A", "B", "C"}
        assert rec["result"] in VALID_RESULTS
        assert isinstance(rec["duration_ms"], int) and rec["duration_ms"] >= 0
        assert isinstance(rec["data"], dict)
        assert "T" in rec["ts"]
    assert summary["total"] == len(records)
    assert summary["failed"] >= 1


def test_summary_counts_match_records(e2e):
    records, summary = e2e["records"], e2e["summary"]
    assert summary["passed"] == sum(1 for r in records if r["result"] == "PASS")
    by_result = {}
    for rec in records:
        by_result[rec["result"]] = by_result.get(rec["result"], 0) + 1
    assert summary["by_result"] == by_result


def test_missing_vs_misplaced_distinction(e2e):
    by_test = e2e["by_test"]
    # planted in the fixture tree — see fixture-config.yaml comments
    assert by_test["presence:Ghost:desktop_folder"]["result"] == "MISSING"
    assert by_test["presence:Ghost:start_menu"]["result"] == "MISSING"
    assert by_test["presence:Paint:desktop_folder"]["result"] == "MISPLACED"
    assert by_test["presence:Calc:start_menu"]["result"] == "MISPLACED"
    # control group: nothing falsely flagged
    assert by_test["presence:Chess:desktop_folder"]["result"] == "PASS"
    assert by_test["presence:Chess:start_menu"]["result"] == "PASS"
    assert by_test["presence:Writer:desktop_folder"]["result"] == "PASS"
    assert by_test["presence:Paint:start_menu"]["result"] == "PASS"
    assert by_test["presence:Calc:desktop_folder"]["result"] == "PASS"


def test_native_app_passes_with_no_orphans(e2e):
    rec = e2e["by_test"]["native:FakeApp"]
    assert rec["result"] == "PASS", rec["detail"]
    survivors = _helpers.wait_no_fake_app(timeout_s=5)
    assert not survivors, (
        f"fake_app orphaned after the run: {[(p.pid, p.name()) for p in survivors]}"
    )


def test_web_results(e2e):
    """With Playwright: real Part A outcomes incl. BLOCKED captcha.
    Without: SPEC mandates one ERROR per web test with an install hint."""
    by_test = e2e["by_test"]
    web = {name: by_test[f"web:{name}"] for name in (
        "FixtureOK", "FixtureSlow", "FixtureCaptcha", "FixtureMissingElements",
        "FixtureServerError",
    )}
    if playwright_skip_reason():
        for name, rec in web.items():
            assert rec["result"] == "ERROR", (name, rec["detail"])
            assert "playwright" in rec["detail"].lower()
        return
    assert web["FixtureOK"]["result"] == "PASS", web["FixtureOK"]["detail"]
    assert web["FixtureSlow"]["result"] == "FAIL"
    assert web["FixtureCaptcha"]["result"] == "BLOCKED"  # logged, never bypassed
    assert "expected" in web["FixtureCaptcha"]["detail"].lower()
    assert web["FixtureMissingElements"]["result"] == "FAIL"
    assert web["FixtureServerError"]["result"] == "FAIL"
    assert e2e["summary"]["blocked"] >= 1


def test_blocked_does_not_drive_exit_code(e2e):
    """BLOCKED is excluded from fail_on; only FAIL/MISSING/MISPLACED/ERROR count."""
    records, summary = e2e["records"], e2e["summary"]
    expected_failed = sum(
        1 for r in records if r["result"] in {"FAIL", "MISSING", "MISPLACED", "ERROR"}
    )
    assert summary["failed"] == expected_failed


def test_log_dir_is_only_write_location(e2e):
    """All artifacts stay under the configured log_dir (user space)."""
    log_dir = e2e["log_dir"]
    # tmp_path also holds our generated config/apps/links dirs — nothing else
    allowed = {"logs", "apps", "links", "fixture-config.yaml"}
    found = {p.name for p in log_dir.parent.iterdir()}
    assert found <= allowed, f"unexpected writes outside log_dir: {sorted(found - allowed)}"


def test_dry_run_lists_plan_and_touches_nothing(tmp_path, web_server):
    cfg_path, log_dir = _render_config(tmp_path, web_server.port)
    proc = _run_cli(cfg_path, tmp_path, "--dry-run", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "planned tests" in out
    for name in ("web:FixtureOK", "native:FakeApp", "presence:Ghost:desktop_folder",
                 "presence:Ghost:start_menu"):
        assert name in out
    assert not list(log_dir.glob("test_run_*.log")), "--dry-run must not write a log"
    assert not _helpers.fake_app_procs(), "--dry-run must not launch anything"


def test_part_filter_runs_only_requested_part(tmp_path, web_server):
    """--part C: only presence records in the log; Part C alone is < 30 s."""
    cfg_path, log_dir = _render_config(tmp_path, web_server.port)
    proc = _run_cli(cfg_path, tmp_path, "--part", "C", timeout=60)
    assert proc.returncode == 1, proc.stderr  # Ghost/Paint/Calc still fail
    logs = list(log_dir.glob("test_run_*.log"))
    assert len(logs) == 1
    header, records, summary = _helpers.parse_log(logs[0])
    assert header["parts"] == ["C"]
    assert records and all(r["component"] == "C" for r in records)
    assert summary["duration_s"] < 30
