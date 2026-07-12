# JioPC Automated Testing Agent

A scripted validation agent for JioPC OS images. It verifies web apps (Part A), native apps (Part B), and desktop/start-menu presence (Part C).

## Setup & Installation
See [INSTALL.md](INSTALL.md) for step-by-step installation instructions on a fresh Ubuntu 24.04 + LxQt environment.

## Dependencies
- Python 3.10+
- `pyyaml`
- `psutil`
- `playwright` (with Chromium browser)

## Usage
Run the agent from the terminal using the provided configuration file:

```bash
python jiopc_agent.py --config jiopc-agent.yaml
```

Options:
- `--part A`, `--part B`, `--part C`: Run only specific parts.
- `--analyse`: Automatically run the LLM analysis script on the resulting log file.
- `--parallel`: Run Part A and Part C concurrently for faster execution.
- `--dry-run`: Validate config and list planned tests without running them.

## Part A web checks
Each web app's `elements` are presence checks that all must hold. An element is
**either** a CSS selector or an accessible role:

```yaml
elements:
  - {selector: "nav", description: top navigation}        # CSS
  - {role: searchbox, description: search box}             # ARIA role
  - {role: button, name: "Sign in", description: login}   # role + accessible name
  - {selector: "footer", state: visible, description: rendered footer}  # must be visible, not just attached
```

The headless browser presents a realistic desktop-Chrome identity (`agent.browser`:
user-agent, locale `en-IN`, timezone `Asia/Kolkata`, viewport, `navigator.webdriver`
masked) so legitimate sites are validated instead of being false-flagged as a bot.
A genuine CAPTCHA/challenge page is still logged `BLOCKED` and **never bypassed**.
Transient navigation failures are retried (`agent.web_retries`, default 1) so a
one-off network blip does not force a HOLD.

## Log Format
The agent outputs a structured JSONL log to `~/.local/share/jiopc/agent/test_run_<timestamp>.log`.
- `header`: Contains run metadata.
- `record`: Contains individual test results (PASS, FAIL, BLOCKED, DEGRADED, MISSING, MISPLACED, ERROR).
- `summary`: Contains total pass/fail counts, duration, and resource usage.

## LLM Configuration
To use the post-run analysis script (`analyse.py`), configure your LLM environment variables. The script is model-agnostic and OpenAI-compatible:

```bash
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_MODEL="gpt-4o"
export LLM_API_KEY="your-api-key"

python analyse.py --log ~/.local/share/jiopc/agent/test_run_...log
```

## Interpretation
- **PROMOTE**: All required tests passed. The OS image is safe to promote.
- **HOLD**: At least one required test failed (FAIL, MISSING, MISPLACED, ERROR). The OS image requires review.


## team InnovAstra
1. Kanakamurthy H
2. Vedant Moghe
3. Anant Asati
