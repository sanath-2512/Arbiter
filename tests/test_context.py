"""Request-view reduction: provider prompt-cache stability."""

import unittest
import json

from arbiter.context import ContextManager, estimate_tokens


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

        self.assertLessEqual(len(breaks), 40 // 4, breaks)  # once per REASONING_STEP (4) turns

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

        # the request fits the window; the newest call and its result stay readable
        self.assertLessEqual(cm.estimate(view), cm.limit)
        self.assertIn("result", view[-1]["content"])

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


class ObservationWindowTest(unittest.TestCase):
    """Only the newest tool outputs are shown verbatim, advanced in steps; totals grow linearly."""

    def run_session(self, window, steps=40, out_chars=6000):
        from arbiter.context import ContextManager
        cm = ContextManager(1_000_000, 8000, 0.8, 12, observation_window=window, window_step=4)
        transcript = [{"role": "system", "content": "s" * 2000}, {"role": "user", "content": "u" * 4000}]
        total, views = 0, []
        for k in range(steps):
            transcript.append({"role": "assistant", "content": f"step {k}", "tool_calls": [
                {"id": f"c{k}", "name": "write_file", "arguments": json.dumps({"path": f"f{k}.py",
                                                                                 "content": "x" * 3000})}]})
            transcript.append({"role": "tool", "content": f"out{k} " + "y" * out_chars, "tool_call_id": f"c{k}",
                               "output_id": f"o{k}"})
            view = cm.prepare(transcript)
            views.append(view)
            total += cm.estimate(view)
        return total, views, cm

    def test_linear_not_quadratic(self):
        full, _, _ = self.run_session(window=0)
        windowed, views, cm = self.run_session(window=8)
        self.assertLess(windowed, full / 2, (windowed, full))  # 40 steps of 6k-char outputs: 2.3x fewer
        last = views[-1]
        verbatim = [m for m in last if m["role"] == "tool" and not m["content"].startswith("[earlier")]
        self.assertTrue(8 <= len(verbatim) < 8 + 4, len(verbatim))
        self.assertTrue(last[-1]["content"].startswith("out39 "))  # the newest output is whole
        old = next(m for m in last if m["role"] == "assistant" and m.get("tool_calls"))
        args = json.loads(old["tool_calls"][0]["arguments"])
        self.assertEqual(args["path"], "f0.py")  # keys kept, the 3000-char content reduced to its size
        self.assertEqual(args["content"], "[3000 chars elided]")
        self.assertIn('read_output(id="o0")', last[3]["content"])
        self.assertGreater(cm.stats()["outside_observation_window"], 0)

    def test_prefix_changes_once_per_step(self):
        _, views, _ = self.run_session(window=8)

        def shared(a, b):
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            return n
        breaks = [k for k in range(1, len(views)) if shared(views[k - 1], views[k]) < len(views[k - 1])]
        self.assertLessEqual(len(breaks), 40 // 4 + 1, breaks)
