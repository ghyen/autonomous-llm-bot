"""Regression tests for bounded legacy-run recovery and fresh-run imports."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import TEST_USER_ID

import bot
import run_state
import trajectory
from ledger import ResearchLedger
from run_workspace import RunCatalog


CHANNEL_ID = 987654799


def _call(call_id, name, arguments, failed=False):
    return {
        "id": call_id,
        "name": name,
        "arguments": arguments,
        "failed": failed,
    }


class LegacyContextRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.catalog = RunCatalog(root / "workspace", root / "logs")
        self.workspace = self.catalog.acquire(TEST_USER_ID, CHANNEL_ID)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _legacy_state(self, next_step=1, summary="", trajectory_gap_step=None):
        return run_state.save(
            self.workspace,
            message_id=101,
            next_step=next_step,
            summary=summary,
            tail=[],
            ledger=ResearchLedger(),
            interrupt={},
            announced_call_ids=[],
            tool_fingerprints=[],
            trajectory_gap_step=trajectory_gap_step,
            task_contract=None,
        )

    def _seed_legacy_trajectory(self):
        trajectory.append_tool_group(
            self.workspace,
            1,
            [_call(
                "state-1",
                "record_state",
                {"updates": {
                    "goal": "trajectory에서 복구한 원래 목표",
                    "evidence": [{
                        "id": "E_ORIGINAL",
                        "summary": "원래 조사에서 확인한 사실",
                        "source": "log://legacy/1",
                    }],
                }},
            )],
            ["[record_state status: success]"],
            {"state-1"},
        )
        trajectory.append_tool_group(
            self.workspace,
            2,
            [_call(
                "probe-2",
                "bash_exec",
                {"command": "printf discovered-fact"},
            )],
            ["[stdout]\ndiscovered-fact\n[exit code: 0]"],
            {"probe-2"},
        )

    def test_legacy_recovery_rebuilds_goal_ledger_summary_and_next_step(self):
        self._seed_legacy_trajectory()
        self._legacy_state(next_step=1)

        recovered = bot.rebuild_legacy_resume_state(
            self.workspace, run_state.load(self.workspace)
        )

        self.assertEqual(
            recovered["task_contract"]["goal"],
            "trajectory에서 복구한 원래 목표",
        )
        self.assertEqual(recovered["ledger"].goal, "trajectory에서 복구한 원래 목표")
        self.assertIn("E_ORIGINAL", recovered["ledger"].state_markers())
        self.assertTrue(recovered["summary"])
        self.assertIn("discovered-fact", recovered["summary"])
        self.assertEqual(recovered["next_step"], 3)

        persisted = run_state.load(self.workspace)
        self.assertEqual(persisted["next_step"], 3)
        self.assertEqual(
            persisted["task_contract"]["goal"],
            "trajectory에서 복구한 원래 목표",
        )

    def test_recovery_preserves_guard_state(self):
        """재개 rebuild가 가드 상태를 기본값으로 덮어쓰면 안 된다.

        live run 실측: 재개한 첫 스텝에 record_stale이 stale=1213/1423으로
        오탐했고, 확정 실패 목록도 함께 사라졌다.
        """
        self._seed_legacy_trajectory()
        fingerprint = "a" * 64
        run_state.save(
            self.workspace,
            message_id=101,
            next_step=1,
            summary="",
            tail=[],
            ledger=ResearchLedger(),
            interrupt={},
            announced_call_ids=[],
            tool_fingerprints=[[fingerprint, 2]],
            trajectory_gap_step=None,
            task_contract=None,
            known_bad_calls={
                fingerprint: {
                    "tool": "read_file",
                    "error": "not_found",
                    "target": "/nope",
                    "first_step": 2,
                }
            },
            artifact_chain=2,
            last_record_step=2,
        )

        recovered = bot.rebuild_legacy_resume_state(
            self.workspace, run_state.load(self.workspace)
        )

        self.assertEqual(recovered["last_record_step"], 2)
        self.assertEqual(recovered["artifact_chain"], 2)
        self.assertIn(fingerprint, recovered["known_bad_calls"])

        persisted = run_state.load(self.workspace)
        self.assertEqual(persisted["last_record_step"], 2)
        self.assertEqual(persisted["artifact_chain"], 2)
        self.assertIn(fingerprint, persisted["known_bad_calls"])

    def test_no_op_recovery_keeps_guard_state(self):
        fingerprint = "b" * 64
        run_state.save(
            self.workspace,
            message_id=101,
            next_step=5,
            summary="이미 요약됨",
            tail=[],
            ledger=ResearchLedger(),
            interrupt={},
            announced_call_ids=[],
            tool_fingerprints=[],
            trajectory_gap_step=None,
            task_contract=None,
            known_bad_calls={
                fingerprint: {
                    "tool": "read_file",
                    "error": "not_found",
                    "target": "/nope",
                    "first_step": 1,
                }
            },
            artifact_chain=1,
            last_record_step=4,
        )

        recovered = bot.rebuild_legacy_resume_state(
            self.workspace, run_state.load(self.workspace), persist=False
        )

        self.assertEqual(recovered["last_record_step"], 4)
        self.assertEqual(recovered["artifact_chain"], 1)
        self.assertIn(fingerprint, recovered["known_bad_calls"])

    def test_fork_imports_only_a_bounded_brief(self):
        self._seed_legacy_trajectory()
        self._legacy_state(next_step=1)
        source_snapshot = run_state.snapshot_path(self.workspace).read_bytes()
        self.catalog.finish(self.workspace, "failed")

        with patch.object(bot, "RUN_CATALOG", self.catalog):
            imported = bot.fork_run(TEST_USER_ID, CHANNEL_ID, self.workspace.run_id)

        self.assertNotEqual(imported.run_id, self.workspace.run_id)
        imported_state = run_state.load(imported)
        self.assertEqual(imported_state["source_run_id"], self.workspace.run_id)
        self.assertEqual(imported_state["source_step"], 2)
        self.assertEqual(
            imported_state["task_contract"]["goal"],
            "trajectory에서 복구한 원래 목표",
        )
        self.assertIn("discovered-fact", imported_state["summary"])
        self.assertIn("E_ORIGINAL", imported_state["ledger"].state_markers())
        self.assertFalse((imported.root / trajectory.FILE_NAME).exists())
        self.assertEqual(
            run_state.snapshot_path(self.workspace).read_bytes(), source_snapshot
        )

    def test_gap_excludes_untrusted_trajectory_records_from_recovery(self):
        self._seed_legacy_trajectory()
        self._legacy_state(next_step=1, trajectory_gap_step=2)

        recovered = bot.rebuild_legacy_resume_state(
            self.workspace, run_state.load(self.workspace)
        )

        self.assertEqual(recovered["next_step"], 2)
        self.assertIn("E_ORIGINAL", recovered["ledger"].state_markers())
        self.assertNotIn("discovered-fact", recovered["summary"])
        self.assertFalse(
            any(marker.startswith("TRAJ_") for marker in recovered["ledger"].state_markers())
        )


class NoOpRolloverTelemetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_rollover_flag_is_false_when_context_is_unchanged(self):
        workspace = SimpleNamespace(root=tempfile.mkdtemp())
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "resume"},
        ]
        async def no_op_rollover(_workspace, current, current_summary, *args, **kwargs):
            return current, current_summary

        with patch.object(
            bot,
            "count_agent_input_tokens",
            AsyncMock(side_effect=[100000, 0]),
        ), patch.object(
            bot,
            "rollover_agent_context",
            AsyncMock(side_effect=no_op_rollover),
        ):
            prepared = await bot.prepare_agent_request_payload(
                workspace,
                messages,
                "summary",
                10,
                1024,
                {"tools": []},
                resume_context=False,
            )

        self.assertFalse(prepared.rollover_used)


if __name__ == "__main__":
    unittest.main()
