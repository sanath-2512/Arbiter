# gheerefill — root targets required by the event: setup, run, test (+ clean).
# Runtime needs only Python >= 3.9 (standard library) and git; `make setup` needs no network.
#
#   export AI_API_KEY=...                 # the only required configuration
#   make setup
#   make run                              # interactive: paste a GitHub issue URL or issue text
#   make run ISSUE=https://github.com/OWNER/REPO/issues/N        # non-interactive
#   make run ISSUE="text of the issue" REPO=/path/to/repo       # non-interactive, local repo
#
# Recipes never expand $(AI_API_KEY): the key cannot be echoed by make. Inputs reach the harness
# through the environment ("$$VAR" in recipes), literally, so spaces, quotes and $ in values are safe.
# Works from any directory (`make -f /path/to/Makefile run`); relative paths in ISSUE=@file,
# TASK, REPO and OUT are resolved against the directory make was started in.
HERE := $(if $(filter Makefile,$(lastword $(MAKEFILE_LIST))),.,$(abspath $(dir $(lastword $(MAKEFILE_LIST)))))
PY := bash scripts/py.sh
export GHEEREFILL_CALLER_DIR := $(CURDIR)
INPUTS := ISSUE ISSUE_URL ISSUE_FILE GITHUB_ISSUE REPO REPO_PATH REPO_URL BASE TASK PROFILE OUT TIME_LIMIT MAX_STEPS SUITE SYSTEMS
# Take inputs literally: make would otherwise expand "$(...)" inside a value (an issue that quotes
# `$(shell ...)` or `$(CC)` would be evaluated or mangled) when exporting or testing it.
$(foreach v,$(INPUTS),$(if $(filter undefined,$(origin $v)),,$(eval override $v := $$(value $v))))
export $(INPUTS)

.PHONY: setup run test clean check-config probe smoke demo chaos baseline-setup eval

setup:                ## verify toolchain, pick a Python >= 3.9, byte-compile the harness (offline)
	@cd "$(HERE)" && bash scripts/setup.sh

run:                  ## launch the harness (interactive on a TTY; ISSUE=, REPO=, BASE=, TASK=, TIME_LIMIT=, MAX_STEPS= optional)
	@cd "$(HERE)" && $(PY) -m gheerefill run $(if $(TASK),--task "$$TASK",) $(if $(PROFILE),--profile "$$PROFILE",) \
		$(if $(OUT),--out "$$OUT",) $(if $(TIME_LIMIT),--time-limit "$$TIME_LIMIT",) $(if $(MAX_STEPS),--max-steps "$$MAX_STEPS",)

test:                 ## deterministic tests (no network, no credentials, no paid calls)
	@cd "$(HERE)" && $(PY) -m unittest discover -s tests -t . $(if $(V),-v,)

check-config:         ## validate profile, resolve the model, check AI_API_KEY with the provider (no tokens)
	@cd "$(HERE)" && $(PY) -m gheerefill check-config $(if $(PROFILE),--profile "$$PROFILE",)

probe:                ## LIVE: endpoint/tool-calling compatibility check (uses AI_API_KEY)
	@cd "$(HERE)" && $(PY) -m gheerefill probe $(if $(PROFILE),--profile "$$PROFILE",)

demo:                 ## offline scripted demo (fake model, clearly labelled non-live)
	@cd "$(HERE)" && $(PY) scripts/make_example.py calc-divide --work work | $(PY) -m gheerefill run --profile profiles/fake-demo.toml --out runs/demo

chaos:                ## offline fault-injection run: random model/tool faults and kills, checks invariants (N=seeds)
	@cd "$(HERE)" && $(PY) scripts/chaos.py --seeds $(or $(N),50)

smoke:                ## LIVE: solve the example tasks with the configured model (uses AI_API_KEY)
	@cd "$(HERE)" && mkdir -p work && $(PY) scripts/make_example.py --work work > work/tasks.jsonl && \
		$(PY) -m gheerefill run --task work/tasks.jsonl --out runs/smoke $(if $(PROFILE),--profile "$$PROFILE",)

baseline-setup:       ## dev only: install pinned mini-swe-agent (.venv-baseline) and Pi (baselines/pi) (network)
	@cd "$(HERE)" && bash baselines/setup_baselines.sh

eval:                 ## dev only, LIVE: paired evaluation SUITE=dir SYSTEMS=ours,mini,pi
	@cd "$(HERE)" && $(PY) scripts/eval.py $(if $(SUITE),--suite "$$SUITE",) $(if $(SYSTEMS),--systems "$$SYSTEMS",) $(if $(PROFILE),--profile "$$PROFILE",)

clean:                ## remove generated state (runs, work copies, cloned issue repositories, caches)
	cd "$(HERE)" && rm -rf runs work workspace .harness-python .venv-baseline
	cd "$(HERE)" && find . -name __pycache__ -type d -prune -exec rm -rf {} +
