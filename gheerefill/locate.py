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

import fnmatch
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


# Trees that rarely hold the fix and can be huge: read last (GitHub linguist's vendor/generated conventions)
PERIPHERAL_DIR = re.compile(r"^(vendor(ed)?|third[_-]?party|3rd[_-]?party|externals?|extern|node_modules|"
                            r"bower_components|site-packages|dist|build|gen|_{0,2}generated_{0,2})([_.-].*)?$", re.I)
MINIFIED = re.compile(r"[.-]min\.(js|mjs|css)$|\.bundle\.(js|mjs)$", re.I)
DOTTED = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,})(?![\w.])")


def linguist_patterns(repo: Path) -> list[str]:
    """Paths `.gitattributes` marks linguist-generated or linguist-vendored."""
    out = []
    try:
        lines = (repo / ".gitattributes").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("#") and any(
                a.split("=")[0] in ("linguist-generated", "linguist-vendored") and not a.endswith("=false")
                for a in parts[1:]):
            out.append(parts[0].lstrip("/"))
    return out


def peripheral(path: str, linguist: list[str] = ()) -> bool:
    parts = path.split("/")
    if any(PERIPHERAL_DIR.match(d) for d in parts[:-1]) or MINIFIED.search(parts[-1]):
        return True
    for pat in linguist:
        if "/" in pat.rstrip("/"):
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("/*") + "/*"):
                return True
        elif fnmatch.fnmatch(parts[-1], pat) or pat.rstrip("/") in parts[:-1]:
            return True
    return False


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


PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[\w \t,*]+)", re.M)
PY_PLAIN = re.compile(r"^[ \t]*import[ \t]+([\w. \t,]+)", re.M)
JS_IMPORT = re.compile(r"""(?:require\(\s*|from\s+|import\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""")
GO_IMPORT = re.compile(r'"([\w.\-/]+)"')


def _py_imports(path: str, text: str) -> set[str]:
    pkg = path.split("/")[:-1]
    out: set[str] = set()
    for plain in PY_PLAIN.findall(text):
        out |= {n.strip().split(" as ")[0].strip() for n in plain.split(",") if n.strip()}
    for frm, names in PY_FROM.findall(text):
        level = len(frm) - len(frm.lstrip("."))
        base = frm.lstrip(".")
        if level:
            anchor = pkg[: len(pkg) - (level - 1)] if level - 1 <= len(pkg) else []
            base = ".".join(anchor + ([base] if base else []))
        out.add(base)
        out |= {f"{base}.{n.strip().split(' as ')[0].strip()}" for n in names.strip("()").replace("\n", " ").split(",")
                if n.strip() and not n.strip().startswith("#")}
    return out


def _py_module_names(path: str) -> set[str]:
    parts = path[:-3].split("/") if path.endswith(".py") else []
    package = bool(parts) and parts[-1] == "__init__"
    if package:
        parts = parts[:-1]
    names = {".".join(parts[i:]) for i in range(len(parts)) if len(parts) - i >= 2}
    if parts and (package or len(parts) == 1 or parts[-2] in ("src", "lib")):
        names.add(parts[-1])  # a top-level package/module is imported by its bare name
    return names


def _js_resolve(importer: str, spec: str, fileset: set[str]) -> str | None:
    base = str(Path(importer).parent / spec)
    parts: list[str] = []
    for p in base.split("/"):
        if p == "..":
            parts = parts[:-1]
        elif p not in (".", ""):
            parts.append(p)
    b = "/".join(parts)
    for cand in (b, *(b + e for e in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")),
                 *(b + "/index" + e for e in (".js", ".ts"))):
        if cand in fileset:
            return cand
    return None


def _rust_relations(t: str, texts: dict[str, str], repo: Path) -> tuple[list[str], list[str]]:
    """(files using the module, test files for it) for a Rust source file: `crate::a::b` / `<crate>::a::b`
    / `super::b` paths, integration tests in the crate's tests/, and the file itself when it has an
    inline #[cfg(test)] module."""
    parts = Path(t).parts
    crate_root = next((Path(*parts[:i]) for i in range(len(parts) - 1, -1, -1)
                       if (repo / Path(*parts[:i]) / "Cargo.toml").is_file()), None)
    if crate_root is None:
        return [], []
    rel = Path(t).relative_to(crate_root) if str(crate_root) != "." else Path(t)
    if rel.parts[:1] != ("src",):
        return [], []
    mod = [p for p in rel.with_suffix("").parts[1:] if p not in ("lib", "main", "mod")]
    try:
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', (repo / crate_root / "Cargo.toml").read_text(errors="replace"), re.M)
    except OSError:
        m = None
    crate = m.group(1).replace("-", "_") if m else None
    root = "" if str(crate_root) == "." else str(crate_root) + "/"
    in_crate = {f: txt for f, txt in texts.items() if f.endswith(".rs") and f.startswith(root) and f != t}
    importers: list[str] = []
    if mod:
        path = "::".join(mod)
        pats = [rf"\bcrate::{re.escape(path)}\b", rf"\bsuper::{re.escape(mod[-1])}\b"]
        if crate:
            pats.append(rf"\b{re.escape(crate)}::{re.escape(path)}\b")
        rx = re.compile("|".join(pats))
        importers = [f for f, txt in in_crate.items() if rx.search(txt)]
    tests = [f for f in in_crate if f.startswith(root + "tests/")
             and (crate is None or crate in in_crate[f] or not mod)]
    if mod:  # integration tests naming the module first
        tests.sort(key=lambda f: (mod[-1] not in in_crate[f], f))
    if "#[cfg(test)]" in texts.get(t, ""):
        tests.insert(0, t)
    return importers, tests


def import_graph(targets: list[str], texts: dict[str, str], fileset: set[str], repo: Path) -> dict[str, dict]:
    """For each target file: the files that import it, and which of those are tests (tests are often
    not next to the code they cover)."""
    from gheerefill.proof import is_test_path

    out: dict[str, dict] = {}
    py_index = {f: _py_imports(f, t) for f, t in texts.items() if f.endswith(".py")}
    js_index = {f: {_js_resolve(f, m, fileset) for m in JS_IMPORT.findall(t)} for f, t in texts.items()
                if f.endswith((".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"))}
    go_module = ""
    try:
        m = re.search(r"^module\s+(\S+)", (repo / "go.mod").read_text(errors="replace"), re.M)
        go_module = m.group(1) if m else ""
    except OSError:
        pass
    for t in targets:
        importers: list[str] = []
        via_package: list[str] = []
        if t.endswith(".py"):
            names = _py_module_names(t)
            importers = [f for f, imps in py_index.items() if f != t and imps & names]
            for pkg_init in [f for f in importers if f.endswith("__init__.py")]:  # re-exported by its package
                pnames = _py_module_names(pkg_init)
                via_package += [f for f, imps in py_index.items() if f not in (t, pkg_init) and imps & pnames
                                and f not in importers]
        elif t in {x for s in js_index.values() for x in s if x}:
            importers = [f for f, imps in js_index.items() if t in imps]
        elif t.endswith(".go") and go_module:
            pkg_dir = str(Path(t).parent)
            path = go_module if pkg_dir == "." else f"{go_module}/{pkg_dir}"
            importers = [f for f, txt in texts.items() if f.endswith(".go") and path in GO_IMPORT.findall(txt)]
            importers += [f for f in texts if f.endswith("_test.go") and str(Path(f).parent) == pkg_dir and f != t]
        elif t.endswith(".rs"):
            users, rs_tests = _rust_relations(t, texts, repo)
            named = [f"{f} (inline #[cfg(test)] module)" if f == t else f for f in rs_tests]
            out[t] = {"imported_by": sorted(f for f in set(users) if not is_test_path(f))[:8],
                      "tests": list(dict.fromkeys(named + sorted(f for f in users if is_test_path(f))))[:8]}
            continue
        importers = sorted(set(importers))
        stem = Path(t).stem.lower().replace("__init__", Path(t).parent.name.lower())
        # tests reaching the module only through its package: those named after the module first
        indirect = sorted({f for f in via_package if is_test_path(f)},
                          key=lambda f: (stem not in Path(f).stem.lower(), f))
        if len(via_package) > 30:
            indirect = [f for f in indirect if stem in Path(f).stem.lower()]
        out[t] = {"imported_by": [f for f in importers if not is_test_path(f)][:8],
                  "tests": ([f for f in importers if is_test_path(f)] + indirect)[:8]}
    return out


def localize(issue: str, repo: Path, files: list[str], *, time_budget_s: float = 3.0,
             byte_budget: int = 40 * 1024 * 1024, top: int = 6) -> dict[str, Any]:
    from gheerefill.proof import is_test_path

    t0 = time.monotonic()
    src = [f for f in files if Path(f).suffix.lower() in SOURCE_EXT]
    fileset = set(files)
    linguist = linguist_patterns(repo)
    out: dict[str, Any] = {"files_named": [], "definitions": [], "ranked": [], "complete": True}

    # 1a. paths and stack frames named in the issue, and dotted module references (pkg.module.Name)
    named = [m[0] for m in PATH_IN_TEXT.findall(issue)] + [m[0] for m in FRAME.findall(issue)]
    for p in dict.fromkeys(named):
        p = p.lstrip("./")
        hits = [p] if p in fileset else [f for f in src if f.endswith("/" + p) or p.endswith("/" + f)]
        if 0 < len(hits) <= 3:
            out["files_named"] += [h for h in hits if h not in out["files_named"]]
    for ref in dict.fromkeys(DOTTED.findall(issue)):
        parts = ref.split(".")
        if "." + parts[-1].lower() in SOURCE_EXT:
            continue  # a file name, handled above
        for k in (len(parts), len(parts) - 1):  # the module itself, or the module holding an attribute
            mod = "/".join(parts[:k])
            hits = [f for f in src if any(f == c or f.endswith("/" + c) for c in (mod + ".py", mod + "/__init__.py"))]
            hits = [h for h in hits if not peripheral(h, linguist)] or hits
            if 0 < len(hits) <= 3:
                out["files_named"] += [h for h in hits if h not in out["files_named"]]
                break

    # 2. read sources (bounded) for definitions and BM25. Order matters when the budget runs out on a
    # large repository: code before tests, vendored/generated trees last, and paths that share words
    # with the issue first within each group (depth-first order would spend it all on shallow tests).
    issue_terms = set(terms(issue))

    def read_order(f: str) -> tuple:
        p = Path(f)
        near = set(split_ident(p.stem)) | {t for d in p.parent.parts for t in split_ident(d)}
        return peripheral(f, linguist), is_test_path(f), -min(len(near & issue_terms), 2), f.count("/"), f

    docs: dict[str, Counter] = {}
    texts: dict[str, str] = {}
    used = 0
    for f in sorted(src, key=read_order):
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
        defs = sorted(out["definitions"], key=lambda d: (peripheral(d["path"], linguist), is_test_path(d["path"]),
                                                         idents.index(d["name"]), d["path"]))
        per: dict[str, int] = {}
        kept = []
        for d in defs:
            per[d["name"]] = per.get(d["name"], 0) + 1
            if per[d["name"]] <= 3:
                kept.append(d)
        out["definitions"] = kept[:12]

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
        scores.sort(key=lambda x: (peripheral(x[1], linguist), -x[0], x[1]))  # vendored copies after the real code
        out["ranked"] = [{"path": f, "score": round(s, 2)} for s, f in scores[:top]]
    # 3. where the likely files sit in the import graph (callers; tests that import them)
    targets = list(dict.fromkeys(out["files_named"] + [d["path"] for d in out["definitions"]]
                                 + [r["path"] for r in out["ranked"][:2]]))[:4]
    if targets and time.monotonic() - t0 < time_budget_s:
        out["related"] = import_graph(targets, texts, fileset, repo)
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
    for path, rel in (loc.get("related") or {}).items():
        if rel.get("imported_by") or rel.get("tests"):
            parts = []
            if rel.get("imported_by"):
                parts.append("imported by " + ", ".join(rel["imported_by"][:5]))
            if rel.get("tests"):
                parts.append("tests that import it: " + ", ".join(rel["tests"][:5]))
            lines.append(f"- {path}: " + "; ".join(parts))
    if not lines:
        return ""
    return "\nHints from a deterministic search of the repository for the issue's terms (unverified; use them only " \
           "if they fit):\n" + "\n".join(lines)
