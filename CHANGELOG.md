# Changelog

## 0.1.0 - Initial release

- Layered architecture: analysis, knowledge, policy, patching, verification, CLI.
- 25 built-in rules (AST based) with JSON knowledge metadata and executable examples.
- Structured edits, overlap detection, byte-accurate patching, CRLF/encoding/permission preservation.
- Atomic writes, timestamped backups, rollback (`codefix undo`).
- Policy engine driven by `codefix.toml`; isolated security extension point.
- Formatter integration (Ruff, Black fallback), pytest execution with failure attribution.
- Refactoring: rename (scope-aware AST, optional LibCST), replace, stub.
- JSON and SARIF output; AI proposal gate (no model bundled).
