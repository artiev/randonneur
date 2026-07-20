"""Tests for ``randonneur.gpx_loader``.

The fixtures are minimal by design: a 4-point single-segment file with an
elevation gap, and a 2-segment / 5-point file. The point is to exercise
the *interesting* branches (segment flattening, ``None`` elevation) without
maintaining brittle hand-computed expected outputs for the bbox/distance
fields — those are checked for sanity, not exact values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from randonneur import gpx_loader

FIXTURES = Path(__file__).parent / "fixtures"


# ─── discover ─────────────────────────────────────────────────────────────────


def test_discover_finds_both_fixtures(tmp_path: Path) -> None:
    # Copy the fixtures into a temp folder so the test doesn't depend on
    # cwd or absolute layout. Sorting is part of the contract.
    for name in ("elevation_gaps.gpx", "multi_segment.gpx"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())

    found = gpx_loader.discover(tmp_path)
    assert [p.name for p in found] == ["elevation_gaps.gpx", "multi_segment.gpx"]


def test_discover_finds_files_in_subdirectories(tmp_path: Path) -> None:
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "a.gpx").write_bytes((FIXTURES / "elevation_gaps.gpx").read_bytes())
    (tmp_path / "b.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())

    found = gpx_loader.discover(tmp_path)
    assert len(found) == 2
    assert all(p.suffix == ".gpx" for p in found)


def test_discover_returns_empty_for_missing_folder(tmp_path: Path) -> None:
    # discover() must not raise on a missing folder — the server decides
    # whether "no such folder" is a hard error.
    assert gpx_loader.discover(tmp_path / "does-not-exist") == []


def test_discover_ignores_non_gpx_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a gpx")
    (tmp_path / "track.gpx").write_bytes((FIXTURES / "elevation_gaps.gpx").read_bytes())
    assert [p.name for p in gpx_loader.discover(tmp_path)] == ["track.gpx"]


# ─── parse ───────────────────────────────────────────────────────────────────


def test_parse_single_segment_with_elevation_gap() -> None:
    track = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")

    assert track.name == "elevation_gaps"
    assert len(track.points) == 4
    # 2nd point has no <ele> — it must come through as None, not as 0.
    assert track.points[0].ele == 2000.0
    assert track.points[1].ele is None
    assert track.points[2].ele == 2050.0
    assert track.points[3].ele == 2030.0


def test_parse_elev_gain_skips_gap_and_counts_only_ascents() -> None:
    # Elevations: 2000, None, 2050, 2030.
    # Ascents: 2050-2000 = 50. The 2030 is a descent (no gain).
    # The None gap must not be counted as ascent.
    track = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")
    assert track.elev_gain_m == pytest.approx(50.0)


def test_parse_multi_segment_flattens_in_order() -> None:
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")

    # 3 points in segment 1, 2 in segment 2 = 5 total, in file order.
    assert len(track.points) == 5
    assert track.points[0].lat == pytest.approx(46.5500)
    assert track.points[2].lat == pytest.approx(46.5520)
    assert track.points[3].lat == pytest.approx(46.6000)  # first of segment 2
    assert track.points[4].lat == pytest.approx(46.6010)


def test_parse_bbox_is_west_south_east_north() -> None:
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    west, south, east, north = track.bbox
    # Multi-segment: lats 46.55..46.601, lons 11.45..11.501.
    assert west == pytest.approx(11.45)
    assert south == pytest.approx(46.55)
    assert east == pytest.approx(11.501)
    assert north == pytest.approx(46.601)


def test_parse_id_is_stable_across_calls() -> None:
    # The same path must yield the same id every time — that's what
    # makes the UI's selected-state survive a reload.
    a = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")
    b = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")
    assert a.id == b.id
    assert len(a.id) == 12  # sha1[:12]


def test_parse_color_is_deterministic() -> None:
    a = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")
    b = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    # Different files → different ids → different colors.
    assert a.id != b.id
    assert a.color != b.color
    # And re-parsing the same file always gives the same color.
    assert a.color == gpx_loader.parse(FIXTURES / "elevation_gaps.gpx").color


def test_parse_color_is_from_palette() -> None:
    track = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")
    assert track.color.startswith("#")
    assert len(track.color) == 7


def test_parse_distance_is_positive() -> None:
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    # Sanity — we don't pin the exact number (gpxpy's length_3d
    # incorporates a small vertical component). Just confirm it ran
    # and produced a sensible magnitude.
    assert track.distance_km > 0.0
    assert track.distance_km < 10.0  # 5 nearby points can't be 10 km
