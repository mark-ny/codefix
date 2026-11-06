"""``codefix diagnostics`` and ``codefix test``."""
from __future__ import annotations

import sys

from .output import emit_json, print_review, to_sarif


def register(sub, common) -> None:
    d = sub.add_parser("diagnostics", parents=[common], help="project-wide findings and metrics")
    d.add_argument("paths", nargs="+")
    d.add_argument("--metrics", action="store_true", help="include complexity/size metrics")
    d.add_argument("--sarif", action="store_true")
    d.set_defaults(handler=run_diagnostics)
    t = sub.add_parser("test", parents=[common], help="run the project's tests with failure attribution")
    t.add_argument("targets", nargs="*", help="test files, directories or node ids")
    t.set_defaults(handler=run_tests, sarif=False)


def run_diagnostics(args) -> int:
    from .main import make_app
    app = make_app(args, args.paths[0])
    data = app.diagnostics(args.paths, metrics=args.metrics)
    if args.sarif:
        emit_json(to_sarif(app.review(args.paths), app.registry), sys.stdout)
    elif args.json:
        emit_json(data, sys.stdout)
    else:
        print_review(app.review(args.paths), sys.stdout)
        s = data["summary"]
        sys.stdout.write(f"{s['lines']} line(s), {s['functions']} function(s), {s['syntax_errors']} syntax error(s)\n")
        if args.metrics and "metrics" in s:
            m = s["metrics"]
            sys.stdout.write(f"complexity: avg {m['average_complexity']}, max {m['max_complexity']}; "
                             f"{m['over_complexity_limit']} over limit; {m['over_length_limit']} too long\n")
            for t in m["most_complex"][:5]:
                sys.stdout.write(f"  {t['file']}:{t['line']} {t['function']} complexity {t['complexity']}\n")
    return data["exit_code"]


def run_tests(args) -> int:
    from .main import make_app
    app = make_app(args, ".")
    result = app.test(args.targets)
    if args.json:
        emit_json(result.to_dict(), sys.stdout)
    else:
        sys.stdout.write(result.stdout)
        if result.stderr.strip():
            sys.stderr.write(result.stderr)
        if result.error:
            sys.stdout.write(f"\nERROR: {result.error}\n")
        for f in result.failures:
            fn = ", ".join(f"{x['function']} ({x['file']}:{x['line']})" for x in f.likely_functions[:3])
            sys.stdout.write(f"\nFAILED {f.test}\n  {f.file}:{f.line}  {f.message}\n"
                             + (f"  likely affected: {fn}\n" if fn else ""))
    return 0 if result.ok else 2
