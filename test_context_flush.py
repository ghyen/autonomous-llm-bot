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
            self.assertIn("## 📌 마일스톤 1 진행 보고 및 핵심 상태", content)
            self.assertIn("Cycles API 엔드포인트", content)
            self.assertNotIn("state_update", content)

    def test_sync_milestone_to_disk_overwrites_with_latest_milestone_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")

            bot.sync_milestone_to_disk(workspace, "마일스톤 1 내용", checkpoint_num=1)
            bot.sync_milestone_to_disk(workspace, "마일스톤 2 내용", checkpoint_num=2)

            findings_path = workspace_path / "findings.md"
            content = findings_path.read_text(encoding="utf-8")
            self.assertIn("## 📌 마일스톤 2 진행 보고 및 핵심 상태", content)
            self.assertIn("마일스톤 2 내용", content)
            # 이전 마일스톤 전문이 계속 누적되지 않고 최신 1장으로 덮어쓰기되어야 함
            self.assertNotIn("마일스톤 1 내용", content)

    def test_sync_milestone_to_disk_preserves_preexisting_preamble(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = Path(tmp)
            findings_path = workspace_path / "findings.md"
            findings_path.write_text("# 프로젝트 기본 조사\n- 초기 발견 사실", encoding="utf-8")

            workspace = SimpleNamespace(root=str(workspace_path), run_id="r1")
            bot.sync_milestone_to_disk(workspace, "마일스톤 1 내용", checkpoint_num=1)
            bot.sync_milestone_to_disk(workspace, "마일스톤 2 내용", checkpoint_num=2)

            content = findings_path.read_text(encoding="utf-8")
            self.assertIn("초기 발견 사실", content)
            self.assertIn("마일스톤 2 내용", content)
            self.assertNotIn("마일스톤 1 내용", content)

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

    def test_flush_preserves_the_tier3_watermark_and_procedure(self):
        # Production mutation caught: resetting the tiered watermark to 0 on
        # every checkpoint flush. Each flush made the next rollover re-derive
        # Tier 3 from step 1, so a 1,200-step run kept circling the first ~130
        # steps and re-read its own oldest procedures every 25 steps.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=str(Path(tmp)), run_id="r1")
            previous = bot.format_tiered_summary(
                tier3="- Step 900-910: profile 경로 전수 404를 확인함.",
                tier3_through=910,
                tier2_lines=[],
                discoveries=["- 참조/산출물: https://example.com/feed"],
            )

            _messages, summary = bot.flush_agent_context(
                workspace=workspace,
                ledger=ResearchLedger(),
                checkpoint_num=37,
                previous_summary=previous,
            )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 910)
        self.assertIn("profile 경로 전수 404", parsed["tier3"])
        self.assertIn("마일스톤 37 완료", parsed["tier3"])
        self.assertIn(
            "- 참조/산출물: https://example.com/feed", parsed["discoveries"]
        )

    def test_repeated_flush_keeps_only_the_latest_milestone_marker(self):        # The milestone note marks the phase boundary, so it replaces the
        # previous marker instead of accumulating one per checkpoint.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=str(Path(tmp)), run_id="r1")
            summary = ""
            for checkpoint in (1, 2, 3):
                _messages, summary = bot.flush_agent_context(
                    workspace=workspace,
                    ledger=ResearchLedger(),
                    checkpoint_num=checkpoint,
                    previous_summary=summary,
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertIn("마일스톤 3 완료", parsed["tier3"])
        self.assertNotIn("마일스톤 1 완료", parsed["tier3"])
        self.assertNotIn("마일스톤 2 완료", parsed["tier3"])
        self.assertLessEqual(len(parsed["tier3"]), bot._TIER3_MAX_CHARS)

    def test_flush_keeps_a_single_line_procedure_that_fills_the_budget(self):
        # Production mutation caught: appending the milestone before clipping.
        # A one-line Tier 3 exactly at the cap then loses the whole procedure to
        # the tail clip while the watermark still claims that range is covered,
        # so the dropped steps are never re-derived. The same ordering left the
        # marker mid-line, which made the next flush append a second one.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=str(Path(tmp)), run_id="r1")
            previous = bot.format_tiered_summary(
                tier3="P" * bot._TIER3_MAX_CHARS,
                tier3_through=1000,
                tier2_lines=[],
                discoveries=[],
            )
            _messages, summary = bot.flush_agent_context(
                workspace=workspace,
                ledger=ResearchLedger(),
                checkpoint_num=37,
                previous_summary=previous,
            )
            _messages, summary = bot.flush_agent_context(
                workspace=workspace,
                ledger=ResearchLedger(),
                checkpoint_num=38,
                previous_summary=summary,
            )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 1000)
        self.assertGreater(parsed["tier3"].count("P"), 1900)
        self.assertEqual(parsed["tier3"].count("마일스톤"), 1)
        self.assertIn("마일스톤 38 완료", parsed["tier3"])
        self.assertLessEqual(len(parsed["tier3"]), bot._TIER3_MAX_CHARS)

    def test_flush_tier3_budget_bounds_the_emergency_retention(self):
        # The OOM retry path rebuilds a payload without the prepare() token
        # preflight, so retaining a full Tier 3 there can reproduce the very
        # prefill overflow that triggered the flush. The watermark and the
        # newest lines still have to survive the smaller budget.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(root=str(Path(tmp)), run_id="r1")
            previous = bot.format_tiered_summary(
                tier3="\n".join(
                    f"- Step {step}: 절차 {step} " + ("x" * 40)
                    for step in range(800, 900)
                ),
                tier3_through=900,
                tier2_lines=[],
                discoveries=[],
            )
            _messages, summary = bot.flush_agent_context(
                workspace=workspace,
                ledger=ResearchLedger(),
                checkpoint_num=9,
                previous_summary=previous,
                tier3_chars=bot.OOM_RETRY_TIER3_CHARS,
            )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 900)
        self.assertLessEqual(len(parsed["tier3"]), bot.OOM_RETRY_TIER3_CHARS)
        self.assertIn("Step 899", parsed["tier3"])
        self.assertNotIn("Step 800", parsed["tier3"])


if __name__ == "__main__":
    unittest.main()
