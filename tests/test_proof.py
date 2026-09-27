"""Proof-carrying patches: runner parsing, comparisons, levels, counterfactual trees, registered
reproductions and adaptive attempts (scripted model; deterministic)."""

import json
from unittest import mock

from arbiter import proof
from arbiter.evidence import VerificationRecord
from arbiter.workspace import Workspace
from tests.helpers import CALC, TEST_CMD, TempDirCase, git, make_repo, run_agent, tc, test_profile, turn

FIX = tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")
BREAK = tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a * b")
TEST = tc("bash", command=TEST_CMD)
SUBMIT = tc("submit", summary="done")


def rec(tree, outcome, failing=None, counts=None, key="k", kind="check"):  # noqa: PLR0913
    return VerificationRecord(id="v", step=1, tree=tree, binding="exact", command=key, check_key=key, cwd="/",
                              exit_code=0 if outcome == "passed" else 1, timed_out=False, outcome=outcome, runner="pytest",
                              counts=counts or {}, detail="", output_id=None, source="agent", duration_s=0.1,
                              kind=kind, failing=failing)


class ParsingTest(TempDirCase):
    def test_failing_test_names_per_runner(self):
        cases = {
            "pytest": ("=== short test summary info ===\nFAILED tests/test_a.py::test_x - AssertionError: 1\n"
                       "ERROR tests/test_b.py::test_y\n=== 1 failed, 1 error in 0.1s ===", ["tests/test_a.py::test_x",
                                                                                         "tests/test_b.py::test_y"]),
            "unittest": ("FAIL: test_true (tests.test_ops.T.test_true)\nERROR: test_e (tests.test_ops.T)\n",
                         ["tests.test_ops.T.test_e", "tests.test_ops.T.test_true"]),
            "go": ("--- FAIL: TestDivide (0.00s)\n    --- FAIL: TestDivide/zero (0.00s)\nFAIL\n",
                   ["TestDivide", "TestDivide/zero"]),
            "cargo": ("test ops::tests::divide ... FAILED\ntest ops::tests::ok ... ok\n", ["ops::tests::divide"]),
            "rspec": ("rspec ./spec/calc_spec.rb:12 # Calc divides\n", ["./spec/calc_spec.rb:12"]),
        }
        for runner, (text, want) in cases.items():
            self.assertEqual(proof.failing_tests(text, runner), want, runner)
        self.assertIsNone(proof.failing_tests("anything", None))

    def test_reproduction_semantics(self):
        self.assertEqual(proof.classify_reproduction("Traceback ...", 1).outcome, "failed")
        self.assertEqual(proof.classify_reproduction("ok", 0).outcome, "passed")
        self.assertEqual(proof.classify_reproduction("", None, timed_out=True).outcome, "timeout")
        # a test runner's summary wins over the exit code (no tests ran is not a pass)
        self.assertEqual(proof.classify_reproduction("=== no tests ran in 0.01s ===", 5).outcome, "no_tests")


class ComparisonTest(TempDirCase):
    def test_verdicts(self):
        f2p = proof.compare("k", "k", "check", rec("H", "failed", ["t1"]), rec("C", "passed", []))
        self.assertEqual((f2p.verdict, f2p.fail_to_pass, f2p.shows_fix), ("fail_to_pass", ["t1"], True))
        p2f = proof.compare("k", "k", "check", rec("H", "passed", []), rec("C", "failed", ["t2"]))
        self.assertEqual((p2f.verdict, p2f.pass_to_fail, p2f.shows_regression), ("pass_to_fail", ["t2"], True))
        both = proof.compare("k", "k", "check", rec("H", "failed", ["t1", "t3"], {"passed": 5}),
                             rec("C", "failed", ["t3", "t4"], {"passed": 5}))
        self.assertEqual((both.fail_to_pass, both.pass_to_fail, both.still_failing), (["t1"], ["t4"], ["t3"]))
        self.assertTrue(both.shows_regression)
        vanished = proof.compare("k", "k", "check", rec("H", "failed", ["t1", "t3"], {"passed": 5}),
                                 rec("C", "failed", ["t3"], {"passed": 4}))
        self.assertEqual(vanished.fail_to_pass, [])  # fewer passing tests: t1 may have been deleted, not fixed
        missing = proof.compare("k", "k", "check", None, rec("C", "passed"))
        self.assertEqual(missing.verdict, "incomplete")

    def test_levels_and_ranking(self):
        gen = lambda t: t == "t_new"  # noqa: E731 - "t_new" was added by the candidate
        fix = proof.compare("a", "a", "check", rec("H", "failed", ["t"]), rec("C", "passed", []))
        p2p = proof.compare("b", "b", "check", rec("H", "passed", []), rec("C", "passed", []))
        reg = proof.compare("b", "b", "check", rec("H", "passed", []), rec("C", "failed", ["x"]))
        self.assertTrue(fix.authoritative_fix)  # an existing test that failed now passes
        self.assertEqual(proof.assess("C", [fix, p2p], tests_modified=[], expected=2).level, "proven")
        self.assertEqual(proof.assess("C", [fix, p2p], tests_modified=["tests/t.py"], expected=2).level, "fixed")
        self.assertEqual(proof.assess("C", [fix], tests_modified=[], expected=2).level, "fixed")
        self.assertEqual(proof.assess("C", [p2p], tests_modified=[], expected=1).level, "passing")
        self.assertEqual(proof.assess("C", [fix, reg], tests_modified=[], expected=2).level, "refuted")
        new_test = proof.compare("a", "a", "check", rec("H", "failed", ["t_new"]), rec("C", "passed", []),
                                 is_generated_test=gen, orig_output="AssertionError: 3 != 3.5")
        self.assertEqual((new_test.generated_fail_to_pass, new_test.qualified), (["t_new"], True))
        self.assertEqual(proof.assess("C", [new_test, p2p], tests_modified=[], expected=2).level, "proven")
        a = proof.assess("A", [fix, p2p], tests_modified=[], expected=2)
        b = proof.assess("B", [p2p], tests_modified=[], expected=1)
        c = proof.assess("C", [fix, p2p], tests_modified=[], expected=2)
        a.diff_lines, c.diff_lines = 10, 2
        ranked = proof.rank_candidates([b, a, c])
        self.assertEqual([x.tree for x in ranked], ["C", "A", "B"])  # proven first; smaller patch breaks the tie
        self.assertEqual(ranked[0].agreement, 4)  # consensus set {A, C} x 2 checks passed (CodeT)

    def test_generated_checks_are_advisory_unless_qualified(self):
        def repro(orig_out, **kw):
            return proof.compare("r", "python repro.py", "reproduction", rec("H", "failed", None, key="r", kind="reproduction"),
                                 rec("C", "passed", None, key="r", kind="reproduction"), orig_output=orig_out, **kw)

        self.assertTrue(repro("AssertionError: 3 != 3.5").qualified_fix)
        self.assertFalse(repro("ModuleNotFoundError: No module named 'calc'").qualified_fix)  # broken environment
        self.assertFalse(repro("AttributeError: module has no attribute 'split_path'").qualified_fix)
        self.assertTrue(repro("AttributeError: module has no attribute 'split_path'", task_kind="feature").qualified_fix)
        self.assertFalse(repro("AssertionError", impl_specific=True).qualified_fix)  # asserts on internals
        self.assertFalse(repro("AssertionError", stable=False).qualified_fix)  # flaky on the original code
        weak = repro("ModuleNotFoundError: x")
        self.assertEqual(proof.assess("C", [weak], tests_modified=[], expected=1).level, "fixed")  # never "proven"

    def test_a_failing_generated_check_never_discards_a_candidate_by_itself(self):
        def comp(tree, cand_outcome, key="r"):
            return proof.compare(key, "python repro.py", "reproduction", rec("H", "failed", None, key=key),
                                 rec(tree, cand_outcome, None, key=key), orig_output="AssertionError")

        p2p = proof.compare("b", "b", "check", rec("H", "passed", []), rec("A", "passed", []))
        alone = proof.assess("A", [comp("A", "failed"), p2p], tests_modified=[], expected=2)
        self.assertEqual(alone.level, "passing")  # advisory, not refuted
        self.assertTrue(alone.advisory)
        # ... but once another regression-free candidate satisfies the qualified check, it discriminates
        other = proof.assess("B", [comp("B", "passed"), p2p], tests_modified=[], expected=2)
        ranked = proof.rank_candidates([alone, other])
        self.assertEqual([x.tree for x in ranked], ["B", "A"])
        self.assertEqual(alone.level, "refuted")
        # a satisfier that itself breaks existing tests does not count
        reg = proof.compare("b", "b", "check", rec("H", "passed", []), rec("D", "failed", ["x"]))
        breaker = proof.assess("D", [comp("D", "passed"), reg], tests_modified=[], expected=2)
        fresh = proof.assess("A2", [comp("A2", "failed"), p2p], tests_modified=[], expected=2)
        ranked = proof.rank_candidates([breaker, fresh])
        self.assertEqual((ranked[0].tree, fresh.level, breaker.level), ("A2", "passing", "refuted"))


class CounterfactualTreeTest(TempDirCase):
    def test_overlay_takes_only_test_changes(self):
        repo = make_repo(self.tmp / "r", CALC)
        ws = Workspace(repo, self.tmp / "state")
        base = ws.init()
        (repo / "calc" / "ops.py").write_text("def divide(a, b):\n    return a / b\n")
        (repo / "tests" / "test_new.py").write_text("def test_new():\n    pass\n")
        (repo / "tests" / "test_ops.py").unlink()
        cand = ws.snapshot()
        cf = ws.overlay_tree(base, cand, ["tests/test_new.py", "tests/test_ops.py"])
        files = {f["path"]: f["status"] for f in ws.changed_files(base, cf)}
        self.assertEqual(files, {"tests/test_new.py": "A", "tests/test_ops.py": "D"})  # source untouched
        self.assertEqual(ws.overlay_tree(base, cand, []), base)


class ReproductionFlowTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "repo", CALC)
        self.run_dir = self.tmp / "run"
        self.scratch = self.run_dir / "scratch"

    def repro(self, name="repro.py", expect="3.5"):
        body = ("import os, sys\nsys.path.insert(0, os.getcwd())\nfrom calc import divide\n"
                f"assert str(divide(7, 2)) == {expect!r}, divide(7, 2)\nprint('ok')\n")
        return [turn(tc("write_file", path=str(self.scratch / name), content=body)),
                turn(tc("register_reproduction", command=f"python3 {self.scratch / name}", description="7/2"))]

    def test_confirmed_reproduction_proves_the_fix_without_repo_tests(self):
        turns = self.repro() + [turn(FIX), turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir)
        report = [m["content"] for m in agent.transcript if m.get("name") == "register_reproduction"][0]
        self.assertIn("On the original code: FAILS", report)
        self.assertIn("reproduces the issue", report)
        self.assertEqual(result["termination"], "model_submitted")  # accepted at the first submit
        self.assertEqual(result["proof"]["level"], "proven", result["proof"])
        (cmp_,) = [c for c in result["proof"]["comparisons"] if c["kind"] == "reproduction"]
        self.assertEqual(cmp_["verdict"], "fail_to_pass")
        self.assertTrue(result["proof"]["reproductions"][0]["confirmed"])
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")

    def test_reproduction_that_passes_on_the_original_code_is_flagged(self):
        turns = self.repro(expect="3") + [turn(SUBMIT), turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir)
        report = [m["content"] for m in agent.transcript if m.get("name") == "register_reproduction"][0]
        self.assertIn("does NOT fail on the original code", report)
        self.assertFalse(result["proof"]["reproductions"][0]["confirmed"])

    def test_added_test_is_checked_on_the_counterfactual(self):
        new_test = ("import unittest\nfrom calc import divide\n\n\nclass N(unittest.TestCase):\n"
                    "    def test_half(self):\n        self.assertEqual(divide(1, 2), 0.5)\n")
        turns = [turn(tc("write_file", path="tests/test_half.py", content=new_test)), turn(FIX), turn(TEST),
                 turn(SUBMIT), turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir)
        p = result["proof"]
        self.assertEqual(p["level"], "proven", p)
        (c,) = p["comparisons"]
        self.assertIn("test_half.N.test_half", c["fail_to_pass"])  # fails without the fix, passes with it
        originals = [r for r in agent.records if r.source == "harness_original"]
        self.assertTrue(originals and originals[0].tree != agent.base_tree)  # base + the new test file

    def test_attempts_restart_from_original_code_after_refuted_candidate(self):
        profile = test_profile()
        profile.policy.max_attempts, profile.policy.min_attempt_s = 3, 0.0
        turns = [turn(BREAK), turn(TEST), turn(SUBMIT), turn(SUBMIT), turn(SUBMIT),  # attempt 1: regression
                 turn(FIX), turn(TEST), turn(SUBMIT)]  # attempt 2
        result, agent = run_agent(self.repo, turns, self.run_dir, profile=profile)
        p = result["proof"]
        self.assertEqual([a["level"] for a in p["attempts"]], ["refuted", "proven"], p["attempts"])
        self.assertEqual(p["level"], "proven")
        self.assertEqual(result["selected_candidate"]["tree"], p["attempts"][1]["candidate"])
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")
        task_msgs = [m for m in agent.transcript if m["role"] == "user"]
        self.assertIn("this is attempt 2", task_msgs[0]["content"])  # fresh context with the observations
        self.assertIn("evidence refuted: regressions: test_ops.T.test_exact", task_msgs[0]["content"])
        state = json.loads((self.run_dir / "state.json").read_text())
        self.assertIsNone(state["detour"])
        self.assertEqual(len(p["ranking"]), 2)

    def test_stuck_attempt_gets_a_fresh_restart_and_best_candidate_wins(self):
        profile = test_profile(max_steps=10)
        profile.policy.max_attempts, profile.policy.min_attempt_s, profile.policy.first_attempt_share = 2, 0.0, 0.3
        idle = turn(tc("bash", command="echo looking"))
        turns = [turn(BREAK), idle, idle, idle, idle] + [turn(FIX), turn(TEST), turn(SUBMIT)]
        result, _ = run_agent(self.repo, turns, self.run_dir, profile=profile)
        attempts = result["proof"]["attempts"]
        self.assertEqual(attempts[0]["termination"], "attempt_budget")
        self.assertEqual(result["proof"]["level"], "proven")
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")

    def test_attempt_with_a_passing_change_is_not_restarted_at_its_share(self):
        """Live finding: with a slow provider the first attempt's time share ran out right after its
        fix passed the tests; the harness reset to the original code and started again. A change whose
        latest check passes keeps its attempt going instead."""
        profile = test_profile(max_steps=10)
        profile.policy.max_attempts, profile.policy.min_attempt_s, profile.policy.first_attempt_share = 2, 0.0, 0.3
        idle = turn(tc("bash", command="echo looking"))
        turns = [turn(FIX), turn(TEST), idle, idle, turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir, profile=profile)
        attempts = result["proof"]["attempts"]
        self.assertEqual(len(attempts), 1, attempts)
        self.assertEqual(attempts[0]["termination"], "model_submitted")
        self.assertEqual(result["proof"]["level"], "proven")

    def test_checks_run_on_a_scratch_copy_are_not_compared(self):
        """Live finding: the model rebuilt the original code in the scratch directory and ran the tests
        there; the harness then re-ran that command as a check of the repository and reported it as
        'still failing' in the submit review."""
        copy = self.run_dir.resolve() / "scratch" / "orig"
        scratch_run = turn(tc("bash", command=f'd={copy}; mkdir -p "$d" && cp -r calc tests "$d"/ && '
                                              f'cd "$d" && python3 -m unittest discover -s tests'))
        result, _ = run_agent(self.repo, [turn(FIX), scratch_run, turn(TEST), turn(SUBMIT)], self.run_dir)
        commands = [c["command"] for c in result["proof"]["comparisons"]]
        self.assertTrue(commands)
        self.assertFalse([c for c in commands if "scratch" in c], commands)
        self.assertEqual(result["proof"]["level"], "proven")

    def test_small_budget_is_not_split_into_attempts_it_cannot_fund(self):
        """4 steps with up to 3 attempts: capping attempt 1 at 60% would end the run after 2 steps with
        nothing, since no fresh attempt fits in the 2 left. The only attempt keeps the whole budget."""
        profile = test_profile(max_steps=4)
        profile.policy.max_attempts, profile.policy.min_attempt_s = 3, 0.0
        idle = turn(tc("bash", command="echo looking"))
        result, _ = run_agent(self.repo, [idle, turn(FIX), turn(TEST), turn(SUBMIT)], self.run_dir, profile=profile)
        attempts = result["proof"]["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0]["termination"], "attempt_budget")
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")

    def test_attempt_cap_is_lifted_when_no_room_for_another_attempt(self):
        """Caps set at the start can become unaffordable (time spent in long commands): at the cap, the
        attempt continues instead of ending into an attempt that cannot run."""
        profile = test_profile(max_steps=12)
        profile.policy.max_attempts, profile.policy.min_attempt_s, profile.policy.first_attempt_share = 2, 0.0, 0.5
        idle = turn(tc("bash", command="echo looking"))
        turns = [idle] * 6 + [turn(FIX), turn(TEST), turn(SUBMIT)]
        with mock.patch("arbiter.agent.Agent._room_for_another_attempt", return_value=False):
            result, _ = run_agent(self.repo, turns, self.run_dir, profile=profile)
        attempts = result["proof"]["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result["proof"]["level"], "proven")

    def test_no_retry_when_the_first_attempt_is_verified(self):
        profile = test_profile()
        profile.policy.max_attempts = 3
        result, _ = run_agent(self.repo, [turn(FIX), turn(TEST), turn(SUBMIT)], self.run_dir, profile=profile)
        self.assertEqual(len(result["proof"]["attempts"]), 1)
        self.assertEqual(result["proof"]["level"], "proven")

    def test_offline_recovery_keeps_every_attempts_candidate(self):
        from arbiter.agent import Agent
        from arbiter.task import Task

        profile = test_profile()
        profile.policy.max_attempts, profile.policy.min_attempt_s = 3, 0.0
        turns = [turn(BREAK), turn(TEST), turn(SUBMIT), turn(SUBMIT), turn(SUBMIT),
                 turn(FIX), turn(TEST), turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir, profile=profile)
        git(self.repo, "checkout", "--", "calc/ops.py")  # simulate a working tree left elsewhere
        recovered = Agent.recover(self.run_dir, profile, Task.from_dict(agent.task.to_dict()), log=lambda m: None)
        again = recovered._finalize()
        self.assertEqual(again["selected_candidate"]["tree"], result["selected_candidate"]["tree"])
        self.assertEqual(again["proof"]["level"], "proven")
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")


class MemoryTest(TempDirCase):
    def test_second_run_on_the_same_repository_sees_verified_facts_only(self):
        repo = make_repo(self.tmp / "repo", CALC)
        git(repo, "remote", "add", "origin", "https://github.com/acme/calc.git")
        mem = self.tmp / "runs" / ".memory"
        install = tc("bash", command="pip install --help >/dev/null")  # stands in for an install command
        run_agent(repo, [turn(install), turn(FIX), turn(TEST), turn(SUBMIT)], self.tmp / "runs" / "a", memory_root=mem)
        (notes,) = [json.loads(p.read_text()) for p in mem.glob("*.json")]
        self.assertEqual([c["command"] for c in notes["checks"]], [TEST_CMD])
        self.assertEqual(notes["setup"], ["pip install --help >/dev/null"])
        git(repo, "checkout", "--", ".")
        other = make_repo(self.tmp / "clone2", CALC)  # another clone of the same repository
        git(other, "remote", "add", "origin", "https://github.com/acme/calc")
        _, agent = run_agent(other, [turn(SUBMIT), turn(SUBMIT)], self.tmp / "runs" / "b", memory_root=mem)
        task_msg = [m for m in agent.transcript if m["role"] == "user"][0]["content"]
        self.assertIn("From 1 earlier run(s) on this repository", task_msg)
        self.assertIn(f"`{TEST_CMD}` ran (unittest", task_msg)
        self.assertNotIn("a / b", task_msg)  # no code or patches are carried over


class AttestationTest(TempDirCase):
    def test_attestation_verifies_and_detects_tampering(self):
        import subprocess
        import sys

        from tests.helpers import ROOT

        repo = make_repo(self.tmp / "repo", CALC)
        run_dir = self.tmp / "run"
        result, _ = run_agent(repo, [turn(FIX), turn(TEST), turn(SUBMIT)], run_dir)
        st = json.loads((run_dir / "attestation.json").read_text())
        self.assertEqual(st["_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(st["subject"][0]["digest"]["sha256"], result["deliverable"]["patch_sha256"])
        self.assertEqual(st["predicate"]["proof"]["level"], "proven")

        def verify(*extra):
            p = subprocess.run([sys.executable, "-m", "arbiter", "verify", "--run-dir", str(run_dir), *extra],
                               cwd=ROOT, capture_output=True, text=True, timeout=300)
            return p.returncode, json.loads(p.stdout)

        code, report = verify("--rerun")
        self.assertEqual(code, 0, report)
        self.assertTrue(all(r["agrees"] for r in report["reruns"]), report["reruns"])
        self.assertEqual(report["reruns"][0]["rerun"], ["fail", "pass"])  # fails on the original, passes patched
        (run_dir / "patch.diff").write_text((run_dir / "patch.diff").read_text().replace("a / b", "a * b"))
        code, report = verify()
        self.assertEqual(code, 1)
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        self.assertIn("patch digest matches the attestation", failed)
        self.assertIn("patch applied to the base reproduces the selected tree", failed)
