"""Issue #49 append-only execution trajectory behavior."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from test_support import TEST_USER_ID  # seeds required config before repo imports
from run_workspace import RunCatalog
import bot
import trajectory

CHANNEL_ID = 987654820


def _call(call_id, name, arguments, failed=False):
    return {
        "id": call_id,
        "name": name,
        "arguments": arguments,
        "failed": failed,
    }


class TrajectoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.workspace = type("Workspace", (), {"root": root / "run"})()
        self.workspace.root.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    @property
    def path(self):
        return self.workspace.root / trajectory.FILE_NAME

    def records(self):
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_append_preserves_bytes_and_builds_a_parent_chain(self):
        # Mutation caught: atomic rewrite or truncating open modes let a later
        # checkpoint replace the trajectory that earlier compaction depends on.
        first = trajectory.append_tool_group(
            self.workspace,
            1,
            [
                _call("c1", "bash_exec", {"command": "curl https://example.com/a"}),
                _call("c2", "read_file", {"path": "missing.txt"}, failed=True),
            ],
            [
                "[stdout]\n200 OK\n[exit code: 0]",
                '{"status":"error","error":"not_found"}',
            ],
            {"c1"},
        )
        before = self.path.read_bytes()

        second = trajectory.append_tool_group(
            self.workspace,
            2,
            [_call("c3", "web_search", {"query": "agent memory"})],
            ["1. Tiered memory\n   https://example.com/memory"],
            {"c3"},
        )

        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        records = self.records()
        self.assertEqual(len(records), 3)
        self.assertEqual(first, records[:2])
        self.assertEqual(second, records[2:])
        self.assertIsNone(records[0]["parent"])
        self.assertEqual(records[1]["parent"], records[0]["id"])
        self.assertEqual(records[2]["parent"], records[1]["id"])
        self.assertEqual([record["step"] for record in records], [1, 1, 2])
        self.assertEqual([record["call_id"] for record in records], ["c1", "c2", "c3"])
        self.assertEqual([record["executed"] for record in records], [True, False, True])
        self.assertEqual([record["failed"] for record in records], [False, True, False])
        for record in records:
            self.assertEqual(record["schema"], trajectory.SCHEMA)
            self.assertRegex(record["id"], r"^[0-9a-f]{64}$")

    def test_read_records_stops_at_the_first_hash_or_parent_break(self):
        # Production mutations caught: trusting an unchanged id after body
        # tampering, or accepting a freshly hashed record whose parent skips the
        # accepted prefix. Either mutation lets forged history feed every tier.
        for step in range(1, 4):
            trajectory.append_tool_group(
                self.workspace,
                step,
                [_call(f"c{step}", "bash_exec", {"command": f"probe-{step}"})],
                [f"result-{step}"],
                {f"c{step}"},
            )
        original = self.records()

        for mutation in ("body", "parent"):
            with self.subTest(mutation=mutation):
                records = json.loads(json.dumps(original))
                if mutation == "body":
                    records[1]["result"] = "forged-result"
                else:
                    records[1]["parent"] = "0" * 64
                    body = {key: value for key, value in records[1].items() if key != "id"}
                    encoded = json.dumps(
                        body,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    records[1]["id"] = __import__("hashlib").sha256(encoded).hexdigest()
                self.path.write_text(
                    "".join(
                        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                        for record in records
                    ),
                    encoding="utf-8",
                )

                trusted = trajectory.read_records(self.workspace)

                self.assertEqual([record["call_id"] for record in trusted], ["c1"])
                self.assertEqual(trajectory.lookup(self.workspace, 3)["status"], "not_found")

    def test_append_refuses_an_integrity_broken_history(self):
        # Production mutation caught: deriving the next parent from an invalid
        # suffix and appending records that no validating reader can ever reach.
        trajectory.append_tool_group(
            self.workspace,
            1,
            [_call("original", "bash_exec", {"command": "true"})],
            ["[exit code: 0]"],
            {"original"},
        )
        [record] = self.records()
        record["result"] = "forged"
        self.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        before = self.path.read_bytes()

        with self.assertRaises(ValueError):
            trajectory.append_tool_group(
                self.workspace,
                2,
                [_call("later", "bash_exec", {"command": "false"})],
                ["[exit code: 1]"],
                {"later"},
            )

        self.assertEqual(self.path.read_bytes(), before)

    def test_lookup_by_step_and_call_id_is_neutral_when_missing(self):
        # Mutation caught: a lookup that returns an error for a missing step
        # feeds the consecutive-failure brake and can stall the research loop.
        trajectory.append_tool_group(
            self.workspace,
            7,
            [
                _call("lookup-a", "bash_exec", {"command": "printf a"}),
                _call("lookup-b", "bash_exec", {"command": "printf b"}),
            ],
            ["a\n[exit code: 0]", "b\n[exit code: 0]"],
            {"lookup-a", "lookup-b"},
        )

        whole_step = trajectory.lookup(self.workspace, 7)
        one_call = trajectory.lookup(self.workspace, 7, "lookup-b")
        missing = trajectory.lookup(self.workspace, 8)

        self.assertEqual(whole_step["status"], "success")
        self.assertEqual([item["call_id"] for item in whole_step["records"]], ["lookup-a", "lookup-b"])
        self.assertEqual(one_call["status"], "success")
        self.assertEqual([item["call_id"] for item in one_call["records"]], ["lookup-b"])
        self.assertEqual(missing, {"status": "not_found", "step": 8, "call_id": ""})

    def test_malformed_lines_are_ignored_without_hiding_valid_records(self):
        # Mutation caught: one partially written final line makes every prior
        # trajectory record unreadable after a process interruption.
        trajectory.append_tool_group(
            self.workspace,
            3,
            [_call("valid", "bash_exec", {"command": "true"})],
            ["[exit code: 0]"],
            {"valid"},
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{truncated\n")

        result = trajectory.lookup(self.workspace, 3)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["records"][0]["call_id"], "valid")

    def test_large_arguments_and_results_are_bounded_with_original_sizes(self):
        # Mutation caught: duplicating write_file content or a large HTTP body in
        # traj.jsonl trades context exhaustion for workspace exhaustion.
        content = "a" * (trajectory.ARGUMENT_STRING_MAX_CHARS + 500)
        result = "HEAD\n" + ("x" * (trajectory.RESULT_MAX_CHARS + 500)) + "\nTAIL"

        [record] = trajectory.append_tool_group(
            self.workspace,
            4,
            [_call("large", "write_file", {"path": "blob.txt", "content": content})],
            [result],
            {"large"},
        )

        self.assertEqual(record["arguments_chars"], len(json.dumps(
            {"path": "blob.txt", "content": content},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )))
        self.assertEqual(record["result_chars"], len(result))
        self.assertLess(len(record["arguments"]["content"]), len(content))
        self.assertIn("생략", record["arguments"]["content"])
        self.assertLessEqual(len(record["result"]), trajectory.RESULT_MAX_CHARS)
        self.assertIn("HEAD", record["result"])
        self.assertIn("TAIL", record["result"])
        self.assertIn("생략", record["result"])

    def test_micro_index_is_one_bounded_line_per_step(self):
        # Mutation caught: emitting one line per call lets a parallel batch make
        # Tier 2 grow without regard to its 20-step resolution budget.
        trajectory.append_tool_group(
            self.workspace,
            11,
            [
                _call("m1", "bash_exec", {"command": "curl https://example.com/" + "a" * 400}),
                _call("m2", "read_file", {"path": "findings.md"}),
            ],
            ["[stdout]\n200 OK\n[exit code: 0]", '{"status":"success","content":"fact"}'],
            {"m1", "m2"},
        )
        trajectory.append_tool_group(
            self.workspace,
            12,
            [_call("m3", "web_search", {"query": "brownfeed id"})],
            ["result"],
            {"m3"},
        )

        lines = trajectory.micro_index(self.workspace, 11, 12)

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("[Step 11: "))
        self.assertTrue(lines[1].startswith("[Step 12: "))
        self.assertIn("bash_exec", lines[0])
        self.assertIn("read_file", lines[0])
        self.assertTrue(all("\n" not in line for line in lines))
        self.assertTrue(all(len(line) <= trajectory.MICRO_LINE_MAX_CHARS for line in lines))

    def test_procedural_source_reports_only_a_contiguous_prefix_within_budget(self):
        # Production mutation caught: selecting newest unseen records and then
        # advancing through the requested endpoint permanently skips the older
        # records omitted by the source budget.
        for step in range(4, 7):
            trajectory.append_tool_group(
                self.workspace,
                step,
                [_call(f"p{step}", "bash_exec", {"command": f"probe-{step}"})],
                [f"result-{step}"],
                {f"p{step}"},
            )

        result = trajectory.procedural_source(
            self.workspace, 6, max_chars=90, start_step=4
        )

        self.assertIsInstance(result, tuple)
        source, through = result
        self.assertLessEqual(len(source), 90)
        self.assertEqual(through, 4)
        self.assertIn("Step 4", source)
        self.assertIn("probe-4", source)
        self.assertNotIn("Step 5", source)
        self.assertNotIn("Step 6", source)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is unavailable")
    def test_trajectory_path_cannot_be_a_symlink_outside_the_run(self):
        # Mutation caught: following a pre-planted traj.jsonl symlink lets model-
        # writable workspace state append structured data to an arbitrary host file.
        outside = Path(self.temp_dir.name) / "outside.jsonl"
        outside.write_text("sentinel", encoding="utf-8")
        os.symlink(outside, self.path)

        with self.assertRaises(OSError):
            trajectory.append_tool_group(
                self.workspace,
                1,
                [_call("escape", "bash_exec", {"command": "true"})],
                ["[exit code: 0]"],
                {"escape"},
            )

        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")


class TrajectoryToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.catalog = RunCatalog(root / "workspace", root / "logs")
        self.workspace = self.catalog.acquire(TEST_USER_ID, CHANNEL_ID)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_lookup_tool_is_advertised_and_dispatched(self):
        # Mutation caught: implementing a reader without schema/dispatch wiring
        # leaves compacted history unreachable to the model.
        trajectory.append_tool_group(
            self.workspace,
            9,
            [_call("stored", "bash_exec", {"command": "printf stored"})],
            ["stored\n[exit code: 0]"],
            {"stored"},
        )
        schemas = {
            entry["function"]["name"]: entry["function"]
            for entry in bot.agent_tool_params()["tools"]
        }

        direct = json.loads(
            await bot.tool_lookup_trajectory(self.workspace, 9, "stored")
        )
        [dispatched_raw] = await bot.execute_tools_in_parallel(
            self.workspace,
            [{
                "id": "lookup-call",
                "name": "lookup_trajectory",
                "arguments": {"step": 9, "call_id": "stored"},
            }],
        )
        dispatched = json.loads(dispatched_raw)

        self.assertIn("lookup_trajectory", schemas)
        parameters = schemas["lookup_trajectory"]["parameters"]
        self.assertEqual(parameters["required"], ["step"])
        self.assertEqual(parameters["properties"]["step"]["minimum"], 1)
        self.assertEqual(direct["status"], "success")
        self.assertEqual(dispatched, direct)
        self.assertEqual(dispatched["records"][0]["call_id"], "stored")

    async def test_missing_lookup_is_neutral_but_io_errors_are_failures(self):
        # Mutation caught: treating not_found as a failed tool consumes the
        # existing consecutive-failure allowance just for asking about history.
        missing_raw = await bot.tool_lookup_trajectory(self.workspace, 999)
        missing = json.loads(missing_raw)

        self.assertEqual(missing["status"], "not_found")
        self.assertFalse(bot._tool_result_failed("lookup_trajectory", missing_raw))
        self.assertTrue(bot._tool_result_failed(
            "lookup_trajectory", '{"status":"error","error":"PermissionError"}'
        ))


if __name__ == "__main__":
    unittest.main()
