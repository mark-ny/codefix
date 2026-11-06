"""Helpers shared by rule implementations."""
from __future__ import annotations

import ast
import re
from typing import Iterator

from ..analysis.ast_engine import AnalysisContext


def is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def stmt_lists(node: ast.AST) -> Iterator[list[ast.stmt]]:
    for _, value in ast.iter_fields(node):
        if isinstance(value, list) and value and all(isinstance(v, ast.stmt) for v in value):
            yield value


def find_pattern(ctx: AnalysisContext, start: int, end: int, pattern: bytes) -> tuple[int, int] | None:
    """Locate regex ``pattern`` between two operands, ignoring comments."""
    segment = ctx.masked(start, end)
    m = re.search(pattern, segment)
    return (start + m.start(), start + m.end()) if m else None


def single_line(text: str) -> bool:
    return "\n" not in text and "\r" not in text


def line_indent(ctx: AnalysisContext, lineno: int, col: int) -> str | None:
    """Leading whitespace if a statement at ``col`` starts its own line, else None."""
    prefix = ctx.index.line_text(lineno)[:col]
    return prefix.decode("utf-8") if prefix.strip() == b"" else None


def is_generator(func: ast.AST) -> bool:
    from ..analysis.complexity import own_nodes
    return any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in own_nodes(func))
