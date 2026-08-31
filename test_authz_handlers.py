"""Handler-level proof that the gate sits ahead of every side effect.

Every identifier here is synthetic.
"""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import discord

from test_support import (
    FakeAuthor,
    FakeChannel,
    FakeMessage,
    TEST_ADMIN_ID,
    TEST_OUTSIDER_ID,
    TEST_USER_ID,
)

import bot

CHANNEL_ID = 987654500


class GateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self._log_dir = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(bot, "SYSTEM_LOG_DIR", self._log_dir.name),
            patch.object(bot, "create_streaming_completion", AsyncMock()),
            patch.object(bot, "execute_tools_in_parallel", AsyncMock(return_value=[])),
        ]
        for item in self._patches:
            item.start()
        self.completion = bot.create_streaming_completion
        self.execute_tools = bot.execute_tools_in_parallel

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._log_dir.cleanup()
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(CHANNEL_ID)
        bot.channel_run_owner.pop(CHANNEL_ID, None)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_stop_requested,
            bot.channel_active_runs,
            bot.channel_user_queue,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    def assertNoSideEffects(self):
        """No model call, no tool run, no state change, no persistent record."""
        self.completion.assert_not_awaited()
        self.execute_tools.assert_not_awaited()
        self.assertEqual(bot.channel_history[CHANNEL_ID], [])
        self.assertFalse(bot.channel_stop_requested[CHANNEL_ID])
        self.assertEqual(bot.channel_user_queue[CHANNEL_ID], [])
        self.assertEqual(os.listdir(self._log_dir.name), [])


class DeniedRequestTest(GateTestCase):
    async def test_outsider_in_a_free_channel_causes_nothing(self):
        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        await bot.on_message(message)

        self.assertNoSideEffects()
        self.assertIn("권한이 없습니다", message.replies[-1])

    async def test_a_dm_does_not_authenticate_an_outsider(self):
        message = FakeMessage("시스템 상태를 조사해줘", 987654501, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        # A DM is a routing signal only; make isinstance(channel, DMChannel) true.
        with patch.object(discord, "DMChannel", FakeChannel):
            await bot.on_message(message)

        self.completion.assert_not_awaited()
        self.execute_tools.assert_not_awaited()
        self.assertEqual(os.listdir(self._log_dir.name), [])
        self.assertIn("권한이 없습니다", message.replies[-1])

    async def test_mentioning_the_bot_does_not_authenticate_an_outsider(self):
        message = FakeMessage("조사해줘", 987654502, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))
        message.mentions = [bot.bot.user]

        await bot.on_message(message)

        self.completion.assert_not_awaited()
        self.assertIn("권한이 없습니다", message.replies[-1])

    async def test_message_addressed_to_nobody_is_ignored_without_a_reply(self):
        message = FakeMessage("잡담", 987654503, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        await bot.on_message(message)

        self.assertEqual(message.replies, [])
        self.completion.assert_not_awaited()


class ControlCommandOrderTest(GateTestCase):
    async def test_outsider_stop_does_not_set_the_flag(self):
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        await bot.on_message(message)

        self.assertFalse(bot.channel_stop_requested[CHANNEL_ID])
        self.assertIn("권한이 없습니다", message.replies[-1])

    async def test_outsider_reset_does_not_clear_history(self):
        bot.channel_history[CHANNEL_ID].append({"role": "user", "content": "지켜져야 하는 기록"})
        message = FakeMessage("!reset", CHANNEL_ID, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        await bot.on_message(message)

        self.assertEqual(len(bot.channel_history[CHANNEL_ID]), 1)

    async def test_control_command_outside_a_routed_channel_is_ignored(self):
        message = FakeMessage("!stop", 987654504, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertFalse(bot.channel_stop_requested[987654504])
        self.assertEqual(message.replies, [])
        bot.channel_stop_requested.pop(987654504, None)

    async def test_allowed_caller_stop_works(self):
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertTrue(bot.channel_stop_requested[CHANNEL_ID])

    async def test_non_owner_cannot_stop_someone_elses_run(self):
        bot.channel_run_owner[CHANNEL_ID] = TEST_ADMIN_ID
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertFalse(bot.channel_stop_requested[CHANNEL_ID])
        self.assertIn("시작한 사용자", message.replies[-1])

    async def test_admin_can_stop_another_users_run(self):
        bot.channel_run_owner[CHANNEL_ID] = TEST_USER_ID
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_ADMIN_ID, "admin"))

        await bot.on_message(message)

        self.assertTrue(bot.channel_stop_requested[CHANNEL_ID])


class PurgeTest(GateTestCase):
    async def test_outsider_purge_never_reaches_discord(self):
        message = FakeMessage("!clear 50", CHANNEL_ID, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"), manage_messages=True)

        await bot.on_message(message)

        self.assertEqual(message.channel.purge_calls, 0)
        self.assertIn("권한이 없습니다", message.replies[-1])

    async def test_allowed_non_admin_purge_is_refused(self):
        bot.channel_history[CHANNEL_ID].append({"role": "user", "content": "보존"})
        message = FakeMessage("!clear 50", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID), manage_messages=True)

        await bot.on_message(message)

        self.assertEqual(message.channel.purge_calls, 0)
        self.assertEqual(len(bot.channel_history[CHANNEL_ID]), 1)
        self.assertIn("DISCORD_ADMIN_USER_IDS", message.replies[-1])

    async def test_admin_without_the_discord_permission_is_refused(self):
        message = FakeMessage("!clear 50", CHANNEL_ID, author=FakeAuthor(TEST_ADMIN_ID, "admin"), manage_messages=False)

        await bot.on_message(message)

        self.assertEqual(message.channel.purge_calls, 0)
        self.assertIn("메시지 관리", message.replies[-1])

    async def test_admin_with_the_permission_purges_then_clears_state(self):
        bot.channel_history[CHANNEL_ID].append({"role": "user", "content": "삭제 대상"})
        message = FakeMessage("!clear 5", CHANNEL_ID, author=FakeAuthor(TEST_ADMIN_ID, "admin"), manage_messages=True)

        with patch.object(bot.asyncio, "sleep", AsyncMock()):
            await bot.on_message(message)

        self.assertEqual(message.channel.purge_calls, 1)
        self.assertEqual(bot.channel_history[CHANNEL_ID], [])

    async def test_failed_purge_keeps_the_history_and_reports_failure(self):
        bot.channel_history[CHANNEL_ID].append({"role": "user", "content": "보존되어야 함"})
        message = FakeMessage(
            "!clear 5",
            CHANNEL_ID,
            author=FakeAuthor(TEST_ADMIN_ID, "admin"),
            manage_messages=True,
            purge_error=discord.Forbidden(
                type("R", (), {"status": 403, "reason": "Forbidden"})(), "no permission"
            ),
        )

        await bot.on_message(message)

        self.assertEqual(message.channel.purge_calls, 1)
        self.assertEqual(len(bot.channel_history[CHANNEL_ID]), 1)
        self.assertIn("대화 기록은 유지되었습니다", message.channel.sent[-1])


class SteeringTest(GateTestCase):
    async def test_non_owner_steering_is_refused_and_not_queued(self):
        bot.channel_active_runs[CHANNEL_ID] = True
        bot.channel_run_owner[CHANNEL_ID] = TEST_ADMIN_ID
        message = FakeMessage("이 방향으로 바꿔줘", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertEqual(bot.channel_user_queue[CHANNEL_ID], [])
        self.assertEqual(os.listdir(self._log_dir.name), [])
        self.assertIn("시작한 사용자", message.replies[-1])

    async def test_owner_steering_is_queued(self):
        bot.channel_active_runs[CHANNEL_ID] = True
        bot.channel_run_owner[CHANNEL_ID] = TEST_USER_ID
        message = FakeMessage("이 방향으로 바꿔줘", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertEqual(len(bot.channel_user_queue[CHANNEL_ID]), 1)


class ToolsDisabledTest(GateTestCase):
    async def test_tools_disabled_offers_the_model_no_tools(self):
        with patch.object(bot, "TOOLS_ENABLED", False):
            params = bot.agent_tool_params()
        self.assertEqual(params, {})
        self.assertEqual(
            bot.agent_tool_params(), {"tools": bot.TOOLS_SCHEMA, "tool_choice": "auto"}
        )


if __name__ == "__main__":
    unittest.main()
