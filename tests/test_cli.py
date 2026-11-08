import json

import pytest

from codefix.cli.main import main

BUGGY = "import os\n\nif a == None:\n    pass\n"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n" + BUGGY)
    return tmp_path


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_no_command_prints_help(capsys):
    code, out, _ = run(capsys)
    assert code == 0 and "COMMAND" in out


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "codefix 0.1.0" in capsys.readouterr().out


def test_review_exit_codes_and_text(proj, capsys):
    code, out, _ = run(capsys, "review", "a.py")
    assert code == 1 and "COMPARE_NONE" in out and "[fixable]" in out and "finding(s)" in out
    (proj / "clean.py").write_text('"""m."""\nx = 1\n')
    assert run(capsys, "review", "clean.py")[0] == 0
    assert run(capsys, "review", "missing.py")[0] == 2


def test_review_sarif(proj, capsys):
    code, out, _ = run(capsys, "review", "a.py", "--sarif")
    doc = json.loads(out)
    assert doc["version"] == "2.1.0" and doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"]
    assert {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]} >= {"COMPARE_NONE", "UNUSED_IMPORT"}


def test_fix_dry_run_then_write_then_undo(proj, capsys):
    original = (proj / "a.py").read_text()
    code, out, _ = run(capsys, "fix", "a.py", "--yes")
    assert code == 1 and "+if a is None:" in out and "Dry run" in out and (proj / "a.py").read_text() == original
    code, out, _ = run(capsys, "fix", "a.py", "--write", "--yes")
    text = (proj / "a.py").read_text()
    assert "a is None" in text and "import os" not in text and "backup:" in out
    code, out, _ = run(capsys, "undo", "a.py", "--list")
    assert "write" in out
    code, out, _ = run(capsys, "undo", "a.py")
    assert code == 0 and (proj / "a.py").read_text() == original
    assert run(capsys, "undo", "a.py")[0] == 2


def test_autofix_is_safe_only_and_json(proj, capsys):
    code, out, err = run(capsys, "autofix", "a.py", "--write", "--safe-only", "--json")
    data = json.loads(out)
    assert data["files"][0]["status"] == "written" and data["summary"]["changes"] == 1
    assert "import os" in (proj / "a.py").read_text() and "a is None" in (proj / "a.py").read_text()
    assert "---" in err                       # human diff goes to stderr in JSON mode, stdout stays pure JSON


def test_approve_requires_explicit_rule_when_not_interactive(proj, capsys):
    code, out, _ = run(capsys, "approve", "a.py", "--write", "--yes")
    assert "import os" in (proj / "a.py").read_text()          # --yes does not satisfy explicit approval
    run(capsys, "approve", "a.py", "--write", "--approve-rule", "UNUSED_IMPORT")
    assert "import os" not in (proj / "a.py").read_text()


def test_syntax_error_file(proj, capsys):
    (proj / "bad.py").write_text("def f(:\n")
    code, out, _ = run(capsys, "review", "bad.py")
    assert code == 1 and "SYNTAX_ERROR" in out
    code, out, _ = run(capsys, "fix", "bad.py", "--write")
    assert code == 2 and (proj / "bad.py").read_text() == "def f(:\n"


def test_diagnostics_metrics_json(proj, capsys):
    (proj / "m.py").write_text("def f(a):\n    if a:\n        return 1\n    return 2\n")
    code, out, _ = run(capsys, "diagnostics", ".", "--metrics", "--json")
    d = json.loads(out)
    assert d["command"] == "diagnostics" and d["summary"]["functions"] == 1
    assert d["summary"]["metrics"]["max_complexity"] == 2
    m_file = next(f for f in d["files"] if f["path"].endswith("m.py"))
    assert m_file["metrics"][0]["name"] == "f"
    code, out, _ = run(capsys, "diagnostics", ".", "--metrics")
    assert "complexity:" in out


def test_test_command_json(proj, capsys):
    (proj / "test_x.py").write_text("def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n")
    code, out, _ = run(capsys, "test", "--json")
    d = json.loads(out)
    assert code == 2 and not d["ok"] and d["summary"]["failed"] == 1 and d["failures"][0]["file"] == "test_x.py"
    assert run(capsys, "test", "test_x.py::test_ok", "--json")[0] == 0


def test_knowledge_commands(proj, capsys):
    code, out, _ = run(capsys, "knowledge", "list")
    assert code == 0 and "COMPARE_NONE" in out and "23 rule(s)" in out
    code, out, _ = run(capsys, "knowledge", "show", "COMPARE_NONE")
    assert "is None" in out and "Autofix: yes" in out
    assert run(capsys, "knowledge", "show", "NOPE")[0] == 2
    assert run(capsys, "knowledge", "validate")[0] == 0
    code, out, _ = run(capsys, "knowledge", "test")
    assert code == 0 and "example(s) passed" in out
    code, out, _ = run(capsys, "knowledge", "list", "--json", "--category", "performance")
    assert [r["id"] for r in json.loads(out)] == ["STRING_CONCAT_LOOP"]
    run(capsys, "knowledge", "feedback", "COMPARE_NONE", "--verdict", "false_positive")
    assert "false_positive" in run(capsys, "knowledge", "feedback")[1]


def test_rename_replace_stub_commands(proj, capsys):
    (proj / "r.py").write_text("def old():\n    return 1\n\nold()\n")
    code, out, _ = run(capsys, "rename", "r.py", "old", "new")
    assert code == 1 and "dry run" in out and "def old" in (proj / "r.py").read_text()
    assert run(capsys, "rename", "r.py", "old", "new", "--write")[0] == 0
    assert (proj / "r.py").read_text() == "def new():\n    return 1\n\nnew()\n"
    (proj / "rep.py").write_text("def new():\n    return 5\n")
    assert run(capsys, "replace", "r.py", "new", "--from-file", "rep.py", "--write")[0] == 0
    assert "return 5" in (proj / "r.py").read_text()
    assert run(capsys, "stub", "r.py", "new", "--write")[0] == 0
    assert "NotImplementedError" in (proj / "r.py").read_text()
    assert run(capsys, "rename", "r.py", "nope", "x")[0] == 2


def test_check_and_format_commands(proj, capsys):
    assert run(capsys, "check", "a.py")[0] == 0
    (proj / "bad.py").write_text("return 1\n")
    code, out, _ = run(capsys, "check", "bad.py", "--json")
    assert code == 1 and json.loads(out)["ok"] is False
    code, out, _ = run(capsys, "format", "a.py")
    assert code == 2 and "no formatter configured" in out


def test_config_errors_and_project_helpers(proj, capsys):
    (proj / "codefix.toml").write_text("[behavior]\nminimum_confidence = 7\n")
    code, _, err = run(capsys, "review", "a.py")
    assert code == 2 and "configuration error" in err
    (proj / "codefix.toml").unlink()
    assert run(capsys, "project", "init")[0] == 0 and (proj / "codefix.toml").exists()
    assert run(capsys, "project", "init")[0] == 2
    code, out, _ = run(capsys, "project", "info", "--json")
    assert json.loads(out)["minimum_confidence"] == 0.8


def test_apply_proposal_requires_approval(proj, capsys):
    (proj / "p.json").write_text(json.dumps({"proposals": [{"file": "a.py", "description": "d", "confidence": 0.9,
                                                            "edits": [{"start_line": 1, "end_line": 1, "text": "a = 2\n"}]}]}))
    code, out, _ = run(capsys, "apply-proposal", "p.json", "--write", "--yes")
    assert (proj / "a.py").read_text().startswith("a = 1")
    (proj / "codefix.toml").write_text('[tests]\ncommand = "python -c pass"\n')
    run(capsys, "apply-proposal", "p.json", "--write", "--approve-rule", "AI_PROPOSAL")
    assert (proj / "a.py").read_text().startswith("a = 2")


def test_teach_list_export_import_remove(proj, capsys):
    code, out, _ = run(capsys, "knowledge", "teach", "USER_HAS_KEY", "--bad", "d.has_key(k)", "--good", "k in d",
                       "--message", "has_key is gone", "--negative", "k in d")
    assert code == 0 and "learned pattern: $d.has_key($k)" in out and "self-test passed" in out
    (proj / "h.py").write_text("def f(m, key):\n    return m.has_key(key)\n")
    code, out, _ = run(capsys, "review", "h.py")
    assert "USER_HAS_KEY" in out and "[fixable]" in out
    assert run(capsys, "fix", "h.py", "--write", "--yes")[0] in (0, 1)
    assert "key in m" in (proj / "h.py").read_text()
    code, out, _ = run(capsys, "knowledge", "list", "--user")
    assert "USER_HAS_KEY" in out and "COMPARE_NONE" not in out
    assert run(capsys, "knowledge", "validate")[0] == 0 and run(capsys, "knowledge", "test")[0] == 0
    assert run(capsys, "knowledge", "export", "-o", "pack.json")[0] == 0
    assert run(capsys, "knowledge", "remove", "USER_HAS_KEY")[0] == 0 and run(capsys, "knowledge", "remove", "USER_HAS_KEY")[0] == 2
    code, out, _ = run(capsys, "knowledge", "import", "pack.json")
    assert code == 0 and "USER_HAS_KEY" in out
    assert run(capsys, "knowledge", "import", "pack.json")[0] == 2                    # duplicate refused
    assert run(capsys, "knowledge", "import", "missing.json")[0] == 2


def test_teach_refuses_bad_examples_and_dry_run(proj, capsys):
    code, out, err = run(capsys, "knowledge", "teach", "USER_X", "--bad", "x = 1", "--good", "x = 1", "--message", "m")
    assert code == 2 and "identical" in err
    code, out, _ = run(capsys, "knowledge", "teach", "USER_X", "--bad", "eval(s)", "--message", "m", "--dry-run")
    assert code == 0 and not (proj / "knowledge").exists()
    assert run(capsys, "knowledge", "teach", "USER_X", "--message", "m")[0] == 2


SKILL_FILE = {"format": "codefix-skill", "name": "team-habits", "title": "Team habits", "rules": [
    {"id": "TEAM_NO_PRINT", "message": "use logging", "bad": "print(x)", "good": "logger.info(x)",
     "match": {"type": "pattern", "pattern": "print($x)", "replace": "logger.info($x)"}},
    {"id": "TEAM_NO_EVAL", "message": "eval banned", "severity": "risky", "bad": "eval(s)", "match": {"type": "call", "name": "eval"}}]}


def test_skill_commands_end_to_end(proj, capsys):
    (proj / "team.json").write_text(json.dumps(SKILL_FILE))
    (proj / "t.py").write_text("def f(s):\n    print(s)\n    return eval(s)\n")
    code, out, _ = run(capsys, "skill", "validate", "team.json")
    assert code == 0 and "2 rule(s); valid" in out and not (proj / "knowledge").exists()
    code, out, _ = run(capsys, "skill", "try", "team.json", "--code-file", "t.py")
    assert code == 0 and "TEAM_NO_PRINT" in out and "+    logger.info(s)" in out
    code, out, _ = run(capsys, "skill", "learn", "team.json", "--json")
    assert code == 0 and json.loads(out)["learned"] is True
    code, out, _ = run(capsys, "skill", "learn", "team.json")
    assert code == 2 and "already exists" in out
    assert run(capsys, "skill", "learn", "team.json", "--replace")[0] == 0
    code, out, _ = run(capsys, "skill", "list")
    assert "team-habits" in out and "2 rule(s)" in out
    code, out, _ = run(capsys, "review", "t.py")
    assert "TEAM_NO_PRINT" in out and "TEAM_NO_EVAL" in out
    assert run(capsys, "skill", "show", "team-habits")[0] == 0 and run(capsys, "skill", "show", "ghost")[0] == 2
    assert run(capsys, "skill", "disable", "team-habits")[0] == 0
    assert "TEAM" not in run(capsys, "review", "t.py")[1]
    assert run(capsys, "skill", "enable", "team-habits")[0] == 0
    assert run(capsys, "skill", "export", "team-habits", "-o", "out.json")[0] == 0 and json.loads((proj / "out.json").read_text())["name"] == "team-habits"
    assert run(capsys, "skill", "remove-rule", "team-habits", "TEAM_NO_EVAL")[0] == 0
    assert run(capsys, "knowledge", "validate")[0] == 0 and run(capsys, "knowledge", "test")[0] == 0
    assert run(capsys, "skill", "forget", "team-habits")[0] == 0 and run(capsys, "skill", "forget", "team-habits")[0] == 2


def test_skill_invalid_files_exit_nonzero_with_reasons(proj, capsys):
    bad = {"name": "bad-skill", "rules": [{"id": "COMPARE_NONE", "message": "m", "bad": "a", "match": {"type": "call", "name": "a"}}]}
    (proj / "bad.json").write_text(json.dumps(bad))
    code, out, _ = run(capsys, "skill", "validate", "bad.json", "--json")
    d = json.loads(out)
    assert code == 2 and d["ok"] is False and "already used" in d["rules"]["COMPARE_NONE"][0]
    assert run(capsys, "skill", "learn", "bad.json")[0] == 2 and not (proj / "knowledge").exists()
    (proj / "junk.json").write_text("{")
    assert run(capsys, "skill", "validate", "junk.json")[0] == 2 and run(capsys, "skill", "validate", "nope.json")[0] == 2
    assert run(capsys, "skill", "new", "my-skill", "--title", "T")[0] == 0 and run(capsys, "skill", "new", "my-skill")[0] == 2


def test_policy_commands(proj, capsys):
    (proj / "t.py").write_text("import os\nif a == None:\n    pass\n")
    assert run(capsys, "policy", "set", "COMPARE_NONE", "--mode", "report_only")[0] == 0
    assert run(capsys, "policy", "set", "NOPE", "--mode", "disabled")[0] == 2
    run(capsys, "policy", "set", "UNUSED_IMPORT", "--severity", "safe", "--exclude", "skip/*")
    code, out, _ = run(capsys, "policy", "show", "--changed")
    assert "COMPARE_NONE" in out and "report_only" in out and "UNUSED_IMPORT" in out and "AUTO".lower() in out.lower()
    run(capsys, "autofix", "t.py", "--write")
    text = (proj / "t.py").read_text()
    assert "a == None" in text and "import os" not in text                        # report-only kept, promoted rule applied
    assert run(capsys, "policy", "behavior", "--allow-risky", "true", "--min-confidence", "0.9")[0] == 0
    code, out, _ = run(capsys, "policy", "show", "--json")
    assert any(r["id"] == "COMPARE_NONE" and r["mode"] == "report_only" for r in json.loads(out))
    assert run(capsys, "policy", "reset")[0] == 2
    assert run(capsys, "policy", "reset", "COMPARE_NONE")[0] == 0 and run(capsys, "policy", "reset", "--all")[0] == 0
    assert json.loads((proj / "codefix.policy.json").read_text())["rules"] == {}
    (proj / "codefix.policy.json").write_text("{bad")
    assert run(capsys, "review", "t.py")[0] == 2                                  # a corrupt policy file is a clear config error
