"""In-process session event bus for websocket control messages.

This module lets async ADK tools publish UI events (video_ready, music_command,
theater_mode, etc.) that are forwarded to the correct websocket session.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Any

_EVENT_QUEUES: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_IOT_CONFIGS: dict[str, dict[str, str]] = {}
_DEFAULT_QUEUE_SIZE = 200
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def get_session_queue(session_id: str) -> asyncio.Queue[dict[str, Any]]:
    """Returns the per-session queue, creating it if needed."""
    if session_id not in _EVENT_QUEUES:
        _EVENT_QUEUES[session_id] = asyncio.Queue(maxsize=_DEFAULT_QUEUE_SIZE)
    return _EVENT_QUEUES[session_id]


def replace_session_queue(session_id: str) -> asyncio.Queue[dict[str, Any]]:
    """Creates a fresh queue for a reconnecting session and preserves buffered events."""
    new_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_DEFAULT_QUEUE_SIZE)
    old_queue = _EVENT_QUEUES.get(session_id)
    if old_queue is not None:
        while not old_queue.empty() and not new_queue.full():
            try:
                new_queue.put_nowait(old_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    _EVENT_QUEUES[session_id] = new_queue
    return new_queue


def publish_session_event(session_id: str, event: Mapping[str, Any]) -> None:
    """Best-effort, non-blocking enqueue of an event for a websocket session."""
    import logging
    logger = logging.getLogger(__name__)
    queue = get_session_queue(session_id)
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(dict(event))
    logger.debug("Published event %s to queue for session %s. Queue size is now %d", event.get("type"), session_id, queue.qsize())


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Registers the main server event loop for cross-thread scheduling."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def schedule_background_task(coro: Awaitable[Any]) -> None:
    """Schedules a coroutine on the main loop when available, else on the current loop."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    loop = _MAIN_LOOP if (_MAIN_LOOP and _MAIN_LOOP.is_running()) else running_loop
    if loop is None:
        logger.warning("No running event loop available to schedule background task.")
        return

    if loop is running_loop:
        task = loop.create_task(coro)
    else:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        task = future

    def _log_error(fut: Any) -> None:
        try:
            exc = fut.exception()
        except Exception:
            exc = None
        if exc:
            logger.warning("Background task failed: %s", exc, exc_info=True)

    try:
        task.add_done_callback(_log_error)  # type: ignore[attr-defined]
    except Exception:
        # Best-effort; run_coroutine_threadsafe returns a concurrent Future.
        pass


def clear_session(session_id: str) -> None:
    """Clears ephemeral queue state for a disconnected session."""
    _EVENT_QUEUES.pop(session_id, None)


def set_session_iot_config(session_id: str, config: Mapping[str, str]) -> None:
    """Stores per-session IoT overrides from ParentGate settings."""
    _IOT_CONFIGS[session_id] = {
        "ha_url": str(config.get("ha_url", "")).strip(),
        "ha_token": str(config.get("ha_token", "")).strip(),
        "ha_entity": str(config.get("ha_entity", "light.living_room")).strip(),
    }


def get_session_iot_config(session_id: str) -> dict[str, str]:
    """Returns per-session IoT config; empty dict when unset."""
    return dict(_IOT_CONFIGS.get(session_id, {}))
