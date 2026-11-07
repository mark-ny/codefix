import json

import pytest

from codefix.analysis import AnalysisEngine
from codefix.config import parse_config
from codefix.knowledge.declarative import (RuleError, build_rule, compile_pattern, complete_snippet, find_matches,
                                           generalize_example, tokenize_text)
from codefix.knowledge.registry import RuleRegistry, build_registry
from codefix.knowledge.userpack import UserPack, entry_from_example
from codefix.knowledge.validator import run_examples, validate_knowledge
from codefix.models import ProjectConfig
from codefix.patching import PatchEngine
from conftest import source


def entry(match, **kw):
    base = {"id": "USER_T", "category": "project", "severity": "review", "confidence": 0.9, "autofix": False,
            "requires_tests": False, "message": "msg", "bad": "x = 1\n", "good": "", "match": match}
    base.update(kw)
    return base


def run(match, code, **kw):
    rule = build_rule(entry(match, autofix=bool(match.get("replace") is not None or match.get("replace_with")), **kw))
    reg = RuleRegistry()
    reg.register(rule)
    src = source(code)
    res = AnalysisEngine(reg, ProjectConfig()).analyze_source(src)
    fixed = [f for f in res.findings if f.fixable]
    text = PatchEngine.apply_findings(src, fixed).text if fixed else code
    return res.findings, text


def matches(pattern, code):
    toks, _ = tokenize_text(code)
    return [(a, b, {k: " ".join(t.string for t in toks[v[0]:v[1]]) for k, v in caps.items()})
            for a, b, caps in find_matches(compile_pattern(pattern), toks)]


def test_pattern_variables_match_whole_operands():
    m = matches("$a == None", "if foo.bar(1)[0] == None:\n    pass\n")
    assert m[0][2] == {"a": "foo . bar ( 1 ) [ 0 ]"}
    assert matches("$a == None", "x = a + b == None\n")[0][2] == {"a": "b"}          # a primary, never half an expression


def test_same_variable_must_match_same_text():
    assert matches("$a == $a", "x == x\n") and not matches("$a == $a", "x == y\n")


def test_pattern_ignores_strings_comments_and_whitespace():
    code = "s = 'x == None'  # x == None\nif  x   ==\tNone: pass\n"
    assert len(matches("$a == None", code)) == 1


def test_pattern_fix_and_unicode():
    findings, out = run({"type": "pattern", "pattern": "$a == None", "replace": "$a is None"},
                        "é = 'ü€'\nif é == None:\n    pass\n")
    assert out == "é = 'ü€'\nif é is None:\n    pass\n" and findings[0].location.line == 2


def test_pattern_with_comment_inside_match_is_reported_not_fixed():
    findings, out = run({"type": "pattern", "pattern": "$a == None", "replace": "$a is None"},
                        "if (x ==  # why\n        None):\n    pass\n")
    assert findings and not findings[0].fixable and "# why" in out


def test_pattern_reordered_arguments():
    _, out = run({"type": "pattern", "pattern": "foo($a, $b)", "replace": "foo($b, $a)"}, "foo(1, bar(2))\nfoo(x, y)\n")
    assert out == "foo(bar(2), 1)\nfoo(y, x)\n"


def test_call_rule_dotted_name_and_rename():
    findings, out = run({"type": "call", "name": "os.system", "replace_with": "run_cmd"},
                        "import os\nos.system('ls')\nx.os.system('no')\nsystem('no')\n")
    assert out == "import os\nrun_cmd('ls')\nx.os.system('no')\nsystem('no')\n" and len(findings) == 1


def test_import_rule():
    findings, out = run({"type": "import", "module": "imp", "replace_with": "importlib"},
                        "import imp\nfrom imp import reload\nimport important\nfrom imp.sub import z\n")
    assert len(findings) == 3
    assert "from importlib import reload" in out and "from importlib.sub import z" in out and "import imp\n" in out


def test_regex_rule_skips_strings_and_comments():
    findings, _ = run({"type": "regex", "pattern": "TODO|FIXME", "flags": "i"},
                      "x = 1  # todo later\ns = 'TODO'\nTODO_LIST = []\nfixme = 2\n")
    assert [f.line for f in findings] == [3, 4] and all(not f.fixable for f in findings)


def test_finding_reports_enclosing_function():
    findings, _ = run({"type": "call", "name": "print"}, "def f():\n    print(1)\n")
    assert findings[0].function == "f"


@pytest.mark.parametrize("match,msg", [
    ({"type": "pattern", "pattern": "$a"}, "literal token"),
    ({"type": "pattern", "pattern": "$a $b x"}, "adjacent"),
    ({"type": "pattern", "pattern": "f($a"}, "unclosed"),
    ({"type": "pattern", "pattern": "f($a)", "replace": "g($zzz)"}, r"\$zzz"),
    ({"type": "regex", "pattern": "("}, "invalid regex"),
    ({"type": "regex", "pattern": "a", "flags": "x"}, "flags"),
    ({"type": "call", "name": "not valid!"}, "dotted"),
    ({"type": "import", "module": ""}, "dotted"),
    ({"type": "nope"}, "match.type"), ("string", "match.type")])
def test_invalid_matches_rejected(match, msg):
    fix = isinstance(match, dict) and ("replace" in match or "replace_with" in match)
    with pytest.raises(RuleError, match=msg):
        build_rule(entry(match, autofix=fix))


def test_entry_validation():
    ok = {"type": "call", "name": "x"}
    for kw, msg in [({"id": "lower"}, "UPPER_SNAKE"), ({"severity": "huge"}, "severity"), ({"confidence": 3}, "confidence"),
                    ({"bad": " "}, "bad"), ({"autofix": True}, "autofix")]:
        with pytest.raises(RuleError, match=msg):
            build_rule(entry(ok, **kw))


@pytest.mark.parametrize("bad,good,pattern,template", [
    ("if x == None:", "if x is None:", "$x == None", "$x is None"),
    ("os.system(cmd)", "subprocess.run(cmd, shell=True)", "os.system($cmd)", "subprocess.run($cmd, shell=True)"),
    ("print(x)", "logger.info(x)", "print($x)", "logger.info($x)"),
    ("f(x)", "f(x, y=1)", "f($x)", "f($x, y=1)"),
    ("foo(a, b)", "foo(b, a)", "foo($a, $b)", "foo($b, $a)"),
    ("d.has_key(k)", "k in d", "$d.has_key($k)", "$k in $d"),
    ("eval(user_input)", None, "eval($user_input)", None)])
def test_generalize_example(bad, good, pattern, template):
    assert generalize_example(bad, good) == (pattern, template)


def test_generalize_rejects_identical_and_empty():
    with pytest.raises(RuleError):
        generalize_example("x = 1", "x = 1")
    with pytest.raises(RuleError):
        generalize_example("", None)


def test_complete_snippet():
    assert complete_snippet("if x:")[0] == "if x:\n    pass\n"
    with pytest.raises(RuleError):
        complete_snippet("x = (")


@pytest.fixture
def pack(tmp_path):
    cfg = parse_config({}, tmp_path)
    return UserPack(cfg), cfg


def test_userpack_roundtrip_and_activation(pack, tmp_path):
    up, cfg = pack
    e, ex = entry_from_example("USER_NO_HAS_KEY", "has_key is gone", "d.has_key(k)", "k in d", negative=["k in d"])
    assert up.save(e, ex) == []
    assert json.loads(up.path.read_text())[0]["id"] == "USER_NO_HAS_KEY"
    reg = build_registry(cfg)                                    # picked up automatically: no config change needed
    assert reg.get("USER_NO_HAS_KEY") is not None and reg.get("USER_NO_HAS_KEY").spec.pack == "project"
    res = AnalysisEngine(reg, cfg).analyze_source(source("def f(m, key):\n    return m.has_key(key)\n"))
    assert any(f.rule_id == "USER_NO_HAS_KEY" and f.fixable for f in res.findings)
    assert [i for i in validate_knowledge(reg, cfg) if i.level == "error"] == []
    results = [r for r in run_examples(reg, cfg) if r.rule_id == "USER_NO_HAS_KEY"]
    assert len(results) == 2 and all(r.ok for r in results)
    assert up.save(e, ex) and "already exists" in up.save(e, ex)[0]
    assert up.save(e, ex, replace=True) == []


def test_userpack_refuses_bad_or_untested_rules(pack):
    up, _ = pack
    e, ex = entry_from_example("USER_X", "m", "d.has_key(k)", "k in d")
    ex[0]["good"] = "something else\n"
    assert any("unexpected fix result" in p for p in up.save(e, ex))          # self-test fails -> nothing saved
    assert not up.path.exists()
    e2, _ = entry_from_example("COMPARE_NONE", "m", "d.has_key(k)", "k in d")
    _, ex2 = entry_from_example("USER_Y", "m", "d.has_key(k)", "k in d")
    assert "already used by a built-in" in up.save(e2, ex2)[0]
    assert up.save(entry({"type": "call", "name": "x"}), []) == ["at least one example that triggers the rule is required"]


def test_userpack_delete_export_import(pack, tmp_path):
    up, cfg = pack
    e, ex = entry_from_example("USER_A", "m", "eval(user_input)", None)
    up.save(e, ex)
    data = up.export()
    assert data["rules"][0]["id"] == "USER_A" and data["examples"]["USER_A"]
    assert up.delete("USER_A") and not up.delete("USER_A") and up.load() == []
    res = up.import_pack(data)
    assert res["imported"] == ["USER_A"] and not res["rejected"]
    res = up.import_pack({"rules": [{"id": "PY_ONLY", "x": 1}, "junk"]})
    assert set(res["rejected"]) == {"PY_ONLY", "?"}
    assert "list of rules" in up.import_pack({"nope": 1})["rejected"]["-"][0]


def test_broken_rule_file_is_reported_not_fatal(tmp_path):
    d = tmp_path / "knowledge" / "project"
    d.mkdir(parents=True)
    (d / "user_rules.json").write_text(json.dumps([entry({"type": "pattern", "pattern": "$a"}, id="USER_BAD")]))
    cfg = parse_config({}, tmp_path)
    reg = build_registry(cfg)
    assert reg.get("USER_BAD") is None and any("USER_BAD" in e for e in reg.plugin_errors)
    assert reg.get("COMPARE_NONE") is not None
    assert any(i.level == "error" and "USER_BAD" in i.message for i in validate_knowledge(reg, cfg))
