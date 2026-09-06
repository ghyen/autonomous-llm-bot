import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage, TEST_USER_ID
from run_workspace import RunCatalog
from ledger import ResearchLedger
import bot
import run_state


class StartupResumeTest(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_stopped_and_uncertain_runs_do_not_resume(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = RunCatalog(Path(root) / "work", Path(root) / "logs")
            workspace = catalog.prepare(TEST_USER_ID, 987654980)
            for allowed, state, pending in ((False, "running", []),
                                             (True, "stopped", []),
                                             (True, "running", [{"id": "uncertain"}])):
                record = {"state": state, "pending_tools": pending}
                with self.subTest(allowed=allowed, state=state, pending=pending), \
                        patch.object(bot, "_startup_resumes", {workspace.run_id: workspace}), \
                        patch.object(bot, "authorize_caller", return_value=allowed), \
                        patch.object(bot.run_state, "load", return_value=record), \
                        patch.object(bot.bot, "get_channel") as fetch, \
                        patch.object(bot, "on_message", AsyncMock()) as handle:
                    await bot.resume_startup_runs()
                    fetch.assert_not_called()
                    handle.assert_not_awaited()

    async def test_ready_resumes_once_without_a_new_user_message(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = RunCatalog(Path(root) / "work", Path(root) / "logs")
            message = FakeMessage("continue investigation", 987654980)
            workspace = catalog.acquire(TEST_USER_ID, message.channel.id)
            run_state.save(workspace, message.id, 17, "prior facts", [], ResearchLedger(), {}, [])
            catalog.finish(workspace, "interrupted")
            channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
            with patch.object(bot, "RUN_CATALOG", catalog), \
                    patch.object(bot, "_startup_resumes", {}), \
                    patch.object(bot.bot, "get_channel", return_value=channel), \
                    patch.object(bot, "on_message", AsyncMock()) as handle:
                bot.recover_interrupted_runs()
                await bot.resume_startup_runs()
                await bot.resume_startup_runs()
                handle.assert_awaited_once_with(message)

    async def test_new_selection_during_fetch_prevents_stale_resume(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = RunCatalog(Path(root) / "work", Path(root) / "logs")
            message = FakeMessage("old investigation", 987654980)
            workspace = catalog.acquire(TEST_USER_ID, message.channel.id)
            run_state.save(workspace, message.id, 17, "facts", [], ResearchLedger(), {}, [])
            catalog.finish(workspace, "interrupted")

            async def fetch(_):
                catalog.prepare(TEST_USER_ID, message.channel.id)
                return message

            channel = SimpleNamespace(fetch_message=fetch)
            with patch.object(bot, "RUN_CATALOG", catalog), \
                    patch.object(bot, "_startup_resumes", {}), \
                    patch.object(bot.bot, "get_channel", return_value=channel), \
                    patch.object(bot, "on_message", AsyncMock()) as handle:
                bot.recover_interrupted_runs()
                await bot.resume_startup_runs()
                handle.assert_not_awaited()
