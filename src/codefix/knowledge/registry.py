"""Rule base classes and the rule registry (built-in rules, plugins, knowledge metadata)."""
from __future__ import annotations

import importlib
import importlib.metadata
from typing import TYPE_CHECKING, Callable, Iterable

from ..models import Finding, ProjectConfig, RuleSpec

if TYPE_CHECKING:  # pragma: no cover
    from ..analysis.ast_engine import AnalysisContext

BUILTIN_RULES: list["Rule"] = []
ENTRY_POINT_GROUP = "codefix.rules"


class Rule:
    """A rule analyses a parsed file and returns findings (with optional structured edits)."""

    spec: RuleSpec

    def check(self, ctx: "AnalysisContext") -> Iterable[Finding]:  # pragma: no cover - interface
        raise NotImplementedError


class FunctionRule(Rule):
    def __init__(self, spec: RuleSpec, fn: Callable[["AnalysisContext", RuleSpec], Iterable[Finding]]):
        self.spec, self._fn = spec, fn

    def check(self, ctx: "AnalysisContext") -> Iterable[Finding]:
        return self._fn(ctx, self.spec)


def rule(spec: RuleSpec, *, builtin: bool = True):
    """Decorator turning ``fn(ctx, spec) -> findings`` into a registered rule."""

    def wrap(fn):
        r = FunctionRule(spec, fn)
        if builtin:
            BUILTIN_RULES.append(r)
        return r

    return wrap


class RegistryError(Exception):
    pass


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._docs: dict[str, RuleSpec] = {}        # knowledge-only entries (no analyzer)
        self.duplicates: list[tuple[str, str]] = []
        self.plugin_errors: list[str] = []
        self.current_origin = "builtin"

    def register(self, rule_obj: Rule, origin: str | None = None) -> None:
        origin = origin or self.current_origin
        rid = rule_obj.spec.id
        if rid in self._rules:
            self.duplicates.append((rid, origin))
            return
        self._rules[rid] = rule_obj

    def add_doc_spec(self, spec: RuleSpec) -> None:
        if spec.id not in self._rules and spec.id not in self._docs:
            self._docs[spec.id] = spec

    def get(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def spec(self, rule_id: str) -> RuleSpec | None:
        r = self._rules.get(rule_id)
        return r.spec if r else self._docs.get(rule_id)

    def rules(self) -> list[Rule]:
        return [self._rules[k] for k in sorted(self._rules)]

    def specs(self) -> list[RuleSpec]:
        merged = {**self._docs, **{k: r.spec for k, r in self._rules.items()}}
        return [merged[k] for k in sorted(merged)]

    def enabled(self, config: ProjectConfig) -> list[Rule]:
        off = set(config.disabled_rules)
        return [r for r in self.rules() if r.spec.id not in off]

    def has_fixer(self, rule_id: str) -> bool:
        r = self._rules.get(rule_id)
        return bool(r and r.spec.autofix)


def _register_plugin_obj(registry: RuleRegistry, obj, origin: str) -> None:
    registry.current_origin = origin
    try:
        _dispatch_plugin_obj(registry, obj, origin)
    finally:
        registry.current_origin = "builtin"


def _dispatch_plugin_obj(registry: RuleRegistry, obj, origin: str) -> None:
    if callable(obj) and not isinstance(obj, (Rule, type)):
        obj(registry)                                   # register(registry) style plugin
    elif isinstance(obj, type) and issubclass(obj, Rule):
        registry.register(obj(), origin)
    elif isinstance(obj, Rule):
        registry.register(obj, origin)
    elif isinstance(obj, (list, tuple)):
        for o in obj:
            _dispatch_plugin_obj(registry, o, origin)
    else:
        raise RegistryError(f"{origin}: unsupported plugin object {obj!r}")


def discover_plugins(registry: RuleRegistry, config: ProjectConfig | None = None) -> None:
    """Load plugins from the ``codefix.rules`` entry-point group and ``[plugins] modules``."""
    try:
        eps = importlib.metadata.entry_points()
        group = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])
    except Exception as exc:  # pragma: no cover
        registry.plugin_errors.append(f"entry point discovery failed: {exc}")
        group = []
    for ep in group:
        try:
            _register_plugin_obj(registry, ep.load(), f"entry point {ep.name}")
        except Exception as exc:
            registry.plugin_errors.append(f"plugin {ep.name}: {exc}")
    for name in (config.plugin_modules if config else []):
        try:
            mod = importlib.import_module(name)
            obj = getattr(mod, "register", None) or getattr(mod, "RULES", None)
            if obj is None:
                raise RegistryError("module exposes neither register(registry) nor RULES")
            _register_plugin_obj(registry, obj, f"module {name}")
        except Exception as exc:
            registry.plugin_errors.append(f"plugin module {name}: {exc}")


def build_registry(config: ProjectConfig | None = None, *, knowledge: bool = True,
                   plugins: bool = True) -> RuleRegistry:
    from ..rules import load_builtin_rules  # local import avoids a cycle

    registry = RuleRegistry()
    for r in load_builtin_rules():
        registry.register(r)
    if plugins:
        discover_plugins(registry, config)
    if knowledge:
        from .loader import apply_knowledge
        apply_knowledge(registry, config or ProjectConfig())
    return registry
