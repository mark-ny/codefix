import pytest

from codefix.config import ConfigError, load_config, parse_config
from codefix.knowledge.registry import build_registry
from codefix.models import Action, Finding, Location, Severity
from codefix.policy import PolicyEngine, iter_python_files
from codefix.models import ProjectConfig


def test_defaults_match_spec():
    c = ProjectConfig()
    assert (c.allow_risky, c.minimum_confidence, c.require_tests_for_risky, c.backup_before_write) == (False, 0.8, True, True)
    assert (c.max_function_lines, c.max_complexity) == (50, 10)


def test_load_example_config_from_discovery(tmp_path):
    (tmp_path / "codefix.toml").write_text(
        '[project]\nname="x"\n[behavior]\nformatter="ruff"\nminimum_confidence=0.9\n'
        '[knowledge]\npacks=["python"]\n[rules]\ndisabled=["MISSING_DOCSTRING"]\n[metrics]\nmax_complexity=7\n')
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    c = load_config(sub)
    assert c.name == "x" and c.formatter == "ruff" and c.minimum_confidence == 0.9
    assert c.disabled_rules == ["MISSING_DOCSTRING"] and c.max_complexity == 7 and c.root == tmp_path.resolve()


@pytest.mark.parametrize("data", [{"behavior": {"minimum_confidence": 1.5}}, {"behavior": {"formatter": "nope"}},
                                  {"metrics": {"max_complexity": 0}}, {"rules": {"disabled": "x"}},
                                  {"behavior": {"allow_risky": "yes"}}])
def test_invalid_config_rejected(data, tmp_path):
    with pytest.raises(ConfigError):
        parse_config(data, tmp_path)


def test_unknown_keys_warn(tmp_path):
    c = parse_config({"behavior": {"typo": 1}, "extra": {}}, tmp_path)
    assert len(c.warnings) == 2


def mk(sev, conf=0.99, cat="correctness", fixable=True, rid="COMPARE_NONE"):
    return Finding(rid, "m", cat, sev, conf, Location("f.py"), None, fixable)


def decide(cfg, f):
    return PolicyEngine(cfg, build_registry(cfg)).decide(f)


def test_decision_ladder():
    cfg = ProjectConfig()
    assert decide(cfg, mk(Severity.SAFE)).action is Action.AUTO
    assert decide(cfg, mk(Severity.REVIEW)).action is Action.CONFIRM
    d = decide(cfg, mk(Severity.RISKY))
    assert d.action is Action.EXPLICIT and d.requires_tests
    assert decide(cfg, mk(Severity.BLOCKED)).action is Action.BLOCK
    assert decide(cfg, mk(Severity.SAFE, conf=0.5)).action is Action.REPORT_ONLY
    assert decide(cfg, mk(Severity.SAFE, fixable=False)).action is Action.REPORT_ONLY


def test_allow_risky_downgrades_to_confirmation_but_requires_tests():
    d = decide(ProjectConfig(allow_risky=True), mk(Severity.RISKY))
    assert d.action is Action.CONFIRM and d.requires_tests


def test_blocked_rules_config_wins():
    assert decide(ProjectConfig(blocked_rules=["COMPARE_NONE"]), mk(Severity.SAFE)).action is Action.BLOCK


def test_security_uses_stricter_policy():
    cfg = ProjectConfig()
    assert decide(cfg, mk(Severity.SAFE, cat="security")).action is Action.REPORT_ONLY        # autofix off by default
    assert decide(cfg, mk(Severity.SAFE, conf=0.9, cat="security")).action is Action.REPORT_ONLY
    on = ProjectConfig(security_allow_autofix=True)
    assert decide(on, mk(Severity.SAFE, cat="security")).action is Action.EXPLICIT              # never automatic
    assert decide(on, mk(Severity.SAFE, conf=0.9, cat="security")).action is Action.REPORT_ONLY  # 0.95 minimum


def test_ai_findings_always_need_explicit_approval_and_tests():
    d = decide(ProjectConfig(), mk(Severity.REVIEW, conf=0.9, cat="ai", rid="AI_PROPOSAL"))
    assert d.action is Action.EXPLICIT and d.requires_tests


def test_directory_scanning_is_deterministic_and_excludes(tmp_path):
    for rel in ["b.py", "a.py", "pkg/c.py", ".git/x.py", ".venv/y.py", "venv/z.py", "__pycache__/q.py",
                "build/w.py", "migrations/m.py", "pkg/skip_me.py", "notes.txt", "x.egg-info/e.py"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    cfg = parse_config({"project": {"exclude": ["migrations/*", "skip_me.py"]}}, tmp_path)
    files, errors = iter_python_files([tmp_path, tmp_path / "missing"], cfg)
    assert [f.relative_to(tmp_path).as_posix() for f in files] == ["a.py", "b.py", "pkg/c.py"]
    assert len(errors) == 1
