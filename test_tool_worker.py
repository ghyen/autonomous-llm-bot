import json
import os
import tempfile
import unittest
from pathlib import Path

from tool_worker import handle_request


class WorkerProtocolTest(unittest.TestCase):
    def test_read_and_write_requests_return_existing_revision_shapes(self):
        with tempfile.TemporaryDirectory() as root:
            result = handle_request(
                {
                    "operation": "write_file",
                    "workspace": root,
                    "path": "note.txt",
                    "content": "inside",
                    "expected_revision": None,
                }
            )
            self.assertEqual(result["status"], "success")

            read = handle_request(
                {"operation": "read_file", "workspace": root, "path": "note.txt"}
            )
            self.assertEqual(read["content"], "inside")
            self.assertTrue(read["revision"].startswith("sha256:"))

    def test_outside_paths_are_refused_before_access(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            result = handle_request(
                {"operation": "read_file", "workspace": root, "path": outside}
            )
            self.assertEqual(result["status"], "error")
            self.assertNotIn("content", result)

    def test_canonical_write_keeps_compare_and_swap_contract(self):
        with tempfile.TemporaryDirectory() as root:
            missing = handle_request(
                {
                    "operation": "write_file",
                    "workspace": root,
                    "path": "plan.md",
                    "content": "first",
                    "expected_revision": None,
                }
            )
            self.assertEqual(missing["error"], "expected_revision_required")

            created = handle_request(
                {
                    "operation": "write_file",
                    "workspace": root,
                    "path": "plan.md",
                    "content": "first",
                    "expected_revision": "absent",
                }
            )
            conflict = handle_request(
                {
                    "operation": "write_file",
                    "workspace": root,
                    "path": "PLAN.MD",
                    "content": "stale",
                    "expected_revision": "absent",
                }
            )
            self.assertEqual(created["status"], "success")
            self.assertEqual(conflict["status"], "conflict")
            self.assertEqual(Path(root, "plan.md").read_text(), "first")

    def test_invalid_requests_are_structured_errors_without_command_echo(self):
        command = "printf PRIVATE-KEY-CANARY"
        result = handle_request(
            {"operation": "bash_exec", "workspace": tempfile.gettempdir(), "command": command}
        )
        self.assertEqual(result["status"], "error")
        self.assertNotIn(command, json.dumps(result))

