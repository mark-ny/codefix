import json

import pytest

from codefix.knowledge.feedback import FeedbackStore
from codefix.knowledge.registry import build_registry, rule
from codefix.knowledge.schema import check_entry
from codefix.knowledge.validator import run_examples, validate_knowledge
from codefix.models import ProjectConfig, RuleSpec, Severity
from codefix.config import parse_config


def pack(tmp_path, entries, name="extra.json"):
    d = tmp_path / "knowledge" / "project"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(entries))
    return parse_config({"knowledge": {"packs": ["python", "project"]}}, tmp_path)


def entry(**kw):
    base = {"id": "X_RULE", "category": "style", "severity": "safe", "confidence": 0.9, "autofix": False,
            "requires_tests": False, "message": "m", "bad": "a", "good": "b"}
    base.update(kw)
    return base


def messages(cfg, level="error"):
    return [f"{i.rule_id}: {i.message}" for i in validate_knowledge(build_registry(cfg), cfg) if i.level == level]


def test_shipped_knowledge_is_valid():
    cfg = ProjectConfig()
    assert [i for i in validate_knowledge(build_registry(cfg), cfg) if i.level == "error"] == []
    results = run_examples(build_registry(cfg), cfg)
    assert results and all(r.ok for r in results), [r for r in results if not r.ok]


def test_detects_duplicate_ids(tmp_path):
    cfg = pack(tmp_path, [entry(id="COMPARE_NONE"), entry(id="DUP"), entry(id="DUP")])
    assert any("DUP: duplicate rule id" in m for m in messages(cfg))
    assert any("COMPARE_NONE: duplicate rule id" in m for m in messages(cfg))   # clashes with the bundled pack


def test_detects_missing_fields_and_bad_values(tmp_path):
    bad = entry(id="BAD1")
    del bad["message"]
    cfg = pack(tmp_path, [bad, entry(id="BAD2", severity="catastrophic"), entry(id="BAD3", confidence=1.5),
                          entry(id="BAD4", confidence="high"), entry(id="BAD5", bad="", good="")])
    m = " | ".join(messages(cfg))
    assert "missing required field 'message'" in m
    assert "invalid severity 'catastrophic'" in m
    assert "BAD3: invalid confidence 1.5" in m and "BAD4: invalid confidence 'high'" in m
    assert "BAD5: missing bad/good example text" in m


def test_detects_autofix_without_analyzer(tmp_path):
    cfg = pack(tmp_path, [entry(id="GHOST", autofix=True)])
    assert any("GHOST: marked autofixable but no analyzer" in m for m in messages(cfg))


def test_detects_autofix_flag_for_rule_that_cannot_fix(monkeypatch):
    cfg = ProjectConfig()
    reg = build_registry(cfg)
    monkeypatch.setattr(reg.get("COMPARE_NONE").spec, "autofix", False)         # JSON still says autofix: true
    issues = validate_knowledge(reg, cfg)
    assert any(i.rule_id == "COMPARE_NONE" and "does not produce fixes" in i.message for i in issues)


def test_detects_conflicting_definitions(monkeypatch):
    cfg = ProjectConfig()
    reg = build_registry(cfg)
    monkeypatch.setattr(reg.get("COMPARE_NONE").spec, "severity", Severity.REVIEW)
    issues = validate_knowledge(reg, cfg)
    assert any(i.rule_id == "COMPARE_NONE" and "conflicting definition of 'severity'" in i.message for i in issues)


def test_detects_missing_examples():
    cfg = ProjectConfig()
    reg = build_registry(cfg)

    @rule(RuleSpec("NO_EXAMPLES", "style", Severity.SAFE, 0.9, message="m"), builtin=False)
    def r(ctx, spec):
        return []

    reg.register(r)
    assert any(i.rule_id == "NO_EXAMPLES" and "missing examples" in i.message for i in validate_knowledge(reg, cfg))


def test_malformed_json_reported(tmp_path):
    d = tmp_path / "knowledge" / "project"
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{not json")
    cfg = parse_config({"knowledge": {"packs": ["python", "project"]}}, tmp_path)
    assert any("cannot read JSON" in m for m in messages(cfg))


def test_check_entry_unknown_field_is_a_warning_only():
    assert check_entry(entry(extra=1)) == ["unknown field 'extra'"]


def test_failing_example_is_reported():
    cfg = ProjectConfig()
    from codefix.knowledge.validator import run_example
    r = run_example(build_registry(cfg), cfg, "COMPARE_NONE",
                    {"name": "wrong", "kind": "fix", "bad": "if x == None:\n    pass\n", "good": "if x == 1:\n    pass\n"})
    assert not r.ok and "unexpected fix result" in r.message


def test_plugin_rules_and_duplicates(tmp_path, monkeypatch):
    (tmp_path / "myplug.py").write_text(
        "from codefix.knowledge.registry import rule\nfrom codefix.models import RuleSpec, Severity\n"
        "import ast\n"
        "@rule(RuleSpec('PLUG_PRINT','style',Severity.SAFE,0.9,message='no print'), builtin=False)\n"
        "def check(ctx, spec):\n"
        "    for n in ast.walk(ctx.tree):\n"
        "        if isinstance(n, ast.Call) and getattr(n.func,'id','')=='print':\n"
        "            yield ctx.finding(spec, n)\n"
        "def register(registry):\n    registry.register(check)\n    registry.register(check)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    cfg = parse_config({"plugins": {"modules": ["myplug", "does_not_exist"]}}, tmp_path)
    reg = build_registry(cfg)
    assert reg.get("PLUG_PRINT") is not None
    assert ("PLUG_PRINT", "module myplug") in reg.duplicates and any("does_not_exist" in e for e in reg.plugin_errors)
    from codefix.analysis import AnalysisEngine
    from conftest import source
    res = AnalysisEngine(reg, cfg).analyze_source(source("print(1)\n"))
    assert [f.rule_id for f in res.findings if f.rule_id == "PLUG_PRINT"] == ["PLUG_PRINT"]


def test_security_extension_point_is_isolated():
    from codefix.rules.security import SECURITY_RULES, security_rule
    assert SECURITY_RULES == []
    with pytest.raises(ValueError):
        security_rule(RuleSpec("S", "style", Severity.SAFE, 0.9))


def test_feedback_is_advisory_only(tmp_path):
    cfg = ProjectConfig(root=tmp_path)
    before = build_registry(cfg).get("COMPARE_NONE").spec.confidence
    store = FeedbackStore(tmp_path)
    store.record("COMPARE_NONE", "false_positive", "numpy", "a.py")
    store.record("COMPARE_NONE", "false_positive")
    assert store.summary() == {"COMPARE_NONE": {"false_positive": 2}}
    assert build_registry(cfg).get("COMPARE_NONE").spec.confidence == before
    assert cfg.disabled_rules == []
    with pytest.raises(ValueError):
        store.record("X", "bogus")
