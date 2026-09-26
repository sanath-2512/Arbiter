from gheerefill import locate
from tests.helpers import CALC, TempDirCase, make_repo


class LocateTest(TempDirCase):
    def setUp(self):
        super().setUp()
        files = dict(CALC)
        files["calc/strings.py"] = "def slugify(text):\n    return text.lower()\n"
        files["docs/notes.txt"] = "divide divide divide"
        self.repo = make_repo(self.tmp / "r", files)
        self.files = sorted(files)

    def test_anchors_and_ranking(self):
        issue = ("`divide(7, 2)` returns 3 instead of 3.5.\n\nTraceback (most recent call last):\n"
                 '  File "/home/someone/project/calc/ops.py", line 2, in divide\n')
        loc = locate.localize(issue, self.repo, self.files)
        self.assertEqual(loc["files_named"], ["calc/ops.py"])  # absolute path mapped by suffix
        self.assertIn({"name": "divide", "path": "calc/ops.py", "line": 1}, loc["definitions"])
        self.assertEqual(loc["ranked"][0]["path"], "calc/ops.py")
        self.assertNotIn("docs/notes.txt", [r["path"] for r in loc["ranked"]])  # not a source file
        text = locate.render(loc)
        self.assertIn("`divide` calc/ops.py:1", text)
        self.assertIn("unverified", text)

    def test_nothing_found_renders_nothing(self):
        loc = locate.localize("The website is slow on Tuesdays.", self.repo, self.files)
        self.assertEqual(locate.render(loc), "")

    def test_budget_is_respected(self):
        loc = locate.localize("divide", self.repo, self.files, time_budget_s=0.0)
        self.assertFalse(loc["complete"])


class ImportGraphTest(TempDirCase):
    def test_python_js_go_importers_and_tests_elsewhere(self):
        files = {
            "src/shop/__init__.py": "from .pricing import total\n",
            "src/shop/pricing.py": "def total(items):\n    return sum(items)\n",
            "src/shop/api/handlers.py": "from ..pricing import total\n",
            "tests/unit/test_pricing.py": "from shop.pricing import total\n",
            "tests/test_shop.py": "import shop\n",
            "web/lib/router.js": "const svc = require('./service');\n",
            "web/lib/service.js": "module.exports = {};\n",
            "web/test/service.test.js": "import svc from '../lib/service'\n",
            "go.mod": "module example.com/app\n",
            "internal/calc/calc.go": "package calc\n",
            "cmd/main.go": 'import (\n  "example.com/app/internal/calc"\n)\n',
            "internal/calc/calc_test.go": "package calc\n",
        }
        repo = make_repo(self.tmp / "g", files, init_git=False)
        texts = {f: (repo / f).read_text() for f in files}
        g = locate.import_graph(["src/shop/pricing.py", "web/lib/service.js", "internal/calc/calc.go"], texts,
                                set(files), repo)
        self.assertEqual(g["src/shop/pricing.py"]["imported_by"], ["src/shop/__init__.py", "src/shop/api/handlers.py"])
        self.assertEqual(g["src/shop/pricing.py"]["tests"], ["tests/unit/test_pricing.py", "tests/test_shop.py"])
        self.assertEqual(g["web/lib/service.js"], {"imported_by": ["web/lib/router.js"],
                                                   "tests": ["web/test/service.test.js"]})
        self.assertEqual(g["internal/calc/calc.go"], {"imported_by": ["cmd/main.go"],
                                                      "tests": ["internal/calc/calc_test.go"]})
