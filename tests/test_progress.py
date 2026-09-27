"""Failure memory (no-progress detection), evidence authority in practice, and task-type hints.

The scripted policies below are deliberately stubborn: they repeat one wrong idea until the harness
intervenes. They test the harness's mechanics, not a real model's behaviour."""

from arbiter.progress import FailureMemory
from arbiter.tasktype import classify
from tests.helpers import CALC, TEST_CMD, TempDirCase, make_repo, run_agent, tc, test_profile, turn

TEST = tc("bash", command=TEST_CMD)
WRONG = ["int(a / b)", "round(a / b)", "(a // b) * 1", "int(a / b) + 0", "a // b + 0", "int(round(a / b))",
         "(a - a % b) // b", "int(a // b)"]


class StubbornPolicy:
    """Edits calc/ops.py with a new variant of the same wrong idea, runs the tests, and repeats. Changes
    course only when told (a NEW HYPOTHESIS message, or a new attempt), if `listens`."""

    def __init__(self, listens: bool = True, submit_after_fix: bool = True):
        self.expr, self.i, self.phase, self.listens = "a // b", 0, "edit", listens
        self.fixed = False
        self.submit_after_fix = submit_after_fix

    def told(self, messages) -> bool:
        last_user = [m for m in messages if m["role"] == "user"][-1]["content"]
        return self.listens and ("NEW HYPOTHESIS" in last_user or "this is attempt 2" in last_user)

    def __call__(self, messages):
        if "this is attempt 2" in [m for m in messages if m["role"] == "user"][0]["content"] and not self.fixed:
            self.expr = "a // b"  # the harness reset the repository to the original code
        if self.told(messages) and not self.fixed:
            self.fixed, self.phase = True, "test"
            new, old = "a / b", self.expr
            self.expr = new
            return turn(tc("edit_file", path="calc/ops.py", old_str=f"return {old}", new_str=f"return {new}"))
        if self.phase == "test":
            self.phase = "submit" if self.fixed else "edit"
            return turn(TEST)
        if self.phase == "submit":
            return turn(tc("submit", summary="true division"))
        old, new = self.expr, WRONG[self.i % len(WRONG)]
        self.i += 1
        self.expr, self.phase = new, "test"
        return turn(tc("edit_file", path="calc/ops.py", old_str=f"return {old}", new_str=f"return {new}"))


class FailureMemoryUnitTest(TempDirCase):
    def test_streak_intervenes_then_escalates_and_resets_on_progress(self):
        fm = FailureMemory()
        obs = lambda tree, files, failing=("t1",), outcome="failed": fm.observe(  # noqa: E731
            "k", tree, outcome, list(failing), {"failed": len(failing)}, "AssertionError", files)
        self.assertIsNone(obs("T0", []))                # first failure (before any edit)
        self.assertIsNone(obs("T1", ["a.py"]))           # same failure after an edit: no progress (2)
        self.assertEqual(obs("T2", ["a.py"]), "intervene")  # (3)
        self.assertIsNone(obs("T3", ["a.py"]))           # (4)
        self.assertEqual(obs("T4", ["a.py"]), "escalate")   # (5)
        self.assertEqual((fm.before_intervention, fm.after_intervention), (2, 2))
        fm2 = FailureMemory()
        fm2.observe("k", "T0", "failed", ["t1"], {"failed": 1}, "", [])
        fm2.observe("k", "T1", "failed", ["t1"], {"failed": 1}, "", ["a.py"])
        self.assertIsNone(fm2.observe("k", "T2", "failed", ["t2"], {"failed": 1}, "", ["a.py"]))  # new signature
        self.assertIsNone(fm2.observe("k", "T3", "passed", None, {"passed": 3}, "", ["a.py"]))
        self.assertNotIn("k", fm2.streaks)  # a pass ends the streak
        fm3 = FailureMemory()
        for t in ("T0", "T0", "T0", "T0"):  # re-running without edits is not a failed repair
            self.assertIsNone(fm3.observe("k", t, "failed", ["t1"], {"failed": 1}, "", []))


class FailureMemoryFlowTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "repo", CALC)

    def run_policy(self, policy, *, memory: bool, attempts: int = 1, steps: int = 24):
        profile = test_profile(max_steps=steps)
        profile.policy.failure_memory = memory
        profile.policy.max_attempts, profile.policy.min_attempt_s = attempts, 0.0
        return run_agent(self.repo, [policy] * 60, self.tmp / f"run-{memory}-{attempts}", profile=profile)

    def test_new_hypothesis_breaks_a_stubborn_loop(self):
        with_memory, agent = self.run_policy(StubbornPolicy(), memory=True)
        self.assertEqual(with_memory["proof"]["level"], "proven", with_memory["proof"]["summary"])
        self.assertEqual(with_memory["progress"]["interventions"], 1)
        self.assertEqual(with_memory["progress"]["before_intervention"], 2)
        self.assertEqual(with_memory["progress"]["after_intervention"], 0)
        msgs = [m["content"] for m in agent.transcript if m["role"] == "user" and "NEW HYPOTHESIS" in m["content"]]
        self.assertIn("test_ops.T.test_true", msgs[0])
        self.assertIn("calc/ops.py", msgs[0])
        (self.repo / "calc" / "ops.py").write_text(CALC["calc/ops.py"])
        without, _ = self.run_policy(StubbornPolicy(), memory=False)
        self.assertEqual(without["termination"], "step_limit")  # the loop never ends on its own
        self.assertNotIn("return a / b\n", (self.repo / "calc" / "ops.py").read_text())

    def test_ignored_intervention_escalates_to_a_fresh_attempt(self):
        policy = StubbornPolicy()
        policy.told = lambda messages: "this is attempt 2" in [m for m in messages if m["role"] == "user"][0]["content"]
        result, _ = self.run_policy(policy, memory=True, attempts=2, steps=40)
        attempts = result["proof"]["attempts"]
        self.assertEqual(attempts[0]["termination"], "no_progress")
        self.assertEqual(result["proof"]["level"], "proven")
        self.assertEqual(result["progress"]["escalations"], 1)


class WrongInterpretationTest(TempDirCase):
    def test_wrong_generated_reproduction_cannot_discard_the_correct_fix(self):
        files = dict(CALC)
        # the repository's own tests do not cover the reported bug (no test_true), as in most real issues
        files["tests/test_ops.py"] = ("import unittest\nfrom calc import divide\n\n\nclass T(unittest.TestCase):\n"
                                      "    def test_exact(self):\n        self.assertEqual(divide(6, 3), 2)\n")
        files["tests/test_zero.py"] = ("import unittest\nfrom calc import divide\n\n\nclass Z(unittest.TestCase):\n"
                                       "    def test_zero_raises(self):\n        with self.assertRaises(ZeroDivisionError):\n"
                                       "            divide(1, 0)\n")
        repo = make_repo(self.tmp / "repo", files)
        scratch = self.tmp / "run" / "scratch"
        # a reproduction that misreads the issue: it also demands divide(1, 0) is None, which the existing
        # tests forbid
        wrong = ("import os, sys\nsys.path.insert(0, os.getcwd())\nfrom calc import divide\n"
                 "assert divide(7, 2) == 3.5\nassert divide(1, 0) is None\n")
        fix = tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")
        bend = tc("edit_file", path="calc/ops.py", old_str="return a // b",
                  new_str="return None if b == 0 else a / b")
        turns = [turn(tc("write_file", path=str(scratch / "repro.py"), content=wrong)),
                 turn(tc("register_reproduction", command=f"python3 {scratch / 'repro.py'}")),
                 turn(fix), turn(TEST), turn(tc("submit")), turn(tc("submit")),      # attempt 1: correct fix
                 turn(bend), turn(TEST), turn(tc("submit")), turn(tc("submit")), turn(tc("submit"))]  # attempt 2
        profile = test_profile()
        profile.policy.max_attempts, profile.policy.min_attempt_s = 2, 0.0
        result, _ = run_agent(repo, turns, self.tmp / "run", profile=profile)
        levels = [a["level"] for a in result["proof"]["attempts"]]
        self.assertEqual(levels[1], "refuted", result["proof"]["attempts"])  # breaks test_zero_raises
        self.assertEqual(result["selected_candidate"]["tree"], result["proof"]["attempts"][0]["candidate"])
        self.assertEqual((repo / "calc" / "ops.py").read_text(), "def divide(a, b):\n    return a / b\n")


class TaskTypeTest(TempDirCase):
    def test_classification_and_prompt_hint(self):
        cases = {"Refactor: move the parser into its own module": "refactor",
                 "divide() used to work in 2.1, broken since 2.2": "regression",
                 "Feature request: add support for custom separators": "feature",
                 "pip install fails to build the wheel on 3.13": "build_config",
                 "Update the tests for the new default timeout": "test_maintenance",
                 "ValueError raised when parsing empty input": "bug"}
        for text, kind in cases.items():
            self.assertEqual(classify(text).kind, kind, text)
        repo = make_repo(self.tmp / "repo", CALC)
        result, agent = run_agent(repo, [turn(tc("submit")), turn(tc("submit"))], self.tmp / "run",
                                  issue="divide() used to work in 2.1 but returns an int since 2.2")
        self.assertEqual(result["task_type"]["kind"], "regression")
        first = [m for m in agent.transcript if m["role"] == "user"][0]["content"]
        self.assertIn("git log -S", first)
