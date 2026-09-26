#!/usr/bin/env bash
# Offline setup: choose a Python >= 3.11, verify git/bash, byte-compile the harness.
set -euo pipefail
cd "$(dirname "$0")/.."

pick_python() {
  for c in "${HARNESS_PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [ -n "$c" ] || continue
    p=$(command -v "$c" 2>/dev/null) || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  if command -v uv >/dev/null 2>&1; then
    p=$(uv python find '>=3.11' 2>/dev/null || true)
    if [ -z "$p" ]; then uv python install 3.12 >/dev/null 2>&1 && p=$(uv python find '>=3.11' 2>/dev/null || true); fi
    [ -n "$p" ] && { echo "$p"; return 0; }
  fi
  return 1
}

PY=$(pick_python) || { echo "setup: ERROR: Python >= 3.11 not found (set HARNESS_PYTHON=/path/to/python3.11+)" >&2; exit 1; }
echo "$PY" > .harness-python

command -v git >/dev/null || { echo "setup: ERROR: git is required" >&2; exit 1; }
GITV=$(git --version | awk '{print $3}')
"$PY" - "$GITV" <<'PYEOF'
import sys
v = tuple(int(x) for x in sys.argv[1].split(".")[:2] if x.isdigit())
if v < (2, 25):
    sys.exit(f"setup: ERROR: git >= 2.25 required, found {sys.argv[1]}")
PYEOF
command -v bash >/dev/null || { echo "setup: ERROR: bash is required" >&2; exit 1; }
command -v rg >/dev/null || echo "setup: note: ripgrep not found; the search tool falls back to grep"

"$PY" -m compileall -q gheerefill >/dev/null
"$PY" -c 'import gheerefill.cli' 
echo "setup: ok (python: $PY $("$PY" -c 'import platform; print(platform.python_version())'), git $GITV)"
