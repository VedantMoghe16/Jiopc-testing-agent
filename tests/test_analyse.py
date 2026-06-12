"""analyse_cli.py — model-agnostic LLM layer against a mock OpenAI endpoint.

SPEC contract: stdlib urllib only; env LLM_BASE_URL / LLM_MODEL / LLM_API_KEY;
POST {base}/chat/completions; prompt from prompts/analyse_log.txt; model
output to stdout; exit 0 on success, 3 on LLM/transport error; --max-log-bytes
smart truncation keeps header, summary and all non-PASS records.
"""

from __future__ import annotations

import io

import pytest

import _helpers

analyse_cli = pytest.importorskip(
    "jiopc_agent.analyse_cli", reason="spine module analyse_cli.py not implemented yet"
)

from jiopc_agent.results import Result, make_record  # noqa: E402
from jiopc_agent.runlog import RunLog  # noqa: E402

PROMPT_TEXT = (
    "You are the JioPC validation analyst. FIXTURE-PROMPT-MARKER.\n"
    "Summarise the JSONL log and end with PROMOTE or HOLD.\n"
)


def _make_log(tmp_path, n_pass: int = 3, pad: int = 0):
    """Write a realistic run log via the real RunLog; returns its path."""
    log = RunLog(tmp_path / "logs", tee=io.StringIO())
    log.header(agent_version="1.0.0", config_path="fixture.yaml", parts=["A", "B", "C"])
    detail_pad = "x" * pad
    for i in range(n_pass):
        log.record(
            make_record("A", f"web:PassApp{i}", Result.PASS, 100 + i, f"200 OK {detail_pad}")
        )
    log.record(
        make_record("C", "presence:Ghost:desktop_folder", Result.MISSING, 1, "not found anywhere")
    )
    log.record(make_record("A", "web:CaptchaApp", Result.BLOCKED, 900, "bot page (expected)"))
    log.summary(total=n_pass + 2, passed=n_pass, failed=1, blocked=1, exit_code=1)
    log.close()
    return log.path


@pytest.fixture()
def analyse_env(tmp_path, monkeypatch, llm_server):
    """cwd with prompts/analyse_log.txt + LLM_* env pointing at the mock."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "analyse_log.txt").write_text(PROMPT_TEXT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # default prompt path is ./prompts/analyse_log.txt
    monkeypatch.setenv("LLM_BASE_URL", llm_server.base_url)
    monkeypatch.setenv("LLM_MODEL", "fixture-model")
    monkeypatch.setenv("LLM_API_KEY", "fixture-key")
    return tmp_path


def test_happy_path_prints_model_output(analyse_env, llm_server, capsys):
    log_path = _make_log(analyse_env)
    rc = _helpers.call_main(analyse_cli.main, ["--log", str(log_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROMOTE" in out  # mock content reached stdout untouched
    assert len(llm_server.requests) == 1


def test_request_is_openai_compatible(analyse_env, llm_server):
    log_path = _make_log(analyse_env)
    _helpers.call_main(analyse_cli.main, ["--log", str(log_path)])
    req = llm_server.requests[0]
    assert req["path"].endswith("/chat/completions")
    payload = req["json"]
    assert payload["model"] == "fixture-model"
    assert isinstance(payload["messages"], list) and payload["messages"]
    sent = str(payload["messages"])
    assert "FIXTURE-PROMPT-MARKER" in sent  # prompt file content included
    assert "presence:Ghost:desktop_folder" in sent  # log content included
    # API key forwarded as a bearer token
    auth = req["headers"].get("Authorization", "")
    assert "fixture-key" in auth


def test_truncation_keeps_non_pass_records(analyse_env, llm_server):
    """--max-log-bytes: drop PASS bulk, keep header/summary/non-PASS (SPEC)."""
    log_path = _make_log(analyse_env, n_pass=300, pad=300)  # ~100 KB of PASS noise
    full_size = log_path.stat().st_size
    budget = 8000
    rc = _helpers.call_main(
        analyse_cli.main, ["--log", str(log_path), "--max-log-bytes", str(budget)]
    )
    assert rc == 0
    sent = str(llm_server.requests[0]["json"]["messages"])
    assert "presence:Ghost:desktop_folder" in sent  # non-PASS survives truncation
    assert "web:CaptchaApp" in sent
    assert '"type": "summary"' in sent or '"type":"summary"' in sent
    assert len(sent.encode()) < full_size  # actually truncated


def test_transport_error_exits_3(analyse_env, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", _helpers.closed_port_url())
    log_path = _make_log(analyse_env)
    rc = _helpers.call_main(analyse_cli.main, ["--log", str(log_path)])
    assert rc == 3


def test_http_error_from_llm_exits_3(analyse_env, llm_server):
    llm_server.status = 500
    log_path = _make_log(analyse_env)
    rc = _helpers.call_main(analyse_cli.main, ["--log", str(log_path)])
    assert rc == 3


def test_missing_env_is_an_llm_error(analyse_env, monkeypatch, capsys):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    log_path = _make_log(analyse_env)
    rc = _helpers.call_main(analyse_cli.main, ["--log", str(log_path)])
    assert rc == 3  # only documented non-zero analyse exit code
    err = capsys.readouterr().err
    assert "LLM_BASE_URL" in err or "LLM" in err  # clear, actionable message


def test_json_mode_runs_and_requests_strict_json(analyse_env, llm_server):
    """--json asks the model for strict-JSON output; the call must succeed."""
    llm_server.content = '{"recommendation": "HOLD", "anomalies": []}'
    log_path = _make_log(analyse_env)
    rc = _helpers.call_main(analyse_cli.main, ["--log", str(log_path), "--json"])
    assert rc == 0
    payload = llm_server.requests[0]["json"]
    # strict-JSON request mode: via response_format or an explicit instruction
    asked = "json" in str(payload).lower()
    assert asked, "expected the --json request to mention JSON output mode"
