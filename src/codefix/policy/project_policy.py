"""Project-wide file discovery and exclusion rules."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ..models import ProjectConfig

DEFAULT_EXCLUDE_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "build", "dist", "node_modules",
                        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".codefix", "site-packages",
                        ".eggs"}


def _excluded_dir(name: str) -> bool:
    return name in DEFAULT_EXCLUDE_DIRS or name.endswith(".egg-info")


def _matches(config: ProjectConfig, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(config.root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat) or fnmatch.fnmatch(rel + "/", pat)
               for pat in config.exclude)


def iter_python_files(paths: list[str | Path], config: ProjectConfig) -> tuple[list[Path], list[str]]:
    """Return ``(files, errors)``: sorted, de-duplicated ``.py`` files and unusable arguments."""
    files: dict[str, Path] = {}
    errors: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            files[str(p.resolve())] = p            # an explicitly named file is always processed
        elif p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames
                                     if not _excluded_dir(d) and not _matches(config, Path(dirpath) / d))
                for name in sorted(filenames):
                    fp = Path(dirpath) / name
                    if name.endswith(".py") and not _matches(config, fp):
                        files.setdefault(str(fp.resolve()), fp)
        else:
            errors.append(f"{raw}: no such file or directory")
    return [files[k] for k in sorted(files)], errors
