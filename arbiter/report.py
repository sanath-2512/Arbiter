"""Human-readable results: a terminal card and a Markdown report per run.

Every statement in the report is derived from recorded artifacts (result.json, evidence.jsonl,
actions.jsonl, patch.diff) and names the artifact, so a reader can check it. The report never
claims correctness; verification statuses mean exactly what evidence.py defines.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from arbiter.records import atomic_write_bytes, read_jsonl


def _c(code: str, text: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on else text


def card(result: dict[str, Any], *, color: bool, max_diff_lines: int = 120) -> str:
    d = result.get("deliverable") or {}
    v = result.get("verification") or {}
    u = result.get("usage") or {}
    iso = result.get("isolation") or {}
    ok = result.get("status") == "completed" and result.get("submission_ready")
    lines = [_c("1", f"━━ Result: {result.get('task_id')} ━━", color)]
    lines.append(f"status        {_c('32' if ok else '31', str(result.get('status')), color)} · termination "
                 f"{result.get('termination')} · submission_ready {'yes' if result.get('submission_ready') else 'no'}")
    if result.get("error"):
        lines.append(f"error         {json.dumps(result['error'])[:300]}")
    vs = v.get("status", "n/a")
    n_sel = len(v.get("records_on_selected") or [])
    lines.append(f"verification  {_c('32' if vs == 'checks_passed' else '33', vs, color)} — {n_sel} check run(s) on the "
                 f"selected code (details in report.md)")
    lines += proof_lines(result.get("proof") or {}, color)
    if d:
        lines.append(f"changes       {d.get('shortstat') or 'no changes'}")
        for f in (d.get("files") or [])[:25]:
            lines.append(f"  {f['status']} {f.get('old_path', '') + ' -> ' if f.get('old_path') else ''}{f['path']}")
        lines.append(f"patch         {d.get('patch_path')} (sha256 {str(d.get('patch_sha256'))[:16]}…)")
        lines.append(f"export        {'verified: ' + d['reconstruction_detail'] if d.get('reconstruction_verified') else 'NOT verified'}")
    lines.append(f"isolation     {'active — ' + iso.get('verified', '') if iso.get('active') else 'inactive (' + str(iso.get('reason')) + ')'}")
    lines.append(f"usage         {u.get('requests')} requests · {u.get('total_tokens')} tokens · "
                 f"{(result.get('timing') or {}).get('total_s')} s · cost {u.get('cost_usd') if u.get('cost_usd') is not None else 'n/a'}")
    lines.append(f"report        {Path(result.get('run_dir', '.')) / 'report.md'}")
    patch = Path(d["patch_path"]).read_text(errors="replace") if d.get("patch_path") and Path(d["patch_path"]).exists() else ""
    if patch:
        plines = patch.splitlines()
        lines.append("")
        for l in plines[:max_diff_lines]:
            if l.startswith("+") and not l.startswith("+++"):
                l = _c("32", l, color)
            elif l.startswith("-") and not l.startswith("---"):
                l = _c("31", l, color)
            elif l.startswith("@@"):
                l = _c("36", l, color)
            lines.append(l)
        if len(plines) > max_diff_lines:
            lines.append(f"[... {len(plines) - max_diff_lines} more diff lines in {d['patch_path']} ...]")
    return "\n".join(lines) + "\n"


LEVEL_COLOR = {"proven": "32", "fixed": "32", "passing": "33", "unverified": "33", "refuted": "31"}
MARK = {"pass": "pass", "fail": "FAIL", None: "—"}


def proof_lines(p: dict[str, Any], color: bool) -> list[str]:
    """The proof table: each check on the original code (+ the patch's own tests) and on the patch."""
    if not p:
        return []
    level = str(p.get("level"))
    out = [f"proof         {_c(LEVEL_COLOR.get(level, '33'), level.upper(), color)} — {p.get('summary', '')}"[:400]]
    comps = p.get("comparisons") or []
    if comps:
        out.append(f"  {'check':<52} {'original':>8} {'patched':>8}  result")
        for c in comps[:8]:
            name = ("repro " if c.get("kind") == "reproduction" else "") + " ".join(str(c.get("command", "")).split())
            name = name if len(name) <= 52 else name[:51] + "…"
            res = c.get("verdict", "").replace("_", " ")
            if c.get("pass_to_fail"):
                res += f" · breaks {', '.join(c['pass_to_fail'][:2])}"
            elif c.get("fail_to_pass"):
                res += f" · fixes {', '.join(c['fail_to_pass'][:2])}"
            out.append(f"  {name:<52} {MARK.get(c.get('original')):>8} {MARK.get(c.get('candidate')):>8}  {res}"[:160])
    attempts = p.get("attempts") or []
    if len(attempts) > 1:
        out.append("attempts      " + " · ".join(f"#{a['n']} {a.get('level')} ({a.get('termination')})" for a in attempts))
    return out


def write_report(result: dict[str, Any], task_issue: str) -> Path:
    run_dir = Path(result["run_dir"])
    d = result.get("deliverable") or {}
    v = result.get("verification") or {}
    evidence = read_jsonl(run_dir / "evidence.jsonl")
    actions = read_jsonl(run_dir / "actions.jsonl")
    m = result.get("model") or {}
    out = [f"# Run report — {result.get('task_id')}", ""]
    out.append("Every item below is taken from files in this run directory; file names are given so each "
               "claim can be checked. Correctness is decided by external evaluation, not by this report.")
    out += ["", "## Outcome", "",
            f"- Status: **{result.get('status')}**, termination `{result.get('termination')}`, "
            f"submission_ready **{result.get('submission_ready')}** (`result.json`)",
            f"- Verification: **{v.get('status')}** — {v.get('detail')} (`evidence.jsonl`)",
            f"- Selected candidate: tree `{(result.get('selected_candidate') or {}).get('tree')}` — "
            f"{(result.get('selected_candidate') or {}).get('reason')} (`candidates.json`)"]
    if result.get("submit_summary"):
        out.append(f"- Model's own summary (a claim, not evidence): {result['submit_summary']}")
    p = result.get("proof") or {}
    if p:
        out += ["", "## Proof (computed by the harness, `arbiter/proof.py`)", "",
                f"Evidence level: **{p.get('level')}** — {p.get('summary')}", "",
                "Each check was run on the original code (plus the patch's own test changes, so new tests exist) "
                "and on the patched code:", "",
                "| check | original | patched | result | fail→pass | pass→fail |", "|---|---|---|---|---|---|"]
        for c in p.get("comparisons") or []:
            out.append(f"| {'reproduction ' if c.get('kind') == 'reproduction' else ''}`{str(c.get('command'))[:70]}` | "
                       f"{c.get('original') or '—'} | {c.get('candidate') or '—'} | {c.get('verdict')} | "
                       f"{', '.join(c.get('fail_to_pass') or [])[:120] or '—'} | "
                       f"{', '.join(c.get('pass_to_fail') or [])[:120] or '—'} |")
        for r in p.get("reproductions") or []:
            state = {True: "confirmed: fails on the original code", False: "does not fail on the original code",
                     None: "unconfirmed"}[r.get("confirmed")]
            out.append(f"- Reproduction {r['id']} (attempt {r.get('attempt')}): `{r['command'][:100]}` — {state}")
        for a in p.get("attempts") or []:
            out.append(f"- Attempt {a['n']}: {a.get('termination')}, {a.get('steps', '?')} steps, "
                       f"{a.get('elapsed_s', '?')} s, evidence **{a.get('level')}** — {a.get('summary')}")
    if d:
        out += ["", "## Deliverable", "",
                f"- Patch: `patch.diff`, {d.get('patch_bytes')} bytes, sha256 `{d.get('patch_sha256')}`",
                f"- Clean reconstruction: {d.get('reconstruction_detail')}",
                f"- Working tree matches selected candidate: {d.get('worktree_matches_selected')}",
                f"- Changes: {d.get('shortstat') or 'none'}"]
        out += [f"  - `{f['status']}` {f['path']}" for f in d.get("files") or []]
        if d.get("excluded_paths"):
            out.append(f"- Excluded from the patch ({d.get('excluded_reason')}): "
                       + ", ".join(f"`{p}`" for p in d["excluded_paths"][:20]))
    if evidence:
        out += ["", "## Checks run (bound to exact code states)", "",
                "| id | tree | outcome | counts | command | source |", "|---|---|---|---|---|---|"]
        for e in evidence:
            out.append(f"| {e['id']} | `{e['tree'][:10]}` | {e['outcome']} | {e['counts']} | `{e['command'][:60]}` | "
                       f"{e['source']} |")
    if actions:
        out += ["", "## Actions (`actions.jsonl`)", ""]
        for a in actions:
            try:
                args = json.loads(a["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            detail = args.get("command") or args.get("path") or args.get("pattern") or ""
            out.append(f"{a['step']}. `{a['tool']}` {str(detail)[:100]} → {a['status']}")
    integ = result.get("integrity") or {}
    iso = result.get("isolation") or {}
    out += ["", "## Integrity and isolation", "",
            f"- Isolation: {'active — ' + iso.get('verified', '') if iso.get('active') else 'inactive — ' + str(iso.get('reason'))}",
            f"- Modified existing test files (lines removed or changed): {integ.get('modified_existing_test_files') or 'none'}",
            f"- Existing test files extended (lines added only): {integ.get('extended_existing_test_files') or 'none'}",
            f"- Added test files: {integ.get('added_test_files') or 'none'}",
            f"- Accesses to controller state: {len(integ.get('controller_state_access') or [])}; "
            f"to the harness checkout: {len(integ.get('harness_repo_access') or [])}",
            f"- Target .git config/hooks changed during the run: {'yes' if integ.get('target_git_control_files_changed') else 'no'}"]
    u = result.get("usage") or {}
    out += ["", "## Reproducibility", "",
            f"- Model: {m.get('provider')} / `{m.get('name')}` at {m.get('base_url')} (profile {m.get('profile')}, "
            f"id `{m.get('profile_id')}`){'; resolution: ' + json.dumps(m.get('resolution')) if m.get('resolution') else ''}",
            f"- Usage: {u.get('requests')} requests ({u.get('requests_usage_unknown')} with unknown usage), "
            f"{u.get('total_tokens')} tokens, cost {u.get('cost_usd') if u.get('cost_usd') is not None else 'not computed'}",
            "- Model outputs are not bit-reproducible on hosted APIs; the full transcript is in `transcript.jsonl`."]
    if result.get("notes"):
        out += ["", "## Notes", ""] + [f"- {n}" for n in result["notes"]]
    out += ["", "## Issue as given to the model", "", "```text", task_issue[:8000], "```", ""]
    path = run_dir / "report.md"
    atomic_write_bytes(path, "\n".join(out).encode("utf-8"))
    return path


def is_tty(stream=None) -> bool:
    stream = stream or sys.stdout
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
