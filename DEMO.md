# DEMO.md — Recording the Challenge 02 demo video

**Form requirement:** the video must show a **full agent run (all 3 parts)** *and*
the **LLM PROMOTE/HOLD output**, recorded **inside the Ubuntu 24.04 + LxQt VM**,
**≤ 3 minutes**, and **viewable without sign-in**.

A full run is ~68 s + a few seconds for the LLM call, so the whole thing fits
comfortably under 3 minutes with narration.

---

## 0. One-time prep (before you hit record)

Do these first so the recording itself is clean and short.

```bash
# 1. Make sure the VM has the latest code and the installed copy matches it.
cd ~/Jiopc-testing-agent
git pull
sudo cp jiopc-agent.yaml /opt/jiopc-testing-agent/jiopc-agent.yaml

# 2. Configure the LLM (model-agnostic, OpenAI-compatible). Use whatever you
#    actually used — OpenAI, a gateway, or a local Ollama. Example:
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_MODEL="gpt-4o"
export LLM_API_KEY="sk-..."      # your real key; keep it off-screen while recording

# 3. Sanity check WITHOUT spending run time — validates config + lists tests.
jiopc-agent --config /opt/jiopc-testing-agent/jiopc-agent.yaml --dry-run
```

- Close noisy windows; leave one maximised terminal with a readable font.
- Have Firefox/VLC/LibreOffice **not** already running (Part B launches them; it's
  good demo footage to see the windows appear and then get cleanly closed).
- A stable network matters — Part A hits live Jio sites.

## 1. Start the screen recorder

Use the VM's built-in recorder or install one:
```bash
sudo apt-get install -y simplescreenrecorder   # or: obs-studio
```
Record the **whole desktop** (so the Part B app windows are visible), with the
terminal in focus.

## 2. The single command to run on camera

Run everything — all three parts **and** the LLM analysis — in one shot:

```bash
jiopc-agent --config /opt/jiopc-testing-agent/jiopc-agent.yaml --analyse
```

What the viewer will see, in order:
1. **Part A** — web tiles checked (`[PASS]/[FAIL]/[BLOCKED]` lines stream to the terminal).
2. **Part B** — native apps launch on screen, get sampled at T+5 s, then cleanly close.
3. **Part C** — desktop/start-menu presence checks (instant).
4. **Summary line** — `NN/43 passed … exit_code=N → <log path>`.
5. **LLM analysis** — the executive summary, anomalies by component, and the
   final **PROMOTE** or **HOLD** recommendation in bold.

> If `--analyse` prints an LLM/transport error, the env vars aren't set correctly.
> Fall back to two commands on camera:
> ```bash
> jiopc-agent --config /opt/jiopc-testing-agent/jiopc-agent.yaml
> jiopc-agent-analyse
> ```

## 3. Narration beats (keep it tight, ~2–2.5 min)

- "Single YAML-driven command, no LLM needed for the run itself."
- Point at a **BLOCKED** line: "bot walls are logged, never bypassed."
- Point at **Part B**: "each app launched, health-sampled, then cleanly terminated —
  no orphaned processes."
- Point at the **summary**: call out the exit code.
- Point at the **LLM verdict**: read the PROMOTE/HOLD line and its one-sentence rationale.
- If the verdict is **HOLD**, say why honestly (e.g. one live tile genuinely failed) —
  that the agent *detects* problems rather than hiding them is the whole point.

## 4. Stop, trim, publish (no sign-in required)

- Trim to ≤ 3 minutes.
- Upload as **YouTube → Unlisted** (most reliably viewable without a Google sign-in),
  **or** Google Drive with sharing set to **"Anyone with the link → Viewer"**.
- **Open the link in a private/incognito window to confirm it plays without login**
  before pasting it into the form.

## 5. Capture the still screenshots too (separate form requirement)

While you're in the VM, grab PNGs for the `screenshots/` folder (see
`screenshots/README.md`): the streaming run, the summary line, and the LLM
PROMOTE/HOLD output. Commit them:
```bash
cp ~/shots/*.png ~/Jiopc-testing-agent/screenshots/
cd ~/Jiopc-testing-agent && git add screenshots && git commit -m "Add VM demo screenshots" && git push
```

---

### Pre-submit checklist
- [ ] Repo is **public** (open it in incognito to confirm).
- [ ] `screenshots/` contains real PNGs from the VM.
- [ ] Video ≤ 3 min, plays in incognito, shows all 3 parts + PROMOTE/HOLD.
- [ ] `benchmarks/REPORT.md` numbers match your latest VM run.
