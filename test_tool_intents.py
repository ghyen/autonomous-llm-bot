import tempfile
from pathlib import Path
from unittest.mock import patch

from test_terminal_state import TerminalStateTestCase, _response, _tool_call, CHANNEL_ID
from test_support import TEST_USER_ID
from run_workspace import RunActiveError, RunCatalog
import bot
import run_state


class ToolIntentTest(TerminalStateTestCase):
    async def test_result_commit_failure_stops_before_another_dispatch(self):
        original = run_state.save
        saves = 0

        def fail_after_start(*args, **kwargs):
            nonlocal saves
            saves += 1
            if saves > 1:
                raise OSError("disk full")
            return original(*args, **kwargs)

        with patch.object(run_state, "save", fail_after_start):
            await self.run_agent([
                _response(tool_calls=[_tool_call("first", "bash_exec", {"command": "true"})]),
                _response(tool_calls=[_tool_call("second", "bash_exec", {"command": "true"})]),
            ])
        self.bash_exec.assert_awaited_once()
        self.assertIn("persist", self.recorder.detail)

    async def test_interrupted_dispatch_requires_owner_resolution(self):
        observed = []

        async def interrupted(workspace, calls, **kwargs):
            pending = run_state.load(workspace)["pending_tools"]
            self.assertEqual(pending[0]["id"], "effect")
            observed.append(workspace)
            # Model a tool effect followed by a failure before the group commit.
            (workspace.root / "effect.txt").write_text("performed")
            raise RuntimeError("process interruption")

        with tempfile.TemporaryDirectory() as root:
            self.log_root = root
            with patch.object(bot, "execute_tools_in_parallel", interrupted):
                await self.run_agent([_response(tool_calls=[
                    _tool_call("effect", "bash_exec", {"command": "some effect"})])])
            workspace = observed[0]
            self.assertTrue(run_state.load(workspace)["pending_tools"])
            catalog = RunCatalog(Path(root) / "workspace", Path(root) / "logs")
            with patch.object(bot, "RUN_CATALOG", catalog):
                with self.assertRaises(RunActiveError):
                    bot.resume_run(TEST_USER_ID, CHANNEL_ID, workspace.run_id)
                bot.resume_run(TEST_USER_ID, CHANNEL_ID, workspace.run_id, "skip")
            record = run_state.load(workspace)
            self.assertNotIn("pending_tools", record)
            self.assertIn("effect", record["executed_call_ids"])
            self.assertEqual(record["next_step"], 2)
            self.assertIn("unknown", record["tail"][-1]["content"])

    async def test_intent_write_failure_prevents_dispatch(self):
        with patch.object(bot.run_state, "mark_pending", side_effect=OSError("disk full")):
            await self.run_agent([_response(tool_calls=[
                _tool_call("effect", "bash_exec", {"command": "effect"})])])
        self.bash_exec.assert_not_awaited()
