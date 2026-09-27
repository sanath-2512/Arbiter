"""Attestation of a run's deliverable, and offline verification of it.

`attestation.json` is an in-toto Statement v1 (https://in-toto.io/Statement/v1) whose subject is the
exported patch and whose predicate binds it to the base and selected trees, the harness's proof
(proof.py) and every evidence record it rests on, with SHA-256 digests of the archived outputs.
It is unsigned: signing would need a key the harness does not have. Integrity comes from the
digests, and `arbiter verify` recomputes them.

`arbiter verify --run-dir DIR` checks, without the model or the network:
  1. the patch file matches the attested digest;
  2. applying the patch to the base tree (from the run's shadow store) reproduces the selected tree;
  3. every evidence record the proof cites exists, is bound to the attested trees, and its archived
     output matches the attested digest.
With --rerun it also re-executes each proof check in a temporary copy of the original code (plus the
patch's test changes) and of the patched code, and reports whether each verdict is reproduced. A
temporary copy is not the original environment (e.g. editable installs point at the original
checkout), so a rerun disagreement is reported, not treated as proof of tampering.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from arbiter import __version__

PREDICATE = "https://github.com/sanath-2512/arbiter/blob/main/docs/proof-v1.md"
HARNESS_ROOT = Path(__file__).resolve().parent.parent


def sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def harness_commit() -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HARNESS_ROOT, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=HARNESS_ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (p.stdout.strip() + ("+dirty" if dirty else "")) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def build(run_dir: Path, result: dict[str, Any], records: list, base_tree: str,
          counterfactual_tree: str | None) -> dict[str, Any]:
    d = result.get("deliverable") or {}
    sel = (result.get("selected_candidate") or {}).get("tree")
    proof = result.get("proof") or {}
    cited = {r.id for r in records if r.tree in (sel, counterfactual_tree)}
    evidence = []
    for r in records:
        if r.id not in cited:
            continue
        out = run_dir / "outputs" / f"{r.output_id}.txt" if r.output_id else None
        evidence.append({"id": r.id, "tree": r.tree, "command": r.command, "kind": r.kind, "source": r.source,
                         "outcome": r.outcome, "counts": r.counts, "failing": r.failing, "binding": r.binding,
                         "output": f"outputs/{r.output_id}.txt" if r.output_id else None,
                         "output_sha256": sha256_file(out) if out else None})
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "patch.diff", "digest": {"sha256": d.get("patch_sha256")}}],
        "predicateType": PREDICATE,
        "predicate": {
            "harness": {"name": "arbiter", "version": __version__, "commit": harness_commit()},
            "task_id": result.get("task_id"),
            "model": {k: (result.get("model") or {}).get(k) for k in ("provider", "name", "base_url", "live",
                                                                      "profile_id", "resolution")},
            "base_tree": base_tree,
            "selected_tree": sel,
            "counterfactual_tree": counterfactual_tree,
            "reconstruction_verified": d.get("reconstruction_verified"),
            "proof": {"level": proof.get("level"), "summary": proof.get("summary"),
                      "comparisons": proof.get("comparisons") or [], "reproductions": proof.get("reproductions") or []},
            "evidence": evidence,
            "files": {name: sha256_file(run_dir / name) for name in ("evidence.jsonl", "transcript.jsonl",
                                                                      "actions.jsonl", "requests.jsonl", "task.json",
                                                                      "profile.json")},
        },
    }


def verify(run_dir: Path, *, rerun: bool = False, timeout_s: float = 300.0) -> dict[str, Any]:
    from arbiter.evidence import classify_output
    from arbiter.proof import classify_reproduction
    from arbiter.workspace import Workspace

    run_dir = Path(run_dir).resolve()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        st = json.loads((run_dir / "attestation.json").read_text())
    except (OSError, ValueError) as e:
        return {"ok": False, "checks": [{"check": "attestation readable", "ok": False, "detail": str(e)}]}
    pred = st.get("predicate") or {}
    want = ((st.get("subject") or [{}])[0].get("digest") or {}).get("sha256")
    got = sha256_file(run_dir / "patch.diff")
    check("patch digest matches the attestation", want is not None and got == want, f"{got}")
    for name, digest in (pred.get("files") or {}).items():
        if digest is not None:
            check(f"{name} digest", sha256_file(run_dir / name) == digest)
    ws = Workspace(Path(tempfile.gettempdir()), run_dir)  # only the shadow store is used
    base, sel = pred.get("base_tree"), pred.get("selected_tree")
    try:
        ws.attach(base)
        ok, detail = ws.verify_reconstruction(base, sel, run_dir / "patch.diff")
        check("patch applied to the base reproduces the selected tree", ok, detail)
    except Exception as e:  # noqa: BLE001
        check("patch applied to the base reproduces the selected tree", False, f"{type(e).__name__}: {e}")
    records = {}
    for line in (run_dir / "evidence.jsonl").read_text().splitlines() if (run_dir / "evidence.jsonl").exists() else []:
        try:
            r = json.loads(line)
            records[r["id"]] = r
        except (ValueError, KeyError):
            continue
    for e in pred.get("evidence") or []:
        r = records.get(e["id"])
        same = r is not None and all(r.get(k) == e.get(k) for k in ("tree", "command", "outcome"))
        out_ok = e.get("output") is None or sha256_file(run_dir / e["output"]) == e.get("output_sha256")
        check(f"evidence {e['id']} ({e['outcome']} on {e['tree'][:10]})", same and out_ok,
              "" if same and out_ok else "record or archived output differs from the attestation")
    reruns = []
    if rerun and sel and base:
        cf = pred.get("counterfactual_tree") or base
        for comp in (pred.get("proof") or {}).get("comparisons") or []:
            row = {"command": comp["command"], "attested": [comp.get("original"), comp.get("candidate")]}
            got_v = []
            for tree in (cf, sel):
                with tempfile.TemporaryDirectory() as td:
                    work = Path(td) / "w"
                    ws.materialize(tree, work)
                    try:
                        p = subprocess.run(["bash", "-c", comp["command"]], cwd=work, capture_output=True, text=True,
                                           timeout=timeout_s)
                        text, code = p.stdout + p.stderr, p.returncode
                    except subprocess.TimeoutExpired:
                        text, code = "", None
                    oc = classify_reproduction(text, code) if comp.get("kind") == "reproduction" else \
                        classify_output(text, code)
                    got_v.append({"passed": "pass", "failed": "fail", "collection_error": "fail"}.get(oc.outcome))
            row["rerun"] = got_v
            row["agrees"] = got_v == row["attested"]
            reruns.append(row)
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "level": (pred.get("proof") or {}).get("level"), "checks": checks, "reruns": reruns}
