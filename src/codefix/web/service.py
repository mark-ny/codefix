"""Web service layer. Pure Python (no HTTP) so it is unit-testable; the server is a thin shell.

Safety model:
* pasted code never touches disk;
* file/folder access is limited to the allowed roots (default: the directory you launched from);
* writing needs an explicit ``confirm`` and goes through the normal pipeline
  (policy -> validation -> diff -> backup -> atomic write -> tests -> rollback).
"""
from __future__ import annotations

import ast
import io
import re
from pathlib import Path
from typing import Any

from ..application import CodeFix, FixOptions
from ..config import ConfigError, apply_overlay, load_config
from ..knowledge.declarative import RuleError, build_rule
from ..knowledge.feedback import FeedbackStore
from ..knowledge.registry import RuleRegistry, build_registry
from ..knowledge.skills import SkillStore
from ..knowledge.userpack import UserPack, entry_from_example, examples_for
from ..models import Action, Finding, Severity
from ..patching import PatchEngine, unified_diff
from ..policy import iter_python_files, overrides
from ..policy.project_policy import DEFAULT_EXCLUDE_DIRS
from ..source import SourceError, SourceFile
from ..verification import validate_change
from ..verification.formatter import format_text
from ..verification.regression import compare_findings

MAX_CODE_BYTES = 2_000_000


class WebError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_ODD_CHARS = {"\u2018": "curly quote ‘", "\u2019": "curly quote ’", "\u201c": "curly quote “", "\u201d": "curly quote ”",
              "\u00a0": "non-breaking space", "\u200b": "zero-width space", "\u200c": "zero-width character",
              "\u200d": "zero-width character", "\ufeff": "byte-order mark", "\u2013": "en dash –", "\u2014": "em dash —"}
_ODD_RE = re.compile("[" + "".join(_ODD_CHARS) + "]")


def syntax_help(code: str) -> dict[str, Any] | None:
    """Explain a syntax error: the lines around it plus likely causes. ``None`` if the code parses."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as exc:
        msg, line, col = exc.msg or "invalid syntax", exc.lineno or 1, exc.offset or 1
    except (ValueError, RecursionError) as exc:
        return {"message": str(exc), "line": 1, "column": 1, "context": [], "hints": []}
    lines = code.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()                                  # the trailing newline is not a line of its own
    line = max(1, min(line, len(lines)))
    context = [{"n": i, "text": lines[i - 1], "current": i == line}
               for i in range(max(1, line - 4), min(len(lines), line + 2) + 1)]
    hints: list[str] = []
    odd: dict[int, set[str]] = {}
    for n, text in enumerate(lines[:line + 1], 1):
        for ch in _ODD_RE.findall(text):
            odd.setdefault(n, set()).add(_ODD_CHARS[ch])
    for n, names in list(odd.items())[:4]:
        hints.append(f"Line {n} contains {', '.join(sorted(names))}. This usually happens when code is copied from a web "
                     "page, chat or document; Python needs plain straight quotes (' or \"), plain spaces and hyphens.")
    stripped = lines[line - 1].strip()
    if re.match(r"(else|elif|except|finally)\b", stripped) or "indent" in msg.lower():
        hints.append("An else/elif/except/finally must line up exactly with the if/for/while/try it belongs to. "
                     "Check the indentation of this line and of the block above it.")
    if any(l.startswith("\t") for l in lines) and any(l.startswith("    ") for l in lines):
        hints.append("The code mixes tab and space indentation. Convert it all to 4 spaces.")
    if "never closed" in msg or "unterminated" in msg or "unexpected EOF" in msg or "invalid syntax" in msg:
        hints.append("Python reports where it noticed the problem, which is often *after* the real mistake. "
                     "Look at the lines just above for a missing closing bracket ) ] } or quote, or a missing colon.")
    if "f-string" in msg:
        hints.append("Check the braces and quotes inside the f-string; older Python versions cannot reuse the same quote inside it.")
    return {"message": msg, "line": line, "column": col, "context": context, "hints": hints}


def finding_key(f: Finding) -> str:
    return f"{f.rule_id}:{f.line}:{f.column}"


class WebService:
    def __init__(self, roots: list[Path] | None = None, config_file: str | None = None):
        self.roots = [Path(r).expanduser().resolve() for r in (roots or [Path.cwd()])]
        self.config_file = config_file

    # ----------------------------------------------------------------- helpers
    def app(self, start: Path | None = None) -> CodeFix:
        start = start or self.roots[0]
        cfg = load_config(start, self.config_file)
        if cfg.config_file is None:       # no codefix.toml: use the allowed folder that contains the target
            cfg.root = next((r for r in self.roots if r == start or r in start.parents), self.roots[0])
            apply_overlay(cfg)            # policy/skill switches live in the project root, which is known only now
        return CodeFix(cfg, build_registry(cfg), out=io.StringIO())

    def resolve(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise WebError("a path is required")
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        p = p.resolve()
        if not any(p == r or r in p.parents for r in self.roots):
            raise WebError(f"{p} is outside the allowed folders ({', '.join(map(str, self.roots))}). "
                           "Restart with --root to allow more.", 403)
        return p

    @staticmethod
    def _describe(app: CodeFix, f: Finding) -> dict[str, Any]:
        d = app.policy.decide(f)
        out = f.to_dict()
        out["key"] = finding_key(f)
        out["action"] = d.action.value
        out["reason"] = d.reason
        out["needs_tests"] = d.requires_tests
        out["edits"] = [{"before": e.expected or "", "after": e.replacement} for e in f.edits[:4]]
        return out

    def state(self) -> dict[str, Any]:
        app = self.app()
        return {"roots": [str(r) for r in self.roots], "cwd": str(self.roots[0]),
                "config": {"formatter": app.config.formatter, "tests": app.config.tests.command,
                           "minimum_confidence": app.config.minimum_confidence,
                           "allow_risky": app.config.allow_risky, "config_file": str(app.config.config_file or "")},
                "rules": len(app.registry.rules()), "version": __import__("codefix").__version__,
                "safe_rules": sorted(self._safe_rules(app.registry)),
                "plugin_errors": app.registry.plugin_errors}

    @staticmethod
    def _safe_rules(reg: RuleRegistry) -> set[str]:
        return {r.spec.id for r in reg.rules() if r.spec.autofix and r.spec.severity is Severity.SAFE}

    # -------------------------------------------------------------- pasted code
    @staticmethod
    def _source(code: Any) -> SourceFile:
        if not isinstance(code, str):
            raise WebError("'code' must be a string")
        data = code.encode("utf-8")
        if len(data) > MAX_CODE_BYTES:
            raise WebError("code is too large (2 MB max)", 413)
        try:
            return SourceFile.from_bytes(data, Path("pasted.py"))
        except SourceError as exc:
            raise WebError(str(exc))

    def analyze_text(self, code: str) -> dict[str, Any]:
        app = self.app()
        res = app.engine.analyze_source(self._source(code))
        return {"syntax_ok": res.syntax_ok, "syntax": None if res.syntax_ok else syntax_help(code),
                "lines": res.lines, "suppressed": res.suppressed, "warnings": res.warnings,
                "findings": [self._describe(app, f) for f in res.findings],
                "metrics": [m.to_dict() for m in res.metrics]}

    def fix_text(self, code: str, selected: list[str] | None = None, apply_safe: bool = True,
                 repeat: bool = False, run_formatter: bool = False) -> dict[str, Any]:
        app = self.app()
        src0 = self._source(code)
        before = app.engine.analyze_source(src0)
        if not before.syntax_ok:
            return {"ok": False, "error": "The code has a syntax error, so nothing can be fixed yet: "
                    + before.findings[0].message + f" (line {before.findings[0].line})", "code": code,
                    "syntax": syntax_help(code)}
        chosen = set(selected or [])
        cur, applied, skipped = src0, [], []
        passes = app.config.max_passes if repeat else 1
        for n in range(passes):
            res = before if n == 0 else app.engine.analyze_source(cur)
            if not res.syntax_ok:
                break
            todo = []
            for f in res.findings:
                if not f.fixable:
                    continue
                d = app.policy.decide(f)
                if d.action in (Action.REPORT_ONLY, Action.BLOCK):
                    continue
                if (n == 0 and finding_key(f) in chosen) or (apply_safe and d.action is Action.AUTO):
                    todo.append(f)
            if not todo:
                break
            base = cur.text
            pr = PatchEngine.apply_findings(cur, todo, lambda t, b=base: validate_change(b, t, "pasted.py"))
            skipped += [{"rule_id": s.rule_id, "line": s.line, "reason": s.reason} for s in pr.skipped]
            if not pr.accepted:
                break
            applied += [{"rule_id": f.rule_id, "line": f.line, "severity": f.severity.value, "message": f.message}
                        for f in pr.accepted]
            cur = cur.with_text(pr.text)
        new_text, warnings = cur.text, []
        if run_formatter and app.config.formatter != "none" and new_text != src0.text:
            fr = format_text(new_text, "pasted.py", app.config)
            if fr.ok:
                new_text = fr.text
            else:
                warnings.append(f"formatter skipped: {fr.error}")
        ok, why = validate_change(src0.text, new_text, "pasted.py")
        if not ok:
            return {"ok": False, "error": f"Refused to return the result: {why}", "code": code}
        after = app.engine.analyze_source(cur.with_text(new_text))
        rok, errs, warns = compare_findings(before, after)
        if not rok:
            return {"ok": False, "error": "Refused to return the result: " + "; ".join(errs), "code": code}
        return {"ok": True, "changed": new_text != src0.text, "code": new_text, "applied": applied, "skipped": skipped,
                "warnings": warnings + warns, "diff": unified_diff(src0.text, new_text, "pasted.py"),
                "verification": ["parsed and compiled", "all definitions kept", "no new correctness findings"],
                "remaining": [self._describe(app, f) for f in after.findings]}

    # -------------------------------------------------------------------- paths
    def ls(self, raw: str | None) -> dict[str, Any]:
        p = self.resolve(raw) if raw else self.roots[0]
        if not p.is_dir():
            raise WebError(f"{p} is not a folder")
        entries = []
        try:
            for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
                if child.is_dir():
                    if child.name in DEFAULT_EXCLUDE_DIRS or child.name.endswith(".egg-info"):
                        continue
                elif not child.name.endswith(".py"):
                    continue
                entries.append({"name": child.name, "path": str(child), "dir": child.is_dir()})
                if len(entries) >= 500:
                    break
        except OSError as exc:
            raise WebError(str(exc), 403)
        parent = p.parent if any(p != r for r in self.roots) and any(r in p.parents for r in self.roots) else None
        return {"path": str(p), "parent": str(parent) if parent else None, "entries": entries}

    def review_path(self, raw: str) -> dict[str, Any]:
        p = self.resolve(raw)
        if not p.exists():
            raise WebError(f"{p} does not exist", 404)
        app = self.app(p)
        files, errors = iter_python_files([p], app.config)
        out_files = []
        for f in files:
            res = app.engine.analyze_file(f)
            out_files.append({"path": str(f), "error": res.error, "syntax_ok": res.syntax_ok, "lines": res.lines,
                              "findings": [self._describe(app, x) for x in res.findings]})
        by_rule: dict[str, dict[str, Any]] = {}
        for fl in out_files:
            for f in fl["findings"]:
                r = by_rule.setdefault(f["rule_id"], {"rule_id": f["rule_id"], "count": 0, "fixable": 0,
                                                      "severity": f["severity"], "action": f["action"], "message": f["message"]})
                r["count"] += 1
                r["fixable"] += bool(f["fixable"])
        return {"files": out_files, "errors": errors, "rules": sorted(by_rule.values(), key=lambda r: -r["count"]),
                "tests_configured": bool(app.config.tests.command.strip())}

    def fix_path(self, raw: str, *, rules: list[str], apply_safe: bool, write: bool, confirm: bool = False) -> dict[str, Any]:
        p = self.resolve(raw)
        if not p.exists():
            raise WebError(f"{p} does not exist", 404)
        if write and not confirm:
            raise WebError("writing needs explicit confirmation", 400)
        app = self.app(p)
        only = set(rules or [])
        if apply_safe:
            only |= self._safe_rules(app.registry)
        if not only:
            raise WebError("nothing selected: tick a rule or enable 'apply safe fixes'")
        opts = FixOptions(command="fix", write=write, yes=False, ci=True, interactive=False, preview=False,
                          approve_rules=set(rules or []), only_rules=only, passes=1, format=True)
        report = app.fix([p], opts)
        return report.to_dict()

    def undo(self, raw: str) -> dict[str, Any]:
        p = self.resolve(raw)
        return self.app(p).undo(p)

    # ---------------------------------------------------------------- knowledge
    def rule_details(self, rule_id: str) -> dict[str, Any]:
        app = self.app()
        spec = app.registry.spec(rule_id)
        if spec is None:
            raise WebError(f"unknown rule {rule_id}", 404)
        return spec.to_dict()

    def kb_list(self) -> dict[str, Any]:
        app = self.app()
        pack = UserPack(app.config)
        mine = {r["id"]: r for r in pack.load()}
        skill_rules = {r["id"]: (sk["name"], r) for sk in SkillStore(app.config).list() for r in sk["rules"]}
        rules = []
        for s in app.registry.specs():
            d = s.to_dict()
            d["user"] = s.id in mine
            d["skill"] = skill_rules[s.id][0] if s.id in skill_rules else ("my-rules" if s.id in mine else None)
            d["match"] = mine.get(s.id, {}).get("match") or (skill_rules[s.id][1]["match"] if s.id in skill_rules else None)
            rules.append(d)
        return {"rules": rules, "user_file": str(pack.path), "errors": app.registry.plugin_errors,
                "feedback": FeedbackStore(app.config.root).summary()}

    @staticmethod
    def _entry_from_form(f: dict[str, Any]) -> dict[str, Any]:
        match = f.get("match")
        bad, good = (f.get("bad") or ""), (f.get("good") or "")
        if not isinstance(match, dict):
            raise WebError("'match' is required")
        rep = bool(match.get("replace") is not None if match.get("type") == "pattern" else match.get("replace_with"))
        return {"id": (f.get("id") or "").strip(), "category": "project", "severity": f.get("severity", "review"),
                "confidence": float(f.get("confidence", 0.9)), "autofix": rep, "requires_tests": bool(f.get("requires_tests", False)),
                "message": (f.get("message") or "").strip(), "explanation": f.get("explanation", ""),
                "bad": bad.strip("\n") + "\n", "good": (good.strip("\n") + "\n") if good.strip() else "", "match": match}

    def kb_learn(self, f: dict[str, Any]) -> dict[str, Any]:
        """Generalise a before/after example into a pattern; self-test it; do not save."""
        try:
            entry, examples = entry_from_example(
                (f.get("id") or "").strip(), (f.get("message") or "").strip(), f.get("bad") or "", f.get("good") or None,
                severity=f.get("severity", "review"), confidence=float(f.get("confidence", 0.9)),
                explanation=f.get("explanation", ""), negative=[n for n in (f.get("negatives") or []) if n.strip()])
        except (RuleError, ValueError) as exc:
            return {"ok": False, "problems": [str(exc)]}
        problems = UserPack(self.app().config).selftest(entry, examples)
        return {"ok": not problems, "problems": problems, "entry": entry, "examples": examples}

    def kb_preview(self, f: dict[str, Any]) -> dict[str, Any]:
        """Run an unsaved rule on some code and show what it would flag and change."""
        entry = f.get("entry") or self._entry_from_form(f)
        try:
            rule = build_rule(entry)
        except RuleError as exc:
            return {"ok": False, "problems": [str(exc)], "matches": []}
        reg = RuleRegistry()
        reg.register(rule)
        from ..analysis.ast_engine import AnalysisEngine
        app = self.app()
        src = self._source(f.get("code") or "")
        res = AnalysisEngine(reg, app.config).analyze_source(src)
        if not res.syntax_ok:
            return {"ok": False, "problems": ["the test code has a syntax error: " + res.findings[0].message], "matches": []}
        matches = [{"line": x.line, "column": x.column, "message": x.message, "fixable": x.fixable,
                    "after": x.edits[0].replacement if x.edits else None} for x in res.findings]
        fixable = [x for x in res.findings if x.fixable]
        pr = PatchEngine.apply_findings(src, fixable, lambda t: validate_change(src.text, t, "pasted.py")) if fixable else None
        return {"ok": True, "problems": [], "matches": matches, "fixed": pr.text if pr else None,
                "diff": unified_diff(src.text, pr.text, "pasted.py") if pr else "",
                "rejected": [s.reason for s in pr.skipped] if pr else []}

    def kb_save(self, f: dict[str, Any]) -> dict[str, Any]:
        entry = f.get("entry") or self._entry_from_form(f)
        try:
            examples = f.get("examples") or examples_for(entry, [n for n in (f.get("negatives") or []) if n.strip()])
        except RuleError as exc:
            return {"ok": False, "problems": [str(exc)]}
        app = self.app()
        target = f.get("skill") or "my-rules"
        if target != "my-rules":
            rep = SkillStore(app.config).add_rule(target, entry, examples, replace=bool(f.get("replace")))
            probs = rep["problems"] + [f"{k}: {p}" for k, v in rep["rules"].items() for p in v]
            return {"ok": rep["ok"], "problems": probs}
        problems = UserPack(app.config).save(entry, examples, replace=bool(f.get("replace")))
        return {"ok": not problems, "problems": problems}

    def kb_delete(self, rule_id: str) -> dict[str, Any]:
        return {"ok": UserPack(self.app().config).delete(rule_id)}

    def kb_export(self) -> dict[str, Any]:
        return UserPack(self.app().config).export()

    def kb_import(self, data: Any, replace: bool = False) -> dict[str, Any]:
        return UserPack(self.app().config).import_pack(data, replace=replace)

    def feedback(self, rule_id: str, verdict: str, note: str = "") -> dict[str, Any]:
        try:
            FeedbackStore(self.app().config.root).record(rule_id, verdict, note)
        except ValueError as exc:
            raise WebError(str(exc))
        return {"ok": True, "note": "recorded (advisory only; no rule was changed)"}


    # ------------------------------------------------------------------- skills
    def skills_list(self) -> dict[str, Any]:
        app = self.app()
        mine = UserPack(app.config).load()
        return {"skills": SkillStore(app.config).list(), "dir": str(SkillStore(app.config).dir),
                "my_rules": [{"id": r["id"], "message": r.get("message", "")} for r in mine]}

    @staticmethod
    def _report(rep: dict[str, Any]) -> dict[str, Any]:
        rep.pop("data", None)
        return rep

    def skill_create(self, name: str, title: str = "", description: str = "") -> dict[str, Any]:
        return self._report(SkillStore(self.app().config).create((name or "").strip(), title, description))

    def skill_validate(self, data: Any) -> dict[str, Any]:
        st = SkillStore(self.app().config)
        return self._report(st.validate(data, replacing=data.get("name") if isinstance(data, dict) else None))

    def skill_learn(self, data: Any, replace: bool = False) -> dict[str, Any]:
        return self._report(SkillStore(self.app().config).learn(data, replace=replace))

    def skill_enable(self, name: str, enabled: bool) -> dict[str, Any]:
        probs = SkillStore(self.app().config).set_enabled(name, enabled)
        return {"ok": not probs, "problems": probs}

    def skill_delete(self, name: str) -> dict[str, Any]:
        return {"ok": SkillStore(self.app().config).delete(name)}

    def skill_export(self, name: str) -> dict[str, Any]:
        data = SkillStore(self.app().config).export(name)
        if data is None:
            raise WebError(f"no skill named {name}", 404)
        return data

    def skill_try(self, data: Any, code: str) -> dict[str, Any]:
        self._source(code)                         # size / type checks
        return SkillStore(self.app().config).try_on(data, code)

    def skill_rule_delete(self, name: str, rule_id: str) -> dict[str, Any]:
        if name == "my-rules":
            return {"ok": UserPack(self.app().config).delete(rule_id)}
        return {"ok": SkillStore(self.app().config).remove_rule(name, rule_id)}

    # ------------------------------------------------------------------- policy
    def policy_get(self) -> dict[str, Any]:
        app = self.app()
        return {"rules": overrides.effective_rows(app.config, app.registry),
                "behavior": {"allow_risky": app.config.allow_risky, "minimum_confidence": app.config.minimum_confidence},
                "file": str(app.config.root / "codefix.policy.json"),
                "note": "Saved to codefix.policy.json next to your project; the terminal uses it too."}

    def policy_save(self, rules: Any, behavior: Any) -> dict[str, Any]:
        app = self.app()
        if not isinstance(rules, dict):
            raise WebError("'rules' must be an object")
        known = {s.id for s in app.registry.specs()}
        unknown = sorted(set(rules) - known)
        if unknown:
            raise WebError("unknown rule(s): " + ", ".join(unknown))
        try:
            overrides.set_many(app.config, rules, behavior if isinstance(behavior, dict) else None)
        except ConfigError as exc:
            raise WebError(str(exc))
        return self.policy_get()

    def policy_reset(self, rule_id: str | None) -> dict[str, Any]:
        overrides.reset(self.app().config, rule_id or None)
        return self.policy_get()
