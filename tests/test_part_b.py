"""part_b_native.py — launch / detect / sample / terminate, zero orphans.

Uses tests/fixtures/fake_app.py: a sleeper that renames itself to
``jiopc-fake-app`` (see its docstring). These tests are wall-clock heavy
(the T+5s sample is part of the SPEC) — roughly 25-30 s total.
"""

from __future__ import annotations

import io
import sys

import pytest

import _helpers
from conftest import FAKE_APP, FAKE_PROCESS_NAME

pytest.importorskip("psutil", reason="psutil is a hard dependency of Part B")
part_b = pytest.importorskip(
    "jiopc_agent.part_b_native", reason="spine module part_b_native.py not implemented yet"
)

from jiopc_agent.runlog import RunLog  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_app_env(tmp_path, monkeypatch):
    """Keep the rename-symlink inside tmp and never leak a fake app."""
    monkeypatch.setenv("FAKE_APP_LINK_DIR", str(tmp_path / "links"))
    monkeypatch.delenv("FAKE_APP_SECONDS", raising=False)
    yield
    _helpers.kill_fake_apps()


def _run_native(tmp_path, *, desktop_file, process_name, launch_timeout_s=10):
    """Build a one-app native config and execute run_part_b once."""
    cfg = _helpers.load_config_text(
        tmp_path,
        f"""
        agent:
          log_dir: {tmp_path / 'logs'}
          cooldown_s: 0
          poll_interval_ms: 100
          term_grace_s: 2
        native_apps:
          - name: FakeApp
            desktop_file: {desktop_file}
            process_name: {process_name}
            launch_timeout_s: {launch_timeout_s}
        """,
    )
    with RunLog(cfg.agent.log_dir, tee=io.StringIO()) as log:
        records = part_b.run_part_b(cfg, log)
    assert len(records) == 1
    rec = records[0]
    assert rec.component == "B"
    assert rec.test == "native:FakeApp"
    return rec


def test_healthy_app_passes_and_is_sampled(tmp_path):
    desktop = _helpers.make_fake_app_desktop(tmp_path / "apps")
    rec = _run_native(tmp_path, desktop_file=desktop, process_name=FAKE_PROCESS_NAME)
    assert _helpers.result_of(rec) == "PASS", rec.detail
    # SPEC: launch_ms (poll-overhead corrected), T+5s rss/cpu samples in data
    assert "launch_ms" in rec.data, rec.data
    assert "rss_mb" in rec.data and rec.data["rss_mb"] > 0, rec.data
    assert "cpu_pct" in rec.data and rec.data["cpu_pct"] >= 0, rec.data


def test_no_orphan_after_run(tmp_path):
    desktop = _helpers.make_fake_app_desktop(tmp_path / "apps")
    _run_native(tmp_path, desktop_file=desktop, process_name=FAKE_PROCESS_NAME)
    survivors = _helpers.wait_no_fake_app(timeout_s=5)
    assert not survivors, (
        "orphaned fake-app process(es) survived Part B cleanup: "
        f"{[(p.pid, p.name()) for p in survivors]}"
    )


def test_app_that_dies_early_is_degraded(tmp_path, monkeypatch):
    """Found, then died before the T+5s sample → DEGRADED (SPEC §Part B)."""
    monkeypatch.setenv("FAKE_APP_SECONDS", "0.8")
    desktop = _helpers.make_fake_app_desktop(tmp_path / "apps")
    rec = _run_native(tmp_path, desktop_file=desktop, process_name=FAKE_PROCESS_NAME)
    assert _helpers.result_of(rec) == "DEGRADED", rec.detail


def test_missing_desktop_file_fails_fast(tmp_path):
    rec = _run_native(
        tmp_path,
        desktop_file=tmp_path / "apps" / "no-such.desktop",
        process_name=FAKE_PROCESS_NAME,
        launch_timeout_s=2,
    )
    assert _helpers.result_of(rec) == "FAIL", rec.detail
    assert rec.duration_ms < 2000  # fail fast: no launch, no poll loop


def test_missing_binary_fails_fast(tmp_path):
    desktop = _helpers.make_fake_app_desktop(
        tmp_path / "apps", exec_cmd="/nonexistent-jiopc/bin/missing-app %U"
    )
    rec = _run_native(
        tmp_path, desktop_file=desktop, process_name="missing-app", launch_timeout_s=2
    )
    assert _helpers.result_of(rec) == "FAIL", rec.detail
    assert rec.duration_ms < 2000


def test_process_never_appearing_fails(tmp_path):
    """Binary exists and runs, but no process ever matches process_name."""
    desktop = _helpers.make_fake_app_desktop(
        tmp_path / "apps", exec_cmd=f'{sys.executable} -c "raise SystemExit(0)"'
    )
    rec = _run_native(
        tmp_path,
        desktop_file=desktop,
        process_name="jiopc-never-appears-xyz",
        launch_timeout_s=2,
    )
    assert _helpers.result_of(rec) == "FAIL", rec.detail


def test_exec_field_codes_do_not_break_launch(tmp_path):
    """%U etc. in Exec= must be stripped before launch (freedesktop spec)."""
    desktop = _helpers.make_fake_app_desktop(
        tmp_path / "apps", exec_cmd=f"{sys.executable} {FAKE_APP} %U %F"
    )
    rec = _run_native(tmp_path, desktop_file=desktop, process_name=FAKE_PROCESS_NAME)
    assert _helpers.result_of(rec) == "PASS", rec.detail
