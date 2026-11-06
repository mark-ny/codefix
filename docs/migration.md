# Migrating from a monolithic script

If your current tool is a single script that parses, "fixes" and writes files in one pass, move it in this order:

1. **Split analysis from writing.** Each check becomes a rule returning `Finding`s; remove every direct file write.
2. **Express each fix as an edit** (`ctx.edit(spec, start, end, text)`) instead of regenerating the file with
   `ast.unparse` — unparsing loses comments, formatting and decorators.
3. **Register the rule** with a `RuleSpec`; add its JSON entry and `bad/good/edge/negative` examples.
4. **Move thresholds and switches** into `codefix.toml` (`[metrics]`, `[rules]`, `[behavior]`).
5. **Let the pipeline write.** Delete your own backup/write/rollback code; use `fix`/`autofix`.
6. **Map old flags:** "auto-fix everything" → `autofix`; "interactive" → `fix`; "force" → `--approve-rule ID`
   (per rule — there is deliberately no global force); "restore" → `undo`.
7. **Verify parity:** run both tools in dry-run on a corpus; diff the findings (`review --json`).
8. **Adopt in CI:** `codefix review src/ --sarif > codefix.sarif`; fail on exit code 1/2.

Behaviour that intentionally differs: nothing is written without `--write`; risky and security fixes are
never automatic; invalid results are dropped instead of written; formatter failures never corrupt files.
