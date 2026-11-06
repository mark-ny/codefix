"""``codefix knowledge list|show|validate|test|feedback``."""
from __future__ import annotations

import sys

from .output import emit_json


def register(sub, common) -> None:
    k = sub.add_parser("knowledge", parents=[common], help="inspect, validate and test the rule knowledge base")
    ks = k.add_subparsers(dest="knowledge_command", required=True)
    ls = ks.add_parser("list", parents=[common], help="list rules")
    ls.add_argument("--category"); ls.add_argument("--pack")
    ls.set_defaults(handler=run_list, sarif=False)
    sh = ks.add_parser("show", parents=[common], help="show one rule in detail")
    sh.add_argument("rule_id")
    sh.set_defaults(handler=run_show, sarif=False)
    ks.add_parser("validate", parents=[common], help="validate knowledge packs").set_defaults(handler=run_validate, sarif=False)
    t = ks.add_parser("test", parents=[common], help="run every rule's bad/good/negative examples")
    t.add_argument("--rule")
    t.set_defaults(handler=run_test, sarif=False)
    fb = ks.add_parser("feedback", parents=[common], help="record advisory feedback (never changes rules)")
    fb.add_argument("rule_id", nargs="?")
    fb.add_argument("--verdict", choices=["false_positive", "helpful", "wrong_fix", "other"])
    fb.add_argument("--note", default=""); fb.add_argument("--file", default="")
    fb.set_defaults(handler=run_feedback, sarif=False)


def _app(args):
    from .main import make_app
    return make_app(args, ".")


def run_list(args) -> int:
    app = _app(args)
    specs = [s for s in app.registry.specs() if (not args.category or s.category == args.category)
             and (not args.pack or s.pack == args.pack)]
    if args.json:
        emit_json([s.to_dict() for s in specs], sys.stdout)
        return 0
    for s in specs:
        fix = "fix" if s.autofix else "   "
        impl = "" if s.implemented else " (docs only)"
        sys.stdout.write(f"{s.id:<22} {s.category:<16} {s.severity.value:<7} {s.confidence:.2f} {fix}  {s.message}{impl}\n")
    sys.stdout.write(f"\n{len(specs)} rule(s)\n")
    return 0


def run_show(args) -> int:
    spec = _app(args).registry.spec(args.rule_id)
    if spec is None:
        sys.stderr.write(f"unknown rule {args.rule_id}\n")
        return 2
    if args.json:
        emit_json(spec.to_dict(), sys.stdout)
        return 0
    w = sys.stdout.write
    w(f"{spec.id}  [{spec.category}, {spec.severity.value}, confidence {spec.confidence}]\n{spec.message}\n\n")
    if spec.explanation or spec.description:
        w((spec.explanation or spec.description) + "\n\n")
    w(f"Autofix: {'yes' if spec.autofix else 'no'}   Requires tests: {'yes' if spec.requires_tests else 'no'}   Pack: {spec.pack}\n\n")
    w(f"Bad:\n{spec.bad}\n\nGood:\n{spec.good}\n")
    if spec.false_positive:
        w(f"\nFalse positives: {spec.false_positive}\n")
    if spec.docs:
        w("\nDocs: " + ", ".join(spec.docs) + "\n")
    return 0


def run_validate(args) -> int:
    from ..knowledge.validator import validate_knowledge
    app = _app(args)
    issues = validate_knowledge(app.registry, app.config)
    errors = [i for i in issues if i.level == "error"]
    if args.json:
        emit_json({"ok": not errors, "issues": [i.to_dict() for i in issues]}, sys.stdout)
    else:
        for i in issues:
            sys.stdout.write(f"{i.level.upper():<8} {i.rule_id}: {i.message}" + (f"  ({i.where})" if i.where else "") + "\n")
        sys.stdout.write(f"\n{len(errors)} error(s), {len(issues) - len(errors)} warning(s)\n")
    return 2 if errors else 0


def run_test(args) -> int:
    from ..knowledge.validator import run_examples
    app = _app(args)
    results = run_examples(app.registry, app.config, args.rule)
    bad = [r for r in results if not r.ok]
    if args.json:
        emit_json({"ok": not bad, "total": len(results), "failed": [r.__dict__ for r in bad]}, sys.stdout)
    else:
        for r in bad:
            sys.stdout.write(f"FAIL {r.rule_id} / {r.name}: {r.message}\n")
        sys.stdout.write(f"{len(results) - len(bad)}/{len(results)} example(s) passed\n")
    return 2 if bad else 0


def run_feedback(args) -> int:
    from ..knowledge.feedback import FeedbackStore
    app = _app(args)
    store = FeedbackStore(app.config.root)
    if args.rule_id and args.verdict:
        store.record(args.rule_id, args.verdict, args.note, args.file)
        sys.stdout.write("feedback recorded (advisory only: no rule or setting was changed)\n")
        return 0
    summary = store.summary()
    if args.json:
        emit_json(summary, sys.stdout)
    else:
        for rid, c in summary.items():
            sys.stdout.write(f"{rid}: {c}\n")
        if not summary:
            sys.stdout.write("no feedback recorded\n")
    return 0
