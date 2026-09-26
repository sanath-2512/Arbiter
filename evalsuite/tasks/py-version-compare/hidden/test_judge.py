import builtins
import importlib
import sys
import unittest


class Judge(unittest.TestCase):
    def test_imports_without_distutils(self):
        real_import = builtins.__import__

        def guarded(name, *a, **k):
            if name == "distutils" or name.startswith("distutils.") or name.startswith("packaging"):
                raise ModuleNotFoundError(name)
            return real_import(name, *a, **k)

        for m in [m for m in sys.modules if m.startswith("pkgver")]:
            del sys.modules[m]
        builtins.__import__ = guarded
        try:
            importlib.import_module("pkgver")
        finally:
            builtins.__import__ = real_import

    def test_ordering(self):
        from pkgver import compare_versions

        chain = ["1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.1", "1.9", "1.10", "2.0"]
        for i, a in enumerate(chain):
            for j, b in enumerate(chain):
                self.assertEqual(compare_versions(a, b), (i > j) - (i < j), (a, b))

    def test_trailing_zeros(self):
        from pkgver import compare_versions, is_newer

        self.assertEqual(compare_versions("1.0", "1.0.0"), 0)
        self.assertFalse(is_newer("1.0.0", "1"))
        self.assertTrue(is_newer("1.0.1", "1"))
