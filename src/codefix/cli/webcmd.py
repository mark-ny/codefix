"""``codefix web``: open the local web interface."""
from __future__ import annotations


def register(sub, common) -> None:
    w = sub.add_parser("web", help="open the browser interface (paste code, fix files, teach new rules)",
                       add_help=False)
    w.add_argument("rest", nargs="*", help=argparse_help())
    w.set_defaults(handler=run, sarif=False, json=False, no_color=True, config=None, verbose=False)


def argparse_help() -> str:
    return "options: --port N --host H --root DIR (repeatable) --config FILE --no-browser"


def run(args) -> int:
    from ..web.server import main
    return main(list(args.rest))
