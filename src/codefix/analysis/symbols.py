"""Scope analysis: bindings, references, undefined names, unused imports.

This is a deliberately conservative, standard-library implementation. Name resolution is
done after the whole tree is walked, so use-before-definition at module level is *not*
reported (it avoids false positives for forward references in functions).
"""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass

BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__path__",
    "__builtins__", "__debug__", "__annotations__", "WindowsError", "__class__",
    "__module__", "__qualname__", "reveal_type", "reveal_locals",
}
DYNAMIC_CALLS = {"exec", "eval", "globals", "locals", "vars"}   # can define or use names invisibly


@dataclass
class Binding:
    name: str
    kind: str                      # import future function class param variable except match global typeparam
    node: ast.AST
    scope: "Scope"
    stmt: ast.AST | None = None    # owning import statement
    used: bool = False
    guarded: bool = False          # import inside try/except ImportError


class Scope:
    def __init__(self, kind: str, node: ast.AST, parent: "Scope | None", name: str = ""):
        self.kind, self.node, self.parent, self.name = kind, node, parent, name
        self.bindings: dict[str, list[Binding]] = {}
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.children: list[Scope] = []

    @property
    def qualname(self) -> str:
        parts, s = [], self
        while s is not None and s.kind != "module":
            if s.kind in ("function", "class") and s.name:
                parts.append(s.name)
            s = s.parent
        return ".".join(reversed(parts))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Scope {self.kind} {self.qualname or '<module>'}>"


@dataclass
class NameRef:
    name: str
    node: ast.AST
    ctx: str                       # load | store | del
    scope: Scope
    target: Scope | None = None
    in_string: bool = False
    name_error_guard: bool = False


class SymbolTable:
    def __init__(self) -> None:
        self.module: Scope = None  # type: ignore[assignment]
        self.scopes: list[Scope] = []
        self.refs: list[NameRef] = []
        self.undefined: list[NameRef] = []
        self.exports: set[str] = set()
        self.star_import = False
        self.dynamic = False
        self.global_decls: list[tuple[ast.AST, Scope]] = []

    def all_bindings(self) -> list[Binding]:
        return [b for s in self.scopes for bl in s.bindings.values() for b in bl]

    def find_scope(self, qualname: str) -> list[Scope]:
        return [s for s in self.scopes if (s.qualname == qualname if qualname else s.kind == "module")]


class _Builder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.table = SymbolTable()
        self.cur: Scope = None  # type: ignore[assignment]
        self.import_guard = 0
        self.name_error_guard = 0
        self.in_annotation = 0

    # ----------------------------------------------------------- scope helpers
    def push(self, kind: str, node: ast.AST, name: str = "") -> Scope:
        s = Scope(kind, node, self.cur, name)
        if self.cur is not None:
            self.cur.children.append(s)
        self.table.scopes.append(s)
        self.cur = s
        return s

    def pop(self) -> None:
        self.cur = self.cur.parent  # type: ignore[assignment]

    def bind(self, name: str, kind: str, node: ast.AST, stmt: ast.AST | None = None,
             skip_comprehension: bool = False) -> Binding | None:
        scope = self.cur
        if skip_comprehension:
            while scope.kind == "comprehension" and scope.parent is not None:
                scope = scope.parent
        if name in scope.globals:
            scope = self.table.module
            kind = "global" if name not in scope.bindings else kind
            b = Binding(name, "global", node, scope, used=True)
            scope.bindings.setdefault(name, []).append(b)
            return b
        if name in scope.nonlocals:
            return None
        b = Binding(name, kind, node, scope, stmt, guarded=bool(self.import_guard))
        scope.bindings.setdefault(name, []).append(b)
        return b

    def add_ref(self, name: str, node: ast.AST, ctx: str, in_string: bool = False) -> NameRef:
        ref = NameRef(name, node, ctx, self.cur, None, in_string, bool(self.name_error_guard))
        self.table.refs.append(ref)
        return ref

    # ------------------------------------------------------------------ module
    def build(self, tree: ast.Module) -> SymbolTable:
        self.table.module = self.push("module", tree)
        for stmt in tree.body:
            self.visit(stmt)
        self.pop()
        self._resolve()
        return self.table

    # -------------------------------------------------------------- statements
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.bind(name, "import", alias, stmt=node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.table.star_import = True
                continue
            kind = "future" if node.module == "__future__" else "import"
            self.bind(alias.asname or alias.name, kind, alias, stmt=node)

    def visit_Global(self, node: ast.Global) -> None:
        self.cur.globals.update(node.names)
        self.table.global_decls.append((node, self.cur))

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.cur.nonlocals.update(node.names)
        self.table.global_decls.append((node, self.cur))

    def _type_params(self, node: ast.AST) -> bool:
        tps = getattr(node, "type_params", None)
        if not tps:
            return False
        self.push("typeparams", node)
        for tp in tps:
            self.bind(tp.name, "typeparam", tp)
        return True

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        a = node.args
        for d in [*a.defaults, *[k for k in a.kw_defaults if k is not None]]:
            self.visit(d)
        self.bind(node.name, "function", node)
        tp = self._type_params(node)
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []),
                  *([a.kwarg] if a.kwarg else [])]
        for p in params:
            self._annotation(p.annotation)
        self._annotation(node.returns)
        self.push("function", node, node.name)
        for p in params:
            self.bind(p.arg, "param", p)
        for stmt in node.body:
            self.visit(stmt)
        self.pop()
        if tp:
            self.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        a = node.args
        for d in [*a.defaults, *[k for k in a.kw_defaults if k is not None]]:
            self.visit(d)
        self.push("function", node, "<lambda>")
        for p in [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []),
                  *([a.kwarg] if a.kwarg else [])]:
            self.bind(p.arg, "param", p)
        self.visit(node.body)
        self.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        self.bind(node.name, "class", node)
        tp = self._type_params(node)
        for b in node.bases:
            self.visit(b)
        for k in node.keywords:
            self.visit(k.value)
        self.push("class", node, node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.pop()
        if tp:
            self.pop()

    def visit_TypeAlias(self, node) -> None:  # py3.12+
        self.bind(node.name.id, "variable", node.name)
        tp = self._type_params(node)
        self.visit(node.value)
        if tp:
            self.pop()

    def _annotation(self, node: ast.AST | None) -> None:
        if node is not None:
            self.in_annotation += 1
            self.visit(node)
            self.in_annotation -= 1

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._annotation(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for t in node.targets:
            self.visit(t)
        if self.cur.kind == "module":
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    self._exports(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self.add_ref(node.target.id, node.target, "load")
            if self.cur.kind == "module" and node.target.id == "__all__":
                self._exports(node.value)
        self.visit(node.target)

    def _exports(self, value: ast.AST) -> None:
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for e in value.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    self.table.exports.add(e.value)

    def visit_Try(self, node: ast.Try) -> None:
        names: set[str] = set()
        for h in node.handlers:
            if h.type is None:
                names.add("*")
            else:
                for n in ast.walk(h.type):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        import_guard = bool(names & {"*", "ImportError", "ModuleNotFoundError", "Exception"})
        name_guard = bool(names & {"*", "NameError"})
        self.import_guard += import_guard
        self.name_error_guard += name_guard
        for stmt in node.body:
            self.visit(stmt)
        self.import_guard -= import_guard
        self.name_error_guard -= name_guard
        for h in node.handlers:
            self.visit(h)
        for stmt in [*node.orelse, *node.finalbody]:
            self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self.bind(node.name, "except", node)
        for stmt in node.body:
            self.visit(stmt)

    # ------------------------------------------------------------ expressions
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.add_ref(node.id, node, "load")
        elif isinstance(node.ctx, ast.Del):
            self.add_ref(node.id, node, "del")
        else:
            b = self.bind(node.id, "variable", node)
            ref = self.add_ref(node.id, node, "store")
            ref.target = b.scope if b else None

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        b = self.bind(node.target.id, "variable", node.target, skip_comprehension=True)
        ref = self.add_ref(node.target.id, node.target, "store")
        ref.target = b.scope if b else None

    def visit_Constant(self, node: ast.Constant) -> None:
        if self.in_annotation and isinstance(node.value, str):
            try:
                expr = ast.parse(node.value.strip(), mode="eval")
            except SyntaxError:
                return
            for n in ast.walk(expr):
                if isinstance(n, ast.Name):
                    self.add_ref(n.id, node, "load", in_string=True)

    def _comprehension(self, node: ast.AST, elts: list[ast.AST]) -> None:
        gens = node.generators  # type: ignore[attr-defined]
        self.visit(gens[0].iter)
        self.push("comprehension", node)
        for i, g in enumerate(gens):
            if i:
                self.visit(g.iter)
            self.visit(g.target)
            for cond in g.ifs:
                self.visit(cond)
        for e in elts:
            self.visit(e)
        self.pop()

    def visit_ListComp(self, node): self._comprehension(node, [node.elt])
    visit_SetComp = visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node): self._comprehension(node, [node.key, node.value])

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in DYNAMIC_CALLS:
            self.table.dynamic = True
        self.generic_visit(node)

    def visit_MatchAs(self, node) -> None:
        if node.pattern is not None:
            self.visit(node.pattern)
        if node.name:
            self.bind(node.name, "match", node)

    def visit_MatchStar(self, node) -> None:
        if node.name:
            self.bind(node.name, "match", node)

    def visit_MatchMapping(self, node) -> None:
        for k in node.keys:
            self.visit(k)
        for p in node.patterns:
            self.visit(p)
        if node.rest:
            self.bind(node.rest, "match", node)

    # -------------------------------------------------------------- resolution
    def _lookup(self, name: str, origin: Scope) -> Scope | None:
        module = self.table.module
        s: Scope | None = origin
        if name in origin.nonlocals:
            s = origin.parent
        while s is not None:
            if name in s.globals:
                return module if name in module.bindings else None
            if (s.kind != "class" or s is origin) and name in s.bindings:
                return s
            s = s.parent
        return None

    def _resolve(self) -> None:
        t = self.table
        for ref in t.refs:
            if ref.ctx == "store" and ref.target is not None:
                continue
            found = self._lookup(ref.name, ref.scope)
            ref.target = found
            if found is not None:
                for b in found.bindings[ref.name]:
                    b.used = True
            elif ref.ctx != "store" and ref.name not in BUILTINS:
                t.undefined.append(ref)
        for name in t.exports:
            for b in t.module.bindings.get(name, []):
                b.used = True


def build_symbols(tree: ast.Module) -> SymbolTable:
    return _Builder().build(tree)
