"""Resume-compact summary tests."""

import unittest

from test_support import FakeMessage  # sets required config env before bot imports

import bot


def _tiered():
    return bot.format_tiered_summary(
        tier3="루프 기록 " * 500,
        tier3_through=160,
        tier2_lines=[f"Step {i}: 핵심" for i in range(1, 21)],
        discoveries=[f"- 발견 {i}" for i in range(1, 11)],
    )


class ParseResumeArgsTest(unittest.TestCase):
    def test_default_is_compact(self):
        run_id, full = bot._parse_resume_args(["!resume", "abc123"])
        self.assertEqual(run_id, "abc123")
        self.assertFalse(full)

    def test_full_flag(self):
        run_id, full = bot._parse_resume_args(["!resume", "abc123", "--full"])
        self.assertEqual(run_id, "abc123")
        self.assertTrue(full)

    def test_missing_run_id(self):
        run_id, full = bot._parse_resume_args(["!resume"])
        self.assertEqual(run_id, "")
        self.assertFalse(full)

    def test_flag_in_run_id_position_rejected(self):
        run_id, _ = bot._parse_resume_args(["!resume", "--full"])
        self.assertEqual(run_id, "--full")


class CompactSummaryForResumeTest(unittest.TestCase):
    def test_drops_tier3_keeps_recent_tier2(self):
        compacted = bot._compact_summary_for_resume(_tiered())
        self.assertNotIn("루프 기록", compacted)
        self.assertIn("Step 20", compacted)
        self.assertNotIn("Step 1:", compacted)
        self.assertIn("발견 10", compacted)

    def test_empty_stays_empty(self):
        self.assertEqual(bot._compact_summary_for_resume(""), "")
        self.assertEqual(bot._compact_summary_for_resume("   "), "")

    def test_legacy_summary_clipped_not_dropped(self):
        legacy = "절차 기록 " * 1000
        compacted = bot._compact_summary_for_resume(legacy)
        self.assertTrue(compacted)
        self.assertLessEqual(len(compacted), bot.RESUME_COMPACT_LEGACY_CHARS + 1)

    def test_idempotent(self):
        once = bot._compact_summary_for_resume(_tiered())
        self.assertEqual(bot._compact_summary_for_resume(once), once)


if __name__ == "__main__":
    unittest.main()
