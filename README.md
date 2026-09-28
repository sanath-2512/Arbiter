# Arbiter

**An autonomous coding agent that proves its fix before it hands it over.**

Give Arbiter a GitHub issue and a repository. It finds the relevant code, reproduces the bug, edits
safely, and runs the tests twice: on the original code and on the fix. You get back a byte-exact
patch plus the evidence that it works.

```text
━━ Result: calc-divide ━━
status        completed · termination model_submitted · submission_ready yes
verification  checks_passed — 2 check run(s) on the selected code (details in report.md)
proof         PROVEN — 2 qualified generated check(s) fail without the change and pass with it
  check                                                original  patched  result
  repro python -c "from calc.ops import divide; asser…     FAIL     pass  fail to pass
  python -m unittest discover -s tests                     FAIL     pass  fail to pass · fixes test_ops.OpsTest.test_divide_true_division
changes       2 files changed, 5 insertions(+), 1 deletion(-)
export        verified: patch applied to a clean base copy reproduces the selected tree exactly
usage         8 requests · 10115 tokens · 1.429 s
```
<sub>Real output of `make demo` (a scripted model, so it runs offline).</sub>

---

## How to use

```bash
git clone https://github.com/sanath-2512/arbiter && cd arbiter
export AI_API_KEY="<your key>"
make setup      # checks Python ≥ 3.9 and git; nothing to install
make run        # paste a GitHub issue URL, owner/repo#N, @issue.md, or the issue text
make test       # 315 offline tests: no network, no key
make demo       # offline demo with a scripted model
make clean      # remove everything the runs created
```

Everything can also be passed as arguments:

```bash
make run ISSUE=https://github.com/OWNER/REPO/issues/123          # clone the repo, fetch the issue, fix it
make run ISSUE="text of the issue" REPO=/path/to/repo            # a local checkout
make run TASK=tasks.jsonl                                        # a batch of tasks (SWE-bench style rows)
```

| Setting | Meaning |
|---|---|
| `AI_API_KEY` | The only credential. Its format selects the provider; it is never written to disk or logs. |
| `AI_MODEL`, `AI_BASE_URL` | Optional: pin a model or point at any OpenAI-compatible endpoint. The model is never substituted. |
| `TIME_LIMIT=`, `MAX_STEPS=` | Limits per task (defaults 1800 s and 150 steps). |

Requirements: Python 3.9+ and git on Linux or macOS. Standard library only; no packages.

---

## What makes it unique

1. **Proof, not a claim.** Every check runs on the original code (with the new tests added) and on the
   fix. A fix is **PROVEN** only when checks that fail on the original pass with the change, with no
   regressions. Every result carries an evidence level: `proven` › `fixed` › `passing` › `unverified`
   › `refuted`.
2. **Confirmed reproductions.** The model registers a command that should show the bug; Arbiter runs
   it on the original code itself and tells the model at once if it does not fail.
3. **It never loses a working fix.** Every step is snapshotted in a private git store. If the model
   breaks something later, crashes, or runs out of time, the best verified version is still
   exported, and the patch is checked by applying it to a clean copy.
4. **Few tokens by design.** Only the newest tool outputs are sent in full; older ones become
   one-line pointers the model can reopen. Build and test noise is folded (a failing `cargo test`
   shrinks from 2.9k to 0.7k characters). Small files the search pinpoints are shown up front, so no
   request is spent reading them. Every request is kept inside the model's context window.
5. **Finds the code without a second model.** File paths, stack-trace frames and identifiers from the
   issue are matched against the repository, then ranked with BM25 and the import/test graph
   (Python, JavaScript/TypeScript, Go, Rust).
6. **Safe edits.** Exact, unique replacements with tolerance for indentation and copied line numbers.
   Edits that would break syntax (Python, JSON, Rust, Go, JavaScript, Ruby) are refused.
7. **Robust to real models.** Tool calls written as plain text, malformed JSON and unfamiliar tool names
   are recovered. Rate limits and server errors are retried within the deadline, and long answers are
   streamed.
8. **Safe to run.** On Linux, the model's commands run in a sandbox that cannot read the API key.
   Commits, branch switches, stashes and even a deleted `.git` are undone. Rust projects are
   pre-built in the background while the model reads the code.

---

## What you get

**In the terminal:** a live line for every step (tool call, result, harness checks on the original
and on the fix), then the result card shown above followed by the patch.

**In `runs/<task>/<run>/`:**

| File | Contents |
|---|---|
| `patch.diff` | The fix, byte-exact and verified on a clean copy of the repository |
| `report.md` | Readable report: outcome, the proof table, the deliverable, every action taken |
| `result.json` | Machine-readable result: status, evidence level, tokens, timing, context statistics |
| `attestation.json` | Tamper-evident record binding the patch to its proof (SHA-256 digests); check it offline with `python3 -m arbiter verify --run-dir DIR` |
| `evidence.jsonl` | Every check with its outcome on the original code and on the fix |
| `transcript.jsonl`, `actions.jsonl`, `requests.jsonl` | Full conversation, every tool call, every model request with its token usage |

The working tree of the repository is left at the fix, uncommitted, on its original base.

**Exit codes:** `0` done · `2` configuration or rejected key · `3` quota exhausted · `4` model
endpoint unreachable.

---

## Check it yourself

```bash
make test                                   # 315 offline tests, including attack and fault cases
make gauntlet-offline                       # 46 tasks (Rust, JS, Python, Go, Ruby) judged by hidden tests, no key
make gauntlet                               # the same 46 tasks with your live model
make chaos                                  # fault injection: crashes, kills, provider errors
python3 scripts/eval.py --validate-suite    # every hidden test fails before the fix and passes after
```

## Layout

```
arbiter/      the harness: agent loop, tools, context, proof, localisation, workspace, models
profiles/     model configuration (default.toml is used by make run)
tests/        offline test suite
scripts/      setup, evaluation, task generator, fault injection
evalsuite/    48 tasks with hidden tests that the harness never reads
```

## Licence and provenance

`arbiter/shell.py` adapts the process-group timeout pattern of mini-swe-agent (MIT).
`arbiter/_vendor/tomli` is tomli 2.2.1, verbatim (MIT). Design notes and measurements: [NOTES.md](NOTES.md).
