"""Elevation profile computation.

Given a track's point list, produce two parallel arrays — cumulative
distance in km and elevation in metres — aligned by index, so a hover at
profile x = 4.2 km can be mapped back to the matching point on the
polyline.

The pair is what the plotly and leaflet sides of the two-way sync need.
Haversine (great-circle) is used for distance so the profile x-axis
matches what a paper map with a scale bar would show. 3D distance
differs by < 0.1% on a hike and isn't worth the extra math.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from randonneur.gpx_loader import Point

# Mean Earth radius in metres (IUGG). Good enough for hiking-scale
# distances where sub-10m accuracy is invisible at the map zoom the UI uses.
_EARTH_RADIUS_M = 6_371_008.8


# ─── Internals ────────────────────────────────────────────────────────────────


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


# ─── Public API ───────────────────────────────────────────────────────────────


def compute_profile(points: Sequence[Point]) -> tuple[list[float], list[float]]:
    """Return ``(distances_km, elevations_m)`` aligned by index.

    Both lists have the same length as ``points``. ``distances[0]`` is
    always 0.0 (no distance has been travelled at the first sample);
    ``distances[i]`` is the cumulative great-circle distance from point
    0 to point *i* in kilometres.

    Elevations are passed through from the points, with ``None`` becoming
    ``0.0``. Why not skip them: the arrays must be index-aligned with the
    point list (the two-way sync relies on this), so dropping points would
    desync the profile. A real GPS elevation dropout is small (a few
    seconds of recording); substituting 0 is wrong but not visually
    misleading at hiking zooms, and avoids carrying a third array.

    No smoothing is applied — see the comment in the body for why.
    """
    n = len(points)
    if n == 0:
        return [], []

    distances: list[float] = [0.0] * n
    cum_m = 0.0
    for i in range(1, n):
        prev, cur = points[i - 1], points[i]
        cum_m += _haversine_m(prev.lat, prev.lon, cur.lat, cur.lon)
        distances[i] = cum_m / 1000.0

    # `points[i].ele` may be None for GPS dropouts. The profile arrays
    # must be aligned 1:1 with `points` (the two-way sync uses the index
    # of the hovered point to find the matching polyline coordinate), so
    # we substitute 0.0 for missing samples. Smoothing would mask these
    # dropouts but also smear real features; we keep the data honest and
    # let a future v2 add a flag for "smooth missing points only".
    elevations: list[float] = [p.ele if p.ele is not None else 0.0 for p in points]

    return distances, elevations
