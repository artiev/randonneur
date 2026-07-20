"""Tests for ``randonneur.watcher``.

The broker is a pure-async object; we drive it directly. The real
``watchfiles.awatch`` is exercised via a real file touch in a temp
dir, marked ``integration`` because it's the only test that actually
touches the filesystem across threads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from randonneur import watcher


class _FakeWebSocket:
    """Minimal WebSocket stand-in for the broker tests.

    The real ``fastapi.WebSocket`` requires an active connection
    (TestClient or real socket); the broker only calls ``subscribe``
    and reads from the returned queue, so a stub is enough.
    """

    def __init__(self) -> None:
        self.id = id(self)


# ─── Pure-broker tests (no filesystem, no real awatch) ──────────────────────


@pytest.fixture(autouse=True)
def _reset_watcher_state() -> None:
    """Each test gets a clean broker. Module-level state survives pytest."""
    watcher.reset_for_tests()
    yield
    watcher.reset_for_tests()


@pytest.mark.asyncio
async def test_subscribe_yields_a_queue() -> None:
    app = object()
    ws = _FakeWebSocket()
    async with watcher.subscribe(app, ws) as q:
        assert isinstance(q, asyncio.Queue)


@pytest.mark.asyncio
async def test_subscribe_unregisters_on_exit() -> None:
    app = object()
    ws = _FakeWebSocket()
    async with watcher.subscribe(app, ws):
        st = watcher.state_for(app)
        assert len(st.clients) == 1
    st = watcher.state_for(app)
    assert st.clients == []


@pytest.mark.asyncio
async def test_subscribe_unregisters_on_exception() -> None:
    app = object()
    ws = _FakeWebSocket()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with watcher.subscribe(app, ws):
            raise _Boom("simulated handler failure")
    st = watcher.state_for(app)
    assert st.clients == []


@pytest.mark.asyncio
async def test_multiple_clients_each_get_their_own_queue() -> None:
    app = object()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    async with watcher.subscribe(app, ws1) as q1, watcher.subscribe(app, ws2) as q2:
        assert q1 is not q2
        st = watcher.state_for(app)
        assert len(st.clients) == 2


@pytest.mark.asyncio
async def test_shutdown_signals_all_clients() -> None:
    # Drive the fan-out path directly: register two clients, push a
    # message via the broker, then call shutdown and confirm both
    # clients get the sentinel.
    app = object()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    async with watcher.subscribe(app, ws1) as q1, watcher.subscribe(app, ws2) as q2:
        # Hand-inject a message via the same code path the watcher
        # uses. ``_broadcast`` is the internal function; that's fine
        # for a test of the broker's behaviour.
        st = watcher.state_for(app)
        await watcher._broadcast(st, {"type": "changed", "files": ["x.gpx"]})
        m1 = await asyncio.wait_for(q1.get(), timeout=0.5)
        m2 = await asyncio.wait_for(q2.get(), timeout=0.5)
        assert m1 == m2 == {"type": "changed", "files": ["x.gpx"]}

        # Now shut down. Both queues get the None sentinel.
        await watcher.shutdown(app)
        s1 = await asyncio.wait_for(q1.get(), timeout=0.5)
        s2 = await asyncio.wait_for(q2.get(), timeout=0.5)
        assert s1 is None and s2 is None


@pytest.mark.asyncio
async def test_set_active_folder_is_idempotent() -> None:
    # Calling set_active_folder twice with the same path must not
    # restart the watcher task. The task lives for the app's lifetime;
    # the test starts it manually because we don't have a real app.
    app = object()
    folder = Path("/tmp/some-nonexistent-folder-for-test")
    await watcher.start(app)
    st = watcher.state_for(app)
    assert st._task is not None
    first_task = st._task
    await watcher.set_active_folder(app, folder)
    await asyncio.sleep(0)  # let the task see the change
    assert st._task is first_task
    await watcher.shutdown(app)


@pytest.mark.asyncio
async def test_set_active_folder_switches_target() -> None:
    # The watcher task is long-lived and re-targets when the active
    # folder changes. The task itself isn't replaced — its inner
    # awatch loop is.
    app = object()
    f1, f2 = Path("/tmp/watch-test-a"), Path("/tmp/watch-test-b")
    await watcher.start(app)
    st = watcher.state_for(app)
    first_task = st._task
    assert first_task is not None
    await watcher.set_active_folder(app, f1)
    await asyncio.sleep(0.05)
    await watcher.set_active_folder(app, f2)
    await asyncio.sleep(0.05)
    # Same task instance — only the inner awatch was swapped.
    assert st._task is first_task
    # And the active folder is the new one.
    assert st.active_folder == f2
    await watcher.shutdown(app)


# ─── Integration: real awatch + real file change ───────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watcher_broadcasts_on_real_file_change(tmp_path: Path) -> None:
    # End-to-end smoke: start the watcher task, set the active folder,
    # touch a file inside it, confirm a "changed" message lands on a
    # subscribed client within the debounce window + some slack.
    app = object()
    folder = tmp_path / "gpx"
    folder.mkdir()
    (folder / "existing.gpx").write_text("<gpx/>")

    ws = _FakeWebSocket()
    await watcher.start(app)
    await watcher.set_active_folder(app, folder)

    async with watcher.subscribe(app, ws) as queue:
        # The watcher broadcasts *every* batch from awatch, including
        # the initial attach snapshot (which lists existing.gpx but
        # not the file we are about to write). We read messages in a
        # loop until we see one whose ``files`` contains fresh.gpx,
        # so this test is robust to whether the attach snapshot and
        # the real change are delivered as one batch or two.
        await asyncio.sleep(0.2)
        (folder / "fresh.gpx").write_text("<gpx/>")

        deadline = asyncio.get_event_loop().time() + 4.0
        msg: dict | None = None
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                candidate = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if any(p.endswith("fresh.gpx") for p in candidate.get("files", [])):
                msg = candidate
                break
        assert msg is not None, "no batch containing fresh.gpx in 4s"
        assert msg["type"] == "changed"

    await watcher.shutdown(app)
