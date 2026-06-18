"""Entry point for ``python -m jiopc_agent``."""

import sys

from jiopc_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
