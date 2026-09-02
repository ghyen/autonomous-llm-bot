"""Run admission and the per-run steering mailbox, end to end.

The issue's synthetic reproduction turned into regression tests: stall a handler
before the lease is published and inject a second goal, block the terminal phase
and compare what the user was told with what actually happened, and prove one
run's cleanup never touches another run's mailbox.

Every identifier here is synthetic.
"""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import (  # sets required config env before bot imports
    FakeAuthor,
    FakeMessage,
    TEST_USER_ID,
    run_catalog_patch,
)

import bot
import steering as steering_mod

CHANNEL_ID = 987654900
OTHER_CHANNEL_ID = 987654901

AGENT_GOAL = "시스템 상태를 조사해줘"
DIRECT_GOAL = "간단히 답해줘"


def _response(content="", tool_calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content,
        reasoning_content="",
        reasoning="",
        tool_calls=list(tool_calls),
    ))])


def _finish_call(call_id="finish-1", report="조사 완료"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="finish_task",
            arguments='{"report":"%s"}' % report,
        ),
    )


def _bash_call(call_id="probe-1"):
    """A step that keeps the loop alive.

    A first step with text and no tool call is taken as a direct answer and ends
    the run, so every test that needs a second iteration has to call a tool.
    """
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="bash_exec", arguments='{"command":"probe"}'),
    )


async def _tool_results(workspace, calls, **kwargs):
    return ["[stdout]\n확인됨\n[exit code: 0]"] * len(calls)


def _payload_text(messages):
    return "\n".join(str(bot._msg_content(msg) or "") for msg in messages or [])


class StalledReplyMessage(FakeMessage):
    """Holds the status reply open - the window a second handler used to enter.

    The run's own status message is the first reply, so only that one stalls;
    the final report reply must still go through or the run never finishes.
    """

    def __init__(self, *args, gate, **kwargs):
        super().__init__(*args, **kwargs)
        self._gate = gate
        self.status_reply_started = asyncio.Event()

    async def reply(self, content):
        if not self.status_reply_started.is_set():
            self.status_reply_started.set()
            await self._gate.wait()
        return await super().reply(content)


class SteeringFlowTestCase(unittest.IsolatedAsyncioTestCase):
    """One live run per channel, a stubbed model, and an isolated catalog."""

    channels = (CHANNEL_ID, OTHER_CHANNEL_ID)

    def setUp(self):
        for channel_id in self.channels:
            bot.FREE_RESPONSE_CHANNEL_IDS.add(channel_id)
        self._log_dir = tempfile.TemporaryDirectory()
        self._patches = [
            run_catalog_patch(bot, self._log_dir.name),
            patch.object(bot, "create_streaming_completion", AsyncMock()),
            patch.object(bot, "execute_tools_in_parallel", _tool_results),
            patch.object(bot, "MAX_AGENT_LOOPS", 3),
            patch.object(bot, "CHECKPOINT_INTERVAL", 99),
            patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99),
        ]
        for item in self._patches:
            item.start()
        self.completion = bot.create_streaming_completion

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._log_dir.cleanup()
        for channel_id in self.channels:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            bot.channel_run_owner.pop(channel_id, None)
            bot.channel_cancel_token.pop(channel_id, None)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_run_leases,
                bot.channel_active_runs,
                bot.channel_ledger,
            ):
                state.pop(channel_id, None)

    def sole_lease(self, channel_id=CHANNEL_ID):
        leases = bot.channel_run_leases[channel_id]
        self.assertEqual(len(leases), 1, "a channel must never hold two runs")
        return leases[0]

    def workspace_count(self):
        return len(list(bot.RUN_CATALOG.runs_root.iterdir()))

    async def steer(self, text, channel_id=CHANNEL_ID, author=None):
        message = FakeMessage(
            text, channel_id, author=author or FakeAuthor(TEST_USER_ID)
        )
        await bot.on_message(message)
        return message


class SteeringMailboxTest(unittest.TestCase):
    """The mailbox alone: order, bound, dedup, and terminal accounting."""

    def mailbox(self, max_depth=3):
        return steering_mod.SteeringMailbox("0" * 32, max_depth=max_depth)

    # Production mutation caught: draining in any order other than arrival order
    # makes which instruction wins depend on timing.
    def test_drain_returns_items_in_arrival_order(self):
        box = self.mailbox()
        box.open()
        for text in ("첫째", "둘째", "셋째"):
            box.offer("tester", text)

        drained = box.drain()

        self.assertEqual([item.text for item in drained], ["첫째", "둘째", "셋째"])
        self.assertEqual([item.state for item in drained], [steering_mod.APPLIED] * 3)
        self.assertEqual(box.depth, 0)

    # Production mutation caught: dropping the bound lets one channel inflate
    # every later prompt with an unbounded number of steering blocks.
    def test_offer_past_the_bound_is_rejected_and_says_the_queue_was_full(self):
        box = self.mailbox(max_depth=2)
        box.open()
        box.offer("tester", "하나")
        box.offer("tester", "둘")

        receipt = box.offer("tester", "셋")

        self.assertEqual(receipt.state, steering_mod.REJECTED)
        self.assertEqual(receipt.reason, steering_mod.REASON_QUEUE_FULL)
        self.assertFalse(receipt.accepted)
        self.assertEqual(box.depth, 2)
        self.assertEqual([item.text for item in box.drain()], ["하나", "둘"])

    # Production mutation caught: re-queueing an identical pending instruction
    # injects the same steering block twice into the prompt.
    def test_identical_pending_instruction_is_coalesced_not_duplicated(self):
        box = self.mailbox()
        box.open()
        box.offer("tester", "같은 지시")

        receipt = box.offer("tester", "같은 지시")

        self.assertEqual(receipt.state, steering_mod.COALESCED)
        self.assertTrue(receipt.accepted)
        self.assertEqual(box.depth, 1)
        self.assertEqual(box.stats()[steering_mod.SUPERSEDED], 1)

    # Production mutation caught: accepting an item when no next step exists is
    # the silent drop the issue reported.
    def test_a_closed_mailbox_refuses_arrivals(self):
        box = self.mailbox()

        before_open = box.offer("tester", "루프 시작 전")
        box.open()
        box.offer("tester", "루프 중")
        box.close(steering_mod.CANCELLED)
        after_close = box.offer("tester", "종료 단계")

        self.assertEqual(before_open.state, steering_mod.REJECTED)
        self.assertEqual(before_open.reason, steering_mod.REASON_NO_LOOP)
        self.assertEqual(after_close.state, steering_mod.REJECTED)
        self.assertEqual(after_close.reason, steering_mod.REASON_TERMINAL)
        self.assertEqual(box.depth, 0)

    # Production mutation caught: leaving items pending at close means an item
    # ends in no terminal state at all and nobody is ever told.
    def test_close_settles_every_pending_item_exactly_once(self):
        box = self.mailbox()
        box.open()
        box.offer("tester", "하나")
        box.offer("tester", "둘")

        unapplied = box.close(steering_mod.CANCELLED)

        self.assertEqual([item.state for item in unapplied], [steering_mod.CANCELLED] * 2)
        self.assertEqual(box.close(steering_mod.CANCELLED), [])
        stats = box.stats()
        self.assertEqual(stats[steering_mod.CANCELLED], 2)
        self.assertEqual(
            stats["received"],
            stats[steering_mod.APPLIED]
            + stats[steering_mod.SUPERSEDED]
            + stats[steering_mod.REJECTED]
            + stats[steering_mod.CANCELLED],
        )

    # Production mutation caught: putting instruction text into the observable
    # counters leaks message content into logs that only need depth and timing.
    def test_stats_expose_depth_and_timestamps_without_message_text(self):
        clock = iter([100.0, 200.0, 300.0])
        box = steering_mod.SteeringMailbox("f" * 32, max_depth=2, clock=lambda: next(clock))
        box.open()
        box.offer("tester", "비밀 지시 문구")
        box.drain()

        stats = box.stats()
        line = box.observability_line()

        self.assertEqual(stats["depth"], 0)
        self.assertEqual(stats["max_depth"], 2)
        self.assertEqual(stats["received"], 1)
        self.assertEqual(stats["last_received_at"], 100.0)
        self.assertEqual(stats["last_applied_at"], 200.0)
        self.assertNotIn("비밀 지시 문구", line)
        self.assertNotIn("tester", line)
        self.assertIn("depth", line)


class RunAdmissionTest(SteeringFlowTestCase):
    # Production mutation caught: publishing the lease after the first await
    # lets a message arriving in that window start a second run on the channel.
    async def test_a_goal_arriving_before_the_status_reply_becomes_steering(self):
        gate = asyncio.Event()
        payloads = []

        async def model(**kwargs):
            payloads.append(list(kwargs.get("messages") or []))
            return _response(tool_calls=[_finish_call()])

        self.completion.side_effect = model
        goal = StalledReplyMessage(AGENT_GOAL, CHANNEL_ID, gate=gate)
        run = asyncio.create_task(bot.on_message(goal))
        try:
            await asyncio.wait_for(goal.status_reply_started.wait(), timeout=1)

            self.assertTrue(
                bot.channel_active_runs[CHANNEL_ID],
                "the channel must read as occupied before the first await",
            )
            mailbox = self.sole_lease()["steering"]
            second = await self.steer("이 방향으로 바꿔줘")

            self.assertEqual(self.workspace_count(), 1)
            self.assertEqual(len(bot.channel_run_leases[CHANNEL_ID]), 1)
            self.assertEqual(second.reactions, ["📥"])
            self.assertEqual(mailbox.depth, 1)
        finally:
            gate.set()
            await asyncio.wait_for(run, timeout=2)

        self.assertEqual(mailbox.stats()[steering_mod.APPLIED], 1)
        self.assertIn("이 방향으로 바꿔줘", _payload_text(payloads[0]))

    # Production mutation caught: a direct-answer run has no next step, so
    # accepting steering for it promises an application that cannot happen.
    async def test_steering_during_a_direct_answer_run_is_refused_not_queued(self):
        gate = asyncio.Event()

        async def model(**kwargs):
            if kwargs.get("stage") == "direct":
                await gate.wait()
                return _response(content="직접 답변")
            return _response(tool_calls=[_finish_call()])

        self.completion.side_effect = model
        run = asyncio.create_task(bot.on_message(FakeMessage(DIRECT_GOAL, CHANNEL_ID)))
        try:
            await asyncio.sleep(0)
            self.assertTrue(bot.channel_active_runs[CHANNEL_ID])
            mailbox = self.sole_lease()["steering"]

            refused = await self.steer("직접 답변 중에 끼어든 지시")

            self.assertEqual(self.workspace_count(), 1)
            self.assertEqual(len(bot.channel_run_leases[CHANNEL_ID]), 1)
            self.assertEqual(refused.reactions, [])
            self.assertIn("적용되지 않았습니다", refused.replies[-1])
            self.assertEqual(mailbox.depth, 0)
            self.assertEqual(mailbox.stats()[steering_mod.REJECTED], 1)
        finally:
            gate.set()
            await asyncio.wait_for(run, timeout=2)


class TerminalPhaseSteeringTest(SteeringFlowTestCase):
    # Production mutation caught: telling a user their instruction lands on the
    # next step while the run is synthesizing its report - there is no next step.
    async def test_steering_during_final_synthesis_is_told_it_was_not_applied(self):
        synthesis_started = asyncio.Event()
        gate = asyncio.Event()

        async def model(**kwargs):
            if kwargs.get("stage") == "synthesis":
                synthesis_started.set()
                await gate.wait()
                return _response(content="최종 보고서")
            return _response(tool_calls=[_bash_call()])

        self.completion.side_effect = model
        with patch.object(bot, "MAX_AGENT_LOOPS", 1):
            run = asyncio.create_task(bot.on_message(FakeMessage(AGENT_GOAL, CHANNEL_ID)))
            try:
                await asyncio.wait_for(synthesis_started.wait(), timeout=1)
                mailbox = self.sole_lease()["steering"]

                refused = await self.steer("합성 중에 도착한 지시")

                self.assertEqual(refused.reactions, [])
                self.assertIn("적용되지 않았습니다", refused.replies[-1])
                self.assertEqual(mailbox.depth, 0)
                self.assertEqual(mailbox.stats()[steering_mod.REJECTED], 1)
            finally:
                gate.set()
                await asyncio.wait_for(run, timeout=2)

    # Production mutation caught: items accepted while the run was still looping
    # but never drained used to vanish with no notice at all.
    async def test_items_left_queued_when_the_run_ends_are_notified_as_unapplied(self):
        step_started = asyncio.Event()
        gate = asyncio.Event()
        payloads = []

        async def model(**kwargs):
            payloads.append(list(kwargs.get("messages") or []))
            step_started.set()
            await gate.wait()
            return _response(tool_calls=[_finish_call()])

        self.completion.side_effect = model
        goal = FakeMessage(AGENT_GOAL, CHANNEL_ID)
        run = asyncio.create_task(bot.on_message(goal))
        try:
            await asyncio.wait_for(step_started.wait(), timeout=1)
            mailbox = self.sole_lease()["steering"]
            await self.steer("뒤늦은 지시 하나")
            await self.steer("뒤늦은 지시 둘")
            self.assertEqual(mailbox.depth, 2)
        finally:
            gate.set()
            await asyncio.wait_for(run, timeout=2)

        stats = mailbox.stats()
        self.assertEqual(stats[steering_mod.CANCELLED], 2)
        self.assertEqual(stats[steering_mod.APPLIED], 0)
        self.assertEqual(stats["depth"], 0)
        unapplied_notices = [
            text for text in goal.channel.sent if "적용되지 않았습니다" in text
        ]
        self.assertEqual(len(unapplied_notices), 1, goal.channel.sent)
        self.assertIn("2건", unapplied_notices[0])
        self.assertNotIn("뒤늦은 지시 하나", _payload_text(payloads[0]))

    # Production mutation caught: a run that dies mid-step skips the terminal
    # close, so queued items would settle with nobody told they never landed.
    async def test_a_crash_mid_step_still_reports_queued_items_as_unapplied(self):
        rollover_started = asyncio.Event()
        gate = asyncio.Event()

        async def model(**kwargs):
            return _response(tool_calls=[_bash_call()])

        async def exploding_rollover(*args, **kwargs):
            rollover_started.set()
            await gate.wait()
            raise RuntimeError("rollover 폭발")

        self.completion.side_effect = model
        goal = FakeMessage(AGENT_GOAL, CHANNEL_ID)
        with patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 1), \
                patch.object(bot, "rollover_agent_context", exploding_rollover):
            run = asyncio.create_task(bot.on_message(goal))
            try:
                await asyncio.wait_for(rollover_started.wait(), timeout=1)
                mailbox = self.sole_lease()["steering"]
                await self.steer("압축 중에 도착한 지시")
                self.assertEqual(mailbox.depth, 1)
            finally:
                gate.set()
                await asyncio.wait_for(run, timeout=2)

        self.assertEqual(mailbox.stats()[steering_mod.CANCELLED], 1)
        unapplied_notices = [
            text for text in goal.channel.sent if "적용되지 않았습니다" in text
        ]
        self.assertEqual(len(unapplied_notices), 1, goal.channel.sent)


class SteeringBackpressureTest(SteeringFlowTestCase):
    # Production mutation caught: draining only at the loop head makes an
    # instruction that arrives during tool work wait out the checkpoint report
    # and the context rollover before it is ever read.
    async def test_steering_queued_during_tool_work_lands_before_the_checkpoint(self):
        tool_started = asyncio.Event()
        gate = asyncio.Event()
        stages = []

        async def model(**kwargs):
            stage = kwargs.get("stage")
            stages.append((stage, _payload_text(kwargs.get("messages"))))
            if stage == "checkpoint":
                return _response(content="중간 보고서")
            if len([s for s, _ in stages if s == "agent"]) == 1:
                return _response(tool_calls=[_bash_call()])
            return _response(tool_calls=[_finish_call()])

        async def stalled_tools(workspace, calls, **kwargs):
            tool_started.set()
            await gate.wait()
            return ["[stdout]\n확인됨\n[exit code: 0]"] * len(calls)

        self.completion.side_effect = model
        with patch.object(bot, "CHECKPOINT_INTERVAL", 1), \
                patch.object(bot, "execute_tools_in_parallel", stalled_tools):
            run = asyncio.create_task(bot.on_message(FakeMessage(AGENT_GOAL, CHANNEL_ID)))
            try:
                await asyncio.wait_for(tool_started.wait(), timeout=1)
                mailbox = self.sole_lease()["steering"]
                await self.steer("도구 실행 중에 도착한 지시")
                self.assertEqual(mailbox.depth, 1)
            finally:
                gate.set()
                await asyncio.wait_for(run, timeout=2)

        checkpoint_payloads = [text for stage, text in stages if stage == "checkpoint"]
        self.assertEqual(len(checkpoint_payloads), 1, stages)
        self.assertIn("도구 실행 중에 도착한 지시", checkpoint_payloads[0])
        self.assertEqual(mailbox.stats()[steering_mod.APPLIED], 1)

    # Production mutation caught: an unbounded queue with no dedup accepts every
    # repeat and every overflow, then replays them all into one prompt.
    async def test_the_bound_holds_and_each_caller_learns_the_real_outcome(self):
        first_step = asyncio.Event()
        gate = asyncio.Event()
        payloads = []

        async def model(**kwargs):
            payloads.append(list(kwargs.get("messages") or []))
            if len(payloads) == 1:
                first_step.set()
                await gate.wait()
                return _response(tool_calls=[_bash_call()])
            return _response(tool_calls=[_finish_call()])

        self.completion.side_effect = model
        with patch.object(bot, "STEERING_QUEUE_MAX", 2):
            run = asyncio.create_task(bot.on_message(FakeMessage(AGENT_GOAL, CHANNEL_ID)))
            try:
                await asyncio.wait_for(first_step.wait(), timeout=1)
                mailbox = self.sole_lease()["steering"]

                queued = await self.steer("첫 지시")
                repeated = await self.steer("첫 지시")
                second = await self.steer("둘째 지시")
                overflowed = await self.steer("셋째 지시")

                self.assertEqual(mailbox.depth, 2)
                self.assertIn("접수", queued.replies[-1])
                self.assertIn("병합", repeated.replies[-1])
                self.assertIn("접수", second.replies[-1])
                self.assertIn("적용되지 않았습니다", overflowed.replies[-1])
                self.assertEqual(overflowed.reactions, [])
            finally:
                gate.set()
                await asyncio.wait_for(run, timeout=2)

        applied_payload = _payload_text(payloads[1])
        self.assertLess(
            applied_payload.index("첫 지시"),
            applied_payload.index("둘째 지시"),
            "steering must be applied in arrival order",
        )
        self.assertNotIn("셋째 지시", applied_payload)
        stats = mailbox.stats()
        self.assertEqual(stats[steering_mod.APPLIED], 2)
        self.assertEqual(stats[steering_mod.SUPERSEDED], 1)
        self.assertEqual(stats[steering_mod.REJECTED], 1)

    # Production mutation caught: without receipt and apply timestamps the
    # nine-minute injection delay the issue reported stays invisible.
    async def test_receipt_and_apply_times_are_observable_without_content(self):
        first_step = asyncio.Event()
        gate = asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs.get("stage"))
            if len(calls) == 1:
                first_step.set()
                await gate.wait()
                return _response(tool_calls=[_bash_call()])
            return _response(tool_calls=[_finish_call()])

        self.completion.side_effect = model
        run = asyncio.create_task(bot.on_message(FakeMessage(AGENT_GOAL, CHANNEL_ID)))
        try:
            await asyncio.wait_for(first_step.wait(), timeout=1)
            mailbox = self.sole_lease()["steering"]
            await self.steer("관측용 지시 본문")
            received_at = mailbox.stats()["last_received_at"]
            self.assertIsNotNone(received_at)
            self.assertEqual(mailbox.stats()["depth"], 1)
        finally:
            gate.set()
            await asyncio.wait_for(run, timeout=2)

        stats = mailbox.stats()
        self.assertIsNotNone(stats["last_applied_at"])
        self.assertGreaterEqual(stats["last_applied_at"], received_at)
        self.assertNotIn("관측용 지시 본문", mailbox.observability_line())


class CrossRunIsolationTest(SteeringFlowTestCase):
    # Production mutation caught: channel-global steering state let one run's
    # cleanup clear a queue and a control token that belong to another run.
    async def test_one_runs_cleanup_leaves_the_other_channels_run_untouched(self):
        first_step = {}
        gates = {CHANNEL_ID: asyncio.Event(), OTHER_CHANNEL_ID: asyncio.Event()}
        started = {
            CHANNEL_ID: asyncio.Event(),
            OTHER_CHANNEL_ID: asyncio.Event(),
        }

        async def model(**kwargs):
            channel_id = OTHER_CHANNEL_ID if "다른 채널" in _payload_text(
                kwargs.get("messages")
            ) else CHANNEL_ID
            if channel_id not in first_step:
                first_step[channel_id] = True
                started[channel_id].set()
                await gates[channel_id].wait()
            return _response(tool_calls=[_finish_call()])

        self.completion.side_effect = model
        run_a = asyncio.create_task(bot.on_message(FakeMessage(AGENT_GOAL, CHANNEL_ID)))
        await asyncio.wait_for(started[CHANNEL_ID].wait(), timeout=1)
        run_b = asyncio.create_task(bot.on_message(
            FakeMessage(AGENT_GOAL + " 다른 채널", OTHER_CHANNEL_ID)
        ))
        await asyncio.wait_for(started[OTHER_CHANNEL_ID].wait(), timeout=1)

        try:
            mailbox_b = self.sole_lease(OTHER_CHANNEL_ID)["steering"]
            token_b = bot.channel_cancel_token[OTHER_CHANNEL_ID]
            await self.steer("다른 채널 전용 지시", channel_id=OTHER_CHANNEL_ID)
            self.assertEqual(mailbox_b.depth, 1)

            gates[CHANNEL_ID].set()
            await asyncio.wait_for(run_a, timeout=2)

            self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)
            self.assertFalse(bot.channel_active_runs[CHANNEL_ID])
            self.assertEqual(mailbox_b.depth, 1)
            self.assertIs(bot.channel_cancel_token[OTHER_CHANNEL_ID], token_b)
            self.assertTrue(bot.channel_active_runs[OTHER_CHANNEL_ID])
        finally:
            for gate in gates.values():
                gate.set()
            await asyncio.gather(run_a, run_b, return_exceptions=True)

        self.assertEqual(mailbox_b.stats()[steering_mod.CANCELLED], 1)
        self.assertFalse(bot.channel_active_runs[OTHER_CHANNEL_ID])


if __name__ == "__main__":
    unittest.main()
