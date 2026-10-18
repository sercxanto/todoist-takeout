"""Command-line entry point."""

import argparse
import sys

from todoist_takeout import __version__

PROG = "todoist-takeout"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Keep a complete, long-term readable local backup of a Todoist account.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_usage(sys.stderr)
    return 2
