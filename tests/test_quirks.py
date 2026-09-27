"""Model-family output quirks (Qwen, DeepSeek and the servers in front of them)."""

import json
import subprocess
import unittest

from arbiter.models import quirks
from arbiter.models.quirks import normalize_call
from arbiter.models.base import ToolCall
from arbiter.tools import tool_specs
from arbiter.config import ToolsConfig

SPECS = {s.name: s for s in tool_specs(ToolsConfig())}


def names(calls):
    return [c.name for c in calls]


class ThinkTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(quirks.split_think("<think>plan it</think>\nDone."), ("plan it", "Done."))
        self.assertEqual(quirks.split_think("plan it</think>Done."), ("plan it", "Done."))  # opened by the template
        self.assertEqual(quirks.split_think("<think>still thinking when cut"), ("still thinking when cut", ""))
        self.assertEqual(quirks.split_think("no tags here"), ("", "no tags here"))
        self.assertEqual(quirks.split_think("<thinking>a</thinking>b"), ("a", "b"))


class TextToolCallTest(unittest.TestCase):
    def test_deepseek_v4_dsml(self):
        text = ('Let me look.\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name="bash">\n'
                '<｜DSML｜parameter name="command" string="true">ls -la src</｜DSML｜parameter>\n'
                '<｜DSML｜parameter name="timeout" string="false">30</｜DSML｜parameter>\n'
                '</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
        calls, rest, dialect = quirks.recover_text_tool_calls(text, SPECS)
        self.assertEqual(dialect, "deepseek-dsml")
        self.assertEqual(names(calls), ["bash"])
        self.assertEqual(calls[0].arguments, {"command": "ls -la src", "timeout": 30})
        self.assertEqual(rest, "Let me look.")

    def test_deepseek_v41_spaced_dsml_and_ascii_bars(self):
        text = ('<｜DSML｜ calls><｜DSML｜ invoke name="read_file"><｜DSML｜ parameter name="path" string="true">'
                'a.py</｜DSML｜ parameter></｜DSML｜ invoke></｜DSML｜ calls>'
                '<|DSML|invoke name="search"><|DSML|parameter name="pattern" string="true">def f</|DSML|parameter>'
                '</|DSML|invoke>')
        calls, rest, _ = quirks.recover_text_tool_calls(text, SPECS)
        self.assertEqual([(c.name, c.arguments) for c in calls],
                         [("read_file", {"path": "a.py"}), ("search", {"pattern": "def f"})])
        self.assertEqual(rest, "")

    def test_deepseek_v3_tokens(self):
        text = ('<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>bash\n```json\n'
                '{"command": "pytest -q"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>')
        calls, rest, dialect = quirks.recover_text_tool_calls(text, SPECS)
        self.assertEqual((dialect, names(calls), calls[0].arguments), ("deepseek-v3-tokens", ["bash"], {"command": "pytest -q"}))

    def test_qwen_coder_xml_multiline_value(self):
        text = ("I'll fix it.\n<tool_call>\n<function=edit_file>\n<parameter=path>\ncalc/ops.py\n</parameter>\n"
                "<parameter=old_str>\n    return a // b\n</parameter>\n<parameter=new_str>\n    return a / b\n"
                "</parameter>\n</function>\n</tool_call>")
        calls, rest, dialect = quirks.recover_text_tool_calls(text, SPECS)
        self.assertEqual(dialect, "qwen-xml")
        self.assertEqual(calls[0].arguments, {"path": "calc/ops.py", "old_str": "    return a // b",
                                              "new_str": "    return a / b"})
        self.assertEqual(rest, "I'll fix it.")

    def test_hermes_json_with_string_arguments(self):
        text = '<tool_call>\n{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}\n</tool_call>'
        calls, _, dialect = quirks.recover_text_tool_calls(text, SPECS)
        self.assertEqual((dialect, calls[0].arguments), ("hermes-json", {"command": "ls"}))

    def test_invoke_xml_and_bare_json_and_fenced_json(self):
        c1, _, d1 = quirks.recover_text_tool_calls(
            '<function_calls><invoke name="bash"><parameter name="command">make test</parameter></invoke>'
            '</function_calls>', SPECS)
        self.assertEqual((d1, c1[0].arguments), ("invoke-xml", {"command": "make test"}))
        c2, rest, d2 = quirks.recover_text_tool_calls('{"name": "submit", "arguments": {"summary": "done"}}', SPECS)
        self.assertEqual((d2, names(c2), rest), ("json", ["submit"], ""))
        c3, rest, _ = quirks.recover_text_tool_calls('Running:\n```json\n{"tool": "bash", "input": {"cmd": "ls"}}\n```',
                                                     SPECS)
        self.assertEqual((names(c3), c3[0].arguments, rest), (["bash"], {"command": "ls"}, "Running:"))

    def test_only_offered_tools_and_not_prose(self):
        for text in ('<tool_call>{"name": "rm_rf_everything", "arguments": {}}</tool_call>',
                     'Here is the JSON config: {"name": "project", "version": 2}',
                     "Use `<function=foo>` syntax in your template.",
                     "The fix: replace `a // b` with `a / b`."):
            calls, rest, dialect = quirks.recover_text_tool_calls(text, SPECS)
            self.assertEqual((calls, rest, dialect), ([], text, None), text)


class NormalizeTest(unittest.TestCase):
    def call(self, name, raw):
        return ToolCall(id="c1", name=name, arguments=None, raw_arguments=raw, parse_error="x")

    def test_names(self):
        for given, want in (("functions.bash", "bash"), ("Bash", "bash"), ("execute_bash", "bash"),
                            ("read-file", "read_file"), ("finish", "submit"), ("grep", "search"),
                            ("default_api:edit_file", "edit_file"), ("nonsense_tool", "nonsense_tool")):
            self.assertEqual(quirks.normalize_tool_name(given, SPECS), want, given)

    def test_str_replace_editor_is_mapped(self):
        c, notes = quirks.normalize_call(self.call("str_replace_editor", json.dumps(
            {"command": "str_replace", "path": "a.py", "old_str": "x", "new_str": "y"})), SPECS)
        self.assertEqual((c.name, c.arguments, c.parse_error), ("edit_file", {"path": "a.py", "old_str": "x", "new_str": "y"}, None))
        c, _ = quirks.normalize_call(self.call("str_replace_editor", json.dumps(
            {"command": "view", "path": "a.py", "view_range": [10, 20]})), SPECS)
        self.assertEqual((c.name, c.arguments), ("read_file", {"path": "a.py", "start_line": 10, "end_line": 20}))
        c, _ = quirks.normalize_call(self.call("str_replace_editor", json.dumps(
            {"command": "create", "path": "n.py", "file_text": "x = 1\n"})), SPECS)
        self.assertEqual((c.name, c.arguments), ("write_file", {"path": "n.py", "content": "x = 1\n"}))

    def test_argument_aliases(self):
        c, notes = quirks.normalize_call(self.call("edit_file", json.dumps(
            {"file_path": "a.py", "old_string": "x", "new_string": "y"})), SPECS)
        self.assertEqual(c.arguments, {"path": "a.py", "old_str": "x", "new_str": "y"})
        self.assertTrue(any("renamed" in n for n in notes))
        c, _ = quirks.normalize_call(self.call("bash", json.dumps({"cmd": ["ls", "-la"]})), SPECS)
        self.assertEqual(c.arguments, {"command": "ls -la"})

    def test_argument_repairs(self):
        cases = {
            '{"command": "ls",}': {"command": "ls"},
            "{'command': 'ls', 'timeout': None}": {"command": "ls", "timeout": None},
            '"{\\"command\\": \\"ls\\"}"': {"command": "ls"},
            '```json\n{"command": "ls"}\n```': {"command": "ls"},
            '{"command": "echo a\nb"}': {"command": "echo a\nb"},
            '{"command": "ls"}{"command": "pwd"}': {"command": "ls"},
            "": {},
        }
        for raw, want in cases.items():
            args, err, _ = quirks.repair_arguments(raw)
            self.assertEqual((args, err), (want, None), raw)
        args, err, _ = quirks.repair_arguments('{"command": ')
        self.assertIsNone(args)
        self.assertIn("not valid JSON", err)

    def test_valid_call_is_untouched(self):
        c = ToolCall(id="c1", name="bash", arguments={"command": "ls"}, raw_arguments='{"command": "ls"}')
        c2, notes = quirks.normalize_call(c, SPECS)
        self.assertIs(c2, c)
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()


class ProviderErrorTest(unittest.TestCase):
    def test_classification(self):
        from arbiter.models.base import ErrorClass, classify_http_error, output_token_limit

        def cls(status, body):
            return classify_http_error(status, json.dumps(body)).cls

        self.assertEqual(cls(400, {"error": {"code": "data_inspection_failed", "message": "Input data may contain "
                                             "inappropriate content.", "type": "data_inspection_failed"}}),
                         ErrorClass.CONTENT_FILTER)
        self.assertEqual(cls(400, {"code": "DataInspectionFailed", "message": "Input text data may contain "
                                   "inappropriate content.", "request_id": "x"}), ErrorClass.CONTENT_FILTER)
        self.assertEqual(cls(400, {"error": {"message": "Content Exists Risk"}}), ErrorClass.CONTENT_FILTER)
        self.assertEqual(cls(400, {"error": {"message": "<400> InternalError.Algo.InvalidParameter: Range of input "
                                             "length should be [1, 98304]"}}), ErrorClass.CONTEXT_OVERFLOW)
        self.assertEqual(cls(402, {"error": {"message": "Insufficient Balance"}}), ErrorClass.QUOTA)
        self.assertEqual(cls(401, {"error": {"message": "Authentication Fails, Your api key: ****abcd is invalid"}}),
                         ErrorClass.AUTH)
        self.assertEqual(output_token_limit("Invalid max_tokens value, the valid range of max_tokens is [1, 8192]",
                                            32768), 8192)

    def test_adaptations(self):
        from arbiter.config import ModelConfig
        from arbiter.models.base import ErrorClass, ModelError
        from arbiter.models.openai_chat import OpenAIChatClient

        c = OpenAIChatClient(ModelConfig(name="m", base_url="http://x/v1"), "k")
        passback = ModelError(ErrorClass.MALFORMED_REQUEST,
                              "invalid_request_error The `reasoning_content` in the thinking mode must be passed back to the API.")
        self.assertIn("all of them", c.adapt(passback))
        self.assertEqual(c.cfg.reasoning_passback, "all")
        self.assertIn("thinking mode disabled", c.adapt(passback))
        self.assertEqual(c.cfg.extra_body["thinking"], {"type": "disabled"})
        c2 = OpenAIChatClient(ModelConfig(name="m", base_url="http://x/v1"), "k")
        self.assertIn("no longer sent", c2.adapt(ModelError(ErrorClass.UNSUPPORTED, "Extra inputs are not permitted: "
                                                                                    "messages.1.reasoning_content")))
        self.assertIn("streaming", c2.adapt(ModelError(ErrorClass.MALFORMED_REQUEST, "This model only support stream "
                                                                                    "mode, please enable the stream parameter.")))
        self.assertTrue(c2.cfg.stream)

    def test_rendering_rules(self):
        from arbiter.config import ModelConfig
        from arbiter.models.openai_chat import OpenAIChatClient

        c = OpenAIChatClient(ModelConfig(name="m", base_url="http://x/v1"), "k")
        msgs = c.render_messages([
            {"role": "assistant", "content": "", "reasoning": "r1", "tool_calls": [{"id": "a", "name": "bash", "arguments": "{}"}]},
            {"role": "tool", "tool_call_id": "a", "content": ""},
            {"role": "assistant", "content": "", "reasoning": "r2", "tool_calls": []},
        ])
        self.assertEqual((msgs[0]["content"], msgs[0]["reasoning_content"]), (None, "r1"))  # null next to calls
        self.assertEqual(msgs[1]["content"], "(no output)")
        self.assertEqual(msgs[2]["content"], "(no reply)")
        self.assertNotIn("reasoning_content", msgs[2])  # only tool-call turns under "auto"

    def test_reasoning_and_cache_fields_are_read(self):
        from arbiter.config import ModelConfig
        from arbiter.models.openai_chat import OpenAIChatClient, accumulate_openai_stream

        c = OpenAIChatClient(ModelConfig(name="m", base_url="http://x/v1"), "k")
        t = c.parse_response({"choices": [{"message": {"content": "x", "reasoning_content": "because"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80,
                                        "prompt_cache_miss_tokens": 20}})
        self.assertEqual((t.reasoning, t.usage.cache_read_tokens, t.usage.input_tokens), ("because", 80, 20))
        data = accumulate_openai_stream(iter([
            ("chunk", {"choices": [{"delta": {"reasoning_content": "be"}}]}),
            ("chunk", {"choices": [{"delta": {"reasoning_content": "cause"}}]}),
            ("chunk", {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}),
            ("done", None)]))
        self.assertEqual(data["choices"][0]["message"]["reasoning_content"], "because")


class ForeignToolVocabularyTest(unittest.TestCase):
    """Calls written for other agents' tool sets (Qwen Code, Claude Code, Codex, Cline, OpenHands, a
    Python interpreter tool) reach the offered tools with their meaning intact."""

    def setUp(self):
        self.specs = {s.name: s for s in tool_specs(ToolsConfig())}

    def norm(self, name, args):
        call = ToolCall(id="c1", name=name, arguments=args, raw_arguments=json.dumps(args))
        out, notes = normalize_call(call, self.specs)
        return out.name, out.arguments, notes

    def test_python_tool_becomes_a_heredoc(self):
        name, args, _ = self.norm("python", {"code": "import sys\nprint(sys.version_info[0] + 1)"})
        self.assertEqual(name, "bash")
        out = subprocess.run(["bash", "-c", args["command"]], capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "4")

    def test_codex_argv_and_plain_argv(self):
        self.assertEqual(self.norm("shell", {"command": ["bash", "-lc", "pytest -x 'a b'"]})[1]["command"],
                         "pytest -x 'a b'")
        self.assertEqual(self.norm("shell", {"command": ["grep", "-rn", "a b", "src"]})[1]["command"],
                         "grep -rn 'a b' src")

    def test_listing_glob_and_line_windows(self):
        self.assertEqual(self.norm("list_directory", {"dir_path": "src"})[:2], ("read_file", {"path": "src"}))
        self.assertEqual(self.norm("LS", {})[:2], ("read_file", {"path": "."}))
        name, args, _ = self.norm("Glob", {"pattern": "*.py"})
        self.assertEqual((name, args["command"]), ("bash", "git ls-files -co --exclude-standard -- ':(glob)**/*.py' | head -200"))
        self.assertIn("cd /abs/src && ", self.norm("glob", {"pattern": "**/*.ts", "path": "/abs/src/"})[1]["command"])
        self.assertEqual(self.norm("Read", {"file_path": "a.py", "offset": 10, "limit": 5})[1],
                         {"path": "a.py", "start_line": 10, "end_line": 14})
        self.assertEqual(self.norm("run_shell_command", {"command": "ls"})[0], "bash")
        self.assertEqual(self.norm("search_file_content", {"pattern": "x"})[0], "search")
        self.assertEqual(self.norm("write_to_file", {"path": "a", "content": "b"})[0], "write_file")

    def test_cline_search_replace_block(self):
        diff = "<<<<<<< SEARCH\n    return a // b\n=======\n    return a / b\n>>>>>>> REPLACE"
        self.assertEqual(self.norm("replace_in_file", {"path": "calc/ops.py", "diff": diff})[:2],
                         ("edit_file", {"path": "calc/ops.py", "old_str": "    return a // b", "new_str": "    return a / b"}))

    def test_extra_arguments_other_agents_send(self):
        name, args, notes = self.norm("execute_command", {"command": "pytest -q", "requires_approval": False,
                                                          "explanation": "run tests", "cwd": "pkg/sub dir"})
        self.assertEqual((name, args), ("bash", {"command": "cd 'pkg/sub dir' && pytest -q"}))
        self.assertEqual(self.norm("bash", {"command": "make", "timeout_ms": 120000})[1], {"command": "make", "timeout": 120})
        self.assertEqual(self.norm("read_file", {"path": "a.py", "thought": "look"})[1], {"path": "a.py"})
        # an argument that would change the meaning is not dropped silently
        self.assertIn("mystery", self.norm("read_file", {"path": "a.py", "mystery": 1})[1])
