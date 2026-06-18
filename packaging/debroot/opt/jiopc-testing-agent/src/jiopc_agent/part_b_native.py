"""Part B — native application health checks.

For each configured app: resolve and parse its .desktop file, verify the
binary exists, launch it in a **new session** (so we own the whole process
group), poll until the real process appears, sample RSS/CPU at T+5 s after
detection, then terminate the group SIGTERM → grace → SIGKILL and sweep for
survivors. Zero orphaned processes is a graded checklist item.

Result semantics (SPEC §Part B):

* FAIL      — .desktop/binary missing, or the process never appeared within
              ``launch_timeout_s``.
* DEGRADED  — process appeared but died before the T+5 s sample.
* PASS      — launched, sampled, terminated cleanly.
* ERROR     — the agent could not run the test (e.g. psutil missing).

Overhead accounting (graded): the time the agent spends *scanning* for the
process each poll is measured and subtracted, so ``data.launch_ms`` reflects
the app, not the agent. The raw figure is kept as ``data.poll_overhead_ms``
(methodology in benchmarks/REPORT.md).

Portability: everything here works on macOS for dev (``start_new_session``
uses setsid(2), which POSIX macs have). The only Linux-specific concern is
DISPLAY, which we merely *honour* from the inherited environment and note in
``data`` when unset on Linux — no DISPLAY logic runs on macOS.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from jiopc_agent.config import AgentConfig, NativeApp
from jiopc_agent.results import Result, TestRecord, make_record
from jiopc_agent.runlog import RunLog

#: Sample the app's RSS/CPU this long after the process is detected (SPEC).
SAMPLE_DELAY_S = 5.0
#: cpu_percent measurement window for the T+5s sample (SPEC: interval=1.0).
CPU_SAMPLE_INTERVAL_S = 1.0
#: Linux ``/proc/<pid>/comm`` truncates names to 15 chars; match accordingly.
_COMM_TRUNCATE = 15


# ---------------------------------------------------------------------------
# Defensive collaborators (selfwatch + desktop_entry are other modules)
# ---------------------------------------------------------------------------


def _selfwatch() -> Any | None:
    """The selfwatch module, or None — PID registration is best-effort."""
    try:
        from jiopc_agent import selfwatch

        return selfwatch
    except Exception:  # noqa: BLE001 - bonus layer must never break Part B
        return None


def _exec_argv(desktop_path: Path) -> list[str]:
    """Argv from the .desktop ``Exec=`` line, %-field codes stripped.

    Prefers the shared :mod:`jiopc_agent.desktop_entry` parser (SPEC API:
    ``parse(path) -> dict``, ``exec_argv(entry) -> list[str]``); falls back to
    a minimal internal parse if that module is not available, so Part B has
    no hard dependency on Part C's deliverable.
    """
    try:
        from jiopc_agent import desktop_entry

        return list(desktop_entry.exec_argv(desktop_entry.parse(desktop_path)))
    except ImportError:
        return _fallback_exec_argv(desktop_path)


def _fallback_exec_argv(desktop_path: Path) -> list[str]:
    """Minimal freedesktop Exec= extraction: [Desktop Entry] section only."""
    exec_line = ""
    in_entry = False
    for raw in desktop_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if in_entry and line.startswith("Exec=") and not exec_line:
            exec_line = line[len("Exec="):].strip()
    if not exec_line:
        return []
    argv = shlex.split(exec_line)
    # Strip freedesktop %-field codes (%u %U %f %F %i %c %k ...).
    return [a for a in argv if not (len(a) == 2 and a.startswith("%"))]


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _resolve_desktop_file(cfg: AgentConfig, app: NativeApp) -> Path | None:
    """YAML path first, then the standard applications dirs by basename."""
    candidates: list[Path] = []
    if app.desktop_file is not None:
        candidates.append(app.desktop_file)
        basename = app.desktop_file.name
    else:
        basename = f"{app.process_name}.desktop"
    for apps_dir in cfg.agent.paths.applications_dirs:
        candidates.append(apps_dir / basename)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _resolve_binary(argv: list[str]) -> tuple[str | None, list[str]]:
    """Resolve argv[0] (skipping an ``env VAR=...`` prefix) to an executable.

    Returns ``(resolved_path_or_None, effective_argv)``.
    """
    eff = list(argv)
    if eff and Path(eff[0]).name == "env":
        eff = eff[1:]
        while eff and "=" in eff[0] and not eff[0].startswith(("/", ".")):
            eff = eff[1:]
    if not eff:
        return None, eff
    target = eff[0]
    if os.path.isabs(target):
        return target if os.path.isfile(target) and os.access(target, os.X_OK) else None, eff
    return shutil.which(target), eff


# ---------------------------------------------------------------------------
# Process detection / group management (psutil passed in, imported once)
# ---------------------------------------------------------------------------


def _name_matches(process_name: str, candidate: str) -> bool:
    want = process_name.lower()
    got = candidate.lower()
    if got == want:
        return True
    # Linux comm truncation: "gcompris-qt" may report as a 15-char prefix.
    return len(got) >= _COMM_TRUNCATE and want.startswith(got)


def _proc_matches(psutil: Any, proc: Any, process_name: str) -> bool:
    try:
        if _name_matches(process_name, proc.name()):
            return True
        exe = proc.exe()
        return bool(exe) and _name_matches(process_name, Path(exe).name)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _find_process(
    psutil: Any, child_pid: int, process_name: str, launched_at: float
) -> Any | None:
    """Our child's tree first, then any system process newer than the launch."""
    try:
        root = psutil.Process(child_pid)
        for proc in [root, *root.children(recursive=True)]:
            if _proc_matches(psutil, proc, process_name):
                return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass  # child already gone; it may have re-exec'd or forked away
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if proc.create_time() >= launched_at - 1.0 and _proc_matches(
                psutil, proc, process_name
            ):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None


def _group_members(psutil: Any, pgid: int) -> list[Any]:
    """Live processes belonging to process group ``pgid``."""
    members: list[Any] = []
    for proc in psutil.process_iter([]):
        try:
            if os.getpgid(proc.pid) == pgid:
                members.append(proc)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return members


def _is_alive(psutil: Any, proc: Any) -> bool:
    """True for a genuinely running process; zombies count as dead.

    Our direct child is a zombie between termination and the ``popen.wait()``
    reap — ``is_running()`` alone would misreport it as a SIGTERM survivor.
    """
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_proc(psutil: Any, proc: Any, sig: int) -> None:
    try:
        proc.send_signal(sig)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _terminate_launch(
    psutil: Any, pgid: int, matched: Any | None, grace_s: float
) -> list[str]:
    """SIGTERM the group (and the matched process if it escaped the group),
    wait up to ``grace_s``, SIGKILL survivors. Returns names of processes
    that survived SIGTERM and needed SIGKILL (the per-launch orphan sweep).
    """

    def targets() -> list[Any]:
        procs = _group_members(psutil, pgid)
        if matched is not None and _is_alive(psutil, matched):
            if all(p.pid != matched.pid for p in procs):
                procs.append(matched)
        return procs

    _signal_group(pgid, signal.SIGTERM)
    if matched is not None:
        _kill_proc(psutil, matched, signal.SIGTERM)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        live = [p for p in targets() if _is_alive(psutil, p)]
        if not live:
            return []
        time.sleep(0.2)

    survivors = [p for p in targets() if _is_alive(psutil, p)]
    names: list[str] = []
    for proc in survivors:
        try:
            names.append(f"{proc.name()}({proc.pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            names.append(f"pid {proc.pid}")
    _signal_group(pgid, signal.SIGKILL)
    for proc in survivors:
        _kill_proc(psutil, proc, signal.SIGKILL)
    time.sleep(0.2)  # let the kernel reap before the caller re-checks
    return names


def _final_sweep(psutil: Any, pgids: list[int]) -> list[str]:
    """End-of-part sweep: SIGKILL anything still alive from our launches."""
    leftovers: list[str] = []
    for pgid in pgids:
        for proc in _group_members(psutil, pgid):
            if not _is_alive(psutil, proc):
                continue
            try:
                leftovers.append(f"{proc.name()}({proc.pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                leftovers.append(f"pid {proc.pid}")
            _kill_proc(psutil, proc, signal.SIGKILL)
        _signal_group(pgid, signal.SIGKILL)
    return leftovers


# ---------------------------------------------------------------------------
# Single app test
# ---------------------------------------------------------------------------


def _test_app(
    psutil: Any, cfg: AgentConfig, app: NativeApp, launched_pgids: list[int]
) -> TestRecord:
    """Run the full launch→detect→sample→terminate cycle for one app."""
    test = f"native:{app.name}"
    started = time.perf_counter()
    data: dict[str, Any] = {}

    def record(result: Result, detail: str) -> TestRecord:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return make_record("B", test, result, duration_ms, detail, data)

    # 1) .desktop resolution + Exec parse + binary check (FAIL fast).
    desktop_path = _resolve_desktop_file(cfg, app)
    if desktop_path is None:
        return record(Result.FAIL, "no .desktop file found (YAML path or standard dirs)")
    data["desktop_file"] = str(desktop_path)

    try:
        argv = _exec_argv(desktop_path)
    except Exception as exc:  # noqa: BLE001 - unparseable file is a test failure
        return record(Result.FAIL, f"cannot parse {desktop_path.name}: {exc}")
    if not argv:
        return record(Result.FAIL, f"{desktop_path.name} has no usable Exec= line")

    binary, argv = _resolve_binary(argv)
    if binary is None or not argv:
        missing = argv[0] if argv else "<empty>"
        return record(Result.FAIL, f"Exec binary not found/executable: {missing}")
    data["binary"] = binary
    data["argv"] = argv

    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        data["display"] = "unset"  # GUI launch will likely fail; flag for the LLM

    # 2) Launch in a new session — we own the whole process group for cleanup.
    launched_at = time.time()
    try:
        popen = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=dict(os.environ),  # DISPLAY (when present) is honoured
        )
    except OSError as exc:
        return record(Result.FAIL, f"launch failed: {exc}")

    pgid = popen.pid  # new session ⇒ the child is its own group leader
    launched_pgids.append(pgid)
    watch = _selfwatch()
    if watch is not None:
        watch.register_app_pid(popen.pid)

    matched: Any | None = None
    try:
        # 3) Poll for the real process; measure our own scanning overhead.
        poll_s = cfg.agent.poll_interval_ms / 1000.0
        t_launch = time.perf_counter()
        deadline = t_launch + app.launch_timeout_s
        overhead_s = 0.0
        while True:
            scan_start = time.perf_counter()
            matched = _find_process(psutil, popen.pid, app.process_name, launched_at)
            overhead_s += time.perf_counter() - scan_start
            if matched is not None:
                detected = time.perf_counter()
                break
            if time.perf_counter() >= deadline:
                break
            time.sleep(min(poll_s, max(0.0, deadline - time.perf_counter())))

        data["poll_overhead_ms"] = int(overhead_s * 1000)
        if matched is None:
            rc = popen.poll()
            hint = f" (launcher exited rc={rc})" if rc is not None else ""
            return record(
                Result.FAIL,
                f"process '{app.process_name}' not found within "
                f"{app.launch_timeout_s:g}s{hint}",
            )

        # Reported launch time excludes the agent's own polling cost (graded).
        launch_ms = max(0, int((detected - t_launch - overhead_s) * 1000))
        data["launch_ms"] = launch_ms
        data["pid"] = matched.pid

        # 4) T+5s health sample.
        time.sleep(SAMPLE_DELAY_S)
        died = False
        try:
            died = (not matched.is_running()) or (
                matched.status() == psutil.STATUS_ZOMBIE
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            died = True
        except psutil.AccessDenied:
            pass  # alive, just unreadable — sampling below will report it
        if died:
            return record(
                Result.DEGRADED,
                f"launched in {launch_ms}ms but died before the T+{SAMPLE_DELAY_S:g}s sample",
            )

        try:
            rss_mb = round(matched.memory_info().rss / (1024 * 1024), 1)
            cpu_pct = round(matched.cpu_percent(interval=CPU_SAMPLE_INTERVAL_S), 1)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return record(
                Result.DEGRADED,
                f"launched in {launch_ms}ms but died during the T+{SAMPLE_DELAY_S:g}s sample",
            )
        except psutil.AccessDenied:
            rss_mb = cpu_pct = None  # type: ignore[assignment]
        data["rss_mb"] = rss_mb
        data["cpu_pct"] = cpu_pct

        return record(
            Result.PASS,
            f"launched in {launch_ms}ms, healthy at T+{SAMPLE_DELAY_S:g}s "
            f"(rss {rss_mb} MB, cpu {cpu_pct}%)",
        )
    finally:
        # 5) Cleanup runs on EVERY path: SIGTERM group → grace → SIGKILL.
        orphans = _terminate_launch(psutil, pgid, matched, cfg.agent.term_grace_s)
        if orphans:
            data["warning"] = "survivors after SIGTERM grace; SIGKILLed"
            data["orphans_killed"] = orphans
        try:
            popen.wait(timeout=1.0)  # reap our direct child, no zombie left
        except (subprocess.TimeoutExpired, OSError):
            pass
        if watch is not None:
            watch.unregister_app_pid(popen.pid)


# ---------------------------------------------------------------------------
# Part entry point (binding contract)
# ---------------------------------------------------------------------------


def run_part_b(cfg: AgentConfig, log: RunLog) -> list[TestRecord]:
    """Run all Part B tests, appending each record to ``log`` live."""
    records: list[TestRecord] = []
    try:
        import psutil
    except ModuleNotFoundError:
        for app in cfg.native_apps:
            rec = make_record(
                "B",
                f"native:{app.name}",
                Result.ERROR,
                0,
                "psutil is not installed; run 'pip install psutil' "
                "(or 'apt install python3-psutil' on Ubuntu)",
            )
            log.record(rec)
            records.append(rec)
        return records

    launched_pgids: list[int] = []
    for i, app in enumerate(cfg.native_apps):
        if i > 0 and cfg.agent.cooldown_s > 0:
            # Cooldown so one app's shutdown cannot pollute the next app's
            # launch_ms / CPU samples (rationale in benchmarks/REPORT.md).
            time.sleep(cfg.agent.cooldown_s)
        try:
            rec = _test_app(psutil, cfg, app, launched_pgids)
        except Exception as exc:  # noqa: BLE001 - per-test failures never crash the run
            rec = make_record(
                "B",
                f"native:{app.name}",
                Result.ERROR,
                0,
                f"agent error during test: {type(exc).__name__}: {exc}",
            )
        log.record(rec)
        records.append(rec)

    # End-of-part orphan sweep: nothing we launched may outlive the run.
    leftovers = _final_sweep(psutil, launched_pgids)
    if leftovers:
        rec = make_record(
            "B",
            "native:orphan_sweep",
            Result.ERROR,
            0,
            f"orphaned process(es) survived per-app cleanup and were SIGKILLed: "
            f"{', '.join(leftovers)}",
            {"orphans_killed": leftovers},
        )
        log.record(rec)
        records.append(rec)
    return records
