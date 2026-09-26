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

## Quick start

```bash
make setup                                   # choose Python ≥ 3.11, check git, byte-compile
make test                                    # deterministic tests: no network, no credentials

export AI_API_KEY=...                        # credential (never logged, never passed to tools)
export AI_MODEL=<prescribed-model-id>        # until profiles/default.toml pins it
export AI_BASE_URL=https://<endpoint>/v1     # OpenAI-compatible prefix (or Anthropic base)
make check-config && make probe              # LIVE: endpoint + tool-calling compatibility check

make run TASK=task.json                      # or: make run < tasks.jsonl
```

`make run` works without a TTY and never prompts. If the model or credential is missing, it emits a
`configuration_error` record per task and exits with status 2. It never substitutes a model.

## Task protocol (local development adapter — replace when the official one is published)

Input can be one JSON object, a JSON array, or JSON Lines, from a file (`TASK=`) or stdin:

```json
{"task_id": "demo-1", "repo_path": "/path/to/repo", "issue": "text of the issue",
 "limits": {"time_limit_s": 900, "max_steps": 80}}
```

- **EOF** ends input. Every task read so far is processed in order.
- **Malformed input:** each bad line or object produces an `invalid_input` record, and processing
  continues.
- **Output:** one JSON result record per task, one line each on stdout. Progress goes to stderr.
- **Deliverable:** the target repository's working tree is left at the selected candidate, as
  uncommitted changes on the original base. `runs/<task>/<run>/patch.diff` holds the byte-exact
  patch. The patch is verified by applying it to a clean copy of the base.
- **Exit status:**
  - 0: every task completed.
  - 1: some input was invalid, or there was an infrastructure error.
  - 2: configuration error.
- **The result record never claims correctness.** `submission_ready` is an artifact/protocol
  condition. `verification.status` is one of `checks_passed`, `checks_failed`,
  `verification_inconclusive` or `verification_unavailable`. Per-run files (transcript, actions,
  evidence, candidates, requests, archived outputs, checkpoint) live in the run directory, which is
  always outside the target repo.

## What the harness does

| Mechanism | What it guarantees |
|---|---|
| **Validated tools** | `bash`, `read_file`, `search`, `edit_file`, `write_file`, `read_output`, `submit`. Arguments are validated before dispatch. Edits must match exactly and uniquely, or they fail without touching the file. |
| **Bounded observations** | Every output is archived verbatim. The model sees head, tail and error-like lines, plus exact line ranges for anything omitted. |
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
