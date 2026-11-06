"""Formatter integration (Ruff first, Black as fallback). Always runs in memory."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..models import ProjectConfig
from .syntax import check_syntax, validate_change


@dataclass
class FormatResult:
    ok: bool
    text: str
    formatter: str = ""
    error: str = ""
    changed: bool = False
    available: bool = True


class CommandFormatter:
    """A formatter run as ``argv(path)`` reading source on stdin and writing it to stdout."""

    def __init__(self, name: str, argv: Callable[[str], list[str]]):
        self.name, self._argv = name, argv

    def command(self, path: str) -> list[str]:
        return self._argv(path)

    def available(self) -> bool:
        try:
            probe = self._argv("probe.py")[:1]
            if probe[0] == sys.executable:
                cmd = self._argv("probe.py")[:3] + ["--version"]
            else:
                cmd = [probe[0], "--version"]
            return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


_AVAIL: dict[str, bool] = {}


def _tool(name: str, args: Callable[[str], list[str]]) -> CommandFormatter:
    exe = shutil.which(name)
    base = [exe] if exe else [sys.executable, "-m", name]
    return CommandFormatter(name, lambda p: base + args(p))


def default_formatters(config: ProjectConfig) -> list[CommandFormatter]:
    ruff = lambda: _tool("ruff", lambda p: ["format", "--stdin-filename", p, "-"])
    black = lambda: _tool("black", lambda p: ["-q", "--stdin-filename", p, "-"])
    return {"none": [], "black": [black()], "ruff": [ruff(), black()], "auto": [ruff(), black()]}[config.formatter]


def format_text(text: str, path: str | Path, config: ProjectConfig,
                formatters: list[CommandFormatter] | None = None) -> FormatResult:
    """Format ``text`` in memory. Invalid formatter output is rejected, never returned."""
    chain = default_formatters(config) if formatters is None else formatters
    if not chain:
        return FormatResult(True, text, "", available=False)
    last_error = ""
    for fm in chain:
        if fm.name not in _AVAIL or formatters is not None:
            _AVAIL[fm.name] = fm.available()
        if not _AVAIL[fm.name]:
            last_error = f"{fm.name} is not installed"
            continue
        try:
            proc = subprocess.run(fm.command(str(path)), input=text.encode("utf-8"), capture_output=True,
                                  cwd=config.root, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return FormatResult(False, text, fm.name, f"{fm.name} failed to run: {exc}")
        if proc.returncode != 0:
            return FormatResult(False, text, fm.name,
                                f"{fm.name} exited with {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
        try:
            out = proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return FormatResult(False, text, fm.name, f"{fm.name} produced non-UTF-8 output")
        if not check_syntax(out, str(path)).parse_ok:
            return FormatResult(False, text, fm.name, f"{fm.name} produced invalid Python; output discarded")
        ok, why = validate_change(text, out, str(path))
        if not ok:
            return FormatResult(False, text, fm.name, f"{fm.name} output rejected: {why}")
        return FormatResult(True, out, fm.name, changed=out != text)
    return FormatResult(False, text, "", last_error or "no formatter available", available=False)
