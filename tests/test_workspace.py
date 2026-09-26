import os
import subprocess

from gheerefill.workspace import Workspace
from tests.helpers import TempDirCase, git, make_repo


class WorkspaceTest(TempDirCase):
    def test_artifact_fidelity_and_clean_reconstruction(self):
        repo = make_repo(self.tmp / "r", {
            "a.txt": "a\nb\n", "crlf.txt": b"x\r\ny\r\n", "gone.txt": "bye\n", "from.txt": "move me\n",
            "sp ace ü.txt": "keep\n", ".gitignore": "*.log\nbuild/\n", "tool.sh": "echo\n",
        })
        ws = Workspace(repo, self.tmp / "state")
        base = ws.init()
        (repo / "a.txt").write_text("a\nB\n")
        (repo / "gone.txt").unlink()
        os.rename(repo / "from.txt", repo / "to.txt")
        (repo / "new dir").mkdir()
        (repo / "new dir" / "n ü.py").write_text("print(1)\n")
        os.chmod(repo / "tool.sh", 0o755)
        os.symlink("a.txt", repo / "link")
        (repo / "crlf.txt").write_bytes(b"x\r\nz\r\n")
        (repo / "blob.bin").write_bytes(bytes(range(256)))
        (repo / "debug.log").write_text("ignored")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "m.pyc").write_bytes(b"\0")
        cand = ws.snapshot()
        files = {f["path"]: f for f in ws.changed_files(base, cand)}
        self.assertEqual(files["to.txt"]["status"], "R")
        self.assertEqual(files["gone.txt"]["status"], "D")
        self.assertEqual(files["tool.sh"]["new_mode"], "100755")
        self.assertEqual(files["link"]["new_mode"], "120000")
        self.assertIn("new dir/n ü.py", files)
        self.assertNotIn("debug.log", files)
        self.assertFalse(any("__pycache__" in p for p in files))
        patch = self.tmp / "p.diff"
        patch.write_bytes(ws.patch(base, cand))
        ok, detail = ws.verify_reconstruction(base, cand, patch)
        self.assertTrue(ok, detail)
        self.assertIn("debug.log", ws.ignored_paths())
        # also applies with the target repository's own git in a fresh clone
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True, capture_output=True)
        subprocess.run(["git", "apply", str(patch)], cwd=clone, check=True)
        self.assertEqual((clone / "crlf.txt").read_bytes(), b"x\r\nz\r\n")
        self.assertTrue(os.access(clone / "tool.sh", os.X_OK))
        self.assertTrue((clone / "link").is_symlink())

    def test_restore_earlier_candidate_and_base(self):
        repo = make_repo(self.tmp / "r", {"m.py": "v = 0\n"})
        ws = Workspace(repo, self.tmp / "state")
        base = ws.init()
        (repo / "m.py").write_text("v = 1\n")
        (repo / "extra.py").write_text("x\n")
        good = ws.snapshot()
        (repo / "m.py").write_text("v = broken\n")
        (repo / "extra.py").unlink()
        (repo / "junk.py").write_text("junk\n")
        (repo / "cache.log").write_text("ignored? no, no .gitignore here\n")
        ws.snapshot()
        self.assertTrue(ws.restore(good))
        self.assertEqual((repo / "m.py").read_text(), "v = 1\n")
        self.assertTrue((repo / "extra.py").exists())
        self.assertFalse((repo / "junk.py").exists())
        self.assertEqual(ws.snapshot(), good)
        ws.restore(base)
        self.assertEqual(sorted(os.listdir(repo)), [".git", "m.py"])
        self.assertFalse(ws.restore(base))

    def test_tracked_files_matching_ignore_rules_are_still_captured(self):
        repo = make_repo(self.tmp / "r", {"build/keep.py": "x = 1\n", "node_modules/vendored.js": "a\n"})
        (repo / ".gitignore").write_text("build/\nnode_modules/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "ignore")
        ws = Workspace(repo, self.tmp / "state")
        base = ws.init()
        (repo / "build" / "keep.py").write_text("x = 2\n")
        (repo / "build" / "new.py").write_text("untracked in ignored dir\n")
        (repo / "node_modules" / "vendored.js").write_text("b\n")
        cand = ws.snapshot()
        paths = {f["path"] for f in ws.changed_files(base, cand)}
        self.assertEqual(paths, {"build/keep.py", "node_modules/vendored.js"})

    def test_non_git_target(self):
        repo = make_repo(self.tmp / "plain", {"x.py": "1\n", "venv_like/.venv/lib.py": "no\n"}, init_git=False)
        ws = Workspace(repo, self.tmp / "state")
        base = ws.init()
        self.assertIsNone(ws.target_git_state())
        (repo / "x.py").write_text("2\n")
        cand = ws.snapshot()
        self.assertEqual([f["path"] for f in ws.changed_files(base, cand)], ["x.py"])
        self.assertFalse((repo / ".git").exists())  # target never touched

    def test_target_git_hygiene_after_agent_commit_and_staging(self):
        repo = make_repo(self.tmp / "r", {"a.py": "1\n", "b.py": "1\n"})
        ws = Workspace(repo, self.tmp / "state")
        initial = ws.target_git_state()
        ws.init()
        (repo / "a.py").write_text("2\n")
        git(repo, "commit", "-qam", "agent committed")
        git(repo, "checkout", "-q", "-b", "agent-branch")
        (repo / "b.py").write_text("2\n")
        git(repo, "add", "b.py")
        notes = ws.restore_target_git(initial)
        self.assertTrue(notes)
        self.assertEqual(git(repo, "rev-parse", "HEAD").strip(), initial.head_commit)
        self.assertEqual(git(repo, "symbolic-ref", "HEAD").strip(), "refs/heads/main")
        self.assertEqual((repo / "a.py").read_text(), "2\n")  # working tree untouched
        diff = git(repo, "diff", "--name-only")
        self.assertEqual(set(diff.split()), {"a.py", "b.py"})  # both visible as unstaged changes vs base
        self.assertEqual(git(repo, "diff", "--cached", "--name-only").strip(), "")

    def test_shadow_store_never_writes_into_target_git(self):
        repo = make_repo(self.tmp / "r", {"a.py": "1\n"})
        before = sorted(p for p in (repo / ".git").rglob("*"))
        ws = Workspace(repo, self.tmp / "state")
        ws.init()
        (repo / "a.py").write_text("2\n")
        ws.snapshot()
        after = sorted(p for p in (repo / ".git").rglob("*"))
        self.assertEqual(before, after)
