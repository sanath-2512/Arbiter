import json
import os
from unittest import mock

from arbiter.records import Redactor, append_jsonl, atomic_write_json, read_jsonl, safe_name
from tests.helpers import TempDirCase


class RecordsTest(TempDirCase):
    def test_atomic_write_survives_interrupted_publication(self):
        p = self.tmp / "state.json"
        atomic_write_json(p, {"v": 1})
        with mock.patch("arbiter.records.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                atomic_write_json(p, {"v": 2})
        self.assertEqual(json.loads(p.read_text()), {"v": 1})
        self.assertEqual([x for x in os.listdir(self.tmp) if x.endswith(".tmp")], [])

    def test_jsonl_ignores_torn_final_line_only(self):
        p = self.tmp / "t.jsonl"
        append_jsonl(p, {"a": 1})
        with open(p, "a") as fh:
            fh.write('{"a": 2')  # torn append
        self.assertEqual(read_jsonl(p), [{"a": 1}])
        p.write_text('{"a": 1}\n{bad\n{"a": 3}\n')
        with self.assertRaises(json.JSONDecodeError):
            read_jsonl(p)

    def test_redactor_nested(self):
        r = Redactor(["sk-secret-123456", "", "short"])
        self.assertEqual(r.obj({"x": ["key=sk-secret-123456"], "y": 3}), {"x": ["key=[REDACTED]"], "y": 3})
        self.assertEqual(r.text("short stays"), "short stays")  # too short to redact safely

    def test_safe_name(self):
        self.assertEqual(safe_name("django__django-1234"), "django__django-1234")
        self.assertEqual(safe_name("../../etc/passwd"), "etc_passwd")
        self.assertNotIn("/", safe_name("a/b c"))
