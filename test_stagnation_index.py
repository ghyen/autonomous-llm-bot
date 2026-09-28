"""Stagnation-index tests.

The index is reported, never enforced: no single cheap signal separates a
circling run from a healthy long run (measured across four production runs),
so these tests pin the arithmetic, not a judgment.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage  # sets required config env before bot imports

import bot
import stagnation
import trajectory


def _workspace(root):
    run = Path(root) / "run"
    run.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root=str(run))


def _append(workspace, step, command, result, failed=False):
    call_id = f"c{step}-{abs(hash(command)) % 10_000_000}"
    trajectory.append_tool_group(
        workspace,
        step,
        [{
            "id": call_id,
            "name": "bash_exec",
            "arguments": {"command": command},
            "failed": failed,
        }],
        [result],
        {call_id},
    )


def _dead(url):
    return f"[stdout]\n404 page for {url}\n[exit code: 0]"


class ComputeIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = _workspace(self._tmp.name)
        # steps 1..3: dead-end repeats of one target (history before window)
        for step in (1, 2, 3):
            _append(
                self.workspace, step,
                f"curl https://x.test/dead",
                _dead("https://x.test/dead"),
            )
        # steps 4..6: window content — one repeat, one fresh target, one live
        _append(self.workspace, 4, "curl https://x.test/dead", _dead("x"))
        _append(
            self.workspace, 5, "curl https://x.test/fresh",
            "[stdout]\n{\"data\": []}\n[exit code: 0]",
        )
        _append(
            self.workspace, 6, "curl https://x.test/dead",
            _dead("x"),
        )

    def _records(self):
        records, _complete = trajectory.read_records(self.workspace)
        return records

    def test_window_counts(self):
        index = stagnation.compute_stagnation_index(
            self._records(), 6, window_steps=3
        )
        self.assertEqual(index["window_steps"], 3)
        self.assertEqual(index["end_step"], 6)
        # window steps 4..6 hold 3 observations
        self.assertEqual(index["observations"], 3)
        # only the fresh target is novel; the dead target was seen before
        self.assertEqual(index["novel_pairs"], 1)
        # reobserved: 2 of 3
        self.assertEqual(index["reobserve_pct"], 67)

    def test_dead_repeat_over_counts_only_the_excess(self):
        # limit is shared with the guard allowance (3): the 4th..6th hits count.
        for step in (7, 8, 9, 10):
            _append(
                self.workspace, step,
                f"curl https://x.test/dead",
                _dead("x"),
            )
        index = stagnation.compute_stagnation_index(
            self._records(), 10, window_steps=10
        )
        # dead target seen at steps 1,2,3,4,6,7,8,9,10 = 9 times -> 9 - 3 = 6 over
        self.assertEqual(index["dead_repeat_over"], 6)

    def test_top_repeats_are_ranked_and_bounded(self):
        for step in (7, 8):
            _append(
                self.workspace, step,
                "curl https://x.test/fresh",
                "[stdout]\nok\n[exit code: 0]",
            )
        index = stagnation.compute_stagnation_index(
            self._records(), 8, window_steps=8
        )
        top = index["top_repeats"]
        self.assertLessEqual(len(top), 5)
        self.assertEqual(top[0]["target"], "https://x.test/dead")
        counts = [item["count"] for item in top]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_empty_history_is_all_zeros(self):
        index = stagnation.compute_stagnation_index([], 100)
        self.assertEqual(index["observations"], 0)
        self.assertEqual(index["novel_pairs"], 0)
        self.assertEqual(index["dead_repeat_over"], 0)
        self.assertEqual(index["reobserve_pct"], 0)
        self.assertEqual(index["top_repeats"], [])


class FormatLinesTest(unittest.TestCase):
    def test_four_lines_max_with_top_repeats(self):
        index = {
            "window_steps": 100,
            "end_step": 1100,
            "observations": 90,
            "novel_pairs": 12,
            "dead_repeat_over": 18,
            "reobserve_pct": 64,
            "top_repeats": [
                {"target": "https://x.test/" + ("p" * 100), "outcome": "http404", "count": 20},
                {"target": "https://x.test/feed", "outcome": "exit0", "count": 9},
                {"target": "https://x.test/a", "outcome": "exit0", "count": 4},
                {"target": "https://x.test/b", "outcome": "exit0", "count": 2},
            ],
        }
        lines = stagnation.format_stagnation_lines(index)
        self.assertLessEqual(len(lines), 4)
        self.assertIn("12", lines[0])
        self.assertIn("18", lines[0])
        self.assertTrue(all(len(line) < 200 for line in lines))


class EmitHelperTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = _workspace(self._tmp.name)
        _append(
            self.workspace, 1, "curl https://x.test/dead", _dead("x"),
        )

    def test_logs_event_and_returns_lines(self):
        events = []

        def record(_workspace, kind, **kwargs):
            events.append((kind, kwargs))

        with patch.object(bot, "log_session_event", record):
            lines = bot._emit_stagnation_index(self.workspace, 1)
        kinds = [kind for kind, _ in events]
        self.assertIn("stagnation_index", kinds)
        payload = dict(events)["stagnation_index"]
        self.assertEqual(payload["step"], 1)
        self.assertIn("novel_pairs", payload)
        self.assertTrue(lines and lines[0].startswith("> "))

    def test_broken_workspace_fails_open(self):
        broken = SimpleNamespace(root="/nonexistent-path-xyz")
        with patch.object(
            bot, "log_session_event", side_effect=AssertionError("must not log")
        ):
            self.assertEqual(bot._emit_stagnation_index(broken, 5), [])


if __name__ == "__main__":
    unittest.main()
