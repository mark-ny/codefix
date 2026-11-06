"""JSON schema of knowledge rules."""
from __future__ import annotations

from typing import Any

from ..models import RuleSpec, Severity

REQUIRED_FIELDS = ("id", "category", "severity", "confidence", "autofix", "requires_tests", "message", "bad", "good")
OPTIONAL_FIELDS = ("description", "explanation", "false_positive", "frameworks", "docs", "tests", "implemented")
VALID_SEVERITIES = tuple(s.value for s in Severity)
COMPARED_FIELDS = ("category", "severity", "confidence", "autofix", "requires_tests")


class SchemaError(ValueError):
    pass


def check_entry(d: Any) -> list[str]:
    """Return a list of problems with one raw JSON rule entry (empty list = valid)."""
    if not isinstance(d, dict):
        return ["entry is not an object"]
    problems = [f"missing required field '{k}'" for k in REQUIRED_FIELDS if k not in d]
    if "severity" in d and d["severity"] not in VALID_SEVERITIES:
        problems.append(f"invalid severity {d['severity']!r} (expected one of {', '.join(VALID_SEVERITIES)})")
    if "confidence" in d:
        c = d["confidence"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0.0 <= c <= 1.0:
            problems.append(f"invalid confidence {c!r} (expected a number between 0.0 and 1.0)")
    for k in ("autofix", "requires_tests"):
        if k in d and not isinstance(d[k], bool):
            problems.append(f"'{k}' must be true or false")
    for k in ("id", "category", "message"):
        if k in d and (not isinstance(d[k], str) or not d[k].strip()):
            problems.append(f"'{k}' must be a non-empty string")
    for k in ("bad", "good"):
        if k in d and not isinstance(d[k], str):
            problems.append(f"'{k}' must be a string")
    for k in ("frameworks", "docs", "tests"):
        if k in d and not (isinstance(d[k], list) and all(isinstance(x, str) for x in d[k])):
            problems.append(f"'{k}' must be a list of strings")
    unknown = set(d) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    problems += [f"unknown field '{k}'" for k in sorted(unknown)]
    return problems


def rule_from_dict(d: dict[str, Any], pack: str = "python") -> RuleSpec:
    problems = [p for p in check_entry(d) if not p.startswith("unknown field")]
    if problems:
        raise SchemaError("; ".join(problems))
    return RuleSpec(
        id=d["id"], category=d["category"], severity=Severity(d["severity"]), confidence=float(d["confidence"]),
        description=d.get("description", ""), message=d["message"], explanation=d.get("explanation", ""),
        bad=d["bad"], good=d["good"], autofix=d["autofix"], requires_tests=d["requires_tests"],
        false_positive=d.get("false_positive", ""), frameworks=list(d.get("frameworks", [])),
        docs=list(d.get("docs", [])), tests=list(d.get("tests", [])), pack=pack,
        implemented=bool(d.get("implemented", True)))


def rule_to_dict(spec: RuleSpec) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": spec.id, "category": spec.category, "severity": spec.severity.value, "confidence": spec.confidence,
        "autofix": spec.autofix, "requires_tests": spec.requires_tests, "message": spec.message,
        "description": spec.description, "explanation": spec.explanation, "bad": spec.bad, "good": spec.good,
        "false_positive": spec.false_positive}
    if spec.frameworks: d["frameworks"] = spec.frameworks
    if spec.docs: d["docs"] = spec.docs
    if spec.tests: d["tests"] = spec.tests
    if not spec.implemented: d["implemented"] = False
    return d
