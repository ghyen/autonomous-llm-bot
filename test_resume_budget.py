from unittest.mock import patch

from test_terminal_state import TerminalStateTestCase, _response, _tool_call
from ledger import ResearchLedger
import bot
import outcome


class ResumeBudgetTest(TerminalStateTestCase):
    async def test_resume_beyond_old_limit_gets_bounded_new_steps(self):
        record = {"next_step": 2001, "message_id": 1, "executed_call_ids": [],
                  "ledger": ResearchLedger(), "summary": "prior work", "tail": []}
        with patch.object(bot.run_state, "load", return_value=record):
            await self.run_agent([
                _response(tool_calls=[_tool_call("new1", "bash_exec", {"command": "true"})]),
                _response(tool_calls=[_tool_call("new2", "bash_exec", {"command": "true"})]),
            ], max_loops=2)
        self.assertEqual(self.bash_exec.await_count, 2)
        self.assertEqual(self.recorder.reason, outcome.EXHAUSTED)
        self.assertEqual(self.recorder.detail, outcome.DETAIL_STEP_BUDGET)
