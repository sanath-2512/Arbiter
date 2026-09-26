import json
import os

from gheerefill.models.textproto import TextProtocolClient
from gheerefill.models.fake import FakeClient
from gheerefill.agent import Agent
from gheerefill.records import read_jsonl
from gheerefill.task import Task
from tests.helpers import CALC, TEST_CMD, TempDirCase, git, make_repo, run_agent, tc, test_profile, turn

FIX = tc("edit_file", path="calc/ops.py", old_str="return a // b", new_str="return a / b")
BREAK = tc("edit_file", path="calc/ops.py", old_str="return a / b", new_str="return a * b")
TEST = tc("bash", command=TEST_CMD)
SUBMIT = tc("submit", summary="done")


class AgentTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "repo", CALC)
        self.run_dir = self.tmp / "run"

    def patch(self):
        return (self.run_dir / "patch.diff").read_text()

    def test_happy_path(self):
        result, agent = run_agent(self.repo, [turn(tc("read_file", path="calc/ops.py")), turn(TEST), turn(FIX), turn(TEST),
                                              turn(SUBMIT)], self.run_dir)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["termination"], "model_submitted")
        self.assertTrue(result["submission_ready"])
        self.assertTrue(result["deliverable"]["reconstruction_verified"])
        self.assertEqual(result["verification"]["status"], "checks_passed")
        self.assertIn("+    return a / b", self.patch())
        self.assertFalse(result["model"]["live"])
        self.assertNotIn("correct", json.dumps(result).lower().replace("correctness", ""))
        for f in ("transcript.jsonl", "evidence.jsonl", "candidates.json", "state.json", "result.json", "requests.jsonl"):
            self.assertTrue((self.run_dir / f).exists(), f)
        ev = read_jsonl(self.run_dir / "evidence.jsonl")
        self.assertEqual([e["outcome"] for e in ev], ["failed", "passed"])
        self.assertNotEqual(ev[0]["tree"], ev[1]["tree"])
        self.assertEqual(result["usage"]["requests"], 5)

    def test_degraded_later_attempt_restores_dominating_candidate(self):
        result, _ = run_agent(self.repo, [turn(FIX), turn(TEST), turn(BREAK), turn(TEST)], self.run_dir,
                              profile=test_profile(max_steps=4))
        self.assertEqual(result["termination"], "step_limit")
        self.assertIn("dominates", result["selected_candidate"]["reason"])
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")
        self.assertIn("+    return a / b", self.patch())
        self.assertEqual(result["verification"]["status"], "checks_passed")
        self.assertTrue(result["deliverable"]["worktree_matches_selected"])

    def test_deadline_still_finalises(self):
        now = [0.0]

        def slow(messages):
            now[0] += 50.0
            return turn(FIX) if len(messages) < 4 else turn(TEST)

        profile = test_profile(time_limit_s=120, finalize_reserve_s=20)
        profile.policy.final_recheck = False
        result, _ = run_agent(self.repo, [slow] * 10, self.run_dir, profile=profile, clock=lambda: now[0])
        self.assertEqual(result["termination"], "deadline_reached")
        self.assertTrue(result["submission_ready"])
        self.assertIn("+    return a / b", self.patch())

    def test_context_overflow_reduces_and_continues(self):
        turns = [turn(tc("bash", command="seq 1 5000")), {"error": "context_overflow", "message": "too long"},
                 turn(FIX), turn(TEST), turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir)
        self.assertEqual(result["termination"], "model_submitted")
        self.assertGreaterEqual(agent.ctx.pressure, 1)

    def test_overstated_context_window_is_learned_from_overflow(self):
        profile = test_profile()
        profile.model.context_window = 1_000_000  # misconfigured: far larger than the "real" window

        def gate(messages):
            chars = sum(len(m.get("content") or "") for m in messages)
            if chars > 12000:
                return {"error": "context_overflow", "message": "maximum context length exceeded"}
            return turn(tc("bash", command="seq 1 3000")) if len(messages) < 12 else turn(SUBMIT)

        result, agent = run_agent(self.repo, [gate] * 14 + [turn(SUBMIT)], self.run_dir, profile=profile)
        self.assertNotEqual(result["termination"], "model_error:context_overflow")
        self.assertLess(agent.ctx.limit, 1_000_000)

    def test_permanent_api_failure_finalises(self):
        result, _ = run_agent(self.repo, [turn(FIX), {"error": "authentication", "message": "bad key"}], self.run_dir)
        self.assertEqual(result["termination"], "model_error:authentication")
        self.assertEqual(result["error"]["class"], "authentication")
        self.assertTrue(result["submission_ready"])
        self.assertIn("+    return a / b", self.patch())  # the partial fix is preserved, not discarded

    def test_repeated_format_errors_and_malformed_arguments(self):
        bad = {"text": "", "tool_calls": [{"name": "bash", "raw_arguments": "{\"command\": "}]}
        result, agent = run_agent(self.repo, [{"text": "I think..."}, bad, {"text": "hmm"}, bad], self.run_dir)
        self.assertEqual(result["termination"], "repeated_format_errors")
        tool_msgs = [m for m in agent.transcript if m["role"] == "tool"]
        self.assertTrue(all("Nothing was executed" in m["content"] for m in tool_msgs))

    def test_submit_review_flags_scratch_files(self):
        turns = [turn(FIX), turn(tc("write_file", path="repro.py", content="print(1)\n")), turn(TEST), turn(SUBMIT),
                 turn(tc("bash", command="rm repro.py")), turn(SUBMIT)]
        result, agent = run_agent(self.repo, turns, self.run_dir)
        review = [m["content"] for m in agent.transcript if m["role"] == "tool" and m["name"] == "submit"][0]
        self.assertIn("repro.py", review)
        self.assertNotIn("repro.py", self.patch())
        self.assertEqual(result["termination"], "model_submitted")

    def test_submit_with_malformed_arguments_is_still_a_submit(self):
        bad_submit = {"text": "", "tool_calls": [{"name": "submit", "raw_arguments": "{summary: oops"}]}
        result, agent = run_agent(self.repo, [turn(FIX), turn(TEST), bad_submit], self.run_dir)
        self.assertEqual(result["termination"], "model_submitted")

    def test_empty_submission_warned_then_respected(self):
        result, agent = run_agent(self.repo, [turn(SUBMIT), turn(SUBMIT)], self.run_dir)
        self.assertIn("no changes", [m for m in agent.transcript if m["role"] == "tool"][0]["content"])
        self.assertTrue(result["deliverable"]["empty"])

    def test_timeout_after_partial_mutation_is_captured(self):
        turns = [turn(tc("bash", command="printf 'X = 1\\n' >> calc/ops.py; sleep 30", timeout=1))]
        result, agent = run_agent(self.repo, turns, self.run_dir, profile=test_profile(max_steps=1))
        self.assertEqual(len(agent.history), 2)
        self.assertIn("+X = 1", self.patch())
        self.assertIn("TIMED OUT", [m for m in agent.transcript if m["role"] == "tool"][0]["content"])

    def test_stale_verification_triggers_final_recheck(self):
        # tests pass on the fix, then another edit changes the tree: that evidence is now stale
        turns = [turn(FIX), turn(TEST), turn(tc("write_file", path="calc/extra.py", content="X = 1\n")),
                 turn(SUBMIT), turn(SUBMIT)]
        result, _ = run_agent(self.repo, turns, self.run_dir)
        recs = result["verification"]["records_on_selected"]
        self.assertEqual([r["source"] for r in recs], ["harness_recheck"])
        self.assertEqual(result["verification"]["status"], "checks_passed")

    def test_recheck_evidence_can_restore_earlier_verified_candidate(self):
        # verified fix, then an untested breaking edit, then submit (review), then submit again
        turns = [turn(FIX), turn(TEST), turn(BREAK), turn(SUBMIT), turn(SUBMIT)]
        result, _ = run_agent(self.repo, turns, self.run_dir)
        self.assertEqual(result["termination"], "model_submitted")
        self.assertTrue(any("changed the selection" in n for n in result["notes"]), result["notes"])
        self.assertEqual((self.repo / "calc/ops.py").read_text(), "def divide(a, b):\n    return a / b\n")
        self.assertEqual(result["verification"]["status"], "checks_passed")
        self.assertIn("+    return a / b", self.patch())

    def test_cancellation_finalises(self):
        holder = {}

        def cancel(messages):
            holder["agent"].request_cancel()
            return turn(TEST)

        agent = Agent(Task("t", self.repo, "fix"), test_profile(), FakeClient([turn(FIX), cancel, turn(TEST)]),
                      self.run_dir, log=lambda m: None, sleep=lambda s: None)
        holder["agent"] = agent
        result = agent.run()
        self.assertEqual(result["termination"], "cancelled")
        self.assertEqual(result["usage"]["requests_usage_unknown"], 1)  # interrupted request is counted, not dropped
        self.assertTrue(result["submission_ready"])
        self.assertIn("+    return a / b", self.patch())

    def test_crash_in_loop_still_exports(self):
        agent = Agent(Task("t", self.repo, "fix"), test_profile(), FakeClient([turn(FIX), turn(TEST)]), self.run_dir,
                      log=lambda m: None)
        original = agent._run_tool
        calls = {"n": 0}

        def flaky(call):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated harness bug")
            return original(call)

        agent._run_tool = flaky
        result = agent.run()
        self.assertEqual(result["termination"], "crash")
        self.assertIn("simulated harness bug", result["error"]["message"])
        self.assertTrue(result["submission_ready"])

    def test_secret_never_reaches_records(self):
        secret = "sk-supersecret-9876543210"
        env = {**os.environ, "AI_API_KEY": secret}
        from gheerefill.records import Redactor

        result, _ = run_agent(self.repo, [turn(tc("bash", command="env; echo $AI_API_KEY; cat /proc/$PPID/environ | tr '\\0' '\\n'")),
                                          turn(SUBMIT), turn(SUBMIT)],
                              self.run_dir, env=env, redactor=Redactor([secret]))
        for p in self.run_dir.rglob("*"):
            if p.is_file() and "shadow.git" not in p.parts:
                self.assertNotIn(secret.encode(), p.read_bytes(), p)

    def test_agent_git_commit_is_undone_but_changes_kept(self):
        base_head = git(self.repo, "rev-parse", "HEAD").strip()
        turns = [turn(FIX), turn(tc("bash", command="git -c user.name=a -c user.email=a@a commit -qam fix && git log --oneline | head -1")),
                 turn(TEST), turn(SUBMIT)]
        result, _ = run_agent(self.repo, turns, self.run_dir)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), base_head)
        self.assertIn("calc/ops.py", git(self.repo, "diff", "--name-only"))
        self.assertTrue(any("committed" in n for n in result["notes"]))

    def test_text_protocol_end_to_end(self):
        text_turns = [
            {"text": "<function=edit_file>\n<parameter=path>\ncalc/ops.py\n</parameter>\n<parameter=old_str>\n"
                     "    return a // b\n</parameter>\n<parameter=new_str>\n    return a / b\n</parameter>\n</function>"},
            {"text": f"<function=bash>\n<parameter=command>\n{TEST_CMD}\n</parameter>\n</function>"},
            {"text": "<function=submit>\n<parameter=summary>\nfixed\n</parameter>\n</function>"},
        ]
        agent = Agent(Task("t", self.repo, "fix"), test_profile(), TextProtocolClient(FakeClient(text_turns)),
                      self.run_dir, log=lambda m: None)
        result = agent.run()
        self.assertEqual(result["termination"], "model_submitted")
        self.assertEqual(result["verification"]["status"], "checks_passed")

    def test_repetition_notice(self):
        turns = [turn(tc("bash", command="echo same"))] * 3 + [turn(SUBMIT), turn(SUBMIT)]
        _, agent = run_agent(self.repo, turns, self.run_dir)
        notes = [m for m in agent.transcript if m["role"] == "user" and "same command" in m["content"]]
        self.assertEqual(len(notes), 1)

    def test_oversized_issue_is_bounded_with_full_text_available(self):
        profile = test_profile()
        profile.model.context_window, profile.model.max_output_tokens = 4000, 1000
        issue = "START " + "x" * 20000 + " END"
        result, agent = run_agent(self.repo, [turn(SUBMIT), turn(SUBMIT)], self.run_dir, profile=profile, issue=issue)
        shown = agent.transcript[1]["content"]
        self.assertLess(len(shown), 6000)
        self.assertIn("START", shown)
        self.assertIn("END", shown)
        self.assertIn("ISSUE_FULL.md", shown)
        self.assertEqual((self.run_dir / "scratch" / "ISSUE_FULL.md").read_text(), issue)

    def test_setup_failure_reports_infrastructure_error(self):
        (self.run_dir / "shadow.git").mkdir(parents=True)  # pre-existing store: refuse to clobber
        result, _ = run_agent(self.repo, [turn(SUBMIT)], self.run_dir)
        self.assertEqual(result["termination"], "setup_failed")
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertFalse(result["submission_ready"])
