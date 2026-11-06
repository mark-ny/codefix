import os
import stat

import pytest

from codefix.models import Finding, Location, ProposedEdit, Severity
from codefix.patching import (BackupManager, EditConflict, PatchEngine, RollbackError, RollbackManager,
                              atomic_write_bytes, unified_diff)
from codefix.source import LineIndex, SourceError, SourceFile
from codefix.verification import check_syntax, validate_change
from conftest import source


def edit(src, old, new, rule="X"):
    data = src.text.encode()
    s = data.index(old.encode())
    return ProposedEdit(rule, "t.py", s, s + len(old.encode()), new, expected=old)


def finding(edits, rule="X"):
    return Finding(rule, "m", "correctness", Severity.SAFE, 1.0, Location("t.py", 1), None, True, {}, edits)


def test_unicode_offsets_are_byte_accurate():
    code = "s = 'héllo wörld €'; x = 1\nif x == None: pass\n"
    src = source(code)
    from codefix.analysis.ast_engine import AnalysisEngine
    from codefix.knowledge.registry import build_registry
    from codefix.models import ProjectConfig
    res = AnalysisEngine(build_registry(), ProjectConfig()).analyze_source(src)
    f = next(f for f in res.findings if f.rule_id == "COMPARE_NONE")
    assert PatchEngine.apply(src, f.edits) == code.replace("== None", "is None")
    idx = LineIndex(src.text.encode())
    assert idx.char_column(1, code.index(";") + 4) == code.index(";") + 1   # 4 extra UTF-8 bytes precede it


def test_overlapping_edits_are_detected():
    src = source("abcdef\n")
    a, b = edit(src, "abc", "X"), edit(src, "cde", "Y")
    assert PatchEngine.find_conflicts([a, b])
    with pytest.raises(EditConflict):
        PatchEngine.apply(src, [a, b])


def test_adjacent_edits_and_bottom_to_top_application():
    src = source("abcdef\n")
    assert PatchEngine.apply(src, [edit(src, "abc", "1"), edit(src, "def", "22")]) == "122\n"


def test_same_point_insertions_conflict():
    src = source("ab\n")
    ins = lambda t: ProposedEdit("X", "t.py", 1, 1, t)
    with pytest.raises(EditConflict):
        PatchEngine.apply(src, [ins("1"), ins("2")])


def test_stale_edit_rejected():
    src = source("abc\n")
    e = edit(src, "abc", "x")
    e.expected = "zzz"
    with pytest.raises(EditConflict):
        PatchEngine.apply(src, [e])


def test_apply_findings_skips_overlapping_findings():
    src = source("abcdef\n")
    r = PatchEngine.apply_findings(src, [finding([edit(src, "abc", "X")], "A"), finding([edit(src, "cde", "Y")], "B")])
    assert len(r.accepted) == 1 and len(r.skipped) == 1 and "overlaps" in r.skipped[0].reason


def test_invalid_result_is_rejected_per_finding():
    code = "x = 1\ny = 2\n"
    src = source(code)
    good = finding([edit(src, "x = 1", "x = 10")], "GOOD")
    bad = finding([edit(src, "y = 2", "y = (")], "BAD")
    r = PatchEngine.apply_findings(src, [good, bad], lambda t: validate_change(code, t))
    assert r.text == "x = 10\ny = 2\n"
    assert [f.rule_id for f in r.accepted] == ["GOOD"] and r.skipped[0].rule_id == "BAD"


def test_validation_blocks_disappearing_function():
    ok, msg = validate_change("def f():\n    pass\n", "x = 1\n")
    assert not ok and "disappeared" in msg
    ok, _ = validate_change("def f():\n    pass\n", "x = 1\n", expected_removed=("f",))
    assert ok


def test_compile_level_errors_are_caught():
    assert check_syntax("x = 1\n").ok
    s = check_syntax("return 1\n")
    assert s.parse_ok and not s.ok


def test_crlf_encoding_bom_roundtrip(tmp_path):
    raw = b"\xef\xbb\xbfx = 1\r\nif x == None:\r\n    pass\r\n"
    p = tmp_path / "a.py"
    p.write_bytes(raw)
    src = SourceFile.read(p)
    assert src.bom and src.newline == "\r\n"
    assert src.encode() == raw
    e = edit(src, "== None", "is None\n# n")        # inserted text uses the file's newline
    assert PatchEngine.apply(src, [e]).endswith("is None\r\n# n:\r\n    pass\r\n")


def test_latin1_source_preserved(tmp_path):
    raw = b"# -*- coding: latin-1 -*-\ns = '\xe9'\nif s == None:\n    pass\n"
    p = tmp_path / "a.py"
    p.write_bytes(raw)
    src = SourceFile.read(p)
    assert src.encoding == "iso-8859-1" and "é" in src.text
    assert src.encode(src.text.replace("== None", "is None")) == raw.replace(b"== None", b"is None")
    with pytest.raises(SourceError):
        src.encode(src.text + "# €\n")


def test_lone_cr_rejected():
    with pytest.raises(SourceError):
        SourceFile.from_bytes(b"a = 1\rb = 2\r", __import__("pathlib").Path("x.py"))


def test_atomic_write_preserves_permissions(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("old")
    p.chmod(0o640)
    atomic_write_bytes(p, b"new")
    assert p.read_text() == "new"
    assert stat.S_IMODE(p.stat().st_mode) == 0o640


def test_atomic_write_failure_leaves_original_and_no_temp(tmp_path, monkeypatch):
    p = tmp_path / "f.py"
    p.write_text("original")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        atomic_write_bytes(p, b"new")
    monkeypatch.undo()
    assert p.read_text() == "original"
    assert [x.name for x in tmp_path.iterdir()] == ["f.py"]


def test_atomic_write_follows_symlink(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("a")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    atomic_write_bytes(link, b"b")
    assert link.is_symlink() and real.read_text() == "b"


def test_backup_and_rollback(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("v1")
    p.chmod(0o600)
    bm = BackupManager(tmp_path)
    rec1 = bm.create(p)
    p.write_text("v2")
    rec2 = bm.create(p)
    p.write_text("v3")
    rb = RollbackManager(bm)
    assert rb.undo(p).backup == rec2.backup and p.read_text() == "v2"
    assert rb.undo(p).backup == rec1.backup and p.read_text() == "v1"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    with pytest.raises(RollbackError):
        rb.undo(p)


def test_rollback_refuses_corrupt_backup(tmp_path):
    p = tmp_path / "f.py"
    p.write_text("v1")
    bm = BackupManager(tmp_path)
    rec = bm.create(p)
    open(rec.backup, "w").write("tampered")
    with pytest.raises(RollbackError):
        RollbackManager(bm).restore(rec)


def test_unified_diff():
    d = unified_diff("a\n", "b\n", "x.py")
    assert d.startswith("--- a/x.py\n+++ b/x.py") and "-a" in d and "+b" in d
    assert unified_diff("a\n", "a\n", "x.py") == ""
