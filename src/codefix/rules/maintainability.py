"""Maintainability rules driven by function metrics and structural hashing."""
from __future__ import annotations

import ast

from ..knowledge.registry import rule
from ..models import Location, RuleSpec, Severity
from ._util import stmt_lists

R = Severity.REVIEW


def _metric_rule(spec_id, desc, message, explanation, attr, cfg_attr, label, confidence=0.9):
    spec = RuleSpec(spec_id, "maintainability", R, confidence, desc, message, explanation,
                    f"# a function with {label} above the configured limit", "# split it into smaller functions",
                    False, False, "Thresholds are configurable under [metrics] in codefix.toml.")

    @rule(spec)
    def check(ctx, spec):
        limit = getattr(ctx.config, cfg_attr)
        for (node, qual, _), m in zip(ctx.functions, ctx.metrics):
            value = getattr(m, attr)
            if value > limit:
                loc = None
                if attr == "nesting":
                    loc = Location(ctx.path, m.nesting_line, 1, m.nesting_line, 1)
                yield ctx.finding(spec, node, f"'{qual}' has {label} {value} (limit {limit})", function=qual,
                                  location=loc, metadata={"value": value, "limit": limit})

    return check


_metric_rule("FUNCTION_TOO_LONG", "Function has too many lines.", "Function is too long.",
             "Long functions are hard to read, test and reuse.", "lines", "max_function_lines", "length")
_metric_rule("HIGH_COMPLEXITY", "Cyclomatic complexity is too high.", "Cyclomatic complexity is too high.",
             "Each branch adds a path that needs testing; high values predict bugs.", "complexity", "max_complexity",
             "cyclomatic complexity")
_metric_rule("TOO_MANY_PARAMS", "Function takes too many parameters.", "Too many parameters.",
             "Long parameter lists are error-prone; group related values in an object.", "params", "max_params",
             "parameter count")
_metric_rule("TOO_MANY_RETURNS", "Function has too many return statements.", "Too many return statements.",
             "Many exits make control flow hard to follow.", "returns", "max_returns", "return count")
_metric_rule("TOO_MANY_CALLS", "Function makes too many calls.", "Too many calls in one function.",
             "A function orchestrating many calls usually does too much.", "calls", "max_calls", "call count", 0.8)
_metric_rule("EXCESSIVE_NESTING", "Control flow is nested too deeply.", "Excessive nesting.",
             "Deep nesting hurts readability; use early returns or helper functions.", "nesting", "max_nesting",
             "nesting depth")


def _strip_doc(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _count_stmts(nodes) -> int:
    return sum(isinstance(n, ast.stmt) for s in nodes for n in ast.walk(s))


@rule(RuleSpec("DUPLICATE_CODE", "maintainability", R, 0.8,
               "Identical functions or statement blocks appear more than once.", "Duplicated code.",
               "Structurally identical code (same AST, ignoring comments and formatting) should live in one place.",
               "def a(x):\n    ...\n\ndef b(x):\n    ...  # same body", "# extract the shared logic", False, False,
               "Only exact structural duplicates are detected; renamed variables are not matched."))
def duplicate_code(ctx, spec):
    min_stmts = ctx.config.min_duplicate_statements
    seen: dict[str, ast.AST] = {}
    dup_funcs: list[ast.AST] = []
    for node, qual, _ in ctx.functions:
        body = _strip_doc(node.body)
        if _count_stmts(body) < min_stmts + 1:
            continue
        key = ast.dump(ast.Module(body=body, type_ignores=[]))
        if key in seen:
            first = seen[key]
            dup_funcs.append(node)
            yield ctx.finding(spec, node, f"'{qual}' duplicates the body of '{first.name}' (line {first.lineno})",
                              function=qual, metadata={"duplicate_of_line": first.lineno})
        else:
            seen[key] = node
    inside_dup = {id(n) for f in dup_funcs for n in ast.walk(f)}
    windows: dict[str, ast.stmt] = {}
    covered: set[int] = set()
    for node in ast.walk(ctx.tree):
        for lst in stmt_lists(node):
            if id(node) in inside_dup:
                continue
            for i in range(len(lst) - min_stmts + 1):
                chunk = lst[i:i + min_stmts]
                if any(id(s) in covered for s in chunk) or (chunk[-1].end_lineno - chunk[0].lineno + 1) < 6:
                    continue
                key = "|".join(ast.dump(s) for s in chunk)
                if key in windows and windows[key] is not chunk[0]:
                    first = windows[key]
                    if first.lineno != chunk[0].lineno and not any(id(s) in covered for s in chunk):
                        covered.update(id(s) for s in chunk)
                        yield ctx.finding(spec, chunk[0],
                                          f"{min_stmts} statements duplicate the block at line {first.lineno}",
                                          metadata={"duplicate_of_line": first.lineno})
                else:
                    windows.setdefault(key, chunk[0])
