"""Locating and loading knowledge packs (JSON) and rule examples."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import ProjectConfig
from .registry import RuleRegistry
from .schema import SchemaError, check_entry, rule_from_dict

PACK_LAYOUT: dict[str, str] = {
    "python": "python/patterns.json", "django": "frameworks/django.json", "fastapi": "frameworks/fastapi.json",
    "flask": "frameworks/flask.json", "security": "security/rules.json",
}
ALL_PACKS = [*PACK_LAYOUT, "project"]


@dataclass
class PackFile:
    pack: str
    path: Path
    entries: list[Any] = field(default_factory=list)
    error: str | None = None


def knowledge_dirs(config: ProjectConfig) -> list[Path]:
    """Bundled packs first, then the project's own. Duplicated directories are collapsed."""
    cands: list[Path] = []
    env = os.environ.get("CODEFIX_KNOWLEDGE_DIR")
    here = Path(__file__).resolve()
    cands.append(here.parents[1] / "_knowledge")           # bundled in the wheel
    cands.append(here.parents[3] / "knowledge")            # source checkout / editable install
    if env:
        cands.append(Path(env))
    cands += [(config.root / p) for p in config.knowledge_paths]
    cands.append(config.root / "knowledge")
    out, seen = [], set()
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r.is_dir() and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _read(pack: str, path: Path) -> PackFile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return PackFile(pack, path, error=f"cannot read JSON: {exc}")
    if isinstance(data, dict) and "rules" in data:
        data = data["rules"]
    if not isinstance(data, list):
        return PackFile(pack, path, error="top level must be a list of rules (or {\"rules\": [...]})")
    return PackFile(pack, path, entries=data)


def load_pack_files(config: ProjectConfig, packs: list[str] | None = None) -> list[PackFile]:
    wanted = ALL_PACKS if packs is None else packs
    out: list[PackFile] = []
    for d in knowledge_dirs(config):
        for pack in wanted:
            if pack == "project":
                files = sorted((d / "project").glob("*.json")) if (d / "project").is_dir() else []
            else:
                rel = PACK_LAYOUT.get(pack)
                files = [d / rel] if rel and (d / rel).is_file() else []
            out += [_read(pack, f) for f in files]
    return out


def load_examples(config: ProjectConfig) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for d in knowledge_dirs(config):
        for f in sorted(d.glob("*/examples.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                for rid, exs in data.items():
                    if isinstance(exs, list):
                        merged.setdefault(rid, []).extend(exs)
    return merged


def apply_knowledge(registry: RuleRegistry, config: ProjectConfig) -> None:
    """Merge documentation from the selected packs into the registry (never changes behaviour)."""
    for pf in load_pack_files(config, config.packs):
        for raw in pf.entries:
            if check_entry(raw) and any(not p.startswith("unknown field") for p in check_entry(raw)):
                continue                                   # reported by `codefix knowledge validate`
            try:
                spec = rule_from_dict(raw, pf.pack)
            except SchemaError:
                continue
            existing = registry.get(spec.id)
            if existing is None:
                spec.implemented = False
                registry.add_doc_spec(spec)
                continue
            s = existing.spec
            s.pack = pf.pack
            for attr in ("description", "explanation", "false_positive", "bad", "good", "message"):
                if not getattr(s, attr):
                    setattr(s, attr, getattr(spec, attr))
            for attr in ("frameworks", "docs", "tests"):
                if not getattr(s, attr):
                    setattr(s, attr, getattr(spec, attr))
