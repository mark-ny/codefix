# AGENT_README — how to give CodeFix new skills

**Audience:** an AI agent (or script) that should *extend CodeFix's knowledge* — teach it new things to
find and fix — and tune what it fixes or leaves alone.
**You do not write Python.** You write a small JSON file (a **skill**), check it, and install it with the CLI.
CodeFix refuses anything that does not validate, so a mistake cannot break the user's code or their install.

---

## 0. TL;DR (the whole job in 6 commands)

```bash
codefix knowledge list --json                         # 1. what rules/ids already exist (do not duplicate)
# 2. write my-skill.json  (format in §2-§4)
codefix skill validate my-skill.json --json           # 3. dry run: per-rule problems, nothing installed
codefix skill try my-skill.json --code-file sample.py # 4. see exactly what it would flag/change
codefix skill learn my-skill.json                     # 5. install (all-or-nothing) — active immediately
codefix review sample.py && codefix knowledge test    # 6. verify in the real tool; exit codes: 0 ok, 1 findings, 2 error
```

Exit codes everywhere: `0` success/clean · `1` findings or pending changes · `2` error (invalid skill, bad file…).
Run commands from the **project root** (where `codefix.toml` is, or the folder the user works in).
Add `--json` to get machine-readable output.

---

## 1. Concepts

| Thing | What it is | Where it lives |
|---|---|---|
| **Rule** | One thing to find (and optionally fix), described as data | inside a skill |
| **Skill** | A named bundle of rules (+ optional examples) | `knowledge/skills/<name>.json` |
| **My rules** | Personal rules made with `codefix knowledge teach` / the web page | `knowledge/project/user_rules.json` |
| **Policy** | What CodeFix does with each rule: fix / only report / ignore, how careful, which paths to skip | `codefix.policy.json` |
| **Built-in rules** | 23 shipped rules; read-only (change *behaviour* via policy, never the rule) | the package |

Skills are **data, not code**: they cannot run anything. Fixes still pass the normal safety pipeline
(syntax validation → policy → diff → backup → atomic write → tests → rollback). Skills are shared across the
terminal and the web page; no restart needed.

---

## 2. The skill file

```json
{
  "format": "codefix-skill",
  "version": 1,
  "name": "team-habits",
  "title": "Team habits",
  "description": "What this skill is for (one sentence).",
  "author": "optional",
  "rules": [ /* §3 */ ],
  "examples": { /* optional, §5 */ }
}
```

* `name`: lowercase letters, digits, hyphens; 2–40 chars; must start with a letter/digit. File = `<name>.json`.
* `rules`: at least one. **Rule ids are unique across the entire installation** (built-in + plugins + my-rules + every skill).
* JSON Schema: `knowledge/skill.schema.json` (shape only — `codefix skill validate` is the authority).
* Ready-made examples: `docs/examples/skills/*.json`. Copy one and edit it.

## 3. A rule

Minimal rule (everything else is defaulted):

```json
{"id": "TEAM_NO_EVAL", "message": "eval() is banned", "bad": "x = eval(s)\n",
 "match": {"type": "call", "name": "eval"}}
```

Full rule:

```json
{
  "id": "TEAM_NO_PRINT",
  "message": "Use the logger instead of print()",
  "severity": "review",
  "confidence": 0.9,
  "category": "project",
  "requires_tests": false,
  "explanation": "Why this matters (shown by `codefix knowledge show`).",
  "false_positive": "When the rule is wrong (CLI tools print on purpose).",
  "bad":  "print(value)\n",
  "good": "logger.info(value)\n",
  "match": {"type": "pattern", "pattern": "print($value)", "replace": "logger.info($value)"}
}
```

| Field | Required | Notes |
|---|---|---|
| `id` | yes | `UPPER_SNAKE_CASE`, 3–64 chars, start with a letter. Use a prefix: `TEAM_`, `PROJ_`, `PY2_` |
| `message` | yes | One line, imperative, says what to do. Shown with every finding |
| `bad` | yes | **Valid Python** that the rule must flag. Headers like `if x:` are auto-completed with a body |
| `good` | iff the match has a replacement | What `bad` becomes after the fix, exactly |
| `match` | yes | §4 |
| `severity` | no (`review`) | `safe` / `review` / `risky` — **risk of the fix** (see §6) |
| `confidence` | no (`0.9`) | 0–1: how sure that a hit is a real problem. Findings below the project minimum (default 0.80) are report-only |
| `requires_tests` | no (`false`) | `true` ⇒ fixes are skipped unless the project has a passing test command |
| `autofix` | no | Derived from `match`; if you set it, it must equal "does `match` define a replacement" |
| `category`, `explanation`, `false_positive`, `docs` | no | Documentation |

## 4. Match types

### `pattern` — token pattern with `$variables` (most powerful)

```json
{"type": "pattern", "pattern": "$d.has_key($k)", "replace": "$k in $d"}
```

* Matches **tokens**, not text: spacing, line breaks and comments between tokens are ignored; strings and
  comments are never searched.
* `$name` matches **exactly one operand**: a name, number, string, a bracketed group, or one of those followed
  by `.attr`, `(...)`, `[...]` chains (e.g. `foo.bar(1)[0]`). It does **not** match whole expressions like `a + b`
  or lambdas. Same `$name` twice ⇒ must match identical text.
* Two variables cannot be adjacent; at least one literal token is required; max 500 characters.
* `replace` is a template using the same `$names`. Omit it for a report-only rule.
* If a comment sits *inside* a match, the finding is reported but **not** auto-fixed (to avoid deleting it).
* Matching is token-based, **not type-aware**: `$d.has_key($k)` matches any `.has_key(...)` call. Tighten with
  literal tokens, add negative examples, keep `severity: review`.

### `call` — calls to a (dotted) name

```json
{"type": "call", "name": "os.system", "replace_with": "run_command"}
{"type": "call", "names": ["eval", "exec"]}
```

Matches the call target **as written** (`os.system`, not `o.system` after `import os as o`). `replace_with`
renames only the callee; arguments are untouched. Omit it to report only.

### `import` — banned / legacy modules

```json
{"type": "import", "module": "imp", "replace_with": "importlib"}
```

Matches `import imp`, `import imp.x`, `from imp import y`. `replace_with` is applied only to the
`from imp import y` form; plain `import imp` is reported (renaming it would break later uses).

### `regex` — text search, report only

```json
{"type": "regex", "pattern": "TODO|FIXME", "flags": "i", "code_only": true}
```

Per line; flags `i`, `m`; `code_only` (default true) ignores matches inside strings/comments. Never fixes.
Use only when the other types cannot express it (max 300 chars).

**Which one?** Rewrite code → `pattern`. Rename a function → `call`. Ban a module → `import`. Find a text marker → `regex`.
Need scope/type awareness, or a custom fix → not possible with skills (see §10).

---

## 5. Examples (the self-test)

Every rule is tested before it is installed. If you give no `examples`, CodeFix generates them from `bad`/`good`:

* fix rule → `bad` must be flagged, the fix must produce **exactly** `good`, the result must compile, and running the rule
  again on the result must produce no further fix (idempotent);
* report-only rule → `bad` must be flagged.

Add your own — especially **negatives** (look-alike code that must *not* be flagged):

```json
"examples": {
  "TEAM_NO_PRINT": [
    {"name": "basic",    "kind": "fix",      "bad": "print(x)\n",     "good": "logger.info(x)\n"},
    {"name": "nested",   "kind": "fix",      "bad": "print(a.b[0])\n", "good": "logger.info(a.b[0])\n", "edge": true},
    {"name": "a method", "kind": "negative", "code": "self.print(x)\n"}
  ]
}
```

Kinds: `fix` (`bad`, `good`), `detect` (`code`), `negative` (`code`). Example code must be valid Python.
Your examples replace the generated ones for that rule, so include a `fix`/`detect` example yourself if you
provide any.

---

## 6. Choosing severity and confidence (read this)

| `severity` | Use when | What CodeFix does |
|---|---|---|
| `safe` | The fix **cannot** change behaviour (pure rename of something that cannot work any more, equivalent spelling) | applied automatically by `autofix` |
| `review` *(default)* | Usually right, but a human should look | applied only after confirmation |
| `risky` | May change behaviour (semantics, API, performance) | explicit approval **and** passing tests |

Rules of thumb: report-only rules → any severity (it is just labelling); **when unsure choose `review`**;
never choose `safe` for a pattern with a variable that could match unrelated code; lower `confidence` (0.6–0.8)
for heuristic rules so the user sees them but they are not applied by default. The user can always override
severity later without editing your file (§8).

---

## 7. The workflow, step by step

1. **Understand the request.** What should be found? What should it become? What must NOT be touched?
2. **Check for duplicates:** `codefix knowledge list --json` (every `id`, `message`, `category`). Do not re-implement a built-in rule; if a built-in already does it, change its *policy* (§8) instead.
3. **Collect 2–4 real code snippets** from the project: ones that must be flagged, and look-alikes that must not.
4. **Draft the skill.** One rule per behaviour. Prefer `call`/`import` over `pattern` when they suffice.
   For `pattern`, you can let CodeFix generalise an example for you:
   `codefix knowledge teach TEMP_ID --bad "d.has_key(k)" --good "k in d" --message "m" --dry-run`
   prints the learned `pattern`/`replace` to paste into your file (use `--dry-run`: nothing is saved).
5. **Validate:** `codefix skill validate my-skill.json --json` → read `rules.<ID>` (list of problems per rule). Fix and repeat until `"ok": true`.
6. **Try it on real code, no install:** `codefix skill try my-skill.json --code-file path/to/real_file.py`.
   Check the findings *and the diff*. Fix over-matching by adding literal tokens, then add a `negative` example for the case you found.
7. **Install:** `codefix skill learn my-skill.json` (use `--replace` to update an installed skill).
   All-or-nothing: if any rule fails, nothing is installed.
8. **Verify in the real tool:** `codefix review path/to/real_file.py`, then `codefix knowledge validate` and `codefix knowledge test` (both must exit 0).
9. **Report to the user:** list the rule ids, what each flags/fixes, the severity you chose and why, and how to undo (`codefix skill disable NAME` / `forget NAME`).
10. **Never apply fixes to the user's files yourself** unless asked: use `codefix fix FILE` (dry run) and show the diff; writing needs `--write`.

Manage skills: `codefix skill list|show NAME|enable NAME|disable NAME|forget NAME|remove-rule NAME ID|export NAME -o FILE`, `codefix skill new NAME --title T`.

### JSON shapes (`--json`)

```text
skill validate → {"ok": bool, "name": str, "rule_count": int, "problems": [str], "rules": {"RULE_ID": [str problems]}}
skill learn    → same + {"learned": bool, "path": str}
skill try      → {"ok": bool, "problems": [...], "findings": [{rule_id, line, column, message, fixable, ...}], "fixed": str|null, "diff": str}
skill list     → [{"name", "enabled", "title", "description", "rules": [{"id", "message", "severity", "autofix", "match"}]}]
```

### Error decoder

| Message contains | Meaning / fix |
|---|---|
| `already used by a built-in, plugin, user or other skill rule` | Rename the id (or use `--replace` if it is *this* skill) |
| `id must be UPPER_SNAKE_CASE` | e.g. `TEAM_NO_EVAL` |
| `pattern needs at least one literal token` | Add a fixed token such as `==` or a function name |
| `two variables cannot be adjacent` | Put a literal token between `$a` and `$b` |
| `replacement uses variables not in the pattern` | Every `$x` in `replace` must appear in `pattern` |
| `pattern has an unclosed bracket` | Balance `( [ {` in the pattern |
| `rule did not fire` | `bad` does not match the pattern — check tokens and variable use (`$x` = one operand) |
| `unexpected fix result` | The fix output differs from `good` — fix `replace` or `good` |
| `fix is not idempotent` | The replacement still matches the pattern; make the pattern more specific |
| `the example is not valid Python` | `bad`/`good` must parse (only `if x:`-style headers are completed) |
| `'autofix' must be true exactly when…` | Remove `autofix` (it is derived) or add/remove the replacement |
| `at least one example that triggers the rule is required` | Your custom `examples` for that rule lack a `fix`/`detect` entry |
| `examples given for unknown rule` | A key in `examples` is not a rule id in this skill |

---

## 8. Telling CodeFix what to fix and what to leave alone (policy)

Do **not** edit rule definitions to change behaviour; use policy. It lives in `codefix.policy.json`
(created and validated by the CLI; the hand-written `codefix.toml` is never touched).

```bash
codefix policy show [--changed] [--json]       # effective behaviour of every rule
codefix policy set RULE_ID --mode report_only  # flag it but never fix it
codefix policy set RULE_ID --mode disabled     # do not even run it
codefix policy set RULE_ID --mode blocked      # reported; its fix can never be applied
codefix policy set RULE_ID --mode enabled      # back to normal
codefix policy set RULE_ID --severity safe     # how careful its fix must be (safe|review|risky)
codefix policy set RULE_ID --exclude "tests/*" --exclude "migrations/*"   # skip these paths
codefix policy set RULE_ID --clear-exclude --clear-severity
codefix policy behavior --allow-risky true --min-confidence 0.85
codefix policy reset RULE_ID | codefix policy reset --all
```

File format (if you must write it; `codefix policy show` re-validates it):

```json
{"version": 1,
 "rules": {"MISSING_DOCSTRING": {"mode": "report_only", "exclude": ["tests/*"]},
           "UNUSED_IMPORT": {"severity": "safe"}},
 "behavior": {"allow_risky": false, "minimum_confidence": 0.8},
 "skills": {"old-skill": {"enabled": false}}}
```

Notes: `exclude` globs match the path relative to the project root or the file name. `security`-category rules
can never be promoted to `safe`. A corrupt policy file makes CodeFix exit `2` with a clear message instead of guessing.
Examples/self-tests ignore local policy, so they stay reproducible.

---

## 9. Guardrails — what you MUST and MUST NOT do

**Must**
* Validate and `try` before `learn`; show the user the findings/diff from `try`.
* Include at least one negative example for every `pattern` rule that uses a variable.
* Prefer `review` over `safe`; prefer report-only when a fix is not obviously correct.
* Keep ids unique, prefixed and descriptive; keep messages actionable.
* Tell the user what you added and how to turn it off.

**Must not**
* Do not set `severity: "safe"` unless the fix cannot alter behaviour.
* Do not disable, block or silence a rule, or raise `minimum_confidence`, because of a failing run or a complaint
  unless **the user asked for that change**. Feedback is advisory (`codefix knowledge feedback`) — it never edits rules.
* Do not hand-edit `.codefix/` (backups) or other skills' files; use the CLI.
* Do not write rules that rewrite security-sensitive code (auth, crypto, SQL, shell) with `autofix`; report only.
* Do not put secrets or personal data in skills (they are meant to be shared and committed).
* Do not try to bypass validation (e.g. by editing `knowledge/skills/*.json` directly to skip `learn`); invalid files
  are reported as errors by `codefix knowledge validate`.

---

## 10. Limits — when a skill is not enough

Skills are token/AST-name based. They cannot: use types or scope ("only when `x` is a `dict`"), look at other
files, track data flow, or produce custom edits beyond a template. For those, write a Python plugin
(`docs/plugin-development.md`, `docs/rule-authoring.md`) — and ask the user first, because plugins run code.
Simple things that look hard but work: argument reordering (`foo($a, $b)` → `foo($b, $a)`), wrapping
(`open($p)` → `Path($p).open()`). A fix always replaces the whole matched token run, so to change just one token
include its neighbouring tokens in both `pattern` and `replace`.

---

## 11. Copy-paste templates

**Minimal skill**

```json
{"format": "codefix-skill", "name": "my-skill", "title": "My skill", "rules": [
  {"id": "MY_NO_EVAL", "message": "eval() is not allowed", "bad": "x = eval(s)\n",
   "match": {"type": "call", "name": "eval"}}]}
```

**Fixing rule with explicit examples**

```json
{"id": "MY_ITERITEMS", "message": "iteritems() no longer exists; use items()", "severity": "review",
 "bad": "for k, v in d.iteritems():\n    pass\n", "good": "for k, v in d.items():\n    pass\n",
 "match": {"type": "pattern", "pattern": "$d.iteritems()", "replace": "$d.items()"}}
```

**Prompt to give an agent**

> You are extending CodeFix. Read `AGENT_README.md` completely. Task: <describe what to find and fix>.
> Work in the project root. Follow §7: list existing rules, draft `knowledge/skills`-compatible JSON in a scratch file,
> run `codefix skill validate`, then `codefix skill try` on real project files, add negative examples for look-alikes,
> then `codefix skill learn`, then `codefix knowledge validate && codefix knowledge test`. Respect §9 (severity `review`
> unless provably safe; never change policy unless I ask). Finish by reporting each rule id, what it flags/fixes, its
> severity, and the commands to disable or remove it.

---

## 12. Final checklist

- [ ] `codefix knowledge list --json` checked; ids are new and prefixed
- [ ] `codefix skill validate FILE --json` → `"ok": true`
- [ ] `codefix skill try FILE --code-file REAL_FILE` output reviewed (no over-matching)
- [ ] negative example for each variable pattern
- [ ] severity chosen conservatively; reasons noted
- [ ] `codefix skill learn FILE` exit 0
- [ ] `codefix knowledge validate` and `codefix knowledge test` exit 0
- [ ] user told what was added and how to disable/remove it
