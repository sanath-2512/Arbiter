import io
import json

from gheerefill.task import Task, TaskInputError, iter_tasks
from tests.helpers import TempDirCase


class TaskAdapterTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo dir ü"
        self.repo.mkdir()

    def read(self, text):
        return list(iter_tasks(io.StringIO(text), base_dir=self.tmp))

    def test_jsonl_with_blank_lines_and_malformed_line(self):
        good = json.dumps({"task_id": "a", "repo_path": str(self.repo), "issue": "x"})
        items = self.read(f"\n{good}\n{{not json\n\n{good.replace('\"a\"', '\"b\"')}\n")
        self.assertEqual([type(i).__name__ for i in items], ["Task", "TaskInputError", "Task"])
        self.assertIn(":3", items[1].location)
        self.assertEqual(items[2].task_id, "b")

    def test_pretty_printed_object_and_array_and_aliases(self):
        obj = {"instance_id": 7, "repo": "repo dir ü", "problem_statement": "fix it", "extra": 1}
        items = self.read(json.dumps(obj, indent=2))
        self.assertIsInstance(items[0], Task)
        self.assertEqual(items[0].task_id, "7")
        self.assertEqual(items[0].repo_path, self.repo.resolve())
        self.assertEqual(items[0].metadata, {"extra": 1})
        items = self.read(json.dumps([obj, {"id": "z"}]))
        self.assertIsInstance(items[0], Task)
        self.assertIsInstance(items[1], TaskInputError)
        self.assertIn("missing required", items[1].message)

    def test_useful_errors(self):
        cases = [
            ({"task_id": "a", "repo_path": "/nonexistent/x", "issue": "i"}, "does not exist"),
            ({"task_id": "a", "repo_path": str(self.repo), "issue": "  "}, "non-empty"),
            ({"task_id": "a", "repo_path": str(self.repo), "issue": "i", "limits": {"bogus": 1}}, "unknown limit"),
            ({"task_id": "a", "id": "b", "repo_path": str(self.repo), "issue": "i"}, "conflicting"),
            ([1, 2], "must be a JSON object"),
        ]
        for obj, needle in cases:
            (item,) = self.read(json.dumps(obj) if not isinstance(obj, list) else json.dumps(obj[0]))
            self.assertIsInstance(item, TaskInputError, obj)
            self.assertIn(needle, item.message)

    def test_empty_input_and_eof(self):
        self.assertEqual(self.read(""), [])
        self.assertEqual(self.read("\n\n"), [])

    def test_malformed_first_line_then_jsonl(self):
        good = json.dumps({"task_id": "a", "repo_path": str(self.repo), "issue": "x"})
        items = self.read("{broken\n" + good + "\n")
        self.assertEqual([type(i).__name__ for i in items], ["TaskInputError", "Task"])

    def test_truncated_document(self):
        (item,) = self.read('[{"task_id": "a",\n')
        self.assertIsInstance(item, TaskInputError)
        self.assertIn("malformed JSON", item.message)
