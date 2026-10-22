"""Lifecycle helpers for Slack actions acknowledged before work completes."""

import asyncio
from typing import Any


_tasks: set[asyncio.Task[Any]] = set()


def _finalize(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        print(
            "SLACK_ACTION_TASK_FAILED "
            f"error_type={error.__class__.__name__}"
        )


def start(coro: Any) -> asyncio.Task[Any]:
    """Keep a strong reference to an action task until it finishes."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_finalize)
    return task


async def drain(*, timeout_seconds: float = 35.0) -> None:
    """Give acknowledged actions a bounded graceful-shutdown window."""
    pending = [task for task in _tasks if not task.done()]
    if not pending:
        return
    _, unfinished = await asyncio.wait(
        pending,
        timeout=max(0.0, float(timeout_seconds)),
    )
    for task in unfinished:
        task.cancel()
    if unfinished:
        await asyncio.gather(*unfinished, return_exceptions=True)
