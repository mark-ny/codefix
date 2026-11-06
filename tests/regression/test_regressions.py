"""Each test pins a bug found while building CodeFix."""
import ast

from codefix.analysis import AnalysisEngine, cst_engine
from codefix.knowledge.registry import build_registry
from codefix.models import ProjectConfig
from codefix.patching import PatchEngine
from codefix.verification import validate_change
from conftest import source

REG, CFG = build_registry(), ProjectConfig()


def analyze(code):
    return AnalysisEngine(REG, CFG).analyze_source(source(code)).findings


def autofix(code, rule):
    src = source(code)
    fs = [f for f in AnalysisEngine(REG, CFG).analyze_source(src).findings if f.rule_id == rule and f.fixable]
    out = PatchEngine.apply_findings(src, fs, lambda t: validate_change(code, t)).text
    ast.parse(out)
    return out


def test_unused_import_with_noqa_is_reported_but_not_rewritten():
    code = "import os  # noqa: F401\nx = 1\n"
    f = [f for f in analyze(code) if f.rule_id == "UNUSED_IMPORT"][0]
    assert not f.fixable and autofix(code, "UNUSED_IMPORT") == code


def test_unused_import_removal_after_multibyte_text_on_previous_lines():
    code = "x = 'ñandú €'\nimport os\ny = 'é'\n"
    assert autofix(code, "UNUSED_IMPORT") == "x = 'ñandú €'\ny = 'é'\n"


def test_unused_import_inside_function_and_indented_block():
    code = "def f():\n    import os\n    return 1\n"
    assert autofix(code, "UNUSED_IMPORT") == "def f():\n    return 1\n"


def test_multiline_parenthesised_import_partial_removal():
    code = "from typing import (\n    List,\n    Dict,\n)\n\nx: Dict = {}\n"
    assert autofix(code, "UNUSED_IMPORT") == "from typing import Dict\n\nx: Dict = {}\n"


def test_mutable_default_inside_nested_function_and_async():
    code = "async def f(a=[]):\n    def g(b={}):\n        return b\n    return a\n"
    out = autofix(code, "MUTABLE_DEFAULT")
    assert "a=None" in out and "b=None" in out and "if a is None:\n        a = []" in out
    assert "if b is None:\n            b = {}" in out


def test_mutable_default_tab_indentation():
    code = "def f(a=[]):\n\treturn a\n"
    out = autofix(code, "MUTABLE_DEFAULT")
    assert out == "def f(a=None):\n\tif a is None:\n\t\ta = []\n\treturn a\n"


def test_mutable_default_docstring_only_body():
    out = autofix('def f(a=[]):\n    """Doc."""\n', "MUTABLE_DEFAULT")
    assert out.endswith('    if a is None:\n        a = []\n')


def test_compare_none_in_fstring_and_nested_boolop():
    code = "ok = (x == None) or (y != None and z)\n"
    assert autofix(code, "COMPARE_NONE") == "ok = (x is None) or (y is not None and z)\n"


def test_type_compare_does_not_fire_for_constants_or_calls_with_keywords():
    assert not [f for f in analyze("a = type(x) == 'int'\nb = type(x, y, z) == T\n") if f.rule_id == "TYPE_COMPARE"]


def test_assert_tuple_with_comment_is_not_rewritten():
    code = "assert (x,  # why\n        'm')\n"
    assert autofix(code, "ASSERT_TUPLE") == code


def test_open_assigned_then_returned_is_not_flagged():
    assert not [f for f in analyze("def f(p):\n    h = open(p)\n    return h\n") if f.rule_id == "RESOURCE_NO_CONTEXT"]


def test_nested_elif_does_not_count_as_nesting():
    from codefix.analysis.complexity import max_nesting
    tree = ast.parse("if a:\n    pass\nelif b:\n    pass\nelif c:\n    if d:\n        pass\n")
    assert max_nesting(tree.body)[0] == 2


def test_undefined_name_ignores_class_scope_leak_but_sees_real_errors():
    code = "class A:\n    x = 1\n    def m(self):\n        return x\n"
    assert [f.message for f in analyze(code) if f.rule_id == "UNDEFINED_NAME"] == ["Undefined name 'x'"]


def test_undefined_name_comprehension_walrus_match_and_type_hints():
    code = ("from __future__ import annotations\n"
            "def f(items: list[Foo]) -> Bar:\n"
            "    if (n := len(items)) > 1:\n        pass\n"
            "    match items:\n        case [first, *rest]:\n            print(first, rest, n)\n"
            "    return [y for y in items if y]\n"
            "class Foo: ...\nclass Bar: ...\n")
    assert not [f for f in analyze(code) if f.rule_id == "UNDEFINED_NAME"]


def test_literal_string_annotations_are_not_undefined_names():
    code = "from typing import Literal\nx: Literal['on', 'off'] = 'on'\n"
    assert not [f for f in analyze(code) if f.rule_id == "UNDEFINED_NAME"]


def test_libcst_builtin_scope_cycle_does_not_hang():
    if not cst_engine.available():
        return
    text, _, where = cst_engine.rename("def old(): pass\nold()\n", "old", "new")
    assert text == "def new(): pass\nnew()\n" and where == "<module>"


def test_failing_rule_does_not_crash_the_run():
    from codefix.knowledge.registry import FunctionRule, RuleRegistry
    from codefix.models import RuleSpec, Severity
    reg = RuleRegistry()
    reg.register(FunctionRule(RuleSpec("BOOM", "x", Severity.SAFE, 1.0), lambda ctx, spec: 1 / 0))
    res = AnalysisEngine(reg, CFG).analyze_source(source("x = 1\n"))
    assert res.warnings and "BOOM crashed" in res.warnings[0]


def test_symlinked_file_keeps_link(tmp_path):
    from codefix.config import parse_config
    from codefix.application import CodeFix
    import io
    real = tmp_path / "real.py"
    real.write_text("if a == None:\n    pass\n")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    app = CodeFix(parse_config({}, tmp_path), out=io.StringIO())
    from conftest import opts
    app.fix([link], opts(command="autofix", yes=False))
    assert link.is_symlink() and "a is None" in real.read_text()
