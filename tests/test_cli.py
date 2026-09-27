import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.helpers import CALC, ROOT, TEST_CMD, TempDirCase, make_repo, tc, turn

FIX = tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")


def base_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("AI_API_KEY", "AI_MODEL", "AI_BASE_URL", "AI_PROVIDER",
                                                          "ARBITER_PROFILE", "ARBITER_OUT")}
    env.update(extra)
    return env


class CliTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "repo", CALC)
        self.out = self.tmp / "out"

    def fake_profile(self, turns, **limits):
        script = self.tmp / "script.json"
        script.write_text(json.dumps(turns))
        lim = "".join(f"{k} = {v}\n" for k, v in limits.items())
        prof = self.tmp / "fake.toml"
        prof.write_text(f'name = "t"\n[model]\nprovider = "fake"\nscript = "{script}"\n[limits]\n{lim}')
        return prof

    def task_line(self, task_id="t1", **kw):
        return json.dumps({"task_id": task_id, "repo_path": str(self.repo), "issue": "fix divide", **kw}) + "\n"

    def run_cli(self, args, stdin="", env=None, timeout=120):
        return subprocess.run([sys.executable, "-m", "arbiter", *args], input=stdin, capture_output=True, text=True,
                              cwd=ROOT, env=env or base_env(), timeout=timeout)

    def records(self, proc):
        return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]

    def test_missing_credential_fails_clearly_per_task(self):
        p = self.run_cli(["run", "--out", str(self.out)], self.task_line() + self.task_line("t2"),
                         env=base_env(AI_MODEL="some-model", AI_BASE_URL="https://example.invalid/v1"))
        self.assertEqual(p.returncode, 2)
        recs = self.records(p)
        self.assertEqual([r["status"] for r in recs], ["configuration_error"])  # fails before any task starts
        self.assertIn("AI_API_KEY is not set", recs[0]["error"]["message"])
        self.assertEqual((self.repo / "calc/ops.py").read_text(), CALC["calc/ops.py"])

    def test_missing_model_never_substituted(self):
        p = self.run_cli(["run", "--out", str(self.out)], self.task_line(), env=base_env(AI_API_KEY="k" * 20))
        self.assertEqual(p.returncode, 2)
        self.assertIn("never tried against other providers", self.records(p)[0]["error"]["message"])

    def test_stdin_jsonl_with_malformed_line_no_tty(self):
        prof = self.fake_profile([turn(FIX), turn(tc("bash", command=TEST_CMD)), turn(tc("submit"))])
        stdin = "{broken json\n" + self.task_line()
        p = self.run_cli(["run", "--profile", str(prof), "--out", str(self.out)], stdin)
        recs = self.records(p)
        self.assertEqual([r["status"] for r in recs], ["invalid_input", "completed"], p.stderr[-2000:])
        self.assertEqual(p.returncode, 1)
        self.assertTrue(recs[1]["submission_ready"])
        self.assertEqual(recs[1]["verification"]["status"], "checks_passed")
        self.assertIn("step 1", p.stderr)

    def test_make_run_target(self):
        prof = self.fake_profile([turn(FIX), turn(tc("submit"))])
        taskfile = self.tmp / "task.json"
        taskfile.write_text(json.dumps({"task_id": "mk", "repo_path": str(self.repo), "issue": "fix"}, indent=2))
        p = subprocess.run(["make", "-s", "run", f"TASK={taskfile}", f"PROFILE={prof}", f"OUT={self.out}"], cwd=ROOT,
                           capture_output=True, text=True, env=base_env(), timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        (rec,) = self.records(p)
        self.assertEqual(rec["task_id"], "mk")
        self.assertIn("+    return a / b", Path(rec["deliverable"]["patch_path"]).read_text())

    def test_task_from_fifo_and_empty_device(self):
        prof = self.fake_profile([turn(FIX), turn(tc("submit"))])
        p = self.run_cli(["run", "--profile", str(prof), "--out", str(self.out), "--task", "/dev/null"])
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        fifo = self.tmp / "tasks.fifo"
        os.mkfifo(fifo)
        proc = subprocess.Popen([sys.executable, "-m", "arbiter", "run", "--profile", str(prof), "--out", str(self.out),
                                 "--task", str(fifo)], cwd=ROOT, env=base_env(), stdout=subprocess.PIPE, text=True)
        with open(fifo, "w") as fh:
            fh.write(self.task_line("fifo-task"))
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(json.loads(out)["task_id"], "fifo-task")

    def test_output_dir_inside_repo_rejected(self):
        prof = self.fake_profile([turn(tc("submit"))])
        p = self.run_cli(["run", "--profile", str(prof), "--out", str(self.repo / "runs")], self.task_line())
        self.assertEqual(self.records(p)[0]["status"], "configuration_error")

    def _start_slow_run(self):
        prof = self.fake_profile([turn(FIX), turn(tc("bash", command="sleep 60")), turn(tc("submit"))])
        proc = subprocess.Popen([sys.executable, "-m", "arbiter", "run", "--profile", str(prof), "--out", str(self.out)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=ROOT, env=base_env())
        proc.stdin.write(self.task_line())
        proc.stdin.close()
        proc.stdin = None
        deadline = time.time() + 30
        while time.time() < deadline:
            states = list(self.out.rglob("state.json"))
            if states and json.loads(states[0].read_text()).get("step", 0) >= 1:
                acts = list(self.out.rglob("actions.jsonl"))
                if acts and "sleep 60" not in acts[0].read_text():
                    time.sleep(0.5)  # now inside the long bash call
                    return proc, states[0].parent
            time.sleep(0.1)
        proc.kill()
        self.fail("run did not reach step 1")

    def test_sigterm_finalises_with_best_candidate(self):
        proc, run_dir = self._start_slow_run()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        (rec,) = [json.loads(l) for l in out.splitlines() if l.strip()]
        self.assertEqual(rec["termination"], "cancelled", err[-2000:])
        self.assertTrue(rec["submission_ready"])
        self.assertIn("+    return a / b", (run_dir / "patch.diff").read_text())

    def test_sigkill_then_offline_recovery_and_identity_check(self):
        proc, run_dir = self._start_slow_run()
        proc.kill()
        proc.wait(timeout=10)
        self.assertFalse((run_dir / "result.json").exists())
        # identity mismatch is refused
        task = json.loads((run_dir / "task.json").read_text())
        (run_dir / "task.json").write_text(json.dumps({**task, "issue": "different"}))
        p = self.run_cli(["finalize", "--run-dir", str(run_dir)])
        self.assertEqual(p.returncode, 2)
        self.assertIn("identity mismatch", p.stderr)
        (run_dir / "task.json").write_text(json.dumps(task))
        p = self.run_cli(["finalize", "--run-dir", str(run_dir)])
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        rec = self.records(p)[0]
        self.assertEqual(rec["termination"], "recovered_after_interruption")
        self.assertEqual(rec["usage"]["requests"], 2)  # ledger rebuilt from requests.jsonl, not reset to zero
        self.assertGreater(rec["usage"]["total_tokens"], 0)
        self.assertTrue(rec["deliverable"]["reconstruction_verified"])
        self.assertIn("+    return a / b", (run_dir / "patch.diff").read_text())
        p = self.run_cli(["finalize", "--run-dir", str(run_dir)])
        self.assertEqual(p.returncode, 2)  # already finalised

    def test_kill_during_model_call_counts_unknown_usage(self):
        prof = self.fake_profile([turn(FIX), {"sleep_s": 60, "text": "slow", "tool_calls": []}])
        proc = subprocess.Popen([sys.executable, "-m", "arbiter", "run", "--profile", str(prof), "--out", str(self.out)],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                                cwd=ROOT, env=base_env())
        proc.stdin.write(self.task_line())
        proc.stdin.close()
        deadline = time.time() + 30
        while time.time() < deadline and not list(self.out.rglob("inflight.json")):
            time.sleep(0.1)
        states = list(self.out.rglob("state.json"))
        while time.time() < deadline and not (states and json.loads(states[0].read_text()).get("step", 0) >= 1
                                               and list(self.out.rglob("inflight.json"))):
            time.sleep(0.1)
            states = list(self.out.rglob("state.json"))
        proc.kill()
        proc.wait(timeout=10)
        run_dir = states[0].parent
        p = self.run_cli(["finalize", "--run-dir", str(run_dir)])
        rec = self.records(p)[0]
        self.assertEqual(rec["usage"]["requests_usage_unknown"], 1)
        self.assertFalse(rec["usage"]["usage_complete"])
        self.assertIn("+    return a / b", (run_dir / "patch.diff").read_text())

    def test_check_config(self):
        prof = self.fake_profile([])
        p = self.run_cli(["check-config", "--profile", str(prof)])
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_cli(["check-config"], env=base_env(AI_MODEL="m", AI_BASE_URL="https://x/v1"))
        self.assertEqual(p.returncode, 2)

    def test_make_from_another_directory_resolves_caller_relative_paths(self):
        prof = self.fake_profile([turn(FIX), turn(tc("submit"))])
        caller = self.tmp / "caller"
        caller.mkdir()
        (caller / "task.json").write_text(json.dumps({"task_id": "rel", "repo_path": str(self.repo), "issue": "fix"}))
        p = subprocess.run(["make", "-s", "-f", str(ROOT / "Makefile"), "run", "TASK=task.json", f"PROFILE={prof}",
                            "OUT=my runs", "MAX_STEPS=7"], cwd=caller, capture_output=True, text=True,
                           env=base_env(), timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        (rec,) = self.records(p)
        self.assertTrue(Path(rec["run_dir"]).is_relative_to(caller / "my runs"))
        limits = json.loads((Path(rec["run_dir"]) / "profile.json").read_text())["limits"]
        self.assertEqual(limits["max_steps"], 7)


class PromptReaderTest(unittest.TestCase):
    def reader(self, text):
        import io

        from arbiter.cli import PromptReader

        return PromptReader(io.StringIO(text), io.StringIO())

    def test_bracketed_paste_with_blank_lines_is_one_entry(self):
        r = self.reader("\x1b[200~Title\n\nBody line\n\x1b[201~\n")
        self.assertEqual(r.read_entry("> "), "Title\n\nBody line\n")
        self.assertIsNone(r.read_entry("> "))

    def test_paste_without_trailing_newline_and_text_typed_after_it(self):
        self.assertEqual(self.reader("\x1b[200~line1\nline2\x1b[201~\n").read_entry("> "), "line1\nline2")
        self.assertEqual(self.reader("\x1b[200~see\x1b[201~ below\n").read_entry("> "), "seebelow")
        self.assertEqual(self.reader("\x1b[200~text\x1b[201~/go\n").read_entry("> "), "text")

    def test_single_line_forms_are_taken_immediately(self):
        for line in ("https://github.com/o/r/issues/3\n", "o/r#3\n", "@issue.md\n", "/quit\n"):
            self.assertEqual(self.reader(line + "unrelated\n").read_entry("> "), line)

    def test_typed_text_ends_with_go_or_eof(self):
        self.assertEqual(self.reader("[Bug] divide\ndetails\n/go\nnext\n").read_entry("> "), "[Bug] divide\ndetails\n")
        self.assertEqual(self.reader("only line\n").read_entry("> "), "only line\n")
