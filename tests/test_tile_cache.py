"""Tests for ``randonneur.tile_cache`` and ``randonneur.rate_limit``.

The tile cache talks to the real OpenTopoMap endpoint, so the tests
mark themselves as integration-only (``@pytest.mark.integration``).
The unit tests for the rate limiter stand alone.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from randonneur import tile_cache
from randonneur.config import TILE_RATE_LIMITS, TILE_URL_TEMPLATES, cache_dir
from randonneur.rate_limit import RateLimiter, limiter_for, reset_limiters


# ─── Rate limiter (unit, no network) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limiter_caps_calls_per_second() -> None:
    # 20 calls/sec means a 50 ms minimum gap. Time 5 calls; expect ≥ 200 ms.
    rl = RateLimiter(rate=20.0)
    t0 = time.monotonic()
    for _ in range(5):
        async with rl:
            pass
    elapsed = time.monotonic() - t0
    # 4 gaps × 50 ms = 200 ms minimum. Give a small tolerance for asyncio
    # scheduling jitter on a loaded CI box.
    assert elapsed >= 0.18, f"elapsed {elapsed:.3f}s, expected ≥ 0.18s"


@pytest.mark.asyncio
async def test_rate_limiter_serialises_concurrent_callers() -> None:
    # 5 concurrent tasks through a 10/s limiter must end up spread over
    # ~0.4s, not fired in a single burst. This is the property that
    # protects OpenTopoMap from us.
    rl = RateLimiter(rate=10.0)
    t0 = time.monotonic()

    async def call() -> None:
        async with rl:
            pass

    await asyncio.gather(*(call() for _ in range(5)))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.35, f"concurrent calls fired in {elapsed:.3f}s, expected ≥ 0.35s"


def test_rate_limiter_rejects_zero_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(rate=0)
    with pytest.raises(ValueError):
        RateLimiter(rate=-1.0)


def test_limiter_for_returns_singleton() -> None:
    # The cache relies on a single limiter per host so concurrent
    # fetches share the rate. Verify the singleton behaviour.
    reset_limiters()
    a = limiter_for("opentopomap", 1.0)
    b = limiter_for("opentopomap", 999.0)  # rate ignored on second call
    assert a is b
    # Different source → different limiter.
    c = limiter_for("thunderforest-outdoors", 8.0)
    assert c is not a
    reset_limiters()


# ─── Tile cache: config shape (no network) ──────────────────────────────────


def test_known_sources_matches_url_templates() -> None:
    # Drift between the two tables would be a real bug — the endpoint
    # 404s for sources that aren't in TILE_URL_TEMPLATES, and rate
    # limits without a URL can't be fetched.
    assert set(tile_cache.known_sources()) == set(TILE_URL_TEMPLATES.keys())


def test_opentopomap_rate_matches_policy() -> None:
    # OpenTopoMap asks for ≤1 req/s. The number in code is the contract;
    # a future "let's bump it to 2 because it'll be fine" change would
    # get our IP throttled. Pin it.
    assert TILE_RATE_LIMITS["opentopomap"] == 1.0


def test_tile_path_under_cache_dir() -> None:
    p = tile_cache.tile_path("opentopomap", 5, 10, 20)
    assert p == cache_dir() / "tiles" / "opentopomap" / "5" / "10" / "20.png"


def test_tile_path_does_not_escape_cache_dir() -> None:
    # tile_path uses z/x/y ints, but a caller (or a future refactor)
    # that lets the path be user-controlled must not let "../" walk out
    # of the cache. The function takes ints, so this is a tautology
    # today; keep the test as a tripwire if the signature changes.
    p = tile_cache.tile_path("opentopomap", 0, 0, 0)
    parts = p.parts
    assert "tiles" in parts
    assert ".." not in parts


@pytest.mark.asyncio
async def test_fetch_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown tile source"):
        await tile_cache.fetch_tile_bytes("bogus-source", 0, 0, 0)


def test_is_source_available_opentopomap_always_true() -> None:
    # OpenTopoMap has no prereqs. Even with no env vars set, the
    # source is available.
    assert tile_cache.is_source_available("opentopomap") is True


def test_is_source_available_thunderforest_requires_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Thunderforest needs RANDONNEUR_THUNDERFOREST_KEY. Without it
    # the source is in the whitelist (known_sources()) but
    # unavailable for fetching.
    monkeypatch.delenv("RANDONNEUR_THUNDERFOREST_KEY", raising=False)
    assert "thunderforest-outdoors" in set(tile_cache.known_sources())
    assert tile_cache.is_source_available("thunderforest-outdoors") is False

    monkeypatch.setenv("RANDONNEUR_THUNDERFOREST_KEY", "test-key-abc")
    assert tile_cache.is_source_available("thunderforest-outdoors") is True


def test_is_source_available_unknown_source_false() -> None:
    assert tile_cache.is_source_available("bogus-source") is False


def test_render_url_substitutes_apikey_when_template_needs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Thunderforest template has ``?apikey={apikey}``. _render_url
    # must fill that placeholder from the env so the upstream call
    # authenticates. We don't make the call — we just check the
    # string the cache would request.
    monkeypatch.setenv("RANDONNEUR_THUNDERFOREST_KEY", "secret-xyz")
    url = tile_cache._render_url("thunderforest-outdoors", 5, 16, 11)
    assert "apikey=secret-xyz" in url
    assert "{apikey}" not in url
    assert "/5/16/11.png" in url


def test_render_url_works_without_apikey_when_template_doesnt_need_it() -> None:
    # OpenTopoMap's template has no {apikey} placeholder; the call
    # should succeed regardless of env state.
    url = tile_cache._render_url("opentopomap", 5, 16, 11)
    assert "{apikey}" not in url
    assert "5/16/11.png" in url


@pytest.mark.asyncio
async def test_fetch_tile_bytes_rejects_unconfigured_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Calling fetch on a source that's whitelisted but unconfigured
    # (e.g. Thunderforest with no key) must raise, not silently 404
    # or hang. The endpoint layer turns this into 503.
    monkeypatch.delenv("RANDONNEUR_THUNDERFOREST_KEY", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        await tile_cache.fetch_tile_bytes("thunderforest-outdoors", 0, 0, 0)


class _Fake404Response:
    """Mimics the httpx.Response surface _do_fetch uses."""

    status_code = 404
    content = b""

    def raise_for_status(self) -> None:
        pass


class _FakeAsyncClient:
    """Stub AsyncClient that always 404s and records aclose() calls."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.aclose_called = False

    async def get(self, url: str) -> _Fake404Response:  # noqa: ARG002
        return _Fake404Response()

    async def aclose(self) -> None:
        # In real httpx, aclose() returns when the response stream
        # has been fully consumed. The bug we're fixing was that
        # letting the 404 propagate through ``async with`` left a
        # connection-cleanup future un-awaited; here we just record
        # the call so the test can assert the fix is in place.
        self.aclose_called = True


@pytest.mark.asyncio
async def test_404_from_upstream_raises_tile_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenTopoMap returns 404 for tiles outside coverage (e.g. a
    # track over open ocean at z=17). That is a *normal* answer, not
    # an upstream error, so ``_do_fetch`` must translate it into
    # TileNotFoundError rather than letting ``raise_for_status``
    # produce an HTTPStatusError that the server would 502.
    fake = _FakeAsyncClient()
    monkeypatch.setattr(tile_cache.httpx, "AsyncClient", lambda *a, **kw: fake)
    with pytest.raises(tile_cache.TileNotFoundError, match="not found upstream"):
        await tile_cache._do_fetch(
            "opentopomap", 17, 67926, 47788, tile_cache.tile_path("opentopomap", 17, 67926, 47788)
        )
    # The AsyncClient must be closed *before* the exception escapes,
    # so its connection-cleanup future is awaited (the original bug).
    assert fake.aclose_called, "AsyncClient.aclose() was not called before the 404 raised"


@pytest.mark.asyncio
async def test_tile_not_found_does_not_leak_future_on_originator_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for "Future exception was never retrieved" warnings
    # that fired when a 404 propagated through _fetch_with_coalesce.
    # The originating task had set the future's exception but never
    # awaited the future itself; on GC, asyncio logged the warning.
    # The fix routes both the originator and any waiters through the
    # same future, so its result/exception is consumed.
    loop = asyncio.get_running_loop()
    captured: list[str] = []
    loop.set_exception_handler(
        lambda _l, ctx: captured.append(ctx.get("message", ""))
        if "never retrieved" in ctx.get("message", "")
        else None
    )
    monkeypatch.setattr(tile_cache.httpx, "AsyncClient", _FakeAsyncClient)
    for i in range(20):
        with pytest.raises(tile_cache.TileNotFoundError):
            await tile_cache.fetch_tile_bytes("opentopomap", 17, 67924 + i, 47788)
    # Give the loop a chance to surface any pending warnings.
    await asyncio.sleep(0.05)
    assert captured == [], f"unexpected 'never retrieved' warnings: {captured}"


@pytest.mark.asyncio
async def test_tile_not_found_does_not_leak_future_under_concurrent_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same as above, but with N concurrent callers all waiting on
    # the same tile. The coalescing path must not leak futures
    # either.
    loop = asyncio.get_running_loop()
    captured: list[str] = []
    loop.set_exception_handler(
        lambda _l, ctx: captured.append(ctx.get("message", ""))
        if "never retrieved" in ctx.get("message", "")
        else None
    )
    monkeypatch.setattr(tile_cache.httpx, "AsyncClient", _FakeAsyncClient)
    results = await asyncio.gather(
        *(tile_cache.fetch_tile_bytes("opentopomap", 17, 67924, 47788) for _ in range(10)),
        return_exceptions=True,
    )
    # All 10 callers see the same TileNotFoundError (one upstream
    # fetch, one shared future, ten waiters all observing it).
    assert all(isinstance(r, tile_cache.TileNotFoundError) for r in results), results
    await asyncio.sleep(0.05)
    assert captured == [], f"unexpected 'never retrieved' warnings: {captured}"


# ─── Tile cache: integration (hits real OpenTopoMap) ────────────────────────


@pytest.fixture
def isolated_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the cache at a temp dir so tests don't touch the real one.

    The cache_dir() helper honours $XDG_CACHE_HOME; we use that rather
    than monkeypatching the function so the production code path is
    exercised.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_tile_writes_to_disk(isolated_cache_dir: Path) -> None:
    # A real tile at a known z/x/y. The Dolomites 46.5/11.4 area is the
    # default map view, so picking z=5 around that lat/lon is a stable
    # choice (the tile is small but real).
    z, x, y = 5, 16, 11
    path = tile_cache.tile_path("opentopomap", z, x, y)

    # Pre-condition: no file yet.
    assert not path.exists()

    data = await tile_cache.fetch_tile_bytes("opentopomap", z, x, y)

    # Post-condition: file on disk, starts with the PNG magic.
    assert path.is_file()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert path.read_bytes() == data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_tile_uses_cache_on_second_call(
    isolated_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sanity: second call must not hit the network. We assert that by
    # making the network call fail — if the cache worked, the second
    # call returns the bytes anyway.
    z, x, y = 5, 16, 11
    await tile_cache.fetch_tile_bytes("opentopomap", z, x, y)

    # Now patch the underlying httpx call to raise. If the cache is
    # working, the second call returns the on-disk bytes regardless.
    from randonneur import tile_cache as tc

    real_do_fetch = tc._do_fetch

    async def broken_do_fetch(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("network should not be hit on cache hit")

    monkeypatch.setattr(tc, "_do_fetch", broken_do_fetch)
    try:
        data = await tc.fetch_tile_bytes("opentopomap", z, x, y)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        # Restore for any later tests in the same session.
        monkeypatch.setattr(tc, "_do_fetch", real_do_fetch)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_fetches_for_same_tile_coalesce(
    isolated_cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 10 concurrent calls for the same tile → exactly one network fetch.
    from randonneur import tile_cache as tc

    call_count = 0
    real_do_fetch = tc._do_fetch

    async def counting_do_fetch(*args: object, **kwargs: object) -> bytes:
        nonlocal call_count
        call_count += 1
        return await real_do_fetch(*args, **kwargs)

    monkeypatch.setattr(tc, "_do_fetch", counting_do_fetch)
    z, x, y = 5, 16, 11
    try:
        results = await asyncio.gather(
            *(tc.fetch_tile_bytes("opentopomap", z, x, y) for _ in range(10))
        )
    finally:
        monkeypatch.setattr(tc, "_do_fetch", real_do_fetch)
    assert call_count == 1, f"expected 1 fetch, got {call_count}"
    assert len({r[:8] for r in results}) == 1  # all the same bytes
