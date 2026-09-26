"""Request-view reduction: provider prompt-cache stability."""

import unittest

from gheerefill.context import ContextManager


class ReasoningCacheStabilityTest(unittest.TestCase):
    def test_prefix_changes_once_per_step_not_every_request(self):
        cm = ContextManager(1_000_000, 8000, 0.8, 12)
        transcript = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        views = []
        for k in range(40):
            transcript.append({"role": "assistant", "content": f"t{k}", "reasoning": "r" * 3000,
                               "tool_calls": [{"id": f"c{k}", "name": "bash", "arguments": "{}"}]})
            transcript.append({"role": "tool", "content": f"out{k}", "tool_call_id": f"c{k}"})
            views.append(cm.prepare(transcript))

        def shared(a, b):
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            return n
        # a request's prefix is the previous request's view unless the stub cut-off stepped
        breaks = [k for k in range(1, 40) if shared(views[k - 1], views[k]) < len(views[k - 1])]
        self.assertLessEqual(len(breaks), 5, breaks)  # a sliding cut-off breaks it on ~36 of 40 requests
        last = views[-1]
        full = [m for m in last if m.get("reasoning") == "r" * 3000]
        self.assertGreaterEqual(len(full), ContextManager.REASONING_KEEP_TURNS)
        self.assertLess(len(full), ContextManager.REASONING_KEEP_TURNS + ContextManager.REASONING_STEP)
