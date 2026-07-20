"""Per-host async rate limiter.

OpenTopoMap's tile usage policy asks for no more than 1 request/second
per client. Thunderforest's free tier allows more. Rather than encode
either policy in the call site, this module is the single place where
"how fast do we hit a given host" lives, and the tile cache picks a
limiter per source.

The implementation is a token bucket with capacity 1 (so it behaves
like "one in flight, then wait 1/rate seconds before the next"). That
matches what OpenTopoMap actually needs — they don't care about burst
shape, they care about the long-run rate — and keeps the state to a
single :class:`asyncio.Lock` + a timestamp. A multi-token bucket would
be measurably more code for no observable difference at our request
rate.

Per-host singletons are kept in :data:`_LIMITERS` so every caller shares
the same gate. Without that, two concurrent fetches for the same host
would each see an empty bucket and both fire.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Gate an async workload to at most ``rate`` calls per second.

    Usage::

        limiter = RateLimiter(rate=1.0)
        async with limiter:
            await do_request()

    Calls beyond the rate are delayed, not rejected — a tile request
    must always eventually succeed, so callers don't need a retry path.
    """

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        self._min_interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._last_call = 0.0  # epoch seconds; 0 means "no call yet"

    async def __aenter__(self) -> "RateLimiter":
        await self._lock.acquire()
        # Wait outside the lock? No — we need to hold the lock while we
        # sleep, otherwise two waiters can both wake up and fire
        # back-to-back. Sleep inside the lock and the second waiter
        # starts its clock from the first's fire time, which is what
        # we want.
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


# Per-host singletons. Keys are the source identifiers used in
# ``TILE_SOURCES`` (config.py): the same string the browser sees in the
# URL prefix, so a misconfigured source still throttles correctly.
_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(source: str, rate: float) -> RateLimiter:
    """Return the per-host :class:`RateLimiter` for ``source``.

    The first call for a given source creates the limiter; subsequent
    calls return the same one. ``rate`` is only consulted on first call
    so callers can safely pass a value computed from config without
    worrying about drift between modules.
    """
    existing = _LIMITERS.get(source)
    if existing is not None:
        return existing
    rl = RateLimiter(rate)
    _LIMITERS[source] = rl
    return rl


def reset_limiters() -> None:
    """Clear all singletons. Test-only — never call in production."""
    _LIMITERS.clear()
