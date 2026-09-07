"""Issue #49 explicit Tier 1/Tier 2/Tier 3 memory behavior."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage  # sets required config env before bot imports
import bot
import trajectory
from deadlines import StageTimeout
from ledger import ResearchLedger


class TieredMemoryFormatTest(unittest.TestCase):
    def test_format_and_parse_tiered_summary(self):
        tier3 = "- Step 1-10: 공개 API 3종이 모두 401 인증 게이트웨이에서 중단됨."
        tier2 = [
            "[Step 11: bash_exec({\"command\":\"curl /profile\"}) -> 401]",
            "[Step 12: web_search({\"query\":\"cached profile\"}) -> no result]",
        ]
        discoveries = ["- 파일: `findings.md`", "- 스킬: `skills/parse_logs.py`"]

        formatted = bot.format_tiered_summary(
            tier3=tier3,
            tier3_through=10,
            tier2_lines=tier2,
            discoveries=discoveries,
        )

        self.assertIn(bot.TIER3_SECTION_HEADER, formatted)
        self.assertIn(bot.TIER3_AUTHORITY_NOTICE, formatted)
        self.assertIn("Step 1-10", formatted)
        self.assertIn(bot.TIER2_SECTION_HEADER, formatted)
        self.assertIn(tier2[0], formatted)
        self.assertIn(bot.ARTIFACTS_SECTION_HEADER, formatted)

        parsed = bot.parse_tiered_summary(formatted)
        self.assertEqual(parsed["tier3"], tier3)
        self.assertEqual(parsed["tier3_through"], 10)
        self.assertEqual(parsed["tier2"], tier2)
        self.assertEqual(parsed["discoveries"], discoveries)

    def test_obsolete_summary_formats_are_not_migrated_or_parsed(self):
        # Production mutations caught: compatibility parsers keep old authority
        # prose alive, or trust a pre-integrity Tier 3 coverage watermark.
        obsolete = (
            (
                "## 🏛️ 장기 마일스톤 색인\n- H_OLD is active\n\n"
                "## 🔍 직전 구간 상세 요약\nold detail"
            ),
            (
                f"{bot.TIER3_SECTION_HEADER}\n{bot.TIER3_AUTHORITY_NOTICE}\n"
                "적용 범위: Step 1-10\n- Step 1-10: old unverified procedure\n\n"
                f"{bot.TIER2_SECTION_HEADER}\n(아직 중기 스텝 인덱스 없음)"
            ),
        )

        for old in obsolete:
            with self.subTest(summary=old.splitlines()[0]):
                self.assertEqual(
                    bot.parse_tiered_summary(old),
                    {"tier3": "", "tier3_through": 0, "tier2": [], "discoveries": []},
                )
        self.assertFalse(hasattr(bot, "parse_hierarchical_summary"))
        self.assertFalse(hasattr(bot, "update_hierarchical_summary"))

    def test_summary_clipping_reserves_space_for_omission_marker(self):
        summary = bot._clip_summary_text(
            "x" * (bot.ROLLING_SUMMARY_MAX_CHARS + 1),
            bot.ROLLING_SUMMARY_MAX_CHARS,
        )

        self.assertLessEqual(len(summary), bot.ROLLING_SUMMARY_MAX_CHARS)
        self.assertTrue(summary.endswith(" ...[생략]"))

    def test_tier1_split_keeps_ten_complete_parallel_tool_groups_verbatim(self):
        # Production mutation caught: counting tool result messages instead of
        # assistant/tool groups makes parallel calls consume the retention budget.
        messages = [{"role": "system", "content": "system"}]
        for step in range(1, 13):
            call_count = 3 if step % 2 else 2
            calls = [
                {
                    "id": f"call-{step}-{index}",
                    "type": "function",
                    "function": {"name": "bash_exec", "arguments": "{}"},
                }
                for index in range(call_count)
            ]
            messages.append({
                "role": "assistant",
                "content": f"Step {step} reasoning",
                "tool_calls": calls,
            })
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": "bash_exec",
                    "content": f"verbatim-result-{call['id']}",
                }
                for call in calls
            )

        old, recent = bot.split_recent_agent_context(messages)

        recent_groups = [
            item for item in recent
            if bot._msg_role(item) == "assistant" and bot._msg_tool_calls(item)
        ]
        old_groups = [
            item for item in old
            if bot._msg_role(item) == "assistant" and bot._msg_tool_calls(item)
        ]
        announced = {
            call["id"]
            for item in recent_groups
            for call in bot._msg_tool_calls(item)
        }
        settled = {
            item["tool_call_id"]
            for item in recent
            if bot._msg_role(item) == "tool"
        }

        self.assertEqual(len(recent_groups), 10)
        self.assertEqual(len(old_groups), 2)
        self.assertEqual(bot._msg_content(recent[0]), "Step 3 reasoning")
        self.assertEqual(bot._msg_content(recent[-1]), "verbatim-result-call-12-1")
        self.assertEqual(len(settled), 25)
        self.assertEqual(settled, announced)


class RolloverTieredIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def _make_payload(self, workspace):
        ledger = ResearchLedger()
        ledger.set_goal("보안 취약점 조사")
        ledger.add_evidence("E_AUTH", "인증 토큰 누락", "log://auth")
        ledger.declare_hypothesis("H_AUTH", "인증 모듈 결함", "active", "E_AUTH")

        payload = [{"role": "system", "content": bot.build_system_content(workspace, ledger)}]
        for step in range(1, 15):
            payload.append({
                "role": "assistant",
                "content": f"Step {step} 진행",
                "tool_calls": [{
                    "id": f"call_{step}",
                    "type": "function",
                    "function": {"name": "bash_exec", "arguments": '{"command":"echo test"}'},
                }],
            })
            payload.append({
                "role": "tool",
                "tool_call_id": f"call_{step}",
                "name": "bash_exec",
                "content": f"[stdout]\nStep {step} output\n[exit code: 0]",
            })
        return ledger, payload

    @staticmethod
    def _seed_trajectory(workspace, through=40):
        for step in range(1, through + 1):
            command = f"curl https://example.com/step/{step}"
            trajectory.append_tool_group(
                workspace,
                step,
                [{
                    "id": f"traj-{step}",
                    "name": "bash_exec",
                    "arguments": {"command": command},
                    "failed": step <= 10,
                }],
                [
                    f"[stderr]\nHTTP 401 at step {step}\n[exit code: 22]"
                    if step <= 10
                    else f"[stdout]\nresult-{step}\n[exit code: 0]"
                ],
                {f"traj-{step}"},
            )

    async def test_rollover_uses_trajectory_for_tier2_and_procedural_tier3(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            with patch.object(
                bot,
                "run_completion_stage",
                AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
            ):
                rolled, summary = await bot.rollover_agent_context(
                    workspace, payload, existing_summary="", step_num=40, ledger=ledger
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 10)
        self.assertIn("Step 1-10", parsed["tier3"])
        self.assertEqual(len(parsed["tier2"]), 20)
        self.assertTrue(parsed["tier2"][0].startswith("[Step 11:"))
        self.assertTrue(parsed["tier2"][-1].startswith("[Step 30:"))
        self.assertNotIn("H_AUTH=active@v1", summary)
        self.assertNotIn("보안 취약점 조사", parsed["tier3"])

        system = bot._msg_content(rolled[0])
        self.assertIn(bot.TIER3_SECTION_HEADER, system)
        self.assertIn("H_AUTH=active@v1", system)
        self.assertTrue(system.rstrip().endswith(ledger.render().rstrip()))
        recent_tools = [item for item in rolled if bot._msg_role(item) == "tool"]
        self.assertEqual(len(recent_tools), 10)
        self.assertEqual(bot._msg_content(recent_tools[-1]), "[stdout]\nStep 14 output\n[exit code: 0]")

    async def test_rollover_does_not_advance_past_hash_invalid_trajectory(self):
        # Production mutation caught: procedural_source can discard a corrupt
        # suffix while rollover still persists tier3_through=tier3_end.
        import json

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            path = trajectory.trajectory_path(workspace)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            records[5]["result"] = "forged-result"
            path.write_text(
                "".join(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )
            with patch.object(
                bot,
                "run_completion_stage",
                AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
            ):
                rolled, summary = await bot.rollover_agent_context(
                    workspace, payload, existing_summary="", step_num=40, ledger=ledger
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 5)
        self.assertIn("Step 1-5", parsed["tier3"])
        self.assertNotIn("Step 6", parsed["tier3"])
        self.assertTrue(
            bot._msg_content(rolled[0]).rstrip().endswith(ledger.render().rstrip())
        )

    async def test_rollover_stops_at_durable_internal_or_trailing_trajectory_gap(self):
        # Production mutation caught: a syntactically complete sparse chain can
        # hide a caught zero-byte append failure unless its first gap is a cap.
        for recorded_steps in ((1,), (1, 3)):
            with self.subTest(recorded_steps=recorded_steps):
                with tempfile.TemporaryDirectory() as temp_dir:
                    workspace = SimpleNamespace(root=temp_dir)
                    ledger, payload = self._make_payload(workspace)
                    for step in recorded_steps:
                        trajectory.append_tool_group(
                            workspace,
                            step,
                            [{
                                "id": f"gap-{step}",
                                "name": "bash_exec",
                                "arguments": {"command": f"probe-{step}"},
                                "failed": False,
                            }],
                            [f"result-{step}"],
                            {f"gap-{step}"},
                        )
                    with patch.object(
                        bot,
                        "run_completion_stage",
                        AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
                    ):
                        rolled, summary = await bot.rollover_agent_context(
                            workspace,
                            payload,
                            existing_summary="",
                            step_num=33,
                            ledger=ledger,
                            trajectory_gap_step=2,
                        )

                parsed = bot.parse_tiered_summary(summary)
                self.assertEqual(parsed["tier3_through"], 1)
                self.assertIn("Step 1-1", parsed["tier3"])
                self.assertNotIn("Step 3", parsed["tier3"])
                self.assertTrue(
                    bot._msg_content(rolled[0]).rstrip().endswith(
                        ledger.render().rstrip()
                    )
                )

    async def test_rollover_keeps_a_new_increment_before_advancing_its_watermark(self):
        # Production mutation caught: appending a new increment after a full
        # Tier 3 and prefix-clipping it away while still claiming coverage.
        marker = "NEW-INCREMENT-11-20"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace, through=50)
            existing = bot.format_tiered_summary(
                tier3="old-procedure-" + ("x" * bot._TIER3_MAX_CHARS),
                tier3_through=10,
                tier2_lines=[],
                discoveries=[],
            )
            completion = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=f"- {marker}: 인증 실패 뒤 대체 경로를 시도함."
                ))]
            ))
            with patch.object(bot, "run_completion_stage", completion):
                rolled, summary = await bot.rollover_agent_context(
                    workspace,
                    payload,
                    existing_summary=existing,
                    step_num=50,
                    ledger=ledger,
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 20)
        self.assertIn(marker, parsed["tier3"])
        self.assertLessEqual(len(parsed["tier3"]), bot._TIER3_MAX_CHARS)
        self.assertTrue(
            bot._msg_content(rolled[0]).rstrip().endswith(ledger.render().rstrip())
        )

    async def test_successful_compactor_receives_no_ledger_facts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            completion = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="- Step 1-10: 동일 엔드포인트 요청이 401에서 막혀 캐시 우회로 전환함."
                ))]
            ))
            with patch.object(bot, "run_completion_stage", completion):
                rolled, summary = await bot.rollover_agent_context(
                    workspace, payload, existing_summary="", step_num=40, ledger=ledger
                )

        prompt = completion.await_args.kwargs["messages"][1]["content"]
        self.assertNotIn("H_AUTH=active@v1", prompt)
        self.assertNotIn("보안 취약점 조사", prompt)
        parsed = bot.parse_tiered_summary(summary)
        self.assertIn("캐시 우회", parsed["tier3"])
        self.assertNotIn("H_AUTH=active@v1", parsed["tier3"])
        self.assertIn("H_AUTH=active@v1", bot._msg_content(rolled[0]))


if __name__ == "__main__":
    unittest.main()
