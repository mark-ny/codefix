# Expanding CodeFix's knowledge

You can teach CodeFix new rules **without writing Python**. A taught rule is plain data stored in
`<project>/knowledge/project/user_rules.json` (examples in `examples.json` beside it). It is active
immediately in the terminal (`review`, `fix`, `autofix`, …) and in the web page, is cross-checked by
`codefix knowledge validate/test`, and can be shared as a JSON pack. Rules cannot execute code; fixes still go
through policy, syntax validation, diff, backup and tests.

## 1. Teach by example (easiest)

Web: **Teach CodeFix** → give an id, a message, the code to flag, and what it should become → **Learn pattern** →
**Try on code** → **Save rule**. Terminal:

```bash
codefix knowledge teach USER_NO_HAS_KEY --bad "d.has_key(k)" --good "k in d" \
        --message "dict.has_key was removed in Python 3" --negative "k in d"
codefix knowledge teach USER_NO_EVAL --bad "eval(user_input)" --message "eval runs arbitrary code"   # report-only
```

CodeFix generalises the example: names that look like data become `$variables` (`$d.has_key($k)` → `$k in $d`);
callee names, attributes, keywords and builtins stay literal. Check the learned pattern with `--dry-run`, or in the
page, and edit it if it is too broad or narrow. A rule is saved only if its self-test passes (the bad example fires,
the fix produces exactly the good example and still compiles, negatives do not fire).

## 2. Other rule types (web form, or JSON)

| `match.type` | Fields | Fix |
|---|---|---|
| `pattern` | `pattern` (tokens with `$vars`), `replace` (template) | optional |
| `call` | `name` / `names` (dotted, e.g. `os.system`) | `replace_with` renames the callee |
| `import` | `module` | `replace_with` for `from x import y` |
| `regex` | `pattern`, `flags` (`i`,`m`), `code_only` (default true) | none (report only) |

```json
{"id": "USER_NO_PRINT", "category": "project", "severity": "review", "confidence": 0.9,
 "autofix": true, "requires_tests": false, "message": "Use logging instead of print()",
 "bad": "print(x)\n", "good": "logger.info(x)\n",
 "match": {"type": "pattern", "pattern": "print($x)", "replace": "logger.info($x)"}}
```

`autofix` must be true exactly when a replacement is defined. Ids are `UPPER_SNAKE_CASE` and may not clash with
built-in or plugin rules.

### Pattern language

* Tokens are matched, not text: whitespace, line breaks and comments between tokens do not matter, and
  strings/comments are never searched.
* `$name` matches **one operand**: a name, number, string, bracketed group, or such an operand followed by
  attribute/call/subscript chains (`foo.bar(1)[0]`). The same `$name` twice must match identical text.
* Two variables cannot be adjacent; a pattern needs at least one literal token.
* If a match contains a comment, the finding is reported but not auto-fixed.
* Matching is token-based, not type-aware: `$a.has_key($k)` matches any `.has_key(...)` call. Use *Try on code*,
  keep severity at `review`, and add negative examples for look-alikes.

## 3. Manage and share

```bash
codefix knowledge list --user          # your rules
codefix knowledge show USER_NO_PRINT
codefix knowledge remove USER_NO_PRINT
codefix knowledge export -o team-rules.json
codefix knowledge import team-rules.json [--replace]   # every imported rule is re-validated and self-tested
codefix knowledge validate && codefix knowledge test    # includes your rules
codefix knowledge feedback USER_NO_PRINT --verdict false_positive   # advisory log only
```

Commit `knowledge/project/` to your repository so the whole team shares the rules. Feedback never changes a rule
by itself; edit or remove the rule deliberately.

## 4. Skills: named bundles you can hand to CodeFix

A **skill** is a JSON file `knowledge/skills/<name>.json` bundling several rules (and optionally their examples).
Make one in the page (*Skills & knowledge* → Create a new skill → pick it in *Add it to skill* when saving rules), with
`codefix skill new NAME`, or **give CodeFix a finished file to learn**:

```bash
codefix skill validate team.json     # dry run, per-rule report, nothing installed
codefix skill try team.json --code-file src/app.py   # what would it flag/change?
codefix skill learn team.json        # install; all-or-nothing; active immediately
codefix skill list | show NAME | disable NAME | enable NAME | forget NAME | export NAME -o team.json
```

Rule ids must be unique across built-ins, plugins, your rules and every skill. A skill that fails any check is not
installed at all. Disabled skills keep their file but their rules do not run. Edit a rule in the page (*edit* next to
it), or edit the JSON and `learn --replace`. The full format, an error decoder and guardrails for AI agents are in
`AGENT_README.md`; the schema is `knowledge/skill.schema.json`; examples are in `docs/examples/skills/`.

## 5. Deciding what to fix and what to leave alone

Rules you taught, built-in rules and skills all obey the same policy, edited in the page (*Rules & policy*) or with
`codefix policy`: per rule *enabled / report_only / disabled / blocked*, an optional fix-severity override
(*safe / review / risky*), and path exclusions such as `tests/*`. Stored in `codefix.policy.json` (your hand-written
`codefix.toml` is never rewritten). Security rules can never be promoted to `safe`.

## 6. When you need real logic

Rules that need scope or type information, or custom fixes, are Python plugins (`docs/plugin-development.md`,
`docs/rule-authoring.md`). Documentation-only entries in the framework packs show where such rules would go.
