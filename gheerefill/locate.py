"""Deterministic localisation hints from the issue text. No extra model is involved: the prescribed
model is the only model the harness ever calls.

1. Anchors: file paths and stack-trace frames named in the issue that exist in the repository, and
   definitions of the code identifiers the issue mentions.
2. BM25 ranking (Robertson & Zaragoza; the SWE-bench retrieval baseline) of source files against
   the issue's identifier-like terms, with the file path counted as part of the document.

Bounded in time and bytes; the result is shown to the model as unverified hints and recorded in
localization.json. The harness never acts on it.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".swift", ".m", ".mm", ".lua", ".ex",
    ".exs", ".erl", ".hs", ".ml", ".clj", ".dart", ".vue", ".svelte", ".sh", ".r", ".jl", ".zig", ".nim",
}
STOP = set("""
the and for that this with from have not are was were but you your can will would should could when what which
there their then than into been being does did doing done also only just like more most some such very about
after before over under again once here where while each other any all both few many much same so too use used
using get got make made new one two issue bug error fix fixed problem expected actual behavior behaviour result
results return returns returned value values none null true false self cls def class import function func var
let const print test tests testing file files line lines code python version work works working case cases call
called calls need needs please thanks example examples output input run running raise raised exception see
""".split())
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
PATH_IN_TEXT = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:%s))(?::(\d+))?\b" %
                          "|".join(sorted(e[1:] for e in SOURCE_EXT)))
FRAME = re.compile(r'File "([^"]+)", line (\d+)')
BACKTICK = re.compile(r"`([^`\n]{2,80})`")
DEFINITION = r"(?:def|class|function|func|fn|interface|struct|type|enum|trait|module|record)\s+{name}\b|" \
             r"\b{name}\s*(?:=|:)\s*(?:function\b|\(|lambda\b|async\b)"


def split_ident(tok: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok).replace("_", " ").lower().split()
    out = [p for p in parts if len(p) >= 3]
    low = tok.lower()
    if low not in out:
        out.append(low)
    return out


def terms(text: str) -> list[str]:
    return [t for tok in IDENT.findall(text) for t in split_ident(tok) if t not in STOP]


def code_identifiers(issue: str) -> list[str]:
    """Identifiers that look like code: in backticks, CamelCase, snake_case or called like f()."""
    found: list[str] = []
    for span in BACKTICK.findall(issue):
        found += [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", span) if len(t) >= 3]
    found += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]+|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b",
                        issue)
    found += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\(", issue)
    seen: list[str] = []
    for t in found:
        if t.lower() not in STOP and t not in seen:
            seen.append(t)
    return seen[:15]


def localize(issue: str, repo: Path, files: list[str], *, time_budget_s: float = 3.0,
             byte_budget: int = 40 * 1024 * 1024, top: int = 6) -> dict[str, Any]:
    t0 = time.monotonic()
    src = [f for f in files if Path(f).suffix.lower() in SOURCE_EXT]
    fileset = set(files)
    out: dict[str, Any] = {"files_named": [], "definitions": [], "ranked": [], "complete": True}

    # 1a. paths and stack frames named in the issue
    named = [m[0] for m in PATH_IN_TEXT.findall(issue)] + [m[0] for m in FRAME.findall(issue)]
    for p in dict.fromkeys(named):
        p = p.lstrip("./")
        hits = [p] if p in fileset else [f for f in src if f.endswith("/" + p) or p.endswith("/" + f)]
        if 0 < len(hits) <= 3:
            out["files_named"] += [h for h in hits if h not in out["files_named"]]

    # 2. read sources (bounded) for definitions and BM25
    docs: dict[str, Counter] = {}
    texts: dict[str, str] = {}
    used = 0
    for f in sorted(src, key=lambda f: (f.count("/"), f)):
        if time.monotonic() - t0 > time_budget_s * 0.6 or used > byte_budget:
            out["complete"] = False
            break
        try:
            data = (repo / f).read_bytes()
        except OSError:
            continue
        if len(data) > 512 * 1024 or b"\0" in data[:4096]:
            continue
        used += len(data)
        text = data.decode("utf-8", "replace")
        texts[f] = text
        c = Counter(terms(text))
        for t in split_ident(Path(f).stem) + [p for d in Path(f).parent.parts for p in split_ident(d)]:
            c[t] += 3  # the path is part of the document
        docs[f] = c

    # 1b. definitions of identifiers the issue mentions
    idents = code_identifiers(issue)
    if idents and texts:
        pats = [(n, re.compile(DEFINITION.format(name=re.escape(n)), re.M)) for n in idents]
        for f, text in texts.items():
            if time.monotonic() - t0 > time_budget_s * 0.8:
                out["complete"] = False
                break
            for name, pat in pats:
                m = pat.search(text)
                if m:
                    out["definitions"].append({"name": name, "path": f, "line": text.count("\n", 0, m.start()) + 1})
        out["definitions"] = sorted(out["definitions"], key=lambda d: (idents.index(d["name"]), d["path"]))[:12]

    # 2b. BM25 over the loaded documents
    q = Counter(terms(issue) + [t for i in idents for t in split_ident(i)])  # code identifiers count twice
    if q and docs:
        n = len(docs)
        avg = sum(sum(c.values()) for c in docs.values()) / n
        df = Counter(t for c in docs.values() for t in set(c) if t in q)
        k1, b = 1.2, 0.75
        scores = []
        for f, c in docs.items():
            length = sum(c.values()) or 1
            s = 0.0
            for t, qtf in q.items():
                tf = c.get(t, 0)
                if tf:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += qtf * idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
            if s > 0:
                scores.append((s, f))
        scores.sort(key=lambda x: (-x[0], x[1]))
        out["ranked"] = [{"path": f, "score": round(s, 2)} for s, f in scores[:top]]
    out["elapsed_s"] = round(time.monotonic() - t0, 3)
    out["files_indexed"] = len(docs)
    return out


def render(loc: dict[str, Any]) -> str:
    lines = []
    if loc.get("files_named"):
        lines.append("- Files named in the issue: " + ", ".join(loc["files_named"][:6]))
    if loc.get("definitions"):
        lines.append("- Definitions of identifiers the issue mentions: "
                     + ", ".join(f"`{d['name']}` {d['path']}:{d['line']}" for d in loc["definitions"][:8]))
    if loc.get("ranked"):
        lines.append("- Files whose content best matches the issue text (BM25): "
                     + ", ".join(r["path"] for r in loc["ranked"]))
    if not lines:
        return ""
    return "\nHints from a deterministic search of the repository for the issue's terms (unverified; use them only " \
           "if they fit):\n" + "\n".join(lines)
