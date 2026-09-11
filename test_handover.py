"""Run handover tests."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from test_support import (
    FakeMessage,
    TEST_USER_ID,
    run_catalog_patch,
)

import bot
import run_state
from ledger import ResearchLedger


CHANNEL_ID = 987654810


class HandoverTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self._stack = ExitStack()
        self._stack.enter_context(
            run_catalog_patch(bot, self._scratch.name, patch_context_counter=False)
        )
        self.addCleanup(self._stack.close)
        self.addCleanup(self._scratch.cleanup)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    def tearDown(self):
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    def _save_old_record(self, workspace, goal="묵은 목표"):
        ledger = ResearchLedger()
        ledger.set_goal(goal)
        artifacts = Path(workspace.root) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "out_small.log").write_text("x" * 100, encoding="utf-8")
        (artifacts / "out_big.log").write_bytes(b"y" * (bot.HANDOVER_ARTIFACT_MAX_BYTES + 1))
        known = {}
        bot._record_known_bad(
            known, "read_file", {"path": "findings.md"},
            "not_found", "findings.md", 12,
        )
        return run_state.save(
            workspace,
            message_id=7,
            next_step=50,
            summary=bot.format_tiered_summary(
                tier3="절차 " * 100, tier2_lines=["Step 49: 핵심"],
                discoveries=["- 발견 1"],
            ),
            tail=[],
            ledger=ledger,
            interrupt={},
            announced_call_ids=[],
            tool_fingerprints=[],
            trajectory_gap_step=None,
            known_bad_calls=known,
        )

    def test_handover_carries_judgment_avoidance_and_artifacts(self):
        old = bot.RUN_CATALOG.prepare(TEST_USER_ID, CHANNEL_ID)
        self._save_old_record(old)

        new_workspace, note = bot.handover_run(TEST_USER_ID, CHANNEL_ID, old.run_id)

        self.assertNotEqual(new_workspace.run_id, old.run_id)
        self.assertIn("묵은 목표", bot.channel_ledger[CHANNEL_ID].render())
        self.assertIn("묵은 목표", bot.channel_summary[CHANNEL_ID])
        self.assertIn("findings.md", bot.channel_summary[CHANNEL_ID])
        self.assertLessEqual(len(note), bot.HANDOVER_NOTE_MAX_CHARS + 1)
        known = new_workspace.known_bad_calls
        self.assertEqual(len(known), 1)
        entry = next(iter(known.values()))
        self.assertEqual(entry["target"], "findings.md")
        copied = sorted(
            child.name
            for child in (Path(new_workspace.root) / "handover").iterdir()
        )
        self.assertEqual(copied, ["out_small.log"])
        # 이전 레코드는 남는다.
        self.assertIsNotNone(run_state.load(old))

    def test_handover_refuses_active_run(self):
        old = bot.RUN_CATALOG.prepare(TEST_USER_ID, CHANNEL_ID)
        old.status = "active"
        with self.assertRaises(bot.RunActiveError):
            bot.handover_run(TEST_USER_ID, CHANNEL_ID, old.run_id)

    def test_handover_refuses_missing_record(self):
        old = bot.RUN_CATALOG.prepare(TEST_USER_ID, CHANNEL_ID)
        with self.assertRaises(bot.RunNotFoundError):
            bot.handover_run(TEST_USER_ID, CHANNEL_ID, old.run_id)

    def test_note_tolerates_empty_record(self):
        note = bot._build_handover_note({}, "deadbeef", 0)
        self.assertIn("deadbeef", note)


if __name__ == "__main__":
    unittest.main()
