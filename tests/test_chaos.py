"""A few fault-injection seeds on every `make test` (the full run is `make chaos`)."""

import subprocess
import sys

from tests.helpers import CALC, ROOT, TempDirCase, make_repo, run_agent, tc, turn


class ChaosSmokeTest(TempDirCase):
    def test_invariants_hold_for_sample_seeds(self):
        # seeds 0-7 cover crashes, cancellations, provider errors, multiple attempts and one SIGKILL + recovery
        p = subprocess.run([sys.executable, "scripts/chaos.py", "--seeds", "8", "--kill-every", "8"], cwd=ROOT,
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-3000:])
        self.assertIn("0 with invariant violations", p.stdout)

    def test_unexpected_client_exception_is_still_counted(self):
        """Regression (found by chaos): a non-ModelError raised inside the model client escaped the
        retry loop uncounted although the request may have been billed."""
        repo = make_repo(self.tmp / "repo", CALC)

        def boom(messages):
            raise KeyError("unexpected response shape")

        result, agent = run_agent(repo, [turn(tc("bash", command="true")), boom], self.tmp / "run")
        self.assertEqual(result["termination"], "crash")
        self.assertEqual(result["usage"]["requests"], 2)
        self.assertEqual(result["usage"]["requests_usage_unknown"], 1)
