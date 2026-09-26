# Engineering notes

These are persistent working notes. Update them when the evidence changes.

## 1. Contract status

The authoritative source is "AI Harness Hackathon 2026 — Standardised Makefile-Based Evaluation
Setup" (received 2026-09-26).

**Confirmed by the official document**

| Item | Current handling |
|---|---|
| Root `Makefile` with `setup`, `run`, `test` (and `clean`); setup and run must work | All exist. Setup is offline (~0.2 s). Recipes `cd` to the harness, so `make -f /path/Makefile run` works from anywhere. `make run` runs setup itself if it was skipped. |
| Credential only via `AI_API_KEY`; never hard-coded, committed, or in `.env`/docs | Read once, normalised (whitespace/quotes), then scrubbed from the process. Recipes never expand it (`make -n` shows no key). A `.env.example` holds only `AI_API_KEY=`; `.env` is git-ignored and never overrides the environment. |
| The evaluator runs `export AI_API_KEY; make setup; make run` and does not edit files | The model configuration is committed. `provider = "auto"` resolves endpoint and model from the key format; no other variables are needed. |
| Model configuration clearly defined; use the prescribed model; no substitution | `profiles/default.toml` holds `[model]` and the `[[auto]]` rules. A pinned name is never replaced. Resolution is printed and recorded. Nothing else ever calls a model: localisation is BM25, and selection is execution evidence. |
| Text-only models and input | Text only. Screenshots in issues are announced as not visible, not silently dropped. |
| `make run` launches the harness; the issue/test case is then supplied to it | Interactive console on a TTY: URL, `owner/repo#N`, `@file`, or a pasted text (bracketed paste: paste, then Enter; `/go` fallback). A supplied failing test is run first and fixed in the code, not the test. Also `ISSUE=`, `TASK=`, stdin. Loops for further issues; Ctrl-C at the prompt exits. |
| Environment independence; reproducible execution | Python ≥ 3.9 standard library only (tomli vendored for 3.9/3.10) and git ≥ 2.25. Tested on 3.9, 3.10, 3.11, 3.13. Randomness is limited to provider sampling and seeded retry jitter. Every result records the resolved model, profile hash, base commit and an attestation. |

**Still open (no organiser information yet)**

| Item | Current handling |
|---|---|
| Which model/provider is prescribed | Auto-resolution from the key format. Pin it in `profiles/default.toml` once announced. |
| How the issue is "supplied to the running harness" | Every plausible channel is supported (TTY URL/paste, `ISSUE=`, stdin, file, JSON). |
| Whether the repository is pre-provisioned | `REPO=` for a provided checkout. A checkout of the issue's repository in the `make` directory, or at `/testbed`, is used in place; its `.git/config` is read as text. Otherwise the repository is cloned. |
| Time/token limits, scoring | Defaults are 1800 s / 150 steps. Per-task limits are accepted; `TIME_LIMIT`/`MAX_STEPS` override both. |
| Network policy | Needs HTTPS to the model endpoint, plus GitHub when cloning. |

## 2. Claims and their evidence

| Claim | Implemented | Deterministically tested | Live-tested | Benchmark-supported |
|---|---|---|---|---|
| End-to-end solve path (issue → model → tools → edit → check → proof → export → attestation) | yes | yes: scripted model, and a scripted HTTP server through the real transports | **no** | no |
| Proof-carrying patches: reproductions confirmed on the original code; counterfactual fail→pass / pass→fail by test name; evidence levels | yes | yes (`tests/test_proof.py`: parsers, verdicts, levels, counterfactual tree, flows) | **no** | no |
| Adaptive attempts: retry only on refuted/stuck; cross-checked ranking with CodeT agreement | yes | yes (refuted→proven, stuck→restart, no retry when verified, recovery keeps all candidates) | **no** | no |
| Localisation hints (issue anchors + BM25) | yes | yes | no | no |
| Edit syntax guard (confirmed with the project's interpreter) and near-miss hints | yes | yes | no | no |
| Execution-verified repository memory | yes | yes (a second clone of the same repository sees the facts, never code) | no | no |
| Attestation + offline `verify` (+ `--rerun`) | yes | yes (verifies; detects a tampered patch) | — | — |
| Robustness under faults: 8 invariants over random trajectories, crashes, cancellations, SIGKILL + recovery | yes | yes: 200 seeds / 0 violations (`make chaos`); 8 seeds in `make test`. Found and fixed an uncounted-request bug. | — | — |
| Endpoint compatibility (OpenAI/Anthropic, tool calls, usage, streaming, output-cap adaptation, restricted keys) | yes | yes (local servers, error-message fixtures) | **no** | — |
| Artifact fidelity and clean reconstruction | yes | yes, including `git apply` in a fresh clone | no | — |
| Credential isolation from model commands and target-repo git | yes | yes (real process chain with a control run) | no | — |
| Official procedure (`export AI_API_KEY; make setup; make run` + URL/text/file/JSON; pty session) | yes | yes (fake GitHub API + local remote; bracketed paste and Ctrl-C through a pty) | no | — |
| Better than mini-swe-agent / Pi | — | — | — | **no data** |

The eval pipeline has been exercised end to end for ours, mini-swe-agent and Pi against a
*scripted* OpenAI-compatible server. That checks wiring and matched conditions, not coding ability.
`model.live=true` in a record only means requests went to a network endpoint.

## 3. Why this design

The model is fixed and identical for every team, so only the harness can change the outcome. The
published evidence on what moves a fixed model's resolve rate, and what we took from it:

| Evidence | What it shows | What we built |
|---|---|---|
| LangChain Deep Agents, Terminal-Bench 2.0: 52.8% → 66.5% (top-30 → top-5) with the model unchanged | Harness-only gains came from self-verification loops, environment context and doom-loop detection, not prompt tweaks | Submit gate with the harness's own verification; repo overview + localisation; repetition notice; "nothing verified yet" nudge |
| Agentless (reproduction tests kept only if they fail on the original code; regression filtering; voting) | Generated reproductions are only useful once confirmed on the original code | `register_reproduction` runs on the original code immediately and tells the model if it does not reproduce |
| TestPrune, FSE 2026: reusing existing regression tests gives +8–13% relative resolution | Existing tests are a cheap, strong signal against regressions | Counterfactual comparison of the agent's own test commands; pass→fail by test name |
| CodeT (dual execution agreement): +18.8 pass@1 | Candidates that agree on passing the same tests are more likely right | Agreement term in the cross-attempt ranking |
| Repeated attempts + consensus, e.g. Risa 44.9 → 48.2; SWE-Replay: −17% cost by reusing exploration | Extra attempts help, but blind resampling is wasteful | Retry only when evidence refutes or the attempt is stuck; the next attempt inherits the harness's observations, not the transcript |
| Harness-design study (Sep 2026, 176 settings): context management mainly prevents overflow; planning helps weak models; tools help weak-bash models | Components should suit the model | Overflow learning; full tools and bash-only profiles; no planner model |
| SWE-agent ACI: linting on edit | Refusing syntax-breaking edits prevents cascades | Syntax guard, confirmed with the project's interpreter |
| Self-evolving skill libraries can "misevolve" (arXiv 2608.12851) | Learned procedures can drift into unsafe or wrong behaviour | Memory limited to execution-observed facts; nothing the model merely claims is kept |

**What rivals are likely to build**, and why we did not:
- **Multi-agent planner/executor/reviewer stacks:** cost and latency, and planning adds little for
  strong models.
- **An LLM judge:** it approves wrong patches.
- **Hermes-style memory and skills:** misevolution and leakage risk.
- **Embedding search:** a second model risks the prescribed-model rule.
- **Parallel repository copies:** editable installs make copies test the wrong code.

Our counter-position is to make each decision rest on an execution result, and to show the evidence
to the judge.

## 4. Architecture decisions

1. **Own stdlib runtime; mini-swe-agent kept as a pinned baseline.** Using mini-swe-agent 2.4.6
   would have meant replacing its model layer (litellm, fixed retries), its environment (it forwards
   `os.environ`, including the key, to model commands) and its loop (no deadline reserve, no
   candidates). Stdlib-only makes `make setup` offline and install-risk free.
2. **Shadow git store** for candidates and for the harness's detours. The store gives:
   - byte-exact snapshots, 0.04 s per step at 20k files;
   - counterfactual trees built from base plus test paths, in a temporary index;
   - restores that are verified by tree id.
3. **Deliverable = working tree at the selected candidate + byte-exact patch**, proven to
   reconstruct from a clean base. Agent commits, branch switches and staging are undone.
4. **Evidence bound to tree ids.** A content change gives a new tree with no evidence, so stale
   verification cannot leak. Levels and ranking are deterministic functions of the records
   (`proof.py`).
5. **Harness detours.** To run a check on another state, the harness snapshots the model's state,
   restores the target state, runs the check, restores back and verifies the tree id.
   - `state.json` records the pending detour.
   - Recovery restores the selected candidate regardless.
6. **Attempts are sequential in one working tree**, not parallel copies. This keeps environment
   fidelity: editable installs, node_modules and build caches all stay valid. Time shares apply per
   attempt:
   - attempt 1 gets 60% of the work time when more attempts are allowed;
   - later attempts split the rest.
7. **Rule-based context reduction only**, with sticky elision so the prompt prefix stays cacheable.
   Anthropic prompt caching is enabled. No summariser model.
8. **HTTP via urllib.** Streaming is opt-in.
9. **Tests use `unittest`**, so `make test` has zero dependencies and runs offline.

## 5. Runtime policies (profile flags; unmeasured until live runs)

| Policy | Default | Rationale |
|---|---|---|
| `verify_at_submit` | on | The harness runs the agent's checks and reproductions on the counterfactual and the candidate before accepting |
| `submit_review` | on: one review; a second only if the evidence refutes | Surfaces regressions by name, unconfirmed fixes, scratch files |
| `max_attempts` / `first_attempt_share` / `retry_below` / `min_attempt_s` | 3 / 0.6 / `unverified` / 120 s | A submitted candidate is retried only if refuted. A stuck attempt (time share used without a fixed/proven change) is retried. No retry without time. |
| `localize` | on | Anchors + BM25 hints (≤3 s); unverified |
| `memory` | on (CLI only) | Execution-observed test/install commands from earlier runs on the same repository |
| `tools.syntax_guard` | on | Refuses edits that break an existing parseable `.py`/`.json`; repeat to override |
| `repo_overview`, `budget_notices`, `repetition_notice` | on | Context; wrap-up; loop detection |
| `recover_empty_final`, `dominance_selection`, `final_recheck`, `git_hygiene` | on | Within-attempt selection and deliverable hygiene |
| `sandbox` | `key` | Landlock domain for model commands and target-repo git |

## 6. Known limitations

- **No live validation yet.** Prompt quality, tool-use reliability, solve rate and the calibration
  of the proof levels are unknown until the prescribed model is available.
- **Proof scope.**
  - A `proven` level covers only the checks that were run.
  - A wrong reproduction can make a wrong fix look proven. Cross-checking across attempts reduces
    this but does not remove it.
  - Test names are parsed heuristically; without names, counts are compared.
  - The counterfactual tree takes test files by path pattern. Test helpers stored elsewhere are
    not carried over.
- **Isolation.**
  - Model commands cannot read the credential from other processes' environments (verified at
    start-up).
  - They can read the harness repository and write outside the target repository. Such access is
    recorded in `result.integrity`, not prevented.
  - Isolation applies to model commands, not to the harness process itself.
- **Unsupported artifact types:** empty directories, nested repositories/submodules, git-lfs
  smudge.
- **Recovery.** SIGKILL cannot be handled in-process. Offline `finalize` recovers the deliverable,
  but someone has to run it.
- **Interactive paste.** Terminals cap a single line at ~4 KB in line mode. For very long single
  lines, use `ISSUE=@file`.
- **Process groups.** Processes that call `setsid()` escape process-group cleanup.

## 7. What we did not build, and why

| Not built | Reason |
|---|---|
| Multi-agent orchestration, planner/reviewer models | Cost and latency; the evidence shows planning helps only weak models. Our verification is execution-based instead. |
| LLM-as-judge selection | Approves wrong patches; selection uses execution evidence only |
| Embeddings / vector DB | Needs a second model (prescribed-model rule); BM25 needs none |
| Self-writing skill library | Misevolution and cross-task leakage risk; memory is limited to observed facts |
| Parallel attempts in repository copies | Environment fidelity (editable installs, caches, ports) |
| MCTS / tree search | Complexity without evidence of benefit at this budget |
| Model summarisation of context | Extra calls; rule-based elision suffices |
| `apply_patch` (V4A) tool for GPT-family models | Model-specific and unvalidated without the prescribed model; `edit_file` works for all |
| Transcript replay command | `verify --rerun` covers re-checking the result; model outputs are not replayable on hosted APIs anyway |
| Mined real-repository dev tasks | Without a live key they would yield no measurement; the 8 owned tasks validate the pipeline |
| Full-screen TUI, web dashboard | Line console + proof card + `report.md` are enough and work over any terminal or pipe |
| Docker/containers, egress proxy, confine-by-default | Not needed for the credential property (verified per run) and risky under an unknown evaluator environment |
| Implicit `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`, Azure key headers | Implicit routing of the key is risky; only the documented `AI_*` overrides apply |
| GitHub Enterprise/GitLab intake, PR creation | Outside the official procedure; `GHEEREFILL_GITHUB_API` exists for a GHE API |
| Confining the harness process itself | Considered and not pursued in this version; the limitation is documented in §6 |

## 8. Provenance

| Component | Source | Version | Our changes |
|---|---|---|---|
| `gheerefill/shell.py` process-group kill on timeout | mini-swe-agent `environments/local.py` (MIT) | 2.4.6 | Pipe pump with an exact output cap, cancel hook, TERM→KILL, leftover cleanup |
| `gheerefill/_vendor/tomli` | PyPI `tomli` sdist (MIT) | 2.2.1 (sha256 `cd45e1dc…45ff`) | None (verbatim); imported only without `tomllib` |
| Baseline `mini` | PyPI `mini-swe-agent` | 2.4.6 (lock file) | None to upstream code |
| Baseline `pi` | npm `@mariozechner/pi-coding-agent` | 0.73.1 (lock file) | None to upstream code |
| Ideas (not code) | CodeT (arXiv 2207.10397); Agentless (2407.01489); TestPrune (2510.18270); SWE-Replay (2601.22129); Risa (2608.22191); harness-design study (2609.20804); LangChain Deep Agents blog; in-toto Statement v1; BM25 | — | Re-implemented from the published descriptions. Their reported numbers are not our evidence. |

## 9. Next actions

1. **With a real key**, in this order:
   - `make probe`;
   - `make eval SYSTEMS=ours,mini,pi` with `--repeats 2` on dev;
   - classify every failure (taxonomy below);
   - read the proof-level calibration table.
2. **Decision rule, fixed in advance:**
   - Keep a mechanism only if dev+selection loses no task the default solves and it saves ≥10% of
     tokens, or it solves ≥1 more task at ≤1.2× cost. Otherwise switch it off in the profile.
   - Run the final partition once, at the end.
3. **When the model is prescribed:** pin it in `[model]`, re-run `make probe`, and revisit
   `max_attempts` against the organisers' time limit.

**Failure taxonomy for live runs:** investigation failure · wrong interpretation · correct
diagnosis/wrong patch · incomplete patch · verification failure · false-positive proof ·
false-negative proof · setup failure · API failure · budget exhaustion · context failure ·
artifact/export failure · regression introduced · selector failure.

## 10. Rehearsal log

### 2026-09-26 — rehearsal on commit 3823c1a
- Procedure: fresh `git clone`, AI_* variables unset.
- `make setup`: ok, offline, 0.2 s.
- `make test`: 100 tests OK.
- `make run` without credentials: exit 2, `configuration_error`, repository untouched.
- `make demo`: completed, checks passed, `live=false`.
- Found and fixed a 3.12-only f-string in a test.

### 2026-09-26 — portability and proof pipeline
- **Test suite:** 142 tests pass on Python 3.9 (uv-installed), 3.10, 3.11 and 3.13.
  `tests/test_compat.py` parses every source file with the 3.9 grammar and imports it on the older
  interpreters that are present.
- **Make invocations:**
  - `make -f /abs/Makefile run TASK=rel.json OUT="my runs" MAX_STEPS=7` from another directory
    works; paths resolve against the caller directory.
  - `make run` without a prior setup runs setup itself.
- **Proof pipeline:**
  - 177 tests pass.
  - `make chaos` passes 200 seeds (25 SIGKILL + recovery) with 0 invariant violations, after fixing
    the one bug it found (a non-`ModelError` client exception went uncounted).
  - `make demo` shows `PROVEN`: the reproduction and the unittest suite both fail on the original
    code and pass on the patch. `gheerefill verify --rerun` reproduces both verdicts.
