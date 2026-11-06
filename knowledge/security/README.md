# Security knowledge pack (extension point)

`rules.json` is intentionally an empty list: CodeFix ships **no** security rules.

Add your own organisation's rules here and implement them as analyzers with
`codefix.rules.security.security_rule` (or a plugin). Security rules are isolated from the
general rules and receive stricter policy defaults:

* never applied automatically, even when `severity` is `safe` (explicit approval required);
* minimum confidence `[security] minimum_confidence` (default 0.95);
* autofix disabled unless `[security] allow_autofix = true`, and then still explicit-approval only.

Each entry uses the normal knowledge schema with `"category": "security"`.
