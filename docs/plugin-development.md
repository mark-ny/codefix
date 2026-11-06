# Plugin development

Plugins add rules. Two discovery mechanisms:

1. **Entry points** (installed packages):

   ```toml
   [project.entry-points."codefix.rules"]
   myrules = "my_package.codefix_plugin:register"
   ```

2. **Config modules** (no packaging needed): `[plugins] modules = ["tools.codefix_rules"]`.

The target may be a `register(registry)` function, a `Rule` instance/subclass, or a list (`RULES`):

```python
from codefix.knowledge.registry import rule

@rule(RuleSpec(...), builtin=False)        # builtin=False: don't auto-register globally
def my_rule(ctx, spec): ...

def register(registry):
    registry.register(my_rule)
```

Plugin errors never crash a run: they are collected in `registry.plugin_errors` and shown by
`codefix knowledge validate` (as are duplicate ids, attributed to the plugin that caused them).
Rules that raise at analysis time are skipped with a warning on that file's result.

Ship knowledge JSON for plugin rules in a project pack (`knowledge/project/*.json`, enable with
`packs = ["python", "project"]`) so `knowledge show` can explain them and validation can cross-check them.
