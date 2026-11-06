# Configuration (`codefix.toml`)

Found by searching upwards from the first path argument (or `--config`). Unknown keys produce
warnings; invalid values are errors (exit 2). See `codefix.toml.example`.

| Key | Default | Meaning |
|---|---|---|
| `project.exclude` | `[]` | fnmatch patterns (relative path or basename) |
| `behavior.formatter` | `none` | `ruff` / `auto` (Ruff, then Black), `black`, `none` |
| `behavior.allow_risky` | `false` | `true` downgrades risky fixes from explicit approval to confirmation (tests still required) |
| `behavior.minimum_confidence` | `0.80` | findings below this are report-only |
| `behavior.require_tests_for_risky` | `true` | risky fixes are skipped unless `[tests] command` is set, and must pass |
| `behavior.backup_before_write` | `true` | `false` disables `undo` (rollback keeps the original in memory) |
| `behavior.rollback_on_failure` | `true` | restore after failing tests |
| `behavior.max_passes` | `3` | re-analysis passes for `autofix` |
| `knowledge.packs` / `paths` | `["python"]` / `[]` | packs: `python django fastapi flask security project` |
| `rules.disabled` / `blocked` | `[]` | disabled rules do not run; blocked rules are reported but never applied |
| `metrics.*` | 50 / 10 / 5 / 6 / 30 / 4 / 4 | `max_function_lines`, `max_complexity`, `max_params`, `max_returns`, `max_calls`, `max_nesting`, `min_duplicate_statements` |
| `tests.command` | `""` | e.g. `python -m pytest`; pytest flags are added only for pytest commands |
| `tests.run_after_write` | `true` | `timeout` (seconds) also available |
| `security.minimum_confidence` | `0.95` | stricter threshold for `security` findings |
| `security.allow_autofix` | `false` | even when `true`, security fixes need explicit approval |
| `plugins.modules` | `[]` | importable modules exposing `register(registry)` or `RULES` |

Decision table: `blocked`/`rules.blocked` → BLOCK; not fixable → report; confidence below threshold →
report; `ai` → explicit; `security` → report (or explicit if allowed); `safe` → auto; `review` →
confirm; `risky` → explicit (confirm if `allow_risky`).
