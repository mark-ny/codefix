"""Timestamped backups kept under ``<root>/.codefix/backups``."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .atomic_write import atomic_write_bytes


@dataclass
class BackupRecord:
    original: str          # absolute path of the backed-up file
    backup: str            # absolute path of the backup copy
    timestamp: str
    sha256: str
    mode: int
    kind: str = "write"    # write | pre-undo
    consumed: bool = False

    @property
    def meta_path(self) -> Path:
        return Path(self.backup + ".json")

    def save_meta(self) -> None:
        atomic_write_bytes(self.meta_path, json.dumps(asdict(self), indent=2).encode())


class BackupManager:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.dir = self.root / ".codefix" / "backups"

    def _slot(self, path: Path) -> Path:
        digest = hashlib.sha1(str(path).encode()).hexdigest()[:10]
        return self.dir / f"{digest}_{path.name}"

    def create(self, path: str | Path, kind: str = "write") -> BackupRecord:
        real = Path(os.path.realpath(path))
        data = real.read_bytes()
        mode = stat.S_IMODE(os.stat(real).st_mode)
        slot = self._slot(real)
        slot.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
        backup = slot / f"{stamp}.bak"
        atomic_write_bytes(backup, data, mode=mode)       # the backup itself is written atomically
        rec = BackupRecord(str(real), str(backup), stamp, hashlib.sha256(data).hexdigest(), mode, kind)
        rec.save_meta()
        return rec

    def list(self, path: str | Path) -> list[BackupRecord]:
        slot = self._slot(Path(os.path.realpath(path)))
        if not slot.is_dir():
            return []
        out = []
        for meta in sorted(slot.glob("*.bak.json")):
            try:
                out.append(BackupRecord(**json.loads(meta.read_text())))
            except (OSError, ValueError, TypeError):
                continue
        return out

    def latest(self, path: str | Path) -> BackupRecord | None:
        usable = [r for r in self.list(path) if r.kind == "write" and not r.consumed]
        return usable[-1] if usable else None
