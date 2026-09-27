# arbiter proof predicate, version 1

`attestation.json` in each completed run directory is an
[in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md).
It is **unsigned**, because the harness holds no signing key. Integrity comes from the SHA-256
digests, which `python -m arbiter verify --run-dir DIR` recomputes.

- `subject`: `[{"name": "patch.diff", "digest": {"sha256": ...}}]`, the exported deliverable.
- `predicateType`: this document's URL.
- `predicate`:

| field | meaning |
|---|---|
| `harness` | name, version and the git commit of the harness checkout (`+dirty` if modified) |
| `task_id`, `model` | the task and the resolved model (provider, name, endpoint, live flag, profile id) |
| `base_tree` | git tree id of the original code, as captured before any change |
| `selected_tree` | git tree id of the delivered code; `patch.diff` applied to `base_tree` must produce it |
| `counterfactual_tree` | `base_tree` with only the selected candidate's test-file changes applied |
| `reconstruction_verified` | the harness applied the patch to a clean base copy and got `selected_tree` |
| `proof` | the level (`proven`, `fixed`, `passing`, `unverified`, `refuted`), its summary, each comparison (check, verdict on the counterfactual and on the patch, fail→pass and pass→fail test names) and the registered reproductions |
| `evidence` | every record cited by the proof: id, tree, command, kind, source, outcome, counts, failing tests, binding, and the path and SHA-256 of the archived output |
| `files` | SHA-256 of `evidence.jsonl`, `transcript.jsonl`, `actions.jsonl`, `requests.jsonl`, `task.json`, `profile.json` |

## What `verify` checks

`verify` works offline and needs only the run directory (its `shadow.git` holds the trees):

1. `patch.diff` matches the subject digest, and the listed record files match their digests.
2. Applying `patch.diff` to `base_tree` yields exactly `selected_tree`.
3. Every cited evidence record exists in `evidence.jsonl` with the attested tree, command and
   outcome, and its archived output matches the attested digest.

`--rerun` additionally re-executes each proof check in temporary copies of `counterfactual_tree` and
`selected_tree`, and reports whether each verdict is reproduced. A temporary copy is not the
original environment (an editable install, for example, still points at the original checkout),
so a disagreement is reported, not treated as tampering.

## What it does not claim

The proof says what the harness observed by execution. It does not say the patch is correct. The
evaluator's own tests decide that. A `proven` level means that the checks the agent and harness ran
fail without the change and pass with it, with no regression among those checks.
