"""Handler-level proof that the gate sits ahead of every side effect.

Every identifier here is synthetic.
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from deadlines import CancelToken
from test_support import (
    FakeAuthor,
    FakeChannel,
    FakeInteraction,
    FakeMessage,
    TEST_ADMIN_ID,
    TEST_OUTSIDER_ID,
    TEST_USER_ID,
    run_catalog_patch,
)

import authz
import bot
import steering

CHANNEL_ID = 987654500


class GateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self._log_dir = tempfile.TemporaryDirectory()
        self._patches = [
            run_catalog_patch(bot, self._log_dir.name),
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
        bot.channel_cancel_token.pop(CHANNEL_ID, None)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_run_leases,
            bot.channel_active_runs,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    def assertNoSideEffects(self):
        """No model call, no tool run, no state change, no persistent record."""
        self.completion.assert_not_awaited()
        self.execute_tools.assert_not_awaited()
        self.assertEqual(bot.channel_history[CHANNEL_ID], [])
        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
        self.assertEqual(list(bot.RUN_CATALOG.runs_root.iterdir()), [])
        self.assertEqual(list(bot.RUN_CATALOG.logs_root.iterdir()), [])


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
        self.assertEqual(list(bot.RUN_CATALOG.runs_root.iterdir()), [])
        self.assertEqual(list(bot.RUN_CATALOG.logs_root.iterdir()), [])
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
    def start_fake_run(self, owner_id):
        """Stand in for an in-flight run so !stop has a token to cancel."""
        token = CancelToken()
        bot.channel_cancel_token[CHANNEL_ID] = token
        bot.channel_run_owner[CHANNEL_ID] = owner_id
        return token

    async def test_outsider_stop_does_not_cancel_the_run(self):
        token = self.start_fake_run(TEST_USER_ID)
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        await bot.on_message(message)

        self.assertFalse(token.cancelled)
        self.assertIn("권한이 없습니다", message.replies[-1])

    async def test_outsider_reset_does_not_clear_history(self):
        bot.channel_history[CHANNEL_ID].append({"role": "user", "content": "지켜져야 하는 기록"})
        message = FakeMessage("!reset", CHANNEL_ID, author=FakeAuthor(TEST_OUTSIDER_ID, "outsider"))

        await bot.on_message(message)

        self.assertEqual(len(bot.channel_history[CHANNEL_ID]), 1)

    async def test_control_command_outside_a_routed_channel_is_ignored(self):
        other_channel = 987654504
        token = CancelToken()
        bot.channel_cancel_token[other_channel] = token
        try:
            message = FakeMessage("!stop", other_channel, author=FakeAuthor(TEST_USER_ID))
            await bot.on_message(message)
            self.assertFalse(token.cancelled)
            self.assertEqual(message.replies, [])
        finally:
            bot.channel_cancel_token.pop(other_channel, None)

    async def test_allowed_owner_stop_cancels_the_run(self):
        token = self.start_fake_run(TEST_USER_ID)
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertTrue(token.cancelled)

    async def test_stop_with_no_run_in_flight_says_so(self):
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertIn("진행 중인 자율 탐색이 없습니다", message.replies[-1])

    async def test_non_owner_cannot_stop_someone_elses_run(self):
        token = self.start_fake_run(TEST_ADMIN_ID)
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertFalse(token.cancelled)
        self.assertIn("시작한 사용자", message.replies[-1])

    async def test_admin_can_stop_another_users_run(self):
        token = self.start_fake_run(TEST_USER_ID)
        message = FakeMessage("!stop", CHANNEL_ID, author=FakeAuthor(TEST_ADMIN_ID, "admin"))

        await bot.on_message(message)

        self.assertTrue(token.cancelled)


class PendingDirectOwnershipTest(GateTestCase):
    # Mutation caught: publishing a direct-run token before its owner lets an
    # unrelated allowlisted non-admin cancel the pending response.
    async def test_non_owner_cannot_stop_a_pending_direct_run(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def model(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="직접 답변"),
            )])

        self.completion.side_effect = model
        owner_message = FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_ADMIN_ID, "admin"),
        )
        run = asyncio.create_task(bot.on_message(owner_message))
        await asyncio.wait_for(started.wait(), timeout=1)
        token = bot.channel_cancel_token[CHANNEL_ID]

        try:
            stop = FakeMessage(
                "!stop",
                CHANNEL_ID,
                author=FakeAuthor(TEST_USER_ID, "other-user"),
            )
            await bot.on_message(stop)

            self.assertFalse(token.cancelled)
            self.assertIn("시작한 사용자", stop.replies[-1])
        finally:
            release.set()
            await asyncio.gather(run, return_exceptions=True)

    # Mutation caught: letting caller-level CancelledError bypass direct-route
    # cleanup leaves an owner and active token for a run that no longer exists.
    async def test_caller_cancellation_releases_a_pending_direct_run(self):
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def model(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        self.completion.side_effect = model
        message = FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_USER_ID),
        )
        run = asyncio.create_task(bot.on_message(message))
        await asyncio.wait_for(started.wait(), timeout=1)

        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run

        self.assertTrue(finalized.is_set())
        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
        self.assertNotIn(CHANNEL_ID, bot.channel_run_owner)
        self.assertFalse(bot.channel_active_runs[CHANNEL_ID])

    # Production mutation caught: admitting a second goal while a run already
    # holds the channel puts two runs on one session, which is the race this
    # issue closes. The second message is steering for the live run, and a
    # direct-answer run has no step to apply it to, so it must be refused
    # outright rather than accepted and dropped.
    async def test_a_second_goal_during_a_pending_direct_run_is_not_admitted(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def model(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="직접 답변"),
            )])

        self.completion.side_effect = model
        first_run = asyncio.create_task(bot.on_message(FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_USER_ID, "first-owner"),
        )))
        await asyncio.wait_for(started.wait(), timeout=1)
        first_token = bot.channel_cancel_token[CHANNEL_ID]

        try:
            second = FakeMessage(
                "간단히 답해줘",
                CHANNEL_ID,
                author=FakeAuthor(TEST_ADMIN_ID, "second-owner"),
            )
            await bot.on_message(second)

            self.assertEqual(len(bot.channel_run_leases[CHANNEL_ID]), 1)
            self.assertEqual(len(list(bot.RUN_CATALOG.runs_root.iterdir())), 1)
            self.assertIs(bot.channel_cancel_token[CHANNEL_ID], first_token)
            self.assertEqual(bot.channel_run_owner[CHANNEL_ID], TEST_USER_ID)
            self.assertEqual(second.reactions, [])
            self.assertIn("적용되지 않았습니다", second.replies[-1])
        finally:
            release.set()
            await asyncio.wait_for(first_run, timeout=1)

        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
        self.assertNotIn(CHANNEL_ID, bot.channel_run_owner)
        self.assertFalse(bot.channel_active_runs[CHANNEL_ID])


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
    def start_active_run(self, owner_id):
        workspace = bot.RUN_CATALOG.acquire(owner_id, CHANNEL_ID)
        token = CancelToken()
        mailbox = steering.SteeringMailbox(workspace.run_id)
        # 루프가 도는 런을 흉내내므로 큐도 열려 있어야 한다. 닫힌 큐는
        # "반영할 스텝이 없다"는 다른 상황이다.
        mailbox.open()
        lease = {
            "token": token,
            "owner": owner_id,
            "active": True,
            "workspace": workspace,
            "steering": mailbox,
        }
        bot.channel_run_leases[CHANNEL_ID].append(lease)
        bot.channel_active_runs[CHANNEL_ID] = True
        bot.channel_run_owner[CHANNEL_ID] = owner_id
        return lease

    async def test_non_owner_steering_is_refused_and_not_queued(self):
        lease = self.start_active_run(TEST_ADMIN_ID)
        message = FakeMessage("이 방향으로 바꿔줘", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertEqual(lease["steering"].depth, 0)
        self.assertEqual(len(list(bot.RUN_CATALOG.runs_root.iterdir())), 1)
        self.assertEqual(list(bot.RUN_CATALOG.logs_root.iterdir()), [])
        self.assertIn("시작한 사용자", message.replies[-1])

    async def test_owner_steering_is_queued(self):
        lease = self.start_active_run(TEST_USER_ID)
        message = FakeMessage("이 방향으로 바꿔줘", CHANNEL_ID, author=FakeAuthor(TEST_USER_ID))

        await bot.on_message(message)

        self.assertEqual(len(lease["steering"]), 1)


class ToolsDisabledTest(GateTestCase):
    async def test_tools_disabled_offers_the_model_no_tools(self):
        with patch.object(bot, "TOOLS_ENABLED", False):
            params = bot.agent_tool_params()
        self.assertEqual(params, {})
        self.assertEqual(
            bot.agent_tool_params(), {"tools": bot.TOOLS_SCHEMA, "tool_choice": "auto"}
        )


class SlashCommandGateTest(GateTestCase):
    """The four slash commands had no coverage at all.

    Completion criterion "text and slash commands use the same policy path" was
    only ever confirmed by reading the code, which is how a doubled ⛔ marker in
    deny_interaction survived.
    """

    async def test_outsider_is_refused_by_every_slash_command(self):
        cases = (
            ("reset", bot.slash_reset, ()),
            ("stop", bot.slash_stop, ()),
            ("clear", bot.slash_clear, (10,)),
            ("reasoning", bot.slash_reasoning, ("high",)),
        )
        for name, command, args in cases:
            with self.subTest(command=name):
                interaction = FakeInteraction(
                    CHANNEL_ID, user_id=TEST_OUTSIDER_ID, manage_messages=True
                )
                await command.callback(interaction, *args)

                self.assertEqual(len(interaction.replies), 1)
                self.assertNotIn("초기화", interaction.replies[0])
                self.assertEqual(interaction.channel.purge_calls, 0)
                self.assertNotIn(CHANNEL_ID, bot.channel_reasoning)
                self.assertNoSideEffects()

    async def test_slash_denial_carries_exactly_one_deny_marker(self):
        # Mutation caught: prefixing DENY_ACCESS_MESSAGE, which already opens with
        # ⛔, printed the marker twice on every slash access denial while the text
        # path printed it once.
        interaction = FakeInteraction(CHANNEL_ID, user_id=TEST_OUTSIDER_ID)
        await bot.slash_reasoning.callback(interaction, "high")

        self.assertEqual(interaction.replies, [authz.DENY_ACCESS_MESSAGE])
        self.assertEqual(interaction.replies[0].count("⛔"), 1)

    async def test_slash_purge_is_judged_on_the_callers_own_permission(self):
        # The caller is on the allowlist and is an admin, but holds no Manage
        # Messages permission, so the purge must not happen.
        interaction = FakeInteraction(
            CHANNEL_ID, user_id=TEST_ADMIN_ID, manage_messages=False
        )
        await bot.slash_clear.callback(interaction, 10)

        self.assertEqual(interaction.channel.purge_calls, 0)
        self.assertIn("메시지 관리", interaction.replies[0])

    async def test_slash_purge_clears_state_only_after_a_successful_delete(self):
        bot.channel_history[CHANNEL_ID] = [{"role": "user", "content": "keep me"}]
        interaction = FakeInteraction(
            CHANNEL_ID,
            user_id=TEST_ADMIN_ID,
            manage_messages=True,
            purge_error=RuntimeError("rate limited"),
        )
        await bot.slash_clear.callback(interaction, 10)

        self.assertEqual(interaction.channel.purge_calls, 1)
        self.assertEqual(
            bot.channel_history[CHANNEL_ID], [{"role": "user", "content": "keep me"}]
        )
        self.assertIn("유지", interaction.replies[-1])

    async def test_allowed_caller_passes_every_slash_gate(self):
        reset = FakeInteraction(CHANNEL_ID)
        await bot.slash_reset.callback(reset)
        self.assertIn("초기화", reset.replies[0])

        reasoning = FakeInteraction(CHANNEL_ID)
        await bot.slash_reasoning.callback(reasoning, "high")
        self.assertEqual(bot.channel_reasoning[CHANNEL_ID], "high")

        purge = FakeInteraction(CHANNEL_ID, user_id=TEST_ADMIN_ID, manage_messages=True)
        await bot.slash_clear.callback(purge, 3)
        self.assertEqual(purge.channel.purge_calls, 1)

        stop = FakeInteraction(CHANNEL_ID)
        await bot.slash_stop.callback(stop)
        self.assertIn("진행 중인 자율 탐색이 없습니다", stop.replies[0])


if __name__ == "__main__":
    unittest.main()
