"""Deterministic-failure avoidance list tests."""

import json
import unittest
from types import SimpleNamespace

from test_support import FakeMessage  # sets required config env before bot imports

import bot
import run_state
from ledger import ResearchLedger


NOT_FOUND = json.dumps({
    "error": "not_found",
    "path": "findings.md",
    "status": "error",
})


def _args(**kwargs):
    return dict(kwargs)


class DeterministicFailureTest(unittest.TestCase):
    def test_not_found_is_deterministic(self):
        self.assertEqual(
            bot._deterministic_tool_failure("read_file", NOT_FOUND),
            ("not_found", "findings.md"),
        )

    def test_transient_failure_is_not_deterministic(self):
        self.assertIsNone(
            bot._deterministic_tool_failure(
                "bash_exec", "[stdout] x\n[exit code: 1]"
            )
        )
        self.assertIsNone(
            bot._deterministic_tool_failure("read_file", "plain text")
        )

    def test_record_and_block(self):
        known = {}
        bot._record_known_bad(
            known, "read_file", _args(path="findings.md"),
            "not_found", "findings.md", 12,
        )
        blocked = json.loads(
            bot._known_bad_block(known, "read_file", _args(path="findings.md"))
        )
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["reason"], "known_failure")
        self.assertEqual(blocked["first_step"], 12)

    def test_other_arguments_not_blocked(self):
        known = {}
        bot._record_known_bad(
            known, "read_file", _args(path="findings.md"),
            "not_found", "findings.md", 12,
        )
        self.assertIsNone(
            bot._known_bad_block(known, "read_file", _args(path="plan.md"))
        )

    def test_record_is_bounded(self):
        known = {}
        for index in range(bot.KNOWN_BAD_CALLS_MAX + 5):
            bot._record_known_bad(
                known, "read_file", _args(path=f"f{index}.md"),
                "not_found", f"f{index}.md", index + 1,
            )
        self.assertEqual(len(known), bot.KNOWN_BAD_CALLS_MAX)
        self.assertIsNone(
            bot._known_bad_block(known, "read_file", _args(path="f0.md"))
        )

    def test_write_success_invalidates_read_entry(self):
        known = {}
        bot._record_known_bad(
            known, "read_file", _args(path="findings.md"),
            "not_found", "findings.md", 12,
        )
        bot._invalidate_known_bad(
            known, "write_file", _args(path="findings.md")
        )
        self.assertEqual(known, {})

    def test_render_block_bounded_and_empty(self):
        self.assertEqual(bot._render_avoidance_block({}), "")
        known = {}
        for index in range(10):
            bot._record_known_bad(
                known, "read_file", _args(path=f"f{index}.md"),
                "not_found", f"f{index}.md", index + 1,
            )
        block = bot._render_avoidance_block(known)
        self.assertIn("f0.md", block)
        self.assertLessEqual(len(block), bot.AVOIDANCE_BLOCK_MAX_CHARS + 1)

    def test_system_prompt_includes_avoidance(self):
        known = {}
        bot._record_known_bad(
            known, "read_file", _args(path="findings.md"),
            "not_found", "findings.md", 12,
        )
        workspace = SimpleNamespace(root="/tmp", known_bad_calls=known)
        content = bot.build_system_content(workspace, ResearchLedger(), "")
        self.assertIn("findings.md", content)
        self.assertIn("재시도 금지", content)


class KnownBadStateRoundtripTest(unittest.TestCase):
    def _workspace(self, root):
        workspace = SimpleNamespace(
            root=str(root), run_id="r1", owner_id=1, channel_id=2
        )
        return workspace

    def test_save_load_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            known = {}
            bot._record_known_bad(
                known, "read_file", _args(path="findings.md"),
                "not_found", "findings.md", 12,
            )
            run_state.save(
                workspace,
                message_id=7,
                next_step=3,
                summary="",
                tail=[],
                ledger=ResearchLedger(),
                interrupt={},
                announced_call_ids=[],
                tool_fingerprints=[],
                trajectory_gap_step=None,
                known_bad_calls=known,
            )
            restored = run_state.load(workspace)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["known_bad_calls"], known)

    def test_missing_key_loads_as_empty(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._workspace(Path(tmp))
            run_state.save(
                workspace,
                message_id=7,
                next_step=3,
                summary="",
                tail=[],
                ledger=ResearchLedger(),
                interrupt={},
                announced_call_ids=[],
                tool_fingerprints=[],
                trajectory_gap_step=None,
            )
            payload = json.loads(
                (Path(tmp) / run_state.FILE_NAME).read_text(encoding="utf-8")
            )
            del payload["known_bad_calls"]
            (Path(tmp) / run_state.FILE_NAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            restored = run_state.load(workspace)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["known_bad_calls"], {})


if __name__ == "__main__":
    unittest.main()
