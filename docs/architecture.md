# Architecture

```
cli/  ──►  application.py  ──►  analysis ─► knowledge(registry) ─► rules
                │                  │
                ├──► policy        └── models / source
                ├──► patching (engine, diff, atomic_write, backup, rollback)
                └──► verification (syntax, formatter, tests, attribution, regression)
```

| Layer | Responsibility | May write files? |
|---|---|---|
| `analysis/` | Parse, scope analysis (`symbols.py`), metrics (`complexity.py`), run rules (`ast_engine.py`), optional LibCST (`cst_engine.py`) | No |
| `knowledge/` | Rule registry, JSON packs, schema, validator, advisory feedback | Feedback log only |
| `rules/` | Rule implementations returning `Finding`s with `ProposedEdit`s | No |
| `policy/` | `PolicyEngine.decide(finding) → Decision(action, reason)`; file discovery | No |
| `patching/` | Apply edits, diff, atomic write, backups, rollback | Yes (only via `application`) |
| `verification/` | Syntax/structure validation, formatter, pytest + attribution, before/after comparison | No (runs subprocesses) |
| `application.py` | Orchestration and the write pipeline | Yes |
| `cli/` | argparse, rendering (text/JSON/SARIF) | No |

## Data flow of `fix --write`

1. Read file (`SourceFile`: encoding, BOM, newline, mode). Analyse; abort on syntax errors.
2. Policy decides each fixable finding (`AUTO/CONFIRM/EXPLICIT/REPORT_ONLY/BLOCK`).
3. `ApprovalSession` asks for confirmation where required.
4. `PatchEngine.apply_findings`: drop conflicting findings, apply bottom-to-top, validate; on failure retry findings individually.
5. Repeat for `--passes`. Run formatter in memory (validated). Compare findings before/after.
6. Show diff. Re-check the file is unchanged on disk. Back up, atomic write.
7. Run tests; on failure restore the backup.

## Models

`Location`, `Finding`, `ProposedEdit` (UTF-8 byte offsets + `expected` text), `RuleSpec`,
`AnalysisResult`, `ChangeSet`, `VerificationResult`, `ProjectConfig`, `Decision` live in `models.py`.

## AI proposals

`ai.py` converts `{"proposals":[{"file","description","confidence","edits":[{"start_line","end_line","text"}]}]}`
into findings of category `ai`. They are always `EXPLICIT` and `requires_tests`, and go through the
same pipeline. No model is called by CodeFix.
