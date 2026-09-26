"""Proof-carrying patches: execution evidence, computed by the harness, that a candidate fixes the
issue without breaking what worked before. The model's own claims are never evidence.

The same command is compared on three states:
  original        the base tree (code before any change);
  counterfactual  the base tree plus the candidate's own test-file changes, so tests the
                  candidate added exist but the source fix does not (SWE-bench's FAIL_TO_PASS
                  idea, computed without any reference solution);
  candidate       the candidate tree.
Per check this yields fail→pass (the check demonstrates the fix), pass→fail (a regression the
candidate introduced) and still-failing tests (pre-existing or unfixed). Failing tests are
compared by name when the runner lists them, otherwise by counts.

Levels, strongest first (deterministic; `assess`):
  proven      >= 1 fail→pass check, no regression, every relevant check compared, and no
              existing test file modified (so the evidence does not rest on edited tests)
  fixed       >= 1 fail→pass check and no regression found (some comparisons missing, or
              existing tests were modified)
  passing     conclusive passes on the candidate, but nothing shown to fail without the fix
  unverified  no conclusive evidence
  refuted     the evidence contradicts the candidate: a regression, or a confirmed
              reproduction still failing on it
Candidates are ranked by (level, fail→pass checks, agreement with other candidates, fewer
failing checks, smaller patch, later attempt). Agreement follows CodeT's dual execution
agreement (Chen et al., 2022): candidates passing the same set of checks form a consensus set,
scored by its size times the number of checks it passes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from gheerefill.evidence import CONCLUSIVE_FAIL, CONCLUSIVE_PASS, CheckOutcome, VerificationRecord, classify_output

LEVELS = ("refuted", "unverified", "passing", "fixed", "proven")  # ascending strength
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|testing|__tests__|spec|specs|testdata|fixtures)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|"
    r"\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$|Test\.java$|Tests?\.(kt|cs|swift)$|_spec\.rb$|_test\.rb$"
)
MAX_FAILING = 500


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


# ------------------------------------------------------------------------------ output parsing
_UNITTEST_ID = re.compile(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", re.M)


def failing_tests(text: str, runner: str | None) -> list[str] | None:
    """Names of failing tests reported in a runner's output; None when they cannot be listed."""
    names: list[str] = []
    if runner == "pytest":
        names = re.findall(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", text, re.M)
        names += re.findall(r"^(\S+::\S+) (?:FAILED|ERROR)\b", text, re.M)
    elif runner == "unittest":
        for method, where in _UNITTEST_ID.findall(text):
            names.append(where if where.endswith("." + method) else f"{where}.{method}")
    elif runner == "go":
        names = re.findall(r"^\s*--- FAIL: (\S+)", text, re.M)
    elif runner == "cargo":
        names = re.findall(r"^test (\S+) \.\.\. FAILED", text, re.M)
    elif runner == "jest/vitest":
        names = [n.strip() for n in re.findall(r"^\s*● (.+?)\s*$", text, re.M) if "Test suite failed to run" not in n]
        names += [n.strip() for n in re.findall(r"^\s*[✕×] (.+?)(?: \(\d+ ?m?s\))?\s*$", text, re.M)]
    elif runner == "rspec":
        names = re.findall(r"^rspec (\S+)", text, re.M)
    elif runner == "junit":
        names = re.findall(r"^\[ERROR\]\s+([\w.$]+\.\w+)(?::\d+)?\s", text, re.M)
        names += re.findall(r"^\[ERROR\]\s+(\w+)\([\w.$]+\)\s+Time elapsed", text, re.M)
    else:
        return None
    return sorted(set(names))[:MAX_FAILING]


def classify_reproduction(text: str, exit_code: int | None, *, timed_out: bool = False) -> CheckOutcome:
    """A registered reproduction passes when it exits 0 and fails otherwise, unless it is a test runner
    whose summary says more (a runner that ran no tests is not a pass)."""
    oc = classify_output(text, exit_code, timed_out=timed_out)
    if oc.runner is not None and oc.outcome != "inconclusive":
        return oc
    if timed_out:
        return CheckOutcome("timeout", detail="reproduction timed out")
    if exit_code is None:
        return CheckOutcome("inconclusive", detail="reproduction was interrupted")
    if exit_code == 0:
        return CheckOutcome("passed", "exit-code", {}, "exit code 0")
    return CheckOutcome("failed", "exit-code", {}, f"exit code {exit_code}")


def verdict(rec: VerificationRecord | None) -> str | None:
    if rec is None or rec.binding != "exact":
        return None
    if rec.outcome in CONCLUSIVE_PASS:
        return "pass"
    if rec.outcome in CONCLUSIVE_FAIL:
        return "fail"
    return None


def latest(records: list[VerificationRecord], check_key: str, tree: str) -> VerificationRecord | None:
    found = None
    for r in records:
        if r.check_key == check_key and r.tree == tree and r.binding == "exact":
            found = r
    return found


# ------------------------------------------------------------------------------ comparison
@dataclass
class Comparison:
    check_key: str
    command: str
    kind: str  # check | reproduction
    original: str | None  # pass | fail | None (not compared)
    candidate: str | None
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_fail: list[str] = field(default_factory=list)
    still_failing: list[str] = field(default_factory=list)
    counts_original: dict[str, int] = field(default_factory=dict)
    counts_candidate: dict[str, int] = field(default_factory=dict)
    verdict: str = "incomplete"  # fail_to_pass | pass_to_fail | pass_to_pass | fail_to_fail | incomplete
    detail: str = ""

    @property
    def shows_fix(self) -> bool:
        return self.verdict == "fail_to_pass" or (self.verdict == "fail_to_fail" and bool(self.fail_to_pass)
                                                  and not self.pass_to_fail)

    @property
    def shows_regression(self) -> bool:
        return self.verdict == "pass_to_fail" or bool(self.pass_to_fail)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["shows_fix"], d["shows_regression"] = self.shows_fix, self.shows_regression
        return d


def compare(check_key: str, command: str, kind: str, orig: VerificationRecord | None,
            cand: VerificationRecord | None) -> Comparison:
    c = Comparison(check_key, command, kind, verdict(orig), verdict(cand))
    if orig is not None:
        c.counts_original = dict(orig.counts)
    if cand is not None:
        c.counts_candidate = dict(cand.counts)
    if c.original is None or c.candidate is None:
        missing = "original" if c.original is None else "candidate"
        c.detail = f"no conclusive run on the {missing} state"
        return c
    f_orig = orig.failing if orig and orig.failing is not None else None
    f_cand = cand.failing if cand and cand.failing is not None else None
    if c.original == "fail" and c.candidate == "pass":
        c.verdict, c.fail_to_pass = "fail_to_pass", list(f_orig or [])
        c.detail = "fails without the change, passes with it"
    elif c.original == "pass" and c.candidate == "fail":
        c.verdict, c.pass_to_fail = "pass_to_fail", list(f_cand or [])
        c.detail = "passes without the change, fails with it"
    elif c.original == "pass":
        c.verdict, c.detail = "pass_to_pass", "passes with and without the change"
    else:
        c.verdict = "fail_to_fail"
        if f_orig is not None and f_cand is not None and (f_orig or f_cand):
            gone = sorted(set(f_orig) - set(f_cand))
            # A test that vanished (deleted/renamed) is not a fix: require no fewer passing tests.
            if c.counts_candidate.get("passed", 0) >= c.counts_original.get("passed", 0):
                c.fail_to_pass = gone
            c.pass_to_fail = sorted(set(f_cand) - set(f_orig))
            c.still_failing = sorted(set(f_cand) & set(f_orig))
            c.detail = (f"{len(c.fail_to_pass)} fixed, {len(c.pass_to_fail)} newly failing, "
                        f"{len(c.still_failing)} failing before and after")
        else:
            fo = c.counts_original.get("failed", 0) + c.counts_original.get("errors", 0)
            fc = c.counts_candidate.get("failed", 0) + c.counts_candidate.get("errors", 0)
            c.detail = f"fails before and after ({fo} -> {fc} failing; test names not available)"
            if fc > fo and kind == "check":
                c.pass_to_fail = [f"{fc - fo} more failing test(s) than without the change"]
    return c


# ------------------------------------------------------------------------------ assessment
@dataclass
class Assessment:
    tree: str
    level: str
    comparisons: list[Comparison]
    tests_modified: list[str]
    reasons: list[str]
    attempt: int = 1
    diff_lines: int = 0
    pass_set: tuple[str, ...] = ()
    agreement: int = 0

    @property
    def rank(self) -> tuple:
        fixes = sum(1 for c in self.comparisons if c.shows_fix)
        failing = sum(1 for c in self.comparisons if c.candidate == "fail")
        return (LEVELS.index(self.level), fixes, self.agreement, -failing, -self.diff_lines, self.attempt)

    def summary(self) -> str:
        fixes = [c for c in self.comparisons if c.shows_fix]
        regress = [c for c in self.comparisons if c.shows_regression]
        parts = []
        if fixes:
            parts.append(f"{len(fixes)} check(s) fail without the change and pass with it")
        if regress:
            names = [n for c in regress for n in c.pass_to_fail][:5]
            parts.append(f"regressions: {', '.join(names) or len(regress)}")
        parts += self.reasons
        return "; ".join(parts) or "no conclusive check on this candidate"

    def to_dict(self) -> dict[str, Any]:
        return {"tree": self.tree, "level": self.level, "attempt": self.attempt, "summary": self.summary(),
                "comparisons": [c.to_dict() for c in self.comparisons], "tests_modified": self.tests_modified,
                "diff_lines": self.diff_lines, "agreement": self.agreement, "rank": list(self.rank)}


def assess(tree: str, comparisons: list[Comparison], *, tests_modified: list[str], expected: int,
           reproductions_failing: list[str] = ()) -> Assessment:
    """`expected`: number of checks that should have been compared (for the `proven` level)."""
    reasons: list[str] = []
    regressions = [c for c in comparisons if c.shows_regression]
    fixes = [c for c in comparisons if c.shows_fix]
    complete = expected > 0 and sum(1 for c in comparisons if c.verdict != "incomplete") >= expected
    if reproductions_failing:
        reasons.append(f"confirmed reproduction(s) still failing: {', '.join(reproductions_failing)}")
    if regressions or reproductions_failing:
        level = "refuted"
    elif fixes and complete and not tests_modified:
        level = "proven"
    elif fixes:
        level = "fixed"
        if tests_modified:
            reasons.append("existing test files were modified, so the evidence is not independent of the change")
        if not complete:
            reasons.append("not every check could be compared")
    elif any(c.candidate == "pass" for c in comparisons):
        level = "passing"
        reasons.append("no check was shown to fail without the change")
    else:
        level = "unverified"
    a = Assessment(tree, level, comparisons, list(tests_modified), reasons)
    a.pass_set = tuple(sorted(c.check_key for c in comparisons if c.candidate == "pass"))
    return a


def rank_candidates(assessments: list[Assessment]) -> list[Assessment]:
    """Best first. Sets CodeT-style agreement: size of the consensus set x checks passed."""
    for a in assessments:
        same = sum(1 for b in assessments if b.pass_set == a.pass_set)
        a.agreement = same * len(a.pass_set)
    return sorted(assessments, key=lambda a: a.rank, reverse=True)
