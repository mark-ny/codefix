"""The policy engine decides what may happen to each finding. It never touches files."""
from __future__ import annotations

from ..knowledge.registry import RuleRegistry
from ..models import Action, Decision, Finding, ProjectConfig, Severity
from .confidence import meets_threshold, threshold_for


class PolicyEngine:
    def __init__(self, config: ProjectConfig, registry: RuleRegistry):
        self.config, self.registry = config, registry

    def decide(self, f: Finding) -> Decision:
        cfg = self.config
        spec = self.registry.spec(f.rule_id)
        needs_tests = bool(spec and spec.requires_tests)
        is_security = f.category == "security"

        if f.severity is Severity.BLOCKED or f.rule_id in cfg.blocked_rules:
            return Decision(f, Action.BLOCK, "blocked: this finding can never be applied automatically")
        if not f.fixable:
            return Decision(f, Action.REPORT_ONLY, "no automatic fix available")
        if not meets_threshold(cfg, f):
            return Decision(f, Action.REPORT_ONLY,
                            f"confidence {f.confidence:.2f} is below the minimum {threshold_for(cfg, f):.2f}")
        if f.category == "ai":
            return Decision(f, Action.EXPLICIT, "AI-generated proposal: explicit approval, parsing and tests required", True)
        if is_security:
            if not cfg.security_allow_autofix:
                return Decision(f, Action.REPORT_ONLY, "security autofix is disabled ([security] allow_autofix)")
            return Decision(f, Action.EXPLICIT, "security fix: explicit approval required", needs_tests)
        if f.severity is Severity.SAFE:
            return Decision(f, Action.AUTO, "safe, high-confidence fix", needs_tests)
        if f.severity is Severity.REVIEW:
            return Decision(f, Action.CONFIRM, "review-level fix: confirmation required", needs_tests)
        needs_tests = needs_tests or cfg.require_tests_for_risky
        if cfg.allow_risky:
            return Decision(f, Action.CONFIRM, "risky fix allowed by project policy: confirmation and tests required",
                            needs_tests)
        return Decision(f, Action.EXPLICIT, "risky fix: explicit approval required", needs_tests)
