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
    r"^("
    r"pytest|py\.test|tox|nox|"
    r"python[\d.]*\s+(-[a-zA-Z]+\s+)*-m\s+(pytest|unittest|nose2?|doctest)|"
    r"python[\d.]*\s+(-[a-zA-Z]+\s+)*\S*(runtests|run_tests|manage)\.py(\s+test)?|"
    r"go\s+test|cargo\s+(test|nextest)|"
    r"(npm|pnpm|yarn|bun)\s+(run\s+)?(test|jest|vitest|mocha)|npx\s+(jest|vitest|mocha)|jest|vitest|mocha|"
    r"mvn\b.*\b(test|verify)\b|(\./)?gradlew?\b.*\btest\b|"
    r"make\s+(test|check)|ctest|rspec|bundle\s+exec\s+(rake|rspec)|phpunit|(\./)?vendor/bin/phpunit|"
    r"dotnet\s+test|swift\s+test|mix\s+test|bazel\s+test|"
    r"node\s+(--test|--run\s+test)|node\s+\S*(test|spec)\S*\.[cm]?[jt]s|bun\s+test|deno\s+test|(\./)?\S*bats|"
    r"ruby\s+(-\S+\s+)*\S*(_test|test_|_spec)\S*\.rb|(bundle\s+exec\s+)?rake\s+(test|spec)|"
    r"php\s+artisan\s+test|(\./)?(vendor/bin/)?pest|hatch\s+(test|run\s+\S*test)|"
    r"python[\d.]*\s+(-[a-zA-Z]+\s+)*\S*test\S*\.py|(\./)?\S*(run_?tests?|runtests)\S*\.(py|sh)|"
    r"(bash|sh)\s+\S*test\S*\.sh"
    r")(\s|$)"
)
_WRAPPERS = {"time", "env", "nice", "xvfb-run", "sudo", "command", "exec"}
_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye"}


def is_check_command(command: str) -> bool:
    """True if any simple command in the line starts with a known test runner."""
    for seg in re.split(r"&&|\|\||[;|\n]", command):
        words = seg.strip().lstrip("(").split()
        while words:
            w = words[0]
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or w in _WRAPPERS:
                words = words[1:]
            elif w == "timeout":
                words = words[1:]
                while words and (words[0].startswith("-") or re.match(r"^\d+[smh]?$", words[0])):
                    words = words[1:]
            elif w in _RUNNERS and len(words) > 1 and words[1] in ("run", "exec"):
                words = words[2:]
            else:
                break
        if words and CHECK_COMMAND_RE.match(" ".join(words)):
            return True
    return False


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
    # cargo
    elif "test result:" in text:
        runner = "cargo"
        for status, p, f, ig in re.findall(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
            counts["passed"] = counts.get("passed", 0) + int(p)
            counts["failed"] = counts.get("failed", 0) + int(f)
            counts["skipped"] = counts.get("skipped", 0) + int(ig)
    elif re.search(r"error(\[E\d+\])?: could not compile|error: could not compile", text):
        runner, collection_error = "cargo", True
    # jest / vitest (before go: jest prints "FAIL path" lines too)
    elif re.search(r"^\s*Tests:?\s+.*\d+ (passed|failed|total)", text, re.M):
        runner = "jest/vitest"
        line = re.findall(r"^\s*Tests:?\s+(.*)$", text, re.M)[-1]
        for n, kind in re.findall(r"(\d+) (passed|failed|skipped|todo)", line):
            counts[kind] = counts.get(kind, 0) + int(n)
        broken = len(re.findall(r"Test suite failed to run|Failed to load (?:url|test file)|Error: Cannot find module",
                                text))
        if broken:  # a test file that failed to load is a failure even when every test that ran passed
            counts["errors"] = counts.get("errors", 0) + broken
        if re.search(r"Test suite failed to run|Failed to load|SyntaxError", text) and not counts.get("passed"):
            collection_error = True
    elif re.search(r"No tests found", text):
        runner = "jest/vitest"
    # mocha
    elif re.search(r"^\s*\d+ passing \(", text, re.M):
        runner = "mocha"
        counts = {"passed": _num(r"^\s*(\d+) passing", text), "failed": _num(r"^\s*(\d+) failing", text),
                  "skipped": _num(r"^\s*(\d+) pending", text)}
    # TAP: node --test, tape, bats, prove
    elif re.search(r"^# (pass|fail) \d+", text, re.M) or (re.search(r"^1\.\.\d+", text, re.M)
                                                          and re.search(r"^(not )?ok \d+", text, re.M)):
        runner = "tap"
        if re.search(r"^# pass \d+", text, re.M):
            counts = {"passed": _num(r"^# pass (\d+)", text), "failed": _num(r"^# fail (\d+)", text),
                      "skipped": _num(r"^# skipped (\d+)", text) + _num(r"^# todo (\d+)", text)}
        else:
            lines = re.findall(r"^(not ok|ok) \d+.*$", text, re.M)
            skip = len(re.findall(r"^(?:not )?ok \d+.*# (?:SKIP|TODO)", text, re.M | re.I))
            counts = {"passed": sum(1 for l in lines if l == "ok"), "failed": sum(1 for l in lines if l == "not ok"),
                      "skipped": skip}
            counts["passed"] = max(0, counts["passed"] - skip)
    # minitest (Ruby)
    elif re.search(r"\d+ runs, \d+ assertions, \d+ failures, \d+ errors", text):
        runner = "minitest"
        r, f, e, sk = (int(x) for x in re.findall(r"(\d+) runs, \d+ assertions, (\d+) failures, (\d+) errors, "
                                                   r"(\d+) skips", text)[-1])
        counts = {"passed": max(0, r - f - e - sk), "failed": f, "errors": e, "skipped": sk}
    # maven / surefire
    elif re.search(r"Tests run: \d+, Failures: \d+", text):
        runner = "junit"
        last = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)(?:, Skipped: (\d+))?", text)[-1]
        run, f, e, sk = (int(x or 0) for x in last)
        counts = {"passed": max(0, run - f - e - sk), "failed": f, "errors": e, "skipped": sk}
        if "COMPILATION ERROR" in text:
            collection_error = True
    # gradle
    elif re.search(r"\d+ tests? completed, \d+ failed", text) or re.search(r"> Task :\S*test\S*", text, re.I):
        runner = "gradle"
        m = re.findall(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?", text)
        if m:
            done, f, sk = int(m[-1][0]), int(m[-1][1]), int(m[-1][2] or 0)
            counts = {"passed": max(0, done - f - sk), "failed": f, "skipped": sk}
        elif "BUILD SUCCESSFUL" in text:
            counts = {"passed": max(1, len(re.findall(r" PASSED$", text, re.M)))}
        if re.search(r"Compilation failed|compileTestJava FAILED|compileJava FAILED|compileKotlin FAILED", text):
            collection_error = True
    # rspec
    elif re.search(r"\d+ examples?, \d+ failures?", text):
        runner = "rspec"
        m = re.findall(r"(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", text)[-1]
        ex, f, pend = int(m[0]), int(m[1]), int(m[2] or 0)
        counts = {"passed": ex - f - pend, "failed": f, "skipped": pend}
    # phpunit
    elif re.search(r"^OK \(\d+ tests?, \d+ assertions?\)|^Tests: \d+, Assertions: \d+", text, re.M):
        runner = "phpunit"
        ok = re.findall(r"^OK \((\d+) tests?", text, re.M)
        if ok:
            counts = {"passed": int(ok[-1])}
        else:
            t = _num(r"^Tests: (\d+)", text)
            f, e = _num(r"Failures: (\d+)", text), _num(r"Errors: (\d+)", text)
            sk = _num(r"Skipped: (\d+)", text) + _num(r"Incomplete: (\d+)", text)
            counts = {"passed": max(0, t - f - e - sk), "failed": f, "errors": e, "skipped": sk}
    # dotnet test
    elif re.search(r"(Passed|Failed)!\s+-\s+Failed:\s+\d+, Passed:\s+\d+", text):
        runner = "dotnet"
        for f, pz, sk in re.findall(r"Failed:\s+(\d+), Passed:\s+(\d+), Skipped:\s+(\d+)", text):
            counts["failed"] = counts.get("failed", 0) + int(f)
            counts["passed"] = counts.get("passed", 0) + int(pz)
            counts["skipped"] = counts.get("skipped", 0) + int(sk)
    # ctest
    elif re.search(r"\d+% tests passed, \d+ tests? failed out of \d+", text):
        runner = "ctest"
        f, total = (int(x) for x in re.findall(r"tests passed, (\d+) tests? failed out of (\d+)", text)[-1])
        counts = {"passed": total - f, "failed": f}
    # ExUnit (Elixir)
    elif re.search(r"^\d+ (?:tests?|doctests?)(?:, \d+ doctests?)?, \d+ failures?", text, re.M):
        runner = "exunit"
        m = re.findall(r"^(\d+) tests?, (\d+) failures?(?:, (\d+) (?:skipped|excluded))?", text, re.M)
        if m:
            t, f, sk = int(m[-1][0]), int(m[-1][1]), int(m[-1][2] or 0)
            counts = {"passed": max(0, t - f - sk), "failed": f, "skipped": sk}
    # swift / XCTest
    elif re.search(r"Executed \d+ tests?, with \d+ failures?", text):
        runner = "xctest"
        t, f = (int(x) for x in re.findall(r"Executed (\d+) tests?, with (\d+) failures?", text)[-1])
        counts = {"passed": t - f, "failed": f}
    # deno
    elif re.search(r"^(ok|FAILED) \| \d+ passed.*\| \d+ failed", text, re.M):
        runner = "deno"
        p_, f_ = (int(x) for x in re.findall(r"\| (\d+) passed.*?\| (\d+) failed", text)[-1])
        counts = {"passed": p_, "failed": f_}
    # go test (strict: package lines carry a duration or "(cached)")
    elif re.search(r"^(ok|FAIL)\s+\S+\s+(\d+(\.\d+)?s|\(cached\))", text, re.M) or re.search(r"^\s*--- (FAIL|PASS):", text, re.M) \
            or re.search(r"^\?\s+\S+\s+\[no test files\]|^FAIL\s+\S+\s+\[(build|setup) failed\]", text, re.M):
        runner = "go"
        counts = {
            "passed": len(re.findall(r"^\s*--- PASS:", text, re.M)),
            "failed": len(re.findall(r"^\s*--- FAIL:", text, re.M)),
            "skipped": len(re.findall(r"^\s*--- SKIP:", text, re.M)),
        }
        ok_pkgs = len(re.findall(r"^ok\s+\S+", text, re.M))
        fail_pkgs = len(re.findall(r"^FAIL\s+\S+\s", text, re.M))
        if counts["passed"] == 0 and ok_pkgs:
            counts["passed"] = ok_pkgs  # non-verbose: count passing packages
        if fail_pkgs and not counts["failed"]:
            counts["failed"] = fail_pkgs
        if re.search(r"\[build failed\]|\[setup failed\]|cannot find package|undefined: ", text):
            collection_error = True
        if "[no tests to run]" in text and not counts["passed"] and not counts["failed"]:
            counts = {}

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
    source: str  # agent | harness_recheck | harness_original | harness_candidate
    duration_s: float
    kind: str = "check"  # check (test runner) | reproduction (registered; exit-code semantics)
    failing: list[str] | None = None  # failing test names, when the runner lists them

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
    recover_verified: bool = False,
    also_empty: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Choose the deliverable among archived states. `history` lists distinct trees in the
    order they were observed. Returns (tree, reason).

    An empty final state (no change against `base_tree` or any of `also_empty`) is replaced by the
    latest earlier candidate: any candidate when `recover_empty_final`, else (`recover_verified`) only
    one whose checks passed, e.g. after the model stashed or reset a verified fix and then submitted."""
    empty = {base_tree, *also_empty}
    candidates = [t for t in dict.fromkeys(history + [final_tree]) if t not in empty]
    if final_tree in empty:
        passing = [t for t in candidates if "pass" in verdicts(records, t).values()
                   and "fail" not in verdicts(records, t).values()]
        if recover_empty_final and candidates:
            return (passing or candidates)[-1], (
                "final working tree had no changes; restored the most recent archived non-empty candidate"
                + (" with passing checks" if passing else ""))
        if recover_verified and passing:
            return passing[-1], ("final working tree had no changes, but an earlier candidate passed its checks "
                                 "(its changes were undone before submitting); restored that candidate")
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
