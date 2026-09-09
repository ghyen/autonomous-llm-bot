import unittest
from unittest.mock import patch, AsyncMock
import tempfile
import discord

import test_support  # sets required config env before bot imports
import bot
from bot import (
    has_recent_tool_error,
    resolve_adaptive_reasoning_effort,
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

    def test_tool_error_triggers_low_effort_with_512_cap(self):
        payload = [{"role": "tool", "name": "read_file", "content": '{"status":"error","error":"not_found"}'}]
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("low", 512))

    def test_tool_error_respects_smaller_reasoning_token_ceiling(self):
        payload = [{"role": "tool", "name": "read_file", "content": '{"status":"error","error":"not_found"}'}]
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=256,
        )
        self.assertEqual((effort, tokens), ("low", 256))

    def test_successful_tool_uses_configured_effort(self):
        payload = [{"role": "tool", "name": "bash_exec", "content": "[stdout]\nok\n[exit code: 0]"}]
        # configured high
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("high", 1536))

        # configured medium
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="medium",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=2000,
        )
        self.assertEqual((effort, tokens), ("medium", 1536))

        # configured low
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=1,
            consecutive_internal_thoughts=0,
            configured_effort="low",
            adaptive_enabled=True,
            messages_payload=payload,
            reasoning_max_tokens=1536,
        )
        self.assertEqual((effort, tokens), ("low", 512))


if __name__ == "__main__":
    unittest.main()
