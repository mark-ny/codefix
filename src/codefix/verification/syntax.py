"""Syntax and structural validation applied before anything is written."""
from __future__ import annotations

import ast
import warnings
from collections import Counter
from dataclasses import dataclass


@dataclass
class SyntaxResult:
    ok: bool
    parse_ok: bool
    error: str = ""
    line: int = 0
    column: int = 0


def check_syntax(text: str, filename: str = "<string>") -> SyntaxResult:
    """Parse and compile ``text``. ``ok`` means it compiles; ``parse_ok`` means it parses."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            tree = ast.parse(text, filename=filename)
        except SyntaxError as exc:
            return SyntaxResult(False, False, f"{exc.msg}", exc.lineno or 0, exc.offset or 0)
        except (ValueError, RecursionError, MemoryError) as exc:
            return SyntaxResult(False, False, str(exc) or exc.__class__.__name__)
        try:
            compile(tree, filename, "exec", dont_inherit=True)
        except SyntaxError as exc:
            return SyntaxResult(False, True, f"{exc.msg}", exc.lineno or 0, exc.offset or 0)
        except (ValueError, RecursionError, MemoryError, OverflowError) as exc:
            return SyntaxResult(False, True, str(exc) or exc.__class__.__name__)
    return SyntaxResult(True, True)


def defined_symbols(tree: ast.AST) -> Counter:
    """Counter of qualified function and class names, e.g. ``Foo.bar``."""
    counts: Counter = Counter()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{child.name}"
                counts[q] += 1
                walk(child, q + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return counts


def validate_change(original: str, new: str, filename: str = "<string>",
                    expected_removed: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Check that ``new`` is a valid replacement for ``original``."""
    before = check_syntax(original, filename)
    after = check_syntax(new, filename)
    if not after.parse_ok:
        return False, f"result does not parse: {after.error} (line {after.line})"
    if before.ok and not after.ok:
        return False, f"result does not compile: {after.error} (line {after.line})"
    if before.parse_ok:
        lost = Counter(defined_symbols(ast.parse(original))) - Counter(defined_symbols(ast.parse(new)))
        lost = Counter({k: v for k, v in lost.items() if k not in expected_removed})
        if lost:
            return False, "definitions disappeared: " + ", ".join(sorted(lost))
    return True, ""
