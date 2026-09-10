"""Token-aware agent context budget tests."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage

import bot


TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash_exec",
        "description": "run shell",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]


def _messages(group_count=3):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "goal"},
    ]
    for step in range(1, group_count + 1):
        call_id = f"call-{step}"
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "bash_exec",
                    "arguments": json.dumps({"command": f"printf {step}"}),
                },
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": "bash_exec",
            "content": f"result-{step}",
        })
    return messages


class TokenCountPayloadTest(unittest.TestCase):
    def test_count_payload_preserves_tool_protocol(self):
        payload = bot.build_token_count_payload(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "goal"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash_exec",
                            "arguments": '{"command":"printf ok"}',
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "bash_exec",
                    "content": "[stdout] ok",
                },
            ],
            {"tools": TOOLS},
        )

        self.assertEqual(payload["system"], "system")
        self.assertEqual(payload["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(payload["messages"][1]["content"][0]["id"], "call-1")
        self.assertEqual(
            payload["messages"][1]["content"][0]["input"],
            {"command": "printf ok"},
        )
        self.assertEqual(
            payload["messages"][2]["content"][0]["tool_use_id"], "call-1"
        )
        self.assertEqual(payload["messages"][2]["content"][0]["content"], "[stdout] ok")
        self.assertEqual(payload["tools"][0]["name"], "bash_exec")
        self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")

    def test_count_payload_groups_consecutive_tool_results(self):
        messages = [
            {"role": "user", "content": "goal"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "bash_exec", "arguments": "{}"},
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {"name": "bash_exec", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "one"},
            {"role": "tool", "tool_call_id": "call-2", "content": "two"},
        ]

        payload = bot.build_token_count_payload(messages, {"tools": TOOLS})

        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual(payload["messages"][2]["role"], "user")
        self.assertEqual(
            [block["tool_use_id"] for block in payload["messages"][2]["content"]],
            ["call-1", "call-2"],
        )

    def test_fallback_estimate_includes_serialized_tools_and_arguments(self):
        with_tools = bot.approximate_agent_input_tokens(_messages(1), {"tools": TOOLS})
        without_tools = bot.approximate_agent_input_tokens(_messages(1), {"tools": []})
        unicode_result = bot.approximate_agent_input_tokens(
            [
                {"role": "user", "content": "목표"},
                {"role": "tool", "tool_call_id": "call-1", "content": "가" * 20},
            ],
            {"tools": []},
        )

        self.assertGreater(with_tools, without_tools)
        self.assertGreater(unicode_result, bot.approximate_agent_input_tokens(
            [
                {"role": "user", "content": "goal"},
                {"role": "tool", "tool_call_id": "call-1", "content": "a" * 20},
            ],
            {"tools": []},
        ))

    def test_count_request_uses_configured_endpoint_and_response(self):
        calls = []

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"input_tokens": 321}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response()

        with patch.object(bot.httpx, "AsyncClient", return_value=Client()):
            count = self._run(bot.count_agent_input_tokens(_messages(1), {"tools": TOOLS}))

        self.assertEqual(count, 321)
        self.assertEqual(calls[0][0], bot.LLM_BASE_URL.rstrip("/") + "/messages/count_tokens")
        self.assertEqual(calls[0][1]["json"]["model"], bot.MODEL_NAME)

    @staticmethod
    def _run(awaitable):
        import asyncio

        return asyncio.run(awaitable)


class ContextPreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_over_budget_rolls_once_then_trims_complete_groups(self):
        messages = _messages(3)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=tmp)
            with patch.object(
                bot,
                "count_agent_input_tokens",
                AsyncMock(side_effect=[9000, 9000, 4000]),
            ), patch.object(
                bot,
                "rollover_agent_context",
                AsyncMock(return_value=(messages, "summary")),
            ) as rollover:
                result = await bot.prepare_agent_request_payload(
                    workspace,
                    messages,
                    "",
                    12,
                    4096,
                    {"tools": TOOLS},
                )

        self.assertEqual(result.input_budget, 4864)
        rollover.assert_awaited_once()
        self.assertEqual(result.input_tokens, 4000)
        self.assertTrue(bot.validate_chat_payload(result.messages).ok)
        self.assertEqual(
            len([m for m in result.messages if bot._msg_role(m) == "tool"]),
            1,
        )
        self.assertEqual(result.summary, "summary")
        self.assertEqual(result.trim_passes, 1)

    async def test_over_budget_fails_closed_after_all_trim_passes(self):
        messages = _messages(3)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=tmp)
            with patch.object(
                bot,
                "count_agent_input_tokens",
                AsyncMock(return_value=9000),
            ), patch.object(
                bot,
                "rollover_agent_context",
                AsyncMock(return_value=(messages, "summary")),
            ):
                with self.assertRaises(bot.AgentContextBudgetExceeded):
                    await bot.prepare_agent_request_payload(
                        workspace,
                        messages,
                        "",
                        12,
                        4096,
                        {"tools": TOOLS},
                    )

    async def test_unavailable_counter_uses_conservative_fallback(self):
        messages = _messages(1)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=tmp)
            with patch.object(
                bot,
                "count_agent_input_tokens",
                AsyncMock(return_value=None),
            ):
                result = await bot.prepare_agent_request_payload(
                    workspace,
                    messages,
                    "",
                    1,
                    4096,
                    {"tools": TOOLS},
                )

        self.assertTrue(result.count_fallback)
        self.assertIsInstance(result.input_tokens, int)
        self.assertLessEqual(result.input_tokens, result.input_budget)

    async def test_unavailable_counter_keeps_base_prompt_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=tmp)
            messages = [
                {
                    "role": "system",
                    "content": bot.build_system_content(workspace),
                },
                {"role": "user", "content": "시스템 상태를 조사해줘"},
            ]
            with patch.object(
                bot,
                "count_agent_input_tokens",
                AsyncMock(return_value=None),
            ):
                result = await bot.prepare_agent_request_payload(
                    workspace,
                    messages,
                    "",
                    1,
                    2048,
                    bot.agent_tool_params(),
                )

        self.assertTrue(result.count_fallback)
        self.assertLessEqual(result.input_tokens, result.input_budget)


class LaunchConfigurationTest(unittest.TestCase):
    def test_omlx_runs_one_request_at_a_time(self):
        tokens = Path(__file__).parent.joinpath("scripts", "run_omlx.sh").read_text().split()
        index = tokens.index("--max-concurrent-requests")
        self.assertEqual(tokens[index + 1], "1")
        self.assertEqual(bot.KEEP_RECENT_TOOL_GROUPS, 2)


if __name__ == "__main__":
    unittest.main()
