"""Scripted policy (stale edit) for click_help_parameter: moves `help` handling into Parameter. One
edit is written against text the policy itself already changed (stale old_str). The harness must
refuse it without touching the file and show the closest current text; the policy then recovers
using only that hint (it strips the line numbers and edits what the hint shows)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import step, tc, tool_results  # noqa: E402

P = "src/click/core.py"
ARG_ORIGINAL = '''        deprecated = attrs.get("deprecated", False)

        if help:
            help = inspect.cleandoc(help)

        if deprecated:
            label = _format_deprecated_label(deprecated)
            help = f"{help} {label}" if help else label

        self.help = help

        super().__init__(param_decls, required=required, **attrs)
'''
OBSOLETE = {'deprecated = attrs.get("deprecated", False)', "if help:", "help = inspect.cleandoc(help)",
            "if deprecated:", "label = _format_deprecated_label(deprecated)",
            'help = f"{help} {label}" if help else label', "self.help = help"}
PLAN = [
    tc("search", pattern=r"inspect\.cleandoc\(help\)"),
    tc("edit_file", path=P,
       old_str="        deprecated: bool | str = False,\n    ) -> None:\n"
               "        self.name, self.opts, self.secondary_opts = self._parse_decls(",
       new_str="        deprecated: bool | str = False,\n        help: str | None = None,\n    ) -> None:\n"
               "        self.name, self.opts, self.secondary_opts = self._parse_decls("),
    tc("edit_file", path=P,
       old_str="        self._custom_shell_complete = shell_complete\n        self.deprecated = deprecated\n",
       new_str="        self._custom_shell_complete = shell_complete\n        self.deprecated = deprecated\n\n"
               "        if help:\n            help = inspect.cleandoc(help)\n\n        if deprecated:\n"
               "            label = _format_deprecated_label(deprecated)\n"
               '            help = f"{help} {label}" if help else label\n\n        self.help = help\n'),
    tc("edit_file", path=P, old_str='            "envvar": self.envvar,\n        }\n',
       new_str='            "envvar": self.envvar,\n            "help": self.help,\n        }\n'),
    # step 1 on Argument: hand `help` to Parameter
    tc("edit_file", path=P,
       old_str="        self.help = help\n\n        super().__init__(param_decls, required=required, **attrs)\n",
       new_str="        super().__init__(param_decls, required=required, help=help, **attrs)\n"),
    # STALE: written against the Argument block as it was before the previous edit
    tc("edit_file", path=P, old_str=ARG_ORIGINAL,
       new_str="        super().__init__(param_decls, required=required, help=help, **attrs)\n"),
    "RECOVER_FROM_HINT",
    tc("edit_file", path=P,
       old_str="        if help:\n            help = inspect.cleandoc(help)\n\n        super().__init__(\n"
               "            param_decls, type=type, multiple=multiple, deprecated=deprecated, **attrs\n        )\n",
       new_str="        super().__init__(\n            param_decls,\n            type=type,\n            multiple=multiple,\n"
               "            deprecated=deprecated,\n            help=help,\n            **attrs,\n        )\n"),
    tc("edit_file", path=P,
       old_str="        if deprecated:\n            label = _format_deprecated_label(deprecated)\n"
               '            help = f"{help} {label}" if help else label\n\n        self.prompt = prompt_text\n',
       new_str="        self.prompt = prompt_text\n"),
    tc("edit_file", path=P,
       old_str="        self.allow_from_autoenv = allow_from_autoenv\n        self.help = help\n",
       new_str="        self.allow_from_autoenv = allow_from_autoenv\n"),
    tc("bash", command="python -m pytest -q -p no:cacheprovider tests/test_info_dict.py tests/test_options.py "
                       "tests/test_arguments.py tests/test_basic.py"),
]


def hint_text(result: str) -> str | None:
    m = re.search(r"it reads exactly:\n(.*?)\nCopy old_str from it exactly", result, re.S)
    if not m:
        return None
    return "".join(re.sub(r"^\s*\d+\t", "", l) + "\n" for l in m.group(1).split("\n"))


def recover(messages):
    results = tool_results(messages)
    current = hint_text(results[-1]) if results and "old_str not found" in results[-1] else None
    if current is None:  # the harness gave no usable hint: nothing to recover from (recorded by the check)
        return {"content": "no hint", "tool_calls": [tc("read_file", path=P, start_line=3740, end_line=3780)]}
    kept, blank = [], False
    for line in current.split("\n")[:-1]:
        if line.strip() in OBSOLETE:
            continue
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        kept.append(line)
    return {"content": "re-applying against the current text",
            "tool_calls": [tc("edit_file", path=P, old_str=current, new_str="\n".join(kept) + "\n")]}


def respond(messages, tools):
    i = step(messages)
    if i < len(PLAN):
        turn = PLAN[i]
        if turn == "RECOVER_FROM_HINT":
            return recover(messages)
        return {"content": f"step {i + 1}", "tool_calls": [turn]}
    return {"content": "done", "tool_calls": [tc("submit", summary="help handling moved into Parameter")]}
