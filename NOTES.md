# Engineering notes

These are persistent working notes. Update them when evidence changes.

## 1. Contract status

No official event documents were available in the workspace. The rules below come from the
implementation brief.

**Confirmed**

| Item | Current handling |
|---|---|
| Root `make setup` / `make run` / `make test` (+ `clean`) | Implemented. Setup works offline. Run is unattended. Test needs no network or credentials. |
| Credential from `AI_API_KEY`, never committed or logged | Read only in `models/__init__.py`. Stripped from tool environments. Redacted from every record (tested). |
| Use the prescribed model; never substitute silently | The profile must name the model. A missing model or credential gives a clear error. There is no fallback model. |
| Text-only evaluation; unattended operation | Text tools only. No TTY needed (tested via subprocess without a TTY). |
| Evaluator clones, supplies credentials, runs setup/run, supplies tasks | Setup needs only Python ≥ 3.11 + git. The task source is replaceable (`task.py`). |

**Development assumptions**

| Item | Current handling |
|---|---|
| Provider/API protocol | OpenAI-compatible Chat Completions (default) and Anthropic Messages adapters, both non-streaming with native tools. An explicit `text` protocol exists as a separate profile. |
| Context/output limits | Profile defaults: 128k context, 8192 output tokens. |
| Reasoning controls | `model.extra_body` passthrough (e.g. `reasoning_effort`, `thinking`). |
| Token/time/concurrency limits | Defaults 1800 s / 150 steps. Per-task `limits` override them. Tasks run sequentially. |
| Task input/output format | Local adapter: JSON / JSON Lines in, one JSON record per task out. The deliverable is the in-place working tree plus a patch file. |
| Repository languages | Language-agnostic tools, plus output parsers for common test runners. |
| Prepared vs bare environments | Assumed prepared. The model may install dependencies via bash. There is no automatic setup-recipe generation. |
| Network/container permissions | The harness needs outbound HTTPS to the model endpoint only. |
| Cross-task persistence | None is used. Each task is independent. |

**Blocked on organiser information**

| Item | Current handling |
|---|---|
| Exact model/version | Configure via `AI_MODEL` or `profiles/default.toml`. |
| Provider/API protocol | See the assumption above. |
| Context/output limits | See the assumption above. |
| Reasoning controls | See the assumption above. |
| Token/time/concurrency limits | See the assumption above. |
| Task input/output format | See the assumption above. **The local protocol is not proof of event compatibility.** |
| Repository languages | See the assumption above. |
| Prepared vs bare environments | See the assumption above. |
| Network/container permissions | See the assumption above. |
| Scoring weights | Unknown. The design maximises valid, verified submissions. |
| Cross-task persistence | See the assumption above. |
| Code-reuse rules | The runtime is original code with one attributed adaptation. Baselines are dev-only. |
| Restrictions on dev-time optimisation / auxiliary models | None are used, so nothing needs permission yet. |

## 2. Claims and their evidence

| Claim | Implemented | Deterministically tested | Live-tested | Benchmark-supported |
|---|---|---|---|---|
| End-to-end solve path (issue → model → tools → edit → check → export → record) | yes | yes: scripted model, and a scripted HTTP server through the real transport | **no** | no |
| Endpoint compatibility (OpenAI/Anthropic, tool calls, usage) | yes | yes: local server, request shape, error classes | **no** | — |
| Candidate preservation and dominance restoration | yes | yes | no | no |
| Artifact fidelity (add/delete/rename/mode/symlink/CRLF/binary/unicode paths) and clean reconstruction | yes | yes, including `git apply` in a fresh clone | no | — |
| Failure handling (timeouts, descendants, runaway output, cancel, SIGTERM, SIGKILL + recovery, crash, budget exhaustion, context overflow, auth/quota/rate-limit/5xx) | yes | yes | no | — |
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
7. **Non-streaming HTTP via urllib.** Simplest failure semantics. A read timeout bounds a
   non-streaming call. Revisit if the prescribed endpoint requires streaming.
8. **Tests use `unittest`**, so `make test` has zero dependencies and works offline on the evaluator.

## 4. Runtime policies (defaults; each is a profile flag — unmeasured until live runs)

| Policy | Default | Rationale | Measured? |
|---|---|---|---|
| `repo_overview` | on | Top-level listing + manifests in the first message saves 1–2 steps | no |
| `submit_review` | on, at most once | Flags an empty diff, new (possibly scratch) files, or no check since the last edit | no |
| `recover_empty_final` | on (not after a confirmed model submit) | An empty patch cannot pass; the latest archived candidate can | no |
| `dominance_selection` | on | Deterministic; only acts on conflicting evidence from the same check | no |
| `final_recheck` | on | Re-runs the last agent check on the selected tree when evidence is stale; no model tokens | no |
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
- **Unsupported artifact types.** Empty directories; content inside nested git repositories or
  submodules; git-lfs smudge semantics. New files under ignored paths are excluded and listed.
- **Recovery.**
  - SIGKILL cannot be handled in-process. Offline `finalize` recovers the deliverable from the
    checkpoint, but only if someone runs it.
  - The in-place working tree at kill time may be mid-edit.
  - Conversation resumption is not supported; only finalisation is.
- **Requests.** HTTP is non-streaming. A provider requiring streaming, or answering slower than
  `request_timeout_s`, will fail with a classified timeout.
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
