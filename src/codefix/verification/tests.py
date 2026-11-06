"""Run the project's tests and attribute failures."""
from __future__ import annotations

import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from ..models import ProjectConfig
from .attribution import Failure, parse_pytest_output, parse_summary


@dataclass
class TestRunResult:
    __test__ = False                      # not a pytest class

    ok: bool
    returncode: int
    command: list[str]
    duration: float = 0.0
    summary: dict[str, int] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    def to_dict(self, include_output: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "returncode": self.returncode, "command": self.command,
                             "duration": round(self.duration, 3), "summary": self.summary, "error": self.error,
                             "failures": [f.to_dict() for f in self.failures]}
        if include_output:
            d["stdout"], d["stderr"] = self.stdout, self.stderr
        return d


def build_command(config: ProjectConfig, targets: list[str]) -> list[str]:
    cmd = shlex.split(config.tests.command) if config.tests.command.strip() else [sys.executable, "-m", "pytest"]
    if any("pytest" in part for part in cmd):
        for flag in ("-q", "--tb=short", "-rfE", "-p", "no:cacheprovider"):
            if flag not in cmd:
                cmd.append(flag)
    return cmd + list(targets)


def run_tests(config: ProjectConfig, targets: list[str] | None = None, timeout: int | None = None) -> TestRunResult:
    cmd = build_command(config, targets or [])
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=config.root,
                              timeout=timeout or config.tests.timeout)
    except subprocess.TimeoutExpired:
        return TestRunResult(False, -1, cmd, time.monotonic() - start, error="test run timed out")
    except OSError as exc:
        return TestRunResult(False, -1, cmd, time.monotonic() - start, error=f"cannot run tests: {exc}")
    out = proc.stdout + "\n" + proc.stderr
    error = ""
    if "No module named pytest" in out:
        error = "pytest is not installed"
    elif proc.returncode == 5:
        error = "no tests were collected"
    return TestRunResult(proc.returncode == 0, proc.returncode, cmd, time.monotonic() - start,
                         parse_summary(proc.stdout), parse_pytest_output(proc.stdout, config.root),
                         proc.stdout, proc.stderr, error)
