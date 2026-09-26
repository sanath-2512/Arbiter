"""Solver loop and finalisation.

inspect -> hypothesise -> edit -> verify -> repair or finish, driven by the model, with the
controller responsible for mechanics only: validated tools, per-step candidate capture,
evidence bound to exact trees, budget/deadline enforcement, bounded retries, context
protection, a one-shot submit review, and a finalisation path that always runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from gheerefill import __version__
from gheerefill.budget import Budget
from gheerefill.config import Profile
from gheerefill.context import ContextManager, estimate_tokens
from gheerefill.evidence import (
    VerificationRecord,
    classify_output,
    is_check_command,
    normalize_command,
    select_candidate,
    verification_status,
)
from gheerefill.models.base import AttemptRecord, ErrorClass, ModelClient, ModelError, ToolCall, call_with_retry
from gheerefill.outputs import OutputArchive
from gheerefill import prompts
from gheerefill.records import Redactor, append_jsonl, atomic_write_bytes, atomic_write_json
from gheerefill.sandbox import Sandbox, confine_paths
from gheerefill.shell import read_output_file, run_shell, tool_environment
from gheerefill.task import Task
from gheerefill.tools import ToolBox, ToolResult
from gheerefill.workspace import Workspace, WorkspaceError

RESULT_SCHEMA = "gheerefill.result/v1"
HARNESS_ROOT = Path(__file__).resolve().parent.parent
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|testing|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|"
    r"(^|/)conftest\.py$|Test\.java$|_spec\.rb$"
)


class Cancelled(Exception):
    pass


def _short(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Agent:
    def __init__(
        self,
        task: Task,
        profile: Profile,
        client: ModelClient,
        run_dir: Path,
        *,
        env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.task = task
        self.profile = profile
        self.client = client
        self.run_dir = Path(run_dir)
        self.env = dict(os.environ if env is None else env)
        self.redactor = redactor or Redactor()
        self._log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.clock = clock
        self._sleep = sleep
        self.rng = random.Random(profile.retry.seed)
        self.budget = Budget(profile.limits, profile.model.pricing, clock)
        self.cancel_requested = False
        self.in_model_call = False
        self.termination: str | None = None
        self.error: dict[str, Any] | None = None
        self.notes: list[str] = []
        self.ws: Workspace | None = None
        self.base_tree: str | None = None
        self.last_tree: str | None = None
        self.history: list[str] = []
        self.candidate_meta: list[dict[str, Any]] = []
        self.records: list[VerificationRecord] = []
        self.transcript: list[dict[str, Any]] = []
        self.recent_actions: list[tuple[str, str]] = []
        self.repetition_warned: set[tuple[str, str]] = set()
        self.target_git_initial = None
        self.target_git_fp_initial = None
        self.sandbox: Sandbox | None = None
        self.base_ignored: set[str] = set()
        self.submit_summary = ""
        self.integrity: dict[str, list[dict[str, Any]]] = {"controller_state_access": [], "harness_repo_access": []}

    # ------------------------------------------------------------------ utils
    def log(self, msg: str) -> None:
        self._log(self.redactor.text(f"[{self.task.task_id}] {msg}"))

    def request_cancel(self) -> None:
        """Signal-handler entry point. Interrupts a blocking model call; tool subprocesses
        notice the flag within ~0.2 s and are killed."""
        self.cancel_requested = True
        if self.in_model_call:
            raise Cancelled()

    def _append(self, msg: dict[str, Any]) -> None:
        self.transcript.append(msg)
        append_jsonl(self.run_dir / "transcript.jsonl", self.redactor.obj(msg))

    def _capture_state(self, why: str = "") -> str:
        assert self.ws is not None
        tree = self.ws.snapshot()
        if tree != self.last_tree:
            self.last_tree = tree
            if tree not in self.history:
                self.history.append(tree)
                self.candidate_meta.append(
                    {"tree": tree, "step": self.budget.steps, "elapsed_s": round(self.budget.elapsed(), 2),
                     "why": why, "is_base": tree == self.base_tree}
                )
                atomic_write_json(self.run_dir / "candidates.json", self.candidate_meta)
        return tree

    def _checkpoint(self, phase: str) -> None:
        atomic_write_json(
            self.run_dir / "state.json",
            {
                "schema": "gheerefill.state/v1",
                "harness_version": __version__,
                "phase": phase,
                "task_id": self.task.task_id,
                "task_sha256": hashlib.sha256(json.dumps(self.task.to_dict(), sort_keys=True).encode()).hexdigest(),
                "repo": str(self.task.repo_path),
                "profile_id": self.profile.identity(),
                "base_tree": self.base_tree,
                "last_tree": self.last_tree,
                "history": self.history,
                "step": self.budget.steps,
                "termination": self.termination,
                "target_git_initial": self.target_git_initial.to_dict() if self.target_git_initial else None,
                "target_git_fingerprint": self.target_git_fp_initial,
                "base_ignored": sorted(self.base_ignored),
                "updated_at": time.time(),
            },
        )

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        t = self.clock()
        try:
            self._setup()
        except Exception as e:  # noqa: BLE001 - every failure must reach finalisation
            self.termination = "setup_failed"
            self.error = {"type": type(e).__name__, "message": self.redactor.text(str(e))[:2000]}
            self.log(f"setup failed: {e}")
        self.budget.add_phase("setup", self.clock() - t)
        if self.termination is None:
            t = self.clock()
            try:
                self._loop()
            except Cancelled:
                self.termination = "cancelled"
            except Exception as e:  # noqa: BLE001
                self.termination = "crash"
                self.error = {
                    "type": type(e).__name__,
                    "message": self.redactor.text(str(e))[:2000],
                    "traceback": self.redactor.text(traceback.format_exc())[-4000:],
                }
                self.log(f"crash: {type(e).__name__}: {e}")
            self.budget.add_phase("solve", self.clock() - t)
        t = self.clock()
        try:
            result = self._finalize()
        except Cancelled:
            self.cancel_requested = True
            result = self._finalize()
        self.budget.add_phase("finalize", self.clock() - t)
        result["timing"]["finalize_s"] = round(self.budget.phase_s.get("finalize", 0.0), 3)
        result["timing"]["total_s"] = round(self.budget.elapsed(), 3)
        result["usage"] = self.budget.summary()
        atomic_write_json(self.run_dir / "result.json", result)
        return result

    @classmethod
    def recover(cls, run_dir: Path, profile: Profile, task: Task, *, log: Callable[[str], None] | None = None,
                env: dict[str, str] | None = None) -> "Agent":
        """Rebuild controller state from checkpoint files so an interrupted run can be finalised
        without any model call. The caller must have verified task/profile identity."""
        from gheerefill.workspace import TargetGitState
        from gheerefill.records import read_jsonl

        state = json.loads((Path(run_dir) / "state.json").read_text())

        class _NoModel:
            provider = "none"
            model_name = profile.model.name or "none"

            def complete(self, *a, **k):  # pragma: no cover - never called
                raise ModelError(ErrorClass.MALFORMED_REQUEST, "recovery mode makes no model calls")

        agent = cls(task, profile, _NoModel(), Path(run_dir), env=env, log=log)
        agent.ws = Workspace(task.repo_path, Path(run_dir))
        agent.sandbox = Sandbox(profile.policy.sandbox if profile.policy.sandbox != "confine" else "key").probe()
        if agent.sandbox.active:
            agent.ws.wrap = agent.sandbox.wrap
        agent.ws.attach(state["base_tree"])
        agent.base_tree = state["base_tree"]
        agent.history = list(state.get("history") or [state["base_tree"]])
        agent.last_tree = state.get("last_tree")
        cand = Path(run_dir) / "candidates.json"
        agent.candidate_meta = json.loads(cand.read_text()) if cand.exists() else []
        agent.records = [VerificationRecord(**d) for d in read_jsonl(Path(run_dir) / "evidence.jsonl")]
        tg = state.get("target_git_initial")
        agent.target_git_initial = TargetGitState(**tg) if tg else None
        agent.target_git_fp_initial = state.get("target_git_fingerprint")
        agent.base_ignored = set(state.get("base_ignored") or [])
        agent.budget.steps = int(state.get("step") or 0)
        for d in read_jsonl(Path(run_dir) / "requests.jsonl"):
            fields = {k: d.get(k) for k in AttemptRecord.__dataclass_fields__}
            agent.budget.record_attempt(AttemptRecord(**fields), role=d.get("role", "solver"))
        if (Path(run_dir) / "inflight.json").exists():
            agent.budget.requests += 1
            agent.budget.requests_usage_unknown += 1
            agent.notes.append("a model request was in flight when the run was interrupted; its usage is unknown")
        agent.termination = "recovered_after_interruption"
        agent.notes.append(f"finalised offline from checkpoint (interrupted in phase {state.get('phase')!r}, "
                           f"previous termination {state.get('termination')!r})")
        return agent

    def _setup(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.run_dir / "task.json", self.task.to_dict())
        atomic_write_json(self.run_dir / "profile.json", self.redactor.obj(self.profile.to_dict()))
        self.scratch = self.run_dir / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.archive = OutputArchive(self.run_dir / "outputs", self.redactor)
        self.ws = Workspace(self.task.repo_path, self.run_dir)
        mode = self.profile.policy.sandbox
        self.sandbox = Sandbox(mode, confine_paths(self.ws.repo, self.scratch.resolve()) if mode == "confine" else []).probe()
        if self.sandbox.active:
            self.ws.wrap = self.sandbox.wrap
        self.log("sandbox: " + ("active — " + self.sandbox.status["verified"] if self.sandbox.active
                                else f"inactive ({self.sandbox.status.get('reason')})"))
        self.target_git_fp_initial = self.ws.target_git_fingerprint()
        self.target_git_initial = self.ws.target_git_state()
        self.base_tree = self.ws.init()
        self.last_tree = self.base_tree
        self.history = [self.base_tree]
        self.candidate_meta = [{"tree": self.base_tree, "step": 0, "elapsed_s": 0.0, "why": "base", "is_base": True}]
        atomic_write_json(self.run_dir / "candidates.json", self.candidate_meta)
        self.base_ignored = self.ws.ignored_paths()
        tool_env = tool_environment(self.env, self.scratch, (self.profile.model.api_key_env,))
        self.tools = ToolBox(
            self.task.repo_path, self.scratch, self.archive, self.profile.tools, tool_env,
            time_budget=self.budget.work_remaining, should_cancel=lambda: self.cancel_requested,
            wrap=self.sandbox.wrap if self.sandbox.active else None,
        )
        self.specs = list(self.tools.specs.values())
        overview = prompts.repo_overview(self.tools.repo) if self.profile.policy.repo_overview else ""
        issue = self.task.issue.strip()
        issue_cap = int((self.profile.model.context_window - self.profile.model.max_output_tokens) * 0.4 * 3.2)
        if len(issue) > issue_cap:
            full = self.tools.scratch / "ISSUE_FULL.md"
            full.write_text(issue, encoding="utf-8")
            issue = (issue[: issue_cap // 2] + f"\n\n[... issue truncated to fit the context window; the complete text "
                     f"({len(self.task.issue)} chars) is in {full} ...]\n\n" + issue[-issue_cap // 4:])
            self.notes.append(f"issue text truncated in the prompt ({len(self.task.issue)} chars); full text at {full}")
        self._append({"role": "system", "content": prompts.SYSTEM.format(repo=self.tools.repo, scratch=self.tools.scratch)})
        self._append({"role": "user", "content": prompts.TASK.format(issue=issue, overview=overview)})
        spec_tokens = estimate_tokens([{"content": json.dumps([s.parameters for s in self.specs])}])
        self.ctx = ContextManager(
            self.profile.model.context_window, self.profile.model.max_output_tokens,
            self.profile.policy.context_reduce_at, self.profile.policy.keep_recent_messages,
            fixed_overhead_tokens=spec_tokens + 200,
        )
        self._checkpoint("solving")
        self.log(f"base tree {self.base_tree[:12]} · repo {self.task.repo_path} · model {self.client.model_name} "
                 f"({self.client.provider}) · limits {self.profile.limits.time_limit_s:.0f}s/{self.profile.limits.max_steps} steps")

    def _on_attempt(self, rec: AttemptRecord) -> None:
        self.budget.record_attempt(rec)
        append_jsonl(self.run_dir / "requests.jsonl", self.redactor.obj({**rec.__dict__, "step": self.budget.steps + 1}))
        if rec.outcome != "ok":
            self.log(f"model request failed ({rec.outcome}, attempt {rec.attempt}): {_short(rec.message, 160)}")

    def _loop(self) -> None:
        pol, lim = self.profile.policy, self.profile.limits
        format_errors = 0
        submit_reviews = 0
        overflow_retries = 0
        notice_sent = False
        while True:
            if self.cancel_requested:
                raise Cancelled()
            reason = self.budget.stop_reason()
            if reason:
                self.termination = reason
                return
            steps_left = lim.max_steps - self.budget.steps
            secs_left = self.budget.work_remaining()
            if pol.budget_notices and not notice_sent and (
                steps_left <= max(3, int(lim.max_steps * 0.1)) or secs_left <= max(60.0, lim.time_limit_s * 0.1)
            ):
                notice_sent = True
                self._append({"role": "user", "content": prompts.budget_notice(steps_left, secs_left)})
            view = self.ctx.prepare(self.transcript)
            inflight = self.run_dir / "inflight.json"
            try:
                atomic_write_json(inflight, {"step": self.budget.steps + 1, "started_at": time.time()})
                self.in_model_call = True
                if self.cancel_requested:
                    raise Cancelled()
                t_call = time.monotonic()
                try:
                    turn = call_with_retry(
                        self.client, view, self.specs,
                        max_attempts=self.profile.retry.max_attempts,
                        base_delay_s=self.profile.retry.base_delay_s,
                        max_delay_s=self.profile.retry.max_delay_s,
                        time_left=self.budget.work_remaining,
                        request_timeout_s=self.profile.model.request_timeout_s,
                        on_attempt=self._on_attempt,
                        rng=self.rng,
                        sleep=self._sleep,
                    )
                except Cancelled:
                    self.in_model_call = False
                    self._on_attempt(AttemptRecord(0, time.time(), time.monotonic() - t_call, ErrorClass.CANCELLED.value,
                                                   None, None, True, "request interrupted by cancellation"))
                    raise
            except ModelError as e:
                if e.cls == ErrorClass.CONTEXT_OVERFLOW and overflow_retries < 3:
                    overflow_retries += 1
                    self.ctx.force_reduce(self.ctx.estimate(view))
                    self.log(f"context overflow reported by provider; reducing context (level {self.ctx.pressure})")
                    continue
                self.termination = "deadline_reached" if e.cls == ErrorClass.DEADLINE else f"model_error:{e.cls.value}"
                self.error = {"class": e.cls.value, "message": self.redactor.text(e.message)[:1000], "status": e.status}
                return
            finally:
                self.in_model_call = False
                try:
                    inflight.unlink()
                except FileNotFoundError:
                    pass
            overflow_retries = 0
            self.budget.steps += 1
            self.ctx.observe(view, turn.usage)
            for n in turn.notes:
                if n not in self.notes:
                    self.notes.append(n)
            self._append(turn.to_message())
            if turn.text.strip():
                self.log(f"step {self.budget.steps} · {_short(turn.text, 120)}")
            if not turn.tool_calls:
                format_errors += 1
                self._append({"role": "user", "content": prompts.CUT_OFF if turn.finish_reason in ("length", "max_tokens")
                              else prompts.NO_TOOL_CALL})
                if format_errors >= lim.max_consecutive_format_errors:
                    self.termination = "repeated_format_errors"
                    return
                continue
            submitted, any_valid = False, False
            for call in turn.tool_calls:
                if submitted:
                    self._tool_message(call, "Not executed: submit was already called earlier in this reply.", {})
                    continue
                if call.name == "submit":  # all submit arguments are optional; malformed ones are ignored
                    any_valid = True
                    review = self._submit_gate(submit_reviews)
                    if review is None:
                        submitted = True
                        self.submit_summary = str((call.arguments or {}).get("summary", ""))[:2000]
                        self._tool_message(call, "Submission accepted.", {"tool": "submit"})
                    else:
                        submit_reviews += 1
                        self._tool_message(call, review, {"tool": "submit"})
                    continue
                res = self._run_tool(call)
                if res.meta.get("error") not in ("argument_parse", "argument_validation", "unknown_tool"):
                    any_valid = True
            format_errors = 0 if any_valid else format_errors + 1
            self._capture_state(f"after step {self.budget.steps}")
            self._checkpoint("solving")
            if submitted:
                self.termination = "model_submitted"
                return
            if format_errors >= lim.max_consecutive_format_errors:
                self.termination = "repeated_format_errors"
                return

    def _tool_message(self, call: ToolCall, content: str, meta: dict[str, Any]) -> None:
        self._append({
            "role": "tool", "tool_call_id": call.id, "name": call.name, "content": content,
            "output_id": meta.get("output_id"), "source_output": meta.get("source_output"),
        })

    def _run_tool(self, call: ToolCall) -> ToolResult:
        cmd = (call.arguments or {}).get("command") if call.name == "bash" else None
        is_check = isinstance(cmd, str) and call.parse_error is None and is_check_command(cmd)
        pre_tree = self._capture_state(f"before check at step {self.budget.steps}") if is_check else None
        self._audit_access(call)
        res = self.tools.execute(call)
        self.budget.record_tool(call.name, float(res.meta.get("duration_s", 0.0)))
        self._tool_message(call, res.content, res.meta)
        append_jsonl(self.run_dir / "actions.jsonl", self.redactor.obj({
            "step": self.budget.steps, "tool": call.name, "arguments": call.raw_arguments[:4000],
            "status": res.status, "meta": res.meta,
        }))
        label = _short(cmd, 70) if cmd else _short(call.raw_arguments, 70)
        self.log(f"step {self.budget.steps} · {call.name} {label} → {res.status}"
                 + (f" exit={res.meta.get('exit_code')}" if call.name == "bash" else ""))
        if is_check and res.status in ("ok", "timeout") and pre_tree is not None:
            post_tree = self._capture_state(f"after check at step {self.budget.steps}")
            text = self.archive.read_text(res.meta["output_id"]) or ""
            oc = classify_output(text, res.meta.get("exit_code"), timed_out=bool(res.meta.get("timed_out")),
                                 piped="|" in cmd)
            rec = VerificationRecord(
                id=f"v{len(self.records) + 1}", step=self.budget.steps, tree=pre_tree,
                binding="exact" if pre_tree == post_tree else "mutated", command=cmd,
                check_key=normalize_command(cmd), cwd=str(self.tools.repo), exit_code=res.meta.get("exit_code"),
                timed_out=bool(res.meta.get("timed_out")), outcome=oc.outcome, runner=oc.runner, counts=oc.counts,
                detail=oc.detail, output_id=res.meta.get("output_id"), source="agent",
                duration_s=float(res.meta.get("duration_s", 0.0)),
            )
            self.records.append(rec)
            append_jsonl(self.run_dir / "evidence.jsonl", rec.to_dict())
        if cmd and self.profile.policy.repetition_notice:
            key = (normalize_command(cmd), hashlib.sha1(res.content.encode()).hexdigest())
            self.recent_actions = (self.recent_actions + [key])[-8:]
            n = self.recent_actions.count(key)
            if n >= 3 and key not in self.repetition_warned:
                self.repetition_warned.add(key)
                self._append({"role": "user", "content": prompts.REPETITION.format(n=n)})
        return res

    def _git_tamper(self) -> dict[str, Any] | None:
        """Compare the target's .git config/hooks with their state at the start of the run."""
        if self.ws is None or self.target_git_fp_initial is None:
            return None
        now = self.ws.target_git_fingerprint()
        if now == self.target_git_fp_initial:
            return None
        return {"before": self.target_git_fp_initial, "after": now}

    def _audit_access(self, call: ToolCall) -> None:
        """Record (not block) references to controller-owned state or the harness repository.
        Observations for audit only: without OS isolation these cannot be enforced."""
        args = call.arguments or {}
        text = " ".join(str(v) for v in args.values() if isinstance(v, str))
        if not text:
            return
        run_dir, scratch = str(self.run_dir.resolve()), str(self.tools.scratch)
        paths = [text]
        if call.name in ("read_file", "search", "write_file", "edit_file") and isinstance(args.get("path"), str):
            p = Path(args["path"])
            paths.append(str((p if p.is_absolute() else self.tools.repo / p).resolve()))
        for t in paths:
            if run_dir in t.replace(scratch, ""):
                self.integrity["controller_state_access"].append({"step": self.budget.steps, "tool": call.name,
                                                                  "detail": _short(text, 200)})
                break
        harness, repo = str(HARNESS_ROOT), str(self.tools.repo)
        # Work repos and run dirs may live inside the harness checkout (eval runs): strip them first.
        if any(harness in t.replace(repo, "").replace(run_dir, "") for t in paths):
            self.integrity["harness_repo_access"].append({"step": self.budget.steps, "tool": call.name,
                                                         "detail": _short(text, 200)})

    def _submit_gate(self, reviews_done: int) -> str | None:
        if not self.profile.policy.submit_review or reviews_done >= 1:
            return None
        assert self.ws is not None and self.base_tree is not None
        tree = self._capture_state("at submit")
        if tree == self.base_tree:
            return prompts.SUBMIT_EMPTY
        files = self.ws.changed_files(self.base_tree, tree)
        new_files = [f["path"] for f in files if f["status"] == "A"]
        unverified = not any(r.tree == tree and r.binding == "exact" for r in self.records)
        if not new_files and not unverified:
            return None
        changed = [f"{f['status']} {f['path']}" for f in files]
        return prompts.submit_review(changed, new_files, unverified)

    # ------------------------------------------------------------------ finalisation
    def _final_recheck(self, selected: str) -> list[str]:
        notes: list[str] = []
        agent_checks = [r for r in self.records if r.source == "agent" and r.outcome not in ("inconclusive",)]
        if not agent_checks or any(r.tree == selected and r.binding == "exact" for r in self.records):
            return notes
        last = agent_checks[-1]
        need = last.duration_s * 1.5 + 10.0
        available = self.budget.remaining() - 15.0
        if available < need or self.cancel_requested:
            notes.append(f"final re-check skipped: needs ~{need:.0f}s, {max(0.0, available):.0f}s available")
            return notes
        oid, out_path = self.archive.allocate()
        r = run_shell(last.command, cwd=self.tools.repo, env=self.tools.env,
                      timeout_s=min(self.profile.tools.bash_timeout_s, available), output_path=out_path,
                      max_output_bytes=self.profile.tools.max_output_bytes, should_cancel=lambda: self.cancel_requested,
                      wrap=self.tools.wrap)
        self.archive.redact_file(out_path)
        text = read_output_file(out_path)
        post = self.ws.snapshot()
        oc = classify_output(text, r.exit_code, timed_out=r.timed_out, piped="|" in last.command)
        rec = VerificationRecord(
            id=f"v{len(self.records) + 1}", step=self.budget.steps, tree=selected,
            binding="exact" if post == selected else "mutated", command=last.command, check_key=last.check_key,
            cwd=last.cwd, exit_code=r.exit_code, timed_out=r.timed_out, outcome=oc.outcome, runner=oc.runner,
            counts=oc.counts, detail=oc.detail, output_id=oid, source="harness_recheck", duration_s=r.duration_s,
        )
        self.records.append(rec)
        append_jsonl(self.run_dir / "evidence.jsonl", rec.to_dict())
        notes.append(f"re-ran the agent's last check on the selected candidate: {oc.outcome} ({_short(last.command, 60)})")
        if post != selected:
            self.ws.restore(selected)
            notes.append("the re-check modified tracked files; restored the selected candidate afterwards")
        return notes

    def _finalize(self) -> dict[str, Any]:
        fin_notes: list[str] = []
        result: dict[str, Any] = self._result_skeleton()
        if self.ws is None or self.base_tree is None:
            result["status"] = "infrastructure_error"
            return result
        pol = self.profile.policy
        try:
            final_tree = self._capture_state("final")
            if pol.git_hygiene:
                try:
                    fin_notes += self.ws.restore_target_git(self.target_git_initial)
                except WorkspaceError as e:
                    fin_notes.append(f"target git hygiene failed: {e}")
            selected, reason = select_candidate(
                final_tree, self.base_tree, self.history, self.records,
                dominance=pol.dominance_selection,
                recover_empty_final=pol.recover_empty_final and self.termination != "model_submitted",
            )
            if selected != final_tree:
                self.ws.restore(selected)
                fin_notes.append(f"restored archived candidate {selected[:12]} into the working tree")
            if pol.final_recheck and hasattr(self, "tools"):
                n_before = len(self.records)
                fin_notes += self._final_recheck(selected)
                if len(self.records) > n_before and pol.dominance_selection:
                    # New exact evidence on the selected tree: apply the same dominance rule once more.
                    again, why = select_candidate(selected, self.base_tree, self.history, self.records,
                                                  dominance=True, recover_empty_final=False)
                    if again != selected:
                        self.ws.restore(again)
                        fin_notes.append(f"re-check evidence changed the selection to {again[:12]}; restored it")
                        selected, reason = again, why
            patch = self.ws.patch(self.base_tree, selected)
            patch_path = self.run_dir / "patch.diff"
            atomic_write_bytes(patch_path, patch)
            recon_ok, recon_detail = self.ws.verify_reconstruction(self.base_tree, selected, patch_path)
            worktree_ok = self.ws.snapshot() == selected
            files = self.ws.changed_files(self.base_tree, selected)
            excluded = sorted(self.ws.ignored_paths() - self.base_ignored)
            vstatus, vdetail = verification_status(self.records, selected)
            step_of = {c["tree"]: c["step"] for c in self.candidate_meta}
            result.update({
                "status": "completed",
                "submission_ready": bool(recon_ok and worktree_ok),
                "deliverable": {
                    "mode": "working tree left at the selected candidate (in place) + patch file",
                    "patch_path": str(patch_path),
                    "patch_sha256": hashlib.sha256(patch).hexdigest(),
                    "patch_bytes": len(patch),
                    "empty": selected == self.base_tree,
                    "files": files,
                    "shortstat": self.ws.shortstat(self.base_tree, selected),
                    "reconstruction_verified": recon_ok,
                    "reconstruction_detail": recon_detail,
                    "worktree_matches_selected": worktree_ok,
                    "excluded_paths": excluded[:200],
                    "excluded_reason": "new paths matching .gitignore/info/exclude or harness cache excludes "
                                       "(caches, virtualenvs, node_modules); not part of the patch",
                },
                "selected_candidate": {"tree": selected, "reason": reason, "step": step_of.get(selected),
                                       "is_final_state": selected == final_tree},
                "candidates_observed": len([t for t in self.history if t != self.base_tree]),
                "integrity": {
                    "note": "observations for audit, not a verdict; the harness does not have OS-level isolation",
                    "modified_existing_test_files": [f["path"] for f in files if f["status"] in "MDR"
                                                     and TEST_PATH_RE.search(f.get("old_path") or f["path"])],
                    "added_test_files": [f["path"] for f in files if f["status"] == "A" and TEST_PATH_RE.search(f["path"])],
                    "target_git_control_files_changed": self._git_tamper(),
                    **{k: v[:20] for k, v in self.integrity.items()},
                },
                "verification": {
                    "status": vstatus,
                    "detail": vdetail,
                    "records_on_selected": [
                        {k: r.to_dict()[k] for k in ("id", "command", "outcome", "counts", "runner", "source", "binding")}
                        for r in self.records if r.tree == selected
                    ],
                    "total_records": len(self.records),
                },
            })
        except Exception as e:  # noqa: BLE001
            result["status"] = "infrastructure_error"
            result["error"] = result.get("error") or {}
            result["error"]["finalize"] = f"{type(e).__name__}: {self.redactor.text(str(e))[:1000]}"
            self.log(f"finalisation error: {e}")
        result["notes"] = self.notes + fin_notes
        self._checkpoint("finalized")
        self.log(
            f"done · termination={self.termination} · verification={result.get('verification', {}).get('status')} · "
            f"files={len(result.get('deliverable', {}).get('files', []))} · submission_ready={result['submission_ready']}"
        )
        return result

    def _result_skeleton(self) -> dict[str, Any]:
        m = self.profile.model
        return {
            "schema": RESULT_SCHEMA,
            "task_id": self.task.task_id,
            "run_dir": str(self.run_dir),
            "harness": {"name": "gheerefill", "version": __version__},
            "model": {
                "provider": m.provider, "name": self.client.model_name, "base_url": m.base_url,
                "tool_protocol": m.tool_protocol, "live": m.provider != "fake",
                "profile": self.profile.name, "profile_id": self.profile.identity(),
                "overrides": self.profile.overrides,
            },
            "status": "infrastructure_error",
            "isolation": self.sandbox.status if self.sandbox else None,
            "termination": self.termination,
            "submission_ready": False,
            "submit_summary": self.submit_summary,
            "error": self.error,
            "usage": self.budget.summary(),
            "timing": {
                "total_s": round(self.budget.elapsed(), 3),
                "setup_s": round(self.budget.phase_s.get("setup", 0.0), 3),
                "solve_s": round(self.budget.phase_s.get("solve", 0.0), 3),
                "model_s": round(self.budget.model_time_s, 3),
                "tool_s": round(self.budget.tool_time_s, 3),
            },
            "note": "Lifecycle outcome only. Semantic correctness is decided by external evaluation.",
        }
