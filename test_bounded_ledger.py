import json
import unittest
from ledger import ResearchLedger


class BoundedLedgerTest(unittest.TestCase):
    def test_large_prompt_view_preserves_full_durable_state(self):
        ledger = ResearchLedger()
        ledger.set_goal("preserve investigation")
        for i in range(2000):
            ledger.add_evidence(f"E{i}", "x" * 220, "s" * 160)
        ledger.declare_hypothesis("H_current", "investigate", evidence_id="E1999")
        before = json.dumps(ledger.to_dict(), sort_keys=True)
        rendered = ledger.render(max_chars=12000)
        self.assertLessEqual(len(rendered), 12000)
        self.assertIn("H_current=active@v1", rendered)
        self.assertIn("E1999", rendered)
        self.assertIn("Partial state", rendered)
        self.assertIn("state.json", rendered)
        self.assertEqual(json.dumps(ledger.to_dict(), sort_keys=True), before)
        self.assertGreater(len(ledger.render()), 700000)
        report = ledger.apply_updates({"goal": "continued investigation"})
        self.assertLess(len(report), 13000)
