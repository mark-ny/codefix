"""Editing *what CodeFix fixes and what it leaves alone* (``codefix.policy.json``)."""
from __future__ import annotations

from typing import Any

from ..config import read_overlay, write_overlay
from ..knowledge.registry import RuleRegistry
from ..models import Finding, Location, ProjectConfig, Severity
from .decision import PolicyEngine


def effective_rows(config: ProjectConfig, registry: RuleRegistry) -> list[dict[str, Any]]:
    """One row per rule: its defaults, the user's override and what the policy will actually do."""
    engine = PolicyEngine(config, registry)
    rows = []
    for s in registry.specs():
        ov = config.rule_overrides.get(s.id, {})
        mode = ov.get("mode", "enabled")
        sev = ov.get("severity") if s.category != "security" else None
        fixable = s.autofix and mode != "report_only"
        f = Finding(s.id, s.message, s.category, Severity(sev or s.severity.value), s.confidence,
                    Location("x.py"), None, fixable)
        action = "disabled" if mode == "disabled" else "block" if mode == "blocked" else \
            ("report_only" if not s.implemented else engine.decide(f).action.value)
        rows.append({"id": s.id, "category": s.category, "message": s.message, "autofix": s.autofix,
                     "default_severity": s.severity.value, "severity": sev, "mode": mode,
                     "exclude": ov.get("exclude", []), "effective": action, "pack": s.pack,
                     "implemented": s.implemented, "customised": bool(ov)})
    return rows


def set_rule(config: ProjectConfig, rule_id: str, *, mode: str | None = None, severity: str | None = None,
             exclude: list[str] | None = None, clear_severity: bool = False) -> dict[str, Any]:
    ov = read_overlay(config.root)
    cur = dict(ov["rules"].get(rule_id, {}))
    if mode is not None:
        cur["mode"] = mode
    if severity is not None:
        cur["severity"] = severity
    if clear_severity:
        cur.pop("severity", None)
    if exclude is not None:
        cur["exclude"] = exclude
    ov["rules"][rule_id] = cur
    return write_overlay(config.root, ov)


def set_many(config: ProjectConfig, rules: dict[str, dict[str, Any]], behavior: dict[str, Any] | None = None) -> dict[str, Any]:
    ov = read_overlay(config.root)
    ov["rules"] = rules
    if behavior is not None:
        ov["behavior"] = behavior
    return write_overlay(config.root, ov)


def reset(config: ProjectConfig, rule_id: str | None = None) -> dict[str, Any]:
    ov = read_overlay(config.root)
    if rule_id is None:
        ov["rules"], ov["behavior"] = {}, {}
    else:
        ov["rules"].pop(rule_id, None)
    return write_overlay(config.root, ov)
