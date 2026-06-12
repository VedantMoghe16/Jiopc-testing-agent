<!-- Provenance: real `analyse.py` stdout against samples/test_run_SAMPLE.log via a local OpenAI-compatible mock endpoint; the analysis text was authored by the integrator following prompts/analyse_log.txt (no live LLM was available on this machine). -->
## Executive summary
16 tests ran across all three components (A web, B native, C presence) in 11.7 s; 8 passed, 3 FAIL, 2 MISPLACED, 2 MISSING, 1 expected BLOCKED (summary exit_code 1). With required failures in components A and C, this image does not look safe to promote.

## Anomalies & failures by component
### A
- web:FixtureSlow — FAIL: slow load, 1512ms > 250ms threshold (HTTP 200). The page is reachable but far over its load budget.
- web:FixtureMissingElements — FAIL: HTTP 200 but 0/2 expected elements present (missing: top navigation, search box). The page loads without its required UI.
- web:FixtureServerError — FAIL: HTTP 500. The service is returning a server error.
- (web:FixtureCaptcha was BLOCKED by a bot-detection page, marked expected per config — informational only, not an anomaly.)

### B
B: no anomalies. native:FakeApp PASS (launched in 100ms, healthy at T+5s, rss 16.1 MB, cpu 0.0%); no orphan warnings.

### C
- presence:Paint:desktop_folder — MISPLACED: fixture-paint.desktop found in Games, expected Desktop/Education/. The app is installed but its desktop shortcut is mis-shipped.
- presence:Calc:start_menu — MISPLACED: fixture-calc.desktop Categories= is Utility, expected to contain Office. Wrong start-menu category; the app will appear under the wrong menu.
- presence:Ghost:desktop_folder — MISSING: fixture-ghost.desktop not found in applications dirs or under the Desktop tree. The app is absent from the image.
- presence:Ghost:start_menu — MISSING: same .desktop absent from the applications dirs; both presence dimensions confirm the app was never shipped.

## Patterns & correlations
- Ghost is MISSING on both dimensions — a wholly absent app (packaging omission), distinct from the MISPLACED cases which are installed but mis-filed.
- Paint and Calc each fail on exactly one different dimension (desktop folder vs start-menu category) — two independent per-app shipping defects, not a single folder-naming defect.
- Part A load-time outlier: FixtureSlow load_ms 1512 vs 2–14 ms for every other page checked.
- regressions list is empty — nothing that passed the previous run broke this run.
- Agent resource numbers are within budget: agent_peak_rss_mb 40.2 (< 150 MB), agent_avg_cpu_pct 2.0 (< 20% of one vCPU); browser_rss_mb 428.0 is accounted separately.

## Recommendation
**HOLD** — the run has 3 FAIL, 2 MISPLACED and 2 MISSING required records and summary exit_code 1; the expected BLOCKED on web:FixtureCaptcha alone would not have forced this.

