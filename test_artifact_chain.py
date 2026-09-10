"""Artifact-chain guard unit and state tests."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_support import FakeMessage  # sets required config env before bot imports

import bot
import run_state
from ledger import ResearchLedger


class IsArtifactReadTest(unittest.TestCase):
    def test_bash_cat_of_artifact(self):
        self.assertTrue(bot._is_artifact_read(
            "bash_exec", {"command": "cat runs/abc/artifacts/out_1.log"}
        ))

    def test_bash_grep_of_artifact_is_still_artifact_read(self):
        self.assertTrue(bot._is_artifact_read(
            "bash_exec", {"command": "grep -n 'x' runs/abc/artifacts/out_1.log | head"}
        ))

    def test_plain_bash_is_not(self):
        self.assertFalse(bot._is_artifact_read(
            "bash_exec", {"command": "ls -la"}
        ))

    def test_read_file_of_artifact(self):
        self.assertTrue(bot._is_artifact_read(
            "read_file", {"path": "artifacts/out_1.log"}
        ))

    def test_read_file_elsewhere_is_not(self):
        self.assertFalse(bot._is_artifact_read(
            "read_file", {"path": "findings.md"}
        ))

    def test_other_tools_never_match(self):
        self.assertFalse(bot._is_artifact_read(
            "web_search", {"query": "artifacts/ foo"}
        ))
        self.assertFalse(bot._is_artifact_read("bash_exec", "not-a-dict"))


class ArtifactChainStateTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(run_state.normalize_artifact_chain(3), 3)
        self.assertEqual(run_state.normalize_artifact_chain(None), 0)
        self.assertEqual(run_state.normalize_artifact_chain(-1), 0)
        self.assertEqual(run_state.normalize_artifact_chain(True), 0)
        self.assertEqual(run_state.normalize_artifact_chain("3"), 0)

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(
                root=str(Path(tmp)), run_id="r1", owner_id=1, channel_id=2
            )
            run_state.save(
                workspace,
                message_id=7,
                next_step=3,
                summary="",
                tail=[],
                ledger=ResearchLedger(),
                interrupt={},
                announced_call_ids=[],
                tool_fingerprints=[],
                trajectory_gap_step=None,
                artifact_chain=2,
            )
            restored = run_state.load(workspace)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["artifact_chain"], 2)

    def test_missing_key_loads_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = SimpleNamespace(
                root=str(Path(tmp)), run_id="r1", owner_id=1, channel_id=2
            )
            run_state.save(
                workspace,
                message_id=7,
                next_step=3,
                summary="",
                tail=[],
                ledger=ResearchLedger(),
                interrupt={},
                announced_call_ids=[],
                tool_fingerprints=[],
                trajectory_gap_step=None,
            )
            payload = json.loads(
                (Path(tmp) / run_state.FILE_NAME).read_text(encoding="utf-8")
            )
            del payload["artifact_chain"]
            (Path(tmp) / run_state.FILE_NAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            restored = run_state.load(workspace)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["artifact_chain"], 0)


if __name__ == "__main__":
    unittest.main()
