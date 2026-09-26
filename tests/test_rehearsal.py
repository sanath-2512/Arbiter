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
                     if l.startswith("+") and not l.startswith("+++")]
            added = [l for l in added if len(l) > 12]  # meaningful lines, not "}" or "return x"
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


class ManifestTest(TempDirCase):
    """The gauntlet and mechanism manifests describe what the lab claims to run."""

    def test_gauntlet_has_the_declared_mix_of_existing_tasks(self):
        g = json.loads((ROOT / "rehearsal" / "manifests" / "gauntlet.json").read_text())
        names = [t["task"] for t in g["tasks"]]
        self.assertEqual(len(names), 20)
        self.assertEqual(len(set(names)), 20)
        for t in g["tasks"]:
            task = json.loads((R.TASKS / t["task"] / "task.json").read_text())
            self.assertEqual((task["size_class"], task["type"]), (t["complexity"], t["type"]), t["task"])
        count = lambda key, value: sum(1 for t in g["tasks"] if t[key] == value)  # noqa: E731
        for size, n in g["mix"]["complexity"].items():
            self.assertEqual(count("complexity", size), n, size)
        for kind, n in g["mix"]["type"].items():
            got = sum(count("type", k) for k in (("build_config", "test_maintenance") if kind == "build_config_test"
                                                 else (kind,)))
            self.assertEqual(got, n, kind)
        kinds = {i["kind"] for i in g["injections"]}
        self.assertEqual(kinds, {"timeout", "stale_edit", "bad_candidate", "tool_failure", "interrupted_run"})
        for inj in g["injections"]:
            self.assertIn(inj["task"], names)
            if inj.get("policy"):
                self.assertTrue((ROOT / "rehearsal" / "policies" / f"{inj['policy']}.py").exists(), inj["policy"])

    def test_gauntlet_plan_without_a_key_runs_only_scripted_injections(self):
        g = json.loads((ROOT / "rehearsal" / "manifests" / "gauntlet.json").read_text())
        runs, notes = R.gauntlet_plan(g, ["A", "F"], 1, live=False, upstream=None)
        self.assertTrue(runs)
        self.assertTrue(all(r["injection"]["mode"] == "scripted" for r in runs))
        self.assertTrue(any("need the prescribed model" in n for n in notes))
        live, _ = R.gauntlet_plan(g, ["A", "F"], 2, live=True, upstream="http://127.0.0.1:1")
        self.assertEqual(sum(1 for r in live if "injection" not in r), 20 * 2 * 2)

    def test_mechanism_scenarios_reference_existing_tasks_and_policies(self):
        m = json.loads((ROOT / "rehearsal" / "manifests" / "mechanisms.json").read_text())
        configs = json.loads(R.CONFIGS.read_text())
        for sc in m["scenarios"]:
            self.assertTrue((R.TASKS / sc["task"] / "task.json").exists(), sc["id"])
            self.assertTrue((ROOT / "rehearsal" / "policies" / f"{sc['policy']}.py").exists(), sc["id"])
            self.assertTrue(set(sc["configs"]) <= set(configs), sc["id"])

    def test_expectations(self):
        sc = {"expect": {"*": {"solved": True}, "F": {"interventions_min": 1, "after_intervention_max": 0}}}
        self.assertEqual(R.expectation(sc, "F", {"solved": True, "interventions": 1, "after_intervention": 0}),
                         (True, []))
        ok, misses = R.expectation(sc, "F", {"solved": True, "interventions": 0, "after_intervention": 2})
        self.assertFalse(ok)
        self.assertEqual(len(misses), 2)
        self.assertEqual(R.expectation({"expect": {"F": {"solved": True}}}, "A", {"solved": False}), (None, []))


class OverlayTest(TempDirCase):
    def test_layered_overlay_is_deterministic_and_unremarkable(self):
        src = make_repo(self.tmp / "upstream", CALC)
        base = git(src, "rev-parse", "HEAD").strip()
        mirror = self.tmp / "calc.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(mirror)], check=True)
        overlay = {"layers": [
            {"kind": "vendored_copy", "dest": "legacy/vendor", "src": ["calc/ops.py"]},
            {"kind": "generated_bulk", "dir": "gen", "files": 250, "mention": "divide", "mention_every": 50},
            {"kind": "long_line", "path": "assets/index.min.js", "mention": "divide", "bytes": 200_000}]}
        task = {"task_id": "t", "repo": "calc", "base_commit": base, "history_depth": 5, "overlay": overlay}
        repo = {"mirror": str(mirror)}
        a = R.build_base(task, repo, self.tmp / "a")
        b = R.build_base(task, repo, self.tmp / "b")
        head = lambda d: git(d, "rev-parse", "HEAD").strip()  # noqa: E731
        self.assertEqual(head(a), head(b))  # same base for every run and for the judge
        self.assertNotIn("rehearsal", git(a, "log", "--format=%s", "-1").lower())
        self.assertEqual((a / "legacy/vendor/calc/ops.py").read_text(), (a / "calc/ops.py").read_text())
        self.assertEqual(len(list((a / "gen").rglob("*.py"))), 250)
        self.assertEqual(sum("divide" in p.read_text() for p in (a / "gen").rglob("*.py")), 5)
        text = (a / "assets/index.min.js").read_text()
        self.assertNotIn("\n", text)
        self.assertGreater(len(text), 150_000)
