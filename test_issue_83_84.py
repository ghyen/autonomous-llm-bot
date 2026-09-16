"""Comprehensive regression tests for Issue #83 and Issue #84.

Issue #83: record_stale gate reset only on substantive updates, duplicate evidence
detection, and deadlock escape hatches.
Issue #84: Context-aware block directives for findings.md/plan.md, line-based slicing
(offset/limit) in read_file, and partitioned loop guard fingerprints.
"""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage, run_catalog_patch

import bot
import ledger
import run_workspace
import workspace_io


CHANNEL_ID = 987654899
LONG_REPORT = "검증 완료 보고서입니다. " + ("세부 내용 확인. " * 50)
SUCCESS_RESULT = "[stdout]\nok\n[exit code: 0]"


def _response(content="", tool_calls=()):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    reasoning_content="",
                    reasoning="",
                    tool_calls=list(tool_calls),
                )
            )
        ]
    )


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


class Issue83LedgerTest(unittest.IsolatedAsyncioTestCase):
    """Issue #83 ResearchLedger unit tests for delta classification & deduplication."""

    def setUp(self):
        self.ledger = ledger.ResearchLedger()

    def test_duplicate_evidence_detection_and_substantive_flag(self):
        # 1. First registration: substantive new evidence
        res1 = self.ledger.apply_updates_with_status({
            "evidence": [{
                "id": "E_CONTEXT_FLUSH",
                "summary": "컨텍스트 긴급 플러시 후 재개. Phase 1 완료",
                "source": "system",
            }]
        })
        self.assertTrue(res1.delta.substantive)
        self.assertEqual(res1.delta.new_evidence, ["E_CONTEXT_FLUSH"])
        self.assertEqual(res1.delta.duplicate_evidence, [])

        # 2. Similar summary with different ID -> duplicate, non-substantive
        res2 = self.ledger.apply_updates_with_status({
            "evidence": [{
                "id": "E_CTX_RESTART",
                "summary": "컨텍스트 플러시 후 재개. Phase 1 완료",
                "source": "system",
            }]
        })
        self.assertFalse(res2.delta.substantive)
        self.assertEqual(res2.delta.duplicate_evidence, ["E_CTX_RESTART"])
        self.assertEqual(res2.delta.new_evidence, [])
        self.assertIn("[중복 증거 감지]", res2.report)

        # 3. Normalized ID match with same semantic intent -> duplicate
        res3 = self.ledger.apply_updates_with_status({
            "evidence": [{
                "id": "e-context-flush",
                "summary": "플러시 후 재개",
                "source": "system",
            }]
        })
        self.assertFalse(res3.delta.substantive)
        self.assertEqual(res3.delta.duplicate_evidence, ["e-context-flush"])

        # 4. Truly new evidence -> substantive
        res4 = self.ledger.apply_updates_with_status({
            "evidence": [{
                "id": "E_PORT_DISCOVERY",
                "summary": "비인증 /api/v1/cycles 엔드포인트에서 200 OK 수신",
                "source": "curl http://localhost:8080",
            }]
        })
        self.assertTrue(res4.delta.substantive)
        self.assertEqual(res4.delta.new_evidence, ["E_PORT_DISCOVERY"])

    def test_substantive_delta_for_retraction_hypothesis_and_goal(self):
        # Initial state
        self.ledger.apply_updates_with_status({
            "evidence": [{"id": "E1", "summary": "초기 증거"}],
            "hypotheses": [{"id": "H1", "statement": "초기 가설", "status": "active"}],
        })

        # Goal change
        res_goal = self.ledger.apply_updates_with_status({"goal": "새로운 목표 설정"})
        self.assertTrue(res_goal.delta.substantive)
        self.assertTrue(res_goal.delta.goal_changed)

        # Retraction
        res_retract = self.ledger.apply_updates_with_status({
            "evidence": [{"id": "E1", "retracted": True, "note": "반증됨"}]
        })
        self.assertTrue(res_retract.delta.substantive)
        self.assertEqual(res_retract.delta.retracted_evidence, ["E1"])

        # Hypothesis transition with a new valid evidence
        self.ledger.add_evidence("E2", summary="반증 사실 발견")
        res_hypo = self.ledger.apply_updates_with_status({
            "hypotheses": [{"id": "H1", "status": "rejected", "evidence_id": "E2"}]
        })
        self.assertTrue(res_hypo.delta.substantive)
        self.assertEqual(res_hypo.delta.hypotheses_changed, ["H1"])

        # Conclusion addition
        res_conc = self.ledger.apply_updates_with_status({
            "conclusions": [{"id": "C1", "statement": "확정 결론", "premises": ["H1"]}]
        })
        self.assertTrue(res_conc.delta.substantive)
        self.assertEqual(res_conc.delta.conclusions_changed, ["C1"])

    async def test_tool_record_state_duplicate_status_and_no_change(self):
        # Register initial evidence
        await bot.tool_record_state(
            self.ledger,
            {"evidence": [{"id": "E1", "summary": "최초 관측 사실"}]}
        )

        # Duplicate registration returns duplicate status
        dup_res = await bot.tool_record_state(
            self.ledger,
            {"evidence": [{"id": "E2", "summary": "최초 관측 사실"}]}
        )
        self.assertTrue(dup_res.startswith("[record_state status: duplicate]"))
        self.assertIn("[중복 증거 감지]", dup_res)

        # no_change=True returns success with notice
        no_change_res = await bot.tool_record_state(
            self.ledger,
            {"evidence": [{"id": "E3", "summary": "최초 관측 사실"}], "no_change": True}
        )
        self.assertTrue(no_change_res.startswith("[record_state status: success]"))
        self.assertIn("[명시적 변경 없음(no_change)]", no_change_res)


class Issue84WorkspaceTest(unittest.TestCase):
    """Issue #84 line-based slicing and cache separation unit tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        # Create a 10-line file
        self.file_path = self.root / "sample.txt"
        self.lines = [f"Line {i}: data value {i}\n" for i in range(1, 11)]
        self.file_path.write_text("".join(self.lines), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_workspace(self):
        return run_workspace.RunWorkspace(
            run_id="run_test",
            owner_id=1,
            channel_id=1,
            root=self.root,
            log_path=self.root / "test.jsonl",
            status="active",
            created_at="2026-09-16T00:00:00Z",
            updated_at="2026-09-16T00:00:00Z",
        )

    def test_workspace_io_read_file_slicing(self):
        # 1. Full read (no offset/limit)
        full_envelope = workspace_io.read_file(self.root, "sample.txt")
        self.assertEqual(full_envelope["status"], "success")
        self.assertEqual(full_envelope["content"], "".join(self.lines))
        self.assertNotIn("offset", full_envelope)

        # 2. Slice read: offset=3, limit=4 (lines 3 to 6)
        slice_envelope = workspace_io.read_file(self.root, "sample.txt", offset=3, limit=4)
        self.assertEqual(slice_envelope["status"], "success")
        self.assertEqual(slice_envelope["offset"], 3)
        self.assertEqual(slice_envelope["limit"], 4)
        self.assertEqual(slice_envelope["total_lines"], 10)
        expected_content = "".join(self.lines[2:6])
        self.assertEqual(slice_envelope["content"], expected_content)

        # 3. Beyond total lines
        beyond_envelope = workspace_io.read_file(self.root, "sample.txt", offset=15, limit=5)
        self.assertEqual(beyond_envelope["status"], "success")
        self.assertEqual(beyond_envelope["content"], "")
        self.assertEqual(beyond_envelope["offset"], 15)

    def test_remember_worker_read_does_not_short_circuit_sliced_reads(self):
        workspace = self._make_workspace()
        full_res = workspace_io.read_file(self.root, "sample.txt")
        # 1. First full read cached
        env1 = workspace.remember_worker_read(full_res)
        self.assertEqual(env1["status"], "success")

        # 2. Second full read returns unchanged
        env2 = workspace.remember_worker_read(dict(full_res))
        self.assertEqual(env2["status"], "unchanged")

        # 3. Sliced read must NOT return unchanged even though revision matches
        slice_res = workspace_io.read_file(self.root, "sample.txt", offset=2, limit=2)
        env3 = workspace.remember_worker_read(dict(slice_res))
        self.assertEqual(env3["status"], "success")
        self.assertEqual(env3["offset"], 2)
        self.assertIn("Line 2", env3["content"])

    def test_read_file_fingerprint_partitioning(self):
        fp_full = bot._tool_fingerprint("read_file", {"path": "findings.md"})
        fp_slice1 = bot._tool_fingerprint("read_file", {"path": "findings.md", "offset": 1, "limit": 20})
        fp_slice2 = bot._tool_fingerprint("read_file", {"path": "findings.md", "offset": 21, "limit": 20})

        self.assertNotEqual(fp_full, fp_slice1)
        self.assertNotEqual(fp_slice1, fp_slice2)
        # Empty/None offset and limit should match full fingerprint
        fp_none = bot._tool_fingerprint("read_file", {"path": "findings.md", "offset": None, "limit": None})
        self.assertEqual(fp_full, fp_none)

    def test_context_aware_directive_on_loop_guard_and_unchanged(self):
        # 1. Loop guard directive for findings.md
        blocked_raw = bot._blocked_tool_result(
            "loop_guard_repeat",
            "read_file",
            8,
            1,
            first_step=10,
            target_path="findings.md",
        )
        blocked = json.loads(blocked_raw)
        self.assertIn("[📌 기확정 사전 지식]", blocked["directive"])
        self.assertIn("read_file(path='findings.md', offset=..., limit=...)", blocked["directive"])
        self.assertIn("grep -n", blocked["directive"])
        self.assertIn("lookup_trajectory", blocked["directive"])

        # 2. Unchanged directive for plan.md in tool_read_file
        workspace = self._make_workspace()
        plan_file = self.root / "plan.md"
        plan_file.write_text("# Plan\n1. Do something", encoding="utf-8")

        async def run_reads():
            r1 = await bot.tool_read_file(workspace, "plan.md")
            env1 = json.loads(r1)
            self.assertEqual(env1["status"], "success")

            r2 = await bot.tool_read_file(workspace, "plan.md")
            env2 = json.loads(r2)
            self.assertEqual(env2["status"], "unchanged")
            self.assertIn("[내용 변경 없음 (unchanged)]", env2["directive"])
            self.assertIn("[📌 기확정 사전 지식]", env2["directive"])
            self.assertIn("grep -n", env2["directive"])

        import asyncio
        asyncio.run(run_reads())


class Issue83And84EndToEndAgentTest(unittest.IsolatedAsyncioTestCase):
    """End-to-end agent tests verifying stale gate deduplication and deadlock escapes."""

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

    async def run_agent(self, responses):
        self.dispatched_batches = []
        self.model = ModelStub(responses)
        original_dispatch = bot.execute_tools_in_parallel

        async def recording_dispatch(workspace, tool_calls, *args, **kwargs):
            self.dispatched_batches.append([
                (call["id"], call["name"], call["arguments"])
                for call in tool_calls
            ])
            return await original_dispatch(workspace, tool_calls, *args, **kwargs)

        async def fake_bash(workspace, cmd, call_id):
            return SUCCESS_RESULT

        message = FakeMessage("조사 진행해줘", CHANNEL_ID)
        with tempfile.TemporaryDirectory() as log_dir, ExitStack() as stack:
            stack.enter_context(run_catalog_patch(bot, log_dir))
            stack.enter_context(patch.object(bot, "MAX_AGENT_LOOPS", len(responses) + 1))
            stack.enter_context(patch.object(bot, "CHECKPOINT_INTERVAL", 99))
            stack.enter_context(patch.object(bot, "ROLLING_COMPACTION_INTERVAL", 99))
            stack.enter_context(patch.object(bot, "tool_bash_exec", fake_bash))
            stack.enter_context(patch.object(bot, "execute_tools_in_parallel", recording_dispatch))
            stack.enter_context(patch.object(bot, "create_streaming_completion", self.model))
            await bot.on_message(message)
        return message

    def _last_tool_message(self, step_idx):
        payload = self.model.agent_payloads[step_idx]
        return [m for m in payload if bot._msg_role(m) == "tool"][-1]

    async def test_record_stale_gate_ignores_duplicate_evidence(self):
        """Duplicate evidence does not reset stale gate; subsequent bash is blocked."""
        with patch.object(bot, "RECORD_STATE_STALE_STEPS", 2):
            await self.run_agent([
                # Step 1: Initial substantive evidence
                _response(tool_calls=[
                    _tool_call("rec-1", "record_state", {
                        "evidence": [{"id": "E1", "summary": "정상 신규 증거"}]
                    }),
                ]),
                # Step 2: Normal tool call (last_record_step is step 1, stale = 1 < 2)
                _response(tool_calls=[_tool_call("bash-1", "bash_exec", {"command": "echo 1"})]),
                # Step 3: Stale threshold reached (stale = 2 >= 2). We provide duplicate evidence E2.
                _response(tool_calls=[
                    _tool_call("rec-dup", "record_state", {
                        "evidence": [{"id": "E2", "summary": "정상 신규 증거"}]
                    }),
                ]),
                # Step 4: Stale gate was NOT reset! bash call must be blocked by record_stale
                _response(tool_calls=[_tool_call("bash-2", "bash_exec", {"command": "echo 2"})]),
                _response(tool_calls=[_tool_call("finish", "finish_task", {"report": LONG_REPORT})]),
            ])

        # Verify dispatched calls
        dispatched_names = [
            [call[1] for call in batch] for batch in self.dispatched_batches
        ]
        self.assertEqual(
            dispatched_names,
            [["record_state"], ["bash_exec"], ["record_state"]]
        )

        # Check that bash-2 was blocked with reason="record_stale"
        tool_msg = self._last_tool_message(4)
        blocked_info = json.loads(tool_msg["content"])
        self.assertTrue(blocked_info.get("blocked"))
        self.assertEqual(blocked_info.get("reason"), "record_stale")

    async def test_deadlock_escape_after_three_duplicates(self):
        """Three consecutive duplicate records triggers escape hatch and unblocks the gate."""
        with patch.object(bot, "RECORD_STATE_STALE_STEPS", 2):
            await self.run_agent([
                # Step 1: Initial substantive evidence
                _response(tool_calls=[
                    _tool_call("rec-1", "record_state", {
                        "evidence": [{"id": "E1", "summary": "동일 사실"}]
                    }),
                ]),
                # Step 2: Duplicate 1
                _response(tool_calls=[
                    _tool_call("rec-dup-1", "record_state", {
                        "evidence": [{"id": "E2", "summary": "동일 사실"}]
                    }),
                ]),
                # Step 3: Duplicate 2
                _response(tool_calls=[
                    _tool_call("rec-dup-2", "record_state", {
                        "evidence": [{"id": "E3", "summary": "동일 사실"}]
                    }),
                ]),
                # Step 4: Duplicate 3 -> Triggers escape hatch! Gate opens!
                _response(tool_calls=[
                    _tool_call("rec-dup-3", "record_state", {
                        "evidence": [{"id": "E4", "summary": "동일 사실"}]
                    }),
                ]),
                # Step 5: bash_exec should now succeed without record_stale block!
                _response(tool_calls=[_tool_call("bash-after-escape", "bash_exec", {"command": "echo ok"})]),
                _response(tool_calls=[_tool_call("finish", "finish_task", {"report": LONG_REPORT})]),
            ])

        dispatched_names = [
            [call[1] for call in batch] for batch in self.dispatched_batches
        ]
        self.assertEqual(
            dispatched_names,
            [["record_state"], ["record_state"], ["record_state"], ["record_state"], ["bash_exec"]]
        )

    async def test_deadlock_escape_with_explicit_no_change_flag(self):
        """Explicit no_change=True flag triggers escape hatch on first attempt."""
        with patch.object(bot, "RECORD_STATE_STALE_STEPS", 2):
            await self.run_agent([
                # Step 1: Initial substantive evidence
                _response(tool_calls=[
                    _tool_call("rec-1", "record_state", {
                        "evidence": [{"id": "E1", "summary": "사실 A"}]
                    }),
                ]),
                # Step 2: Duplicate evidence with no_change=True -> Escape hatch opens gate
                _response(tool_calls=[
                    _tool_call("rec-no-change", "record_state", {
                        "evidence": [{"id": "E2", "summary": "사실 A"}],
                        "no_change": True,
                    }),
                ]),
                # Step 3: bash_exec succeeds without block
                _response(tool_calls=[_tool_call("bash-1", "bash_exec", {"command": "echo test"})]),
                _response(tool_calls=[_tool_call("finish", "finish_task", {"report": LONG_REPORT})]),
            ])

        dispatched_names = [
            [call[1] for call in batch] for batch in self.dispatched_batches
        ]
        self.assertEqual(
            dispatched_names,
            [["record_state"], ["record_state"], ["bash_exec"]]
        )


if __name__ == "__main__":
    unittest.main()
