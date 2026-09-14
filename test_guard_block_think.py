"""Guard-block streak forced-think unit tests (issue #78)."""

import unittest

from test_support import FakeMessage  # sets required config env before bot imports

import bot
from bot import (
    GUARD_BLOCK_THINK_STEPS,
    resolve_adaptive_reasoning_effort,
)


class GuardBlockStreakTest(unittest.TestCase):
    def test_clean_step_resets_streak(self):
        self.assertEqual(
            bot._update_guard_block_streak(2, False, False), 0
        )

    def test_blocked_step_increments_streak(self):
        self.assertEqual(
            bot._update_guard_block_streak(0, True, False), 1
        )
        self.assertEqual(
            bot._update_guard_block_streak(2, True, False), 3
        )

    def test_dispatched_think_resets_streak(self):
        self.assertEqual(
            bot._update_guard_block_streak(2, True, True), 0
        )
        self.assertEqual(
            bot._update_guard_block_streak(2, False, True), 0
        )


class ForceGuardThinkTest(unittest.TestCase):
    def test_below_limit_does_not_force(self):
        self.assertFalse(
            bot._should_force_guard_think(GUARD_BLOCK_THINK_STEPS - 1, False)
        )

    def test_at_limit_forces_when_no_pending_think(self):
        self.assertTrue(
            bot._should_force_guard_think(GUARD_BLOCK_THINK_STEPS, False)
        )

    def test_pending_think_is_not_overwritten(self):
        self.assertFalse(
            bot._should_force_guard_think(GUARD_BLOCK_THINK_STEPS + 5, True)
        )

    def test_forced_pending_grants_low_reasoning_next_step(self):
        # 강제 think 예약이 다음 스텝에서 실제 추론 예산으로 이어지는지 확인.
        effort, tokens = resolve_adaptive_reasoning_effort(
            iteration=5,
            consecutive_internal_thoughts=0,
            configured_effort="high",
            adaptive_enabled=True,
            messages_payload=[
                {"role": "tool", "name": "bash_exec",
                 "content": "[stdout]\nok\n[exit code: 0]"}
            ],
            reasoning_max_tokens=1536,
            pending_think_effort="low",
        )
        self.assertEqual((effort, tokens), ("low", 512))

    def test_focus_text_mentions_replanning(self):
        self.assertIn("재수립", bot.GUARD_BLOCK_THINK_FOCUS)


if __name__ == "__main__":
    unittest.main()
