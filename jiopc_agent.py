#!/usr/bin/env python3
"""Thin shim so the PDF CLI works straight from a repo checkout:

    python jiopc_agent.py --config jiopc-agent.yaml [--part A|B|C] [--analyse]

It simply puts ./src on sys.path and delegates to jiopc_agent.cli.main().
Installed entry points (pip / .deb) use jiopc_agent.cli:main directly.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jiopc_agent.cli import main  # noqa: E402  (path setup must precede import)

if __name__ == "__main__":
    sys.exit(main())
