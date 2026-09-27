"""What kind of engineering work the issue asks for, and therefore what evidence to expect.

Different tasks need different proof: a bug fix should make something fail-then-pass; a feature may
have no failing test until one is written for the new behaviour (a missing name on the original code
is then the expected failure); a refactoring must change nothing observable, so its evidence is
"existing tests still pass"; test maintenance changes existing tests on purpose; a build/config
problem is reproduced by the failing build command; a regression can be located in history.

Keyword heuristics over the issue text (title weighted), deterministic, with the matched phrase
recorded. Misclassification only changes a one-line strategy hint and small evidence-policy details;
the model sees the issue itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TYPES = ("test_maintenance", "build_config", "regression", "refactor", "feature", "bug")
PATTERNS = {
    "test_maintenance": r"\b(update|adjust|fix|rewrite)\s+(the\s+)?(unit\s+)?tests?\b|\btests?\s+(are|is)\s+(outdated|"
                        r"obsolete|stale)\b|\bflaky\s+tests?\b",
    "build_config": r"\b(fails?|failing|broken|error)\b[^.\n]{0,60}\b(to\s+)?(build|install|package|compile)\b|"
                    r"\b(pip install|setup\.py|pyproject(\.toml)?|wheel|sdist|build backend|packaging|entry[ -]points?|"
                    r"requirements\.txt|tox\.ini|ci workflow|github actions|dockerfile|makefile|toolchain|linker)\b",
    "regression": r"\bregress(ion|ed)?\b|\bused to work\b|\bworked (fine |correctly )?(in|before|until|with)\b|"
                  r"\bno longer (works?|supports?|accepts?|handles?)\b|\bstopped working\b|\bbroke(n)? (after|since|in)\b|"
                  r"\bsince (upgrading|updating|version|v?\d+\.\d+)\b|\bafter (upgrading|updating) to\b",
    "refactor": r"\brefactor(ing)?\b|\brestructur(e|ing)\b|\breorgani[sz](e|ing)\b|\bmove\b[^.\n]{0,60}\b(into|to)\b"
                r"[^.\n]{0,30}\b(module|package|file|class)\b|\brename\b[^.\n]{0,40}\b(module|function|class|package)\b|"
                r"\bextract\b[^.\n]{0,40}\binto\b|\bdeduplicat(e|ion)\b|\b(without|not) chang(e|ing) (the )?behaviou?r\b|"
                r"\bpreserv(e|ing) (the )?(existing )?behaviou?r\b",
    "feature": r"\bfeature request\b|\badd (support|an? option|a (new )?(flag|parameter|argument|setting|method|"
               r"function|command))\b|\b(support|allow|enable)\b[^.\n]{0,40}\b(new|custom|additional|optional)\b|"
               r"\bimplement\b|\bintroduce\b|\bwould be (nice|useful|great)\b|\bit would help if\b",
    "bug": r"\b(bug|incorrect(ly)?|wrong|crash(es|ed)?|traceback|fails?|failed|rais(e|es|ed|ing)|unexpected(ly)?|"
           r"doesn'?t work|does not work|broken|returns? \w+ instead|\w*error|\w*exception)\b",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}

HINTS = {
    "regression": "This reads like a regression (it used to work). The history is available: `git log -S<text>`, "
                  "`git log -p -- <file>` or `git bisect` can locate the change that broke it.",
    "feature": "This reads like a feature request: there may be no failing test yet. Write a test for the requested "
               "behaviour first (it may fail because the new name does not exist yet), register it as the "
               "reproduction, then implement it.",
    "refactor": "This reads like a refactoring: behaviour must not change. Identify the existing tests that cover the "
                "code, run them before and after; a failing reproduction is not expected.",
    "build_config": "This reads like a build or configuration problem: reproduce it with the failing build, install or "
                    "test command and register that command as the reproduction.",
    "test_maintenance": "This asks for changed behaviour together with updated tests: editing existing tests is expected "
                        "here, but keep the rest of the suite passing and do not weaken unrelated tests.",
}


@dataclass
class TaskType:
    kind: str
    confidence: str  # high (title match) | medium (body match) | low (default)
    matched: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "confidence": self.confidence, "matched": self.matched}


def classify(issue: str) -> TaskType:
    text = issue.strip()
    title = text.split("\n", 1)[0]
    for kind in TYPES[:-1]:  # priority order; "bug" is the fallback
        m = _COMPILED[kind].search(title)
        if m:
            return TaskType(kind, "high", m.group(0))
    for kind in TYPES[:-1]:
        m = _COMPILED[kind].search(text[:4000])
        if m:
            return TaskType(kind, "medium", m.group(0))
    m = _COMPILED["bug"].search(text[:4000])
    return TaskType("bug", "medium" if m else "low", m.group(0) if m else "")


def hint(t: TaskType) -> str:
    return HINTS.get(t.kind, "") if t.confidence in ("high", "medium") else ""
