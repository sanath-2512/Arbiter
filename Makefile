# gheerefill — root targets required by the event: setup, run, test (+ clean).
# Runtime needs only Python >= 3.11 (stdlib) and git; `make setup` needs no network.
SHELL := /bin/bash
PY := $(shell cat .harness-python 2>/dev/null || command -v python3)

.PHONY: setup run test clean check-config probe smoke demo baseline-setup baseline eval

setup:                ## verify toolchain, pick a Python >= 3.11, byte-compile the harness
	@bash scripts/setup.sh

run:                  ## solve tasks: JSON/JSONL on stdin, or TASK=file; PROFILE=, OUT= optional
	@$(PY) -m gheerefill run $(if $(TASK),--task "$(TASK)",) $(if $(PROFILE),--profile "$(PROFILE)",) $(if $(OUT),--out "$(OUT)",)

test:                 ## deterministic tests (no network, no credentials, no paid calls)
	@$(PY) -m unittest discover -s tests -t . $(if $(V),-v,)

check-config:         ## validate the profile and that AI_API_KEY is present (no network)
	@$(PY) -m gheerefill check-config $(if $(PROFILE),--profile "$(PROFILE)",)

probe:                ## LIVE: endpoint/tool-calling compatibility check (uses AI_API_KEY)
	@$(PY) -m gheerefill probe $(if $(PROFILE),--profile "$(PROFILE)",)

demo:                 ## offline scripted demo (fake model, clearly labelled non-live)
	@$(PY) scripts/make_example.py calc-divide --work work | $(PY) -m gheerefill run --profile profiles/fake-demo.toml --out runs/demo

smoke:                ## LIVE: solve the example tasks with the configured model (uses AI_API_KEY)
	@$(PY) scripts/make_example.py --work work > work/tasks.jsonl && $(PY) -m gheerefill run --task work/tasks.jsonl --out runs/smoke $(if $(PROFILE),--profile "$(PROFILE)",)

baseline-setup:       ## dev only: install pinned mini-swe-agent (.venv-baseline) and Pi (baselines/pi) (network)
	@bash baselines/setup_baselines.sh

baseline:             ## dev only, LIVE: run upstream mini-swe-agent on TASK=file with the same model
	@.venv-baseline/bin/python baselines/run_mini.py --task "$(TASK)" --out runs/baseline-mini $(if $(PROFILE),--profile "$(PROFILE)",)

eval:                 ## dev only, LIVE: paired evaluation SUITE=dir SYSTEMS=ours,mini
	@$(PY) scripts/eval.py $(if $(SUITE),--suite "$(SUITE)",) $(if $(SYSTEMS),--systems "$(SYSTEMS)",) $(if $(PROFILE),--profile "$(PROFILE)",)

clean:                ## remove generated state (runs, work copies, caches)
	rm -rf runs work .harness-python .venv-baseline
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
