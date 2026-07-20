"""Tests for ``randonneur.profile``.

The ``profile_handcheck.gpx`` fixture is designed so the expected
cumulative distances are computable by hand: each segment is either a
pure north move (lon constant) or a pure east move (lat constant), and
at the equator 0.001 deg lat ≈ 111.32 m. The E-W moves are short enough
that the small cos(lat) correction is within the tolerance.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from randonneur import gpx_loader, profile

FIXTURES = Path(__file__).parent / "fixtures"

# The arc length of 0.001 deg on a sphere of radius 6,371,008.8 m is
# exactly 111.19508 m. The widely-cited "111.32 m per degree" comes from
# the WGS84 meridional radius and is what you'd see on a paper map; the
# haversine (great-circle) function in profile.py is sphere-based, so
# the *function's* answer for 0.001 deg of north at the equator is
# 111.195 m. The test below asserts the function's value with a tight
# relative tolerance that catches any regression in the formula without
# coupling to whichever mean-radius convention we pick later.
_ARC_KM_0_001_N = 111.19508 / 1000.0  # expected haversine for 0.001° N at lat 0


def _load(name: str) -> tuple[list[float], list[float]]:
    """Convenience: load a fixture and return its (distances, elevations)."""
    track = gpx_loader.parse(FIXTURES / name)
    return profile.compute_profile(track.points)


# ─── Shape & alignment ────────────────────────────────────────────────────────


def test_empty_points_returns_empty_arrays() -> None:
    assert profile.compute_profile([]) == ([], [])


def test_single_point_has_zero_distance_and_its_elevation() -> None:
    from randonneur.gpx_loader import Point
    distances, elevations = profile.compute_profile([Point(lat=0.0, lon=0.0, ele=500.0)])
    assert distances == [0.0]
    assert elevations == [500.0]


def test_output_arrays_are_same_length_as_input() -> None:
    # Multi-segment fixture has 5 points → arrays must be 5 long.
    distances, elevations = _load("multi_segment.gpx")
    assert len(distances) == 5
    assert len(elevations) == 5


# ─── Distance semantics ──────────────────────────────────────────────────────


def test_distance_starts_at_zero() -> None:
    distances, _ = _load("profile_handcheck.gpx")
    assert distances[0] == 0.0


def test_cumulative_distance_is_monotonic_nondecreasing() -> None:
    # Monotonic non-decreasing on real data — haversine is always ≥ 0.
    distances, _ = _load("profile_handcheck.gpx")
    for prev, cur in zip(distances, distances[1:]):
        assert cur >= prev


def test_cumulative_distance_matches_hand_computed_north_segment() -> None:
    # First move: (0,0) → (0.001, 0). Pure N. ~111.195 m ≈ 0.1112 km.
    distances, _ = _load("profile_handcheck.gpx")
    assert distances[1] == pytest.approx(_ARC_KM_0_001_N, rel=1e-6)


def test_cumulative_distance_is_sum_of_segments() -> None:
    # d[3] = d[1] + (segment 1→2: east at lat 0.001) + (segment 2→3: north again).
    # The E-W leg's length is cos(lat) * 0.001-deg-arc, but applied to the
    # haversine convention (which is what compute_profile uses).
    distances, _ = _load("profile_handcheck.gpx")
    east_segment_km = math.cos(math.radians(0.001)) * _ARC_KM_0_001_N
    assert distances[3] == pytest.approx(
        _ARC_KM_0_001_N + east_segment_km + _ARC_KM_0_001_N, rel=1e-6
    )


# ─── Elevation semantics ─────────────────────────────────────────────────────


def test_elevations_pass_through_in_order() -> None:
    _, elevations = _load("profile_handcheck.gpx")
    # 100, 110, 120, 130 from the fixture.
    assert elevations == [100.0, 110.0, 120.0, 130.0]


def test_missing_elevation_becomes_zero_not_nan() -> None:
    # The elevation_gaps fixture has a None at index 1. We must not
    # produce NaN — that would break Plotly. The arrays must stay aligned
    # with the point list (see # why in profile.py), so we substitute 0.0
    # and accept that the user might see a small dip at a GPS dropout.
    _, elevations = _load("elevation_gaps.gpx")
    assert elevations[1] == 0.0
    # And the array is still 4 long, aligned with the 4 points.
    assert len(elevations) == 4
