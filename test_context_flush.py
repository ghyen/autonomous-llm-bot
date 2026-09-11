"""Tests for milestone sync and context flush / blackboard handover."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_support import FakeMessage
import bot
from ledger import ResearchLedger


class ContextFlushTest(unittest.TestCase):
    def test_is_context_or_oom_error(self):
        self.assertTrue(bot.is_context_or_oom_error(Exception("Prefill context too large for available memory: predicted peak would exceed prefill safety cap 22.5GB")))
        self.assertTrue(bot.is_context_or_oom_error(Exception("CUDA out of memory")))
        self.assertTrue(bot.is_context_or_oom_error(Exception("maximum context length is 16384 tokens, however you requested 18000")))
        self.assertTrue(bot.is_context_or_oom_error(Exception("exceeded memory limit for MPS/Metal")))
        self.assertFalse(bot.is_context_or_oom_error(Exception("Invalid tool call ID")))
        self.assertFalse(bot.is_context_or_oom_error(Exception("Connection refused")))

    def test_sync_milestone_to_disk_creates_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")

            report_text = (
                "### 분석 결과\n"
                "- Cycles API 엔드포인트 `/api/v1/cycles/2` 발견\n"
                "- 응답에 유저 닉네임과 레벨 포함됨\n"
                "```state_update\n"
                '{"phase": "analysis"}\n'
                "```"
            )

            bot.sync_milestone_to_disk(workspace, report_text, checkpoint_num=1)

            findings_path = workspace_path / "findings.md"
            self.assertTrue(findings_path.is_file())
            content = findings_path.read_text(encoding="utf-8")
            self.assertIn("## 📌 마일스톤 1 진행 보고 및 발견점", content)
            self.assertIn("Cycles API 엔드포인트", content)
            self.assertNotIn("state_update", content)

    def test_sync_milestone_to_disk_appends_subsequent_milestones(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")

            bot.sync_milestone_to_disk(workspace, "마일스톤 1 내용", checkpoint_num=1)
            bot.sync_milestone_to_disk(workspace, "마일스톤 2 내용", checkpoint_num=2)

            findings_path = workspace_path / "findings.md"
            content = findings_path.read_text(encoding="utf-8")
            self.assertIn("## 📌 마일스톤 1 진행 보고 및 발견점", content)
            self.assertIn("## 📌 마일스톤 2 진행 보고 및 발견점", content)
            self.assertIn("마일스톤 1 내용", content)
            self.assertIn("마일스톤 2 내용", content)

    def test_flush_agent_context_resets_messages_and_embeds_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            (workspace_path / "plan.md").write_text("# 목표 계획\n1. API 분석\n2. 계정 매핑", encoding="utf-8")
            (workspace_path / "findings.md").write_text("# 조사 결과\n- Cycles API 확인됨", encoding="utf-8")

            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")
            ledger = ResearchLedger()

            messages, summary = bot.flush_agent_context(
                workspace=workspace,
                ledger=ledger,
                checkpoint_num=1,
            )

            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[1]["role"], "user")

            system_content = messages[0]["content"]
            self.assertIn("[📌 기확정 사전 지식 및 계획", system_content)
            self.assertIn("Cycles API 확인됨", system_content)
            self.assertIn("목표 계획", system_content)
            self.assertIn("마일스톤 1 완료", summary)
            self.assertIn("findings.md", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
