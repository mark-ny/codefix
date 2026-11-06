# Rule authoring

A rule is a function `check(ctx, spec) -> Iterable[Finding]` plus a `RuleSpec`:

```python
import ast
from codefix.knowledge.registry import rule
from codefix.models import RuleSpec, Severity

@rule(RuleSpec("NO_PRINT", "style", Severity.REVIEW, 0.9,
               description="print() left in code", message="Remove print()",
               explanation="...", bad="print(x)\n", good="log.debug(x)\n",
               autofix=False, requires_tests=False, false_positive="CLI tools print on purpose."))
def no_print(ctx, spec):
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            yield ctx.finding(spec, node)
```

## Context helpers

`ctx.tree`, `ctx.symbols` (scopes/bindings/undefined names), `ctx.functions`, `ctx.metrics`,
`ctx.parents`, `ctx.config`, `ctx.span(node)` (UTF-8 byte offsets), `ctx.segment(node)`,
`ctx.masked(start, end)` (comments blanked), `ctx.index.line_text(n)`,
`ctx.edit(spec, start, end, replacement)`, `ctx.finding(spec, node, message, edits=[...])`.

## Writing a fix

Return `edits=[ctx.edit(spec, start, end, new_text)]`. Offsets are **bytes** of the UTF-8 text; use
`ctx.span` rather than `col_offset` arithmetic. Rules for safe fixes:

* Touch only the minimum range (an operator, a name) so comments/formatting survive.
* Decline (no edit) in any ambiguous layout: comments inside the span, multi-line operands,
  statements sharing a line. A finding without edits is reported, not fixed.
* One finding = one atomic group; edits of a finding are applied together or not at all.
* Pick severity by *risk of the fix*: `safe` (behaviour-preserving), `review` (usually right),
  `risky` (can change behaviour; set `requires_tests=True`). Confidence is how sure you are the finding is real.

## Knowledge entry and examples

`knowledge/python/patterns.json` entry (required: `id category severity confidence autofix
requires_tests message bad good`; optional: `description explanation false_positive frameworks docs tests`):

```json
{"id": "COMPARE_NONE", "category": "correctness", "severity": "safe", "confidence": 0.99, "autofix": true,
 "requires_tests": false, "message": "Use `is None` instead of `== None`.", "bad": "if value == None:", "good": "if value is None:"}
```

`examples.json` maps rule id → examples: `{"kind": "fix", "bad", "good", "edge": true}`,
`{"kind": "detect", "code"}`, `{"kind": "negative", "code"}`, optional `"config": {"max_params": 2}`.
Every autofix rule needs a `fix`, an `edge` fix and a `negative`; every other rule a `detect` and a `negative`.
`codefix knowledge validate` and `codefix knowledge test` enforce this; a fixed result must compile and be idempotent.

## Security rules

Use `codefix.rules.security.security_rule` (category must be `security`). They are isolated from general
rules and always subject to: no automatic application, `[security] minimum_confidence` (0.95), autofix off
unless `[security] allow_autofix`, and then explicit approval. Document them in `knowledge/security/rules.json`.
Do not ship fixes for security findings unless they have been reviewed and tested by your security team.
