import unittest

from confmerge import load_layers


class LoaderTest(unittest.TestCase):
    def test_top_level_override(self):
        self.assertEqual(load_layers(['{"debug": true}'])["debug"], True)


if __name__ == "__main__":
    unittest.main()
