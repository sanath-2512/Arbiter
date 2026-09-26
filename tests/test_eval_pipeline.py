"""The development evaluator end to end, through the REAL HTTP transports of both systems, against a
scripted OpenAI-compatible server. Verifies wiring and matched conditions, not model capability."""

import json
import os
import subprocess
import sys
import unittest

from tests.fake_openai_server import FakeOpenAIServer
from tests.helpers import ROOT, TempDirCase

OURS_TOOLS = "bash,edit_file,read_file,read_output,register_reproduction,search,submit,write_file"
SCRIPT = {
    OURS_TOOLS: [
        {"content": "inspect", "tool_calls": [{"name": "read_file", "arguments": {"path": "calc/ops.py"}}]},
        {"content": "fix", "tool_calls": [{"name": "edit_file", "arguments": {
            "path": "calc/ops.py", "old_str": "return a // b", "new_str": "return a / b"}}]},
        {"content": "verify", "tool_calls": [{"name": "bash", "arguments": {"command": "python3 -m unittest discover -s tests"}}]},
        {"content": "done", "tool_calls": [{"name": "submit", "arguments": {"summary": "true division"}}]},
        # the harness's review (no check fails without the change in the visible tests) -> confirm
        {"content": "confirm", "tool_calls": [{"name": "submit", "arguments": {"summary": "true division"}}]},
    ],
    "bash,edit,read,write": [
        {"content": "fix", "tool_calls": [{"name": "edit", "arguments": {
            "path": "calc/ops.py", "edits": [{"oldText": "return a // b", "newText": "return a / b"}]}}]},
        {"content": "Fixed divide to use true division."},
    ],
    "bash": [
        {"content": "fix", "tool_calls": [{"name": "bash", "arguments": {"command": "sed -i 's|a // b|a / b|' calc/ops.py"}}]},
        {"content": "done", "tool_calls": [{"name": "bash", "arguments": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}}]},
    ],
}


class EvalPipelineTest(TempDirCase):
    def test_validate_suite_python_tasks(self):
        p = subprocess.run([sys.executable, "scripts/eval.py", "--validate-suite", "--out", str(self.tmp / "v"),
                            "--tasks", "calc-divide,py-slugify,py-deep-merge,py-ttl-cache"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.count("OK "), 4)

    def test_all_systems_through_real_transports(self):
        systems = ["ours"]
        if (ROOT / ".venv-baseline" / "bin" / "python").exists():
            systems.append("mini")
        if (ROOT / "baselines" / "pi" / "node_modules" / ".bin" / "pi").exists():
            systems.append("pi")
        with FakeOpenAIServer(SCRIPT) as srv:
            env = {**os.environ, "AI_API_KEY": "sk-fake-000000", "AI_MODEL": "scripted-model", "AI_BASE_URL": srv.base_url,
                   "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}
            p = subprocess.run([sys.executable, "scripts/eval.py", "--partition", "dev", "--tasks", "calc-divide",
                                "--systems", ",".join(systems), "--out", str(self.tmp / "e")],
                               cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-3000:])
        recs = [json.loads(l) for l in (self.tmp / "e" / "results.jsonl").read_text().splitlines()]
        self.assertEqual([r["system"] for r in recs], systems)
        for r in recs:
            self.assertTrue(r["label"]["judge_pass"], (r["system"], r.get("termination"), r.get("error")))
            self.assertEqual(r["audit"], [])
        ours = recs[0]
        self.assertEqual(ours["verification"], "checks_passed")
        self.assertEqual(ours["usage"]["requests"], 5)  # incl. the confirming submit after the harness review
        self.assertIn("Paired comparison" if len(systems) > 1 else "Records", (self.tmp / "e" / "summary.md").read_text())
        models = {json.loads(json.dumps(r["model"]))["name"] for r in recs}
        self.assertEqual(models, {"scripted-model"})  # identical model settings for every system
        if len(systems) < 3:
            raise unittest.SkipTest(f"only {systems} verified; install baselines with `make baseline-setup`")


class StreamingEndToEndTest(TempDirCase):
    def test_ours_streaming_solve_and_probe_through_scripted_server(self):
        import shutil

        repo = self.tmp / "repo"
        shutil.copytree(ROOT / "evalsuite" / "tasks" / "calc-divide" / "repo", repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        prof = self.tmp / "stream.toml"
        prof.write_text('name = "stream"\n[model]\nprovider = "openai_chat"\nstream = true\n')
        task = json.dumps({"task_id": "s1", "repo_path": str(repo), "issue": "divide truncates; use true division"})
        with FakeOpenAIServer(SCRIPT) as srv:
            env = {**os.environ, "AI_API_KEY": "sk-fake-000000", "AI_MODEL": "scripted-model", "AI_BASE_URL": srv.base_url,
                   "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--profile", str(prof), "--out",
                                str(self.tmp / "out")], input=task + "\n", cwd=ROOT, capture_output=True, text=True,
                               env=env, timeout=120)
            probe = subprocess.run([sys.executable, "-m", "gheerefill", "probe", "--profile", str(prof)], cwd=ROOT,
                                   capture_output=True, text=True, env=env, timeout=60)
        rec = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(rec["termination"], "model_submitted", p.stderr[-2000:])
        self.assertEqual(rec["verification"]["status"], "checks_passed")
        self.assertTrue(all(r["body"].get("stream") for r in srv.requests[:4]))
        report = json.loads(probe.stdout)
        self.assertTrue(report["alternate_mode"]["tool_call_ok"])
        self.assertNotIn("sk-fake-000000", probe.stdout)
