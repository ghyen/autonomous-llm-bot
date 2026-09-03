"""The six scenarios from issue #3, each reaching exactly one terminal reason.

Every identifier here is synthetic.
"""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage, run_catalog_patch

import bot
import outcome as outcome_mod
from deadlines import StageTimeout

CHANNEL_ID = 987654600

LONG_REPORT = "조사 결과 정리. " + ("확인됨. " * 80)


def _response(content="", tool_calls=(), reasoning=""):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        reasoning="",
        tool_calls=list(tool_calls),
    ))])


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


class RunRecorder:
    """Captures the settled outcome by wrapping RunOutcome.settle."""

    def __init__(self):
        self.settled = []
        self.ignored = []

    def install(self):
        recorder = self
        original = bot.RunOutcome.settle

        def tracked(self, reason, detail=""):
            accepted = original(self, reason, detail)
            (recorder.settled if accepted else recorder.ignored).append((reason, detail))
            return accepted

        return patch.object(bot.RunOutcome, "settle", tracked)

    @property
    def reason(self):
        return self.settled[0][0] if self.settled else None

    @property
    def detail(self):
        return self.settled[0][1] if self.settled else ""


class TerminalStateTestCase(unittest.IsolatedAsyncioTestCase):
    # Subclasses that read the session log after a run set this to a directory
    # they own, so the tree outlives the run instead of being thrown away.
    log_root = None

    def setUp(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self.recorder = RunRecorder()

    def tearDown(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(CHANNEL_ID)
        bot.channel_run_owner.pop(CHANNEL_ID, None)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_active_runs,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    async def run_agent(self, responses, request="시스템 상태를 조사해줘", max_loops=6,
                        checkpoint_interval=99, compaction_interval=99, stop_after=None,
                        tool_result="[stdout]\nok\n[exit code: 0]", message=None):
        """Drive on_message with scripted model responses."""
        script = list(responses)
        calls = {"count": 0}

        async def model(**kwargs):
            messages = kwargs.get("messages") or []
            system = bot._msg_content(messages[0]) if messages else ""
            if any(marker in system for marker in ("수석 분석가", "AI 리포터", "컨텍스트 압축기")):
                self.synthesis_calls.append(kwargs)
                return _response(content=LONG_REPORT)
            calls["count"] += 1
            if stop_after is not None and calls["count"] == stop_after:
                bot.request_run_cancel(CHANNEL_ID)
            return script.pop(0) if script else _response(content="계속 진행합니다.")

        self.synthesis_calls = []
        # Kept on self so assertions still reach it after the patch is undone.
        self.bash_exec = AsyncMock(return_value=tool_result)
        message = message if message is not None else FakeMessage(request, CHANNEL_ID)

        with tempfile.TemporaryDirectory() as scratch:
            log_dir = self.log_root or scratch
            with run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "MAX_AGENT_LOOPS", max_loops), \
                    patch.object(bot, "CHECKPOINT_INTERVAL", checkpoint_interval), \
                    patch.object(bot, "ROLLING_COMPACTION_INTERVAL", compaction_interval), \
                    patch.object(bot, "tool_bash_exec", self.bash_exec), \
                    patch.object(bot, "create_streaming_completion", model), \
                    self.recorder.install():
                await bot.on_message(message)

        return message

    @property
    def final_reply(self):
        return self._final_reply

    async def drive(self, *args, **kwargs):
        message = await self.run_agent(*args, **kwargs)
        self._final_reply = "\n".join(message.replies[1:] + message.channel.sent)
        return message


class ExactlyOneReasonTest(TerminalStateTestCase):
    async def test_1_parsed_finish_task_completes(self):
        await self.drive([
            _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
            _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
        ])

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        self.assertEqual(self.recorder.detail, outcome_mod.DETAIL_FINISH_TASK)
        self.assertIn("완료 시간", self.final_reply)
        self.assertNotIn("미완료", self.final_reply)

    async def test_2_completion_text_after_tools_does_not_stall_and_continues_until_budget(self):
        """Completion text without finish_task continues autonomously without stall until step budget."""
        await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(content="최종 결론 보고서: " + ("정리 완료. " * 60)),
            ],
            max_loops=3,
        )

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.EXHAUSTED)
        self.assertEqual(self.recorder.detail, outcome_mod.DETAIL_STEP_BUDGET)
        self.assertIn("미완료", self.final_reply)

    async def test_2b_internal_thought_without_tools_continues_to_next_step(self):
        """Pure internal reasoning (<think> only) proceeds without stall or nudge until finish_task."""
        await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(content="<think>추가 단서를 위해 README를 확인해야겠다.</think>"),
                _response(tool_calls=[_tool_call("c2", "bash_exec", {"command": "cat README.md"})]),
                _response(tool_calls=[_tool_call("c3", "finish_task", {"report": "완료 보고서"})]),
            ],
            max_loops=12,
        )

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        self.assertEqual(self.recorder.detail, outcome_mod.DETAIL_FINISH_TASK)
        self.assertIn("완료 시간", self.final_reply)

    # Mutation caught: omitting the post-model cancellation checkpoint lets
    # the response returned after cancellation start its second tool, probe2.
    async def test_3_stop_arriving_at_the_checkpoint_yields_stopped(self):
        await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(tool_calls=[_tool_call("c2", "bash_exec", {"command": "probe2"})]),
                _response(tool_calls=[_tool_call("c3", "bash_exec", {"command": "probe3"})]),
            ],
            max_loops=6,
            checkpoint_interval=2,
            compaction_interval=2,
            stop_after=2,
        )

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.STOPPED)
        self.assertIn("총 1개 도구 실행", self.final_reply)
        self.assertEqual(
            [call.args[1] for call in self.bash_exec.await_args_list],
            ["probe"],
        )
        self.assertIn("미완료", self.final_reply)
        self.assertNotIn("완료 시간", self.final_reply)

    async def test_4_stop_with_zero_tools_executed_never_says_completed(self):
        await self.drive(
            [_response(content="")],
            max_loops=4,
            stop_after=1,
        )

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.STOPPED)
        self.assertEqual(bot.channel_ledger[CHANNEL_ID].render(), "")
        self.assertIn("미완료", self.final_reply)
        self.assertNotIn("완료 시간", self.final_reply)

    async def test_5_tool_call_on_the_last_step_exhausts_without_a_rollover(self):
        message = await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(tool_calls=[_tool_call("c2", "bash_exec", {"command": "probe2"})]),
            ],
            max_loops=2,
            compaction_interval=2,
            checkpoint_interval=99,
        )

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.EXHAUSTED)
        self.assertEqual(self.recorder.detail, outcome_mod.DETAIL_STEP_BUDGET)
        # Step 2 is both the last step and a compaction step; no rollover ran.
        compaction_calls = [
            call for call in self.synthesis_calls
            if "컨텍스트 압축기" in bot._msg_content(call["messages"][0])
        ]
        self.assertEqual(compaction_calls, [])
        self.assertIn("미완료", self.final_reply)

    async def test_6_finish_task_with_companion_calls_runs_neither_and_says_so(self):
        message = await self.drive([
            _response(tool_calls=[
                _tool_call("c1", "finish_task", {"report": LONG_REPORT}),
                _tool_call("c2", "bash_exec", {"command": "should-not-run"}),
                _tool_call("c3", "write_file", {"path": "x", "content": "y"}),
            ]),
        ])

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        self.bash_exec.assert_not_awaited()
        self.assertIn("bash_exec", self.final_reply)
        self.assertIn("실행하지 않았습니다", self.final_reply)


class NoWorkAfterSettlingTest(TerminalStateTestCase):
    async def test_no_nudge_tool_checkpoint_or_rollover_after_finish_task(self):
        await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
                _response(tool_calls=[_tool_call("c3", "bash_exec", {"command": "must-not-run"})]),
            ],
            max_loops=8,
            checkpoint_interval=2,
            compaction_interval=2,
        )

        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        # Only the first bash_exec ran; the loop stopped at the transition.
        self.assertEqual(self.bash_exec.await_count, 1)
        self.assertEqual(self.synthesis_calls, [])

    async def test_a_second_transition_is_recorded_but_does_not_take_effect(self):
        await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
            ],
            max_loops=2,
            compaction_interval=99,
        )

        # Step 2 is the last step and also carries finish_task: COMPLETED wins,
        # and the trailing "loop ended" EXHAUSTED attempt is only recorded.
        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        self.assertIn((outcome_mod.EXHAUSTED, outcome_mod.DETAIL_STEP_BUDGET), self.recorder.ignored)


class DirectAnswerTest(TerminalStateTestCase):
    # Mutation caught: returning from the fast direct route before common cleanup
    # leaves a stale cancel token registered for a run that already answered.
    async def test_direct_route_cleans_the_token_after_success(self):
        message = await self.run_agent(
            [_response(content="짧은 답변입니다.")],
            request="간단히 답해줘",
        )

        self.assertIn("짧은 답변입니다.", message.replies)
        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)

    # Mutation caught: omitting the direct-stage token and typed cancellation
    # handling turns a stop into a successful direct answer or research fallback.
    async def test_direct_route_stop_is_not_swallowed_by_generic_fallback(self):
        message = await self.run_agent(
            [_response(content="취소 뒤 답변하면 안 됩니다.")],
            request="간단히 답해줘",
            stop_after=1,
        )

        delivered = "\n".join(message.replies + message.channel.sent)
        self.assertIn(outcome_mod.LABELS[outcome_mod.STOPPED], delivered)
        self.assertNotIn("취소 뒤 답변하면 안 됩니다.", delivered)
        self.assertEqual(self.synthesis_calls, [])
        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)

    # Mutation caught: splitting an unbounded local fallback into safe-sized
    # messages still permits arbitrarily many Discord sends and delays stop.
    async def test_direct_route_timeout_report_has_total_bound_without_mutating_ledger(self):
        async def timed_out(**kwargs):
            raise StageTimeout("direct", 0.1)

        ledger = bot.channel_ledger[CHANNEL_ID]
        ledger.apply_updates({
            "evidence": [
                {
                    "id": f"E_DIRECT_BOUND_{index:03d}",
                    "summary": "직접 응답 실패 전에 보존된 상세 관측 " * 8,
                    "source": f"test://direct-bounded/{index}",
                }
                for index in range(140)
            ],
        })
        full_ledger = ledger.render()
        message = FakeMessage("간단히 답해줘", CHANNEL_ID)

        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "create_streaming_completion", timed_out), \
                self.recorder.install():
            await bot.on_message(message)

        chunks = message.replies + message.channel.sent
        delivered = "".join(chunks)
        self.assertLessEqual(len(chunks), 3)
        self.assertLessEqual(sum(len(chunk) for chunk in chunks), 5700)
        self.assertLessEqual(max(len(chunk) for chunk in chunks), 1900)
        self.assertIn("[중간 상세 내용 생략 — 전체 원장은 내부 상태에 유지됨]", delivered)
        self.assertIn("마감 초과", delivered)
        self.assertEqual(ledger.render(), full_ledger)
        self.assertIn("E_DIRECT_BOUND_000", ledger.state_markers())
        self.assertIn("E_DIRECT_BOUND_139", ledger.state_markers())

    # Mutation caught: a broad direct-route fallback that catches StageTimeout
    # starts a second model request instead of reporting the bounded failure.
    async def test_direct_route_timeout_does_not_start_research_fallback(self):
        calls = []

        async def timed_out(**kwargs):
            calls.append(kwargs.get("stage"))
            raise StageTimeout("direct", 0.1)

        message = FakeMessage("간단히 답해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                patch.object(bot, "create_streaming_completion", timed_out), \
                self.recorder.install():
            await bot.on_message(message)

        delivered = "\n".join(message.replies + message.channel.sent)
        self.assertEqual(calls, ["direct"])
        self.assertEqual(self.recorder.reason, outcome_mod.FAILED)
        self.assertIn("마감 초과", delivered)
        self.assertNotIn(CHANNEL_ID, bot.channel_cancel_token)

    async def test_direct_answer_completes_without_a_footer(self):
        await self.drive(
            [_response(content="인증된 상태는 본인 확인이 끝난 상태입니다.")],
            request="인증된 상태라는 게 뭐야?",
        )

        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        self.assertEqual(self.recorder.detail, outcome_mod.DETAIL_DIRECT_ANSWER)
        self.assertNotIn("완료 시간", self.final_reply)
        self.assertNotIn("미완료", self.final_reply)
        self.assertEqual(self.synthesis_calls, [])


class FailureTest(TerminalStateTestCase):
    # Mutation caught: letting an ordinary primary-model error reach the outer
    # handler drops the ledger/summary and sends only an unbounded raw error.
    async def test_agent_exception_uses_bounded_preserved_state_report(self):
        marker = "E_AGENT_UPSTREAM_FAILURE"
        summary = "SUMMARY_BEFORE_AGENT_FAILURE"
        ledger = bot.channel_ledger[CHANNEL_ID]
        ledger.apply_updates({
            "evidence": [{
                "id": marker,
                "summary": "모델 실패 전에 보존된 관측",
                "source": "test://agent-failure",
            }],
        })
        full_ledger = ledger.render()
        bot.channel_summary[CHANNEL_ID] = summary
        stages = []
        failure = "MODEL_FAILURE_PREFIX " + ("x" * 700) + " MODEL_FAILURE_TAIL"

        async def exploding(**kwargs):
            stages.append(kwargs.get("stage"))
            raise RuntimeError(failure)

        message = FakeMessage("조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", exploding), \
                self.recorder.install():
            await bot.on_message(message)

        chunks = message.replies[1:] + message.channel.sent
        delivered = "".join(chunks)
        self.assertEqual(stages, ["agent"])
        self.assertEqual(self.recorder.reason, outcome_mod.FAILED)
        self.assertIn("업스트림 실패", self.recorder.detail)
        self.assertIn("MODEL_FAILURE_PREFIX", delivered)
        self.assertIn("...[생략]", delivered)
        self.assertNotIn("MODEL_FAILURE_TAIL", delivered)
        self.assertIn(marker, delivered)
        self.assertIn(summary, delivered)
        self.assertIn("미완료", delivered)
        self.assertNotIn("작업 도중 예외 발생", delivered)
        self.assertTrue(chunks)
        self.assertLessEqual(len(chunks), 3)
        self.assertLessEqual(sum(len(chunk) for chunk in chunks), 5700)
        self.assertLessEqual(max(len(chunk) for chunk in chunks), 1900)
        self.assertEqual(ledger.render(), full_ledger)

    # Mutation caught: a 400 recovery attempt is still the same bounded model
    # stage. An ordinary retry failure must not escape to the raw outer handler.
    async def test_400_retry_exception_uses_preserved_state_report(self):
        marker = "E_RETRY_UPSTREAM_FAILURE"
        summary = "SUMMARY_BEFORE_RETRY_FAILURE"
        bot.channel_ledger[CHANNEL_ID].apply_updates({
            "evidence": [{
                "id": marker,
                "summary": "재시도 실패 전에 보존된 관측",
                "source": "test://retry-failure",
            }],
        })
        bot.channel_summary[CHANNEL_ID] = summary
        stages = []
        failure = "RETRY_FAILURE_PREFIX " + ("z" * 700) + " RETRY_FAILURE_TAIL"

        async def model(**kwargs):
            stages.append(kwargs.get("stage"))
            if len(stages) == 1:
                raise RuntimeError("400 invalid tool_call_id")
            raise OSError(failure)

        message = FakeMessage("조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", model), \
                self.recorder.install():
            await bot.on_message(message)

        delivered = "".join(message.replies[1:] + message.channel.sent)
        self.assertEqual(stages, ["agent", "agent:retry"])
        self.assertEqual(self.recorder.reason, outcome_mod.FAILED)
        self.assertIn("업스트림 실패", self.recorder.detail)
        self.assertIn("RETRY_FAILURE_PREFIX", delivered)
        self.assertIn("...[생략]", delivered)
        self.assertNotIn("RETRY_FAILURE_TAIL", delivered)
        self.assertIn(marker, delivered)
        self.assertIn(summary, delivered)
        self.assertNotIn("작업 도중 예외 발생", delivered)

    # Mutation caught: an ordinary child-tool error is reaped by the batch
    # helper, but must also settle FAILED at the caller boundary so preserved
    # state is reported without starting synthesis.
    async def test_tool_exception_uses_bounded_preserved_state_report(self):
        marker = "E_TOOL_UPSTREAM_FAILURE"
        summary = "SUMMARY_BEFORE_TOOL_FAILURE"
        ledger = bot.channel_ledger[CHANNEL_ID]
        ledger.apply_updates({
            "evidence": [{
                "id": marker,
                "summary": "도구 실패 전에 보존된 관측",
                "source": "test://tool-failure",
            }],
        })
        full_ledger = ledger.render()
        bot.channel_summary[CHANNEL_ID] = summary
        sibling_started = bot.asyncio.Event()
        sibling_finalized = bot.asyncio.Event()
        release = bot.asyncio.Event()
        stages = []
        failure = "TOOL_FAILURE_PREFIX " + ("y" * 700) + " TOOL_FAILURE_TAIL"

        async def model(**kwargs):
            stages.append(kwargs.get("stage"))
            return _response(tool_calls=[
                _tool_call("c1", "bash_exec", {"command": "fail"}),
                _tool_call("c2", "bash_exec", {"command": "block"}),
            ])

        async def tool(workspace, command):
            if command == "fail":
                await sibling_started.wait()
                raise ValueError(failure)
            sibling_started.set()
            try:
                await release.wait()
            finally:
                sibling_finalized.set()

        message = FakeMessage("조사해줘", CHANNEL_ID)
        try:
            with tempfile.TemporaryDirectory() as log_dir, \
                    run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                    patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                    patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                    patch.object(bot, "create_streaming_completion", model), \
                    patch.object(bot, "tool_bash_exec", tool), \
                    self.recorder.install():
                await bot.on_message(message)
        finally:
            release.set()

        chunks = message.replies[1:] + message.channel.sent
        delivered = "".join(chunks)
        self.assertTrue(sibling_finalized.is_set())
        self.assertEqual(stages, ["agent"])
        self.assertEqual(self.recorder.reason, outcome_mod.FAILED)
        self.assertIn("업스트림 실패", self.recorder.detail)
        self.assertIn("TOOL_FAILURE_PREFIX", delivered)
        self.assertIn("...[생략]", delivered)
        self.assertNotIn("TOOL_FAILURE_TAIL", delivered)
        self.assertIn(marker, delivered)
        self.assertIn(summary, delivered)
        self.assertIn("미완료", delivered)
        self.assertNotIn("작업 도중 예외 발생", delivered)
        self.assertTrue(chunks)
        self.assertLessEqual(len(chunks), 3)
        self.assertLessEqual(sum(len(chunk) for chunk in chunks), 5700)
        self.assertLessEqual(max(len(chunk) for chunk in chunks), 1900)
        self.assertEqual(ledger.render(), full_ledger)


class DeliveryFailureLabelTest(TerminalStateTestCase):
    async def test_completed_run_that_fails_delivery_is_not_labelled_complete(self):
        # Mutation caught: settle() is first-wins, so once a run had settled
        # COMPLETED the exception handler's settle(FAILED) changed nothing and
        # outcome.label still read "✅ 조사 완료" on a message whose whole point was
        # to report that the report never arrived.
        class BrokenReplyMessage(FakeMessage):
            async def reply(self, content):
                handle = await FakeMessage.reply(self, content)
                if len(self.replies) > 1:
                    raise RuntimeError("discord unavailable")
                return handle

        message = BrokenReplyMessage("시스템 상태를 조사해줘", CHANNEL_ID)
        await self.run_agent(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
            ],
            message=message,
        )

        self.assertEqual(self.recorder.reason, outcome_mod.COMPLETED)
        notices = [
            edit
            for handle in message.reply_handles
            for edit in handle.edits
            if edit and "작업 도중 예외 발생" in edit
        ]
        self.assertTrue(notices, "예외 메시지가 사용자에게 전달되지 않았습니다")
        failure_notice = notices[-1]
        self.assertIn(outcome_mod.LABELS[outcome_mod.FAILED], failure_notice)
        self.assertNotIn(outcome_mod.LABELS[outcome_mod.COMPLETED], failure_notice)
        self.assertIn("미완료", failure_notice)


if __name__ == "__main__":
    unittest.main()
