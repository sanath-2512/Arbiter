"""Model-facing tools: validated arguments, structured status, bounded observations.

Tools: bash, read_file, search, edit_file, write_file, read_output, submit.
`submit` is interpreted by the controller (agent.py), not executed here.

Argument handling: arguments are validated against each tool's JSON schema before
dispatch. The only normalisations applied are predefined and semantics-preserving:
integer-valued strings/floats for integer fields, "true"/"false" strings for booleans,
and CRLF line-ending adaptation in edit_file when the file uses CRLF. Nothing else is
guessed or rewritten; errors are returned precisely so the model can correct itself.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gheerefill.config import ToolsConfig
from gheerefill.models.base import ToolCall, ToolSpec
from gheerefill.outputs import OutputArchive, bounded_view, numbered_range
from gheerefill.shell import read_output_file, run_shell

LARGE_OUTPUT_BYTES = 8 * 1024 * 1024


def _spec(name: str, description: str, props: dict[str, Any], required: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": props, "required": required, "additionalProperties": False},
    )


def tool_specs(cfg: ToolsConfig) -> list[ToolSpec]:
    bash = _spec(
        "bash",
        "Run a bash command at the repository root in a fresh non-interactive shell (no state persists "
        f"between calls; stdin is closed; background processes are stopped when it returns). Default timeout "
        f"{int(cfg.bash_timeout_s)}s. Returns exit code and output (long output is truncated with retrieval hints).",
        {
            "command": {"type": "string", "description": "The bash command to run."},
            "timeout": {"type": "integer", "description": "Optional timeout in seconds."},
        },
        ["command"],
    )
    submit = _spec(
        "submit",
        "Finish the task. Call only when the change is complete and verified (or when you cannot do better). "
        "Your current working-tree changes are the submission.",
        {"summary": {"type": "string", "description": "One or two sentences on what was changed and how it was verified."}},
        [],
    )
    if cfg.set == "bash_only":
        return [bash, submit]
    return [
        bash,
        _spec(
            "read_file",
            f"Read a text file with line numbers (at most {cfg.read_max_lines} lines per call), or list a directory. "
            "Paths are relative to the repository root unless absolute.",
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-based first line (default 1)."},
                "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
            },
            ["path"],
        ),
        _spec(
            "search",
            "Search file contents (ripgrep syntax regex; respects .gitignore). Returns path:line:text matches.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "File or directory to search (default: repository root)."},
                "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'."},
                "fixed_strings": {"type": "boolean", "description": "Treat pattern as a literal string."},
                "case_insensitive": {"type": "boolean"},
                "context": {"type": "integer", "description": "Lines of context around each match (0-5)."},
            },
            ["pattern"],
        ),
        _spec(
            "edit_file",
            "Replace an exact, unique occurrence of old_str with new_str in an existing file. old_str must match the "
            "file exactly (including indentation). Fails without changing anything if old_str is missing or "
            "ambiguous (unless replace_all is true).",
            {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
            },
            ["path", "old_str", "new_str"],
        ),
        _spec(
            "write_file",
            "Create a file or overwrite it entirely with the given content (parent directories are created).",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        _spec(
            "read_output",
            "Retrieve lines of an earlier, truncated tool output by its id (e.g. o12).",
            {
                "id": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            ["id"],
        ),
        submit,
    ]


@dataclass
class ToolResult:
    content: str
    status: str  # ok | error | timeout | cancelled | output_limit
    meta: dict[str, Any] = field(default_factory=dict)


class ToolArgumentError(ValueError):
    pass


def validate_arguments(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    props = spec.parameters.get("properties", {})
    missing = [k for k in spec.parameters.get("required", []) if k not in args]
    if missing:
        raise ToolArgumentError(f"missing required argument(s): {', '.join(missing)}")
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ToolArgumentError(f"unknown argument(s): {', '.join(unknown)} (allowed: {', '.join(props)})")
    out: dict[str, Any] = {}
    for k, v in args.items():
        t = props[k].get("type")
        if v is None and k not in spec.parameters.get("required", []):
            continue
        if t == "string":
            if not isinstance(v, str):
                raise ToolArgumentError(f"argument {k!r} must be a string")
        elif t == "integer":
            if isinstance(v, bool):
                raise ToolArgumentError(f"argument {k!r} must be an integer")
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            elif isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
                v = int(v)
            if not isinstance(v, int):
                raise ToolArgumentError(f"argument {k!r} must be an integer")
        elif t == "boolean":
            if isinstance(v, str) and v.lower() in ("true", "false"):
                v = v.lower() == "true"
            if not isinstance(v, bool):
                raise ToolArgumentError(f"argument {k!r} must be true or false")
        out[k] = v
    return out


class ToolBox:
    def __init__(
        self,
        repo: Path,
        scratch: Path,
        archive: OutputArchive,
        cfg: ToolsConfig,
        env: dict[str, str],
        *,
        time_budget: Callable[[], float] = lambda: 1e9,
        should_cancel: Callable[[], bool] | None = None,
    ):
        self.repo = Path(os.path.realpath(repo))
        self.scratch = Path(os.path.realpath(scratch))
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.archive = archive
        self.cfg = cfg
        self.env = env
        self.time_budget = time_budget
        self.should_cancel = should_cancel
        self.specs = {s.name: s for s in tool_specs(cfg)}
        self._rg = shutil.which("rg")

    # ------------------------------------------------------------------ dispatch
    def execute(self, call: ToolCall) -> ToolResult:
        t0 = time.monotonic()
        spec = self.specs.get(call.name)
        if spec is None or call.name == "submit":
            res = ToolResult(
                f"Error: unknown tool {call.name!r}. Available tools: {', '.join(self.specs)}.", "error",
                {"error": "unknown_tool"},
            )
        elif call.parse_error or call.arguments is None:
            raw = (call.raw_arguments or "")[:300]
            res = ToolResult(
                f"Error: invalid arguments for {call.name}: {call.parse_error or 'not an object'}. "
                f"Raw arguments (first 300 chars): {raw!r}. Nothing was executed.",
                "error",
                {"error": "argument_parse"},
            )
        else:
            try:
                args = validate_arguments(spec, call.arguments)
                res = getattr(self, f"_tool_{call.name}")(args)
            except ToolArgumentError as e:
                res = ToolResult(f"Error: {call.name}: {e}. Nothing was executed.", "error", {"error": "argument_validation"})
        res.meta.setdefault("tool", call.name)
        res.meta.setdefault("duration_s", round(time.monotonic() - t0, 3))
        res.meta.setdefault("cwd", str(self.repo))
        if "output_id" not in res.meta:
            res.meta["output_id"] = self.archive.store(res.content)
        return res

    # ------------------------------------------------------------------ helpers
    def _resolve(self, p: str, *, write: bool) -> Path:
        if not p or "\x00" in p:
            raise ToolArgumentError("path must be a non-empty string without NUL bytes")
        path = Path(p)
        if not path.is_absolute():
            path = self.repo / path
        real = Path(os.path.realpath(path))
        if write:
            roots = (self.repo, self.scratch)
            if not any(real == r or r in real.parents for r in roots):
                raise ToolArgumentError(
                    f"refusing to write outside the repository ({self.repo}) or scratch directory ({self.scratch}): {p}"
                )
            if real == self.repo / ".git" or (self.repo / ".git") in real.parents:
                raise ToolArgumentError("refusing to modify files inside .git")
        return real

    def _display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.repo))
        except ValueError:
            return str(path)

    def _timeout(self, requested: int | None, default: float) -> float:
        available = self.time_budget()
        t = float(requested) if requested and requested > 0 else default
        return max(1.0, min(t, available))

    # ------------------------------------------------------------------ tools
    def _tool_bash(self, args: dict[str, Any]) -> ToolResult:
        command = args["command"]
        if not command.strip():
            raise ToolArgumentError("command is empty")
        if self.time_budget() < 1.0:
            return ToolResult("Error: no time budget left to run commands. Submit now.", "error", {"error": "no_budget"})
        timeout = self._timeout(args.get("timeout"), self.cfg.bash_timeout_s)
        oid, out_path = self.archive.allocate()
        r = run_shell(
            command,
            cwd=self.repo,
            env=self.env,
            timeout_s=timeout,
            output_path=out_path,
            max_output_bytes=self.cfg.max_output_bytes,
            should_cancel=self.should_cancel,
        )
        self.archive.redact_file(out_path)
        text = self.archive.redactor.text(read_output_file(out_path, LARGE_OUTPUT_BYTES))
        huge = r.output_bytes > LARGE_OUTPUT_BYTES
        view, truncated = bounded_view(text, self.cfg.max_observation_chars, None if huge else oid)
        if huge:
            view += f"\n[very large output ({r.output_bytes} bytes); inspect with bash, e.g. grep/sed on {out_path}]"
        if r.timed_out:
            status = "timeout"
            head = (
                f"[TIMED OUT after {timeout:.0f}s; the process group was killed. Files may have been changed before "
                f"the timeout. Partial output follows.]"
            )
        elif r.cancelled:
            status, head = "cancelled", "[CANCELLED by the harness; process group killed. Partial output follows.]"
        elif r.output_limit_hit:
            status = "output_limit"
            head = f"[KILLED: output exceeded {self.cfg.max_output_bytes} bytes. Partial output follows.]"
        else:
            status, head = "ok", f"[exit_code={r.exit_code} · {r.duration_s:.1f}s]"
        if r.leftover_processes_killed:
            head += " [background processes left running by the command were stopped]"
        body = view if text.strip() else "(no output)"
        if truncated and not huge:
            body += f'\n[full output: read_output(id="{oid}") or {out_path}]'
        return ToolResult(
            f"{head}\n{body}",
            status,
            {
                "exit_code": r.exit_code,
                "timed_out": r.timed_out,
                "cancelled": r.cancelled,
                "output_limit_hit": r.output_limit_hit,
                "duration_s": round(r.duration_s, 3),
                "timeout_s": timeout,
                "output_id": oid,
                "output_bytes": r.output_bytes,
                "truncated": truncated,
                "command": command,
            },
        )

    def _tool_read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve(args["path"], write=False)
        shown = self._display(path)
        if not path.exists():
            return ToolResult(f"Error: {shown} does not exist.", "error", {"error": "not_found"})
        if path.is_dir():
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            names = [e.name + ("/" if e.is_dir() else "") for e in entries if e.name != ".git"]
            limit = 300
            listing = "\n".join(names[:limit])
            more = f"\n[... {len(names) - limit} more entries; use bash ls/find]" if len(names) > limit else ""
            return ToolResult(f"Directory {shown}/ ({len(names)} entries):\n{listing}{more}", "ok", {})
        size = path.stat().st_size
        if size > 20 * 1024 * 1024:
            return ToolResult(f"Error: {shown} is {size} bytes; too large. Use search or bash (sed -n) instead.", "error", {})
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            return ToolResult(f"Error: {shown} looks binary ({size} bytes); not displayed.", "error", {"error": "binary"})
        text = data.decode("utf-8", errors="replace")
        start = args.get("start_line") or 1
        end = args.get("end_line")
        if start < 1:
            raise ToolArgumentError("start_line must be >= 1")
        if end is not None and end < start:
            raise ToolArgumentError("end_line must be >= start_line")
        rendered, first, last, total = numbered_range(
            text, start, end, self.cfg.read_max_lines, self.cfg.max_observation_chars
        )
        if total == 0:
            return ToolResult(f"{shown} is empty (0 lines).", "ok", {})
        if start > total:
            return ToolResult(f"Error: start_line {start} is beyond the end of {shown} ({total} lines).", "error", {})
        complete = first == 1 and last == total
        header = f"{shown} ({total} lines)" if complete else f"{shown} — lines {first}-{last} of {total}"
        footer = ""
        if last < total and (end is None or last < min(end, total)):
            footer = f"\n[showing lines {first}-{last} of {total}; call read_file with start_line={last + 1} for more]"
        elif last < total:
            footer = f"\n[file continues to line {total}]"
        return ToolResult(f"{header}\n{rendered}{footer}", "ok", {"lines": [first, last, total]})

    def _tool_search(self, args: dict[str, Any]) -> ToolResult:
        pattern = args["pattern"]
        if not pattern:
            raise ToolArgumentError("pattern is empty")
        target = self._resolve(args.get("path") or ".", write=False)
        ctx = args.get("context") or 0
        if not 0 <= ctx <= 5:
            raise ToolArgumentError("context must be between 0 and 5")
        rel_target = os.path.relpath(target, self.repo) if str(target).startswith(str(self.repo)) else str(target)
        if self._rg:
            cmd = [self._rg, "--line-number", "--no-heading", "--color=never", "--max-columns=400",
                   "--max-columns-preview", "--sort=path"]
            if args.get("glob"):
                cmd += ["--glob", args["glob"]]
            if args.get("fixed_strings"):
                cmd.append("--fixed-strings")
            if args.get("case_insensitive"):
                cmd.append("--ignore-case")
            if ctx:
                cmd += ["--context", str(ctx)]
            cmd += ["--", pattern, rel_target]
        else:
            cmd = ["grep", "-rnI", "--color=never", "--exclude-dir=.git", "--exclude-dir=node_modules"]
            cmd.append("-F" if args.get("fixed_strings") else "-E")
            if args.get("case_insensitive"):
                cmd.append("-i")
            if args.get("glob"):
                cmd.append(f"--include={args['glob']}")
            if ctx:
                cmd += ["-C", str(ctx)]
            cmd += ["--", pattern, rel_target]
        oid, out_path = self.archive.allocate()
        r = run_shell(
            " ".join(shlex.quote(c) for c in cmd),
            cwd=self.repo,
            env=self.env,
            timeout_s=self._timeout(None, 60.0),
            output_path=out_path,
            max_output_bytes=self.cfg.max_output_bytes,
            should_cancel=self.should_cancel,
        )
        text = read_output_file(out_path, LARGE_OUTPUT_BYTES)
        if r.timed_out:
            return ToolResult("Error: search timed out; narrow the path or glob.", "timeout", {"output_id": oid})
        if r.exit_code == 1 and not text.strip():
            return ToolResult(f"No matches for {pattern!r} in {rel_target}.", "ok", {"output_id": oid, "matches": 0})
        if r.exit_code not in (0, 1):
            return ToolResult(f"Error: search failed (exit {r.exit_code}):\n{text[:2000]}", "error", {"output_id": oid})
        lines = [l for l in text.split("\n") if l]
        match_lines = [l for l in lines if re.match(r"^.+?:\d+:", l)]
        files = {l.split(":", 1)[0] for l in match_lines}
        limit = self.cfg.search_max_results
        shown = lines[:limit]
        body = "\n".join(shown)
        view, _ = bounded_view(body, self.cfg.max_observation_chars, oid)
        summary = f"{len(match_lines)} matching lines in {len(files)} files"
        if len(lines) > limit:
            summary += f"; showing first {limit} result lines (all results: read_output(id=\"{oid}\"))"
        return ToolResult(f"[{summary}]\n{view}", "ok", {"output_id": oid, "matches": len(match_lines)})

    def _tool_edit_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve(args["path"], write=True)
        shown = self._display(path)
        old, new = args["old_str"], args["new_str"]
        if not path.is_file():
            return ToolResult(f"Error: {shown} does not exist (use write_file to create files).", "error", {"error": "not_found"})
        if old == "":
            raise ToolArgumentError("old_str must not be empty")
        if old == new:
            raise ToolArgumentError("old_str and new_str are identical; nothing to change")
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(f"Error: {shown} is not valid UTF-8; edit it with bash instead.", "error", {})
        note = ""
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\r\n" not in old and "\n" in old:
            old_crlf, new_crlf = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            if text.count(old_crlf):
                old, new, count = old_crlf, new_crlf, text.count(old_crlf)
                note = " (matched after adapting line endings to the file's CRLF)"
        if count == 0:
            first = next((l.strip() for l in old.split("\n") if l.strip()), "")
            hits = [i + 1 for i, l in enumerate(text.split("\n")) if first and l.strip() == first]
            hint = (
                f" The first line of old_str appears (ignoring surrounding whitespace) at line(s) "
                f"{', '.join(map(str, hits[:10]))}; re-read those lines and copy the text exactly."
                if hits
                else " Re-read the file and copy the text exactly (including indentation)."
            )
            return ToolResult(f"Error: old_str not found in {shown}. No changes made.{hint}", "error", {"error": "no_match"})
        if count > 1 and not args.get("replace_all"):
            starts, pos = [], text.find(old)
            while pos != -1 and len(starts) < 20:
                starts.append(text.count("\n", 0, pos) + 1)
                pos = text.find(old, pos + 1)
            return ToolResult(
                f"Error: old_str occurs {count} times in {shown} (starting at lines {', '.join(map(str, starts))}). "
                "No changes made. Include more surrounding context to make it unique, or set replace_all=true.",
                "error",
                {"error": "ambiguous"},
            )
        first_pos = text.find(old)
        new_text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        with open(path, "wb") as fh:
            fh.write(new_text.encode("utf-8"))
        start_line = new_text.count("\n", 0, first_pos) + 1
        span = new.count("\n") + 1
        snippet, a, b, total = numbered_range(new_text, max(1, start_line - 3), start_line + span + 2, 60, 4000)
        n = count if args.get("replace_all") else 1
        return ToolResult(
            f"Edited {shown}: replaced {n} occurrence{'s' if n > 1 else ''}{note}. Lines {a}-{b} now read:\n{snippet}",
            "ok",
            {"path": shown, "replacements": n},
        )

    def _tool_write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve(args["path"], write=True)
        shown = self._display(path)
        if path.is_dir():
            return ToolResult(f"Error: {shown} is a directory.", "error", {})
        existed = path.exists()
        before = path.read_bytes().count(b"\n") if existed else 0
        path.parent.mkdir(parents=True, exist_ok=True)
        content = args["content"]
        with open(path, "wb") as fh:
            fh.write(content.encode("utf-8"))
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        verb = f"Overwrote {shown} (previously {before} lines)" if existed else f"Created {shown}"
        return ToolResult(f"{verb}: {lines} lines, {len(content.encode())} bytes.", "ok", {"path": shown, "created": not existed})

    def _tool_read_output(self, args: dict[str, Any]) -> ToolResult:
        oid = args["id"].strip()
        text = self.archive.read_text(oid)
        if text is None:
            return ToolResult(f"Error: no archived output with id {oid!r}.", "error", {"error": "not_found"})
        start = args.get("start_line") or 1
        end = args.get("end_line")
        if start < 1 or (end is not None and end < start):
            raise ToolArgumentError("need 1 <= start_line <= end_line")
        rendered, first, last, total = numbered_range(text, start, end, self.cfg.read_max_lines, self.cfg.max_observation_chars)
        if total == 0:
            return ToolResult(f"Output {oid} is empty.", "ok", {"output_id": None, "source_output": oid})
        if start > total:
            return ToolResult(f"Error: output {oid} has only {total} lines.", "error", {"output_id": None, "source_output": oid})
        more = f"\n[lines {first}-{last} of {total}; request start_line={last + 1} for more]" if last < total else ""
        return ToolResult(f"Output {oid} — lines {first}-{last} of {total}:\n{rendered}{more}", "ok",
                          {"output_id": None, "source_output": oid})
