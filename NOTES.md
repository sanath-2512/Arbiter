# Engineering notes

These are persistent working notes. Update them when evidence changes.

## 1. Contract status

The authoritative source is "AI Harness Hackathon 2026 — Standardised Makefile-Based Evaluation
Setup" (received 2026-09-26).

**Confirmed by the official document**

| Item | Current handling |
|---|---|
| Root `Makefile` with `setup`, `run`, `test` (`clean` where applicable); at minimum setup and run must work | All four exist. Setup is offline and takes ~0.2 s. The official sequence has been rehearsed from a fresh clone. |
| Credential only via `AI_API_KEY`; never hard-coded, committed, or in `.env`/docs | Read once, then scrubbed from the process. Recipes never expand it (`make -n` shows no key). A `.env.example` is provided; `.env` is git-ignored and never overrides the environment. |
| The evaluator runs only `export AI_API_KEY; make setup; make run` and must not edit files or configuration | The model configuration is committed. `provider = "auto"` resolves the endpoint and model from the key format, with no other variables needed. |
| Model configuration clearly defined in the application/config files; use the prescribed model if specified; no substitution | `profiles/default.toml` holds `[model]` plus the `[[auto]]` rules. A pinned name is never replaced. Resolution is printed and recorded. |
| Text-only models and input | Text only. |
| `make run` launches the harness; the GitHub issue/test case is then supplied to the running harness | Interactive console on a TTY (URL, `owner/repo#N`, `@file`, or pasted text). Non-interactive via `ISSUE=`, `TASK=` or stdin. Loops for further issues. |
| A TUI, if any, launches via `make run` | Line-mode console, no full-screen UI. Works over any terminal, and piped. |
| Declare dependencies; document randomness and settings that affect results | Runtime needs only stdlib + git. Randomness is limited to provider sampling (provider default temperature) and seeded retry jitter. Every result records the resolved model, profile hash, and base commit. |

**Still open (no organiser information yet)**

| Item | Current handling |
|---|---|
| Which model/provider is prescribed | Auto-resolution from the key format. Pin it in `profiles/default.toml` once announced. |
| How exactly the issue is "supplied to the running harness" | Every plausible channel is supported (TTY paste/URL, `ISSUE=`, stdin, file, JSON). |
| Whether the repository is pre-provisioned or must be cloned | `REPO=` for a provided checkout; otherwise clone from GitHub. |
| Time/token limits, concurrency, scoring weights | Defaults are 1800 s / 150 steps. Per-task limits are accepted. |
| Network policy in the evaluation environment | Needs HTTPS to the model endpoint, plus GitHub if cloning. |
| Cross-task persistence | None used. |

## 2. Claims and their evidence

| Claim | Implemented | Deterministically tested | Live-tested | Benchmark-supported |
|---|---|---|---|---|
| End-to-end solve path (issue → model → tools → edit → check → export → record) | yes | yes: scripted model, and a scripted HTTP server through the real transport | **no** | no |
| Endpoint compatibility (OpenAI/Anthropic, tool calls, usage, streaming and non-streaming) | yes | yes: local server, request shape, error classes, SSE reassembly | **no** | — |
| Candidate preservation and dominance restoration | yes | yes | no | no |
| Artifact fidelity (add/delete/rename/mode/symlink/CRLF/binary/unicode paths) and clean reconstruction | yes | yes, including `git apply` in a fresh clone | no | — |
| Failure handling (timeouts, descendants, runaway output, cancel, SIGTERM, SIGKILL + recovery, crash, budget exhaustion, context overflow, auth/quota/rate-limit/5xx) | yes | yes | no | — |
| Integrity observations (controller-state / harness-repo access, edits to existing tests) in every result; eval trajectory audit | yes | yes (incl. a simulated `evalsuite/hidden` peek during an eval run) | no | — |
| Better than mini-swe-agent / Pi | — | — | — | **no data** |

The eval pipeline has been exercised end to end for ours, mini-swe-agent and Pi against a
*scripted* OpenAI-compatible server. That checks wiring and matched conditions, not coding ability.
In result records, `model.live=true` only means requests went to a network endpoint (see
`model.base_url`). It does not by itself mean a real model was used.

## 3. Architecture decisions

1. **Own stdlib runtime; mini-swe-agent kept as a pinned baseline, not a dependency.** Meeting the
   brief with mini-swe-agent 2.4.6 would have meant replacing all of the following:
   - its model layer (litellm; a 10× fixed retry; raises on unregistered pricing);
   - its environment (forwards `os.environ`, including the API key, to model commands);
   - its loop (no deadline reserve, no candidates or evidence).

   What remains useful is ~50 lines of pattern, adapted with attribution in `shell.py`. The
   stdlib-only runtime makes `make setup` network-free and removes install risk.
2. **Shadow git store for candidates** (`workspace.py`).
   - Measured on 20k files: first snapshot 0.28 s, incremental 0.04 s. That makes per-step capture
     affordable and catches shell-made mutations that structured edit tools would miss.
   - `info/attributes` disables eol/filters so snapshots are byte-exact.
   - Target-tracked files are force-added, so `.gitignore` cannot hide tracked changes.
3. **Deliverable = working tree left at the selected candidate + byte-exact patch.**
   - The patch has no rename detection, so both `git apply` and `patch` accept its text hunks.
   - Finalisation proves the patch reproduces the selected tree from a clean base.
   - Agent commits, branch switches and staging are undone without touching the working tree.
4. **Evidence is bound to tree ids.** A content change produces a new tree with no evidence, so
   stale verification cannot leak. Selection uses a deterministic dominance rule, not weighted scores.
5. **Rule-based context reduction only.** Old tool outputs are elided with pointers to their
   verbatim archive. Reductions are sticky, keeping the prompt prefix stable for provider caching.
   Pressure escalates on provider-reported overflow. No summariser model.
6. **One solver session.** No reviewer model, no generated tests, no multi-candidate search. These
   stay conditional until paired evaluation shows they add correct submissions under the same budget.
7. **HTTP via urllib; streaming is opt-in (`model.stream`).**
   - Non-streaming is the default: it has the simplest failure semantics.
   - Streaming (SSE) exists for endpoints that are slow enough to hit gateway idle timeouts, and it
     enforces the total deadline mid-response. It is implemented for both providers and tested on
     reassembly, cut streams, deadlines and error events.
   - `make probe` measures both modes, so the choice rests on evidence.
8. **Tests use `unittest`**, so `make test` has zero dependencies and works offline on the evaluator.

## 4. Runtime policies (defaults; each is a profile flag — unmeasured until live runs)

| Policy | Default | Rationale | Measured? |
|---|---|---|---|
| `repo_overview` | on | Top-level listing, manifests and manifest-derived test-command hints (marked unverified) in the first message save 1–2 steps | no |
| `submit_review` | on, at most once | Flags an empty diff, new (possibly scratch) files, or no check since the last edit | no |
| `recover_empty_final` | on (not after a confirmed model submit) | An empty patch cannot pass; the latest archived candidate can | no |
| `dominance_selection` | on | Deterministic; only acts on conflicting evidence from the same check | no |
| `final_recheck` | on | Re-runs the last agent check on the selected tree when evidence is stale (no model tokens); if the new exact evidence shows an earlier candidate dominates, that candidate is restored | no |
| `budget_notices` | on, once | Surfaces remaining steps/time near the end | no |
| `repetition_notice` | on | Same command + same output ×3 | no |
| `git_hygiene` | on | Undoes agent commits/branch/staging so the deliverable is uncommitted changes on the base | no |

## 5. Known limitations

- **No live validation yet.** Prompt quality, tool-use reliability and solve rate with the
  prescribed model are unknown.
- **Isolation.**
  - Filtering environment variables is not isolation. A same-user process can read the harness's
    memory or `/proc/<pid>/environ`.
  - A shell-command denylist is not used and would not be a sandbox.
  - The model's bash can read the harness repository (including `evalsuite/`) and write outside the
    repo. The tool-level write boundary covers only `write_file`/`edit_file`.
  - Such access is *recorded* in `result.integrity` and in the eval audit, not prevented. The
    controller keeps its authoritative state in memory; files in the run directory are mirrors. The
    exception is offline `finalize`, which trusts the run directory.
- **Unsupported artifact types.** Empty directories; content inside nested git repositories or
  submodules; git-lfs smudge semantics. New files under ignored paths are excluded and listed.
- **Recovery.**
  - SIGKILL cannot be handled in-process. Offline `finalize` recovers the deliverable from the
    checkpoint, but only if someone runs it.
  - The in-place working tree at kill time may be mid-edit.
  - Conversation resumption is not supported; only finalisation is.
- **Requests.** Non-streaming by default. Enable `model.stream` if the endpoint needs streaming or
  is slow. A response slower than `request_timeout_s` fails with a classified timeout.
- **Test-output parsing.** Heuristic; unrecognised output is `inconclusive`, never a pass.
- **Process groups.** Processes that call `setsid()` escape process-group cleanup.
- **Upstream quirks.** Pi sends `store` and `max_completion_tokens` (its upstream behaviour). Some
  OpenAI-compatible servers may reject these; if so, set Pi compat flags in `baselines/run_pi.py`
  and document it.

## 6. What we will not build (unless measurement changes the decision)

Web dashboard, visual agent graphs, multi-agent orchestration, vector DB / embeddings, long-term
memory, specialist-agent swarms, RL pipelines, model training, plugin ecosystems, UI polish,
benchmark-specific hacks. The following conditional mechanisms wait for evidence:
- generated tests (ExecCritic-style);
- multiple candidates;
- setup-recipe generation (BootstrapAgent-style);
- model summarisation;
- GEPA-style prompt optimisation, only after trustworthy measurement, with the model/metering/holdout protected.

## 7. Provenance

| Component | Source | Version | Our changes |
|---|---|---|---|
| `gheerefill/shell.py` process-group kill on timeout | mini-swe-agent `environments/local.py` (MIT) | 2.4.6 | Pipe pump with exact output cap, cancel hook, TERM→KILL escalation, leftover cleanup |
| Baseline `mini` | PyPI `mini-swe-agent` | 2.4.6 (`baselines/requirements-mini.lock`) | None to upstream code; runner documents the matching changes |
| Baseline `pi` | npm `@mariozechner/pi-coding-agent` | 0.73.1 (`baselines/pi/package-lock.json`) | None to upstream code; runner documents the matching changes |
| Research references (ExecCritic, SoL-Pi, BootstrapAgent, GEPA, HarnessOpt-Bench, Meerkat) | — | — | Not used in code. Their reported results are not our evidence. |

## 8. Current milestone and next actions

Fixed request prefix: system prompt ~0.3k tokens + tool schemas ~0.9k (full set) or ~0.26k
(bash-only).

**Validated milestone.** A and B, deterministic only:
- the full solve/export path;
- the reliability core;
- all three baselines wired through the eval runner;
- 100+ unittest cases passing (`make test`);
- a clean-checkout rehearsal (see §9).

**Next concrete actions, in order:**
1. When the organisers publish model, endpoint and protocol:
   - pin them in `profiles/default.toml`;
   - replace or extend `task.py` with the official adapter;
   - run `make probe`, then `make smoke`.
2. Run `scripts/eval.py --partition dev --systems ours,mini,pi --repeats 2` live and classify every
   failure using the taxonomy below.
3. Fix the largest observed failure class, then re-run the dev and selection partitions. Only then
   run `final` once.
4. Ablate the policies in §4, starting with `submit_review`, `final_recheck` and `repo_overview`,
   against the default under matched budgets.
5. Rehearse the live demo: `python3 scripts/demo_live.py`.

**Failure taxonomy for live runs:** investigation failure · wrong interpretation · correct
diagnosis/wrong patch · incomplete patch · verification failure · false-positive verification ·
setup failure · API failure · budget exhaustion · context failure · artifact/export failure ·
regression introduced · selector failure.

## 9. Clean-checkout rehearsal log

See the entry appended below by the rehearsal procedure (fresh `git clone` → `make setup` →
`make test` → `make run` with the scripted demo profile).

### 2026-09-26 — rehearsal on commit 3823c1a (+ fixes in the following commit)
Procedure: `git clone -b claude/modest-dirac-h7ees8` into an empty directory, with AI_* variables unset.
- `make setup`: ok, offline, 0.2 s. It picked `/usr/bin/python3.13`.
- `make test`: 100 tests OK (1 skipped: baselines are not installed in a fresh clone).
- `make run TASK=… </dev/null` without credentials: exit 2. Each task gets a `configuration_error`
  record ("model.name is not configured … never substitutes a default model"). The repo is untouched.
- `make demo` (scripted model): `completed / model_submitted / submission_ready=true / checks_passed`,
  labelled `live=false`.
- Found and fixed: one *test* file used a Python 3.12-only f-string, so `make test` failed on 3.11.
  The runtime itself compiled on 3.11. Added `tests/test_compat.py`. All tests now pass on 3.11,
  3.12 and 3.13.
- Not rehearsed: anything live (no credentials or model), and the official task protocol (not published).
