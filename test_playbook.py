"""Issue #51 playbook inheritance, prompt injection, and rule recording."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_support import TEST_USER_ID  # sets required config env before bot imports
import bot
import workspace_io
from ledger import ResearchLedger

CHANNEL_A = 987654810


def _write_playbook(root, text):
    with open(os.path.join(root, "playbook.md"), "w", encoding="utf-8") as handle:
        handle.write(text)


# build_system_content joins its parts with a blank line, so this is the injected
# section itself, not the label as it is also mentioned inside the prompt text.
def _section_marker():
    return f"\n\n[{bot.PLAYBOOK_LABEL}]\n"


class PlaybookPromptTest(unittest.TestCase):
    def test_build_system_content_injects_playbook_as_negative_constraints(self):
        # Production mutation caught: dropping the injection leaves each new run
        # blind to environment rules it already paid to learn.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            _write_playbook(
                temp_dir,
                "## 환경 및 도구 제약 (Environment Rules)\n"
                "- Mac 기본 grep은 BSD grep이므로 -P를 지원하지 않는다\n",
            )

            content = bot.build_system_content(workspace)

            self.assertIn(_section_marker(), content)
            self.assertIn("Mac 기본 grep은 BSD grep이므로 -P를 지원하지 않는다", content)

    def test_playbook_block_precedes_summary_and_state_block(self):
        # Production mutation caught: appending the playbook after the summary or
        # after the state block lets summary growth push constraints out of view,
        # or displaces the authoritative state block from last position.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            _write_playbook(temp_dir, "- n.kakao.com/talk/{id}는 401 게이트웨이다\n")
            ledger = ResearchLedger()
            ledger.set_goal("계정 운영자 신원 확인")

            content = bot.build_system_content(workspace, ledger, "누적 요약 본문")

            playbook_at = content.index(_section_marker())
            summary_at = content.index(f"[{bot.ROLLING_SUMMARY_LABEL}]")
            state_at = content.index(ledger.render())
            self.assertLess(playbook_at, summary_at)
            self.assertLess(summary_at, state_at)
            self.assertTrue(content.rstrip().endswith(ledger.render().rstrip()))

    def test_missing_or_unreachable_playbook_adds_no_section_and_never_raises(self):
        # Production mutation caught: a strict read makes build_system_content
        # explode for the many callers whose workspace root does not exist.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            self.assertEqual(bot.render_playbook_block(workspace), "")
            self.assertNotIn(_section_marker(), bot.build_system_content(workspace))

        unreachable = SimpleNamespace(root="/tmp/run-does-not-exist-0123456789")
        self.assertEqual(bot.render_playbook_block(unreachable), "")
        self.assertNotIn(_section_marker(), bot.build_system_content(unreachable))

    def test_oversized_playbook_is_clipped_before_injection(self):
        # Production mutation caught: an unbounded playbook is re-pinned into
        # message 0 every step, so it silently taxes every request.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            _write_playbook(temp_dir, "- 규칙\n" * 5000)

            block = bot.render_playbook_block(workspace)

            self.assertLessEqual(len(block), bot.PLAYBOOK_MAX_CHARS + 200)
            self.assertIn("생략", block)


class RecordPlaybookTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.catalog = bot.RunCatalog(
            os.path.join(self.temp_dir.name, "workspace"),
            os.path.join(self.temp_dir.name, "logs"),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def workspace(self):
        return self.catalog.acquire(TEST_USER_ID, CHANNEL_A)

    def playbook_text(self, workspace):
        return (workspace.root / "playbook.md").read_text(encoding="utf-8")

    async def test_recorded_rule_lands_under_its_section_header(self):
        # Production mutation caught: writing rules without a section makes the
        # playbook an unsorted pile the model cannot skim.
        workspace = self.workspace()

        result = json.loads(
            await bot.tool_record_playbook(
                workspace, "environment", "Mac grep은 -P를 지원하지 않는다"
            )
        )

        self.assertEqual(result["status"], "success")
        text = self.playbook_text(workspace)
        self.assertIn(bot.PLAYBOOK_SECTIONS["environment"], text)
        self.assertIn("- Mac grep은 -P를 지원하지 않는다", text)

    async def test_second_rule_accumulates_instead_of_replacing_the_first(self):
        # Production mutation caught: reading through the run's read-hash cache
        # returns "unchanged" with no content on the second call, so the append
        # overwrites every previously learned rule with just the newest one.
        workspace = self.workspace()

        await bot.tool_record_playbook(workspace, "environment", "첫 번째 규칙")
        await bot.tool_record_playbook(workspace, "dead_end", "두 번째 규칙")

        text = self.playbook_text(workspace)
        self.assertIn("첫 번째 규칙", text)
        self.assertIn("두 번째 규칙", text)
        self.assertIn(bot.PLAYBOOK_SECTIONS["environment"], text)
        self.assertIn(bot.PLAYBOOK_SECTIONS["dead_end"], text)

    async def test_duplicate_rule_is_not_appended_twice(self):
        # Production mutation caught: re-recording the same lesson every run
        # grows the block that is re-pinned into message 0 on every step.
        workspace = self.workspace()

        await bot.tool_record_playbook(workspace, "environment", "동일 규칙")
        second = json.loads(
            await bot.tool_record_playbook(workspace, "environment", "동일 규칙")
        )

        self.assertEqual(second["status"], "success")
        self.assertEqual(self.playbook_text(workspace).count("동일 규칙"), 1)

    async def test_unknown_rule_type_is_refused_and_counts_as_a_failed_tool(self):
        # Production mutation caught: silently accepting any rule_type scatters
        # rules into invented sections, and a result that does not read as a
        # failure lets the model retry the same refusal forever.
        workspace = self.workspace()

        raw = await bot.tool_record_playbook(workspace, "미지의종류", "무언가")

        result = json.loads(raw)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "unknown_rule_type")
        self.assertTrue(bot._tool_result_failed("record_playbook", raw))
        self.assertFalse((workspace.root / "playbook.md").exists())

    async def test_empty_rule_is_refused(self):
        # Production mutation caught: blank rules pad the injected block with
        # bullet points that carry no constraint.
        workspace = self.workspace()

        result = json.loads(
            await bot.tool_record_playbook(workspace, "environment", "   ")
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "empty_rule")

    async def test_conflicting_write_is_retried_against_the_current_revision(self):
        # Production mutation caught: a single-shot compare-and-swap loses the
        # rule whenever another canonical write lands between read and write.
        workspace = self.workspace()
        await bot.tool_record_playbook(workspace, "environment", "기존 규칙")
        real_write = workspace.write
        calls = []

        async def conflict_once(path, content, expected_revision):
            calls.append(expected_revision)
            if len(calls) == 1:
                return {"status": "conflict", "path": path, "current_revision": "x"}
            return await real_write(path, content, expected_revision)

        with patch.object(workspace, "write", conflict_once):
            result = json.loads(
                await bot.tool_record_playbook(workspace, "strategy", "새 규칙")
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(calls), 2)
        text = self.playbook_text(workspace)
        self.assertIn("기존 규칙", text)
        self.assertIn("새 규칙", text)

    async def test_a_full_playbook_refuses_new_rules_instead_of_growing(self):
        # Production mutation caught: an unbounded playbook is injected into
        # message 0 on every step, so it must refuse growth, not clip silently.
        workspace = self.workspace()
        await workspace.write("playbook.md", "- 규칙\n" * 5000, "absent")

        result = json.loads(
            await bot.tool_record_playbook(workspace, "environment", "한 줄 더")
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "playbook_full")

    async def test_playbook_is_a_canonical_file(self):
        # Production mutation caught: leaving playbook.md out of CANONICAL_NAMES
        # drops cross-run inheritance, the write lock, and compare-and-swap.
        self.assertIn("playbook.md", workspace_io.CANONICAL_NAMES)

    async def test_dispatcher_routes_record_playbook_to_its_handler(self):
        # Production mutation caught: a missing dispatch branch falls through to
        # "[Error: Unknown tool function]", which costs a step and looks like a
        # model mistake rather than missing wiring.
        workspace = self.workspace()

        results = await bot.execute_tools_in_parallel(
            workspace,
            [{
                "id": "call_pb_1",
                "name": "record_playbook",
                "arguments": {"rule_type": "dead_end", "rule_content": "401 게이트웨이"},
            }],
        )

        self.assertEqual(json.loads(results[0])["status"], "success")
        self.assertIn("401 게이트웨이", self.playbook_text(workspace))

    async def test_record_playbook_is_offered_to_the_model(self):
        # Production mutation caught: a handler the schema never advertises can
        # only be reached by accident.
        names = [
            entry["function"]["name"]
            for entry in bot.agent_tool_params()["tools"]
        ]
        self.assertIn("record_playbook", names)


if __name__ == "__main__":
    unittest.main()
