"""Tool-call payload integrity: streaming reassembly, pre-send validation, recovery.

Covers the streaming collector (previously uncovered because every agent-level
test patches `create_streaming_completion` out), the single pre-send payload
validator, and the recovery branch that used to flatten the tool protocol on any
exception whose text merely contained "400".

Every identifier here is synthetic.
"""

import json
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage, run_catalog_patch

import bot
import outcome as outcome_mod

CHANNEL_ID = 987654900

LONG_REPORT = "payload 무결성 점검 완료. " + ("확인됨. " * 80)
SUCCESS_RESULT = "[stdout]\nok\n[exit code: 0]"


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


def _partial(**fields):
    """A streamed tool_call delta. Absent keys are absent attributes, not None."""
    function_fields = {
        key: fields.pop(key) for key in ("name", "arguments") if key in fields
    }
    if function_fields:
        fields["function"] = SimpleNamespace(**function_fields)
    return SimpleNamespace(**fields)


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class FakeToolCallStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self._index]
        self._index += 1
        return chunk

    async def close(self):
        self.closed = True


async def _collect(chunks):
    """Run the real streaming collector over synthetic tool_call deltas."""
    async def create(**kwargs):
        return FakeToolCallStream(chunks)

    stub = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with patch.object(bot, "client", stub):
        response = await bot.create_streaming_completion(model="stub", messages=[])
    return response.choices[0].message.tool_calls


class StreamingToolCallReassemblyTest(unittest.IsolatedAsyncioTestCase):
    async def test_index_less_argument_fragments_join_one_call(self):
        # Production mutation caught: keying an index-less fragment on
        # len(tool_buffers) splits one streamed call into N half-merged calls.
        calls = await _collect([
            _chunk(tool_calls=[_partial(id="call_a", name="bash_exec", arguments='{"comm')]),
            _chunk(tool_calls=[_partial(arguments='and": "printf ')]),
            _chunk(tool_calls=[_partial(arguments='hi"}')]),
        ])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_a")
        self.assertEqual(calls[0].function.name, "bash_exec")
        self.assertEqual(json.loads(calls[0].function.arguments), {"command": "printf hi"})

    async def test_fallback_ids_never_repeat_across_two_completions(self):
        # Production mutation caught: a per-completion counter mints
        # call_stream_0 in every step, so one conversation carries duplicates.
        first = await _collect([
            _chunk(tool_calls=[_partial(index=0, name="bash_exec", arguments="{}")]),
        ])
        second = await _collect([
            _chunk(tool_calls=[_partial(index=0, name="bash_exec", arguments="{}")]),
        ])

        self.assertTrue(first[0].id)
        self.assertTrue(second[0].id)
        self.assertNotEqual(first[0].id, second[0].id)

    async def test_id_fragments_are_reassembled_not_overwritten(self):
        # Production mutation caught: assigning buf["id"] per chunk keeps only
        # the last fragment, so a fragmented id is silently truncated.
        calls = await _collect([
            _chunk(tool_calls=[_partial(index=0, id="call_", name="bash_exec")]),
            _chunk(tool_calls=[_partial(index=0, id="9f", arguments="{}")]),
        ])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_9f")

    async def test_repeated_whole_id_is_not_concatenated(self):
        # Production mutation caught: naive id concatenation turns a provider
        # that repeats the full id on every chunk into call_1call_1.
        calls = await _collect([
            _chunk(tool_calls=[_partial(index=0, id="call_1", name="bash_exec")]),
            _chunk(tool_calls=[_partial(index=0, id="call_1", arguments="{}")]),
        ])

        self.assertEqual([call.id for call in calls], ["call_1"])

    async def test_nameless_fragment_is_refused_before_dispatch(self):
        # Production mutation caught: a buffer that never received a function
        # name reaches dispatch as a real call with name "".
        calls = await _collect([
            _chunk(tool_calls=[_partial(index=0, id="call_b", arguments='{"command": "x"}')]),
        ])

        self.assertEqual(calls, [])

    async def test_mixed_index_types_keep_arrival_order(self):
        # Production mutation caught: sorted() over buffers keyed by both int
        # and str indexes raises TypeError and loses the whole response.
        calls = await _collect([
            _chunk(tool_calls=[_partial(index=0, id="call_x", name="bash_exec", arguments="{}")]),
            _chunk(tool_calls=[_partial(index="1", id="call_y", name="read_file", arguments="{}")]),
        ])

        self.assertEqual([call.id for call in calls], ["call_x", "call_y"])


def _assistant(content, calls):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for call_id, name, arguments in calls
        ],
    }


def _tool(call_id, content=SUCCESS_RESULT, name="bash_exec"):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


class ChatPayloadValidatorTest(unittest.TestCase):
    def _verdict(self, messages):
        return bot.validate_chat_payload(messages)

    def test_balanced_duplicate_ids_fail_validation(self):
        # Production mutation caught: counting calls against results treats a
        # 2/2 payload whose ids are the same id twice as valid.
        payload = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            _assistant(None, [("dup", "bash_exec", "{}"), ("dup", "read_file", "{}")]),
            _tool("dup"),
            _tool("dup"),
        ]

        verdict = self._verdict(payload)

        self.assertFalse(verdict.ok)
        self.assertIn("tool_call_id_duplicate", verdict.defects)
        self.assertIn("tool_result_duplicate", verdict.defects)
        repaired = self._verdict(verdict.messages)
        self.assertTrue(repaired.ok, repaired.defects)
        self.assertEqual(len(bot._msg_tool_calls(verdict.messages[2])), 1)
        self.assertEqual(
            [message for message in verdict.messages if bot._msg_role(message) == "tool"],
            [_tool("dup")],
        )

    def test_orphan_result_and_unanswered_call_fail_validation(self):
        # Production mutation caught: a result whose id was never announced and
        # an announced call with no result both ship as a "balanced" 1/1 payload.
        payload = [
            {"role": "system", "content": "s"},
            _assistant(None, [("c1", "bash_exec", "{}")]),
            _tool("c9"),
        ]

        verdict = self._verdict(payload)

        self.assertIn("tool_result_orphan", verdict.defects)
        self.assertIn("tool_call_unanswered", verdict.defects)
        self.assertTrue(self._verdict(verdict.messages).ok)
        self.assertEqual(
            [bot._msg_role(message) for message in verdict.messages],
            ["system", "assistant", "tool", "user"],
        )
        # The announced call gets an explicit "no result" answer and the orphan
        # keeps its text as user content, so neither side is silently dropped.
        self.assertEqual(verdict.messages[2]["tool_call_id"], "c1")
        self.assertEqual(verdict.messages[2]["content"], bot.TOOL_PAYLOAD_MISSING_RESULT)
        self.assertIn(SUCCESS_RESULT, bot._msg_content(verdict.messages[3]))

    def test_result_wedged_behind_a_user_message_is_restored_adjacent(self):
        # Production mutation caught: a user or system message inside a tool
        # group is sent as-is, so the group is no longer adjacent to its call.
        payload = [
            {"role": "system", "content": "s"},
            _assistant(None, [("c1", "bash_exec", "{}"), ("c2", "bash_exec", "{}")]),
            _tool("c1"),
            {"role": "user", "content": "끼어든 사용자 지시"},
            _tool("c2"),
        ]

        verdict = self._verdict(payload)

        self.assertIn("tool_group_split", verdict.defects)
        self.assertEqual(
            [bot._msg_role(message) for message in verdict.messages],
            ["system", "assistant", "tool", "tool", "user"],
        )
        self.assertEqual(
            [message["tool_call_id"] for message in verdict.messages[2:4]],
            ["c1", "c2"],
        )
        self.assertTrue(self._verdict(verdict.messages).ok)

    def test_none_result_content_becomes_a_string(self):
        # Production mutation caught: a missing tool result is serialized as
        # content None, which no chat template can render.
        payload = [
            {"role": "system", "content": "s"},
            _assistant(None, [("c1", "bash_exec", "{}")]),
            _tool("c1", content=None),
        ]

        verdict = self._verdict(payload)

        self.assertIn("tool_content_missing", verdict.defects)
        self.assertIsInstance(verdict.messages[2]["content"], str)
        self.assertTrue(verdict.messages[2]["content"])
        self.assertTrue(self._verdict(verdict.messages).ok)

    def test_empty_id_and_missing_name_calls_are_rejected(self):
        # Production mutation caught: provider ids and names are copied verbatim,
        # so an empty id or empty function name is announced to the server.
        payload = [
            {"role": "system", "content": "s"},
            _assistant(None, [("", "bash_exec", "{}"), ("c2", "", "{}")]),
        ]

        verdict = self._verdict(payload)

        self.assertIn("tool_call_id_missing", verdict.defects)
        self.assertIn("tool_name_missing", verdict.defects)
        self.assertEqual(bot._msg_tool_calls(verdict.messages[1]), [])

    def test_non_string_arguments_are_serialized(self):
        # Production mutation caught: dict arguments reach the wire as an object
        # where the tool-call schema requires a JSON string.
        payload = [
            {"role": "system", "content": "s"},
            _assistant(None, [("c1", "bash_exec", {"command": "printf hi"})]),
            _tool("c1"),
        ]

        verdict = self._verdict(payload)

        self.assertIn("tool_arguments_not_string", verdict.defects)
        arguments = verdict.messages[1]["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(arguments, str)
        self.assertEqual(json.loads(arguments), {"command": "printf hi"})

    def test_second_system_message_is_demoted_by_the_single_validator(self):
        # Production mutation caught: dropping the system-position rule from the
        # one validator revives the "system must be first" template error.
        payload = [
            {"role": "system", "content": "첫 시스템"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "나중 시스템"},
        ]

        verdict = self._verdict(payload)

        self.assertIn("system_position", verdict.defects)
        self.assertEqual(
            [bot._msg_role(message) for message in verdict.messages],
            ["system", "user", "user"],
        )
        self.assertIn("나중 시스템", bot._msg_content(verdict.messages[2]))
        self.assertTrue(self._verdict(verdict.messages).ok)

    def test_clean_tool_group_passes_untouched(self):
        # Production mutation caught: an over-eager repair that drops or reorders
        # a well-formed group destroys history the model still needs.
        payload = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            _assistant(None, [("c1", "bash_exec", "{}"), ("c2", "read_file", "{}")]),
            _tool("c1"),
            _tool("c2", name="read_file"),
            {"role": "user", "content": "다음 지시"},
        ]

        verdict = self._verdict(payload)

        self.assertTrue(verdict.ok, verdict.defects)
        self.assertEqual(verdict.messages, payload)

    def test_fingerprint_masks_ids_and_omits_content(self):
        # Production mutation caught: a diagnostic record that carries raw ids or
        # message content leaks payload text into the session log.
        payload = [
            {"role": "system", "content": "기밀 시스템 프롬프트"},
            _assistant("기밀 판단", [("secret-call-1", "bash_exec", "{}")]),
            _tool("secret-call-1", content="기밀 실행 결과"),
        ]

        fingerprint = bot._payload_fingerprint(payload)

        self.assertNotIn("secret-call-1", fingerprint)
        self.assertNotIn("기밀", fingerprint)
        self.assertEqual(fingerprint, "system|assistant[t1]|tool:t1")


class RecordingModel:
    """Model double that records every outgoing stage payload and its params."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    @property
    def stages(self):
        return [call["stage"] for call in self.calls]

    async def __call__(self, **kwargs):
        messages = kwargs.get("messages") or []
        system = bot._msg_content(messages[0]) if messages else ""
        if any(marker in system for marker in ("수석 분석가", "AI 리포터", "컨텍스트 압축기")):
            return _response(content=LONG_REPORT)
        self.calls.append({
            "stage": kwargs.get("stage"),
            "messages": messages,
            "has_tools": "tools" in kwargs,
        })
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class PayloadRecoveryDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self._reset_channel_state()

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

    async def run_agent(self, script, max_loops=6, log_records=None, dispatch=None):
        self.model = RecordingModel(script)

        async def stub_bash(workspace, command):
            return SUCCESS_RESULT

        original_log = bot.log_session_event

        def recording_log(workspace, kind, *args, **fields):
            if log_records is not None:
                log_records.append((kind, fields))
            original_log(workspace, kind, *args, **fields)

        message = FakeMessage("payload 무결성 점검해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, ExitStack() as stack:
            stack.enter_context(run_catalog_patch(bot, log_dir))
            stack.enter_context(patch.object(bot, "MAX_AGENT_LOOPS", max_loops))
            stack.enter_context(patch.object(bot, "CHECKPOINT_INTERVAL", 99))
            stack.enter_context(patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99))
            stack.enter_context(patch.object(bot, "tool_bash_exec", stub_bash))
            stack.enter_context(patch.object(bot, "log_session_event", recording_log))
            stack.enter_context(patch.object(bot, "create_streaming_completion", self.model))
            if dispatch is not None:
                stack.enter_context(patch.object(bot, "execute_tools_in_parallel", dispatch))
            await bot.on_message(message)
        return message

    async def test_missing_parallel_result_is_repaired_before_it_is_sent(self):
        # Production mutation caught: a short result list from the tool stage
        # leaves a role="tool" message with content None in the live history, and
        # with no validator at the send boundary it ships that way every step.
        async def short_dispatch(workspace, tool_calls, *args, **kwargs):
            return [SUCCESS_RESULT]

        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call("pair-1", "bash_exec", {"command": "printf a"}),
                    _tool_call("pair-2", "bash_exec", {"command": "printf b"}),
                ]),
                _response(tool_calls=[_tool_call("done", "finish_task", {"report": LONG_REPORT})]),
            ],
            dispatch=short_dispatch,
        )

        sent = self.model.calls[1]["messages"]
        results = [message for message in sent if bot._msg_role(message) == "tool"]
        self.assertEqual([message["tool_call_id"] for message in results], ["pair-1", "pair-2"])
        for result in results:
            self.assertIsInstance(result["content"], str)
        self.assertEqual(results[1]["content"], bot.TOOL_PAYLOAD_MISSING_RESULT)

    async def test_unrelated_400_is_not_flattened_or_retried(self):
        # Production mutation caught: substring-matching "400" sends a context
        # length error down the tool-correlation recovery and retries it.
        message = await self.run_agent([
            RuntimeError("400 Bad Request: context length exceeded"),
        ])

        self.assertEqual(self.model.stages, ["agent"])
        delivered = "".join(message.replies[1:] + message.channel.sent)
        self.assertIn("업스트림 실패", delivered)
        self.assertIn("context length exceeded", delivered)

    async def test_tool_correlation_400_retries_once_without_tool_params(self):
        # Production mutation caught: the recovery retry still offers tools into
        # a history whose tool protocol was just erased, inviting the same 400.
        await self.run_agent([
            _response(tool_calls=[_tool_call("corr-1", "bash_exec", {"command": "printf a"})]),
            RuntimeError("400 Bad Request: invalid tool_call_id in message"),
            _response(content="복구 후 계속 진행합니다."),
            _response(tool_calls=[_tool_call("done", "finish_task", {"report": LONG_REPORT})]),
        ])

        self.assertEqual(
            self.model.stages, ["agent", "agent", "agent:retry", "agent"]
        )
        retry = self.model.calls[2]
        self.assertFalse(retry["has_tools"])
        self.assertEqual(
            [bot._msg_role(m) for m in retry["messages"] if bot._msg_role(m) == "tool"], []
        )
        self.assertTrue(all(
            not bot._msg_tool_calls(m) for m in retry["messages"]
        ))

    async def test_erased_tool_protocol_persists_into_the_next_step(self):
        # Production mutation caught: writing the recovery payload to a local
        # variable leaves messages_payload corrupt, so the same 400 refires
        # every remaining step.
        await self.run_agent([
            _response(tool_calls=[_tool_call("corr-1", "bash_exec", {"command": "printf a"})]),
            RuntimeError("400 Bad Request: invalid tool_call_id in message"),
            _response(content="복구 후 계속 진행합니다."),
            _response(tool_calls=[_tool_call("done", "finish_task", {"report": LONG_REPORT})]),
        ])

        next_step = self.model.calls[3]["messages"]
        self.assertEqual(
            [bot._msg_role(m) for m in next_step if bot._msg_role(m) == "tool"], []
        )
        self.assertTrue(all(not bot._msg_tool_calls(m) for m in next_step))

    async def test_failure_record_keeps_masked_ids_and_ledger_revision(self):
        # Production mutation caught: a recovery that logs nothing structural
        # cannot tell a server/template problem from a client-side one.
        records = []
        await self.run_agent(
            [
                _response(tool_calls=[
                    _tool_call("corr-secret-1", "bash_exec", {"command": "printf a"}),
                ]),
                RuntimeError("400 Bad Request: invalid tool_call_id in message"),
                _response(content="복구 후 계속 진행합니다."),
                _response(tool_calls=[_tool_call("done", "finish_task", {"report": LONG_REPORT})]),
            ],
            log_records=records,
        )

        failures = [fields for kind, fields in records if kind == "model_stage_failure"]
        self.assertEqual(len(failures), 1)
        record = failures[0]
        self.assertIn("revision", record)
        self.assertIn("tool:t1", record["fingerprint"])
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("corr-secret-1", serialized)
        self.assertNotIn(SUCCESS_RESULT, serialized)


if __name__ == "__main__":
    unittest.main()
