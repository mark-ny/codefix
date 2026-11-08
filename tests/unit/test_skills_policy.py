import json

import pytest

from codefix.analysis import AnalysisEngine
from codefix.config import ConfigError, OVERLAY_NAME, parse_config, read_overlay, validate_overlay, write_overlay
from codefix.knowledge.declarative import normalize_entry
from codefix.knowledge.registry import build_registry
from codefix.knowledge.skills import SkillStore
from codefix.knowledge.userpack import UserPack, entry_from_example
from codefix.knowledge.validator import run_examples, validate_knowledge
from codefix.models import Action
from codefix.policy import PolicyEngine, overrides
from conftest import source

PRINT = {"id": "TEAM_NO_PRINT", "message": "use logging", "bad": "print(x)", "good": "logger.info(x)",
         "match": {"type": "pattern", "pattern": "print($x)", "replace": "logger.info($x)"}}
EVAL = {"id": "TEAM_NO_EVAL", "message": "eval banned", "severity": "risky", "bad": "eval(s)", "match": {"type": "call", "name": "eval"}}
SKILL = {"format": "codefix-skill", "name": "team-habits", "title": "Team", "description": "d", "rules": [PRINT, EVAL]}


@pytest.fixture
def env(tmp_path):
    cfg = parse_config({}, tmp_path)
    return tmp_path, cfg, SkillStore(cfg)


def reload(tmp_path):
    return parse_config({}, tmp_path)


def findings(cfg, code):
    return AnalysisEngine(build_registry(cfg), cfg).analyze_source(source(code)).findings


# ------------------------------------------------------------------ skills
def test_minimal_rules_get_defaults():
    e = normalize_entry({"id": "ABC", "message": "m", "bad": "eval(s)", "match": {"type": "call", "name": "eval"}})
    assert (e["category"], e["severity"], e["confidence"], e["autofix"], e["requires_tests"]) == ("project", "review", 0.9, False, False)
    assert normalize_entry(PRINT)["autofix"] is True and normalize_entry(PRINT)["bad"].endswith("\n")


def test_validate_reports_per_rule_without_writing(env):
    tmp, cfg, st = env
    rep = st.validate(SKILL)
    assert rep["ok"] and rep["rule_count"] == 2 and rep["rules"] == {"TEAM_NO_PRINT": [], "TEAM_NO_EVAL": []}
    assert not st.dir.exists()


def test_learn_is_all_or_nothing(env):
    tmp, cfg, st = env
    broken = dict(SKILL, rules=[PRINT, dict(EVAL, match={"type": "call", "name": "bad name!"})])
    rep = st.learn(broken)
    assert not rep["learned"] and not rep["ok"] and rep["rules"]["TEAM_NO_PRINT"] == [] and rep["rules"]["TEAM_NO_EVAL"]
    assert st.names() == []                                    # the good rule was NOT installed either


def test_learn_activates_immediately_everywhere(env):
    tmp, cfg, st = env
    assert st.learn(SKILL)["learned"]
    cfg2 = reload(tmp)
    assert build_registry(cfg2).get("TEAM_NO_PRINT").spec.pack == "skill:team-habits"
    f = [x for x in findings(cfg2, "def f(s):\n    print(s)\n    return eval(s)\n") if x.rule_id.startswith("TEAM_")]
    assert {x.rule_id for x in f} == {"TEAM_NO_PRINT", "TEAM_NO_EVAL"} and next(x for x in f if x.rule_id == "TEAM_NO_PRINT").fixable
    assert [i for i in validate_knowledge(build_registry(cfg2), cfg2) if i.level == "error"] == []
    assert all(r.ok for r in run_examples(build_registry(cfg2), cfg2) if r.rule_id.startswith("TEAM_"))
    assert not (tmp / OVERLAY_NAME).exists()                    # learning does not create an empty policy file


def test_learn_refuses_clashes_duplicates_and_existing(env):
    tmp, cfg, st = env
    clash = {"name": "x-skill", "rules": [dict(EVAL, id="COMPARE_NONE")]}
    assert "already used" in st.validate(clash)["rules"]["COMPARE_NONE"][0]
    dup = {"name": "x-skill", "rules": [EVAL, EVAL]}
    assert "duplicate" in st.validate(dup)["rules"]["TEAM_NO_EVAL"][0]
    assert st.learn(SKILL)["learned"]
    again = st.learn(SKILL)
    assert not again["learned"] and "already exists" in again["problems"][0]
    assert st.learn(SKILL, replace=True)["learned"]
    other = {"name": "other-skill", "rules": [PRINT]}
    assert "already used" in st.validate(other)["rules"]["TEAM_NO_PRINT"][0]           # ids are unique across skills


@pytest.mark.parametrize("data,msg", [("nope", "JSON object"), ({"name": "Bad Name", "rules": [EVAL]}, "'name'"),
                                      ({"name": "ok-name", "rules": []}, "at least one rule"),
                                      ({"name": "ok-name", "rules": "x"}, "must be a list"),
                                      ({"format": "other", "name": "ok-name", "rules": [EVAL]}, "'format'"),
                                      ({"name": "ok-name", "rules": [EVAL], "examples": {"GHOST": []}}, "unknown rule")])
def test_validate_structural_errors(env, data, msg):
    assert any(msg in p for p in env[2].validate(data)["problems"])


def test_failing_example_blocks_learning(env):
    tmp, cfg, st = env
    skill = dict(SKILL, examples={"TEAM_NO_PRINT": [{"name": "wrong", "kind": "fix", "bad": "print(x)\n", "good": "nope(x)\n"}]})
    rep = st.learn(skill)
    assert not rep["learned"] and "unexpected fix result" in rep["rules"]["TEAM_NO_PRINT"][0]


def test_create_enable_disable_delete(env):
    tmp, cfg, st = env
    assert st.create("my-skill", "T", "d")["ok"] and not st.create("my-skill")["ok"] and not st.create("Bad!")["ok"]
    assert st.add_rule("my-skill", EVAL)["ok"]
    assert build_registry(reload(tmp)).get("TEAM_NO_EVAL") is not None
    assert st.set_enabled("my-skill", False) == []
    assert json.loads((tmp / OVERLAY_NAME).read_text())["skills"] == {"my-skill": {"enabled": False}}
    assert build_registry(reload(tmp)).get("TEAM_NO_EVAL") is None
    assert [s["enabled"] for s in SkillStore(reload(tmp)).list()] == [False]
    assert st.set_enabled("my-skill", True) == [] and build_registry(reload(tmp)).get("TEAM_NO_EVAL") is not None
    assert st.set_enabled("ghost", True)
    assert st.delete("my-skill") and not st.delete("my-skill") and not st.delete("../etc/passwd")


def test_edit_rules_inside_a_skill(env):
    tmp, cfg, st = env
    st.learn(SKILL)
    assert not st.add_rule("team-habits", EVAL)["ok"]                                  # exists
    edited = dict(EVAL, message="eval is forbidden here")
    assert st.add_rule("team-habits", edited, replace=True)["ok"]
    assert [r["message"] for r in st.read("team-habits")["rules"] if r["id"] == "TEAM_NO_EVAL"] == ["eval is forbidden here"]
    assert st.remove_rule("team-habits", "TEAM_NO_PRINT") and not st.remove_rule("team-habits", "TEAM_NO_PRINT")
    assert [r["id"] for r in st.read("team-habits")["rules"]] == ["TEAM_NO_EVAL"]
    assert not st.add_rule("ghost", EVAL)["ok"]


def test_try_on_runs_unsaved_skill(env):
    tmp, cfg, st = env
    res = st.try_on(SKILL, "def f(s):\n    print(s)\n")
    assert res["ok"] and res["fixed"] == "def f(s):\n    logger.info(s)\n" and "-    print(s)" in res["diff"]
    assert not st.try_on(SKILL, "def (:\n")["ok"]
    assert not st.try_on({"name": "x"}, "x = 1\n")["ok"]
    assert st.names() == []


def test_export_roundtrip_between_projects(env, tmp_path_factory):
    tmp, cfg, st = env
    st.learn(SKILL)
    data = st.export("team-habits")
    other = tmp_path_factory.mktemp("other")
    assert SkillStore(parse_config({}, other)).learn(data)["learned"]


def test_broken_skill_file_is_reported_not_fatal(tmp_path):
    d = tmp_path / "knowledge" / "skills"
    d.mkdir(parents=True)
    (d / "junk.json").write_text("{nope")
    (d / "half.json").write_text(json.dumps({"name": "half", "rules": [dict(PRINT, match={"type": "pattern", "pattern": "$a"})]}))
    cfg = parse_config({}, tmp_path)
    reg = build_registry(cfg)
    assert reg.get("COMPARE_NONE") is not None and any("junk.json" in e for e in reg.plugin_errors) and any("half" in e for e in reg.plugin_errors)
    assert [s["ok"] for s in SkillStore(cfg).list()] == [True, False] or [s["ok"] for s in SkillStore(cfg).list()] == [False, True]


def test_my_rules_and_skills_coexist(env):
    tmp, cfg, st = env
    e, ex = entry_from_example("USER_HAS_KEY", "m", "d.has_key(k)", "k in d")
    assert UserPack(cfg).save(e, ex) == []
    assert st.learn(SKILL)["learned"]
    reg = build_registry(reload(tmp))
    assert reg.get("USER_HAS_KEY") and reg.get("TEAM_NO_PRINT")
    assert "already used" in st.validate({"name": "z-skill", "rules": [dict(EVAL, id="USER_HAS_KEY")]})["rules"]["USER_HAS_KEY"][0]


# ------------------------------------------------------------------ policy overlay
def test_overlay_validation_and_normalisation(tmp_path):
    ok = validate_overlay({"rules": {"A": {"mode": "enabled"}, "B": {"mode": "report_only", "exclude": [" tests/* "]}},
                           "behavior": {"allow_risky": True}, "skills": {"s": {"enabled": True}, "t": {"enabled": False}}})
    assert ok["rules"] == {"B": {"mode": "report_only", "exclude": ["tests/*"]}} and ok["skills"] == {"t": {"enabled": False}}
    for bad in [[], {"rules": {"A": {"mode": "nope"}}}, {"rules": {"A": {"severity": "huge"}}}, {"rules": {"A": {"exclude": "x"}}},
                {"rules": {"A": {"typo": 1}}}, {"behavior": {"minimum_confidence": 2}}, {"behavior": {"allow_risky": "yes"}},
                {"behavior": {"other": 1}}, {"surprise": 1}, {"skills": {"s": {"enabled": "no"}}}]:
        with pytest.raises(ConfigError):
            validate_overlay(bad)
    (tmp_path / OVERLAY_NAME).write_text("{broken")
    with pytest.raises(ConfigError):
        read_overlay(tmp_path)


def test_overlay_is_applied_to_config_and_survives_a_hand_written_toml(tmp_path):
    (tmp_path / "codefix.toml").write_text("[behavior]\nminimum_confidence = 0.7\nallow_risky = false\n# my comment\n")
    write_overlay(tmp_path, {"behavior": {"allow_risky": True}, "rules": {"COMPARE_NONE": {"mode": "disabled"}}})
    cfg = parse_config(__import__("tomllib").loads((tmp_path / "codefix.toml").read_text()), tmp_path)
    assert cfg.allow_risky is True and cfg.minimum_confidence == 0.7                  # overlay wins only where it speaks
    assert "# my comment" in (tmp_path / "codefix.toml").read_text()                  # the toml file is never rewritten


def test_modes(tmp_path):
    code = "if a == None:\n    pass\n"
    base = parse_config({}, tmp_path)
    assert findings(base, code)[0].fixable
    for mode, check in [("report_only", lambda f: f and not f[0].fixable), ("disabled", lambda f: not f),
                        ("blocked", lambda f: f and f[0].severity.value == "blocked")]:
        write_overlay(tmp_path, {"rules": {"COMPARE_NONE": {"mode": mode}}})
        got = [f for f in findings(parse_config({}, tmp_path), code) if f.rule_id == "COMPARE_NONE"]
        assert check(got), mode


def test_severity_override_changes_the_approval_ladder(tmp_path):
    write_overlay(tmp_path, {"rules": {"UNUSED_IMPORT": {"severity": "safe"}, "COMPARE_NONE": {"severity": "risky"}}})
    cfg = parse_config({}, tmp_path)
    reg = build_registry(cfg)
    pe = PolicyEngine(cfg, reg)
    f = {x.rule_id: x for x in findings(cfg, "import os\nif a == None:\n    pass\n")}
    assert pe.decide(f["UNUSED_IMPORT"]).action is Action.AUTO and pe.decide(f["COMPARE_NONE"]).action is Action.EXPLICIT


def test_exclude_paths(tmp_path):
    write_overlay(tmp_path, {"rules": {"MISSING_DOCSTRING": {"exclude": ["scripts/*", "legacy.py"]}}})
    cfg = parse_config({}, tmp_path)
    reg = build_registry(cfg)
    eng = AnalysisEngine(reg, cfg)
    code = "def public():\n    return 1\n"
    for rel, expect in [("src/a.py", 1), ("scripts/b.py", 0), ("legacy.py", 0), ("src/legacy.py", 0)]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code)
        res = eng.analyze_file(p)
        assert len([f for f in res.findings if f.rule_id == "MISSING_DOCSTRING"]) == expect, rel
    assert res.suppressed == 1


def test_security_severity_cannot_be_lowered(tmp_path):
    from codefix.knowledge.registry import FunctionRule
    from codefix.models import RuleSpec, Severity
    write_overlay(tmp_path, {"rules": {"SEC_X": {"severity": "safe"}}})
    cfg = parse_config({}, tmp_path)
    reg = build_registry(cfg)
    reg.register(FunctionRule(RuleSpec("SEC_X", "security", Severity.REVIEW, 0.99, message="m", autofix=True),
                              lambda ctx, spec: [ctx.finding(spec, ctx.tree.body[0])]))
    f = AnalysisEngine(reg, cfg).analyze_source(source("x = 1\n")).findings[0]
    assert f.severity is Severity.REVIEW


def test_effective_rows_and_helpers(tmp_path):
    cfg = parse_config({}, tmp_path)
    reg = build_registry(cfg)
    rows = {r["id"]: r for r in overrides.effective_rows(cfg, reg)}
    assert rows["COMPARE_NONE"]["effective"] == "auto" and rows["UNUSED_IMPORT"]["effective"] == "confirm"
    assert rows["MUTABLE_DEFAULT"]["effective"] == "explicit" and rows["UNDEFINED_NAME"]["effective"] == "report_only"
    overrides.set_rule(cfg, "COMPARE_NONE", mode="disabled")
    overrides.set_rule(cfg, "UNUSED_IMPORT", severity="safe", exclude=["tests/*"])
    cfg = parse_config({}, tmp_path)
    rows = {r["id"]: r for r in overrides.effective_rows(cfg, reg)}
    assert rows["COMPARE_NONE"]["effective"] == "disabled" and rows["UNUSED_IMPORT"]["effective"] == "auto"
    assert rows["UNUSED_IMPORT"]["exclude"] == ["tests/*"] and rows["UNUSED_IMPORT"]["customised"]
    overrides.reset(cfg, "COMPARE_NONE")
    assert "COMPARE_NONE" not in read_overlay(tmp_path)["rules"] and "UNUSED_IMPORT" in read_overlay(tmp_path)["rules"]
    overrides.reset(cfg)
    assert read_overlay(tmp_path)["rules"] == {}


def test_examples_ignore_local_policy(tmp_path):
    write_overlay(tmp_path, {"rules": {"COMPARE_NONE": {"mode": "report_only"}}})
    cfg = parse_config({}, tmp_path)
    assert all(r.ok for r in run_examples(build_registry(cfg), cfg, "COMPARE_NONE"))
