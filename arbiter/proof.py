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
  refuted     authoritative evidence contradicts it (a regression in existing tests), or it
              fails a qualified generated check that another regression-free candidate passes
Generated evidence (registered reproductions, tests the candidate added) is advisory unless qualified;
see "authority" below. Exploration (another attempt) may be triggered by advisory evidence; discarding
a candidate never is.
Candidates are ranked by (level, fail→pass checks, agreement with other candidates, fewer
failing checks, smaller patch, later attempt). Agreement follows CodeT's dual execution
agreement (Chen et al., 2022): candidates passing the same set of checks form a consensus set,
scored by its size times the number of checks it passes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from arbiter.evidence import CONCLUSIVE_FAIL, CONCLUSIVE_PASS, CheckOutcome, VerificationRecord, classify_output

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
        names = re.findall(r"^test (.+?) \.\.\. FAILED", text, re.M)
        # `cargo test -q` prints no per-test lines: the "failures:" list closing each binary names them
        for block in re.findall(r"^failures:\n((?:    \S.*\n)+)", text, re.M):
            names += [l.strip() for l in block.splitlines() if l.strip()]
    elif runner == "jest/vitest":
        names = [n.strip() for n in re.findall(r"^\s*● (.+?)\s*$", text, re.M) if "Test suite failed to run" not in n]
        names += [re.sub(r"\s+\(?\d+(?:\.\d+)? ?m?s\)?$", "", n.strip())
                  for n in re.findall(r"^\s*[✕×] (.+?)\s*$", text, re.M)]
        names += [n.strip() for n in re.findall(r"^\s*FAIL\s+\S+ > (.+?)\s*$", text, re.M)]  # vitest detail headers
        names = [n for n in names if not re.match(r"^\S+\.(?:m?[jt]sx?) \(", n)]
    elif runner == "mocha":
        after = text.split(" failing", 1)[1] if " failing" in text else ""
        for m in re.finditer(r"^[ \t]*\d+\) (.+?)\n((?:[ \t]{4,}\S[^\n]*?:[ \t]*\n)*)", after, re.M):
            parts = [m.group(1).rstrip(":")] + [l.strip().rstrip(":") for l in m.group(2).splitlines() if l.strip()]
            names.append(" ".join(parts))
    elif runner == "tap":
        names = [n.strip() for n in re.findall(r"^\s*not ok \d+(?: -)? (.+?)(?:\s+# (?!SKIP|TODO).*)?$", text, re.M)
                 if not re.search(r"# (SKIP|TODO)", n, re.I)]
    elif runner == "minitest":
        names = re.findall(r"^\s*\d+\) (?:Failure|Error):\n(\S+?#\S+?)(?: \[|:|$)", text, re.M)
    elif runner == "gradle":
        names = [f"{c}.{t}" for c, t in re.findall(r"^(\S+) > (.+?) FAILED\s*$", text, re.M)]
    elif runner == "phpunit":
        names = re.findall(r"^\d+\) ([\w\\]+::\w+)", text, re.M)
    elif runner == "dotnet":
        names = re.findall(r"^\s*Failed (\S+) \[", text, re.M)
    elif runner == "ctest":
        names = re.findall(r"^\s*\d+ - (\S+) \((?:Failed|SEGFAULT|Timeout|Not Run)\)", text, re.M)
    elif runner == "exunit":
        names = [f"{mod} {t}" for t, mod in re.findall(r"^\s+\d+\) test (.+?) \((\S+)\)", text, re.M)]
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


# ------------------------------------------------------------------------------ authority
# Evidence tiers. AUTHORITATIVE: tests that exist in the original repository and that the candidate did
# not rewrite. GENERATED: registered reproductions and tests the candidate added. Generated evidence is
# ADVISORY unless QUALIFIED: it fails on the original code for a behavioural reason (not a broken
# environment), gives the same verdict twice, and does not assert on private internals. Only
# authoritative evidence can refute a candidate on its own; a qualified generated check refutes a
# candidate only comparatively, when another candidate without regressions passes it.
ENV_ERROR = re.compile(r"ModuleNotFoundError|SyntaxError|IndentationError|TabError|command not found|"
                       r"No such file or directory|can't open file|fixture '[^']+' not found|ERROR collecting|"
                       r"Permission denied|ImportError while importing test module", re.I)
MISSING_API = re.compile(r"ImportError|cannot import name|AttributeError|has no attribute|unexpected keyword argument|"
                         r"NameError|is not defined|TypeError: .*argument", re.I)
IMPL_SPECIFIC = re.compile(r"\._[a-z]\w*\b|\bmock\.patch(\.object)?\(|\bpatch\([\"'][\w.]+\._|monkeypatch\.setattr\(|"
                           r"MagicMock\(", re.I)


def test_name(test_id: str) -> str:
    """The identifying name inside a runner's test id (pytest/unittest/go/cargo/rspec/jest)."""
    t = re.sub(r"\[.*\]$", "", test_id.strip())
    for sep in ("::", "."):
        if sep in t and not t.startswith("./"):
            t = t.split(sep)[-1]
    return t.split("/")[0] if re.match(r"^Test\w+/", t) else t


def qualify(orig_output: str, orig_outcome: str | None, *, task_kind: str, impl_specific: bool,
            stable: bool | None) -> tuple[bool, str]:
    """Is a generated check's failure on the original code trustworthy evidence?"""
    if orig_outcome == "collection_error" and task_kind != "feature":
        return False, "on the original code it could not even run (collection error)"
    if ENV_ERROR.search(orig_output or ""):
        return False, "on the original code it failed with an environment error, not the reported behaviour"
    if MISSING_API.search(orig_output or "") and task_kind not in ("feature", "refactor"):
        return False, "on the original code it failed on a missing name, not on the reported behaviour"
    if impl_specific:
        return False, "it asserts on private internals or mocks, so it may reject a correct alternative fix"
    if stable is False:
        return False, "it gave different verdicts on repeated runs of the original code"
    return True, "fails on the original code for a behavioural reason"


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
    # authority split (see above)
    authoritative_fail_to_pass: list[str] = field(default_factory=list)
    generated_fail_to_pass: list[str] = field(default_factory=list)
    authoritative_regressions: list[str] = field(default_factory=list)
    advisory_regressions: list[str] = field(default_factory=list)
    generated: bool = False  # the check itself is generated (a registered reproduction script, or only new tests)
    qualified: bool | None = None  # for generated evidence
    qualification: str = ""

    @property
    def shows_fix(self) -> bool:
        return self.verdict == "fail_to_pass" or (self.verdict == "fail_to_fail" and bool(self.fail_to_pass)
                                                  and not self.pass_to_fail)

    @property
    def authoritative_fix(self) -> bool:
        return self.shows_fix and bool(self.authoritative_fail_to_pass)

    @property
    def qualified_fix(self) -> bool:
        return self.shows_fix and bool(self.generated_fail_to_pass) and bool(self.qualified)

    @property
    def shows_regression(self) -> bool:
        return bool(self.authoritative_regressions)

    @property
    def advisory_failure(self) -> bool:
        """Generated evidence that the candidate does not satisfy (it never refutes on its own)."""
        return self.generated and self.original == "fail" and self.candidate == "fail"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(shows_fix=self.shows_fix, shows_regression=self.shows_regression,
                 authoritative_fix=self.authoritative_fix, qualified_fix=self.qualified_fix,
                 advisory_failure=self.advisory_failure)
        return d


def compare(check_key: str, command: str, kind: str, orig: VerificationRecord | None,
            cand: VerificationRecord | None, *, is_generated_test=None, generated_check: bool | None = None,
            orig_output: str = "", task_kind: str = "bug", impl_specific: bool = False,
            stable: bool | None = None) -> Comparison:
    """`is_generated_test(test_id)`: True for tests the candidate added or rewrote. `generated_check`:
    the whole check is generated (registered reproduction script); default: reproductions are generated."""
    is_gen = is_generated_test or (lambda _t: False)
    c = Comparison(check_key, command, kind, verdict(orig), verdict(cand))
    c.generated = (kind == "reproduction") if generated_check is None else generated_check
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

    # --- authority split
    fixes = c.fail_to_pass if c.fail_to_pass else (["(whole check)"] if c.verdict == "fail_to_pass" else [])
    for t in fixes:
        gen = c.generated or (t != "(whole check)" and is_gen(t)) or (t == "(whole check)" and bool(
            generated_check))
        (c.generated_fail_to_pass if gen else c.authoritative_fail_to_pass).append(t)
    for t in c.pass_to_fail:
        (c.advisory_regressions if c.generated or is_gen(t) else c.authoritative_regressions).append(t)
    if c.generated_fail_to_pass or c.advisory_failure:
        c.qualified, c.qualification = qualify(orig_output, orig.outcome if orig else None, task_kind=task_kind,
                                               impl_specific=impl_specific, stable=stable)
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
    advisory: list[str] = field(default_factory=list)

    @property
    def rank(self) -> tuple:
        auth = sum(1 for c in self.comparisons if c.authoritative_fix)
        qual = sum(1 for c in self.comparisons if c.qualified_fix)
        failing = sum(1 for c in self.comparisons if c.candidate == "fail" and not c.generated)
        return (LEVELS.index(self.level), auth, qual, self.agreement, -failing, -self.diff_lines, self.attempt)

    def summary(self) -> str:
        auth = [c for c in self.comparisons if c.authoritative_fix]
        qual = [c for c in self.comparisons if c.qualified_fix and not c.authoritative_fix]
        adv = [c for c in self.comparisons if c.shows_fix and not c.authoritative_fix and not c.qualified_fix]
        regress = [c for c in self.comparisons if c.shows_regression]
        parts = []
        if auth:
            parts.append(f"{len(auth)} existing check(s) fail without the change and pass with it")
        if qual:
            parts.append(f"{len(qual)} qualified generated check(s) fail without the change and pass with it")
        if adv:
            parts.append(f"{len(adv)} generated check(s) show the fix but are advisory only")
        if regress:
            names = [n for c in regress for n in c.authoritative_regressions][:5]
            parts.append(f"regressions: {', '.join(names) or len(regress)}")
        parts += self.reasons + [f"advisory: {a}" for a in self.advisory]
        return "; ".join(parts) or "no conclusive check on this candidate"

    def to_dict(self) -> dict[str, Any]:
        return {"tree": self.tree, "level": self.level, "attempt": self.attempt, "summary": self.summary(),
                "comparisons": [c.to_dict() for c in self.comparisons], "tests_modified": self.tests_modified,
                "diff_lines": self.diff_lines, "agreement": self.agreement, "rank": list(self.rank),
                "advisory": self.advisory}


def assess(tree: str, comparisons: list[Comparison], *, tests_modified: list[str], expected: int,
           task_kind: str = "bug") -> Assessment:
    """`expected`: number of checks that should have been compared (for the `proven` level)."""
    reasons: list[str] = []
    advisory = [f"`{c.command[:80]}` still fails ({c.qualification or 'generated check'})"
                for c in comparisons if c.advisory_failure]
    advisory += [f"`{c.command[:80]}`: {', '.join(c.advisory_regressions[:3])} newly failing in generated tests"
                 for c in comparisons if c.advisory_regressions]
    regressions = [c for c in comparisons if c.shows_regression]
    strong = [c for c in comparisons if c.authoritative_fix or c.qualified_fix]
    weak = [c for c in comparisons if c.shows_fix and c not in strong]
    complete = expected > 0 and sum(1 for c in comparisons if c.verdict != "incomplete") >= expected
    if regressions:
        level = "refuted"
    elif strong and complete and not tests_modified:
        level = "proven"
    elif strong or weak:
        level = "fixed"
        if tests_modified:
            reasons.append("existing tests were rewritten, so the evidence is not independent of the change"
                           + (" (expected for this task)" if task_kind == "test_maintenance" else ""))
        if not complete:
            reasons.append("not every check could be compared")
        if weak and not strong:
            reasons.append("the fix is shown only by unqualified generated checks: "
                           + "; ".join(c.qualification for c in weak if c.qualification)[:200])
    elif any(c.candidate == "pass" for c in comparisons):
        level = "passing"
        if task_kind != "refactor":
            reasons.append("no check was shown to fail without the change")
    else:
        level = "unverified"
    a = Assessment(tree, level, comparisons, list(tests_modified), reasons, advisory=advisory)
    a.pass_set = tuple(sorted(c.check_key for c in comparisons if c.candidate == "pass"))
    return a


def rank_candidates(assessments: list[Assessment]) -> list[Assessment]:
    """Best first. Applies comparative refutation for qualified generated checks, then CodeT-style
    agreement (size of the consensus set x checks passed)."""
    by_key: dict[str, list[tuple[Assessment, Comparison]]] = {}
    for a in assessments:
        for c in a.comparisons:
            by_key.setdefault(c.check_key, []).append((a, c))
    for key, pairs in by_key.items():
        gen = [(a, c) for a, c in pairs if c.generated and c.original == "fail" and c.qualified is not False]
        passers = [a for a, c in gen if c.candidate == "pass" and a.level != "refuted"]
        if not passers:
            continue
        for a, c in gen:
            if c.candidate == "fail" and a.level != "refuted":
                a.level = "refuted"
                a.reasons.append(f"fails the qualified generated check `{c.command[:60]}` that attempt "
                                 f"{passers[0].attempt} satisfies without regressions")
    for a in assessments:
        same = sum(1 for b in assessments if b.pass_set == a.pass_set)
        a.agreement = same * len(a.pass_set)
    return sorted(assessments, key=lambda a: a.rank, reverse=True)
