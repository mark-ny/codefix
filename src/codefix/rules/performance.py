"""Performance rules."""
from __future__ import annotations

import ast

from ..analysis.complexity import own_nodes
from ..knowledge.registry import rule
from ..models import RuleSpec, Severity


def _is_str_expr(n: ast.AST) -> bool:
    if isinstance(n, ast.JoinedStr):
        return True
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return True
    return isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("str", "repr", "format")


@rule(RuleSpec("STRING_CONCAT_LOOP", "performance", Severity.REVIEW, 0.75,
               "Strings are built with `+=` inside a loop.", "String concatenation inside a loop.",
               "Repeated concatenation copies the string each time (quadratic in the worst case). "
               "Collect parts in a list and use ''.join().",
               "out = ''\nfor w in words:\n    out += w\n", "out = ''.join(words)\n", False, False,
               "CPython often optimises `s += x`, so small loops are fine. Detection is name/literal based."))
def string_concat_loop(ctx, spec):
    scopes: list[ast.AST] = [ctx.tree, *[f for f, _, _ in ctx.functions]]
    reported: set[int] = set()
    for scope in scopes:
        nodes = list(ast.walk(ctx.tree)) if scope is ctx.tree else [scope, *own_nodes(scope)]
        if scope is ctx.tree:
            nodes = [scope, *own_nodes_module(ctx.tree)]
        str_names = {t.id for n in nodes if isinstance(n, ast.Assign) and _is_str_expr(n.value)
                     for t in n.targets if isinstance(t, ast.Name)}
        for loop in (n for n in nodes if isinstance(n, (ast.For, ast.AsyncFor, ast.While))):
            for n in ast.walk(loop):
                if id(n) in reported:
                    continue
                name = None
                if isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add) and isinstance(n.target, ast.Name):
                    if n.target.id in str_names or _is_str_expr(n.value):
                        name = n.target.id
                elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                        and isinstance(n.value, ast.BinOp) and isinstance(n.value.op, ast.Add):
                    t = n.targets[0].id
                    left = n.value.left
                    if isinstance(left, ast.Name) and left.id == t and (t in str_names or _is_str_expr(n.value.right)):
                        name = t
                if name:
                    reported.add(id(n))
                    yield ctx.finding(spec, n, f"String '{name}' is built with += inside a loop; use ''.join()",
                                      metadata={"name": name})


def own_nodes_module(tree: ast.Module):
    stack = list(tree.body)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))
