"""Security rule extension point.

CodeFix deliberately ships **no** security rules. Rules added here are isolated from the
general rule set: they are always reported under the ``security`` category and the policy
engine applies stricter defaults to them (see docs/rule-authoring.md, "Security rules").

Add rules with the decorator below, or from a plugin (``codefix.rules`` entry point)::

    from codefix.rules.security import security_rule
    from codefix.models import RuleSpec, Severity

    @security_rule(RuleSpec("SEC_EXAMPLE", "security", Severity.REVIEW, 0.95, ...))
    def check(ctx, spec):
        ...
        yield ctx.finding(spec, node, "message")
"""
from __future__ import annotations

from ..knowledge.registry import FunctionRule, Rule
from ..models import RuleSpec

SECURITY_CATEGORY = "security"
SECURITY_RULES: list[Rule] = []     # intentionally empty


def security_rule(spec: RuleSpec):
    if spec.category != SECURITY_CATEGORY:
        raise ValueError(f"security rule {spec.id} must use category '{SECURITY_CATEGORY}'")

    def wrap(fn):
        r = FunctionRule(spec, fn)
        SECURITY_RULES.append(r)
        return r

    return wrap
