"""Source file reading with exact round-tripping of encoding, BOM and line endings."""
from __future__ import annotations

import bisect
import io
import os
import stat
import tokenize
from dataclasses import dataclass
from pathlib import Path


class SourceError(Exception):
    """The file cannot be read or safely handled."""


@dataclass
class SourceFile:
    path: Path
    text: str                      # decoded text, original newlines retained
    encoding: str = "utf-8"
    bom: bool = False
    newline: str = "\n"            # dominant newline convention
    mixed_newlines: bool = False
    mode: int | None = None

    @classmethod
    def read(cls, path: str | Path) -> "SourceFile":
        p = Path(path)
        try:
            data = p.read_bytes()
            mode = stat.S_IMODE(os.stat(p).st_mode)
        except OSError as exc:
            raise SourceError(f"cannot read {p}: {exc}") from exc
        return cls.from_bytes(data, p, mode)

    @classmethod
    def from_bytes(cls, data: bytes, path: Path, mode: int | None = None) -> "SourceFile":
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(encoding)
        except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
            raise SourceError(f"cannot decode {path}: {exc}") from exc
        bom = encoding == "utf-8-sig"
        if bom:
            encoding = "utf-8"
        crlf = text.count("\r\n")
        lone_cr = text.replace("\r\n", "").count("\r")
        lf = text.count("\n") - crlf
        if lone_cr:
            raise SourceError(f"{path}: lone CR line endings are not supported")
        newline = "\r\n" if crlf > lf else "\n"
        return cls(p_(path), text, encoding, bom, newline, bool(crlf and lf), mode)

    def with_text(self, text: str) -> "SourceFile":
        return SourceFile(self.path, text, self.encoding, self.bom, self.newline, self.mixed_newlines, self.mode)

    def encode(self, text: str | None = None) -> bytes:
        text = self.text if text is None else text
        try:
            raw = text.encode(self.encoding)
        except UnicodeEncodeError as exc:
            raise SourceError(f"result cannot be encoded as {self.encoding}: {exc}") from exc
        return (b"\xef\xbb\xbf" + raw) if self.bom else raw

    def native_newlines(self, text: str) -> str:
        """Convert ``\\n`` in generated text to this file's newline convention."""
        if self.newline == "\n":
            return text
        return text.replace("\r\n", "\n").replace("\n", self.newline)


def p_(path: str | Path) -> Path:
    return path if isinstance(path, Path) else Path(path)


class LineIndex:
    """Maps AST ``(lineno, col_offset)`` pairs (UTF-8 byte columns) to byte offsets."""

    def __init__(self, data: bytes):
        self.data = data
        self.starts = [0]
        pos = data.find(b"\n")
        while pos != -1:
            self.starts.append(pos + 1)
            pos = data.find(b"\n", pos + 1)

    def offset(self, lineno: int, col: int) -> int:
        return self.starts[lineno - 1] + col

    def line_start(self, lineno: int) -> int:
        return self.starts[lineno - 1] if lineno - 1 < len(self.starts) else len(self.data)

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def line_text(self, lineno: int) -> bytes:
        if lineno - 1 >= len(self.starts):
            return b""
        end = self.starts[lineno] if lineno < len(self.starts) else len(self.data)
        return self.data[self.starts[lineno - 1]:end].rstrip(b"\r\n")

    def char_column(self, lineno: int, byte_col: int) -> int:
        """1-based character column for display."""
        line = self.line_text(lineno)
        return len(line[:byte_col].decode("utf-8", "replace")) + 1
