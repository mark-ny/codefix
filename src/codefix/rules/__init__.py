"""Built-in rules. Importing this package registers every rule module."""
from __future__ import annotations


def load_builtin_rules():
    from ..knowledge.registry import BUILTIN_RULES
    from . import maintainability, performance, python, security, style  # noqa: F401

    return [*BUILTIN_RULES, *security.SECURITY_RULES]
