"""``fix``, ``autofix``, ``approve``, ``undo``, ``format`` and ``apply-proposal``."""
from __future__ import annotations

import os
import sys

from ..application import EXIT_ERROR, EXIT_OK, FixOptions
from .output import emit_json, print_run


def _approval_flags(p) -> None:
    p.add_argument("--write", action="store_true", help="apply changes (default is a dry run that only shows the diff)")
    p.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    p.add_argument("--yes", "-y", action="store_true", help="accept review-level fixes without prompting "
                   "(risky/security fixes still need --approve-rule)")
    p.add_argument("--ci", action="store_true", help="never prompt; apply only what policy allows")
    p.add_argument("--approve-rule", action="append", default=[], metavar="RULE_ID",
                   help="pre-approve a rule, including risky ones (repeatable)")
    p.add_argument("--rule", action="append", default=[], metavar="RULE_ID", help="only consider these rules")
    p.add_argument("--no-format", action="store_true", help="skip the configured formatter")


def register(sub, common) -> None:
    f = sub.add_parser("fix", parents=[common], help="propose and apply fixes with interactive approval")
    f.add_argument("paths", nargs="+")
    _approval_flags(f)
    f.add_argument("--passes", type=int, default=1, help="re-analysis passes (default 1)")
    f.set_defaults(handler=lambda a: run_fix(a, "fix"), sarif=False)

    a = sub.add_parser("autofix", parents=[common], help="apply only safe, high-confidence fixes, never prompting")
    a.add_argument("paths", nargs="+")
    _approval_flags(a)
    a.add_argument("--safe-only", action="store_true", help="only severity=safe fixes (this is already the default)")
    a.add_argument("--passes", type=int, default=0, help="re-analysis passes (default: [behavior] max_passes)")
    a.set_defaults(handler=lambda a_: run_fix(a_, "autofix"), sarif=False)

    ap = sub.add_parser("approve", parents=[common], help="review pending review/risky fixes one by one and approve them")
    ap.add_argument("paths", nargs="+")
    _approval_flags(ap)
    ap.set_defaults(handler=lambda a_: run_fix(a_, "approve"), sarif=False, passes=1)

    u = sub.add_parser("undo", parents=[common], help="restore the most recent backup of a file")
    u.add_argument("path")
    u.add_argument("--list", action="store_true", help="list available backups instead of restoring")
    u.set_defaults(handler=run_undo, sarif=False)

    fm = sub.add_parser("format", parents=[common], help="run the configured formatter in memory, validate, diff")
    fm.add_argument("paths", nargs="+")
    fm.add_argument("--write", action="store_true")
    fm.set_defaults(handler=run_format, sarif=False)

    pr = sub.add_parser("apply-proposal", parents=[common],
                        help="run an AI/human structured patch proposal through the safety pipeline")
    pr.add_argument("proposal", help="JSON proposal file (see docs/architecture.md)")
    _approval_flags(pr)
    pr.set_defaults(handler=run_proposal, sarif=False)


def _opts(args, command: str) -> FixOptions:
    ci = args.ci or os.environ.get("CI", "").lower() in ("1", "true", "yes")
    write = args.write and not args.dry_run
    interactive = write and not args.yes and not ci and sys.stdin.isatty()
    passes = getattr(args, "passes", 1)
    return FixOptions(command=command, write=write, yes=args.yes, ci=ci, interactive=interactive,
                      approve_rules=set(args.approve_rule), only_rules=set(args.rule) or None,
                      safe_only=getattr(args, "safe_only", False), format=not args.no_format,
                      passes=passes, preview=not write)


def _finish(app, report, args) -> int:
    if args.json:
        emit_json(report.to_dict(), sys.stdout)
    else:
        print_run(report, sys.stdout, args.verbose)
    return report.exit_code


def run_fix(args, command: str) -> int:
    from .main import make_app
    app = make_app(args, args.paths[0])
    opts = _opts(args, command)
    if command == "autofix":
        opts.passes = args.passes or app.config.max_passes
    return _finish(app, app.fix(args.paths, opts), args)


def run_proposal(args) -> int:
    from .main import make_app
    app = make_app(args, ".")
    return _finish(app, app.apply_proposals(args.proposal, _opts(args, "fix")), args)


def run_format(args) -> int:
    from .main import make_app
    app = make_app(args, args.paths[0])
    return _finish(app, app.format(args.paths, write=args.write), args)


def run_undo(args) -> int:
    from .main import make_app
    app = make_app(args, args.path)
    result = app.undo(args.path, list_only=args.list)
    if args.json:
        emit_json(result, sys.stdout)
    elif args.list:
        for r in result["backups"]:
            sys.stdout.write(f"{r['timestamp']}  {r['kind']:<8} {'(used)' if r['consumed'] else ''}  {r['backup']}\n")
        if not result["backups"]:
            sys.stdout.write("no backups\n")
    elif result["restored"]:
        sys.stdout.write(f"restored {result['path']} from {result['from_backup']}\n")
    else:
        sys.stdout.write(f"cannot undo: {result['error']}\n")
    return EXIT_OK if args.list or result.get("restored") else EXIT_ERROR
