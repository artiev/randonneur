"""Mirror of the JS profile-→-map sync math, runnable in Python.

The JavaScript ``latLonAtDistance`` and ``upperBoundIndex`` in
``app.js`` are what drive the profile-hover crosshair on the map. We
can't run a browser in CI, so this file ports the *exact* same
algorithm and exercises it against the real GPX fixtures. If the JS
ever drifts, this test won't catch it — but it will catch every
edge case in the algorithm that the JS code was derived from, and
the JS code is small enough that visual review keeps them in sync.

Per the behaviour file: a real browser run is the only proof the UI
works end-to-end. This is the "exercise the math against real data"
layer under that.
"""

from __future__ import annotations

from pathlib import Path

from randonneur import gpx_loader, profile

FIXTURES = Path(__file__).parent / "fixtures"


def upper_bound_index(arr: list[float], x: float) -> int:
    """Index of the largest element in ``arr`` that is <= ``x``.

    Returns -1 if ``x < arr[0]``. ``arr`` must be non-decreasing.
    """
    if not arr or x < arr[0]:
        return -1
    lo, hi = 0, len(arr) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if arr[mid] <= x:
            lo = mid
        else:
            hi = mid - 1
    return lo


def lat_lon_at_distance(pts, dists, distance_km):
    """Linear-interpolate (lat, lon) at a cumulative distance.

    Returns ``None`` if the distance is out of range or the inputs
    are too short. Mirrors ``latLonAtDistance`` in ``app.js``; if you
    change one, change the other.
    """
    if len(dists) < 2 or len(pts) != len(dists):
        return None
    if distance_km < dists[0] or distance_km > dists[-1]:
        return None
    i = upper_bound_index(dists, distance_km)
    if i < 0:
        return None
    if i == len(dists) - 1 or dists[i] == distance_km:
        return (pts[i].lat, pts[i].lon)
    d0, d1 = dists[i], dists[i + 1]
    t_frac = (distance_km - d0) / (d1 - d0) if d1 != d0 else 0
    return (
        pts[i].lat + t_frac * (pts[i + 1].lat - pts[i].lat),
        pts[i].lon + t_frac * (pts[i + 1].lon - pts[i].lon),
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_upper_bound_index_finds_correct_position() -> None:
    # Standard mid-array.
    assert upper_bound_index([1, 2, 3, 4, 5], 3) == 2
    # x equals an interior element.
    assert upper_bound_index([1, 2, 3, 4, 5], 4) == 3
    # x below the first element.
    assert upper_bound_index([1, 2, 3], 0) == -1
    # x above the last element clamps to the last index.
    assert upper_bound_index([1, 2, 3], 99) == 2
    # x equal to the last element.
    assert upper_bound_index([1, 2, 3], 3) == 2
    # Empty array: -1.
    assert upper_bound_index([], 0) == -1


def test_lat_lon_at_distance_exact_hit_on_interior_point() -> None:
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    dists, _ = profile.compute_profile(track.points)
    ll = lat_lon_at_distance(track.points, dists, dists[1])
    assert ll == (track.points[1].lat, track.points[1].lon)


def test_lat_lon_at_distance_midpoint_of_diagonal_segment() -> None:
    # The first segment of multi_segment is diagonal: lat AND lon change.
    # p0=(46.5500, 11.4500), p1=(46.5510, 11.4510) → midpoint (46.5505, 11.4505).
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    dists, _ = profile.compute_profile(track.points)
    ll = lat_lon_at_distance(track.points, dists, dists[1] / 2)
    assert ll is not None
    assert abs(ll[0] - 46.5505) < 1e-9
    assert abs(ll[1] - 11.4505) < 1e-9


def test_lat_lon_at_distance_last_point_exact_hit() -> None:
    # Special case: i == len-1 must not underflow when accessing i+1.
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    dists, _ = profile.compute_profile(track.points)
    ll = lat_lon_at_distance(track.points, dists, dists[-1])
    assert ll == (track.points[-1].lat, track.points[-1].lon)


def test_lat_lon_at_distance_crosses_segment_boundary() -> None:
    # multi_segment has a ~6.5 km jump between points 2 and 3 (segments
    # reset). The interpolation must NOT span that gap as a straight
    # line — point 3 is the first of a new segment and the
    # function must return it exactly when distance == dists[3].
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    dists, _ = profile.compute_profile(track.points)
    ll = lat_lon_at_distance(track.points, dists, dists[3])
    assert ll == (track.points[3].lat, track.points[3].lon)


def test_lat_lon_at_distance_out_of_range_returns_none() -> None:
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    dists, _ = profile.compute_profile(track.points)
    assert lat_lon_at_distance(track.points, dists, 99.0) is None
    assert lat_lon_at_distance(track.points, dists, -1.0) is None


def test_lat_lon_at_distance_rejects_short_inputs() -> None:
    # < 2 points → None (can't interpolate).
    from randonneur.gpx_loader import Point
    assert lat_lon_at_distance([Point(0, 0, 0)], [0.0], 0.0) is None
    assert lat_lon_at_distance([], [], 0.0) is None
    # Mismatched lengths → None.
    assert lat_lon_at_distance(
        [Point(0, 0, 0), Point(1, 1, 1)], [0.0, 1.0, 2.0], 0.5
    ) is None
