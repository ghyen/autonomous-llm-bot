import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage
import bot
import httpx
from openai import APIStatusError
from deadlines import CancelToken, RunCancelled, StageTimeout


class CompletionRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_failure_stops_after_three_attempts(self):
        complete = AsyncMock(side_effect=httpx.ConnectError("offline"))
        with patch.object(bot, "create_streaming_completion", complete), \
                patch.object(bot.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(httpx.ConnectError):
                await bot.run_completion_stage()
        self.assertEqual(complete.await_count, 3)

    async def test_transient_failure_recovers_but_bad_request_does_not_retry(self):
        for status, expected in ((503, 2), (429, 2), (400, 1)):
            response = httpx.Response(status, request=httpx.Request("POST", "http://localhost/v1"))
            error = APIStatusError("upstream", response=response, body=None)
            complete = AsyncMock(side_effect=[error, "ok"])
            with patch.object(bot, "create_streaming_completion", complete), \
                    patch.object(bot.asyncio, "sleep", AsyncMock()):
                if status == 400:
                    with self.assertRaises(APIStatusError):
                        await bot.run_completion_stage()
                else:
                    self.assertEqual(await bot.run_completion_stage(), "ok")
            self.assertEqual(complete.await_count, expected)

    async def test_deadline_and_cancel_cover_backoff(self):
        with patch.object(bot, "create_streaming_completion", AsyncMock(side_effect=httpx.ReadTimeout("offline"))) as complete:
            with self.assertRaises(StageTimeout):
                await bot.run_completion_stage(deadline=time.monotonic() + 0.05)
            self.assertEqual(complete.await_count, 1)
            token = CancelToken()
            task = asyncio.create_task(bot.run_completion_stage(token=token))
            await asyncio.sleep(0.02)
            token.cancel("stop")
            with self.assertRaises(RunCancelled):
                await asyncio.wait_for(task, 1)
