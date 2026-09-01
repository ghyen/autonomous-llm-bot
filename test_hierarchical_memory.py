import os
import unittest
from unittest.mock import patch

from test_support import FakeMessage
import bot
from ledger import ResearchLedger


class HierarchicalMemoryTest(unittest.TestCase):
    def test_format_and_parse_hierarchical_summary(self):
        milestones = ["• 구간 1 (Step 1-10): 로그 분석 완료 및 취약점 후보 식별 (H_A 선언)"]
        recent = "로그에서 인증 우회 경로 발견. curl로 엔드포인트 직접 검증함."
        discoveries = ["- 파일: `findings.md`", "- 스킬: `skills/parse_logs.py`"]

        formatted = bot.format_hierarchical_summary(
            milestones=milestones,
            recent_summary=recent,
            discoveries=discoveries,
        )

        self.assertIn("## 🏛️ 장기 마일스톤 색인", formatted)
        self.assertIn("구간 1 (Step 1-10)", formatted)
        self.assertIn("## 🔍 직전 구간 상세 요약", formatted)
        self.assertIn("인증 우회 경로 발견", formatted)
        self.assertIn("## 📁 핵심 발견 및 산출물 색인", formatted)
        self.assertIn("skills/parse_logs.py", formatted)

        parsed = bot.parse_hierarchical_summary(formatted)
        self.assertEqual(len(parsed["milestones"]), 1)
        self.assertIn("구간 1 (Step 1-10)", parsed["milestones"][0])
        self.assertIn("인증 우회 경로 발견", parsed["recent_summary"])
        self.assertTrue(any("skills/parse_logs.py" in d for d in parsed["discoveries"]))

    def test_update_hierarchical_summary_condenses_previous_recent_into_milestones(self):
        # Step 10: initial summary created
        initial_recent = "1단계 조사: 포트 스캔 및 서비스 포트 8080 확인. H_PORT 선언."
        initial = bot.format_hierarchical_summary(
            milestones=[],
            recent_summary=initial_recent,
            discoveries=["- 파일: `findings.md`"],
        )

        # Step 20: next rollover updates hierarchical summary
        step20_recent = "2단계 조사: 8080 포트 인증 헤더 누락 확인. H_PORT 반증 및 E_AUTH 등록."
        updated = bot.update_hierarchical_summary(
            existing_summary=initial,
            new_recent_summary=step20_recent,
            step_range="Step 1-10",
            discoveries=["- 파일: `findings.md`", "- 스킬: `skills/check_auth.py`"],
        )

        parsed = bot.parse_hierarchical_summary(updated)
        # Old recent summary was archived into milestones
        self.assertTrue(len(parsed["milestones"]) >= 1)
        self.assertIn("Step 1-10", parsed["milestones"][0])
        self.assertIn("포트 스캔", parsed["milestones"][0])
        # New recent summary is now the active recent phase
        self.assertIn("2단계 조사", parsed["recent_summary"])
        self.assertIn("skills/check_auth.py", "\n".join(parsed["discoveries"]))


class RolloverHierarchicalIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def _make_payload(self):
        ledger = ResearchLedger()
        ledger.set_goal("보안 취약점 조사")
        ledger.add_evidence("E_AUTH", "인증 토큰 누락", "log://auth")
        ledger.declare_hypothesis("H_AUTH", "인증 모듈 결함", "active", "E_AUTH")

        payload = [{"role": "system", "content": bot.build_system_content(bot.SYSTEM_PROMPT, ledger)}]
        for step in range(12):
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

    async def test_rollover_produces_hierarchical_summary_in_system_prompt(self):
        ledger, payload = self._make_payload()
        rolled, summary = await bot.rollover_agent_context(
            payload, existing_summary="", step_num=10, ledger=ledger
        )

        self.assertIn("## 🏛️ 장기 마일스톤 색인", summary)
        self.assertIn("## 🔍 직전 구간 상세 요약", summary)
        self.assertEqual(bot._msg_role(rolled[0]), "system")
        self.assertIn("## 🏛️ 장기 마일스톤 색인", bot._msg_content(rolled[0]))
        self.assertIn("H_AUTH=active@v1", bot._msg_content(rolled[0]))


if __name__ == "__main__":
    unittest.main()
