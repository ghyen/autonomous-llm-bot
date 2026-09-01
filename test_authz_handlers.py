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
        bot.channel_cancel_token.pop(CHANNEL_ID, None)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
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
        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
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

    # Mutation caught: identity-blind cleanup by an older direct run can erase
    # the newer run's published token and make authorized stop miss its target.
    async def test_stale_direct_cleanup_preserves_newer_stop_token(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs.get("stage"))
            if len(calls) == 1:
                first_started.set()
                await first_release.wait()
            else:
                second_started.set()
                await second_release.wait()
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="직접 답변"),
            )])

        self.completion.side_effect = model
        first_message = FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_USER_ID, "first-owner"),
        )
        first_run = asyncio.create_task(bot.on_message(first_message))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        first_token = bot.channel_cancel_token[CHANNEL_ID]

        second_message = FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_ADMIN_ID, "second-owner"),
        )
        second_run = asyncio.create_task(bot.on_message(second_message))
        second_started_waiter = asyncio.create_task(second_started.wait())

        try:
            done, _ = await asyncio.wait(
                {second_run, second_started_waiter},
                timeout=1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            self.assertIn(
                second_started_waiter,
                done,
                "the pending-direct admission gate rejected the second request",
            )
            self.assertEqual(calls, ["direct", "direct"])
            second_token = bot.channel_cancel_token[CHANNEL_ID]
            self.assertIsNot(second_token, first_token)
            self.assertEqual(bot.channel_run_owner[CHANNEL_ID], TEST_ADMIN_ID)

            first_release.set()
            await asyncio.wait_for(first_run, timeout=1)
            self.assertIs(bot.channel_cancel_token[CHANNEL_ID], second_token)
            self.assertEqual(bot.channel_run_owner[CHANNEL_ID], TEST_ADMIN_ID)

            stop = FakeMessage(
                "!stop",
                CHANNEL_ID,
                author=FakeAuthor(TEST_ADMIN_ID, "second-owner"),
            )
            await bot.on_message(stop)
            self.assertTrue(second_token.cancelled)
            await asyncio.wait_for(second_run, timeout=1)
        finally:
            first_release.set()
            second_release.set()
            second_started_waiter.cancel()
            first_run.cancel()
            second_run.cancel()
            await asyncio.gather(
                first_run, second_run, second_started_waiter,
                return_exceptions=True,
            )

        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
        self.assertNotIn(CHANNEL_ID, bot.channel_run_owner)
        self.assertFalse(bot.channel_active_runs[CHANNEL_ID])

    # Mutation caught: when the newer direct run finishes first, its cleanup
    # must republish the older still-live run so that owner's stop remains valid.
    async def test_newer_direct_cleanup_restores_older_stop_token(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs.get("stage"))
            if len(calls) == 1:
                first_started.set()
                await first_release.wait()
            else:
                second_started.set()
                await second_release.wait()
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="직접 답변"),
            )])

        self.completion.side_effect = model
        first_run = asyncio.create_task(bot.on_message(FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_USER_ID, "first-owner"),
        )))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        first_token = bot.channel_cancel_token[CHANNEL_ID]

        second_run = asyncio.create_task(bot.on_message(FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(TEST_ADMIN_ID, "second-owner"),
        )))
        await asyncio.wait_for(second_started.wait(), timeout=1)
        second_token = bot.channel_cancel_token[CHANNEL_ID]

        try:
            second_release.set()
            await asyncio.wait_for(second_run, timeout=1)
            self.assertIs(bot.channel_cancel_token.get(CHANNEL_ID), first_token)
            self.assertEqual(bot.channel_run_owner.get(CHANNEL_ID), TEST_USER_ID)

            stop = FakeMessage(
                "!stop",
                CHANNEL_ID,
                author=FakeAuthor(TEST_USER_ID, "first-owner"),
            )
            await bot.on_message(stop)
            self.assertTrue(first_token.cancelled)
            self.assertFalse(second_token.cancelled)
            await asyncio.wait_for(first_run, timeout=1)
        finally:
            first_release.set()
            second_release.set()
            first_run.cancel()
            second_run.cancel()
            await asyncio.gather(first_run, second_run, return_exceptions=True)

        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
        self.assertNotIn(CHANNEL_ID, bot.channel_run_owner)
        self.assertFalse(bot.channel_active_runs[CHANNEL_ID])
    async def _start_active_run_with_newer_pending_direct(
            self, active_owner_id, pending_owner_id):
        first_direct_started = asyncio.Event()
        second_direct_started = asyncio.Event()
        first_direct_release = asyncio.Event()
        second_direct_release = asyncio.Event()
        active_started = asyncio.Event()
        active_cleanup_started = asyncio.Event()
        active_cleanup_release = asyncio.Event()
        direct_calls = 0

        async def model(**kwargs):
            nonlocal direct_calls
            if kwargs.get("stage") == "direct":
                direct_calls += 1
                if direct_calls == 1:
                    first_direct_started.set()
                    await first_direct_release.wait()
                    content = ""
                else:
                    second_direct_started.set()
                    await second_direct_release.wait()
                    content = "대기 중 직접 답변"
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )])

            active_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active_cleanup_started.set()
                await active_cleanup_release.wait()

        self.completion.side_effect = model
        active_run = asyncio.create_task(bot.on_message(FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(active_owner_id, "active-owner"),
        )))
        await asyncio.wait_for(first_direct_started.wait(), timeout=1)
        active_token = bot.channel_cancel_token[CHANNEL_ID]

        pending_run = asyncio.create_task(bot.on_message(FakeMessage(
            "간단히 답해줘",
            CHANNEL_ID,
            author=FakeAuthor(pending_owner_id, "pending-owner"),
        )))
        await asyncio.wait_for(second_direct_started.wait(), timeout=1)
        pending_token = bot.channel_cancel_token[CHANNEL_ID]

        first_direct_release.set()
        await asyncio.wait_for(active_started.wait(), timeout=1)
        return SimpleNamespace(
            active_run=active_run,
            pending_run=pending_run,
            active_token=active_token,
            pending_token=pending_token,
            first_direct_release=first_direct_release,
            second_direct_release=second_direct_release,
            active_cleanup_started=active_cleanup_started,
            active_cleanup_release=active_cleanup_release,
        )

    # Mutation caught: newest-pending publication must not authorize that owner
    # to steer the older active run or make stop cancel the pending token. After
    # active-first completion, control returns to the surviving pending lease.
    async def test_active_lease_controls_overlap_when_active_finishes_first(self):
        pending_owner_id = 333333333333333333
        bot.ALLOWED_USER_IDS.add(pending_owner_id)
        state = await self._start_active_run_with_newer_pending_direct(
            TEST_USER_ID, pending_owner_id
        )

        try:
            pending_steering = FakeMessage(
                "대기 소유자의 잘못된 지시",
                CHANNEL_ID,
                author=FakeAuthor(pending_owner_id, "pending-owner"),
            )
            await bot.on_message(pending_steering)
            self.assertIn("시작한 사용자", pending_steering.replies[-1])
            self.assertEqual(bot.channel_user_queue[CHANNEL_ID], [])

            active_steering = FakeMessage(
                "활성 소유자의 지시",
                CHANNEL_ID,
                author=FakeAuthor(TEST_USER_ID, "active-owner"),
            )
            await bot.on_message(active_steering)
            self.assertEqual(active_steering.reactions, ["📥"])
            self.assertEqual(
                bot.channel_user_queue[CHANNEL_ID],
                [("active-owner", "활성 소유자의 지시")],
            )

            stop = FakeMessage(
                "!stop",
                CHANNEL_ID,
                author=FakeAuthor(TEST_USER_ID, "active-owner"),
            )
            await bot.on_message(stop)
            self.assertTrue(state.active_token.cancelled)
            self.assertFalse(state.pending_token.cancelled)
            await asyncio.wait_for(state.active_cleanup_started.wait(), timeout=1)
            state.active_cleanup_release.set()
            await asyncio.wait_for(state.active_run, timeout=1)

            self.assertIs(
                bot.channel_cancel_token.get(CHANNEL_ID), state.pending_token
            )
            self.assertEqual(
                bot.channel_run_owner.get(CHANNEL_ID), pending_owner_id
            )
            self.assertFalse(bot.channel_active_runs[CHANNEL_ID])

            state.second_direct_release.set()
            await asyncio.wait_for(state.pending_run, timeout=1)
        finally:
            bot.ALLOWED_USER_IDS.discard(pending_owner_id)
            state.first_direct_release.set()
            state.second_direct_release.set()
            state.active_cleanup_release.set()
            state.active_run.cancel()
            state.pending_run.cancel()
            await asyncio.gather(
                state.active_run, state.pending_run, return_exceptions=True
            )

        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
        self.assertNotIn(CHANNEL_ID, bot.channel_run_owner)
        self.assertFalse(bot.channel_active_runs[CHANNEL_ID])

    # Mutation caught: stop must target the active lease even when cancellation
    # cleanup is held open and the newer pending direct run completes first.
    async def test_active_lease_controls_overlap_when_pending_finishes_first(self):
        pending_owner_id = 333333333333333333
        bot.ALLOWED_USER_IDS.add(pending_owner_id)
        state = await self._start_active_run_with_newer_pending_direct(
            TEST_USER_ID, pending_owner_id
        )

        try:
            stop = FakeMessage(
                "!stop",
                CHANNEL_ID,
                author=FakeAuthor(TEST_USER_ID, "active-owner"),
            )
            await bot.on_message(stop)
            self.assertTrue(state.active_token.cancelled)
            self.assertFalse(state.pending_token.cancelled)
            await asyncio.wait_for(state.active_cleanup_started.wait(), timeout=1)

            state.second_direct_release.set()
            await asyncio.wait_for(state.pending_run, timeout=1)
            self.assertIs(
                bot.channel_cancel_token.get(CHANNEL_ID), state.active_token
            )
            self.assertEqual(
                bot.channel_run_owner.get(CHANNEL_ID), TEST_USER_ID
            )
            self.assertTrue(bot.channel_active_runs[CHANNEL_ID])

            state.active_cleanup_release.set()
            await asyncio.wait_for(state.active_run, timeout=1)
        finally:
            bot.ALLOWED_USER_IDS.discard(pending_owner_id)
            state.first_direct_release.set()
            state.second_direct_release.set()
            state.active_cleanup_release.set()
            state.active_run.cancel()
            state.pending_run.cancel()
            await asyncio.gather(
                state.active_run, state.pending_run, return_exceptions=True
            )

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
