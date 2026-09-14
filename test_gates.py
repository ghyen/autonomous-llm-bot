"""Record-stale and think-required gate unit tests."""

import unittest
from types import SimpleNamespace

from test_support import FakeMessage  # sets required config env before bot imports

import bot
import run_state
from ledger import ResearchLedger


class RecordStaleBlockTest(unittest.TestCase):
    def test_record_state_exempt(self):
        self.assertIsNone(bot._record_stale_block(0, 100, "record_state", {}))

    def test_force_exempt(self):
        self.assertIsNone(
            bot._record_stale_block(0, 100, "bash_exec", {"force": True})
        )

    def test_boundary(self):
        self.assertIsNone(bot._record_stale_block(0, 24, "bash_exec", {}))
        blocked = bot._record_stale_block(0, 25, "bash_exec", {})
        self.assertIn("record_stale", blocked)
        self.assertIn("25", blocked)

    def test_recent_record_passes(self):
        self.assertIsNone(bot._record_stale_block(90, 100, "bash_exec", {}))


class ThinkRequiredBlockTest(unittest.TestCase):
    def test_no_debt_passes(self):
        self.assertIsNone(bot._think_required_block(False, "bash_exec", {}))

    def test_think_exempt(self):
        self.assertIsNone(
            bot._think_required_block(True, "think", {"focus": "x"})
        )

    def test_force_exempt(self):
        self.assertIsNone(
            bot._think_required_block(True, "bash_exec", {"force": True})
        )

    def test_debt_blocks(self):
        blocked = bot._think_required_block(True, "bash_exec", {"command": "ls"})
        self.assertIn("think_required", blocked)


class SubstantiveRevisionTest(unittest.TestCase):
    def test_empty_update_does_not_bump_revision(self):
        ledger = ResearchLedger()
        before = ledger.revision
        ledger.apply_updates({})
        self.assertEqual(ledger.revision, before)

    def test_evidence_bumps_revision(self):
        ledger = ResearchLedger()
        before = ledger.revision
        ledger.apply_updates({"evidence": [
            {"id": "E1", "summary": "s", "source": "t"},
        ]})
        self.assertGreater(ledger.revision, before)


class StaleWarningRenderTest(unittest.TestCase):
    def test_warning_at_threshold(self):
        workspace = SimpleNamespace(
            root="/tmp", last_record_step=0, current_tool_step=15
        )
        content = bot.build_system_content(workspace, ResearchLedger(), "")
        self.assertIn("상태 기록 알림", content)

    def test_no_warning_when_fresh(self):
        workspace = SimpleNamespace(
            root="/tmp", last_record_step=0, current_tool_step=14
        )
        content = bot.build_system_content(workspace, ResearchLedger(), "")
        self.assertNotIn("상태 기록 알림", content)

    def test_no_attrs_no_warning(self):
        workspace = SimpleNamespace(root="/tmp")
        content = bot.build_system_content(workspace, ResearchLedger(), "")
        self.assertNotIn("상태 기록 알림", content)


class LastRecordStepStateTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(run_state.normalize_last_record_step(7), 7)
        self.assertEqual(run_state.normalize_last_record_step(None), 0)
        self.assertEqual(run_state.normalize_last_record_step(-1), 0)
        self.assertEqual(run_state.normalize_last_record_step(True), 0)

    def test_missing_key_loads_as_zero(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(
                root=str(Path(tmp)), run_id="r1", owner_id=1, channel_id=2
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
            )
            payload = json.loads(
                (Path(tmp) / run_state.FILE_NAME).read_text(encoding="utf-8")
            )
            del payload["last_record_step"]
            (Path(tmp) / run_state.FILE_NAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            restored = run_state.load(workspace)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["last_record_step"], 0)


if __name__ == "__main__":
    unittest.main()
