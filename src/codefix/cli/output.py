"""Human, JSON and SARIF rendering."""
from __future__ import annotations

import json
from typing import Any, TextIO

from .. import __version__
from ..application import FileOutcome, ReviewReport, RunReport
from ..knowledge.registry import RuleRegistry
from ..models import Severity

SARIF_LEVEL = {Severity.SAFE: "note", Severity.REVIEW: "warning", Severity.RISKY: "warning", Severity.BLOCKED: "error"}


def emit_json(obj: Any, out: TextIO) -> None:
    out.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def print_review(report: ReviewReport, out: TextIO) -> None:
    for r in report.results:
        if r.error:
            out.write(f"{r.path}: ERROR {r.error}\n")
        for w in r.warnings:
            out.write(f"{r.path}: warning: {w}\n")
        for f in r.findings:
            tag = " [fixable]" if f.fixable else ""
            out.write(f"{f.path}:{f.line}:{f.column}: {f.severity.value:<7} {f.rule_id} {f.message}{tag}\n")
    for e in report.errors:
        out.write(f"ERROR {e}\n")
    s = report.summary()
    out.write(f"\n{s['findings']} finding(s) in {s['files']} file(s); {s['fixable']} fixable, "
              f"{s['suppressed']} suppressed, {s['errors']} error(s)\n")


def print_outcome(o: FileOutcome, out: TextIO, verbose: bool = False) -> None:
    cs = o.changeset
    label = {"written": "written", "dry-run": "dry run (nothing written)", "unchanged": "no changes",
             "rolled_back": "ROLLED BACK", "failed": "FAILED", "rejected": "REJECTED", "error": "ERROR"}[o.status]
    out.write(f"{o.path}: {label}\n")
    if o.error:
        out.write(f"  {o.error}\n")
    if cs:
        if cs.description:
            out.write(f"  {cs.description}\n")
        for f in cs.applied:
            out.write(f"  line {f.line:<5} {f.rule_id:<20} {f.severity.value:<7} {f.message}\n")
        for s in cs.skipped:
            out.write(f"  skipped line {s.line} {s.rule_id}: {s.reason}\n")
        for w in cs.warnings:
            out.write(f"  warning: {w}\n")
        if verbose or o.failed:
            for st in cs.verification.steps:
                out.write(f"  verify {'ok  ' if st.ok else 'FAIL'} {st.name}: {st.message}\n")
    if o.backup:
        out.write(f"  backup: {o.backup}\n")
    if o.tests and not o.tests.ok:
        for fail in o.tests.failures[:5]:
            fn = ", ".join(f"{x['function']} ({x['file']}:{x['line']})" for x in fail.likely_functions[:2])
            out.write(f"  failing test: {fail.test} - {fail.message}" + (f" [likely: {fn}]" if fn else "") + "\n")


def print_run(report: RunReport, out: TextIO, verbose: bool = False) -> None:
    for o in report.outcomes:
        if o.status != "unchanged" or verbose or o.error:
            print_outcome(o, out, verbose)
    for e in report.errors:
        out.write(f"ERROR {e}\n")
    s = report.summary()
    out.write(f"\n{s['files']} file(s): {s['by_status']}; {s['changes']} change(s) "
              f"{'applied' if report.write else 'proposed'}, {s['remaining_findings']} finding(s) remaining, "
              f"{s['errors']} error(s)\n")
    if not report.write and s["changes"]:
        out.write("Dry run: re-run with --write to apply (backups are created first).\n")


def to_sarif(report: ReviewReport, registry: RuleRegistry) -> dict[str, Any]:
    used = sorted({f.rule_id for f in report.findings})
    rules = []
    for rid in used:
        spec = registry.spec(rid)
        rules.append({"id": rid, "shortDescription": {"text": spec.message if spec else rid},
                      "fullDescription": {"text": (spec.explanation or spec.description) if spec else rid},
                      "properties": {"category": spec.category if spec else "", "autofix": bool(spec and spec.autofix)}})
    results = [{
        "ruleId": f.rule_id, "level": SARIF_LEVEL[f.severity], "message": {"text": f.message},
        "properties": {"confidence": f.confidence, "fixable": f.fixable, "severity": f.severity.value},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": f.path.replace("\\", "/")},
            "region": {"startLine": f.location.line, "startColumn": f.location.column,
                       "endLine": f.location.end_line, "endColumn": f.location.end_column}}}]} for f in report.findings]
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "codefix", "version": __version__, "rules": rules}}, "results": results}]}
