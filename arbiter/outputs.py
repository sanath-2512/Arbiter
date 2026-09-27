"""Tool-output archive and bounded, diagnostic-preserving observation rendering.

Every tool result is archived verbatim (after secret redaction) under `outputs/<id>.txt`.
The model receives a bounded view. When lines are omitted the view says exactly which
line ranges were dropped and how to retrieve them, so an excerpt is never presented as the
complete output.
"""

from __future__ import annotations

import re
from pathlib import Path

from arbiter.records import Redactor

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
        """Terminal-clean text of an archived output (the file itself stays byte-exact)."""
        p = self.path(oid)
        return clean_terminal_text(p.read_text(encoding="utf-8", errors="replace")) if p else None


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_terminal_text(text: str) -> str:
    """What a terminal would show: no colour/cursor escapes, a carriage-return progress line reduced to
    its final state, no NUL or other control bytes. Saves tokens (tools that ignore NO_COLOR), keeps
    runner summaries parseable, and keeps bytes some providers reject out of requests."""
    if "\x1b" in text:
        text = _ANSI.sub("", text)
    if "\r" in text:
        text = "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
    return _CONTROL.sub("", text)


def _cap_line(line: str, lineno: int, oid: str | None) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    ref = f'; full line: read_output(id="{oid}", start_line={lineno}, end_line={lineno})' if oid else ""
    return line[:MAX_LINE_CHARS] + f" …[line {lineno} cut: {len(line)} chars{ref}]"


def _omission(a: int, b: int, oid: str | None) -> str:
    n = b - a + 1
    ref = f'; retrieve: read_output(id="{oid}", start_line={a}, end_line={b})' if oid else ""
    return f"[... {n} line{'s' if n != 1 else ''} omitted (lines {a}-{b}){ref} ...]"


# Output that costs tokens and carries no information for the model: build/download progress and
# runs of passing-test lines (cargo, pytest -v, go -v, unittest -v, TAP, jest/mocha ticks). Runs of at
# least FOLD_MIN such lines are folded into one marker that names the line range, so read_output can
# still retrieve them. Failures, errors and summaries are never folded.
FOLD_MIN = 6
FOLD_RULES = (
    ("build/download progress", re.compile(
        r"^\s*(Compiling|Checking|Downloaded|Downloading|Fresh|Updating|Locking|Adding|Blocking|Unpacking|Fetching|"
        r"Documenting|Collecting|Using cached|Requirement already satisfied|Obtaining|Preparing metadata|"
        r"Building wheel|Created wheel|Stored in directory|go: downloading|go: finding|go: extracting|"
        r"npm (WARN|notice|http))\b")),
    ("passing test lines", re.compile(
        r"^(test \S.* \.\.\. (ok|ignored)\s*$|\S+::\S+ PASSED\b|\s*--- PASS: |\s*=== (RUN|PAUSE|CONT) |"
        r"\s*(✓|✔|√) |ok \d+ |.*\) \.\.\. ok\s*$)")),
    # RUST_BACKTRACE frames inside the standard library (the project's own frames stay visible)
    ("standard-library backtrace frames", re.compile(
        r"^\s+(\d+: (core|std|alloc|test|__rust|rust_begin_unwind|<\w+ as core::)|at /rustc/[0-9a-f]+/library/)")),
)
_WARNING = re.compile(r"^warning(\[[\w-]+\])?: (?!`[^`]*` \(.*\) generated \d+ warning)")
WARNINGS_SHOWN = 2


def fold_runs(lines: list[str]) -> list[tuple[int, int, str]]:
    """(first, last, label) ranges of foldable lines, 0-based inclusive, non-overlapping."""
    folds: list[tuple[int, int, str]] = []
    n, i = len(lines), 0
    while i < n:
        rule = next((label for label, rx in FOLD_RULES if rx.search(lines[i])), None)
        if rule is None:
            i += 1
            continue
        rx = dict(FOLD_RULES)[rule]
        j = i
        while j + 1 < n and rx.search(lines[j + 1]):
            j += 1
        if j - i + 1 >= FOLD_MIN:
            folds.append((i, j, rule))
        i = j + 1
    # compiler warnings (rustc/cargo style blocks ending at a blank line): keep the first few
    blocks, i = [], 0
    while i < n:
        if _WARNING.match(lines[i]):
            j = i
            while j + 1 < n and lines[j + 1].strip() and not _WARNING.match(lines[j + 1]) \
                    and not lines[j + 1].startswith("error"):
                j += 1
            if j + 1 < n and not lines[j + 1].strip():
                j += 1
            blocks.append((i, j))
            i = j + 1
        else:
            i += 1
    group: list[tuple[int, int]] = []
    for b in blocks + [(-1, -1)]:
        if group and b[0] == group[-1][1] + 1:
            group.append(b)
            continue
        if len(group) > WARNINGS_SHOWN:
            a, z = group[WARNINGS_SHOWN][0], group[-1][1]
            if not any(f[0] <= z and a <= f[1] for f in folds):
                folds.append((a, z, f"{len(group) - WARNINGS_SHOWN} more compiler warnings"))
        group = [b]
    return sorted(folds)


def _fold_marker(a: int, b: int, label: str, oid: str | None) -> str:
    ref = f'; read_output(id="{oid}", start_line={a}, end_line={b})' if oid else ""
    return f"[... {b - a + 1} lines folded: {label} (lines {a}-{b}){ref} ...]"


def bounded_view(text: str, max_chars: int, oid: str | None, fold: bool = True) -> tuple[str, bool]:
    """Return (view, truncated). Folds noise (see FOLD_RULES), then keeps head, tail, and error-like
    lines from the middle."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    capped = [_cap_line(l, i + 1, oid) for i, l in enumerate(lines)]
    n = len(capped)
    folds = fold_runs(lines) if fold else []
    marker = {a: (a, b, label) for a, b, label in folds}
    hidden = {i for a, b, _ in folds for i in range(a, b + 1)}

    def render(keep: set[int]) -> str:
        out: list[str] = []
        k = 0
        while k < n:
            if k in marker:
                a, b, label = marker[k]
                out.append(_fold_marker(a + 1, b + 1, label, oid))
                k = b + 1
            elif k in keep:
                out.append(capped[k])
                k += 1
            else:
                start = k
                while k < n and k not in keep and k not in marker:
                    k += 1
                out.append(_omission(start + 1, k, oid))
        return "\n".join(out)

    visible = [i for i in range(n) if i not in hidden]
    if not folds:
        joined = "\n".join(capped)
        if len(joined) <= max_chars:
            return joined, joined != "\n".join(lines)
    else:
        view = render(set(visible))
        if len(view) <= max_chars:
            return view, True
    head_budget, tail_budget = int(max_chars * 0.30), int(max_chars * 0.45)
    mid_budget = max_chars - head_budget - tail_budget
    keep: set[int] = set()
    used = 0
    p = 0
    while p < len(visible) and used + len(capped[visible[p]]) + 1 <= head_budget:
        keep.add(visible[p])
        used += len(capped[visible[p]]) + 1
        p += 1
    head_end = p
    used = 0
    q = len(visible) - 1
    while q >= head_end and used + len(capped[visible[q]]) + 1 <= tail_budget:
        keep.add(visible[q])
        used += len(capped[visible[q]]) + 1
        q -= 1
    tail_start = q + 1
    used = 0
    for r in range(head_end, tail_start):
        if used >= mid_budget:
            break
        if SALIENT_RE.search(lines[visible[r]]):
            for w in visible[max(head_end, r - 1):min(tail_start, r + 3)]:
                if w not in keep and used + len(capped[w]) + 1 <= mid_budget:
                    keep.add(w)
                    used += len(capped[w]) + 1
    header = f"[output truncated: {n} lines, {len(text)} chars in total; showing {len(keep)} lines]"
    return header + "\n" + render(keep), True


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
