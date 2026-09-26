# gheerefill

An autonomous coding-agent harness for the AI Coding Harness Hackathon. Every team runs the same
prescribed model, so the harness is what decides the outcome. Most harnesses end with the model
saying "done". gheerefill ends with a **proof-carrying patch**. The harness itself checks, by
running code, that:

- the change makes something pass that failed on the original code, and
- it breaks nothing that passed before.

Then it exports a patch that is verified byte-for-byte, with an attestation anyone can re-check
offline.

> **Status:** implemented and deterministically tested: 177 tests, plus 200 fault-injection seeds
> with 0 invariant violations. **Not yet live-validated.** The organisers have not yet supplied the
> prescribed model and a key, so no result here comes from a real model call. See
> [NOTES.md](NOTES.md) for every claim and its evidence.

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
isolation     active — a sandboxed child could not read a parent process's environment
```

## Quick start (the official evaluation procedure)

```bash
export AI_API_KEY="<PROVIDED_API_KEY>"   # the only required configuration
make setup                               # offline: picks Python >= 3.9, checks git, byte-compiles
make run                                 # then supply the issue at the prompt:
#   issue> https://github.com/OWNER/REPO/issues/123      (or OWNER/REPO#123, or @issue.md,
#                                                           or paste the issue text and press Enter)
make test                                # deterministic tests: no network, no credentials
```

Without a terminal, or to pass options:

```bash
make run ISSUE=https://github.com/OWNER/REPO/issues/123        # clone + fetch issue + solve
make run ISSUE="text of the issue" REPO=/path/to/repo         # local repository
make run TASK=tasks.jsonl TIME_LIMIT=900 MAX_STEPS=80         # JSON tasks, explicit limits
make run ISSUE=... BASE=before-issue                          # historical base for a closed issue
```

- `make run` never prompts without a terminal, and exits 0 when no input is given.
- It works from any directory (`make -f /path/to/Makefile run`).
- It needs only Python ≥ 3.9 (standard library) and git ≥ 2.25. `make run` runs `make setup`
  itself if needed.
- At start-up it prints the resolved model, the verified isolation status and where results go.
- Results:
  - **Terminal:** the card above.
  - **Target repository:** the change is left uncommitted on the original base.
  - **Run directory** `runs/<task>/<run>/`:
    - `patch.diff`;
    - `report.md`, where every claim names the file it comes from;
    - `attestation.json`;
    - `result.json` and the full records.
  - Without a terminal, results come as one JSON line per task.

## How it solves an issue

1. **Localise.** The issue's own anchors are resolved against the repository, deterministically
   and without an extra model: file paths, stack-trace frames, and definitions of the identifiers
   it mentions. Source files are ranked with BM25. The first prompt shows these as unverified hints
   (`localization.json`).
2. **Reproduce.** The model registers a reproduction command (`register_reproduction`). The harness
   runs it on the original code *at once* and tells the model whether it really fails there. A
   reproduction that passes on buggy code is caught before it can mislead.
3. **Edit safely.** Exact and unique string edits are used:
   - A near miss shows the most similar region verbatim.
   - An edit that would make an existing `.py` or `.json` file unparseable is refused, with the
     error location. This is confirmed with the project's own interpreter first.
4. **Verify against the counterfactual.** At submit, each of the agent's test commands, and each
   registered reproduction, is run twice:
   - on the original code *plus the patch's own test changes*, so new tests exist but the fix
     does not;
   - on the patched code.

   Failing test names are compared (pytest, unittest, go, cargo, jest, rspec, junit). The result is
   the **fail→pass** set (the fix is demonstrated) and the **pass→fail** set (regressions, reported
   to the model by name).
5. **Decide by evidence.** Each candidate gets a deterministic level:

   | level | meaning |
   |---|---|
   | `proven` | ≥1 check fails without the change and passes with it; no regression; nothing unchecked; existing tests not edited |
   | `fixed` | fail→pass shown, no regression found, but something could not be compared (or existing tests were edited) |
   | `passing` | checks pass, but nothing was shown to fail without the change |
   | `unverified` | no conclusive evidence |
   | `refuted` | a regression, or a confirmed reproduction still failing |

6. **Retry only when it pays.** A new attempt starts from the original code, with a fresh context
   and the harness's observations. It happens only when a submitted candidate is `refuted`, or an
   attempt used its time share without a verified change, and only if time remains. Easy issues
   finish in one attempt. Candidates from all attempts are cross-checked on every reproduction and
   ranked by:
   1. level;
   2. fail→pass checks;
   3. agreement between attempts (CodeT's dual execution agreement);
   4. patch size.
7. **Export and attest.** The selected state is exported as a byte-exact patch and verified by
   applying it to a clean copy of the base. `attestation.json` is an in-toto Statement binding the
   patch digest to the trees, the proof and the digests of every cited output.
   `python -m gheerefill verify --run-dir DIR [--rerun]` re-checks it offline.

Why this design: see [NOTES.md](NOTES.md#why-this-design) for the evidence behind each choice
(LangChain's harness-only gains, Agentless, TestPrune, CodeT, SWE-Replay, the 2026 harness-design
study).

## Robustness and safety

| Mechanism | What it guarantees |
|---|---|
| **Crash-proof finalisation** | Every step's working tree is snapshotted into a shadow git store in the run directory; nothing is written to the target's `.git`. Finalisation always runs: after a crash, SIGTERM or budget exhaustion. After SIGKILL, `python -m gheerefill finalize --run-dir DIR` finishes the run offline, without model calls. |
| **Fault injection** | `make chaos` runs random trajectories with provider errors, crashes, cancellations, multiple attempts and real SIGKILL+recovery. It checks eight invariants, including: the patch reconstructs; the tree equals the selection; the target's HEAD and index are restored; every request is counted; the attestation verifies. It found one accounting bug, which is fixed and has a regression test. |
| **Credential isolation** | Model commands, and the harness's git calls into the target, run in a Linux Landlock domain with the ptrace-type capabilities dropped. They cannot read the environment of the harness, `make` or the evaluator's shell. This needs no root, container or packages, and is verified by a self-test at startup. The harness also scrubs the key from its own memory image, and the key is only ever sent to the one endpoint its format maps to. |
| **Bounded, honest accounting** | A monotonic deadline and a finalisation reserve. Every request attempt is counted. Interrupted or failed requests are recorded as usage-unknown, never zero. |
| **Provider tolerance** | Errors are classified and retried within bounds, deadline-aware and honouring Retry-After. The harness adapts to parameter rejections: an output-token cap, `max_tokens` vs `max_completion_tokens`, temperature. Restricted keys that cannot list models still work. Anthropic prompt caching is on. Streaming is optional. |
| **Safe repository memory** | Later runs on the same repository see facts the harness *observed by execution*: test commands that ran, and installs that succeeded. Never code, patches or issue text. |

## Configuration

- **Model** (`profiles/default.toml`, committed):
  - If a model is prescribed, set `[model] provider/name/base_url`. It is then used exactly and
    never substituted.
  - Until then `provider = "auto"`. Ordered `[[auto]]` rules map the key's *format* to exactly one
    provider endpoint and a model preference list. The first preferred model that the provider
    lists is used and recorded.
  - `AI_MODEL`, `AI_BASE_URL` and `AI_PROVIDER` override the profile.
  - `make check-config` shows the resolution without spending tokens. `make probe` tests tool
    calling live.
- **Policies** are profile flags, strictly validated:
  - `verify_at_submit`;
  - `max_attempts` (3), `first_attempt_share`, `retry_below`, `min_attempt_s`;
  - `localize`, `memory`, `tools.syntax_guard`;
  - `sandbox` (`key` | `confine` | `off`), plus the context and budget settings.
- **Limits:** per-task limits from the evaluator take precedence over the profile. `TIME_LIMIT` and
  `MAX_STEPS` on the command line win over both.

## Development evaluation (dev only; needs live credentials)

```bash
make baseline-setup                                   # pinned mini-swe-agent 2.4.6 + Pi 0.73.1
python3 scripts/eval.py --validate-suite              # hidden tests fail on base, pass on reference
make eval SYSTEMS=ours,mini,pi                         # same model, same limits, same tasks
```

The summary reports:
- pass@1 and pass^k over repeats;
- tokens, wall time and cost per solved task;
- a paired sign test against each baseline;
- a calibration table: how often each proof level was confirmed by the hidden tests.

`evalsuite/` holds 8 owned tasks (Python, JS, Go) with judge-owned hidden tests, split into dev,
selection and final partitions.

## Layout

```
gheerefill/   runtime (stdlib only)
  cli, intake (issue URL/text/JSON), resolve (model), config, agent (controller, attempts),
  proof (counterfactual evidence), locate (anchors + BM25), memory, tools, shell, workspace
  (shadow git), evidence, attest (in-toto + verify), budget, context, prompts, report, sandbox
  _vendor/tomli (MIT; only on Python 3.9/3.10)
profiles/     run profiles (default.toml is the submission profile)
tests/        deterministic unittest suite (no network, no credentials)
scripts/      setup, py.sh, chaos (fault injection), eval, examples, live demo
evalsuite/    dev evaluation tasks: evaluation boundary, never read by the runtime
baselines/    pinned upstream baselines (dev only)
docs/         proof-v1.md (attestation predicate)
```

## Licence and provenance

- `gheerefill/shell.py` adapts the process-group timeout pattern from mini-swe-agent (MIT,
  © 2025 Kilian A. Lieret and Carlos E. Jimenez).
- `gheerefill/_vendor/tomli` is tomli 2.2.1, verbatim (MIT, © 2021 Taneli Hukkinen). It is used
  only where the standard library lacks `tomllib`.
- The baselines are installed from pinned upstream releases, not vendored.

Details are in [NOTES.md](NOTES.md#provenance).
