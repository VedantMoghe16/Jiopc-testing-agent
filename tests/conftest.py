"""Shared pytest fixtures for the jiopc-testing-agent suite.

Provides:

* ``web_server``        — session-scoped threaded http.server over
                          tests/fixtures/web (ok / slow / captcha /
                          missing-elements pages, plus an injected /500).
* ``llm_server``        — function-scoped mock OpenAI-compatible
                          ``/chat/completions`` endpoint that records requests.
* ``playwright_ready``  — session-scoped skip gate: skips the requesting test
                          when Playwright or headless Chromium is unavailable.
* path constants        — REPO_ROOT, FIXTURES_DIR, DESKTOP_TREE, FAKE_APP, ...

All agent imports go through ``src/`` (inserted on sys.path below), matching
the repo-checkout shim behaviour.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = TESTS_DIR / "fixtures"
WEB_DIR = FIXTURES_DIR / "web"
DESKTOP_TREE = FIXTURES_DIR / "desktop_tree"
FAKE_APP = FIXTURES_DIR / "fake_app.py"
FIXTURE_CONFIG = TESTS_DIR / "fixture-config.yaml"

#: port written in fixture-config.yaml; the session server prefers it so the
#: static config works unchanged, but falls back to an ephemeral port.
FIXTURE_PORT = 8901
#: server-side delay added to /slow.html (seconds); thresholds in configs are
#: far below this so the "slow load" FAIL is deterministic.
SLOW_DELAY_S = 1.5
#: process name fake_app.py renames itself to (see fixtures/fake_app.py).
FAKE_PROCESS_NAME = "jiopc-fake-app"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# Fixture web server (Part A + end-to-end)
# ---------------------------------------------------------------------------


class _WebHandler(SimpleHTTPRequestHandler):
    """Static files from tests/fixtures/web, with two dynamic behaviours."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == "/500":
            self.send_error(500, "fixture-injected server error")
            return
        if path.startswith("/slow"):
            time.sleep(SLOW_DELAY_S)
        super().do_GET()

    def log_message(self, fmt, *args):  # silence per-request stderr noise
        pass


def _start_web_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _WebHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="fixture-web", daemon=True).start()
    return server


@pytest.fixture(scope="session")
def web_server():
    """Threaded HTTP server over tests/fixtures/web; yields .port / .base_url."""
    try:
        server = _start_web_server(FIXTURE_PORT)
    except OSError:  # port taken (parallel CI job, leftover process)
        server = _start_web_server(0)
    port = server.server_address[1]
    yield SimpleNamespace(port=port, base_url=f"http://127.0.0.1:{port}")
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Mock OpenAI-compatible LLM endpoint (analyse tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def llm_server():
    """Mock ``POST {base}/chat/completions`` endpoint.

    Yields an object with ``base_url`` (ends in /v1), ``requests`` (list of
    captured {path, headers, json} dicts), and mutable ``content`` / ``status``
    knobs to shape the next response.
    """
    state = SimpleNamespace(
        requests=[],
        content="**Executive summary**\nAll fixture tests inspected.\n\n**PROMOTE**",
        status=200,
        base_url="",
    )

    class _LLMHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"_raw": body.decode("utf-8", errors="replace")}
            state.requests.append(
                {"path": self.path, "headers": dict(self.headers), "json": payload}
            )
            if state.status != 200:
                self.send_error(state.status, "fixture-injected LLM error")
                return
            data = json.dumps(
                {
                    "id": "mock-1",
                    "object": "chat.completion",
                    "model": payload.get("model", "mock-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": state.content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LLMHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="mock-llm", daemon=True).start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield state
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Playwright availability gate (Part A must skip cleanly without it)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def playwright_skip_reason() -> str:
    """'' when headless Chromium works; otherwise a human-readable skip reason."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - any import problem means skip
        return f"playwright not installed: {exc}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - missing browser binary etc.
        return f"headless Chromium unavailable: {exc}"
    return ""


@pytest.fixture(scope="session")
def playwright_ready() -> bool:
    """Skip the requesting test unless Playwright + headless Chromium work."""
    reason = playwright_skip_reason()
    if reason:
        pytest.skip(f"Part A skipped: {reason}")
    return True
