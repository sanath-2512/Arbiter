"""Token economy of tool output: noise is folded before the model sees it, never the signal."""

import re
import shutil
import unittest

from gheerefill.config import ToolsConfig
from gheerefill.models.base import ToolCall
from gheerefill.outputs import OutputArchive, bounded_view
from gheerefill.records import Redactor
from gheerefill.shell import tool_environment
from gheerefill.tools import BUILD_COMMAND, ToolBox
from tests.helpers import TempDirCase

CARGO = "\n".join(
    ["    Updating crates.io index"] + [f"   Compiling dep{i} v0.1.{i}" for i in range(40)]
    + ["    Finished `test` profile [unoptimized + debuginfo] target(s) in 12.3s",
       "     Running unittests src/lib.rs (target/debug/deps/calc-abc)", "", "running 60 tests"]
    + [f"test tests::t{i} ... ok" for i in range(59)]
    + ["test tests::halves ... FAILED", "", "failures:", "", "---- tests::halves stdout ----",
       "thread 'tests::halves' panicked at src/lib.rs:9:9:", "assertion `left == right` failed", "  left: 3.0",
       " right: 3.5", "", "failures:", "    tests::halves", "",
       "test result: FAILED. 59 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s"])


def warning(i):
    return [f"warning: unused variable: `x{i}`", f" --> src/lib.rs:{i}:9", "  |",
            f"{i} |     let x{i} = 1;", "  |         ^^ help: prefix it with an underscore", ""]


class FoldTest(unittest.TestCase):
    def test_cargo_progress_and_passing_lines_fold_failures_stay(self):
        view, truncated = bounded_view(CARGO, 12000, "o7")
        self.assertTrue(truncated)
        self.assertLess(len(view), len(CARGO) / 3)
        for kept in ("test tests::halves ... FAILED", "panicked at src/lib.rs:9:9", "  left: 3.0", " right: 3.5",
                     "test result: FAILED. 59 passed; 1 failed", "running 60 tests", "Finished `test` profile"):
            self.assertIn(kept, view)
        self.assertNotIn("Compiling dep3 ", view)
        self.assertNotIn("test tests::t5 ... ok", view)
        # markers name the original line numbers, so the folded text can be fetched exactly
        m = re.search(r"(\d+) lines folded: passing test lines \(lines (\d+)-(\d+)\)", view)
        lines = CARGO.split("\n")
        a, b = int(m.group(2)), int(m.group(3))
        self.assertEqual(lines[a - 1], "test tests::t0 ... ok")
        self.assertEqual(lines[b - 1], "test tests::t58 ... ok")

    def test_other_runners(self):
        pytest_v = "\n".join([f"tests/test_a.py::test_{i} PASSED [ {i}%]" for i in range(30)]
                             + ["tests/test_a.py::test_bad FAILED [100%]", "E   assert 3 == 3.5"])
        view, _ = bounded_view(pytest_v, 12000, "o1")
        self.assertIn("test_bad FAILED", view)
        self.assertIn("E   assert 3 == 3.5", view)
        self.assertNotIn("test_7 PASSED", view)
        go_v = "\n".join(sum([[f"=== RUN   TestOk{i}", f"--- PASS: TestOk{i} (0.00s)"] for i in range(10)], [])
                         + ["=== RUN   TestBad", "    calc_test.go:9: got 3, want 3.5", "--- FAIL: TestBad (0.00s)",
                            "FAIL"])
        view, _ = bounded_view(go_v, 12000, "o2")
        self.assertIn("--- FAIL: TestBad", view)
        self.assertIn("got 3, want 3.5", view)
        self.assertNotIn("TestOk4", view)

    def test_warnings_beyond_the_first_two_fold_errors_never(self):
        text = "\n".join(sum((warning(i) for i in range(1, 9)), [])
                         + ["error[E0308]: mismatched types", " --> src/lib.rs:20:5", "", "warning: `calc` (lib) "
                            "generated 8 warnings", "error: could not compile `calc`"])
        view, _ = bounded_view(text, 12000, "o3")
        self.assertIn("unused variable: `x1`", view)
        self.assertIn("unused variable: `x2`", view)
        self.assertNotIn("unused variable: `x5`", view)
        self.assertIn("6 more compiler warnings", view)
        self.assertIn("error[E0308]: mismatched types", view)
        self.assertIn("generated 8 warnings", view)

    def test_short_or_plain_output_is_untouched(self):
        text = "\n".join(f"test t{i} ... ok" for i in range(5)) + "\nall good"
        self.assertEqual(bounded_view(text, 12000, "o4"), (text, False))

    def test_build_commands_get_a_longer_default_timeout(self):
        for c in ("cargo test", "cd crates/x && cargo test -p x", "go test ./...", "./gradlew test", "mvn -q test"):
            self.assertTrue(BUILD_COMMAND.search(c), c)
        for c in ("python -m pytest", "ls", "echo cargo"):
            self.assertFalse(BUILD_COMMAND.search(c), c)


class ExternalSyntaxGuardTest(TempDirCase):
    def box(self):
        return ToolBox(self.tmp / "r", self.tmp / "scratch", OutputArchive(self.tmp / "out", Redactor([])), ToolsConfig(),
                       tool_environment(dict(__import__("os").environ), self.tmp / "scratch", ("AI_API_KEY",)))

    def edit(self, box, path, old, new):
        return box.execute(ToolCall(id="1", name="edit_file", arguments={"path": path, "old_str": old, "new_str": new},
                                    raw_arguments="{}"))

    def check(self, tool, name, src, old, bad, good):
        if shutil.which(tool) is None:
            self.skipTest(f"{tool} not installed")
        (self.tmp / "r").mkdir(exist_ok=True)
        (self.tmp / "r" / name).write_text(src)
        box = self.box()
        res = self.edit(box, name, old, bad)
        self.assertEqual(res.status, "error", res.content)
        self.assertIn("unparseable", res.content)
        self.assertEqual((self.tmp / "r" / name).read_text(), src)
        res = self.edit(box, name, old, good)
        self.assertEqual(res.status, "ok", res.content)

    def test_rust(self):
        (self.tmp / "r").mkdir()
        (self.tmp / "r" / "Cargo.toml").write_text('[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n')
        self.check("rustfmt", "lib.rs", "pub fn f(a: i64) -> i64 {\n    a / 2\n}\n", "    a / 2\n}", "    a / 2.0\n",
                   "    a * 2\n}")

    def test_go_js_ruby(self):
        self.check("gofmt", "f.go", "package x\n\nfunc F() int {\n\treturn 1\n}\n", "\treturn 1\n}", "\treturn 1\n",
                   "\treturn 2\n}")
        self.check("node", "f.js", "function f() {\n  return 1;\n}\n", "  return 1;\n}", "  return (1;\n}",
                   "  return 2;\n}")
        self.check("ruby", "f.rb", "def f\n  1\nend\n", "  1\nend", "  1\n", "  2\nend")


if __name__ == "__main__":
    unittest.main()
