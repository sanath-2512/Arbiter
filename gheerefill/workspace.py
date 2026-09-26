"""Candidate state capture, restoration and export using a shadow git object store.

The shadow repository lives in the run directory (outside the target), so capturing
state never writes into the target repository or its .git. Snapshots record the complete
working tree (tracked + untracked-not-ignored files, deletions, modes, symlinks) byte-for-
byte: `info/attributes` disables eol conversion, filters and encodings.

What is captured:
- every file the target's own git tracks (force-added at base, so .gitignore cannot hide
  tracked files), and every new file not matched by ignore rules;
- ignore rules = the target's .gitignore files + its .git/info/exclude + a short list of
  cache/environment directories (CACHE_EXCLUDES). New files under ignored paths are
  excluded from candidates and reported as `excluded_paths` with the reason.

Not supported (documented limitations): empty directories, content inside nested git
repositories/submodules, git-lfs smudge semantics.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CACHE_EXCLUDES = [
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".hypothesis/",
    ".tox/",
    ".nox/",
    ".venv/",
    "node_modules/",
    ".eggs/",
    "*.egg-info/",
    ".coverage",
    ".coverage.*",
    ".DS_Store",
    ".gradle/",
    ".ipynb_checkpoints/",
    # untracked compiled outputs (tracked files are force-added at base, so they are never excluded)
    "*.o",
    "*.obj",
    "*.class",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.pyd",
]

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_GIT_CFG = [
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "core.filemode=true",
    "-c", "core.symlinks=true",
    "-c", "core.quotepath=off",
    "-c", "core.fsmonitor=false",
    "-c", "gc.auto=0",
    "-c", "diff.noprefix=false",
    "--literal-pathspecs",
]


class WorkspaceError(RuntimeError):
    pass


# Config keys that make git execute programs; forced off for every call into the target repo.
TARGET_GIT_HARDENING = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.untrackedCache=false",
    "-c", "credential.helper=",
    "-c", "protocol.allow=never",
]


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_AUTHOR_NAME", "gheerefill")
    env.setdefault("GIT_AUTHOR_EMAIL", "gheerefill@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "gheerefill")
    env.setdefault("GIT_COMMITTER_EMAIL", "gheerefill@localhost")
    return env


def run_git(args: list[str], *, cwd: Path, env_extra: dict[str, str] | None = None, input: bytes | None = None,
            check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    env = _clean_env()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["git", *_GIT_CFG, *args], cwd=str(cwd), env=env, input=input,
                          capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[:800]}")
    return proc


@dataclass
class TargetGitState:
    head_commit: str | None
    head_ref: str | None
    index_matches_head: bool

    def to_dict(self) -> dict[str, Any]:
        return {"head_commit": self.head_commit, "head_ref": self.head_ref, "index_matches_head": self.index_matches_head}


class _GitCopier:
    """copytree copy_function for a .git directory: hard-link objects, copy everything else."""

    def __init__(self, root: Path, copy_limit: int | None):
        self.root, self.limit, self.copied = str(root), copy_limit, 0

    def __call__(self, src: str, dst: str) -> None:
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
            return
        if f"{os.sep}objects{os.sep}" in src[len(self.root):]:
            try:
                os.link(src, dst)
                return
            except OSError:
                self.copied += os.path.getsize(src)
                if self.limit is not None and self.copied > self.limit:
                    raise WorkspaceError("objects too large to copy across devices") from None
        shutil.copy2(src, dst)


class Workspace:
    def __init__(self, repo: Path, state_dir: Path):
        self.repo = Path(os.path.realpath(repo))
        self.state_dir = Path(state_dir)
        self.gitdir = self.state_dir / "shadow.git"
        self.index = self.gitdir / "gheerefill-index"
        self.base_tree: str | None = None
        self.wrap = None  # optional argv wrapper (sandbox) for target-repo git calls

    # -------------------------------------------------------------- plumbing
    def _env(self, work_tree: Path | None = None, index: Path | None = None) -> dict[str, str]:
        return {
            "GIT_DIR": str(self.gitdir),
            "GIT_WORK_TREE": str(work_tree or self.repo),
            "GIT_INDEX_FILE": str(index or self.index),
        }

    def git(self, *args: str, work_tree: Path | None = None, index: Path | None = None, input: bytes | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        return run_git(list(args), cwd=work_tree or self.repo, env_extra=self._env(work_tree, index), input=input, check=check)

    def target_is_git(self) -> bool:
        if not (self.repo / ".git").exists():
            return False
        return self._target_git("rev-parse", "--is-inside-work-tree").returncode == 0

    def _target_git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """Run git against the TARGET repository's own .git. The model can edit that repository's
        config (e.g. core.fsmonitor, hooks, filters), which git would execute. Defences: overrides for
        the executing keys, a minimal environment with no credentials, and (when available) the same
        Landlock domain as model commands, so anything that still runs cannot read the key."""
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER") if k in os.environ}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        argv = ["git", *TARGET_GIT_HARDENING, "-c", f"safe.directory={self.repo}", *args]
        if self.wrap is not None:
            argv = self.wrap(argv)
        p = subprocess.run(argv, cwd=self.repo, capture_output=True, env=env, timeout=300)
        if check and p.returncode != 0:
            raise WorkspaceError(f"target git {' '.join(args)} failed: {p.stderr.decode(errors='replace')[:500]}")
        return p

    def target_git_fingerprint(self) -> dict[str, Any] | None:
        """Tamper evidence for the target's git control files (config, hooks, info/attributes)."""
        gd = self.repo / ".git"
        if not gd.is_dir():
            return None
        import hashlib

        def digest(p: Path) -> str | None:
            return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else None

        hooks = gd / "hooks"
        active = sorted(h.name for h in hooks.iterdir() if h.is_file() and not h.name.endswith(".sample")) \
            if hooks.is_dir() else []
        return {"config": digest(gd / "config"), "hooks": active, "info_attributes": digest(gd / "info" / "attributes")}

    # -------------------------------------------------------------- lifecycle
    def init(self) -> str:
        """Create the shadow store and capture the base state. Returns the base tree id."""
        if self.gitdir.exists():
            raise WorkspaceError(f"shadow store already exists: {self.gitdir}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        run_git(["init", "--bare", "-q", str(self.gitdir)], cwd=self.state_dir)
        info = self.gitdir / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* -text -filter -ident -working-tree-encoding\n")
        excludes = list(CACHE_EXCLUDES) + [f"/{d}/" for d in self.environment_dirs()]
        if (self.repo / "Cargo.toml").is_file():
            excludes.append("/target/")
        target_exclude = self.repo / ".git" / "info" / "exclude"
        if target_exclude.is_file():
            excludes.append(target_exclude.read_text(errors="replace"))
        (info / "exclude").write_text("\n".join(excludes) + "\n")
        if self.target_is_git():
            tracked = self._target_git("ls-files", "-z", check=True).stdout
            if tracked.strip(b"\0"):
                self.git("add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", input=tracked)
        self.base_tree = self.snapshot()
        return self.base_tree

    def environment_dirs(self, max_depth: int = 2) -> list[str]:
        """Untracked virtualenv/conda directories inside the repository (relative paths)."""
        found: list[str] = []

        def walk(d: Path, depth: int) -> None:
            try:
                entries = [e for e in d.iterdir() if e.is_dir() and not e.is_symlink() and e.name != ".git"]
            except OSError:
                return
            for e in entries:
                if (e / "pyvenv.cfg").is_file() or (e / "conda-meta").is_dir():
                    found.append(str(e.relative_to(self.repo)))
                elif depth < max_depth and e.name not in ("node_modules", ".tox", ".nox"):
                    walk(e, depth + 1)

        walk(self.repo, 1)
        return sorted(found)

    def attach(self, base_tree: str) -> None:
        """Re-open an existing shadow store (recovery)."""
        if not self.gitdir.is_dir():
            raise WorkspaceError(f"no shadow store at {self.gitdir}")
        if not self.has_object(base_tree):
            raise WorkspaceError(f"base tree {base_tree} missing from shadow store")
        self.base_tree = base_tree

    def snapshot(self) -> str:
        self.git("add", "-A")
        return self.git("write-tree").stdout.decode().strip()

    def has_object(self, oid: str) -> bool:
        return self.git("cat-file", "-e", oid, check=False).returncode == 0

    # -------------------------------------------------------------- inspection
    def changed_files(self, a: str, b: str) -> list[dict[str, str]]:
        """Changes from tree a to tree b, with rename detection and file modes."""
        out = self.git("diff-tree", "-r", "-z", "-M", "--raw", "--no-ext-diff", a, b).stdout.decode(errors="replace")
        tokens = out.split("\0")
        files, i = [], 0
        while i < len(tokens) and tokens[i].startswith(":"):
            fields = tokens[i][1:].split()
            old_mode, new_mode, status = fields[0], fields[1], fields[4]
            entry = {"status": status[0], "old_mode": old_mode, "new_mode": new_mode}
            if status[0] in "RC":
                entry.update(old_path=tokens[i + 1], path=tokens[i + 2], score=status[1:])
                i += 3
            else:
                entry["path"] = tokens[i + 1]
                i += 2
            files.append(entry)
        return files

    def patch(self, a: str, b: str) -> bytes:
        """Byte-exact patch (no rename detection, so both `git apply` and GNU patch accept text hunks)."""
        return self.git("diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv",
                        "--src-prefix=a/", "--dst-prefix=b/", a, b).stdout

    def lines_removed(self, a: str, b: str, path: str) -> int:
        """Lines deleted or changed in `path` between two trees (0 = the change only adds lines)."""
        out = self.git("diff", "--numstat", "--no-renames", a, b, "--", path).stdout.decode(errors="replace")
        total = 0
        for line in out.splitlines():
            parts = line.split("\t")
            total += int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1  # binary: count as changed
        return total

    def shortstat(self, a: str, b: str) -> str:
        return self.git("diff", "--shortstat", "--no-renames", a, b).stdout.decode().strip()

    def ignored_paths(self) -> set[str]:
        out = self.git("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory").stdout
        return {p for p in out.decode(errors="replace").split("\0") if p}

    # -------------------------------------------------------------- mutation
    def restore(self, tree: str) -> bool:
        """Make the working tree equal to `tree` (ignored files untouched). Returns True if it changed."""
        if not self.has_object(tree):
            raise WorkspaceError(f"cannot restore unknown tree {tree}")
        current = self.snapshot()
        if current == tree:
            return False
        self.git("read-tree", "--reset", "-u", tree)
        after = self.snapshot()
        if after != tree:
            raise WorkspaceError(f"restore verification failed: expected {tree}, got {after}")
        return True

    def materialize(self, tree: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.state_dir) as td:
            idx = Path(td) / "index"
            self.git("read-tree", tree, index=idx)
            self.git("checkout-index", "-a", "-f", f"--prefix={str(dest).rstrip('/')}/", index=idx)

    def tree_of_dir(self, directory: Path) -> str:
        with tempfile.TemporaryDirectory(dir=self.state_dir) as td:
            idx = Path(td) / "index"
            self.git("add", "-A", "-f", work_tree=directory, index=idx)
            return self.git("write-tree", work_tree=directory, index=idx).stdout.decode().strip()

    def overlay_tree(self, base: str, source: str, paths: list[str]) -> str:
        """`base` with the given paths taken from `source` (removed where `source` lacks them).
        Used for the counterfactual state: original code + the candidate's own test changes."""
        if not paths:
            return base
        with tempfile.TemporaryDirectory(dir=self.state_dir) as td:
            idx = Path(td) / "index"
            self.git("read-tree", base, index=idx)
            listing = self.git("ls-tree", "-r", "-z", source, "--", *paths).stdout
            present: dict[str, tuple[str, str]] = {}
            for item in listing.split(b"\0"):
                if item:
                    meta, path = item.split(b"\t", 1)
                    mode, _typ, oid = meta.decode().split()
                    present[path.decode("utf-8", "surrogateescape")] = (mode, oid)
            info = b""
            for p in paths:
                mode, oid = present.get(p, ("0", "0" * 40))  # mode 0 removes the entry
                info += f"{mode} {oid}\t".encode() + p.encode("utf-8", "surrogateescape") + b"\0"
            self.git("update-index", "-z", "--index-info", index=idx, input=info)
            return self.git("write-tree", index=idx).stdout.decode().strip()

    def verify_reconstruction(self, base: str, candidate: str, patch_path: Path) -> tuple[bool, str]:
        """Apply the exported patch to a clean copy of the base and compare tree ids."""
        with tempfile.TemporaryDirectory(dir=self.state_dir) as td:
            work = Path(td) / "w"
            self.materialize(base, work)
            run_git(["init", "-q"], cwd=work)
            if patch_path.stat().st_size > 0:
                p = run_git(["apply", "--binary", "--whitespace=nowarn", str(patch_path)], cwd=work, check=False)
                if p.returncode != 0:
                    return False, f"git apply failed: {p.stderr.decode(errors='replace')[:500]}"
            shutil.rmtree(work / ".git")
            got = self.tree_of_dir(work)
        if got != candidate:
            return False, f"reconstructed tree {got} != selected {candidate}"
        return True, "patch applied to a clean base copy reproduces the selected tree exactly"

    # -------------------------------------------------------------- target git hygiene
    def target_git_state(self) -> TargetGitState | None:
        if not self.target_is_git():
            return None
        head = self._target_git("rev-parse", "--verify", "-q", "HEAD")
        ref = self._target_git("symbolic-ref", "-q", "HEAD")
        head_commit = head.stdout.decode().strip() or None if head.returncode == 0 else None
        clean = True
        if head_commit:
            clean = self._target_git("diff-index", "--cached", "--quiet", "HEAD").returncode == 0
        return TargetGitState(head_commit, ref.stdout.decode().strip() or None if ref.returncode == 0 else None, clean)

    # A model command can remove or replace the target's .git (`rm -rf .git`, `git init`). The
    # deliverable survives (it lives in the shadow store), but an evaluator reading `git diff` in the
    # repository would not. Keep a start-of-run copy: objects are hard-linked (content-addressed and
    # never rewritten in place), everything else is copied, so a later in-place edit cannot reach it.
    BACKUP_COPY_LIMIT = 1 << 30  # bytes of objects copied when hard links are impossible (other device)

    @property
    def git_backup(self) -> Path:
        return self.state_dir / "target-git"

    def backup_target_git(self) -> str:
        gd = self.repo / ".git"
        dest = self.git_backup
        if dest.exists() or dest.is_symlink():
            return "kept"
        if gd.is_file():  # worktree / submodule pointer
            shutil.copy2(gd, dest)
            return "pointer file copied"
        if not gd.is_dir():
            return "no .git"
        copier = _GitCopier(gd, self.BACKUP_COPY_LIMIT)
        try:
            shutil.copytree(gd, dest, symlinks=True, copy_function=copier,
                            ignore=shutil.ignore_patterns("*.lock", "fsmonitor--daemon*"))
        except (OSError, shutil.Error, WorkspaceError) as e:
            shutil.rmtree(dest, ignore_errors=True)
            return f"not kept ({str(e)[:120]})"
        return "kept" + (f" ({copier.copied >> 20} MB of objects copied: other device)" if copier.copied else "")

    def target_git_damaged(self, initial: TargetGitState | None, thorough: bool = False) -> bool:
        gd = self.repo / ".git"
        if not (self.git_backup.exists() or self.git_backup.is_symlink()):
            return False
        if self.git_backup.is_file():
            return not gd.is_file() or gd.read_bytes() != self.git_backup.read_bytes()
        if not (gd.is_dir() and (gd / "HEAD").is_file() and (gd / "objects").is_dir()):
            return True
        if thorough and initial is not None and initial.head_commit:
            return self._target_git("cat-file", "-e", f"{initial.head_commit}^{{commit}}").returncode != 0
        return False

    def repair_target_git(self, initial: TargetGitState | None, thorough: bool = True) -> list[str]:
        """Put the start-of-run .git back if it was removed or replaced. The working tree is untouched."""
        if not self.target_git_damaged(initial, thorough):
            return []
        gd = self.repo / ".git"
        if gd.exists() or gd.is_symlink():
            n = 1
            while (self.state_dir / f"target-git-replaced-{n}").exists():
                n += 1
            shutil.move(str(gd), str(self.state_dir / f"target-git-replaced-{n}"))
        if self.git_backup.is_file():
            shutil.copy2(self.git_backup, gd)
        else:
            shutil.copytree(self.git_backup, gd, symlinks=True, copy_function=_GitCopier(self.git_backup, None))
        return ["restored the repository's .git from the start-of-run copy (a command had removed or replaced it); "
                "working tree untouched"]

    def target_stash(self) -> str | None:
        if not self.target_is_git():
            return None
        p = self._target_git("rev-parse", "-q", "--verify", "refs/stash")
        return p.stdout.decode().strip() or None if p.returncode == 0 else None

    def drop_new_stash(self, initial_stash: str | None) -> list[str]:
        """`git stash` during the run leaves a stash entry behind (its content is in the shadow store)."""
        if initial_stash is not None or self.target_stash() is None:
            return []
        self._target_git("update-ref", "-d", "refs/stash")
        return ["removed the stash entry created during the run (its content is archived in the run directory)"]

    def restore_target_git(self, initial: TargetGitState | None) -> list[str]:
        """Undo HEAD/branch/index changes made by the agent (commits, checkouts, staging) without
        touching the working tree, so the deliverable is uncommitted changes on the original base."""
        notes: list[str] = []
        if initial is None:
            return notes
        cur = self.target_git_state()
        if cur is None:
            notes.append("target .git became unusable during the run; not restored")
            return notes
        if cur.head_ref != initial.head_ref:
            if initial.head_ref:
                self._target_git("symbolic-ref", "HEAD", initial.head_ref, check=True)
            elif initial.head_commit:
                self._target_git("update-ref", "--no-deref", "HEAD", initial.head_commit, check=True)
            notes.append(f"restored HEAD to {initial.head_ref or initial.head_commit} (agent had switched it)")
        if initial.head_commit:
            now = self._target_git("rev-parse", "--verify", "-q", "HEAD").stdout.decode().strip()
            if now != initial.head_commit:
                if initial.head_ref:
                    self._target_git("update-ref", initial.head_ref, initial.head_commit, check=True)
                else:
                    self._target_git("update-ref", "--no-deref", "HEAD", initial.head_commit, check=True)
                notes.append(f"moved HEAD back to base commit {initial.head_commit[:12]} (agent had committed)")
            if initial.index_matches_head and self._target_git("diff-index", "--cached", "--quiet", "HEAD").returncode != 0:
                self._target_git("reset", "-q", check=True)
                notes.append("reset the target index to HEAD (agent had staged changes); working tree untouched")
        return notes
