"""Structured-edit application: conflict detection, byte-accurate bottom-to-top patching."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..models import Finding, ProposedEdit, SkippedChange
from ..source import SourceFile

Validator = Callable[[str], "tuple[bool, str]"]


class EditConflict(Exception):
    """Edits overlap, are out of range, or no longer match the source they were made for."""


@dataclass
class PatchResult:
    text: str
    accepted: list[Finding] = field(default_factory=list)
    skipped: list[SkippedChange] = field(default_factory=list)


def _conflicts(a: ProposedEdit, b: ProposedEdit) -> bool:
    if (a.start, a.end, a.replacement) == (b.start, b.end, b.replacement):
        return False                      # identical duplicates are harmless
    lo, hi = (a, b) if (a.start, a.end) <= (b.start, b.end) else (b, a)
    if hi.start < lo.end:
        return True                       # overlap
    if lo.start == hi.start and lo.end == hi.end:
        return True                       # same range, different text
    if lo.start == lo.end == hi.start == hi.end:
        return True                       # two insertions at the same point: order undefined
    return False


class PatchEngine:
    @staticmethod
    def find_conflicts(edits: Iterable[ProposedEdit]) -> list[tuple[ProposedEdit, ProposedEdit]]:
        ordered = sorted(edits, key=lambda e: (e.start, e.end))
        found = []
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                if b.start > a.end:
                    break
                if _conflicts(a, b):
                    found.append((a, b))
        return found

    @staticmethod
    def apply(source: SourceFile, edits: Iterable[ProposedEdit]) -> str:
        """Apply edits (UTF-8 byte offsets) bottom-to-top and return the new text."""
        data = source.text.encode("utf-8")
        unique: dict[tuple[int, int, str], ProposedEdit] = {}
        for e in edits:
            if not (0 <= e.start <= e.end <= len(data)):
                raise EditConflict(f"{e.rule_id}: edit range {e.start}:{e.end} is outside the file")
            if e.expected is not None and data[e.start:e.end].decode("utf-8", "replace") != e.expected:
                raise EditConflict(f"{e.rule_id}: source changed under the edit at offset {e.start}")
            unique[(e.start, e.end, e.replacement)] = e
        ordered = sorted(unique.values(), key=lambda e: (e.start, e.end))
        bad = PatchEngine.find_conflicts(ordered)
        if bad:
            a, b = bad[0]
            raise EditConflict(f"overlapping edits: {a.rule_id}@{a.start}-{a.end} vs {b.rule_id}@{b.start}-{b.end}")
        out = data
        for e in reversed(ordered):
            out = out[:e.start] + source.native_newlines(e.replacement).encode("utf-8") + out[e.end:]
        return out.decode("utf-8")

    @classmethod
    def apply_findings(cls, source: SourceFile, findings: Iterable[Finding],
                       validate: Validator | None = None) -> PatchResult:
        """Apply the edits of whole findings (all-or-nothing each) and validate the result.

        Findings whose edits collide with already accepted ones are skipped. If the combined
        result fails validation, each finding is tried alone and only the valid ones are kept.
        """
        result = PatchResult(source.text)
        ordered = sorted((f for f in findings if f.edits),
                         key=lambda f: (f.severity.rank, min(e.start for e in f.edits), f.rule_id))
        accepted: list[Finding] = []
        taken: list[ProposedEdit] = []
        for f in ordered:
            try:
                cls.apply(source, f.edits)        # range / stale / self-overlap check
            except EditConflict as exc:
                result.skipped.append(SkippedChange(f.rule_id, f.line, str(exc)))
                continue
            clash = next((o for o in taken for e in f.edits if _conflicts(e, o)), None)
            if clash is not None:
                result.skipped.append(SkippedChange(f.rule_id, f.line, f"overlaps another edit ({clash.rule_id})"))
                continue
            accepted.append(f)
            taken.extend(f.edits)
        if not accepted:
            return result

        def build(group: list[Finding]) -> str:
            return cls.apply(source, [e for f in group for e in f.edits])

        text = build(accepted)
        ok, msg = validate(text) if validate else (True, "")
        if not ok:
            valid = []
            for f in accepted:
                single_ok, single_msg = (validate(build([f])) if validate else (True, ""))
                if single_ok:
                    valid.append(f)
                else:
                    result.skipped.append(SkippedChange(f.rule_id, f.line, f"result rejected: {single_msg}"))
            accepted = valid
            text = build(accepted) if accepted else source.text
            if accepted and validate:
                ok, msg = validate(text)
                if not ok:
                    for f in accepted:
                        result.skipped.append(SkippedChange(f.rule_id, f.line, f"combined result rejected: {msg}"))
                    accepted, text = [], source.text
        result.text, result.accepted = text, accepted
        return result
