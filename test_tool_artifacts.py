"""Oversized tool output is persisted as a run artifact, not thrown away.

Issue #48. The clamp these tests replace kept the first 2500 characters and
discarded the rest, so nothing downstream could ever recover the tail.
"""

import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_support import TEST_USER_ID, run_catalog_patch

import bot


CHANNEL_ID = 987654830
LONG_STDOUT_SCRIPT = 'import sys; sys.stdout.write("x" * 5001); raise SystemExit(7)'


def long_bash_command():
    return "{0} -c {1}".format(
        shlex.quote(sys.executable), shlex.quote(LONG_STDOUT_SCRIPT)
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

        self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
        self.assertIn("artifacts/out_bash-call-1.log", result)
        artifact = self.run.root / "artifacts" / "out_bash-call-1.log"
        self.assertEqual(artifact.read_text(encoding="utf-8").count("x"), 5001)

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

    # Mutation caught: leaving web_search unbounded lets one search reply push
    # an arbitrary number of characters straight into the model context.
    async def test_long_web_search_result_is_persisted_in_full_and_summarized(self):
        with patch.object(bot.tool_sandbox, "run_worker", search_worker(20)):
            result = await bot.tool_web_search(self.run, "질의", "search-call-1")

        self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
        self.assertIn("artifacts/out_search-call-1.log", result)
        artifact = self.run.root / "artifacts" / "out_search-call-1.log"
        stored = artifact.read_text(encoding="utf-8")
        self.assertIn("https://example.com/19", stored)

    # Mutation caught: writing an artifact per call with no run-level budget
    # fills the workspace until the disk monitor aborts every later bash call.
    async def test_artifacts_stop_at_the_run_budget_without_losing_the_marker(self):
        command = long_bash_command()
        with patch.object(bot, "ARTIFACT_RUN_BYTE_BUDGET", 6000):
            first = await bot.tool_bash_exec(self.run, command, "budget-1")
            second = await bot.tool_bash_exec(self.run, command, "budget-2")

        self.assertIn("artifacts/out_budget-1.log", first)
        self.assertNotIn("artifacts/out_budget-2.log", second)
        self.assertEqual(
            [path.name for path in self.artifact_files()], ["out_budget-1.log"]
        )
        for result in (first, second):
            self.assertLess(len(result), bot.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
            self.assertRegex(result, r"\[exit code: 7\]\s*$")


    # Mutation caught: writing the artifact somewhere the sandboxed shell cannot
    # reach makes the grep hint a lie and the stored output unreadable.
    async def test_the_next_bash_call_can_read_the_artifact_it_was_pointed_at(self):
        stored = await bot.tool_bash_exec(self.run, long_bash_command(), "grep-me")
        path = re.search(r"artifacts/out_grep-me\.log", stored).group(0)

        counted = await bot.tool_bash_exec(
            self.run, f"wc -c < {path}", "count-call"
        )

        self.assertIn("5010", counted)
        self.assertRegex(counted, r"\[exit code: 0\]\s*$")


if __name__ == "__main__":
    unittest.main()
