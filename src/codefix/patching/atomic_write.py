"""Atomic file replacement with fsync and permission preservation."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _fsync_dir(directory: str) -> None:
    if os.name == "nt":  # pragma: no cover
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def atomic_write_bytes(path: str | Path, data: bytes, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` via a temp file + ``os.replace``.

    Symlinks are followed (the target is replaced, the link is kept). The mode of an
    existing file is preserved unless ``mode`` is given.
    """
    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)
        except FileNotFoundError:
            mode = 0o666 & ~_umask()
    fd, tmp = tempfile.mkstemp(prefix=".codefix-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        try:  # keep ownership where we are allowed to
            st = os.stat(target)
            os.chown(tmp, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)
