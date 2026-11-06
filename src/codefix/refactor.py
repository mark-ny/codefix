"""Safe refactorings: rename, replace function, stub function.

Every operation returns a :class:`RefactorResult` holding the new text; nothing is written here.
The caller (application layer) verifies, diffs, backs up and writes.
"""
from __future__ import annotations

import ast
import io
import keyword
import re
import textwrap
import tokenize
from dataclasses import dataclass, field

from .analysis import cst_engine
from .analysis.complexity import collect_functions
from .analysis.symbols import build_symbols
from .models import ProposedEdit
from .patching.engine import EditConflict, PatchEngine
from .source import LineIndex, SourceFile
from .verification.syntax import defined_symbols


class RefactorError(Exception):
    pass


@dataclass
class RefactorResult:
    new_text: str
    description: str
    warnings: list[str] = field(default_factory=list)
    expected_removed: tuple[str, ...] = ()
    engine: str = "ast"


def _parse(source: SourceFile) -> ast.Module:
    try:
        return ast.parse(source.text)
    except SyntaxError as exc:
        raise RefactorError(f"cannot refactor a file with a syntax error (line {exc.lineno}): {exc.msg}") from exc


def _removed_names(old_text: str, new_text: str) -> tuple[str, ...]:
    try:
        lost = defined_symbols(ast.parse(old_text)) - defined_symbols(ast.parse(new_text))
    except SyntaxError:
        return ()
    return tuple(sorted(lost))


# --------------------------------------------------------------------------- rename
def rename_symbol(source: SourceFile, old: str, new: str, scope: str | None = None,
                  engine: str = "auto") -> RefactorResult:
    for n in (old, new):
        if not n.isidentifier() or keyword.iskeyword(n):
            raise RefactorError(f"'{n}' is not a valid identifier")
    if old == new:
        raise RefactorError("old and new names are identical")
    _parse(source)
    if engine not in ("auto", "ast", "libcst"):
        raise RefactorError("engine must be auto, ast or libcst")
    if engine == "libcst" and not cst_engine.available():
        raise RefactorError("LibCST is not installed (pip install libcst) - use --engine ast")
    fallback_note: list[str] = []
    if engine in ("auto", "libcst") and cst_engine.available():
        try:
            text, warns, where = cst_engine.rename(source.text, old, new, scope)
            warns += ["attribute accesses (obj.%s), keyword arguments and strings are never renamed; "
                      "other files are not updated" % old]
            return RefactorResult(text, f"rename '{old}' -> '{new}' in scope {where} (LibCST)", warns,
                                  _removed_names(source.text, text), "libcst")
        except cst_engine.CSTError as exc:
            if engine == "libcst":
                raise RefactorError(str(exc)) from exc
            fallback_note.append(f"LibCST engine declined ({exc}); used the standard-library AST engine")
    elif engine == "auto":
        fallback_note.append("LibCST is not installed: using the standard-library AST engine "
                             "(scope-aware, but less precise for f-strings before Python 3.12 and for exotic syntax)")
    res = _rename_ast(source, old, new, scope)
    res.warnings = fallback_note + res.warnings
    return res


def _rename_ast(source: SourceFile, old: str, new: str, scope_name: str | None) -> RefactorResult:
    tree = _parse(source)
    st = build_symbols(tree)
    data = source.text.encode("utf-8")
    idx = LineIndex(data)
    oldb = old.encode()
    cands = [s for s in st.scopes if old in s.bindings and s.kind != "typeparams"]
    if not cands:
        raise RefactorError(f"no binding named '{old}' found")

    def depth(s):
        d = 0
        while s.parent is not None:
            s, d = s.parent, d + 1
        return d

    def label(s):
        return repr(s.qualname or "<module>")

    warnings: list[str] = []
    if scope_name is not None:
        want = "" if scope_name in ("", "<module>") else scope_name
        picked = [s for s in cands if s.qualname == want]
        if not picked:
            raise RefactorError(f"'{old}' is not bound in scope {scope_name!r}; candidates: " + ", ".join(map(label, cands)))
        target = picked[0]
    else:
        best = min(depth(s) for s in cands)
        top = [s for s in cands if depth(s) == best]
        if len(top) > 1:
            raise RefactorError(f"'{old}' is bound in several scopes ({', '.join(map(label, top))}); choose one with --scope")
        target = top[0]
        others = [s for s in cands if s is not target]
        if others:
            warnings.append(f"'{old}' is also bound in {', '.join(map(label, others))}; those scopes were left untouched")

    if new in target.bindings:
        raise RefactorError(f"'{new}' already exists in scope {label(target)}")
    positions: set[tuple[int, int]] = set()

    def add(start: int, end: int) -> None:
        if data[start:end] == oldb:
            positions.add((start, end))
        else:
            warnings.append(f"skipped a position at line {idx.line_of(start)}: source text did not match (f-string?)")

    for ref in st.refs:
        if ref.name != old or ref.target is not target or ref.in_string or not isinstance(ref.node, ast.Name):
            continue
        # collision check: any scope between the reference and the target already binds `new`
        s = ref.scope
        while s is not None:
            if new in s.bindings and (s.kind != "class" or s is ref.scope):
                raise RefactorError(f"renaming would collide with an existing '{new}' in scope {label(s)}")
            if s is target:
                break
            s = s.parent
        add(idx.offset(ref.node.lineno, ref.node.col_offset), idx.offset(ref.node.end_lineno, ref.node.end_col_offset))

    name_re = re.escape(old).encode()
    for b in target.bindings[old]:
        n = b.node
        if b.kind in ("variable", "global"):
            continue                                   # covered by the references above
        if b.kind == "param":
            add(idx.offset(n.lineno, n.col_offset), idx.offset(n.lineno, n.col_offset) + len(oldb))
        elif b.kind in ("function", "class"):
            m = re.compile(rb"(?:async\s+)?(?:def|class)\s+(" + name_re + rb")\b").match(data, idx.offset(n.lineno, n.col_offset))
            if m:
                add(*m.span(1))
        elif b.kind == "except":
            lo = idx.offset(n.type.end_lineno, n.type.end_col_offset) if n.type else idx.offset(n.lineno, n.col_offset)
            m = re.compile(rb"\bas\s+(" + name_re + rb")\b").search(data, lo)
            if m:
                add(*m.span(1))
        elif b.kind == "match":
            s0 = idx.offset(n.lineno, n.col_offset)
            m = re.compile(rb"(?:\*\*?\s*)?(" + name_re + rb")\b|\bas\s+(" + name_re + rb")\b").search(data, s0)
            if m:
                add(*(m.span(1) if m.group(1) else m.span(2)))
        elif b.kind == "import":
            if n.asname != old:
                raise RefactorError(f"'{old}' is bound by an import without an alias; renaming would break the import")
            lo, hi = idx.offset(n.lineno, n.col_offset), idx.offset(n.end_lineno, n.end_col_offset)
            m = re.compile(rb"\bas\s+(" + name_re + rb")\b").search(data, lo, hi)
            if m:
                add(*m.span(1))
        else:
            raise RefactorError(f"unsupported binding kind '{b.kind}' for '{old}'")

    for decl, sc in st.global_decls:
        if old not in decl.names:
            continue
        if isinstance(decl, ast.Global):
            applies = target.kind == "module"
        else:
            s = sc.parent
            while s is not None and old not in s.bindings:
                s = s.parent
            applies = s is target
        if applies:
            lo, hi = idx.offset(decl.lineno, decl.col_offset), idx.offset(decl.end_lineno, decl.end_col_offset)
            kw = 6 if isinstance(decl, ast.Global) else 8
            for m in re.finditer(rb"\b" + name_re + rb"\b", data[lo + kw:hi]):
                add(lo + kw + m.start(), lo + kw + m.end())

    attrs = sum(isinstance(n, ast.Attribute) and n.attr == old for n in ast.walk(tree))
    kwargs = sum(isinstance(n, ast.keyword) and n.arg == old for n in ast.walk(tree))
    if attrs:
        warnings.append(f"{attrs} attribute access(es) named '{old}' were not renamed (the AST engine cannot resolve receivers)")
    if kwargs:
        warnings.append(f"{kwargs} keyword argument(s) named '{old}' were not renamed")
    if old in st.exports:
        warnings.append(f"'{old}' appears in __all__ as a string; update it manually")
    if not positions:
        raise RefactorError(f"nothing to rename for '{old}' in scope {label(target)}")
    edits = [ProposedEdit("RENAME", str(source.path), s, e, new, f"{old} -> {new}", expected=old) for s, e in sorted(positions)]
    try:
        text = PatchEngine.apply(source, edits)
    except EditConflict as exc:
        raise RefactorError(str(exc)) from exc
    warnings.append("other files that import or call this name are not updated")
    return RefactorResult(text, f"rename '{old}' -> '{new}' in scope {label(target)} ({len(positions)} occurrence(s))",
                          warnings, _removed_names(source.text, text), "ast")


# --------------------------------------------------------------- function helpers
def _find_function(tree: ast.Module, target: str):
    funcs = collect_functions(tree)
    exact = [(n, q) for n, q, _ in funcs if q == target]
    found = exact or [(n, q) for n, q, _ in funcs if q.split(".")[-1] == target]
    if not found:
        raise RefactorError(f"function '{target}' not found")
    if len(found) > 1:
        raise RefactorError(f"'{target}' is ambiguous ({', '.join(q for _, q in found)}); use a qualified name like Class.method")
    return found[0]


def _continuation_lines(text: str) -> set[int]:
    """1-based line numbers that begin inside a multi-line string (must not be re-indented)."""
    lines: set[int] = set()
    fstart: list[int] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            name = tokenize.tok_name.get(tok.type, "")
            if name == "STRING" and tok.start[0] != tok.end[0]:
                lines.update(range(tok.start[0] + 1, tok.end[0] + 1))
            elif name == "FSTRING_START":
                fstart.append(tok.start[0])
            elif name == "FSTRING_END" and fstart:
                s = fstart.pop()
                lines.update(range(s + 1, tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return lines


def _reindent(text: str, indent: str) -> str:
    text = text.replace("\r\n", "\n").strip("\n")
    first = next((l for l in text.split("\n") if l.strip()), "")
    base = first[:len(first) - len(first.lstrip())]
    skip = _continuation_lines(text)
    out = []
    for i, line in enumerate(text.split("\n"), 1):
        if i in skip:
            out.append(line)
            continue
        if base and line.startswith(base):
            line = line[len(base):]
        out.append(indent + line if line.strip() else "")
    return "\n".join(out)


def _line_indent(idx: LineIndex, node: ast.AST) -> str:
    prefix = idx.line_text(node.lineno)[:node.col_offset]
    if prefix.strip():
        raise RefactorError("the function shares its line with other code; refusing to rewrite it")
    return prefix.decode("utf-8")


def replace_function(source: SourceFile, target: str, replacement: str) -> RefactorResult:
    tree = _parse(source)
    node, qual = _find_function(tree, target)
    simple = qual.split(".")[-1]
    try:
        rep_tree = ast.parse(textwrap.dedent(replacement.replace("\r\n", "\n")).strip("\n") + "\n")
    except SyntaxError as exc:
        raise RefactorError(f"replacement does not parse: {exc.msg} (line {exc.lineno})") from exc
    if len(rep_tree.body) != 1 or not isinstance(rep_tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise RefactorError("replacement must contain exactly one function definition and nothing else")
    rep = rep_tree.body[0]
    if rep.name != simple:
        raise RefactorError(f"replacement defines '{rep.name}' but the target is '{simple}'")
    data = source.text.encode("utf-8")
    idx = LineIndex(data)
    indent = _line_indent(idx, node)
    start = idx.offset(node.lineno, node.col_offset)
    kept_decorators = not rep.decorator_list
    if rep.decorator_list and node.decorator_list:
        d0 = node.decorator_list[0]
        start = data.rfind(b"@", idx.line_start(d0.lineno), idx.offset(d0.lineno, d0.col_offset))
        if start < 0:
            raise RefactorError("cannot locate the original decorator")
    end = idx.offset(node.end_lineno, node.end_col_offset)
    body = _reindent(replacement, indent)
    if body.startswith(indent):
        body = body[len(indent):]
    edit = ProposedEdit("REPLACE", str(source.path), start, end, body, f"replace {qual}", expected=data[start:end].decode("utf-8"))
    text = PatchEngine.apply(source, [edit])
    removed = tuple(n for n in _removed_names(source.text, text) if n.startswith(qual + "."))
    new_tree = ast.parse(text)
    if defined_symbols(new_tree)[qual] != 1:
        raise RefactorError(f"after the change '{qual}' is defined {defined_symbols(new_tree)[qual]} times (expected exactly 1)")
    warns = []
    if kept_decorators and node.decorator_list:
        warns.append(f"kept the original decorator(s) on '{qual}'")
    if removed:
        warns.append("nested definitions removed: " + ", ".join(removed))
    return RefactorResult(text, f"replace function '{qual}'", warns, removed)


def stub_function(source: SourceFile, target: str) -> RefactorResult:
    """Replace a function body with ``raise NotImplementedError`` (docstring kept), or add a new stub."""
    tree = _parse(source)
    data = source.text.encode("utf-8")
    idx = LineIndex(data)
    try:
        node, qual = _find_function(tree, target)
    except RefactorError:
        if "." in target:
            raise
        sep = "" if source.text.endswith("\n") or not source.text else "\n"
        add = f"{sep}\n\ndef {target}(*args, **kwargs):\n    raise NotImplementedError({target!r})\n"
        edit = ProposedEdit("STUB", str(source.path), len(data), len(data), add, f"add stub {target}")
        return RefactorResult(PatchEngine.apply(source, [edit]), f"append new stub '{target}'", [])
    body = node.body
    has_doc = (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
               and isinstance(body[0].value.value, str))
    rest = body[1:] if has_doc else body
    msg = f"raise NotImplementedError({qual!r})"
    if not rest:
        raise RefactorError(f"'{qual}' is already a stub (no body beyond its docstring)")
    s = idx.offset(rest[0].lineno, rest[0].col_offset)
    e = idx.offset(node.end_lineno, node.end_col_offset)
    edit = ProposedEdit("STUB", str(source.path), s, e, msg, f"stub {qual}", expected=data[s:e].decode("utf-8"))
    text = PatchEngine.apply(source, [edit])
    removed = tuple(n for n in _removed_names(source.text, text) if n.startswith(qual + "."))
    return RefactorResult(text, f"replace the body of '{qual}' with a NotImplementedError stub",
                          ["the previous body is discarded (a backup is kept when --write is used)"], removed)
