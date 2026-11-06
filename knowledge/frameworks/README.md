# Framework packs

One JSON file per framework (`django.json`, `fastapi.json`, `flask.json`). Enable a pack in
`codefix.toml`:

```toml
[knowledge]
packs = ["python", "django"]
```

The shipped entries are **documentation-only** (`"implemented": false`): they describe the
pattern and the recommended fix, but no analyzer runs for them. To make one executable, write
a plugin rule with the same `id` (see `docs/plugin-development.md`); `codefix knowledge validate`
then cross-checks the JSON against the analyzer.
