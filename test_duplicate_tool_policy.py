"""Issue #8 pre-dispatch duplicate-tool and execution-budget behavior."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage

import bot


CHANNEL_ID = 987654800
LONG_REPORT = "중복 도구 제어 확인 완료. " + ("확인됨. " * 80)
SUCCESS_RESULT = "[stdout]\nok\n[exit code: 0]"
BLOCK_DIRECTIVE = "Change the arguments or use a different approach before retrying."


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
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )


class ModelStub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.agent_payloads = []

    async def __call__(self, **kwargs):
        messages = kwargs.get("messages") or []
        system = bot._msg_content(messages[0]) if messages else ""
        if any(marker in system for marker in ("수석 분석가", "AI 리포터", "컨텍스트 압축기")):
            return _response(content=LONG_REPORT)
        self.agent_payloads.append(messages)
        return self.responses.pop(0)


class DuplicateToolPolicyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._reset_channel_state()
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)

    def tearDown(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(CHANNEL_ID)
        self._reset_channel_state()

    def _reset_channel_state(self):
        bot.channel_run_owner.pop(CHANNEL_ID, None)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_run_leases,
            bot.channel_active_runs,
            bot.channel_user_queue,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    async def run_agent(self, responses, tool_result=SUCCESS_RESULT, run_budget=None):
        self.executed_commands = []
        self.dispatched_batches = []
        self.model = ModelStub(responses)
        original_dispatch = bot.execute_tools_in_parallel

        async def recording_bash(command):
            self.executed_commands.append(command)
            if callable(tool_result):
                return tool_result(command, len(self.executed_commands))
            return tool_result

        async def recording_dispatch(tool_calls, *args, **kwargs):
            self.dispatched_batches.append([
                (call["id"], call["name"], call["arguments"])
                for call in tool_calls
            ])
            return await original_dispatch(tool_calls, *args, **kwargs)

        message = FakeMessage("중복 도구 실행을 점검해줘", CHANNEL_ID)
        self.reply_messages = []
        original_reply = message.reply

        async def recording_reply(content):
            sent = await original_reply(content)
            self.reply_messages.append(sent)
            return sent

        message.reply = recording_reply
        with tempfile.TemporaryDirectory() as log_dir, ExitStack() as stack:
            stack.enter_context(patch.object(bot, "SYSTEM_LOG_DIR", log_dir))
            stack.enter_context(patch.object(bot, "MAX_AGENT_LOOPS", len(responses) + 1))
            stack.enter_context(patch.object(bot, "CHECKPOINT_INTERVAL", 99))
            stack.enter_context(patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99))
            stack.enter_context(patch.object(bot, "tool_bash_exec", recording_bash))
            stack.enter_context(patch.object(bot, "execute_tools_in_parallel", recording_dispatch))
            stack.enter_context(patch.object(bot, "create_streaming_completion", self.model))
            if run_budget is not None:
                stack.enter_context(patch.object(
                    bot,
                    "MAX_TOOL_EXECUTIONS_PER_RUN",
                    run_budget,
                    create=True,
                ))
            await bot.on_message(message)
        return message

    @staticmethod
    def _assistant_call_ids(payload):
        assistant = next(
            message for message in payload
            if bot._msg_role(message) == "assistant" and message.get("tool_calls")
        )
        return [call["id"] for call in assistant["tool_calls"]]

    @staticmethod
    def _tool_messages(payload):
        return [message for message in payload if bot._msg_role(message) == "tool"]

    # Mutation caught: dispatching the original batch without filtering exact
    # duplicate signatures performs the same side effect twice.
    async def test_same_batch_exact_duplicate_executes_once_and_reports_each_id(self):
        await self.run_agent([
            _response(tool_calls=[
                _tool_call("exact-1", "bash_exec", {"command": "printf exact"}),
                _tool_call("exact-2", "bash_exec", {"command": "printf exact"}),
            ]),
            _response(tool_calls=[
                _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(self.executed_commands, ["printf exact"])
        self.assertEqual(
            [[call_id for call_id, _name, _arguments in batch]
             for batch in self.dispatched_batches],
            [["exact-1"]],
        )

        next_payload = self.model.agent_payloads[1]
        self.assertEqual(self._assistant_call_ids(next_payload), ["exact-1", "exact-2"])
        tool_messages = self._tool_messages(next_payload)
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["exact-1", "exact-2"],
        )
        self.assertEqual(tool_messages[0]["content"], SUCCESS_RESULT)

        blocked = {
            "blocked": True,
            "count": 1,
            "directive": BLOCK_DIRECTIVE,
            "limit": 1,
            "reason": "same_batch_duplicate",
            "tool": "bash_exec",
        }
        self.assertEqual(json.loads(tool_messages[1]["content"]), blocked)
        self.assertEqual(
            tool_messages[1]["content"],
            json.dumps(blocked, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    # Mutation caught: canonicalizing arguments without sorted keys treats the
    # same JSON object in a different key order as a second side effect.
    async def test_same_batch_key_order_variant_is_the_same_signature(self):
        await self.run_agent([
            _response(tool_calls=[
                _tool_call("ordered-1", "bash_exec", {
                    "command": "printf key-order",
                    "options": {"beta": 2, "alpha": "한글"},
                }),
                _tool_call("ordered-2", "bash_exec", {
                    "options": {"alpha": "한글", "beta": 2},
                    "command": "printf key-order",
                }),
            ]),
            _response(tool_calls=[
                _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(self.executed_commands, ["printf key-order"])
        self.assertEqual(len(self.dispatched_batches), 1)
        self.assertEqual([call[0] for call in self.dispatched_batches[0]], ["ordered-1"])

        next_payload = self.model.agent_payloads[1]
        self.assertEqual(self._assistant_call_ids(next_payload), ["ordered-1", "ordered-2"])
        tool_messages = self._tool_messages(next_payload)
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["ordered-1", "ordered-2"],
        )
        blocked = json.loads(tool_messages[1]["content"])
        self.assertEqual(blocked["reason"], "same_batch_duplicate")
        self.assertEqual(blocked["tool"], "bash_exec")
        self.assertTrue(blocked["blocked"])
        self.assertIn("arguments", blocked["directive"].lower())
        self.assertIn("approach", blocked["directive"].lower())

    # Mutation caught: checking repeated failures only after dispatch lets the
    # immediately consecutive third unchanged call perform a third side effect.
    async def test_third_consecutive_failed_call_is_blocked_before_dispatch(self):
        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"failure-{attempt}", "bash_exec", {"command": "always-fail"}),
                ])
                for attempt in range(1, 4)
            ] + [
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result="[Error: synthetic failure]",
        )

        self.assertEqual(self.executed_commands, ["always-fail", "always-fail"])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["failure-1"], ["failure-2"]],
        )

        feedback_payload = self.model.agent_payloads[3]
        tool_messages = self._tool_messages(feedback_payload)
        self.assertEqual(tool_messages[-1]["tool_call_id"], "failure-3")
        blocked = json.loads(tool_messages[-1]["content"])
        self.assertEqual(blocked["reason"], "consecutive_failure_limit")
        self.assertEqual(blocked["tool"], "bash_exec")
        self.assertEqual(blocked["limit"], 2)
        self.assertEqual(blocked["count"], 2)
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["directive"], BLOCK_DIRECTIVE)

    # Mutation caught: treating a nonzero bash exit as success leaves the old A
    # failure streak adjacent across B and falsely blocks the final A in A,B,A.
    async def test_different_dispatched_signature_breaks_failure_adjacency(self):
        def result_for(command, _execution_count):
            if command == "A":
                return "[Error: A failed]"
            return "[stderr]\nB failed\n[exit code: 7]"

        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call("a-1", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("a-2", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("b-1", "bash_exec", {"command": "B"}),
                ]),
                _response(tool_calls=[
                    _tool_call("a-3", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result=result_for,
        )

        self.assertEqual(self.executed_commands, ["A", "A", "B", "A"])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["a-1"], ["a-2"], ["b-1"], ["a-3"]],
        )
        feedback_payload = self.model.agent_payloads[4]
        latest_tool = self._tool_messages(feedback_payload)[-1]
        self.assertEqual(latest_tool["tool_call_id"], "a-3")
        self.assertEqual(latest_tool["content"], "[Error: A failed]")

    # Mutation caught: evaluating a whole response against stale prior-batch
    # failure state blocks A in same-response [B, A] even though B dispatches.
    async def test_different_call_in_same_batch_breaks_prior_failure_adjacency(self):
        def result_for(command, _execution_count):
            return f"[Error: {command} failed]"

        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call("same-response-a-1", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("same-response-a-2", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("same-response-b", "bash_exec", {"command": "B"}),
                    _tool_call("same-response-a-3", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result=result_for,
        )

        self.assertEqual(self.executed_commands, ["A", "A", "B", "A"])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [
                ["same-response-a-1"],
                ["same-response-a-2"],
                ["same-response-b", "same-response-a-3"],
            ],
        )
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "same-response-a-3")
        self.assertEqual(latest_tool["content"], "[Error: A failed]")

    # Mutation caught: failing to clear adjacency after an explicit successful
    # result carries an old failure across the success and blocks a later retry.
    async def test_successful_repeat_resets_the_failure_streak(self):
        results = {
            1: "[Error: first failure]",
            2: "[stdout]\nrecovered\n[exit code: 0]",
            3: "[Error: second failure after recovery]",
            4: "[Error: allowed second consecutive failure]",
        }

        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"success-reset-{attempt}", "bash_exec", {"command": "recovering-A"}),
                ])
                for attempt in range(1, 5)
            ] + [
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result=lambda _command, execution_count: results[execution_count],
        )

        self.assertEqual(self.executed_commands, ["recovering-A"] * 4)
        self.assertEqual(len(self.dispatched_batches), 4)
        latest_tool = self._tool_messages(self.model.agent_payloads[4])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "success-reset-4")
        self.assertEqual(latest_tool["content"], results[4])

    # Mutation caught: inferring failure from arbitrary prose containing error
    # words prevents that successful prose result from resetting adjacency.
    async def test_arbitrary_failure_words_are_not_a_failure_contract(self):
        results = {
            1: "[Error: first failure]",
            2: "The prior operation failed, but this successful result recovered it.",
            3: "[Error: failure after prose success]",
            4: "[Error: allowed second failure after prose success]",
        }

        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"prose-{attempt}", "bash_exec", {"command": "prose-A"}),
                ])
                for attempt in range(1, 5)
            ] + [
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result=lambda _command, execution_count: results[execution_count],
        )

        self.assertEqual(self.executed_commands, ["prose-A"] * 4)
        self.assertEqual(len(self.dispatched_batches), 4)
        latest_tool = self._tool_messages(self.model.agent_payloads[4])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "prose-4")
        self.assertEqual(latest_tool["content"], results[4])

    # Mutation caught: reading the first exit-code-looking stdout line instead
    # of the final bash status classifies a successful repeat as failed.
    async def test_bash_stdout_cannot_impersonate_the_final_exit_code(self):
        successful_result = "[stdout]\n[exit code: 7]\n[exit code: 0]"
        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"stdout-marker-{attempt}", "bash_exec", {"command": "marker-A"}),
                ])
                for attempt in range(1, 4)
            ] + [
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result=successful_result,
        )

        self.assertEqual(self.executed_commands, ["marker-A"] * 3)
        self.assertEqual(len(self.dispatched_batches), 3)
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "stdout-marker-3")
        self.assertEqual(latest_tool["content"], successful_result)

    # Mutation caught: treating the direct record_state refusal report as a
    # success allows an unchanged third refused update to dispatch again.
    async def test_direct_record_state_refusal_counts_as_failure(self):
        await self.run_agent([
            *[
                _response(tool_calls=[
                    _tool_call(f"record-direct-{attempt}", "record_state", []),
                ])
                for attempt in range(1, 4)
            ],
            _response(tool_calls=[
                _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(self.executed_commands, [])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["record-direct-1"], ["record-direct-2"]],
        )
        feedback_payload = self.model.agent_payloads[3]
        latest_tool = self._tool_messages(feedback_payload)[-1]
        self.assertEqual(latest_tool["tool_call_id"], "record-direct-3")
        blocked = json.loads(latest_tool["content"])
        self.assertEqual(blocked["reason"], "consecutive_failure_limit")
        self.assertEqual(blocked["tool"], "record_state")
        self.assertEqual(blocked["count"], 2)

    # Mutation caught: recognizing only the direct record_state refusal text
    # misses the structured `거부:` report and dispatches its third repeat.
    async def test_structured_record_state_refusal_counts_as_failure(self):
        refused_update = {"evidence": ["malformed evidence item"]}
        await self.run_agent([
            *[
                _response(tool_calls=[
                    _tool_call(
                        f"record-structured-{attempt}",
                        "record_state",
                        refused_update,
                    ),
                ])
                for attempt in range(1, 4)
            ],
            _response(tool_calls=[
                _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["record-structured-1"], ["record-structured-2"]],
        )
        feedback_payload = self.model.agent_payloads[3]
        latest_tool = self._tool_messages(feedback_payload)[-1]
        self.assertEqual(latest_tool["tool_call_id"], "record-structured-3")
        blocked = json.loads(latest_tool["content"])
        self.assertEqual(blocked["reason"], "consecutive_failure_limit")
        self.assertEqual(blocked["tool"], "record_state")
        self.assertEqual(blocked["count"], 2)

    # Mutation caught: matching only an adjacent `반영`/`거부` separator misses
    # the producer's blank line and executes a third mixed-refusal update.
    async def test_mixed_record_state_refusal_counts_as_failure(self):
        mixed_update = {
            "goal": "mixed refusal goal",
            "evidence": ["malformed evidence item"],
        }
        await self.run_agent([
            *[
                _response(tool_calls=[
                    _tool_call(f"record-mixed-{attempt}", "record_state", mixed_update),
                ])
                for attempt in range(1, 4)
            ],
            _response(tool_calls=[
                _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["record-mixed-1"], ["record-mixed-2"]],
        )
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "record-mixed-3")
        blocked = json.loads(latest_tool["content"])
        self.assertEqual(blocked["reason"], "consecutive_failure_limit")
        self.assertEqual(blocked["tool"], "record_state")
        self.assertEqual(blocked["count"], 2)

    # Mutation caught: admitting a whole 100+ call response without reserving
    # only the remaining run budget performs excess side effects, while labeling
    # all requests as executions also exposes a false live count.
    async def test_run_budget_bounds_large_batch_and_reports_every_original_id(self):
        requested_calls = [
            _tool_call(
                f"budget-{index:03d}",
                "bash_exec",
                {"command": f"budget-command-{index:03d}"},
            )
            for index in range(101)
        ]
        message = await self.run_agent(
            [
                _response(tool_calls=requested_calls),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            run_budget=3,
        )

        self.assertEqual(
            self.executed_commands,
            ["budget-command-000", "budget-command-001", "budget-command-002"],
        )
        self.assertEqual(len(self.dispatched_batches), 1)
        self.assertEqual(
            [call[0] for call in self.dispatched_batches[0]],
            ["budget-000", "budget-001", "budget-002"],
        )

        feedback_payload = self.model.agent_payloads[1]
        expected_ids = [f"budget-{index:03d}" for index in range(101)]
        self.assertEqual(self._assistant_call_ids(feedback_payload), expected_ids)
        tool_messages = self._tool_messages(feedback_payload)
        self.assertEqual(
            [tool_message["tool_call_id"] for tool_message in tool_messages],
            expected_ids,
        )
        blocked = json.loads(tool_messages[-1]["content"])
        self.assertEqual(blocked["reason"], "run_tool_budget_exhausted")
        self.assertEqual(blocked["tool"], "bash_exec")
        self.assertEqual(blocked["limit"], 3)
        self.assertEqual(blocked["count"], 3)
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["directive"], BLOCK_DIRECTIVE)

        delivered = "\n".join(message.replies[1:] + message.channel.sent)
        self.assertIn("총 3개 도구 실행", delivered)
        self.assertNotIn("총 101개 도구 실행", delivered)

        live_status = "\n".join(
            edit for edit in self.reply_messages[0].edits if edit
        )
        self.assertIn("현재까지 실행: `0개`", live_status)
        self.assertIn("요청 도구", live_status)
        self.assertNotIn("총 도구: `101개`", live_status)


if __name__ == "__main__":
    unittest.main()
