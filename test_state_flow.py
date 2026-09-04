"""The issue's synthetic reproduction, turned into a regression test.

`H_A` is refuted at revision 2 by `E_NEG`, which only appears from the second
line of an old tool result onward. `C_A` was concluded from `H_A@v1`. The
compactor is stubbed to echo the previous summary verbatim. The markers must
survive micro compaction, the checkpoint, the rollover, a following run, and
final synthesis.
"""

import json
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage, run_catalog_patch  # sets required config env before bot imports

import bot
from ledger import ResearchLedger

SEED_SUMMARY = "이전 누적 요약: A 경로 가설을 조사 중이며 아직 결론이 없다."
TEST_WORKSPACE = SimpleNamespace(root="/tmp/run-0123456789abcdef0123456789abcdef")

BASH_RESULT = "\n".join([
    "[stdout]",
    "reproduce.sh 시작",
    "E_NEG: A 경로를 차단한 뒤에도 장애가 그대로 재현되었다",
    "따라서 H_A는 반증되었다",
    "[exit code: 0]",
])

CHECKPOINT_REPORT = """### 1. 📌 현재까지 완료된 핵심 작업
A 경로 차단 실험을 완료했습니다.

### 2. 🔍 발견된 핵심 데이터
정정: B 캐시 가설도 반증되었습니다.

### 3. 🎯 향후 진행할 구체적인 작업 계획
남은 후보를 조사합니다.

```state_update
{"evidence": [{"id": "E_CP", "summary": "B 캐시 비활성 후에도 장애 재현", "source": "log://3"}],
 "hypotheses": [{"id": "H_B", "status": "rejected", "evidence_id": "E_CP"}]}
```
"""


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


def serialize(messages) -> str:
    """Flatten a request payload the way the model would see it."""
    parts = []
    for msg in messages or []:
        parts.append(str(bot._msg_role(msg)))
        parts.append(str(bot._msg_content(msg) or ""))
        for call in bot._msg_tool_calls(msg):
            parts.append(bot._tool_call_summary(call))
    return "\n".join(parts)


class ModelStub:
    """Routes every completion call by prompt role and records the payload."""

    def __init__(self, agent_responses):
        self.agent_responses = list(agent_responses)
        self.calls = []

    def payloads(self, kind):
        return [messages for call_kind, messages in self.calls if call_kind == kind]

    async def __call__(self, **kwargs):
        messages = kwargs.get("messages") or []
        system = bot._msg_content(messages[0]) if messages else ""
        if "컨텍스트 압축기" in system:
            kind = "rollover"
        elif "AI 리포터" in system:
            kind = "checkpoint"
        elif "수석 분석가" in system:
            kind = "synthesis"
        else:
            kind = "agent"
        self.calls.append((kind, messages))

        if kind == "rollover":
            # The repro's stalled compactor: hand back the previous summary.
            prompt = bot._msg_content(messages[-1])
            echoed = re.search(r"\[기존 누적 요약\]\n(.*?)\n\n\[", prompt, re.DOTALL)
            return _response(content=echoed.group(1).strip() if echoed else "")
        if kind == "checkpoint":
            return _response(content=CHECKPOINT_REPORT)
        if kind == "synthesis":
            return _response(content="최종 보고서: " + ("확인됨. " * 60))
        return self.agent_responses.pop(0)


def refuted_ledger():
    ledger = ResearchLedger()
    ledger.apply_updates({
        "goal": "장애 원인 규명",
        "evidence": [{"id": "E_POS", "summary": "A 경로 오류 로그 관측", "source": "log://1"}],
        "hypotheses": [{"id": "H_A", "statement": "A 경로가 장애 원인이다", "evidence_id": "E_POS"}],
        "conclusions": [{"id": "C_A", "statement": "A 경로 차단으로 장애가 멈춘다", "premises": ["H_A"]}],
    })
    ledger.apply_updates({
        "evidence": [{"id": "E_NEG", "summary": "A 경로 차단 후에도 장애 재현", "source": "log://2"}],
        "hypotheses": [{"id": "H_A", "status": "rejected", "evidence_id": "E_NEG"}],
    })
    return ledger


class ImmutableContextTest(unittest.TestCase):
    def test_context_remains_immutable_for_prefix_cache(self):
        ledger = refuted_ledger()
        payload = [{"role": "system", "content": bot.build_system_content(TEST_WORKSPACE, ledger, SEED_SUMMARY)}]
        for step in range(6):
            payload.append({
                "role": "assistant",
                "content": f"step {step} 판단",
                "tool_calls": [{
                    "id": f"c{step}",
                    "type": "function",
                    "function": {"name": "bash_exec", "arguments": json.dumps({"command": f"probe {step}"})},
                }],
            })
            payload.append({
                "role": "tool", "tool_call_id": f"c{step}", "name": "bash_exec", "content": BASH_RESULT,
            })

        validated = bot.validate_chat_payload(payload).messages
        text = serialize(validated)

        # Prefix Cache 보존을 위해 과거 도구 결과를 임의로 변조/생략하지 않는다.
        self.assertNotIn("실행 결과 생략", text)
        # BASH_RESULT 원문과 권위 있는 상태가 온전히 보존된다.
        self.assertIn("E_NEG: A 경로를 차단한 뒤에도 장애가 그대로 재현되었다", text)
        for marker in ("H_A=rejected@v2", "C_A=무효", "E_NEG"):
            self.assertIn(marker, text)


class RollupSourceBudgetTest(unittest.TestCase):
    def test_newest_refutation_survives_a_tight_budget(self):
        messages = [
            {"role": "tool", "name": "bash_exec", "content": f"오래된 결과 {i} " + ("가" * 400)}
            for i in range(20)
        ]
        messages.append({"role": "tool", "name": "bash_exec", "content": BASH_RESULT})

        source = bot.build_rollup_source(messages, max_chars=1500)

        self.assertIn("E_NEG", source)
        # Chronological order is restored after the newest-first budget pass.
        self.assertLess(source.index("오래된 결과"), source.index("E_NEG"))


class RolloverValidationTest(unittest.IsolatedAsyncioTestCase):
    def _payload(self):
        ledger = refuted_ledger()
        payload = [{"role": "system", "content": bot.build_system_content(TEST_WORKSPACE, ledger, SEED_SUMMARY)}]
        for step in range(12):
            payload.append({
                "role": "assistant",
                "content": f"step {step} 판단",
                "tool_calls": [{
                    "id": f"c{step}",
                    "type": "function",
                    "function": {"name": "bash_exec", "arguments": json.dumps({"command": f"probe {step}"})},
                }],
            })
            payload.append({
                "role": "tool", "tool_call_id": f"c{step}", "name": "bash_exec", "content": BASH_RESULT,
            })
        return ledger, payload

    async def test_echoed_summary_is_rejected_and_markers_are_restored(self):
        ledger, payload = self._payload()
        stub = ModelStub([])

        with patch.object(bot, "create_streaming_completion", stub):
            rolled, summary = await bot.rollover_agent_context(
                TEST_WORKSPACE, payload, SEED_SUMMARY, 10, ledger=ledger
            )

        # The echoed summary was refused, so the new summary is not the old one.
        self.assertNotEqual(summary.strip(), SEED_SUMMARY)
        for marker in ("H_A=rejected@v2", "C_A=무효", "E_NEG"):
            self.assertIn(marker, summary)
            self.assertIn(marker, serialize(rolled))
        self.assertEqual(bot._msg_role(rolled[0]), "system")

    async def test_rollover_is_a_no_op_when_the_source_adds_nothing(self):
        ledger, payload = self._payload()
        old_messages, _ = bot.split_recent_agent_context(payload)
        already_summarized = bot.build_rollup_source(old_messages)
        stub = ModelStub([])

        with patch.object(bot, "create_streaming_completion", stub):
            rolled, summary = await bot.rollover_agent_context(
                TEST_WORKSPACE, payload, already_summarized, 10, ledger=ledger
            )

        self.assertEqual(stub.calls, [])
        self.assertIs(rolled, payload)
        self.assertEqual(summary, already_summarized)


class StateUpdateBlockTest(unittest.TestCase):
    def test_block_is_extracted_and_removed_from_the_report(self):
        updates, cleaned = bot.parse_state_update_blocks(CHECKPOINT_REPORT)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["hypotheses"][0]["id"], "H_B")
        self.assertNotIn("state_update", cleaned)
        self.assertIn("정정: B 캐시 가설도 반증되었습니다.", cleaned)

    def test_report_without_a_block_is_returned_unchanged(self):
        updates, cleaned = bot.parse_state_update_blocks("정상 보고서")
        self.assertEqual(updates, [])
        self.assertEqual(cleaned, "정상 보고서")

    def test_malformed_json_is_dropped_instead_of_leaking_into_discord(self):
        updates, cleaned = bot.parse_state_update_blocks("보고서\n```state_update\n{not json}\n```\n")
        self.assertEqual(updates, [])
        self.assertNotIn("state_update", cleaned)


class MarkerSurvivalThroughTheRunTest(unittest.IsolatedAsyncioTestCase):
    CHANNEL_ID = 987654400

    def setUp(self):
        self._reset_channel_state()
        bot.FREE_RESPONSE_CHANNEL_IDS.add(self.CHANNEL_ID)
        bot.channel_summary[self.CHANNEL_ID] = SEED_SUMMARY

    def tearDown(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(self.CHANNEL_ID)
        self._reset_channel_state()

    def _reset_channel_state(self):
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_active_runs,
            bot.channel_ledger,
        ):
            state.pop(self.CHANNEL_ID, None)

    async def test_refutation_survives_every_transformation_and_the_next_run(self):
        first_run = [
            _response(tool_calls=[_tool_call("c1", "record_state", {
                "goal": "장애 원인 규명",
                "evidence": [{"id": "E_POS", "summary": "A 경로 오류 로그 관측", "source": "log://1"}],
                "hypotheses": [
                    {"id": "H_A", "statement": "A 경로가 장애 원인이다", "evidence_id": "E_POS"},
                    {"id": "H_B", "statement": "B 캐시가 장애 원인이다", "evidence_id": "E_POS"},
                ],
                "conclusions": [
                    {"id": "C_A", "statement": "A 경로 차단으로 장애가 멈춘다", "premises": ["H_A"]},
                ],
            })]),
            _response(tool_calls=[
                _tool_call("c2", "bash_exec", {"command": "reproduce.sh"}),
                _tool_call("c3", "record_state", {
                    "evidence": [{"id": "E_NEG", "summary": "A 경로 차단 후에도 장애 재현", "source": "log://2"}],
                    "hypotheses": [{"id": "H_A", "status": "rejected", "evidence_id": "E_NEG"}],
                }),
            ]),
            # finish_task with an empty report: the run completes but there is no
            # text to send, so final synthesis runs. (Report length no longer
            # decides this - see outcome.py.)
            _response(tool_calls=[_tool_call("c4", "finish_task", {"report": ""})]),
        ]
        stub = ModelStub(first_run)

        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 2), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 2), \
                patch.object(bot, "KEEP_RECENT_TOOL_MESSAGES", 2), \
                patch.object(bot, "tool_bash_exec", AsyncMock(return_value=BASH_RESULT)), \
                patch.object(bot, "create_streaming_completion", stub):
            await bot.on_message(FakeMessage("장애 원인을 조사해줘", self.CHANNEL_ID))

        ledger = bot.channel_ledger[self.CHANNEL_ID]
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")
        self.assertFalse(ledger.conclusion_is_valid("C_A"))

        # Criterion 4: the checkpoint's correction reached the ledger, and it
        # did so before the rollover that runs in the same step.
        self.assertEqual(ledger.hypothesis_marker("H_B"), "H_B=rejected@v2")
        rollover_payloads = stub.payloads("rollover")
        self.assertEqual(len(rollover_payloads), 1)
        self.assertIn("H_B=rejected@v2", serialize(rollover_payloads[0]))

        # Criterion 3: every downstream transformation carries the markers.
        for kind in ("checkpoint", "rollover", "synthesis"):
            payloads = stub.payloads(kind)
            self.assertTrue(payloads, f"{kind} 호출이 없습니다")
            for messages in payloads:
                text = serialize(messages)
                for marker in ("H_A=rejected@v2", "C_A=무효", "E_NEG"):
                    self.assertIn(marker, text, f"{kind} payload에 {marker}가 없습니다")

        # The last agent step of the run sees the refutation too.
        self.assertIn("H_A=rejected@v2", serialize(stub.payloads("agent")[-1]))

        # Criterion 6: final synthesis gets the cumulative summary and the tail.
        synthesis_text = serialize(stub.payloads("synthesis")[0])
        self.assertIn(bot.ROLLING_SUMMARY_LABEL, synthesis_text)
        self.assertIn("reproduce.sh", synthesis_text)

        # Criterion 3, next run: a fresh request on the same channel still
        # starts from the refuted state.
        next_run = ModelStub([
            _response(tool_calls=[_tool_call("c5", "finish_task", {
                "report": "이전 결론을 유지합니다. " + ("확인됨. " * 60),
            })]),
        ])
        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "create_streaming_completion", next_run):
            await bot.on_message(FakeMessage("아까 A 경로 가설 다시 보자", self.CHANNEL_ID))

        text = serialize(next_run.payloads("agent")[0])
        for marker in ("H_A=rejected@v2", "C_A=무효", "E_NEG"):
            self.assertIn(marker, text)

    async def test_reactivating_a_refuted_hypothesis_is_refused_in_band(self):
        responses = [
            _response(tool_calls=[_tool_call("c1", "record_state", {
                "evidence": [{"id": "E_POS", "summary": "A 경로 오류 로그", "source": "log://1"}],
                "hypotheses": [{"id": "H_A", "statement": "A 경로가 원인이다", "evidence_id": "E_POS"}],
            })]),
            _response(tool_calls=[_tool_call("c2", "record_state", {
                "evidence": [{"id": "E_NEG", "summary": "A 경로 차단 후에도 재현", "source": "log://2"}],
                "hypotheses": [{"id": "H_A", "status": "rejected", "evidence_id": "E_NEG"}],
            })]),
            _response(tool_calls=[_tool_call("c3", "record_state", {
                "hypotheses": [{"id": "H_A", "statement": "A 경로가 원인이다", "status": "active"}],
            })]),
            _response(tool_calls=[_tool_call("c4", "finish_task", {
                "report": "정리했습니다. " + ("확인됨. " * 60),
            })]),
        ]
        stub = ModelStub(responses)

        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 5), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", stub):
            await bot.on_message(FakeMessage("장애 원인을 조사해줘", self.CHANNEL_ID))

        ledger = bot.channel_ledger[self.CHANNEL_ID]
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")

        # The refusal is handed back to the model as the tool result.
        refusal_payload = serialize(stub.payloads("agent")[-1])
        self.assertIn("이미 반증되었습니다", refusal_payload)
        self.assertIn("reopen", refusal_payload)

    async def test_reopen_with_new_evidence_is_the_only_way_back_to_active(self):
        responses = [
            _response(tool_calls=[_tool_call("c1", "record_state", {
                "evidence": [{"id": "E_POS", "summary": "A 경로 오류 로그", "source": "log://1"}],
                "hypotheses": [{"id": "H_A", "statement": "A 경로가 원인이다", "evidence_id": "E_POS"}],
            })]),
            _response(tool_calls=[_tool_call("c2", "record_state", {
                "evidence": [{"id": "E_NEG", "summary": "A 경로 차단 후에도 재현", "source": "log://2"}],
                "hypotheses": [{"id": "H_A", "status": "rejected", "evidence_id": "E_NEG"}],
            })]),
            _response(tool_calls=[_tool_call("c3", "record_state", {
                "evidence": [{"id": "E_NEW", "summary": "고부하 조건에서 A 경로 오류 재현", "source": "log://4"}],
                "hypotheses": [{"id": "H_A", "status": "reopen", "evidence_id": "E_NEW"}],
            })]),
            _response(tool_calls=[_tool_call("c4", "finish_task", {
                "report": "정리했습니다. " + ("확인됨. " * 60),
            })]),
        ]
        stub = ModelStub(responses)

        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 5), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 99), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "create_streaming_completion", stub):
            await bot.on_message(FakeMessage("장애 원인을 조사해줘", self.CHANNEL_ID))

        self.assertEqual(
            bot.channel_ledger[self.CHANNEL_ID].hypothesis_marker("H_A"), "H_A=active@v3"
        )


class CheckpointFailureTest(unittest.IsolatedAsyncioTestCase):
    CHANNEL_ID = 987654401

    def tearDown(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(self.CHANNEL_ID)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_active_runs,
            bot.channel_ledger,
        ):
            state.pop(self.CHANNEL_ID, None)

    async def test_failed_checkpoint_leaves_no_success_marker(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(self.CHANNEL_ID)
        agent_responses = [
            _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
            _response(tool_calls=[_tool_call("c2", "bash_exec", {"command": "probe2"})]),
            _response(tool_calls=[_tool_call("c3", "finish_task", {
                "report": "정리했습니다. " + ("확인됨. " * 60),
            })]),
        ]
        stub = ModelStub(agent_responses)

        async def failing_or_agent(**kwargs):
            messages = kwargs.get("messages") or []
            if "AI 리포터" in bot._msg_content(messages[0] if messages else {}):
                raise RuntimeError("checkpoint backend down")
            return await stub(**kwargs)

        with tempfile.TemporaryDirectory() as log_dir, \
                run_catalog_patch(bot, log_dir), \
                patch.object(bot, "MAX_AGENT_LOOPS", 4), \
                patch.object(bot, "CHECKPOINT_INTERVAL", 2), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99), \
                patch.object(bot, "tool_bash_exec", AsyncMock(return_value=BASH_RESULT)), \
                patch.object(bot, "create_streaming_completion", failing_or_agent):
            await bot.on_message(FakeMessage("장애 원인을 조사해줘", self.CHANNEL_ID))

        last_agent_payload = serialize(stub.payloads("agent")[-1])
        self.assertIn("중간 보고서 생성 실패", last_agent_payload)
        self.assertNotIn("중간 보고서 제출 완료", last_agent_payload)


if __name__ == "__main__":
    unittest.main()
