"""Optional AI suggestion layer (Stage 5): a *gate*, not a model.

An AI tool (or a human) produces a structured JSON proposal. Nothing here talks to a network
or writes to disk. Proposals are converted to ordinary findings with category ``ai`` and then
pass through exactly the same pipeline as every other change: parse check, symbol check,
policy (always explicit approval), diff review, backup, tests, rollback.

Proposal file format::

    {"proposals": [{"file": "app.py", "description": "simplify loop", "confidence": 0.9,
                    "edits": [{"start_line": 10, "end_line": 12, "text": "new code\\n"}]}]}

``start_line``/``end_line`` are 1-based and inclusive; ``end_line = start_line - 1`` inserts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .models import Finding, Location, ProposedEdit, Severity
from .source import LineIndex, SourceError, SourceFile


class SuggestionProvider(Protocol):
    """Implement this to plug a model in. It must only *return* proposals."""

    def suggest(self, path: str, source: str) -> list[dict[str, Any]]: ...


class NullProvider:
    def suggest(self, path: str, source: str) -> list[dict[str, Any]]:
        return []


class ProposalError(Exception):
    pass


def proposals_to_findings(data: dict[str, Any], root: Path) -> tuple[dict[Path, list[Finding]], list[str]]:
    """Validate a proposal document and convert it to findings grouped by file."""
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
        raise ProposalError('expected an object with a "proposals" list')
    grouped: dict[Path, list[Finding]] = {}
    problems: list[str] = []
    sources: dict[Path, tuple[SourceFile, LineIndex]] = {}
    for n, prop in enumerate(data["proposals"], 1):
        try:
            file = Path(prop["file"])
            path = file if file.is_absolute() else (root / file)
            if path not in sources:
                src = SourceFile.read(path)
                sources[path] = (src, LineIndex(src.text.encode("utf-8")))
            src, idx = sources[path]
            nlines = len(idx.starts) - (1 if src.text.endswith("\n") else 0)
            conf = float(prop.get("confidence", 0.0))
            if not 0.0 <= conf <= 1.0:
                raise ProposalError("confidence must be between 0 and 1")
            edits: list[ProposedEdit] = []
            for e in prop["edits"]:
                s_line, e_line, text = int(e["start_line"]), int(e["end_line"]), str(e["text"])
                if s_line < 1 or e_line < s_line - 1 or e_line > nlines:
                    raise ProposalError(f"line range {s_line}-{e_line} is outside the file ({nlines} lines)")
                start, end = idx.line_start(s_line), idx.line_start(e_line + 1)
                if text and not text.endswith("\n") and end > start:
                    text += "\n"
                edits.append(ProposedEdit("AI_PROPOSAL", str(path), start, end, text, prop.get("description", ""),
                                          expected=idx.data[start:end].decode("utf-8")))
            if not edits:
                raise ProposalError("no edits")
            first = prop["edits"][0]
            grouped.setdefault(path, []).append(Finding(
                "AI_PROPOSAL", prop.get("description") or "AI-generated change", "ai", Severity.REVIEW, conf,
                Location(str(path), int(first["start_line"]), 1, int(first["end_line"]), 1), None, True,
                {"source": "ai"}, edits))
        except (KeyError, TypeError, ValueError, ProposalError, SourceError) as exc:
            problems.append(f"proposal {n}: {exc}")
    return grouped, problems


def load_proposals(path: str | Path, root: Path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProposalError(f"cannot read proposal file: {exc}") from exc
    return proposals_to_findings(data, root)
