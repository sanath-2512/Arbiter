"""Runtime prompts. Kept short: they are paid for on every request of every task."""

from __future__ import annotations

import os
from pathlib import Path

SYSTEM = """You are an autonomous software engineer. You are working in the repository at {repo}. \
Nobody will answer questions, so work independently until the task is done, then call submit.

How to work:
1. Understand the issue. Find the relevant code with search/read_file, and check the tests and callers that matter.
2. If practical, reproduce the problem or pin down the expected behaviour with a quick check before editing.
3. Make a complete fix in the source. Keep unrelated code, public interfaces and style unchanged. Only change \
existing tests if the task requires it; adding tests is fine.
4. Verify. Run the relevant existing tests and your reproduction, and read failures carefully. Don't claim success \
without evidence.
5. Delete scratch files you created in the repository (keep throwaway scripts in {scratch}), then call submit.

Notes:
- Every bash call runs in a fresh shell at the repository root.
- Do not commit, stash, reset or check out git history. Your working-tree changes are the submission.
- Long outputs are truncated. The notice tells you how to see what was omitted.
- Time and steps are limited. Budget notices will tell you when to wrap up."""

TASK = """<issue>
{issue}
</issue>
{overview}"""

MANIFESTS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "pytest.ini", "noxfile.py",
    "package.json", "pnpm-lock.yaml", "yarn.lock", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "composer.json", "Makefile", "CMakeLists.txt", "mix.exs", "Package.swift",
)


def repo_overview(repo: Path, limit: int = 60) -> str:
    try:
        entries = sorted(os.listdir(repo))
    except OSError:
        return ""
    entries = [e for e in entries if e != ".git"]
    shown = [e + ("/" if (repo / e).is_dir() else "") for e in entries[:limit]]
    more = f" … (+{len(entries) - limit} more)" if len(entries) > limit else ""
    manifests = [m for m in MANIFESTS if (repo / m).exists()]
    lines = [f"Repository top level: {' '.join(shown)}{more}"]
    if manifests:
        lines.append(f"Build/test manifests present: {', '.join(manifests)}")
    return "\n" + "\n".join(lines)


SUBMIT_EMPTY = (
    "You called submit, but the repository has no changes. If the issue needs a code change, make it first. "
    "If you really mean to submit no change, call submit again."
)


def submit_review(changed: list[str], new_files: list[str], unverified: bool) -> str:
    parts = ["Before this is accepted, review the submission:"]
    parts.append("- Changed files: " + ", ".join(changed[:30]) + (" …" if len(changed) > 30 else ""))
    if new_files:
        parts.append(
            "- New files that were not in the repository: " + ", ".join(new_files[:20])
            + ". Delete any that are scratch or reproduction files and not part of the fix."
        )
    if unverified:
        parts.append("- No test or check has run on the current code since your last edit.")
    parts.append("If everything is intended, call submit again.")
    return "\n".join(parts)


def budget_notice(steps_left: int, seconds_left: float) -> str:
    return (
        f"Budget notice: about {steps_left} steps and {int(seconds_left // 60)} min {int(seconds_left % 60)} s remain. "
        "Wrap up now: make sure the fix is in place, run the key check, then submit."
    )


REPETITION = (
    "Note: you have now run the same command {n} times and got the same output. It is not giving new information. "
    "Try a different approach."
)

NO_TOOL_CALL = "Your reply contained no tool call. Every reply must call a tool. Call submit when you are finished."
CUT_OFF = (
    "Your reply hit the output length limit before any complete tool call. Keep replies shorter: brief reasoning, "
    "then one tool call."
)
