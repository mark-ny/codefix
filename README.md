# CodeFix

A safe, explainable Python **code review, correction, refactoring and verification** tool.
Standard library only at its core (Python 3.10+); Ruff, Black, pytest and LibCST are optional.

> Analyzers never write to disk. Every change is a structured edit that is syntax-validated,
> policy-checked, diffed, backed up, written atomically, tested and — if tests fail — rolled back.

## Installation

```bash
pip install .                    # core (no third-party runtime dependencies on 3.11+)
pip install ".[all]"             # + ruff, black, pytest, libcst
pip install -e ".[dev]"          # development install
codefix --version
```

On Python 3.10 the only dependency is `tomli` (installed automatically). The wheel bundles the
knowledge packs; to use your own, set `[knowledge] paths` or `CODEFIX_KNOWLEDGE_DIR`.

Quick start:

```bash
codefix review src/                 # analyse, never writes
codefix fix src/app.py              # dry run: shows the diff only
codefix fix src/app.py --write      # interactive approval, backup, tests, rollback
codefix autofix src/ --write        # only safe + high-confidence fixes, no prompts
codefix undo src/app.py
```

## Command reference

Every command accepts `--config FILE`, `--json`, `--no-color`, `-v`. Human text (including diffs)
goes to stdout, or to stderr when `--json`/`--sarif` is used so stdout stays machine-readable.
Paths may be files or directories (recursive; `.git`, `.venv`, `venv`, `__pycache__`, `build`,
`dist`, `node_modules`, tool caches, `*.egg-info` and `[project] exclude` are skipped; files are
processed in sorted order and the run continues after per-file errors).

| Command | Purpose |
|---|---|
| `review PATH... [--json] [--sarif]` | Analyse; exit 1 if findings, 2 on errors |
| `check PATH...` | Syntax (compile) check and formatter check; never writes |
| `fix PATH... [--write] [--yes] [--ci] [--approve-rule ID] [--rule ID] [--passes N] [--no-format]` | Apply fixes with approval. **Dry run unless `--write`** |
| `autofix PATH... [--write] [--safe-only] [--passes N]` | Apply only `safe`, high-confidence fixes; never prompts |
| `approve PATH... [--write] [--approve-rule ID] [--yes]` | Review pending (non-auto) fixes; every one needs typed `approve` |
| `undo FILE [--list]` | Restore the latest unused backup (earlier backups on repeated calls) |
| `format PATH... [--write]` | Run Ruff (Black fallback) in memory, validate, diff |
| `test [TARGET...] [--json]` | Run pytest (or `[tests] command`) with failure attribution |
| `diagnostics PATH... [--metrics] [--json] [--sarif]` | Project-wide findings and complexity metrics |
| `rename FILE OLD NEW [--scope Q] [--engine auto\|ast\|libcst] [--write]` | Scope-aware single-file rename |
| `replace FILE FUNC --from-file F [--write]` | Replace one function, keeping decorators/indent/comments |
| `stub FILE FUNC [--write]` | Replace a body with `raise NotImplementedError` (docstring kept) or add a new stub |
| `apply-proposal FILE.json [--write ...]` | Run an AI/human structured patch proposal through the same gate |
| `knowledge list\|show ID\|validate\|test\|feedback` | Inspect, validate and test the rule knowledge base |
| `project init\|info` | Write a starter `codefix.toml` / show effective config |

**Exit codes:** `0` clean/success · `1` findings or pending changes · `2` errors (unreadable or
syntax-error file, verification failure, test failure/rollback, bad config).

**Approval ladder** (see `docs/configuration.md`):

| Severity | Policy |
|---|---|
| `safe` + confidence ≥ minimum | applied automatically (`autofix`, `fix`) |
| `review` | confirmation (`y/n/a/r/q`, or `--yes`) |
| `risky` | explicit approval (`--approve-rule ID` or typing `approve`); tests required |
| `blocked` | never applied |
| low confidence / no fix | reported only |
| `security` category | never automatic; disabled by default; 0.95 minimum confidence |

Interactive prompt:

```text
Finding 1 of 3
File: app/parser.py
Line: 42
Rule: COMPARE_NONE
Severity: safe
Message: Use `is None` instead of `== None`

Apply this change? [y]es [n]o [a]ll [r]eject-all [q]uit:
```

`a` applies this rule everywhere, `r` rejects it everywhere (this run only), `q` stops. `--yes`
accepts review-level fixes; `--ci` (or `CI=true`, or no TTY) never prompts.

## Built-in rules (23)

Correctness: `SYNTAX_ERROR`, `UNDEFINED_NAME`, `UNUSED_IMPORT`✎, `SHADOW_BUILTIN`, `UNREACHABLE_CODE`,
`MUTABLE_DEFAULT`✎(risky), `BARE_EXCEPT`✎, `EXCEPT_PASS`, `COMPARE_NONE`✎, `COMPARE_LITERAL`✎,
`ASSERT_TUPLE`✎, `TYPE_COMPARE`✎, `RESOURCE_NO_CONTEXT`, `INCONSISTENT_RETURN`✎.
Maintainability: `EXCESSIVE_NESTING`, `FUNCTION_TOO_LONG`, `HIGH_COMPLEXITY`, `TOO_MANY_PARAMS`,
`TOO_MANY_RETURNS`, `TOO_MANY_CALLS`, `DUPLICATE_CODE`. Performance: `STRING_CONCAT_LOOP`.
Style: `MISSING_DOCSTRING`. (✎ = has an autofix.) Details: `codefix knowledge show ID` or `docs/rules.md`.
Suppress one line with `# codefix: ignore` or `# codefix: ignore[RULE_ID,...]`.

**No security rules ship.** `rules/security.py` and `knowledge/security/` are clean extension points.

## Safety guarantees

1. Analysis, knowledge, policy, patching, verification and CLI are separate layers; analyzers only *return* findings.
2. Edits are byte-offset replacements (UTF-8 AST offsets handled correctly); overlapping, stale or out-of-range edits are rejected.
3. The result must **parse and compile**, keep every function/class definition (unless the refactor declares the removal), and not introduce new `UNDEFINED_NAME`/syntax findings. A bad edit is dropped individually; if nothing valid remains nothing is written.
4. Encoding (incl. `# coding:` and BOM), line endings (CRLF/LF), permissions and symlinks are preserved. Lone-CR files and results that cannot be encoded are refused.
5. Writes are atomic (temp file + `fsync` + `os.replace` + directory fsync) and preceded by a checksummed, timestamped backup under `.codefix/backups/`. The file is re-checked just before writing; if it changed on disk, nothing is written.
6. Tests run after each write (when configured); on failure the original is restored automatically (`rollback_on_failure`, default on). If rollback is disabled, or fails, the report says so loudly.
7. Formatter output is validated in memory; invalid or failing formatter output is discarded, never written.
8. Feedback is advisory only: nothing silently changes or disables rules.
9. `--write` is always required to modify anything; dry runs create no backups and touch no files.

## AST vs LibCST

| | Standard-library `ast` (always available) | LibCST (optional) |
|---|---|---|
| Analysis rules | All 23 rules | Not used (rules stay AST-based) |
| Autofix edits | Byte-exact text replacement; comments/formatting untouched by construction | n/a |
| `rename` | Own scope analysis; handles params, defs, classes, globals, except/match names, import aliases; verifies each position's text | `ScopeProvider`-based, formatting-preserving; chosen by `--engine auto` when installed, falls back to AST if it declines |
| Known gaps | f-string names before Python 3.12 are skipped with a warning | Same semantic limits |

Neither engine renames attribute accesses (`obj.old`), keyword arguments, strings, or other files;
both report this explicitly. Name collisions and ambiguous scopes are refused.

## Known limitations / incomplete features

* **Stage 4 not complete:** *import organization* and *extract-function refactoring* are **not implemented**. LibCST is used for `rename` only.
* **Stage 5 (AI layer) is a gate, not a model:** `apply-proposal` validates structured proposals through parse/policy/diff/backup/tests; no model or network client is bundled.
* Name analysis is not flow-sensitive: use-before-assignment and conditional definitions are not reported; `exec`/`eval` lower confidence.
* `DUPLICATE_CODE` finds exact structural duplicates within one file only; `TOO_MANY_CALLS` counts call expressions.
* `rename` is single-file; callers in other files are not updated.
* Tests run after every written file (no batch "after all files" mode); each run is a full test command.
* Removing imports can leave blank lines at the top of a file (run a formatter). The formatter reformats the whole file when used with `fix`.
* `MUTABLE_DEFAULT` keeps type annotations unchanged (consider `list | None`).
* Backups are never pruned automatically.
* Developed and tested on Linux with Python 3.12 only. Python 3.10/3.11 and Windows are supported in code (guarded syntax, `tomli` fallback, no POSIX-only calls without guards) but **not tested**.
* SARIF output is basic (rules, results, locations; no fixes/code flows).

## Development

```bash
pip install -e ".[all]"
python -m pytest                      # 248 tests
python tools/validate_knowledge.py    # knowledge packs + executable examples
python tools/generate_rule_report.py  # docs/rules.md
```

Documentation: `docs/architecture.md`, `docs/configuration.md`, `docs/rule-authoring.md`,
`docs/plugin-development.md`, `docs/migration.md`.
