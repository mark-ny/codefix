"""Compare analysis before and after a change to catch fixes that make code worse."""
from __future__ import annotations

from collections import Counter

from ..models import AnalysisResult

# Increases in these mean the edit very likely broke the file even though it still parses.
HARD = {"UNDEFINED_NAME", "SYNTAX_ERROR"}


def compare_findings(before: AnalysisResult, after: AnalysisResult) -> tuple[bool, list[str], list[str]]:
    """Return ``(ok, errors, warnings)``."""
    if not after.syntax_ok:
        return False, ["the result has a syntax error"], []
    b = Counter(f.rule_id for f in before.findings)
    a = Counter(f.rule_id for f in after.findings)
    errors, warnings = [], []
    for rid, n in a.items():
        grew = n - b.get(rid, 0)
        if grew > 0:
            msg = f"the change introduced {grew} new {rid} finding(s)"
            (errors if rid in HARD else warnings).append(msg)
    return not errors, errors, warnings
