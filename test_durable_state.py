"""이슈 #6의 재현 시나리오를 회귀 테스트로 고정한다.

런 상태가 메모리에만 있어 재시작 후 Step 1부터 다시 시작하던 문제를 닫는다.
세 시나리오를 그대로 옮겼다: (1) 병렬 도구 그룹 하나를 완료한 뒤 저장하고 새
프로세스 객체로 복원, (2) 체크포인트 커밋 전/후 중단의 복구 결과 비교,
(3) 롤오버 직후 재시작에서 요약·tail·다음 커서 유지.

모든 식별자는 합성이다. 실제 토큰·ID·경로는 쓰지 않는다.
"""

import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage, TEST_USER_ID
from test_terminal_state import _response, _tool_call

import bot
import run_state
import run_workspace
from ledger import ResearchLedger
from run_workspace import RunCatalog

CHANNEL_ID = 987654700
ORIGIN_MESSAGE_ID = 700000000000000009
LONG_REPORT = "조사 결과 정리. " + ("확인됨. " * 80)
BASH_RESULT = "[stdout]\nE_NEG: A 경로 차단 후에도 장애 재현\n[exit code: 0]"

ROLLED_MARK = "ROLLEDMARK-x1y2z3"

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

DECLARE_STATE = {
    "goal": "장애 원인 규명",
    "evidence": [{"id": "E_POS", "summary": "A 경로 오류 로그 관측", "source": "log://1"}],
    "hypotheses": [
        {"id": "H_A", "statement": "A 경로가 장애 원인이다", "evidence_id": "E_POS"},
        {"id": "H_B", "statement": "B 캐시가 장애 원인이다", "evidence_id": "E_POS"},
    ],
    "conclusions": [
        {"id": "C_A", "statement": "A 경로 차단으로 장애가 멈춘다", "premises": ["H_A"]}
    ],
}


def refuted_ledger():
    """반증된 가설과 그 때문에 무효가 된 결론을 가진 원장."""
    ledger = ResearchLedger()
    ledger.apply_updates(DECLARE_STATE)
    ledger.apply_updates({
        "evidence": [{"id": "E_NEG", "summary": "A 경로 차단 후에도 장애 재현", "source": "log://2"}],
        "hypotheses": [{"id": "H_A", "status": "rejected", "evidence_id": "E_NEG"}],
    })
    return ledger


class ModelStub:
    """프롬프트의 역할로 호출을 분류하고 payload를 기록한다."""

    def __init__(self, agent_responses, checkpoint_error=None):
        self.agent_responses = list(agent_responses)
        self.checkpoint_error = checkpoint_error
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
            return _response(content=bot.format_hierarchical_summary(
                milestones=["• 구간 (Step 1-1): A 경로 실험 완료"],
                recent_summary=f"{ROLLED_MARK} 최근 구간 상세 요약",
                discoveries=["- 참조/산출물: `findings.md`"],
            ))
        if kind == "checkpoint":
            if self.checkpoint_error is not None:
                raise self.checkpoint_error
            return _response(content=CHECKPOINT_REPORT)
        if kind == "synthesis":
            return _response(content=LONG_REPORT)
        return self.agent_responses.pop(0)


@contextmanager
def _nothing():
    yield


class DurableStateTestCase(unittest.IsolatedAsyncioTestCase):
    """한 임시 트리를 여러 '프로세스'가 이어 쓰는 시나리오용 공용 준비."""

    def setUp(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self._scratch = tempfile.TemporaryDirectory()
        self.root = Path(self._scratch.name)
        self.restart()

    def tearDown(self):
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(CHANNEL_ID)
        self.restart()
        self._scratch.cleanup()

    def restart(self):
        """프로세스 재시작과 같은 상태로 만든다: 모듈 전역은 프로세스와 함께 죽는다."""
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_active_runs,
            bot.channel_ledger,
            bot.channel_run_leases,
            bot.channel_run_owner,
        ):
            state.pop(CHANNEL_ID, None)

    def catalog(self, name="p1"):
        """같은 이름으로 다시 만들면 같은 디스크를 재스캔하는 새 프로세스가 된다."""
        return RunCatalog(self.root / name / "workspace", self.root / name / "logs")

    async def drive(
        self,
        catalog,
        responses,
        request="장애 원인을 조사해줘",
        message_id=ORIGIN_MESSAGE_ID,
        max_loops=6,
        checkpoint_interval=99,
        compaction_interval=99,
        keep_recent_tool_messages=8,
        killed=False,
        tool_result=BASH_RESULT,
        checkpoint_error=None,
    ):
        """스크립트된 모델 응답으로 실제 on_message를 돈다.

        `killed=True`는 SIGKILL과 같은 상태를 남긴다: finally가 돌지 않으므로
        레코드는 종료 표시 없이 그대로 디스크에 남는다.
        """
        stub = ModelStub(responses, checkpoint_error=checkpoint_error)
        self.bash_exec = AsyncMock(return_value=tool_result)
        message = FakeMessage(request, CHANNEL_ID, message_id=message_id)
        kill = (
            patch.object(run_state, "terminate", lambda *args, **kwargs: False)
            if killed
            else _nothing()
        )
        with redirect_stdout(StringIO()), \
                patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot, "MAX_AGENT_LOOPS", max_loops), \
                patch.object(bot, "CHECKPOINT_INTERVAL", checkpoint_interval), \
                patch.object(bot, "ROLLING_COMPACTION_INTERVAL", compaction_interval), \
                patch.object(bot, "KEEP_RECENT_TOOL_MESSAGES", keep_recent_tool_messages), \
                patch.object(bot, "tool_bash_exec", self.bash_exec), \
                patch.object(bot, "create_streaming_completion", stub), \
                kill:
            await bot.on_message(message)
        self.stub = stub
        return message

    def only_run(self, catalog):
        runs = catalog.workspaces(CHANNEL_ID)
        self.assertEqual(len(runs), 1, "채널에 런이 하나만 있어야 합니다")
        return runs[0]

    def records(self, workspace):
        return [
            json.loads(line)
            for line in workspace.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def recover(self, catalog):
        with redirect_stdout(StringIO()), patch.object(bot, "RUN_CATALOG", catalog):
            return bot.recover_interrupted_runs()


class SnapshotRoundTripTest(DurableStateTestCase):
    def _saved(self, workspace, **overrides):
        payload = dict(
            message_id=ORIGIN_MESSAGE_ID,
            next_step=7,
            summary="누적 요약 본문",
            tail=[
                {"role": "user", "content": "장애 원인을 조사해줘"},
                {"role": "assistant", "content": "확인합니다"},
            ],
            ledger=refuted_ledger(),
            interrupt={
                "cancelled": True,
                "reason": "사용자 중단",
                "steering": {"depth": 2, "applied": 1},
            },
            executed_call_ids=["c1", "c2"],
        )
        payload.update(overrides)
        return run_state.save(workspace, **payload)

    def test_3_goal_state_tail_interrupt_and_cursor_round_trip_exactly(self):
        # Production mutation caught: dropping any field a resumed run needs -
        # goal, ledger state, tail, interrupt state, or the next cursor - so the
        # restored run silently continues from a different state than it saved.
        catalog = self.catalog()
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
        saved = self._saved(workspace)

        # 새 프로세스: 같은 디스크를 다시 읽는 새 워크스페이스 객체.
        fresh = self.catalog().lookup_owned(TEST_USER_ID, workspace.run_id)
        restored = run_state.load(fresh)

        self.assertIsNotNone(restored)
        self.assertEqual(restored["run_id"], workspace.run_id)
        self.assertEqual(restored["message_id"], ORIGIN_MESSAGE_ID)
        self.assertEqual(restored["state"], run_state.RUNNING)
        self.assertEqual(restored["next_step"], 7)
        self.assertEqual(restored["summary"], saved["summary"])
        self.assertEqual(restored["tail"], saved["tail"])
        self.assertEqual(restored["interrupt"], saved["interrupt"])
        self.assertEqual(restored["executed_call_ids"], ["c1", "c2"])

        ledger = restored["ledger"]
        self.assertEqual(ledger.goal, "장애 원인 규명")
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")
        self.assertFalse(ledger.conclusion_is_valid("C_A"))
        self.assertEqual(ledger.render(), refuted_ledger().render())

        # 프로그램 상태이지만 디스크에 남는다. 런 워크스페이스와 같은 권한이어야 한다.
        path = run_state.snapshot_path(workspace)
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(path.parent.stat().st_mode)[-3:], "700")

    def test_3_a_record_that_does_not_match_the_schema_is_discarded(self):
        # Production mutation caught: migrating or half-reading a mismatched
        # record instead of discarding it, which resumes a run into a state no
        # code path can produce.
        catalog = self.catalog()
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
        self._saved(workspace)
        path = run_state.snapshot_path(workspace)

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = run_state.SCHEMA + 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(run_state.load(workspace))

        path.write_text("{ this is not json", encoding="utf-8")
        self.assertIsNone(run_state.load(workspace))

        payload["schema"] = run_state.SCHEMA
        payload["run_id"] = "f" * 32
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(run_state.load(workspace))

    def test_6_an_interrupted_save_leaves_the_previous_record_intact(self):
        # Production mutation caught: writing the state file in place, so an
        # interrupt during the write truncates it and takes the whole run's
        # recoverable state with it.
        catalog = self.catalog()
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
        self._saved(workspace, summary="첫 번째 요약", next_step=3)
        before = run_state.snapshot_path(workspace).read_bytes()

        with patch.object(
            run_workspace.os, "replace", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                self._saved(workspace, summary="두 번째 요약", next_step=99)

        self.assertEqual(run_state.snapshot_path(workspace).read_bytes(), before)
        restored = run_state.load(workspace)
        self.assertEqual(restored["summary"], "첫 번째 요약")
        self.assertEqual(restored["next_step"], 3)
        # 임시 파일이 남아 다음 스캔을 오염시키지 않는다.
        self.assertEqual(
            [path.name for path in workspace.root.glob(".state.json*")], []
        )


class LedgerSerializationTest(unittest.TestCase):
    def test_3_refuted_hypothesis_and_invalid_conclusion_survive_the_round_trip(self):
        # Production mutation caught: a snapshot without the ledger, or one that
        # stores derived validity, which can resurrect a rejected hypothesis as
        # fact after a restart.
        ledger = refuted_ledger()
        restored = ResearchLedger.from_dict(ledger.to_dict())

        self.assertEqual(restored.goal, ledger.goal)
        self.assertEqual(restored.revision, ledger.revision)
        self.assertEqual(restored.state_markers(), ledger.state_markers())
        self.assertEqual(restored.render(), ledger.render())
        self.assertEqual(restored.hypothesis_marker("H_A"), "H_A=rejected@v2")
        self.assertFalse(restored.conclusion_is_valid("C_A"))
        self.assertEqual(restored.stale_premises("C_A"), ["H_A"])

    def test_3_reopen_still_needs_evidence_not_already_cited_after_restore(self):
        # Production mutation caught: losing the transition trail, which is what
        # makes an already-cited reopen refusable.
        restored = ResearchLedger.from_dict(refuted_ledger().to_dict())
        self.assertEqual(restored.cited_evidence("H_A"), ["E_POS", "E_NEG"])

        report, refused = restored.apply_updates_with_status({
            "hypotheses": [{"id": "H_A", "status": "reopen", "evidence_id": "E_NEG"}]
        })
        self.assertTrue(refused)
        self.assertIn("이미 이 가설의 전이에서 인용되었습니다", report)
        self.assertEqual(restored.hypothesis_marker("H_A"), "H_A=rejected@v2")

    def test_3_a_malformed_ledger_payload_is_refused(self):
        # Production mutation caught: accepting a partially readable ledger,
        # which restores an inconsistent authoritative state.
        for payload in (
            "not a dict",
            {"goal": "x"},
            {"goal": "x", "revision": "one", "evidence": [], "hypotheses": [], "conclusions": []},
            {"goal": "x", "revision": 1, "evidence": [{"summary": "no id"}],
             "hypotheses": [], "conclusions": []},
            {"goal": "x", "revision": 1, "evidence": [], "conclusions": [],
             "hypotheses": [{"id": "H", "transitions": [{"unexpected": 1}]}]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ResearchLedger.from_dict(payload)


class SnapshotBoundaryTest(DurableStateTestCase):
    def test_2_the_tail_never_cuts_across_a_parallel_call_result_group(self):
        # Production mutation caught: bounding the tail by message count alone,
        # which can save an assistant tool-call group whose results are missing
        # (or results whose instruction is missing) - a payload that 400s and a
        # side effect that runs twice.
        payload = [
            {"role": "system", "content": "시스템"},
            {"role": "user", "content": "목표"},
            {
                "role": "assistant",
                "content": "완결된 그룹",
                "tool_calls": [
                    {"id": "done1", "type": "function",
                     "function": {"name": "bash_exec", "arguments": "{}"}},
                    {"id": "done2", "type": "function",
                     "function": {"name": "bash_exec", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "done1", "name": "bash_exec", "content": "ok1"},
            {"role": "tool", "tool_call_id": "done2", "name": "bash_exec", "content": "ok2"},
            {
                "role": "assistant",
                "content": "결과가 아직 없는 그룹",
                "tool_calls": [
                    {"id": "half1", "type": "function",
                     "function": {"name": "bash_exec", "arguments": "{}"}},
                    {"id": "half2", "type": "function",
                     "function": {"name": "bash_exec", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "half1", "name": "bash_exec", "content": "ok"},
        ]

        tail = bot.snapshot_tail(payload)

        self.assertEqual(self._unsettled(tail), [])
        self.assertEqual(self._orphan_results(tail), [])
        self.assertNotIn("half1", json.dumps(tail, ensure_ascii=False))
        self.assertIn("done2", json.dumps(tail, ensure_ascii=False))

        # 앞을 자를 때도 짝 없는 결과를 남기지 않는다.
        cut = bot.snapshot_tail(payload, max_messages=3)
        self.assertEqual(self._orphan_results(cut), [])

    async def test_2_the_saved_record_of_a_real_parallel_group_holds_every_result(self):
        # Production mutation caught: snapshotting between dispatch and results,
        # so a restart replays half of a parallel group.
        catalog = self.catalog()
        await self.drive(
            catalog,
            [
                _response(content="두 도구를 동시에 실행합니다", tool_calls=[
                    _tool_call("c1", "bash_exec", {"command": "reproduce.sh"}),
                    _tool_call("c2", "bash_exec", {"command": "collect.sh"}),
                ]),
                _response(tool_calls=[_tool_call("c3", "finish_task", {"report": LONG_REPORT})]),
            ],
            killed=True,
        )
        record = run_state.load(self.only_run(catalog))

        self.assertIsNotNone(record)
        self.assertEqual(record["state"], run_state.RUNNING)
        self.assertEqual(record["next_step"], 2)
        self.assertEqual(record["executed_call_ids"], ["c1", "c2"])
        self.assertEqual(self._unsettled(record["tail"]), [])
        self.assertEqual(self._orphan_results(record["tail"]), [])

    @staticmethod
    def _call_ids(message):
        ids = []
        for call in bot._msg_tool_calls(message):
            ids.append(call.get("id") if isinstance(call, dict) else getattr(call, "id", None))
        return ids

    def _unsettled(self, tail):
        """결과가 오지 않은 도구 호출 id."""
        settled = {
            item.get("tool_call_id")
            for item in tail
            if bot._msg_role(item) == "tool"
        }
        return [
            call_id
            for item in tail
            for call_id in self._call_ids(item)
            if call_id not in settled
        ]

    def _orphan_results(self, tail):
        """지시를 잃은 도구 결과 id."""
        instructed = {
            call_id for item in tail for call_id in self._call_ids(item)
        }
        return [
            item.get("tool_call_id")
            for item in tail
            if bot._msg_role(item) == "tool"
            and item.get("tool_call_id") not in instructed
        ]


class RestartRecoveryTest(DurableStateTestCase):
    async def _crash_after_one_group(self, catalog):
        await self.drive(
            catalog,
            [
                _response(content="상태를 기록하고 재현합니다", tool_calls=[
                    _tool_call("c1", "bash_exec", {"command": "reproduce.sh"}),
                    _tool_call("c2", "record_state", DECLARE_STATE),
                ]),
                _response(tool_calls=[_tool_call("c3", "finish_task", {"report": LONG_REPORT})]),
            ],
            killed=True,
        )
        return self.only_run(catalog)

    async def test_1_restart_resumes_the_same_run_at_the_next_step(self):
        # Production mutation caught: rebuilding the payload and the step cursor
        # from scratch on every request, so a restart starts the same goal again
        # at Step 1 and re-runs its side effects.
        crashed = await self._crash_after_one_group(self.catalog())
        record = run_state.load(crashed)
        self.assertEqual(record["state"], run_state.RUNNING)
        resume_step = record["next_step"]

        self.restart()
        fresh = self.catalog()
        self.assertEqual(self.recover(fresh)["recovered"], 1)

        await self.drive(
            fresh,
            [
                # 재시작 전에 이미 실행한 호출을 모델이 다시 요청해도 부작용은
                # 다시 일어나지 않아야 한다.
                _response(tool_calls=[_tool_call("c1", "bash_exec", {"command": "reproduce.sh"})]),
                _response(tool_calls=[_tool_call("c9", "finish_task", {"report": LONG_REPORT})]),
            ],
            message_id=ORIGIN_MESSAGE_ID,
        )

        resumed = self.only_run(fresh)
        self.assertEqual(resumed.run_id, crashed.run_id)
        self.assertEqual(self.bash_exec.await_count, 0)

        # 복원된 원장이 재시작을 넘어 살아남는다.
        ledger = bot.channel_ledger[CHANNEL_ID]
        self.assertEqual(ledger.goal, "장애 원인 규명")
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=active@v1")

        # 이미 실행한 호출은 결정적 구조화 결과로 차단된다.
        blocked = json.loads(self.bash_result_of(self.stub, "c1"))
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["reason"], "already_executed")

        records = self.records(resumed)
        resumed_records = records[_last_index(records, "run_resumed"):]
        self.assertTrue(resumed_records, "run_resumed 레코드가 없습니다")
        self.assertEqual(resumed_records[0]["next_step"], resume_step)
        steps = [
            item["step"] for item in resumed_records
            if item["kind"] in ("model_response", "tool_call")
        ]
        self.assertTrue(steps)
        self.assertEqual(min(steps), resume_step)

    @staticmethod
    def bash_result_of(stub, call_id):
        """가장 나중에 붙은 결과. 복원한 tail에도 같은 id의 옛 결과가 들어 있다."""
        for messages in reversed(stub.payloads("agent")):
            for item in reversed(messages):
                if bot._msg_role(item) == "tool" and item.get("tool_call_id") == call_id:
                    return bot._msg_content(item)
        raise AssertionError(f"{call_id} 결과가 payload에 없습니다")

    async def test_4_startup_never_emits_a_second_step_1_for_the_same_message(self):
        # Production mutation caught: no startup detection of unterminated
        # records, so the same originating message is silently replayed from
        # Step 1 and Step 1 alone cannot tell a restart from a new request.
        crashed = await self._crash_after_one_group(self.catalog())
        self.restart()

        fresh = self.catalog()
        self.recover(fresh)
        armed = [
            item for item in self.records(crashed) if item["kind"] == "run_resume_armed"
        ]
        self.assertEqual(len(armed), 1)

        message = await self.drive(
            fresh,
            [_response(tool_calls=[_tool_call("c9", "finish_task", {"report": LONG_REPORT})])],
            message_id=ORIGIN_MESSAGE_ID,
        )

        records = self.records(self.only_run(fresh))
        resumed = records[_last_index(records, "run_resumed"):]
        first_steps = [
            item["step"] for item in resumed
            if item["kind"] in ("model_response", "tool_call")
        ]
        self.assertTrue(first_steps)
        self.assertNotIn(1, first_steps)

        # 조용히 이어가지 않는다: 재개 사실이 채널에도 남는다.
        notices = "\n".join(message.channel.sent + message.replies)
        self.assertIn("재개", notices)

    async def test_4_an_unusable_record_is_aborted_exactly_once(self):
        # Production mutation caught: a corrupt or unresumable record either
        # crashing startup or being retried on every restart, with no single
        # explicit abort to read afterwards.
        crashed = await self._crash_after_one_group(self.catalog())
        run_state.snapshot_path(crashed).write_text("{ truncated", encoding="utf-8")
        self.restart()

        fresh = self.catalog()
        summary = self.recover(fresh)
        self.assertEqual(summary, {"recovered": 0, "aborted": 1})
        aborts = [item for item in self.records(crashed) if item["kind"] == "run_abort"]
        self.assertEqual(len(aborts), 1)
        self.assertFalse(run_state.snapshot_path(crashed).exists())

        # 두 번째 시작은 아무것도 다시 중단하지 않는다.
        self.assertEqual(
            self.recover(self.catalog()), {"recovered": 0, "aborted": 0}
        )

    async def test_1_reset_deletes_the_record_so_the_old_run_cannot_resurrect(self):
        # Production mutation caught: !reset clearing memory only, so the run
        # the user just discarded comes back from disk after a restart.
        crashed = await self._crash_after_one_group(self.catalog())
        self.assertTrue(run_state.snapshot_path(crashed).exists())
        self.restart()

        fresh = self.catalog()
        with redirect_stdout(StringIO()), patch.object(bot, "RUN_CATALOG", fresh):
            await bot.on_message(FakeMessage("!reset", CHANNEL_ID))

        self.assertFalse(run_state.snapshot_path(crashed).exists())
        self.assertEqual(
            self.recover(self.catalog()), {"recovered": 0, "aborted": 0}
        )


class CheckpointBoundaryTest(DurableStateTestCase):
    """재현 2: 체크포인트 커밋 전/후 중단의 복구 결과를 비교한다."""

    async def _crash_around_checkpoint(self, name, checkpoint_error):
        catalog = self.catalog(name)
        await self.drive(
            catalog,
            [
                _response(content="상태를 기록합니다", tool_calls=[
                    _tool_call("c1", "bash_exec", {"command": "reproduce.sh"}),
                    _tool_call("c2", "record_state", DECLARE_STATE),
                ]),
                _response(tool_calls=[_tool_call("c3", "finish_task", {"report": LONG_REPORT})]),
            ],
            max_loops=4,
            checkpoint_interval=1,
            killed=True,
            checkpoint_error=checkpoint_error,
        )
        return run_state.load(self.only_run(catalog))

    async def test_1_a_crash_before_the_checkpoint_commit_loses_only_the_correction(self):
        # Production mutation caught: snapshotting before the interim report's
        # ledger corrections are applied, so a crash in the next interval loses a
        # correction that was already committed to the authoritative state.
        before = await self._crash_around_checkpoint("before", RuntimeError("보고 단계 중단"))
        self.restart()
        after = await self._crash_around_checkpoint("after", None)

        # 두 경우 모두 완결된 그룹과 다음 커서는 살아남는다.
        for record in (before, after):
            self.assertEqual(record["next_step"], 2)
            self.assertEqual(record["executed_call_ids"], ["c1", "c2"])

        # 차이는 정정 하나뿐이다: 커밋 전에는 없고, 커밋 후에는 있다.
        self.assertEqual(before["ledger"].hypothesis_marker("H_B"), "H_B=active@v1")
        self.assertEqual(after["ledger"].hypothesis_marker("H_B"), "H_B=rejected@v2")


class RolloverBoundaryTest(DurableStateTestCase):
    """재현 3: 롤오버 직후 재시작에서 요약·tail·다음 커서가 유지된다."""

    async def _crash_after_rollover(self):
        catalog = self.catalog()
        await self.drive(
            catalog,
            [
                _response(content="첫 구간", tool_calls=[
                    _tool_call("c1", "bash_exec", {"command": "reproduce.sh"}),
                ]),
                _response(content="둘째 구간", tool_calls=[
                    _tool_call("c2", "bash_exec", {"command": "collect.sh"}),
                ]),
                _response(tool_calls=[_tool_call("c3", "finish_task", {"report": LONG_REPORT})]),
            ],
            max_loops=5,
            compaction_interval=2,
            keep_recent_tool_messages=1,
            killed=True,
        )
        return catalog, run_state.load(self.only_run(catalog))

    async def test_3_restart_after_a_rollover_keeps_summary_tail_and_cursor(self):
        # Production mutation caught: a rollover that leaves no durable trace,
        # so a restart right after it loses the summary the compaction just
        # produced along with the tail it was replaced by.
        _catalog, record = await self._crash_after_rollover()

        self.assertIn(ROLLED_MARK, record["summary"])
        self.assertIn(bot.MILESTONES_SECTION_HEADER, record["summary"])
        self.assertEqual(record["next_step"], 3)
        self.assertTrue(record["tail"])
        self.assertIn("collect.sh", json.dumps(record["tail"], ensure_ascii=False))

    async def test_3_the_rollover_summary_is_written_back_to_channel_memory(self):
        # Production mutation caught: assigning the rolled summary to a closure
        # local only, so every rollover summary is discarded at function exit and
        # even the next message in the same process starts from the stale one.
        await self._crash_after_rollover()
        self.assertIn(ROLLED_MARK, bot.channel_summary[CHANNEL_ID])


class HistoryOverflowTest(DurableStateTestCase):
    async def test_3_history_overflow_merges_instead_of_clobbering_the_summary(self):
        # Production mutation caught: replacing channel_summary with plain chat
        # snippets on overflow, which destroys a restored hierarchical summary
        # and the state markers embedded in it on the first long conversation.
        seeded = bot.update_hierarchical_summary(
            existing_summary="",
            new_recent_summary="H_A=rejected@v2 를 확인한 구간",
            step_range="Step 1-10",
        )
        bot.channel_summary[CHANNEL_ID] = seeded
        bot.channel_history[CHANNEL_ID] = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn {index}"}
            for index in range(bot.MAX_RECENT_TURNS * 2 + 4)
        ]

        await self.drive(
            self.catalog(),
            [_response(tool_calls=[_tool_call("c1", "finish_task", {"report": LONG_REPORT})])],
        )

        merged = bot.channel_summary[CHANNEL_ID]
        self.assertIn("H_A=rejected@v2", merged)
        self.assertIn("이전 대화 요약", merged)


class InterimReportNamingTest(DurableStateTestCase):
    async def test_5_the_discord_interim_report_is_not_called_a_checkpoint(self):
        # Production mutation caught: calling the Discord progress briefing a
        # checkpoint again, which is what made an unrecoverable run look
        # recoverable.
        message = await self.drive(
            self.catalog(),
            [
                _response(content="구간 작업", tool_calls=[
                    _tool_call("c1", "bash_exec", {"command": "reproduce.sh"}),
                ]),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
            ],
            max_loops=4,
            checkpoint_interval=1,
        )

        channel_text = "\n".join(
            message.channel.sent
            + message.replies
            + [
                str(edit)
                for handle in message.reply_handles + message.channel.sent_messages
                for edit in handle.edits
            ]
        )
        self.assertIn("중간 진행 보고서", channel_text)
        self.assertNotIn("체크포인트", channel_text)

    def test_5_the_documentation_does_not_call_it_a_persistent_checkpoint(self):
        # Production mutation caught: documentation still promising a recovery
        # point that the interim report never was.
        readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("Checkpoint Synthesis", readme)
        self.assertIn("state.json", readme)
        self.assertIn("run_state.py", readme)


def _last_index(records, kind):
    for index in range(len(records) - 1, -1, -1):
        if records[index]["kind"] == kind:
            return index
    raise AssertionError(f"{kind} 레코드가 없습니다")


if __name__ == "__main__":
    unittest.main()
