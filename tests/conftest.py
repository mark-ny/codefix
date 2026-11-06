import io
import sys
import textwrap
from pathlib import Path

import pytest

from codefix.application import CodeFix, FixOptions
from codefix.config import parse_config
from codefix.knowledge.registry import build_registry
from codefix.source import SourceFile


@pytest.fixture
def project(tmp_path):
    """A scratch project directory with helpers."""

    class P:
        root = tmp_path

        def write(self, rel, text, mode=None):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(textwrap.dedent(text).encode() if isinstance(text, str) else text)
            if mode:
                p.chmod(mode)
            return p

        def app(self, toml=None, **kw):
            data = toml or {}
            cfg = parse_config(data, tmp_path)
            out = io.StringIO()
            app = CodeFix(cfg, build_registry(cfg), out=out, **kw)
            app._buf = out
            return app

    return P()


def opts(**kw):
    base = dict(command="fix", write=True, yes=True, ci=False, interactive=False, preview=False, format=False)
    base.update(kw)
    return FixOptions(**base)


def source(text, name="t.py"):
    return SourceFile.from_bytes(text.encode("utf-8"), Path(name))


@pytest.fixture
def fake_formatter(tmp_path):
    """Create a formatter script: mode 'invalid' | 'fail' | 'upper'."""
    from codefix.verification.formatter import CommandFormatter

    def make(mode):
        script = tmp_path / f"fmt_{mode}.py"
        script.write_text(
            "import sys\n"
            "if '--version' in sys.argv: sys.exit(0)\n"
            f"mode = {mode!r}\n"
            "src = sys.stdin.read()\n"
            "if mode == 'invalid': sys.stdout.write('def (:\\n')\n"
            "elif mode == 'fail': sys.stderr.write('boom'); sys.exit(3)\n"
            "else: sys.stdout.write(src.replace('  ', '    ') if mode == 'indent' else src + '\\n# formatted\\n')\n")
        return CommandFormatter(mode, lambda p, s=script: [sys.executable, str(s)])

    return make
