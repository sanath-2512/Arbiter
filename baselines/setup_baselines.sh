#!/usr/bin/env bash
# Development-only: install the pinned upstream mini-swe-agent baseline into .venv-baseline.
# Needs network access (PyPI). Not used by `make setup` / `make run`.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=$(cat .harness-python 2>/dev/null || command -v python3)
if [ ! -x .venv-baseline/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then uv venv -q -p "$PY" .venv-baseline; else "$PY" -m venv .venv-baseline; fi
fi
if command -v uv >/dev/null 2>&1; then
  if [ -f baselines/requirements-mini.lock ]; then
    uv pip install -q -p .venv-baseline/bin/python -r baselines/requirements-mini.lock
  else
    uv pip install -q -p .venv-baseline/bin/python -r baselines/requirements-mini.txt
  fi
else
  .venv-baseline/bin/python -m pip install -q -r "$( [ -f baselines/requirements-mini.lock ] && echo baselines/requirements-mini.lock || echo baselines/requirements-mini.txt)"
fi
.venv-baseline/bin/python -c "import minisweagent, sys; print('mini-swe-agent', minisweagent.__version__)" 2>/dev/null | tail -1
