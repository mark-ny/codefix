# Project knowledge pack

Drop `*.json` files here (same schema as `python/patterns.json`) to document project-specific
rules and conventions. Enable with `packs = ["python", "project"]`. Entries without an analyzer
are treated as documentation; `codefix knowledge validate` reports them as warnings.
