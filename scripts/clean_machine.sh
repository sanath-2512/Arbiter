#!/usr/bin/env bash
# Clean-machine rehearsal (dev only): what a judge's machine looks like on first contact.
#
# A fresh clone of the COMMITTED repository (HEAD), an empty HOME, no caches, a minimal environment
# (env -i: PATH, HOME, LANG, TMPDIR only; no proxy, so no outbound network), stdin from /dev/null (no
# TTY). Then exactly the documented commands: make setup, make test, make run in every input mode the
# event document allows, make clean. The model is the lab's scripted policy server behind the real
# HTTP transport (AI_BASE_URL), so no credential is needed and none is used; the GitHub API for the
# ISSUE=URL flow is a local stand-in (ARBITER_GITHUB_API / ARBITER_GITHUB_CLONE_BASE). Patches
# made through `make run` are judged by the lab exactly like gauntlet runs (clean base + hidden tests).
#
#   scripts/clean_machine.sh                       # interpreter chosen by make setup
#   HARNESS_PYTHON=python3.9 scripts/clean_machine.sh
#
# Writes rehearsal/results/clean_machine-<python>.json (summary) and prints the log.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LAB="python3 $ROOT/scripts/rehearsal.py"
TASK=click_sentinel_copy
W=$(mktemp -d "${TMPDIR:-/tmp}/arbiter-clean.XXXXXX")
LOG=$W/log.txt
RESULTS=()
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

record() {  # name pass|fail seconds detail
  printf '%-4s %-34s %6ss  %s\n' "$([ "$2" = pass ] && echo PASS || echo FAIL)" "$1" "$3" "$4" | tee -a "$LOG"
  RESULTS+=("$(python3 -c 'import json,sys; print(json.dumps({"step": sys.argv[1], "ok": sys.argv[2] == "pass", "seconds": float(sys.argv[3]), "detail": sys.argv[4]}))' "$1" "$2" "$3" "$4")")
}

start_server() {  # command... ; prints the first stdout line (the server URL)
  local out=$W/srv.$RANDOM
  "$@" > "$out" 2>&1 &
  PIDS+=($!)
  for _ in $(seq 100); do [ -s "$out" ] && break; sleep 0.1; done
  head -1 "$out"
}

HP=""
[ -n "${HARNESS_PYTHON:-}" ] && HP=$(command -v "$HARNESS_PYTHON")
MINPATH=/usr/local/bin:/usr/bin:/bin
cleanenv() {
  env -i PATH="$MINPATH" HOME="$W/home" LANG=C.UTF-8 TMPDIR="$W/tmp" ${HP:+HARNESS_PYTHON="$HP"} "$@" < /dev/null
}

git clone -q "file://$ROOT" "$W/arbiter" || { echo "clone failed"; exit 1; }
mkdir -p "$W/home" "$W/tmp"
G=$W/arbiter
echo "clean clone of $(git -C "$G" rev-parse --short=12 HEAD) in $G" | tee -a "$LOG"
ISSUE_TEXT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["issue"])' "$ROOT/rehearsal/tasks/$TASK/task.json")

# 1. setup (offline, no caches)
t0=$SECONDS; out=$(cleanenv make -C "$G" setup 2>&1); rc=$?
[ $rc -eq 0 ] && echo "$out" | grep -q "setup: ok" && record setup pass $((SECONDS-t0)) "$(echo "$out" | tail -1)" \
  || record setup fail $((SECONDS-t0)) "rc=$rc $(echo "$out" | tail -3 | tr '\n' ' ')"
PYV=$(cat "$G/.harness-python" 2>/dev/null)
PYV=$("$PYV" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)

# 2. deterministic tests
t0=$SECONDS; out=$(cleanenv make -C "$G" test 2>&1); rc=$?
ran=$(echo "$out" | grep -Eo "Ran [0-9]+ tests" | tail -1)
[ $rc -eq 0 ] && echo "$out" | grep -qE "^OK" && record test pass $((SECONDS-t0)) "$ran" \
  || record test fail $((SECONDS-t0)) "rc=$rc $ran $(echo "$out" | grep -E "FAIL|ERROR" | head -3 | tr '\n' ' ')"

# 3. no key: clear configuration error, no traceback, fast
$LAB base --task $TASK --dest "$W/r_nokey" > /dev/null
t0=$SECONDS; out=$(cleanenv make -C "$G" run ISSUE="$ISSUE_TEXT" REPO="$W/r_nokey" 2>&1); rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "AI_API_KEY" && ! echo "$out" | grep -q Traceback; then
  record "run: no AI_API_KEY" pass $((SECONDS-t0)) "rc=$rc: $(echo "$out" | grep -m1 AI_API_KEY | cut -c1-120)"
else record "run: no AI_API_KEY" fail $((SECONDS-t0)) "rc=$rc $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-200)"; fi

# 4. no TTY and no ISSUE: must not hang waiting for input
t0=$SECONDS; out=$(cleanenv AI_API_KEY=sk-not-used timeout 120 make -C "$G" run 2>&1); rc=$?
if [ $rc -ne 0 ] && [ $rc -ne 124 ] && ! echo "$out" | grep -q Traceback; then
  record "run: no TTY, no ISSUE" pass $((SECONDS-t0)) "rc=$rc: $(echo "$out" | grep -v '^make' | tail -1 | cut -c1-120)"
else record "run: no TTY, no ISSUE" fail $((SECONDS-t0)) "rc=$rc $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-200)"; fi

# model endpoints (lab side): the scripted policy, and a proxy that answers 401 invalid key
POLICY=$(start_server python3 "$ROOT/scripts/policy_server.py" click_sentinel_fix)
AUTH=$(start_server python3 "$ROOT/scripts/fault_proxy.py" "${POLICY%/v1}" '[{"at": 1, "kind": "auth"}, {"at": 2, "kind": "auth"}, {"at": 3, "kind": "auth"}]')
MODEL_ENV=(AI_PROVIDER=openai_chat AI_MODEL=scripted-policy)

# 5. invalid key: stop at once with a clear error
$LAB base --task $TASK --dest "$W/r_badkey" > /dev/null
t0=$SECONDS; out=$(cleanenv AI_API_KEY=sk-invalid-not-a-key AI_BASE_URL="$AUTH/v1" "${MODEL_ENV[@]}" \
  timeout 300 make -C "$G" run ISSUE="$ISSUE_TEXT" REPO="$W/r_badkey" 2>&1); rc=$?
if [ $rc -ne 0 ] && [ $rc -ne 124 ] && echo "$out" | grep -qiE "invalid|401|rejected|key" && ! echo "$out" | grep -q Traceback; then
  record "run: invalid key" pass $((SECONDS-t0)) "rc=$rc: $(echo "$out" | grep -iE -m1 "invalid|401|rejected" | cut -c1-140)"
else record "run: invalid key" fail $((SECONDS-t0)) "rc=$rc $(echo "$out" | tail -3 | tr '\n' ' ' | cut -c1-240)"; fi

judge_run() {  # name out
  local name=$1 out=$2 patch
  patch=$(echo "$out" | python3 -c '
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip().startswith("{")]
r = json.loads(lines[-1]) if lines else {}
print((r.get("deliverable") or {}).get("patch_path") or "")')
  if [ -z "$patch" ] || [ ! -f "$patch" ]; then echo "no patch"; return 1; fi
  $LAB judge-patch --task $TASK --patch "$patch" 2>/dev/null | tail -1
}

# 6. ISSUE text + REPO, no TTY, through the real HTTP transport
$LAB base --task $TASK --dest "$W/r_text" > /dev/null
t0=$SECONDS; out=$(cleanenv AI_API_KEY=sk-scripted-policy-not-a-key AI_BASE_URL="$POLICY" "${MODEL_ENV[@]}" \
  timeout 900 make -C "$G" run ISSUE="$ISSUE_TEXT" REPO="$W/r_text" 2>&1); rc=$?
verdict=$(judge_run text "$out")
if [ $rc -eq 0 ] && echo "$verdict" | grep -q '"solved": true'; then
  record "run: ISSUE=text REPO=path" pass $((SECONDS-t0)) "judged: $verdict"
else record "run: ISSUE=text REPO=path" fail $((SECONDS-t0)) "rc=$rc judged: $verdict $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-200)"; fi

# 7. ISSUE=URL: issue fetched from a local stand-in of the GitHub API, repository cloned from a local base
mkdir -p "$W/github/pallets"
$LAB base --task $TASK --dest "$W/github/pallets/click" > /dev/null
printf '%s\n' "$ISSUE_TEXT" > "$W/issue.md"
GH=$(start_server python3 "$ROOT/scripts/fake_github.py" pallets/click 4242 "$W/issue.md")
POLICY2=$(start_server python3 "$ROOT/scripts/policy_server.py" click_sentinel_fix)
t0=$SECONDS; out=$(cleanenv AI_API_KEY=sk-scripted-policy-not-a-key AI_BASE_URL="$POLICY2" "${MODEL_ENV[@]}" \
  ARBITER_GITHUB_API="$GH" ARBITER_GITHUB_CLONE_BASE="file://$W/github" \
  timeout 900 make -C "$G" run ISSUE=https://github.com/pallets/click/issues/4242 2>&1); rc=$?
verdict=$(judge_run url "$out")
if [ $rc -eq 0 ] && echo "$verdict" | grep -q '"solved": true'; then
  record "run: ISSUE=<github issue URL>" pass $((SECONDS-t0)) "judged: $verdict"
else record "run: ISSUE=<github issue URL>" fail $((SECONDS-t0)) "rc=$rc judged: $verdict $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-200)"; fi

# 8. nothing secret-looking was written by the runs (the scripted key is the only key that existed)
KEYDIRS=("$W/home"); for d in "$G/runs" "$G/workspace" "$W"/r_*; do [ -e "$d" ] && KEYDIRS+=("$d"); done
if grep -rIl "sk-scripted-policy-not-a-key\|sk-invalid-not-a-key" "${KEYDIRS[@]}" 2>/dev/null | head -1 | grep -q .; then
  record "no key material on disk" fail 0 "$(grep -rIl 'sk-scripted-policy-not-a-key\|sk-invalid-not-a-key' "${KEYDIRS[@]}" | head -3 | tr '\n' ' ')"
else record "no key material on disk" pass 0 "checked run outputs, cloned workspaces, target repos and HOME"; fi

# 9. clean
t0=$SECONDS; out=$(cleanenv make -C "$G" clean 2>&1); rc=$?
[ $rc -eq 0 ] && [ ! -e "$G/runs" ] && [ ! -e "$G/workspace" ] && record clean pass $((SECONDS-t0)) "runs/ and workspace/ removed" \
  || record clean fail $((SECONDS-t0)) "rc=$rc"

passed=$(printf '%s\n' "${RESULTS[@]}" | grep -c '"ok": true')
total=${#RESULTS[@]}
SUMMARY="$ROOT/rehearsal/results/clean_machine-python$PYV.json"
mkdir -p "$ROOT/rehearsal/results"
printf '%s\n' "${RESULTS[@]}" | python3 -c '
import json, sys
steps = [json.loads(l) for l in sys.stdin if l.strip()]
print(json.dumps({"schema": "arbiter.clean-machine/v1", "commit": sys.argv[1], "python": sys.argv[2],
                  "passed": sum(s["ok"] for s in steps), "total": len(steps), "steps": steps}, indent=2))' \
  "$(git -C "$G" rev-parse --short=12 HEAD)" "$PYV" > "$SUMMARY"
echo "clean machine (python $PYV): $passed/$total passed; summary: $SUMMARY" | tee -a "$LOG"
[ "$passed" = "$total" ]
