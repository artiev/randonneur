"""FastAPI app: the backend the browser UI talks to.

Endpoints:

- ``GET /api/folder?path=<absolute>`` — list parsed tracks for a folder.
  Per-file parse errors are returned in the response (``errors`` field)
  so the UI can show "could not parse foo.gpx" rather than dropping
  the whole folder.
- ``GET /api/tracks/{track_id}`` — full polyline points + summary for
  one track. The map needs the points (the folder summary deliberately
  omits them to keep the list small).
- ``GET /api/tracks/{track_id}/profile`` — aligned ``(distances_km,
  elevations_m)`` arrays for the Plotly elevation chart. Computed via
  :func:`randonneur.profile.compute_profile`.
- ``GET /api/settings`` — base layer + scale bar prefs. Returns the
  available tile sources (with availability flags driven by which API
  keys are present in the env) and the current defaults.

Static files are mounted at ``/`` from ``randonneur/static`` and serve
the HTML/CSS/JS shell plus the Leaflet map and Plotly elevation profile
built on top of it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import gpxpy.gpx
import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from randonneur import gpx_loader, profile, tile_cache, watcher
from randonneur.config import (
    DEFAULT_TILE_SOURCE,
    TILE_SOURCE_LABELS,
)
from randonneur.log import get_logger

_log = get_logger("server")

# Per-app state, keyed on id(app) so multiple apps in one process (only
# tests today, but multi-folder / multi-session features tomorrow) don't
# share caches. The watcher uses the same id(app) keying — see
# ``watcher.state_for``. Holding the cache here rather than on the module
# means a track loaded into app A is invisible to app B, which is the
# invariant the per-track endpoints rely on.
_APPS: dict[int, "_AppState"] = {}


class _AppState:
    """Per-FastAPI-app mutable state. Currently just the track cache."""

    def __init__(self) -> None:
        # id → Track. Populated lazily as the folder is listed, used by the
        # per-track endpoints. We don't persist this — the file on disk is
        # the source of truth; a restart is a clean slate. The cache also
        # lets the folder summary and the track detail share the same parsed
        # object (no re-parse on click).
        self.track_cache: dict[str, gpx_loader.Track] = {}


def _state_for(app: FastAPI) -> _AppState:
    """Return the per-app state, creating it on first use."""
    key = id(app)
    st = _APPS.get(key)
    if st is None:
        st = _AppState()
        _APPS[key] = st
    return st


def reset_for_tests() -> None:
    """Drop all per-app state. Test-only — never call in production."""
    _APPS.clear()


# ─── Response models ──────────────────────────────────────────────────────────


class Point(BaseModel):
    lat: float
    lon: float
    ele: float | None


class TrackSummary(BaseModel):
    """Lightweight track record returned to the UI's folder list.

    The point list is intentionally omitted — the UI fetches polyline
    points and the elevation profile via dedicated endpoints, so the
    folder list stays small even with many tracks.
    """

    id: str
    name: str
    color: str
    points: int
    distance_km: float
    elev_gain_m: float
    bbox: tuple[float, float, float, float] = Field(description="(west, south, east, north)")


class TrackDetail(BaseModel):
    """Full track record returned to the map. Includes the polyline."""

    id: str
    name: str
    color: str
    distance_km: float
    elev_gain_m: float
    bbox: tuple[float, float, float, float]
    points: list[Point]


class TrackProfile(BaseModel):
    """Aligned distance/elevation arrays for the Plotly elevation chart."""

    id: str
    name: str
    color: str
    distances_km: list[float] = Field(
        description="Cumulative haversine distance in km, same length as points."
    )
    elevations_m: list[float] = Field(
        description="Elevation in metres aligned by index with distances_km. "
        "Missing samples are 0.0 (see profile.compute_profile for why)."
    )


class FolderResponse(BaseModel):
    path: str | None
    tracks: list[TrackSummary]
    errors: list[str] = Field(
        default_factory=list,
        description="Per-file parse failures: '<name>: <reason>'.",
    )


class TileSource(BaseModel):
    """A single tile source in the settings panel."""

    id: str
    name: str
    available: bool = Field(
        description="True if the source can be fetched right now. "
        "False when an API key is missing (e.g. Thunderforest)."
    )
    needs_key: bool = Field(
        description="True if the source requires an API key the user "
        "must provide (currently only Thunderforest)."
    )


class SettingsResponse(BaseModel):
    """User-facing settings payload.

    ``current_source`` is the default the UI should use on first
    load; the UI is free to override it via in-memory state and
    doesn't need to round-trip changes through the server. (No
    server-side state is kept — this is a viewer, not a configurator.)
    """

    current_source: str
    sources: list[TileSource]


# ─── Internals ────────────────────────────────────────────────────────────────


def _get_cached(app: FastAPI, track_id: str) -> gpx_loader.Track:
    """Return the cached Track for ``app``, or 404. Shared by all per-track endpoints."""
    try:
        return _state_for(app).track_cache[track_id]
    except KeyError:
        # Either the user navigated to a stale id, or the server was
        # restarted since the folder was loaded. The UI handles this by
        # re-listing the folder.
        raise HTTPException(status_code=404, detail=f"unknown track: {track_id}")


# ─── App factory ──────────────────────────────────────────────────────────────


def create_app(static_dir: Path | None = None) -> FastAPI:
    """Build the FastAPI app.

    ``static_dir`` is injected so tests can point at a temp folder; the
    production server (server_thread) uses the package's ``static/``.

    A lifespan handler is registered so the watcher task is stopped
    cleanly on app shutdown — without it, a Ctrl-C leaves a dangling
    ``awatch`` coroutine that holds the asyncio loop open and prevents
    the process from exiting.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Start the long-lived watcher task in the app's event loop.
        # It will idle on its change_event until /api/folder sets an
        # active folder. Started before yield so the task is bound to
        # the same loop that runs request handlers; if we created it
        # in a handler, the TestClient's per-request portal would
        # cancel it on handler return.
        await watcher.start(_app)
        try:
            yield
        finally:
            await watcher.shutdown(_app)

    app = FastAPI(title="randonneur", version="0.1.0", lifespan=lifespan)

    @app.get("/api/folder", response_model=FolderResponse)
    async def api_folder(path: Annotated[str | None, Query()] = None) -> FolderResponse:
        if path is None:
            return FolderResponse(path=None, tracks=[])
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise HTTPException(status_code=404, detail=f"folder not found: {folder}")

        files = gpx_loader.discover(folder)
        tracks: list[TrackSummary] = []
        errors: list[str] = []
        # Reset the cache on every folder load — tracks from a different
        # folder must not bleed into the new one. (A future version could
        # keep entries for files whose mtime hasn't changed; not worth the
        # complexity for personal-scale folders.)
        cache = _state_for(app).track_cache
        cache.clear()
        for f in files:
            try:
                t = gpx_loader.parse(f)
            except gpxpy.gpx.GPXException as e:
                # Per-file failure: log and surface, but keep going so
                # one bad file doesn't blank the whole folder.
                errors.append(f"{f.name}: {e.__class__.__name__}: {e}")
                continue
            except OSError as e:
                errors.append(f"{f.name}: {e.__class__.__name__}: {e}")
                continue
            cache[t.id] = t
            tracks.append(
                TrackSummary(
                    id=t.id,
                    name=t.name,
                    color=t.color,
                    points=len(t.points),
                    distance_km=round(t.distance_km, 3),
                    elev_gain_m=round(t.elev_gain_m, 1),
                    bbox=t.bbox,
                )
            )
        _log.info("Folder %s: %d track(s), %d error(s)", folder, len(tracks), len(errors))
        # Point the watcher at this folder (no-op if unchanged). This
        # is fire-and-forget at the broker level — the handler doesn't
        # wait for an awatch attach, so a slow first attach never
        # delays the response.
        await watcher.set_active_folder(app, folder)
        return FolderResponse(path=str(folder), tracks=tracks, errors=errors)

    @app.get("/api/tracks/{track_id}", response_model=TrackDetail)
    def api_track(track_id: str) -> TrackDetail:
        t = _get_cached(app, track_id)
        return TrackDetail(
            id=t.id,
            name=t.name,
            color=t.color,
            distance_km=round(t.distance_km, 3),
            elev_gain_m=round(t.elev_gain_m, 1),
            bbox=t.bbox,
            # Sending the full point list over the wire is fine for v1
            # — typical GPX files are a few hundred points; 10,000+ is
            # rare and still small in JSON. If a future user has 100k+
            # point tracks, downsample here.
            points=[Point(lat=p.lat, lon=p.lon, ele=p.ele) for p in t.points],
        )

    @app.get("/api/tracks/{track_id}/profile", response_model=TrackProfile)
    def api_track_profile(track_id: str) -> TrackProfile:
        t = _get_cached(app, track_id)
        distances_km, elevations_m = profile.compute_profile(t.points)
        # TrackProfile is the Plotly-shaped payload. The arrays stay
        # raw (no downsampling) — Plotly handles 1k-10k points in
        # scatter mode fine; switch to scattergl in a future v2 if
        # anyone hits a real perf issue.
        return TrackProfile(
            id=t.id,
            name=t.name,
            color=t.color,
            distances_km=distances_km,
            elevations_m=elevations_m,
        )

    @app.get("/api/settings", response_model=SettingsResponse)
    def api_settings() -> SettingsResponse:
        # The settings panel in the UI needs to know which tile
        # sources exist and which are available right now. A source
        # is "in the whitelist" iff its URL is in
        # TILE_URL_TEMPLATES; it's "available" iff it has no
        # environment prereqs (or they're met). The Thunderforest
        # entry is always in the whitelist — the panel just marks
        # it as unavailable when RANDONNEUR_THUNDERFOREST_KEY is
        # unset, so the user sees "missing key" rather than a 502
        # on the first map pan.
        sources: list[TileSource] = []
        for sid in tile_cache.known_sources():
            sources.append(
                TileSource(
                    id=sid,
                    name=TILE_SOURCE_LABELS.get(sid, sid),
                    available=tile_cache.is_source_available(sid),
                    needs_key=sid == "thunderforest-outdoors",
                )
            )
        return SettingsResponse(
            current_source=DEFAULT_TILE_SOURCE,
            sources=sources,
        )

    @app.get("/api/tiles/{source}/{z}/{x}/{y}.png")
    async def api_tile(source: str, z: int, x: int, y: int) -> Response:
        # Why the path is /api/tiles/<source>/... and not /tiles/<source>/...:
        # the latter would collide with the static-files mount at "/" when
        # a v2 adds, say, /tiles/opentopomap/{z}/<file>. Keeping the tile
        # endpoint under /api keeps the URL space unambiguous.
        #
        # All z/x/y are ints — Leaflet guarantees this but a malicious
        # client could send paths like "..%2F..%2Fetc%2Fpasswd". The
        # int coercion in the function signature rejects anything that
        # doesn't parse, and tile_path(...) uses Path so a "0/0/0" tile
        # can never escape the cache dir.
        if source not in tile_cache.known_sources():
            raise HTTPException(status_code=404, detail=f"unknown tile source: {source}")
        if not tile_cache.is_source_available(source):
            # Whitelisted but unconfigured (e.g. Thunderforest without
            # an API key). 503 is the honest code here: the *resource*
            # is real, but the server can't supply it right now.
            raise HTTPException(
                status_code=503,
                detail=f"tile source {source!r} is not configured (missing API key?)",
            )
        try:
            data = await tile_cache.fetch_tile_bytes(source, z, x, y)
        except tile_cache.TileNotFoundError:
            # The upstream provider has no tile for this (z, x, y) —
            # the map will show a blank square, which is the right
            # behaviour for the user's data (a track over open ocean
            # at z=17, say). 404 to the browser is honest; do NOT
            # 502 this — it is not an upstream error, it's a normal
            # answer from the tile server.
            raise HTTPException(
                status_code=404,
                detail=f"tile {source}/{z}/{x}/{y} not found",
            )
        except httpx.HTTPError as e:
            # Upstream error (5xx, 429, DNS, etc.). 502 is the honest
            # upstream-equivalent code; a 503 would be wrong because
            # *we* aren't unavailable, our dependency is.
            raise HTTPException(status_code=502, detail=f"upstream tile error: {e}")
        # image/png because OpenTopoMap and Thunderforest both serve
        # PNG; if a v2 adds a vector source (pbf) this becomes a
        # content-type switch keyed on the source.
        return Response(content=data, media_type="image/png")

    @app.websocket("/api/ws")
    async def api_ws(websocket: WebSocket) -> None:
        # Hot-reload channel. The client connects once after loading a
        # folder and stays connected for the life of the page. The
        # server fans out every debounced change batch as one JSON
        # message. The client doesn't act on the specific files; it
        # re-fetches /api/folder on every message.
        #
        # A single shared subscription per app; the broker handles
        # fan-out. We do not require an active folder here — if the
        # user hasn't picked one yet, the client just gets a quiet
        # connection that will start producing messages as soon as
        # /api/folder is called.
        await websocket.accept()
        async with watcher.subscribe(app, websocket) as queue:
            try:
                while True:
                    msg = await queue.get()
                    if msg is None:
                        # Server is shutting down — close gracefully.
                        await websocket.close()
                        return
                    await websocket.send_json(msg)
            except WebSocketDisconnect:
                # Client went away (page closed, navigation). The
                # ``subscribe`` context manager unregisters on exit.
                return

    if static_dir is not None and static_dir.is_dir():
        # Mount at root; FastAPI's StaticFiles handles missing-file 404s.
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# ─── Server entry point (used by the CLI) ────────────────────────────────────


def static_files_dir() -> Path:
    """Path to the package's ``static/`` directory (HTML/JS/CSS)."""
    return Path(__file__).parent / "static"


__all__ = ["create_app", "static_files_dir"]
