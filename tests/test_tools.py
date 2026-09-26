import os
import time

from gheerefill.config import ToolsConfig
from gheerefill.models.base import ToolCall
from gheerefill.outputs import OutputArchive, bounded_view
from gheerefill.records import Redactor
from gheerefill.shell import tool_environment
from gheerefill.tools import ToolBox
from tests.helpers import TempDirCase


def call(name, raw=None, **args):
    import json

    return ToolCall(id="c", name=name, arguments=None if raw is not None else args,
                    raw_arguments=raw if raw is not None else json.dumps(args),
                    parse_error="bad json" if raw is not None else None)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombie processes count as dead
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False


class ToolsTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.cfg = ToolsConfig(max_observation_chars=2000, read_max_lines=50, bash_timeout_s=20)
        self.env = tool_environment({**os.environ, "AI_API_KEY": "sk-live-abcdef123", "OTHER_TOKEN": "t0k3n-value",
                                     "MY_SERVICE_API_KEY": "zzz"}, self.tmp / "scratch", ("AI_API_KEY",))
        self.archive = OutputArchive(self.tmp / "outputs", Redactor(["sk-live-abcdef123"]))
        self.box = ToolBox(self.repo, self.tmp / "scratch", self.archive, self.cfg, self.env)

    # ---- edit_file
    def test_edit_unique_ambiguous_missing(self):
        f = self.repo / "a b ü.py"
        f.write_text("x = 1\ny = 1\nx = 1\n")
        r = self.box.execute(call("edit_file", path="a b ü.py", old_str="x = 1", new_str="x = 2"))
        self.assertEqual(r.status, "error")
        self.assertIn("occurs 2 times", r.content)
        self.assertIn("lines 1, 3", r.content)
        self.assertEqual(f.read_text(), "x = 1\ny = 1\nx = 1\n")  # unchanged
        r = self.box.execute(call("edit_file", path="a b ü.py", old_str="y = 1\nx = 1", new_str="y = 5\nx = 1"))
        self.assertEqual(r.status, "ok", r.content)
        self.assertEqual(f.read_text(), "x = 1\ny = 5\nx = 1\n")
        r = self.box.execute(call("edit_file", path="a b ü.py", old_str="  y = 5", new_str="z"))
        self.assertEqual(r.status, "error")
        self.assertIn("line(s) 2", r.content)  # whitespace-insensitive hint, but no guessing
        self.assertEqual(f.read_text(), "x = 1\ny = 5\nx = 1\n")
        r = self.box.execute(call("edit_file", path="a b ü.py", old_str="x = 1", new_str="x = 9", replace_all=True))
        self.assertEqual(f.read_text(), "x = 9\ny = 5\nx = 9\n")

    def test_edit_stale_context_fails_cleanly(self):
        f = self.repo / "m.py"
        f.write_text("def f():\n    return 1\n")
        f.write_text("def f():\n    return 2\n")  # changed since the model last read it
        r = self.box.execute(call("edit_file", path="m.py", old_str="    return 1", new_str="    return 3"))
        self.assertEqual(r.status, "error")
        self.assertEqual(f.read_text(), "def f():\n    return 2\n")

    def test_edit_crlf_adaptation_and_non_utf8(self):
        f = self.repo / "w.txt"
        f.write_bytes(b"a\r\nb\r\nc\r\n")
        r = self.box.execute(call("edit_file", path="w.txt", old_str="a\nb", new_str="a\nB"))
        self.assertEqual(r.status, "ok", r.content)
        self.assertIn("CRLF", r.content)
        self.assertEqual(f.read_bytes(), b"a\r\nB\r\nc\r\n")
        (self.repo / "bin").write_bytes(b"\xff\xfe\x00x")
        r = self.box.execute(call("edit_file", path="bin", old_str="x", new_str="y"))
        self.assertIn("not valid UTF-8", r.content)

    def test_edit_preserves_mode(self):
        f = self.repo / "run.sh"
        f.write_text("echo a\n")
        os.chmod(f, 0o755)
        self.box.execute(call("edit_file", path="run.sh", old_str="echo a", new_str="echo b"))
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o755)

    # ---- write/read and path safety
    def test_write_read_multiline_unicode(self):
        content = "line 1\n  ünïcode ✓\n\ttab\n"
        r = self.box.execute(call("write_file", path="new dir/f ü.txt", content=content))
        self.assertEqual(r.status, "ok")
        self.assertEqual((self.repo / "new dir" / "f ü.txt").read_text(), content)
        r = self.box.execute(call("read_file", path="new dir/f ü.txt"))
        self.assertIn("     2\t  ünïcode ✓", r.content)
        self.assertIn("(3 lines)", r.content)

    def test_write_outside_repo_and_into_git_refused(self):
        (self.repo / ".git").mkdir()
        os.symlink(self.tmp, self.repo / "escape")
        for p in ("../x.txt", str(self.tmp / "x.txt"), ".git/config", "escape/x.txt"):
            r = self.box.execute(call("write_file", path=p, content="x"))
            self.assertEqual(r.status, "error", p)
        self.assertFalse((self.tmp / "x.txt").exists())
        r = self.box.execute(call("write_file", path=str(self.tmp / "scratch" / "repro.py"), content="print(1)\n"))
        self.assertEqual(r.status, "ok")  # the scratch directory is writable

    def test_read_ranges_and_truncation_notice(self):
        (self.repo / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 201)))
        r = self.box.execute(call("read_file", path="big.py"))
        self.assertIn("lines 1-50 of 200", r.content)
        self.assertIn("start_line=51", r.content)
        r = self.box.execute(call("read_file", path="big.py", start_line=190, end_line=195))
        self.assertIn("   190\tline 190", r.content)
        self.assertNotIn("line 196", r.content)
        r = self.box.execute(call("read_file", path="big.py", start_line=500))
        self.assertIn("beyond the end", r.content)
        r = self.box.execute(call("read_file", path="."))
        self.assertIn("big.py", r.content)

    # ---- argument validation
    def test_argument_validation(self):
        r = self.box.execute(call("bash"))
        self.assertIn("missing required argument(s): command", r.content)
        r = self.box.execute(call("bash", command="echo hi", cwd="/"))
        self.assertIn("unknown argument(s): cwd", r.content)
        r = self.box.execute(call("bash", raw='{"command": "echo hi"'))
        self.assertIn("Nothing was executed", r.content)
        r = self.box.execute(call("bash", command="echo ok", timeout="5"))
        self.assertEqual(r.status, "ok")  # predefined integer normalisation
        r = self.box.execute(call("launch_rockets"))
        self.assertIn("unknown tool", r.content)

    # ---- bash
    def test_oversized_output_exact_retrieval(self):
        cmd = ("for i in $(seq 1 3000); do echo \"row $i\"; done; echo 'AssertionError: expected 3 got 4' ; "
               "for i in $(seq 1 3000); do echo \"tail $i\"; done")
        r = self.box.execute(call("bash", command=cmd))
        self.assertLessEqual(len(r.content), 2600)
        self.assertIn("AssertionError: expected 3 got 4", r.content)  # salient middle line kept
        self.assertIn("omitted (lines", r.content)
        oid = r.meta["output_id"]
        r2 = self.box.execute(call("read_output", id=oid, start_line=2995, end_line=3002))
        self.assertIn("  2995\trow 2995", r2.content)
        self.assertIn("  3001\tAssertionError: expected 3 got 4", r2.content)
        self.assertIn("  3002\ttail 1", r2.content)
        full = self.archive.read_text(oid)
        self.assertEqual(full.count("\n"), 6001)

    def test_timeout_kills_descendants_and_reports_partial_mutation(self):
        pidfile = self.repo / "pid"
        cmd = f"echo changed > f.txt; (sleep 300 & echo $! > {pidfile}); sleep 300"
        t0 = time.monotonic()
        r = self.box.execute(call("bash", command=cmd, timeout=1))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(r.status, "timeout")
        self.assertIn("Files may have been changed", r.content)
        self.assertEqual((self.repo / "f.txt").read_text(), "changed\n")
        time.sleep(0.2)
        self.assertFalse(pid_alive(int(pidfile.read_text())))

    def test_background_process_left_by_command_is_stopped(self):
        pidfile = self.repo / "bg.pid"
        r = self.box.execute(call("bash", command=f"sleep 300 & echo $! > {pidfile}; echo started"))
        self.assertEqual(r.status, "ok")
        self.assertIn("background processes", r.content)
        time.sleep(0.2)
        self.assertFalse(pid_alive(int(pidfile.read_text())))

    def test_secrets_not_in_tool_environment_and_redacted(self):
        r = self.box.execute(call("bash", command="env"))
        self.assertNotIn("sk-live-abcdef123", r.content)
        self.assertNotIn("AI_API_KEY", r.content)
        self.assertNotIn("t0k3n-value", r.content)
        self.assertNotIn("MY_SERVICE_API_KEY", r.content)
        self.assertIn("PATH=", r.content)
        r = self.box.execute(call("bash", command="echo leaked sk-live-abcdef123"))
        self.assertIn("[REDACTED]", r.content)
        self.assertNotIn("sk-live-abcdef123", self.archive.read_text(r.meta["output_id"]))

    def test_runaway_output_is_capped(self):
        box = ToolBox(self.repo, self.tmp / "scratch", self.archive,
                      ToolsConfig(max_output_bytes=200_000, bash_timeout_s=20), self.env)
        r = box.execute(call("bash", command="yes 'spam spam spam'"))
        self.assertEqual(r.status, "output_limit")
        self.assertLess(r.meta["output_bytes"], 5_000_000)

    def test_cancel_stops_command(self):
        flag = {"v": False}
        box = ToolBox(self.repo, self.tmp / "scratch", self.archive, self.cfg, self.env, should_cancel=lambda: flag["v"])
        import threading

        threading.Timer(0.5, lambda: flag.__setitem__("v", True)).start()
        t0 = time.monotonic()
        r = box.execute(call("bash", command="sleep 30"))
        self.assertEqual(r.status, "cancelled")
        self.assertLess(time.monotonic() - t0, 5)

    def test_time_budget_caps_timeout(self):
        box = ToolBox(self.repo, self.tmp / "scratch", self.archive, self.cfg, self.env, time_budget=lambda: 1.0)
        r = box.execute(call("bash", command="sleep 5", timeout=100))
        self.assertEqual(r.status, "timeout")
        self.assertEqual(r.meta["timeout_s"], 1.0)
        box = ToolBox(self.repo, self.tmp / "scratch", self.archive, self.cfg, self.env, time_budget=lambda: 0.0)
        self.assertIn("no time budget", box.execute(call("bash", command="true")).content)

    # ---- search
    def test_search_rg_and_grep_fallback(self):
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "mod.py").write_text("def target_fn():\n    pass\n")
        (self.repo / "notes.md").write_text("target_fn mentioned\n")
        for use_rg in (True, False):
            if not use_rg:
                self.box._rg = None
            r = self.box.execute(call("search", pattern="target_fn", glob="*.py"))
            self.assertIn("pkg/mod.py:1:def target_fn", r.content, use_rg)
            self.assertNotIn("notes.md", r.content)
            r = self.box.execute(call("search", pattern="nomatch_xyz"))
            self.assertIn("No matches", r.content)
            r = self.box.execute(call("search", pattern="(unclosed"))
            self.assertEqual(r.status, "error", use_rg)


class BoundedViewTest(TempDirCase):
    def test_short_output_unchanged_and_long_line_capped(self):
        self.assertEqual(bounded_view("a\nb\n", 100, "o1"), ("a\nb", False))
        view, trunc = bounded_view("x" * 5000, 10000, "o1")
        self.assertTrue(trunc)
        self.assertIn('read_output(id="o1", start_line=1, end_line=1)', view)
