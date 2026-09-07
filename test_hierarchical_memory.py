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

    def test_obsolete_two_level_format_is_not_migrated(self):
        # Production mutation caught: a compatibility parser keeps two sources
        # of truth alive and lets old milestone prose revive rejected hypotheses.
        old = (
            "## 🏛️ 장기 마일스톤 색인\n- H_OLD is active\n\n"
            "## 🔍 직전 구간 상세 요약\nold detail"
        )

        parsed = bot.parse_tiered_summary(old)

        self.assertEqual(
            parsed,
            {"tier3": "", "tier3_through": 0, "tier2": [], "discoveries": []},
        )
        self.assertFalse(hasattr(bot, "parse_hierarchical_summary"))
        self.assertFalse(hasattr(bot, "update_hierarchical_summary"))

    def test_merge_procedural_note_preserves_tiers_without_claiming_authority(self):
        initial = bot.format_tiered_summary(
            tier3="- Step 1-10: API 인증 경로를 시도했으나 401로 중단됨.",
            tier3_through=10,
            tier2_lines=["[Step 11: bash_exec({}) -> ok]"],
            discoveries=["- 파일: `findings.md`"],
        )

        merged = bot.merge_tier3_procedure(
            initial,
            "이전 대화 구간: 사용자가 캐시 우회도 확인하라고 지시함.",
        )
        parsed = bot.parse_tiered_summary(merged)

        self.assertIn("API 인증 경로", parsed["tier3"])
        self.assertIn("캐시 우회", parsed["tier3"])
        self.assertEqual(parsed["tier3_through"], 10)
        self.assertEqual(parsed["tier2"], ["[Step 11: bash_exec({}) -> ok]"])
        self.assertNotIn("rejected@", parsed["tier3"])

    def test_summary_clipping_reserves_space_for_omission_marker(self):
        summary = bot._clip_summary_text(
            "x" * (bot.ROLLING_SUMMARY_MAX_CHARS + 1),
            bot.ROLLING_SUMMARY_MAX_CHARS,
        )

        self.assertLessEqual(len(summary), bot.ROLLING_SUMMARY_MAX_CHARS)
        self.assertTrue(summary.endswith(" ...[생략]"))

    def test_tier1_split_keeps_ten_complete_tool_groups_verbatim(self):
        messages = [{"role": "system", "content": "system"}]
        for step in range(1, 13):
            messages.extend([
                {
                    "role": "assistant",
                    "content": f"Step {step} reasoning",
                    "tool_calls": [{
                        "id": f"call-{step}",
                        "type": "function",
                        "function": {"name": "bash_exec", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{step}",
                    "name": "bash_exec",
                    "content": f"verbatim-result-{step}",
                },
            ])

        old, recent = bot.split_recent_agent_context(messages)

        self.assertEqual(sum(bot._msg_role(item) == "tool" for item in recent), 10)
        self.assertEqual(bot._msg_role(recent[0]), "assistant")
        self.assertEqual(bot._msg_content(recent[-1]), "verbatim-result-12")
        self.assertEqual(sum(bot._msg_role(item) == "tool" for item in old), 2)


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
