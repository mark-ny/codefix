import ast

import pytest

from codefix import refactor
from codefix.analysis import cst_engine
from conftest import source

ENGINES = ["ast"] + (["libcst"] if cst_engine.available() else [])


@pytest.mark.parametrize("engine", ENGINES)
def test_rename_ignores_strings_comments_and_other_scopes(engine):
    code = '''def old(a):
    return a

class K:
    def m(self):
        old = 1          # local variable, different scope
        return old

def other():
    print("old")  # old
    return old(1)
'''
    r = refactor.rename_symbol(source(code), "old", "new", engine=engine)
    assert r.new_text == code.replace("def old(", "def new(").replace("return old(1)", "return new(1)")
    assert r.engine == engine
    ast.parse(r.new_text)


@pytest.mark.parametrize("engine", ENGINES)
def test_rename_with_explicit_scope(engine):
    code = "def f(x):\n    y = x\n    return y\n\ndef g():\n    y = 2\n    return y\n"
    with pytest.raises(refactor.RefactorError, match="several scopes"):
        refactor.rename_symbol(source(code), "y", "z", engine=engine)
    r = refactor.rename_symbol(source(code), "y", "z", scope="g", engine=engine)
    assert "def f(x):\n    y = x\n    return y" in r.new_text and "z = 2\n    return z" in r.new_text


@pytest.mark.parametrize("engine", ENGINES)
def test_rename_collision_and_bad_identifiers(engine):
    with pytest.raises(refactor.RefactorError):
        refactor.rename_symbol(source("a = 1\nb = 2\n"), "a", "b", engine=engine)
    with pytest.raises(refactor.RefactorError):
        refactor.rename_symbol(source("a = 1\n"), "a", "class", engine=engine)
    with pytest.raises(refactor.RefactorError):
        refactor.rename_symbol(source("a = 1\n"), "nope", "x", engine=engine)


def test_rename_ast_parameter_class_and_unicode():
    code = "class Old:\n    pass\n\ndef f(é, p):\n    s = 'ü€'\n    return Old(), é, p\n"
    r = refactor.rename_symbol(source(code), "Old", "New", engine="ast")
    assert r.new_text.count("New") == 2 and "ü€" in r.new_text
    r2 = refactor.rename_symbol(source(code), "p", "q", engine="ast")
    assert "def f(é, q):" in r2.new_text and "return Old(), é, q" in r2.new_text
    assert r.expected_removed == ("Old",)


def test_rename_ast_reports_limitations():
    code = "def old(): pass\nobj.old()\nf(old=1)\n"
    r = refactor.rename_symbol(source(code), "old", "new", engine="ast")
    assert any("attribute" in w for w in r.warnings) and any("keyword" in w for w in r.warnings)
    assert "obj.old()" in r.new_text and "f(old=1)" in r.new_text


def test_rename_global_and_import_alias():
    code = "counter = 0\n\ndef bump():\n    global counter\n    counter += 1\n"
    r = refactor.rename_symbol(source(code), "counter", "total", engine="ast")
    assert r.new_text == code.replace("counter", "total")
    with pytest.raises(refactor.RefactorError, match="import"):
        refactor.rename_symbol(source("import os\nprint(os.name)\n"), "os", "x", engine="ast")
    r = refactor.rename_symbol(source("import os as o\nprint(o.name)\n"), "o", "oo", engine="ast")
    assert r.new_text == "import os as oo\nprint(oo.name)\n"


def test_rename_refuses_syntax_error():
    with pytest.raises(refactor.RefactorError):
        refactor.rename_symbol(source("def f(:\n"), "f", "g")


CLASS = '''import os

class A:
    # leading comment
    @deco(1)
    @other
    def f(self, x):
        """doc
    indented-in-string"""
        return 1  # trailing

    def g(self):
        pass
'''


def test_replace_preserves_decorators_comments_indent_and_strings():
    rep = 'def f(self, x):\n    """new\n  keep   spacing"""\n    return 2\n'
    r = refactor.replace_function(source(CLASS), "A.f", rep)
    assert "    # leading comment\n    @deco(1)\n    @other\n    def f(self, x):\n" in r.new_text
    assert '        """new\n  keep   spacing"""\n        return 2' in r.new_text      # string content not re-indented
    assert "    def g(self):\n        pass\n" in r.new_text
    assert any("kept the original decorator" in w for w in r.warnings)
    tree = ast.parse(r.new_text)
    assert sum(isinstance(n, ast.FunctionDef) and n.name == "f" for n in ast.walk(tree)) == 1


def test_replace_with_new_decorators_replaces_old_ones():
    r = refactor.replace_function(source(CLASS), "f", "@only\ndef f(self, x):\n    return 3\n")
    assert "@only" in r.new_text and "@deco(1)" not in r.new_text


@pytest.mark.parametrize("rep,msg", [("def other(self):\n    pass\n", "defines 'other'"),
                                     ("def f(:\n", "does not parse"),
                                     ("def f(self, x):\n    pass\n\nx = 1\n", "exactly one function"),
                                     ("x = 1\n", "exactly one function")])
def test_replace_rejects_bad_replacements(rep, msg):
    with pytest.raises(refactor.RefactorError, match=msg):
        refactor.replace_function(source(CLASS), "A.f", rep)


def test_replace_ambiguous_and_missing():
    code = "class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\n"
    with pytest.raises(refactor.RefactorError, match="ambiguous"):
        refactor.replace_function(source(code), "run", "def run(self):\n    pass\n")
    with pytest.raises(refactor.RefactorError, match="not found"):
        refactor.replace_function(source(code), "zzz", "def zzz():\n    pass\n")
    assert refactor.replace_function(source(code), "B.run", "def run(self):\n    return 1\n").new_text.count("return 1") == 1


def test_replace_removed_nested_definitions_are_reported():
    code = "def f():\n    def inner():\n        pass\n    return inner\n"
    r = refactor.replace_function(source(code), "f", "def f():\n    return 1\n")
    assert r.expected_removed == ("f.inner",)


def test_stub_keeps_signature_decorators_and_docstring():
    code = '@deco\ndef f(x, *, y=2):\n    """Doc."""\n    z = x + y\n    return z\n'
    r = refactor.stub_function(source(code), "f")
    assert r.new_text == '@deco\ndef f(x, *, y=2):\n    """Doc."""\n    raise NotImplementedError(\'f\')\n'


def test_stub_same_line_body_and_new_function_and_noop():
    assert refactor.stub_function(source("def f(): return 1\n"), "f").new_text == "def f(): raise NotImplementedError('f')\n"
    out = refactor.stub_function(source("x = 1\n"), "brand_new").new_text
    assert out.endswith("def brand_new(*args, **kwargs):\n    raise NotImplementedError('brand_new')\n")
    with pytest.raises(refactor.RefactorError, match="already a stub"):
        refactor.stub_function(source('def f():\n    """d"""\n'), "f")
