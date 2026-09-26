"""Task adapter: LOCAL DEVELOPMENT PROTOCOL (not the official event protocol).

Input: a single JSON object, a JSON array of objects, or JSON Lines (one object per line).
Each task object:

    {"task_id": "...", "repo_path": "/abs/or/relative/path", "issue": "text",
     "limits": {"time_limit_s": 900, "max_steps": 80}}          # limits optional

Accepted aliases (semantics-preserving): id/instance_id -> task_id,
repo/repository -> repo_path, problem_statement/issue_text -> issue.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterator

from gheerefill.config import LimitsConfig

_ALIASES = {
    "task_id": ("task_id", "id", "instance_id"),
    "repo_path": ("repo_path", "repo", "repository"),
    "issue": ("issue", "problem_statement", "issue_text"),
}
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
    found = [(k, obj[k]) for k in _ALIASES[canonical] if k in obj]
    if len(found) > 1:
        raise ValueError(f"conflicting keys for {canonical}: {[k for k, _ in found]}")
    return found[0] if found else (None, None)


def parse_task(obj: Any, base_dir: Path) -> Task:
    if not isinstance(obj, dict):
        raise ValueError(f"task must be a JSON object, got {type(obj).__name__}")
    _, task_id = _pick(obj, "task_id")
    _, repo = _pick(obj, "repo_path")
    _, issue = _pick(obj, "issue")
    missing = [n for n, v in (("task_id", task_id), ("repo_path", repo), ("issue", issue)) if v in (None, "")]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    if not isinstance(task_id, (str, int)):
        raise ValueError("task_id must be a string")
    if not isinstance(repo, str):
        raise ValueError("repo_path must be a string path")
    if not isinstance(issue, str) or not issue.strip():
        raise ValueError("issue must be a non-empty string")
    repo_path = Path(repo)
    if not repo_path.is_absolute():
        repo_path = (base_dir / repo_path).resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo_path does not exist or is not a directory: {repo_path}")
    limits = obj.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    unknown = set(limits) - _LIMIT_KEYS
    if unknown:
        raise ValueError(f"unknown limit key(s): {sorted(unknown)} (allowed: {sorted(_LIMIT_KEYS)})")
    known = {k for ks in _ALIASES.values() for k in ks} | {"limits"}
    metadata = {k: v for k, v in obj.items() if k not in known}
    return Task(task_id=str(task_id), repo_path=repo_path, issue=issue, limits=dict(limits), metadata=metadata)


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
