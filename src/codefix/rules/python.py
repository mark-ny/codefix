"""General Python correctness rules."""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

from ..analysis.complexity import own_nodes
from ..knowledge.registry import rule
from ..models import RuleSpec, Severity
from ._util import find_pattern, is_generator, is_none, line_indent, single_line, stmt_lists

S, R, K = Severity.SAFE, Severity.REVIEW, Severity.RISKY


@rule(RuleSpec("SYNTAX_ERROR", "correctness", Severity.BLOCKED, 1.0,
               "The file cannot be parsed.", "Syntax error in source file.",
               "Python could not parse the file. All other analysis is skipped until it is fixed.",
               "def f(:\n    pass\n", "def f():\n    pass\n", False, False,
               "Never a false positive unless the file targets a newer Python than the interpreter running CodeFix."))
def syntax_error(ctx, spec):
    return []   # reported by the engine itself when parsing fails


@rule(RuleSpec("UNDEFINED_NAME", "correctness", R, 0.9,
               "A name is used but never defined.", "Name is not defined in any visible scope.",
               "Using a name that no scope defines raises NameError at runtime.",
               "def f():\n    return missing_name\n", "def f(missing_name):\n    return missing_name\n", False, False,
               "Skipped in files with `from x import *`. Lowered confidence when exec/eval/globals() are used."))
def undefined_name(ctx, spec):
    st = ctx.symbols
    if st.star_import:
        return
    for ref in st.undefined:
        if ref.in_string or ref.name_error_guard:
            continue
        yield ctx.finding(spec, ref.node, f"Undefined name '{ref.name}'", metadata={"name": ref.name})


def _alias_text(a: ast.alias) -> str:
    return f"{a.name} as {a.asname}" if a.asname else a.name


def _import_edits(ctx, spec, stmt, unused_aliases):
    start, end = ctx.span(stmt)
    if line_indent(ctx, stmt.lineno, stmt.col_offset) is None:
        return []
    if ctx.index.line_text(stmt.end_lineno)[stmt.end_col_offset:].strip():
        return []                       # trailing comment / `;` : leave it alone (respects `# noqa`)
    if b"#" in ctx.data[start:end]:
        return []
    parent = ctx.parents[stmt]
    block = next((lst for lst in stmt_lists(parent) if stmt in lst), None)
    if block is None:
        return []
    remaining = [a for a in stmt.names if id(a) not in unused_aliases]
    if not remaining:
        if len(block) == 1:
            return [ctx.edit(spec, start, end, "pass", "replace the only statement with pass")]
        ls = ctx.index.line_start(stmt.lineno)
        le = ctx.index.line_start(stmt.end_lineno + 1)
        return [ctx.edit(spec, ls, le, "", "remove unused import")]
    names = ", ".join(_alias_text(a) for a in remaining)
    if isinstance(stmt, ast.Import):
        new = f"import {names}"
    else:
        new = f"from {'.' * stmt.level}{stmt.module or ''} import {names}"
    return [ctx.edit(spec, start, end, new, "drop unused names from import")]


@rule(RuleSpec("UNUSED_IMPORT", "correctness", R, 0.9,
               "An imported name is never used.", "Imported name is never used.",
               "Unused imports slow start-up, hide real dependencies and confuse readers. Removal is "
               "'review' level because an import can have side effects or be a deliberate re-export.",
               "import os\nimport sys\n\nprint(sys.argv)\n", "import sys\n\nprint(sys.argv)\n", True, False,
               "Skipped in __init__.py, for names in __all__, `__future__` imports and imports guarded by "
               "try/except ImportError. Lines with trailing comments (e.g. `# noqa`) are reported but not fixed."))
def unused_import(ctx, spec):
    if Path(ctx.path).name == "__init__.py":
        return
    by_stmt: dict[ast.AST, list] = {}
    for b in ctx.symbols.all_bindings():
        if b.kind == "import" and not b.used and not b.guarded:
            by_stmt.setdefault(b.stmt, []).append(b)
    for stmt, bindings in sorted(by_stmt.items(), key=lambda kv: (kv[0].lineno, kv[0].col_offset)):
        names = [b.name for b in bindings]
        unused = {id(b.node) for b in bindings}
        label = ", ".join(f"'{n}'" for n in names)
        yield ctx.finding(spec, stmt, f"{label} imported but unused",
                          edits=_import_edits(ctx, spec, stmt, unused), metadata={"names": names})


_SHADOW_SKIP = {"copyright", "credits", "license", "exit", "quit", "help"}
SHADOWABLE = {n for n in dir(builtins) if not n.startswith("_") and n not in _SHADOW_SKIP}


@rule(RuleSpec("SHADOW_BUILTIN", "correctness", R, 0.8,
               "A variable, parameter or function hides a built-in.", "Name shadows a built-in.",
               "Rebinding names such as `list`, `id` or `type` makes the built-in unreachable in that scope "
               "and causes confusing errors later.",
               "def f(list):\n    return len(list)\n", "def f(items):\n    return len(items)\n", False, False,
               "Class attributes (e.g. ORM fields named `id`) are not reported."))
def shadow_builtin(ctx, spec):
    seen = set()
    for b in ctx.symbols.all_bindings():
        if b.kind not in ("function", "class", "param", "variable", "except", "match"):
            continue
        if b.scope.kind == "class" or b.name not in SHADOWABLE or (id(b.scope), b.name) in seen:
            continue
        seen.add((id(b.scope), b.name))
        yield ctx.finding(spec, b.node, f"'{b.name}' shadows a built-in", metadata={"name": b.name})


_TERMINAL = (ast.Return, ast.Raise, ast.Break, ast.Continue)


@rule(RuleSpec("UNREACHABLE_CODE", "correctness", R, 0.95,
               "Statements follow return/raise/break/continue.", "Unreachable code.",
               "Code after an unconditional return, raise, break or continue never runs.",
               "def f():\n    return 1\n    print('never')\n", "def f():\n    return 1\n", False, False,
               "Only straight-line statement lists are analysed; `sys.exit()` is not treated as terminal."))
def unreachable_code(ctx, spec):
    for node in ast.walk(ctx.tree):
        for lst in stmt_lists(node):
            for i, stmt in enumerate(lst[:-1]):
                if isinstance(stmt, _TERMINAL):
                    kind = type(stmt).__name__.lower()
                    yield ctx.finding(spec, lst[i + 1], f"Unreachable code after '{kind}'")
                    break


MUTABLE_CALLS = {"list", "dict", "set", "bytearray", "defaultdict", "deque", "OrderedDict", "Counter"}


def _is_mutable_default(n: ast.AST) -> bool:
    if isinstance(n, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(n, ast.Call):
        f = n.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        return name in MUTABLE_CALLS
    return False


def _default_pairs(args: ast.arguments):
    pos = [*args.posonlyargs, *args.args]
    pairs = list(zip(pos[len(pos) - len(args.defaults):], args.defaults))
    pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
    return pairs


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _mutable_fix(ctx, spec, func, pairs):
    body0 = func.body[0]
    if all(_is_docstring(s) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                and s.value.value is Ellipsis) for s in func.body):
        if not _is_docstring(body0):
            return []                   # stub / overload: nothing to insert into
    indent = line_indent(ctx, body0.lineno, body0.col_offset)
    if indent is None:
        return []
    def_indent = line_indent(ctx, func.lineno, func.col_offset) or ""
    unit = indent[len(def_indent):] if indent.startswith(def_indent) and len(indent) > len(def_indent) else "    "
    edits, block = [], ""
    for arg, default in pairs:
        src = ctx.segment(default)
        if not single_line(src):
            return []
        s, e = ctx.span(default)
        edits.append(ctx.edit(spec, s, e, "None", f"default of '{arg.arg}' becomes None"))
        block += f"{indent}if {arg.arg} is None:\n{indent}{unit}{arg.arg} = {src}\n"
    if _is_docstring(body0):
        rest = ctx.index.line_text(body0.end_lineno)[body0.end_col_offset:].strip()
        if rest and not rest.startswith(b"#"):
            return []
        at = ctx.index.line_start(body0.end_lineno + 1)
        if at >= len(ctx.data) and not ctx.data.endswith(b"\n"):
            return []
    else:
        at = ctx.index.line_start(body0.lineno)
    edits.append(ctx.edit(spec, at, at, block, "create the mutable default inside the function"))
    return edits


@rule(RuleSpec("MUTABLE_DEFAULT", "correctness", K, 0.9,
               "A mutable object is used as a default argument.", "Mutable default argument.",
               "Default values are evaluated once, so a list/dict/set default is shared by every call. The "
               "fix replaces the default with None and creates the object inside the function. It is 'risky' "
               "because callers may rely on the shared state, so tests are required.",
               "def add(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n",
               "def add(item, bucket=None):\n    if bucket is None:\n        bucket = []\n    bucket.append(item)\n    return bucket\n",
               True, True,
               "Intentional caching via a mutable default is rare but real; add `# codefix: ignore[MUTABLE_DEFAULT]`. "
               "Type annotations are left untouched (consider `list | None`)."))
def mutable_default(ctx, spec):
    for node in ast.walk(ctx.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        pairs = [(a, d) for a, d in _default_pairs(node.args) if _is_mutable_default(d)]
        if not pairs:
            continue
        edits = [] if isinstance(node, ast.Lambda) else _mutable_fix(ctx, spec, node, pairs)
        names = [a.arg for a, _ in pairs]
        yield ctx.finding(spec, pairs[0][1], "Mutable default argument for " + ", ".join(f"'{n}'" for n in names),
                          edits=edits, metadata={"parameters": names})


def _ends_with_bare_raise(handler: ast.ExceptHandler) -> bool:
    last = handler.body[-1]
    return isinstance(last, ast.Raise) and last.exc is None


@rule(RuleSpec("BARE_EXCEPT", "correctness", R, 0.9,
               "`except:` catches everything, including KeyboardInterrupt and SystemExit.", "Bare 'except:' block.",
               "A bare except hides interrupts and genuine bugs. The fix narrows it to `except Exception:`, which "
               "still changes behaviour for BaseException subclasses, hence 'review'.",
               "try:\n    work()\nexcept:\n    handle()\n", "try:\n    work()\nexcept Exception:\n    handle()\n", True, False,
               "Handlers that end in a bare `raise` (cleanup then re-raise) are not reported."))
def bare_except(ctx, spec):
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None and not _ends_with_bare_raise(node):
            start, _ = ctx.span(node)
            edits = []
            if ctx.data[start:start + 6] == b"except":
                edits = [ctx.edit(spec, start + 6, start + 6, " Exception", "catch Exception instead of everything")]
            yield ctx.finding(spec, node, edits=edits)


@rule(RuleSpec("EXCEPT_PASS", "correctness", R, 0.9,
               "`except Exception: pass` silently swallows errors.", "Exception swallowed with 'pass'.",
               "Silencing every exception makes failures invisible. Log, narrow the exception, or re-raise.",
               "try:\n    work()\nexcept Exception:\n    pass\n",
               "try:\n    work()\nexcept Exception:\n    logger.exception('work failed')\n", False, False,
               "`contextlib.suppress(SpecificError)` is the explicit alternative."))
def except_pass(ctx, spec):
    for node in ast.walk(ctx.tree):
        if (isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name)
                and node.type.id in ("Exception", "BaseException")
                and len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
            yield ctx.finding(spec, node, f"'except {node.type.id}: pass' swallows every error")


def _operands(node: ast.Compare):
    return [node.left, *node.comparators]


@rule(RuleSpec("COMPARE_NONE", "correctness", S, 0.99,
               "Comparison to None with == or !=.", "Use `is None` instead of `== None`.",
               "None is a singleton; identity comparison is faster and not fooled by custom __eq__.",
               "if value == None:", "if value is None:", True, False,
               "Objects overriding __eq__ to compare equal to None (e.g. numpy arrays compare element-wise) "
               "change meaning; that is almost never intended."))
def compare_none(ctx, spec):
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Compare):
            continue
        ops = _operands(node)
        for i, op in enumerate(node.ops):
            if isinstance(op, (ast.Eq, ast.NotEq)) and (is_none(ops[i]) or is_none(ops[i + 1])):
                eq = isinstance(op, ast.Eq)
                lo, hi = ctx.span(ops[i])[1], ctx.span(ops[i + 1])[0]
                pos = find_pattern(ctx, lo, hi, rb"==" if eq else rb"!=")
                edits = [ctx.edit(spec, pos[0], pos[1], "is" if eq else "is not")] if pos else []
                yield ctx.finding(spec, node, "Use `is None` instead of `== None`" if eq
                                  else "Use `is not None` instead of `!= None`", edits=edits)


def _is_literal(n: ast.AST) -> bool:
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        n = n.operand
    if isinstance(n, ast.JoinedStr):
        return True
    return isinstance(n, ast.Constant) and isinstance(n.value, (int, float, complex, str, bytes)) \
        and not isinstance(n.value, bool)


@rule(RuleSpec("COMPARE_LITERAL", "correctness", R, 0.9,
               "Identity comparison (`is`) with a literal.", "Use == / != to compare with a literal.",
               "`is` compares object identity, which is unreliable for numbers and strings (and a SyntaxWarning).",
               "if status is 200:", "if status == 200:", True, False,
               "`is` with None/True/False/Ellipsis is correct and not reported."))
def compare_literal(ctx, spec):
    for node in ast.walk(ctx.tree):
        if not isinstance(node, ast.Compare):
            continue
        ops = _operands(node)
        for i, op in enumerate(node.ops):
            if isinstance(op, (ast.Is, ast.IsNot)) and (_is_literal(ops[i]) or _is_literal(ops[i + 1])):
                neg = isinstance(op, ast.IsNot)
                lo, hi = ctx.span(ops[i])[1], ctx.span(ops[i + 1])[0]
                pos = find_pattern(ctx, lo, hi, rb"\bis(?:\s|\\\n)+not\b" if neg else rb"\bis\b")
                edits = [ctx.edit(spec, pos[0], pos[1], "!=" if neg else "==")] if pos else []
                yield ctx.finding(spec, node, "Use `!=` instead of `is not` with a literal" if neg
                                  else "Use `==` instead of `is` with a literal", edits=edits)


@rule(RuleSpec("ASSERT_TUPLE", "correctness", R, 0.95,
               "`assert(cond, msg)` asserts a non-empty tuple, so it never fails.", "assert on a tuple is always true.",
               "Parenthesising the condition and message creates a tuple, which is always truthy.",
               "assert(x > 0, 'x must be positive')", "assert x > 0, 'x must be positive'", True, False,
               "Tuples with more than two elements are reported without a fix."))
def assert_tuple(ctx, spec):
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) and node.test.elts:
            elts = node.test.elts
            edits = []
            start, end = ctx.span(node)
            srcs = [ctx.segment(e) for e in elts]
            if len(elts) <= 2 and all(single_line(s) for s in srcs) and b"#" not in ctx.data[start:end] \
                    and not any(isinstance(e, (ast.Starred, ast.NamedExpr)) for e in elts) and node.msg is None:
                edits = [ctx.edit(spec, start, end, "assert " + ", ".join(srcs))]
            yield ctx.finding(spec, node, edits=edits)


def _type_call(n: ast.AST) -> ast.Call | None:
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "type" \
            and len(n.args) == 1 and not n.keywords and not isinstance(n.args[0], ast.Starred):
        return n
    return None


@rule(RuleSpec("TYPE_COMPARE", "correctness", R, 0.85,
               "`type(x) == T` ignores subclasses.", "Use isinstance() instead of comparing type().",
               "isinstance() honours inheritance. The fix is 'review' because exact-type checks are sometimes intended.",
               "if type(x) == int:", "if isinstance(x, int):", True, False,
               "Use `type(x) is T` deliberately when subclasses must be excluded."))
def type_compare(ctx, spec):
    for node in ast.walk(ctx.tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))):
            continue
        left, right = node.left, node.comparators[0]
        call, other = (_type_call(left), right) if _type_call(left) else (_type_call(right), left)
        if call is None or _type_call(other) or isinstance(other, (ast.Constant, ast.Tuple, ast.List, ast.Set, ast.Dict)):
            continue
        negate = isinstance(node.ops[0], (ast.NotEq, ast.IsNot))
        s, e = ctx.span(node)
        edits = []
        if b"#" not in ctx.data[s:e]:
            arg, typ = ctx.segment(call.args[0]), ctx.segment(other)
            if single_line(arg) and single_line(typ):
                edits = [ctx.edit(spec, s, e, f"{'not ' if negate else ''}isinstance({arg}, {typ})")]
        yield ctx.finding(spec, node, edits=edits)


def _open_in_with(ctx, call: ast.Call) -> bool:
    parent = ctx.parents.get(call)
    if isinstance(parent, ast.withitem):
        return True
    if isinstance(parent, (ast.Return, ast.Yield, ast.YieldFrom)):
        return True                     # ownership is handed to the caller
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) \
            and parent.func.attr in ("enter_context", "enter_async_context", "closing"):
        return True
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "closing":
        return True
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
        name = parent.targets[0].id
        scope = ctx.parents.get(parent)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            scope = ctx.parents.get(scope)
        for n in ast.walk(scope or ctx.tree):
            if isinstance(n, ast.withitem) and isinstance(n.context_expr, ast.Name) and n.context_expr.id == name:
                return True
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == name:
                return True                 # ownership is handed to the caller
    return False


@rule(RuleSpec("RESOURCE_NO_CONTEXT", "correctness", R, 0.8,
               "`open()` is used without a `with` block.", "Resource opened outside a context manager.",
               "Files opened without `with` stay open if an exception occurs before close().",
               "f = open(path)\ndata = f.read()\nf.close()\n", "with open(path) as f:\n    data = f.read()\n", False, False,
               "Not reported when the handle is returned, closed via ExitStack/closing, or later used in `with`."))
def resource_no_context(ctx, spec):
    if "open" in ctx.symbols.module.bindings:
        return
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" \
                and not _open_in_with(ctx, node):
            yield ctx.finding(spec, node, "open() used outside a `with` block")


def _can_fall_through(body: list[ast.stmt]) -> bool:
    if not body:
        return True
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return False
    if isinstance(last, ast.If):
        return _can_fall_through(last.body) or _can_fall_through(last.orelse)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _can_fall_through(last.body)
    if isinstance(last, ast.Try):
        if last.finalbody and not _can_fall_through(last.finalbody):
            return False
        normal = last.orelse or last.body
        return _can_fall_through(normal) or any(_can_fall_through(h.body) for h in last.handlers)
    if isinstance(last, ast.While) and isinstance(last.test, ast.Constant) and last.test.value \
            and not any(isinstance(n, ast.Break) for n in ast.walk(last)):
        return False
    return True


@rule(RuleSpec("INCONSISTENT_RETURN", "correctness", S, 0.9,
               "Some paths return a value and others return nothing.", "Inconsistent return behaviour.",
               "Mixing `return value` with bare `return` (or falling off the end) makes callers guess whether "
               "None is meaningful. The safe fix makes bare returns explicit (`return None`).",
               "def f(x):\n    if x:\n        return 1\n    return\n",
               "def f(x):\n    if x:\n        return 1\n    return None\n", True, False,
               "Functions annotated `-> None` and generators are skipped. Falling off the end is reported, not fixed."))
def inconsistent_return(ctx, spec):
    for node, qual, _ in ctx.functions:
        if is_generator(node) or (isinstance(node.returns, ast.Constant) and node.returns.value is None):
            continue
        rets = [n for n in own_nodes(node) if isinstance(n, ast.Return)]
        valued = [r for r in rets if r.value is not None and not is_none(r.value)]
        bare = [r for r in rets if r.value is None]
        if not valued:
            continue
        implicit = _can_fall_through(node.body)
        if not bare and not implicit:
            continue
        edits = []
        for r in bare:
            s, e = ctx.span(r)
            if ctx.text(s, e) == "return":
                edits.append(ctx.edit(spec, s, e, "return None", "make the bare return explicit"))
        what = "bare 'return'" if bare else "falling off the end"
        yield ctx.finding(spec, bare[0] if bare else node,
                          f"'{qual}' returns a value on some paths but uses {what} on others",
                          edits=edits if bare and len(edits) == len(bare) else [], function=qual)
