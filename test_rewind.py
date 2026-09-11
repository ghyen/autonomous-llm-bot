"""Run rewind tests."""

import json
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
import trajectory
from ledger import ResearchLedger


CHANNEL_ID = 987654820


def _call(call_id, name, arguments, artifact_path=None):
    call = {"id": call_id, "name": name, "arguments": arguments}
    if artifact_path is not None:
        call["artifact_path"] = artifact_path
    return call


class RewindTest(unittest.IsolatedAsyncioTestCase):
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

    def _seed_run(self):
        workspace = bot.RUN_CATALOG.prepare(TEST_USER_ID, CHANNEL_ID)
        artifact_name = "artifacts/out_." + "a" * 64 + ".log"
        (Path(workspace.root) / "artifacts").mkdir(parents=True, exist_ok=True)
        (Path(workspace.root) / artifact_name).write_text("old", encoding="utf-8")
        trajectory.append_tool_group(
            workspace, 1,
            [_call("c1", "bash_exec", {"command": "ls"})],
            ["[stdout]\nok\n[exit code: 0]"], ["c1"],
        )
        trajectory.append_tool_group(
            workspace, 2,
            [_call("c2", "read_file", {"path": "missing.md"})],
            [json.dumps({"error": "not_found", "path": "missing.md", "status": "error"})],
            ["c2"],
        )
        trajectory.append_tool_group(
            workspace, 3,
            [_call("c3", "bash_exec", {"command": "cat x"}, artifact_name)],
            ["[stdout]\nbig"],
            ["c3"],
        )
        ledger = ResearchLedger()
        ledger.set_goal("되돌리기 목표")
        known = {}
        bot._record_known_bad(
            known, "read_file", {"path": "missing.md"},
            "not_found", "missing.md", 2,
        )
        bot._record_known_bad(
            known, "read_file", {"path": "later.md"},
            "not_found", "later.md", 3,
        )
        fp_old = bot._tool_fingerprint("bash_exec", {"command": "ls"})
        fp_new = bot._tool_fingerprint("bash_exec", {"command": "cat x"})
        run_state.save(
            workspace,
            message_id=7,
            next_step=4,
            summary=bot.format_tiered_summary(
                tier3="옛 절차",
                tier3_through=3,
                tier2_lines=["[Step 1: ls -> completed]", "[Step 3: cat -> completed]"],
                discoveries=["- 발견 1"],
            ),
            tail=[{"role": "user", "content": "old"}],
            ledger=ledger,
            interrupt={},
            announced_call_ids=["c1", "c2", "c3"],
            tool_fingerprints=[[fp_old, 1], [fp_new, 3]],
            trajectory_gap_step=3,
            state="stopped",
            artifact_manifest={
                "version": 1,
                "items": [
                    {"path": artifact_name, "kind": "tool_output", "step": 3},
                ],
            },
            known_bad_calls=known,
            artifact_chain=2,
        )
        return workspace

    def test_rewind_truncates_and_rebuilds(self):
        workspace = self._seed_run()

        new_workspace, stats = bot.rewind_run(
            TEST_USER_ID, CHANNEL_ID, workspace.run_id, 2
        )

        self.assertEqual(stats["next_step"], 3)
        self.assertEqual(stats["dropped_records"], 1)
        self.assertEqual(stats["artifacts_deleted"], 1)
        records, complete = trajectory.read_records(workspace)
        self.assertTrue(complete)
        self.assertEqual([rec["step"] for rec in records], [1, 2])
        self.assertTrue((Path(workspace.root) / "traj.jsonl.bak").is_file())
        self.assertFalse(
            (Path(workspace.root) / ("artifacts/out_." + "a" * 64 + ".log")).exists()
        )
        restored = run_state.load(workspace)
        self.assertEqual(restored["next_step"], 3)
        self.assertEqual(restored["tail"], [])
        self.assertEqual(restored["artifact_chain"], 0)
        self.assertIsNone(restored["trajectory_gap_step"])
        self.assertEqual(restored["ledger"].goal, "되돌리기 목표")
        self.assertEqual(
            [fp for fp, _ in restored["tool_fingerprints"]],
            [bot._tool_fingerprint("bash_exec", {"command": "ls"})],
        )
        self.assertEqual(len(restored["known_bad_calls"]), 1)
        self.assertEqual(
            restored["artifact_manifest"], {"version": 1, "items": []}
        )
        summary = restored["summary"]
        self.assertNotIn("Step 3", summary)
        self.assertIn("Step 1", summary)
        self.assertIn("- 발견 1", summary)

    def test_rewind_refusals(self):
        workspace = self._seed_run()
        with self.assertRaises(ValueError):
            bot.rewind_run(TEST_USER_ID, CHANNEL_ID, workspace.run_id, 0)
        with self.assertRaises(ValueError):
            bot.rewind_run(TEST_USER_ID, CHANNEL_ID, workspace.run_id, 4)
        with self.assertRaises(ValueError):
            bot.rewind_run(TEST_USER_ID, CHANNEL_ID, workspace.run_id, 99)
        with self.assertRaises(bot.RunNotFoundError):
            bot.rewind_run(TEST_USER_ID, CHANNEL_ID, "no" * 16, 1)
        workspace.status = "active"
        with self.assertRaises(bot.RunActiveError):
            bot.rewind_run(TEST_USER_ID, CHANNEL_ID, workspace.run_id, 2)

    def test_parse_rewind_args(self):
        run_id, step = bot._parse_rewind_args(["!rewind", "abc", "12"])
        self.assertEqual((run_id, step), ("abc", 12))
        run_id, step = bot._parse_rewind_args(["!rewind", "abc", "nope"])
        self.assertEqual(step, 0)
        run_id, step = bot._parse_rewind_args(["!rewind"])
        self.assertEqual((run_id, step), ("", 0))


if __name__ == "__main__":
    unittest.main()
