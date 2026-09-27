"""Request-view reduction: provider prompt-cache stability."""

import unittest
import json

from gheerefill.context import ContextManager, estimate_tokens


class ReasoningCacheStabilityTest(unittest.TestCase):
    def test_prefix_changes_once_per_step_not_every_request(self):
        cm = ContextManager(1_000_000, 8000, 0.8, 12)
        transcript = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]

        views = []

        for k in range(40):
            transcript.append(
                {
                    "role": "assistant",
                    "content": f"t{k}",
                    "reasoning": "r" * 3000,
                    "tool_calls": [
                        {
                            "id": f"c{k}",
                            "name": "bash",
                            "arguments": "{}",
                        }
                    ],
                }
            )

            transcript.append(
                {
                    "role": "tool",
                    "content": f"out{k}",
                    "tool_call_id": f"c{k}",
                }
            )

            views.append(cm.prepare(transcript))

        def shared(a, b):
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            return n

        # A request's prefix is the previous request's view unless
        # the stub cut-off stepped.
        breaks = [
            k
            for k in range(1, 40)
            if shared(views[k - 1], views[k]) < len(views[k - 1])
        ]

        self.assertLessEqual(len(breaks), 5, breaks)

        # A sliding cut-off would break it on ~36 of 40 requests.
        last = views[-1]

        full = [
            m
            for m in last
            if m.get("reasoning") == "r" * 3000
        ]

        self.assertGreaterEqual(
            len(full),
            ContextManager.REASONING_KEEP_TURNS,
        )

        self.assertLess(
            len(full),
            ContextManager.REASONING_KEEP_TURNS
            + ContextManager.REASONING_STEP,
        )


class ContextPressureTest(unittest.TestCase):
    def test_huge_old_tool_arguments_are_compacted_as_valid_json(self):
        cm = ContextManager(8_000, 1_000, 0.75, 4)

        transcript = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ]

        for n in range(8):
            transcript += [
                {
                    "role": "assistant",
                    "content": "reasoning " * 400,
                    "tool_calls": [
                        {
                            "id": f"c{n}",
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "path": f"generated/{n}.txt",
                                    "content": "x" * 12_000,
                                }
                            ),
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"c{n}",
                    "content": "result " * 1_000,
                    "output_id": f"o{n}",
                },
            ]

        view = cm.prepare(transcript)
        stats = cm.stats()

        self.assertLessEqual(
            cm.estimate(view),
            int(cm.limit * cm.reduce_at * 0.6),
        )

        self.assertGreater(stats["elided_tool_arguments"], 0)
        self.assertGreater(stats["elided_tool_outputs"], 0)

        for message in view:
            for call in message.get("tool_calls") or []:
                self.assertIsInstance(
                    json.loads(call["arguments"]),
                    dict,
                )
                self.assertIn("id", call)

        self.assertGreater(
            stats["peak_estimate_tokens"],
            estimate_tokens(view),
        )
