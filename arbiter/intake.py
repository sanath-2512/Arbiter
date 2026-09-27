"""Turn whatever the evaluator supplies into tasks.

Accepted forms (auto-detected; the local JSON protocol of task.py still works):
- a GitHub issue URL `https://github.com/OWNER/REPO/issues/N` (or `.../pull/N`) or `OWNER/REPO#N`;
- `@path` to a file holding any of these forms;
- plain issue text (pasted), which needs a repository (REPO / REPO_PATH / REPO_URL, or asked
  interactively);
- a JSON task object / array / JSON Lines.

Repository resolution for GitHub issues: an explicit REPO (path or git URL) wins. Otherwise the
repository is cloned into the harness workspace (`workspace/OWNER__REPO__N`, partial clone:
full history, file contents fetched lazily). The base is the default branch unless BASE is set to
a commit or to `before-issue` (the last commit before the issue was opened).

Issue text is untrusted input. It is passed to the model inside <issue> tags as a description of
the problem; the harness never follows instructions from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arbiter.models.http import _ssl_context
from arbiter.records import safe_name
from arbiter.task import Task, TaskInputError, iter_tasks

ISSUE_URL = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)/?(?:[#?].*)?$")
SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
MAX_ISSUE_CHARS = 60_000


class IntakeError(ValueError):
    pass


@dataclass
class Request:
    """One unit of work before repository preparation."""

    issue_text: str | None = None
    github: tuple[str, str, int] | None = None
    task: Task | None = None  # already-complete task from the JSON protocol
    error: TaskInputError | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_github_ref(text: str) -> tuple[str, str, int] | None:
    t = text.strip()
    m = ISSUE_URL.match(t)
    if m:
        return m.group(1), m.group(2).removesuffix(".git"), int(m.group(4))
    m = SHORTHAND.match(t)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def parse_input(text: str, *, base_dir: Path, source: str = "input", _depth: int = 0) -> list[Request]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith(("{", "[")):
        import io

        out = []
        for item in iter_tasks(io.StringIO(stripped), base_dir=base_dir, source=source):
            out.append(Request(task=item, source=source) if isinstance(item, Task) else Request(error=item, source=source))
        if out and not all(r.error and "malformed JSON" in r.error.message for r in out):
            return out
    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    if stripped.startswith("@") and len(lines) == 1 and _depth < 2:
        path = (base_dir / stripped[1:].strip()).expanduser()
        if not path.is_file():
            raise IntakeError(f"issue file not found: {path}")
        return parse_input(path.read_text(encoding="utf-8", errors="replace"), base_dir=path.parent,
                           source=str(path), _depth=_depth + 1)
    refs = [parse_github_ref(l) for l in lines]
    if all(refs):
        return [Request(github=r, source=l) for r, l in zip(refs, lines)]
    if refs[0] and not any(refs[1:]):
        # "<issue URL>" followed by free text: that issue, plus the evaluator's extra instructions.
        extra = stripped.split("\n", 1)[1].strip()
        return [Request(github=refs[0], source=lines[0], metadata={"instructions": extra[:MAX_ISSUE_CHARS // 4]})]
    return [Request(issue_text=stripped, source=source)]


# ------------------------------------------------------------------------------------ GitHub

def _github_api() -> str:
    return os.environ.get("ARBITER_GITHUB_API", "https://api.github.com").rstrip("/")


def _github_get(path: str, token: str | None) -> Any:
    url = _github_api() + path
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "arbiter",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context() if url.startswith("https") else None) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        hint = " (unauthenticated limit is 60 requests/hour; set GITHUB_TOKEN)" if e.code in (403, 429) else ""
        raise IntakeError(f"GitHub API {path}: HTTP {e.code}{hint}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise IntakeError(f"cannot reach the GitHub API ({e}); paste the issue text and set REPO instead") from None


def fetch_issue(owner: str, repo: str, number: int, token: str | None = None) -> dict[str, Any]:
    issue = _github_get(f"/repos/{owner}/{repo}/issues/{number}", token)
    comments: list[dict[str, Any]] = []
    if issue.get("comments"):
        try:
            comments = _github_get(f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=50", token)
        except IntakeError:
            comments = []
    return {
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "state": issue.get("state"),
        "created_at": issue.get("created_at"),
        "closed_at": issue.get("closed_at"),
        "author": (issue.get("user") or {}).get("login"),
        "html_url": issue.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}",
        "is_pull_request": "pull_request" in issue,
        "labels": [l.get("name") for l in issue.get("labels") or [] if isinstance(l, dict)],
        "comments": [{"author": (c.get("user") or {}).get("login"), "created_at": c.get("created_at"),
                      "body": c.get("body") or ""} for c in comments if isinstance(c, dict)],
    }


IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)\s]+[^)]*\)|<img\s[^>]*>", re.I)


def image_note(text: str) -> str:
    """Screenshots cannot be seen by a text-only harness; say so instead of letting the model guess."""
    n = len(IMAGE_RE.findall(text))
    if not n:
        return ""
    return (f"\n\n[Harness note: the text above embeds {n} image(s) (screenshots or diagrams) that are not shown "
            "here. Work from the text, the code and your own reproduction.]")


def compose_issue_text(meta: dict[str, Any]) -> str:
    head = f"# {meta['title']}\n{meta['html_url']} · state: {meta['state']} · opened {meta['created_at']}"
    if meta.get("labels"):
        head += f" · labels: {', '.join(meta['labels'])}"
    text = head + "\n\n" + (meta["body"].strip() or "(no description)")
    if meta["comments"]:
        text += f"\n\n## Discussion ({len(meta['comments'])} comments, oldest first)"
        for c in meta["comments"]:
            text += f"\n\n### {c['author']} ({c['created_at']})\n{c['body'].strip()}"
    if len(text) > MAX_ISSUE_CHARS:
        text = text[:MAX_ISSUE_CHARS] + "\n\n[... discussion truncated ...]"
    return text + image_note(text)


# ------------------------------------------------------------------------------------ repositories

def _git(args: list[str], cwd: Path | None = None, timeout: float = 900,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "USER", "HTTPS_PROXY", "https_proxy",
                                      "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "GIT_SSL_CAINFO") if k in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", **(extra_env or {}))
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args], cwd=cwd,
                          capture_output=True, text=True, env=env, timeout=timeout)


def supports_partial_clone(url: str) -> bool:
    """Whether the server advertises partial-clone filters (protocol v2 `fetch=... filter`). A server
    that ignores `--filter` (older git servers, some mirrors, a shallow local source) turns a blob-less
    clone into one fetch per object: thousands of round trips, observed as a clone that never ends."""
    try:
        p = _git(["-c", "protocol.version=2", "ls-remote", "--heads", url], timeout=120,
                 extra_env={"GIT_TRACE_PACKET": "1"})
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0 and any(re.search(r"upload-pack> fetch=.*\bfilter\b", l) for l in p.stderr.splitlines())


def clone_url(owner: str, repo: str) -> str:
    base = os.environ.get("ARBITER_GITHUB_CLONE_BASE", "https://github.com").rstrip("/")
    return f"{base}/{owner}/{repo}.git" if base.startswith("http") else f"{base}/{owner}/{repo}"


HARNESS_ROOT = Path(__file__).resolve().parent.parent
PREPARED_CHECKOUTS = ("/testbed",)  # SWE-bench-style evaluation images keep the repository here


def origin_matches(repo_dir: Path, owner: str, repo: str) -> bool:
    """True if the checkout's .git/config names github.com/OWNER/REPO as a remote. The config file
    is read as text; git is not executed in an unknown checkout."""
    cfg = repo_dir / ".git" / "config"
    try:
        text = cfg.read_text(errors="replace") if cfg.is_file() else ""
    except OSError:
        return False
    want = re.compile(rf"github\.com[:/]+{re.escape(owner)}/{re.escape(repo)}(\.git)?/?$", re.I)
    return any(want.search(u.strip()) for u in re.findall(r"^\s*url\s*=\s*(.+)$", text, re.M))


def find_prepared_checkout(owner: str, repo: str) -> Path | None:
    """An existing checkout of this repository that the evaluator prepared: the directory `make`
    was started in, or a conventional location such as /testbed. Never the harness itself."""
    candidates = [os.environ.get("ARBITER_CALLER_DIR", ""), *PREPARED_CHECKOUTS]
    for c in candidates:
        if not c:
            continue
        p = Path(c).resolve()
        if p != HARNESS_ROOT and p.is_dir() and origin_matches(p, owner, repo):
            return p
    return None


def _base_ignored(base: str | None, log) -> list[str]:
    if not base:
        return []
    msg = f"BASE={base} ignored: an existing checkout is used exactly as it is (check out the base there instead)"
    log("note: " + msg)
    return [msg]


_CLAIMS: dict[str, Any] = {}


def claim_workspace(dest: Path) -> bool:
    """Exclusive, process-lifetime lock on a workspace clone (released by release_workspace or exit)."""
    try:
        import fcntl
    except ImportError:  # no flock on this platform: single-run behaviour
        return True
    fh = open(str(dest) + ".lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _CLAIMS[str(dest)] = fh
    return True


def release_workspace(dest: Path | str) -> None:
    fh = _CLAIMS.pop(str(dest), None)
    if fh is not None:
        fh.close()


def prepare_repo(spec: str | None, *, owner: str | None, repo: str | None, number: int | None, workspace: Path,
                 base: str | None, issue_created_at: str | None, log) -> tuple[Path, list[str]]:
    """Return a local repository path for the task and notes describing what was done."""
    notes: list[str] = []
    if spec and not re.match(r"^(https?://|git@|ssh://|file://)", spec):
        path = Path(spec).expanduser().resolve()
        if not path.is_dir():
            raise IntakeError(f"REPO path does not exist: {path}")
        return path, [f"repository: {path} (supplied, used in place)"] + _base_ignored(base, log)
    if not spec and owner and repo:
        found = find_prepared_checkout(owner, repo)
        if found is not None:
            log(f"using the prepared checkout {found} (its remote is github.com/{owner}/{repo}); not cloning")
            return found, [f"repository: {found} (existing checkout of {owner}/{repo}, used in place)"] + \
                _base_ignored(base, log)
    url = spec or clone_url(owner or "", repo or "")
    name = safe_name(f"{owner}__{repo}__{number}" if owner and number is not None else
                     f"{owner}__{repo}" if owner else Path(url.rstrip("/")).stem)
    workspace.mkdir(parents=True, exist_ok=True)
    dest = workspace / name
    n = 1
    # Parallel runs (an evaluator running several tasks of one repository at once) must never share a
    # clone: each holds an exclusive lock on its directory until its task ends.
    while True:
        if claim_workspace(dest):
            if not dest.exists():
                break
            status = _git(["status", "--porcelain"], cwd=dest)
            if status.returncode == 0 and not status.stdout.strip():
                notes.append(f"repository: reused clean clone {dest}")
                break
            release_workspace(dest)
        n += 1
        dest = workspace / f"{name}-{n}"
    try:
        if not dest.exists():
            partial = supports_partial_clone(url)
            log(f"cloning {url} -> {dest}" + (" (blob-less partial clone)" if partial else ""))
            p = _git(["clone", "--quiet", *(["--filter=blob:none"] if partial else []), url, str(dest)])
            if p.returncode != 0 and partial:
                shutil.rmtree(dest, ignore_errors=True)
                p = _git(["clone", "--quiet", url, str(dest)])
            if p.returncode != 0:
                raise IntakeError(f"git clone {url} failed: {p.stderr.strip()[:400]}")
            notes.append(f"repository: cloned {url} into {dest} (default branch)")
        if base:
            target = base
            if base == "before-issue":
                if not issue_created_at:
                    raise IntakeError("BASE=before-issue needs the issue's creation time (GitHub issues only)")
                r = _git(["rev-list", "-1", f"--before={issue_created_at}", "HEAD"], cwd=dest)
                target = r.stdout.strip()
                if not target:
                    raise IntakeError("no commit precedes the issue's creation time")
            p = _git(["checkout", "--quiet", "-B", "arbiter-base", target], cwd=dest)
            if p.returncode != 0 and re.fullmatch(r"[0-9a-f]{7,40}", target):
                # not reachable from the cloned branches (e.g. only from a pull-request ref): fetch it by id
                if _git(["fetch", "--quiet", "origin", target], cwd=dest).returncode == 0:
                    p = _git(["checkout", "--quiet", "-B", "arbiter-base", target], cwd=dest)
            if p.returncode != 0:
                raise IntakeError(f"cannot check out BASE {base}: {p.stderr.strip()[:300]}")
            notes.append(f"base: {base} -> {target[:12]}")
    except BaseException:
        release_workspace(dest)
        raise
    head = _git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
    notes.append(f"base commit: {head}")
    return dest, notes


def align_local_base(repo: Path, base: str) -> list[str]:
    """A task names its base commit and a local checkout: work on that commit. A clean checkout at
    another commit is moved there (detached HEAD); a dirty one is left alone, with a note."""
    if not (repo / ".git").exists():
        return []
    head = _git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=repo).stdout.strip()
    target = _git(["rev-parse", "-q", "--verify", f"{base}^{{commit}}"], cwd=repo).stdout.strip()
    if not target:
        return [f"base_commit {base[:12]} is not in the local checkout; working on its current state {head[:12]}"]
    if head == target:
        return []
    if _git(["status", "--porcelain", "--untracked-files=no"], cwd=repo).stdout.strip():
        return [f"the checkout is at {head[:12]}, not base_commit {base[:12]}, and has uncommitted changes: "
                "left as it is"]
    p = _git(["checkout", "--quiet", "--detach", target], cwd=repo)
    if p.returncode != 0:
        return [f"could not check out base_commit {base[:12]}: {p.stderr.strip()[:200]}"]
    return [f"checked out base_commit {target[:12]} (the checkout was at {head[:12]})"]


def build_task(req: Request, *, repo_spec: str | None, base: str | None, workspace: Path, github_token: str | None,
               limits: dict[str, Any], log) -> Task:
    if req.task is not None:
        t = req.task
        slug = t.metadata.get("remote_repo")
        if slug:
            owner, name = slug.split("/", 1)
            base_commit = t.metadata.get("base_commit") or base
            path, notes = prepare_repo(repo_spec, owner=owner, repo=name, number=None, workspace=workspace,
                                       base=str(base_commit) if base_commit else None, issue_created_at=None, log=log)
            t.repo_path = path
            t.metadata["intake_notes"] = notes
        elif t.metadata.get("base_commit"):
            notes = align_local_base(t.repo_path, str(t.metadata["base_commit"]))
            if notes:
                t.metadata["intake_notes"] = notes
                for n in notes:
                    log("note: " + n)
        return t
    if req.github:
        owner, repo, number = req.github
        meta = fetch_issue(owner, repo, number, github_token)
        notes = []
        if meta["is_pull_request"]:
            notes.append("the reference is a pull request; its description is used as the task text")
        if meta["state"] == "closed" and not base and not repo_spec:
            notes.append("the issue is CLOSED: a fix may already exist on the default branch. Set BASE=before-issue "
                         "(or BASE=<commit>, or REPO at the intended commit) to work on the historical code")
        for n in notes:
            log("note: " + n)
        path, repo_notes = prepare_repo(repo_spec, owner=owner, repo=repo, number=number, workspace=workspace,
                                        base=base, issue_created_at=meta["created_at"], log=log)
        issue = compose_issue_text(meta)
        if req.metadata.get("instructions"):
            issue += "\n\n## Additional instructions supplied with the issue\n" + req.metadata["instructions"]
        return Task(task_id=f"{owner}__{repo}-{number}", repo_path=path, issue=issue,
                    limits=dict(limits), metadata={"source": meta["html_url"], "issue_state": meta["state"],
                                                   "intake_notes": notes + repo_notes})
    if req.issue_text is not None:
        if not repo_spec:
            raise IntakeError("plain issue text needs a repository: set REPO=<path or git URL>")
        path, repo_notes = prepare_repo(repo_spec, owner=None, repo=None, number=None, workspace=workspace,
                                        base=base, issue_created_at=None, log=log)
        tid = "issue-" + hashlib.sha256(req.issue_text.encode()).hexdigest()[:8]
        return Task(task_id=tid, repo_path=path, issue=req.issue_text + image_note(req.issue_text), limits=dict(limits),
                    metadata={"source": req.source, "intake_notes": repo_notes})
    raise IntakeError(req.error.message if req.error else "empty request")
