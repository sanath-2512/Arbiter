"""Background pre-build of Rust tests: what it runs, and what it tells the model."""

import os
import shutil
import time

from arbiter.outputs import OutputArchive
from arbiter.prewarm import Prewarm, plan
from arbiter.records import Redactor
from arbiter.shell import tool_environment
from arbiter.workspace import Workspace
from tests.helpers import TempDirCase, make_repo

CRATE = {"Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
         "src/lib.rs": "pub fn divide(a: f64, b: f64) -> f64 {\n    (a as i64 / b as i64) as f64\n}\n\n"
                       "#[cfg(test)]\nmod tests {\n    #[test]\n    fn halves() {\n"
                       "        assert_eq!(super::divide(7.0, 2.0), 3.5);\n    }\n}\n"}


def warm(repo, tmp, result=None):
    p = Prewarm(repo, tool_environment(dict(os.environ), tmp / "s", ()), None,
                OutputArchive(tmp / "out", Redactor([])), 300, lambda m: None)
    if result is not None:
        p.result = result
    return p


class PrewarmTest(TempDirCase):
    def test_plan(self):
        self.assertEqual(plan(make_repo(self.tmp / "r", CRATE))[0], "cargo test --no-run")
        self.assertIsNone(plan(make_repo(self.tmp / "p", {"a.py": "x = 1\n"})))

    def test_notes(self):
        base = {"command": "cargo test --no-run", "exit_code": 0, "timed_out": False, "cancelled": False,
                "duration_s": 94.0, "output_id": "o3", "text": ""}
        cases = [
            ({}, "pre-built in the background"),
            ({"command": "cargo test --no-run --offline", "network_failed_first": True}, "add --offline"),
            ({"exit_code": None, "timed_out": True}, "did not finish"),
            ({"exit_code": 101, "text": "error: failed to download from `https://index.crates.io/`"},
             "dependencies are not available here"),
            ({"exit_code": 101, "text": "   Compiling calc\nerror[E0425]: cannot find value `x`\n"},
             "failure is there before any change"),
        ]
        for extra, needle in cases:
            p = warm(self.tmp, self.tmp, {**base, **extra})
            note = p.take_note()
            self.assertIn(needle, note or "", extra)
            self.assertIsNone(p.take_note())  # said once
        self.assertIsNone(warm(self.tmp, self.tmp, {**base, "cancelled": True}).take_note())

    def test_real_prebuild_and_snapshots_ignore_the_new_lock_file(self):
        if shutil.which("cargo") is None:
            self.skipTest("cargo not installed")
        repo = make_repo(self.tmp / "crate", CRATE)
        ws = Workspace(repo, self.tmp / "state")
        base = ws.init()
        p = warm(repo, self.tmp)
        self.assertTrue(p.start())
        deadline = time.monotonic() + 300
        while p.running and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIn("pre-built", p.take_note() or "", p.summary())
        self.assertTrue((repo / "target").is_dir())
        self.assertTrue((repo / "Cargo.lock").is_file())
        self.assertEqual(ws.snapshot(), base)  # target/ and the new Cargo.lock are not part of any tree
