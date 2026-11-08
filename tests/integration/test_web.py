import json
import threading
import urllib.error
import urllib.request

import pytest

from codefix.web.server import is_loopback, main as web_main, make_server
from codefix.web.service import WebError, WebService

BUGGY = "import os\n\ndef add(i, b=[]):\n    b.append(i)\n    if i == None:\n        return\n    return b\n"


@pytest.fixture
def svc(tmp_path):
    return WebService([tmp_path])


# ---------------------------------------------------------------- pasted code
def test_analyze_text_returns_policy_actions(svc):
    r = svc.analyze_text(BUGGY)
    acts = {f["rule_id"]: f["action"] for f in r["findings"]}
    assert acts["COMPARE_NONE"] == "auto" and acts["UNUSED_IMPORT"] == "confirm" and acts["MUTABLE_DEFAULT"] == "explicit"
    assert all("key" in f for f in r["findings"])


def test_fix_text_safe_only_then_selected(svc):
    r = svc.fix_text(BUGGY, apply_safe=True)
    assert r["ok"] and "i is None" in r["code"] and "import os" in r["code"] and "b=[]" in r["code"]
    sel = [f["key"] for f in svc.analyze_text(r["code"])["findings"] if f["rule_id"] in ("UNUSED_IMPORT", "MUTABLE_DEFAULT")]
    r2 = svc.fix_text(r["code"], selected=sel, apply_safe=False)
    assert "import os" not in r2["code"] and "b=None" in r2["code"] and r2["diff"].startswith("--- a/pasted.py")
    assert r2["remaining"] is not None


def test_fix_text_nothing_selected_changes_nothing(svc):
    r = svc.fix_text(BUGGY, selected=[], apply_safe=False)
    assert r["ok"] and not r["changed"] and r["code"] == BUGGY


def test_fix_text_repeat_until_stable(svc):
    assert svc.fix_text("if a == None:\n    pass\n", repeat=True)["code"] == "if a is None:\n    pass\n"


def test_fix_text_never_applies_report_only(svc):
    code = "def f():\n    return undefined_name\n"
    assert svc.fix_text(code, selected=["UNDEFINED_NAME:2:12"])["code"] == code


def test_fix_text_syntax_error_and_limits(svc):
    r = svc.fix_text("def f(:\n")
    assert not r["ok"] and "syntax error" in r["error"] and r["code"] == "def f(:\n"
    with pytest.raises(WebError):
        svc.analyze_text("x" * 2_100_000)
    with pytest.raises(WebError):
        svc.analyze_text(123)


def test_pasted_code_never_touches_disk(svc, tmp_path):
    svc.fix_text(BUGGY)
    svc.analyze_text(BUGGY)
    assert list(tmp_path.iterdir()) == []


def test_fix_text_refuses_when_validation_fails(svc, monkeypatch):
    from codefix.web import service
    monkeypatch.setattr(service, "validate_change", lambda *a, **k: (False, "forced"))
    r = svc.fix_text(BUGGY)
    assert not r["ok"] and r["code"] == BUGGY


# ---------------------------------------------------------------------- paths
def test_sandbox(svc, tmp_path):
    (tmp_path / "a.py").write_text(BUGGY)
    assert svc.resolve("a.py") == tmp_path / "a.py"
    for bad in ["/etc/passwd", "../outside.py", "", "   "]:
        with pytest.raises(WebError):
            svc.resolve(bad)
    (tmp_path.parent / "outside_link.py").write_text("x")
    (tmp_path / "link.py").symlink_to(tmp_path.parent / "outside_link.py")
    with pytest.raises(WebError) as e:
        svc.resolve("link.py")                              # symlinks cannot escape the sandbox
    assert e.value.status == 403


def test_ls_review_preview_apply_undo(svc, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(BUGGY)
    (tmp_path / ".venv").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    ls = svc.ls(None)
    assert [e["name"] for e in ls["entries"]] == ["src"]
    assert [e["name"] for e in svc.ls("src")["entries"]] == ["app.py"] and svc.ls("src")["parent"] == str(tmp_path)
    rv = svc.review_path("src")
    assert {r["rule_id"] for r in rv["rules"]} >= {"COMPARE_NONE", "UNUSED_IMPORT", "MUTABLE_DEFAULT"}
    prev = svc.fix_path("src", rules=["UNUSED_IMPORT"], apply_safe=True, write=False)
    assert prev["files"][0]["status"] == "dry-run" and "-import os" in prev["files"][0]["changeset"]["diff"]
    assert (tmp_path / "src" / "app.py").read_text() == BUGGY
    with pytest.raises(WebError, match="confirmation"):
        svc.fix_path("src", rules=[], apply_safe=True, write=True)
    with pytest.raises(WebError, match="nothing selected"):
        svc.fix_path("src", rules=[], apply_safe=False, write=False)
    done = svc.fix_path("src", rules=["UNUSED_IMPORT"], apply_safe=True, write=True, confirm=True)
    f = done["files"][0]
    assert f["status"] == "written" and (tmp_path / ".codefix" / "backups").exists()
    text = (tmp_path / "src" / "app.py").read_text()
    assert "i is None" in text and "import os" not in text and "b=[]" in text       # risky rule not ticked: untouched
    assert svc.undo("src/app.py")["restored"] and (tmp_path / "src" / "app.py").read_text() == BUGGY


def test_risky_rule_needs_tests_configured_for_files(svc, tmp_path):
    (tmp_path / "a.py").write_text(BUGGY)
    r = svc.fix_path("a.py", rules=["MUTABLE_DEFAULT"], apply_safe=False, write=True, confirm=True)
    skipped = r["files"][0]["changeset"]["skipped"]
    assert any("requires passing tests" in s["reason"] for s in skipped)           # no [tests] command configured
    assert "b=[]" in (tmp_path / "a.py").read_text()


def test_failing_tests_roll_back_from_the_web(tmp_path):
    import sys
    (tmp_path / "codefix.toml").write_text(f'[tests]\ncommand = "{sys.executable} -c \\"import sys; sys.exit(1)\\""\n')
    (tmp_path / "a.py").write_text(BUGGY)
    r = WebService([tmp_path]).fix_path("a.py", rules=[], apply_safe=True, write=True, confirm=True)
    assert r["files"][0]["status"] == "rolled_back" and (tmp_path / "a.py").read_text() == BUGGY


# ------------------------------------------------------------------ knowledge
def test_knowledge_flow(svc, tmp_path):
    form = {"id": "USER_HASKEY", "message": "gone in py3", "bad": "d.has_key(k)", "good": "k in d", "negatives": ["k in d"]}
    learned = svc.kb_learn(form)
    assert learned["ok"] and learned["entry"]["match"] == {"type": "pattern", "pattern": "$d.has_key($k)", "replace": "$k in $d"}
    prev = svc.kb_preview({"entry": learned["entry"], "code": "if m.has_key(x):\n    pass\n"})
    assert prev["ok"] and prev["matches"][0]["line"] == 1 and prev["fixed"] == "if x in m:\n    pass\n"
    assert svc.kb_save({"entry": learned["entry"], "negatives": ["k in d"]}) == {"ok": True, "problems": []}
    assert any(r["id"] == "USER_HASKEY" and r["user"] for r in svc.kb_list()["rules"])
    assert any(f["rule_id"] == "USER_HASKEY" for f in svc.analyze_text("m.has_key(x)\n")["findings"])   # live immediately
    assert not svc.kb_save({"entry": learned["entry"]})["ok"]                                       # duplicate
    assert svc.kb_save({"entry": learned["entry"], "replace": True})["ok"]
    assert svc.kb_export()["rules"][0]["id"] == "USER_HASKEY"
    assert svc.kb_delete("USER_HASKEY")["ok"] and not svc.kb_delete("USER_HASKEY")["ok"]
    assert svc.kb_import(svc.kb_export())["imported"] == []


def test_kb_forms_for_every_rule_type(svc):
    cases = [
        {"id": "USER_CALL", "message": "m", "bad": "os.system(c)", "good": "run(c)", "match": {"type": "call", "name": "os.system", "replace_with": "run"}},
        {"id": "USER_IMP", "message": "m", "bad": "import imp", "match": {"type": "import", "module": "imp"}},
        {"id": "USER_RX", "message": "m", "bad": "TODO = 1", "match": {"type": "regex", "pattern": "TODO"}},
        {"id": "USER_PAT", "message": "m", "bad": "a == None", "good": "a is None", "match": {"type": "pattern", "pattern": "$a == None", "replace": "$a is None"}}]
    for c in cases:
        assert svc.kb_save(c)["ok"], c["id"]
    assert len([r for r in svc.kb_list()["rules"] if r["user"]]) == 4


def test_kb_errors_are_explained(svc):
    assert not svc.kb_learn({"id": "USER_X", "message": "m", "bad": "x = (", "good": "y"})["ok"]
    r = svc.kb_save({"id": "bad id", "message": "m", "bad": "x", "match": {"type": "call", "name": "x"}})
    assert not r["ok"] and "UPPER_SNAKE" in r["problems"][0]
    p = svc.kb_preview({"entry": {"id": "USER_Z"}, "code": "x"})
    assert not p["ok"]
    with pytest.raises(WebError):
        svc.kb_save({"id": "USER_Q"})


def test_feedback_is_advisory(svc):
    assert "advisory" in svc.feedback("COMPARE_NONE", "false_positive", "numpy")["note"]
    with pytest.raises(WebError):
        svc.feedback("X", "nope")
    assert svc.kb_list()["feedback"] == {"COMPARE_NONE": {"false_positive": 1}}


# ------------------------------------------------------------------- HTTP layer
@pytest.fixture
def server(tmp_path):
    srv = make_server("127.0.0.1", 0, [tmp_path])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def http(srv, path, body=None, token=True, headers=None, method=None):
    h = dict(headers or {})
    if token:
        h["X-CodeFix-Token"] = srv.token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url.rstrip("/") + path, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_index_page_has_token_and_security_headers(server):
    status, body, hdr = http(server, "/", token=False)
    assert status == 200 and server.token in body and "__CODEFIX_TOKEN__" not in body
    assert "default-src 'none'" in hdr["Content-Security-Policy"] and hdr["Cache-Control"] == "no-store"
    assert "https://" not in body and "http://" not in body                                   # no external resources
    for needle in ("<script src", "<link ", "cdn."):
        assert needle not in body


def test_api_requires_token_host_and_same_origin(server):
    assert http(server, "/api/state", token=False)[0] == 403
    assert http(server, "/api/state", headers={"X-CodeFix-Token": "wrong"}, token=False)[0] == 403
    assert http(server, "/api/state", headers={"Host": "evil.example"})[0] == 403               # DNS rebinding
    assert http(server, "/api/analyze", {"code": "x"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert http(server, "/", headers={"Host": "evil.example"}, token=False)[0] == 403
    assert http(server, "/api/state")[0] == 200


def test_api_flow_and_errors(server, tmp_path):
    s, body, _ = http(server, "/api/analyze", {"code": BUGGY})
    assert s == 200 and json.loads(body)["findings"]
    s, body, _ = http(server, "/api/fix", {"code": BUGGY, "apply_safe": True})
    assert json.loads(body)["code"].count("is None") == 1
    assert http(server, "/api/nope", {})[0] == 404 and http(server, "/nope", token=False)[0] == 404
    s, body, _ = http(server, "/api/ls", {"path": "/etc"})
    assert s == 403 and "outside the allowed" in json.loads(body)["error"]
    req = urllib.request.Request(server.url + "api/analyze", data=b"not json", headers={"X-CodeFix-Token": server.token}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400
    big = urllib.request.Request(server.url + "api/analyze", data=b"{" + b" " * 9_000_000 + b"}", headers={"X-CodeFix-Token": server.token}, method="POST")
    with pytest.raises((urllib.error.HTTPError, urllib.error.URLError, ConnectionError)) as e:
        urllib.request.urlopen(big)                       # refused before the body is read (client may see a reset)
    assert getattr(e.value, "code", 413) == 413
    assert http(server, "/api/state")[0] == 200                                              # still serving


def test_http_write_requires_confirm(server, tmp_path):
    (tmp_path / "a.py").write_text(BUGGY)
    s, body, _ = http(server, "/api/fix_path", {"path": "a.py", "apply_safe": True, "write": True})
    assert s == 400 and (tmp_path / "a.py").read_text() == BUGGY
    s, body, _ = http(server, "/api/fix_path", {"path": "a.py", "apply_safe": True, "write": True, "confirm": "yes"})
    assert s == 400                                                                          # must be literally true
    s, body, _ = http(server, "/api/fix_path", {"path": "a.py", "apply_safe": True, "write": True, "confirm": True})
    assert s == 200 and "i is None" in (tmp_path / "a.py").read_text()


def test_server_port_fallback_and_remote_refusal(tmp_path, capsys):
    a = make_server("127.0.0.1", 0, [tmp_path])
    b = make_server("127.0.0.1", a.port, [tmp_path])
    try:
        assert b.port != a.port
    finally:
        a.server_close(); b.server_close()
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and not is_loopback("8.8.8.8")
    assert web_main(["--host", "8.8.8.8", "--no-browser"]) == 2 and "non-loopback" in capsys.readouterr().err
    assert web_main(["--root", str(tmp_path / "missing"), "--no-browser"]) == 2


def test_codefix_web_subcommand_routes_to_server(tmp_path, capsys):
    from codefix.cli.main import main
    assert main(["web", "--root", str(tmp_path / "missing")]) == 2
    assert "not a folder" in capsys.readouterr().err


def test_syntax_errors_come_with_context_and_hints(svc):
    code = "class A:\n    def f(self):\n        try:\n            x = 1\n        except Exception:\n            pass\n    else:\n        pass\n"
    for r in (svc.analyze_text(code), svc.fix_text(code)):
        sx = r["syntax"]
        assert sx["line"] == 7 and [c["n"] for c in sx["context"]][-1] == 8
        assert [c["n"] for c in sx["context"] if c["current"]] == [7]
        assert any("line up" in h for h in sx["hints"])
    assert svc.analyze_text("x = 1\n")["syntax"] is None


def test_syntax_hints_for_pasted_text_problems(svc):
    sx = svc.analyze_text("x = \u201chello\u201d\n")["syntax"]
    assert any("curly quote" in h for h in sx["hints"])
    sx = svc.analyze_text("foo(1, 2\nbar = 3\n")["syntax"]
    assert "never closed" in sx["message"] and any("missing closing bracket" in h for h in sx["hints"])
    sx = svc.analyze_text("if x:\n\ty = 1\n    z = 2\n")["syntax"]
    assert any("mixes tab" in h for h in sx["hints"])


# ------------------------------------------------------ skills / policy / layout (web)
SK = {"format": "codefix-skill", "name": "team-habits", "title": "T", "rules": [
    {"id": "TEAM_NO_EVAL", "message": "no eval", "bad": "eval(s)", "match": {"type": "call", "name": "eval"}},
    {"id": "TEAM_PRINT", "message": "logging", "bad": "print(x)", "good": "logger.info(x)",
     "match": {"type": "pattern", "pattern": "print($x)", "replace": "logger.info($x)"}}]}


def test_web_skill_lifecycle(svc):
    assert svc.skill_validate(SK)["ok"] and svc.skills_list()["skills"] == []
    assert svc.skill_learn(SK)["learned"]
    assert not svc.skill_learn(SK)["learned"] and svc.skill_learn(SK, True)["learned"]
    s = svc.skills_list()["skills"][0]
    assert s["name"] == "team-habits" and s["enabled"] and len(s["rules"]) == 2
    assert any(f["rule_id"] == "TEAM_NO_EVAL" for f in svc.analyze_text("eval(s)\n")["findings"])
    assert svc.skill_try(SK, "print(1)\n")["fixed"] == "logger.info(1)\n"
    assert svc.skill_enable("team-habits", False)["ok"]
    assert not any(f["rule_id"] == "TEAM_NO_EVAL" for f in svc.analyze_text("eval(s)\n")["findings"])
    assert svc.skill_enable("team-habits", True)["ok"]
    assert svc.skill_export("team-habits")["name"] == "team-habits"
    with pytest.raises(WebError):
        svc.skill_export("ghost")
    assert svc.skill_rule_delete("team-habits", "TEAM_NO_EVAL")["ok"] and svc.skill_delete("team-habits")["ok"]


def test_web_rule_saved_into_chosen_skill_and_editable(svc):
    assert svc.skill_create("mine", "Mine")["ok"] and not svc.skill_create("mine")["ok"]
    form = {"skill": "mine", "id": "MINE_EVAL", "message": "no eval", "bad": "eval(s)", "match": {"type": "call", "name": "eval"}}
    assert svc.kb_save(form)["ok"]
    assert [r["skill"] for r in svc.kb_list()["rules"] if r["id"] == "MINE_EVAL"] == ["mine"]
    assert not svc.kb_save(form)["ok"]                                                      # exists
    assert svc.kb_save({**form, "message": "no eval, ever", "replace": True})["ok"]
    assert [r["message"] for r in svc.skills_list()["skills"][0]["rules"]] == ["no eval, ever"]
    assert not svc.kb_save({**form, "skill": "ghost"})["ok"]


def test_web_policy_roundtrip(svc, tmp_path):
    p = svc.policy_get()
    assert {r["id"]: r["effective"] for r in p["rules"]}["COMPARE_NONE"] == "auto"
    saved = svc.policy_save({"COMPARE_NONE": {"mode": "report_only"}, "UNUSED_IMPORT": {"severity": "safe", "exclude": ["tests/*"]}},
                            {"allow_risky": True, "minimum_confidence": 0.9})
    rows = {r["id"]: r for r in saved["rules"]}
    assert rows["COMPARE_NONE"]["effective"] == "report_only" and rows["UNUSED_IMPORT"]["effective"] == "auto"
    assert saved["behavior"] == {"allow_risky": True, "minimum_confidence": 0.9}
    f = svc.analyze_text("import os\nif a == None:\n    pass\n")["findings"]
    assert {x["rule_id"]: x["action"] for x in f}["COMPARE_NONE"] == "report_only"
    assert svc.fix_text("if a == None:\n    pass\n")["changed"] is False                    # never fixed any more
    with pytest.raises(WebError, match="unknown rule"):
        svc.policy_save({"NOPE": {"mode": "disabled"}}, None)
    with pytest.raises(WebError):
        svc.policy_save({"COMPARE_NONE": {"mode": "weird"}}, None)
    with pytest.raises(WebError):
        svc.policy_save("x", None)
    assert svc.policy_reset(None)["rules"][0]["customised"] is False
    assert svc.fix_text("if a == None:\n    pass\n")["changed"] is True
    assert (tmp_path / "codefix.policy.json").exists()


def test_http_skill_and_policy_endpoints(server, tmp_path):
    s, body, _ = http(server, "/api/skills/learn", {"data": SK})
    assert s == 200 and json.loads(body)["learned"]
    s, body, _ = http(server, "/api/skills")
    assert json.loads(body)["skills"][0]["name"] == "team-habits"
    s, body, _ = http(server, "/api/policy/save", {"rules": {"COMPARE_NONE": {"mode": "disabled"}}, "behavior": {}})
    assert s == 200 and (tmp_path / "codefix.policy.json").exists()
    s, body, _ = http(server, "/api/policy/save", {"rules": {"COMPARE_NONE": {"mode": "bad"}}})
    assert s == 400
    assert http(server, "/api/skills/learn", {"data": SK}, token=False)[0] == 403


def test_page_layout_has_separate_output_and_all_tabs(server):
    _, body, _ = http(server, "/", token=False)
    for needle in ('id="out-code"', "readonly", 'data-tab="policy"', 'id="sk-json"', 'id="p-table"', "Skills &amp; knowledge"):
        assert needle in body, needle
