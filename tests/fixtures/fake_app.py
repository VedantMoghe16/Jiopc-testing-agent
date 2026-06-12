#!/usr/bin/env python3
"""Long-running fake "native app" used by the Part B tests.

Behaviour (per SPEC §Tests: "sleeps, ignores nothing, names itself"):

* names itself — on start it copies the *running* interpreter binary to
  ``jiopc-fake-app`` and re-execs through the copy, so psutil sees a process
  whose name AND exe basename are ``jiopc-fake-app``. A copy (not a symlink)
  is required on macOS, where the kernel proc name resolves symlinks; it also
  works on Linux. ``PYTHONHOME`` is pinned so the relocated binary still finds
  its stdlib. The copy lives in ``$FAKE_APP_LINK_DIR`` (tests point it at a
  pytest tmp dir; default: a fresh temp dir). If renaming fails for any
  reason the app still runs un-renamed — tests that need the name fail loudly.
* sleeps — stays alive for ``$FAKE_APP_SECONDS`` (default 120) seconds in a
  short-interval sleep loop, then exits 0. A small value (< 5) simulates the
  launched-then-died DEGRADED case.
* ignores nothing — no signal handlers are installed, so SIGTERM/SIGKILL
  from the agent's process-group cleanup work normally.

Only the pre-exec phase may import third-party modules (psutil, to find the
real binary behind macOS launcher stubs); the renamed process is pure stdlib.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

PROCESS_NAME = "jiopc-fake-app"
RENAMED_FLAG = "--renamed"


def _running_binary() -> str:
    """Path of the binary actually executing us.

    On macOS, homebrew/framework pythons re-exec through a launcher stub, so
    ``sys.executable`` is NOT the running binary; psutil knows the truth.
    """
    try:
        import psutil

        exe = psutil.Process().exe()
        if exe:
            return exe
    except Exception:  # noqa: BLE001 - any failure → best-effort fallback
        pass
    return os.path.realpath(sys.executable)


def _reexec_with_name() -> None:
    """Copy the interpreter to PROCESS_NAME and exec through it."""
    target_dir = os.environ.get("FAKE_APP_LINK_DIR")
    if not target_dir:
        target_dir = tempfile.mkdtemp(prefix="jiopc-fake-app-")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, PROCESS_NAME)
    if not os.path.exists(target):
        shutil.copy2(_running_binary(), target)
    # the relocated binary cannot derive the stdlib location from its path
    os.environ["PYTHONHOME"] = sys.base_prefix
    os.execv(target, [target, os.path.abspath(__file__), RENAMED_FLAG])


def main(argv: list[str]) -> int:
    if RENAMED_FLAG not in argv:
        try:
            _reexec_with_name()
        except OSError:
            pass  # run un-renamed; tests that need the name will fail loudly
    lifetime = float(os.environ.get("FAKE_APP_SECONDS", "120"))
    deadline = time.monotonic() + lifetime
    while time.monotonic() < deadline:
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
