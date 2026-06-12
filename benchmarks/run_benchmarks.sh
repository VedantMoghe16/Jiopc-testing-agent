#!/usr/bin/env bash
# run_benchmarks.sh — reproducible benchmark methodology for the JioPC agent.
#
# What it measures (SPEC §Benchmarks):
#   * RUNS (default 5) timed FULL runs  -> wall-clock p50/p95
#       - on Linux each run is wrapped in `/usr/bin/time -v` (peak RSS of the
#         whole pipeline as a cross-check); macOS falls back to plain timing.
#   * agent CPU / RSS from the selfwatch figures in each run's JSONL summary
#     (agent_peak_rss_mb / agent_avg_cpu_pct) -> p50/p95.
#   * Part C isolated timing (RUNS runs of `--part C`) vs the < 30 s budget.
#   * Part B per-app overhead extraction: launch_ms and any polling-overhead
#     fields found in component-B records of the latest full-run log.
#
# Usage:
#   bash benchmarks/run_benchmarks.sh [CONFIG_YAML]
# Env:
#   RUNS=5            number of full runs (and Part C runs)
#   AGENT_CMD=...     override the agent command (default: auto-detect
#                     `jiopc-agent` on PATH, else `python3 -m jiopc_agent`
#                     from the repo root)
#
# Output: a markdown results table on stdout (paste into benchmarks/REPORT.md)
# and raw per-run artifacts under benchmarks/results-<timestamp>/.
# Test failures (agent exit 1) do NOT abort the benchmark — exit codes are
# recorded; only exit 2 (config error) aborts.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
CONFIG="${1:-${REPO_ROOT}/jiopc-agent.yaml}"
RUNS="${RUNS:-5}"
STAMP="$(date +%Y-%m-%dT%H-%M-%S)"
OUT_DIR="${SCRIPT_DIR}/results-${STAMP}"
mkdir -p "${OUT_DIR}"

if [[ ! -f "${CONFIG}" ]]; then
    echo "ERROR: config not found: ${CONFIG}" >&2
    exit 2
fi

PYTHON="${PYTHON:-python3}"

# ------------------------------------------------------------ agent cmd ---
if [[ -n "${AGENT_CMD:-}" ]]; then
    : # caller override
elif command -v jiopc-agent >/dev/null 2>&1; then
    AGENT_CMD="jiopc-agent"
else
    AGENT_CMD="env PYTHONPATH=${REPO_ROOT}/src ${PYTHON} -m jiopc_agent"
fi
echo "agent command: ${AGENT_CMD}" >&2
echo "config:        ${CONFIG}" >&2
echo "runs:          ${RUNS}" >&2

# ------------------------------------------------------------- log dir ----
LOG_DIR="$(${PYTHON} - "${CONFIG}" <<'PYEOF'
import sys
from pathlib import Path
try:
    import yaml
    cfg = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
    log_dir = (cfg.get("agent") or {}).get("log_dir") or "~/.local/share/jiopc/agent/"
except Exception:
    log_dir = "~/.local/share/jiopc/agent/"
print(Path(log_dir).expanduser())
PYEOF
)"
echo "log dir:       ${LOG_DIR}" >&2

# /usr/bin/time -v exists on Linux (GNU time); macOS `time -v` does not.
GNU_TIME=""
if /usr/bin/time -v true >/dev/null 2>&1; then
    GNU_TIME="/usr/bin/time -v"
fi

newest_log() {
    # Newest test_run_*.log in LOG_DIR (empty string if none).
    ls -1t "${LOG_DIR}"/test_run_*.log 2>/dev/null | head -n1 || true
}

# One run; args: <label> <wall-file> <extra agent args...>
timed_run() {
    local label="$1" wall_file="$2"
    shift 2
    local t0 t1 rc=0
    t0="$(${PYTHON} -c 'import time; print(time.monotonic())')"
    if [[ -n "${GNU_TIME}" ]]; then
        # shellcheck disable=SC2086  # AGENT_CMD is intentionally word-split
        ${GNU_TIME} -o "${OUT_DIR}/${label}.time" \
            ${AGENT_CMD} --config "${CONFIG}" --no-email "$@" \
            >"${OUT_DIR}/${label}.stdout" 2>"${OUT_DIR}/${label}.stderr" || rc=$?
    else
        # shellcheck disable=SC2086
        ${AGENT_CMD} --config "${CONFIG}" --no-email "$@" \
            >"${OUT_DIR}/${label}.stdout" 2>"${OUT_DIR}/${label}.stderr" || rc=$?
    fi
    t1="$(${PYTHON} -c 'import time; print(time.monotonic())')"
    if [[ ${rc} -eq 2 ]]; then
        echo "ERROR: agent exited 2 (config/usage error) on ${label}; see ${OUT_DIR}/${label}.stderr" >&2
        exit 2
    fi
    ${PYTHON} -c "print(f'{${t1}-${t0}:.3f}')" >"${wall_file}"
    echo "${rc}" >"${OUT_DIR}/${label}.exitcode"
    echo "  ${label}: wall=$(cat "${wall_file}")s exit=${rc}" >&2
}

# --------------------------------------------------------- 1. full runs ---
echo "== ${RUNS} full runs ==" >&2
: >"${OUT_DIR}/full_logs.list"
for i in $(seq 1 "${RUNS}"); do
    timed_run "full-${i}" "${OUT_DIR}/full-${i}.wall"
    newest_log >>"${OUT_DIR}/full_logs.list"
done

# --------------------------------------------------- 2. Part C isolated ---
echo "== ${RUNS} Part C isolated runs ==" >&2
for i in $(seq 1 "${RUNS}"); do
    timed_run "partc-${i}" "${OUT_DIR}/partc-${i}.wall" --part C
done

# ------------------------------------------- 3. crunch numbers (python) ---
${PYTHON} - "${OUT_DIR}" "${RUNS}" <<'PYEOF'
"""Aggregate wall times, selfwatch summaries and Part B records -> markdown."""
import json
import statistics
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
runs = int(sys.argv[2])


def pctl(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1))))
    return xs[k]


def walls(prefix: str) -> list[float]:
    out = []
    for i in range(1, runs + 1):
        f = out_dir / f"{prefix}-{i}.wall"
        if f.exists():
            out.append(float(f.read_text().strip()))
    return out


full_walls = walls("full")
partc_walls = walls("partc")

# Per-run summary lines from the agent's own JSONL logs (selfwatch evidence).
peak_rss, avg_cpu, durations, exit_codes = [], [], [], []
log_paths = []
list_file = out_dir / "full_logs.list"
if list_file.exists():
    log_paths = [Path(p) for p in list_file.read_text().splitlines() if p.strip()]
for lp in dict.fromkeys(log_paths):  # dedupe, keep order
    try:
        lines = lp.read_text().splitlines()
    except OSError:
        continue
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "summary":
            if isinstance(obj.get("agent_peak_rss_mb"), (int, float)):
                peak_rss.append(float(obj["agent_peak_rss_mb"]))
            if isinstance(obj.get("agent_avg_cpu_pct"), (int, float)):
                avg_cpu.append(float(obj["agent_avg_cpu_pct"]))
            if isinstance(obj.get("duration_s"), (int, float)):
                durations.append(float(obj["duration_s"]))
            if obj.get("exit_code") is not None:
                exit_codes.append(obj["exit_code"])
            break

# Part B per-app overhead from the newest full-run log.
partb_rows = []
if log_paths:
    for line in log_paths[-1].read_text().splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "record" and obj.get("component") == "B":
            data = obj.get("data") or {}
            overhead = next(
                (data[k] for k in data if "overhead" in k.lower()), None
            )
            partb_rows.append(
                (
                    obj.get("test", "?"),
                    obj.get("result", "?"),
                    obj.get("duration_ms"),
                    data.get("launch_ms"),
                    overhead,
                    data.get("rss_mb"),
                    data.get("cpu_pct"),
                )
            )


def fmt(v, suffix=""):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}{suffix}"
    return f"{v}{suffix}"


lines = []
lines.append(f"## Benchmark results ({out_dir.name})")
lines.append("")
lines.append("| Metric | p50 | p95 | Target | n |")
lines.append("|---|---|---|---|---|")
lines.append(
    f"| Full run wall time (s) | {fmt(pctl(full_walls, 50))} "
    f"| {fmt(pctl(full_walls, 95))} | < 300 | {len(full_walls)} |"
)
lines.append(
    f"| Full run duration_s (log) | {fmt(pctl(durations, 50))} "
    f"| {fmt(pctl(durations, 95))} | < 300 | {len(durations)} |"
)
lines.append(
    f"| Part C wall time (s) | {fmt(pctl(partc_walls, 50))} "
    f"| {fmt(pctl(partc_walls, 95))} | < 30 | {len(partc_walls)} |"
)
lines.append(
    f"| Agent peak RSS (MB, selfwatch) | {fmt(pctl(peak_rss, 50))} "
    f"| {fmt(pctl(peak_rss, 95))} | < 150 | {len(peak_rss)} |"
)
lines.append(
    f"| Agent avg CPU (% of one vCPU, selfwatch) | {fmt(pctl(avg_cpu, 50))} "
    f"| {fmt(pctl(avg_cpu, 95))} | < 20 | {len(avg_cpu)} |"
)
lines.append("")
lines.append(f"Exit codes across full runs: {exit_codes or 'n/a'}")
lines.append("")
lines.append("### Part B per-app launch + overhead (newest full-run log)")
lines.append("")
if partb_rows:
    lines.append(
        "| Test | Result | duration_ms | launch_ms | overhead | rss_mb | cpu_pct |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for row in partb_rows:
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
else:
    lines.append("_No Part B records found in the latest log._")
lines.append("")
lines.append(
    "Methodology: wall time via monotonic clock around each agent invocation "
    "(GNU `/usr/bin/time -v` cross-check files alongside on Linux); CPU/RSS "
    "are the agent's own selfwatch figures from each run's JSONL summary; "
    "Part C timed in isolation with `--part C`."
)

report = "\n".join(lines) + "\n"
(out_dir / "results.md").write_text(report)
print(report)
PYEOF

echo "" >&2
echo "Raw artifacts: ${OUT_DIR}" >&2
echo "Paste ${OUT_DIR}/results.md into benchmarks/REPORT.md" >&2
