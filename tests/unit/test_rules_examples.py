"""Every built-in rule is exercised through its executable examples (bad / good / edge / negative)."""
import pytest

from codefix.knowledge.loader import load_examples
from codefix.knowledge.registry import build_registry
from codefix.knowledge.validator import run_example
from codefix.models import ProjectConfig

CONFIG = ProjectConfig()
REGISTRY = build_registry(CONFIG)
EXAMPLES = load_examples(CONFIG)
CASES = [(rid, ex) for rid, exs in sorted(EXAMPLES.items()) for ex in exs]


@pytest.mark.parametrize("rule_id,example", CASES, ids=[f"{r}:{e['name']}" for r, e in CASES])
def test_example(rule_id, example):
    result = run_example(REGISTRY, CONFIG, rule_id, example)
    assert result.ok, result.message


@pytest.mark.parametrize("rule", [r.spec for r in REGISTRY.rules() if r.spec.id != "SYNTAX_ERROR"], ids=lambda s: s.id)
def test_every_rule_has_examples(rule):
    exs = EXAMPLES.get(rule.id, [])
    kinds = [e["kind"] for e in exs]
    assert "negative" in kinds, "positive AND negative examples required"
    if rule.autofix:
        assert "fix" in kinds, "autofix rules need a bad/good example"
        assert any(e.get("edge") for e in exs if e["kind"] == "fix"), "autofix rules need an edge case"
    else:
        assert "detect" in kinds


def test_all_required_rules_exist():
    required = {"UNDEFINED_NAME", "UNUSED_IMPORT", "SHADOW_BUILTIN", "UNREACHABLE_CODE", "MUTABLE_DEFAULT",
                "BARE_EXCEPT", "EXCEPT_PASS", "COMPARE_NONE", "COMPARE_LITERAL", "ASSERT_TUPLE", "TYPE_COMPARE",
                "RESOURCE_NO_CONTEXT", "EXCESSIVE_NESTING", "FUNCTION_TOO_LONG", "HIGH_COMPLEXITY",
                "TOO_MANY_PARAMS", "TOO_MANY_RETURNS", "TOO_MANY_CALLS", "DUPLICATE_CODE", "STRING_CONCAT_LOOP",
                "MISSING_DOCSTRING", "INCONSISTENT_RETURN", "SYNTAX_ERROR"}
    assert required <= {s.id for s in REGISTRY.specs()}


def test_no_builtin_security_rules():
    assert not [s for s in REGISTRY.specs() if s.category == "security"]
