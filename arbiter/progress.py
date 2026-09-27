"""Failure memory: notice when the agent keeps failing the same way, and force a change of approach.

A check run that fails is summarised as a *failure signature*: the check, plus the names of the
failing tests when the runner lists them (otherwise a normalised digest of the error lines). A
run counts as **no progress** when all of these hold:
- it repeats the previous signature of the same check;
- the code changed in between, with an edit touching files already edited during this streak;
- the number of failing tests did not go down.

After `intervene_at` consecutive no-progress failures (3: the first failure plus two failed
repairs of the same idea), the controller is told to intervene with a NEW HYPOTHESIS message. It
lists what was tried and what kept failing. If the same streak continues for `escalate_after`
more failures, the controller may end the attempt: a fresh attempt starts from the original code
and carries the observations, not the failed reasoning (see agent.py).

Deterministic, bounded, and based only on execution records, never on the model's own words.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

_VOLATILE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x_"),
    (re.compile(r"\b\d+(\.\d+)?\s*(s|ms|sec|seconds)\b"), "_s"),
    (re.compile(r"line \d+"), "line _"),
    (re.compile(r":\d+(:\d+)?\b"), ":_"),
    (re.compile(r"/[^\s:'\"]+/"), "/_/"),
    (re.compile(r"\b\d+\b"), "_"),
]
_ERROR_LINE = re.compile(r"(Error|Exception|assert|FAIL|panic|expected|Traceback|error\b)", re.I)


def error_digest(text: str) -> str:
    """Stable digest of the error-looking lines of an output (volatile tokens normalised)."""
    lines = [l.strip() for l in text.splitlines() if _ERROR_LINE.search(l)][-12:]
    norm = []
    for l in lines:
        for pat, rep in _VOLATILE:
            l = pat.sub(rep, l)
        norm.append(l)
    return hashlib.sha1("\n".join(norm).encode()).hexdigest()[:12] if norm else ""


def headline(text: str) -> str:
    """The most informative error line, for the intervention message."""
    for l in reversed(text.splitlines()):
        s = l.strip()
        if s and re.search(r"(Error|Exception|assert|FAIL|panic)", s) and not s.startswith(("File ", "at ")):
            return s[:160]
    return ""


@dataclass
class Streak:
    key: str
    signature: tuple
    count: int = 1
    files: dict[str, int] = field(default_factory=dict)
    failing: int = 0
    headline: str = ""
    intervened: bool = False
    escalated: bool = False


@dataclass
class FailureMemory:
    intervene_at: int = 3
    escalate_after: int = 2
    streaks: dict[str, Streak] = field(default_factory=dict)
    last_tree: dict[str, str] = field(default_factory=dict)
    no_progress_failures: int = 0
    before_intervention: int = 0
    after_intervention: int = 0
    interventions: int = 0
    escalations: int = 0

    def observe(self, key: str, tree: str, outcome: str, failing_tests: list[str] | None, counts: dict[str, int],
                output: str, changed_files: list[str]) -> str | None:
        """Record one check run. Returns "intervene", "escalate" or None."""
        prev_tree = self.last_tree.get(key)
        self.last_tree[key] = tree
        if outcome not in ("failed", "collection_error"):
            self.streaks.pop(key, None)
            return None
        sig = (key, tuple(failing_tests)) if failing_tests else (key, error_digest(output))
        failing = int(counts.get("failed", 0)) + int(counts.get("errors", 0))
        s = self.streaks.get(key)
        edited = prev_tree is not None and prev_tree != tree and bool(changed_files)
        # a streak that began before any edit (e.g. the reproduction run) adopts the first edited files
        same_region = s is not None and (not s.files or bool(set(changed_files) & set(s.files)))
        if s is not None and s.signature == sig and edited and same_region and failing >= s.failing:
            s.count += 1
            for f in changed_files:
                s.files[f] = s.files.get(f, 0) + 1
            s.failing = failing
            self.no_progress_failures += 1
            if s.intervened:
                self.after_intervention += 1
            else:
                self.before_intervention += 1
        elif s is not None and s.signature == sig and not edited:
            return None  # re-running without edits is the repetition notice's business
        else:
            s = Streak(key, sig, 1, {f: 1 for f in changed_files}, failing, headline(output))
            self.streaks[key] = s
            return None
        if not s.intervened and s.count >= self.intervene_at:
            s.intervened = True
            self.interventions += 1
            return "intervene"
        if s.intervened and not s.escalated and s.count >= self.intervene_at + self.escalate_after:
            s.escalated = True
            self.escalations += 1
            return "escalate"
        return None

    def reset_attempt(self) -> None:
        self.streaks.clear()
        self.last_tree.clear()

    def message(self, key: str, command: str) -> str:
        s = self.streaks[key]
        tried = ", ".join(f"{f} ({n}x)" for f, n in sorted(s.files.items(), key=lambda x: -x[1])[:6])
        failing = ", ".join(s.signature[1][:4]) if isinstance(s.signature[1], tuple) and s.signature[1] else s.headline
        return (
            f"NEW HYPOTHESIS NEEDED. `{command[:120]}` has now failed the same way {s.count} times after edits to the "
            f"same code, with no improvement.\n- Still failing: {failing or 'same error output'}\n- Edited meanwhile: "
            f"{tried}\nTreat the current explanation as wrong. Before editing again: state a different root cause, "
            "gather evidence for it (read the callers and callees of the failing code, the test's expectations and "
            "fixtures, or narrow the reproduction), and consider reverting edits that did not help."
        )

    def to_dict(self) -> dict[str, Any]:
        return {"no_progress_failures": self.no_progress_failures, "before_intervention": self.before_intervention,
                "after_intervention": self.after_intervention, "interventions": self.interventions,
                "escalations": self.escalations}
