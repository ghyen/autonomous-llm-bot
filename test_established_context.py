"""Tests for established canonical context (findings.md, plan.md) injection."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_support import FakeMessage  # config init
import bot
from ledger import ResearchLedger


class EstablishedContextTest(unittest.TestCase):
    def test_empty_when_no_canonical_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=str(Path(tmp)), run_id="r1")
            content = bot.build_system_content(workspace)
            self.assertNotIn("[📌 기확정 사전 지식", content)

    def test_injects_plan_from_run_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            (workspace_path / "plan.md").write_text("# 목표: 닉네임 매핑\n- [ ] 1단계", encoding="utf-8")
            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")
            content = bot.build_system_content(workspace)
            self.assertIn("[📌 기확정 사전 지식 및 계획", content)
            self.assertIn("### 📋 확정 프로젝트 계획 (plan.md)", content)
            self.assertIn("# 목표: 닉네임 매핑", content)

    def test_injects_findings_and_plan_from_parent_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent_workspace = Path(tmp)
            runs_dir = parent_workspace / "runs"
            run_dir = runs_dir / "test_run"
            run_dir.mkdir(parents=True)

            (parent_workspace / "plan.md").write_text("# 공용 플랜", encoding="utf-8")
            (parent_workspace / "findings.md").write_text(
                "# 분석 보고서\n## 1. 아키텍처\n내부 ID 은닉 확인\n"
                "## [확정] 결론\n1. direct API 불가\n2. 패시브 OSINT 필요\n",
                encoding="utf-8",
            )

            workspace = SimpleNamespace(root=str(run_dir), run_id="r1")
            content = bot.build_system_content(workspace)
            self.assertIn("[📌 기확정 사전 지식 및 계획", content)
            self.assertIn("### 📋 확정 프로젝트 계획 (plan.md)", content)
            self.assertIn("# 공용 플랜", content)
            self.assertIn("### 🔍 확정 조사 결론 (findings.md)", content)
            self.assertIn("내부 ID 은닉 확인", content)
            self.assertIn("## [확정] 결론", content)

    def test_large_findings_extracts_conclusion_and_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            large_content = (
                "# 헤더 요약 정보\n"
                + ("- 데이터 라인\n" * 500)
                + "## [확정] 최종 핵심 결론\n중요한 결론 내용입니다.\n"
            )
            (workspace_path / "findings.md").write_text(large_content, encoding="utf-8")
            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")
            content = bot.build_system_content(workspace)
            self.assertIn("[📌 기확정 사전 지식 및 계획", content)
            self.assertIn("# 헤더 요약 정보", content)
            self.assertIn("...[중간 상세 데이터 생략]...", content)
            self.assertIn("## [확정] 최종 핵심 결론", content)
            self.assertIn("중요한 결론 내용입니다.", content)
            self.assertLessEqual(len(content), 12000)


if __name__ == "__main__":
    unittest.main()
