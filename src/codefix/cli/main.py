"""Command line entry point."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .. import __version__
from ..application import EXIT_ERROR, CodeFix
from ..config import ConfigError, load_config
from . import diagnostics, fix, knowledge, project, review


def common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--config", metavar="FILE", help="path to codefix.toml (default: search upwards)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    p.add_argument("--no-color", action="store_true", help="disable coloured diffs")
    p.add_argument("-v", "--verbose", action="store_true", help="show verification steps")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codefix", description="Safe, explainable Python code review, "
                                     "correction, refactoring and verification.")
    parser.add_argument("--version", action="version", version=f"codefix {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    common = common_parser()
    for mod in (review, fix, diagnostics, project, knowledge):
        mod.register(sub, common)
    return parser


def make_app(args: argparse.Namespace, start: str | None = None, **kw: Any) -> CodeFix:
    config = load_config(start or ".", args.config)
    machine = getattr(args, "json", False) or getattr(args, "sarif", False)
    out = sys.stderr if machine else sys.stdout
    color = (not args.no_color) and out.isatty() and "NO_COLOR" not in os.environ
    for w in config.warnings:
        sys.stderr.write(f"codefix: config warning: {w}\n")
    return CodeFix(config, out=out, color=color, **kw)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        sys.stderr.write(f"codefix: configuration error: {exc}\n")
        return EXIT_ERROR
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("codefix: interrupted (no partial writes: files are replaced atomically)\n")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
