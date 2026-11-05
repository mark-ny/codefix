# Codefix

A policy-aware automated code analysis and repair toolkit.

## Project layout

- `src/codefix/analysis/` — source analysis and diagnostics.
- `src/codefix/knowledge/` — knowledge loading and lookup.
- `src/codefix/policy/` — policies for safe, reviewable changes.
- `src/codefix/patching/` — patch generation and application.
- `src/codefix/verification/` — validation of proposed changes.
- `src/codefix/rules/` — analysis and repair rules.
- `src/codefix/cli/` — command-line interface.
- `knowledge/` — Python, framework, security, and project references.
- `tests/`, `tools/`, and `docs/` — tests, development utilities, and documentation.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```
