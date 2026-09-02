"""The nine reproduction scenarios from issue #11.

Default logging must carry structured metadata only: no raw reasoning, no raw
tool arguments, no raw tool results, no raw user text - in the session log, on
stdout/stderr, or in what Discord receives. Identity (time, revision, process,
run, step) must be on every record instead.

Every canary below is synthetic. No production ids, tokens, or paths.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import FakeMessage, TEST_USER_ID, run_catalog_patch
from test_terminal_state import RunRecorder, TerminalStateTestCase, _response, _tool_call

import bot
import outcome as outcome_mod
import session_log
import steering as steering_mod
from run_workspace import RunCatalog

CHANNEL_ID = 987654611

GOAL_CANARY = "GOALCANARY-a1b2c3"
REASONING_CANARY = "REASONCANARY-d4e5f6"
TOOL_ARG_CANARY = "TOOLARGCANARY-g7h8i9"
TOOL_RESULT_CANARY = "TOOLRESULTCANARY-j1k2l3"
EXCEPTION_CANARY = "EXCEPTIONCANARY-m4n5o6"
STEERING_CANARY = "STEERINGCANARY-p7q8r9"

ALL_CANARIES = (
    GOAL_CANARY,
    REASONING_CANARY,
    TOOL_ARG_CANARY,
    TOOL_RESULT_CANARY,
    EXCEPTION_CANARY,
)

LONG_REPORT = "조사 결과 정리. " + ("확인됨. " * 80)


def _workspace_double(root, run_id="0" * 32):
    """Smallest object the sink needs: a run id and a log path."""
    return SimpleNamespace(run_id=run_id, log_path=Path(root) / (run_id + ".jsonl"))


def _records(log_path):
    return [
        json.loads(line)
        for line in Path(log_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_logs(log_root):
    return sorted(Path(log_root).glob("logs/runs/*.jsonl"))


class LogSinkTestCase(TerminalStateTestCase):
    """Drives a real run but keeps the log tree so it can be read afterwards."""

    channel_id = CHANNEL_ID

    def setUp(self):
        super().setUp()
        bot.FREE_RESPONSE_CHANNEL_IDS.add(CHANNEL_ID)
        self._scratch = tempfile.TemporaryDirectory()
        self.log_root = self._scratch.name
        self.stdout = StringIO()
        self.stderr = StringIO()

    def tearDown(self):
        super().tearDown()
        bot.FREE_RESPONSE_CHANNEL_IDS.discard(CHANNEL_ID)
        for state in (
            bot.channel_history,
            bot.channel_summary,
            bot.channel_reasoning,
            bot.channel_cancel_token,
            bot.channel_active_runs,
            bot.channel_ledger,
        ):
            state.pop(CHANNEL_ID, None)
        bot.channel_run_leases.pop(CHANNEL_ID, None)
        bot.channel_run_owner.pop(CHANNEL_ID, None)
        self._scratch.cleanup()

    async def drive_captured(self, *args, **kwargs):
        """Run the agent with stdout/stderr captured, as the issue requires."""
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            message = await self.drive(*args, **kwargs)
        return message

    @property
    def console(self):
        return self.stdout.getvalue() + self.stderr.getvalue()

    @property
    def log_text(self):
        return "\n".join(
            path.read_text(encoding="utf-8") for path in _run_logs(self.log_root)
        )

    @property
    def log_records(self):
        records = []
        for path in _run_logs(self.log_root):
            records.extend(_records(path))
        return records

    def discord_text(self, message):
        edits = []
        for handle in message.reply_handles + message.channel.sent_messages:
            edits.extend(str(edit) for edit in handle.edits)
        return "\n".join(message.replies + message.channel.sent + edits)


class DefaultSinkContentTest(LogSinkTestCase):
    async def test_11_canaries_never_reach_the_session_log_or_console(self):
        # Production mutation caught: logging raw reasoning, tool arguments,
        # tool results, or user text again instead of derived metadata.
        message = await self.drive_captured(
            [
                _response(
                    content="점검을 시작합니다.",
                    reasoning=f"내부 추론: {REASONING_CANARY}",
                    tool_calls=[_tool_call("c1", "bash_exec", {"command": f"probe {TOOL_ARG_CANARY}"})],
                ),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
            ],
            request=f"{GOAL_CANARY} 상태를 조사해줘",
            tool_result=f"[stdout]\n{TOOL_RESULT_CANARY}\n[exit code: 0]",
        )

        for canary in ALL_CANARIES:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, self.log_text)
                self.assertNotIn(canary, self.console)
        self.assertNotIn(REASONING_CANARY, self.discord_text(message))
        self.assertNotIn(GOAL_CANARY, self.discord_text(message))

        # The metadata that replaces it still has to be there.
        kinds = {record["kind"] for record in self.log_records}
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        tool_call = next(r for r in self.log_records if r["kind"] == "tool_call")
        self.assertEqual(tool_call["tool"], "bash_exec")

    async def test_11_exception_text_is_recorded_by_type_not_by_message(self):
        # Production mutation caught: writing str(exception) into the log or the
        # channel, which carries whatever value blew up.
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr), \
                patch.object(
                    bot, "format_full_discord_output",
                    side_effect=RuntimeError(EXCEPTION_CANARY),
                ):
            message = await self.run_agent(
                [_response(tool_calls=[_tool_call("c1", "finish_task", {"report": LONG_REPORT})])],
            )

        discord_text = self.discord_text(message)
        self.assertNotIn(EXCEPTION_CANARY, self.log_text)
        self.assertNotIn(EXCEPTION_CANARY, self.console)
        self.assertNotIn(EXCEPTION_CANARY, discord_text)
        self.assertIn("RuntimeError", self.log_text)

    async def test_11_steering_text_is_counted_not_copied(self):
        # Production mutation caught: mid-flight steering being appended to the
        # log verbatim, which is how user text got in there in the first place.
        steering = FakeMessage(STEERING_CANARY, CHANNEL_ID)
        bot.channel_active_runs[CHANNEL_ID] = True
        catalog = RunCatalog(Path(self.log_root) / "workspace", Path(self.log_root) / "logs")
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
        mailbox = steering_mod.SteeringMailbox(
            workspace.run_id, max_depth=bot.STEERING_QUEUE_MAX
        )
        mailbox.open()
        bot.channel_run_leases[CHANNEL_ID].append(
            {
                "token": bot.CancelToken(),
                "owner": TEST_USER_ID,
                "active": True,
                "workspace": workspace,
                "steering": mailbox,
            }
        )

        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            await bot.on_message(steering)

        # The instruction itself is still held in memory for the next step - it
        # is the *sinks* that must only ever see a count.
        self.assertEqual([item.text for item in mailbox.drain()], [STEERING_CANARY])
        self.assertNotIn(STEERING_CANARY, self.log_text)
        self.assertNotIn(STEERING_CANARY, self.console)
        self.assertNotIn(STEERING_CANARY, "\n".join(steering.replies))
        kinds = {record["kind"] for record in self.log_records}
        self.assertIn("steering_received", kinds)


class ReasoningNeverRenderedTest(unittest.IsolatedAsyncioTestCase):
    def test_11_thinking_block_is_dropped_from_discord_output(self):
        # Production mutation caught: restoring the 💭 thinking block, which
        # published raw chain of thought to the channel.
        rendered = bot.format_full_discord_output(
            f"<think>\n{REASONING_CANARY}\n</think>\n\n최종 답변입니다."
        )
        self.assertNotIn(REASONING_CANARY, rendered)
        self.assertNotIn("심층 추론", rendered)
        self.assertIn("최종 답변입니다.", rendered)

    def test_11_reasoning_only_output_becomes_a_bounded_status_line(self):
        # Production mutation caught: falling back to printing the reasoning
        # when the model produced nothing else.
        rendered = bot.format_full_discord_output(f"<think>\n{REASONING_CANARY}\n</think>")
        self.assertNotIn(REASONING_CANARY, rendered)
        self.assertTrue(rendered.strip())


class LiveStatusCardTest(LogSinkTestCase):
    async def test_11_live_card_reports_progress_without_reasoning(self):
        # Production mutation caught: the dashboard card echoing the last lines
        # of the reasoning trace back into the channel.
        message = await self.drive_captured(
            [
                _response(
                    content="",
                    reasoning=f"단계별 판단: {REASONING_CANARY}",
                    tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})],
                ),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
            ],
        )

        card_text = "\n".join(
            str(edit) for handle in message.reply_handles for edit in handle.edits
        )
        self.assertNotIn(REASONING_CANARY, card_text)
        self.assertIn("Step", card_text)


class RecordIdentityTest(LogSinkTestCase):
    async def test_11_every_record_carries_time_revision_process_run_and_step(self):
        # Production mutation caught: dropping any identifier that lets an
        # operator line a stdout line up with a run and a step.
        await self.drive_captured(
            [
                _response(content="확인 중", tool_calls=[_tool_call("c1", "bash_exec", {"command": "probe"})]),
                _response(tool_calls=[_tool_call("c2", "finish_task", {"report": LONG_REPORT})]),
            ],
        )

        records = self.log_records
        self.assertTrue(records)
        run_ids = set()
        for record in records:
            for field in ("ts", "rev", "pid", "run", "step", "kind"):
                self.assertIn(field, record)
            self.assertRegex(record["ts"], r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
            self.assertRegex(record["run"], r"\A[0-9a-f]{32}\Z")
            self.assertIsInstance(record["step"], int)
            self.assertEqual(record["pid"], os.getpid())
            run_ids.add(record["run"])
        self.assertEqual(len(run_ids), 1)
        self.assertNotIn(bot.DISCORD_TOKEN, self.log_text)

        # Every console line is a record too, so stdout can be correlated.
        for line in [l for l in self.stdout.getvalue().splitlines() if l.strip()]:
            parsed = json.loads(line)
            self.assertIn("ts", parsed)
            self.assertIn("rev", parsed)
            self.assertIn("pid", parsed)

    async def test_11_two_runs_in_one_day_stay_separable(self):
        # Production mutation caught: collapsing runs back into one shared file
        # keyed by date and channel.
        for _ in range(2):
            await self.drive_captured(
                [_response(tool_calls=[_tool_call("c1", "finish_task", {"report": LONG_REPORT})])],
            )

        logs = _run_logs(self.log_root)
        self.assertEqual(len(logs), 2)
        runs = set()
        for path in logs:
            record_runs = {record["run"] for record in _records(path)}
            self.assertEqual(len(record_runs), 1)
            self.assertEqual(record_runs.pop(), path.stem)
            runs.add(path.stem)
        self.assertEqual(len(runs), 2)


class TerminalEventTest(LogSinkTestCase):
    def _terminals(self):
        return [r for r in self.log_records if r["kind"] == "run_end"]

    def _starts(self):
        return [r for r in self.log_records if r["kind"] == "run_start"]

    async def test_11_finish_task_run_logs_one_start_and_one_terminal(self):
        # Production mutation caught: a second terminal record, or none, when a
        # run completes normally.
        await self.drive_captured(
            [_response(tool_calls=[_tool_call("c1", "finish_task", {"report": LONG_REPORT})])],
        )

        self.assertEqual(len(self._starts()), 1)
        self.assertEqual(len(self._terminals()), 1)
        terminal = self._terminals()[0]
        self.assertEqual(terminal["status"], outcome_mod.COMPLETED)
        self.assertIn(outcome_mod.DETAIL_FINISH_TASK, terminal["detail"])
        self.assertFalse(terminal["abnormal"])

    async def test_11_direct_answer_success_logs_a_terminal_event(self):
        # Production mutation caught: the direct-answer fast path returning
        # without a reason-bearing terminal record - the one run type that had
        # no terminal event at all.
        await self.drive_captured(
            [_response(content="짧은 답변입니다.")],
            request="파이썬이 뭔지 간단히 설명해줘",
        )

        self.assertEqual(len(self._starts()), 1)
        self.assertEqual(len(self._terminals()), 1)
        terminal = self._terminals()[0]
        self.assertEqual(terminal["status"], outcome_mod.COMPLETED)
        self.assertIn(outcome_mod.DETAIL_DIRECT_ANSWER, terminal["detail"])
        self.assertEqual(len(self.recorder.settled), 1)

    async def test_11_short_answer_fast_path_logs_a_terminal_event(self):
        # The request above reaches the direct answer from inside the loop. A
        # request that `wants_direct_response` actually matches never enters the
        # loop at all, and that is the path which used to return after a
        # stdout-only "간단 답변 완료" with no RunOutcome and no terminal record.
        self.assertTrue(bot.wants_direct_response("간단히 설명해줘"))

        await self.drive_captured(
            [_response(content="짧은 답변입니다.")],
            request="간단히 설명해줘",
        )

        self.assertEqual(len(self._starts()), 1)
        self.assertEqual(len(self._terminals()), 1)
        terminal = self._terminals()[0]
        self.assertEqual(terminal["status"], outcome_mod.COMPLETED)
        self.assertIn(outcome_mod.DETAIL_DIRECT_ANSWER, terminal["detail"])
        self.assertEqual(len(self.recorder.settled), 1)

    async def test_11_user_stop_is_distinguishable_from_completion(self):
        # Production mutation caught: an interrupted run logging the same
        # terminal status as a finished one.
        await self.drive_captured(
            [_response(content="계속 진행합니다.")],
            stop_after=1,
        )

        terminals = self._terminals()
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["status"], outcome_mod.STOPPED)

    async def test_11_abnormal_termination_is_marked(self):
        # Production mutation caught: an exception-terminated run being
        # indistinguishable from an orderly failure.
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr), \
                patch.object(
                    bot, "format_full_discord_output", side_effect=RuntimeError("boom")
                ):
            await self.run_agent(
                [_response(tool_calls=[_tool_call("c1", "finish_task", {"report": LONG_REPORT})])],
            )

        terminals = self._terminals()
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["status"], outcome_mod.FAILED)
        self.assertTrue(terminals[0]["abnormal"])
        self.assertEqual(len(self._starts()), 1)


class PermissionTest(unittest.IsolatedAsyncioTestCase):
    def test_11_modes_are_deterministic_under_a_permissive_umask(self):
        # Production mutation caught: relying on umask for log permissions, so a
        # permissive umask leaves run logs world-readable.
        script = """
import json, os, stat, sys
sys.path.insert(0, sys.argv[1])
os.umask(0)
import session_log
root = sys.argv[2]
loose_dir = os.path.join(root, "runs")
os.makedirs(loose_dir)
os.chmod(loose_dir, 0o777)
loose_file = os.path.join(loose_dir, "0" * 32 + ".jsonl")
with open(loose_file, "w") as handle:
    handle.write("")
os.chmod(loose_file, 0o666)
fresh_dir = os.path.join(root, "fresh")
session_log.secure_directory(loose_dir)
session_log.secure_directory(fresh_dir)
from types import SimpleNamespace
from pathlib import Path
for name in ("0" * 32, "1" * 32):
    workspace = SimpleNamespace(run_id=name, log_path=Path(loose_dir) / (name + ".jsonl"))
    session_log.log_session_event(workspace, "probe", step=1)
print(json.dumps({
    "corrected_dir": stat.S_IMODE(os.stat(loose_dir).st_mode),
    "fresh_dir": stat.S_IMODE(os.stat(fresh_dir).st_mode),
    "corrected_file": stat.S_IMODE(os.stat(loose_file).st_mode),
    "fresh_file": stat.S_IMODE(os.stat(os.path.join(loose_dir, "1" * 32 + ".jsonl")).st_mode),
}))
"""
        with tempfile.TemporaryDirectory() as root:
            completed = subprocess.run(
                [sys.executable, "-c", script, str(Path(__file__).parent), root],
                capture_output=True,
                text=True,
                check=True,
            )
        modes = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(modes["fresh_file"], 0o600)
        self.assertEqual(modes["corrected_file"], 0o600)
        self.assertEqual(modes["fresh_dir"], 0o700)
        self.assertEqual(modes["corrected_dir"], 0o700)


class RotationAndRetentionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.root = Path(self._scratch.name)
        self.log_root = self.root / "logs"
        session_log.secure_directory(self.log_root / "runs")

    def tearDown(self):
        self._scratch.cleanup()

    def test_11_oversized_run_log_rotates_and_keeps_one_generation(self):
        # Production mutation caught: an unbounded run log that grows until the
        # disk does the rotating.
        workspace = _workspace_double(self.log_root / "runs", "a" * 32)
        with patch.object(session_log, "MAX_BYTES", 400):
            for index in range(60):
                session_log.log_session_event(workspace, "probe", step=index)

        rotated = workspace.log_path.with_suffix(".1.jsonl")
        self.assertTrue(rotated.exists())
        self.assertTrue(workspace.log_path.exists())
        self.assertLessEqual(workspace.log_path.stat().st_size, 400 + 400)
        self.assertEqual(oct(rotated.stat().st_mode)[-3:], "600")

    def test_11_expired_logs_are_deleted_on_a_fake_clock(self):
        # Production mutation caught: a retention window that never expires, so
        # every run ever executed stays on disk forever.
        workspace = _workspace_double(self.log_root / "runs", "b" * 32)
        session_log.log_session_event(workspace, "probe", step=1)

        with patch.object(session_log, "LOG_ROOT", self.log_root), \
                patch.object(session_log, "RETENTION_SECONDS", 7 * 86400.0), \
                patch.object(session_log, "CONTENT_RETENTION_SECONDS", 3600.0), \
                patch.object(session_log, "CONTENT_DEBUG", True):
            session_log.log_content_debug(workspace, "probe", "detail", step=1)

            kept = session_log.sweep_retention(now=time.time())
            self.assertEqual(kept["logs"], 0)
            self.assertTrue(workspace.log_path.exists())

            # Content debugging expires first: hours, not days.
            swept = session_log.sweep_retention(now=time.time() + 2 * 3600)
            self.assertEqual(swept["content"], 1)
            self.assertTrue(workspace.log_path.exists())

            swept = session_log.sweep_retention(now=time.time() + 30 * 86400)
            self.assertEqual(swept["logs"], 1)
            self.assertFalse(workspace.log_path.exists())

    def test_11_expired_run_workspaces_are_deleted_and_active_runs_survive(self):
        # Production mutation caught: retention that only covers logs, leaving
        # every run's files - including its collected data - on disk forever.
        catalog = RunCatalog(self.root / "workspace", self.root / "logs")
        finished = catalog.acquire(TEST_USER_ID, CHANNEL_ID)
        catalog.finish(finished, "completed")
        active = catalog.acquire(TEST_USER_ID, CHANNEL_ID)

        deleted = catalog.sweep_retention(7 * 86400.0, now=time.time())
        self.assertEqual(deleted, 0)

        deleted = catalog.sweep_retention(7 * 86400.0, now=time.time() + 30 * 86400)
        self.assertEqual(deleted, 1)
        self.assertFalse(finished.root.exists())
        self.assertTrue(active.root.exists())


class ContentDebugTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.log_root = Path(self._scratch.name) / "logs"
        session_log.secure_directory(self.log_root / "runs")

    def tearDown(self):
        self._scratch.cleanup()

    def test_11_content_debug_is_denied_by_default(self):
        # Production mutation caught: content-level capture defaulting to on,
        # which reintroduces the whole leak behind a config field.
        self.assertFalse(session_log.CONTENT_DEBUG)
        self.assertFalse(bot.CONFIG.log_content_debug)

        workspace = _workspace_double(self.log_root / "runs", "c" * 32)
        with patch.object(session_log, "LOG_ROOT", self.log_root):
            written = session_log.log_content_debug(workspace, "probe", REASONING_CANARY, step=1)

        self.assertFalse(written)
        self.assertFalse((self.log_root / "content-debug").exists())

    def test_11_opt_in_content_debug_writes_to_a_restricted_sink(self):
        # Production mutation caught: opt-in content capture landing in the
        # normal log or with normal permissions.
        workspace = _workspace_double(self.log_root / "runs", "d" * 32)
        with patch.object(session_log, "LOG_ROOT", self.log_root), \
                patch.object(session_log, "CONTENT_DEBUG", True), \
                redirect_stdout(StringIO()):
            session_log.log_session_event(workspace, "probe", step=1)
            written = session_log.log_content_debug(workspace, "probe", REASONING_CANARY, step=1)

        self.assertTrue(written)
        sink = self.log_root / "content-debug" / ("d" * 32 + ".jsonl")
        self.assertIn(REASONING_CANARY, sink.read_text(encoding="utf-8"))
        self.assertNotIn(REASONING_CANARY, workspace.log_path.read_text(encoding="utf-8"))
        self.assertEqual(oct(sink.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(sink.parent.stat().st_mode)[-3:], "700")


class StartupRecordTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.root = Path(self._scratch.name)
        self.log_root = self.root / "logs"

    def tearDown(self):
        self._scratch.cleanup()

    def test_11_startup_writes_commit_dependencies_and_config(self):
        # Production mutation caught: no way to tie an observation to the
        # deployment that produced it.
        # The catalog is redirected too: startup maintenance sweeps run
        # workspaces, which must never touch a real run tree during a test.
        with patch.object(session_log, "LOG_ROOT", self.log_root), \
                run_catalog_patch(bot, self.root), \
                redirect_stdout(StringIO()):
            path = bot.startup_maintenance()

        record = _records(path)[-1]
        self.assertEqual(record["kind"], "startup")
        self.assertTrue(record["rev"])
        self.assertIn("python", record["deps"])
        self.assertIn("discord.py", record["deps"])
        config_text = "\n".join(record["config"])
        self.assertIn("model:", config_text)
        self.assertIn("tools:", config_text)
        self.assertNotIn(bot.DISCORD_TOKEN, config_text)
        self.assertEqual(oct(Path(path).stat().st_mode)[-3:], "600")


if __name__ == "__main__":
    unittest.main()
