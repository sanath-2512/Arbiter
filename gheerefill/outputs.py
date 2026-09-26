"""Tool-output archive and bounded, diagnostic-preserving observation rendering.

Every tool result is archived verbatim (after secret redaction) under `outputs/<id>.txt`.
The model receives a bounded view. When lines are omitted the view says exactly which
line ranges were dropped and how to retrieve them, so an excerpt is never presented as the
complete output.
"""

from __future__ import annotations

import re
from pathlib import Path

from gheerefill.records import Redactor

SALIENT_RE = re.compile(
    r"(error|exception|traceback|failed|failure|fail:|assert|fatal|panic|segmentation fault|"
    r"undefined|cannot|not found|no such|denied|warning:|expected|actual|mismatch|E   )",
    re.I,
)
MAX_LINE_CHARS = 1200
_ID_RE = re.compile(r"^o\d+$")


class OutputArchive:
    def __init__(self, directory: Path, redactor: Redactor | None = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        existing = [int(p.stem[1:]) for p in self.dir.glob("o*.txt") if p.stem[1:].isdigit()]
        self._next = max(existing, default=0) + 1

    def allocate(self) -> tuple[str, Path]:
        oid = f"o{self._next}"
        self._next += 1
        return oid, self.dir / f"{oid}.txt"

    def store(self, text: str) -> str:
        oid, path = self.allocate()
        path.write_text(self.redactor.text(text), encoding="utf-8")
        return oid

    def redact_file(self, path: Path) -> None:
        """Rewrite an archived file if it contains a known secret."""
        if not self.redactor._secrets:
            return
        data = path.read_bytes()
        if any(s.encode() in data for s in self.redactor._secrets):
            path.write_text(self.redactor.text(data.decode("utf-8", errors="replace")), encoding="utf-8")

    def path(self, oid: str) -> Path | None:
        if not _ID_RE.match(oid or ""):
            return None
        p = self.dir / f"{oid}.txt"
        return p if p.exists() else None

    def read_text(self, oid: str) -> str | None:
        p = self.path(oid)
        return p.read_text(encoding="utf-8", errors="replace") if p else None


def _cap_line(line: str, lineno: int, oid: str | None) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    ref = f'; full line: read_output(id="{oid}", start_line={lineno}, end_line={lineno})' if oid else ""
    return line[:MAX_LINE_CHARS] + f" …[line {lineno} cut: {len(line)} chars{ref}]"


def _omission(a: int, b: int, oid: str | None) -> str:
    n = b - a + 1
    ref = f'; retrieve: read_output(id="{oid}", start_line={a}, end_line={b})' if oid else ""
    return f"[... {n} line{'s' if n != 1 else ''} omitted (lines {a}-{b}){ref} ...]"


def bounded_view(text: str, max_chars: int, oid: str | None) -> tuple[str, bool]:
    """Return (view, truncated). Keeps head, tail, and error-like lines from the middle."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    capped = [_cap_line(l, i + 1, oid) for i, l in enumerate(lines)]
    joined = "\n".join(capped)
    if len(joined) <= max_chars:
        return joined, joined != "\n".join(lines)
    n = len(capped)
    head_budget, tail_budget = int(max_chars * 0.30), int(max_chars * 0.45)
    mid_budget = max_chars - head_budget - tail_budget
    keep: set[int] = set()
    used = 0
    i = 0
    while i < n and used + len(capped[i]) + 1 <= head_budget:
        keep.add(i)
        used += len(capped[i]) + 1
        i += 1
    head_end = i
    used = 0
    j = n - 1
    while j >= head_end and used + len(capped[j]) + 1 <= tail_budget:
        keep.add(j)
        used += len(capped[j]) + 1
        j -= 1
    tail_start = j + 1
    used = 0
    for k in range(head_end, tail_start):
        if used >= mid_budget:
            break
        if SALIENT_RE.search(lines[k]):
            for w in range(max(head_end, k - 1), min(tail_start, k + 3)):
                if w not in keep and used + len(capped[w]) + 1 <= mid_budget:
                    keep.add(w)
                    used += len(capped[w]) + 1
    out: list[str] = []
    k = 0
    while k < n:
        if k in keep:
            out.append(capped[k])
            k += 1
        else:
            start = k
            while k < n and k not in keep:
                k += 1
            out.append(_omission(start + 1, k, oid))
    header = f"[output truncated: {n} lines, {len(text)} chars in total; showing {len(keep)} lines]"
    return header + "\n" + "\n".join(out), True


def numbered_range(text: str, start: int, end: int | None, max_lines: int, max_chars: int) -> tuple[str, int, int, int]:
    """Render lines [start, end] (1-based, inclusive) with line numbers, bounded.
    Returns (rendered, first_shown, last_shown, total_lines)."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    total = len(lines)
    if total == 0:
        return "", 0, 0, 0
    end = total if end is None else min(end, total)
    end = min(end, start + max_lines - 1)
    out, used, last = [], 0, start - 1
    for ln in range(start, end + 1):
        row = f"{ln:>6}\t{_cap_line(lines[ln - 1], ln, None)}"
        if used + len(row) + 1 > max_chars and out:
            break
        out.append(row)
        used += len(row) + 1
        last = ln
    return "\n".join(out), start, last, total
