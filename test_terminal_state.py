"""The six scenarios from issue #3, each reaching exactly one terminal reason.

Every identifier here is synthetic.
"""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage

import bot
import outcome as outcome_mod

CHANNEL_ID = 987654600

LONG_REPORT = "조사 결과 정리. " + ("확인됨. " * 80)


def _response(content="", tool_calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content,
        reasoning_content="",
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
            bot.channel_stop_requested,
            bot.channel_active_runs,
            bot.channel_user_queue,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    async def run_agent(self, responses, request="시스템 상태를 조사해줘", max_loops=6,
                        checkpoint_interval=99, compaction_interval=99, stop_after=None,
                        tool_result="[stdout]\nok\n[exit code: 0]"):
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
                bot.channel_stop_requested[CHANNEL_ID] = True
            return script.pop(0) if script else _response(content="계속 진행합니다.")

        self.synthesis_calls = []
        # Kept on self so assertions still reach it after the patch is undone.
        self.bash_exec = AsyncMock(return_value=tool_result)
        message = FakeMessage(request, CHANNEL_ID)

        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
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

    async def test_2_short_completion_text_after_tools_does_not_end_the_run(self):
        """The keyword+length guess is gone. Only finish_task completes a run."""
        await self.drive(
            [
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(content="최종 결론 보고서: " + ("정리 완료. " * 60)),
            ],
            max_loops=12,
        )

        self.assertEqual(len(self.recorder.settled), 1)
        self.assertEqual(self.recorder.reason, outcome_mod.EXHAUSTED)
        self.assertIn(outcome_mod.DETAIL_NO_TOOL_STALL, self.recorder.detail)
        self.assertIn("미완료", self.final_reply)
        self.assertNotIn("완료 시간", self.final_reply)

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
    async def test_an_exception_settles_failed_and_never_claims_completion(self):
        async def exploding(**kwargs):
            raise RuntimeError("backend exploded")

        message = FakeMessage("조사해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, \
                patch.object(bot, "SYSTEM_LOG_DIR", log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                patch.object(bot, "create_streaming_completion", exploding), \
                self.recorder.install():
            await bot.on_message(message)

        self.assertEqual(self.recorder.reason, outcome_mod.FAILED)
        self.assertIn("예외", self.recorder.detail)


if __name__ == "__main__":
    unittest.main()
