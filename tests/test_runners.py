"""Test-runner output across ecosystems. Most fixtures are real output captured from the toolchains
(cargo, go, node --test, jest, mocha, vitest, minitest, pytest); the rest are the runners' documented
formats. A misread runner turns a failing test run into "inconclusive" or worse into a pass."""

import unittest
from pathlib import Path

from arbiter.evidence import classify_output
from arbiter.proof import failing_tests

FIX = Path(__file__).parent / "fixtures" / "runners"
EXPECT = {  # file: (runner, outcome, failing test names)
    "cargo_fail": ("cargo", "failed", ["tests::half"]),
    "cargo_pass": ("cargo", "passed", []),
    "cargo_q_fail": ("cargo", "failed", ["tests::halves"]),  # -q: names only in the failures list
    "cargo_nff_doctest_fail": ("cargo", "failed", ["src/lib.rs - half (line 3)", "tests::halves"]),
    "cargo_compile_error": ("cargo", "collection_error", []),  # does not compile: no test verdict
    "go_fail": ("go", "failed", ["TestHalf"]),
    "go_v_fail": ("go", "failed", ["TestHalf"]),
    "go_pass": ("go", "passed", []),
    "go_v_pass": ("go", "passed", []),
    "jest_fail": ("jest/vitest", "failed", ["half"]),
    "jest_pass": ("jest/vitest", "failed", []),  # every test passed, but one test file failed to run
    "mocha_fail": ("mocha", "failed", ["calc half"]),
    "mocha_pass": ("mocha", "passed", []),
    "vitest_fail": ("jest/vitest", "failed", ["half"]),
    "vitest_pass": ("jest/vitest", "passed", []),
    "node_test_fail": ("tap", "failed", ["half"]),
    "node_tap_fail": ("tap", "failed", ["half"]),
    "node_test_pass": ("tap", "passed", []),
    "minitest_fail": ("minitest", "failed", ["CalcTest#test_half"]),
    "minitest_pass": ("minitest", "passed", []),
    "pytest_fail": ("pytest", "failed", ["py/test_calc.py::test_half"]),
    "pytest_q_fail": ("pytest", "failed", ["py/test_calc.py::test_half"]),
    "pytest_q_pass": ("pytest", "passed", []),
    "gradle_fail": ("gradle", "failed", ["CalcTest.divideHalf()"]),
    "gradle_pass": ("gradle", "passed", []),
    "maven_fail": ("junit", "failed", None),
    "phpunit_fail": ("phpunit", "failed", ["Tests\\CalcTest::testHalf"]),
    "phpunit_pass": ("phpunit", "passed", []),
    "dotnet_fail": ("dotnet", "failed", ["Calc.Tests.CalcTest.DivideHalf"]),
    "dotnet_pass": ("dotnet", "passed", []),
    "ctest_fail": ("ctest", "failed", ["half"]),
    "exunit_fail": ("exunit", "failed", ["CalcTest divides with a fraction"]),
    "xctest_fail": ("xctest", "failed", None),
    "deno_fail": ("deno", "failed", None),
    "bats_fail": ("tap", "failed", ["divides with a fraction"]),
}


class RunnerOutputTest(unittest.TestCase):
    def test_every_fixture(self):
        self.assertEqual(sorted(p.stem for p in FIX.glob("*.txt")), sorted(EXPECT))
        for name, (runner, outcome, names) in EXPECT.items():
            with self.subTest(name):
                text = (FIX / f"{name}.txt").read_text()
                oc = classify_output(text, 1 if outcome == "failed" else 0)
                self.assertEqual((oc.runner, oc.outcome), (runner, outcome), oc)
                if names is not None:
                    self.assertEqual(failing_tests(text, oc.runner), names)


class CheckCommandTest(unittest.TestCase):
    def test_test_commands_across_ecosystems(self):
        from arbiter.evidence import is_check_command

        for cmd in ("node --test", "ruby test/calc_test.rb", "ruby -Ilib -Itest test/test_calc.rb", "rake test",
                    "deno test", "bats test", "bun test", "php artisan test", "./runtests.py", "hatch test",
                    "bash run_tests.sh", "./run_tests.sh", "python tests/test_calc.py", "node test/calc.test.js",
                    "vendor/bin/pest", "npx vitest run", "./gradlew test", "mvn -q test", "dotnet test", "mix test",
                    "ctest --output-on-failure", "swift test", "cargo test", "go test ./...", "uv run pytest"):
            self.assertTrue(is_check_command(cmd), cmd)
        for cmd in ("node server.js", "ruby script.rb", "python setup.py install", "cat test.txt", "ls tests",
                    "echo pytest", "git status", "bash build.sh"):
            self.assertFalse(is_check_command(cmd), cmd)
