import asyncio
import time
import unittest

from deadlines import (
    CancelToken,
    RunCancelled,
    StageBudget,
    StageTimeout,
    stream_chunks,
    with_deadline,
)

TICK = 0.05


class CancelTokenTest(unittest.IsolatedAsyncioTestCase):
    async def test_starts_uncancelled(self):
        token = CancelToken()
        self.assertFalse(token.cancelled)
        token.raise_if_cancelled()

    async def test_first_cancel_wins(self):
        token = CancelToken()
        self.assertTrue(token.cancel("사용자 중단 요청"))
        self.assertFalse(token.cancel("다른 이유"))
        self.assertEqual(token.reason, "사용자 중단 요청")

    async def test_raise_if_cancelled(self):
        token = CancelToken()
        token.cancel("중단")
        with self.assertRaises(RunCancelled) as caught:
            token.raise_if_cancelled()
        self.assertEqual(caught.exception.reason, "중단")

    async def test_wait_returns_when_cancelled_before_waiting(self):
        token = CancelToken()
        token.cancel("먼저 취소")
        await asyncio.wait_for(token.wait(), timeout=1)

    async def test_wait_returns_when_cancelled_while_waiting(self):
        token = CancelToken()

        async def cancel_soon():
            await asyncio.sleep(TICK)
            token.cancel("나중 취소")

        asyncio.ensure_future(cancel_soon())
        await asyncio.wait_for(token.wait(), timeout=1)
        self.assertTrue(token.cancelled)


class WithDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_result_when_it_finishes_in_time(self):
        async def work():
            await asyncio.sleep(TICK)
            return "done"

        self.assertEqual(await with_deadline(work(), 10 * TICK), "done")

    async def test_propagates_the_inner_exception(self):
        async def work():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            await with_deadline(work(), 10 * TICK)

    async def test_timeout_raises_and_reaps_the_task(self):
        started = asyncio.Event()
        cleaned = []

        async def wedged():
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cleaned.append("cancelled")
                raise

        with self.assertRaises(StageTimeout) as caught:
            await with_deadline(wedged(), TICK, stage="agent")

        self.assertTrue(started.is_set())
        self.assertEqual(caught.exception.stage, "agent")
        # Cleanup already ran before the exception surfaced.
        self.assertEqual(cleaned, ["cancelled"])

    async def test_cancellation_wins_over_a_long_budget(self):
        token = CancelToken()

        async def wedged():
            await asyncio.sleep(60)

        async def cancel_soon():
            await asyncio.sleep(TICK)
            token.cancel("사용자 중단 요청")

        asyncio.ensure_future(cancel_soon())
        started = time.monotonic()
        with self.assertRaises(RunCancelled) as caught:
            await with_deadline(wedged(), 60, token, stage="agent")
        elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.reason, "사용자 중단 요청")
        self.assertLess(elapsed, 2.0)

    async def test_a_stage_never_starts_after_cancellation(self):
        token = CancelToken()
        token.cancel("이미 취소")
        started = []

        async def work():
            started.append(True)
            return "should not run"

        with self.assertRaises(RunCancelled):
            await with_deadline(work(), 10 * TICK, token, stage="checkpoint")

        self.assertEqual(started, [])

    # Mutation caught: treating a pre-created Future like an unopened coroutine
    # leaves its child running and its terminal exception unobserved.
    async def test_pre_cancelled_token_reaps_an_already_created_gather(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def wedged():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cleaned.set()

        child = asyncio.ensure_future(wedged())
        await started.wait()
        batch = asyncio.gather(child)
        token = CancelToken()
        token.cancel("이미 취소")

        try:
            with self.assertRaises(RunCancelled):
                await with_deadline(batch, 10 * TICK, token, stage="tool")

            self.assertEqual(
                (batch.done(), child.done(), cleaned.is_set()),
                (True, True, True),
            )
        finally:
            batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)

    # Mutation caught: omitting cleanup when this wrapper is itself cancelled
    # strands both the stage work and the token waiter behind its caller.
    async def test_outer_cancellation_reaps_work_and_cancel_waiter(self):
        work_started = asyncio.Event()
        work_cleaned = asyncio.Event()
        waiter_started = asyncio.Event()
        waiter_cleaned = asyncio.Event()

        class ObservedToken(CancelToken):
            async def wait(self):
                waiter_started.set()
                try:
                    await super().wait()
                finally:
                    waiter_cleaned.set()

        async def wedged():
            work_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                work_cleaned.set()

        token = ObservedToken()
        work = asyncio.ensure_future(wedged())
        outer = asyncio.ensure_future(with_deadline(work, 60, token, stage="agent"))
        await asyncio.gather(work_started.wait(), waiter_started.wait())

        try:
            outer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await outer

            self.assertEqual(
                (work_cleaned.is_set(), waiter_cleaned.is_set()),
                (True, True),
            )
        finally:
            token.cancel("테스트 정리")
            work.cancel()
            await asyncio.gather(work, outer, return_exceptions=True)
            await asyncio.wait_for(waiter_cleaned.wait(), timeout=1)

    async def test_child_tasks_are_cancelled_through_gather(self):
        cancelled = []

        async def child(index):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(index)
                raise

        async def batch():
            await asyncio.gather(*(child(i) for i in range(3)))

        with self.assertRaises(StageTimeout):
            await with_deadline(batch(), TICK, stage="tool")

        self.assertEqual(sorted(cancelled), [0, 1, 2])


class FakeStream:
    def __init__(self, chunks, gap=0.0, stall_after=None):
        self.chunks = list(chunks)
        self.gap = gap
        self.stall_after = stall_after
        self.closed = False
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.stall_after is not None and self._index == self.stall_after:
            await asyncio.sleep(60)
        if self._index >= len(self.chunks):
            raise StopAsyncIteration
        if self.gap:
            await asyncio.sleep(self.gap)
        chunk = self.chunks[self._index]
        self._index += 1
        return chunk

    async def close(self):
        self.closed = True


class StreamChunksTest(unittest.IsolatedAsyncioTestCase):
    async def test_yields_every_chunk_and_closes_the_stream(self):
        stream = FakeStream(["a", "b", "c"])
        budget = StageBudget("agent", total=10, idle=10 * TICK)

        collected = [chunk async for chunk in stream_chunks(stream, budget)]

        self.assertEqual(collected, ["a", "b", "c"])
        self.assertTrue(stream.closed)

    async def test_idle_gap_beyond_the_budget_times_out(self):
        stream = FakeStream(["a", "b"], stall_after=1)
        budget = StageBudget("agent", total=60, idle=TICK)

        collected = []
        with self.assertRaises(StageTimeout):
            async for chunk in stream_chunks(stream, budget):
                collected.append(chunk)

        self.assertEqual(collected, ["a"])
        self.assertTrue(stream.closed)

    async def test_cancellation_stops_consumption_promptly(self):
        stream = FakeStream(["a"] * 1000, gap=TICK)
        budget = StageBudget("agent", total=60, idle=10 * TICK)
        token = CancelToken()

        async def cancel_soon():
            await asyncio.sleep(3 * TICK)
            token.cancel("사용자 중단 요청")

        asyncio.ensure_future(cancel_soon())
        started = time.monotonic()
        with self.assertRaises(RunCancelled):
            async for _ in stream_chunks(stream, budget, token):
                pass
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
