"""The user's own knowledge: declarative rules saved under ``<project>/knowledge/project/``."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import ProjectConfig
from ..patching.atomic_write import atomic_write_bytes
from .declarative import RuleError, build_rule, complete_snippet, generalize_example
from .registry import RuleRegistry, build_registry

USER_DIR = Path("knowledge") / "project"
USER_FILE = "user_rules.json"
EXAMPLES_FILE = "examples.json"


def entry_from_example(rule_id: str, message: str, bad: str, good: str | None, *, severity: str = "review",
                       confidence: float = 0.9, category: str = "project", explanation: str = "",
                       negative: list[str] | None = None, requires_tests: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Learn a rule from a before/after example. Returns ``(entry, examples)``."""
    pattern, template = generalize_example(bad, good)
    match: dict[str, Any] = {"type": "pattern", "pattern": pattern}
    if template is not None:
        match["replace"] = template
    entry: dict[str, Any] = {
        "id": rule_id, "category": category, "severity": severity, "confidence": confidence,
        "autofix": template is not None, "requires_tests": requires_tests, "message": message,
        "explanation": explanation, "bad": bad.strip("\n") + "\n", "good": (good or "").strip("\n") + ("\n" if good else ""),
        "match": match}
    return entry, examples_for(entry, negative or [])


def examples_for(entry: dict[str, Any], negatives: list[str]) -> list[dict[str, Any]]:
    bad_code, suffix = complete_snippet(entry["bad"])
    examples: list[dict[str, Any]] = []
    if entry["autofix"]:
        good_code = complete_snippet(entry["good"] + "" if not suffix else entry["good"])[0]
        if suffix and not good_code.endswith(suffix):
            good_code = good_code.rstrip("\n") + "\n" + suffix
        examples.append({"name": "taught example", "kind": "fix", "bad": bad_code, "good": good_code})
    else:
        examples.append({"name": "taught example", "kind": "detect", "code": bad_code})
    for i, neg in enumerate(negatives, 1):
        examples.append({"name": f"negative {i}", "kind": "negative", "code": complete_snippet(neg)[0]})
    return examples


class UserPack:
    def __init__(self, config: ProjectConfig):
        self.config = config
        self.dir = config.root / USER_DIR
        self.path = self.dir / USER_FILE
        self.examples_path = self.dir / EXAMPLES_FILE

    # ---- storage
    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _examples(self) -> dict[str, list]:
        try:
            data = json.loads(self.examples_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, rules: list[dict], examples: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.path, (json.dumps(rules, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        atomic_write_bytes(self.examples_path, (json.dumps(examples, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))

    # ---- operations
    def selftest(self, entry: dict[str, Any], examples: list[dict[str, Any]]) -> list[str]:
        """Build the rule in isolation and run its examples. Returns problems (empty = ok)."""
        from .validator import run_example
        try:
            rule = build_rule(entry)
        except RuleError as exc:
            return [str(exc)]
        if not any(e.get("kind") in ("fix", "detect") for e in examples):
            return ["at least one example that triggers the rule is required"]
        reg = RuleRegistry()
        reg.register(rule)
        problems = []
        for ex in examples:
            r = run_example(reg, self.config, entry["id"], ex)
            if not r.ok:
                problems.append(f"example '{ex.get('name')}': {r.message}")
        return problems

    def save(self, entry: dict[str, Any], examples: list[dict[str, Any]], *, replace: bool = False) -> list[str]:
        """Validate, self-test and persist. Returns problems; nothing is written if any."""
        problems = self.selftest(entry, examples)
        if problems:
            return problems
        existing = build_registry(self.config, plugins=True)
        rules = self.load()
        mine = {r.get("id") for r in rules}
        if entry["id"] in mine and not replace:
            return [f"rule {entry['id']} already exists (save again with replace to overwrite)"]
        if existing.get(entry["id"]) is not None and entry["id"] not in mine:
            return [f"rule id {entry['id']} is already used by a built-in or plugin rule"]
        rules = [r for r in rules if r.get("id") != entry["id"]] + [entry]
        ex = self._examples()
        ex[entry["id"]] = examples
        self._write(rules, ex)
        return []

    def delete(self, rule_id: str) -> bool:
        rules = self.load()
        kept = [r for r in rules if r.get("id") != rule_id]
        if len(kept) == len(rules):
            return False
        ex = self._examples()
        ex.pop(rule_id, None)
        self._write(kept, ex)
        return True

    def export(self) -> dict[str, Any]:
        ex = self._examples()
        return {"format": "codefix-knowledge", "version": 1, "rules": self.load(),
                "examples": {r["id"]: ex.get(r["id"], []) for r in self.load()}}

    def import_pack(self, data: Any, *, replace: bool = False) -> dict[str, Any]:
        """Import ``{"rules": [...], "examples": {...}}`` or a bare list of rule entries."""
        rules = data.get("rules") if isinstance(data, dict) else data
        examples = data.get("examples", {}) if isinstance(data, dict) else {}
        if not isinstance(rules, list):
            return {"imported": [], "rejected": {"-": ["expected a list of rules or an object with a 'rules' list"]}}
        imported, rejected = [], {}
        for entry in rules:
            rid = entry.get("id", "?") if isinstance(entry, dict) else "?"
            if not isinstance(entry, dict) or "match" not in entry:
                rejected[rid] = ["only declarative rules (with a 'match') can be imported; Python rules need a plugin"]
                continue
            exs = examples.get(rid) or None
            try:
                exs = exs or examples_for(entry, [])
            except RuleError as exc:
                rejected[rid] = [str(exc)]
                continue
            problems = self.save(entry, exs, replace=replace)
            (rejected.__setitem__(rid, problems) if problems else imported.append(rid))
        return {"imported": imported, "rejected": rejected}
