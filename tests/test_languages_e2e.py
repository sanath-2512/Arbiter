"""End to end on real toolchains (Go, Rust, Node, Ruby) behind DeepSeek-/Qwen-like endpoints.

Each repository has a real bug and a real failing test; a scripted policy runs the tests, fixes the
code and runs them again, while the provider emulator shapes the wire (reasoning passback, leaked
tool calls, renamed arguments). The harness must classify the real runner output (a failure, then
a pass), deliver the fix, and prove it. A toolchain missing from the machine skips its case.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

from scripts.provider_emulator import ProviderEmulator
from tests.helpers import ROOT, TempDirCase, make_repo

LANGS = {
    "go": {
        "tool": "go",
        "files": {
            "go.mod": "module calc\n\ngo 1.18\n",
            "calc.go": "package calc\n\nfunc Divide(a, b float64) float64 {\n\treturn float64(int(a) / int(b))\n}\n",
            "calc_test.go": ('package calc\n\nimport "testing"\n\nfunc TestDivide(t *testing.T) {\n'
                             '\tif got := Divide(7, 2); got != 3.5 {\n\t\tt.Fatalf("Divide(7, 2) = %v, want 3.5", got)\n'
                             '\t}\n}\n'),
        },
        "test": "go test ./...",
        # written with spaces, as models often do for Go: the edit tool must re-indent to tabs
        "edit": ("calc.go", "    return float64(int(a) / int(b))", "    return a / b"),
        "runner": "go",
    },
    "rust": {
        "tool": "cargo",
        "files": {
            "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
            "src/lib.rs": ("pub fn divide(a: f64, b: f64) -> f64 {\n    (a as i64 / b as i64) as f64\n}\n\n"
                           "#[cfg(test)]\nmod tests {\n    #[test]\n    fn halves() {\n"
                           "        assert_eq!(super::divide(7.0, 2.0), 3.5);\n    }\n}\n"),
        },
        "test": "cargo test --offline",
        "edit": ("src/lib.rs", "(a as i64 / b as i64) as f64", "a / b"),
        "runner": "cargo",
    },
    "node": {
        "tool": "node",
        "files": {
            "package.json": '{"name": "calc", "version": "1.0.0", "scripts": {"test": "node --test"}}\n',
            "calc.js": "exports.divide = (a, b) => Math.trunc(a / b);\n",
            "test/calc.test.js": ("const test = require('node:test');\nconst assert = require('node:assert');\n"
                                  "const { divide } = require('../calc');\n\n"
                                  "test('divide keeps the fraction', () => {\n  assert.strictEqual(divide(7, 2), 3.5);\n});\n"),
        },
        "test": "node --test",
        "edit": ("calc.js", "Math.trunc(a / b)", "a / b"),
        "runner": "tap",
    },
    "ruby": {
        "tool": "ruby",
        "files": {
            "lib/calc.rb": "module Calc\n  def self.divide(a, b)\n    a / b\n  end\nend\n",
            "test/test_calc.rb": ('require "minitest/autorun"\nrequire_relative "../lib/calc"\n\n'
                                  "class TestCalc < Minitest::Test\n  def test_divide\n"
                                  "    assert_equal 3.5, Calc.divide(7, 2)\n  end\nend\n"),
        },
        "test": "ruby -Ilib test/test_calc.rb",
        "edit": ("lib/calc.rb", "a / b", "a.to_f / b"),
        "runner": "minitest",
    },
}


def policy_for(spec):
    path, old, new = spec["edit"]
    plan = [("bash", {"command": spec["test"]}),
            ("edit_file", {"path": path, "old_str": old, "new_str": new}),
            ("bash", {"command": spec["test"]})]

    def respond(messages, tools):
        i = sum(1 for m in messages if m.get("role") == "assistant")
        if i < len(plan):
            name, args = plan[i]
            return {"content": f"step {i + 1}", "tool_calls": [{"name": name, "arguments": args}]}
        return {"content": "done", "tool_calls": [{"name": "submit", "arguments": {"summary": "fixed division"}}]}
    return respond


class RealToolchainTest(TempDirCase):
    def run_lang(self, lang, family, seed):
        spec = LANGS[lang]
        if shutil.which(spec["tool"]) is None:
            self.skipTest(f"{spec['tool']} not installed")
        repo = make_repo(self.tmp / f"{lang}-{family}", spec["files"])
        self.require_toolchain(repo, spec)
        prof = self.tmp / "p.toml"
        prof.write_text('[model]\nprovider = "openai_chat"\nname = "emulated"\nbase_url = "http://127.0.0.1:1/v1"\n'
                        '\n[limits]\ntime_limit_s = 400\nmax_steps = 30\n')
        with ProviderEmulator(policy_for(spec), family, seed=seed, scale=1.0) as emu:
            env = {**os.environ, "AI_API_KEY": "sk-emulated-0000", "AI_BASE_URL": emu.base_url,
                   "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
            p = subprocess.run([sys.executable, "-m", "gheerefill", "run", "--profile", str(prof), "--no-discover",
                                "--out", str(self.tmp / "out")],
                               input=json.dumps({"task_id": lang, "repo_path": str(repo),
                                                 "issue": "divide(7, 2) returns 3; it should return 3.5."}) + "\n",
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=420)
        lines = [l for l in p.stdout.splitlines() if l.strip().startswith("{")]
        self.assertTrue(lines, p.stderr[-3000:])
        rec = json.loads(lines[-1])
        detail = f"{emu.stats()} quirks={rec.get('model_quirks')}\n{p.stderr[-3000:]}"
        with open(os.path.join(rec["run_dir"], "evidence.jsonl")) as fh:
            evidence = [json.loads(l) for l in fh]
        outcomes = [(e["runner"], e["outcome"]) for e in evidence if e["source"] == "agent"]
        self.assertEqual(outcomes[:2], [(spec["runner"], "failed"), (spec["runner"], "passed")], detail)
        self.assertEqual(rec["termination"], "model_submitted", detail)
        self.assertEqual(rec["verification"]["status"], "checks_passed", detail)
        self.assertTrue(rec["submission_ready"], detail)
        self.assertEqual(emu.rejections, {}, detail)
        with open(rec["deliverable"]["patch_path"]) as fh:
            rec["patch_text"] = fh.read()
        self.assertIn(spec["edit"][2].strip(), rec["patch_text"])
        # only the fix: no Cargo.lock or other byproduct of running the tests
        self.assertEqual([f["path"] for f in rec["deliverable"]["files"]], [spec["edit"][0]], detail)
        return rec

    def require_toolchain(self, repo, spec):
        """Skip unless this machine's toolchain runs the test and reports the planted failure (an old
        node without --test, a Ruby without minitest, no network for a toolchain download)."""
        from gheerefill.evidence import classify_output
        try:
            p = subprocess.run(spec["test"], shell=True, cwd=repo, capture_output=True, text=True, timeout=300,
                               env={**os.environ, "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local"})
        except subprocess.TimeoutExpired:
            self.skipTest(f"{spec['tool']} too slow here")
        oc = classify_output(p.stdout + p.stderr, p.returncode, timed_out=False, piped=False)
        if (oc.runner, oc.outcome) != (spec["runner"], "failed"):
            self.skipTest(f"{spec['tool']} here does not run the test as expected: {oc.runner}/{oc.outcome}")
        subprocess.run(["git", "clean", "-qfdx"], cwd=repo, check=True)  # build output from the pre-check

    def test_go_behind_deepseek(self):
        rec = self.run_lang("go", "deepseek", 1)
        self.assertIn("\treturn a / b", rec["patch_text"])  # re-indented to tabs

    def test_rust_behind_qwen(self):
        self.run_lang("rust", "qwen", 2)

    def test_node_behind_deepseek(self):
        self.run_lang("node", "deepseek", 3)

    def test_ruby_behind_qwen(self):
        self.run_lang("ruby", "qwen", 4)


if __name__ == "__main__":
    unittest.main()
