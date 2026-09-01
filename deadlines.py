"""Per-stage deadlines and one cancellation token per run.

No stage - model call, tool batch, checkpoint, rollover, final synthesis - had
an application-level deadline or a cancellation point. The OpenAI client was
built with `timeout=None`, the streaming loop consumed `async for` with no idle
limit, tool execution sat behind a bare `asyncio.gather` barrier, and the 400
recovery retry was unbounded too. So one wedged stage held the whole run, and
`!stop` only set a flag that was polled at the top of the loop: the interrupt
could not land until the stage it was waiting on finished on its own.

Two primitives fix that shape:

* `CancelToken` - one per run. `!stop` cancels it, and every stage races against
  it, so an interrupt lands while a stage is still in flight instead of after.
* `with_deadline` - runs an awaitable against a total budget and a token. On
  timeout or cancellation it cancels the work and *awaits its cleanup* before
  raising, so no task is left running behind the run that abandoned it.

Timeout and cancellation raise different exceptions on purpose. A deadline
overrun is an upstream problem, a cancellation is a user decision, and the run's
terminal reason has to tell them apart.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

DEFAULT_STAGE = "stage"


class RunCancelled(Exception):
    """The run's cancel token fired while this stage was in flight."""

    def __init__(self, reason: str = ""):
        self.reason = reason or "취소됨"
        super().__init__(self.reason)


class StageTimeout(Exception):
    """A stage exceeded its configured budget."""

    def __init__(self, stage: str, seconds: float):
        self.stage = stage
        self.seconds = seconds
        super().__init__("{0} 단계가 마감 {1}초를 초과했습니다.".format(stage, seconds))


@dataclass(frozen=True)
class StageBudget:
    """Separate budgets: total wall clock, and max gap between stream chunks."""

    name: str
    total: float
    idle: Optional[float] = None


class CancelToken:
    """Cooperative cancellation with an awaitable edge.

    The event is created lazily so a token can be constructed outside a running
    loop without binding to the wrong one.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._event: Optional[asyncio.Event] = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "") -> bool:
        """Cancel the run. Only the first call takes effect."""
        if self._cancelled:
            return False
        self._cancelled = True
        self._reason = reason or "취소됨"
        if self._event is not None:
            self._event.set()
        return True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise RunCancelled(self._reason)

    async def wait(self) -> None:
        if self._event is None:
            self._event = asyncio.Event()
            if self._cancelled:
                self._event.set()
        await self._event.wait()


def _discard(awaitable) -> None:
    """Close a coroutine we decided not to start, so it does not warn."""
    close = getattr(awaitable, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


async def _reap(*tasks) -> None:
    async def cleanup():
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in tasks if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # Cleanup is work this layer owns. Caller cancellation may arrive after the
    # stage race is decided, including while a child is in an async finalizer;
    # retain and re-await the cleanup task until it finishes before surfacing it.
    cleanup_task = asyncio.create_task(cleanup())
    cancellation = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as cancelled:
            if cancellation is None:
                cancellation = cancelled
    await cleanup_task
    if cancellation is not None:
        raise cancellation


async def with_deadline(awaitable, seconds: Optional[float], token: Optional[CancelToken] = None,
                        stage: str = DEFAULT_STAGE):
    """Await `awaitable` under a total budget and a cancel token.

    Raises RunCancelled or StageTimeout, having already cancelled and reaped the
    work. A stage is never started once the token is already cancelled.
    """
    if token is not None and token.cancelled:
        if asyncio.isfuture(awaitable):
            await _reap(awaitable)
        else:
            _discard(awaitable)
        raise RunCancelled(token.reason)

    if seconds is not None and seconds <= 0:
        if asyncio.isfuture(awaitable):
            await _reap(awaitable)
        else:
            _discard(awaitable)
        raise StageTimeout(stage, seconds)

    task = asyncio.ensure_future(awaitable)
    cancel_waiter = asyncio.ensure_future(token.wait()) if token is not None else None
    waiters = {task} if cancel_waiter is None else {task, cancel_waiter}

    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # Caller cancellation is not a stage result. Reap our children, then
        # preserve the caller's original exception unchanged.
        await _reap(task, cancel_waiter)
        raise

    # Cancellation wins a tie with completed work: a response that arrives in
    # the same loop turn must not start another stage after the stop request.
    if token is not None and token.cancelled:
        await _reap(task, cancel_waiter)
        raise RunCancelled(token.reason)

    if task.done():
        await _reap(cancel_waiter)
        if token is not None:
            token.raise_if_cancelled()
        return task.result()

    # Cancel first, then wait for the cleanup to finish, then report why.
    await _reap(task, cancel_waiter)
    raise StageTimeout(stage, seconds)


async def stream_chunks(stream, budget: StageBudget, token: Optional[CancelToken] = None):
    """Yield stream chunks under an idle deadline, checking the token each turn.

    `async for` gives no way to bound the gap between chunks, which is how a
    silently stalled upstream held a run open for over an hour.
    """
    iterator = stream.__aiter__()
    try:
        while True:
            if token is not None:
                token.raise_if_cancelled()
            step = iterator.__anext__()
            try:
                if budget.idle:
                    chunk = await with_deadline(step, budget.idle, token, budget.name + ":idle")
                else:
                    chunk = await step
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
