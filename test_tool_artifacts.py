"""Oversized tool output is persisted as a run artifact, not thrown away.

Issue #48. The clamp these tests replace kept the first 2500 characters and
discarded the rest, so nothing downstream could ever recover the tail.
"""

import hashlib
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_support import TEST_USER_ID, run_catalog_patch

import bot
import trajectory


CHANNEL_ID = 987654830
ARTIFACT_PATH_PATTERN = r"artifacts/out_[A-Za-z0-9_.-]+\.log"


def artifact_path(result):
    return re.search(ARTIFACT_PATH_PATTERN, result).group(0)


def long_bash_command(character="x"):
    script = (
        "import sys; sys.stdout.write({0!r} * 5001); raise SystemExit(7)".format(
            character
        )
    )
    return "{0} -c {1}".format(
        shlex.quote(sys.executable), shlex.quote(script)
    )


def search_worker(count):
    """A web_search worker reply whose formatted text overflows the budget."""
    return AsyncMock(return_value={
        "status": "success",
        "results": [
            {
                "title": "제목 {0}".format(index),
                "href": "https://example.com/{0}".format(index),
                "body": "본문 " * 40,
            }
            for index in range(count)
        ],
    })


class ToolArtifactTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        catalog = run_catalog_patch(bot, self.temp_dir.name)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.run = bot.RUN_CATALOG.acquire(TEST_USER_ID, CHANNEL_ID)

    def artifact_files(self):
        directory = self.run.root / "artifacts"
        return sorted(path for path in directory.glob("*") if path.is_file())

    def files_outside_run_root(self):
        base = Path(self.temp_dir.name)
        return sorted(
            str(path.relative_to(base))
            for path in base.rglob("*")
            if path.is_file() and self.run.root not in path.parents
        )

    # Mutation caught: clamping the bash payload to the context budget and
    # returning the head throws the tail away, so nothing can recover it later.
    async def test_long_bash_output_is_persisted_in_full_and_summarized(self):
        result = await bot.tool_bash_exec(
            self.run, long_bash_command(), "bash-call-1"
        )

        path = artifact_path(result)
        self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
        self.assertEqual(
            (self.run.root / path).read_text(encoding="utf-8").count("x"), 5001
        )

    # Mutation caught: appending the artifact path or the grep hint after the
    # exit marker unanchors _tool_result_failed and disables the failure brake.
    async def test_summarized_bash_output_still_ends_with_the_exit_marker(self):
        result = await bot.tool_bash_exec(
            self.run, long_bash_command(), "bash-call-2"
        )

        self.assertRegex(result, r"\[exit code: 7\]\s*$")
        self.assertTrue(bot._tool_result_failed("bash_exec", result))

    # Mutation caught: encapsulating every result buries a short, already
    # readable tool output behind a file the model has to open to read it.
    async def test_short_bash_output_is_returned_verbatim(self):
        result = await bot.tool_bash_exec(self.run, "printf ok", "bash-call-3")

        self.assertEqual(result, "[stdout]\nok\n[exit code: 0]")
        self.assertFalse((self.run.root / "artifacts").exists())

    # Mutation caught: joining the model-supplied call id into the artifact path
    # unsanitized lets one line of model output write outside the run root.
    async def test_traversal_call_id_cannot_write_outside_the_run_root(self):
        before = self.files_outside_run_root()

        result = await bot.tool_bash_exec(
            self.run, long_bash_command(), "../../../../pwned"
        )

        self.assertEqual(self.files_outside_run_root(), before)
        self.assertEqual(len(self.artifact_files()), 1)
        self.assertEqual(
            self.artifact_files()[0].read_text(encoding="utf-8").count("x"), 5001
        )
        self.assertIn("artifacts/out_", result)
        self.assertRegex(result, r"\[exit code: 7\]\s*$")

    # Mutation caught: resolving only the artifact filename still lets a
    # preplanted directory symlink redirect the parent process outside the run.
    async def test_artifact_directory_symlink_cannot_escape_run_root(self):
        outside = Path(self.temp_dir.name) / "outside-artifacts"
        outside.mkdir()
        original_mode = outside.stat().st_mode
        (self.run.root / "artifacts").symlink_to(
            outside, target_is_directory=True
        )

        result = await bot.tool_bash_exec(
            self.run, long_bash_command(), "symlink-call"
        )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(outside.stat().st_mode, original_mode)
        self.assertNotIn("artifacts/out_", result)
        self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
        self.assertRegex(result, r"\[exit code: 7\]\s*$")

    # Mutation caught: preserving case-sensitive IDs verbatim makes distinct
    # calls alias the same artifact on a case-insensitive filesystem.
    async def test_case_variant_call_ids_have_distinct_artifacts(self):
        first = await bot.tool_bash_exec(
            self.run, long_bash_command("x"), "CallA"
        )
        second = await bot.tool_bash_exec(
            self.run, long_bash_command("y"), "calla"
        )

        first_path = artifact_path(first)
        second_path = artifact_path(second)
        self.assertNotEqual(first_path.casefold(), second_path.casefold())
        self.assertEqual(
            (self.run.root / first_path).read_text(encoding="utf-8").count("x"),
            5001,
        )
        self.assertEqual(
            (self.run.root / second_path).read_text(encoding="utf-8").count("y"),
            5001,
        )

    # Mutation caught: raw safe IDs and hashed unsafe IDs sharing one namespace
    # lets a second, distinct call silently replace the first call's full text.
    async def test_safe_call_id_cannot_overwrite_hashed_call_id_artifact(self):
        unsafe_id = "../../same-artifact"
        colliding_safe_id = hashlib.sha256(unsafe_id.encode("utf-8")).hexdigest()[:32]

        first = await bot.tool_bash_exec(
            self.run, long_bash_command("x"), unsafe_id
        )
        second = await bot.tool_bash_exec(
            self.run, long_bash_command("y"), colliding_safe_id
        )

        first_path = artifact_path(first)
        second_path = artifact_path(second)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(
            (self.run.root / first_path).read_text(encoding="utf-8").count("x"),
            5001,
        )
        self.assertEqual(
            (self.run.root / second_path).read_text(encoding="utf-8").count("y"),
            5001,
        )

    # Mutation caught: leaving web_search unbounded lets one search reply push
    # an arbitrary number of characters straight into the model context.
    async def test_long_web_search_result_is_persisted_in_full_and_summarized(self):
        with patch.object(bot.tool_sandbox, "run_worker", search_worker(20)):
            result = await bot.tool_web_search(self.run, "질의", "search-call-1")

        path = artifact_path(result)
        self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
        stored = (self.run.root / path).read_text(encoding="utf-8")
        self.assertIn("https://example.com/19", stored)

    # Mutation caught: writing an artifact per call with no run-level budget
    # fills the workspace until the disk monitor aborts every later bash call.
    async def test_artifacts_stop_at_the_run_budget_without_losing_the_marker(self):
        command = long_bash_command()
        with patch.object(bot, "ARTIFACT_RUN_BYTE_BUDGET", 6000):
            first = await bot.tool_bash_exec(self.run, command, "budget-1")
            second = await bot.tool_bash_exec(self.run, command, "budget-2")

        first_path = artifact_path(first)
        self.assertNotIn("artifacts/out_", second)
        self.assertEqual(
            [path.name for path in self.artifact_files()], [Path(first_path).name]
        )
        for result in (first, second):
            self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
            self.assertRegex(result, r"\[exit code: 7\]\s*$")

    # Mutation caught: routing read_file through artifact encapsulation creates
    # a redundant copy even though the full source and its revision are durable.
    async def test_long_read_file_uses_source_without_an_artifact_copy(self):
        content = "read-file-source\n" * 300
        source = self.run.root / "large.txt"
        source.write_text(content, encoding="utf-8")

        result = json.loads(await bot.tool_read_file(self.run, "large.txt"))

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], content[:2500])
        self.assertEqual(
            result["revision"],
            "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(source.read_text(encoding="utf-8"), content)
        self.assertFalse((self.run.root / "artifacts").exists())

    # Mutation caught: writing the artifact somewhere the sandboxed shell cannot
    # reach makes the grep hint a lie and the stored output unreadable.
    async def test_the_next_bash_call_can_read_the_artifact_it_was_pointed_at(self):
        stored = await bot.tool_bash_exec(self.run, long_bash_command(), "grep-me")
        path = artifact_path(stored)

        counted = await bot.tool_bash_exec(
            self.run, f"wc -c < {path}", "count-call"
        )

        self.assertIn("5010", counted)
        self.assertRegex(counted, r"\[exit code: 0\]\s*$")

    async def test_parallel_artifact_slots_follow_long_and_short_calls(self):
        artifact_paths = [None, None]
        long_result, short_result = await bot.execute_tools_in_parallel(
            self.run,
            [
                {
                    "id": "long-dispatch",
                    "name": "bash_exec",
                    "arguments": {"command": long_bash_command()},
                },
                {
                    "id": "short-dispatch",
                    "name": "bash_exec",
                    "arguments": {"command": "printf ok"},
                },
            ],
            artifact_paths=artifact_paths,
        )

        self.assertEqual(
            artifact_paths, [artifact_path(long_result), None]
        )
        self.assertEqual(short_result, "[stdout]\nok\n[exit code: 0]")

    async def test_lookup_trajectory_response_is_aggregate_bounded(self):
        calls = [
            {
                "id": f"lookup-source-{index}",
                "name": "bash_exec",
                "arguments": {"command": f"probe-{index}"},
                "failed": False,
            }
            for index in range(20)
        ]
        trajectory.append_tool_group(
            self.run,
            7,
            calls,
            ["x" * 5000 for _ in calls],
            {call["id"] for call in calls},
        )

        artifact_paths = [None]
        [result] = await bot.execute_tools_in_parallel(
            self.run,
            [{
                "id": "lookup-envelope",
                "name": "lookup_trajectory",
                "arguments": {"step": 7},
            }],
            artifact_paths=artifact_paths,
        )

        self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
        path = artifact_path(result)
        self.assertEqual(artifact_paths, [path])
        stored = json.loads((self.run.root / path).read_text(encoding="utf-8"))
        self.assertEqual(stored["total"], 20)
        self.assertEqual(len(stored["records"]), 20)


if __name__ == "__main__":
    unittest.main()
