"""AST analysis engine: builds an :class:`AnalysisContext` and runs the enabled rules."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import AnalysisResult, Finding, Location, ProjectConfig, ProposedEdit, RuleSpec, Severity
from ..source import LineIndex, SourceError, SourceFile
from .complexity import collect_functions, module_metrics
from .symbols import SymbolTable, build_symbols

if TYPE_CHECKING:  # pragma: no cover
    from ..knowledge.registry import RuleRegistry

IGNORE_RE = re.compile(r"#\s*codefix:\s*ignore(?:\[([A-Za-z0-9_,\s]+)\])?")


class AnalysisContext:
    def __init__(self, source: SourceFile, tree: ast.Module, config: ProjectConfig):
        self.source, self.tree, self.config = source, tree, config
        self.path = str(source.path)
        self.data = source.text.encode("utf-8")
        self.index = LineIndex(self.data)
        self._parents: dict[ast.AST, ast.AST] | None = None
        self._symbols: SymbolTable | None = None
        self._metrics = None
        self._functions = None

    # ------------------------------------------------------------------ cached
    @property
    def parents(self) -> dict[ast.AST, ast.AST]:
        if self._parents is None:
            self._parents = {c: p for p in ast.walk(self.tree) for c in ast.iter_child_nodes(p)}
        return self._parents

    @property
    def symbols(self) -> SymbolTable:
        if self._symbols is None:
            self._symbols = build_symbols(self.tree)
        return self._symbols

    @property
    def functions(self):
        if self._functions is None:
            self._functions = collect_functions(self.tree)
        return self._functions

    @property
    def metrics(self):
        if self._metrics is None:
            self._metrics = module_metrics(self.tree)
        return self._metrics

    @property
    def is_test_file(self) -> bool:
        n = Path(self.path).name
        return n.startswith("test_") or n.endswith("_test.py") or n == "conftest.py"

    # ----------------------------------------------------------------- helpers
    def span(self, node: ast.AST) -> tuple[int, int]:
        return (self.index.offset(node.lineno, node.col_offset),
                self.index.offset(node.end_lineno, node.end_col_offset))  # type: ignore[attr-defined]

    def text(self, start: int, end: int) -> str:
        return self.data[start:end].decode("utf-8")

    def segment(self, node: ast.AST) -> str:
        return self.text(*self.span(node))

    def masked(self, start: int, end: int) -> bytes:
        """Bytes ``[start:end]`` with comments blanked out (same length)."""
        buf = bytearray(self.data[start:end])
        i = 0
        while i < len(buf):
            if buf[i] == 0x23:  # '#'
                j = buf.find(b"\n", i)
                j = len(buf) if j == -1 else j
                buf[i:j] = b" " * (j - i)
                i = j
            i += 1
        return bytes(buf)

    def location(self, node: ast.AST) -> Location:
        end_line = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
        end_col = getattr(node, "end_col_offset", None)
        end_col = self.index.char_column(end_line, end_col) if end_col is not None else 1
        return Location(self.path, node.lineno, self.index.char_column(node.lineno, node.col_offset),  # type: ignore[attr-defined]
                        end_line, end_col)

    def enclosing_function(self, node: ast.AST) -> str | None:
        names, cur, found = [], node, False
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found = True
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            cur = self.parents.get(cur)
        return ".".join(reversed(names)) if found else None

    def edit(self, spec: RuleSpec, start: int, end: int, replacement: str, description: str = "") -> ProposedEdit:
        return ProposedEdit(spec.id, self.path, start, end, replacement, description or spec.message,
                            expected=self.text(start, end))

    def finding(self, spec: RuleSpec, node: ast.AST, message: str | None = None, *,
                edits: list[ProposedEdit] | None = None, severity: Severity | None = None,
                confidence: float | None = None, function: str | None = None,
                metadata: dict | None = None, location: Location | None = None) -> Finding:
        return Finding(
            rule_id=spec.id, message=message or spec.message or spec.description, category=spec.category,
            severity=severity or spec.severity, confidence=spec.confidence if confidence is None else confidence,
            location=location or self.location(node),
            function=function if function is not None else self.enclosing_function(node),
            fixable=bool(edits), metadata=metadata or {}, edits=edits or [])


class AnalysisEngine:
    def __init__(self, registry: "RuleRegistry", config: ProjectConfig):
        self.registry, self.config = registry, config

    def analyze_file(self, path: str | Path) -> AnalysisResult:
        try:
            return self.analyze_source(SourceFile.read(path))
        except SourceError as exc:
            return AnalysisResult(str(path), error=str(exc))

    def analyze_source(self, source: SourceFile) -> AnalysisResult:
        path = str(source.path)
        result = AnalysisResult(path, lines=source.text.count("\n") + (0 if source.text.endswith("\n") or not source.text else 1))
        try:
            tree = ast.parse(source.text, filename=path)
        except SyntaxError as exc:
            result.syntax_ok = False
            spec = self.registry.spec("SYNTAX_ERROR")
            line = exc.lineno or 1
            col = exc.offset or 1
            result.findings.append(Finding(
                "SYNTAX_ERROR", f"Syntax error: {exc.msg}", "correctness", Severity.BLOCKED, 1.0,
                Location(path, line, col, exc.end_lineno or line, exc.end_offset or col), None, False,
                {"explanation": spec.explanation if spec else ""}))
            return result
        except (ValueError, RecursionError, MemoryError) as exc:
            result.syntax_ok = False
            result.error = f"cannot parse {path}: {exc}"
            return result

        ctx = AnalysisContext(source, tree, self.config)
        from .confidence_hooks import adjust  # noqa: PLC0415 (avoids import cycle with policy)

        blocked = set(self.config.blocked_rules)
        for rule in self.registry.enabled(self.config):
            if rule.spec.id == "SYNTAX_ERROR":
                continue
            try:
                found = list(rule.check(ctx))
            except Exception as exc:  # a broken rule must never break the run
                result.warnings.append(f"rule {rule.spec.id} crashed: {exc.__class__.__name__}: {exc}")
                continue
            for f in found:
                if f.rule_id in blocked:
                    f.severity = Severity.BLOCKED
                adjust(f, ctx)
                if self._suppressed(ctx, f):
                    result.suppressed += 1
                    continue
                result.findings.append(f)
        result.findings.sort(key=lambda f: (f.line, f.column, f.rule_id))
        result.metrics = ctx.metrics
        return result

    @staticmethod
    def _suppressed(ctx: AnalysisContext, f: Finding) -> bool:
        for line in {f.location.line, f.location.end_line}:
            m = IGNORE_RE.search(ctx.index.line_text(line).decode("utf-8", "replace"))
            if m:
                ids = m.group(1)
                if ids is None or f.rule_id in {i.strip() for i in ids.split(",")}:
                    return True
        return False
