"""Context-sensitive confidence adjustments applied right after a rule reports."""
from __future__ import annotations

from ..models import Finding


def adjust(finding: Finding, ctx) -> None:
    c = finding.confidence
    if finding.rule_id in ("UNDEFINED_NAME", "UNUSED_IMPORT") and ctx.symbols.dynamic:
        c -= 0.3                      # exec/eval/globals() can create or use names invisibly
    if finding.rule_id == "MISSING_DOCSTRING" and ctx.is_test_file:
        c -= 0.3
    finding.confidence = round(max(0.0, min(1.0, c)), 4)
