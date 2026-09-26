"""Judge Rehearsal Lab: evaluation boundary and an offline end-to-end judged run on a local fixture."""

import json
import re
import subprocess
import sys

from tests.helpers import CALC, ROOT, TempDirCase, git, make_repo

sys.path.insert(0, str(ROOT))
from scripts import rehearsal as R  # noqa: E402


class BoundaryTest(TempDirCase):
    def test_runtime_never_references_the_lab_or_eval_data(self):
        for path in (ROOT / "gheerefill").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"rehearsal|evalsuite", text), f"{path} references the evaluation lab")

    def test_labels_are_encoded_and_absent_from_public_task_files(self):
        for d in sorted(p for p in (ROOT / "rehearsal" / "tasks").iterdir() if (p / "task.json").exists()):
            raw = (d / "label.b64").read_text()
            self.assertNotIn("diff --git", raw)
            label = R.decode_label(raw)
            public = (d / "task.json").read_text()
            added = [l[1:].strip() for l in label["reference_patch"].splitlines()
                     if l.startswith("+") and not l.startswith("+++") and len(l.strip()) > 12]
            leaked = [l for l in added if l in public]
            self.assertEqual(leaked, [], f"{d.name}: reference fix lines appear in the public task")
            self.assertTrue(label["verify"]["fail_to_pass"], d.name)


class OfflineJudgedRunTest(TempDirCase):
    """The whole judge flow on a local 'mirror' with a scripted policy (no network, no model)."""

    def setUp(self):
        super().setUp()
        src = make_repo(self.tmp / "upstream", CALC)
        base = git(src, "rev-parse", "HEAD").strip()
        (src / "calc" / "ops.py").write_text("def divide(a, b):\n    return a / b\n")
        (src / "tests" / "test_half.py").write_text(
            "import unittest\nfrom calc import divide\n\n\nclass H(unittest.TestCase):\n"
            "    def test_half(self):\n        self.assertEqual(divide(1, 2), 0.5)\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "fix")
        mirrors = self.tmp / "mirrors"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(mirrors / "calc.git")], check=True)
        self.orig = {k: getattr(R, k) for k in ("TASKS", "MIRRORS", "MANIFEST", "CONFIGS", "WORK", "ENVS")}
        R.TASKS, R.MIRRORS, R.WORK, R.ENVS = self.tmp / "tasks", mirrors, self.tmp / "work", self.tmp / "envs"
        R.MANIFEST = self.tmp / "repos.json"
        R.MANIFEST.write_text(json.dumps({"calc": {"url": str(src), "mirror": "calc.git", "language": "python",
                                                   "size_class": "small", "env": {"system_python": True}}}))
        R.CONFIGS = self.orig["CONFIGS"]
        diff = lambda *paths: git(src, "diff", "--full-index", base, "HEAD", "--", *paths)  # noqa: E731
        d = R.TASKS / "calc_001"
        d.mkdir(parents=True)
        (d / "task.json").write_text(json.dumps({"task_id": "calc_001", "repo": "calc", "base_commit": base,
                                                 "type": "bug", "size_class": "small", "history_depth": 5,
                                                 "issue": "divide(1, 2) returns 0; expected 0.5",
                                                 "limits": {"time_limit_s": 120, "max_steps": 12}}))
        (d / "label.b64").write_text(R.encode_label({
            "test_patch": diff("tests"), "reference_patch": diff("calc"), "gold_files": ["calc/ops.py"],
            "verify": {"runner": "unittest", "cmd": "python3 -m unittest -v {tests}", "run_tests": ["tests.test_half",
                                                                                              "tests.test_ops"],
                       "fail_to_pass": ["tests.test_half.H.test_half"], "pass_to_pass": ["tests.test_ops.T.test_exact"]}}))
        pol = self.tmp / "policies"
        pol.mkdir()
        (pol / "_common.py").write_text((ROOT / "rehearsal" / "policies" / "_common.py").read_text())
        (pol / "fix.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
            "from _common import script, tc\n"
            "respond = script([tc('edit_file', path='calc/ops.py', old_str='a // b', new_str='a / b'),\n"
            "                  tc('bash', command='python3 -m unittest discover -s tests')])\n")
        (pol / "noop.py").write_text("def respond(messages, tools):\n"
                                     "    return {'content': '', 'tool_calls': [{'name': 'submit', 'arguments': {}}]}\n")
        from scripts import policy_server

        self.orig_pol = policy_server.POLICIES
        policy_server.POLICIES = pol

    def tearDown(self):
        from scripts import policy_server

        for k, v in self.orig.items():
            setattr(R, k, v)
        policy_server.POLICIES = self.orig_pol
        super().tearDown()

    def test_validate_then_judge_a_fix_and_an_empty_submission(self):
        (row,) = R.validate(R.all_tasks(), log=lambda *a: None)
        self.assertTrue(row["ok"], row)
        task = R.load_task(R.TASKS / "calc_001")
        good = R.run_one(task, "F", policy="fix", log=lambda *a: None)
        self.assertTrue(good["solved"], good)
        self.assertEqual((good["failure_class"], good["artifact_valid"], good["audit"]), ("solved", True, []))
        self.assertEqual(good["model_kind"], "scripted-policy")
        empty = R.run_one(task, "A", policy="noop", log=lambda *a: None)
        self.assertEqual((empty["solved"], empty["failure_class"]), (False, "empty_patch"))
        text = R.report([good, empty])
        self.assertIn("| F | 1/1 |", text)
        self.assertIn("not evidence about a real model's capability", text)
