"""Knowledge validation and executable rule examples."""
from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..analysis.ast_engine import AnalysisEngine
from ..models import ProjectConfig
from ..patching.engine import PatchEngine
from ..source import SourceFile
from ..verification.syntax import check_syntax, validate_change
from .loader import load_examples, load_pack_files
from .registry import RuleRegistry
from .schema import COMPARED_FIELDS, check_entry


@dataclass
class Issue:
    level: str          # error | warning
    rule_id: str
    message: str
    where: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def validate_knowledge(registry: RuleRegistry, config: ProjectConfig) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    json_ids: set[str] = set()
    for pf in load_pack_files(config):
        where = str(pf.path)
        if pf.error:
            issues.append(Issue("error", "-", pf.error, where))
            continue
        for i, raw in enumerate(pf.entries):
            rid = raw.get("id", f"<entry {i}>") if isinstance(raw, dict) else f"<entry {i}>"
            for p in check_entry(raw):
                issues.append(Issue("warning" if p.startswith("unknown field") else "error", str(rid), p, where))
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            if rid in seen:
                issues.append(Issue("error", rid, f"duplicate rule id (also defined in {seen[rid]})", where))
                continue
            seen[rid] = where
            json_ids.add(rid)
            if not raw.get("bad") or (not raw.get("good") and "match" not in raw):
                issues.append(Issue("error", rid, "missing bad/good example text", where))
            rule = registry.get(rid)
            if rule is None and "match" in raw:
                continue                       # invalid declarative rule: reported via registry.plugin_errors
            if rule is None:
                if raw.get("autofix") is True:
                    issues.append(Issue("error", rid, "marked autofixable but no analyzer is registered for it", where))
                else:
                    issues.append(Issue("warning", rid, "documentation-only entry: no analyzer is registered", where))
                continue
            if raw.get("autofix") is True and not rule.spec.autofix:
                issues.append(Issue("error", rid, "marked autofixable but its analyzer does not produce fixes", where))
            for fld in COMPARED_FIELDS:
                if fld in raw:
                    py = getattr(rule.spec, fld)
                    py = py.value if hasattr(py, "value") else py
                    if raw[fld] != py:
                        issues.append(Issue("error", rid, f"conflicting definition of '{fld}': JSON={raw[fld]!r} "
                                            f"vs analyzer={py!r}", where))
    for rid, origin in registry.duplicates:
        issues.append(Issue("error", rid, f"duplicate rule id registered by {origin}"))
    for msg in registry.plugin_errors:
        issues.append(Issue("error", "-", msg))
    examples = load_examples(config)
    for r in registry.rules():
        spec = r.spec
        if spec.id not in json_ids and spec.pack != "security":
            issues.append(Issue("warning", spec.id, "rule has no knowledge metadata in any loaded pack"))
        exs = examples.get(spec.id, [])
        if spec.id not in {"SYNTAX_ERROR"}:
            if not exs:
                issues.append(Issue("error", spec.id, "missing examples (add an entry to examples.json)"))
                continue
            kinds = Counter(e.get("kind") for e in exs)
            strict = spec.pack != "project"        # built-in rules need the full set; user rules need one working example
            level = "error" if strict else "warning"
            if kinds["negative"] == 0:
                issues.append(Issue(level, spec.id, "no negative example"))
            if spec.autofix and kinds["fix"] == 0:
                issues.append(Issue("error", spec.id, "autofix rule needs a fix example"))
            elif spec.autofix and strict and not any(e.get("edge") for e in exs if e.get("kind") == "fix"):
                issues.append(Issue("error", spec.id, "autofix rule needs an edge-case fix example"))
            if not spec.autofix and kinds["detect"] == 0:
                issues.append(Issue("error", spec.id, "no detection example"))
    return issues


@dataclass
class ExampleResult:
    rule_id: str
    name: str
    ok: bool
    message: str = ""


def _only(registry: RuleRegistry, rule_id: str) -> RuleRegistry:
    sub = RuleRegistry()
    sub.register(registry.get(rule_id))   # type: ignore[arg-type]
    return sub


def run_example(registry: RuleRegistry, config: ProjectConfig, rule_id: str, ex: dict) -> ExampleResult:
    name, kind = ex.get("name", "?"), ex.get("kind")
    cfg = dataclasses.replace(config, disabled_rules=[], blocked_rules=[], **ex.get("config", {}))
    engine = AnalysisEngine(_only(registry, rule_id), cfg)
    code = ex.get("bad") if kind == "fix" else ex.get("code", "")
    src = SourceFile.from_bytes(code.encode("utf-8"), Path("example.py"))
    found = [f for f in engine.analyze_source(src).findings if f.rule_id == rule_id]
    if kind == "negative":
        return ExampleResult(rule_id, name, not found, "" if not found else f"unexpected finding: {found[0].message}")
    if not found:
        return ExampleResult(rule_id, name, False, "rule did not fire")
    if kind == "detect":
        return ExampleResult(rule_id, name, True)
    if kind != "fix":
        return ExampleResult(rule_id, name, False, f"unknown example kind {kind!r}")
    fixable = [f for f in found if f.fixable]
    if not fixable:
        return ExampleResult(rule_id, name, False, "rule fired but produced no fix")
    res = PatchEngine.apply_findings(src, fixable, lambda t: validate_change(code, t))
    if res.text != ex.get("good"):
        return ExampleResult(rule_id, name, False, f"unexpected fix result:\n{res.text}")
    if not check_syntax(res.text).ok:
        return ExampleResult(rule_id, name, False, "fixed code does not compile")
    again = [f for f in engine.analyze_source(src.with_text(res.text)).findings if f.rule_id == rule_id and f.fixable]
    if again:
        return ExampleResult(rule_id, name, False, "fix is not idempotent: the rule still wants to fix the result")
    return ExampleResult(rule_id, name, True)


def run_examples(registry: RuleRegistry, config: ProjectConfig, only: str | None = None) -> list[ExampleResult]:
    out = []
    for rid, exs in sorted(load_examples(config).items()):
        if only and rid != only:
            continue
        if registry.get(rid) is None:
            out.append(ExampleResult(rid, "-", False, "examples for a rule with no analyzer"))
            continue
        out += [run_example(registry, config, rid, ex) for ex in exs]
    return out
