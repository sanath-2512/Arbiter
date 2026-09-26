import json

from gheerefill.prompts import repo_overview, test_command_hints
from tests.helpers import TempDirCase, make_repo


class PromptsTest(TempDirCase):
    def test_hints_from_manifests(self):
        repo = make_repo(self.tmp / "r", {
            "package.json": json.dumps({"scripts": {"test": "node --test test/*.test.js"}}),
            "Makefile": "build:\n\techo\ntest: build\n\techo t\n",
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
            "go.mod": "module x\n",
        }, init_git=False)
        hints = test_command_hints(repo)
        self.assertEqual(hints[0], "npm test  (runs: node --test test/*.test.js)")
        self.assertIn("make test", hints)
        self.assertIn("pytest", hints)
        self.assertIn("go test ./...", hints)
        self.assertIn("unverified", repo_overview(repo))

    def test_no_hints_and_placeholder_npm_script(self):
        repo = make_repo(self.tmp / "r", {"package.json": json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}),
                                          "README.md": "x"}, init_git=False)
        self.assertEqual(test_command_hints(repo), [])
        self.assertNotIn("Test commands", repo_overview(repo))
