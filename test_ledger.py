import unittest

from ledger import ACTIVE, LedgerRefusal, REJECTED, ResearchLedger


def seeded_ledger():
    ledger = ResearchLedger()
    ledger.set_goal("A 경로 장애 원인 규명")
    ledger.add_evidence("E_POS", "A 경로에서 오류 로그 관측", source="log://1")
    ledger.declare_hypothesis("H_A", "A 경로가 장애 원인이다", evidence_id="E_POS")
    ledger.add_evidence("E_NEG", "A 경로 차단 후에도 장애 재현", source="log://2")
    return ledger


class HypothesisTransitionTest(unittest.TestCase):
    def setUp(self):
        self.ledger = seeded_ledger()

    def test_new_hypothesis_starts_active_at_v1(self):
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=active@v1")

    def test_rejection_advances_revision_and_records_evidence(self):
        self.ledger.reject_hypothesis("H_A", "E_NEG")
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")
        self.assertIn("E_NEG", self.ledger.cited_evidence("H_A"))

    def test_rejected_hypothesis_cannot_be_reactivated_by_declaration(self):
        self.ledger.reject_hypothesis("H_A", "E_NEG")
        with self.assertRaises(LedgerRefusal):
            self.ledger.declare_hypothesis("H_A", "A 경로가 장애 원인이다", status=ACTIVE)
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")

    def test_reopen_is_refused_when_evidence_was_already_cited(self):
        self.ledger.reject_hypothesis("H_A", "E_NEG")
        with self.assertRaises(LedgerRefusal):
            self.ledger.reopen_hypothesis("H_A", "E_NEG")
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")

    def test_reopen_with_new_evidence_reactivates_at_next_revision(self):
        self.ledger.reject_hypothesis("H_A", "E_NEG")
        self.ledger.add_evidence("E_NEW", "다른 부하 조건에서 A 경로 오류 재현", source="log://3")
        self.ledger.reopen_hypothesis("H_A", "E_NEW")
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=active@v3")

    def test_transition_requires_registered_evidence(self):
        with self.assertRaises(LedgerRefusal):
            self.ledger.reject_hypothesis("H_A", "E_UNKNOWN")
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=active@v1")

    def test_redeclaring_the_same_status_does_not_advance_revision(self):
        self.ledger.declare_hypothesis("H_A", "A 경로가 장애 원인이다 (표현 수정)")
        self.assertEqual(self.ledger.hypothesis_marker("H_A"), "H_A=active@v1")


class ConclusionInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = seeded_ledger()
        self.ledger.add_conclusion("C_A", "A 경로를 차단하면 장애가 멈춘다", ["H_A"])

    def test_conclusion_is_valid_while_premise_revision_holds(self):
        self.assertTrue(self.ledger.conclusion_is_valid("C_A"))
        self.assertEqual(self.ledger.conclusion_marker("C_A"), "C_A=유효")

    def test_conclusion_is_invalidated_when_premise_revision_moves(self):
        self.ledger.reject_hypothesis("H_A", "E_NEG")
        self.assertFalse(self.ledger.conclusion_is_valid("C_A"))
        self.assertEqual(self.ledger.conclusion_marker("C_A"), "C_A=무효")
        self.assertEqual(self.ledger.stale_premises("C_A"), ["H_A"])

    def test_conclusion_pinned_to_a_rejected_premise_is_never_valid(self):
        self.ledger.reject_hypothesis("H_A", "E_NEG")
        self.ledger.add_conclusion("C_B", "A 경로는 장애와 무관하지 않다", ["H_A"])
        self.assertFalse(self.ledger.conclusion_is_valid("C_B"))

    def test_conclusion_on_unknown_premise_is_invalid(self):
        self.ledger.add_conclusion("C_C", "근거 없는 결론", ["H_MISSING"])
        self.assertFalse(self.ledger.conclusion_is_valid("C_C"))


class RenderTest(unittest.TestCase):
    def test_render_carries_every_state_marker(self):
        ledger = seeded_ledger()
        ledger.add_conclusion("C_A", "A 경로를 차단하면 장애가 멈춘다", ["H_A"])
        ledger.reject_hypothesis("H_A", "E_NEG")

        rendered = ledger.render()
        for marker in ("H_A=rejected@v2", "C_A=무효", "E_NEG"):
            self.assertIn(marker, rendered)
        self.assertEqual(
            sorted(ledger.state_markers()),
            sorted(["H_A=rejected@v2", "C_A=무효", "E_NEG", "E_POS"]),
        )

    def test_empty_ledger_renders_nothing(self):
        self.assertEqual(ResearchLedger().render(), "")
        self.assertEqual(ResearchLedger().state_markers(), [])

    def test_render_clips_long_free_text_but_keeps_markers(self):
        ledger = ResearchLedger()
        ledger.add_evidence("E_LONG", "가" * 5000)
        ledger.declare_hypothesis("H_LONG", "나" * 5000, evidence_id="E_LONG")
        rendered = ledger.render()
        self.assertIn("H_LONG=active@v1", rendered)
        self.assertIn("E_LONG", rendered)
        self.assertLess(len(rendered), 2000)


class ApplyUpdatesTest(unittest.TestCase):
    def test_batch_applies_in_dependency_order(self):
        ledger = ResearchLedger()
        ledger.apply_updates({
            "goal": "A 경로 장애 원인 규명",
            "evidence": [{"id": "E_POS", "summary": "A 경로 오류 로그", "source": "log://1"}],
            "hypotheses": [
                {"id": "H_A", "statement": "A 경로가 원인이다", "evidence_id": "E_POS"},
            ],
            "conclusions": [{"id": "C_A", "statement": "A 경로 차단으로 해결된다", "premises": ["H_A"]}],
        })
        self.assertTrue(ledger.conclusion_is_valid("C_A"))

        report = ledger.apply_updates({
            "evidence": [{"id": "E_NEG", "summary": "A 경로 차단 후에도 재현", "source": "log://2"}],
            "hypotheses": [{"id": "H_A", "status": "rejected", "evidence_id": "E_NEG"}],
        })

        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")
        self.assertFalse(ledger.conclusion_is_valid("C_A"))
        self.assertIn("H_A=rejected@v2", report)

    def test_hypothesis_declared_rejected_at_birth_stays_at_v1(self):
        ledger = ResearchLedger()
        ledger.apply_updates({
            "evidence": [{"id": "E_NEG", "summary": "재현 실패", "source": "log://2"}],
            "hypotheses": [
                {"id": "H_Z", "statement": "Z가 원인이다", "status": "rejected", "evidence_id": "E_NEG"},
            ],
        })
        self.assertEqual(ledger.hypothesis_marker("H_Z"), "H_Z=rejected@v1")
        with self.assertRaises(LedgerRefusal):
            ledger.declare_hypothesis("H_Z", status=ACTIVE)

    def test_refused_transition_is_reported_without_changing_state(self):
        ledger = seeded_ledger()
        ledger.reject_hypothesis("H_A", "E_NEG")

        report = ledger.apply_updates({
            "hypotheses": [{"id": "H_A", "statement": "A 경로가 원인이다", "status": "active"}],
        })

        self.assertIn("거부", report)
        self.assertIn("reopen", report)
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=rejected@v2")

    def test_reopen_status_keyword_routes_through_the_reopen_rule(self):
        ledger = seeded_ledger()
        ledger.reject_hypothesis("H_A", "E_NEG")
        ledger.apply_updates({
            "evidence": [{"id": "E_NEW", "summary": "다른 조건에서 재현", "source": "log://3"}],
            "hypotheses": [{"id": "H_A", "status": "reopen", "evidence_id": "E_NEW"}],
        })
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=active@v3")

    def test_unknown_status_is_refused(self):
        ledger = seeded_ledger()
        report = ledger.apply_updates({
            "hypotheses": [{"id": "H_A", "status": "maybe"}],
        })
        self.assertIn("거부", report)
        self.assertEqual(ledger.hypothesis_marker("H_A"), "H_A=active@v1")

    def test_ledger_revision_tracks_accepted_mutations_only(self):
        ledger = seeded_ledger()
        before = ledger.revision
        ledger.apply_updates({"hypotheses": [{"id": "H_A", "status": "maybe"}]})
        self.assertEqual(ledger.revision, before)
        ledger.reject_hypothesis("H_A", "E_NEG")
        self.assertGreater(ledger.revision, before)


class StatusConstantTest(unittest.TestCase):
    def test_status_constants_match_marker_text(self):
        self.assertEqual((ACTIVE, REJECTED), ("active", "rejected"))


if __name__ == "__main__":
    unittest.main()
