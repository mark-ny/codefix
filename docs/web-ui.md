# Web interface

```bash
pip install -e ".[all]"      # or: pip install ".[all]"
codefix web                  # opens http://127.0.0.1:8765/ in your browser
```

Equivalent launchers (all run the same server): `codefix-ui`, `python -m codefix.web`, and `index.html`
(a console script installed next to `codefix`; works on Linux/macOS when your venv's `bin` is on `PATH`.
On Windows a bare `index.html` may be handled by the file association instead, so prefer `codefix web`).
The page itself is `src/codefix/web/index.html`: one self-contained file, no CDN, works offline.

Options: `--port N` (default 8765; the next free port is used if busy), `--root DIR` (repeatable: folders the
page may read/modify; default = the directory you launched from), `--config FILE`, `--no-browser`.

## Tabs

* **Paste code** – paste, **Analyze**, tick the fixes you want, **Fix selected + safe**. Safe fixes can be applied
  automatically (checkbox); review-level and risky fixes only when ticked. The result replaces the text box,
  **Undo** steps back, a diff and the verification summary are shown. Pasted code never touches disk.
* **Files & folders** – enter a path (or Browse) → **Review** → choose which rules to fix → **Preview** (dry run) or
  **Apply** (confirmation → backup → atomic write → your tests → automatic rollback on failure; **Undo** restores).
* **Teach CodeFix** – add your own rules (see `docs/expanding-knowledge.md`).
* **Help** – badges and safety notes.

## Security model

* Listens on `127.0.0.1` only; a non-loopback `--host` is refused without `--allow-remote`.
* Every API call needs a per-session random token (embedded in the page) and a matching `Host`/`Origin`
  (blocks other websites and DNS-rebinding attacks).
* File access is restricted to the allowed roots; symlinks cannot escape them.
* Writing needs `confirm: true`; the normal pipeline applies (policy, validation, diff, backup, atomic write,
  tests, rollback). Risky fixes on files still require `[tests] command`.
* Strict Content-Security-Policy (`default-src 'none'`), all dynamic text inserted with `textContent`.
* Requests are capped at 8 MB; pasted code at 2 MB.

## HTTP API (for scripting)

All endpoints are JSON; send `X-CodeFix-Token` (from the page's `<meta name="codefix-token">`).
`GET /api/state`, `POST /api/analyze {code}`, `/api/fix {code, selected[], apply_safe, repeat, format}`,
`/api/ls {path}`, `/api/review_path {path}`, `/api/fix_path {path, rules[], apply_safe, write, confirm}`,
`/api/undo {path}`, `/api/rule {id}`, `GET /api/kb/list`, `/api/kb/learn`, `/api/kb/preview`, `/api/kb/save`,
`/api/kb/delete`, `GET /api/kb/export`, `/api/kb/import`, `/api/feedback`.

## Testing the page

`pytest` covers the service and HTTP layers. `tools/ui_smoke_test.js` drives the real page in jsdom against a
running server (needs Node and `npm i jsdom`): start `codefix web --no-browser` in a scratch project with a
`src/app.py`, then `node tools/ui_smoke_test.js http://127.0.0.1:8765/`.
