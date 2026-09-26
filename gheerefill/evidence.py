"""Verification evidence: test-runner output classification, records bound to exact
candidate trees, and deterministic candidate selection.

A record is bound to the tree captured immediately before the check ran. If the tree
changed while the check ran (e.g. `sed -i ... && pytest`), the record is marked
`binding="mutated"` and is not used for comparisons. Evidence for one tree is never
reused for another: a content change produces a new tree id with no records.

Outcomes: passed | failed | collection_error | no_tests | skipped_only | timeout | inconclusive.
Only `passed` and `failed`/`collection_error` are conclusive. A zero exit code alone is
never treated as `passed`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

CHECK_COMMAND_RE = re.compile(
    r"(^|[\s;&|(])("
    r"pytest|py\.test|tox|nox|"
    r"python[\d.]*\s+(-[a-zA-Z]+\s+)*-m\s+(pytest|unittest|nose2?|doctest)|"
    r"python[\d.]*\s+\S*(runtests|run_tests|manage)\.py(\s+test)?|"
    r"go\s+test|cargo\s+(test|nextest)|"
    r"(npm|pnpm|yarn|bun)\s+(run\s+)?(test|jest|vitest|mocha)|npx\s+(jest|vitest|mocha)|jest|vitest|mocha|"
    r"mvn\b[^;&|]*\b(test|verify)\b|(\./)?gradlew?\b[^;&|]*\btest\b|"
    r"make\s+(test|check)|ctest|rspec|bundle\s+exec\s+(rake|rspec)|phpunit|dotnet\s+test|swift\s+test|mix\s+test|"
    r"bazel\s+test"
    r")\b"
)


def is_check_command(command: str) -> bool:
    return bool(CHECK_COMMAND_RE.search(command))


def normalize_command(command: str) -> str:
    c = " ".join(command.split())
    c = re.sub(r"\s*2>&1", "", c)
    c = re.sub(r"\s*\|\s*(tail|head)(\s+-n)?\s+-?\d+\s*$", "", c)
    c = re.sub(r"\s*\|\s*(tail|head)\s*$", "", c)
    return c.strip()


@dataclass
class CheckOutcome:
    outcome: str
    runner: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""


def _num(pattern: str, text: str) -> int:
    return sum(int(m) for m in re.findall(pattern, text, re.M))


def classify_output(text: str, exit_code: int | None, *, timed_out: bool = False, piped: bool = False) -> CheckOutcome:
    if timed_out:
        return CheckOutcome("timeout", detail="check timed out")
    counts: dict[str, int] = {}
    runner = None
    collection_error = False

    # pytest (verbose "=== ... in 1.2s ===" and quiet "3 passed in 0.1s" summaries)
    summaries = re.findall(
        r"^[=\s]*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected|rerun)(?:, )?)+)"
        r" in [\d.]+s", text, re.M)
    if summaries or re.search(r"^=+ no tests ran", text, re.M) or "collected 0 items" in text:
        runner = "pytest"
        for s in summaries:
            for n, kind in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)", s):
                key = "errors" if kind.startswith("error") else kind
                counts[key] = counts.get(key, 0) + int(n)
        if re.search(r"ERROR collecting|errors? during collection|ImportError while importing test module", text):
            collection_error = True
        if exit_code in (4,) and not summaries:
            collection_error = True
    # unittest / django
    elif re.search(r"^Ran \d+ tests? in", text, re.M):
        runner = "unittest"
        ran = _num(r"^Ran (\d+) tests? in", text)
        failures = _num(r"FAILED \([^)]*?failures=(\d+)", text)
        errors = _num(r"FAILED \([^)]*?errors=(\d+)", text)
        skipped = _num(r"(?:OK|FAILED) \([^)]*?skipped=(\d+)", text)
        counts = {"passed": max(0, ran - failures - errors - skipped), "failed": failures, "errors": errors, "skipped": skipped}
        if re.search(r"ImportError|ModuleNotFoundError|Failed to import test module", text) and ran <= errors:
            collection_error = True
    # go test
    elif re.search(r"^(ok|FAIL|\?)\s+\S+", text, re.M) or "--- FAIL:" in text:
        runner = "go"
        counts = {
            "passed": len(re.findall(r"^\s*--- PASS:", text, re.M)),
            "failed": len(re.findall(r"^\s*--- FAIL:", text, re.M)),
            "skipped": len(re.findall(r"^\s*--- SKIP:", text, re.M)),
        }
        ok_pkgs = len(re.findall(r"^ok\s+\S+", text, re.M))
        fail_pkgs = len(re.findall(r"^FAIL\s+\S+", text, re.M))
        if counts["passed"] == 0 and ok_pkgs:
            counts["passed"] = ok_pkgs  # non-verbose: count passing packages
        if fail_pkgs and not counts["failed"]:
            counts["failed"] = fail_pkgs
        if re.search(r"\[build failed\]|\[setup failed\]|cannot find package|undefined: ", text):
            collection_error = True
        if "[no tests to run]" in text and not counts["passed"] and not counts["failed"]:
            counts = {}
    # cargo
    elif "test result:" in text:
        runner = "cargo"
        for status, p, f, ig in re.findall(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
            counts["passed"] = counts.get("passed", 0) + int(p)
            counts["failed"] = counts.get("failed", 0) + int(f)
            counts["skipped"] = counts.get("skipped", 0) + int(ig)
    elif re.search(r"error(\[E\d+\])?: could not compile|error: could not compile", text):
        runner, collection_error = "cargo", True
    # jest / vitest
    elif re.search(r"^\s*Tests:?\s+.*\d+ (passed|failed|total)", text, re.M):
        runner = "jest/vitest"
        line = re.findall(r"^\s*Tests:?\s+(.*)$", text, re.M)[-1]
        for n, kind in re.findall(r"(\d+) (passed|failed|skipped|todo)", line):
            counts[kind] = counts.get(kind, 0) + int(n)
        if re.search(r"Test suite failed to run|Failed to load|SyntaxError", text) and not counts.get("passed"):
            collection_error = True
    elif re.search(r"No tests found", text):
        runner = "jest/vitest"
    # mocha
    elif re.search(r"^\s*\d+ passing \(", text, re.M):
        runner = "mocha"
        counts = {"passed": _num(r"^\s*(\d+) passing", text), "failed": _num(r"^\s*(\d+) failing", text),
                  "skipped": _num(r"^\s*(\d+) pending", text)}
    # maven / gradle / surefire
    elif re.search(r"Tests run: \d+, Failures: \d+", text):
        runner = "junit"
        last = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)(?:, Skipped: (\d+))?", text)[-1]
        run, f, e, sk = (int(x or 0) for x in last)
        counts = {"passed": max(0, run - f - e - sk), "failed": f, "errors": e, "skipped": sk}
        if "COMPILATION ERROR" in text:
            collection_error = True
    # rspec
    elif re.search(r"\d+ examples?, \d+ failures?", text):
        runner = "rspec"
        m = re.findall(r"(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", text)[-1]
        ex, f, pend = int(m[0]), int(m[1]), int(m[2] or 0)
        counts = {"passed": ex - f - pend, "failed": f, "skipped": pend}

    if runner is None:
        return CheckOutcome("inconclusive", detail=f"unrecognised runner output (exit code {exit_code})")
    passed, failed = counts.get("passed", 0) + counts.get("xfailed", 0), counts.get("failed", 0) + counts.get("errors", 0)
    skipped = counts.get("skipped", 0)
    if collection_error and passed == 0:
        return CheckOutcome("collection_error", runner, counts, "tests could not be collected/built")
    if failed > 0:
        return CheckOutcome("failed", runner, counts)
    if passed == 0 and skipped > 0:
        return CheckOutcome("skipped_only", runner, counts, "only skipped tests")
    if passed == 0:
        return CheckOutcome("no_tests", runner, counts, "no tests ran")
    if exit_code not in (0, None) and not piped:
        return CheckOutcome("inconclusive", runner, counts, f"summary shows no failures but exit code was {exit_code}")
    return CheckOutcome("passed", runner, counts)


CONCLUSIVE_PASS = {"passed"}
CONCLUSIVE_FAIL = {"failed", "collection_error"}


@dataclass
class VerificationRecord:
    id: str
    step: int
    tree: str
    binding: str  # exact | mutated
    command: str
    check_key: str
    cwd: str
    exit_code: int | None
    timed_out: bool
    outcome: str
    runner: str | None
    counts: dict[str, int]
    detail: str
    output_id: str | None
    source: str  # agent | harness_recheck
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verdicts(records: list[VerificationRecord], tree: str) -> dict[str, str]:
    """Latest conclusive verdict per check for one tree ('pass'/'fail')."""
    out: dict[str, str] = {}
    for r in records:
        if r.tree != tree or r.binding != "exact":
            continue
        if r.outcome in CONCLUSIVE_PASS:
            out[r.check_key] = "pass"
        elif r.outcome in CONCLUSIVE_FAIL:
            out[r.check_key] = "fail"
    return out


def dominates(a: dict[str, str], b: dict[str, str]) -> bool:
    """A dominates B: on every check both have a verdict for, A never fails where B passes,
    and A passes at least one check B fails."""
    shared = set(a) & set(b)
    if not shared:
        return False
    if any(a[k] == "fail" and b[k] == "pass" for k in shared):
        return False
    return any(a[k] == "pass" and b[k] == "fail" for k in shared)


def select_candidate(
    final_tree: str,
    base_tree: str,
    history: list[str],
    records: list[VerificationRecord],
    *,
    dominance: bool = True,
    recover_empty_final: bool = True,
) -> tuple[str, str]:
    """Choose the deliverable among archived states. `history` lists distinct trees in the
    order they were observed. Returns (tree, reason)."""
    candidates = [t for t in dict.fromkeys(history + [final_tree]) if t != base_tree]
    if final_tree == base_tree:
        if recover_empty_final and candidates:
            best = candidates[-1]
            passing = [t for t in candidates if "pass" in verdicts(records, t).values()
                       and "fail" not in verdicts(records, t).values()]
            if passing:
                best = passing[-1]
            return best, "final working tree had no changes; restored the most recent archived non-empty candidate" + (
                " with passing checks" if passing else "")
        return final_tree, "final working tree has no changes and no earlier candidate exists"
    if not dominance:
        return final_tree, "final working tree (dominance selection disabled)"
    final_v = verdicts(records, final_tree)
    dominating = [t for t in candidates if t != final_tree and dominates(verdicts(records, t), final_v)]
    if dominating:
        undominated = [t for t in dominating if not any(dominates(verdicts(records, o), verdicts(records, t))
                                                         for o in dominating if o != t)]
        pick = (undominated or dominating)[-1]
        return pick, (
            "an earlier candidate dominates the final state on shared checks "
            f"(earlier: {verdicts(records, pick)}; final: {final_v})"
        )
    return final_tree, "final working tree (no archived candidate dominates it)"


def verification_status(records: list[VerificationRecord], tree: str) -> tuple[str, str]:
    exact = [r for r in records if r.tree == tree and r.binding == "exact"]
    if not records:
        return "verification_unavailable", "no recognised test/check command was run"
    if not exact:
        return "verification_inconclusive", "checks ran, but none on the selected candidate's exact content"
    v = verdicts(records, tree)
    if not v:
        kinds = sorted({r.outcome for r in exact})
        return "verification_inconclusive", f"checks on the selected candidate were inconclusive: {kinds}"
    if "fail" in v.values():
        return "checks_failed", f"latest verdicts: {v}"
    return "checks_passed", f"latest verdicts: {v}"
