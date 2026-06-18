"""Part C — desktop & start-menu presence checks (read-only, < 30 s).

Strategy (perf budget): each source directory is scanned exactly **once**
up front into an in-memory index, then every configured app is evaluated
against the index — no per-app filesystem walks.

Per app, two records (so the summary can pinpoint which dimension broke):

* ``presence:<App>:desktop_folder`` — the ``.desktop`` file (or a symlink)
  must sit under ``<desktop_dir>/<expected_folder>/``. Present elsewhere on
  the desktop → MISPLACED; absent everywhere → MISSING.
* ``presence:<App>:start_menu`` — the system ``.desktop`` entry's
  ``Categories=`` must contain the expected category. Entry absent →
  MISSING; present with wrong/absent category → MISPLACED.

Nothing is launched and nothing is written; all paths come from the YAML
(``agent.paths``), so tests point this at a fixture tree.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from jiopc_agent import desktop_entry
from jiopc_agent.config import AgentConfig, PresenceApp
from jiopc_agent.results import Result, TestRecord, make_record
from jiopc_agent.runlog import RunLog

#: Display name for a .desktop file sitting directly in the desktop root.
_ROOT_FOLDER_LABEL = "(desktop root)"


# ---------------------------------------------------------------------------
# Index building (single scan per source)
# ---------------------------------------------------------------------------


def _index_applications(dirs: tuple[Path, ...]) -> dict[str, Path]:
    """Map ``.desktop`` basename → path across the applications dirs.

    Directories are scanned in YAML order; the first occurrence of an id
    wins (the order therefore expresses precedence). Unreadable or missing
    directories are skipped — on the dev mac ``/usr/share/applications``
    typically does not exist.
    """
    index: dict[str, Path] = {}
    for directory in dirs:
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.name.endswith(".desktop") and entry.name not in index:
                        index[entry.name] = Path(entry.path)
        except OSError:
            continue
    return index


def _index_desktop(root: Path) -> dict[str, list[str]]:
    """Map ``.desktop`` basename → list of folders (relative to ``root``).

    One ``os.walk`` over the desktop tree. Symlinked ``.desktop`` files are
    included (they appear in ``filenames``); a file directly in the root is
    recorded with folder ``""``.
    """
    found: dict[str, list[str]] = {}
    if not root.is_dir():
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        folder = "" if rel == "." else rel
        for name in filenames:
            if name.endswith(".desktop"):
                found.setdefault(name, []).append(folder)
    return found


# ---------------------------------------------------------------------------
# Per-app checks
# ---------------------------------------------------------------------------


def _check_desktop_folder(
    app: PresenceApp,
    apps_index: dict[str, Path],
    desktop_index: dict[str, list[str]],
    desktop_dir: Path,
) -> TestRecord:
    """Evaluate the on-desktop placement of one app against the indexes."""
    started = time.monotonic()
    folders = desktop_index.get(app.desktop_id)
    in_app_dirs = app.desktop_id in apps_index
    data: dict = {
        "desktop_id": app.desktop_id,
        "expected": app.desktop_folder,
        "found": folders or [],
        "in_applications_dirs": in_app_dirs,
    }

    if folders is None:
        if in_app_dirs:
            detail = (
                f"{app.desktop_id} exists in applications dirs but has no "
                f"shortcut anywhere under {desktop_dir}"
            )
        else:
            detail = (
                f"{app.desktop_id} not found in applications dirs or under "
                f"{desktop_dir}"
            )
        result = Result.MISSING
    elif app.desktop_folder in folders:
        result = Result.PASS
        detail = f"{app.desktop_id} present in Desktop/{app.desktop_folder}/"
    else:
        result = Result.MISPLACED
        shown = ", ".join(f or _ROOT_FOLDER_LABEL for f in folders)
        detail = (
            f"{app.desktop_id} found in {shown}, "
            f"expected Desktop/{app.desktop_folder}/"
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    return make_record(
        "C", f"presence:{app.name}:desktop_folder", result, duration_ms, detail, data
    )


def _check_start_menu(
    app: PresenceApp,
    apps_index: dict[str, Path],
    applications_dirs: tuple[Path, ...],
) -> TestRecord:
    """Evaluate the start-menu category of one app's system ``.desktop``."""
    started = time.monotonic()
    path = apps_index.get(app.desktop_id)
    data: dict = {"desktop_id": app.desktop_id, "expected": app.start_menu_category}

    if path is None:
        result = Result.MISSING
        searched = ", ".join(str(d) for d in applications_dirs)
        detail = f"{app.desktop_id} not found in [{searched}]"
        data["found"] = []
    else:
        data["desktop_file"] = str(path)
        cats = desktop_entry.categories(desktop_entry.parse(path))
        data["found"] = sorted(cats)
        if app.start_menu_category in cats:
            result = Result.PASS
            detail = (
                f"{app.desktop_id} Categories= contains "
                f"{app.start_menu_category}"
            )
        else:
            result = Result.MISPLACED
            shown = ";".join(sorted(cats)) or "<empty>"
            detail = (
                f"{app.desktop_id} Categories= is {shown}, "
                f"expected to contain {app.start_menu_category}"
            )

    duration_ms = int((time.monotonic() - started) * 1000)
    return make_record(
        "C", f"presence:{app.name}:start_menu", result, duration_ms, detail, data
    )


# ---------------------------------------------------------------------------
# Part entry point (binding contract — see SPEC §Part contracts)
# ---------------------------------------------------------------------------


def run_part_c(cfg: AgentConfig, log: RunLog) -> list[TestRecord]:
    """Run all presence checks; two records per app, appended live to ``log``.

    Per-test exceptions become ERROR records — this function never raises.
    """
    paths = cfg.agent.paths

    scan_started = time.monotonic()
    apps_index = _index_applications(paths.applications_dirs)
    desktop_index = _index_desktop(paths.desktop_dir)
    index_scan_ms = int((time.monotonic() - scan_started) * 1000)

    records: list[TestRecord] = []
    first = True
    for app in cfg.desktop_presence:
        checks = (
            ("desktop_folder", lambda a=app: _check_desktop_folder(
                a, apps_index, desktop_index, paths.desktop_dir
            )),
            ("start_menu", lambda a=app: _check_start_menu(
                a, apps_index, paths.applications_dirs
            )),
        )
        for dim, check in checks:
            try:
                rec = check()
            except Exception as exc:  # noqa: BLE001 - never crash the run
                rec = make_record(
                    "C",
                    f"presence:{app.name}:{dim}",
                    Result.ERROR,
                    0,
                    f"presence check failed: {type(exc).__name__}: {exc}",
                    {"desktop_id": app.desktop_id},
                )
            if first:  # evidence for the < 30 s Part C budget in every log
                rec.data["index_scan_ms"] = index_scan_ms
                first = False
            log.record(rec)
            records.append(rec)
    return records
