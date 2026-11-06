"""Advisory feedback storage.

Feedback is *recorded and reported only*. It never edits rules, severities or configuration:
changing behaviour is a deliberate human decision made in ``codefix.toml``.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

VERDICTS = ("false_positive", "helpful", "wrong_fix", "other")


class FeedbackStore:
    def __init__(self, root: str | Path):
        self.path = Path(root) / ".codefix" / "feedback.jsonl"

    def record(self, rule_id: str, verdict: str, note: str = "", file: str = "") -> dict:
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule_id": rule_id,
                 "verdict": verdict, "note": note, "file": file}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def summary(self) -> dict[str, dict[str, int]]:
        per: dict[str, Counter] = {}
        for e in self.entries():
            per.setdefault(e["rule_id"], Counter())[e["verdict"]] += 1
        return {k: dict(v) for k, v in sorted(per.items())}
