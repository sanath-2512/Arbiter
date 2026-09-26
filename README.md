# gheerefill

An autonomous coding-agent harness for the AI Coding Harness Hackathon. It gives one model session
validated tools inside a target repository. It preserves every candidate state the model produces
and binds test evidence to the exact code it ran on. It always finishes by exporting a byte-exact,
clean-reconstruction-verified deliverable.

> **Status:** implemented and deterministically tested (see [NOTES.md](NOTES.md)). **Not yet
> live-validated.** The organiser-prescribed model, endpoint and task protocol have not been
> supplied, so no claim here rests on a real model call.

## Requirements

- Python ≥ 3.11 (standard library only) and `git` ≥ 2.25, `bash`.
- Optional: `rg` (ripgrep). If it is missing, search falls back to `grep`.
- No third-party runtime packages. `make setup` needs no network.

## Quick start (the official evaluation procedure)

```bash
export AI_API_KEY="<PROVIDED_API_KEY>"   # the only required configuration
make setup                               # offline: picks Python >= 3.11, checks git, byte-compiles
make run                                 # launches the harness; then supply the issue:
#   issue> https://github.com/OWNER/REPO/issues/123        (or OWNER/REPO#123, or @issue.md,
#                                                             or paste text and end with /go)
make test                                # deterministic tests: no network, no credentials
```

The same run can also be driven without a terminal:

```bash
make run ISSUE=https://github.com/OWNER/REPO/issues/123      # clone + fetch issue + solve
make run ISSUE="text of the issue" REPO=/path/to/repo       # local repository
make run TASK=tasks.jsonl                                   # JSON / JSON Lines tasks
BASE=before-issue make run ISSUE=...                        # historical base for a closed issue
```

`make run` never prompts when it has no terminal, and never fails merely because no input was
given. At start-up it prints three things: the resolved model, the verified isolation status, and
where results go.

**Model configuration** (`profiles/default.toml`, committed). If the organisers prescribe a model,
set `[model] provider/name/base_url` and the harness uses exactly that and never substitutes another
model. Until then `provider = "auto"` applies. Ordered `[[auto]]` rules map the credential's
*format* (for example `sk-ant-`, `sk-proj-`, `AIza`) to exactly one provider endpoint and a
model-preference list. The first preferred model that the provider's `/models` endpoint lists is
used, and its exact id is recorded. The key is sent only to that one endpoint, never tried against
other providers. `AI_MODEL`, `AI_BASE_URL` and `AI_PROVIDER` override the file without editing it,
and `make check-config` shows the resolution without spending tokens.

**Result:**
- **Terminal:** a card with status, verification evidence, changed files and the diff.
- **Repository:** the change is left uncommitted on the original base.
- **Run directory** (`runs/<task>/<run>/`):
  - `patch.diff`, byte-exact and verified by clean reconstruction;
  - `report.md`, where each claim names the file it comes from;
  - `result.json` and the full records.

When stdout is not a terminal, results are emitted as one JSON line per task.

## Input and output formats

- **Inputs** (auto-detected):
  - a GitHub issue or pull-request URL, or `OWNER/REPO#N`, whose title, body and discussion are
    fetched;
  - `@file`;
  - plain text, which needs `REPO`;
  - JSON, e.g.
    `{"task_id": "...", "repo_path": "...", "issue": "...", "limits": {"time_limit_s": 900, "max_steps": 80}}`,
    as one object, an array, or JSON Lines.
- **Issue text is untrusted input.** It reaches the model inside `<issue>` tags as a problem
  description.
- **Repositories:** an explicit `REPO` (path or git URL) wins. Otherwise GitHub issues are cloned
  into `workspace/OWNER__REPO__N`, as a partial clone of the default branch. For a *closed* issue
  the harness warns that the fix may already exist, and `BASE=before-issue` selects the last commit
  before the issue was opened.
- **Outputs:**
  - A terminal card, or one JSON result record per task on stdout when it is not a terminal.
    Progress goes to stderr.
  - The record never claims correctness. `submission_ready` is an artifact condition;
    `verification.status` is one of `checks_passed`, `checks_failed`,
    `verification_inconclusive` or `verification_unavailable`.
- **Exit status:**
  - 0: tasks completed, or no input was given.
  - 1: invalid input, or an infrastructure error.
  - 2: configuration error, e.g. a missing key or an unknown key format.

## What the harness does

| Mechanism | What it guarantees |
|---|---|
| **Validated tools** | `bash`, `read_file`, `search`, `edit_file`, `write_file`, `read_output`, `submit`. Arguments are validated before dispatch. Edits must match exactly and uniquely, or they fail without touching the file. |
| **Bounded observations** | Every output is archived verbatim. The model sees head, tail and error-like lines, plus exact line ranges for anything omitted. |
| **Credential isolation** | Model commands, and the harness's own git calls into the target repo, run in a Linux Landlock domain with the ptrace-type capabilities dropped. From there they cannot read `/proc/<pid>/environ` of the harness, `make` or the evaluator's shell. It needs no root, container or packages, and is verified by a self-test at startup. The harness also scrubs the key from its own environment block. |
| **Processes** | Each command runs in its own process group. Timeouts, cancellation and an exact output cap all kill the group. Background leftovers are stopped. Credentials are stripped from the command environment. |
| **Candidate capture** | A shadow git store in the run directory snapshots the full working tree after every step (~0.04 s per step). This covers adds, deletes, renames, modes, symlinks, CRLF and binary files. Nothing is ever written into the target's `.git`. |
| **Evidence** | Test-runner output is classified (pytest, unittest, go, cargo, jest/vitest, mocha, junit, rspec) and bound to the exact tree it ran on. A zero exit code alone never counts as a pass. |
| **Selection** | The final state is kept unless an earlier archived candidate *dominates* it on shared checks. If the run ended with an empty tree, the latest non-empty candidate is restored. |
| **Budget** | A monotonic deadline and a finalisation reserve. Every request attempt is recorded, and usage of interrupted requests is marked unknown, never zero. |
| **Model client** | Failures are classified: auth, quota, rate limit, server, unsupported parameter, context overflow, timeout, network. Retries are bounded, deadline-aware and honour Retry-After. |
| **Finalisation** | Always runs, including after crash, SIGTERM or budget exhaustion. It undoes agent commits/branch switches/staging, and re-runs the last check on the selected tree when that evidence is stale. After SIGKILL, run `python -m gheerefill finalize --run-dir DIR` (no model calls). |

## Configuration

Profiles are TOML files in `profiles/`, strictly validated so unknown keys are errors. They pin the
provider (`openai_chat` or `anthropic_messages`), the model and endpoint, and the tool protocol:
`native` function calling, or an explicitly chosen `text` protocol. They also pin generation
settings, limits and policy flags. `model.stream = true` enables SSE streaming. `AI_MODEL`, `AI_BASE_URL` and `AI_PROVIDER` override the profile,
and the result records which values came from overrides. Per-task `limits` from the evaluator take
precedence.

Sources of randomness:
- Model sampling, set by the provider and profile settings.
- Retry jitter, which is seeded (`retry.seed`).

## Development evaluation (dev only; needs live credentials)

```bash
make baseline-setup                                   # pinned mini-swe-agent 2.4.6 + Pi 0.73.1
python3 scripts/eval.py --validate-suite              # judges fail on base, pass on reference
python3 scripts/eval.py --partition dev --systems ours,mini,pi --repeats 1
python3 scripts/demo_live.py                          # live demo incl. SIGKILL → offline recovery
```

`evalsuite/` holds 8 small, owned screening tasks (Python, JS, Go) with judge-owned hidden tests,
split into dev, selection and final partitions. Each system's exported patch is judged on a clean
base. The runner writes a record for every scheduled run, plus a paired summary. See
[evalsuite/README.md](evalsuite/README.md).

## Layout

```
gheerefill/     runtime (stdlib only): cli, task adapter, config, models/, tools, shell, outputs,
                workspace (shadow git), evidence, budget, context, prompts, agent (controller)
profiles/       run profiles (default.toml is the submission profile)
tests/          deterministic unittest suite (no network, no credentials)
evalsuite/      dev evaluation tasks — evaluation boundary, never read by the runtime
baselines/      pinned upstream baselines (dev only)
scripts/        setup, example preparation, eval runner, live demo
examples/       example repository + scripted demo (clearly labelled non-live)
```

## Licence and provenance

`gheerefill/shell.py` adapts the process-group timeout pattern from mini-swe-agent (MIT, © 2025
Kilian A. Lieret and Carlos E. Jimenez). Both baselines are installed from pinned upstream
releases and are not vendored. Details are in [NOTES.md](NOTES.md#provenance).
