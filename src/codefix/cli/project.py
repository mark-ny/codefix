"""Refactoring commands and project helpers: rename, replace, stub, project init/info."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .output import emit_json, print_run


def register(sub, common) -> None:
    r = sub.add_parser("rename", parents=[common], help="scope-aware rename of a symbol in one file")
    r.add_argument("path"); r.add_argument("old"); r.add_argument("new")
    r.add_argument("--scope", help="qualified scope that owns the name, e.g. 'Class.method' (default: outermost)")
    r.add_argument("--engine", choices=["auto", "ast", "libcst"], default="auto")
    r.add_argument("--write", action="store_true")
    r.set_defaults(handler=run_rename, sarif=False)

    rp = sub.add_parser("replace", parents=[common], help="replace one function with the code in a file")
    rp.add_argument("path"); rp.add_argument("function")
    rp.add_argument("--from-file", required=True, metavar="FILE", dest="from_file")
    rp.add_argument("--write", action="store_true")
    rp.set_defaults(handler=run_replace, sarif=False)

    s = sub.add_parser("stub", parents=[common], help="stub a function body with NotImplementedError (or add a new stub)")
    s.add_argument("path"); s.add_argument("function")
    s.add_argument("--write", action="store_true")
    s.set_defaults(handler=run_stub, sarif=False)

    pj = sub.add_parser("project", parents=[common], help="project helpers")
    psub = pj.add_subparsers(dest="project_command", required=True)
    pi = psub.add_parser("init", parents=[common], help="write a starter codefix.toml")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(handler=run_init, sarif=False)
    pn = psub.add_parser("info", parents=[common], help="show the effective configuration")
    pn.set_defaults(handler=run_info, sarif=False)


def _go(args, fn_name: str, *rest) -> int:
    from .main import make_app
    app = make_app(args, args.path)
    report = getattr(app, fn_name)(args.path, *rest)
    if args.json:
        emit_json(report.to_dict(), sys.stdout)
    else:
        print_run(report, sys.stdout, args.verbose)
    return report.exit_code


def run_rename(args) -> int:
    return _go(args, "rename", args.old, args.new, args.write, args.scope, args.engine)


def run_replace(args) -> int:
    return _go(args, "replace", args.function, args.from_file, args.write)


def run_stub(args) -> int:
    return _go(args, "stub", args.function, args.write)


def run_init(args) -> int:
    target = Path("codefix.toml")
    if target.exists() and not args.force:
        sys.stderr.write("codefix.toml already exists (use --force to overwrite)\n")
        return 2
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "codefix.toml.example", here.parents[1] / "_knowledge" / ".." / "codefix.toml.example"):
        if cand.is_file():
            shutil.copyfile(cand, target)
            break
    else:
        target.write_text('[behavior]\nformatter = "none"\nminimum_confidence = 0.80\n\n[knowledge]\npacks = ["python"]\n')
    sys.stdout.write(f"wrote {target}\n")
    return 0


def run_info(args) -> int:
    from .main import make_app
    app = make_app(args, ".")
    data = app.config.to_dict()
    data["rules"] = len(app.registry.rules())
    if args.json:
        emit_json(data, sys.stdout)
    else:
        for k, v in data.items():
            sys.stdout.write(f"{k}: {v}\n")
    return 0
