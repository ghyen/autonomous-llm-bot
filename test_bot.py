import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage, run_catalog_patch  # sets required config env before bot imports

import bot


class RoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_free_answer_finishes_after_one_model_call(self):
        channel_id = 987654321
        message = FakeMessage("인증된 상태라는 게 뭐야?", channel_id)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="<think>이 설명은 짧아야 한다.</think>\n인증된 상태는 본인 확인이 끝난 상태입니다.",
            reasoning_content="사용자는 짧은 답을 원한다.",
            reasoning="",
            tool_calls=[],
        ))])

        bot.FREE_RESPONSE_CHANNEL_IDS.add(channel_id)
        try:
            with tempfile.TemporaryDirectory() as log_dir, \
                    run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "MAX_AGENT_LOOPS", 2), \
                    patch.object(bot, "create_streaming_completion", AsyncMock(return_value=response)) as completion, \
                    patch.object(bot, "execute_tools_in_parallel", AsyncMock()) as execute_tools:
                await bot.on_message(message)

            self.assertEqual(completion.await_count, 1)
            execute_tools.assert_not_awaited()
            completion_args = completion.await_args.kwargs
            self.assertEqual(completion_args["tool_choice"], "auto")
            self.assertEqual(completion_args["reasoning_effort"], "none")
            self.assertEqual(completion_args["max_tokens"], 1024)
            self.assertEqual(
                message.replies[-1],
                "인증된 상태는 본인 확인이 끝난 상태입니다.",
            )
        finally:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_cancel_token,
                bot.channel_active_runs,
            ):
                state.pop(channel_id, None)

    async def test_explicit_brief_request_uses_tool_free_call(self):
        channel_id = 987654322
        message = FakeMessage("인증된 상태라는게 뭔데? 간단히 답해줘", channel_id)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="<think>짧게 답한다.</think>\n인증된 상태는 본인 확인이 끝난 상태입니다.",
            reasoning_content="",
            reasoning="",
            tool_calls=[],
        ))])

        bot.FREE_RESPONSE_CHANNEL_IDS.add(channel_id)
        try:
            with tempfile.TemporaryDirectory() as log_dir, \
                    run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "create_streaming_completion", AsyncMock(return_value=response)) as completion, \
                    patch.object(bot, "execute_tools_in_parallel", AsyncMock()) as execute_tools:
                await bot.on_message(message)

            self.assertEqual(completion.await_count, 1)
            execute_tools.assert_not_awaited()
            completion_args = completion.await_args.kwargs
            self.assertNotIn("tools", completion_args)
            self.assertEqual(completion_args["reasoning_effort"], "none")
            self.assertEqual(completion_args["max_tokens"], 512)
            self.assertEqual(
                message.replies[-1],
                "인증된 상태는 본인 확인이 끝난 상태입니다.",
            )
        finally:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_cancel_token,
                bot.channel_active_runs,
            ):
                state.pop(channel_id, None)

    async def test_research_continues_after_a_progress_message(self):
        channel_id = 987654323
        message = FakeMessage("간단히 말하지 말고 현재 시스템 상태를 조사해줘", channel_id)
        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="",
                reasoning="",
                tool_calls=[SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(name="bash_exec", arguments='{"command":"true"}'),
                )],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="조사를 계속하겠습니다.",
                reasoning_content="",
                reasoning="",
                tool_calls=[],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="",
                reasoning="",
                tool_calls=[SimpleNamespace(
                    id="call_2",
                    function=SimpleNamespace(
                        name="finish_task",
                        arguments=json.dumps(
                            {"report": "조사 완료 " + ("확인됨. " * 50)},
                            ensure_ascii=False,
                        ),
                    ),
                )],
            ))]),
        ]

        bot.FREE_RESPONSE_CHANNEL_IDS.add(channel_id)
        try:
            with tempfile.TemporaryDirectory() as log_dir, \
                    run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                    patch.object(bot, "create_streaming_completion", AsyncMock(side_effect=responses)) as completion, \
                    patch.object(bot, "execute_tools_in_parallel", AsyncMock(return_value=["ok"])) as execute_tools:
                await bot.on_message(message)

            self.assertEqual(completion.await_count, 3)
            execute_tools.assert_awaited_once()
            self.assertIn("조사 완료", message.replies[-1])
        finally:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_cancel_token,
                bot.channel_active_runs,
            ):
                state.pop(channel_id, None)

    async def test_empty_first_response_retries(self):
        channel_id = 987654324
        message = FakeMessage("간단히 답해줘", channel_id)
        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="",
                reasoning="",
                tool_calls=[],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="조사를 계속하겠습니다.",
                reasoning_content="",
                reasoning="",
                tool_calls=[],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="",
                reasoning="",
                tool_calls=[SimpleNamespace(
                    id="call_3",
                    function=SimpleNamespace(
                        name="finish_task",
                        arguments=json.dumps(
                            {"report": "답변 완료 " + ("확인됨. " * 50)},
                            ensure_ascii=False,
                        ),
                    ),
                )],
            ))]),
        ]

        bot.FREE_RESPONSE_CHANNEL_IDS.add(channel_id)
        try:
            with tempfile.TemporaryDirectory() as log_dir, \
                    run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                    patch.object(bot, "create_streaming_completion", AsyncMock(side_effect=responses)) as completion:
                await bot.on_message(message)

            self.assertEqual(completion.await_count, 3)
            self.assertNotIn("tools", completion.await_args_list[0].kwargs)
            self.assertIn("tools", completion.await_args_list[1].kwargs)
            self.assertIn("답변 완료", message.replies[-1])
        finally:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_cancel_token,
                bot.channel_active_runs,
            ):
                state.pop(channel_id, None)




class ReasoningThreadTimelineTest(unittest.IsolatedAsyncioTestCase):
    def test_extract_thought_monologue_from_reasoning(self):
        thought = bot.extract_thought_monologue("디렉토리 구조를 확인한 후 git log를 조회하자.", "")
        self.assertEqual(thought, "디렉토리 구조를 확인한 후 git log를 조회하자.")

    def test_extract_thought_monologue_from_think_tags(self):
        content = "<think>포트 점유 상태를 파악해야 한다.</think>확인 중..."
        thought = bot.extract_thought_monologue("", content)
        self.assertEqual(thought, "포트 점유 상태를 파악해야 한다.")

    async def test_send_thought_to_thread_chunks_long_text(self):
        class FakeThread:
            def __init__(self):
                self.sent = []
            async def send(self, content):
                self.sent.append(content)

        thread = FakeThread()
        long_thought = "A" * 3700
        await bot.send_thought_to_thread(thread, 1, long_thought)
        self.assertEqual(len(thread.sent), 3)
        self.assertTrue(all("```text" in msg for msg in thread.sent))

    async def test_send_thought_to_thread_escapes_triple_backticks(self):
        class FakeThread:
            def __init__(self):
                self.sent = []
            async def send(self, content):
                self.sent.append(content)

        thread = FakeThread()
        await bot.send_thought_to_thread(thread, 2, "코드 블록 ```python print(1) ``` 검토")
        self.assertEqual(len(thread.sent), 1)
        self.assertNotIn("```python", thread.sent[0])
        self.assertIn("'''", thread.sent[0])
    async def test_on_message_creates_thread_and_streams_monologue(self):
        class FakeThreadTarget:
            def __init__(self, thread_id=999111):
                self.id = thread_id
                self.sent = []
            async def send(self, content):
                self.sent.append(content)

        class MessageWithThread(FakeMessage):
            def __init__(self, content, channel_id):
                super().__init__(content, channel_id)
                self.thread_instance = FakeThreadTarget()
            async def create_thread(self, name="", auto_archive_duration=60):
                return self.thread_instance

        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="",
                reasoning="",
                tool_calls=[SimpleNamespace(
                    id="call_0",
                    function=SimpleNamespace(name="bash_exec", arguments='{"command":"true"}'),
                )],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="상세한 1단계 추론 독백입니다.",
                reasoning="",
                tool_calls=[SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(name="bash_exec", arguments='{"command":"true"}'),
                )],
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="2단계 판단 독백입니다.",
                reasoning_content="",
                reasoning="",
                tool_calls=[SimpleNamespace(
                    id="call_2",
                    function=SimpleNamespace(
                        name="finish_task",
                        arguments=json.dumps(
                            {"report": "조사 완료 " + ("확인됨. " * 50)},
                            ensure_ascii=False,
                        ),
                    ),
                )],
            ))]),
        ]

        channel_id = 987654321
        message = MessageWithThread("시스템 분석해줘", channel_id)
        bot.FREE_RESPONSE_CHANNEL_IDS.add(channel_id)
        try:
            with tempfile.TemporaryDirectory() as log_dir, \
                    run_catalog_patch(bot, log_dir), \
                    patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                    patch.object(bot, "create_streaming_completion", AsyncMock(side_effect=responses)), \
                    patch.object(bot, "execute_tools_in_parallel", AsyncMock(return_value=["ok"])):
                await bot.on_message(message)

            self.assertTrue(len(message.thread_instance.sent) >= 2)
            self.assertIn("상세한 1단계 추론 독백입니다.", message.thread_instance.sent[0])
            self.assertIn("2단계 판단 독백입니다.", message.thread_instance.sent[1])
            self.assertIn("탐색 완료", message.thread_instance.sent[-1])
        finally:
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_cancel_token,
                bot.channel_active_runs,
            ):
                state.pop(channel_id, None)


if __name__ == "__main__":
    unittest.main()
