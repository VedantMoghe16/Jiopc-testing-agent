#!/usr/bin/env python3
"""Thin shim so the LLM analysis layer works straight from a repo checkout:

    python analyse.py [--log <path>] [--json] [--max-log-bytes N]

It simply puts ./src on sys.path and delegates to jiopc_agent.analyse_cli.main().
Installed entry points (pip / .deb) use jiopc_agent.analyse_cli:main directly.

stdlib only at runtime; needs LLM_BASE_URL + LLM_MODEL (LLM_API_KEY optional).
Exit codes: 0 success; 2 usage error; 3 LLM/transport error.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jiopc_agent.analyse_cli import main  # noqa: E402  (path setup must precede import)

if __name__ == "__main__":
    sys.exit(main())
