"""Application service: orchestrates analysis, policy, patching and verification.

The CLI is a thin layer over :class:`CodeFix`. Nothing below this module prints or exits.
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from . import refactor
from .ai import ProposalError, load_proposals
from .analysis.ast_engine import AnalysisEngine
from .config import load_config
from .knowledge.registry import RuleRegistry, build_registry
from .models import Action, AnalysisResult, ChangeSet, Decision, Finding, ProjectConfig, SkippedChange
from .patching import (BackupManager, EditConflict, PatchEngine, RollbackError, RollbackManager, atomic_write_bytes,
                       unified_diff)
from .patching.diff import colorize
from .policy import PolicyEngine, iter_python_files
from .source import SourceError, SourceFile
from .verification.formatter import CommandFormatter, format_text
from .verification.regression import compare_findings
from .verification.syntax import check_syntax, validate_change
from .verification.tests import TestRunResult, run_tests

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


class UserQuit(Exception):
    pass


@dataclass
class FixOptions:
    command: str = "fix"                 # fix | autofix | approve
    write: bool = False
    yes: bool = False
    ci: bool = False
    interactive: bool = False
    approve_rules: set[str] = field(default_factory=set)
    passes: int = 1
    only_rules: set[str] | None = None
    safe_only: bool = False
    format: bool = True
    preview: bool = False                # dry run: show every fixable change as if approved


@dataclass
class FileOutcome:
    path: str
    status: str            # unchanged | dry-run | written | rolled_back | failed | rejected | error
    changeset: ChangeSet | None = None
    error: str | None = None
    remaining: list[Finding] = field(default_factory=list)
    tests: TestRunResult | None = None
    backup: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in ("rolled_back", "failed", "rejected", "error")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "error": self.error, "backup": self.backup,
                "changeset": self.changeset.to_dict() if self.changeset else None,
                "remaining_findings": [f.to_dict() for f in self.remaining],
                "tests": self.tests.to_dict() if self.tests else None}


@dataclass
class RunReport:
    command: str
    outcomes: list[FileOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    write: bool = False

    @property
    def exit_code(self) -> int:
        if self.errors or any(o.failed for o in self.outcomes):
            return EXIT_ERROR
        if any(o.remaining for o in self.outcomes):
            return EXIT_FINDINGS
        if not self.write and any(o.status == "dry-run" for o in self.outcomes):
            return EXIT_FINDINGS
        return EXIT_OK

    def summary(self) -> dict[str, Any]:
        c = Counter(o.status for o in self.outcomes)
        applied = sum(len(o.changeset.applied) or 1 for o in self.outcomes if o.changeset
                      and o.status in (("written",) if self.write else ("dry-run",)))
        return {"files": len(self.outcomes), "by_status": dict(c), "changes": applied,
                "remaining_findings": sum(len(o.remaining) for o in self.outcomes), "errors": len(self.errors)
                + sum(o.failed for o in self.outcomes)}

    def to_dict(self) -> dict[str, Any]:
        return {"tool": "codefix", "command": self.command, "write": self.write, "summary": self.summary(),
                "exit_code": self.exit_code, "errors": self.errors, "files": [o.to_dict() for o in self.outcomes]}


@dataclass
class ReviewReport:
    results: list[AnalysisResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return [f for r in self.results for f in r.findings]

    @property
    def exit_code(self) -> int:
        if self.errors or any(r.error for r in self.results):
            return EXIT_ERROR
        return EXIT_FINDINGS if self.findings else EXIT_OK

    def summary(self) -> dict[str, Any]:
        fs = self.findings
        return {"files": len(self.results), "findings": len(fs), "fixable": sum(f.fixable for f in fs),
                "by_severity": dict(Counter(f.severity.value for f in fs)),
                "by_category": dict(Counter(f.category for f in fs)), "by_rule": dict(sorted(Counter(f.rule_id for f in fs).items())),
                "suppressed": sum(r.suppressed for r in self.results),
                "errors": len(self.errors) + sum(bool(r.error) for r in self.results)}

    def to_dict(self, metrics: bool = False) -> dict[str, Any]:
        return {"tool": "codefix", "command": "review", "summary": self.summary(), "exit_code": self.exit_code,
                "errors": self.errors, "files": [r.to_dict(metrics) for r in self.results]}


class ApprovalSession:
    """Decides whether each proposed change may be applied (interactive, --yes or CI)."""

    def __init__(self, opts: FixOptions, out: TextIO, input_fn: Callable[[str], str]):
        self.opts, self.out, self.input = opts, out, input_fn
        self.accept_all: set[str] = set()
        self.reject_all: set[str] = set()

    def ask(self, d: Decision, index: int, total: int) -> bool:
        f, rid = d.finding, d.finding.rule_id
        if self.opts.preview or rid in self.opts.approve_rules:
            return True
        if rid in self.reject_all:
            return False
        explicit = d.action is Action.EXPLICIT or self.opts.command == "approve"
        if not explicit and rid in self.accept_all:
            return True
        if self.opts.ci or not self.opts.interactive:
            return bool(self.opts.yes and not explicit)
        self.out.write(f"\nFinding {index} of {total}\nFile: {f.path}\nLine: {f.line}\nRule: {rid}\n"
                       f"Severity: {f.severity.value}\nMessage: {f.message}\n")
        for e in f.edits[:3]:
            before = e.expected if e.expected is not None else ""
            self.out.write(f"  - {before.strip()[:70]!r} -> {e.replacement.strip()[:70]!r}\n")
        self.out.write("\n")
        if explicit:
            ans = self.input(f"This {f.severity.value} change needs explicit approval. Type 'approve' to apply, "
                             "anything else to skip: ").strip().lower()
            return ans == "approve"
        while True:
            ans = self.input("Apply this change? [y]es [n]o [a]ll [r]eject-all [q]uit: ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no", ""):
                return False
            if ans in ("a", "all"):
                self.accept_all.add(rid)
                return True
            if ans in ("r", "reject-all"):
                self.reject_all.add(rid)
                return False
            if ans in ("q", "quit"):
                raise UserQuit()


class CodeFix:
    def __init__(self, config: ProjectConfig | None = None, registry: RuleRegistry | None = None, *,
                 out: TextIO | None = None, input_fn: Callable[[str], str] | None = None,
                 formatters: list[CommandFormatter] | None = None, color: bool = False):
        self.config = config or load_config()
        self.registry = registry or build_registry(self.config)
        self.engine = AnalysisEngine(self.registry, self.config)
        self.policy = PolicyEngine(self.config, self.registry)
        self.backups = BackupManager(self.config.root)
        self.rollback = RollbackManager(self.backups)
        self.out = out or sys.stdout
        self.input = input_fn or input
        self.formatters = formatters
        self.color = color

    # ------------------------------------------------------------------ helpers
    def _say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def collect(self, paths: list[str | Path]) -> tuple[list[Path], list[str]]:
        return iter_python_files(paths, self.config)

    # ------------------------------------------------------------------- review
    def review(self, paths: list[str | Path]) -> ReviewReport:
        files, errors = self.collect(paths)
        report = ReviewReport(errors=errors)
        for f in files:
            try:
                report.results.append(self.engine.analyze_file(f))
            except Exception as exc:  # keep going after unexpected per-file failures
                report.results.append(AnalysisResult(str(f), error=f"{exc.__class__.__name__}: {exc}"))
        return report

    def diagnostics(self, paths: list[str | Path], metrics: bool = False) -> dict[str, Any]:
        report = self.review(paths)
        funcs = [(r.path, m) for r in report.results for m in r.metrics]
        d: dict[str, Any] = report.to_dict(metrics)
        d["command"] = "diagnostics"
        s = d["summary"]
        s["lines"] = sum(r.lines for r in report.results)
        s["functions"] = len(funcs)
        s["syntax_errors"] = sum(not r.syntax_ok for r in report.results)
        if metrics:
            cfg = self.config
            top = sorted(funcs, key=lambda fm: (-fm[1].complexity, fm[0], fm[1].line))[:10]
            s["metrics"] = {
                "average_complexity": round(sum(m.complexity for _, m in funcs) / len(funcs), 2) if funcs else 0,
                "max_complexity": max((m.complexity for _, m in funcs), default=0),
                "over_complexity_limit": sum(m.complexity > cfg.max_complexity for _, m in funcs),
                "over_length_limit": sum(m.lines > cfg.max_function_lines for _, m in funcs),
                "most_complex": [{"file": p, "function": m.name, "line": m.line, "complexity": m.complexity} for p, m in top]}
        return d

    # ---------------------------------------------------------------- fix flows
    def fix(self, paths: list[str | Path], opts: FixOptions) -> RunReport:
        files, errors = self.collect(paths)
        report = RunReport(opts.command, errors=errors, write=opts.write)
        session = ApprovalSession(opts, self.out, self.input)
        for f in files:
            try:
                report.outcomes.append(self._fix_file(f, opts, session))
            except UserQuit:
                report.errors.append("quit by user")
                break
            except Exception as exc:
                report.outcomes.append(FileOutcome(str(f), "error", error=f"{exc.__class__.__name__}: {exc}"))
        return report

    def apply_proposals(self, proposal_file: str | Path, opts: FixOptions) -> RunReport:
        report = RunReport("apply-proposal", write=opts.write)
        try:
            grouped, problems = load_proposals(proposal_file, self.config.root)
        except ProposalError as exc:
            report.errors.append(str(exc))
            return report
        report.errors += problems
        session = ApprovalSession(opts, self.out, self.input)
        for path in sorted(grouped):
            try:
                report.outcomes.append(self._fix_file(path, opts, session, proposals=grouped[path]))
            except UserQuit:
                report.errors.append("quit by user")
                break
            except Exception as exc:
                report.outcomes.append(FileOutcome(str(path), "error", error=f"{exc.__class__.__name__}: {exc}"))
        return report

    def _select(self, cands: list[Finding], opts: FixOptions, session: ApprovalSession,
                cs: ChangeSet) -> list[tuple[Finding, Decision]]:
        decisions = [self.policy.decide(f) for f in cands]
        pending = [d for d in decisions if d.action in (Action.CONFIRM, Action.EXPLICIT)]
        selected: list[tuple[Finding, Decision]] = []
        n = 0
        have_tests = bool(self.config.tests.command.strip())
        for d in decisions:
            f = d.finding
            if d.action is Action.AUTO:
                if opts.command == "approve":
                    continue
            elif d.action in (Action.CONFIRM, Action.EXPLICIT):
                if opts.command == "autofix" or opts.safe_only:
                    continue
                n += 1
                if not session.ask(d, n, len(pending)):
                    cs.skipped.append(SkippedChange(f.rule_id, f.line, "not approved"))
                    continue
            else:
                continue
            if d.requires_tests and not have_tests:
                cs.skipped.append(SkippedChange(f.rule_id, f.line,
                                                "requires passing tests but no test command is configured ([tests] command)"))
                continue
            selected.append((f, d))
        return selected

    def _fix_file(self, path: Path, opts: FixOptions, session: ApprovalSession,
                  proposals: list[Finding] | None = None) -> FileOutcome:
        try:
            src0 = SourceFile.read(path)
        except SourceError as exc:
            return FileOutcome(str(path), "error", error=str(exc))
        first = self.engine.analyze_source(src0)
        if first.error:
            return FileOutcome(str(path), "error", error=first.error)
        if not first.syntax_ok:
            return FileOutcome(str(path), "error", remaining=first.findings,
                               error="file has a syntax error and cannot be fixed: " + first.findings[0].message)
        cs = ChangeSet(str(path), src0.text, src0.text)
        cur, res = src0, first
        needs_tests = False
        for pass_no in range(1 if proposals is not None else max(1, opts.passes)):
            if pass_no:
                res = self.engine.analyze_source(cur)
                if not res.syntax_ok:
                    break
            cands = proposals if proposals is not None else [f for f in res.findings if f.fixable]
            if opts.only_rules:
                cands = [f for f in cands if f.rule_id in opts.only_rules]
            selected = self._select(cands, opts, session, cs)
            if not selected:
                break
            base = cur.text
            pr = PatchEngine.apply_findings(cur, [f for f, _ in selected],
                                            lambda t, b=base: validate_change(b, t, str(path)))
            cs.skipped += pr.skipped
            if not pr.accepted:
                break
            cs.applied += pr.accepted
            ids = {id(f) for f in pr.accepted}
            needs_tests = needs_tests or any(d.requires_tests for f, d in selected if id(f) in ids)
            cur = cur.with_text(pr.text)
        cs.new = cur.text
        after = self.engine.analyze_source(cur) if cs.changed else first
        remaining = list(after.findings)
        if not cs.changed:
            return FileOutcome(str(path), "unchanged", cs, remaining=remaining)
        out = self._finalize(src0, cs, first, opts.write, needs_tests, opts.format)
        out.remaining = remaining
        return out

    # ----------------------------------------------------------------- finalize
    def _finalize(self, src: SourceFile, cs: ChangeSet, before: AnalysisResult | None, write: bool,
                  needs_tests: bool, run_formatter: bool) -> FileOutcome:
        path, v = cs.path, cs.verification
        v.add("parse-original", check_syntax(src.text, path).parse_ok, "original file parses")
        ok, why = validate_change(src.text, cs.new, path, cs.expected_removed)
        v.add("parse-result", ok, why or "edited source parses and keeps every definition")
        if ok and run_formatter and self.config.formatter != "none":
            fr = format_text(cs.new, path, self.config, self.formatters)
            if fr.ok and fr.changed:
                cs.new = fr.text
                v.add("format", True, f"formatted with {fr.formatter}; result parses again")
            elif not fr.ok:
                cs.warnings.append(f"formatter skipped: {fr.error}" + ("" if not fr.available else
                                   " (the unformatted edit is kept)"))
        try:
            src.encode(cs.new)
            v.add("encoding", True, f"representable as {src.encoding}")
        except SourceError as exc:
            v.add("encoding", False, str(exc))
        if v.ok:
            after = self.engine.analyze_source(src.with_text(cs.new))
            before = before or self.engine.analyze_source(src)
            rok, errs, warns = compare_findings(before, after)
            v.add("regression", rok, "; ".join(errs) or "no new correctness findings")
            cs.warnings += warns
        cs.diff = unified_diff(src.text, cs.new, path)
        if cs.diff:
            self._say(colorize(cs.diff) if self.color else cs.diff.rstrip("\n"))
        if not v.ok:
            failed = "; ".join(f"{s.name}: {s.message}" for s in v.steps if not s.ok)
            return FileOutcome(path, "rejected", cs, error=f"verification failed, nothing written ({failed})")
        if not cs.changed:
            return FileOutcome(path, "unchanged", cs)
        if not write:
            return FileOutcome(path, "dry-run", cs)
        return self._write(src, cs, needs_tests)

    def _write(self, src: SourceFile, cs: ChangeSet, needs_tests: bool) -> FileOutcome:
        path = Path(cs.path)
        v = cs.verification
        original_bytes = src.encode()
        try:
            if path.read_bytes() != original_bytes:
                v.add("unchanged-on-disk", False, "file changed since it was read")
                return FileOutcome(cs.path, "rejected", cs, error="the file changed on disk while CodeFix was working; nothing written")
        except OSError as exc:
            return FileOutcome(cs.path, "error", cs, error=str(exc))
        record = None
        try:
            if self.config.backup_before_write:
                record = self.backups.create(path)
            atomic_write_bytes(path, src.encode(cs.new), mode=src.mode)
        except (OSError, SourceError) as exc:
            return FileOutcome(cs.path, "error", cs, error=f"write failed: {exc}",
                               backup=record.backup if record else None)
        v.add("write", True, "atomic write completed" + (f", backup {Path(record.backup).name}" if record else ""))
        backup = record.backup if record else None
        run_it = needs_tests or (bool(self.config.tests.command.strip()) and self.config.tests.run_after_write)
        if not run_it:
            return FileOutcome(cs.path, "written", cs, backup=backup)
        tr = run_tests(self.config)
        if tr.ok:
            v.add("tests", True, "configured tests passed")
            return FileOutcome(cs.path, "written", cs, tests=tr, backup=backup)
        v.add("tests", False, tr.error or f"{len(tr.failures)} failing test(s)")
        if not self.config.rollback_on_failure:
            return FileOutcome(cs.path, "failed", cs, tests=tr, backup=backup,
                               error="tests failed and rollback_on_failure is disabled: the file stays modified")
        try:
            if record:
                self.rollback.restore(record, keep_pre_undo=False)
            else:
                atomic_write_bytes(path, original_bytes, mode=src.mode)
        except (RollbackError, OSError) as exc:
            return FileOutcome(cs.path, "failed", cs, tests=tr, backup=backup,
                               error=f"tests failed AND rollback failed ({exc}); restore {cs.path} manually")
        v.add("rollback", True, "original restored after failing tests")
        return FileOutcome(cs.path, "rolled_back", cs, tests=tr, backup=backup,
                           error="tests failed; the original file was restored")

    # -------------------------------------------------------------- refactoring
    def _refactor(self, path: str | Path, fn: Callable[[SourceFile], refactor.RefactorResult], write: bool) -> RunReport:
        report = RunReport("refactor", write=write)
        try:
            src = SourceFile.read(path)
            res = fn(src)
        except (SourceError, refactor.RefactorError, EditConflict) as exc:
            report.outcomes.append(FileOutcome(str(path), "error", error=str(exc)))
            return report
        cs = ChangeSet(str(path), src.text, res.new_text, description=res.description, warnings=list(res.warnings),
                       expected_removed=res.expected_removed)
        report.outcomes.append(self._finalize(src, cs, None, write, False, False))
        return report

    def rename(self, path, old, new, write=False, scope=None, engine="auto") -> RunReport:
        r = self._refactor(path, lambda s: refactor.rename_symbol(s, old, new, scope, engine), write)
        r.command = "rename"
        return r

    def replace(self, path, name, replacement_file, write=False) -> RunReport:
        try:
            text = Path(replacement_file).read_text(encoding="utf-8")
        except OSError as exc:
            return RunReport("replace", errors=[f"cannot read {replacement_file}: {exc}"], write=write)
        r = self._refactor(path, lambda s: refactor.replace_function(s, name, text), write)
        r.command = "replace"
        return r

    def stub(self, path, name, write=False) -> RunReport:
        r = self._refactor(path, lambda s: refactor.stub_function(s, name), write)
        r.command = "stub"
        return r

    # ------------------------------------------------------- format / check / test
    def format(self, paths: list[str | Path], write: bool = False) -> RunReport:
        files, errors = self.collect(paths)
        report = RunReport("format", errors=errors, write=write)
        if self.config.formatter == "none" and self.formatters is None:
            report.errors.append("no formatter configured: set [behavior] formatter = \"ruff\" (or \"black\")")
            return report
        for p in files:
            try:
                src = SourceFile.read(p)
            except SourceError as exc:
                report.outcomes.append(FileOutcome(str(p), "error", error=str(exc)))
                continue
            fr = format_text(src.text, p, self.config, self.formatters)
            if not fr.ok:
                report.outcomes.append(FileOutcome(str(p), "error", error=f"formatter failed: {fr.error}"))
                continue
            if not fr.changed:
                report.outcomes.append(FileOutcome(str(p), "unchanged"))
                continue
            cs = ChangeSet(str(p), src.text, fr.text, description=f"format with {fr.formatter}")
            report.outcomes.append(self._finalize(src, cs, None, write, False, False))
        return report

    def check(self, paths: list[str | Path]) -> dict[str, Any]:
        files, errors = self.collect(paths)
        entries = []
        for p in files:
            e: dict[str, Any] = {"path": str(p), "syntax_ok": False, "formatted": None, "error": None}
            try:
                src = SourceFile.read(p)
                s = check_syntax(src.text, str(p))
                e["syntax_ok"] = s.ok
                if not s.ok:
                    e["error"] = f"line {s.line}: {s.error}"
                elif self.config.formatter != "none" or self.formatters:
                    fr = format_text(src.text, p, self.config, self.formatters)
                    e["formatted"] = (not fr.changed) if fr.ok else None
                    if not fr.ok:
                        e["error"] = f"formatter: {fr.error}"
            except SourceError as exc:
                e["error"] = str(exc)
            entries.append(e)
        bad = bool(errors) or any(not e["syntax_ok"] or e["formatted"] is False for e in entries)
        return {"tool": "codefix", "command": "check", "ok": not bad, "errors": errors, "files": entries,
                "exit_code": EXIT_FINDINGS if bad else EXIT_OK}

    def test(self, targets: list[str] | None = None) -> TestRunResult:
        return run_tests(self.config, targets)

    # --------------------------------------------------------------------- undo
    def undo(self, path: str | Path, list_only: bool = False) -> dict[str, Any]:
        if list_only:
            return {"path": str(path), "backups": [r.__dict__ for r in self.backups.list(path)]}
        try:
            rec = self.rollback.undo(path)
        except (RollbackError, OSError) as exc:
            return {"path": str(path), "restored": False, "error": str(exc)}
        return {"path": str(path), "restored": True, "from_backup": rec.backup, "backup_timestamp": rec.timestamp}

