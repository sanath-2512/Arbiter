#!/usr/bin/env bash
# Offline setup: choose a Python >= 3.9, verify git/bash, byte-compile the harness.
# The chosen interpreter is recorded in .harness-python (git-ignored) and used by every target.
set -euo pipefail
cd "$(dirname "$0")/.."

usable() {  # $1 = candidate interpreter; needs >= 3.9 with ssl (HTTPS) and ctypes (credential hygiene)
  "$1" - <<'PYEOF' 2>/dev/null
import sys
if sys.version_info < (3, 9):
    sys.exit(1)
import ssl, ctypes, json, subprocess  # noqa: F401,E401 - fail here, not in the middle of a run
PYEOF
}

pick_python() {
  local c p
  # Newest first: 3.11+ ships tomllib; 3.9/3.10 use the vendored copy (gheerefill/_vendor).
  for c in "${HARNESS_PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    [ -n "$c" ] || continue
    p=$(command -v "$c" 2>/dev/null) || continue
    if usable "$p"; then echo "$p"; return 0; fi
    [ "$c" = "${HARNESS_PYTHON:-}" ] && echo "setup: warning: HARNESS_PYTHON=$c is not usable (needs >= 3.9 with ssl, ctypes)" >&2
  done
  if command -v uv >/dev/null 2>&1; then  # last resort; needs network only if nothing is installed
    p=$(uv python find '>=3.9' 2>/dev/null || true)
    if [ -z "$p" ]; then
      echo "setup: no suitable Python found; installing one with uv (network)" >&2
      uv python install 3.12 >/dev/null 2>&1 && p=$(uv python find '>=3.9' 2>/dev/null || true)
    fi
    if [ -n "$p" ] && usable "$p"; then echo "$p"; return 0; fi
  fi
  return 1
}

PY=$(pick_python) || {
  echo "setup: ERROR: no Python >= 3.9 with the ssl and ctypes modules was found." >&2
  echo "       Install one (e.g. apt install python3, brew install python) or set HARNESS_PYTHON=/path/to/python3." >&2
  exit 1
}

command -v git >/dev/null || { echo "setup: ERROR: git is required" >&2; exit 1; }
GITV=$(git --version | awk '{print $3}')
"$PY" - "$GITV" <<'PYEOF'
import sys
v = tuple(int(x) for x in sys.argv[1].split(".")[:2] if x.isdigit())
if v < (2, 25):
    sys.exit(f"setup: ERROR: git >= 2.25 required, found {sys.argv[1]}")
PYEOF
command -v rg >/dev/null || echo "setup: note: ripgrep not found; the search tool falls back to grep"

"$PY" -m compileall -q gheerefill >/dev/null
"$PY" -c 'import gheerefill.cli'
echo "$PY" > .harness-python
echo "setup: ok (python: $PY $("$PY" -c 'import platform; print(platform.python_version())'), git $GITV)"
