import copy
import unittest

import confmerge
from confmerge import deep_merge, load_layers


class Judge(unittest.TestCase):
    def test_nested_keys_survive(self):
        cfg = load_layers(['{"db": {"port": 6543}}'])
        self.assertEqual(cfg["db"], {"host": "localhost", "port": 6543})

    def test_defaults_not_mutated(self):
        before = copy.deepcopy(confmerge.DEFAULTS)
        load_layers(['{"db": {"port": 1}, "features": ["b"]}'])
        load_layers(['{"debug": true}'])
        self.assertEqual(confmerge.DEFAULTS, before)

    def test_deep_merge_pure_and_lists_replace(self):
        base = {"a": {"b": {"c": 1, "d": 2}}, "l": [1, 2]}
        over = {"a": {"b": {"d": 3}}, "l": [3]}
        base_copy, over_copy = copy.deepcopy(base), copy.deepcopy(over)
        self.assertEqual(deep_merge(base, over), {"a": {"b": {"c": 1, "d": 3}}, "l": [3]})
        self.assertEqual((base, over), (base_copy, over_copy))

    def test_layer_order(self):
        cfg = load_layers(['{"db": {"host": "h1"}}', '{"db": {"host": "h2", "port": 7}}'])
        self.assertEqual(cfg["db"], {"host": "h2", "port": 7})
