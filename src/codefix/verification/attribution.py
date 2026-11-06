"""Turn pytest output into structured failures with likely culprits."""
from __future__ import annotations

import ast
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SUMMARY_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)")
HEADER_RE = re.compile(r"^_{2,} (.+?) _{2,}$")
FRAME_RE = re.compile(r"^(\S+?\.py):(\d+): in (.+)$")
EXC_RE = re.compile(r"^(\S+?\.py):(\d+): (\S.*)$")
FAILED_RE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


@dataclass
class Frame:
    file: str
    line: int
    function: str


@dataclass
class Failure:
    test: str
    file: str = ""
    line: int = 0
    message: str = ""
    frames: list[Frame] = field(default_factory=list)
    likely_functions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_test_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def enclosing_function(path: Path, line: int) -> str | None:
    """Qualified name of the function containing ``line`` in ``path`` (None if unknown)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    best: tuple[int, str] | None = None

    def walk(node: ast.AST, prefix: str) -> None:
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + child.name
                if child.lineno <= line <= (child.end_lineno or child.lineno) and not isinstance(child, ast.ClassDef):
                    span = (child.end_lineno or child.lineno) - child.lineno
                    if best is None or span <= best[0]:
                        best = (span, q)
                walk(child, q + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return best[1] if best else None


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules", ".tox", ".codefix", "site-packages"}


def infer_from_test_line(root: Path, test_file: str, line: int, limit: int = 500) -> list[dict[str, Any]]:
    """When a test fails in its own body, guess the code under test from the calls on the failing line."""
    try:
        tree = ast.parse((root / test_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return []
    stmt = None
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and n.lineno <= line <= (n.end_lineno or n.lineno):
            if stmt is None or (n.end_lineno - n.lineno) <= (stmt.end_lineno - stmt.lineno):
                stmt = n
    if stmt is None:
        return []
    names = {c.func.id if isinstance(c.func, ast.Name) else c.func.attr
             for c in ast.walk(stmt) if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute))}
    found: list[dict[str, Any]] = []
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if not name.endswith(".py") or is_test_file(name):
                continue
            seen += 1
            if seen > limit:
                return found
            fp = Path(dirpath) / name
            try:
                t = ast.parse(fp.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                continue
            for q, node in _qualified_functions(t):
                if node.name in names:
                    found.append({"file": fp.relative_to(root).as_posix(), "line": node.lineno,
                                  "function": q, "inferred": True})
    return found


def _qualified_functions(tree: ast.AST):
    def walk(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + child.name, child
                yield from walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + child.name + ".")
            else:
                yield from walk(child, prefix)
    yield from walk(tree, "")


def parse_summary(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in reversed(text.splitlines()):
        found = SUMMARY_RE.findall(line)
        if found and (" in " in line or line.startswith("=")):
            for n, word in found:
                key = "errors" if word.startswith("error") else "warnings" if word.startswith("warning") else word
                counts[key] = int(n)
            break
    return counts


def parse_pytest_output(text: str, root: str | Path = ".") -> list[Failure]:
    root = Path(root)
    lines = text.splitlines()
    summary: dict[str, tuple[str, str]] = {}
    for ln in lines:
        m = FAILED_RE.match(ln.strip())
        if m:
            summary[m.group(2)] = (m.group(1), m.group(3) or "")
    failures: list[Failure] = []
    cur: Failure | None = None
    for ln in lines:
        h = HEADER_RE.match(ln)
        if h:
            cur = Failure(test=h.group(1).replace("ERROR collecting ", ""))
            failures.append(cur)
            continue
        if cur is None:
            continue
        if ln.startswith("=") and "short test summary" in ln:
            cur = None
            continue
        f = FRAME_RE.match(ln)
        if f:
            cur.frames.append(Frame(f.group(1), int(f.group(2)), f.group(3).strip()))
            continue
        if ln.startswith("E") and not cur.message and ln[1:2].isspace():
            cur.message = ln[1:].strip()
            continue
        e = EXC_RE.match(ln)
        if e and not ln.startswith(" ") and cur.frames is not None:
            cur.frames.append(Frame(e.group(1), int(e.group(2)), ""))
    # attach node ids and messages from the short summary
    for fail in failures:
        short = fail.test.split(".")[-1].split("[")[0]
        for nodeid, (_, msg) in summary.items():
            tail = nodeid.split("::")[-1].split("[")[0]
            if tail == short or nodeid.endswith(fail.test) or nodeid == fail.test:
                fail.test = nodeid
                fail.message = fail.message or msg
                break
    known = {f.test for f in failures}
    for nodeid, (_, msg) in summary.items():
        if nodeid not in known:
            failures.append(Failure(test=nodeid, message=msg))
    for fail in failures:
        in_tests = [fr for fr in fail.frames if is_test_file(fr.file)]
        prefix = fail.test.split("::")[0]
        if prefix.endswith(".py"):
            fail.file = prefix
        if in_tests:
            fail.file = fail.file or in_tests[0].file
            fail.line = in_tests[0].line
        elif fail.frames:
            fail.file, fail.line = fail.frames[0].file, fail.frames[0].line
        for fr in reversed([fr for fr in fail.frames if not is_test_file(fr.file)]):
            func = fr.function or enclosing_function(root / fr.file, fr.line) or ""
            qual = enclosing_function(root / fr.file, fr.line) or func
            fail.likely_functions.append({"file": fr.file, "line": fr.line, "function": qual or func})
        if not fail.likely_functions and fail.file and fail.line and is_test_file(fail.file):
            fail.likely_functions = infer_from_test_line(root, fail.file, fail.line)
    return failures
