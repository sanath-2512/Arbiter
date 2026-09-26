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
