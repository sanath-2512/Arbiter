"""Credential-isolation regression tests (attacks performed against our own harness)."""

import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from arbiter.sandbox import Sandbox
from tests.helpers import CALC, ROOT, TEST_CMD, TempDirCase, git, make_repo, run_agent, tc, turn

SECRET = "sk-ant-api03-SECRETVALUE-0123456789abcdef"
SANDBOX_OK = Sandbox("key").probe().active


class CredentialIsolationTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "repo", CALC)
        self.loot = self.tmp / "loot.txt"

    def exfil_script(self) -> str:
        # What a prompt-injected model would try: its own env, every ancestor's environ, the harness.
        return (f"sh -c 'env; for p in $(ls /proc | grep -E \"^[0-9]+$\"); do tr \"\\\\0\" \"\\\\n\" "
                f"< /proc/$p/environ 2>/dev/null; done' >> {self.loot} 2>/dev/null; true")

    @mock.patch.dict(os.environ, {"AI_API_KEY": SECRET})
    def test_fsmonitor_injection_does_not_leak_key(self):
        """Regression: the model sets core.fsmonitor in the target repo; the harness's own git calls at
        finalisation used to execute it with AI_API_KEY in the environment."""
        turns = [turn(tc("bash", command=f'git config core.fsmonitor "{self.exfil_script()}"')),
                 turn(tc("submit")), turn(tc("submit"))]
        result, _ = run_agent(self.repo, turns, self.tmp / "run")
        leaked = self.loot.read_text() if self.loot.exists() else ""
        self.assertNotIn(SECRET, leaked)
        self.assertIsNotNone(result["integrity"]["target_git_control_files_changed"])  # tamper evidence

    @mock.patch.dict(os.environ, {"AI_API_KEY": SECRET})
    def test_preexisting_malicious_hooks_and_config_are_not_executed(self):
        git(self.repo, "config", "core.fsmonitor", self.exfil_script())
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\n{self.exfil_script()}\n")
        hook.chmod(0o755)
        turns = [turn(tc("edit_file", path="calc/ops.py", old_str="a // b", new_str="a / b")),
                 turn(tc("bash", command=TEST_CMD)), turn(tc("submit"))]
        run_agent(self.repo, turns, self.tmp / "run")
        self.assertFalse(self.loot.exists() and SECRET in self.loot.read_text())

    def run_cli_under_key_holding_parent(self, turns, sandbox: str) -> dict:
        """Process chain like the evaluator's: parent (stand-in for make, key in its *initial*
        environment) -> harness CLI -> model commands. Returns the result record."""
        import json

        script = self.tmp / f"script-{sandbox}.json"
        script.write_text(json.dumps(turns))
        prof = self.tmp / f"p-{sandbox}.toml"
        prof.write_text(f'[model]\nprovider = "fake"\nscript = "{script}"\n[policy]\nsandbox = "{sandbox}"\n')
        task = json.dumps({"task_id": "t", "repo_path": str(self.repo), "issue": "fix"})
        parent = ("import subprocess, sys; r = subprocess.run([sys.executable, '-m', 'arbiter', 'run', '--profile', "
                  f"{str(prof)!r}, '--out', {str(self.tmp / ('out-' + sandbox))!r}], input={task!r} + '\\n', "
                  "capture_output=True, text=True); print(r.stdout)")
        env = {**os.environ, "AI_API_KEY": SECRET}
        p = subprocess.run([sys.executable, "-c", parent], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        return json.loads(p.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(SANDBOX_OK, "Landlock sandbox unavailable on this kernel")
    def test_model_command_cannot_read_any_process_environment(self):
        turns = [turn(tc("bash", command=self.exfil_script())), turn(tc("submit")), turn(tc("submit"))]
        control = self.run_cli_under_key_holding_parent(turns, "off")
        self.assertFalse(control["isolation"]["active"])
        self.assertIn(SECRET, self.loot.read_text())  # sensitivity: the attack works without the sandbox
        self.loot.unlink()
        guarded = self.run_cli_under_key_holding_parent(turns, "key")
        self.assertTrue(guarded["isolation"]["active"])
        self.assertTrue(self.loot.exists())
        self.assertNotIn(SECRET, self.loot.read_text())

    @unittest.skipUnless(SANDBOX_OK, "Landlock sandbox unavailable on this kernel")
    def test_fsmonitor_injection_through_real_process_chain(self):
        turns = [turn(tc("bash", command=f'git config core.fsmonitor "{self.exfil_script()}"')),
                 turn(tc("submit")), turn(tc("submit"))]
        result = self.run_cli_under_key_holding_parent(turns, "key")
        self.assertFalse(self.loot.exists() and SECRET in self.loot.read_text())
        self.assertIsNotNone(result["integrity"]["target_git_control_files_changed"])

    @unittest.skipUnless(os.path.isdir("/proc"), "/proc is unavailable on this platform")
    def test_harness_process_scrubs_its_own_environment(self):
        code = ("from arbiter.credentials import take_credential; import sys, time; "
                "v = take_credential('AI_API_KEY'); sys.stdout.write(str(len(v)) + '\\n'); sys.stdout.flush(); time.sleep(5)")
        p = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "AI_API_KEY": SECRET})
        try:
            self.assertEqual(p.stdout.readline().strip(), str(len(SECRET)))  # value was obtained...
            try:
                environ = open(f"/proc/{p.pid}/environ", "rb").read()
            except PermissionError:
                environ = b""  # non-dumpable: not readable at all without CAP_SYS_PTRACE
            self.assertNotIn(SECRET.encode(), environ)  # ...and scrubbed from the process
        finally:
            p.kill()
            p.wait()


class SandboxLauncherTest(TempDirCase):
    @unittest.skipUnless(SANDBOX_OK, "Landlock sandbox unavailable on this kernel")
    def test_confine_mode_blocks_writes_outside_allowed_paths(self):
        allowed = self.tmp / "allowed"
        allowed.mkdir()
        sb = Sandbox("confine", [str(allowed)]).probe()
        self.assertTrue(sb.active, sb.status)
        outside = self.tmp / "outside.txt"
        p = subprocess.run(sb.wrap(["bash", "-c", f"echo x > {allowed}/ok && echo y > {outside}"]),
                           capture_output=True, text=True)
        self.assertTrue((allowed / "ok").exists())
        self.assertFalse(outside.exists())
        self.assertIn("Permission denied", p.stderr)

    def test_launcher_never_runs_unsandboxed_silently(self):
        # Invalid usage must fail closed (exit 126), not fall through to running the command.
        p = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "arbiter" / "sandbox_exec.py"), "{}", "true"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 126)
