"""Agent self-monitoring: background thread sampling our own CPU/RSS.

A :class:`SelfWatch` thread samples ``psutil.Process()`` (the agent) plus all
of its descendants every ``interval_s`` (default 0.5 s) into a bounded ring
buffer. Two exclusion sets keep the numbers honest against the < 150 MB /
< 20% CPU budgets, which cover the agent only:

* **apps under test** — Part B registers each launched PID via
  :func:`register_app_pid` and removes it with :func:`unregister_app_pid`;
  the watcher skips those subtrees entirely.
* **the Playwright browser** — Part A may register the Chromium PID via
  :func:`register_browser_pid`; its subtree is accounted separately and
  surfaces in the summary as ``browser_rss_mb``.

``stop()`` returns ``{"peak_rss_mb", "avg_cpu_pct", "browser_rss_mb",
"samples"}`` which runner.py merges into the summary line, making the
benchmark claim self-evidencing in every log.

The module is a bonus layer: runner.py wraps every call defensively, and
``start()`` raising (e.g. psutil missing) merely omits the stats.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any

#: Default sampling cadence (SPEC: every 500 ms).
DEFAULT_INTERVAL_S = 0.5
#: Ring-buffer capacity: 0.5 s cadence * 2400 = 20 min, 4x the 5-min budget.
DEFAULT_MAXLEN = 2400

# ---------------------------------------------------------------------------
# PID registration (module-level so part modules need no watcher handle)
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_app_pids: set[int] = set()
_browser_pids: set[int] = set()


def register_app_pid(pid: int) -> None:
    """Exclude ``pid`` (an app under test) and its subtree from agent stats."""
    with _registry_lock:
        _app_pids.add(int(pid))


def unregister_app_pid(pid: int) -> None:
    """Stop excluding ``pid`` (call after the app has been terminated)."""
    with _registry_lock:
        _app_pids.discard(int(pid))


def register_browser_pid(pid: int) -> None:
    """Track ``pid`` (the Playwright browser) separately as browser_rss_mb."""
    with _registry_lock:
        _browser_pids.add(int(pid))


def _snapshot_registry() -> tuple[set[int], set[int]]:
    with _registry_lock:
        return set(_app_pids), set(_browser_pids)


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


class SelfWatch:
    """Background sampler for the agent's own resource usage.

    Usage (what runner.py does)::

        watcher = SelfWatch()
        watcher.start()
        ...
        stats = watcher.stop()   # {"peak_rss_mb": ..., "avg_cpu_pct": ..., ...}
    """

    def __init__(
        self,
        interval_s: float = DEFAULT_INTERVAL_S,
        maxlen: int = DEFAULT_MAXLEN,
    ) -> None:
        self._interval_s = max(0.1, float(interval_s))
        self._rss_samples: deque[float] = deque(maxlen=maxlen)
        self._cpu_samples: deque[float] = deque(maxlen=maxlen)
        self._browser_rss_samples: deque[float] = deque(maxlen=maxlen)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil: Any = None
        self._primed = False  # first cpu_percent(None) call returns 0.0
        # cpu_percent(interval=None) state lives on the Process *instance*;
        # a fresh instance always reads 0.0, so reuse instances across samples.
        self._proc_cache: dict[int, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start sampling; raises if psutil is unavailable (runner catches)."""
        import psutil  # lazy: missing psutil only disables stats, not the run

        self._psutil = psutil
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="selfwatch", daemon=True
        )
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        """Stop the sampler and return aggregate stats for the summary."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s * 4)
            self._thread = None
        return self._stats()

    # -- sampling ------------------------------------------------------------

    def _loop(self) -> None:
        # Sample once immediately (primes cpu_percent), then on the interval.
        while True:
            try:
                self._sample_once()
            except Exception:  # noqa: BLE001 - monitoring must never crash a run
                pass
            if self._stop_event.wait(self._interval_s):
                return

    def _sample_once(self) -> None:
        psutil = self._psutil
        app_pids, browser_pids = _snapshot_registry()
        me = psutil.Process(os.getpid())

        agent_rss = 0
        agent_cpu = 0.0
        browser_rss = 0
        cpu_valid = self._primed  # discard the priming pass's zero readings

        live_pids: set[int] = set()
        for walked in self._walk_tree(me, app_pids):
            try:
                proc = self._cached_proc(walked)
                live_pids.add(proc.pid)
                in_browser = self._under_any(proc, browser_pids)
                rss = proc.memory_info().rss
                if in_browser:
                    browser_rss += rss
                    continue
                agent_rss += rss
                # interval=None: non-blocking, measures since previous call on
                # this (cached) instance.
                agent_cpu += proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        # Drop cache entries for processes that have exited.
        for pid in set(self._proc_cache) - live_pids:
            del self._proc_cache[pid]

        self._rss_samples.append(agent_rss / (1024 * 1024))
        if cpu_valid:
            self._cpu_samples.append(agent_cpu)
        if browser_pids:
            self._browser_rss_samples.append(browser_rss / (1024 * 1024))
        self._primed = True

    def _cached_proc(self, proc: Any) -> Any:
        """Reuse a prior Process instance for ``proc.pid`` when still valid."""
        cached = self._proc_cache.get(proc.pid)
        if cached is not None:
            try:
                if cached.create_time() == proc.create_time():
                    return cached
            except self._psutil.Error:
                pass
        self._proc_cache[proc.pid] = proc
        return proc

    def _walk_tree(self, root: Any, exclude_pids: set[int]) -> list[Any]:
        """``root`` + recursive children, pruning excluded (app) subtrees."""
        psutil = self._psutil
        procs = [root]
        try:
            children = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return procs
        excluded: set[int] = set()
        for child in children:
            pid = child.pid
            if pid in exclude_pids:
                excluded.add(pid)
                continue
            try:
                ppid = child.ppid()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if ppid in excluded or ppid in exclude_pids:
                excluded.add(pid)  # descendant of an excluded app process
                continue
            procs.append(child)
        return procs

    def _under_any(self, proc: Any, pids: set[int]) -> bool:
        """True if ``proc`` is one of ``pids`` or descends from one of them."""
        if not pids:
            return False
        psutil = self._psutil
        if proc.pid in pids:
            return True
        try:
            parent = proc.parent()
            hops = 0
            while parent is not None and hops < 32:
                if parent.pid in pids:
                    return True
                parent = parent.parent()
                hops += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return False

    # -- aggregation ----------------------------------------------------------

    def _stats(self) -> dict[str, Any]:
        rss = list(self._rss_samples)
        cpu = list(self._cpu_samples)
        browser = list(self._browser_rss_samples)
        return {
            "peak_rss_mb": round(max(rss), 1) if rss else None,
            "avg_cpu_pct": round(sum(cpu) / len(cpu), 1) if cpu else None,
            "browser_rss_mb": round(max(browser), 1) if browser else None,
            "samples": len(rss),
        }
