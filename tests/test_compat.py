import shutil
import subprocess
import unittest

from tests.helpers import ROOT


class MinimumPythonTest(unittest.TestCase):
    """The runtime and tests must stay valid on the minimum supported Python (3.11)."""

    def test_compiles_on_python311(self):
        py = shutil.which("python3.11")
        if py is None:
            raise unittest.SkipTest("python3.11 not installed")
        p = subprocess.run([py, "-m", "compileall", "-q", "gheerefill", "tests", "scripts", "baselines"],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
