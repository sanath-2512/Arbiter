import unittest

from cachekit import LRUCache


class CacheTest(unittest.TestCase):
    def test_eviction(self):
        c = LRUCache(2)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")
        c.set("c", 3)
        self.assertIn("a", c)
        self.assertNotIn("b", c)
        self.assertEqual(len(c), 2)


if __name__ == "__main__":
    unittest.main()
