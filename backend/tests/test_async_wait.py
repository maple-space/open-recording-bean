"""Cancellation must survive completion races on every supported Python."""
import asyncio

import pytest
import pytest_asyncio

from backend.async_wait import wait_with_timeout


@pytest_asyncio.fixture(autouse=True)
async def no_leaked_tasks():
    before = asyncio.all_tasks()
    yield
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - before - {asyncio.current_task()})


@pytest.mark.parametrize('timeout', [None, 1])
async def test_wait_returns_result_and_propagates_inner_error(timeout):
    async def result():
        return 42

    async def error():
        raise LookupError('inner failure')

    assert await wait_with_timeout(result(), timeout) == 42
    with pytest.raises(LookupError, match='inner failure'):
        await wait_with_timeout(error(), timeout)


@pytest.mark.parametrize('timeout', [0, -1])
async def test_nonpositive_timeout_does_not_start_coroutine(timeout):
    started = []

    async def child():
        started.append(True)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await wait_with_timeout(child(), timeout)
    assert not started
    ready = asyncio.get_running_loop().create_future()
    ready.set_result(42)
    assert await wait_with_timeout(ready, timeout) == 42


@pytest.mark.parametrize('timeout', [None, 0, -1, 1])
async def test_already_done_future_does_not_hide_pending_parent_cancel(timeout):
    continued = []

    async def parent():
        ready = asyncio.get_running_loop().create_future()
        ready.set_result(42)
        asyncio.current_task().cancel()
        continued.append(await wait_with_timeout(ready, timeout))

    task = asyncio.create_task(parent())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert continued == []


@pytest.mark.parametrize('timeout', [None, 1])
@pytest.mark.parametrize('outcome', ['result', 'error'])
async def test_cancel_at_child_completion_wins_over_result_or_error(timeout, outcome):
    owner = asyncio.current_task()
    continued = []

    async def child():
        owner.cancel()
        if outcome == 'error':
            raise LookupError('late inner failure')
        return 42

    with pytest.raises(asyncio.CancelledError):
        continued.append(await wait_with_timeout(child(), timeout))
    assert continued == []


async def test_timeout_waits_for_child_cleanup():
    cleaned = []

    async def child():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.001)
            cleaned.append(True)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await wait_with_timeout(child(), .001)
    assert cleaned == [True]


@pytest.mark.parametrize('outcome', ['result', 'error'])
async def test_timeout_is_not_turned_into_success_or_inner_error_by_cleanup(outcome):
    async def child():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if outcome == 'error':
                raise LookupError('late cleanup failure')
            return 42

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await wait_with_timeout(child(), .001)


@pytest.mark.parametrize('timeout', [None, 1])
@pytest.mark.parametrize('outcome', ['result', 'error'])
async def test_child_suppressing_cancel_cannot_suppress_parent_cancel(timeout, outcome):
    entered = asyncio.Event()
    children = []

    async def child():
        children.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if outcome == 'error':
                raise LookupError('cleanup failure')
            return 42

    task = asyncio.create_task(wait_with_timeout(child(), timeout))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].done()


@pytest.mark.parametrize('initial_stop', ['timeout', 'cancel'])
async def test_repeated_parent_cancel_drains_child_cleanup(initial_stop):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []
    children = []

    async def child():
        children.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.append(True)

    task = asyncio.create_task(wait_with_timeout(child(), .005 if initial_stop == 'timeout' else None))
    try:
        await entered.wait()
        if initial_stop == 'cancel':
            task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]
        assert children[0].done()
    finally:
        release.set()
        await asyncio.gather(task, *children, return_exceptions=True)


async def test_child_cancellation_is_propagated():
    async def child():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await wait_with_timeout(child(), 1)
