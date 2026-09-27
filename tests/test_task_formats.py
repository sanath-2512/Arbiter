"""Task rows as published by the benchmark families an evaluator is likely to feed in."""

import io
import json
import subprocess
import sys
from pathlib import Path

from arbiter.intake import Request, build_task
from arbiter.task import Task, evaluation_tests, iter_tasks, parse_task
from tests.helpers import CALC, TempDirCase, git, make_repo

GOLD = "diff --git a/calc/ops.py b/calc/ops.py\n-    return a // b\n+    return a / b  # GOLD-SOLUTION\n"


class PublishedFormatsTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "repo", CALC)

    def test_swe_bench_row_never_carries_the_reference_patch(self):
        row = {"instance_id": "acme__calc-1", "repo": "acme/calc", "repo_path": str(self.repo), "base_commit": "HEAD",
               "problem_statement": "divide is wrong", "patch": GOLD, "hints_text": "", "version": "1.0",
               "FAIL_TO_PASS": '["tests/test_ops.py::test_divide"]', "PASS_TO_PASS": "[]", "test_patch": ""}
        (t,) = list(iter_tasks(io.StringIO(json.dumps(row)), base_dir=self.tmp))
        self.assertIsInstance(t, Task, t)
        self.assertEqual(t.repo_path, self.repo.resolve())  # the local checkout wins over the slug
        self.assertNotIn("GOLD-SOLUTION", json.dumps(t.to_dict()))
        self.assertEqual(t.metadata["withheld_fields"], ["patch"])
        self.assertEqual(evaluation_tests(t.metadata)["fail_to_pass"], ["tests/test_ops.py::test_divide"])

    def test_priority_instead_of_rejection(self):
        t = parse_task({"task_id": "a", "id": "b", "issue": "x", "problem_statement": "y", "repo": "acme/calc",
                        "repo_path": str(self.repo)}, self.tmp)
        self.assertEqual((t.task_id, t.issue, t.repo_path), ("a", "x", self.repo.resolve()))

    def test_swe_bench_pro_specification_fields(self):
        t = parse_task({"instance_id": "p1", "repo": str(self.repo), "problem_statement": "Add a modulo helper.",
                        "requirements": "- `mod(a, b)` returns a % b", "interface": "def mod(a: int, b: int) -> int",
                        "fail_to_pass": ["tests/test_ops.py::test_mod"]}, self.tmp)
        self.assertIn("## Requirements\n- `mod(a, b)` returns a % b", t.issue)
        self.assertIn("## Interface the tests expect\ndef mod(a: int, b: int) -> int", t.issue)

    def test_multi_swe_bench_row(self):
        row = {"org": "acme", "repo": "calc", "number": 12, "instance_id": "acme__calc-12",
               "resolved_issues": [{"number": 11, "title": "divide truncates", "body": "divide(7, 2) gives 3"}],
               "base": {"label": "acme:main", "ref": "main", "sha": "0123abc"}, "fix_patch": GOLD,
               "test_patch": "", "f2p_tests": {"tests.test_ops.test_divide": {"run": "FAIL", "test": "FAIL", "fix": "PASS"}},
               "p2p_tests": {}}
        t = parse_task(row, self.tmp)
        self.assertEqual(t.metadata["remote_repo"], "acme/calc")
        self.assertEqual(t.metadata["base_commit"], "0123abc")
        self.assertEqual(t.issue, "divide truncates\n\ndivide(7, 2) gives 3")
        self.assertEqual(evaluation_tests(t.metadata), {"fail_to_pass": ["tests.test_ops.test_divide"]})
        self.assertNotIn("GOLD-SOLUTION", json.dumps(t.to_dict()))

    def test_polybench_and_rebench_test_fields(self):
        t = parse_task({"instance_id": "x", "repo": str(self.repo), "problem_statement": "p", "F2P": "['a', 'b']",
                        "P2P": "c\nd", "test_command": "npx jest src"}, self.tmp)
        ev = evaluation_tests(t.metadata)
        self.assertEqual((ev["pass_to_pass"], ev["test_command"]), (["c", "d"], "npx jest src"))
        t = parse_task({"instance_id": "y", "repo": str(self.repo), "problem_statement": "p",
                        "install_config": {"test_cmd": ["pytest -rA", "pytest -k slow"]}}, self.tmp)
        self.assertEqual(evaluation_tests(t.metadata)["test_command"], "pytest -rA && pytest -k slow")

    def test_local_checkout_is_moved_to_the_base_commit(self):
        base = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "calc" / "later.py").write_text("X = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "later")
        t = parse_task({"instance_id": "b", "repo": str(self.repo), "problem_statement": "p", "base_commit": base},
                       self.tmp)
        logs = []
        build_task(Request(task=t, source="t"), repo_spec=None, base=None, workspace=self.tmp / "ws",
                   github_token=None, limits={}, log=logs.append)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), base)
        self.assertTrue(any("checked out base_commit" in m for m in logs), logs)
        # a dirty checkout at another commit is left alone
        git(self.repo, "checkout", "-q", "-")
        (self.repo / "calc" / "ops.py").write_text("# local edit\n")
        logs.clear()
        build_task(Request(task=t, source="t"), repo_spec=None, base=None, workspace=self.tmp / "ws",
                   github_token=None, limits={}, log=logs.append)
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD").strip(), base)
        self.assertTrue(any("left as it is" in m for m in logs), logs)


class ParallelClonesTest(TempDirCase):
    def test_parallel_tasks_of_one_repository_get_separate_clones(self):
        from arbiter.intake import prepare_repo, release_workspace
        src = make_repo(self.tmp / "upstream", CALC)
        ws = self.tmp / "ws"
        url = "file://" + str(src)
        a, _ = prepare_repo(url, owner="acme", repo="calc", number=None, workspace=ws, base=None,
                            issue_created_at=None, log=lambda m: None)
        b, _ = prepare_repo(url, owner="acme", repo="calc", number=None, workspace=ws, base=None,
                            issue_created_at=None, log=lambda m: None)
        self.assertNotEqual(a, b)  # a is clean but still held by its (running) task
        # a separate process sees both held
        code = ("import sys; from pathlib import Path; from arbiter.intake import claim_workspace; "
                "print(claim_workspace(Path(sys.argv[1])), claim_workspace(Path(sys.argv[2])))")
        out = subprocess.run([sys.executable, "-c", code, str(a), str(b)], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1]).stdout.strip()
        self.assertEqual(out, "False False")
        release_workspace(a)
        c, notes = prepare_repo(url, owner="acme", repo="calc", number=None, workspace=ws, base=None,
                                issue_created_at=None, log=lambda m: None)
        self.assertEqual(c, a)  # released and clean: reused
        self.assertTrue(any("reused clean clone" in n for n in notes))
        release_workspace(b)
        release_workspace(c)
