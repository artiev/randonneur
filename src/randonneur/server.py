"""FastAPI app: the backend the browser UI talks to.

Endpoints:

- ``GET /api/folder`` — list parsed tracks for the configured folder.
  The folder is fixed for the server's lifetime (set via the
  ``--directory`` CLI flag at startup) and lives in the per-app state.
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

import gpxpy.gpx
import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

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
    """Per-FastAPI-app mutable state.

    Two fields:

    - ``active_folder``: the folder the server was started with (via
      ``create_app(active_folder=...)`` / ``randonneur serve --directory
      <path>``). The folder is fixed for the server's lifetime; the
      UI shows it as read-only information.
    - ``track_cache``: id → Track. Populated lazily as the folder is
      listed, used by the per-track endpoints. We don't persist this
      — the file on disk is the source of truth; a restart is a clean
      slate. The cache also lets the folder summary and the track
      detail share the same parsed object (no re-parse on click).
    """

    def __init__(self) -> None:
        self.active_folder: Path | None = None
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


class ElevGainLoss(BaseModel):
    """Elevation gain/loss for one smoothing half-window.

    The UI offers a fixed set of windows (5/10/15 samples); gain/loss
    is precomputed for each at parse time and shipped as a list so
    switching the smoothing setting updates every stat line instantly
    with no refetch. ``half`` is in samples (±this many points); the
    physical meaning depends on the track's sampling rate, which is
    why :class:`TrackProfile` also ships ``sample_interval_s``.
    """

    half: int = Field(description="Smoothing half-window in samples (±this many points).")
    gain_m: float
    loss_m: float


class TrackSummary(BaseModel):
    """Lightweight track record returned to the UI's folder list.

    The point list is intentionally omitted — the UI fetches polyline
    points and the elevation profile via dedicated endpoints, so the
    folder list stays small even with many tracks. The metadata
    fields are inlined so the editor can populate without a second
    round-trip per track; they're tiny strings.
    """

    id: str
    name: str
    color: str
    points: int
    distance_km: float
    elev_gain_loss_m: list[ElevGainLoss]
    bbox: tuple[float, float, float, float] = Field(description="(west, south, east, north)")
    metadata_name: str | None = None
    metadata_desc: str | None = None
    metadata_author: str | None = None
    track_name: str | None = None
    track_desc: str | None = None


class TrackDetail(BaseModel):
    """Full track record returned to the map. Includes the polyline."""

    id: str
    name: str
    color: str
    distance_km: float
    elev_gain_loss_m: list[ElevGainLoss]
    bbox: tuple[float, float, float, float]
    points: list[Point]
    metadata_name: str | None = None
    metadata_desc: str | None = None
    metadata_author: str | None = None
    track_name: str | None = None
    track_desc: str | None = None


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
    # Gap-aware stats over the *real* elevations (None skipped). The
    # arrays above substitute 0.0 for gaps to stay index-aligned with
    # Plotly and the hover-sync, so recomputing these client-side from
    # elevations_m would count a dropout as a ~2000 m plunge. Ship the
    # server's numbers and let the UI display them directly — one source
    # of truth, matching the sidebar stat line exactly. Gain/loss ships
    # for every smoothing window so the UI can switch without a refetch.
    elev_gain_loss_m: list[ElevGainLoss]
    elev_min_m: float | None = Field(
        description="Lowest real elevation in metres, or null if no point has one."
    )
    elev_max_m: float | None = Field(
        description="Highest real elevation in metres, or null if no point has one."
    )
    sample_interval_s: int | None = Field(
        description="Median inter-point sampling interval in seconds (rounded), "
        "or null if the track has no <time> stamps. Shown in the stat line "
        "so the ±N-sample smoothing window can be read in real time."
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


# Max length of a single metadata field. Matches the cap enforced by
# ``gpx_loader.write_metadata``; pinned here so the OpenAPI schema
# matches the runtime check.
_METADATA_FIELD_MAX = 1000


class MetadataPatch(BaseModel):
    """Body of PATCH /api/tracks/{id}/metadata.

    Each field is optional: callers send only what the user edited.
    ``""`` (empty string) means "clear this field"; the server treats
    that as remove (the file will not contain the element). ``None``
    (the field is missing from the JSON body) is a no-op for the
    field. The two are easy to confuse in the UI; the editor's
    "Clear" button sends ``""``, not a missing field.
    """

    metadata_name: str | None = None
    metadata_desc: str | None = None
    metadata_author: str | None = None
    track_name: str | None = None
    track_desc: str | None = None

    @field_validator(
        "metadata_name", "metadata_desc", "metadata_author",
        "track_name", "track_desc",
    )
    @classmethod
    def _cap_length(cls, v: str | None) -> str | None:
        if v is not None and len(v) > _METADATA_FIELD_MAX:
            raise ValueError(
                f"metadata field is {len(v)} chars; max is {_METADATA_FIELD_MAX}"
            )
        return v


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


def _elev_gain_loss_models(t: gpx_loader.Track) -> list[ElevGainLoss]:
    """Build the wire-shape gain/loss list for a Track, one per window.

    Centralised so the folder list, track detail, profile, and PATCH
    response all round and order the windows identically. The dict on
    the Track is keyed by half; emit in ascending half order so the UI
    can index predictably if it ever wants to.
    """
    return [
        ElevGainLoss(half=h, gain_m=round(g, 1), loss_m=round(l, 1))
        for h, (g, l) in sorted(t.elev_gain_loss.items())
    ]


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that forces the browser to revalidate on every load.

    Starlette's StaticFiles sends ``Last-Modified`` and ``ETag`` but no
    ``Cache-Control``, so a browser falls back to *heuristic* caching —
    it may serve a stale ``app.js`` straight from its disk cache without
    revalidating, for an unpredictable window based on the file's age.
    That bit us when the elevation gain/loss was reshaped from two
    scalars (``elev_gain_m`` / ``elev_loss_m``) to a per-window list
    (``elev_gain_loss_m``): a browser that still had the old ``app.js``
    cached ran it against the new JSON, read the now-missing
    ``elev_gain_m`` as ``undefined``, and rendered ``↑ NaN m · ↓ NaN m``
    in every stat line. A hard refresh cleared it, but the class of bug
    (stale JS, fresh API) is silent and recurring.

    ``Cache-Control: no-cache`` doesn't mean "don't cache" — it means
    "revalidate before using". The browser sends
    ``If-Modified-Since`` / ``If-None-Match``; StaticFiles returns 304
    when the file is unchanged (sub-millisecond, no body) or 200 with
    the new bytes when it has changed. So a local dev viewer always
    runs the current static assets without paying a full re-download on
    every request. The tile endpoint (``/api/tiles/...``) is a separate
    route, not this mount, so tiles keep their own disk-cache path.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# ─── App factory ──────────────────────────────────────────────────────────────


def create_app(
    static_dir: Path | None = None,
    *,
    active_folder: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``static_dir`` is injected so tests can point at a temp folder; the
    production server (server_thread) uses the package's ``static/``.

    ``active_folder`` is the folder the server was started with. The
    CLI (``randonneur serve --directory <path>``) passes it in; tests
    inject a temp folder the same way. The folder is fixed for the
    server's lifetime — the UI shows it read-only, restart with a new
    path to change it.

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
        # If the app was created with a known folder (the CLI case),
        # point the watcher at it before the first HTTP request so the
        # hot-reload channel is live from the moment the UI connects.
        if active_folder is not None:
            await watcher.set_active_folder(_app, active_folder)
        try:
            yield
        finally:
            await watcher.shutdown(_app)

    app = FastAPI(title="randonneur", version="0.1.0", lifespan=lifespan)
    if active_folder is not None:
        # Stored in per-app state (keyed on id(app)) so multiple apps
        # in one process — only tests today — each have their own
        # folder. The handler below reads from this same state.
        _state_for(app).active_folder = active_folder

    @app.get("/api/folder", response_model=FolderResponse)
    async def api_folder() -> FolderResponse:
        # The folder is set at server start (``create_app(active_folder=...)``
        # / ``randonneur serve --directory <path>``) and lives in the
        # per-app state. A request handler is not the place to change it
        # — the folder is fixed for the server's lifetime. ``None`` means
        # "the server was started without --directory" (e.g. an embedded
        # test app); the UI then just shows the empty state.
        folder = _state_for(app).active_folder
        if folder is None:
            return FolderResponse(path=None, tracks=[])
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
                    elev_gain_loss_m=_elev_gain_loss_models(t),
                    bbox=t.bbox,
                    metadata_name=t.metadata_name,
                    metadata_desc=t.metadata_desc,
                    metadata_author=t.metadata_author,
                    track_name=t.track_name,
                    track_desc=t.track_desc,
                )
            )
        _log.info("Folder %s: %d track(s), %d error(s)", folder, len(tracks), len(errors))
        # The folder is already set on the watcher at startup (via
        # ``create_app`` / the lifespan), so this is a no-op when the
        # folder matches. Kept for the (currently dead) embedded-test
        # case where active_folder is set after the lifespan ran.
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
            elev_gain_loss_m=_elev_gain_loss_models(t),
            bbox=t.bbox,
            # Sending the full point list over the wire is fine for v1
            # — typical GPX files are a few hundred points; 10,000+ is
            # rare and still small in JSON. If a future user has 100k+
            # point tracks, downsample here.
            points=[Point(lat=p.lat, lon=p.lon, ele=p.ele) for p in t.points],
            metadata_name=t.metadata_name,
            metadata_desc=t.metadata_desc,
            metadata_author=t.metadata_author,
            track_name=t.track_name,
            track_desc=t.track_desc,
        )

    @app.get("/api/tracks/{track_id}/profile", response_model=TrackProfile)
    def api_track_profile(track_id: str) -> TrackProfile:
        t = _get_cached(app, track_id)
        distances_km, elevations_m = profile.compute_profile(t.points)
        elev_min, elev_max = gpx_loader._elevation_min_max(t.points)
        # TrackProfile is the Plotly-shaped payload. The arrays stay
        # raw (no downsampling) — Plotly handles 1k-10k points in
        # scatter mode fine; switch to scattergl in a future v2 if
        # anyone hits a real perf issue. The gap-aware stats are the
        # same numbers the sidebar shows (computed once at parse time
        # for gain/loss, here for min/max) so the two stat lines can't
        # drift — see the TrackProfile field comments for why the UI
        # must not recompute these from elevations_m.
        return TrackProfile(
            id=t.id,
            name=t.name,
            color=t.color,
            distances_km=distances_km,
            elevations_m=elevations_m,
            elev_gain_loss_m=_elev_gain_loss_models(t),
            elev_min_m=elev_min,
            elev_max_m=elev_max,
            sample_interval_s=t.sample_interval_s,
        )

    @app.patch("/api/tracks/{track_id}/metadata", response_model=TrackSummary)
    def api_patch_metadata(track_id: str, patch: MetadataPatch) -> TrackSummary:
        # Update the metadata fields of one GPX file on disk and
        # refresh the cache so the change is visible in the UI without
        # a folder reload. PATCH is the right verb (the editor sends
        # only the fields the user touched, not the whole document).
        #
        # The write is atomic (tmp + os.replace inside
        # gpx_loader.write_metadata); a crash mid-write leaves the
        # user's original file untouched. A parse failure on the
        # existing file (already-cached, but maybe the user just
        # edited it externally to something malformed) is propagated
        # as 422 — the UI shows it as a save error.
        current = _get_cached(app, track_id)
        try:
            updated = gpx_loader.write_metadata(
                current.path,
                metadata_name=patch.metadata_name,
                metadata_desc=patch.metadata_desc,
                metadata_author=patch.metadata_author,
                track_name=patch.track_name,
                track_desc=patch.track_desc,
            )
        except gpxpy.gpx.GPXException as e:
            raise HTTPException(
                status_code=422,
                detail=f"could not re-parse {current.path.name} after edit: {e}",
            )
        except ValueError as e:
            # Field too long. The pydantic validator usually catches
            # this first, but a direct call from a test path could
            # bypass the validator.
            raise HTTPException(status_code=422, detail=str(e))
        _state_for(app).track_cache[track_id] = updated
        return TrackSummary(
            id=updated.id,
            name=updated.name,
            color=updated.color,
            points=len(updated.points),
            distance_km=round(updated.distance_km, 3),
            elev_gain_loss_m=_elev_gain_loss_models(updated),
            bbox=updated.bbox,
            metadata_name=updated.metadata_name,
            metadata_desc=updated.metadata_desc,
            metadata_author=updated.metadata_author,
            track_name=updated.track_name,
            track_desc=updated.track_desc,
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
        # Served through _NoCacheStaticFiles so the browser always
        # revalidates static assets (see that class for the NaN bug this
        # prevents).
        app.mount(
            "/",
            _NoCacheStaticFiles(directory=str(static_dir), html=True),
            name="static",
        )

    return app


# ─── Server entry point (used by the CLI) ────────────────────────────────────


def static_files_dir() -> Path:
    """Path to the package's ``static/`` directory (HTML/JS/CSS)."""
    return Path(__file__).parent / "static"


__all__ = ["create_app", "static_files_dir"]
