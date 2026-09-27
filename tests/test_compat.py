import ast
import shutil
import subprocess
import unittest

from tests.helpers import ROOT

MIN = (3, 9)
SOURCES = [p for d in ("arbiter", "tests", "scripts", "baselines") for p in sorted((ROOT / d).rglob("*.py"))
           if "_vendor" not in p.parts]


def interpreters():
    """Older interpreters available here (PATH), used for a real import/parse smoke test."""
    found = []
    for name in ("python3.9", "python3.10"):
        py = shutil.which(name)
        if py:
            found.append(py)
    return found


class MinimumPythonTest(unittest.TestCase):
    """The runtime and tests must stay valid on the minimum supported Python (3.9)."""

    def test_sources_parse_with_minimum_grammar(self):
        for path in SOURCES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=MIN)

    def test_no_runtime_pep604_unions_at_module_level(self):
        # `X | Y` between types is evaluated at import time outside annotations; it fails before 3.10.
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.BitOr):
                    sides = (node.value.left, node.value.right)
                    if any(isinstance(x, (ast.Subscript, ast.Name)) and not isinstance(x, ast.Constant) for x in sides):
                        names = {getattr(x, "id", None) or getattr(getattr(x, "value", None), "id", None) for x in sides}
                        if names & {"dict", "list", "tuple", "set", "str", "int", "float", "bytes", "None", "Callable",
                                    "Any", "Path"}:
                            self.fail(f"{path.relative_to(ROOT)}:{node.lineno}: runtime type union needs typing.Union")

    def test_imports_and_profile_load_on_older_interpreters(self):
        pys = interpreters()
        if not pys:
            raise unittest.SkipTest("no python3.9/python3.10 on PATH")
        code = ("import arbiter.cli, arbiter.agent, arbiter.intake, arbiter.resolve, arbiter.report; "
                "from arbiter.config import load_profile, validate; "
                "p = load_profile('profiles/default.toml', env={}); validate(p); print(len(p.auto))")
        for py in pys:
            with self.subTest(python=py):
                p = subprocess.run([py, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr[-2000:])
                self.assertGreater(int(p.stdout.strip()), 0)
