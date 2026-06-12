# SPEC — JioPC Challenge 02: Automated Testing Agent

This is the binding build specification. Every module must conform to the interfaces here.
Target: Ubuntu 24.04 + LxQt, 4 vCPU / 8 GB / no GPU, **no root at runtime**, all state in $HOME.
Dev machine is macOS — code must run on both (CI/dev on mac, production on Linux); Linux-only
behaviour must degrade gracefully or be isolated behind small helpers.

## What we are building

A scripted validation agent, run manually from a terminal, that verifies a freshly patched
JioPC OS Image: web apps reachable & visually sound (Part A), native apps healthy (Part B),
all apps present in correct desktop folders & start-menu categories (Part C). Results go to a
structured JSONL log in `~/.local/share/jiopc/agent/`; a separate model-agnostic LLM layer
(`analyse.py` + `prompts/analyse_log.txt`) reads the log and prints an executive summary,
anomalies, correlations, and a PROMOTE/HOLD recommendation.

Hard budgets: full run < 5 min; Part C alone < 30 s; agent CPU < 20% of one vCPU sustained;
agent RAM < 150 MB total (all processes, EXCLUDING the apps under test); zero orphaned
processes; no writes outside user space; exit 0 iff all required tests pass.

Explicit anti-goals from the brief (§5.3 — judges check these):
- No hardcoded app names/URLs in code — **everything** driven by YAML.
- The agent must run and produce its log with NO LLM available (LLM is post-run only).
- No daemon/background service; single command, runs unattended, exits.
- No CAPTCHA solving — bot-detection pages are logged BLOCKED, never bypassed.
- Nothing written to /tmp or system paths.

## Repository layout (repo root = this directory)

```
jiopc-testing-agent/
├── README.md                  # setup, usage, log format, LLM config, interpretation
├── INSTALL.md                 # step-by-step fresh Ubuntu 24.04 + LxQt install
├── design.md                  # architecture + diagrams (ASCII ok), YAML schema docs,
│                              # tech justification, known limitations
├── SPEC.md                    # this file
├── jiopc_agent.py             # thin shim: `python jiopc_agent.py --config ...` (PDF CLI)
├── analyse.py                 # thin shim → src/jiopc_agent/analyse_cli.py
├── jiopc-agent.yaml           # fully populated config: 15 apps, all web + native apps
├── prompts/
│   └── analyse_log.txt        # the LLM prompt (a graded deliverable — craft carefully)
├── src/jiopc_agent/
│   ├── __init__.py            # __version__ = "1.0.0"
│   ├── __main__.py            # python -m jiopc_agent → cli.main()
│   ├── cli.py                 # argparse, exit codes
│   ├── config.py              # YAML load + validation (friendly errors)
│   ├── results.py             # Result enum + TestRecord dataclass
│   ├── runlog.py              # JSONL writer, summary block, human tee to stderr
│   ├── runner.py              # orchestrates parts (sequential default, --parallel bonus)
│   ├── part_a_web.py          # web app checks (Playwright, headless Chromium)
│   ├── part_b_native.py       # native app health (psutil)
│   ├── part_c_presence.py     # desktop/start-menu structural checks (read-only)
│   ├── desktop_entry.py       # minimal freedesktop .desktop parser (no PyXDG dep)
│   ├── selfwatch.py           # samples agent's own CPU/RSS → logged in summary
│   ├── history.py             # bonus: run history + regression detection
│   ├── notify.py              # bonus: SMTP summary email (opt-in via YAML)
│   └── analyse_cli.py         # model-agnostic LLM analysis (stdlib urllib only)
├── packaging/
│   ├── build-deb.sh           # builds dist/jiopc-testing-agent_1.0.0_all.deb (dpkg-deb)
│   └── deb/                   # control, postinst, prerm templates
├── benchmarks/
│   ├── run_benchmarks.sh      # reproducible methodology (time, /proc, psutil sampling)
│   └── REPORT.md              # filled-in report: CPU/RAM/duration p50+p95, Part B overhead
├── ci/
│   └── github-actions-example.yml   # bonus: containerised run + LLM comment step
├── tests/
│   ├── conftest.py            # fixtures: fake XDG tree, local HTTP server, fake app
│   ├── fixtures/
│   │   ├── desktop_tree/      # fake /usr/share/applications + Desktop folders
│   │   ├── web/               # ok.html, slow.html, captcha.html, missing-elements.html
│   │   └── fake_app.py        # long-running script used as a "native app" in tests
│   ├── fixture-config.yaml    # config pointing at fixtures (used by tests + demo)
│   ├── test_config.py  test_desktop_entry.py  test_part_c.py
│   ├── test_part_b.py  test_part_a.py  test_runlog.py  test_analyse.py
│   └── test_end_to_end.py     # full run against fixtures, asserts exit code + log schema
├── samples/
│   ├── test_run_SAMPLE.log    # real log produced by the end-to-end fixture run
│   └── analysis_SAMPLE.md     # real analyse.py output against that log
├── screenshots/  video/       # placeholders with README noting what to capture in the VM
└── pyproject.toml             # name jiopc-testing-agent; deps: pyyaml, psutil; extras:
                               # web=[playwright], dev=[pytest]
```

## Core interfaces (binding)

### results.py
```python
class Result(str, Enum):
    PASS, FAIL, BLOCKED, DEGRADED, MISSING, MISPLACED, ERROR
    # ERROR = the agent itself failed to execute a test (infra problem, e.g. browser
    # missing). Counts as failure for exit-code purposes, distinct in the log.

@dataclass
class TestRecord:
    ts: str          # ISO-8601 with timezone
    component: str   # "A" | "B" | "C"
    test: str        # e.g. "web:JioSaavn", "native:Files", "presence:Chess:desktop_folder"
    result: Result
    duration_ms: int
    detail: str      # one line, human readable
    data: dict       # structured extras (load_ms, rss_mb, cpu_pct, expected, found, ...)
    def to_json(self) -> str
REQUIRED_FAIL_RESULTS = {FAIL, MISSING, MISPLACED, ERROR}   # drive non-zero exit
# BLOCKED and DEGRADED do NOT fail the run by default (documented; configurable via
# agent.fail_on: [...] in YAML).
```

### runlog.py — log format (documented in README; the LLM prompt mirrors it)
JSON Lines. First line header, one line per record, final summary line:
```
{"type":"header","run_id":"2026-06-12T10-30-00","agent_version":"1.0.0","host":...,"config_path":...,"parts":["A","B","C"]}
{"type":"record","ts":...,"component":"A","test":"web:JioSaavn","result":"PASS","duration_ms":1240,"detail":"200 OK, 2/2 elements, load 1240ms < 8000ms","data":{...}}
{"type":"summary","total":26,"passed":24,"failed":1,"blocked":1,"by_component":{"A":{...},"B":{...},"C":{...}},"by_result":{...},"duration_s":93.4,"agent_peak_rss_mb":61.2,"agent_avg_cpu_pct":7.3,"regressions":[...],"exit_code":1}
```
File: `<log_dir>/test_run_<YYYY-MM-DDTHH-MM-SS>.log`. Also tee human-readable one-liners
to stderr (`[PASS] A web:JioSaavn (1240ms) — ...`) so the engineer sees progress; the
log file stays machine-clean.
API: `RunLog(log_dir) → .path, .header(...), .record(TestRecord), .summary(...) → dict`.

### config.py
`load_config(path: Path) -> AgentConfig` — dataclasses mirroring the YAML below.
Validation errors raise `ConfigError` with file/key context; cli prints them and exits 2.
All paths `expanduser()`d. Defaults applied here, in one place.

### Part contracts
```python
def run_part_a(cfg: AgentConfig, log: RunLog) -> list[TestRecord]
def run_part_b(cfg: AgentConfig, log: RunLog) -> list[TestRecord]
def run_part_c(cfg: AgentConfig, log: RunLog) -> list[TestRecord]
```
Each appends records to the log as it goes (live tee), and returns them. Each must catch
its own per-test exceptions → ERROR record, never crash the run.

### cli.py
```
python jiopc_agent.py --config jiopc-agent.yaml [--part A] [--part B] [--part C]
                      [--analyse] [--parallel] [--dry-run] [--no-email]
```
- `--part` repeatable; order of execution = `agent.part_order` from YAML (default A,B,C).
- `--analyse`: after the run, invoke the analysis layer on the fresh log (requires LLM env
  vars; if missing, print a clear message and still exit with the RUN's exit code).
- `--dry-run`: validate config + list planned tests, touch nothing.
- `--parallel`: bonus mode — Part A and Part C run concurrently (threads), Part B always
  exclusive afterwards (so launches aren't perturbed). Document CPU/RAM headroom logic.
- Exit codes: 0 all required pass; 1 ≥1 required failure; 2 config/usage error.

## Part A — web apps (part_a_web.py)
- Playwright sync API, **one** headless Chromium shared across all URLs (perf budget).
- Per test: `goto(url, wait_until="domcontentloaded", timeout=load_timeout_ms)`; record
  HTTP status; then bot-detection scan; then element checks (CSS selectors, each with its
  own short timeout, configurable `element_timeout_ms` default 5000); record
  `load_ms` from Playwright navigation timing; flag `load_ms > load_time_threshold_ms` →
  FAIL with detail "slow load" (still records the value).
- Bot detection (→ BLOCKED, logged, never bypassed): page title/body matched against a
  YAML-extensible heuristic list (defaults: "just a moment", "are you human",
  "verify you are", "access denied", "attention required") + presence of
  `iframe[src*="recaptcha"], iframe[src*="hcaptcha"], #challenge-form, #cf-challenge`.
  If `bot_detection_expected: true` and BLOCKED → detail notes "expected"; result stays
  BLOCKED (LLM treats expected-BLOCKED as non-anomalous).
- 4xx/5xx, timeout, connection error, blank page (`document.body` empty) → FAIL.
- Playwright/Chromium missing → one ERROR record per web test with install hint.
- Screenshots on failure: save to `<log_dir>/artifacts/<run_id>/<test>.png` (innovation;
  path goes into `data.screenshot`). Never on PASS (disk + time budget).

## Part B — native apps (part_b_native.py)
- For each app: verify .desktop exists (search YAML path, then standard dirs); parse
  `Exec=` via desktop_entry.py (strip %-field codes per freedesktop spec); resolve binary
  (absolute path or PATH lookup) → exists + executable, else FAIL fast.
- Launch: `subprocess.Popen(cmd, start_new_session=True, stdout/err=DEVNULL, env with
  DISPLAY honoured)`. New session ⇒ we own the whole process group for cleanup.
- Poll for a process whose name or exe matches `process_name` (psutil, walk our child's
  tree first, then system-wide match newer than launch time) every `poll_interval_ms`
  (default 500) up to `launch_timeout_s` (default 10). Not found → FAIL. Found then died
  before T+5s sample → DEGRADED.
- At T+5s after detection: sample VmRSS (psutil `memory_info().rss`) and CPU% (
  `cpu_percent(interval=1.0)`) → into `data`.
- Terminate: SIGTERM to the process group → wait `term_grace_s` (default 5) → SIGKILL
  group → after run, sweep: any survivor from our launches = log WARNING in record data
  and kill; assert none remain (this is a graded checklist item).
- Cooldown `cooldown_s` (default 2) between apps — documented rationale in REPORT.md.
- Overhead accounting (graded): measure agent's own poll cost; the launch-time we report
  (`data.launch_ms`) = detection time minus measured polling overhead; REPORT.md states
  the overhead figure and methodology.
- macOS dev note: everything above works on mac with psutil; guard Linux-only bits
  (`os.setsid` is fine on mac; just no DISPLAY logic needed).

## Part C — presence (part_c_presence.py + desktop_entry.py)
- Read-only. No launching, no elevated privileges. Must complete < 30 s (it's pure I/O —
  scan directories once, build an index, then evaluate all apps against the index).
- Sources: system app dirs (default `/usr/share/applications`, `~/.local/share/applications`,
  overridable in YAML for tests) and desktop folder root (default `~/Desktop`, overridable).
- For each app: locate `<app>.desktop` anywhere in sources+desktop → else MISSING.
  If found: on-desktop check = file (or symlink) present under `Desktop/<expected_folder>/`;
  present but under a different folder → MISPLACED (detail says found vs expected).
  Start-menu check = `Categories=` of the system .desktop contains expected category;
  file exists but wrong/absent category → MISPLACED.
- Two records per app (`presence:<App>:desktop_folder`, `presence:<App>:start_menu`) so the
  summary can pinpoint which dimension broke.
- desktop_entry.py: tiny INI-ish parser honouring `[Desktop Entry]` section, key=value,
  comments, localised keys ignored; functions `parse(path) -> dict`, `exec_argv(entry) ->
  list[str]` (strips %u/%U/%f/%F/... codes), `categories(entry) -> set[str]`.

## selfwatch.py (innovation + benchmark evidence)
Background thread sampling `psutil.Process()` + children (excluding apps under test by
PID set registration from part B, and the Playwright browser counted separately as
`browser_rss_mb`): rss, cpu%. Every 500 ms into a ring buffer; summary gets peak/avg.
This makes the benchmark claim self-evidencing in every log.

## history.py (bonus — trend analysis)
After each run append one line to `<log_dir>/history.jsonl` (run_id, totals, per-test
result map). On the next run, diff: tests PASS last run that are now FAIL/MISSING/... →
`regressions` list in the summary + flagged in stderr tee + surfaced to the LLM.

## analyse_cli.py + analyse.py shim (LLM layer)
- stdlib only (urllib.request). Env: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`
  (`LLM_API_KEY` optional for local Ollama). POST `{base}/chat/completions`
  (OpenAI-compatible; works for OpenAI, Anthropic-compatible gateways, Mistral, Ollama).
- `python analyse.py --log <path>` (default: newest `test_run_*.log` in default log dir);
  `--json` to request strict-JSON output mode from the model; `--max-log-bytes` guard with
  smart truncation (keep header, summary, all non-PASS records, sample of PASS).
- Reads `prompts/analyse_log.txt` (path from YAML `agent.llm_prompt_file`, overridable).
- Prints model output to stdout; exit 0 on success, 3 on LLM/transport error (never
  masks the agent's own exit semantics).

### prompts/analyse_log.txt (graded deliverable — write with care)
Must instruct the model to produce, in order, with markdown headers:
1. **Executive summary** — one paragraph: N ran / N passed, safe to promote?
2. **Anomalies & failures by component** (A/B/C) — one line each: what + why it matters.
   BLOCKED ≠ FAIL: expected-BLOCKED is informational; unexpected BLOCKED is an anomaly
   but not a regression. DEGRADED means launched-then-died. MISSING vs MISPLACED defined.
3. **Patterns & correlations** — e.g. shared Categories among Part B failures, same domain
   among BLOCKED, regression list from summary, load-time outliers.
4. **Recommendation** — exactly `PROMOTE` or `HOLD` in bold + one-sentence rationale.
   Rule given to model: any FAIL/MISSING/MISPLACED/ERROR on a required test ⇒ HOLD.
Include the log-format legend in the prompt so any model can parse it; demand terse,
factual output; forbid invented data.

## YAML schema (jiopc-agent.yaml — ship fully populated)
```yaml
agent:
  log_dir: ~/.local/share/jiopc/agent/
  llm_prompt_file: ./prompts/analyse_log.txt
  part_order: [A, B, C]
  fail_on: [FAIL, MISSING, MISPLACED, ERROR]   # results that drive exit != 0
  cooldown_s: 2
  poll_interval_ms: 500
  parallel: false
  email: {enabled: false, smtp_host: "", smtp_port: 587, from: "", to: "", use_tls: true}
  paths:   # overridable for tests; defaults shown
    applications_dirs: [/usr/share/applications, ~/.local/share/applications]
    desktop_dir: ~/Desktop
web_apps:        # Part A
  - name: JioSaavn
    url: https://www.jiosaavn.com
    load_time_threshold_ms: 8000
    bot_detection_expected: false
    elements:
      - {selector: "nav", description: top navigation}
      - {selector: "input[type=search], [role=search]", description: search box}
native_apps:     # Part B
  - name: Files
    desktop_file: /usr/share/applications/pcmanfm-qt.desktop
    process_name: pcmanfm-qt
    launch_timeout_s: 10
desktop_presence:  # Part C — exactly 15 apps
  - {name: Chess, desktop_id: org.gnome.Chess.desktop, desktop_folder: Games, start_menu_category: Game}
```
Populate realistically for a JioPC image on Ubuntu 24.04 + LxQt:
- web_apps (≥6): JioSaavn, JioCinema, JioMart, JioCloud, MyJio/Jio.com, Wikipedia
  (education tile). Mark one with `bot_detection_expected: true` where realistic and say
  why in a YAML comment.
- native_apps (≥6): Firefox, PCManFM-Qt (Files), FeatherPad (Text Editor), QTerminal,
  LibreOffice Writer (soffice → process `soffice.bin`), VLC, GCompris.
- desktop_presence: exactly 15 across folders Games / Education / Productivity with
  start-menu categories Game / Education / Office|Utility|Network. Use real Ubuntu 24.04
  package .desktop ids (e.g. org.gnome.Chess.desktop, gnome-mines, aisleriot,
  gcompris-qt, org.kde.kgeography, tuxmath, tuxtype, libreoffice-writer/-calc/-impress,
  featherpad, pcmanfm-qt, qterminal, firefox, vlc).
YAML comments throughout — the file doubles as schema documentation.

## Packaging (.deb) — auto-DQ if it doesn't install on fresh Ubuntu 24.04
- `packaging/build-deb.sh`: stages files under `debroot/`, `dpkg-deb --build --root-owner-group`.
- Installs to `/opt/jiopc-testing-agent/` (code, prompts, default YAML) +
  `/usr/bin/jiopc-agent` and `/usr/bin/jiopc-agent-analyse` launcher scripts.
- `control`: Depends: python3 (>= 3.10), python3-yaml, python3-psutil. Playwright can't
  come from apt → `postinst` creates `/opt/jiopc-testing-agent/venv`, pip-installs
  `playwright`, runs `playwright install chromium --with-deps` best-effort; on failure
  prints clear manual instructions and exits 0 (Part A then yields ERROR records with the
  same hint — agent still installs and Parts B/C work). INSTALL.md documents both paths.
- Launchers prefer the venv python if present, else system python3.
- Root is used only at install time (normal for dpkg); runtime is pure user-space.

## Benchmarks (benchmarks/)
- `run_benchmarks.sh`: 5 timed full runs (`/usr/bin/time -v` on Linux, fallback `time`),
  p50/p95 of duration; CPU/RSS from selfwatch summaries (jq or python one-liner over the
  logs); Part C isolated timing; Part B per-app overhead extraction.
- REPORT.md: results table vs targets, methodology, Part B overhead figure + how
  launch_ms subtracts it, false-DEGRADED mitigation discussion, cooldown rationale.
  Include the mac dev-run numbers clearly labelled + placeholders marked `TODO(VM)` for
  the Ubuntu VM numbers with exact commands to fill them.

## Tests (pytest; all green on macOS)
- Fixtures simulate everything: fake XDG/app/Desktop tree (good + missing + misplaced
  cases), threaded local HTTP server serving ok/slow/captcha/missing-element pages,
  `fake_app.py` (sleeps, ignores nothing, names itself) for Part B, mock OpenAI-compatible
  endpoint (http.server) for analyse tests.
- `test_end_to_end.py`: full run on fixture config → asserts exit code, log schema
  (every line valid JSON, header/summary present), MISSING vs MISPLACED distinction,
  BLOCKED on captcha page, no orphan from fake_app.
- Part A tests auto-skip if Playwright/Chromium unavailable (but verify agent will
  install it in the venv).

## Quality bar
- Python 3.10+ compatible (target ships 3.12; dev mac has 3.14). Type hints throughout,
  docstrings on public functions, no dead code, ruff-clean style (4-space, f-strings).
- No global mutable state; everything injected via AgentConfig.
- Every YAML knob has a default and is documented in design.md §schema.
- stderr is for humans, stdout reserved (analysis output, --dry-run listing); log file
  is the machine artifact.
