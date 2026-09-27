# gheerefill

**An autonomous coding-agent harness that does not stop at "done": it proves its fix.**

Give it a GitHub issue and a repository. It finds the relevant code, reproduces the bug, edits
safely, runs the tests on the original code *and* on the fix, and delivers a byte-exact patch with
the evidence attached. Built for DeepSeek and Qwen, token-lean, and crash-proof.

```text
━━ Result: calc-divide ━━                              (make demo: scripted model, not live)
status        completed · termination model_submitted · submission_ready yes
verification  checks_passed — 2 check run(s) on the selected code (details in report.md)
proof         PROVEN — 2 check(s) fail without the change and pass with it
  check                                                original  patched  result
  repro python -c "from calc.ops import divide; asser…     FAIL     pass  fail to pass
  python -m unittest discover -s tests                     FAIL     pass  fail to pass · fixes test_ops.OpsTest.test_divide_true_division
changes       2 files changed, 5 insertions(+), 1 deletion(-)
export        verified: patch applied to a clean base copy reproduces the selected tree exactly
```

---

## Quick start (the official evaluation procedure)

```bash
git clone https://github.com/sanath-2512/gheerefill && cd gheerefill
export AI_API_KEY="<PROVIDED_API_KEY>"
make setup      # offline, ~1 s: finds Python ≥ 3.9 and git; no packages to install
make run        # then paste a GitHub issue URL, owner/repo#N, @issue.md, or the issue text
make test       # 312 deterministic tests: no network, no credentials
```

Without a terminal (automation), everything is an argument:

```bash
make run ISSUE=https://github.com/OWNER/REPO/issues/123               # clone + fetch issue + solve
make run ISSUE="text of the issue" REPO=/path/to/repo                 # local checkout
make run TASK=swe_instances.jsonl                                     # SWE-bench-style rows
```

- **One credential only:** `AI_API_KEY`. Nothing is hard-coded, logged or committed; `.env.example`
  contains only `AI_API_KEY=`.
- **Zero configuration:** the key's format selects the provider. DeepSeek and Alibaba Cloud (Qwen)
  keys, OpenRouter, NVIDIA NIM, Groq, and any OpenAI-compatible server (`AI_BASE_URL`) work out of
  the box. A prescribed model is pinned with `AI_MODEL` or in `profiles/default.toml` and is never
  substituted.
- **Text only**, Python standard library only, runs on Linux and macOS.
- Exit codes: `0` done · `2` configuration / rejected key · `3` quota exhausted · `4` model endpoint
  unreachable.

---

## How it meets the brief

| The brief asks for | What gheerefill does |
|---|---|
| **Understand the issue** | Resolves the issue's own anchors (file paths, stack-trace frames, identifiers) against the repo and ranks files with BM25, with no second model. Supplied tests (`FAIL_TO_PASS`, `test_patch`) are applied and named. |
| **Navigate the repository** | read, search, outline and list tools; module and test relationships for Python, JS/TS, Go and Rust; the repo layout and test commands up front. |
| **Use tools intelligently** | 8 small tools. Tool calls that DeepSeek or Qwen write as text, malformed JSON, and other agents' tool names are recovered and translated automatically. |
| **Manage context** | Only the newest tool outputs travel in full; older ones become one-line pointers the model can expand. Build and test noise is folded. Requests always fit the model's window. |
| **Recover from failures** | Rate limits and server errors are retried within the deadline; long answers stream. Failure memory and fresh attempts break loops, and `.git`, stash and reset damage is undone. It never loses a verified fix: every step is snapshotted and export survives crashes. |
| **Correct, verified changes** | Every check runs twice: on the original code (with the new tests) and on the fix. Only a change that turns failing into passing, with no regressions, is **PROVEN**. Edits that break syntax are refused. |
| **Tokens and compute** | About 2.3× fewer input tokens than resending the full history on long tasks; a failing `cargo test` shown in 0.7k chars instead of 2.9k; retries only when evidence says the fix is wrong; a background Rust pre-build. |

---

## Results

| Evidence | Result |
|---|---|
| Deterministic suite | **312 tests pass** (`make test`), including an attack catalogue, provider emulators and real Go/Rust/Node/Ruby toolchains |
| Fault injection | **0 invariant violations** over 200 seeds with crashes, kills and provider errors (`make chaos`) |
| Clean-machine rehearsal | **9/9** official steps from a fresh clone, empty environment, no terminal (Python 3.9 and 3.13) |
| 46-task gauntlet, offline | **46/46** judged by hidden tests behind DeepSeek- and Qwen-like endpoints, with 0 provider-rule violations. The model is scripted, so this checks the pipeline, not model ability. |
| Live DeepSeek V4.1-Flash | **8/8** owned tasks, **9/10** real-repository tasks (free tier) |
| Live Qwen 3.8-27B | Every task the free pool let finish passed the hidden tests with **proven** evidence; the rest hit free-tier rate limits |

Every claim and its evidence: [NOTES.md](NOTES.md).

---

## How it solves an issue

1. **Localise:** anchors from the issue plus BM25 ranking give the model a short, ranked list of
   files to start from.
2. **Reproduce:** the model registers a command that should fail; the harness runs it on the
   original code at once and tells the model if it does not.
3. **Edit safely:** exact, unique replacements, with lenient matching for indentation and line-number
   slips. A parse check (Python, JSON, Rust via `rustfmt`, Go, JS, Ruby) refuses edits that break
   syntax.
4. **Verify against the counterfactual:** each check runs on the original code plus the new tests,
   and on the fix; failing test names are compared across 17 test runners.
5. **Decide by evidence:** `proven` › `fixed` › `passing` › `unverified` › `refuted`. It retries from
   the original code only when the evidence refutes the fix or the attempt stalls.
6. **Export and attest:** a byte-exact `patch.diff`, verified on a clean copy, plus an in-toto
   attestation (`python -m gheerefill verify --run-dir DIR`).

Each run writes `runs/<task>/<run>/` with `patch.diff`, `report.md`, `result.json` (tokens, context
statistics, proof) and the full transcript.

---

## Built for DeepSeek and Qwen

| Provider behaviour | Handling |
|---|---|
| Thinking mode requires `reasoning_content` back on tool turns | Sent back automatically; old reasoning is trimmed in steps so the prompt cache survives |
| Tool calls leaked as text (DeepSeek DSML, Qwen XML, Hermes JSON) | Recovered for offered tools only |
| Long thinking answers | Streamed: no idle timeouts, bounded by the task deadline |
| Moderation, stream-only models, rate limits, quota | Retried, adapted or reported with a clear exit code |
| DeepSeek and DashScope keys share one format | Endpoints are tried in order; the key moves on only on a 401 |

---

## Robustness and safety

- **Credential isolation:** on Linux, model commands run in a Landlock sandbox that cannot read the
  key. The key is sent only to the endpoint its format maps to.
- **Repository hygiene:** the change is left uncommitted on the original base. Commits, branch
  switches, stashes and even a deleted `.git` are undone, and build lock files stay out of the patch.
- **Hostile input:** shell metacharacters in `ISSUE=`, megabyte issues, tool-call floods, binary and
  non-UTF-8 files, parallel runs.
- **Deadlines:** a monotonic budget with a finalisation reserve; the deliverable is always exported.

---

## Evaluate it yourself

```bash
make gauntlet                   # LIVE: 46 tasks (Rust, JS, Python, Go, Ruby) judged by hidden tests
make gauntlet-offline           # the same tasks behind DeepSeek/Qwen emulators, no key needed
make chaos                      # fault injection
python3 scripts/eval.py --validate-suite    # every hidden test fails on base and passes on the reference
```

---

## Configuration

`profiles/default.toml` holds the model configuration: `[model]` to pin a prescribed model, and
`[[auto]]` rules mapping key formats to providers. Environment overrides: `AI_MODEL`, `AI_BASE_URL`,
`AI_PROVIDER`. Limits: `TIME_LIMIT=` and `MAX_STEPS=` (defaults 1800 s / 150 steps). Randomness is
limited to provider sampling and a seeded retry jitter.

## Layout

```
gheerefill/   runtime (standard library only): agent, tools, context, proof, locate, workspace,
              models (OpenAI-compatible, Anthropic, quirk repair), intake, prewarm, sandbox, attest
profiles/     model configuration (default.toml is the submission profile)
tests/        312 deterministic tests
scripts/      setup, eval, gauntlet generator, provider emulators, chaos, clean-machine rehearsal
evalsuite/    48 owned tasks with judge-owned hidden tests (never read by the runtime)
rehearsal/    rehearsal lab: real pinned repositories, results and terminal logs
```

## Licence and provenance

`gheerefill/shell.py` adapts the process-group timeout pattern of mini-swe-agent (MIT).
`gheerefill/_vendor/tomli` is tomli 2.2.1, verbatim (MIT). Details in [NOTES.md](NOTES.md#8-provenance).
