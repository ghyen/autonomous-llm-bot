"""Issue #7 acceptance tests for isolated, owner-bound run workspaces."""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import (
    FakeAuthor,
    FakeMessage,
    TEST_ADMIN_ID,
    TEST_USER_ID,
)

import bot

try:
    import run_workspace
except ModuleNotFoundError:
    run_workspace = None


CHANNEL_A = 987654810
CHANNEL_B = 987654811


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class WorkspaceTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp_dir.name) / "workspace"
        self.log_root = Path(self.temp_dir.name) / "logs"

    def tearDown(self):
        self.temp_dir.cleanup()

    def catalog(self):
        self.assertIsNotNone(
            run_workspace,
            "production mutation caught: the run workspace/catalog module is missing",
        )
        return run_workspace.RunCatalog(self.workspace_root, self.log_root)

    @staticmethod
    def metadata(workspace):
        return json.loads((workspace.root / "run.json").read_text(encoding="utf-8"))


class CatalogIsolationTest(WorkspaceTestCase):
    async def test_opaque_metadata_and_same_or_cross_channel_runs_are_isolated(self):
        # Production mutation caught: deriving paths from Discord IDs or reusing a
        # channel-global directory lets overlapping runs overwrite each other.
        catalog = self.catalog()
        first = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        same_channel = catalog.acquire(TEST_ADMIN_ID, CHANNEL_A)
        other_channel = catalog.acquire(TEST_USER_ID, CHANNEL_B)

        await asyncio.gather(
            first.write("relative.txt", "first", None),
            same_channel.write("relative.txt", "same-channel", None),
            other_channel.write("relative.txt", "other-channel", None),
        )

        self.assertEqual(
            {
                (first.root / "relative.txt").read_text(),
                (same_channel.root / "relative.txt").read_text(),
                (other_channel.root / "relative.txt").read_text(),
            },
            {"first", "same-channel", "other-channel"},
        )
        self.assertEqual(len({first.run_id, same_channel.run_id, other_channel.run_id}), 3)
        for workspace in (first, same_channel, other_channel):
            self.assertRegex(workspace.run_id, r"\A[0-9a-f]{32}\Z")
            self.assertEqual(workspace.root.parent, self.workspace_root / "runs")
            self.assertEqual(workspace.log_path.parent, self.log_root / "runs")
            for raw_id in (TEST_USER_ID, TEST_ADMIN_ID, CHANNEL_A, CHANNEL_B):
                self.assertNotIn(str(raw_id), str(workspace.root))
                self.assertNotIn(str(raw_id), str(workspace.log_path))
            metadata = self.metadata(workspace)
            self.assertEqual(metadata["run_id"], workspace.run_id)
            self.assertEqual(metadata["owner_id"], workspace.owner_id)
            self.assertEqual(metadata["channel_id"], workspace.channel_id)
            self.assertEqual(metadata["status"], "active")
            self.assertIn("created_at", metadata)
            self.assertIn("updated_at", metadata)

    async def test_startup_interrupts_stale_active_without_legacy_migration(self):
        # Production mutation caught: startup that trusts stale active metadata
        # makes a crashed run permanently non-resumable or migrates global files.
        catalog = self.catalog()
        legacy = self.workspace_root / "plan.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"legacy-global-bytes")
        active = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        await active.write("plan.md", "before restart", "absent")

        restarted = run_workspace.RunCatalog(self.workspace_root, self.log_root)
        interrupted = restarted.lookup_owned(TEST_USER_ID, active.run_id)

        self.assertEqual(interrupted.status, "interrupted")
        self.assertEqual(self.metadata(interrupted)["status"], "interrupted")
        self.assertEqual(legacy.read_bytes(), b"legacy-global-bytes")
        restarted.resume(TEST_USER_ID, CHANNEL_B, active.run_id)
        resumed = restarted.acquire(TEST_USER_ID, CHANNEL_B)
        first_read = resumed.read("plan.md")
        self.assertEqual(first_read["status"], "success")
        self.assertEqual(first_read["content"], "before restart")
        self.assertEqual(resumed.channel_id, CHANNEL_B)

    async def test_exact_owner_resume_delete_and_active_rejections_are_private(self):
        # Production mutation caught: reusing CONTROL/admin authority as a
        # workspace ACL leaks run existence or allows cross-owner destruction.
        catalog = self.catalog()
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        await workspace.write("notes.txt", "private", None)
        workspace.log_path.write_text("private log", encoding="utf-8")

        with self.assertRaises(run_workspace.RunActiveError):
            catalog.prepare(TEST_USER_ID, CHANNEL_A)
        with self.assertRaises(run_workspace.RunActiveError):
            catalog.delete(TEST_USER_ID, workspace.run_id)

        catalog.finish(workspace, "stopped")
        for operation in (
            lambda: catalog.lookup_owned(TEST_ADMIN_ID, workspace.run_id),
            lambda: catalog.resume(TEST_ADMIN_ID, CHANNEL_A, workspace.run_id),
            lambda: catalog.delete(TEST_ADMIN_ID, workspace.run_id),
        ):
            with self.assertRaises(run_workspace.RunNotFoundError):
                operation()

        catalog.resume(TEST_USER_ID, CHANNEL_B, workspace.run_id)
        resumed = catalog.acquire(TEST_USER_ID, CHANNEL_B)
        self.assertEqual(resumed.run_id, workspace.run_id)
        with self.assertRaises(run_workspace.RunActiveError):
            catalog.resume(TEST_USER_ID, CHANNEL_A, workspace.run_id)
        catalog.finish(resumed, "completed")
        root = resumed.root
        log_path = resumed.log_path
        catalog.delete(TEST_USER_ID, resumed.run_id)
        self.assertFalse(root.exists())
        self.assertFalse(log_path.exists())
        with self.assertRaises(run_workspace.RunNotFoundError):
            catalog.lookup_owned(TEST_USER_ID, resumed.run_id)
        self.assertFalse(hasattr(catalog, "share"))
        self.assertFalse(hasattr(catalog, "list_runs"))

    async def test_prepared_run_is_consumed_once_and_old_bytes_are_retained(self):
        # Production mutation caught: reset/new that truncates the old workspace
        # or leaves the selection reusable violates rollover and single-use rules.
        catalog = self.catalog()
        old = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        await old.write("notes.txt", "retained", None)
        catalog.finish(old, "completed")

        prepared = catalog.prepare(TEST_USER_ID, CHANNEL_A)
        self.assertEqual(prepared.status, "prepared")
        selected = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        following = catalog.acquire(TEST_USER_ID, CHANNEL_A)

        self.assertEqual(selected.run_id, prepared.run_id)
        self.assertNotEqual(following.run_id, prepared.run_id)
        self.assertEqual((old.root / "notes.txt").read_text(), "retained")
        self.assertEqual(list(selected.root.iterdir()), [selected.root / "run.json"])

    async def test_latest_cross_channel_resume_is_the_only_selection(self):
        # Production mutation caught: retaining the old channel selection lets
        # it steal a run after a later resume explicitly selected a new channel.
        catalog = self.catalog()
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        catalog.finish(workspace, "stopped")

        catalog.resume(TEST_USER_ID, CHANNEL_A, workspace.run_id)
        catalog.resume(TEST_USER_ID, CHANNEL_B, workspace.run_id)
        stale_channel = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        latest_channel = catalog.acquire(TEST_USER_ID, CHANNEL_B)

        self.assertNotEqual(stale_channel.run_id, workspace.run_id)
        self.assertEqual(latest_channel.run_id, workspace.run_id)
        catalog.finish(stale_channel, "completed")
        catalog.finish(latest_channel, "completed")


class CanonicalIntegrityTest(WorkspaceTestCase):
    async def test_canonical_reads_are_complete_hash_aware_and_ordinary_reads_clip(self):
        # Production mutation caught: hashing clipped text, clipping canonical
        # content, or returning unchanged bytes defeats revision-aware research.
        workspace = self.catalog().acquire(TEST_USER_ID, CHANNEL_A)
        canonical_bytes = ("가" * 5000).encode("utf-8")
        (workspace.root / "plan.md").write_bytes(canonical_bytes)

        first = workspace.read("plan.md")
        unchanged = workspace.read("plan.md")
        changed_bytes = canonical_bytes + b"!"
        (workspace.root / "plan.md").write_bytes(changed_bytes)
        changed = workspace.read("plan.md")
        (workspace.root / "ordinary.txt").write_bytes(b"x" * 5001)
        ordinary = workspace.read("ordinary.txt")

        self.assertEqual(first["content"], canonical_bytes.decode())
        self.assertEqual(first["revision"], sha256(canonical_bytes))
        self.assertEqual(unchanged, {
            "status": "unchanged",
            "path": "plan.md",
            "revision": sha256(canonical_bytes),
            "reference": sha256(canonical_bytes),
        })
        self.assertEqual(changed["content"], changed_bytes.decode())
        self.assertEqual(changed["revision"], sha256(changed_bytes))
        self.assertTrue(ordinary["truncated"])
        self.assertEqual(len(ordinary["content"]), 4000)
        self.assertEqual(ordinary["revision"], sha256(b"x" * 5001))

    async def test_canonical_cas_is_atomic_and_concurrent_writers_have_one_winner(self):
        # Production mutation caught: comparing outside the lock or truncating in
        # place permits stale/concurrent writers to corrupt exact canonical bytes.
        workspace = self.catalog().acquire(TEST_USER_ID, CHANNEL_A)
        missing_revision = await workspace.write("plan.md", "x", None)
        invalid_revision = await workspace.write("plan.md", "x", "sha256:bad")
        created = await workspace.write("plan.md", "first\n", "absent")
        stale_bytes = (workspace.root / "plan.md").read_bytes()
        stale = await workspace.write("plan.md", "stale", "absent")

        self.assertEqual(missing_revision["status"], "error")
        self.assertEqual(invalid_revision["status"], "error")
        self.assertEqual(created["status"], "success")
        self.assertEqual(stale["status"], "conflict")
        self.assertEqual((workspace.root / "plan.md").read_bytes(), stale_bytes)

        with patch.object(
            run_workspace.os, "replace", wraps=os.replace
        ) as atomic_replace:
            updated = await workspace.write(
                "plan.md", "second\n", created["revision"]
            )
        self.assertEqual(updated["status"], "success")
        self.assertTrue(any(
            Path(call.args[1]) == workspace.root / "plan.md"
            for call in atomic_replace.call_args_list
        ))
        self.assertEqual((workspace.root / "plan.md").read_bytes(), b"second\n")

        expected = updated["revision"]
        first, second = await asyncio.gather(
            workspace.write("plan.md", "winner-a\n", expected),
            workspace.write("plan.md", "winner-b\n", expected),
        )
        statuses = sorted([first["status"], second["status"]])
        self.assertEqual(statuses, ["conflict", "success"])
        self.assertIn(
            (workspace.root / "plan.md").read_bytes(),
            (b"winner-a\n", b"winner-b\n"),
        )
        self.assertFalse(any(path.name.startswith(".plan.md.") for path in workspace.root.iterdir()))
        self.assertEqual(workspace.read("plan.md")["status"], "unchanged")

    async def test_relative_root_absolute_canonical_alias_cannot_bypass_cas(self):
        # Production mutation caught: keeping a relative catalog root makes an
        # absolute alias of the same canonical file look ordinary and skip CAS.
        catalog = run_workspace.RunCatalog(
            os.path.relpath(self.workspace_root),
            os.path.relpath(self.log_root),
        )
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_A)

        for name in ("plan.md", "findings.md"):
            created = await workspace.write(name, f"first-{name}", "absent")
            absolute_alias = os.path.abspath(str(workspace.root / name))
            bypass = await workspace.write(absolute_alias, "bypass", None)
            self.assertEqual(bypass["status"], "error")
            self.assertEqual(bypass["error"], "expected_revision_required")
            self.assertEqual(
                (workspace.root / name).read_text(encoding="utf-8"),
                f"first-{name}",
            )
            updated = await workspace.write(
                absolute_alias, f"second-{name}", created["revision"]
            )
            self.assertEqual(updated["status"], "success")

    async def test_read_hash_lru_is_per_execution_and_capped_at_128(self):
        # Production mutation caught: an unbounded/global hash cache leaks memory
        # and makes a resumed execution suppress its required first full read.
        catalog = self.catalog()
        workspace = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        for index in range(129):
            (workspace.root / f"file-{index}.txt").write_text(str(index), encoding="utf-8")

        self.assertEqual(workspace.read("file-0.txt")["status"], "success")
        for index in range(1, 129):
            workspace.read(f"file-{index}.txt")
        self.assertEqual(workspace.read("file-128.txt")["status"], "unchanged")
        self.assertEqual(workspace.read("file-0.txt")["status"], "success")

        written = await workspace.write("ordinary.txt", "updated", None)
        self.assertEqual(written["status"], "success")
        self.assertEqual(workspace.read("ordinary.txt")["status"], "unchanged")
        catalog.finish(workspace, "interrupted")
        catalog.resume(TEST_USER_ID, CHANNEL_A, workspace.run_id)
        resumed = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        self.assertEqual(resumed.read("ordinary.txt")["status"], "success")


class ToolIntegrationTest(WorkspaceTestCase):
    async def test_relative_paths_and_bash_use_the_run_root_without_broadening_scope(self):
        # Production mutation caught: retaining the global cwd/path join permits
        # run overlap, while realpath confinement would broaden Issue #7 scope.
        workspace = self.catalog().acquire(TEST_USER_ID, CHANNEL_A)
        self.assertEqual(workspace.resolve("nested/file.txt"), workspace.root / "nested/file.txt")
        with self.assertRaises(ValueError):
            workspace.resolve("../escape.txt")
        with self.assertRaises(ValueError):
            workspace.resolve("nested/../../escape.txt")
        absolute = Path(self.temp_dir.name) / "absolute.txt"
        self.assertEqual(workspace.resolve(str(absolute)), absolute)

        bash_result = await bot.tool_bash_exec(workspace, "pwd")
        self.assertIn(str(workspace.root), bash_result)
        write_result = json.loads(await bot.tool_write_file(
            workspace, "nested/file.txt", "inside", None
        ))
        read_result = json.loads(await bot.tool_read_file(workspace, "nested/file.txt"))
        escaped = json.loads(await bot.tool_read_file(workspace, "../escape.txt"))
        self.assertEqual(write_result["status"], "success")
        self.assertEqual(read_result["status"], "unchanged")
        self.assertEqual(read_result["reference"], write_result["revision"])
        self.assertEqual(
            (workspace.root / "nested/file.txt").read_text(encoding="utf-8"),
            "inside",
        )
        self.assertEqual(escaped["status"], "error")

    async def test_prompt_schema_and_dispatcher_receive_explicit_run_context(self):
        # Production mutation caught: a global/default context makes overlapping
        # dispatches and rollovers silently target whichever workspace won last.
        workspace = self.catalog().acquire(TEST_USER_ID, CHANNEL_A)
        prompt = bot.build_system_content(workspace, None, "summary")
        schema_text = json.dumps(bot.TOOLS_SCHEMA, ensure_ascii=False)

        self.assertIn(str(workspace.root), prompt)
        self.assertIn(workspace.run_id, prompt)
        self.assertNotIn(str(TEST_USER_ID), prompt)
        self.assertNotIn(str(CHANNEL_A), prompt)
        self.assertNotIn(str(bot.CONFIG.workspace_dir), schema_text)
        write_schema = next(
            item["function"] for item in bot.TOOLS_SCHEMA
            if item["function"]["name"] == "write_file"
        )
        self.assertIn("expected_revision", write_schema["parameters"]["properties"])

        calls = [{"name": "bash_exec", "arguments": {"command": "true"}}]
        with patch.object(bot, "tool_bash_exec", AsyncMock(return_value="ok")) as bash:
            result = await bot.execute_tools_in_parallel(workspace, calls)
        self.assertEqual(result, ["ok"])
        bash.assert_awaited_once_with(workspace, "true")

        messages = [{"role": "system", "content": prompt}]
        for index in range(10):
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": "bash_exec", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "name": "bash_exec",
                    "content": f"result-{index}",
                },
            ])
        summary_response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="rolled summary")
        )])
        with patch.object(
            bot, "run_completion_stage", AsyncMock(return_value=summary_response)
        ):
            rolled, summary = await bot.rollover_agent_context(
                workspace,
                messages,
                "",
                10,
                session_file=workspace.log_path,
            )
        self.assertEqual(summary, "rolled summary")
        self.assertIn(str(workspace.root), bot._msg_content(rolled[0]))
        self.assertIn(workspace.run_id, bot._msg_content(rolled[0]))
        self.assertTrue(workspace.log_path.exists())


class HandlerWorkspaceTest(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        bot.FREE_RESPONSE_CHANNEL_IDS.update((CHANNEL_A, CHANNEL_B))

    def tearDown(self):
        for channel_id in (CHANNEL_A, CHANNEL_B):
            bot.FREE_RESPONSE_CHANNEL_IDS.discard(channel_id)
            bot.channel_run_owner.pop(channel_id, None)
            bot.channel_cancel_token.pop(channel_id, None)
            bot.channel_run_leases.pop(channel_id, None)
            for state in (
                bot.channel_history,
                bot.channel_summary,
                bot.channel_reasoning,
                bot.channel_active_runs,
                bot.channel_user_queue,
                bot.channel_ledger,
            ):
                state.pop(channel_id, None)
        super().tearDown()

    async def test_direct_only_and_direct_fallback_keep_one_run_identity(self):
        # Production mutation caught: bypassing run creation for direct answers or
        # replacing context on fallback loses logs and splits one execution.
        catalog = self.catalog()
        direct_response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="direct answer", reasoning_content="", reasoning="", tool_calls=[]
        ))])
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot, "create_streaming_completion", AsyncMock(return_value=direct_response)), \
                patch.object(bot, "execute_tools_in_parallel", AsyncMock()) as execute:
            await bot.on_message(FakeMessage(
                "answer briefly", CHANNEL_A, author=FakeAuthor(TEST_USER_ID)
            ))
        execute.assert_not_awaited()
        run_dirs = list((self.workspace_root / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        direct_meta = json.loads((run_dirs[0] / "run.json").read_text())
        self.assertEqual(direct_meta["status"], "completed")
        self.assertTrue((self.log_root / "runs" / f"{direct_meta['run_id']}.md").exists())

        empty_direct = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="", reasoning_content="", reasoning="", tool_calls=[]
        ))])
        tool_call = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="", reasoning_content="", reasoning="", tool_calls=[SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="bash_exec", arguments='{"command":"true"}'),
            )]
        ))])
        finish = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="", reasoning_content="", reasoning="", tool_calls=[SimpleNamespace(
                id="call-2",
                function=SimpleNamespace(name="finish_task", arguments='{"report":"done"}'),
            )]
        ))])
        dispatcher = AsyncMock(return_value=["[exit code: 0]"])
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot, "MAX_AGENT_LOOPS", 3), \
                patch.object(bot, "create_streaming_completion", AsyncMock(
                    side_effect=[empty_direct, tool_call, finish]
                )), \
                patch.object(bot, "execute_tools_in_parallel", dispatcher):
            await bot.on_message(FakeMessage(
                "answer briefly", CHANNEL_B, author=FakeAuthor(TEST_USER_ID)
            ))
        run_dirs = list((self.workspace_root / "runs").iterdir())
        self.assertEqual(len(run_dirs), 2)
        fallback_workspace = dispatcher.await_args.args[0]
        self.assertIn(fallback_workspace.root, run_dirs)
        self.assertNotEqual(fallback_workspace.run_id, direct_meta["run_id"])
        self.assertEqual(self.metadata(fallback_workspace)["status"], "completed")
        self.assertTrue(fallback_workspace.log_path.exists())

    async def test_failed_direct_delivery_is_not_persisted_as_completed(self):
        # Production mutation caught: an unconditional completion in finally
        # labels a run completed even when Discord never received the answer.
        catalog = self.catalog()
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="direct answer", reasoning_content="", reasoning="", tool_calls=[]
        ))])
        message = FakeMessage(
            "answer briefly", CHANNEL_A, author=FakeAuthor(TEST_USER_ID)
        )

        async def failed_reply(content):
            raise RuntimeError("delivery failed")

        message.reply = failed_reply
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(
                    bot,
                    "create_streaming_completion",
                    AsyncMock(return_value=response),
                ):
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                await bot.on_message(message)

        run_root = next((self.workspace_root / "runs").iterdir())
        metadata = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "failed")

    async def test_new_resume_delete_and_clear_have_owner_safe_text_semantics(self):
        # Production mutation caught: lifecycle commands that only clear memory,
        # leak cross-owner existence, or mutate selection before failed purge.
        catalog = self.catalog()
        retained = catalog.acquire(TEST_USER_ID, CHANNEL_A)
        await retained.write("notes.txt", "retained", None)
        catalog.finish(retained, "stopped")
        bot.channel_history[CHANNEL_A].append({"role": "user", "content": "old"})

        with patch.object(bot, "RUN_CATALOG", catalog):
            reset_message = FakeMessage("!reset", CHANNEL_A, author=FakeAuthor(TEST_USER_ID))
            await bot.on_message(reset_message)
            self.assertEqual(bot.channel_history[CHANNEL_A], [])
            reset_selected = catalog.acquire(TEST_USER_ID, CHANNEL_A)
            self.assertNotEqual(reset_selected.run_id, retained.run_id)
            self.assertEqual(list(reset_selected.root.iterdir()), [reset_selected.root / "run.json"])
            catalog.finish(reset_selected, "completed")

            bot.channel_history[CHANNEL_A].append({"role": "user", "content": "old again"})
            new_message = FakeMessage("!new", CHANNEL_A, author=FakeAuthor(TEST_USER_ID))
            await bot.on_message(new_message)
            self.assertEqual(bot.channel_history[CHANNEL_A], [])
            self.assertIn("run", new_message.replies[-1].lower())
            selected = catalog.acquire(TEST_USER_ID, CHANNEL_A)
            self.assertNotIn(selected.run_id, {retained.run_id, reset_selected.run_id})
            self.assertEqual((retained.root / "notes.txt").read_text(), "retained")
            catalog.finish(selected, "completed")

            cross_owner = FakeMessage(
                f"!resume {retained.run_id}", CHANNEL_A,
                author=FakeAuthor(TEST_ADMIN_ID, "admin"),
            )
            await bot.on_message(cross_owner)
            self.assertIn("not found", cross_owner.replies[-1].lower())

            resume = FakeMessage(
                f"!resume {retained.run_id}", CHANNEL_B,
                author=FakeAuthor(TEST_USER_ID),
            )
            await bot.on_message(resume)
            resumed = catalog.acquire(TEST_USER_ID, CHANNEL_B)
            self.assertEqual(resumed.run_id, retained.run_id)
            catalog.finish(resumed, "completed")

            failed_clear = FakeMessage(
                "!clear 5", CHANNEL_A,
                author=FakeAuthor(TEST_ADMIN_ID, "admin"),
                manage_messages=True,
                purge_error=RuntimeError("purge failed"),
            )
            before = {path.name for path in (self.workspace_root / "runs").iterdir()}
            bot.channel_history[CHANNEL_A].append({"role": "user", "content": "keep"})
            await bot.on_message(failed_clear)
            after = {path.name for path in (self.workspace_root / "runs").iterdir()}
            self.assertEqual(after, before)
            self.assertEqual(len(bot.channel_history[CHANNEL_A]), 1)

            successful_clear = FakeMessage(
                "!clear 5", CHANNEL_A,
                author=FakeAuthor(TEST_ADMIN_ID, "admin"),
                manage_messages=True,
            )
            with patch.object(bot.asyncio, "sleep", AsyncMock()):
                await bot.on_message(successful_clear)
            self.assertEqual(successful_clear.channel.purge_calls, 1)
            self.assertEqual(bot.channel_history[CHANNEL_A], [])
            cleared = catalog.acquire(TEST_ADMIN_ID, CHANNEL_A)
            self.assertNotIn(cleared.run_id, before)
            self.assertEqual(list(cleared.root.iterdir()), [cleared.root / "run.json"])
            catalog.finish(cleared, "completed")

            delete = FakeMessage(
                f"!delete {retained.run_id}", CHANNEL_B,
                author=FakeAuthor(TEST_USER_ID),
            )
            await bot.on_message(delete)
            self.assertFalse(retained.root.exists())

    async def test_clear_reserves_admission_across_text_and_slash_purge(self):
        # Production mutation caught: a goal admitted while clear awaits purge
        # makes reset fail after Discord messages were already destroyed.
        catalog = self.catalog()

        class Response:
            def __init__(self):
                self.messages = []

            async def send_message(self, content, **kwargs):
                self.messages.append(content)

            async def defer(self, **kwargs):
                return None

        class Followup:
            def __init__(self):
                self.messages = []

            async def send(self, content, **kwargs):
                self.messages.append(content)

        class Interaction:
            def __init__(self, channel_id, channel):
                self.user = FakeAuthor(TEST_ADMIN_ID, "admin")
                self.channel_id = channel_id
                self.channel = channel
                self.permissions = SimpleNamespace(manage_messages=True)
                self.response = Response()
                self.followup = Followup()

        async def exercise(channel_id, slash):
            purge_started = asyncio.Event()
            release_purge = asyncio.Event()
            model_started = asyncio.Event()

            async def paused_purge(limit=0):
                purge_started.set()
                await release_purge.wait()
                return [object()]

            async def blocked_model(**kwargs):
                model_started.set()
                await asyncio.Event().wait()

            if slash:
                channel = FakeMessage("ignored", channel_id).channel
                channel.purge = paused_purge
                interaction = Interaction(channel_id, channel)
                clear_call = bot.bot.tree.get_command("clear").callback(
                    interaction, 5
                )
            else:
                clear_message = FakeMessage(
                    "!clear 5",
                    channel_id,
                    author=FakeAuthor(TEST_ADMIN_ID, "admin"),
                    manage_messages=True,
                )
                clear_message.channel.purge = paused_purge
                clear_call = bot.on_message(clear_message)

            with patch.object(bot, "RUN_CATALOG", catalog), \
                    patch.object(bot, "create_streaming_completion", blocked_model):
                clear_task = asyncio.create_task(clear_call)
                await asyncio.wait_for(purge_started.wait(), timeout=1)
                goal = FakeMessage(
                    "answer briefly", channel_id, author=FakeAuthor(TEST_USER_ID)
                )
                goal_task = asyncio.create_task(bot.on_message(goal))
                model_wait = asyncio.create_task(model_started.wait())
                done, _ = await asyncio.wait(
                    (goal_task, model_wait),
                    timeout=1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self.assertTrue(done)
                release_purge.set()
                clear_error = None
                try:
                    if slash:
                        await clear_task
                    else:
                        with patch.object(bot.asyncio, "sleep", AsyncMock()):
                            await clear_task
                except Exception as error:
                    clear_error = error
                finally:
                    if not model_wait.done():
                        model_wait.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await model_wait
                    if goal_task.done():
                        await goal_task
                    else:
                        goal_task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await goal_task

            self.assertIsNone(clear_error)
            self.assertFalse(model_started.is_set())
            self.assertIn("clear", goal.replies[-1].lower())
            selected = catalog.acquire(TEST_ADMIN_ID, channel_id)
            self.assertEqual(list(selected.root.iterdir()), [selected.root / "run.json"])
            catalog.finish(selected, "completed")

        await exercise(CHANNEL_A, slash=False)
        await exercise(CHANNEL_B, slash=True)

    async def test_malformed_clear_count_cannot_strand_admission(self):
        # Production mutation caught: reserving before tolerant count parsing
        # lets a Unicode digit error block future goals until process restart.
        catalog = self.catalog()
        message = FakeMessage(
            "!clear ²",
            CHANNEL_A,
            author=FakeAuthor(TEST_ADMIN_ID, "admin"),
            manage_messages=True,
        )
        clear_error = None
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot.asyncio, "sleep", AsyncMock()):
            try:
                await bot.on_message(message)
            except ValueError as error:
                clear_error = error
            admission_error = None
            try:
                following = catalog.acquire(TEST_USER_ID, CHANNEL_A)
            except run_workspace.RunActiveError as error:
                admission_error = error
            else:
                catalog.finish(following, "completed")

        self.assertIsNone(clear_error)
        self.assertIsNone(admission_error)
        self.assertEqual(message.channel.purge_calls, 1)

    async def test_active_reset_rejects_and_stop_persists_stopped(self):
        # Production mutation caught: checking only steerable channel state lets
        # reset race a pending direct run, and cleanup that omits stop loses resume.
        catalog = self.catalog()
        started = asyncio.Event()

        async def blocked_model(**kwargs):
            started.set()
            await asyncio.Event().wait()

        run_message = FakeMessage(
            "investigate the system", CHANNEL_A, author=FakeAuthor(TEST_USER_ID)
        )
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot, "create_streaming_completion", blocked_model):
            task = asyncio.create_task(bot.on_message(run_message))
            await asyncio.wait_for(started.wait(), timeout=1)
            bot.channel_history[CHANNEL_A].append({"role": "user", "content": "keep"})
            reset = FakeMessage("!reset", CHANNEL_A, author=FakeAuthor(TEST_USER_ID))
            await bot.on_message(reset)
            self.assertIn("active", reset.replies[-1].lower())
            self.assertTrue(bot.channel_history[CHANNEL_A])
            stop = FakeMessage("!stop", CHANNEL_A, author=FakeAuthor(TEST_USER_ID))
            await bot.on_message(stop)
            await asyncio.wait_for(task, timeout=1)

        run_dirs = list((self.workspace_root / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        metadata = json.loads((run_dirs[0] / "run.json").read_text())
        self.assertEqual(metadata["status"], "stopped")
        self.assertTrue(run_dirs[0].exists())

    async def test_cleanup_persists_failed_exhausted_and_caller_interrupted(self):
        # Production mutation caught: treating every finally path as completed
        # destroys the distinction needed to resume failed or abandoned work.
        catalog = self.catalog()
        failed_message = FakeMessage(
            "answer briefly", CHANNEL_A, author=FakeAuthor(TEST_USER_ID)
        )
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(
                    bot,
                    "create_streaming_completion",
                    AsyncMock(side_effect=bot.StageTimeout("direct", 0.1)),
                ):
            await bot.on_message(failed_message)

        continuing = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="", reasoning_content="", reasoning="", tool_calls=[SimpleNamespace(
                id="call-exhaust",
                function=SimpleNamespace(
                    name="bash_exec", arguments='{"command":"true"}'
                ),
            )]
        ))])
        partial_report = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="partial report", reasoning_content="", reasoning="", tool_calls=[]
        ))])
        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot, "MAX_AGENT_LOOPS", 1), \
                patch.object(
                    bot,
                    "execute_tools_in_parallel",
                    AsyncMock(return_value=["[exit code: 0]"]),
                ), \
                patch.object(
                    bot,
                    "create_streaming_completion",
                    AsyncMock(side_effect=[continuing, partial_report]),
                ):
            await bot.on_message(FakeMessage(
                "investigate the system", CHANNEL_A, author=FakeAuthor(TEST_USER_ID)
            ))

        started = asyncio.Event()

        async def blocked_model(**kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(bot, "RUN_CATALOG", catalog), \
                patch.object(bot, "create_streaming_completion", blocked_model):
            task = asyncio.create_task(bot.on_message(FakeMessage(
                "answer briefly", CHANNEL_B, author=FakeAuthor(TEST_USER_ID)
            )))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        metadata = [
            json.loads((run_root / "run.json").read_text(encoding="utf-8"))
            for run_root in (self.workspace_root / "runs").iterdir()
        ]
        self.assertEqual(
            sorted(item["status"] for item in metadata),
            ["exhausted", "failed", "interrupted"],
        )
        self.assertTrue(all(
            (self.log_root / "runs" / f"{item['run_id']}.md").exists()
            for item in metadata
        ))

    async def test_slash_new_resume_delete_are_registered_and_use_same_catalog(self):
        # Production mutation caught: text-only handlers leave slash lifecycle
        # commands absent or wired to divergent authorization/storage behavior.
        catalog = self.catalog()
        commands = {command.name for command in bot.bot.tree.get_commands()}
        self.assertTrue({"new", "resume", "delete"}.issubset(commands))

        class Response:
            def __init__(self):
                self.messages = []

            async def send_message(self, content, **kwargs):
                self.messages.append(content)

        class Interaction:
            def __init__(self, user_id, channel_id):
                self.user = FakeAuthor(user_id)
                self.channel_id = channel_id
                self.channel = SimpleNamespace()
                self.response = Response()

        with patch.object(bot, "RUN_CATALOG", catalog):
            new_interaction = Interaction(TEST_USER_ID, CHANNEL_A)
            await bot.bot.tree.get_command("new").callback(new_interaction)
            selected = catalog.acquire(TEST_USER_ID, CHANNEL_A)
            catalog.finish(selected, "completed")

            resume_interaction = Interaction(TEST_USER_ID, CHANNEL_B)
            await bot.bot.tree.get_command("resume").callback(
                resume_interaction, selected.run_id
            )
            resumed = catalog.acquire(TEST_USER_ID, CHANNEL_B)
            catalog.finish(resumed, "stopped")

            delete_interaction = Interaction(TEST_USER_ID, CHANNEL_B)
            await bot.bot.tree.get_command("delete").callback(
                delete_interaction, resumed.run_id
            )
            self.assertFalse(resumed.root.exists())


if __name__ == "__main__":
    unittest.main()
