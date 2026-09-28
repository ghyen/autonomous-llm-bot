"""Record-time grounding warning tests.

A new evidence claim is checked against recent trajectory observations of the
same target. Only machine-readable contradictions warn, and a warning never
blocks: the record still succeeds. Production case: steps 23-24 measured
404 + 0 phone patterns, step 28 recorded "010-3183-3933 발견".
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage  # sets required config env before bot imports

import bot
import grounding
import trajectory
from ledger import ResearchLedger


def _workspace(root):
    run = Path(root) / "run"
    run.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root=str(run))


def _append(workspace, step, command, result, failed=False):
    call_id = f"c{step}-{abs(hash(command)) % 10_000_000}"
    trajectory.append_tool_group(
        workspace,
        step,
        [{
            "id": call_id,
            "name": "bash_exec",
            "arguments": {"command": command},
            "failed": failed,
        }],
        [result],
        {call_id},
    )


def _dead_404(url):
    return (
        "[stdout]\n<!doctype html><title>Page Not Found (404)</title> "
        "5980 bytes\n[exit code: 0]"
    )


def _records(workspace):
    records, _complete = trajectory.read_records(workspace)
    return records


class GroundingCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = _workspace(self._tmp.name)
        self.url = "https://brownfeed.com/en/profile/55142"
        _append(self.workspace, 23, f"curl {self.url}", _dead_404(self.url))
        _append(
            self.workspace, 24, f"curl {self.url}",
            "[stdout]\n010 patterns: 0\n[exit code: 0]",
        )

    def test_warns_on_phone_claim_against_dead_measurements(self):
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{
                "id": "E_PHONE_55142",
                "source": f"curl {self.url}",
                "summary": "user 55142 profile page에서 010-3183-3933 발견",
            }],
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("E_PHONE_55142", warnings[0])
        self.assertIn("23,24", warnings[0])
        self.assertIn("retracts", warnings[0])

    def test_no_warning_when_support_is_recent(self):
        _append(
            self.workspace, 25, f"curl {self.url}",
            "[stdout]\ncall 010-3183-3933 answered 200\n[exit code: 0]",
        )
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{
                "id": "E_PHONE_55142",
                "source": f"curl {self.url}",
                "summary": "user 55142 profile page에서 010-3183-3933 발견",
            }],
        )
        self.assertEqual(warnings, [])

    def test_no_warning_for_negated_claim(self):
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{
                "id": "E_NO_PHONE",
                "source": f"curl {self.url}",
                "summary": "profile page에서 010 패턴 0건",
            }],
        )
        self.assertEqual(warnings, [])

    def test_no_warning_without_targets(self):
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{"id": "E_X", "summary": "010-3183-3933 발견"}],
        )
        self.assertEqual(warnings, [])

    def test_no_warning_with_single_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            _append(workspace, 9, f"curl {self.url}", _dead_404(self.url))
            records, _ = trajectory.read_records(workspace)
            warnings = grounding.check_evidence_grounding(
                records,
                [{
                    "id": "E_PHONE_55142",
                    "source": f"curl {self.url}",
                    "summary": "user 55142 profile page에서 010-3183-3933 발견",
                }],
            )
            self.assertEqual(warnings, [])

    def test_warnings_capped_at_three(self):
        items = [
            {
                "id": f"E_{index}",
                "source": f"curl {self.url}",
                "summary": f"010-3183-393{index} 발견",
            }
            for index in range(6)
        ]
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace), items
        )
        self.assertLessEqual(len(warnings), 3)

    def test_never_raises_on_garbage(self):
        self.assertEqual(grounding.check_evidence_grounding(None, None), [])
        self.assertEqual(grounding.check_evidence_grounding([], [{"id": ""}]), [])
        self.assertEqual(
            grounding.check_evidence_grounding("junk", [{"id": "E_X"}]), []
        )

    def test_negation_forms_suppress_the_claim(self):
        for summary in (
            "profile page에 010-3183-3933은 없는 값",
            "010 패턴 미검출",
            "전화번호 발견되지 않음",
            "010-3183-3933 없어",
        ):
            with self.subTest(summary=summary):
                warnings = grounding.check_evidence_grounding(
                    _records(self.workspace),
                    [{
                        "id": "E_X",
                        "source": f"curl {self.url}",
                        "summary": summary,
                    }],
                )
                self.assertEqual(warnings, [])

    def test_phone_formats_are_recognized(self):
        for phone in ("010 3183 3933", "+82-10-3183-3933", "(010) 3183-3933"):
            with self.subTest(phone=phone):
                warnings = grounding.check_evidence_grounding(
                    _records(self.workspace),
                    [{
                        "id": "E_X",
                        "source": f"curl {self.url}",
                        "summary": f"profile page에서 {phone} 발견",
                    }],
                )
                self.assertEqual(len(warnings), 1)

    def test_multi_target_evidence_is_skipped(self):
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{
                "id": "E_X",
                "source": f"curl {self.url} curl https://x.test/other",
                "summary": "user 55142 profile page에서 010-3183-3933 발견",
            }],
        )
        self.assertEqual(warnings, [])

    def test_window_anchors_to_current_step(self):
        # current_step=26이면 23~24가 창 밖에 있어 경고가 나지 않는다.
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{
                "id": "E_X",
                "source": f"curl {self.url}",
                "summary": "user 55142 profile page에서 010-3183-3933 발견",
            }],
            current_step=35,
        )
        self.assertEqual(warnings, [])
        # 같은 입력도 current_step=24면 창 안에 들어 경고한다.
        warnings = grounding.check_evidence_grounding(
            _records(self.workspace),
            [{
                "id": "E_X",
                "source": f"curl {self.url}",
                "summary": "user 55142 profile page에서 010-3183-3933 발견",
            }],
            current_step=24,
        )
        self.assertEqual(len(warnings), 1)


class RecordStateWiringTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = _workspace(self._tmp.name)
        self.url = "https://brownfeed.com/en/profile/55142"
        _append(self.workspace, 23, f"curl {self.url}", _dead_404(self.url))
        _append(
            self.workspace, 24, f"curl {self.url}",
            "[stdout]\n010 patterns: 0\n[exit code: 0]",
        )

    async def test_warning_appended_without_blocking(self):
        ledger = ResearchLedger()
        events = []

        def record(_workspace, kind, **kwargs):
            events.append((kind, kwargs))

        with patch.object(bot, "log_session_event", record):
            result = await bot.tool_record_state(
                ledger,
                {"evidence": [{
                    "id": "E_PHONE_55142",
                    "source": f"curl {self.url}",
                    "summary": "user 55142 profile page에서 010-3183-3933 발견",
                }]},
                self.workspace,
            )
        self.assertTrue(result.startswith("[record_state status: success]"))
        self.assertIn("[grounding 주의]", result)
        self.assertIn("E_PHONE_55142", result)
        kinds = [kind for kind, _ in events]
        self.assertIn("record_grounding_warning", kinds)
        # 기록 자체는 성공한다: 경고는 막지 않는다.
        self.assertIn("E_PHONE_55142", ledger._evidence)

    async def test_no_workspace_means_no_check(self):
        ledger = ResearchLedger()
        result = await bot.tool_record_state(
            ledger,
            {"evidence": [{
                "id": "E_PHONE_55142",
                "source": f"curl {self.url}",
                "summary": "user 55142 profile page에서 010-3183-3933 발견",
            }]},
        )
        self.assertTrue(result.startswith("[record_state status: success]"))
        self.assertNotIn("[grounding 주의]", result)


if __name__ == "__main__":
    unittest.main()
