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
from gheerefill.models.base import (AttemptRecord, ErrorClass, ModelClient, ModelError, ToolCall, call_with_retry,
                                    output_token_limit)
from gheerefill.outputs import OutputArchive
from gheerefill import attest, locate, memory, prompts, proof, tasktype
from gheerefill.progress import FailureMemory
from gheerefill.records import Redactor, append_jsonl, atomic_write_bytes, atomic_write_json
from gheerefill.sandbox import Sandbox, confine_paths
from gheerefill.shell import read_output_file, run_shell, tool_environment
from gheerefill.task import Task
from gheerefill.tools import ToolBox, ToolResult
from gheerefill.workspace import Workspace, WorkspaceError

RESULT_SCHEMA = "gheerefill.result/v1"
HARNESS_ROOT = Path(__file__).resolve().parent.parent
TEST_PATH_RE = proof.TEST_PATH_RE
FATAL_TERMINATIONS = ("cancelled", "crash", "setup_failed", "deadline_reached", "token_budget", "cost_budget",
                      "step_limit", "repeated_format_errors")


class Cancelled(Exception):
    recorded_by_caller = True  # the loop records the interrupted request itself (call_with_retry must not)


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
        memory_root: Path | None = None,
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
        # Identity as configured at start: request parameters adapted mid-run (e.g. a lower output cap)
        # must not make the checkpoint look like a different profile to `finalize`.
        self.profile_id = profile.identity()
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
        # proof-carrying patches and adaptive attempts (proof.py)
        self.attempt = 1
        self.attempts: list[dict[str, Any]] = []
        self.attempt_trees: list[str] = []
        self.attempt_start: dict[str, float] = {"step": 0, "elapsed": 0.0}
        self.attempt_caps: tuple[float, int] | None = None  # (elapsed-seconds deadline, step cap)
        self.reproductions: list[dict[str, Any]] = []
        self.assessments: dict[str, proof.Assessment] = {}
        self.detour: str | None = None
        self.setup_commands: list[str] = []
        self.memory_root = memory_root if profile.policy.memory else None  # set by the CLI: <runs>/.memory
        self.task_type: tasktype.TaskType | None = None
        self.failure_memory = FailureMemory()
        self.escalate_attempt = False
        self.final_state_tree: str | None = None

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
        if tree not in self.attempt_trees:
            self.attempt_trees.append(tree)
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
                "profile_id": self.profile_id,
                "base_tree": self.base_tree,
                "last_tree": self.last_tree,
                "history": self.history,
                "step": self.budget.steps,
                "termination": self.termination,
                "target_git_initial": self.target_git_initial.to_dict() if self.target_git_initial else None,
                "target_git_fingerprint": self.target_git_fp_initial,
                "base_ignored": sorted(self.base_ignored),
                "attempt": self.attempt,
                "attempts": self.attempts,
                "attempt_trees": self.attempt_trees,
                "reproductions": self.reproductions,
                "detour": self.detour,
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
            while True:
                self._begin_attempt()
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
                try:
                    self._end_attempt()
                    if not self._next_attempt_worthwhile():
                        break
                    self._prepare_next_attempt()
                except Cancelled:
                    self.cancel_requested = True
                    break
                except Exception as e:  # noqa: BLE001 - attempts are an optimisation; never lose the result
                    self.notes.append(f"attempt bookkeeping failed ({type(e).__name__}: {e}); no further attempts")
                    self.log(f"attempt bookkeeping failed: {e}")
                    break
            self._restore_after_detour()
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
        if self.memory_root is not None and self.ws is not None:
            try:
                memory.update(self.memory_root, self.ws.repo, self.records, self.setup_commands, self.redactor.text)
            except Exception as e:  # noqa: BLE001 - notes are optional
                self.log(f"repository notes not saved: {e}")
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
        agent.attempt = int(state.get("attempt") or 1)
        agent.attempts = list(state.get("attempts") or [])
        agent.attempt_trees = list(state.get("attempt_trees") or [])
        agent.reproductions = list(state.get("reproductions") or [])
        if state.get("detour"):
            agent.notes.append("the run was interrupted while the harness ran a check on another state; the "
                               "selected candidate is restored at finalisation")
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
        self.task_type = tasktype.classify(self.task.issue)
        if self.profile.policy.task_type_hints and tasktype.hint(self.task_type):
            overview += "\n" + tasktype.hint(self.task_type)
        if self.memory_root is not None:
            overview += memory.render(memory.load(self.memory_root, self.ws.repo))
        if self.profile.policy.localize:
            try:
                listing = self.ws.git("ls-tree", "-r", "-z", "--name-only", self.base_tree).stdout
                loc = locate.localize(self.task.issue, self.tools.repo,
                                      [f for f in listing.decode("utf-8", "replace").split("\0") if f])
                atomic_write_json(self.run_dir / "localization.json", loc)
                overview += locate.render(loc)
            except Exception as e:  # noqa: BLE001 - hints are optional
                self.notes.append(f"localisation hints skipped: {type(e).__name__}: {e}")
        issue = self.task.issue.strip()
        issue_cap = int((self.profile.model.context_window - self.profile.model.max_output_tokens) * 0.4 * 3.2)
        if len(issue) > issue_cap:
            full = self.tools.scratch / "ISSUE_FULL.md"
            full.write_text(issue, encoding="utf-8")
            issue = (issue[: issue_cap // 2] + f"\n\n[... issue truncated to fit the context window; the complete text "
                     f"({len(self.task.issue)} chars) is in {full} ...]\n\n" + issue[-issue_cap // 4:])
            self.notes.append(f"issue text truncated in the prompt ({len(self.task.issue)} chars); full text at {full}")
        hint = prompts.REPRODUCE_HINT if "register_reproduction" in self.tools.specs else ""
        self._system_msg = prompts.SYSTEM.format(repo=self.tools.repo, scratch=self.tools.scratch, reproduce=hint)
        self._task_msg = prompts.TASK.format(issue=issue, overview=overview)
        self._append({"role": "system", "content": self._system_msg})
        self._append({"role": "user", "content": self._task_msg})
        self._new_context()
        self._checkpoint("solving")
        self.log(f"base tree {self.base_tree[:12]} · repo {self.task.repo_path} · model {self.client.model_name} "
                 f"({self.client.provider}) · limits {self.profile.limits.time_limit_s:.0f}s/{self.profile.limits.max_steps} steps")

    def _new_context(self) -> None:
        spec_tokens = estimate_tokens([{"content": json.dumps([s.parameters for s in self.specs])}])
        self.ctx = ContextManager(
            self.profile.model.context_window, self.profile.model.max_output_tokens,
            self.profile.policy.context_reduce_at, self.profile.policy.keep_recent_messages,
            fixed_overhead_tokens=spec_tokens + 200,
        )

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
        adaptations = 0
        notice_sent = check_nudged = False
        while True:
            if self.cancel_requested:
                raise Cancelled()
            reason = self.budget.stop_reason()
            if reason:
                self.termination = reason
                return
            caps = self.attempt_caps
            if caps and (self.budget.elapsed() >= caps[0] or self.budget.steps >= caps[1]):
                self.termination = "attempt_budget"
                return
            steps_left = (caps[1] if caps else lim.max_steps) - self.budget.steps
            secs_left = min(self.budget.work_remaining(), caps[0] - self.budget.elapsed()) if caps \
                else self.budget.work_remaining()
            max_steps = (caps[1] - self.attempt_start["step"]) if caps else lim.max_steps
            span_s = (caps[0] - self.attempt_start["elapsed"]) if caps else lim.time_limit_s
            if pol.budget_notices and not notice_sent and (
                steps_left <= max(3, int(max_steps * 0.1)) or secs_left <= max(60.0, span_s * 0.1)
            ):
                notice_sent = True
                self._append({"role": "user", "content": prompts.budget_notice(steps_left, secs_left)})
            used = max((self.budget.steps - self.attempt_start["step"]) / max(1, max_steps),
                       (self.budget.elapsed() - self.attempt_start["elapsed"]) / max(1.0, span_s))
            if pol.budget_notices and not check_nudged and used >= 0.25 and not self._checked_this_attempt():
                check_nudged = True
                self._append({"role": "user",
                              "content": prompts.no_check_yet("register_reproduction" in self.tools.specs)})
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
                adaptable = e.cls in (ErrorClass.UNSUPPORTED, ErrorClass.MALFORMED_REQUEST) or (
                    e.cls == ErrorClass.CONTEXT_OVERFLOW
                    and output_token_limit(e.message, self.profile.model.max_output_tokens) is not None)
                if adaptable and adaptations < 4 and hasattr(self.client, "adapt"):
                    change = self.client.adapt(e)
                    if change:
                        adaptations += 1
                        self.notes.append(f"parameter compatibility: {change}")
                        self.log(f"parameter compatibility: {change}")
                        continue
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
                if call.name == "register_reproduction" and "register_reproduction" in self.tools.specs:
                    any_valid = True
                    self._register_reproduction(call)
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
            if self.escalate_attempt and not submitted:
                self.termination = "no_progress"
                return
            if submitted:
                self.termination = "model_submitted"
                return
            if format_errors >= lim.max_consecutive_format_errors:
                self.termination = "repeated_format_errors"
                return

    def _checked_this_attempt(self) -> bool:
        start = int(self.attempt_start["step"])
        return any(r.source == "agent" and r.step > start for r in self.records) or any(
            r.get("attempt") == self.attempt for r in self.reproductions)

    def _tool_message(self, call: ToolCall, content: str, meta: dict[str, Any]) -> None:
        self._append({
            "role": "tool", "tool_call_id": call.id, "name": call.name, "content": content,
            "output_id": meta.get("output_id"), "source_output": meta.get("source_output"),
        })

    def _run_tool(self, call: ToolCall) -> ToolResult:
        cmd = (call.arguments or {}).get("command") if call.name == "bash" else None
        repro_keys = {r["check_key"] for r in self.reproductions}
        is_repro = isinstance(cmd, str) and call.parse_error is None and normalize_command(cmd) in repro_keys
        is_check = isinstance(cmd, str) and call.parse_error is None and (is_repro or is_check_command(cmd))
        pre_tree = self._capture_state(f"before check at step {self.budget.steps}") if is_check else None
        self._audit_access(call)
        res = self.tools.execute(call)
        self.budget.record_tool(call.name, float(res.meta.get("duration_s", 0.0)))
        self._tool_message(call, res.content, res.meta)
        append_jsonl(self.run_dir / "actions.jsonl", self.redactor.obj({
            "step": self.budget.steps, "tool": call.name, "arguments": call.raw_arguments[:4000],
            "status": res.status, "meta": res.meta,
        }))
        if cmd and res.status == "ok" and res.meta.get("exit_code") == 0 and memory.INSTALL_RE.match(cmd):
            self.setup_commands.append(cmd)
        label = _short(cmd, 70) if cmd else _short(call.raw_arguments, 70)
        self.log(f"step {self.budget.steps} · {call.name} {label} → {res.status}"
                 + (f" exit={res.meta.get('exit_code')}" if call.name == "bash" else ""))
        if is_check and res.status in ("ok", "timeout") and pre_tree is not None:
            post_tree = self._capture_state(f"after check at step {self.budget.steps}")
            text = self.archive.read_text(res.meta["output_id"]) or ""
            if is_repro:
                oc = proof.classify_reproduction(text, res.meta.get("exit_code"),
                                                 timed_out=bool(res.meta.get("timed_out")))
            else:
                oc = classify_output(text, res.meta.get("exit_code"), timed_out=bool(res.meta.get("timed_out")),
                                     piped="|" in cmd)
            rec = VerificationRecord(
                id=f"v{len(self.records) + 1}", step=self.budget.steps, tree=pre_tree,
                binding="exact" if pre_tree == post_tree else "mutated", command=cmd,
                check_key=normalize_command(cmd), cwd=str(self.tools.repo), exit_code=res.meta.get("exit_code"),
                timed_out=bool(res.meta.get("timed_out")), outcome=oc.outcome, runner=oc.runner, counts=oc.counts,
                detail=oc.detail, output_id=res.meta.get("output_id"), source="agent",
                duration_s=float(res.meta.get("duration_s", 0.0)), kind="reproduction" if is_repro else "check",
                failing=proof.failing_tests(text, oc.runner),
            )
            self.records.append(rec)
            append_jsonl(self.run_dir / "evidence.jsonl", rec.to_dict())
            if self.profile.policy.failure_memory:
                self._failure_memory(rec, text, cmd)
        if cmd and self.profile.policy.repetition_notice:
            stable = re.sub(r" · [0-9.]+s\]", "]", res.content)  # durations differ between identical runs
            # same command, same output, same code: edits in between are the failure memory's business
            key = (normalize_command(cmd), hashlib.sha1(stable.encode()).hexdigest(), self.last_tree or "")
            self.recent_actions = (self.recent_actions + [key])[-8:]
            n = self.recent_actions.count(key)
            if n >= 3 and key not in self.repetition_warned:
                self.repetition_warned.add(key)
                self._append({"role": "user", "content": prompts.REPETITION.format(n=n)})
        return res

    def _failure_memory(self, rec: VerificationRecord, text: str, cmd: str) -> None:
        assert self.ws is not None
        prev = self.failure_memory.last_tree.get(rec.check_key)
        changed = [f["path"] for f in self.ws.changed_files(prev, rec.tree)] if prev and prev != rec.tree else []
        action = self.failure_memory.observe(rec.check_key, rec.tree, rec.outcome, rec.failing, rec.counts, text,
                                             changed)
        if action == "intervene":
            self._append({"role": "user", "content": self.failure_memory.message(rec.check_key, cmd)})
            self.log("failure memory: the same failure after repeated edits to the same code; asked for a new hypothesis")
        elif action == "escalate":
            if self.attempt < self.profile.policy.max_attempts:
                self.escalate_attempt = True
                self.log("failure memory: still no progress after the new-hypothesis request; ending this attempt")
            else:
                self._append({"role": "user", "content": self.failure_memory.message(rec.check_key, cmd)
                              + " This is the last attempt: revert what did not help and try a different fix."})

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
        """Review a submission once; review it a second time only when the harness's own verification
        contradicts it (a regression, or a confirmed reproduction still failing)."""
        pol = self.profile.policy
        if not pol.submit_review or reviews_done >= 2:
            return None
        assert self.ws is not None and self.base_tree is not None
        tree = self._capture_state("at submit")
        if tree == self.base_tree:
            return prompts.SUBMIT_EMPTY if reviews_done == 0 else None
        assessment = self._verify_candidate(tree) if pol.verify_at_submit else None
        if reviews_done == 1 and (assessment is None or assessment.level != "refuted"):
            return None
        files = self.ws.changed_files(self.base_tree, tree)
        new_files = [f["path"] for f in files if f["status"] == "A"]
        unverified = not any(r.tree == tree and r.binding == "exact" for r in self.records)
        if reviews_done == 0:
            if assessment is None and not new_files and not unverified:
                return None
            if assessment is not None and not new_files and assessment.level in ("proven", "fixed"):
                return None
        changed = [f"{f['status']} {f['path']}" for f in files]
        return prompts.submit_review(changed, new_files, unverified, assessment)

    # ------------------------------------------------------------------ proof-carrying patches
    def _restore_after_detour(self) -> None:
        if self.detour and self.ws is not None:
            try:
                self.ws.restore(self.detour)
            finally:
                self.detour = None

    def _harness_run(self, tree: str, command: str, check_key: str, kind: str, source: str,
                     timeout_s: float) -> VerificationRecord:
        """Run a check on `tree` (switching the working tree there and back if needed); the record is
        bound to `tree`. The agent's own state is captured first and restored afterwards."""
        assert self.ws is not None
        current = self._capture_state("before a harness check")
        if tree != current:
            self.detour = current
            self._checkpoint("solving")
            self.ws.restore(tree)
        oid, out_path = self.archive.allocate()
        try:
            r = run_shell(command, cwd=self.tools.repo, env=self.tools.env, timeout_s=timeout_s, output_path=out_path,
                          max_output_bytes=self.profile.tools.max_output_bytes,
                          should_cancel=lambda: self.cancel_requested, wrap=self.tools.wrap)
            post = self.ws.snapshot()
        finally:
            if tree != current:
                self._restore_after_detour()
                self._checkpoint("solving")
        self.archive.redact_file(out_path)
        text = read_output_file(out_path)
        oc = proof.classify_reproduction(text, r.exit_code, timed_out=r.timed_out) if kind == "reproduction" else \
            classify_output(text, r.exit_code, timed_out=r.timed_out, piped="|" in command)
        rec = VerificationRecord(
            id=f"v{len(self.records) + 1}", step=self.budget.steps, tree=tree,
            binding="exact" if post == tree else "mutated", command=command, check_key=check_key,
            cwd=str(self.tools.repo), exit_code=r.exit_code, timed_out=r.timed_out, outcome=oc.outcome,
            runner=oc.runner, counts=oc.counts, detail=oc.detail, output_id=oid, source=source,
            duration_s=r.duration_s, kind=kind, failing=proof.failing_tests(text, oc.runner),
        )
        self.records.append(rec)
        append_jsonl(self.run_dir / "evidence.jsonl", rec.to_dict())
        self.budget.record_tool("harness_check", r.duration_s)
        where = "original code" if source == "harness_original" else f"candidate {tree[:10]}"
        self.log(f"harness check on the {where}: {_short(command, 60)} → {oc.outcome}")
        return rec

    def _counterfactual(self, tree: str) -> str:
        """Original code + this candidate's own test-file changes (new tests exist, the fix does not)."""
        assert self.ws is not None and self.base_tree is not None
        files = self.ws.changed_files(self.base_tree, tree)
        paths = sorted({p for f in files for p in (f["path"], f.get("old_path")) if p and proof.is_test_path(p)})
        return self.ws.overlay_tree(self.base_tree, tree, paths)

    def _checks_for(self, tree: str) -> list[tuple[str, str, str]]:
        """(check_key, command, kind): registered reproductions, then the agent's latest distinct test
        commands on this exact tree (or, if none ran on it, its most recent ones)."""
        out = [(r["check_key"], r["command"], "reproduction") for r in self.reproductions]
        seen = {k for k, _, _ in out}
        agent = [r for r in self.records if r.source == "agent" and r.kind == "check"]
        pool = [r for r in agent if r.tree == tree and r.binding == "exact"] or agent
        for r in reversed(pool):
            if len(out) >= len(self.reproductions) + 3:
                break
            if r.check_key not in seen:
                seen.add(r.check_key)
                out.append((r.check_key, r.command, "check"))
        return out

    def _ensure_run(self, tree: str, key: str, command: str, kind: str, source: str,
                    allow_runs: bool) -> VerificationRecord | None:
        rec = proof.latest(self.records, key, tree)
        if rec is not None or not allow_runs or self.cancel_requested or not hasattr(self, "tools"):
            return rec  # (offline recovery has no tools: evidence comes from the records only)
        past = [r.duration_s for r in self.records if r.check_key == key]
        need = (max(past) if past else 20.0) * 1.5 + 5.0
        available = self.budget.work_remaining()
        if available < need:
            return None
        return self._harness_run(tree, command, key, kind, source,
                                 timeout_s=min(self.profile.tools.bash_timeout_s, available))

    def _generated_tests(self, tree: str) -> tuple[str, set[str]]:
        """Added lines of the candidate's test-file changes, and the test files it created."""
        assert self.ws is not None and self.base_tree is not None
        files = self.ws.changed_files(self.base_tree, tree)
        paths = [f["path"] for f in files if f["status"] in "AMR" and proof.is_test_path(f["path"])]
        if not paths:
            return "", set()
        diff = self.ws.git("diff", "--no-renames", "--no-ext-diff", "-U0", self.base_tree, tree, "--", *paths,
                           check=False).stdout.decode("utf-8", "replace")
        added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        return added, {f["path"] for f in files if f["status"] == "A" and proof.is_test_path(f["path"])}

    def _script_text(self, command: str) -> str:
        """Contents of script files a reproduction command runs (for the private-internals check)."""
        out = []
        for tok in re.findall(r"[\w./~-]+\.(?:py|js|ts|sh|rb|go)\b", command)[:3]:
            p = Path(tok).expanduser()
            p = p if p.is_absolute() else self.tools.repo / p
            try:
                if p.is_file() and p.stat().st_size < 200_000:
                    out.append(p.read_text(errors="replace"))
            except OSError:
                pass
        return "\n".join(out)

    def _verify_candidate(self, tree: str, checks: list[tuple[str, str, str]] | None = None, *,
                          allow_runs: bool = True) -> proof.Assessment:
        """Compare each check on the counterfactual state and on the candidate (runs what is missing,
        within the remaining work time) and assess the candidate with evidence authority tiers."""
        assert self.ws is not None and self.base_tree is not None
        checks = self._checks_for(tree) if checks is None else checks
        cf = self._counterfactual(tree)
        added_text, added_files = self._generated_tests(tree)

        def is_generated_test(test_id: str) -> bool:
            if "::" in test_id and test_id.split("::", 1)[0] in added_files:
                return True
            name = proof.test_name(test_id)
            return bool(name) and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", added_text) is not None

        repro = {r["check_key"]: r for r in self.reproductions}
        kind_of_task = self.task_type.kind if self.task_type else "bug"
        comparisons = []
        for key, command, kind in checks:
            orig = self._ensure_run(cf, key, command, kind, "harness_original", allow_runs)
            cand = self._ensure_run(tree, key, command, kind, "harness_candidate", allow_runs)
            orig_text = (self.archive.read_text(orig.output_id) or "")[-20000:] if orig and orig.output_id and \
                hasattr(self, "archive") else ""
            script = self._script_text(command) if kind == "reproduction" and hasattr(self, "tools") else ""
            comparisons.append(proof.compare(
                key, command, kind, orig, cand, is_generated_test=is_generated_test,
                generated_check=(kind == "reproduction" and (orig is None or orig.failing is None)),
                orig_output=orig_text, task_kind=kind_of_task,
                impl_specific=bool(proof.IMPL_SPECIFIC.search(added_text + "\n" + script + "\n" + command)),
                stable=(repro.get(key) or {}).get("stable")))
        files = self.ws.changed_files(self.base_tree, tree)
        tests_modified = [f["path"] for f in files if f["status"] in "MDR"
                          and proof.is_test_path(f.get("old_path") or f["path"])
                          and not (f["status"] == "M" and self.ws.lines_removed(self.base_tree, tree, f["path"]) == 0)]
        a = proof.assess(tree, comparisons, tests_modified=tests_modified, expected=len(checks),
                         task_kind=kind_of_task)
        a.attempt = self.attempt
        stat = self.ws.shortstat(self.base_tree, tree)
        a.diff_lines = sum(int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", stat))
        self.assessments[tree] = a
        return a

    def _register_reproduction(self, call: ToolCall) -> None:
        args = call.arguments or {}
        command = args.get("command")
        if call.parse_error or not isinstance(command, str) or not command.strip():
            self._tool_message(call, "Error: register_reproduction needs a non-empty `command` string.",
                               {"tool": "register_reproduction"})
            return
        key = normalize_command(command)
        entry = next((r for r in self.reproductions if r["check_key"] == key), None)
        if entry is None and len(self.reproductions) >= 3:
            self._tool_message(call, "Error: at most 3 reproductions can be registered; re-register an existing "
                                     "command to update it.", {"tool": "register_reproduction"})
            return
        t0 = time.monotonic()
        tree = self._capture_state(f"at reproduction registration (step {self.budget.steps})")
        cf = self._counterfactual(tree)
        if entry is None:
            entry = {"id": f"R{len(self.reproductions) + 1}"}
            self.reproductions.append(entry)
        entry.update(command=command, check_key=key, description=str(args.get("description") or "")[:300],
                     attempt=self.attempt, step=self.budget.steps, confirmed=None)
        timeout = min(self.profile.tools.bash_timeout_s, max(1.0, self.budget.work_remaining()))

        def run(on: str, source: str):
            if self.budget.work_remaining() < 5.0:
                return None
            rec = self._harness_run(on, command, key, "reproduction", source, timeout)
            text = self.archive.read_text(rec.output_id) if rec.output_id else ""
            return rec, "\n".join((text or "").splitlines()[-15:])[-1500:]

        original = run(cf, "harness_original")
        entry["confirmed"] = None if original is None else proof.verdict(original[0]) == "fail"
        entry["stable"] = None
        if original is not None and original[0].duration_s < 10.0 and self.budget.work_remaining() > 30.0:
            again = self._harness_run(cf, command, key, "reproduction", "harness_original", timeout)
            entry["stable"] = proof.verdict(again) == proof.verdict(original[0])
        current = run(tree, "harness_candidate") if tree != cf else None
        self.budget.record_tool("register_reproduction", time.monotonic() - t0)
        self._tool_message(call, prompts.reproduction_report(entry, original, current), {"tool": "register_reproduction"})
        self.log(f"reproduction {entry['id']} registered: {_short(command, 60)} → "
                 + {True: "confirmed (fails on the original code)", False: "NOT reproducing (passes on the original code)",
                    None: "unconfirmed"}[entry["confirmed"]])
        self._checkpoint("solving")

    # ------------------------------------------------------------------ attempts
    def _begin_attempt(self) -> None:
        pol, lim = self.profile.policy, self.profile.limits
        self.attempt_start = {"step": self.budget.steps, "elapsed": self.budget.elapsed()}
        if self.base_tree is not None and not self.attempt_trees:
            self.attempt_trees = [self.base_tree]
        attempts_left = pol.max_attempts - self.attempt + 1
        if attempts_left <= 1:
            self.attempt_caps = None
            return
        share = pol.first_attempt_share if self.attempt == 1 else 1.0 / attempts_left
        secs = max(0.0, self.budget.work_remaining()) * share
        steps = max(1, int((lim.max_steps - self.budget.steps) * share))
        self.attempt_caps = (self.budget.elapsed() + secs, self.budget.steps + steps)

    def _end_attempt(self) -> None:
        if self.ws is None or self.base_tree is None:
            return
        final = self._capture_state(f"end of attempt {self.attempt}")
        pol = self.profile.policy
        candidate, why = select_candidate(
            final, self.base_tree, list(self.attempt_trees), self.records, dominance=pol.dominance_selection,
            recover_empty_final=pol.recover_empty_final and self.termination != "model_submitted")
        entry: dict[str, Any] = {
            "n": self.attempt, "termination": self.termination, "steps": self.budget.steps - int(self.attempt_start["step"]),
            "elapsed_s": round(self.budget.elapsed() - self.attempt_start["elapsed"], 2), "candidate": candidate,
            "candidate_reason": why, "submit_summary": self.submit_summary[:500],
        }
        if candidate != self.base_tree:
            fatal = self.termination in ("cancelled", "crash") or self.cancel_requested
            a = self.assessments.get(candidate) if fatal else None
            # without harness verification (ablation), assess from the agent's own records only
            a = a or self._verify_candidate(candidate, allow_runs=not fatal and pol.verify_at_submit)
            entry.update(level=a.level, summary=a.summary(), advisory=a.advisory,
                         files=[f["path"] for f in self.ws.changed_files(self.base_tree, candidate)])
        else:
            entry.update(level="unverified", summary="no change", files=[])
        self.attempts.append(entry)
        self.log(f"attempt {self.attempt} ended ({self.termination}): candidate {candidate[:12]} · evidence "
                 f"{entry['level']}")
        self._checkpoint("solving")

    def _next_attempt_worthwhile(self) -> bool:
        pol = self.profile.policy
        last = self.attempts[-1] if self.attempts else None
        if last is None or self.attempt >= pol.max_attempts or self.cancel_requested:
            return False
        term = self.termination or ""
        if term in FATAL_TERMINATIONS or term.startswith("model_error"):
            return False
        level = proof.LEVELS.index(last["level"])
        threshold = pol.retry_below if term == "model_submitted" else "fixed"  # stuck: retry unless verified
        explore = pol.retry_on_advisory and bool(last.get("advisory")) and level < proof.LEVELS.index("proven")
        if level >= proof.LEVELS.index(threshold) and not explore:
            return False
        need = max(pol.min_attempt_s, 0.5 * float(last["elapsed_s"]))
        if self.budget.work_remaining() < need or self.profile.limits.max_steps - self.budget.steps < 5:
            self.notes.append(f"no further attempt: evidence {last['level']}, but only "
                              f"{max(0.0, self.budget.work_remaining()):.0f}s of work time left (needs ~{need:.0f}s)")
            return False
        return True

    def _prepare_next_attempt(self) -> None:
        """Start the next attempt from the original code with a fresh conversation that carries the
        harness's observations (not the previous transcript)."""
        assert self.ws is not None and self.base_tree is not None
        self.ws.restore(self.base_tree)
        if self.profile.policy.git_hygiene and self.target_git_initial is not None:
            self.ws.restore_target_git(self.target_git_initial)
        self.attempt += 1
        self.termination = None
        self.submit_summary = ""
        self.last_tree = self.base_tree
        self.attempt_trees = [self.base_tree]
        self.recent_actions, self.repetition_warned = [], set()
        self.failure_memory.reset_attempt()
        self.escalate_attempt = False
        self.transcript = []
        append_jsonl(self.run_dir / "transcript.jsonl", {"role": "harness", "event": "attempt_start",
                                                         "attempt": self.attempt})
        self._append({"role": "system", "content": self._system_msg})
        self._append({"role": "user", "content": self._task_msg
                      + prompts.attempt_note(self.attempt, self.attempts, self.reproductions)})
        self._new_context()
        self.log(f"starting attempt {self.attempt} from the original code (previous evidence: "
                 f"{self.attempts[-1]['level']})")
        self._checkpoint("solving")

    def _select_across_attempts(self) -> tuple[str, str, list[proof.Assessment]]:
        """Cross-check every attempt's candidate on every check (as time allows) and rank them."""
        finalists = list(dict.fromkeys(a["candidate"] for a in self.attempts if a.get("candidate") != self.base_tree))
        if not finalists:
            return self.base_tree, "no attempt produced a change", []
        union: list[tuple[str, str, str]] = []
        for t in finalists:
            for c in self._checks_for(t):
                if c[0] not in {u[0] for u in union}:
                    union.append(c)
        allow = self.termination not in ("cancelled", "crash") and not self.cancel_requested \
            and self.profile.policy.verify_at_submit
        ranked_in = []
        for t in finalists:
            a = self._verify_candidate(t, union, allow_runs=allow)
            a.attempt = next(x["n"] for x in self.attempts if x["candidate"] == t)
            ranked_in.append(a)
        ranked = proof.rank_candidates(ranked_in)
        best = ranked[0]
        why = (f"best evidence across {len(self.attempts)} attempts: attempt {best.attempt} ({best.summary()})")
        return best.tree, why, ranked

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
            self._restore_after_detour()
            final_tree = self._capture_state("final")
            self.final_state_tree = final_tree
            if pol.git_hygiene:
                try:
                    fin_notes += self.ws.restore_target_git(self.target_git_initial)
                except WorkspaceError as e:
                    fin_notes.append(f"target git hygiene failed: {e}")
            if len(self.attempts) < self.attempt:  # the current attempt never closed (interrupted / recovered)
                cand, why = select_candidate(
                    final_tree, self.base_tree, self.attempt_trees or self.history, self.records,
                    dominance=pol.dominance_selection,
                    recover_empty_final=pol.recover_empty_final and self.termination != "model_submitted")
                a = self._verify_candidate(cand, allow_runs=False) if cand != self.base_tree else None
                self.attempts.append({"n": self.attempt, "termination": self.termination, "candidate": cand,
                                      "candidate_reason": why, "level": a.level if a else "unverified",
                                      "summary": a.summary() if a else "no change", "submit_summary": self.submit_summary,
                                      "files": [f["path"] for f in self.ws.changed_files(self.base_tree, cand)]})
            ranked: list[proof.Assessment] = []
            if len(self.attempts) > 1:
                selected, reason, ranked = self._select_across_attempts()
            else:
                selected, reason = self.attempts[0]["candidate"], self.attempts[0]["candidate_reason"]
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
            best = (self.assessments.get(selected) if selected in self.assessments and not ranked else None) or (
                self._verify_candidate(selected, allow_runs=False) if selected != self.base_tree else None)
            result["proof"] = {
                "level": best.level if best else "unverified",
                "summary": best.summary() if best else "no change",
                "comparisons": [c.to_dict() for c in best.comparisons] if best else [],
                "reproductions": self.reproductions,
                "attempts": self.attempts,
                "ranking": [a.to_dict() for a in ranked],
                "method": "each check is run on the original code (plus the candidate's own test changes) and on the "
                          "candidate; levels and ranking are defined in gheerefill/proof.py",
            }
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
                "final_state_tree": final_tree,
                "candidates_observed": len([t for t in self.history if t != self.base_tree]),
                "integrity": {
                    "note": "observations for audit, not a verdict; the harness does not have OS-level isolation",
                    "modified_existing_test_files": [
                        f["path"] for f in files if f["status"] in "MDR" and TEST_PATH_RE.search(f.get("old_path") or f["path"])
                        and not (f["status"] == "M" and self.ws.lines_removed(self.base_tree, selected, f["path"]) == 0)],
                    "extended_existing_test_files": [
                        f["path"] for f in files if f["status"] == "M" and TEST_PATH_RE.search(f["path"])
                        and self.ws.lines_removed(self.base_tree, selected, f["path"]) == 0],
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
        if result.get("status") == "completed":
            try:
                sel = result["selected_candidate"]["tree"]
                cf = self._counterfactual(sel) if sel != self.base_tree else None
                atomic_write_json(self.run_dir / "attestation.json",
                                  attest.build(self.run_dir, result, self.records, self.base_tree, cf))
                result["attestation"] = {"path": str(self.run_dir / "attestation.json"),
                                         "verify": f"python -m gheerefill verify --run-dir {self.run_dir}"}
            except Exception as e:  # noqa: BLE001 - the attestation must never break delivery
                result["notes"].append(f"attestation not written: {type(e).__name__}: {e}")
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
                "profile": self.profile.name, "profile_id": self.profile_id,
                "overrides": self.profile.overrides,
            },
            "status": "infrastructure_error",
            "isolation": self.sandbox.status if self.sandbox else None,
            "termination": self.termination,
            "task_type": self.task_type.to_dict() if self.task_type else None,
            "progress": {**self.failure_memory.to_dict(), "repetition_notices": len(self.repetition_warned)},
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
