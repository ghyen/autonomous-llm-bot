"""Stop latency, stage deadlines, and process-tree reclamation end to end.

Every identifier here is synthetic.
"""

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage, FakeSentMessage

import bot
import outcome as outcome_mod
from deadlines import CancelToken, RunCancelled, StageTimeout

CHANNEL_ID = 987654700
STOP_LATENCY_BUDGET = 2.0

LONG_REPORT = "정리. " + ("확인됨. " * 80)


def _response(content="", tool_calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, reasoning_content="", reasoning="", tool_calls=list(tool_calls),
    ))])


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


def _is_report_stage(messages):
    system = bot._msg_content(messages[0]) if messages else ""
    return any(m in system for m in ("수석 분석가", "AI 리포터", "컨텍스트 압축기"))


class ClientInitializationTest(unittest.TestCase):
    # Mutation caught: removing eager chat-resource initialization moves cold
    # OpenAI setup into the first request's event-loop turn and blocks the loop.
    def test_chat_resource_is_warm_before_the_event_loop_serves_requests(self):
        script = (
            "import asyncio\n"
            "import test_support\n"
            "import bot\n"
            "async def probe():\n"
            "    asyncio.get_running_loop().slow_callback_duration = 0.05\n"
            "    _ = bot.client.chat.completions\n"
            "asyncio.run(probe(), debug=True)\n"
        )
        probe = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stderr, "")


class CancellationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self.settled = []
        original = bot.RunOutcome.settle
        recorder = self.settled

        def tracked(outcome_self, reason, detail=""):
            accepted = original(outcome_self, reason, detail)
            if accepted:
                recorder.append((reason, detail))
            return accepted

        self._settle_patch = patch.object(bot.RunOutcome, "settle", tracked)
        self._settle_patch.start()

    def tearDown(self):
        self._settle_patch.stop()
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(CHANNEL_ID)
        bot.channel_run_owner.pop(CHANNEL_ID, None)
        bot.channel_cancel_token.pop(CHANNEL_ID, None)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_active_runs,
            bot.channel_user_queue,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    @property
    def reason(self):
        return self.settled[0][0] if self.settled else None

    @property
    def detail(self):
        return self.settled[0][1] if self.settled else ""


class StopLatencyTest(CancellationTestCase):
    async def measure_stop(self, model, tool_exec=None, max_loops=8):
        """Run until the stage signals it started, then stop and time the exit."""
        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        patches = [
            patch.object(bot, "MAX_AGENT_LOOPS", max_loops),
            patch.object(bot, "CHECKPOINT_INTERVAL", 99),
            patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99),
            patch.object(bot, "create_streaming_completion", model),
        ]
        if tool_exec is not None:
            patches.append(patch.object(bot, "tool_bash_exec", tool_exec))

        with tempfile.TemporaryDirectory() as log_dir:
            patches.append(patch.object(bot, "SYSTEM_LOG_DIR", log_dir))
            for item in patches:
                item.start()
            try:
                run = asyncio.ensure_future(bot.on_message(message))
                await asyncio.wait_for(self.stage_started.wait(), timeout=5)
                requested = time.monotonic()
                self.assertTrue(bot.request_run_cancel(CHANNEL_ID))
                await asyncio.wait_for(run, timeout=10)
                elapsed = time.monotonic() - requested
            finally:
                for item in reversed(patches):
                    item.stop()
        return elapsed, message

    async def test_stop_during_a_wedged_model_call_returns_within_the_budget(self):
        self.stage_started = asyncio.Event()

        async def model(**kwargs):
            if _is_report_stage(kwargs.get("messages") or []):
                return _response(content=LONG_REPORT)
            self.stage_started.set()
            await asyncio.sleep(60)
            return _response(content="never")

        elapsed, _ = await self.measure_stop(model)

        self.assertLess(elapsed, STOP_LATENCY_BUDGET)
        self.assertEqual(self.reason, outcome_mod.STOPPED)

    async def test_stop_during_a_wedged_tool_returns_within_the_budget(self):
        self.stage_started = asyncio.Event()
        first = {"sent": False}

        async def model(**kwargs):
            if _is_report_stage(kwargs.get("messages") or []):
                return _response(content=LONG_REPORT)
            if not first["sent"]:
                first["sent"] = True
                return _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "wedged"})])
            return _response(content="계속")

        async def wedged_tool(command):
            self.stage_started.set()
            await asyncio.sleep(60)
            return "never"

        elapsed, _ = await self.measure_stop(model, tool_exec=wedged_tool)

        self.assertLess(elapsed, STOP_LATENCY_BUDGET)
        self.assertEqual(self.reason, outcome_mod.STOPPED)

    async def test_stop_during_a_wedged_stream_returns_within_the_budget(self):
        """The idle deadline is long; cancellation must not wait for it."""
        self.stage_started = asyncio.Event()
        started = self.stage_started

        class StallingStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                started.set()
                await asyncio.sleep(60)
                raise StopAsyncIteration

            async def close(self):
                pass

        async def create(**kwargs):
            return StallingStream()

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot.client.chat.completions, "create", create):
            run = asyncio.ensure_future(bot.on_message(message))
            await asyncio.wait_for(started.wait(), timeout=5)
            requested = time.monotonic()
            bot.request_run_cancel(CHANNEL_ID)
            await asyncio.wait_for(run, timeout=10)
            elapsed = time.monotonic() - requested

        self.assertLess(elapsed, STOP_LATENCY_BUDGET)
        self.assertEqual(self.reason, outcome_mod.STOPPED)


class NoStageAfterCancellationTest(CancellationTestCase):
    async def test_checkpoint_rollover_tools_and_retry_never_start_after_stop(self):
        stage_calls = []
        tool_calls = []
        started = asyncio.Event()

        async def model(**kwargs):
            messages = kwargs.get("messages") or []
            system = bot._msg_content(messages[0]) if messages else ""
            for name in ("AI 리포터", "컨텍스트 압축기"):
                if name in system:
                    stage_calls.append(name)
                    return _response(content="보고서")
            if "수석 분석가" in system:
                stage_calls.append("synthesis")
                return _response(content=LONG_REPORT)
            started.set()
            await asyncio.sleep(60)
            return _response(content="never")

        async def tool(command):
            tool_calls.append(command)
            return "ok"

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 8), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 1), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 1), \
                patch.object(bot, "tool_bash_exec", tool), \
                patch.object(bot, "create_streaming_completion", model):
            run = asyncio.ensure_future(bot.on_message(message))
            await asyncio.wait_for(started.wait(), timeout=5)
            bot.request_run_cancel(CHANNEL_ID)
            await asyncio.wait_for(run, timeout=10)

        self.assertEqual(self.reason, outcome_mod.STOPPED)
        self.assertEqual(tool_calls, [])
        # Cancellation starts no follow-up model stage: no checkpoint, rollover,
        # retry, or synthesis. A deterministic partial report is delivered.
        self.assertEqual(stage_calls, [])

    async def test_the_final_report_still_reaches_the_user_after_a_stop(self):
        started = asyncio.Event()

        async def model(**kwargs):
            if _is_report_stage(kwargs.get("messages") or []):
                return _response(content=LONG_REPORT)
            started.set()
            await asyncio.sleep(60)
            return _response(content="never")

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            run = asyncio.ensure_future(bot.on_message(message))
            await asyncio.wait_for(started.wait(), timeout=5)
            bot.request_run_cancel(CHANNEL_ID)
            await asyncio.wait_for(run, timeout=10)

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertIn("미완료", delivered)
        self.assertNotIn("완료 시간", delivered)


class PostModelCancellationBoundaryTest(CancellationTestCase):
    def cancel_from_status_edit(self, message, marker):
        status = FakeSentMessage()
        original_reply = message.reply
        first_reply = True
        fired = []

        async def reply(content):
            nonlocal first_reply
            sent = await original_reply(content)
            if first_reply:
                first_reply = False
                return status
            return sent

        async def edit(**kwargs):
            await FakeSentMessage.edit(status, **kwargs)
            text = kwargs.get("content") or ""
            if not fired and marker in text:
                fired.append(text)
                self.assertTrue(bot.request_run_cancel(CHANNEL_ID))

        message.reply = reply
        status.edit = edit
        return fired

    # Mutation caught: omitting the checkpoint after the reasoning dashboard
    # lets finish_task settle COMPLETED after a stop arrived during that await.
    async def test_stop_from_reasoning_status_edit_beats_finish_task(self):
        async def model(**kwargs):
            return _response(
                content="완료 직전 판단",
                tool_calls=[_tool_call("c1", "finish_task", {"report": LONG_REPORT})],
            )

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        fired = self.cancel_from_status_edit(message, "실시간 추론")
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 1), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            await bot.on_message(message)

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.reason, outcome_mod.STOPPED)
        self.assertIn("미완료", delivered)
        self.assertNotIn("완료 시간", delivered)

    # Mutation caught: omitting the checkpoint after the tool dashboard counts
    # an undispatched batch as executed when stop arrives during that await.
    async def test_stop_from_tool_status_edit_counts_no_undispatched_tool(self):
        async def model(**kwargs):
            return _response(
                content="도구 실행 직전 판단",
                tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})],
            )

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        fired = self.cancel_from_status_edit(message, "터미널 및 네트워크 I/O 실행 중")
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 1), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            await bot.on_message(message)

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(len(fired), 1)
        self.assertEqual(self.reason, outcome_mod.STOPPED)
        self.assertIn("총 0개 도구 실행", delivered)
        self.assertNotIn("총 1개 도구 실행", delivered)


class RolloverBoundaryTest(unittest.IsolatedAsyncioTestCase):
    # Mutation caught: replacing the typed rollover timeout fallback with a
    # re-raise loses the bounded local summary even though source is preserved.
    async def test_rollover_timeout_uses_bounded_local_compaction(self):
        messages = []
        for step in range(10):
            messages.extend([
                {
                    "role": "assistant",
                    "content": f"step {step} 판단",
                    "tool_calls": [{
                        "id": f"c{step}",
                        "type": "function",
                        "function": {
                            "name": "bash_exec",
                            "arguments": json.dumps({"command": f"probe {step}"}),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"c{step}",
                    "name": "bash_exec",
                    "content": f"step {step} 결과",
                },
            ])

        with patch.object(
            bot,
            "run_completion_stage",
            AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
        ):
            rolled, summary = await bot.rollover_agent_context(
                messages, "기존 요약", 10
            )

        self.assertIn("기존 요약", summary)
        self.assertIn("step 0", summary)
        self.assertLessEqual(len(summary), bot.ROLLING_SUMMARY_MAX_CHARS)
        self.assertEqual(bot._msg_role(rolled[0]), "system")
        self.assertIn("롤링 컨텍스트 재개", bot._msg_content(rolled[1]))

    # Mutation caught: checking cancellation only after rollover finds work lets
    # its early no-op return complete after the run was already stopped.
    async def test_pre_cancelled_rollover_cannot_take_the_noop_return(self):
        token = CancelToken()
        token.cancel("롤오버 전 중단")

        with self.assertRaises(RunCancelled):
            await bot.rollover_agent_context([], "", 1, token=token)


class CheckpointBoundaryTest(CancellationTestCase):
    async def run_checkpoint_error(self, error):
        agent_calls = []

        async def model(**kwargs):
            messages = kwargs.get("messages") or []
            system = bot._msg_content(messages[0]) if messages else ""
            if "AI 리포터" in system:
                raise error
            agent_calls.append(kwargs.get("stage"))
            if len(agent_calls) == 1:
                return _response(
                    tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]
                )
            return _response(
                tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]
            )

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 1), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "tool_bash_exec", AsyncMock(return_value="ok")), \
                patch.object(bot, "create_streaming_completion", model):
            await bot.on_message(message)

        return agent_calls, message

    # Mutation caught: a generic checkpoint fallback that swallows RunCancelled
    # continues to another agent request instead of settling STOPPED.
    async def test_checkpoint_cancellation_propagates_without_another_stage(self):
        agent_calls, message = await self.run_checkpoint_error(
            RunCancelled("체크포인트 중단")
        )

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(agent_calls, ["agent"])
        self.assertEqual(self.reason, outcome_mod.STOPPED)
        self.assertIn(outcome_mod.LABELS[outcome_mod.STOPPED], delivered)

    # Mutation caught: a generic checkpoint fallback that swallows StageTimeout
    # continues the run and can later claim completion after the stage failed.
    async def test_checkpoint_timeout_propagates_without_another_stage(self):
        agent_calls, message = await self.run_checkpoint_error(
            StageTimeout("checkpoint", 0.1)
        )

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(agent_calls, ["agent"])
        self.assertEqual(self.reason, outcome_mod.FAILED)
        self.assertIn("마감 초과", delivered)


class FinalSynthesisBoundaryTest(CancellationTestCase):
    # Mutation caught: using the incomplete-report closing text for an already
    # completed empty finish_task contradicts the completion footer on timeout.
    async def test_empty_finish_task_synthesis_timeout_stays_completed_in_output(self):
        synthesis_calls = []

        async def model(**kwargs):
            messages = kwargs.get("messages") or []
            system = bot._msg_content(messages[0]) if messages else ""
            if "수석 분석가" in system:
                synthesis_calls.append(kwargs.get("stage"))
                raise StageTimeout("synthesis", 0.1)
            return _response(
                tool_calls=[_tool_call("c1", "finish_task", {"report": ""})]
            )

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 1), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            await bot.on_message(message)

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(synthesis_calls, ["synthesis"])
        self.assertEqual(self.reason, outcome_mod.COMPLETED)
        self.assertIn("보고서 합성 마감 초과", delivered)
        self.assertIn("완료 시간", delivered)
        self.assertNotIn("미완료", delivered)
        self.assertNotIn("새 요청으로 이어서 진행하세요", delivered)

    # Mutation caught: letting synthesis StageTimeout escape to the outer generic
    # error path suppresses the deterministic partial report delivery.
    async def test_synthesis_timeout_delivers_a_deterministic_partial_report(self):
        synthesis_calls = []

        async def model(**kwargs):
            messages = kwargs.get("messages") or []
            system = bot._msg_content(messages[0]) if messages else ""
            if "수석 분석가" in system:
                synthesis_calls.append(kwargs.get("stage"))
                raise StageTimeout("synthesis", 0.1)
            return _response(content="")

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 1), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            await bot.on_message(message)

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(synthesis_calls, ["synthesis"])
        self.assertEqual(self.reason, outcome_mod.EXHAUSTED)
        self.assertIn("보고서 합성 마감 초과", delivered)
        self.assertIn("미완료", delivered)

    # Mutation caught: omitting the run token from final synthesis lets a late
    # model report arrive after !stop instead of cancelling into local fallback.
    async def test_stop_cancels_synthesis_and_delivers_local_fallback(self):
        synthesis_started = asyncio.Event()
        late_report = "늦게 도착한 모델 보고서"

        async def model(**kwargs):
            messages = kwargs.get("messages") or []
            system = bot._msg_content(messages[0]) if messages else ""
            if "수석 분석가" in system:
                synthesis_started.set()
                await asyncio.sleep(0.35)
                return _response(content=late_report)
            return _response(content="")

        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 1), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            run = asyncio.ensure_future(bot.on_message(message))
            await asyncio.wait_for(synthesis_started.wait(), timeout=1)
            requested = time.monotonic()
            self.assertTrue(bot.request_run_cancel(CHANNEL_ID))
            await asyncio.wait_for(run, timeout=2)
            elapsed = time.monotonic() - requested

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertLess(elapsed, 0.2)
        self.assertIn("보고서 합성 취소", delivered)
        self.assertNotIn(late_report, delivered)
        self.assertIn("미완료", delivered)


class StageDeadlineTest(CancellationTestCase):
    async def test_a_wedged_model_stage_fails_with_a_timeout_reason(self):
        async def model(**kwargs):
            if _is_report_stage(kwargs.get("messages") or []):
                return _response(content=LONG_REPORT)
            await asyncio.sleep(60)
            return _response(content="never")

        tight = dataclasses.replace(bot.CONFIG, model_stage_timeout=0.2, idle_timeout=0.2)
        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)

        async def guarded(**kwargs):
            from deadlines import with_deadline
            return await with_deadline(
                model(**kwargs), tight.model_stage_timeout, kwargs.get("token"), "agent"
            )

        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "CONFIG", tight), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "create_streaming_completion", guarded):
            await asyncio.wait_for(bot.on_message(message), timeout=10)

        self.assertEqual(self.reason, outcome_mod.FAILED)
        self.assertIn("마감 초과", self.detail)
        self.assertIn("agent", self.detail)
        # Distinguishable from a user stop.
        self.assertNotEqual(self.detail, outcome_mod.DETAIL_USER_STOP)


    # Mutation caught: giving the recovery attempt a fresh full timeout lets a
    # 400 response plus a hung retry exceed one model-stage budget.
    async def test_400_retry_uses_only_the_remaining_model_stage_budget(self):
        attempts = []

        async def model(**kwargs):
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                await asyncio.sleep(0.25)
                raise RuntimeError("400 invalid tool_call_id")
            await asyncio.sleep(60)
            return _response(content="never")

        tight = dataclasses.replace(bot.CONFIG, model_stage_timeout=0.4)
        message = FakeMessage("시스템 상태를 조사해줘", CHANNEL_ID)

        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "CONFIG", tight), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model):
            started = time.monotonic()
            await asyncio.wait_for(bot.on_message(message), timeout=2)
            elapsed = time.monotonic() - started

        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.reason, outcome_mod.FAILED)
        self.assertIn("agent:retry", self.detail)
        self.assertLess(elapsed, 0.55)


class ToolBatchCleanupTest(unittest.IsolatedAsyncioTestCase):
    # Mutation caught: narrowing cleanup to token/deadline failures lets an
    # ordinary child exception leave a blocking sibling alive.
    async def test_child_exception_reaps_sibling_and_preserves_exception(self):
        sibling_started = asyncio.Event()
        sibling_finalized = asyncio.Event()
        release = asyncio.Event()
        expected = AttributeError("tool failed")

        async def tool(command):
            if command == "fail":
                await sibling_started.wait()
                raise expected
            sibling_started.set()
            try:
                await release.wait()
            finally:
                sibling_finalized.set()

        calls = [
            {"name": "bash_exec", "arguments": {"command": "fail"}},
            {"name": "bash_exec", "arguments": {"command": "block"}},
        ]

        try:
            with patch.object(bot, "tool_bash_exec", tool):
                with self.assertRaises(AttributeError) as caught:
                    await bot.execute_tools_in_parallel(calls)

            self.assertIs(caught.exception, expected)
            self.assertTrue(sibling_finalized.is_set())
        finally:
            release.set()
            await asyncio.wait_for(sibling_finalized.wait(), timeout=1)


class ProcessTreeTest(unittest.IsolatedAsyncioTestCase):
    """bash_exec used to kill only the shell, leaving grandchildren running."""

    def _descendants_alive(self, marker):
        found = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True
        )
        return [line for line in found.stdout.split() if line.strip()]

    @unittest.skipUnless(sys.platform != "win32", "POSIX process groups only")
    async def test_timeout_reclaims_the_whole_process_tree(self):
        marker = "p2ach-cancel-probe-31573"
        tight = dataclasses.replace(bot.CONFIG, bash_timeout=0.3)

        with tempfile.TemporaryDirectory() as workspace, \
                patch.object(bot, "CONFIG", tight), \
                patch.object(bot, "WORKSPACE_DIR", workspace):
            result = await bot.tool_bash_exec(
                f"sh -c 'sleep 40 # {marker}' & sleep 40 # {marker}"
            )

        self.assertIn("timed out", result)
        # Give the kernel a moment to reap the group.
        await asyncio.sleep(0.3)
        self.assertEqual(self._descendants_alive(marker), [])

    @unittest.skipUnless(sys.platform != "win32", "POSIX process groups only")
    async def test_cancellation_reclaims_the_whole_process_tree(self):
        marker = "p2ach-cancel-probe-31574"

        with tempfile.TemporaryDirectory() as workspace, \
                patch.object(bot, "WORKSPACE_DIR", workspace):
            task = asyncio.ensure_future(
                bot.tool_bash_exec(f"sh -c 'sleep 40 # {marker}' & sleep 40 # {marker}")
            )
            await asyncio.sleep(0.4)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        await asyncio.sleep(0.3)
        self.assertEqual(self._descendants_alive(marker), [])


if __name__ == "__main__":
    unittest.main()
