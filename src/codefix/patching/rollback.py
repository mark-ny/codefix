"""Restore files from backups."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .atomic_write import atomic_write_bytes
from .backup import BackupManager, BackupRecord


class RollbackError(Exception):
    pass


class RollbackManager:
    def __init__(self, backups: BackupManager):
        self.backups = backups

    def restore(self, record: BackupRecord, *, keep_pre_undo: bool = True) -> None:
        backup = Path(record.backup)
        try:
            data = backup.read_bytes()
        except OSError as exc:
            raise RollbackError(f"backup unreadable: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != record.sha256:
            raise RollbackError(f"backup {backup.name} failed its checksum; refusing to restore")
        if keep_pre_undo and Path(record.original).exists():
            self.backups.create(record.original, kind="pre-undo")
        atomic_write_bytes(record.original, data, mode=record.mode)
        record.consumed = True
        record.save_meta()

    def undo(self, path: str | Path) -> BackupRecord:
        rec = self.backups.latest(path)
        if rec is None:
            raise RollbackError(f"no backup available for {path}")
        self.restore(rec)
        return rec
