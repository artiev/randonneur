"""Disk-cached tile fetcher.

A tile is a small PNG (typically 5–20 KB) addressed by ``(source, z,
x, y)``. This module is the single place tiles are fetched from the
network; the rest of the project goes through :func:`fetch_tile_bytes`
which guarantees an on-disk cache and a per-host rate limit.

Cache layout
    ``<cache_dir>/tiles/<source>/<z>/<x>/<y>.png``

Two ``<source>`` values are supported today (OpenTopoMap default,
Thunderforest optional). Anything else is rejected by the server
endpoint so an attacker can't turn us into an open proxy.

API keys
    Sources whose URL template includes ``{apikey}`` (currently only
    Thunderforest) read the key from the environment at fetch time
    via :func:`randonneur.config.thunderforest_api_key`. The key is
    substituted into the URL before the request goes out and never
    leaves the server; the browser only sees
    ``/api/tiles/<source>/{z}/{x}/{y}.png``.

Atomicity
    Tiles are written via ``tmp + os.replace`` so a half-written file
    can't be served. The temp name includes the PID + a counter to
    avoid clashes between concurrent writes of the same tile.

Eviction
    None. Tile data is largely immutable and the personal scale of
    this project doesn't need a TTL. ``rm -rf ~/.cache/randonneur`` is
    the documented way to reset.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from pathlib import Path
from typing import Iterable

import httpx

from randonneur.config import (
    TILE_RATE_LIMITS,
    TILE_URL_TEMPLATES,
    cache_dir,
    thunderforest_api_key,
)
from randonneur.rate_limit import limiter_for


# ─── Public API ────────────────────────────────────────────────────────────────


class TileNotFoundError(Exception):
    """The upstream tile provider has no tile for this (z, x, y).

    This is *not* an upstream error — it's a normal answer. Tile
    providers return 404 for locations outside their coverage (deep
    ocean at high zoom, unmapped regions) and the map just shows a
    blank square. Raising a distinct exception lets the server turn
    this into a 404 to the browser rather than the misleading 502
    ``raise_for_status()`` would have produced.
    """


def known_sources() -> Iterable[str]:
    """Source identifiers accepted by :func:`fetch_tile_bytes`."""
    return TILE_URL_TEMPLATES.keys()


def tile_path(source: str, z: int, x: int, y: int) -> Path:
    """Where the tile *would* live on disk, regardless of whether it's been fetched."""
    return cache_dir() / "tiles" / source / str(z) / str(x) / f"{y}.png"


def is_source_available(source: str) -> bool:
    """Whether a source can be fetched right now.

    OpenTopoMap has no prereqs. Thunderforest needs an API key in the
    environment; without one, the source is in the whitelist but
    unavailable, so the settings panel can show it as "missing key"
    rather than 502ing on the first map pan.
    """
    if source not in TILE_URL_TEMPLATES:
        return False
    if source == "thunderforest-outdoors":
        return thunderforest_api_key() is not None
    return True


async def fetch_tile_bytes(source: str, z: int, x: int, y: int) -> bytes:
    """Return the PNG bytes for ``(source, z, x, y)``, fetching if needed.

    Raises :class:`ValueError` for unknown sources, :class:`RuntimeError`
    if the source needs an API key that isn't set,
    :class:`TileNotFoundError` if the upstream provider has no tile at
    that location (a normal "outside coverage" answer, *not* an error),
    :class:`httpx.HTTPError` for upstream 5xx / 429 / network errors.
    Multiple concurrent calls for the same tile are coalesced (a
    single in-flight network request per tile) so a freshly-loaded
    folder that requests 50 missing tiles doesn't issue 50 parallel
    OpenTopoMap requests and trip their rate limit.
    """
    if source not in TILE_URL_TEMPLATES:
        raise ValueError(f"unknown tile source: {source!r}")
    if not is_source_available(source):
        raise RuntimeError(f"tile source {source!r} is not configured (missing API key?)")
    path = tile_path(source, z, x, y)
    if path.is_file():
        return path.read_bytes()
    return await _fetch_with_coalesce(source, z, x, y, path)


# ─── Internals ────────────────────────────────────────────────────────────────


# Per-tile locks so N concurrent calls for the same (source, z, x, y)
# issue one network request, not N. The dict is process-global; entries
# are removed once the fetch resolves to keep it from growing without
# bound on a long-running server.
_in_flight: dict[tuple[str, int, int, int], asyncio.Future[bytes]] = {}


_counter = itertools.count()


async def _fetch_with_coalesce(
    source: str, z: int, x: int, y: int, path: Path
) -> bytes:
    key = (source, z, x, y)
    existing = _in_flight.get(key)
    if existing is not None:
        # Another task is already fetching this tile. Awaiting its
        # future gives us the same bytes without a second request.
        return await existing

    # We're the originator. The two callers of this coroutine (us
    # and any concurrent waiter) must both observe the result. The
    # simplest correct shape is: a single future for both, populated
    # from the underlying task via a done-callback. The originator
    # awaits that future too — that way the future is "retrieved"
    # (its result/exception consumed) and asyncio doesn't log
    # "Future exception was never retrieved" when it's GC'd.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bytes] = loop.create_future()
    _in_flight[key] = fut
    task = asyncio.create_task(_do_fetch(source, z, x, y, path))

    def _propagate(_t: asyncio.Task[bytes]) -> None:
        # Mirrors task.result() / task.exception() onto the shared
        # future. If the task was cancelled, cancel the future too so
        # waiters don't block forever.
        if task.cancelled():
            if not fut.done():
                fut.cancel()
            return
        exc = task.exception()
        if exc is not None:
            if not fut.done():
                fut.set_exception(exc)
        else:
            if not fut.done():
                fut.set_result(task.result())

    task.add_done_callback(_propagate)
    try:
        return await fut
    finally:
        # Drop the entry so the dict doesn't grow without bound on a
        # long-running server. Hold a reference to the task until
        # here so its exception (if any) is consumed via the future
        # we awaited, not leaked.
        _in_flight.pop(key, None)


async def _do_fetch(source: str, z: int, x: int, y: int, path: Path) -> bytes:
    url = _render_url(source, z, x, y)
    rate = TILE_RATE_LIMITS.get(source, 1.0)
    async with limiter_for(source, rate):
        # Explicit try/finally (not ``async with httpx.AsyncClient``)
        # so the client is closed *before* the 404 path raises. The
        # client's __aexit__ schedules a connection-cleanup task on
        # the response stream; if we re-raise through __aexit__ the
        # cleanup future can be GC'd with its exception unretrieved
        # and asyncio prints "Future exception was never retrieved".
        # Closing the client explicitly awaits the cleanup.
        client = httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                # Tile provider has no tile here — the map will show
                # a blank square, which is the right behaviour for
                # the user's data (e.g. a track over open ocean at z=17).
                raise TileNotFoundError(
                    f"tile {source}/{z}/{x}/{y} not found upstream"
                )
            resp.raise_for_status()
            data = resp.content
        finally:
            await client.aclose()
    _write_atomic(path, data)
    return data


def _render_url(source: str, z: int, x: int, y: int) -> str:
    """Substitute placeholders in the source's URL template.

    ``{s}`` picks an OSM-style subdomain; ``{z}/{x}/{y}`` are the
    tile coordinates. ``{apikey}`` is filled from the env if the
    template needs it; if the key is unset, :func:`fetch_tile_bytes`
    rejects the call before we get here, so the ``.format`` below
    is safe.
    """
    template = TILE_URL_TEMPLATES[source]
    fmt = {"s": _subdomain_for(z), "z": z, "x": x, "y": y}
    if "{apikey}" in template:
        # Caller must have already checked is_source_available; we
        # raise defensively in case that contract is ever loosened.
        key = thunderforest_api_key()
        if key is None:
            raise RuntimeError(f"tile source {source!r} needs an API key")
        fmt["apikey"] = key
    return template.format(**fmt)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file + os.replace.

    Two reasons:

    1. A reader landing on a half-written file would get a corrupt PNG.
    2. ``os.replace`` is atomic on POSIX, so the file is either the
       previous version or the new one — never a mix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{next(_counter)}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        # Don't leave a temp lying around if something went wrong.
        if tmp.exists():
            tmp.unlink()
        raise


def _subdomain_for(z: int) -> str:
    """Pick one of OpenStreetMap's a/b/c subdomains deterministically.

    OpenStreetMap-style tile providers serve the same tile from
    multiple subdomains so a client can parallelise. Spreading requests
    over them is good citizenship. The choice is keyed on ``z`` so the
    same tile always lands on the same subdomain (it doesn't have to,
    but it makes the cache layout easier to reason about in logs).
    """
    return ("a", "b", "c")[z % 3]
