#!/usr/bin/env bash
# Run the interpreter chosen by `make setup`; run setup first if it has not run (or the recorded
# interpreter has disappeared), so `make run` / `make test` also work without a prior `make setup`.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$(cat "$here/.harness-python" 2>/dev/null || true)"
if [ -z "$py" ] || [ ! -x "$py" ]; then
  bash "$here/scripts/setup.sh" >&2
  py="$(cat "$here/.harness-python")"
fi
exec "$py" "$@"
