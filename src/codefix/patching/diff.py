"""Unified diff rendering."""
from __future__ import annotations

import difflib


def unified_diff(old: str, new: str, path: str) -> str:
    if old == new:
        return ""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
    out = []
    for line in lines:
        out.append(line if line.endswith(("\n", "\r")) else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def colorize(diff: str) -> str:
    colors = {"+": "\033[32m", "-": "\033[31m", "@": "\033[36m"}
    out = []
    for line in diff.splitlines(keepends=True):
        c = "\033[1m" if line.startswith(("+++", "---")) else colors.get(line[:1])
        out.append(f"{c}{line.rstrip()}\033[0m\n" if c else line)
    return "".join(out)
