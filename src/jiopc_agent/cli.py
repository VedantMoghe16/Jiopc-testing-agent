"""Command-line interface.

::

    python jiopc_agent.py --config jiopc-agent.yaml [--part A] [--part B] [--part C]
                          [--analyse] [--parallel] [--dry-run] [--no-email]

Exit codes (binding): 0 all required tests pass; 1 ≥1 required failure;
2 config/usage error. ``--analyse`` never changes the run's exit code.

stderr is for humans (progress tee, warnings); stdout is reserved for the
``--dry-run`` listing and the LLM analysis output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jiopc_agent import __version__
from jiopc_agent.config import AgentConfig, ConfigError, load_config
from jiopc_agent import runner

EXIT_OK = 0
EXIT_TEST_FAILURE = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser for the agent CLI (also used by the shim)."""
    parser = argparse.ArgumentParser(
        prog="jiopc-agent",
        description=(
            "JioPC image validation agent: web apps (A), native apps (B), "
            "desktop/start-menu presence (C). Writes a JSONL log; "
            "use --analyse (or analyse.py) for the post-run LLM summary."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="YAML",
        help="path to the agent YAML config (e.g. jiopc-agent.yaml)",
    )
    parser.add_argument(
        "--part",
        action="append",
        choices=("A", "B", "C"),
        dest="parts",
        metavar="{A,B,C}",
        help="run only this part; repeatable (execution order = agent.part_order)",
    )
    parser.add_argument(
        "--analyse",
        action="store_true",
        help="after the run, send the fresh log to the LLM analysis layer "
        "(needs LLM_BASE_URL / LLM_MODEL env vars)",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="bonus: run Parts A and C concurrently; Part B always exclusive after",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate config and list planned tests; touch nothing",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="skip the summary email even if enabled in YAML",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _run_analysis(cfg: AgentConfig, log_path: Path | None) -> None:
    """Invoke the LLM analysis layer on the fresh log, defensively.

    Per spec: if the layer or its env vars are missing, print a clear message
    and leave the run's exit code untouched.
    """
    if log_path is None:
        print("analysis skipped: no log produced this run", file=sys.stderr)
        return
    try:
        import importlib

        mod = importlib.import_module("jiopc_agent.analyse_cli")
    except Exception as exc:  # noqa: BLE001 - analysis layer is post-run only
        print(
            f"analysis unavailable ({exc}); run 'python analyse.py --log "
            f"{log_path}' once the analysis layer is installed",
            file=sys.stderr,
        )
        return
    try:
        analyse = getattr(mod, "analyse_log", None)
        if callable(analyse):
            analyse(log_path, cfg)
        else:
            mod.main(["--log", str(log_path)])
    except Exception as exc:  # noqa: BLE001 - never mask the run's exit code
        print(f"analysis failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        outcome = runner.run(
            cfg,
            parts=args.parts,
            parallel=args.parallel,
            dry_run=args.dry_run,
            no_email=args.no_email,
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_TEST_FAILURE

    if args.analyse and not args.dry_run:
        _run_analysis(cfg, outcome.log_path)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
