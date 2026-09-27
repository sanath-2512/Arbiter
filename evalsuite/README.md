# Development evaluation suite (evaluation boundary)

Small, owned screening tasks for comparing harness configurations and baselines under the same
model and limits. **Not a benchmark**: tasks are tiny and synthetic; results screen for broken
profiles and gross regressions only.

Boundary rules:
- `hidden/` (judge tests) and `reference/` (reference solutions) are judge-owned. The runtime
  package (`gheerefill/`) never reads this directory; agents only receive a fresh copy of `repo/`
  and `issue.md`. The eval runner audits every trajectory for references to `evalsuite`.
  (A model with shell access could still search the filesystem; runs are flagged, not prevented.)
- `reference/` is used only by `scripts/eval.py --validate-suite` to check that each judge fails on
  the base repository and passes on a reference solution.
- Partitions: `dev` (iterate freely), `selection` (compare candidate configurations),
  `gauntlet` (40 generated tasks across Rust, JavaScript, Python, Go and Ruby; regenerate with
  `scripts/make_gauntlet.py`), `final` (untouched holdout). Every run over `final` is appended to `evalsuite/final_runs.log`,
  so repeated inspection is visible.
