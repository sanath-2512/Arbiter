# Judge Rehearsal Lab (dev only)

This lab rehearses how the organisers will evaluate arbiter:
1. a clean checkout of a real repository at a pinned commit;
2. the harness launched with an issue;
3. the patch it exports applied to a fresh clean base;
4. hidden tests run on that base.

The lab never ships with or feeds the runtime. `arbiter/` contains no reference to `rehearsal/`
or `evalsuite/`, and `tests/test_rehearsal.py` enforces this.

```
rehearsal/
  manifests/repos.json      real repositories: URL, mirror, language, size class, test environment
  manifests/configs.json    ablation configs A-F (profile overrides)
  manifests/gauntlet.json   the Judge Gauntlet: 20 tasks with a fixed mix, plus fault injections
  manifests/mechanisms.json scripted mechanism scenarios
  tasks/<id>/task.json      what the harness gets: issue text, pinned base commit, limits, overlay
  tasks/<id>/label.b64      what only the judge reads (gzip+base64): hidden test patch, reference
                            patch, verify command, FAIL_TO_PASS / PASS_TO_PASS, gold files
  issues/<id>.md            hand-written issue texts (symptoms, never the diff's wording)
  lab_tests/                lab-authored hidden tests (only where upstream has no fail-to-pass test)
  policies/                 scripted "models" for mechanism rehearsals
  results/                  committed results (validation, localisation, mechanisms, clean machine)
  repos/, envs/             mirrors and toolchains (git-ignored, rebuilt on demand)
```

## The boundary

Labels are stored encoded, so a grep for a fix never finds one. The public `task.json` contains no
line of the reference fix (enforced by a test). The runner reads a label only after the harness
has exited.

The harness never sees the lab:
- task repositories are built by `git fetch --depth=N` of the base commit into a fresh repository,
  so they have no remote, no future history and no tags;
- every run happens under `$TMPDIR/arbiter-rehearsal`, outside this checkout;
- after each run, the harness's transcript, tool outputs and patch are audited for references to
  labels, mirrors or task files; those findings go into the `audit` field.

## Tasks

Each task is a real historical fix:
- **Base:** the fix's parent commit.
- **Hidden tests:** the fix's test changes.
- **Reference patch:** the fix's other changes.
- **FAIL_TO_PASS / PASS_TO_PASS:** derived by running the touched tests before and after the fix
  (the SWE-bench method). Pytest runs with `--continue-on-collection-errors`, so one module that
  fails to import cannot mask the others.

Issue texts are written by hand from symptoms reproduced on the base commit.

Two refactorings have special handling:
- **`click_help_parameter`:** its upstream tests pass before and after the change. One lab-authored
  test for the API the issue asks for supplies the fail-to-pass signal; the task records it under
  `source.lab_authored_tests`.
- **`pytest_fixturedefs`** was considered and rejected: its only fail-to-pass assertion is a
  tuple-versus-list detail unrelated to the actual fix, a quadratic slowdown.

Every task is re-validated by `python scripts/rehearsal.py validate --jobs 4`. It checks three
things: the hidden tests fail on the base, the reference fix makes all of them pass, and the fix
applies to a clean base. Results are in `results/validation.json`.

**Judge Gauntlet** (`manifests/gauntlet.json`): 20 tasks.
- **Complexity:** 4 medium, 8 large, 6 very large, 2 pathological.
- **Type:** 5 regression, 5 bug, 4 feature, 3 refactor, 3 build/config/test.
- **Repositories:** click, flask, express, pytest, eslint, hugo, sphinx, django, ansible (Python,
  JavaScript, Go).

The pathological tasks add deterministic, unremarkable overlays to a real repository. The overlay
commit is dated like the base and has a neutral message:
- a vendored copy of the code to be fixed (a decoy definition to edit by mistake);
- 15-20k generated files, some mentioning the issue's identifiers (grep noise);
- a multi-megabyte single-line minified asset that matches the issue's terms.

Regressions are labelled "regression" only when the fix's history says so, e.g. "Regression in
<sha>" or "restores v0.152.2 behaviour". Long-standing bugs were relabelled or dropped.

## Commands

```
python scripts/rehearsal.py validate [--tasks a,b] [--jobs N]  # task soundness
python scripts/rehearsal.py localize                            # recall of the deterministic hints
python scripts/rehearsal.py mechanisms [--scenarios ...]        # scripted mechanism rehearsals, ablation
python scripts/rehearsal.py gauntlet [--configs A,F]            # the gauntlet (clean runs need AI_API_KEY)
python scripts/rehearsal.py run --task rehearsal/tasks/ID --config F [--policy P | live]
                                   [--faults JSON --upstream URL] [--signal TERM@S|KILL@S]
python scripts/rehearsal.py judge-patch --task ID --patch FILE  # judge any patch like a gauntlet run
python scripts/rehearsal.py base --task ID --dest DIR           # a clean task checkout (no labels)
python scripts/rehearsal_mine.py list REPO | make ... | remake ID
scripts/clean_machine.sh                                        # fresh clone, env -i, no TTY, make ...
```

Every judged run records the following. `report` renders baseline-vs-config tables, failure
classes, paired sign tests, and failure-memory repeats before and after intervention.
- **Identity:** task, repo, base commit, harness version (`+dirty` when the runtime has uncommitted
  changes).
- **Outcome:** solved, submission-ready, verification, proof level, attempts, failure class.
- **Usage:** requests, tokens, tool calls; tool, setup, solve and total time.
- **Candidates:** candidates, whether one was restored, recovery win or damage (the final state is
  judged too).
- **Artifact:** patch size, clean reconstruction.

## What the results can and cannot say

- **Scripted policies** (`model_kind: scripted-policy`) drive the harness into one failure mode on
  a real repository. They show whether a mechanism works on real test output, e.g. whether a
  regressing final state is replaced by the earlier verified candidate. The policy already knows
  the fix, so these runs are **not** evidence of solve rate.
- **Solve rate, and whether arbiter beats the baseline config,** can only be measured with the
  prescribed model:
  ```
  AI_API_KEY=... python scripts/rehearsal.py gauntlet --configs A,F --repeats 3
  ```
  None of these live runs has been performed (no key), so none is reported.
- **Localisation recall** is deterministic and model-free. It shows what the hints point at, not
  whether a model follows them.
