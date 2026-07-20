"""Folder watcher that broadcasts file changes over WebSocket.

Owns the "which folder am I watching right now" state for the app. A
single long-lived task is started on demand by :func:`set_active_folder`
and swapped out when the user picks a different folder. Connected
WebSocket clients receive every change batch as a single broadcast.

Why a separate module from ``server``:
    The broker holds long-lived async state (a task handle, a list of
    WebSocket send-queues). Pulling it out keeps ``server.py`` focused
    on request handlers and makes the broker testable on its own.

Why a custom broker instead of FastAPI's ``ConnectionManager``:
    FastAPI's docs build a small per-class helper. The shape we need —
    one folder, many clients, single fan-out — doesn't justify a class.
    Two module-level functions and a list of queues is the smallest
    thing that does the job and is trivially testable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from watchfiles import awatch

from randonneur.log import get_logger

_log = get_logger("watcher")

# Default debounce. watchfiles uses 1600 ms which is on the long side
# for a "save and reload" workflow but is the right ballpark for a
# full file manager rename storm or a bulk-import. Editor "save" events
# arrive as a single batch under this value.
_DEBOUNCE_MS = 500

# Each connected WebSocket has a bounded queue. If a slow client falls
# behind, the broker drops messages for that client rather than
# back-pressuring the watcher. ``MAX_QUEUE`` is a safety valve: the
# normal case never comes close to filling one.
_MAX_QUEUE = 64

# Module-level state. One process, one folder, many clients. The
# ``_STATE`` dict is keyed on the FastAPI app instance so multiple
# apps (in the same process — only happens in tests) don't share
# watcher state.
_STATE: dict[int, "WatcherState"] = {}


class WatcherState:
    """Per-app broker: the active folder, the watcher task, the client queues.

    The single ``_task`` is started in the app's lifespan and lives for
    the lifetime of the app. It watches whatever folder is currently
    in ``active_folder``; the producer (``set_active_folder``) just
    updates that field and signals the task to re-target via an
    asyncio Event. This shape survives the TestClient's per-request
    portal lifecycle, where a task created inside a request handler
    gets cancelled when the handler returns.
    """

    def __init__(self) -> None:
        self.active_folder: Path | None = None
        # List of (websocket, queue). Held in registration order so
        # the order of fan-out is predictable in tests.
        self.clients: list[tuple[WebSocket, asyncio.Queue[dict[str, Any] | None]]] = []
        self._task: asyncio.Task[None] | None = None
        # Set to True on shutdown; the watcher task checks it between
        # batches to bail out cleanly.
        self._stopping: bool = False
        # Signalled by set_active_folder when the target changes. The
        # watcher task awaits this between batches and restarts its
        # inner awatch loop pointed at the new folder.
        self._change_event: asyncio.Event = asyncio.Event()


def state_for(app: object) -> WatcherState:
    """Return the per-app :class:`WatcherState`, creating it on first use."""
    key = id(app)
    st = _STATE.get(key)
    if st is None:
        st = WatcherState()
        _STATE[key] = st
    return st


def reset_for_tests() -> None:
    """Drop all per-app state. Test-only — never call in production."""
    _STATE.clear()


# ─── Active folder management ───────────────────────────────────────────────


async def set_active_folder(app: object, folder: Path) -> None:
    """Point the watcher at ``folder``.

    Called by the ``/api/folder`` handler whenever a folder is loaded.
    If the new folder matches the current one, this is a no-op (so a
    client re-fetching the same folder doesn't restart the watcher).
    Otherwise ``active_folder`` is updated and the watcher task is
    signalled to re-target on its next iteration.

    The folder is *not* validated here — the caller has already loaded
    it via ``gpx_loader.discover`` so a missing folder has been turned
    into a 404 before we get here.
    """
    st = state_for(app)
    if st.active_folder is not None and st.active_folder == folder:
        return
    st.active_folder = folder
    st._change_event.set()


async def start(app: object) -> None:
    """Start the long-lived watcher task. Idempotent.

    Called from the FastAPI lifespan handler on app startup. The task
    runs until ``shutdown()`` is called.
    """
    st = state_for(app)
    if st._task is not None and not st._task.done():
        return
    st._stopping = False
    st._task = asyncio.create_task(_run_watcher(st), name="randonneur.watcher")


async def shutdown(app: object) -> None:
    """Stop the watcher and forget the active folder. Idempotent."""
    st = state_for(app)
    st._stopping = True
    st._change_event.set()  # unblock the task so it sees _stopping
    task = st._task
    st._task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    st.active_folder = None
    # Tell any connected clients we're going away. Each queue gets one
    # ``None`` sentinel; the WS handler interprets that as "close".
    for _ws, q in st.clients:
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            pass


# ─── WebSocket subscription ─────────────────────────────────────────────────


@asynccontextmanager
async def subscribe(
    app: object, websocket: WebSocket
) -> AsyncIterator[asyncio.Queue[dict[str, Any] | None]]:
    """Register a client for broadcasts. Yields its send-queue.

    Usage from a FastAPI handler::

        async with subscribe(app, ws) as q:
            while True:
                msg = await q.get()
                if msg is None:
                    break
                await ws.send_json(msg)

    The client is unregistered on exit (normal return, exception, or
    task cancellation). A ``None`` sentinel signals "server is
    shutting down — close the connection".
    """
    st = state_for(app)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_MAX_QUEUE)
    st.clients.append((websocket, queue))
    try:
        yield queue
    finally:
        try:
            st.clients.remove((websocket, queue))
        except ValueError:
            pass


# ─── The watcher task ────────────────────────────────────────────────────────


async def _run_watcher(st: WatcherState) -> None:
    """Long-lived task: watch the active folder, broadcast every change batch.

    The task lives for the lifetime of the app. Its inner loop points
    at whatever folder is in ``st.active_folder`` and re-targets when
    ``set_active_folder`` signals a change. It exits only when
    ``st._stopping`` is set.

    All exceptions are caught and logged; a single failed awatch does
    not kill the task — the loop re-targets (and recovers) on the
    next change event.
    """
    while not st._stopping:
        folder = st.active_folder
        if folder is None:
            # No active folder yet. Wait for one to be set, but stay
            # responsive to shutdown.
            try:
                await asyncio.wait_for(st._change_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            st._change_event.clear()
            continue
        # Have a folder. Watch it until either: (a) it changes,
        # (b) shutdown is requested.
        change_task = asyncio.create_task(st._change_event.wait(), name="change_wait")
        stop_task = asyncio.create_task(_stop_after(st), name="stop_wait")
        try:
            await _watch_one_folder(st, folder, change_task, stop_task)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _log.warning("watcher for %s exited: %s: %s", folder, type(e).__name__, e)
        finally:
            change_task.cancel()
            stop_task.cancel()
            for t in (change_task, stop_task):
                try:
                    await t
                except (asyncio.CancelledError, BaseException):  # noqa: BLE001
                    pass
            # If the event was set, consume it so the next loop iter
            # doesn't immediately re-enter.
            st._change_event.clear()


async def _stop_after(st: WatcherState) -> None:
    """Sleep forever until _stopping flips. Used as a cancellable wait."""
    while not st._stopping:
        await asyncio.sleep(0.1)


async def _watch_one_folder(
    st: WatcherState, folder: Path, change_task: asyncio.Task, stop_task: asyncio.Task
) -> None:
    """Watch ``folder`` until either ``change_task`` or ``stop_task`` fires.

    Every batch from ``awatch`` is broadcast. The first batch on a
    fresh watch is the directory's current state (an "attach
    snapshot" from watchfiles' perspective); the client treats it as
    a normal "changed" event and re-fetches ``/api/folder``, which is
    a no-op refresh if nothing actually changed.

    We *don't* try to drop the first batch, even though the client
    has just loaded the folder from ``/api/folder`` — the timing
    race between "the watch attached" and "the user wrote a file"
    is the very thing that means the first batch can contain a real
    change mixed in with the snapshot. Always broadcasting is
    correct; sometimes-optimally-skipping is flaky.

    Errors propagate to the caller for logging; the outer loop
    handles them.
    """
    aiter = awatch(folder, debounce=_DEBOUNCE_MS, recursive=True)
    try:
        while True:
            # Race the next batch against the change/stop signals.
            # If neither signal fires, take the batch and broadcast.
            next_batch_task = asyncio.create_task(aiter.__anext__())
            change_signal_task = asyncio.create_task(_event_wait(st))
            stop_signal_task = stop_task
            done, _ = await asyncio.wait(
                {next_batch_task, change_signal_task, stop_signal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel whatever didn't fire so the next iteration starts clean.
            for t in (next_batch_task, change_signal_task):
                if t is not None and not t.done():
                    t.cancel()
            if change_signal_task in done or stop_signal_task in done:
                return
            # next_batch_task finished.
            try:
                changes = next_batch_task.result()
            except StopAsyncIteration:
                return
            if not changes:
                continue
            await _broadcast(
                st,
                {
                    "type": "changed",
                    "folder": str(folder),
                    "files": sorted({p for _change, p in changes}),
                },
            )
    finally:
        try:
            await aiter.aclose()
        except RuntimeError:
            pass


async def _event_wait(st: WatcherState) -> None:
    """Wait for the next change-event set; cleared before we re-enter."""
    await st._change_event.wait()


async def _broadcast(st: WatcherState, message: dict[str, Any]) -> None:
    """Fan out a message to every connected client.

    Slow or disconnected clients don't back-pressure the watcher. If
    a queue is full, the message is dropped for that client (they'll
    get the next one). A broken send is treated as "client gone" and
    the client is unregistered.
    """
    if not st.clients:
        return
    for ws, q in st.clients:
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            # Drop the message for this client. They'd rather see the
            # next change than a stale one anyway.
            _log.debug("dropping change for slow client %s", id(ws))
    # Disconnection is detected in the WS handler when it tries to read
    # from the queue, not here — sending directly from the broker would
    # hold the watcher task hostage to a slow client.
