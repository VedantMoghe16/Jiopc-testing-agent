"""part_c_presence.py — desktop folder + start-menu structural checks.

Runs against the static fixture tree (tests/fixtures/desktop_tree) which
encodes PASS, MISSING and MISPLACED cases; see fixture-config.yaml comments.
"""

from __future__ import annotations

import io
import time

import pytest

import _helpers
from conftest import DESKTOP_TREE

part_c = pytest.importorskip(
    "jiopc_agent.part_c_presence", reason="spine module part_c_presence.py not implemented yet"
)

from jiopc_agent.runlog import RunLog  # noqa: E402


PRESENCE_YAML = """
agent:
  log_dir: __LOG_DIR__
  paths:
    applications_dirs: [__TREE__/applications]
    desktop_dir: __TREE__/Desktop
desktop_presence:
  - {name: Chess,  desktop_id: org.fixture.Chess.desktop, desktop_folder: Games,        start_menu_category: Game}
  - {name: Writer, desktop_id: fixture-writer.desktop,    desktop_folder: Productivity, start_menu_category: Office}
  - {name: Paint,  desktop_id: fixture-paint.desktop,     desktop_folder: Education,    start_menu_category: Graphics}
  - {name: Calc,   desktop_id: fixture-calc.desktop,      desktop_folder: Productivity, start_menu_category: Office}
  - {name: Ghost,  desktop_id: fixture-ghost.desktop,     desktop_folder: Games,        start_menu_category: Game}
"""


@pytest.fixture(scope="module")
def part_c_run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("part_c")
    yaml_text = PRESENCE_YAML.replace("__LOG_DIR__", str(tmp_path / "logs")).replace(
        "__TREE__", str(DESKTOP_TREE)
    )
    cfg = _helpers.load_config_text(tmp_path, yaml_text)
    log = RunLog(cfg.agent.log_dir, tee=io.StringIO())
    started = time.monotonic()
    records = part_c.run_part_c(cfg, log)
    elapsed = time.monotonic() - started
    log.close()
    return _helpers.records_by_test(records), records, elapsed, log


def test_two_records_per_app(part_c_run):
    by_test, records, _, _ = part_c_run
    assert len(records) == 10  # 5 apps x 2 dimensions
    for app in ("Chess", "Writer", "Paint", "Calc", "Ghost"):
        assert f"presence:{app}:desktop_folder" in by_test
        assert f"presence:{app}:start_menu" in by_test
    assert all(rec.component == "C" for rec in records)


def test_correct_app_passes_both(part_c_run):
    by_test = part_c_run[0]
    for app in ("Chess", "Writer"):
        assert _helpers.result_of(by_test[f"presence:{app}:desktop_folder"]) == "PASS"
        assert _helpers.result_of(by_test[f"presence:{app}:start_menu"]) == "PASS"


def test_wrong_desktop_folder_is_misplaced(part_c_run):
    by_test = part_c_run[0]
    rec = by_test["presence:Paint:desktop_folder"]
    assert _helpers.result_of(rec) == "MISPLACED"
    # detail says found vs expected (SPEC §Part C)
    detail = rec.detail.lower()
    assert "games" in detail and "education" in detail
    # the other dimension is independent and fine
    assert _helpers.result_of(by_test["presence:Paint:start_menu"]) == "PASS"


def test_wrong_category_is_misplaced(part_c_run):
    by_test = part_c_run[0]
    assert _helpers.result_of(by_test["presence:Calc:desktop_folder"]) == "PASS"
    assert _helpers.result_of(by_test["presence:Calc:start_menu"]) == "MISPLACED"


def test_absent_app_is_missing_not_misplaced(part_c_run):
    by_test = part_c_run[0]
    assert _helpers.result_of(by_test["presence:Ghost:desktop_folder"]) == "MISSING"
    assert _helpers.result_of(by_test["presence:Ghost:start_menu"]) == "MISSING"


def test_records_are_logged_live(part_c_run):
    """Each record is appended to the log as the part runs (live tee)."""
    import json

    _, records, _, log = part_c_run
    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    logged = [line for line in lines if line.get("type") == "record"]
    assert len(logged) == len(records)


def test_part_c_is_fast(part_c_run):
    elapsed = part_c_run[2]
    assert elapsed < 30, f"Part C budget is < 30 s; took {elapsed:.1f}s"


def test_symlinked_desktop_shortcut_passes(tmp_path):
    """A Desktop shortcut that is a symlink to the system entry still counts."""
    apps = tmp_path / "applications"
    desktop = tmp_path / "Desktop" / "Games"
    apps.mkdir()
    desktop.mkdir(parents=True)
    target = apps / "fixture-link.desktop"
    target.write_text(
        "[Desktop Entry]\nType=Application\nName=Link\nExec=link\nCategories=Game;\n",
        encoding="utf-8",
    )
    (desktop / "fixture-link.desktop").symlink_to(target)

    cfg = _helpers.load_config_text(
        tmp_path,
        f"""
        agent:
          log_dir: {tmp_path / 'logs'}
          paths:
            applications_dirs: [{apps}]
            desktop_dir: {tmp_path / 'Desktop'}
        desktop_presence:
          - {{name: Link, desktop_id: fixture-link.desktop, desktop_folder: Games, start_menu_category: Game}}
        """,
    )
    with RunLog(cfg.agent.log_dir, tee=io.StringIO()) as log:
        by_test = _helpers.records_by_test(part_c.run_part_c(cfg, log))
    assert _helpers.result_of(by_test["presence:Link:desktop_folder"]) == "PASS"
    assert _helpers.result_of(by_test["presence:Link:start_menu"]) == "PASS"
