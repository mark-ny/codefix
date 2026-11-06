import json
import os
import stat
import sys

import pytest

from conftest import opts

BUGGY = '''import os


def add(item, bucket=[]):
    """Add."""
    bucket.append(item)
    if item == None:
        return
    return bucket
'''


def read(p):
    return p.read_bytes().decode()


def test_dry_run_never_writes(project):
    p = project.write("a.py", BUGGY)
    app = project.app()
    rep = app.fix([p], opts(write=False, preview=True))
    assert rep.outcomes[0].status == "dry-run" and read(p) == BUGGY
    assert "--- a/" in app._buf.getvalue()           # the diff is shown
    assert not (project.root / ".codefix").exists()  # and no backup is made for a dry run
    assert rep.exit_code == 1


def test_autofix_applies_only_safe_fixes_and_creates_backup(project):
    p = project.write("a.py", BUGGY)
    app = project.app()
    rep = app.fix([p], opts(command="autofix", yes=False, passes=3))
    o = rep.outcomes[0]
    assert o.status == "written" and o.backup and os.path.exists(o.backup)
    text = read(p)
    assert "item is None" in text and "return None" in text
    assert "import os" in text and "bucket=[]" in text            # review/risky fixes were NOT applied
    assert open(o.backup).read() == BUGGY
    assert {f.rule_id for f in o.changeset.applied} == {"COMPARE_NONE", "INCONSISTENT_RETURN"}
    assert rep.exit_code == 1                                     # findings remain


def test_risky_fix_needs_explicit_approval_and_tests(project):
    p = project.write("a.py", BUGGY)
    app = project.app()
    rep = app.fix([p], opts(yes=True))                            # --yes does not cover risky fixes
    assert "bucket=[]" in read(p) or "bucket=None" not in read(p)
    assert "bucket=None" not in read(p)
    rep = app.fix([p], opts(yes=True, approve_rules={"MUTABLE_DEFAULT"}))
    skipped = [s.reason for s in rep.outcomes[0].changeset.skipped]
    assert any("requires passing tests" in r for r in skipped)    # no [tests] command configured
    assert "bucket=None" not in read(p)


def test_full_pipeline_with_tests(project):
    p = project.write("src/a.py", BUGGY)
    project.write("tests/test_a.py", "import sys\nsys.path.insert(0, 'src')\nfrom a import add\n\n"
                  "def test_add():\n    assert add(1, []) == [1]\n\ndef test_fresh():\n    assert add(1) == [1]\n    assert add(2) == [2]\n")
    app = project.app({"tests": {"command": f"{sys.executable} -m pytest"}})
    rep = app.fix([p], opts(approve_rules={"MUTABLE_DEFAULT"}))
    o = rep.outcomes[0]
    assert o.status == "written" and o.tests.ok, o.error
    text = read(p)
    assert "bucket=None" in text and "import os" not in text
    assert [s.name for s in o.changeset.verification.steps][-2:] == ["write", "tests"]


def test_failing_tests_roll_back_automatically(project):
    p = project.write("src/a.py", BUGGY)
    project.write("tests/test_a.py", "import sys\nsys.path.insert(0, 'src')\nfrom a import add\n\n"
                  "def test_add():\n    assert add(1, [])[0] == 99\n")
    app = project.app({"tests": {"command": f"{sys.executable} -m pytest"}})
    rep = app.fix([p], opts(command="autofix", yes=False))
    o = rep.outcomes[0]
    assert o.status == "rolled_back" and read(p) == BUGGY
    assert o.tests.failures and o.tests.failures[0].test.endswith("test_add")
    assert any(s.name == "rollback" and s.ok for s in o.changeset.verification.steps)
    assert rep.exit_code == 2


def test_rollback_disabled_leaves_file_modified_and_says_so(project):
    p = project.write("src/a.py", BUGGY)
    project.write("tests/test_a.py", "def test_x():\n    assert False\n")
    app = project.app({"behavior": {"rollback_on_failure": False}, "tests": {"command": f"{sys.executable} -m pytest"}})
    o = app.fix([p], opts(command="autofix", yes=False)).outcomes[0]
    assert o.status == "failed" and "stays modified" in o.error and "item is None" in read(p)


def test_undo_restores_previous_versions_in_order(project):
    p = project.write("a.py", BUGGY)
    app = project.app()
    app.fix([p], opts(command="autofix", yes=False))
    after_one = read(p)
    app.fix([p], opts(yes=True, approve_rules=set(), only_rules={"UNUSED_IMPORT"}))
    assert "import os" not in read(p)
    assert app.undo(p)["restored"] and read(p) == after_one
    assert app.undo(p)["restored"] and read(p) == BUGGY
    assert not app.undo(p)["restored"]


def test_permissions_preserved_on_write(project):
    p = project.write("a.py", BUGGY, mode=0o750)
    project.app().fix([p], opts(command="autofix", yes=False))
    assert stat.S_IMODE(p.stat().st_mode) == 0o750


def test_crlf_file_stays_crlf(project):
    p = project.write("a.py", BUGGY.replace("\n", "\r\n").encode())
    project.app().fix([p], opts(command="autofix", yes=False))
    raw = p.read_bytes()
    assert b"item is None" in raw and raw.count(b"\r\n") == raw.count(b"\n")


def test_file_changed_on_disk_is_not_overwritten(project, monkeypatch):
    p = project.write("a.py", BUGGY)
    app = project.app()
    real = app._write

    def racing(src, cs, needs):
        p.write_text("# someone else edited this\n")
        return real(src, cs, needs)

    monkeypatch.setattr(app, "_write", racing)
    o = app.fix([p], opts(command="autofix", yes=False)).outcomes[0]
    assert o.status == "rejected" and read(p) == "# someone else edited this\n"


def test_buggy_rule_cannot_write_invalid_code(project):
    """A rule that proposes broken code is rejected by validation; the file is untouched."""
    from codefix.knowledge.registry import FunctionRule
    from codefix.models import RuleSpec, Severity
    p = project.write("a.py", "x = 1\n")
    app = project.app()

    def evil(ctx, spec):
        import ast
        n = next(n for n in ast.walk(ctx.tree) if isinstance(n, ast.Assign))
        s, e = ctx.span(n)
        yield ctx.finding(spec, n, "broken", edits=[ctx.edit(spec, s, e, "x = (")])

    app.registry.register(FunctionRule(RuleSpec("EVIL", "correctness", Severity.SAFE, 1.0, message="m", autofix=True), evil))
    rep = app.fix([p], opts(command="autofix", yes=False))
    assert read(p) == "x = 1\n" and rep.outcomes[0].status == "unchanged"
    assert not (project.root / ".codefix").exists()


def test_removing_a_function_is_blocked(project):
    from codefix.knowledge.registry import FunctionRule
    from codefix.models import RuleSpec, Severity
    p = project.write("a.py", "def keep():\n    pass\n\nx = 1\n")
    app = project.app()

    def deleter(ctx, spec):
        f = ctx.tree.body[0]
        s, e = ctx.span(f)
        yield ctx.finding(spec, f, "drop", edits=[ctx.edit(spec, s, e, "pass")])

    app.registry.register(FunctionRule(RuleSpec("DELETER", "correctness", Severity.SAFE, 1.0, message="m", autofix=True), deleter))
    rep = app.fix([p], opts(command="autofix", yes=False, only_rules={"DELETER"}))
    o = rep.outcomes[0]
    assert read(p).startswith("def keep") and any("disappeared" in s.reason for s in o.changeset.skipped)


def test_policy_blocked_rule_is_never_applied(project):
    p = project.write("a.py", BUGGY)
    app = project.app({"rules": {"blocked": ["COMPARE_NONE"]}})
    app.fix([p], opts(command="autofix", yes=False))
    assert "item == None" in read(p)
    app.fix([p], opts(yes=True, approve_rules={"COMPARE_NONE"}))
    assert "item == None" in read(p)


def test_disabled_rules_are_not_run(project):
    p = project.write("a.py", BUGGY)
    rep = project.app({"rules": {"disabled": ["COMPARE_NONE", "UNUSED_IMPORT"]}}).review([p])
    assert "COMPARE_NONE" not in {f.rule_id for f in rep.findings}


def test_inline_suppression(project):
    p = project.write("a.py", "x = y = z = 1\nif x == None:  # codefix: ignore[COMPARE_NONE]\n    pass\nif y == None:  # codefix: ignore\n    pass\n"
                      "if z == None:\n    pass\n")
    rep = project.app().review([p])
    assert [f.line for f in rep.findings if f.rule_id == "COMPARE_NONE"] == [6] and rep.results[0].suppressed == 2


def test_interactive_prompt_flow(project):
    p = project.write("a.py", "import os\nimport sys\nimport re\nimport json\n\nprint(1)\n")
    answers = iter(["y", "n", "a"])
    prompts = []
    app = project.app(input_fn=lambda q: (prompts.append(q), next(answers))[1])
    app.fix([p], opts(yes=False, interactive=True))
    out = app._buf.getvalue()
    assert "Finding 1 of 4" in out and "Rule: UNUSED_IMPORT" in out and "Severity: review" in out
    assert "Apply this change? [y]es [n]o [a]ll" in prompts[0]
    text = read(p)
    assert "import os" not in text and "import sys" in text and "import re" not in text and "import json" not in text


def test_interactive_quit_and_reject_all(project):
    p = project.write("a.py", "import os\nimport sys\n\nprint(1)\n")
    app = project.app(input_fn=lambda q: "q")
    rep = app.fix([p], opts(yes=False, interactive=True))
    assert "quit by user" in rep.errors and "import os" in read(p)
    app2 = project.app(input_fn=lambda q: "r")
    app2.fix([p], opts(yes=False, interactive=True))
    assert "import os" in read(p) and "import sys" in read(p)


def test_explicit_approval_requires_typed_word(project):
    p = project.write("a.py", "def f(a=[]):\n    return a\n")
    app = project.app({"tests": {"command": f"{sys.executable} -c pass"}}, input_fn=lambda q: "y")
    app.fix([p], opts(yes=False, interactive=True))
    assert "a=[]" in read(p)
    app = project.app({"tests": {"command": f"{sys.executable} -c pass"}}, input_fn=lambda q: "approve")
    o = app.fix([p], opts(yes=False, interactive=True)).outcomes[0]
    assert "a=None" in read(p) and o.status == "written"


def test_ci_mode_never_prompts(project):
    p = project.write("a.py", "import os\n\nprint(1)\n")
    def boom(q): raise AssertionError("prompted in CI mode")
    app = project.app(input_fn=boom)
    app.fix([p], opts(yes=False, ci=True, interactive=True))
    assert "import os" in read(p)


def test_directory_run_continues_after_errors_and_summarises(project):
    project.write("pkg/ok.py", "if a == None:\n    pass\n")
    project.write("pkg/bad.py", "def f(:\n")
    project.write("pkg/latin.py", b"x = '\xe9'\n")           # undecodable as UTF-8, no coding cookie
    project.write("pkg/sub/also.py", "if b == None:\n    pass\n")
    app = project.app()
    rep = app.fix([project.root / "pkg"], opts(command="autofix", yes=False))
    by = {os.path.basename(o.path): o for o in rep.outcomes}
    assert by["ok.py"].status == "written" and by["also.py"].status == "written"
    assert by["bad.py"].status == "error" and by["latin.py"].status == "error"
    assert rep.exit_code == 2 and rep.summary()["by_status"]["written"] == 2
    review = app.review([project.root / "pkg"])
    assert review.exit_code == 2 and [r.path for r in review.results] == sorted(r.path for r in review.results)


def test_json_output_is_machine_readable(project, capsys):
    from codefix.cli.main import main
    project.write("a.py", BUGGY)
    code = main(["review", str(project.root / "a.py"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["tool"] == "codefix" and data["summary"]["findings"] == len(data["files"][0]["findings"])
    f = data["files"][0]["findings"][0]
    assert {"rule_id", "message", "category", "severity", "confidence", "file", "line", "column", "end_line",
            "end_column", "function", "fixable", "metadata"} <= set(f)


def test_format_pipeline_rejects_bad_formatter_but_keeps_fix(project, fake_formatter):
    p = project.write("a.py", "if a == None:\n    pass\n")
    app = project.app({"behavior": {"formatter": "ruff"}}, formatters=[fake_formatter("invalid")])
    o = app.fix([p], opts(command="autofix", yes=False, format=True)).outcomes[0]
    assert o.status == "written" and read(p) == "if a is None:\n    pass\n"
    assert any("formatter skipped" in w and "invalid Python" in w for w in o.changeset.warnings)


def test_format_pipeline_applies_valid_formatter_output(project, fake_formatter):
    p = project.write("a.py", "if a == None:\n  pass\n")
    app = project.app({"behavior": {"formatter": "ruff"}}, formatters=[fake_formatter("indent")])
    app.fix([p], opts(command="autofix", yes=False, format=True))
    assert read(p) == "if a is None:\n    pass\n"


def test_format_command_fails_loudly_when_formatter_breaks(project, fake_formatter):
    p = project.write("a.py", "x = 1\n")
    app = project.app({"behavior": {"formatter": "ruff"}}, formatters=[fake_formatter("fail")])
    rep = app.format([p], write=True)
    assert rep.outcomes[0].status == "error" and rep.exit_code == 2 and read(p) == "x = 1\n"


def test_check_command(project):
    project.write("good.py", "x = 1\n")
    project.write("bad.py", "return 1\n")
    res = project.app().check([project.root])
    assert not res["ok"] and {os.path.basename(e["path"]): e["syntax_ok"] for e in res["files"]} == {"bad.py": False, "good.py": True}


@pytest.mark.skipif(not __import__("codefix.analysis.cst_engine", fromlist=["x"]).available(), reason="libcst missing")
def test_rename_command_writes_with_backup(project):
    p = project.write("a.py", "def old():\n    pass\n\nold()\n")
    rep = project.app().rename(p, "old", "new", write=True)
    assert rep.outcomes[0].status == "written" and read(p) == "def new():\n    pass\n\nnew()\n"
    assert rep.outcomes[0].backup


def test_replace_and_stub_commands(project):
    p = project.write("a.py", "@d\ndef f(x):\n    return x\n")
    rep_file = project.write("rep.py", "def f(x):\n    return x * 2\n")
    app = project.app()
    assert app.replace(p, "f", rep_file, write=True).outcomes[0].status == "written"
    assert read(p) == "@d\ndef f(x):\n    return x * 2\n"
    assert app.stub(p, "f", write=False).outcomes[0].status == "dry-run" and "x * 2" in read(p)
    app.stub(p, "f", write=True)
    assert "raise NotImplementedError('f')" in read(p) and read(p).startswith("@d")


def test_ai_proposals_pass_through_the_same_gate(project):
    p = project.write("a.py", "def f():\n    return 1\n")
    prop = project.write("prop.json", json.dumps({"proposals": [
        {"file": "a.py", "description": "use 2", "confidence": 0.9, "edits": [{"start_line": 2, "end_line": 2, "text": "    return 2\n"}]},
        {"file": "a.py", "description": "broken", "confidence": 0.9, "edits": [{"start_line": 1, "end_line": 1, "text": "def f(:\n"}]}]}))
    app = project.app({"tests": {"command": f"{sys.executable} -c pass"}})
    # without explicit approval nothing happens
    o = app.apply_proposals(prop, opts(yes=True)).outcomes[0]
    assert read(p) == "def f():\n    return 1\n"
    o = app.apply_proposals(prop, opts(yes=True, approve_rules={"AI_PROPOSAL"})).outcomes[0]
    assert "return 2" in read(p) and "def f():" in read(p)                 # valid one applied, broken one rejected
    assert any("rejected" in s.reason for s in o.changeset.skipped)
    bad = project.write("bad.json", json.dumps({"proposals": [{"file": "a.py", "confidence": 2, "edits": []}]}))
    assert app.apply_proposals(bad, opts()).errors
