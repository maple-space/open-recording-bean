"""Owned asynchronous waits with consistent cancellation on Python 3.10+."""
import asyncio


async def wait_with_timeout(awaitable, timeout):
    """Wait for one owned operation, then cancel and drain it on timeout/stop.

    Unlike Python 3.10's wait_for, simultaneous completion cannot erase a parent
    cancellation. Once a deadline expires it stays a timeout, even if the child
    suppresses cancellation. Cleanup may exceed the deadline and repeated parent
    cancellation still waits for the child to finish before propagating the stop.
    A None timeout has no deadline; nonpositive timeouts never start a coroutine.
    """
    future = asyncio.ensure_future(awaitable)
    stopping = False
    try:
        if timeout is None or timeout > 0 or future.done():
            # Always suspend, even for an already-done future, so pending parent
            # cancellation is delivered before any result can escape.
            done, _ = await asyncio.wait((future,), timeout=timeout)
            if done:
                return future.result()
        stopping = True
        future.cancel()
        await asyncio.wait((future,))
    except asyncio.CancelledError:
        if not stopping:
            future.cancel()
        # asyncio.wait does not forward a second cancel into the child's cleanup.
        # Consume late exceptions too: external cancellation must remain the result.
        while not future.done():
            try:
                await asyncio.wait((future,))
            except asyncio.CancelledError:
                pass
        if not future.cancelled():
            future.exception()
        raise
    if not future.cancelled():
        future.exception()
    raise asyncio.TimeoutError()
