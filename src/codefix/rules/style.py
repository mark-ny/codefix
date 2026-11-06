"""Style rules."""
from __future__ import annotations

import ast

from ..knowledge.registry import rule
from ..models import RuleSpec, Severity


@rule(RuleSpec("MISSING_DOCSTRING", "style", Severity.SAFE, 0.9,
               "A public function has no docstring.", "Public function is missing a docstring.",
               "Public functions and methods form an API; a docstring states the contract.",
               "def area(w, h):\n    return w * h\n", 'def area(w, h):\n    """Return the area."""\n    return w * h\n',
               False, False,
               "Private (`_x`) and dunder names, nested functions, overloads, property setters and test files are skipped."))
def missing_docstring(ctx, spec):
    if ctx.is_test_file:
        return
    for node, qual, is_method in ctx.functions:
        name = node.name
        parent = ctx.parents.get(node)
        top_level = isinstance(parent, ast.Module)
        in_public_class = isinstance(parent, ast.ClassDef) and isinstance(ctx.parents.get(parent), ast.Module) \
            and not parent.name.startswith("_")
        if not (top_level or in_public_class) or name.startswith("_") or name.startswith("test_"):
            continue
        if any((isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter", "getter", "overload"))
               or (isinstance(d, ast.Name) and d.id == "overload") for d in node.decorator_list):
            continue
        if ast.get_docstring(node) is None:
            yield ctx.finding(spec, node, f"Public {'method' if is_method else 'function'} '{qual}' has no docstring",
                              function=qual)
