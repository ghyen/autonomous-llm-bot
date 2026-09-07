"""Opt-in real backend and sandbox check; Discord is replaced with test doubles.

RUN_LOCAL_LLM_SMOKE=1 python -m unittest test_local_llm_smoke -v
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from test_support import FakeMessage, run_catalog_patch
from test_terminal_state import RunRecorder

import bot
import outcome


@unittest.skipUnless(os.environ.get("RUN_LOCAL_LLM_SMOKE") == "1", "requires a running local LLM")
class LocalLLMSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_model_tools_and_completion(self):
        channel = 987654999
        message = FakeMessage(
            "Run this local smoke check using tools. First call bash_exec with command "
            "printf LOCAL_LLM_SMOKE_OK > smoke.txt. After receiving the result, call read_file on smoke.txt. "
            "After receiving that result, call finish_task with a report containing "
            "LOCAL_LLM_SMOKE_OK and what you read. Do not modify other files.",
            channel,
        )
        recorder = RunRecorder()
        bot.FREE_RESPONSE_CHANNEL_IDS.add(channel)
        bot.channel_reasoning[channel] = "none"
        try:
            with tempfile.TemporaryDirectory() as root, run_catalog_patch(bot, root), \
                    recorder.install(), patch.object(bot, "MAX_AGENT_LOOPS", 6), \
                    patch.object(bot, "log_session_event", wraps=bot.log_session_event) as events:
                await asyncio.wait_for(bot.on_message(message), timeout=600)

                responses = [c.kwargs for c in events.call_args_list if c.args[1] == "model_response"]
                results = [c.kwargs for c in events.call_args_list if c.args[1] == "tool_result"]
                self.assertGreaterEqual(len(responses), 3)
                self.assertTrue(all(r["effort"] == "none" for r in responses))
                self.assertTrue(all(r["reasoning_chars"] == 0 for r in responses))
                for name in ("bash_exec", "read_file"):
                    self.assertTrue(any(r["tool"] == name and r["status"] == "ok" for r in results), results)
                self.assertEqual(recorder.reason, outcome.COMPLETED)
                self.assertEqual(recorder.detail, outcome.DETAIL_FINISH_TASK)
                self.assertIn("LOCAL_LLM_SMOKE_OK", "\n".join(message.replies))
        finally:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel)
            for state in (
                bot.channel_history, bot.channel_summary, bot.channel_reasoning,
                bot.channel_cancel_token, bot.channel_active_runs,
                bot.channel_run_owner, bot.channel_ledger,
            ):
                state.pop(channel, None)
