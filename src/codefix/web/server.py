"""Tiny local HTTP server for the CodeFix web interface (standard library only)."""
from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .service import WebError, WebService

MAX_BODY = 8_000_000
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def load_index() -> str:
    return resources.files("codefix.web").joinpath("index.html").read_text(encoding="utf-8")


class CodeFixServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], service: WebService):
        self.service = service
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()          # one disk-changing operation at a time
        super().__init__(addr, Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        host = self.server_address[0]
        return f"http://{'127.0.0.1' if host in ('', '0.0.0.0') else host}:{self.port}/"


class Handler(BaseHTTPRequestHandler):
    server: CodeFixServer
    server_version = "CodeFix"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    # ----------------------------------------------------------------- plumbing
    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        names = {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}", f"[::1]:{self.server.port}"}
        bound = self.server.server_address[0]
        if bound not in LOOPBACK and bound not in ("", "0.0.0.0"):
            names.add(f"{bound}:{self.server.port}")
        if bound in ("", "0.0.0.0"):
            return True                                  # explicit --allow-remote: token still required
        return host in names

    def _guard(self, api: bool) -> bool:
        if not self._host_ok():                          # blocks DNS-rebinding
            self._json(403, {"error": "bad Host header"})
            return False
        if api:
            if not secrets.compare_digest(self.headers.get("X-CodeFix-Token", ""), self.server.token):
                self._json(403, {"error": "missing or wrong session token; reload the page"})
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                self._json(403, {"error": "cross-origin request refused"})
                return False
        return True

    def _body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise WebError("bad Content-Length")
        if n > MAX_BODY:
            raise WebError("request too large", 413)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise WebError("request body is not valid JSON")
        if not isinstance(data, dict):
            raise WebError("request body must be a JSON object")
        return data

    # ------------------------------------------------------------------- routes
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not self._guard(False):
                return
            html = load_index().replace("__CODEFIX_TOKEN__", self.server.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                          "connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'"})
            return
        if path.startswith("/api/") and self._guard(True):
            self._api("GET", path, {})
            return
        if not path.startswith("/api/"):
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/") or not self._guard(True):
            if not path.startswith("/api/"):
                self._json(404, {"error": "not found"})
            return
        try:
            body = self._body()
        except WebError as exc:
            self._json(exc.status, {"error": str(exc)})
            return
        self._api("POST", path, body)

    def _api(self, method: str, path: str, b: dict[str, Any]) -> None:
        svc = self.server.service
        try:
            routes = {
                ("GET", "/api/state"): lambda: svc.state(),
                ("POST", "/api/analyze"): lambda: svc.analyze_text(b.get("code", "")),
                ("POST", "/api/fix"): lambda: svc.fix_text(b.get("code", ""), b.get("selected") or [],
                                                           bool(b.get("apply_safe", True)), bool(b.get("repeat")),
                                                           bool(b.get("format"))),
                ("POST", "/api/ls"): lambda: svc.ls(b.get("path")),
                ("POST", "/api/review_path"): lambda: svc.review_path(b.get("path", "")),
                ("POST", "/api/fix_path"): lambda: self._locked(lambda: svc.fix_path(
                    b.get("path", ""), rules=b.get("rules") or [], apply_safe=bool(b.get("apply_safe")),
                    write=bool(b.get("write")), confirm=b.get("confirm") is True)),
                ("POST", "/api/undo"): lambda: self._locked(lambda: svc.undo(b.get("path", ""))),
                ("POST", "/api/rule"): lambda: svc.rule_details(b.get("id", "")),
                ("GET", "/api/kb/list"): lambda: svc.kb_list(),
                ("POST", "/api/kb/learn"): lambda: svc.kb_learn(b),
                ("POST", "/api/kb/preview"): lambda: svc.kb_preview(b),
                ("POST", "/api/kb/save"): lambda: self._locked(lambda: svc.kb_save(b)),
                ("POST", "/api/kb/delete"): lambda: self._locked(lambda: svc.kb_delete(b.get("id", ""))),
                ("GET", "/api/kb/export"): lambda: svc.kb_export(),
                ("POST", "/api/kb/import"): lambda: self._locked(lambda: svc.kb_import(b.get("data"), bool(b.get("replace")))),
                ("GET", "/api/skills"): lambda: svc.skills_list(),
                ("POST", "/api/skills/create"): lambda: self._locked(lambda: svc.skill_create(b.get("name", ""), b.get("title", ""), b.get("description", ""))),
                ("POST", "/api/skills/validate"): lambda: svc.skill_validate(b.get("data")),
                ("POST", "/api/skills/learn"): lambda: self._locked(lambda: svc.skill_learn(b.get("data"), bool(b.get("replace")))),
                ("POST", "/api/skills/enable"): lambda: self._locked(lambda: svc.skill_enable(b.get("name", ""), bool(b.get("enabled")))),
                ("POST", "/api/skills/delete"): lambda: self._locked(lambda: svc.skill_delete(b.get("name", ""))),
                ("POST", "/api/skills/export"): lambda: svc.skill_export(b.get("name", "")),
                ("POST", "/api/skills/try"): lambda: svc.skill_try(b.get("data"), b.get("code", "")),
                ("POST", "/api/skills/rule_delete"): lambda: self._locked(lambda: svc.skill_rule_delete(b.get("skill", ""), b.get("id", ""))),
                ("GET", "/api/policy"): lambda: svc.policy_get(),
                ("POST", "/api/policy/save"): lambda: self._locked(lambda: svc.policy_save(b.get("rules"), b.get("behavior"))),
                ("POST", "/api/policy/reset"): lambda: self._locked(lambda: svc.policy_reset(b.get("id"))),
                ("POST", "/api/feedback"): lambda: svc.feedback(b.get("rule_id", ""), b.get("verdict", ""), b.get("note", "")),
            }
            fn = routes.get((method, path))
            if fn is None:
                self._json(404, {"error": "unknown endpoint"})
                return
            self._json(200, fn())
        except WebError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # never leak a traceback page; report and keep serving
            self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def _locked(self, fn):
        with self.server.lock:
            return fn()


def make_server(host: str = "127.0.0.1", port: int = 8765, roots: list[Path] | None = None,
                config_file: str | None = None) -> CodeFixServer:
    last: OSError | None = None
    for p in ([port] if port == 0 else range(port, port + 20)):
        try:
            return CodeFixServer((host, p), WebService(roots, config_file))
        except OSError as exc:
            last = exc
    raise OSError(f"could not bind {host}:{port}..{port + 19}: {last}")


def is_loopback(host: str) -> bool:
    if host in LOOPBACK:
        return True
    try:
        return socket.gethostbyname(host).startswith("127.")
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codefix web", description="Open the CodeFix web interface in your browser.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="folder the page may read/modify (repeatable; default: current directory)")
    ap.add_argument("--config", help="path to codefix.toml")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--allow-remote", action="store_true", help="allow binding a non-loopback address (NOT recommended)")
    args = ap.parse_args(argv)
    if not is_loopback(args.host) and not args.allow_remote:
        sys.stderr.write("refusing to listen on a non-loopback address: the page can edit your files.\n"
                         "Use --allow-remote only if you understand the risk.\n")
        return 2
    roots = [Path(r) for r in args.root] or [Path.cwd()]
    for r in roots:
        if not r.is_dir():
            sys.stderr.write(f"--root {r} is not a folder\n")
            return 2
    try:
        server = make_server(args.host, args.port, roots, args.config)
    except OSError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    print(f"CodeFix is running at {server.url}")
    print(f"Allowed folders: {', '.join(str(r.resolve()) for r in roots)}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(server.url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
