"""``codefix review`` and ``codefix check``."""
from __future__ import annotations

import sys

from .output import emit_json, print_review, to_sarif


def register(sub, common) -> None:
    p = sub.add_parser("review", parents=[common], help="analyse files or directories (never writes)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--sarif", action="store_true", help="SARIF 2.1.0 output on stdout")
    p.set_defaults(handler=run_review)
    c = sub.add_parser("check", parents=[common], help="verify syntax (and formatting when a formatter is configured)")
    c.add_argument("paths", nargs="+")
    c.set_defaults(handler=run_check, sarif=False)


def run_review(args) -> int:
    from .main import make_app
    app = make_app(args, args.paths[0])
    report = app.review(args.paths)
    if args.sarif:
        emit_json(to_sarif(report, app.registry), sys.stdout)
    elif args.json:
        emit_json(report.to_dict(), sys.stdout)
    else:
        print_review(report, sys.stdout)
    return report.exit_code


def run_check(args) -> int:
    from .main import make_app
    app = make_app(args, args.paths[0])
    result = app.check(args.paths)
    if args.json:
        emit_json(result, sys.stdout)
    else:
        for e in result["files"]:
            state = "ok" if e["syntax_ok"] and e["formatted"] is not False else "FAIL"
            note = e["error"] or ("needs formatting" if e["formatted"] is False else "")
            sys.stdout.write(f"{e['path']}: {state} {note}\n")
        for err in result["errors"]:
            sys.stdout.write(f"ERROR {err}\n")
    return result["exit_code"]
