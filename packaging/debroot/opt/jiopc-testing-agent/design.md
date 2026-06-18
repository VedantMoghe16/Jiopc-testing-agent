# Design Document

## Architecture

The JioPC Automated Testing Agent is composed of a runner core and three testing components, complemented by an LLM-powered log analysis script.

```text
+-------------------+        +--------------------+
| jiopc-agent.yaml  | -----> | jiopc_agent.py CLI |
+-------------------+        +---------+----------+
                                       |
+--------------------------------------+--------------------------------------+
|                              runner.py                                      |
+-----------------------------------------------------------------------------+
|        Part A         |        Part B         |        Part C               |
|    (part_a_web.py)    |  (part_b_native.py)   |  (part_c_presence.py)       |
|  Playwright + Chrome  |   psutil + subprocess |  PyXDG (desktop_entry.py)   |
+-----------------------+-----------------------+-----------------------------+
|                               runlog.py                                     |
|                       (JSONL Output -> ~/.local/...)                        |
+--------------------------------------+--------------------------------------+
                                       |
                                       v
                           +------------------------+
                           | analyse.py (LLM Layer) |
                           +------------------------+
```

## Technology Choices & Justification
- **Language**: Python 3.10+ (Available on Ubuntu 24.04, rich ecosystem).
- **YAML Parsing**: `pyyaml` (Standard, widely used).
- **Part A (Web)**: Playwright with headless Chromium. Excellent modern JS handling, reliable element selectors, and robust load-timing APIs.
- **Part B (Native)**: `psutil` + `subprocess`. Native cross-platform process tree visibility, straightforward VmRSS/CPU sampling, and clean termination.
- **Part C (Presence)**: Custom minimal `.desktop` parser (`desktop_entry.py`) matching freedesktop specs. Avoids heavy external dependencies like PyXDG.
- **LLM Analysis**: Standard library `urllib.request`. Zero-dependency OpenAI-compatible API client, ensuring the script is lightweight.

## YAML Schema Documentation
The `jiopc-agent.yaml` is the single source of truth for the agent.

- `agent`: Defines global settings (`log_dir`, `llm_prompt_file`, `part_order`, `fail_on`, pacing/timeouts).
- `web_apps`: A list of dictionaries defining Part A targets (`name`, `url`, `load_time_threshold_ms`, `bot_detection_expected`, and expected `elements`).
- `native_apps`: A list defining Part B targets (`name`, `desktop_file`, `process_name`, `launch_timeout_s`).
- `desktop_presence`: A list defining Part C targets (`name`, `desktop_id`, `desktop_folder`, `start_menu_category`).

## Known Limitations
- The agent does not handle interactive desktop elements like modal dialogs or authentication prompts during Part B.
- CAPTCHA pages (Part A) are correctly logged as `BLOCKED` but cannot be bypassed.
- Process memory measurement relies on `VmRSS`, which may not precisely capture shared library usage in LxQt.
