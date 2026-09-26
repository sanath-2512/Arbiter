"""Tasks that carry the evaluation's tests (SWE-bench fields: FAIL_TO_PASS, PASS_TO_PASS, test_patch)."""

import json
import subprocess
from pathlib import Path

from gheerefill.task import evaluation_tests, parse_task
from tests.helpers import CALC, TEST_CMD, TempDirCase, git, make_repo, run_agent, tc, turn

TEST_PATCH = """diff --git a/tests/test_half.py b/tests/test_half.py
new file mode 100644
--- /dev/null
+++ b/tests/test_half.py
@@ -0,0 +1,7 @@
+import unittest
+from calc import divide
+
+
+class H(unittest.TestCase):
+    def test_half(self):
+        self.assertEqual(divide(1, 2), 0.5)
"""
FIX = turn(tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b"))
RUN = turn(tc("bash", command=TEST_CMD))
SUBMIT = turn(tc("submit", summary="true division"))


class EvaluationTestsTest(TempDirCase):
    def test_fields_in_every_spelling(self):
        self.assertEqual(evaluation_tests({"FAIL_TO_PASS": '["a::t1", "a::t2"]', "PASS_TO_PASS": "[]"}),
                         {"fail_to_pass": ["a::t1", "a::t2"]})
        self.assertEqual(evaluation_tests({"tests_to_pass": ["x"], "test_cmd": "pytest -q"}),
                         {"fail_to_pass": ["x"], "test_command": "pytest -q"})
        self.assertEqual(evaluation_tests({"FAIL_TO_PASS": "t1\nt2"}), {"fail_to_pass": ["t1", "t2"]})

    def test_swe_bench_instance_names_a_remote_repository(self):
        t = parse_task({"instance_id": "django__django-1", "repo": "django/django", "base_commit": "abc",
                        "problem_statement": "x", "FAIL_TO_PASS": "[]"}, Path("/nonexistent"))
        self.assertEqual((t.metadata["remote_repo"], t.metadata["base_commit"]), ("django/django", "abc"))
        with self.assertRaises(ValueError):
            parse_task({"id": "x", "repo": "not a slug/at all/x", "issue": "y"}, Path("/nonexistent"))

    def test_supplied_tests_are_applied_proven_and_kept_out_of_the_patch(self):
        repo = make_repo(self.tmp / "repo", CALC)
        seen = []
        read_new_test = turn(tc("read_file", path="tests/test_half.py"))
        result, agent = run_agent(repo, [read_new_test, FIX, RUN, SUBMIT, SUBMIT], self.tmp / "run",
                                  metadata={"test_patch": TEST_PATCH, "FAIL_TO_PASS": '["tests/test_half.py::H::test_half"]'})
        task_msg = next(m for m in agent.transcript if m["role"] == "user")["content"]
        self.assertIn("Tests the evaluation will run", task_msg)
        self.assertIn("tests/test_half.py::H::test_half", task_msg)
        read = next(m for m in agent.transcript if m["role"] == "tool")["content"]
        self.assertIn("divide(1, 2), 0.5", read)  # the test was in the working tree from the start
        self.assertEqual(result["proof"]["level"], "proven", result["proof"])
        c = result["proof"]["comparisons"][0]
        self.assertIn("test_half", " ".join(c["fail_to_pass"]))  # fails without the fix, passes with it
        patch = Path(result["deliverable"]["patch_path"]).read_text()
        self.assertIn("calc/ops.py", patch)
        self.assertNotIn("test_half", patch)  # the evaluator applies its own copy
        self.assertTrue(result["deliverable"]["reconstruction_verified"])
        self.assertTrue(result["submission_ready"])
        # the evaluator's flow: clean base + our patch + their test patch applies and passes
        clone = self.tmp / "judge"
        subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
        git(clone, "checkout", "-q", git(repo, "rev-parse", "HEAD").strip())
        subprocess.run(["git", "checkout", "-q", "HEAD", "--", "."], cwd=clone, check=True)
        subprocess.run(["git", "apply", result["deliverable"]["patch_path"]], cwd=clone, check=True)
        subprocess.run(["git", "apply", "-"], input=TEST_PATCH.encode(), cwd=clone, check=True)
        p = subprocess.run(TEST_CMD.split(), cwd=clone, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_new_attempt_starts_with_the_supplied_tests(self):
        from tests.helpers import test_profile
        repo = make_repo(self.tmp / "repo", CALC)
        profile = test_profile()
        profile.policy.max_attempts, profile.policy.min_attempt_s = 2, 0.0
        breaking = turn(tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return 0"))
        turns = [breaking, RUN, SUBMIT, SUBMIT, SUBMIT, turn(tc("read_file", path="tests/test_half.py")), FIX, RUN, SUBMIT]
        result, agent = run_agent(repo, turns, self.tmp / "run", profile=profile, metadata={"test_patch": TEST_PATCH})
        self.assertEqual(len(result["proof"]["attempts"]), 2, result["proof"]["attempts"])
        reads = [m["content"] for m in agent.transcript if m["role"] == "tool" and "test_half" in m["content"]]
        self.assertTrue(reads)
        self.assertNotIn("test_half", Path(result["deliverable"]["patch_path"]).read_text())


class TamperedEvaluationTestsTest(TempDirCase):
    def test_model_cannot_pass_by_editing_the_supplied_tests(self):
        repo = make_repo(self.tmp / "repo", CALC)
        meta = {"test_patch": TEST_PATCH, "FAIL_TO_PASS": '["tests/test_half.py::H::test_half"]'}
        weaken = turn(tc("edit_file", path="tests/test_half.py", old_str="0.5", new_str="0"))
        sed = turn(tc("bash", command="sed -i 's/0.5/0/' tests/test_half.py"))
        result, agent = run_agent(repo, [weaken, sed, RUN, SUBMIT, FIX, RUN, SUBMIT, SUBMIT], self.tmp / "run",
                                  metadata=meta)
        tool = [m["content"] for m in agent.transcript if m["role"] == "tool"]
        self.assertIn("one of the evaluation's test files", tool[0])  # edit_file refused
        self.assertTrue(any("put the originals back" in t for t in tool), tool)  # the sed edit undone at submit
        self.assertEqual((repo / "tests" / "test_half.py").exists(), False)  # not delivered, as before
        self.assertEqual(result["proof"]["level"], "proven", result["proof"])  # proven against the real test
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())


class RunHintTest(TempDirCase):
    def test_runner_derived_from_test_name_format(self):
        from gheerefill import prompts
        repo = self.tmp / "django"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "runtests.py").write_text("")
        (repo / "tests" / "test_sqlite.py").write_text("")
        self.assertEqual(prompts.run_hint(["test_ipv6 (servers.tests.LiveServerAddress)"], repo),
                         "python tests/runtests.py --settings=test_sqlite --parallel 1 "
                         "servers.tests.LiveServerAddress.test_ipv6")
        (self.tmp / "go").mkdir()
        (self.tmp / "go" / "go.mod").write_text("module x\n")
        self.assertEqual(prompts.run_hint(["TestParse/empty", "TestParse/nil", "TestLex"], self.tmp / "go"),
                         "go test ./... -run '^(TestLex|TestParse)$'")
        self.assertIsNone(prompts.run_hint(["test_foo"], self.tmp))
        text = prompts.evaluation_tests({"test_patch": "+    def test_added(self):\n"}, ["t.py"], self.tmp)
        self.assertIn("add or modify: test_added", text)
