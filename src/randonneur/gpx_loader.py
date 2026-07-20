"""GPX discovery and parsing.

``discover`` finds every ``*.gpx`` under a folder (sorted, deterministic);
``parse`` reads one file and flattens its tracks/segments into a single
list of ``(lat, lon, ele)`` points plus a small set of summary stats the
UI needs (bbox, length, elevation gain, stable id, deterministic color).

Discovery and parsing are separate on purpose: discovery is cheap and
tolerant of permission errors; parsing can fail on malformed XML and the
caller (the server) needs to see the failure per file so the UI can show
"could not parse foo.gpx" rather than silently dropping the whole folder.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import gpxpy
import gpxpy.gpx

# ─── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Point:
    """A single point on a track. ``ele`` is metres or ``None`` if missing."""

    lat: float
    lon: float
    ele: float | None


@dataclass(frozen=True)
class Track:
    """A parsed GPX track, flattened across all segments of all tracks in the file."""

    id: str  # stable hash of the absolute path
    name: str  # filename stem
    path: Path  # absolute path
    color: str  # hex color from the deterministic palette
    points: tuple[Point, ...]
    bbox: tuple[float, float, float, float]  # (west, south, east, north)
    distance_km: float
    elev_gain_m: float  # sum of positive elevation deltas only


# ─── Constants ────────────────────────────────────────────────────────────────

# 12-color palette — visually distinct on the map and on the OpenTopoMap
# basemap. Picked from a colour-blind-friendly set; no two adjacent hues
# share a luminance band.
_PALETTE: tuple[str, ...] = (
    "#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42be65",
    "#f032e6", "#9A6324", "#469990", "#800000", "#808000", "#000075",
)

_GPX_SUFFIX = ".gpx"


# ─── Internals ────────────────────────────────────────────────────────────────


def _track_id(path: Path) -> str:
    """Stable, short id for a track file.

    Why sha1 of the absolute path (not a random uuid): the same file must
    map to the same id across runs so the UI's selected-state survives a
    reload. 12 hex chars are enough entropy to avoid collisions in any
    personal-sized folder.
    """
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]


def _track_color(track_id: str) -> str:
    """Deterministic color from the id so the same file always renders the same color."""
    return _PALETTE[int(track_id[:4], 16) % len(_PALETTE)]


def _flatten_points(gpx: gpxpy.gpx.GPX) -> list[Point]:
    """Flatten tracks → segments → points into a single ordered list.

    Multi-track and multi-segment files are concatenated in the order
    gpxpy yields them. gpxpy already returns points in the order they
    appear in the file, which matches the order a GPS recorded them.
    """
    out: list[Point] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                out.append(Point(lat=p.latitude, lon=p.longitude, ele=p.elevation))
    return out


def _bbox(points: list[Point]) -> tuple[float, float, float, float]:
    """(west, south, east, north) over a list of points.

    A single-point list collapses to a zero-area bbox at that point —
    callers (Leaflet) handle that fine.
    """
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    west = min(p.lon for p in points)
    east = max(p.lon for p in points)
    south = min(p.lat for p in points)
    north = max(p.lat for p in points)
    return (west, south, east, north)


def _elev_gain(points: list[Point]) -> float:
    """Sum of positive elevation deltas in metres.

    Gaps (None) are skipped without counting the gap as ascent. This
    matches what hikers care about: actual climbed metres, not noise
    from missing samples.
    """
    gain = 0.0
    prev: float | None = None
    for p in points:
        if p.ele is None:
            continue
        if prev is not None and p.ele > prev:
            gain += p.ele - prev
        prev = p.ele
    return gain


# ─── Public API ───────────────────────────────────────────────────────────────


def discover(folder: Path) -> list[Path]:
    """Return all ``*.gpx`` files under ``folder`` (recursive), sorted.

    Sorted so the UI's track list is stable across runs and the color
    assignment is predictable. Missing folders return an empty list
    rather than raising — the server is the one that decides whether
    "folder not found" is a hard error.
    """
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob(f"*{_GPX_SUFFIX}") if p.is_file())


def parse(path: Path) -> Track:
    """Parse one GPX file into a :class:`Track`.

    Raises :class:`gpxpy.gpx.GPXException` (or its subclasses) on malformed
    XML or empty files. The caller is expected to surface the error to
    the UI; this function is the single place where GPX parsing happens.
    """
    # gpxpy.parse wants either an XML string or a file *object*, not a
    # path. Passing str(path) here once caused a confusing
    # "not well-formed (invalid token): line 1, column 5" — it was
    # parsing the path string as XML. Use an open() handle.
    with open(path, "rb") as f:
        gpx = gpxpy.parse(f)
    points = _flatten_points(gpx)
    track_id = _track_id(path)
    return Track(
        id=track_id,
        name=path.stem,
        path=path.resolve(),
        color=_track_color(track_id),
        points=tuple(points),
        bbox=_bbox(points),
        # gpxpy.length_3d sums 3D segment length — close enough to the
        # haversine distance for a folder-list summary. The profile
        # endpoint recomputes distance with its own haversine
        # (profile.py) so the profile x-axis aligns exactly with the
        # point array; the two numbers can differ by a fraction of a
        # percent and that's fine.
        distance_km=gpx.length_3d() / 1000.0,
        elev_gain_m=_elev_gain(points),
    )
