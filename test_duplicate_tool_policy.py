"""Issue #8 pre-dispatch duplicate-tool and execution-budget behavior."""

import json
import shlex
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage, TEST_USER_ID, run_catalog_patch

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


def _raw_tool_call(call_id, name, raw_arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=raw_arguments),
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
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)

    async def run_agent(
        self,
        responses,
        tool_result=SUCCESS_RESULT,
        run_budget=None,
        use_real_bash=False,
    ):
        self.executed_commands = []
        self.dispatched_batches = []
        self.model = ModelStub(responses)
        original_bash = bot.tool_bash_exec
        original_dispatch = bot.execute_tools_in_parallel

        async def recording_bash(workspace, command):
            self.executed_commands.append(command)
            if use_real_bash:
                return await original_bash(workspace, command)
            if callable(tool_result):
                return tool_result(command, len(self.executed_commands))
            return tool_result

        async def recording_dispatch(workspace, tool_calls, *args, **kwargs):
            self.dispatched_batches.append([
                (call["id"], call["name"], call["arguments"])
                for call in tool_calls
            ])
            return await original_dispatch(workspace, tool_calls, *args, **kwargs)

        message = FakeMessage("중복 도구 실행을 점검해줘", CHANNEL_ID)
        self.reply_messages = []
        original_reply = message.reply

        async def recording_reply(content):
            sent = await original_reply(content)
            self.reply_messages.append(sent)
            return sent

        message.reply = recording_reply
        with tempfile.TemporaryDirectory() as log_dir, ExitStack() as stack:
            stack.enter_context(run_catalog_patch(bot, log_dir))
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
    def _assistant_calls(payload):
        assistant = next(
            message for message in payload
            if bot._msg_role(message) == "assistant" and message.get("tool_calls")
        )
        return assistant["tool_calls"]

    @classmethod
    def _assistant_call_ids(cls, payload):
        return [call["id"] for call in cls._assistant_calls(payload)]

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

    # Mutation caught: describing an all-blocked batch as active terminal or
    # network I/O misreports a request that never reaches the dispatcher.
    async def test_all_blocked_status_uses_neutral_request_review_wording(self):
        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"status-failure-{attempt}", "bash_exec", {
                        "command": "status-failure",
                    }),
                ])
                for attempt in range(1, 4)
            ] + [
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result="[Error: status failure]",
        )

        self.assertEqual(self.executed_commands, ["status-failure", "status-failure"])
        request_statuses = [
            edit for edit in self.reply_messages[0].edits
            if edit and "요청 도구" in edit
        ]
        self.assertEqual(len(request_statuses), 3)
        all_blocked_status = request_statuses[-1]
        self.assertIn("도구 요청 검토 중", all_blocked_status)
        self.assertNotIn("터미널 및 네트워크 I/O 실행 중", all_blocked_status)

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

    # Mutation caught: failing to reserve a prior-streak-blocked signature lets
    # a later identical call in [A, B, A] dispatch after B resets adjacency.
    async def test_consecutive_block_reserves_same_batch_identity(self):
        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"reserved-a-{attempt}", "bash_exec", {"command": "A"}),
                ])
                for attempt in range(1, 3)
            ] + [
                _response(tool_calls=[
                    _tool_call("reserved-a-blocked", "bash_exec", {"command": "A"}),
                    _tool_call("reserved-b", "bash_exec", {"command": "B"}),
                    _tool_call("reserved-a-duplicate", "bash_exec", {"command": "A"}),
                ]),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result=lambda command, _count: f"[Error: {command} failed]",
        )

        self.assertEqual(self.executed_commands, ["A", "A", "B"])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["reserved-a-1"], ["reserved-a-2"], ["reserved-b"]],
        )

        batch_ids = {
            "reserved-a-blocked", "reserved-b", "reserved-a-duplicate"
        }
        latest_tools = {
            message["tool_call_id"]: message
            for message in self._tool_messages(self.model.agent_payloads[3])
            if message["tool_call_id"] in batch_ids
        }
        self.assertEqual(
            list(latest_tools),
            ["reserved-a-blocked", "reserved-b", "reserved-a-duplicate"],
        )
        self.assertIn(
            '"reason":"consecutive_failure_limit"',
            latest_tools["reserved-a-blocked"]["content"],
        )
        self.assertEqual(latest_tools["reserved-b"]["content"], "[Error: B failed]")
        self.assertEqual(
            json.loads(latest_tools["reserved-a-duplicate"]["content"])["reason"],
            "same_batch_duplicate",
        )

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

    # Mutation caught: truncating the composed bash result removes the final
    # producer-owned nonzero exit marker from a real noisy subprocess result.
    async def test_real_long_nonzero_bash_keeps_terminal_exit_marker(self):
        script = 'import sys; sys.stdout.write("x" * 5001); raise SystemExit(7)'
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

        with tempfile.TemporaryDirectory() as workspace, run_catalog_patch(
            bot, workspace
        ):
            run = bot.RUN_CATALOG.acquire(TEST_USER_ID, CHANNEL_ID)
            result = await bot.tool_bash_exec(run, command)

        self.assertIn("출력 결과가 너무 길어", result)
        self.assertRegex(result, r"\[exit code: 7\]\s*$")

    # Mutation caught: hiding a real noisy subprocess's nonzero status from the
    # classifier lets the unchanged third failing command execute again.
    async def test_real_long_nonzero_bash_blocks_third_attempt_end_to_end(self):
        script = 'import sys; sys.stdout.write("x" * 5001); raise SystemExit(7)'
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"real-long-failure-{attempt}", "bash_exec", {
                        "command": command,
                    }),
                ])
                for attempt in range(1, 4)
            ] + [
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            use_real_bash=True,
        )

        self.assertEqual(self.executed_commands, [command, command])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["real-long-failure-1"], ["real-long-failure-2"]],
        )
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "real-long-failure-3")
        self.assertEqual(
            json.loads(latest_tool["content"])["reason"],
            "consecutive_failure_limit",
        )

    # Mutation caught: ignoring a producer-owned read_file error envelope lets
    # the unchanged third missing-file read reach the dispatcher.
    async def test_missing_read_file_error_blocks_third_attempt_end_to_end(self):
        arguments = {"path": "missing-for-duplicate-policy.txt"}
        await self.run_agent([
            *[
                _response(tool_calls=[
                    _tool_call(
                        f"missing-read-{attempt}", "read_file", arguments
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
            [["missing-read-1"], ["missing-read-2"]],
        )
        for payload_index in (1, 2):
            result = json.loads(
                self._tool_messages(
                    self.model.agent_payloads[payload_index]
                )[-1]["content"]
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"], "not_found")
        blocked = json.loads(
            self._tool_messages(self.model.agent_payloads[3])[-1]["content"]
        )
        self.assertEqual(blocked["reason"], "consecutive_failure_limit")
        self.assertEqual(blocked["tool"], "read_file")
        self.assertEqual(blocked["count"], 2)

    # Mutation caught: treating a producer-owned write_file conflict envelope as
    # success lets an unchanged third stale canonical write reach the dispatcher.
    async def test_stale_canonical_write_conflict_blocks_third_attempt_end_to_end(self):
        stale_arguments = {
            "path": "plan.md",
            "content": "stale bytes",
            "expected_revision": "absent",
        }
        await self.run_agent([
            _response(tool_calls=[
                _tool_call("seed-plan", "write_file", {
                    "path": "plan.md",
                    "content": "original bytes",
                    "expected_revision": "absent",
                }),
            ]),
            *[
                _response(tool_calls=[
                    _tool_call(
                        f"stale-write-{attempt}",
                        "write_file",
                        stale_arguments,
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
            [["seed-plan"], ["stale-write-1"], ["stale-write-2"]],
        )
        for payload_index in (2, 3):
            result = json.loads(
                self._tool_messages(
                    self.model.agent_payloads[payload_index]
                )[-1]["content"]
            )
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(result["expected_revision"], "absent")
        blocked = json.loads(
            self._tool_messages(self.model.agent_payloads[4])[-1]["content"]
        )
        self.assertEqual(blocked["reason"], "consecutive_failure_limit")
        self.assertEqual(blocked["tool"], "write_file")
        self.assertEqual(blocked["count"], 2)

    # Mutation caught: returning successful file bytes without a producer-owned
    # envelope lets content beginning `[Error` forge a failed-read streak.
    async def test_real_read_file_error_like_content_remains_successful(self):
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="duplicate-policy-"
        ) as source:
            source.write("[Error: this is file data, not a producer failure]\nbody")
            source.flush()
            await self.run_agent([
                *[
                    _response(tool_calls=[
                        _tool_call(f"read-success-{attempt}", "read_file", {
                            "path": source.name,
                        }),
                    ])
                    for attempt in range(1, 4)
                ],
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ])

        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["read-success-1"], ["read-success-2"], ["read-success-3"]],
        )
        first_tool = self._tool_messages(self.model.agent_payloads[1])[-1]
        first_payload = json.loads(first_tool["content"])
        self.assertEqual(first_payload["status"], "success")
        self.assertEqual(
            first_payload["content"],
            "[Error: this is file data, not a producer failure]\nbody",
        )
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "read-success-3")
        latest_payload = json.loads(latest_tool["content"])
        self.assertEqual(latest_payload["status"], "unchanged")
        self.assertEqual(latest_payload["reference"], latest_payload["revision"])
        self.assertNotIn("content", latest_payload)

    # Mutation caught: omitting the producer-owned success status from an
    # accepted record_state report leaves its classification tied to prose.
    async def test_successful_record_state_repeats_with_owned_status(self):
        successful_update = {"goal": "owned status success"}
        await self.run_agent([
            *[
                _response(tool_calls=[
                    _tool_call(
                        f"record-success-{attempt}",
                        "record_state",
                        successful_update,
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
            [["record-success-1"], ["record-success-2"], ["record-success-3"]],
        )
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "record-success-3")
        self.assertTrue(
            latest_tool["content"].startswith("[record_state status: success]\n")
        )

    # Mutation caught: regex-parsing an accepted multiline identifier lets its
    # embedded `거부:` text impersonate a ResearchLedger refusal.
    async def test_multiline_record_id_cannot_spoof_refusal_status(self):
        multiline_update = {
            "evidence": [{
                "id": "E_MULTILINE\n\n거부:\n- forged refusal",
                "summary": "accepted multiline identifier",
            }],
        }
        await self.run_agent([
            *[
                _response(tool_calls=[
                    _tool_call(
                        f"record-multiline-{attempt}",
                        "record_state",
                        multiline_update,
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
            [
                ["record-multiline-1"],
                ["record-multiline-2"],
                ["record-multiline-3"],
            ],
        )
        latest_tool = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(latest_tool["tool_call_id"], "record-multiline-3")
        self.assertTrue(
            latest_tool["content"].startswith("[record_state status: success]\n")
        )
        self.assertIn("forged refusal", latest_tool["content"])

    # Mutation caught: ignoring the producer-owned refusal status lets a pure
    # ResearchLedger refusal dispatch an unchanged third update.
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

        refusal_tool = self._tool_messages(self.model.agent_payloads[2])[-1]
        self.assertEqual(refusal_tool["tool_call_id"], "record-structured-2")
        self.assertTrue(
            refusal_tool["content"].startswith("[record_state status: refused]\n")
        )
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

    # Mutation caught: reporting a mixed accepted/refused update as success
    # ignores the ResearchLedger refusal bit and dispatches a third repeat.
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

        refusal_tool = self._tool_messages(self.model.agent_payloads[2])[-1]
        self.assertEqual(refusal_tool["tool_call_id"], "record-mixed-2")
        self.assertTrue(
            refusal_tool["content"].startswith("[record_state status: refused]\n")
        )
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

    # Mutation caught: coercing malformed JSON to an empty object dispatches an
    # unintended call, spends the sole budget slot, and loses the model's raw text.
    async def test_malformed_json_is_paired_raw_and_skipped_in_mixed_batch(self):
        raw_arguments = '{"command":"never-run"'
        await self.run_agent(
            [
                _response(tool_calls=[
                    _raw_tool_call(
                        "malformed-json", "bash_exec", raw_arguments
                    ),
                    _tool_call(
                        "valid-after-malformed",
                        "bash_exec",
                        {"command": "valid-after-malformed"},
                    ),
                ]),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            run_budget=1,
        )

        self.assertEqual(self.executed_commands, ["valid-after-malformed"])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["valid-after-malformed"]],
        )
        feedback_payload = self.model.agent_payloads[1]
        assistant_calls = self._assistant_calls(feedback_payload)
        self.assertEqual(
            [call["id"] for call in assistant_calls],
            ["malformed-json", "valid-after-malformed"],
        )
        self.assertEqual(
            assistant_calls[0]["function"]["arguments"], raw_arguments
        )
        tool_messages = self._tool_messages(feedback_payload)
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["malformed-json", "valid-after-malformed"],
        )
        self.assertEqual(
            tool_messages[0]["content"],
            '{"directive":"Provide tool arguments as a valid JSON object.",'
            '"error":"invalid_tool_arguments","reason":"malformed_json",'
            '"tool":"bash_exec"}',
        )
        self.assertEqual(tool_messages[1]["content"], SUCCESS_RESULT)

    # Mutation caught: accepting a decoded JSON scalar reaches `_exec_single`
    # and calls `.get()` instead of returning paired trust-boundary feedback.
    async def test_json_scalar_arguments_are_paired_without_dispatch(self):
        raw_arguments = "42"
        await self.run_agent([
            _response(tool_calls=[
                _raw_tool_call("scalar-json", "bash_exec", raw_arguments),
            ]),
            _response(tool_calls=[
                _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(self.dispatched_batches, [])
        self.assertEqual(self.executed_commands, [])
        self.assertEqual(len(self.model.agent_payloads), 2)
        feedback_payload = self.model.agent_payloads[1]
        assistant_calls = self._assistant_calls(feedback_payload)
        self.assertEqual(assistant_calls[0]["id"], "scalar-json")
        self.assertEqual(
            assistant_calls[0]["function"]["arguments"], raw_arguments
        )
        tool_messages = self._tool_messages(feedback_payload)
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["scalar-json"])
        self.assertEqual(
            tool_messages[0]["content"],
            '{"directive":"Provide tool arguments as a valid JSON object.",'
            '"error":"invalid_tool_arguments",'
            '"reason":"arguments_not_object","tool":"bash_exec"}',
        )

    # Mutation caught: selecting malformed finish_task arguments as completion
    # either calls `.get()` on a list or settles the run before corrective feedback.
    async def test_invalid_finish_task_arguments_are_paired_before_valid_retry(self):
        raw_arguments = '["not","an","object"]'
        await self.run_agent([
            _response(tool_calls=[
                _raw_tool_call("invalid-finish", "finish_task", raw_arguments),
            ]),
            _response(tool_calls=[
                _tool_call("valid-finish", "finish_task", {"report": LONG_REPORT}),
            ]),
        ])

        self.assertEqual(self.dispatched_batches, [])
        self.assertEqual(self.executed_commands, [])
        self.assertEqual(len(self.model.agent_payloads), 2)
        feedback_payload = self.model.agent_payloads[1]
        assistant_calls = self._assistant_calls(feedback_payload)
        self.assertEqual(assistant_calls[0]["id"], "invalid-finish")
        self.assertEqual(
            assistant_calls[0]["function"]["arguments"], raw_arguments
        )
        tool_messages = self._tool_messages(feedback_payload)
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["invalid-finish"],
        )
        self.assertEqual(
            json.loads(tool_messages[0]["content"]),
            {
                "directive": "Provide tool arguments as a valid JSON object.",
                "error": "invalid_tool_arguments",
                "reason": "arguments_not_object",
                "tool": "finish_task",
            },
        )

    # Mutation caught: allowing an invalid call to enter policy bookkeeping or
    # dispatch resets a two-failure streak instead of pre-blocking the next A.
    async def test_invalid_arguments_do_not_reset_failure_streak(self):
        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call(f"invalid-streak-a-{attempt}", "bash_exec", {
                        "command": "streak-A",
                    }),
                ])
                for attempt in range(1, 3)
            ] + [
                _response(tool_calls=[
                    _raw_tool_call("invalid-streak-gap", "bash_exec", '"bad"'),
                ]),
                _response(tool_calls=[
                    _tool_call("invalid-streak-a-3", "bash_exec", {
                        "command": "streak-A",
                    }),
                ]),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ],
            tool_result="[Error: streak failure]",
        )

        self.assertEqual(self.executed_commands, ["streak-A", "streak-A"])
        self.assertEqual(
            [[call[0] for call in batch] for batch in self.dispatched_batches],
            [["invalid-streak-a-1"], ["invalid-streak-a-2"]],
        )
        self.assertEqual(len(self.model.agent_payloads), 5)
        invalid_feedback = self._tool_messages(self.model.agent_payloads[3])[-1]
        self.assertEqual(invalid_feedback["tool_call_id"], "invalid-streak-gap")
        self.assertEqual(
            json.loads(invalid_feedback["content"])["reason"],
            "arguments_not_object",
        )
        blocked_feedback = self._tool_messages(self.model.agent_payloads[4])[-1]
        self.assertEqual(blocked_feedback["tool_call_id"], "invalid-streak-a-3")
        self.assertEqual(
            json.loads(blocked_feedback["content"])["reason"],
            "consecutive_failure_limit",
        )

    # Mutation caught: preserving only the last two tool-result messages splits
    # a newer mixed group, erasing its raw arguments and full producer payloads.
    async def test_newest_mixed_tool_group_survives_micro_compaction_intact(self):
        raw_arguments = '{\n  "command": "never-run",\n  "note": "한글 raw bytes"'
        file_content = (
            "[Error: latest bytes are evidence, not a failure]\n"
            "LATEST_READ_PAYLOAD_SENTINEL"
        )
        record_update = {"goal": "LATEST_RECORD_PAYLOAD_SENTINEL"}
        expected_record = await bot.tool_record_state(
            bot.ResearchLedger(), record_update
        )

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="latest-group-"
        ) as source:
            source.write(file_content)
            source.flush()
            latest_calls = [
                _raw_tool_call("latest-malformed", "bash_exec", raw_arguments),
                _tool_call("latest-read", "read_file", {"path": source.name}),
                _tool_call("latest-record", "record_state", record_update),
                _tool_call(
                    "latest-bash-1", "bash_exec", {"command": "latest-command-1"}
                ),
                _tool_call(
                    "latest-bash-2", "bash_exec", {"command": "latest-command-2"}
                ),
            ]
            expected_arguments = [
                call.function.arguments for call in latest_calls
            ]
            await self.run_agent([
                _response(tool_calls=[
                    _tool_call(
                        "older-call", "bash_exec", {"command": "older-command"}
                    ),
                ]),
                _response(tool_calls=latest_calls),
                _response(tool_calls=[
                    _tool_call("finish", "finish_task", {"report": LONG_REPORT}),
                ]),
            ])

        feedback_payload = self.model.agent_payloads[2]
        latest_ids = [call.id for call in latest_calls]
        latest_assistant_index = next(
            index
            for index, payload_message in enumerate(feedback_payload)
            if bot._msg_role(payload_message) == "assistant"
            and [
                call["id"] for call in payload_message.get("tool_calls") or []
            ] == latest_ids
        )
        latest_assistant = feedback_payload[latest_assistant_index]
        self.assertEqual(
            [
                call["function"]["arguments"]
                for call in latest_assistant["tool_calls"]
            ],
            expected_arguments,
        )
        self.assertEqual(
            latest_assistant["tool_calls"][0]["function"]["arguments"].encode(
                "utf-8"
            ),
            raw_arguments.encode("utf-8"),
        )

        latest_tool_messages = feedback_payload[
            latest_assistant_index + 1:latest_assistant_index + 1 + len(latest_calls)
        ]
        self.assertEqual(
            [bot._msg_role(payload_message) for payload_message in latest_tool_messages],
            ["tool"] * len(latest_calls),
        )
        self.assertEqual(
            [payload_message["tool_call_id"] for payload_message in latest_tool_messages],
            latest_ids,
        )
        latest_contents = [
            payload_message["content"] for payload_message in latest_tool_messages
        ]
        self.assertEqual(
            latest_contents[0],
            '{"directive":"Provide tool arguments as a valid JSON object.",'
            '"error":"invalid_tool_arguments","reason":"malformed_json",'
            '"tool":"bash_exec"}',
        )
        read_payload = json.loads(latest_contents[1])
        self.assertEqual(read_payload["status"], "success")
        self.assertEqual(read_payload["content"], file_content)
        self.assertFalse(read_payload["truncated"])
        self.assertEqual(
            latest_contents[2:],
            [expected_record, SUCCESS_RESULT, SUCCESS_RESULT],
        )
        self.assertIn(
            "LATEST_RECORD_PAYLOAD_SENTINEL",
            latest_tool_messages[2]["content"],
        )

        older_tool = next(
            payload_message
            for payload_message in feedback_payload
            if bot._msg_role(payload_message) == "tool"
            and payload_message.get("tool_call_id") == "older-call"
        )
        self.assertIn("실행 결과 생략", older_tool["content"])
        self.assertNotEqual(older_tool["content"], SUCCESS_RESULT)

    # Mutation caught: preserving only two results from a 101-call group makes
    # 96 of its 98 structured run-budget blocks non-JSON in the next payload.
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
        blocked_messages = tool_messages[3:]
        self.assertEqual(len(blocked_messages), 98)
        for index, tool_message in enumerate(blocked_messages, start=3):
            with self.subTest(tool_call_id=tool_message["tool_call_id"]):
                self.assertTrue(
                    tool_message["content"].startswith("{"),
                    f"budget-{index:03d} result is not parseable JSON: "
                    f"{tool_message['content']!r}",
                )
                blocked = json.loads(tool_message["content"])
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
