"""Strongly typed data models shared by every CodeFix layer."""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class Severity(str, enum.Enum):
    SAFE = "safe"
    REVIEW = "review"
    RISKY = "risky"
    BLOCKED = "blocked"

    @property
    def rank(self) -> int:
        return ["safe", "review", "risky", "blocked"].index(self.value)


class Action(str, enum.Enum):
    """What the policy engine allows for a finding."""

    AUTO = "auto"                  # may be applied without asking
    CONFIRM = "confirm"            # needs a yes from the user
    EXPLICIT = "explicit"          # needs explicit, rule-specific approval
    REPORT_ONLY = "report_only"    # never applied
    BLOCK = "block"                # forbidden


@dataclass(frozen=True)
class Location:
    path: str
    line: int = 1
    column: int = 1          # 1-based, counted in characters
    end_line: int = 1
    end_column: int = 1


@dataclass
class ProposedEdit:
    """A replacement of ``source_bytes[start:end]`` (UTF-8 offsets) by ``replacement``."""

    rule_id: str
    path: str
    start: int
    end: int
    replacement: str
    description: str = ""
    expected: str | None = None   # text the rule saw at [start:end]; guards against stale edits

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "path": self.path, "start": self.start, "end": self.end,
                "replacement": self.replacement, "description": self.description}


@dataclass
class Finding:
    rule_id: str
    message: str
    category: str
    severity: Severity
    confidence: float
    location: Location
    function: str | None = None
    fixable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    edits: list[ProposedEdit] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.location.path

    @property
    def line(self) -> int:
        return self.location.line

    @property
    def column(self) -> int:
        return self.location.column

    def to_dict(self, include_edits: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rule_id": self.rule_id, "message": self.message, "category": self.category,
            "severity": self.severity.value, "confidence": self.confidence,
            "file": self.location.path, "line": self.location.line, "column": self.location.column,
            "end_line": self.location.end_line, "end_column": self.location.end_column,
            "function": self.function, "fixable": self.fixable, "metadata": self.metadata,
        }
        if include_edits:
            d["edits"] = [e.to_dict() for e in self.edits]
        return d


@dataclass
class RuleSpec:
    id: str
    category: str
    severity: Severity
    confidence: float
    description: str = ""
    message: str = ""
    explanation: str = ""
    bad: str = ""
    good: str = ""
    autofix: bool = False
    requires_tests: bool = False
    false_positive: str = ""
    frameworks: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    pack: str = "python"
    implemented: bool = True      # False for documentation-only knowledge entries

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class FunctionMetrics:
    name: str
    line: int
    end_line: int
    lines: int
    complexity: int
    params: int
    returns: int
    calls: int
    nesting: int
    nesting_line: int
    is_method: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class AnalysisResult:
    path: str
    findings: list[Finding] = field(default_factory=list)
    metrics: list[FunctionMetrics] = field(default_factory=list)
    error: str | None = None          # file could not be read / analysed
    syntax_ok: bool = True
    suppressed: int = 0
    lines: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, include_metrics: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path, "syntax_ok": self.syntax_ok, "error": self.error,
                             "suppressed": self.suppressed, "lines": self.lines, "warnings": self.warnings,
                             "findings": [f.to_dict() for f in self.findings]}
        if include_metrics:
            d["metrics"] = [m.to_dict() for m in self.metrics]
        return d


@dataclass
class Decision:
    finding: Finding
    action: Action
    reason: str
    requires_tests: bool = False


@dataclass
class VerificationStep:
    name: str
    ok: bool
    message: str = ""


@dataclass
class VerificationResult:
    ok: bool = True
    steps: list[VerificationStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, message: str = "") -> bool:
        self.steps.append(VerificationStep(name, ok, message))
        if not ok:
            self.ok = False
        return ok

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "steps": [dataclasses.asdict(s) for s in self.steps],
                "warnings": self.warnings, "details": self.details}


@dataclass
class SkippedChange:
    rule_id: str
    line: int
    reason: str


@dataclass
class ChangeSet:
    path: str
    original: str
    new: str
    applied: list[Finding] = field(default_factory=list)
    skipped: list[SkippedChange] = field(default_factory=list)
    diff: str = ""
    warnings: list[str] = field(default_factory=list)
    verification: VerificationResult = field(default_factory=VerificationResult)
    description: str = ""
    expected_removed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.original != self.new

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "changed": self.changed, "description": self.description,
                "applied": [{"rule_id": f.rule_id, "line": f.line, "message": f.message} for f in self.applied],
                "skipped": [dataclasses.asdict(s) for s in self.skipped],
                "warnings": self.warnings, "diff": self.diff, "verification": self.verification.to_dict()}


@dataclass
class TestConfig:
    command: str = ""
    run_after_write: bool = True
    timeout: int = 600


@dataclass
class ProjectConfig:
    root: Path = field(default_factory=Path.cwd)
    config_file: Path | None = None
    name: str = ""
    python: str = ""
    exclude: list[str] = field(default_factory=list)
    formatter: str = "none"
    allow_risky: bool = False
    minimum_confidence: float = 0.80
    require_tests_for_risky: bool = True
    backup_before_write: bool = True
    rollback_on_failure: bool = True
    max_passes: int = 3
    packs: list[str] = field(default_factory=lambda: ["python"])
    knowledge_paths: list[str] = field(default_factory=list)
    disabled_rules: list[str] = field(default_factory=list)
    blocked_rules: list[str] = field(default_factory=list)
    max_function_lines: int = 50
    max_complexity: int = 10
    max_params: int = 5
    max_returns: int = 6
    max_calls: int = 30
    max_nesting: int = 4
    min_duplicate_statements: int = 4
    tests: TestConfig = field(default_factory=TestConfig)
    security_minimum_confidence: float = 0.95
    security_allow_autofix: bool = False
    plugin_modules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["root"] = str(self.root)
        d["config_file"] = str(self.config_file) if self.config_file else None
        return d
