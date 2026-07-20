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
    """A parsed GPX track, flattened across all segments of all tracks in the file.

    The display-layer metadata (``track_name`` / ``track_desc`` /
    ``metadata_name`` / ``metadata_desc`` / ``metadata_author``) is read
    at parse time and shipped on the wire so the UI can show the
    current values without a second parse. ``name`` stays as the file
    stem — the sidebar uses it, the polyline tooltip uses it, and it's
    the only name that's guaranteed to be non-empty (every file has a
    stem; not every file has a ``<metadata>`` or ``<trk><name>``). The
    GPX-side display name lives in ``track_name`` and is shown in the
    metadata editor only.
    """

    id: str  # stable hash of the absolute path
    name: str  # filename stem
    path: Path  # absolute path
    color: str  # hex color from the deterministic palette
    points: tuple[Point, ...]
    bbox: tuple[float, float, float, float]  # (west, south, east, north)
    distance_km: float
    elev_gain_m: float  # sum of positive elevation deltas only
    elev_loss_m: float  # sum of negative elevation deltas, as a positive magnitude
    # GPX metadata (from <metadata>). The first <trk>'s <name>/<desc>
    # are exposed as track_name/track_desc; multi-track files would
    # need an extra API call to enumerate, but every fixture in this
    # project is single-track.
    metadata_name: str | None = None
    metadata_desc: str | None = None
    metadata_author: str | None = None
    track_name: str | None = None
    track_desc: str | None = None


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


def _elevation_gain_loss(points: list[Point]) -> tuple[float, float]:
    """Sum of positive and negative elevation deltas in metres.

    Returns ``(gain, loss)`` where gain is total ascent and loss is
    total descent as a *positive* magnitude (the absolute sum of the
    negative deltas). Gaps (None) are skipped without counting the gap
    as either — this matches what hikers care about: actual climbed and
    descended metres, not noise from missing samples. One pass over the
    points so gain and loss share the same gap-skipping rule and can't
    drift apart.
    """
    gain = 0.0
    loss = 0.0
    prev: float | None = None
    for p in points:
        if p.ele is None:
            continue
        if prev is not None:
            dy = p.ele - prev
            if dy > 0:
                gain += dy
            elif dy < 0:
                loss += -dy
        prev = p.ele
    return gain, loss


def _elevation_min_max(points: list[Point]) -> tuple[float | None, float | None]:
    """Lowest and highest real elevation in metres.

    Returns ``(min, max)`` over the non-``None`` samples, or ``(None, None)``
    when no point has an elevation. Gaps are skipped for the same reason
    as in :func:`_elevation_gain_loss`: a GPS dropout is a missing sample,
    not a 0 m reading, so it must never show up as a bogus ``0`` minimum
    on the profile stat line. The caller renders an em dash for the range
    when both are ``None`` rather than a misleading ``0–0 m``.
    """
    real = [p.ele for p in points if p.ele is not None]
    if not real:
        return (None, None)
    return (min(real), max(real))


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
    elev_gain, elev_loss = _elevation_gain_loss(points)
    # GPX display metadata. We grab the first <trk> only — multi-track
    # files are rare in practice (and the loader already flattens
    # across them), so "first trk wins" is the obvious rule. None vs
    # empty-string: gpxpy normalises a missing element to None, so an
    # empty input round-trips as None — we follow that.
    first_track = gpx.tracks[0] if gpx.tracks else None
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
        elev_gain_m=elev_gain,
        elev_loss_m=elev_loss,
        metadata_name=gpx.name or None,
        metadata_desc=gpx.description or None,
        metadata_author=gpx.author_name or None,
        track_name=(first_track.name if first_track else None) or None,
        track_desc=(first_track.description if first_track else None) or None,
    )


# Cap on each metadata field. A 1000-char description is already a
# wall of text; anything longer is almost certainly a paste error, and
# a malicious client could OOM the server by sending megabytes. 1000
# is generous and matches the limit the metadata editor's <textarea>
# uses as a maxlength hint.
_METADATA_FIELD_MAX = 1000


def _set_or_clear(obj: object, attr: str, value: str | None) -> None:
    """Assign ``value`` to ``obj.attr`` if non-None; assign None otherwise.

    gpxpy 1.6.x's ``__delattr__`` is overzealous and crashes on
    ``del gpx.name`` for fields that were never set, so we use the
    ``None``-as-clear convention that gpxpy's ``to_xml`` honours (an
    attribute set to None is omitted from the output). An empty
    string still emits an empty element — the editor's "clear"
    affordance therefore goes through this path, not a direct
    ``""`` assignment.
    """
    setattr(obj, attr, value)


def write_metadata(
    path: Path,
    *,
    metadata_name: str | None = None,
    metadata_desc: str | None = None,
    metadata_author: str | None = None,
    track_name: str | None = None,
    track_desc: str | None = None,
) -> Track:
    """Update the metadata blocks of ``path`` and return the re-parsed Track.

    The path is read, the four fields are written into the parsed
    ``GPX`` (gpxpy's setters know how to emit the right element), and
    the result is serialised via ``to_xml()`` and atomically replaced
    on disk (``tmp + os.replace`` so a crash mid-write can't truncate
    the user's file). The new content is then re-parsed so the caller
    has a fresh :class:`Track` to put in the cache.

    All five parameters are optional; the caller typically sends only
    the fields the user actually edited. An empty string is normalised
    to "remove"; ``None`` is a no-op for the field.

    Raises :class:`gpxpy.gpx.GPXException` on malformed input; any
    string longer than ``_METADATA_FIELD_MAX`` raises :class:`ValueError`.
    """
    for value, field in (
        (metadata_name, "metadata_name"),
        (metadata_desc, "metadata_desc"),
        (metadata_author, "metadata_author"),
        (track_name, "track_name"),
        (track_desc, "track_desc"),
    ):
        if value is not None and len(value) > _METADATA_FIELD_MAX:
            raise ValueError(
                f"{field} is {len(value)} chars; max is {_METADATA_FIELD_MAX}"
            )

    with open(path, "rb") as f:
        gpx = gpxpy.parse(f)

    # Top-level <metadata>. gpxpy exposes ``name`` / ``description`` /
    # ``author_name`` as direct attributes; the author element is
    # created lazily when ``author_name`` is set. We normalise the
    # string-or-None contract here: the PATCH endpoint treats "" as
    # "clear", and we want that to land as a None assignment on the
    # gpxpy object so the element is omitted from to_xml() output
    # (assigning "" would emit an empty <name>, which is the wrong
    # shape — it's not the same as a missing field).
    if metadata_name is not None:
        _set_or_clear(gpx, "name", metadata_name or None)
    if metadata_desc is not None:
        _set_or_clear(gpx, "description", metadata_desc or None)
    if metadata_author is not None:
        # Author is a nested element; gpxpy auto-creates an Author
        # object on first assignment to ``author_name``. Same set-or-
        # clear rule: empty string normalises to None, which removes
        # the <author> block from the output.
        gpx.author_name = metadata_author or None

    if gpx.tracks:
        first = gpx.tracks[0]
        if track_name is not None:
            _set_or_clear(first, "name", track_name or None)
        if track_desc is not None:
            _set_or_clear(first, "description", track_desc or None)

    # Atomic write: tmp + os.replace. A crash between the tmp write
    # and the replace leaves the user's file untouched; the tmp file
    # may dangle but is small and gets overwritten on the next save.
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".randonneur-", suffix=".gpx", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(gpx.to_xml())
        os.replace(tmp, path)
    except BaseException:
        # Clean up the tmp on any failure (parse-error, disk-full,
        # permission). The user's file is the one we must protect.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return parse(path)
