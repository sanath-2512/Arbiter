"""End to end through the real HTTP transport against DeepSeek-like and Qwen-like endpoints.

The endpoint enforces the provider family's documented request rules and injects its documented
output quirks at seeded rates (scripts/provider_emulator.py); a scripted policy decides. The harness
must solve the task regardless, never trip a provider rule, and record which repairs it needed.
"""

import json
import os
import subprocess
import sys

from scripts.provider_emulator import ProviderEmulator
from tests.helpers import CALC, ROOT, TEST_CMD, TempDirCase, git, make_repo

PLAN = [
    ("read_file", {"path": "calc/ops.py"}),
    ("edit_file", {"path": "calc/ops.py", "old_str": "return a // b", "new_str": "return a / b"}),
    ("bash", {"command": TEST_CMD}),
]


def calc_policy(messages, tools):
    """The fix, one tool call per turn; after the plan, submit (again if asked to review)."""
    i = sum(1 for m in messages if m.get("role") == "assistant")
    if i < len(PLAN):
        name, args = PLAN[i]
        return {"content": f"step {i + 1}", "tool_calls": [{"name": name, "arguments": args}]}
    return {"content": "done", "tool_calls": [{"name": "submit", "arguments": {"summary": "true division"}}]}


class EmulatedProviderTest(TempDirCase):
    def run_harness(self, emu, repo, extra_env=None, profile_extra=""):
        prof = self.tmp / "p.toml"
        prof.write_text('[model]\nprovider = "openai_chat"\nname = "emulated"\nbase_url = "http://127.0.0.1:1/v1"\n'
                        + profile_extra + '\n[limits]\ntime_limit_s = 300\nmax_steps = 40\n')
        env = {**os.environ, "AI_API_KEY": "sk-emulated-0000", "AI_BASE_URL": emu.base_url,
               "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1", **(extra_env or {})}
        p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--profile", str(prof), "--no-discover",
                            "--out", str(self.tmp / "out")],
                           input=json.dumps({"task_id": "calc", "repo_path": str(repo),
                                             "issue": "divide(7, 2) returns 3; it should return 3.5."}) + "\n",
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        lines = [l for l in p.stdout.splitlines() if l.strip().startswith("{")]
        self.assertTrue(lines, p.stderr[-3000:])
        return json.loads(lines[-1]), p

    def assert_solved(self, rec, p, repo, emu):
        detail = f"{emu.stats()} quirks={rec.get('model_quirks')} term={rec.get('termination')}\n{p.stderr[-2500:]}"
        self.assertEqual(rec["termination"], "model_submitted", detail)
        self.assertEqual((repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n", detail)
        self.assertEqual(rec["verification"]["status"], "checks_passed", detail)
        self.assertTrue(rec["submission_ready"], detail)

    def test_deepseek_family_across_seeds(self):
        for seed in range(4):
            with self.subTest(seed=seed):
                repo = make_repo(self.tmp / f"ds{seed}", CALC)
                with ProviderEmulator(calc_policy, "deepseek", seed=seed, scale=2.5) as emu:
                    rec, p = self.run_harness(emu, repo)
                self.assert_solved(rec, p, repo, emu)
                # the harness never trips a DeepSeek request rule (reasoning passback, null content)
                self.assertEqual(emu.rejections, {}, emu.stats())
                if emu.injected.get("leak"):
                    self.assertTrue(any("deepseek-dsml" in k for k in rec["model_quirks"]), rec["model_quirks"])
                self.assertGreater(rec["usage"]["cache_read_tokens"], 0)  # DeepSeek cache-hit fields are read

    def test_qwen_family_across_seeds(self):
        for seed in range(4):
            with self.subTest(seed=seed):
                repo = make_repo(self.tmp / f"qw{seed}", CALC)
                with ProviderEmulator(calc_policy, "qwen", seed=seed, scale=2.5) as emu:
                    rec, p = self.run_harness(emu, repo)
                self.assert_solved(rec, p, repo, emu)
                self.assertEqual(emu.rejections, {}, emu.stats())
                self.assertTrue(rec["model_quirks"], emu.stats())

    def test_qwen_moderation_rejection_is_survived(self):
        files = dict(CALC)
        files["calc/ops.py"] = "# MODERATION-TRIGGER (text a provider's filter dislikes)\ndef divide(a, b):\n    return a // b\n"
        repo = make_repo(self.tmp / "mod", files)
        with ProviderEmulator(calc_policy, "qwen", seed=1, scale=0.0) as emu:
            rec, p = self.run_harness(emu, repo)
        self.assertEqual(rec["termination"], "model_submitted", p.stderr[-2500:])
        self.assertIn("return a / b", (repo / "calc/ops.py").read_text())
        self.assertGreaterEqual(emu.rejections.get("moderation", 0), 1)
        self.assertTrue(any("content filter" in k for k in rec["model_quirks"]), rec["model_quirks"])

    def test_stream_only_model_is_adapted_to(self):
        repo = make_repo(self.tmp / "st", CALC)
        with ProviderEmulator(calc_policy, "deepseek", seed=3, scale=1.0, overrides={"stream_only": True}) as emu:
            rec, p = self.run_harness(emu, repo)
        self.assert_solved(rec, p, repo, emu)
        self.assertEqual(emu.rejections.get("stream_only"), 1)  # once, then streaming for the rest of the run
        self.assertTrue(any("streaming" in n for n in rec["notes"]), rec["notes"])

    def test_passback_required_on_all_turns_is_learned(self):
        """A stricter deployment that wants reasoning on every earlier assistant turn: one rejection,
        then the harness sends it on all turns."""
        repo = make_repo(self.tmp / "pb", CALC)
        emu = ProviderEmulator(calc_policy, "deepseek", seed=5, scale=0.0)
        orig = emu.handle

        def strict(body):
            for m in body.get("messages") or []:
                if m.get("role") == "assistant" and not m.get("tool_calls") and not m.get("reasoning_content") \
                        and m.get("content") not in (None, "(no reply)"):
                    emu.rejections["strict"] = emu.rejections.get("strict", 0) + 1
                    return 400, {"error": {"message": "The `reasoning_content` in the thinking mode must be passed "
                                                      "back to the API.", "type": "invalid_request_error"}}
            return orig(body)

        emu.handle = strict
        with emu:
            rec, p = self.run_harness(emu, repo)
        self.assert_solved(rec, p, repo, emu)


class SmallContextWindowTest(TempDirCase):
    """A provider with a small input limit (its own token count): the harness keeps every request
    inside it while the model reads far more than fits, and still solves the task."""
    run_harness = EmulatedProviderTest.run_harness
    assert_solved = EmulatedProviderTest.assert_solved

    def test_reads_exceeding_the_window_many_times_over(self):
        files = dict(CALC)
        for i in range(6):
            files[f"calc/big{i}.py"] = "".join(f"def helper_{i}_{k}(x):\n    return x + {k}  # padding padding\n"
                                               for k in range(600))
        repo = make_repo(self.tmp / "big", files)
        plan = [("read_file", {"path": f"calc/big{i}.py"}) for i in range(6)] + PLAN

        def policy(messages, tools):
            i = sum(1 for m in messages if m.get("role") == "assistant")
            if i < len(plan):
                name, args = plan[i]
                return {"content": f"step {i + 1}", "tool_calls": [{"name": name, "arguments": args}]}
            return {"content": "done", "tool_calls": [{"name": "submit", "arguments": {"summary": "true division"}}]}

        with ProviderEmulator(policy, "qwen", seed=2, scale=0.0, overrides={"input_limit": 9000}) as emu:
            rec, p = self.run_harness(emu, repo, profile_extra="context_window = 9000\nmax_output_tokens = 1500\n")
        self.assert_solved(rec, p, repo, emu)
        self.assertLessEqual(emu.rejections.get("input_length", 0), 1, emu.stats())
        ctx = rec["context"]
        self.assertGreater(ctx["peak_estimate_tokens"], 0)
        self.assertLess(ctx["last_estimate_tokens"], 9000)

