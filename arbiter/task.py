"""Task adapter: LOCAL DEVELOPMENT PROTOCOL (not the official event protocol).

Input: a single JSON object, a JSON array of objects, or JSON Lines (one object per line).
Each task object:

    {"task_id": "...", "repo_path": "/abs/or/relative/path", "issue": "text",
     "limits": {"time_limit_s": 900, "max_steps": 80}}          # limits optional

Accepted aliases, first present wins (see _ALIASES): id/instance_id -> task_id, repo/repository/workdir
-> repo_path (a local directory among them wins over an "owner/name" slug, which is cloned at
base_commit), problem_statement/instruction(s)/description/prompt -> issue. SWE-bench, SWE-bench Pro
(requirements, interface), Multi-SWE-bench (org, resolved_issues, base.sha, f2p_tests) and
SWE-PolyBench (F2P, P2P, test_command) rows are read as they are published. Reference solutions
(`patch`, `fix_patch`, `canonical_solution`, ...) are dropped on input: never stored or shown.
Relative repo paths resolve against `base_dir` (task file directory, or CWD for stdin).

Behaviour:
- EOF ends the input; all tasks read so far are processed.
- A malformed line/object yields a TaskInputError (reported as an `invalid_input` result
  record on stdout); processing continues with the next line.
- Blank lines are ignored.

Replace this module when the official task protocol is published.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterator

from arbiter.config import LimitsConfig

_ALIASES = {  # in priority order: the first present, non-empty key wins
    "task_id": ("task_id", "id", "instance_id", "name"),
    "repo_path": ("repo_path", "repo_dir", "workdir", "work_dir", "repository", "repo"),
    "issue": ("issue", "problem_statement", "issue_text", "instruction", "instructions", "task_description",
              "description", "prompt"),
}
# Reference solutions some datasets ship next to the task (SWE-bench `patch`, Multi-SWE-bench `fix_patch`,
# HumanEval `canonical_solution`, ...). Dropped at parse time: never stored, never shown to the model.
SOLUTION_FIELDS = ("patch", "gold_patch", "golden_patch", "fix_patch", "reference_patch", "solution_patch",
                   "canonical_solution", "solution", "model_patch", "reference_solution")
# Extra specification text some formats keep outside the problem statement (SWE-bench Pro, SWE-bench hints).
SPEC_SECTIONS = (("requirements", "Requirements"), ("interface", "Interface the tests expect"),
                 ("hints_text", "Discussion from the issue thread (supplied with the task)"),
                 ("hints", "Hints supplied with the task"))
_LIMIT_KEYS = {f for f in LimitsConfig.__dataclass_fields__}


@dataclass
class Task:
    task_id: str
    repo_path: Path
    issue: str
    limits: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        """Exact inverse of to_dict (used for checkpoint recovery; no alias handling)."""
        return cls(task_id=str(d["task_id"]), repo_path=Path(d["repo_path"]), issue=d["issue"],
                   limits=dict(d.get("limits") or {}), metadata=dict(d.get("metadata") or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repo_path": str(self.repo_path),
            "issue": self.issue,
            "limits": self.limits,
            "metadata": self.metadata,
        }


@dataclass
class TaskInputError:
    location: str
    message: str
    task_id: str | None = None


def _pick(obj: dict[str, Any], canonical: str) -> tuple[str | None, Any]:
    for k in _ALIASES[canonical]:
        if obj.get(k) not in (None, ""):
            return k, obj[k]
    return None, None


def _issue_text(obj: dict[str, Any]) -> Any:
    _, issue = _pick(obj, "issue")
    if issue is None and isinstance(obj.get("resolved_issues"), list):  # Multi-SWE-bench
        parts = [f"{i.get('title') or ''}\n\n{i.get('body') or ''}".strip() for i in obj["resolved_issues"]
                 if isinstance(i, dict)]
        issue = "\n\n---\n\n".join(p for p in parts if p) or None
    if issue is None and isinstance(obj.get("title"), str):  # a GitHub issue object
        issue = (obj["title"] + "\n\n" + (obj.get("body") or "")).strip()
    if isinstance(issue, str):
        for key, heading in SPEC_SECTIONS:
            v = obj.get(key)
            if isinstance(v, str) and v.strip() and v.strip() not in issue:
                issue += f"\n\n## {heading}\n{v.strip()}"
    return issue


def _repo(obj: dict[str, Any], base_dir: Path) -> tuple[Any, Path | None, str | None]:
    """(raw value, local directory, remote owner/name): a local directory among the repo keys wins."""
    values = [obj[k] for k in _ALIASES["repo_path"] if isinstance(obj.get(k), str) and obj[k].strip()]
    if isinstance(obj.get("org"), str) and values and "/" not in values[-1]:  # Multi-SWE-bench: org + repo
        values.append(f"{obj['org']}/{values[-1]}")
    for v in values:
        p = Path(v).expanduser()
        p = p if p.is_absolute() else (base_dir / p)
        if p.is_dir():
            return v, p.resolve(), None
    for v in values:
        if SLUG.match(v.strip()):
            return v, None, v.strip()
    return (values[0] if values else next((obj[k] for k in _ALIASES["repo_path"] if k in obj), None)), None, None


def _base_commit(obj: dict[str, Any]) -> str | None:
    for k in ("base_commit", "base_sha", "base_revision"):
        if isinstance(obj.get(k), str) and obj[k].strip():
            return obj[k].strip()
    base = obj.get("base")
    if isinstance(base, dict) and isinstance(base.get("sha"), str):  # Multi-SWE-bench
        return base["sha"]
    return None


def parse_task(obj: Any, base_dir: Path) -> Task:
    if not isinstance(obj, dict):
        raise ValueError(f"task must be a JSON object, got {type(obj).__name__}")
    _, task_id = _pick(obj, "task_id")
    repo, repo_path, remote = _repo(obj, base_dir)
    issue = _issue_text(obj)
    missing = [n for n, v in (("task_id", task_id), ("repo_path", repo), ("issue", issue)) if v in (None, "")]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    if not isinstance(task_id, (str, int)):
        raise ValueError("task_id must be a string")
    if not isinstance(repo, str):
        raise ValueError("repo_path must be a string path")
    if not isinstance(issue, str) or not issue.strip():
        raise ValueError("issue must be a non-empty string")
    if repo_path is None and remote is None:
        rp = Path(repo)
        raise ValueError(f"repo_path does not exist or is not a directory: {rp if rp.is_absolute() else (base_dir / rp)}")
    limits = obj.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    unknown = set(limits) - _LIMIT_KEYS
    if unknown:
        raise ValueError(f"unknown limit key(s): {sorted(unknown)} (allowed: {sorted(_LIMIT_KEYS)})")
    known = {k for ks in _ALIASES.values() for k in ks} | {"limits"}
    metadata = {k: v for k, v in obj.items() if k not in known and k not in SOLUTION_FIELDS}
    withheld = sorted(k for k in SOLUTION_FIELDS if k in obj)
    if withheld:
        metadata["withheld_fields"] = withheld
    base = _base_commit(obj)
    if base and "base_commit" not in metadata:
        metadata["base_commit"] = base
    if remote:
        metadata["remote_repo"] = remote
    return Task(task_id=str(task_id), repo_path=repo_path or Path(), issue=issue, limits=dict(limits),
                metadata=metadata)


SLUG = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
TEST_FIELDS = {  # accepted spellings of the evaluation's test information
    "fail_to_pass": ("FAIL_TO_PASS", "fail_to_pass", "F2P", "f2p", "f2p_tests", "tests_to_pass", "failing_tests"),
    "pass_to_pass": ("PASS_TO_PASS", "pass_to_pass", "P2P", "p2p", "p2p_tests", "tests_to_keep_passing"),
    "test_patch": ("test_patch", "tests_patch"),
    "test_command": ("test_command", "test_cmd", "test_cmds", "eval_command"),
}


def evaluation_tests(metadata: dict[str, Any]) -> dict[str, Any]:
    """The evaluation's tests carried by a task (SWE-bench fields and plain spellings)."""
    out: dict[str, Any] = {}
    nested = metadata.get("install_config") if isinstance(metadata.get("install_config"), dict) else {}
    for canon, keys in TEST_FIELDS.items():
        v = next((src[k] for src in (metadata, nested) for k in keys if src.get(k) not in (None, "", [], {})), None)
        if v is None:
            continue
        if canon in ("fail_to_pass", "pass_to_pass"):
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except json.JSONDecodeError:
                    v = [t for t in re.split(r"[\n,]+", v) if t.strip()]
            if isinstance(v, dict):  # Multi-SWE-bench: {test name: {run, test, fix}}
                v = list(v)
            v = [str(t).strip() for t in v if str(t).strip()] if isinstance(v, list) else []
            if not v:
                continue
        else:
            if canon == "test_command" and isinstance(v, list) and all(isinstance(c, str) for c in v):
                v = " && ".join(c.strip() for c in v if c.strip())
            if not isinstance(v, str) or not v.strip():
                continue
        out[canon] = v
    return out


def _task_id_hint(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for k in _ALIASES["task_id"]:
            if isinstance(obj.get(k), (str, int)):
                return str(obj[k])
    return None


def iter_tasks(stream: IO[str], *, base_dir: Path, source: str = "stdin") -> Iterator[Task | TaskInputError]:
    """Yield tasks from a stream. Streams line by line unless the first non-blank
    character opens a multi-line JSON document (pretty-printed object or array)."""
    first_line = None
    buffered: list[str] = []
    for line in stream:
        if line.strip():
            first_line = line
            break
        buffered.append(line)
    if first_line is None:
        return
    stripped = first_line.strip()
    is_single_line_json = False
    if stripped.startswith("{"):
        try:
            json.loads(stripped)
            is_single_line_json = True
        except json.JSONDecodeError:
            pass
    rest_lines = None
    if stripped.startswith("[") or (stripped.startswith("{") and not is_single_line_json):
        text = first_line + stream.read()
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            if stripped.startswith("[") or "\n{" not in text:
                yield TaskInputError(f"{source}", f"malformed JSON document: {e}")
                return
            # Not a multi-line document: a malformed first line followed by JSON Lines.
            rest_lines = text.splitlines(keepends=True)
        else:
            items = doc if isinstance(doc, list) else [doc]
            for i, obj in enumerate(items):
                loc = f"{source}[{i}]"
                try:
                    yield parse_task(obj, base_dir)
                except ValueError as e:
                    yield TaskInputError(loc, str(e), _task_id_hint(obj))
            return
    lineno = len(buffered)

    def lines():
        if rest_lines is not None:
            yield from rest_lines
            return
        yield first_line
        yield from stream

    for line in lines():
        lineno += 1
        if not line.strip():
            continue
        loc = f"{source}:{lineno}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            yield TaskInputError(loc, f"malformed JSON line: {e}")
            continue
        try:
            yield parse_task(obj, base_dir)
        except ValueError as e:
            yield TaskInputError(loc, str(e), _task_id_hint(obj))
