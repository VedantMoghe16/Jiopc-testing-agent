# Benchmark Report

## Hardware Profiles
- **Dev Machine (macOS)**: 12-core CPU, 32GB RAM (M-series).
- **Target VM (Ubuntu 24.04 + LxQt)**: 4 vCPU @ 2.45 GHz, 8 GB RAM.

## Methodology
- **Agent Resource Usage**: The agent records its own peak VmRSS and average CPU% during runs via the `selfwatch.py` module.
- **Duration**: Full run duration is timed using the `time` command (`/usr/bin/time -v` on Linux).
- **Part B Overhead**: The agent uses `psutil` to poll process presence every 500ms. Overhead is calculated by measuring the time spent in the polling loop versus waiting.

## Results (macOS Dev-Run)
*Note: These are baseline metrics from the macOS development environment.*
- **Agent CPU usage (sustained)**: < 5.0%
- **Agent RAM footprint**: ~65 MB (Peak)
- **Full test run duration**: ~45 seconds (Fixture-based)
- **Part C duration**: < 1 second

## Results (Ubuntu VM)
*Source: `benchmarks/run_benchmarks.sh` on the target Ubuntu 24.04 + LxQt VM,
5 full runs + 5 isolated Part C runs (artifacts `results-2026-06-17T20-57-51`).*

| Metric | p50 | p95 | Target |
|---|---|---|---|
| Full run wall time (s) | 68.3 | 68.6 | < 300 |
| Part C wall time (s) | 0.1 | 0.1 | < 30 |
| Agent peak RSS (MB) | 42.3 | 42.3 | < 150 |
| Agent avg CPU (% of one vCPU) | 6.5 | 6.7 | < 20 |

All four resource/timing budgets pass with wide margin. Exit code is `1` on
every full run by design: Part A includes live public web targets, and at least
one (JioCloud — blank page, no `domcontentloaded`) is a genuine failure the
agent correctly reports, so the run ends in a HOLD recommendation rather than a
manufactured all-green. See `design.md` for the live-target rationale.

*Note: an earlier run measured 80.2s p50 / 124.5s p95. The drop to ~68s
followed the Part A selector fixes — matching selectors resolve immediately
instead of consuming the full `element_timeout_ms` on every miss.*

## Part B Application Overhead
Measured per-app polling overhead on the VM run was `~1–17 ms` (psutil process
tree traversal accumulated across the 500 ms poll cycles).

Per-app figures (newest full-run log, `results-2026-06-17T20-57-51`):

| Test | Result | launch_ms | overhead_ms | rss_mb | cpu_pct |
|---|---|---|---|---|---|
| native:Firefox | DEGRADED | 0 | 1 | - | - |
| native:Files | PASS | 0 | 17 | 124.8 | 0.0 |
| native:Text Editor | PASS | 0 | 4 | 123.1 | 0.0 |
| native:Terminal | PASS | 0 | 5 | 120.5 | 0.0 |
| native:LibreOffice Writer | PASS | 501 | 17 | 313.5 | 0.0 |
| native:VLC | PASS | 0 | 6 | 124.7 | 0.0 |
| native:GCompris | PASS | 0 | 4 | 268.0 | 1.0 |

- **Overhead Subtraction**: When recording `launch_ms`, the agent measures the exact time spent executing `psutil` lookups and subtracts this accumulated overhead from the wall-clock duration.
- **False DEGRADED Mitigation**: The 2-second `cooldown_s` ensures that intense I/O from a terminating app's teardown does not starve the CPU for the subsequent app's launch.
- **Firefox DEGRADED**: launches then exits before the T+5s sample (snap/wrapper hands off to a detached process). `DEGRADED` is not in `fail_on`, so it does not affect the exit code.
