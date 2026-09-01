import unittest

from outcome import COMPLETED, EXHAUSTED, FAILED, STOPPED, RunOutcome


class RunOutcomeTest(unittest.TestCase):
    def test_starts_unsettled(self):
        outcome = RunOutcome()
        self.assertIsNone(outcome.reason)
        self.assertIsNone(outcome.reason)
        self.assertFalse(outcome.is_completed)
        self.assertEqual(outcome.label, "진행 중")

    def test_first_transition_wins_and_later_ones_are_recorded_only(self):
        outcome = RunOutcome()
        self.assertTrue(outcome.settle(STOPPED, "사용자 중단 요청"))
        self.assertFalse(outcome.settle(COMPLETED, "finish_task 호출"))
        self.assertFalse(outcome.settle(EXHAUSTED))

        self.assertEqual(outcome.reason, STOPPED)
        self.assertEqual(outcome.detail, "사용자 중단 요청")
        self.assertEqual(outcome.ignored_attempts, [(COMPLETED, "finish_task 호출"), (EXHAUSTED, "")])

    def test_unknown_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            RunOutcome().settle("probably-fine")

    def test_only_completed_reads_as_success(self):
        for reason in (STOPPED, EXHAUSTED, FAILED):
            outcome = RunOutcome()
            outcome.settle(reason)
            self.assertFalse(outcome.is_completed, reason)

        completed = RunOutcome()
        completed.settle(COMPLETED)
        self.assertTrue(completed.is_completed)

    def test_incomplete_labels_say_so(self):
        for reason in (STOPPED, EXHAUSTED, FAILED):
            outcome = RunOutcome()
            outcome.settle(reason)
            self.assertIn("미완료", outcome.label)
            self.assertNotIn("완료 ", outcome.label.replace("미완료", ""))

    def test_completed_label_does_not_claim_incompleteness(self):
        outcome = RunOutcome()
        outcome.settle(COMPLETED)
        self.assertNotIn("미완료", outcome.label)

    def test_describe_includes_the_detail_for_logs(self):
        outcome = RunOutcome()
        outcome.settle(EXHAUSTED, "스텝 예산 소진")
        self.assertEqual(outcome.describe(), "exhausted (스텝 예산 소진)")


if __name__ == "__main__":
    unittest.main()
