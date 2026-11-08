"""Skills: named, shareable bundles of declarative rules (knowledge packs).

A skill is one JSON file in ``<project>/knowledge/skills/<name>.json``::

    {"format": "codefix-skill", "version": 1, "name": "my-team", "title": "...", "description": "...",
     "rules": [ {id, message, bad, good?, match, severity?, confidence?, ...} ],
     "examples": {"RULE_ID": [ {"name", "kind": "fix|detect|negative", ...} ]}}      # optional

Learning a skill is all-or-nothing: every rule must build, must not clash with an existing rule and must pass
its own examples (generated from ``bad``/``good`` when none are given).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import read_overlay, write_overlay
from ..models import ProjectConfig
from ..patching import PatchEngine, unified_diff
from ..patching.atomic_write import atomic_write_bytes
from .declarative import RuleError, build_rule, normalize_entry
from .registry import RuleRegistry, build_registry
from .userpack import examples_for, selftest_entry

FORMAT = "codefix-skill"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


class SkillStore:
    def __init__(self, config: ProjectConfig):
        self.config = config
        self.dir = config.root / "knowledge" / "skills"

    # ------------------------------------------------------------------ storage
    def path(self, name: str):
        return self.dir / f"{name}.json"

    def read(self, name: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path(name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json")) if self.dir.is_dir() else []

    def _write(self, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.path(data["name"]), (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    def list(self) -> list[dict[str, Any]]:
        off = set(self.config.skills_disabled)
        out = []
        for n in self.names():
            d = self.read(n)
            if d is None:
                out.append({"name": n, "ok": False, "error": "not valid JSON", "enabled": n not in off, "rules": []})
                continue
            out.append({"name": n, "ok": True, "title": d.get("title", ""), "description": d.get("description", ""),
                        "author": d.get("author", ""), "enabled": n not in off,
                        "rules": [{"id": r.get("id"), "message": r.get("message", ""), "severity": r.get("severity", "review"),
                                   "autofix": bool(r.get("autofix")), "match": r.get("match")} for r in d.get("rules", []) if isinstance(r, dict)]})
        return out

    # --------------------------------------------------------------- validation
    def _taken_ids(self, replacing: str | None) -> set[str]:
        ids = {s.id for s in build_registry(self.config).specs()}
        if replacing:
            old = self.read(replacing) or {}
            ids -= {r.get("id") for r in old.get("rules", []) if isinstance(r, dict)}
        return ids

    def validate(self, data: Any, *, replacing: str | None = None, require_rules: bool = True) -> dict[str, Any]:
        """Dry-run: check a skill without saving it. Never raises."""
        report: dict[str, Any] = {"ok": False, "name": None, "rule_count": 0, "problems": [], "rules": {}, "data": None}
        if not isinstance(data, dict):
            report["problems"].append("a skill must be a JSON object")
            return report
        if data.get("format", FORMAT) != FORMAT:
            report["problems"].append(f"'format' must be '{FORMAT}'")
        name = data.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            report["problems"].append("'name' must be lowercase letters, digits and hyphens (2-40 characters), e.g. my-team-habits")
        report["name"] = name
        rules = data.get("rules")
        if not isinstance(rules, list):
            report["problems"].append("'rules' must be a list")
            rules = []
        if require_rules and not rules:
            report["problems"].append("a skill needs at least one rule")
        taken = self._taken_ids(replacing)
        seen: set[str] = set()
        norm_rules: list[dict[str, Any]] = []
        examples_in = data.get("examples") if isinstance(data.get("examples"), dict) else {}
        examples_out: dict[str, list] = {}
        for entry in rules:
            rid = entry.get("id", "?") if isinstance(entry, dict) else "?"
            probs: list[str] = []
            try:
                e = normalize_entry(entry)
                build_rule(e, f"skill:{name}")
            except RuleError as exc:
                report["rules"][str(rid)] = [str(exc)]
                continue
            if rid in seen:
                probs.append("duplicate rule id inside this skill")
            if rid in taken and rid not in seen:
                probs.append("this id is already used by a built-in, plugin, user or other skill rule")
            seen.add(rid)
            try:
                exs = examples_in.get(rid) or examples_for(e, [])
            except RuleError as exc:
                exs = []
                probs.append(str(exc))
            if not probs:
                probs += selftest_entry(e, exs, self.config, f"skill:{name}")
            report["rules"][rid] = probs
            norm_rules.append(e)
            examples_out[rid] = exs
        for rid in examples_in:
            if rid not in seen:
                report["problems"].append(f"examples given for unknown rule {rid}")
        report["rule_count"] = len(norm_rules)
        report["ok"] = not report["problems"] and not any(report["rules"].values())
        if report["ok"]:
            report["data"] = {"format": FORMAT, "version": 1, "name": name, "title": str(data.get("title", "")),
                              "description": str(data.get("description", "")), "author": str(data.get("author", "")),
                              "rules": norm_rules, "examples": examples_out}
        return report

    # ---------------------------------------------------------------- operations
    def learn(self, data: Any, *, replace: bool = False) -> dict[str, Any]:
        name = data.get("name") if isinstance(data, dict) else None
        exists = isinstance(name, str) and self.path(name).exists()
        report = self.validate(data, replacing=name if exists else None)
        if exists and not replace:
            report["ok"] = False
            report["problems"].insert(0, f"skill '{name}' already exists (use replace to overwrite it)")
        report["learned"] = False
        if report["ok"]:
            self._write(report["data"])
            self._enable_in_overlay(name, True)
            report["learned"] = True
            report["path"] = str(self.path(name))
        report.pop("data", None)
        return report

    def create(self, name: str, title: str = "", description: str = "") -> dict[str, Any]:
        data = {"format": FORMAT, "version": 1, "name": name, "title": title, "description": description,
                "author": "", "rules": [], "examples": {}}
        report = self.validate(data, require_rules=False)
        if report["ok"] and self.path(name).exists():
            report["ok"] = False
            report["problems"].append(f"skill '{name}' already exists")
        if report["ok"]:
            self._write(report["data"])
        report.pop("data", None)
        return report

    def delete(self, name: str) -> bool:
        if not NAME_RE.match(name or "") or not self.path(name).exists():
            return False
        self.path(name).unlink()
        self._enable_in_overlay(name, True)
        return True

    def _enable_in_overlay(self, name: str, enabled: bool) -> None:
        ov = read_overlay(self.config.root)
        if enabled and name not in ov["skills"]:
            return                                  # nothing to change: do not create an empty policy file
        if enabled:
            ov["skills"].pop(name, None)
        else:
            ov["skills"][name] = {"enabled": False}
        write_overlay(self.config.root, ov)

    def set_enabled(self, name: str, enabled: bool) -> list[str]:
        if not self.path(name).exists():
            return [f"no skill named {name}"]
        if enabled:
            data = self.read(name) or {}
            clash = sorted({r.get("id") for r in data.get("rules", []) if isinstance(r, dict)} & self._taken_ids(None))
            if name in self.config.skills_disabled and clash:
                return [f"cannot enable: rule id(s) {', '.join(map(str, clash))} are now used elsewhere"]
        self._enable_in_overlay(name, enabled)
        return []

    def add_rule(self, name: str, entry: dict[str, Any], examples: list[dict[str, Any]] | None = None,
                 *, replace: bool = False) -> dict[str, Any]:
        data = self.read(name)
        if data is None:
            return {"ok": False, "problems": [f"no skill named {name}"], "rules": {}}
        e = normalize_entry(entry)
        rules = [r for r in data.get("rules", []) if r.get("id") != e.get("id")]
        had = len(rules) != len(data.get("rules", []))
        if had and not replace:
            return {"ok": False, "problems": [f"rule {e.get('id')} already exists in {name} (use replace)"], "rules": {}}
        new = dict(data)
        new["rules"] = [*rules, e]
        ex = dict(data.get("examples") or {})
        if examples:
            ex[e["id"]] = examples
        else:
            ex.pop(e["id"], None)
        new["examples"] = ex
        report = self.validate(new, replacing=name)
        if report["ok"]:
            self._write(report["data"])
        report.pop("data", None)
        return report

    def remove_rule(self, name: str, rule_id: str) -> bool:
        data = self.read(name)
        if not data:
            return False
        kept = [r for r in data.get("rules", []) if r.get("id") != rule_id]
        if len(kept) == len(data.get("rules", [])):
            return False
        data["rules"] = kept
        (data.get("examples") or {}).pop(rule_id, None)
        self._write(data)
        return True

    def export(self, name: str) -> dict[str, Any] | None:
        return self.read(name)

    def try_on(self, data: Any, code: str) -> dict[str, Any]:
        """Run a skill (saved or not) on some code and show what it would flag and change."""
        from ..analysis.ast_engine import AnalysisEngine
        from ..source import SourceFile
        from pathlib import Path
        report = self.validate(data, replacing=data.get("name") if isinstance(data, dict) else None, require_rules=False)
        if not report["ok"]:
            return {"ok": False, "problems": report["problems"], "rules": report["rules"], "findings": []}
        reg = RuleRegistry()
        for e in report["data"]["rules"]:
            reg.register(build_rule(e, f"skill:{report['name']}"))
        src = SourceFile.from_bytes(code.encode("utf-8"), Path("try.py"))
        res = AnalysisEngine(reg, self.config).analyze_source(src)
        if not res.syntax_ok:
            return {"ok": False, "problems": ["the test code has a syntax error: " + res.findings[0].message], "findings": []}
        fixable = [f for f in res.findings if f.fixable]
        pr = PatchEngine.apply_findings(src, fixable) if fixable else None
        return {"ok": True, "problems": [], "findings": [f.to_dict() for f in res.findings],
                "fixed": pr.text if pr else None, "diff": unified_diff(src.text, pr.text, "try.py") if pr else ""}
