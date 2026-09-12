import unittest
from unittest.mock import patch, AsyncMock
import tempfile
import discord

import test_support  # sets required config env before bot imports
import bot
from bot import (
    has_recent_tool_error,
    resolve_adaptive_reasoning_effort,
    resolve_think_tokens,
    tool_think,
    TOOLS_SCHEMA,
)


class HasRecentToolErrorTest(unittest.TestCase):
    def test_empty_or_no_tools(self):
        self.assertFalse(has_recent_tool_error([]))
        self.assertFalse(has_recent_tool_error(None))
        self.assertFalse(has_recent_tool_error([{"role": "user", "content": "hi"}]))
        self.assertFalse(has_recent_tool_error([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}
        ]))

    def test_successful_tools(self):
        payload = [
            {"role": "user", "content": "run task"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash_exec", "arguments": "{}"}}]},
            {"role": "tool", "name": "bash_exec", "content": "[stdout]\nhello\n[exit code: 0]"},
        ]
        self.assertFalse(has_recent_tool_error(payload))

    def test_file_not_found_error(self):
        payload = [
            {"role": "user", "content": "run task"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "name": "read_file", "content": '{"error":"not_found","path":"findings.md","status":"error"}'},
        ]
        self.assertTrue(has_recent_tool_error(payload))

    def test_bash_nonzero_exit_code(self):
        payload = [
            {"role": "user", "content": "run task"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash_exec", "arguments": "{}"}}]},
            {"role": "tool", "name": "bash_exec", "content": "[stderr]\ncommand not found\n[exit code: 127]"},
        ]
        self.assertTrue(has_recent_tool_error(payload))

    def test_batch_with_mixed_results(self):
        payload = [
            {"role": "user", "content": "run task"},
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "name": "read_file", "content": '{"status":"ok","content":"data"}'},
            {"role": "tool", "name": "read_file", "content": '{"error":"not_found","status":"error"}'},
        ]
        self.assertTrue(has_recent_tool_error(payload))

    def test_trailing_user_steering_still_inspects_last_tool_batch(self):
        payload = [
            {"role": "user", "content": "run task"},
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "name": "read_file", "content": '{"error":"not_found","status":"error"}'},
            {"role": "user", "content": "[사용자 실시간 개입] 계속 진행해줘"},
        ]
        self.assertTrue(has_recent_tool_error(payload))

    def test_old_error_superseded_by_recent_success(self):
        payload = [
            {"role": "user", "content": "run task"},
            {"role": "tool", "name": "read_file", "content": '{"error":"not_found","status":"error"}'},
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "name": "bash_exec", "content": "[stdout]\nrecovered\n[exit code: 0]"},
        ]
        self.assertFalse(has_recent_tool_error(payload))


class ResolveThinkTokensTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(resolve_think_tokens("minimal"), 256)
        self.assertEqual(resolve_think_tokens("low"), 512)
        self.assertEqual(resolve_think_tokens("medium"), 1536)
        self.assertEqual(resolve_think_tokens("high"), 4096)

    def test_defaults_and_fallback(self):
        self.assertEqual(resolve_think_tokens("unknown"), 512)
        self.assertEqual(resolve_think_tokens(""), 512)
        self.assertEqual(resolve_think_tokens(None), 512)


class ResolveAdaptiveReasoningEffortTest(unittest.TestCase):
    def test_step_zero_is_always_none(self):
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=0,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=None,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("none", None))

    def test_consecutive_internal_thoughts_forces_none(self):
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=2,
            consecutive_internal_thoughts=1,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=None,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("none", None))

    def test_configured_none_is_always_none(self):
        payload = [{"role": "tool", "name": "read_file", "content": '{"status":"error"}'}]
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="none",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("none", None))

    def test_adaptive_disabled_preserves_configuration(self):
        payload = [{"role": "tool", "name": "read_file", "content": '{"status":"error"}'}]
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=False,
            messages_payload=payload,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("high", 1536))

    def test_pending_think_effort_overrides_to_requested_effort_and_tokens(self):
        payload = [{"role": "tool", "name": "bash_exec", "content": "[stdout]\nok\n[exit code: 0]"}]
        for tier, expected_tokens in [
            ("minimal", 256),
            ("low", 512),
            ("medium", 1536),
            ("high", 4096),
        ]:
            effort, tokens = resolve_adaptive_reasoning_effort(
                iteration=1,
                consecutive_internal_thoughts=0,
                configured_effort="high",
                adaptive_enabled=True,
                messages_payload=payload,
                reasoning_max_tokens=1536,
                pending_think_effort=tier,
            )
            self.assertEqual((effort, tokens), (tier, expected_tokens))

    def test_pending_think_effort_overrides_even_when_configured_none(self):
        payload = [{"role": "tool", "name": "bash_exec", "content": "[stdout]\nok\n[exit code: 0]"}]
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="none",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=1536,
            pending_think_effort="high",
        )
        self.assertEqual((effort, tokens), ("high", 4096))

    def test_normal_tool_execution_defaults_to_none_to_prevent_hang(self):
        payload = [{"role": "tool", "name": "bash_exec", "content": "[stdout]\nok\n[exit code: 0]"}]
        # Normal step without pending think effort defaults to 'none' for fast tool execution
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("none", None))


class ThinkToolSchemaAndExecutionTest(unittest.IsolatedAsyncioTestCase):
    def test_think_tool_in_tools_schema(self):
        think_tool = next((t for t in TOOLS_SCHEMA if t.get("function", {}).get("name") == "think"), None)
        self.assertIsNotNone(think_tool)
        func = think_tool["function"]
        self.assertIn("focus", func["parameters"]["properties"])
        self.assertIn("effort", func["parameters"]["properties"])
        self.assertEqual(func["parameters"]["properties"]["effort"]["enum"], ["minimal", "low", "medium", "high"])
        self.assertIn("focus", func["parameters"]["required"])

    async def test_tool_think_execution(self):
        with tempfile.TemporaryDirectory() as td:
            res = await tool_think(td, focus="로그 에러 원인 규명", effort="medium")
            self.assertIn("심층 사고 모드 예약 완료", res)
            self.assertIn("로그 에러 원인 규명", res)
            self.assertIn("medium", res)

            # fallback to low on invalid effort
            res_invalid = await tool_think(td, focus="가설 검토", effort="invalid_tier")
            self.assertIn("low", res_invalid)


if __name__ == "__main__":
    unittest.main()
