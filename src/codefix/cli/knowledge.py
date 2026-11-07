"""``codefix knowledge list|show|validate|test|feedback``."""
from __future__ import annotations

import json
import sys

from .output import emit_json


def register(sub, common) -> None:
    k = sub.add_parser("knowledge", parents=[common], help="inspect, validate and test the rule knowledge base")
    ks = k.add_subparsers(dest="knowledge_command", required=True)
    ls = ks.add_parser("list", parents=[common], help="list rules")
    ls.add_argument("--category"); ls.add_argument("--pack")
    ls.add_argument("--user", action="store_true", help="only rules you taught CodeFix (knowledge/project/user_rules.json)")
    ls.set_defaults(handler=run_list, sarif=False)
    sh = ks.add_parser("show", parents=[common], help="show one rule in detail")
    sh.add_argument("rule_id")
    sh.set_defaults(handler=run_show, sarif=False)
    ks.add_parser("validate", parents=[common], help="validate knowledge packs").set_defaults(handler=run_validate, sarif=False)
    t = ks.add_parser("test", parents=[common], help="run every rule's bad/good/negative examples")
    t.add_argument("--rule")
    t.set_defaults(handler=run_test, sarif=False)
    te = ks.add_parser("teach", parents=[common], help="teach a new rule from a before/after example")
    te.add_argument("rule_id", help="UPPER_SNAKE_CASE id, e.g. USER_NO_HAS_KEY")
    te.add_argument("--bad", help="code that should be flagged"); te.add_argument("--bad-file")
    te.add_argument("--good", help="what it should become (omit for a report-only rule)"); te.add_argument("--good-file")
    te.add_argument("--message", required=True, help="one-line explanation shown with each finding")
    te.add_argument("--severity", choices=["safe", "review", "risky"], default="review")
    te.add_argument("--confidence", type=float, default=0.9)
    te.add_argument("--explanation", default=""); te.add_argument("--negative", action="append", default=[],
                    help="code that must NOT be flagged (repeatable)")
    te.add_argument("--replace", action="store_true", help="overwrite an existing user rule with this id")
    te.add_argument("--dry-run", action="store_true", help="show the learned pattern and test it without saving")
    te.set_defaults(handler=run_teach, sarif=False)
    rm = ks.add_parser("remove", parents=[common], help="delete a rule you taught")
    rm.add_argument("rule_id"); rm.set_defaults(handler=run_remove, sarif=False)
    ex = ks.add_parser("export", parents=[common], help="export your rules (+ examples) as a shareable JSON pack")
    ex.add_argument("-o", "--output"); ex.set_defaults(handler=run_export, sarif=False)
    im = ks.add_parser("import", parents=[common], help="import a JSON pack of declarative rules")
    im.add_argument("file"); im.add_argument("--replace", action="store_true")
    im.set_defaults(handler=run_import, sarif=False)
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
    from ..knowledge.userpack import UserPack
    mine = {r.get("id") for r in UserPack(app.config).load()} if args.user else None
    specs = [s for s in app.registry.specs() if (not args.category or s.category == args.category)
             and (not args.pack or s.pack == args.pack) and (mine is None or s.id in mine)]
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


def _read_arg(inline, file, what):
    if file:
        from pathlib import Path
        return Path(file).read_text(encoding="utf-8")
    return inline


def run_teach(args) -> int:
    from ..knowledge.declarative import RuleError
    from ..knowledge.userpack import UserPack, entry_from_example
    app = _app(args)
    bad, good = _read_arg(args.bad, args.bad_file, "bad"), _read_arg(args.good, args.good_file, "good")
    if not bad:
        sys.stderr.write("give the code to flag with --bad or --bad-file\n")
        return 2
    try:
        entry, examples = entry_from_example(args.rule_id, args.message, bad, good, severity=args.severity,
                                             confidence=args.confidence, explanation=args.explanation,
                                             negative=args.negative)
    except RuleError as exc:
        sys.stderr.write(f"cannot learn a rule from that example: {exc}\n")
        return 2
    pack = UserPack(app.config)
    problems = pack.selftest(entry, examples) if args.dry_run else pack.save(entry, examples, replace=args.replace)
    m = entry["match"]
    sys.stdout.write(f"learned pattern: {m['pattern']}\n")
    if "replace" in m:
        sys.stdout.write(f"learned fix:     {m['replace']}\n")
    if problems:
        for p in problems:
            sys.stdout.write(f"PROBLEM: {p}\n")
        sys.stdout.write("not saved\n")
        return 2
    sys.stdout.write("self-test passed" + ("" if args.dry_run else f"; saved to {pack.path}") + "\n")
    return 0


def run_remove(args) -> int:
    from ..knowledge.userpack import UserPack
    ok = UserPack(_app(args).config).delete(args.rule_id)
    sys.stdout.write("removed\n" if ok else f"no user rule named {args.rule_id}\n")
    return 0 if ok else 2


def run_export(args) -> int:
    from ..knowledge.userpack import UserPack
    data = UserPack(_app(args).config).export()
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(text, encoding="utf-8")
        sys.stdout.write(f"wrote {len(data['rules'])} rule(s) to {args.output}\n")
    else:
        sys.stdout.write(text)
    return 0


def run_import(args) -> int:
    from pathlib import Path
    from ..knowledge.userpack import UserPack
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"cannot read {args.file}: {exc}\n")
        return 2
    res = UserPack(_app(args).config).import_pack(data, replace=args.replace)
    if args.json:
        emit_json(res, sys.stdout)
    else:
        sys.stdout.write(f"imported: {', '.join(res['imported']) or 'none'}\n")
        for rid, probs in res["rejected"].items():
            sys.stdout.write(f"rejected {rid}: {'; '.join(probs)}\n")
    return 2 if res["rejected"] else 0
