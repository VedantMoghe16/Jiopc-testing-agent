"""Model-agnostic post-run LLM analysis of the agent's JSONL log.

stdlib only (``urllib.request``). Talks to any OpenAI-compatible
``{base}/chat/completions`` endpoint (OpenAI, Anthropic-compatible gateways,
Mistral, Ollama). Configuration via environment variables:

* ``LLM_BASE_URL`` — e.g. ``https://api.openai.com/v1`` or ``http://localhost:11434/v1``
* ``LLM_MODEL``    — e.g. ``gpt-4o-mini`` or ``llama3.1``
* ``LLM_API_KEY``  — optional (local Ollama needs none)

::

    python analyse.py [--log <path>] [--json] [--max-log-bytes N]
                      [--config jiopc-agent.yaml] [--prompt <path>]

Default log: the newest ``test_run_*.log`` in the default log dir (or the
config's ``agent.log_dir`` when ``--config`` is given). The prompt template is
read from ``prompts/analyse_log.txt`` (YAML ``agent.llm_prompt_file``,
overridable with ``--prompt``).

Exit codes: 0 success; 2 usage/config error; 3 LLM/transport error. This tool
never masks the agent's own exit semantics — it is post-run only.

The agent itself runs and produces its log with NO LLM available (brief §5.3);
this layer is strictly optional and read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:  # avoid importing yaml-dependent code unless --config is used
    from jiopc_agent.config import AgentConfig

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LLM_ERROR = 3

#: Mirrors config.DEFAULT_LOG_DIR / DEFAULT_PROMPT_FILE without importing
#: config (which requires PyYAML; this module is stdlib-only by spec).
DEFAULT_LOG_DIR = "~/.local/share/jiopc/agent/"
DEFAULT_PROMPT_FILE = "./prompts/analyse_log.txt"

DEFAULT_MAX_LOG_BYTES = 200_000
DEFAULT_TIMEOUT_S = 120.0

ENV_BASE_URL = "LLM_BASE_URL"
ENV_MODEL = "LLM_MODEL"
ENV_API_KEY = "LLM_API_KEY"

#: Extra user-turn instruction appended in --json mode.
JSON_MODE_INSTRUCTION = (
    "Respond with a single strict JSON object and nothing else (no markdown, "
    "no code fences), with exactly these keys: "
    '"executive_summary" (string), '
    '"anomalies" (object with keys "A", "B", "C", each a list of strings), '
    '"patterns" (list of strings), '
    '"recommendation" ("PROMOTE" or "HOLD"), '
    '"rationale" (one-sentence string).'
)


class AnalyseError(Exception):
    """LLM/transport-level failure (drives exit code 3)."""


# ---------------------------------------------------------------------------
# Log discovery and smart truncation
# ---------------------------------------------------------------------------


def find_latest_log(log_dir: Path) -> Path | None:
    """Newest ``test_run_*.log`` in ``log_dir`` by mtime, or None."""
    log_dir = Path(log_dir).expanduser()
    if not log_dir.is_dir():
        return None
    candidates = sorted(
        log_dir.glob("test_run_*.log"), key=lambda p: p.stat().st_mtime
    )
    return candidates[-1] if candidates else None


def _line_priority(line: str) -> int:
    """0 = always keep (header/summary/non-PASS/unparsable), 1 = PASS sample pool."""
    stripped = line.strip()
    if not stripped:
        return 1  # blank lines are droppable
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed lines may themselves be the anomaly — keep them
    if not isinstance(obj, dict):
        return 0
    if obj.get("type") in ("header", "summary"):
        return 0
    if obj.get("type") == "record" and obj.get("result") == "PASS":
        return 1
    return 0


def smart_truncate(raw: str, max_bytes: int) -> str:
    """Shrink the log under ``max_bytes`` while keeping everything that matters.

    Always kept: the header line, the summary line, every non-PASS record and
    any unparsable line. PASS records are evenly sampled into the remaining
    budget; omissions are declared with a machine-parsable
    ``{"type": "truncation", ...}`` line so the model knows the log is partial.
    """
    if len(raw.encode("utf-8")) <= max_bytes:
        return raw

    lines = raw.splitlines()
    keep: set[int] = set()
    pass_idx: list[int] = []
    for i, line in enumerate(lines):
        if _line_priority(line) == 0:
            keep.add(i)
        elif line.strip():
            pass_idx.append(i)

    def size_of(indices: set[int], omitted: int) -> int:
        notice = _truncation_notice(omitted)
        body = "\n".join(lines[i] for i in sorted(indices))
        return len(body.encode("utf-8")) + len(notice.encode("utf-8")) + 2

    # If even the mandatory lines blow the budget, drop oldest non-PASS records
    # (header and summary are sacrosanct, kept last).
    mandatory = sorted(keep)
    while len(mandatory) > 2 and size_of(set(mandatory), len(pass_idx)) > max_bytes:
        for j, idx in enumerate(mandatory):
            try:
                obj = json.loads(lines[idx])
            except (json.JSONDecodeError, ValueError):
                obj = {}
            if isinstance(obj, dict) and obj.get("type") not in ("header", "summary"):
                del mandatory[j]
                break
        else:
            break
    keep = set(mandatory)

    # Evenly sample PASS records into whatever budget remains.
    sampled: list[int] = []
    for take in range(len(pass_idx), 0, -1):
        step = len(pass_idx) / take
        candidate = [pass_idx[int(k * step)] for k in range(take)]
        if size_of(keep | set(candidate), len(pass_idx) - take) <= max_bytes:
            sampled = candidate
            break

    kept = keep | set(sampled)
    omitted = len(pass_idx) - len(sampled)
    out: list[str] = []
    notice_done = False
    for i in sorted(kept):
        out.append(lines[i])
        # place the notice right after the header line
        if not notice_done and omitted > 0:
            out.append(_truncation_notice(omitted))
            notice_done = True
    if omitted > 0 and not notice_done:
        out.insert(0, _truncation_notice(omitted))
    return "\n".join(out) + "\n"


def _truncation_notice(omitted_pass_records: int) -> str:
    return json.dumps(
        {
            "type": "truncation",
            "omitted_pass_records": omitted_pass_records,
            "note": "log truncated for the LLM; all non-PASS records retained",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Prompt resolution
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Repo root for a source checkout: src/jiopc_agent/ → two levels up."""
    return Path(__file__).resolve().parents[2]


def resolve_prompt_path(prompt: Path) -> Path:
    """Resolve a (possibly relative) prompt path: cwd first, then repo root."""
    prompt = Path(prompt).expanduser()
    if prompt.is_file():
        return prompt
    if not prompt.is_absolute():
        fallback = _repo_root() / prompt
        if fallback.is_file():
            return fallback
    return prompt  # caller reports the not-found error with this path


def load_prompt(prompt_path: Path) -> str:
    path = resolve_prompt_path(prompt_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalyseError(f"cannot read prompt file {path}: {exc}") from exc
    if not text.strip():
        raise AnalyseError(f"prompt file {path} is empty")
    return text


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completion over stdlib urllib
# ---------------------------------------------------------------------------


def build_payload(
    model: str, prompt: str, log_text: str, json_mode: bool
) -> dict[str, Any]:
    """The /chat/completions request body."""
    user_content = (
        "Analyse the following JioPC agent test-run log (JSON Lines).\n\n"
        + log_text
    )
    if json_mode:
        user_content += "\n\n" + JSON_MODE_INSTRUCTION
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def call_llm(
    base_url: str,
    payload: dict[str, Any],
    api_key: str | None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """POST to ``{base}/chat/completions``; return the model's text."""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AnalyseError(f"LLM HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AnalyseError(f"cannot reach LLM at {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise AnalyseError(f"LLM request to {url} timed out after {timeout_s}s") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AnalyseError(f"non-JSON response from {url}: {body[:200]}") from exc

    content = _extract_content(data)
    if content is None:
        raise AnalyseError(f"unexpected response shape from {url}: {body[:200]}")
    return content


def _extract_content(data: Any) -> str | None:
    """choices[0].message.content (OpenAI shape) with an Ollama-native fallback."""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    message = data.get("message")  # Ollama /api/chat shape, just in case
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return None


# ---------------------------------------------------------------------------
# High-level entry points
# ---------------------------------------------------------------------------


def _llm_env() -> tuple[str, str, str | None]:
    """(base_url, model, api_key) from the environment, or AnalyseError."""
    base_url = os.environ.get(ENV_BASE_URL, "").strip()
    model = os.environ.get(ENV_MODEL, "").strip()
    if not base_url or not model:
        raise AnalyseError(
            f"LLM not configured: set {ENV_BASE_URL} and {ENV_MODEL} "
            f"(and optionally {ENV_API_KEY}); e.g. "
            f"{ENV_BASE_URL}=http://localhost:11434/v1 {ENV_MODEL}=llama3.1"
        )
    return base_url, model, os.environ.get(ENV_API_KEY) or None


def analyse_log(
    log_path: Path,
    cfg: "AgentConfig | None" = None,
    *,
    json_mode: bool = False,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    prompt_path: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    out: TextIO | None = None,
) -> str:
    """Analyse one log file and print the model output to ``out`` (stdout).

    This is the hook cli.py calls for ``--analyse``. Raises
    :class:`AnalyseError` on any LLM/transport problem (caller decides the
    exit semantics; the run's own exit code is never affected).
    """
    log_path = Path(log_path).expanduser()
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalyseError(f"cannot read log file {log_path}: {exc}") from exc

    if prompt_path is None:
        prompt_path = (
            cfg.agent.llm_prompt_file if cfg is not None else Path(DEFAULT_PROMPT_FILE)
        )
    prompt = load_prompt(prompt_path)
    base_url, model, api_key = _llm_env()

    log_text = smart_truncate(raw, max_log_bytes)
    payload = build_payload(model, prompt, log_text, json_mode)
    content = call_llm(base_url, payload, api_key, timeout_s)

    print(content, file=out if out is not None else sys.stdout)
    return content


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiopc-agent-analyse",
        description=(
            "Send a jiopc-agent JSONL run log to an OpenAI-compatible LLM and "
            "print an executive summary, anomalies, correlations, and a "
            "PROMOTE/HOLD recommendation. Needs LLM_BASE_URL and LLM_MODEL "
            "(LLM_API_KEY optional, e.g. for local Ollama)."
        ),
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        help="run log to analyse (default: newest test_run_*.log in the log dir)",
    )
    parser.add_argument(
        "--config",
        metavar="YAML",
        help="agent YAML; supplies agent.log_dir and agent.llm_prompt_file",
    )
    parser.add_argument(
        "--prompt",
        metavar="PATH",
        help=f"prompt template (default: {DEFAULT_PROMPT_FILE} or YAML agent.llm_prompt_file)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="request strict-JSON output mode from the model",
    )
    parser.add_argument(
        "--max-log-bytes",
        type=int,
        default=DEFAULT_MAX_LOG_BYTES,
        metavar="N",
        help="truncation guard: keep header, summary, all non-PASS records and "
        f"a sample of PASS records within N bytes (default {DEFAULT_MAX_LOG_BYTES})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help=f"LLM request timeout (default {DEFAULT_TIMEOUT_S:.0f})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``jiopc-agent-analyse`` / ``python analyse.py``)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_log_bytes < 1024:
        print("--max-log-bytes must be >= 1024", file=sys.stderr)
        return EXIT_USAGE

    cfg = None
    if args.config:
        try:
            from jiopc_agent.config import ConfigError, load_config

            cfg = load_config(Path(args.config))
        except Exception as exc:  # ConfigError or missing PyYAML
            print(f"config error: {exc}", file=sys.stderr)
            return EXIT_USAGE

    if args.log:
        log_path = Path(args.log).expanduser()
        if not log_path.is_file():
            print(f"log file not found: {log_path}", file=sys.stderr)
            return EXIT_USAGE
    else:
        log_dir = cfg.agent.log_dir if cfg is not None else Path(DEFAULT_LOG_DIR)
        latest = find_latest_log(Path(log_dir))
        if latest is None:
            print(
                f"no test_run_*.log found in {Path(log_dir).expanduser()}; "
                "run the agent first or pass --log",
                file=sys.stderr,
            )
            return EXIT_USAGE
        log_path = latest
        print(f"analysing newest log: {log_path}", file=sys.stderr)

    prompt_path = Path(args.prompt).expanduser() if args.prompt else None
    try:
        analyse_log(
            log_path,
            cfg,
            json_mode=args.json,
            max_log_bytes=args.max_log_bytes,
            prompt_path=prompt_path,
            timeout_s=args.timeout,
        )
    except AnalyseError as exc:
        print(f"analyse error: {exc}", file=sys.stderr)
        return EXIT_LLM_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
