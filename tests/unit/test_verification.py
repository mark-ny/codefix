import sys


from codefix.config import parse_config
from codefix.verification.attribution import enclosing_function, parse_pytest_output, parse_summary
from codefix.verification.formatter import CommandFormatter, format_text
from codefix.verification.regression import compare_findings
from codefix.verification.tests import build_command, run_tests
from codefix.models import AnalysisResult, ProjectConfig

PYTEST_OUT = """\
============================= test session starts ==============================
collected 3 items

tests/test_calc.py .FF                                                   [100%]

=================================== FAILURES ===================================
__________________________________ test_add ____________________________________

    def test_add():
>       assert add(1, 2) == 4
E       assert 3 == 4
E        +  where 3 = add(1, 2)

tests/test_calc.py:5: AssertionError
___________________________ TestMath.test_divide _______________________________

tests/test_calc.py:9: in test_divide
    divide(1, 0)
src/calc.py:12: in divide
    return a / b
E   ZeroDivisionError: division by zero
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert 3 == 4
FAILED tests/test_calc.py::TestMath::test_divide - ZeroDivisionError: division by zero
========================= 2 failed, 1 passed in 0.05s ==========================
"""


def test_parse_summary():
    assert parse_summary(PYTEST_OUT) == {"failed": 2, "passed": 1}


def test_failure_attribution_extracts_names_files_lines_functions(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\nclass C:\n    pass\n\n"
                                              "def divide(a, b):\n    x = 1\n    y = 2\n    z = 3\n    return a / b\n")
    fails = parse_pytest_output(PYTEST_OUT, tmp_path)
    assert [f.test for f in fails] == ["tests/test_calc.py::test_add", "tests/test_calc.py::TestMath::test_divide"]
    first, second = fails
    assert (first.file, first.line) == ("tests/test_calc.py", 5) and "assert 3 == 4" in first.message
    assert (second.file, second.line) == ("tests/test_calc.py", 9)
    assert second.likely_functions[0] == {"file": "src/calc.py", "line": 12, "function": "divide"}
    assert "ZeroDivisionError" in second.message


def test_enclosing_function(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("class A:\n    def m(self):\n        return 1\n\ndef top():\n    pass\n")
    assert enclosing_function(p, 3) == "A.m" and enclosing_function(p, 6) == "top" and enclosing_function(p, 4) is None


def test_build_command_adds_pytest_flags_only_for_pytest():
    cfg = parse_config({"tests": {"command": "python -m pytest"}}, __import__("pathlib").Path("."))
    assert "--tb=short" in build_command(cfg, ["tests/x.py"]) and build_command(cfg, ["tests/x.py"])[-1] == "tests/x.py"
    other = parse_config({"tests": {"command": "make check"}}, __import__("pathlib").Path("."))
    assert build_command(other, []) == ["make", "check"]


def test_run_tests_end_to_end_with_failure(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    cfg = parse_config({"tests": {"command": f"{sys.executable} -m pytest"}}, tmp_path)
    r = run_tests(cfg)
    assert not r.ok and r.summary["failed"] == 1
    f = r.failures[0]
    assert f.test.endswith("test_calc.py::test_add") and f.file == "test_calc.py" and f.line == 4
    assert f.likely_functions and f.likely_functions[0]["function"] == "add"
    d = r.to_dict()
    assert d["failures"][0]["likely_functions"][0]["file"] == "calc.py"


def test_run_tests_no_tests_collected_is_a_failure(tmp_path):
    r = run_tests(parse_config({"tests": {"command": f"{sys.executable} -m pytest"}}, tmp_path))
    assert not r.ok and "no tests" in r.error


def test_formatter_invalid_output_is_rejected(fake_formatter, tmp_path):
    cfg = ProjectConfig(root=tmp_path, formatter="ruff")
    r = format_text("x = 1\n", "a.py", cfg, [fake_formatter("invalid")])
    assert not r.ok and "invalid Python" in r.error and r.text == "x = 1\n"


def test_formatter_crash_and_missing(fake_formatter, tmp_path):
    cfg = ProjectConfig(root=tmp_path, formatter="ruff")
    r = format_text("x = 1\n", "a.py", cfg, [fake_formatter("fail")])
    assert not r.ok and "exited with 3" in r.error and r.text == "x = 1\n"
    missing = CommandFormatter("ghost", lambda p: ["/nonexistent/binary"])
    r = format_text("x = 1\n", "a.py", cfg, [missing])
    assert not r.ok and not r.available


def test_formatter_fallback_when_first_unavailable(fake_formatter, tmp_path):
    cfg = ProjectConfig(root=tmp_path, formatter="ruff")
    missing = CommandFormatter("ghost", lambda p: ["/nonexistent/binary"])
    r = format_text("x = 1\n", "a.py", cfg, [missing, fake_formatter("upper")])
    assert r.ok and r.formatter == "upper" and r.changed


def test_real_ruff_and_black_when_installed(tmp_path):
    for name in ("ruff", "black"):
        cfg = ProjectConfig(root=tmp_path, formatter=name)
        r = format_text("x   =  [1,2,\n 3]\n", "a.py", cfg)
        if r.available:
            assert r.ok and r.text == "x = [1, 2, 3]\n", (name, r)


def test_formatter_none_is_a_noop(tmp_path):
    r = format_text("x   = 1\n", "a.py", ProjectConfig(root=tmp_path, formatter="none"))
    assert r.ok and not r.changed


def test_regression_compare():
    from codefix.models import Finding, Location, Severity
    f = lambda rid: Finding(rid, "m", "c", Severity.SAFE, 1.0, Location("a.py"), None, False)
    ok, errs, warns = compare_findings(AnalysisResult("a", [f("A")]), AnalysisResult("a", [f("A"), f("B")]))
    assert ok and warns and not errs
    ok, errs, _ = compare_findings(AnalysisResult("a"), AnalysisResult("a", [f("UNDEFINED_NAME")]))
    assert not ok and errs
    ok, errs, _ = compare_findings(AnalysisResult("a"), AnalysisResult("a", syntax_ok=False))
    assert not ok
