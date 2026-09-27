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
| Which model/provider is prescribed | Organisers indicated Qwen and DeepSeek models. The `sk-` + 32-hex rule tries DeepSeek, then DashScope regions and QwenCloud (moving on only on 401); `sk-sp-` keys use the Coding Plan. Pin the exact model in `profiles/default.toml` once announced. |
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
| DeepSeek/Qwen wire behaviour (reasoning passback, null content next to calls, leaked DSML/XML/Hermes calls, `<think>`, moderation 400, stream-only, cache-hit fields, key resolution across vendors) | yes | yes: `scripts/provider_emulator.py` enforces each family's documented request rules and injects its documented output quirks at seeded rates; 8/8 seeds solved with 0 rule violations (`tests/test_provider_emulation.py`) | **no** (endpoints unreachable here) | no |
| Attack catalogue: make metacharacters, POSIX locale, unreachable endpoint, empty input, no commits, detached HEAD + merge, awkward names, `rm -rf .git`, `git init`, `git stash`, dirty start, file-system refusals, 6000 files, 40-call flood, terminal noise, chatty model, 3 MB write, 1 MB issue, lock-file byproducts | yes | yes (`tests/test_attacks.py`); each break it found is fixed at the cause (§10) | — | — |
| Real toolchains: Go, Rust, Node (`node --test`), Ruby (minitest) repositories solved end to end behind the emulators; runner output read as failed → passed | yes | yes (`tests/test_languages_e2e.py`) | no | — |
| Task rows of the SWE-bench family; supplied tests applied, named, read-only to the model, kept out of the patch; reference solutions dropped on input | yes | yes (`tests/test_task_formats.py`, `tests/test_evaluation_tests.py`) | no | — |
| Mechanism rehearsals on real pinned repositories (scripted policies: stuck loop, late regression, wrong generated test, stale edit, API faults, bad/missing key, SIGTERM/SIGKILL, budget exhaustion, hostile tools) | yes | expectations met 25/25 plain, 22/22 behind the DeepSeek emulator, 22/22 behind the Qwen emulator (`rehearsal/results/mechanisms*`) | — | not solve-rate evidence: the policy knows the fix |
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
10. **The target's `.git` is copied at start** (objects hard-linked, control files copied; up to
    1 GB copied when hard links cross devices). A model command that removes or replaces it is
    undone immediately and at finalisation, so an evaluator's `git diff` still works.
11. **Two timeouts per model request.** `request_timeout_s` bounds each wait for data; the whole
    response may take up to 4× that within the task deadline. Thinking models stream (or keep the
    connection alive) for minutes; a single total cap had them killed and retried.
12. **Reasoning passed back is shortened in steps of 8 turns**, not on a sliding window. A sliding
    cut-off changes one old message per request and voids the provider's prefix cache for every
    turn after it; stepped, the cached prefix changes once per step (`tests/test_context.py`).
13. **Lenient edits only when exactly one region fits.** A read_file line-number gutter copied into
    `old_str`, or indentation written differently (tabs vs spaces, a uniform offset), is matched and
    `new_str` re-indented the same way; anything ambiguous is still refused with the nearest text.

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
  of the proof levels are unknown until the prescribed model is available. This environment's
  network policy blocked api.deepseek.com, dashscope*.aliyuncs.com, huggingface.co and ollama.com,
  so neither the hosted APIs nor open weights could be exercised.
- **The emulators are built from documentation and public issue reports** (§11), not from captured
  traffic. A real deployment can differ; the adaptive paths (passback learned from the error text,
  stream-only switch, parameter rejections) exist for that, but are verified only against the
  emulated error messages.
- **DashScope explicit cache markers are not sent.** Only implicit (automatic) prefix caching is
  relied on; adding `cache_control` content parts risks a 400 on untested deployments.
- **Two runs on the same local checkout at once are not protected** (clones in `workspace/` are).
- **Token figures in the rehearsal results are estimates** (the emulators count characters / 4).
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
| GitHub Enterprise/GitLab intake, PR creation | Outside the official procedure; `ARBITER_GITHUB_API` exists for a GHE API |
| Confining the harness process itself | Considered and not pursued in this version; the limitation is documented in §6 |

## 8. Provenance

| Component | Source | Version | Our changes |
|---|---|---|---|
| `arbiter/shell.py` process-group kill on timeout | mini-swe-agent `environments/local.py` (MIT) | 2.4.6 | Pipe pump with an exact output cap, cancel hook, TERM→KILL, leftover cleanup |
| `arbiter/_vendor/tomli` | PyPI `tomli` sdist (MIT) | 2.2.1 (sha256 `cd45e1dc…45ff`) | None (verbatim); imported only without `tomllib` |
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

### 2026-09-26 — attack catalogue, DeepSeek/Qwen emulation, real toolchains (commits 274d530 and later)
- **Breaks found and fixed** (each has a regression test):
  - `make run ISSUE='... $(shell cmd) ...'` ran `cmd` through make and mangled `$$`/`$(X)`: inputs
    are now taken literally.
  - `rm -rf .git` / `git init` by the model left the target without its history: restored.
  - `git stash` then submit delivered an empty patch although the fix had passed its checks: the
    latest passing candidate is delivered, and the notice names the vanished edits.
  - A full SWE-bench row would have stored the gold `patch` in `task.json`, readable by the model:
    reference-solution fields are dropped on input.
  - `repo` + `repo_path`, or `id` + `task_id`, in one row rejected the whole task: priority order.
  - Two parallel tasks of one repository could share a clone: per-clone lock.
  - A Latin-1 source file crashed the edit tool's output path: non-UTF-8 text round-trips; OS and
    Unicode errors become tool errors.
  - A long streamed thinking answer was cut at 300 s total and retried: gap and total are separate.
  - `cargo test` put `Cargo.lock` into the patch: lock-file byproducts are left out.
  - An empty `ISSUE="   "` is treated as no issue (exit 0, "no task supplied", no model call).
- **Mechanism matrices** (scripted policies on real repositories, re-run on clean commit 1b4f978):
  25/25 expectations met plain; 22/22 behind the DeepSeek emulator; 22/22 behind the Qwen emulator.
- **Suite:** 282 tests pass; `make chaos N=40`: 0 invariant violations.
- **Clean machine** (`scripts/clean_machine.sh`: fresh clone, `env -i`, no TTY) on ed372e4: 9/9
  steps on Python 3.9.23 and 3.13.12 (`rehearsal/results/clean_machine-python*.json`), including
  `ISSUE=<text> REPO=` and `ISSUE=<GitHub URL>` runs judged solved on a clean base and no key
  material on disk.

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
    code and pass on the patch. `arbiter verify --rerun` reproduces both verdicts.

## 11. DeepSeek and Qwen: behaviours handled, and sources

| Behaviour | Source | Handling |
|---|---|---|
| Thinking mode on by default; `reasoning_content` must be passed back on assistant turns with tool calls, else 400 | [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/); agent reports [opencode#24566](https://github.com/anomalyco/opencode/issues/24566), [opencode#24722](https://github.com/anomalyco/opencode/issues/24722), [opencode#24114](https://github.com/anomalyco/opencode/issues/24114) | `reasoning_passback = "auto"`; learned from the error text when a deployment wants all turns or none |
| DSML tool-call format (V4: `<｜DSML｜tool_calls>`; V4.1: spaced tags) leaking into `content` | [V4-Pro encoding](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/blob/main/encoding/README.md), [V4.1-Flash encoding](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md), [DeepSeek-V3#1244](https://github.com/deepseek-ai/DeepSeek-V3/issues/1244), [V4-Pro discussion 209](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/discussions/209) | Recovered as tool calls (offered tools only) |
| DeepSeek usage `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`; 402 Insufficient Balance; keep-alive lines | [DeepSeek API docs](https://api-docs.deepseek.com/) | Cache reads counted; quota exits 3; keep-alive tolerated |
| DashScope OpenAI-compatible endpoints per region; `enable_thinking`; thinking models stream-only | [OpenAI compatibility](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope), [deep thinking](https://www.alibabacloud.com/help/en/model-studio/deep-thinking), [API keys and regions](https://www.alibabacloud.com/help/en/model-studio/get-api-key) | Region candidates; switch to streaming on the stream-only error |
| Qwen function calling; Qwen3-Coder trained on XML `<function=...>` and `str_replace_editor` | [Qwen function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling), [Qwen Code](https://www.alibabacloud.com/help/en/model-studio/qwen-code) | XML/Hermes recovery; `str_replace_editor` and Qwen Code tool names translated |
| Error codes: `data_inspection_failed` (moderation), "Range of input length should be [1, N]" (overflow), Arrearage | [Model Studio error codes](https://www.alibabacloud.com/help/en/model-studio/error-code) | Content-filter retry with recent output withheld; overflow reduces context; quota exits 3 |
| Coding Plan keys (`sk-sp-`) and endpoints | [Coding Plan FAQ](https://www.alibabacloud.com/help/en/model-studio/coding-plan-faq) | Separate rule |

## 12. First live runs (2026-09-27) and the fixes they forced

Models: DeepSeek V4.1-Flash through NVIDIA NIM (free tier, 30–300 s per request), Qwen 3.8-27B
through OpenRouter's free pool (frequent upstream 429s). Judged by hidden tests the harness never sees.

| Suite | DeepSeek V4.1-Flash | Qwen 3.8-27B |
|---|---|---|
| `evalsuite/` (8 owned tasks) | 8/8 pass | 4/4 run, 4/4 pass (free daily quota) |
| Rehearsal tasks, 45 min limit (provider ~5x slower than direct APIs) | 9/10 solved (django_ipaddress_field: hidden tests fail) | not run (quota) |

Problems observed live and fixed (each with a regression test that fails without the fix):
1. **Rate limits ended the task.** Six fixed retries (~48 s) of 429 ended a run with 14 of 15 minutes
   left. Rate limits and server errors are now retried for `retry.transient_window_s` (600 s) within
   the deadline; network errors still fail after `max_attempts` (`tests/test_live_findings.py`).
2. **A passing change was thrown away.** When attempt 1's time share ran out right after its fix
   passed the tests, the harness reset to the original code. An attempt whose latest check on the
   current change passed now keeps the budget (`test_attempt_with_a_passing_change_is_not_restarted_at_its_share`).
3. **Stale preference lists refused to start.** NVIDIA NIM no longer serves the NIM rule's models;
   without a pinned model the harness now takes a Qwen/DeepSeek/coder model the provider lists (never
   when a model is pinned; still refuses when nothing suitable is listed).
4. **OpenRouter `-MMDD` ids** (`qwen/qwen3.8-max-0902`) are recognised as variants of the preference,
   so an OpenRouter key no longer falls through from Qwen to DeepSeek.
5. **Scratch copies were compared as checks.** A command that `cd`s into a copy in the run/scratch
   directory is no longer re-run as a check of the repository; the reproduction hint says the harness
   runs it on the original code, so the model need not rebuild the original itself.

## 13. Round 2026-09-27: Rust, the context window, tokens, and a 46-task gauntlet

Integrated first: Aryan Bhargava's context statistics and pressure-time argument compaction, and the
live-run fixes of §12 (merged from `harness-fixes-live-testing`).

**What changed, and the evidence for each** (all in `make test`, 310 tests):

| Change | Evidence |
|---|---|
| Observation window: 8 newest tool outputs verbatim (+ up to 3 until the next step of 4); older ones become a `read_output` pointer; old call arguments keep their keys, long strings shrink to their size | synthetic 40-step session: 2.37M → 1.03M estimated input tokens (`tests/test_context.py`); prefix changes ≤ once per step |
| Old reasoning: 2 newest turns full, cut-off advancing every 4 turns (was 4 / 8) | cache-stability test bound |
| Hard window: after the stepped passes, the newest outputs are cut to fit (head and tail kept) instead of the request failing | provider limited to 9k tokens, six large files read: solved, ≤ 1 rejection (`tests/test_provider_emulation.py`) |
| Output folding: build progress, passing-test lines (cargo, pytest -v, go -v, unittest -v, TAP), std-library backtrace frames, compiler warnings after the first two; line numbers of folded ranges kept for `read_output` | failing `cargo test` view 2,899 → 688 chars (`tests/test_token_economy.py`) |
| Tool schemas 4.6k → 3.8k chars; system prompt asks for brief replies and batched independent reads | per-request overhead −0.5k chars |
| Rust: background pre-build (`cargo test --no-run`, `--offline` retry, one note to the model), 600 s build timeouts, `rustfmt` parse guard (also `gofmt -e`, `node --check`, `ruby -c`), cargo `-q`/doc-test failure names, workspace and name-filter hints, module/test relations, `CARGO_TERM_COLOR=never` and fast network failure | `tests/test_prewarm.py`, `tests/test_token_economy.py`, real cargo fixtures in `tests/fixtures/runners/`, `tests/test_locate.py` |
| New root lock files (Cargo.lock from the first build, package-lock.json, …) are outside every snapshot | a pre-build leaves the snapshot identical to the base (`tests/test_prewarm.py`) |
| NVIDIA NIM and OpenRouter preference lists: DeepSeek/Qwen only | `tests/test_resolution_families.py` |

**Gauntlet** (`evalsuite/`, 46 non-holdout tasks: 16 Rust, 13 JavaScript, 10 Python, 5 Go, 2 Ruby; every
hidden test validated to fail on the base and pass on the reference). Offline, the model replays the
reference fix through the unchanged harness behind each emulator, and each result is judged by the
hidden tests on a clean base:

| Emulated family | Judged pass | Checks passed | Provider-rule rejections | Quirks repaired | Tokens / task (est.) | Requests / task |
|---|---|---|---|---|---|---|
| DeepSeek | 46/46 | 46/46 | 0 | 66 | 5,692 | 5.5 |
| Qwen | 46/46 | 46/46 | 0 | 238 | 5,551 | 5.0 |

This checks the pipeline (tools, toolchains, pre-build, evidence, export, judge) and measures the
harness's overhead; the scripted model knows the fix, so it says nothing about solve rate. Tokens are
the emulator's estimate (characters / 4 of the messages). Logs: `rehearsal/results/gauntlet46-*`.
Live: `make gauntlet` with `AI_API_KEY` (and `AI_MODEL` to pick a model on an aggregator key). This
environment still cannot reach the model providers, so no live run was made in this round.


## 14. Preloading the small files the search points to (after the first live DeepSeek logs)

**Finding.** The live DeepSeek logs (NVIDIA NIM, `g-py-merge-intervals`, `g-js-query`) show the model's
first requests each carrying about 1.7k input tokens and returning about 100 output tokens: one
`read_file` call per request, for files of a few hundred bytes the localisation had already found.
Every such request resends the whole context and waits a full provider round trip.

**Change.** When the issue names a file or a definition it mentions is found (`locate.py`), whole files
that fit `policy.preload_chars` (default 4,000 characters, at most 3 files, never a partial file, never
a path that resolves outside the repository) are shown in the first prompt exactly as `read_file`
shows them, so the model can edit on its first request. A provider content-filter rejection withholds
them like a tool output (`agent._withhold_preload`). `preload_chars = 0` turns it off.

**Measured cost, worst case.** The offline gauntlet's scripted model follows a fixed plan that reads
the files anyway, so it pays for the preload and saves nothing: 46/46 judged pass on both emulators,
estimated tokens per task 5,692 → 6,519 (DeepSeek) and 5,551 → 6,370 (Qwen), about +820 per task. A
live model that uses the preloaded files saves one request per file it would have read (on these
tasks, about 1.8k input tokens and one round trip each). Not yet measured live. The recorded logs in
`rehearsal/results/gauntlet46-*` predate this change.
