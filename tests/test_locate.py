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


class ReadOrderTest(TempDirCase):
    """When the budget cannot cover a large repository, what gets read first decides the hints."""

    def test_code_before_tests_and_vendored_last_under_a_tight_budget(self):
        files = {f"tests/t{i:04d}/test_mod.py": "def test_x():\n    assert 1\n" for i in range(300)}
        files["pkg/db/models/lookups.py"] = "class In:\n    def get_prep_lookup(self):\n        return list(self.rhs)\n"
        files["third_party/django_old/db/models/lookups.py"] = files["pkg/db/models/lookups.py"]
        repo = make_repo(self.tmp / "r", files)
        order = sorted(files, key=lambda f: (locate.peripheral(f), "tests/" in f, f))
        self.assertEqual(order[0], "pkg/db/models/lookups.py")
        loc = locate.localize("`__in` lookups break when given an iterator; see `get_prep_lookup`", repo,
                              sorted(files), time_budget_s=3.0)
        self.assertEqual(loc["definitions"][0]["path"], "pkg/db/models/lookups.py")  # the real one, not the copy
        self.assertEqual(loc["ranked"][0]["path"], "pkg/db/models/lookups.py")

    def test_peripheral_trees(self):
        for p in ("vendor/x.py", "a/third_party/b.py", "extra/vendored_pytest_8/src/m.py", "node_modules/l/i.js",
                  "bench/generated/pkg001/gen_1.py", "static/app.min.js", "web/dist/app.js"):
            self.assertTrue(locate.peripheral(p), p)
        for p in ("src/vendorize.py", "pkg/generator.py", "lib/builder.py", "src/app.js", "gen.py"):
            self.assertFalse(locate.peripheral(p), p)
        repo = make_repo(self.tmp / "g", {".gitattributes": "proto/*.pb.go linguist-generated=true\n"
                                                            "assets/** linguist-vendored\nkeep/** linguist-vendored=false\n",
                                          "x.py": ""})
        pats = locate.linguist_patterns(repo)
        self.assertTrue(locate.peripheral("proto/api.pb.go", pats))
        self.assertTrue(locate.peripheral("assets/js/lib.js", pats))
        self.assertFalse(locate.peripheral("keep/a.py", pats))
        self.assertFalse(locate.peripheral("proto/api.go", pats))

    def test_dotted_module_references_name_files(self):
        files = {"src/_pytest/config/findpaths.py": "class IniValue:\n    pass\n",
                 "src/_pytest/config/__init__.py": "", "src/_pytest/__init__.py": "",
                 "extra/vendored_pytest_8/src/_pytest/config/findpaths.py": "class IniValue:\n    pass\n"}
        repo = make_repo(self.tmp / "d", files)
        loc = locate.localize("Rename `_pytest.config.findpaths.IniValue` (see also www.example.com and 3.1.2)",
                              repo, sorted(files))
        self.assertEqual(loc["files_named"], ["src/_pytest/config/findpaths.py"])
        loc = locate.localize("`_pytest.config.Config` is created twice", repo, sorted(files))  # package + attribute
        self.assertEqual(loc["files_named"], ["src/_pytest/config/__init__.py"])
        loc = locate.localize("`app.run` and `os.path` are attribute access, not module paths", repo, sorted(files))
        self.assertEqual(loc["files_named"], [])
