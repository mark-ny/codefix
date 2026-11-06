"""Optional LibCST-backed engine (formatting-preserving, scope-aware rename)."""
from __future__ import annotations

try:  # pragma: no cover - exercised only when libcst is installed
    import libcst as cst
    from libcst.metadata import MetadataWrapper, ScopeProvider
    HAVE_LIBCST = True
except ImportError:  # pragma: no cover
    HAVE_LIBCST = False


class CSTError(Exception):
    pass


def available() -> bool:
    return HAVE_LIBCST


def _qualname(scope) -> str:
    parts = []
    while scope is not None and type(scope).__name__ != "BuiltinScope":
        name = getattr(scope, "name", None)
        if name and type(scope).__name__ in ("FunctionScope", "ClassScope"):
            parts.append(name)
        scope = getattr(scope, "parent", None)
    return ".".join(reversed(parts))


def rename(text: str, old: str, new: str, scope_name: str | None = None) -> tuple[str, list[str], str]:
    """Rename ``old`` to ``new`` inside one scope. Returns ``(new_text, warnings, scope description)``."""
    if not HAVE_LIBCST:
        raise CSTError("LibCST is not installed")
    try:
        wrapper = MetadataWrapper(cst.parse_module(text))
    except Exception as exc:
        raise CSTError(f"LibCST could not parse the file: {exc}") from exc
    scopes: list = []
    for sc in wrapper.resolve(ScopeProvider).values():
        if sc not in scopes:
            scopes.append(sc)
    cands = [(sc, [a for a in sc[old] if a.scope is sc]) for sc in scopes if type(sc).__name__ != "BuiltinScope"]
    cands = [(sc, assigns) for sc, assigns in cands if assigns]
    if not cands:
        raise CSTError(f"no binding named '{old}' found")
    if scope_name is not None:
        want = "" if scope_name in ("", "<module>") else scope_name
        picked = [c for c in cands if _qualname(c[0]) == want]
        if not picked:
            raise CSTError(f"'{old}' is not bound in scope '{scope_name}'; candidates: "
                           + ", ".join(repr(_qualname(c[0]) or "<module>") for c in cands))
        target, assigns = picked[0]
    else:
        def depth(sc):
            d = 0
            while getattr(sc, "parent", None) is not None and type(sc.parent).__name__ != "BuiltinScope":
                sc, d = sc.parent, d + 1
            return d
        best = min(depth(c[0]) for c in cands)
        top = [c for c in cands if depth(c[0]) == best]
        if len(top) > 1:
            raise CSTError(f"'{old}' is bound in several scopes ({', '.join(repr(_qualname(c[0]) or '<module>') for c in top)}); "
                           "choose one with --scope")
        target, assigns = top[0]
    if [a for a in target[new] if a.scope is target]:
        raise CSTError(f"'{new}' already exists in scope '{_qualname(target) or '<module>'}'")
    ids: set[int] = set()
    warnings: list[str] = []
    for a in assigns:
        node = a.node
        name_node = None
        if isinstance(node, (cst.FunctionDef, cst.ClassDef)):
            name_node = node.name
        elif isinstance(node, cst.Name):
            name_node = node
        elif isinstance(node, cst.Param):
            name_node = node.name
        elif isinstance(node, cst.ImportAlias):
            if node.asname is None:
                raise CSTError(f"'{old}' is bound by an import without an alias; renaming would break the import")
            name_node = node.asname.name if isinstance(node.asname.name, cst.Name) else None
        if name_node is None:
            raise CSTError(f"unsupported binding kind {type(node).__name__} for '{old}'")
        ids.add(id(name_node))
        for ref in a.references:
            if isinstance(ref.node, cst.Name):
                for sc in _chain(ref.scope, target):
                    if [x for x in sc[new] if x.scope is sc]:
                        raise CSTError(f"renaming would collide with an existing '{new}' in scope "
                                       f"'{_qualname(sc) or '<module>'}'")
                ids.add(id(ref.node))
            else:
                warnings.append(f"a reference of type {type(ref.node).__name__} was not renamed")

    class Renamer(cst.CSTTransformer):
        def leave_Name(self, original_node, updated_node):
            return updated_node.with_changes(value=new) if id(original_node) in ids else updated_node

    result = wrapper.visit(Renamer())
    return result.code, warnings, _qualname(target) or "<module>"


def _chain(scope, stop):
    out = []
    while scope is not None and type(scope).__name__ != "BuiltinScope":
        out.append(scope)
        if scope is stop:
            break
        scope = getattr(scope, "parent", None)
    return out
