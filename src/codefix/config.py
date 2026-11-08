"""Loading and validating ``codefix.toml``."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ProjectConfig, TestConfig

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

CONFIG_NAME = "codefix.toml"
FORMATTERS = {"ruff", "black", "auto", "none"}
KNOWN: dict[str, set[str]] = {
    "project": {"name", "python", "exclude"},
    "behavior": {"formatter", "allow_risky", "minimum_confidence", "require_tests_for_risky",
                 "backup_before_write", "rollback_on_failure", "max_passes"},
    "knowledge": {"packs", "paths"},
    "rules": {"disabled", "blocked"},
    "metrics": {"max_function_lines", "max_complexity", "max_params", "max_returns", "max_calls",
                "max_nesting", "min_duplicate_statements"},
    "tests": {"command", "run_after_write", "timeout"},
    "security": {"minimum_confidence", "allow_autofix"},
    "plugins": {"modules"},
}


class ConfigError(Exception):
    pass


OVERLAY_NAME = "codefix.policy.json"
RULE_MODES = ("enabled", "report_only", "disabled", "blocked")
SEVERITIES = ("safe", "review", "risky")


def overlay_path(root: Path) -> Path:
    return Path(root) / OVERLAY_NAME


def validate_overlay(data: Any) -> dict[str, Any]:
    """Validate and normalise the policy overlay (what to fix / not fix). Raises ConfigError."""
    if not isinstance(data, dict):
        raise ConfigError(f"{OVERLAY_NAME} must be a JSON object")
    out: dict[str, Any] = {"version": 1, "rules": {}, "behavior": {}, "skills": {}}
    for key in data:
        if key not in ("version", "rules", "behavior", "skills", "note"):
            raise ConfigError(f"{OVERLAY_NAME}: unknown key '{key}'")
    for rid, ov in (data.get("rules") or {}).items():
        if not isinstance(ov, dict):
            raise ConfigError(f"{OVERLAY_NAME}: rules.{rid} must be an object")
        extra = set(ov) - {"mode", "severity", "exclude"}
        if extra:
            raise ConfigError(f"{OVERLAY_NAME}: rules.{rid} has unknown keys {sorted(extra)}")
        mode, sev, exc = ov.get("mode", "enabled"), ov.get("severity"), ov.get("exclude") or []
        if mode not in RULE_MODES:
            raise ConfigError(f"{OVERLAY_NAME}: rules.{rid}.mode must be one of {', '.join(RULE_MODES)}")
        if sev is not None and sev not in SEVERITIES:
            raise ConfigError(f"{OVERLAY_NAME}: rules.{rid}.severity must be one of {', '.join(SEVERITIES)}")
        if not isinstance(exc, list) or not all(isinstance(x, str) and x.strip() for x in exc):
            raise ConfigError(f"{OVERLAY_NAME}: rules.{rid}.exclude must be a list of glob patterns")
        entry: dict[str, Any] = {}
        if mode != "enabled":
            entry["mode"] = mode
        if sev:
            entry["severity"] = sev
        if exc:
            entry["exclude"] = [x.strip() for x in exc]
        if entry:
            out["rules"][str(rid)] = entry
    beh = data.get("behavior") or {}
    if not isinstance(beh, dict) or set(beh) - {"allow_risky", "minimum_confidence"}:
        raise ConfigError(f"{OVERLAY_NAME}: behavior may only contain allow_risky and minimum_confidence")
    if "allow_risky" in beh:
        if not isinstance(beh["allow_risky"], bool):
            raise ConfigError(f"{OVERLAY_NAME}: behavior.allow_risky must be true or false")
        out["behavior"]["allow_risky"] = beh["allow_risky"]
    if "minimum_confidence" in beh:
        v = beh["minimum_confidence"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ConfigError(f"{OVERLAY_NAME}: behavior.minimum_confidence must be between 0 and 1")
        out["behavior"]["minimum_confidence"] = float(v)
    for name, sk in (data.get("skills") or {}).items():
        if not isinstance(sk, dict) or not isinstance(sk.get("enabled", True), bool):
            raise ConfigError(f"{OVERLAY_NAME}: skills.{name}.enabled must be true or false")
        if sk.get("enabled", True) is False:
            out["skills"][str(name)] = {"enabled": False}
    return out


def read_overlay(root: Path) -> dict[str, Any]:
    p = overlay_path(root)
    if not p.is_file():
        return {"version": 1, "rules": {}, "behavior": {}, "skills": {}}
    try:
        return validate_overlay(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc


def write_overlay(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    from .patching.atomic_write import atomic_write_bytes
    clean = validate_overlay(data)
    atomic_write_bytes(overlay_path(root), (json.dumps(clean, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return clean


def apply_overlay(cfg: ProjectConfig) -> ProjectConfig:
    ov = read_overlay(cfg.root)
    cfg.rule_overrides = ov["rules"]
    cfg.skills_disabled = sorted(ov["skills"])
    for k, v in ov["behavior"].items():
        setattr(cfg, k, v)
    return cfg


def find_config(start: Path) -> Path | None:
    start = start.resolve()
    for d in [start, *start.parents]:
        cand = d / CONFIG_NAME
        if cand.is_file():
            return cand
    return None


def _typed(section: str, key: str, value: Any, kind: type | tuple) -> Any:
    if isinstance(value, bool) and kind in (int, float, (int, float)):
        raise ConfigError(f"[{section}] {key} must be a number")
    if not isinstance(value, kind):
        raise ConfigError(f"[{section}] {key} has wrong type: {type(value).__name__}")
    return value


def _str_list(section: str, key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[{section}] {key} must be a list of strings")
    return list(value)


def parse_config(data: dict[str, Any], root: Path, config_file: Path | None = None) -> ProjectConfig:
    cfg = ProjectConfig(root=root, config_file=config_file)
    for section, body in data.items():
        if section not in KNOWN:
            cfg.warnings.append(f"unknown section [{section}] ignored")
            continue
        if not isinstance(body, dict):
            raise ConfigError(f"[{section}] must be a table")
        for key in body:
            if key not in KNOWN[section]:
                cfg.warnings.append(f"unknown key {section}.{key} ignored")

    p, b, k = data.get("project", {}), data.get("behavior", {}), data.get("knowledge", {})
    r, m, t = data.get("rules", {}), data.get("metrics", {}), data.get("tests", {})
    s, pl = data.get("security", {}), data.get("plugins", {})

    if "name" in p: cfg.name = _typed("project", "name", p["name"], str)
    if "python" in p: cfg.python = str(p["python"])
    if "exclude" in p: cfg.exclude = _str_list("project", "exclude", p["exclude"])

    if "formatter" in b:
        f = _typed("behavior", "formatter", b["formatter"], str).lower()
        if f not in FORMATTERS:
            raise ConfigError(f"[behavior] formatter must be one of {sorted(FORMATTERS)}")
        cfg.formatter = f
    for key in ("allow_risky", "require_tests_for_risky", "backup_before_write", "rollback_on_failure"):
        if key in b: setattr(cfg, key, _typed("behavior", key, b[key], bool))
    if "minimum_confidence" in b:
        v = float(_typed("behavior", "minimum_confidence", b["minimum_confidence"], (int, float)))
        if not 0.0 <= v <= 1.0:
            raise ConfigError("[behavior] minimum_confidence must be between 0.0 and 1.0")
        cfg.minimum_confidence = v
    if "max_passes" in b:
        cfg.max_passes = max(1, _typed("behavior", "max_passes", b["max_passes"], int))

    if "packs" in k: cfg.packs = _str_list("knowledge", "packs", k["packs"])
    if "paths" in k: cfg.knowledge_paths = _str_list("knowledge", "paths", k["paths"])
    if "disabled" in r: cfg.disabled_rules = _str_list("rules", "disabled", r["disabled"])
    if "blocked" in r: cfg.blocked_rules = _str_list("rules", "blocked", r["blocked"])

    for key in KNOWN["metrics"]:
        if key in m:
            v = _typed("metrics", key, m[key], int)
            if v < 1:
                raise ConfigError(f"[metrics] {key} must be >= 1")
            setattr(cfg, key, v)

    tests = TestConfig()
    if "command" in t: tests.command = _typed("tests", "command", t["command"], str)
    if "run_after_write" in t: tests.run_after_write = _typed("tests", "run_after_write", t["run_after_write"], bool)
    if "timeout" in t: tests.timeout = _typed("tests", "timeout", t["timeout"], int)
    cfg.tests = tests

    if "minimum_confidence" in s:
        v = float(_typed("security", "minimum_confidence", s["minimum_confidence"], (int, float)))
        if not 0.0 <= v <= 1.0:
            raise ConfigError("[security] minimum_confidence must be between 0.0 and 1.0")
        cfg.security_minimum_confidence = v
    if "allow_autofix" in s: cfg.security_allow_autofix = _typed("security", "allow_autofix", s["allow_autofix"], bool)
    if "modules" in pl: cfg.plugin_modules = _str_list("plugins", "modules", pl["modules"])
    if not cfg.backup_before_write:
        cfg.warnings.append("backup_before_write=false: undo and automatic rollback are unavailable")
    return apply_overlay(cfg)


def load_config(start: str | Path | None = None, explicit: str | Path | None = None) -> ProjectConfig:
    """Load configuration from ``explicit`` or by searching upwards from ``start``."""
    start_p = Path(start) if start else Path.cwd()
    if start_p.is_file():
        start_p = start_p.parent
    path = Path(explicit) if explicit else find_config(start_p)
    if path is None:                      # no config: the project root is where the user is working
        return apply_overlay(ProjectConfig(root=Path.cwd().resolve()))
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc
    return parse_config(data, path.resolve().parent, path.resolve())
