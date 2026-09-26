"""Model-family output quirks, normalised before the agent sees a turn.

Open-weight and hosted Qwen / DeepSeek models (and the servers in front of them) do not always
return clean native tool calls:

- A tool call leaks into `content` as text, with `finish_reason: stop` and no `tool_calls`. Documented
  dialects: DeepSeek V4 DSML (`<｜DSML｜invoke name=..>` with fullwidth bars; V4.1 puts a space after
  the bar), DeepSeek V3 special tokens (`<｜tool▁call▁begin｜>`), Qwen3-Coder XML
  (`<function=..><parameter=..>`), Hermes JSON (`<tool_call>{"name":..}</tool_call>`), Anthropic-style
  `<invoke name=..>`, and a bare/fenced JSON object. A harness that reads such a turn as "no action"
  wastes a step or ends the run.
- Reasoning arrives inline as `<think>...</think>` (servers without a reasoning parser).
- Arguments are not strict JSON (Python literals, trailing commas, raw newlines, double-encoded
  strings), or use another tool vocabulary the model was trained on (`str_replace_editor`,
  `execute_bash`, `file_path`, `old_string`, ...).

Everything here is deterministic and only accepts names of tools the harness actually offers, so
prose or code that merely looks like a call is not executed.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import uuid
from typing import Any

from gheerefill.models.base import ToolCall, ToolSpec

# ------------------------------------------------------------------ reasoning inline in content
_THINK_BLOCK = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.S | re.I)
_THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.I)
_THINK_OPEN = re.compile(r"^\s*<think(?:ing)?>", re.I)


def split_think(text: str) -> tuple[str, str]:
    """(reasoning, visible). Handles complete blocks, a closing tag only (the chat template opened
    the block in the prompt) and an unterminated opening block (output cut off while thinking)."""
    if not text or "think" not in text.lower():
        return "", text
    parts: list[str] = []
    visible = text
    m = _THINK_CLOSE.search(visible)
    if m and not re.search(r"<think(?:ing)?>", visible[:m.start()], re.I):
        parts.append(visible[:m.start()])
        visible = visible[m.end():]
    visible = _THINK_BLOCK.sub(lambda mm: parts.append(mm.group(1)) or "", visible)
    if _THINK_OPEN.match(visible):
        parts.append(_THINK_OPEN.sub("", visible, count=1))
        visible = ""
    return "\n".join(p.strip() for p in parts if p.strip()), visible.strip()


# ------------------------------------------------------------------ tool names and argument aliases
_NAME_PREFIX = re.compile(r"^(functions?|tools?|default_api|api|tool_call|call)[.:/]", re.I)
NAME_ALIASES = {
    "bash": ("execute_bash", "run_bash", "bash_command", "shell", "run_shell", "execute_command", "run_command",
             "terminal", "run_terminal_cmd", "exec", "execute", "cmd", "command", "sh", "run", "run_shell_command",
             "container.exec", "local_shell"),
    "read_file": ("read", "view", "view_file", "open_file", "open", "cat", "get_file", "file_read", "readfile",
                  "read_file_content", "show_file", "list_directory", "list_dir", "list_files", "listdir", "ls",
                  "view_directory"),
    "write_file": ("write", "create_file", "create", "save_file", "file_write", "writefile", "new_file", "overwrite_file",
                   "write_to_file"),
    "edit_file": ("edit", "replace", "str_replace", "replace_in_file", "file_edit", "modify_file", "apply_edit",
                  "search_replace", "replace_string"),
    "search": ("grep", "rg", "ripgrep", "search_code", "code_search", "find_in_files", "grep_search", "search_files",
               "find", "search_file_content", "search_dir", "search_file", "search_text"),
    "submit": ("finish", "done", "complete", "task_complete", "end", "attempt_completion", "final_answer",
               "submit_solution", "stop"),
    "read_output": ("get_output", "read_tool_output"),
    "register_reproduction": ("reproduce", "register_repro", "add_reproduction"),
}
_ALIAS_INDEX = {a: canon for canon, aliases in NAME_ALIASES.items() for a in aliases}
ARG_ALIASES = {
    "path": ("file_path", "filepath", "filename", "file", "path_name", "target_file", "file_name", "dir", "directory",
             "absolute_path", "relative_path", "dir_path", "folder"),
    "command": ("cmd", "bash_command", "shell_command", "script", "commands"),
    "old_str": ("old_string", "old_text", "old", "search", "find", "original", "old_content", "target"),
    "new_str": ("new_string", "new_text", "new", "replace", "replacement", "new_content", "updated"),
    "content": ("file_text", "text", "contents", "data", "body", "file_content", "code"),
    "pattern": ("query", "regex", "search_term", "term", "expression", "search_pattern", "keyword"),
    "summary": ("message", "result", "explanation", "reason", "final_answer", "answer"),
    "start_line": ("line_start", "from_line", "offset", "start", "start_line_number"),
    "end_line": ("line_end", "to_line", "end"),
    "description": ("desc",),
}


def normalize_tool_name(name: str, specs: dict[str, ToolSpec]) -> str:
    """The offered tool a model-written name refers to, or the name unchanged if none does."""
    if name in specs:
        return name
    n = _NAME_PREFIX.sub("", (name or "").strip().strip("`'\"").rstrip("()")).strip()
    for cand in (n, n.lower(), n.lower().replace("-", "_").replace(" ", "_")):
        if cand in specs:
            return cand
        canon = _ALIAS_INDEX.get(cand)
        if canon and canon in specs:
            return canon
    return name


def str_replace_editor(args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """OpenHands/SWE-agent `str_replace_editor` (the tool Qwen3-Coder is trained on) -> our tools."""
    cmd = str(args.get("command", "")).strip()
    path = args.get("path")
    if cmd == "view":
        out: dict[str, Any] = {"path": path}
        rng = args.get("view_range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            out["start_line"] = rng[0]
            if isinstance(rng[1], int) and rng[1] > 0:
                out["end_line"] = rng[1]
        return "read_file", out
    if cmd == "create":
        return "write_file", {"path": path, "content": args.get("file_text", args.get("content", ""))}
    if cmd == "str_replace":
        return "edit_file", {"path": path, "old_str": args.get("old_str", ""), "new_str": args.get("new_str", "")}
    return None


def canonical_arguments(tool: str, args: dict[str, Any], spec: ToolSpec) -> tuple[dict[str, Any], list[str]]:
    """Rename argument keys the tool does not know to the ones it does (only when unambiguous)."""
    props = spec.parameters.get("properties", {})
    out = dict(args)
    renamed = []
    for canon, aliases in ARG_ALIASES.items():
        if canon not in props or canon in out:
            continue
        present = [a for a in aliases if a in out and a not in props]
        if len(present) == 1:
            out[canon] = out.pop(present[0])
            renamed.append(f"{present[0]}->{canon}")
    if tool == "bash" and isinstance(out.get("command"), list):
        argv = [str(x) for x in out["command"]]
        # Codex-style argv: ["bash", "-lc", "<script>"] runs the script; anything else is quoted as argv
        if len(argv) >= 3 and os.path.basename(argv[0]) in ("bash", "sh", "zsh") and argv[1] in ("-c", "-lc", "-ic"):
            out["command"] = argv[2]
        else:
            out["command"] = shlex.join(argv)
        renamed.append("command list->string")
    if tool == "read_file" and "limit" in out and "limit" not in props and "end_line" in props and "end_line" not in out:
        try:  # offset/limit (lines) as Claude Code / Qwen Code read files
            n = int(out.pop("limit"))
            start = int(out.get("start_line") or 1)
            if n > 0:
                out["end_line"] = start + n - 1
            renamed.append("limit->end_line")
        except (TypeError, ValueError):
            pass
    if tool == "bash":
        cwd = next((k for k in ("cwd", "workdir", "working_dir", "working_directory", "dir", "directory", "path")
                    if k not in props and isinstance(out.get(k), str)), None)
        if cwd and isinstance(out.get("command"), str):
            d = out.pop(cwd).strip()
            if d not in ("", ".", "./"):
                out["command"] = f"cd {shlex.quote(d)} && {out['command']}"
            renamed.append(f"{cwd}->cd prefix")
        for k, scale in (("timeout_s", 1), ("timeout_seconds", 1), ("timeout_ms", 0.001), ("max_time", 1)):
            if k in out and "timeout" in props and "timeout" not in out:
                try:
                    out["timeout"] = max(1, int(float(out.pop(k)) * scale))
                    renamed.append(f"{k}->timeout")
                except (TypeError, ValueError):
                    pass
    dropped = [k for k in list(out) if k not in props and k.lower() in HARMLESS_EXTRAS]
    for k in dropped:
        out.pop(k)
    if dropped:
        renamed.append(f"ignored {', '.join(dropped)}")
    if tool == "read_file" and "path" in props and not out.get("path"):  # list_directory() with no path
        out["path"] = "."
        renamed.append("path defaulted to .")
    return (out if renamed else args), renamed


# Arguments other agents' tools take that carry no instruction for ours (commentary, UI hints).
HARMLESS_EXTRAS = {"explanation", "reason", "reasoning", "thought", "thoughts", "justification", "rationale",
                   "purpose", "intent", "note", "notes", "title", "description", "is_background", "background",
                   "run_in_background", "requires_approval", "safe_to_auto_run", "risk", "security_risk", "confidence",
                   "task_progress", "status"}

PYTHON_TOOLS = ("python", "python3", "execute_python", "run_python", "python_interpreter", "code_interpreter",
                "ipython", "jupyter", "execute_code", "run_code")
GLOB_TOOLS = ("glob", "find_file", "find_files", "file_search", "glob_search", "find_by_name")
_SEARCH_REPLACE = re.compile(r"<<<<<<< ?SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> ?REPLACE", re.S)


def translate_call(name: str, args: dict[str, Any], specs: dict[str, ToolSpec]) -> tuple[str, dict[str, Any]] | None:
    """Tools other agents offer whose arguments need translation, not renaming."""
    n = _NAME_PREFIX.sub("", name.strip()).lower()
    if n in PYTHON_TOOLS and "bash" in specs:
        code = next((args[k] for k in ("code", "script", "source", "input", "command", "cmd") if isinstance(args.get(k), str)),
                    None)
        if code is None:
            return None
        return "bash", {"command": f"python3 - <<'GHEEREFILL_PY'\n{code}\nGHEEREFILL_PY"}
    if n in GLOB_TOOLS and "bash" in specs:
        pattern = next((args[k] for k in ("pattern", "glob", "file_pattern", "name", "query") if isinstance(args.get(k), str)),
                       None)
        if not pattern:
            return None
        base = next((args[k] for k in ("path", "dir", "directory", "dir_path") if isinstance(args.get(k), str)), "")
        base = base.strip().rstrip("/")
        spec = ":(glob)" + (pattern if "/" in pattern else "**/" + pattern)
        cd = f"cd {shlex.quote(base)} && " if base not in ("", ".") else ""
        return "bash", {"command": f"{cd}git ls-files -co --exclude-standard -- {shlex.quote(spec)} | head -200"}
    if n in ("replace_in_file", "apply_diff") and "edit_file" in specs and isinstance(args.get("diff"), str):
        blocks = _SEARCH_REPLACE.findall(args["diff"])
        path = next((args[k] for k in ("path", "file_path", "file") if isinstance(args.get(k), str)), None)
        if len(blocks) == 1 and path:
            return "edit_file", {"path": path, "old_str": blocks[0][0], "new_str": blocks[0][1]}
    return None


# ------------------------------------------------------------------ lenient argument JSON
_FENCE = re.compile(r"^\s*```(?:json|javascript|js|python)?\s*\n?(.*?)\n?```\s*$", re.S | re.I)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def repair_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """(arguments, error, note). Strict JSON first; then the common near-misses."""
    if isinstance(raw, dict):
        return raw, None, "arguments arrived as an object"
    if raw is None:
        return {}, None, None
    text = str(raw).strip()
    if not text:
        return {}, None, None
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v, None, None
        if isinstance(v, str):  # double-encoded
            v2 = json.loads(v)
            if isinstance(v2, dict):
                return v2, None, "arguments were a JSON string containing JSON; decoded twice"
        return None, f"arguments must be a JSON object, got {type(v).__name__}", None
    except (json.JSONDecodeError, ValueError):
        pass
    m = _FENCE.match(text)
    candidates = [(m.group(1).strip(), "code fence removed")] if m else []
    candidates += [(text, "raw control characters accepted"), (_TRAILING_COMMA.sub(r"\1", text), "trailing comma removed")]
    for cand, note in candidates:
        try:
            v = json.loads(cand, strict=False)
            if isinstance(v, dict):
                return v, None, f"arguments repaired ({note})"
        except (json.JSONDecodeError, ValueError):
            pass
    try:  # the first of several concatenated objects
        v, end = json.JSONDecoder(strict=False).raw_decode(text)
        if isinstance(v, dict):
            return v, None, "arguments repaired (trailing data after the first JSON object ignored)"
    except (json.JSONDecodeError, ValueError):
        pass
    try:  # Python literal: single quotes, True/False/None
        v = ast.literal_eval(text)
        if isinstance(v, dict) and all(isinstance(k, str) for k in v):
            return v, None, "arguments repaired (Python literal)"
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"arguments are not valid JSON ({e.msg} at char {e.pos})", None
    return None, "arguments are not valid JSON", None


def normalize_call(call: ToolCall, specs: dict[str, ToolSpec]) -> tuple[ToolCall, list[str]]:
    """Map name and argument vocabulary onto the offered tools; repair arguments. Never invents a call."""
    notes: list[str] = []
    name = normalize_tool_name(call.name, specs)
    args, err = call.arguments, call.parse_error
    if args is None and call.raw_arguments:
        args, err2, note = repair_arguments(call.raw_arguments)
        if args is not None:
            err = None
            if note:
                notes.append(note)
        else:
            err = err or err2
    if name not in specs and name.lower() in ("str_replace_editor", "str_replace_based_edit_tool", "text_editor") \
            and isinstance(args, dict):
        mapped = str_replace_editor(args)
        if mapped and mapped[0] in specs:
            name, args = mapped
            notes.append(f"`{call.name}` command mapped to `{name}`")
    if call.name not in specs and isinstance(args, dict):
        translated = translate_call(call.name, args, specs)
        if translated:
            name, args = translated
            notes.append(f"`{call.name}` call translated to `{name}`")
    if name != call.name and name in specs and not any("mapped to" in n or "translated to" in n for n in notes):
        notes.append(f"tool name `{call.name}` mapped to `{name}`")
    if name in specs and isinstance(args, dict):
        args, renamed = canonical_arguments(name, args, specs[name])
        if renamed:
            notes.append(f"`{name}` arguments renamed: {', '.join(renamed)}")
    if name == call.name and args is call.arguments and err == call.parse_error:
        return call, notes
    raw = json.dumps(args) if isinstance(args, dict) and args is not call.arguments else call.raw_arguments
    return ToolCall(id=call.id, name=name, arguments=args, raw_arguments=raw, parse_error=err if args is None else None), notes


# ------------------------------------------------------------------ tool calls written as text
_BAR = "[｜|]"  # fullwidth U+FF5C and ASCII
_DSML_INVOKE = re.compile(rf"<{_BAR}DSML{_BAR}\s*invoke\s+name=\"([^\"]+)\"\s*>(.*?)</{_BAR}DSML{_BAR}\s*invoke\s*>", re.S)
_DSML_PARAM = re.compile(rf"<{_BAR}DSML{_BAR}\s*parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>(.*?)"
                         rf"</{_BAR}DSML{_BAR}\s*parameter\s*>", re.S)
_DSML_WRAP = re.compile(rf"</?{_BAR}DSML{_BAR}\s*(?:tool_calls|calls|function_calls)\s*>")
_DSV3_CALL = re.compile(r"<[｜|]tool▁call▁begin[｜|]>(?:function<[｜|]tool▁sep[｜|]>)?\s*([\w.\-]+)\s*(?:<[｜|]tool▁sep[｜|]>)?"
                        r"\s*(?:```(?:json)?\s*)?(\{.*?\})\s*(?:```)?\s*<[｜|]tool▁call▁end[｜|]>", re.S)
_DSV3_WRAP = re.compile(r"<[｜|]tool▁calls?▁(?:begin|end)[｜|]>")
_XML_FUNC = re.compile(r"<function=([A-Za-z0-9_.\-]+)>(.*?)</function>", re.S)
_XML_PARAM = re.compile(r"<parameter=([A-Za-z0-9_\-]+)>(.*?)</parameter>", re.S)
_HERMES = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_TOOL_CALL_TAGS = re.compile(r"</?tool_call>")
_INVOKE = re.compile(r"<invoke\s+name=\"([^\"]+)\"\s*>(.*?)</invoke>", re.S)
_INVOKE_PARAM = re.compile(r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>", re.S)
_FUNCTION_CALLS_TAGS = re.compile(r"</?(?:antml:)?function_calls>")
_BRACKET = re.compile(r"\[tool_call:\s*([A-Za-z0-9_.\-]+)\s*(?:for|with)?\s*(\{.*?\})\s*\]", re.S)
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _strip_nl(s: str) -> str:
    return s[1:] if s.startswith("\n") else s


def _typed(value: str, schema: dict[str, Any], as_json: bool) -> Any:
    v = _strip_nl(value)
    v = v[:-1] if v.endswith("\n") else v
    t = schema.get("type")
    if as_json or t in ("integer", "number", "boolean", "array", "object"):
        try:
            return json.loads(v.strip())
        except (json.JSONDecodeError, ValueError):
            if t == "boolean" and v.strip().lower() in ("true", "false"):
                return v.strip().lower() == "true"
    return v


def _make(name: str, args: dict[str, Any], specs: dict[str, ToolSpec]) -> ToolCall | None:
    tool = normalize_tool_name(name, specs)
    if tool not in specs and name.lower() not in ("str_replace_editor", "str_replace_based_edit_tool"):
        return None
    call = ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args, raw_arguments=json.dumps(args))
    call, _ = normalize_call(call, specs)
    return call if call.name in specs else None


def _json_call(obj: Any, specs: dict[str, ToolSpec]) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # {"type": "function", "function": {...}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or obj.get("function")
    if not isinstance(name, str):
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("args", obj.get("input", {}))))
    if isinstance(args, str):
        args, _, _ = repair_arguments(args)
    return _make(name, args if isinstance(args, dict) else {}, specs)


def recover_text_tool_calls(text: str, specs: dict[str, ToolSpec]) -> tuple[list[ToolCall], str, str | None]:
    """Tool calls written into the reply text: (calls, text without them, dialect). Only offered tools."""
    if not text or not specs:
        return [], text, None
    props = lambda n: specs[normalize_tool_name(n, specs)].parameters.get("properties", {}) \
        if normalize_tool_name(n, specs) in specs else {}  # noqa: E731

    if "DSML" in text:
        calls = []
        for m in _DSML_INVOKE.finditer(text):
            p = props(m.group(1))
            args = {k: _typed(v, p.get(k, {}), s == "false") for k, s, v in _DSML_PARAM.findall(m.group(2))}
            c = _make(m.group(1), args, specs)
            if c:
                calls.append(c)
        if calls:
            rest = _DSML_WRAP.sub("", _DSML_INVOKE.sub("", text))
            return calls, rest.strip(), "deepseek-dsml"
    if "tool▁call" in text:
        calls = []
        for m in _DSV3_CALL.finditer(text):
            args, _, _ = repair_arguments(m.group(2))
            c = _make(m.group(1), args or {}, specs)
            if c:
                calls.append(c)
        if calls:
            return calls, _DSV3_WRAP.sub("", _DSV3_CALL.sub("", text)).strip(), "deepseek-v3-tokens"
    if "<function=" in text:
        calls = []
        for m in _XML_FUNC.finditer(text):
            p = props(m.group(1))
            args = {k: _typed(v, p.get(k, {}), False) for k, v in _XML_PARAM.findall(m.group(2))}
            c = _make(m.group(1), args, specs)
            if c:
                calls.append(c)
        if calls:
            return calls, _TOOL_CALL_TAGS.sub("", _XML_FUNC.sub("", text)).strip(), "qwen-xml"
    if "<tool_call>" in text:
        calls = []
        for m in _HERMES.finditer(text):
            obj, _, _ = repair_arguments(m.group(1))
            c = _json_call(obj, specs)
            if c:
                calls.append(c)
        if calls:
            return calls, _HERMES.sub("", text).strip(), "hermes-json"
    if "<invoke" in text:
        calls = []
        for m in _INVOKE.finditer(text):
            p = props(m.group(1))
            args = {k: _typed(v, p.get(k, {}), False) for k, v in _INVOKE_PARAM.findall(m.group(2))}
            c = _make(m.group(1), args, specs)
            if c:
                calls.append(c)
        if calls:
            return calls, _FUNCTION_CALLS_TAGS.sub("", _INVOKE.sub("", text)).strip(), "invoke-xml"
    if "[tool_call:" in text:
        calls = [c for m in _BRACKET.finditer(text) if (c := _json_call(
            {"name": m.group(1), "arguments": repair_arguments(m.group(2))[0] or {}}, specs))]
        if calls:
            return calls, _BRACKET.sub("", text).strip(), "bracket"
    # a JSON object that is the whole reply, or fenced: {"name": "bash", "arguments": {...}}
    stripped = text.strip()
    blobs = [stripped] if stripped.startswith("{") and stripped.endswith("}") else _FENCED_JSON.findall(text)
    calls = []
    for b in blobs:
        obj, _, _ = repair_arguments(b)
        objs = obj.get("tool_calls") if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list) else [obj]
        for o in objs:
            c = _json_call(o, specs)
            if c:
                calls.append(c)
    if calls:
        rest = "" if stripped.startswith("{") else _FENCED_JSON.sub("", text).strip()
        return calls, rest, "json"
    return [], text, None
