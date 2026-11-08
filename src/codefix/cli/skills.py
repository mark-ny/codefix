"""``codefix skill ...`` and ``codefix policy ...``: create, learn and edit knowledge; edit what gets fixed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .output import emit_json


def register(sub, common) -> None:
    k = sub.add_parser("skill", parents=[common], help="create, learn and manage skills (named knowledge packs)")
    ks = k.add_subparsers(dest="skill_command", required=True)
    ks.add_parser("list", parents=[common], help="list skills").set_defaults(handler=run_list, sarif=False)
    sh = ks.add_parser("show", parents=[common], help="show a skill's rules")
    sh.add_argument("name"); sh.set_defaults(handler=run_show, sarif=False)
    nw = ks.add_parser("new", parents=[common], help="create an empty skill")
    nw.add_argument("name"); nw.add_argument("--title", default=""); nw.add_argument("--description", default="")
    nw.set_defaults(handler=run_new, sarif=False)
    va = ks.add_parser("validate", parents=[common], help="dry-run: check a skill file without installing it")
    va.add_argument("file"); va.set_defaults(handler=run_validate, sarif=False)
    tr = ks.add_parser("try", parents=[common], help="run a skill file on code without installing it")
    tr.add_argument("file"); tr.add_argument("--code-file", required=True)
    tr.set_defaults(handler=run_try, sarif=False)
    le = ks.add_parser("learn", parents=[common], help="install a skill file (all rules must validate)")
    le.add_argument("file"); le.add_argument("--replace", action="store_true")
    le.set_defaults(handler=run_learn, sarif=False)
    for name, flag in (("enable", True), ("disable", False)):
        e = ks.add_parser(name, parents=[common], help=f"{name} a skill")
        e.add_argument("name"); e.set_defaults(handler=run_enable, enabled=flag, sarif=False)
    fo = ks.add_parser("forget", parents=[common], help="delete a skill")
    fo.add_argument("name"); fo.set_defaults(handler=run_forget, sarif=False)
    rr = ks.add_parser("remove-rule", parents=[common], help="delete one rule from a skill")
    rr.add_argument("name"); rr.add_argument("rule_id"); rr.set_defaults(handler=run_remove_rule, sarif=False)
    ex = ks.add_parser("export", parents=[common], help="print or save a skill file")
    ex.add_argument("name"); ex.add_argument("-o", "--output"); ex.set_defaults(handler=run_export, sarif=False)

    p = sub.add_parser("policy", parents=[common], help="edit what CodeFix fixes, reports or ignores")
    ps = p.add_subparsers(dest="policy_command", required=True)
    pl = ps.add_parser("show", parents=[common], help="show the effective policy of every rule")
    pl.add_argument("--changed", action="store_true", help="only rules you customised")
    pl.set_defaults(handler=run_policy_show, sarif=False)
    st = ps.add_parser("set", parents=[common], help="change how one rule behaves")
    st.add_argument("rule_id")
    st.add_argument("--mode", choices=["enabled", "report_only", "disabled", "blocked"],
                    help="enabled: normal; report_only: flag but never fix; disabled: never run; blocked: never apply")
    st.add_argument("--severity", choices=["safe", "review", "risky"], help="how risky its fix is (safe = applied automatically)")
    st.add_argument("--exclude", action="append", metavar="GLOB", help="do not report this rule in matching paths (repeatable)")
    st.add_argument("--clear-exclude", action="store_true"); st.add_argument("--clear-severity", action="store_true")
    st.set_defaults(handler=run_policy_set, sarif=False)
    bh = ps.add_parser("behavior", parents=[common], help="global switches")
    bh.add_argument("--allow-risky", choices=["true", "false"]); bh.add_argument("--min-confidence", type=float)
    bh.set_defaults(handler=run_policy_behavior, sarif=False)
    rs = ps.add_parser("reset", parents=[common], help="remove your customisation of a rule (or all)")
    rs.add_argument("rule_id", nargs="?"); rs.add_argument("--all", action="store_true")
    rs.set_defaults(handler=run_policy_reset, sarif=False)


def _app(args):
    from .main import make_app
    return make_app(args, ".")


def _load(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path}: {exc}"


def _print_report(report: dict, args) -> None:
    if args.json:
        emit_json({k: v for k, v in report.items() if k != "data"}, sys.stdout)
        return
    w = sys.stdout.write
    for p in report.get("problems", []):
        w(f"PROBLEM: {p}\n")
    for rid, probs in report.get("rules", {}).items():
        w(f"  {'ok  ' if not probs else 'FAIL'} {rid}\n")
        for p in probs:
            w(f"       {p}\n")
    w(f"{report.get('rule_count', 0)} rule(s); " + ("valid" if report.get("ok") else "NOT valid") + "\n")


def run_list(args) -> int:
    from ..knowledge.skills import SkillStore
    skills = SkillStore(_app(args).config).list()
    if args.json:
        emit_json(skills, sys.stdout)
        return 0
    for s in skills:
        sys.stdout.write(f"{s['name']:<28} {'on ' if s['enabled'] else 'off'} {len(s['rules'])} rule(s)  {s.get('title', '')}\n")
    sys.stdout.write(f"\n{len(skills)} skill(s)\n" if skills else "no skills yet (see AGENT_README.md or `codefix skill new`)\n")
    return 0


def run_show(args) -> int:
    from ..knowledge.skills import SkillStore
    data = SkillStore(_app(args).config).read(args.name)
    if data is None:
        sys.stderr.write(f"no skill named {args.name}\n")
        return 2
    if args.json:
        emit_json(data, sys.stdout)
    else:
        sys.stdout.write(f"{data.get('name')}: {data.get('title', '')}\n{data.get('description', '')}\n\n")
        for r in data.get("rules", []):
            sys.stdout.write(f"  {r['id']:<26} {r.get('severity', 'review'):<7} {r['message']}\n")
    return 0


def run_new(args) -> int:
    from ..knowledge.skills import SkillStore
    rep = SkillStore(_app(args).config).create(args.name, args.title, args.description)
    _print_report(rep, args)
    if rep["ok"]:
        sys.stdout.write(f"created knowledge/skills/{args.name}.json\n") if not args.json else None
    return 0 if rep["ok"] else 2


def run_validate(args) -> int:
    from ..knowledge.skills import SkillStore
    data, err = _load(args.file)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    rep = SkillStore(_app(args).config).validate(data, replacing=data.get("name") if isinstance(data, dict) else None)
    _print_report(rep, args)
    return 0 if rep["ok"] else 2


def run_try(args) -> int:
    from ..knowledge.skills import SkillStore
    data, err = _load(args.file)
    err2 = None
    try:
        code = Path(args.code_file).read_text(encoding="utf-8")
    except OSError as exc:
        err2 = f"cannot read {args.code_file}: {exc}"
    if err or err2:
        sys.stderr.write((err or err2) + "\n")
        return 2
    res = SkillStore(_app(args).config).try_on(data, code)
    if args.json:
        emit_json(res, sys.stdout)
    elif not res["ok"]:
        for p in res["problems"]:
            sys.stdout.write(f"PROBLEM: {p}\n")
    else:
        for f in res["findings"]:
            sys.stdout.write(f"line {f['line']}: {f['rule_id']} {f['message']}{' [fixable]' if f['fixable'] else ''}\n")
        sys.stdout.write(res["diff"] or "")
        sys.stdout.write(f"{len(res['findings'])} finding(s)\n")
    return 0 if res["ok"] else 2


def run_learn(args) -> int:
    from ..knowledge.skills import SkillStore
    data, err = _load(args.file)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    rep = SkillStore(_app(args).config).learn(data, replace=args.replace)
    _print_report(rep, args)
    if rep["learned"] and not args.json:
        sys.stdout.write(f"learned: {rep['path']}\nIt is active now. Check with: codefix skill list\n")
    return 0 if rep["learned"] else 2


def run_enable(args) -> int:
    from ..knowledge.skills import SkillStore
    probs = SkillStore(_app(args).config).set_enabled(args.name, args.enabled)
    for p in probs:
        sys.stderr.write(p + "\n")
    if not probs:
        sys.stdout.write(f"{args.name}: {'enabled' if args.enabled else 'disabled'}\n")
    return 2 if probs else 0


def run_forget(args) -> int:
    from ..knowledge.skills import SkillStore
    ok = SkillStore(_app(args).config).delete(args.name)
    sys.stdout.write("forgotten\n" if ok else f"no skill named {args.name}\n")
    return 0 if ok else 2


def run_remove_rule(args) -> int:
    from ..knowledge.skills import SkillStore
    ok = SkillStore(_app(args).config).remove_rule(args.name, args.rule_id)
    sys.stdout.write("removed\n" if ok else "not found\n")
    return 0 if ok else 2


def run_export(args) -> int:
    from ..knowledge.skills import SkillStore
    data = SkillStore(_app(args).config).export(args.name)
    if data is None:
        sys.stderr.write(f"no skill named {args.name}\n")
        return 2
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        sys.stdout.write(f"wrote {args.output}\n")
    else:
        sys.stdout.write(text)
    return 0


# ---------------------------------------------------------------- policy
def run_policy_show(args) -> int:
    from ..policy.overrides import effective_rows
    app = _app(args)
    rows = [r for r in effective_rows(app.config, app.registry) if r["customised"] or not args.changed]
    if args.json:
        emit_json(rows, sys.stdout)
        return 0
    for r in rows:
        sev = r["severity"] or r["default_severity"]
        ex = f"  exclude: {', '.join(r['exclude'])}" if r["exclude"] else ""
        sys.stdout.write(f"{r['id']:<26} mode={r['mode']:<11} severity={sev:<7} -> {r['effective']:<11}{'*' if r['customised'] else ' '}{ex}\n")
    sys.stdout.write("\n(* = customised in codefix.policy.json)\n")
    return 0


def run_policy_set(args) -> int:
    from ..policy.overrides import set_rule
    app = _app(args)
    if app.registry.spec(args.rule_id) is None:
        sys.stderr.write(f"unknown rule {args.rule_id}\n")
        return 2
    exclude = [] if args.clear_exclude else args.exclude
    set_rule(app.config, args.rule_id, mode=args.mode, severity=args.severity, exclude=exclude,
             clear_severity=args.clear_severity)
    sys.stdout.write(f"updated {args.rule_id} in codefix.policy.json\n")
    return 0


def run_policy_behavior(args) -> int:
    from ..config import read_overlay, write_overlay
    app = _app(args)
    ov = read_overlay(app.config.root)
    if args.allow_risky is not None:
        ov["behavior"]["allow_risky"] = args.allow_risky == "true"
    if args.min_confidence is not None:
        ov["behavior"]["minimum_confidence"] = args.min_confidence
    write_overlay(app.config.root, ov)
    sys.stdout.write(f"behavior: {ov['behavior'] or 'defaults'}\n")
    return 0


def run_policy_reset(args) -> int:
    from ..policy.overrides import reset
    app = _app(args)
    if not args.rule_id and not args.all:
        sys.stderr.write("give a rule id or --all\n")
        return 2
    reset(app.config, None if args.all else args.rule_id)
    sys.stdout.write("reset\n")
    return 0
