"""Attack catalogue: inputs, environments, repositories and model behaviour chosen to break the harness.

Each test names the attack and the property that must survive it. (Many other attacks live next to
the code they exercise: tests/test_security.py, test_shell.py, test_tools.py, test_chaos.py,
test_provider_emulation.py, test_runners.py.)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from gheerefill.outputs import clean_terminal_text
from scripts.provider_emulator import ProviderEmulator
from tests.helpers import CALC, ROOT, TEST_CMD, TempDirCase, git, make_repo, run_agent, tc, test_profile, turn

FIX = turn(tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b"))
RUN = turn(tc("bash", command=TEST_CMD))
SUBMIT = turn(tc("submit", summary="done"))


def scripted(plan):
    def respond(messages, tools):
        i = sum(1 for m in messages if m.get("role") == "assistant")
        if i < len(plan):
            return plan[i]
        return {"content": "done", "tool_calls": [{"name": "submit", "arguments": {"summary": "done"}}]}
    return respond


FIX_PLAN = [
    {"content": "fix", "tool_calls": [{"name": "edit_file", "arguments": {
        "path": "calc/ops.py", "old_str": "return a // b", "new_str": "return a / b"}}]},
    {"content": "verify", "tool_calls": [{"name": "bash", "arguments": {"command": TEST_CMD}}]},
]


class ProcedureAttackTest(TempDirCase):
    """Through `make run` / the CLI, the way the evaluator starts the harness."""

    def make_run(self, emu, repo, issue, extra_env=None, timeout=240):
        prof = self.tmp / "p.toml"
        prof.write_text('[model]\nprovider = "openai_chat"\nname = "m"\nbase_url = "http://127.0.0.1:1/v1"\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AI_", "ISSUE", "REPO", "GHEEREFILL_"))}
        env.update(AI_API_KEY="sk-attack-000000000000", AI_BASE_URL=emu.base_url if emu else "http://127.0.0.1:9/v1",
                   GHEEREFILL_PROFILE=str(prof), GHEEREFILL_OUT=str(self.tmp / "out"), no_proxy="127.0.0.1",
                   NO_PROXY="127.0.0.1")
        env.update(extra_env or {})
        p = subprocess.run(["make", "-s", "run", f"ISSUE={issue}", f"REPO={repo}"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        return p, (json.loads(lines[-1]) if lines else {})

    def test_shell_and_make_metacharacters_in_issue_are_data(self):
        repo = make_repo(self.tmp / "repo", CALC)
        canary = self.tmp / "PWNED"
        issue = (f"divide(7, 2) should be 3.5 $(shell touch {canary}) $(touch {canary}) `touch {canary}` "
                 f"\"; touch {canary}; \" 'quoted' $$HOME $(AI_API_KEY) ${{AI_API_KEY}} %s # not a comment \\n\n"
                 " second line")
        with ProviderEmulator(scripted(FIX_PLAN), "qwen", scale=0.0) as emu:
            p, rec = self.make_run(emu, repo, issue)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertFalse(canary.exists())
        task = json.loads((Path(rec["run_dir"]) / "task.json").read_text())
        self.assertEqual(task["issue"].strip(), issue.strip())
        self.assertNotIn("sk-attack", p.stdout + p.stderr)

    def test_posix_locale_and_non_ascii_issue(self):
        repo = make_repo(self.tmp / "repo", CALC)
        issue = "divide(7, 2) → 3.5 ✗ 得到 3 — مرحبا 🧮 e\u0301"
        with ProviderEmulator(scripted(FIX_PLAN), "deepseek", scale=0.0) as emu:
            p, rec = self.make_run(emu, repo, issue, {"LANG": "C", "LC_ALL": "C", "PYTHONIOENCODING": ""})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertIn("🧮", json.loads((Path(rec["run_dir"]) / "task.json").read_text(encoding="utf-8"))["issue"])
        self.assertIn("return a / b", (repo / "calc/ops.py").read_text())

    def test_unreachable_endpoint_exits_nonzero_quickly(self):
        repo = make_repo(self.tmp / "repo", CALC)
        t0 = time.monotonic()
        p, rec = self.make_run(None, repo, "fix divide", timeout=200)
        self.assertNotEqual(p.returncode, 0, p.stderr[-2000:])  # the CLI exits 4; make reports it as its own 2
        self.assertIn("no response from the model endpoint", p.stderr)
        self.assertIn("Error 4", p.stderr)
        self.assertLess(time.monotonic() - t0, 150)
        self.assertEqual(git(repo, "status", "--porcelain").strip(), "")

    def test_empty_and_missing_issue_inputs(self):
        repo = make_repo(self.tmp / "repo", CALC)
        with ProviderEmulator(scripted(FIX_PLAN), "qwen", scale=0.0) as emu:
            p, _ = self.make_run(emu, repo, "   ")  # blank ISSUE = no ISSUE (TASK= or stdin may follow)
            self.assertIn("no task supplied", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            p, _ = self.make_run(emu, repo, "@does-not-exist.md")
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(emu.requests, 0)


class RepositoryAttackTest(TempDirCase):
    def test_repository_without_commits(self):
        repo = make_repo(self.tmp / "repo", CALC, commit=False)
        result, _ = run_agent(repo, [FIX, RUN, SUBMIT], self.tmp / "run")
        self.assertTrue(result["submission_ready"], result.get("error"))
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())

    def test_detached_head_and_merge_in_progress_are_left_as_found(self):
        repo = make_repo(self.tmp / "repo", CALC)
        git(repo, "checkout", "-q", "--detach")
        (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
        result, _ = run_agent(repo, [FIX, RUN, SUBMIT], self.tmp / "run")
        self.assertTrue(result["submission_ready"])
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), "HEAD")  # still detached
        self.assertTrue((repo / ".git" / "MERGE_HEAD").exists())

    def test_awkward_file_names_round_trip_through_the_patch(self):
        files = dict(CALC)
        name = 'dir with space/fïlé "q" (1).py'
        files[name] = "VALUE = 1\n"
        repo = make_repo(self.tmp / "repo", files)
        edit = turn(tc("edit_file", path=name, old_str="VALUE = 1", new_str="VALUE = 2"))
        new = turn(tc("write_file", path="néw dir/ünïcode ✓.txt", content="x\n"))
        result, _ = run_agent(repo, [edit, new, FIX, RUN, SUBMIT], self.tmp / "run")
        self.assertTrue(result["deliverable"]["reconstruction_verified"], result["deliverable"])
        clone = self.tmp / "clean"
        subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
        subprocess.run(["git", "checkout", "-q", "HEAD", "--", "."], cwd=clone, check=True)
        subprocess.run(["git", "apply", result["deliverable"]["patch_path"]], cwd=clone, check=True)
        self.assertEqual((clone / name).read_text(), "VALUE = 2\n")
        self.assertTrue((clone / "néw dir" / "ünïcode ✓.txt").exists())

    def test_model_deletes_git_directory(self):
        repo = make_repo(self.tmp / "repo", CALC)
        head = git(repo, "rev-parse", "HEAD").strip()
        wreck = turn(tc("bash", command="rm -rf .git"))
        result, _ = run_agent(repo, [FIX, wreck, RUN, SUBMIT], self.tmp / "run")
        self.assertTrue(result["deliverable"]["reconstruction_verified"], result.get("error"))
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())
        self.assertEqual(git(repo, "rev-parse", "HEAD").strip(), head)  # the repository's own history is put back
        self.assertEqual(git(repo, "status", "--porcelain").strip(), "M calc/ops.py")
        self.assertTrue(any(".git" in n for n in result["notes"]), result["notes"])

    def test_model_reinitialises_git(self):
        repo = make_repo(self.tmp / "repo", CALC)
        head = git(repo, "rev-parse", "HEAD").strip()
        wreck = turn(tc("bash", command="rm -rf .git && git init -q && git add -A && git commit -qm mine"))
        result, agent = run_agent(repo, [FIX, wreck, RUN, SUBMIT], self.tmp / "run")
        self.assertEqual(git(repo, "rev-parse", "HEAD").strip(), head)
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())
        self.assertTrue(any("put the original back" in m["content"] for m in agent.transcript if m["role"] == "tool"))

    def test_model_stashes_its_work_away(self):
        repo = make_repo(self.tmp / "repo", CALC)
        stash = turn(tc("bash", command="git stash -q"))
        result, agent = run_agent(repo, [FIX, RUN, stash, SUBMIT, SUBMIT], self.tmp / "run")
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())
        self.assertIn("git stash pop", "\n".join(str(m.get("content")) for m in agent.transcript))
        self.assertEqual(git(repo, "stash", "list").strip(), "")  # the entry it created is gone
        self.assertIn("return a / b", (repo / "calc/ops.py").read_text())

    def test_uncommitted_changes_at_start_are_kept_and_not_in_the_patch(self):
        repo = make_repo(self.tmp / "repo", CALC)
        (repo / "calc" / "__init__.py").write_text("# the user's own work in progress\n")
        (repo / "notes.txt").write_text("untracked, the user's\n")
        result, _ = run_agent(repo, [FIX, RUN, SUBMIT], self.tmp / "run")
        patch = Path(result["deliverable"]["patch_path"]).read_text()
        self.assertIn("return a / b", patch)
        self.assertNotIn("work in progress", patch)
        self.assertNotIn("notes.txt", patch)
        self.assertEqual((repo / "calc" / "__init__.py").read_text(), "# the user's own work in progress\n")
        self.assertTrue((repo / "notes.txt").exists())

    def test_file_system_refusals_become_tool_errors(self):
        repo = make_repo(self.tmp / "repo", CALC)
        bad = [turn(tc("write_file", path="calc/ops.py/child.py", content="x")),  # a path component is a file
               turn(tc("write_file", path="n" * 300 + ".py", content="x")),       # name too long
               turn(tc("write_file", path="s.py", content="\ud800 lone surrogate"))]
        result, agent = run_agent(repo, bad + [FIX, RUN, SUBMIT], self.tmp / "run")
        errors = [m["content"] for m in agent.transcript if m["role"] == "tool"][:3]
        self.assertTrue(all(e.startswith("Error:") for e in errors), errors)
        self.assertTrue(result["submission_ready"])
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())

    def test_large_repository_setup_is_bounded(self):
        files = dict(CALC)
        for i in range(6000):
            files[f"pkg{i % 60}/mod_{i}.py"] = f"def f_{i}(x):\n    return x + {i}\n"
        repo = make_repo(self.tmp / "repo", files)
        t0 = time.monotonic()
        result, agent = run_agent(repo, [FIX, RUN, SUBMIT], self.tmp / "run")
        self.assertLess(result["timing"]["setup_s"], 25, result["timing"])
        self.assertTrue(result["submission_ready"])
        self.assertLess(time.monotonic() - t0, 90)


class ModelBehaviourAttackTest(TempDirCase):
    def test_tool_call_flood_in_one_reply(self):
        repo = make_repo(self.tmp / "repo", CALC)
        flood = turn(*[tc("bash", command=f"echo {i}") for i in range(40)])
        result, agent = run_agent(repo, [flood, FIX, RUN, SUBMIT], self.tmp / "run")
        tool_msgs = [m for m in agent.transcript if m["role"] == "tool"]
        self.assertEqual(sum("Not executed: at most" in m["content"] for m in tool_msgs), 40 - 12)
        self.assertTrue(result["submission_ready"])
        self.assertIn("return a / b", Path(result["deliverable"]["patch_path"]).read_text())

    def test_terminal_noise_is_cleaned_for_the_model(self):
        noisy = "\x1b[31mFAIL\x1b[0m t\r\n10%\r55%\r100%\nnul\x00byte\x1b]0;title\x07!"
        self.assertEqual(clean_terminal_text(noisy), "FAIL t\n100%\nnulbyte!")
        repo = make_repo(self.tmp / "repo", CALC)
        cmd = turn(tc("bash", command="printf '\\033[1;31mred\\033[0m\\r\\n1%%\\r99%%\\n\\000z'"))
        _, agent = run_agent(repo, [cmd, FIX, RUN, SUBMIT], self.tmp / "run")
        out = next(m["content"] for m in agent.transcript if m["role"] == "tool")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x00", out)
        self.assertIn("red", out)
        self.assertIn("99%", out)
        self.assertNotIn("1%\r", out)

    def test_model_that_never_uses_tools_ends_cleanly(self):
        repo = make_repo(self.tmp / "repo", CALC)
        chatter = [turn(text="I think the answer is to use true division.")] * 10
        result, _ = run_agent(repo, chatter, self.tmp / "run")
        self.assertEqual(result["termination"], "repeated_format_errors")
        self.assertTrue(result["deliverable"]["reconstruction_verified"])
        self.assertEqual(git(repo, "status", "--porcelain").strip(), "")

    def test_huge_single_argument(self):
        repo = make_repo(self.tmp / "repo", CALC)
        big = turn(tc("write_file", path="big.txt", content="x" * 3_000_000))
        rm = turn(tc("bash", command="rm big.txt"))
        result, _ = run_agent(repo, [big, rm, FIX, RUN, SUBMIT], self.tmp / "run")
        self.assertTrue(result["submission_ready"])
        self.assertNotIn("big.txt", Path(result["deliverable"]["patch_path"]).read_text())


class ByproductTest(TempDirCase):
    def test_lock_files_follow_their_manifest(self):
        from gheerefill.agent import lockfile_byproducts
        files = [{"path": "Cargo.lock", "status": "A"}, {"path": "src/lib.rs", "status": "M"},
                 {"path": "web/package-lock.json", "status": "M"}, {"path": "api/package-lock.json", "status": "M"},
                 {"path": "api/package.json", "status": "M"}, {"path": "go.sum", "status": "D"}]
        self.assertEqual(lockfile_byproducts(files), ["Cargo.lock", "web/package-lock.json"])

    def test_npm_install_lock_file_is_not_delivered(self):
        repo = make_repo(self.tmp / "repo", {**CALC, "package.json": '{"name": "x"}\n'})
        install = turn(tc("bash", command="printf '{\"lockfileVersion\": 3}\\n' > package-lock.json"))
        result, _ = run_agent(repo, [install, FIX, RUN, SUBMIT], self.tmp / "run")
        self.assertEqual([f["path"] for f in result["deliverable"]["files"]], ["calc/ops.py"])
        self.assertTrue(result["deliverable"]["reconstruction_verified"])


class HugeIssueTest(TempDirCase):
    def test_megabyte_issue_is_truncated_in_the_prompt_and_kept_whole(self):
        from gheerefill.agent import ISSUE_PROMPT_CHARS
        repo = make_repo(self.tmp / "repo", CALC)
        log = "".join(f"2026-09-26 12:00:{i % 60:02d} ERROR worker {i}: ZeroDivisionError\n" for i in range(20000))
        issue = "divide(7, 2) returns 3; it should be 3.5.\n\n```\n" + log + "```\nEND-OF-ISSUE"
        result, agent = run_agent(repo, [FIX, RUN, SUBMIT], self.tmp / "run", issue=issue)
        task_msg = agent.transcript[1]["content"]
        self.assertLess(len(task_msg), ISSUE_PROMPT_CHARS + 20000)
        self.assertIn("divide(7, 2) returns 3", task_msg)
        self.assertIn("END-OF-ISSUE", task_msg)  # the tail is kept too
        full = next(n for n in result["notes"] if "issue text truncated" in n).split("full text at ")[1]
        self.assertEqual(Path(full).read_text(), issue.strip())
        self.assertTrue(result["submission_ready"])
