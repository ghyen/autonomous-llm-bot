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


class StateUpdateBlockParsingTest(unittest.TestCase):
    def test_parse_complete_state_update_block(self):
        text = """### 1. 완료 작업\n- 작업 진행 완료\n\n```state_update\n{\n  "goal": "조사 목표",\n  "evidence": [{"id": "e1", "description": "증거 1", "source": "bash"}]\n}\n```\n\n추가 보고 내용"""
        updates, cleaned = bot.parse_state_update_blocks(text)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["goal"], "조사 목표")
        self.assertNotIn("```state_update", cleaned)
        self.assertNotIn("증거 1", cleaned)
        self.assertIn("### 1. 완료 작업", cleaned)
        self.assertIn("추가 보고 내용", cleaned)

    def test_strip_truncated_unclosed_state_update_block(self):
        text = """### 1. 완료 작업\n- 20스텝 도달\n\n```state_update\n{\n  "goal": "조사 목표",\n  "evidence": [\n    {"id": "e1", "description": "미완성된 잘린 상태 업데이트"""
        updates, cleaned = bot.parse_state_update_blocks(text)
        # 잘린 불완전 JSON은 파싱되지 않지만, 디스코드 보고서 본문에서는 완전히 제거되어야 함
        self.assertEqual(len(updates), 0)
        self.assertNotIn("```state_update", cleaned)
        self.assertNotIn("미완성된 잘린 상태 업데이트", cleaned)
        self.assertEqual(cleaned, "### 1. 완료 작업\n- 20스텝 도달")



class RobustJSONParsingTest(unittest.TestCase):
    def test_robust_json_loads_markdown_fences(self):
        text = "```json\n{\"name\": \"bash_exec\", \"arguments\": {\"command\": \"ls\"}}\n```"
        parsed = bot._robust_json_loads(text)
        self.assertEqual(parsed, {"name": "bash_exec", "arguments": {"command": "ls"}})

    def test_robust_json_loads_trailing_commas(self):
        text = '{"a": 1, "b": [2, 3,], "c": {"d": 4,},}'
        parsed = bot._robust_json_loads(text)
        self.assertEqual(parsed, {"a": 1, "b": [2, 3], "c": {"d": 4}})

    def test_robust_json_loads_unescaped_newlines(self):
        text = '{"command": "echo line1\necho line2"}'
        parsed = bot._robust_json_loads(text)
        self.assertIsNotNone(parsed)
        self.assertIn("echo line1", parsed["command"])

    def test_extract_tool_calls_with_markdown_fence(self):
        text = """<tool_call>
```json
{
  "name": "bash_exec",
  "arguments": {
    "command": "ls -la"
  }
}
```
</tool_call>"""
        extracted = bot.extract_tool_calls_from_text(text)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["name"], "bash_exec")
        self.assertEqual(extracted[0]["arguments"], {"command": "ls -la"})

    def test_extract_tool_calls_with_string_arguments(self):
        text = r'<tool_call>{"name": "read_file", "arguments": "{\"path\": \"workspace/a.txt\"}"}</tool_call>'
        extracted = bot.extract_tool_calls_from_text(text)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["name"], "read_file")
        self.assertEqual(extracted[0]["arguments"], {"path": "workspace/a.txt"})


class MarkdownChunkingTest(unittest.TestCase):
    def test_split_short_text_returns_single_chunk(self):
        text = "Hello world! This is a short response."
        chunks = bot.split_markdown_chunks(text, max_chars=100)
        self.assertEqual(chunks, [text])

    def test_split_preserves_and_balances_code_fences(self):
        long_code = "```python\n" + "\n".join(f"print({i})" for i in range(200)) + "\n```"
        chunks = bot.split_markdown_chunks(long_code, max_chars=400)
        self.assertGreater(len(chunks), 1)
        for i, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk), 400)
            self.assertEqual(
                chunk.count("```") % 2,
                0,
                f"Chunk {i} has unbalanced code fence:\n{chunk}",
            )
            if i > 0:
                self.assertTrue(chunk.startswith("```python\n"))
            self.assertTrue(chunk.endswith("\n```"))

    def test_split_oversized_line_within_code_fence(self):
        huge_line = "x = '" + ("a" * 1500) + "'"
        text = f"```python\n{huge_line}\n```"
        chunks = bot.split_markdown_chunks(text, max_chars=500)
        self.assertGreater(len(chunks), 2)
        for i, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk), 500)
            self.assertEqual(chunk.count("```") % 2, 0)

    def test_split_unclosed_fence_is_auto_closed(self):
        text = "```bash\necho hello\necho world"
        chunks = bot.split_markdown_chunks(text, max_chars=1000)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].endswith("\n```"))
        self.assertEqual(chunks[0].count("```") % 2, 0)

    def test_get_open_code_fence(self):
        self.assertIsNone(bot.get_open_code_fence("hello world"))
        self.assertIsNone(bot.get_open_code_fence("```python\nx = 1\n```"))
        self.assertEqual(bot.get_open_code_fence("```python\nx = 1"), "```python")
        self.assertEqual(bot.get_open_code_fence("~~~json\n{}"), "~~~json")
        self.assertIsNone(bot.get_open_code_fence("Inline `code` and ```one line```"))

    def test_bound_local_fallback_output_preserves_fences(self):
        long_report = (
            "## 조사 종료 상태\n- 실패\n\n"
            "## 최근 실행 기록\n```bash\n"
            + "\n".join(f"cmd_{i} output result line {i}" for i in range(500))
            + "\n```\n\n## 다음 단계\n확인 필요"
        )
        self.assertGreater(len(long_report), bot.LOCAL_FALLBACK_MAX_CHARS)
        bounded = bot.bound_local_fallback_output(long_report)
        self.assertLessEqual(len(bounded), bot.LOCAL_FALLBACK_MAX_CHARS)
        self.assertIn(bot.LOCAL_FALLBACK_OMISSION_MARKER, bounded)

        # Pre-marker and post-marker should both have balanced fences
        parts = bounded.split(bot.LOCAL_FALLBACK_OMISSION_MARKER)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].count("```") % 2, 0)
        self.assertEqual(parts[1].count("```") % 2, 0)

        # Chunks produced from bounded output should never exceed Discord limit
        chunks = bot.split_markdown_chunks(bounded, max_chars=bot.DISCORD_CHUNK_MAX_CHARS)
        self.assertLessEqual(len(chunks), bot.LOCAL_FALLBACK_MAX_CHUNKS)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), bot.DISCORD_CHUNK_MAX_CHARS)
            self.assertEqual(chunk.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()

