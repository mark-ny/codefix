"""Per-function complexity and size metrics."""
from __future__ import annotations

import ast

from ..models import FunctionMetrics

BLOCK_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
if hasattr(ast, "TryStar"):
    BLOCK_NODES += (ast.TryStar,)
if hasattr(ast, "Match"):
    BLOCK_NODES += (ast.Match,)
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def own_nodes(func: ast.AST):
    """Yield nodes inside ``func`` without descending into nested functions/classes/lambdas."""
    stack = list(ast.iter_child_nodes(func))
    # decorators/defaults/annotations belong to the enclosing scope, skip them
    if isinstance(func, FUNC_NODES):
        stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, SCOPE_NODES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def cyclomatic(func: ast.AST) -> int:
    n = 1
    for node in own_nodes(func):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert)):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            n += 1 + len(node.ifs)
        elif hasattr(ast, "match_case") and isinstance(node, ast.match_case):
            n += 1
    return n


def _child_blocks(node: ast.AST) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
        blocks += [node.body, node.orelse, node.finalbody] + [h.body for h in node.handlers]
    elif hasattr(ast, "Match") and isinstance(node, ast.Match):
        blocks += [c.body for c in node.cases]
    else:
        for attr in ("body", "orelse"):
            v = getattr(node, attr, None)
            if isinstance(v, list):
                blocks.append(v)
    return blocks


def max_nesting(body: list[ast.stmt], depth: int = 0) -> tuple[int, int]:
    """Return ``(deepest block level, line of that block)`` for statements at ``depth``."""
    best, line = depth, 0
    for stmt in body:
        if not isinstance(stmt, BLOCK_NODES):
            continue
        level = depth + 1
        if level > best:
            best, line = level, stmt.lineno
        for block in _child_blocks(stmt):
            base = level
            # an `elif` is not extra nesting
            if isinstance(stmt, ast.If) and block is stmt.orelse and len(block) == 1 and isinstance(block[0], ast.If):
                base = level - 1
            sub, sub_line = max_nesting(block, base)
            if sub > best:
                best, line = sub, sub_line
    return best, line


def function_metrics(func: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str, is_method: bool) -> FunctionMetrics:
    a = func.args
    params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])]
    count = len(params)
    if is_method and params and params[0].arg in ("self", "cls"):
        count -= 1
    nodes = list(own_nodes(func))
    nest, nest_line = max_nesting(func.body)
    return FunctionMetrics(
        name=qualname, line=func.lineno, end_line=func.end_lineno or func.lineno,
        lines=(func.end_lineno or func.lineno) - func.lineno + 1, complexity=cyclomatic(func),
        params=count, returns=sum(isinstance(n, ast.Return) for n in nodes),
        calls=sum(isinstance(n, ast.Call) for n in nodes), nesting=nest, nesting_line=nest_line or func.lineno,
        is_method=is_method)


def collect_functions(tree: ast.AST) -> list[tuple[ast.AST, str, bool]]:
    """All functions as ``(node, qualified name, is_method)`` in source order."""
    out: list[tuple[ast.AST, str, bool]] = []

    def walk(node: ast.AST, prefix: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNC_NODES):
                q = prefix + child.name
                out.append((child, q, in_class))
                walk(child, q + ".", False)
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".", True)
            else:
                walk(child, prefix, in_class)

    walk(tree, "", False)
    return out


def module_metrics(tree: ast.AST) -> list[FunctionMetrics]:
    return [function_metrics(f, q, m) for f, q, m in collect_functions(tree)]  # type: ignore[arg-type]
