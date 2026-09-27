"""Durable record helpers: atomic JSON publication, JSONL appends, secret redaction."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish `data` at `path` so readers see either the old or the new complete file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n").encode())


def append_jsonl(path: Path, obj: Any, *, fsync: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, sort_keys=True, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[Any]:
    """Read JSONL, ignoring a torn final line (from an interrupted append)."""
    out = []
    path = Path(path)
    if not path.exists():
        return out
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return out


class Redactor:
    """Replace known secret values in text and nested JSON-like objects."""

    def __init__(self, secrets: list[str] | None = None):
        self._secrets: list[str] = []
        for s in secrets or []:
            self.add(s)

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def text(self, value: str) -> str:
        if not self._secrets or not isinstance(value, str):
            return value
        for s in self._secrets:
            if s in value:
                value = value.replace(s, REDACTED)
        return value

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str, limit: int = 100) -> str:
    """Filesystem-safe rendering of an identifier (the original is kept in records)."""
    cleaned = _SAFE_ID.sub("_", value).strip("._") or "task"
    return cleaned[:limit]
