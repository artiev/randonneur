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


def test_parse_elev_gain_and_loss_skips_gap() -> None:
    # Elevations: 2000, None, 2050, 2030.
    # Gain: 2050-2000 = 50 (the None gap is skipped, not counted as ascent).
    # Loss: 2030-2050 = -20 → 20 m of descent (a positive magnitude).
    track = gpx_loader.parse(FIXTURES / "elevation_gaps.gpx")
    assert track.elev_gain_m == pytest.approx(50.0)
    assert track.elev_loss_m == pytest.approx(20.0)


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


# ─── metadata ────────────────────────────────────────────────────────────────


def test_parse_extracts_metadata_from_top_level_block() -> None:
    # with_metadata.gpx has a <metadata> with <name>, <desc>, and
    # <author><name>. The dataclass must surface all three.
    track = gpx_loader.parse(FIXTURES / "with_metadata.gpx")
    assert track.metadata_name == "Tour du Mont Blanc"
    assert track.metadata_desc is not None and "France" in track.metadata_desc
    assert track.metadata_author == "Some Hiker"


def test_parse_extracts_first_trk_name_and_desc() -> None:
    # We grab the *first* <trk>'s <name>/<desc> (the loader flattens
    # multi-track files into one Track). Multi-track fixtures are
    # out of scope; single-track is the rule the editor follows.
    track = gpx_loader.parse(FIXTURES / "with_metadata.gpx")
    assert track.track_name == "Day 1: Les Houches to Les Contamines"
    assert track.track_desc is not None and "Voza" in track.track_desc


def test_parse_metadata_defaults_to_none_when_absent() -> None:
    # The original multi_segment fixture has no <metadata> block and
    # no <author>. The new fields must default to None, not "" (the
    # PATCH endpoint treats "" as "clear", which would round-trip
    # wrongly if the field started as None).
    track = gpx_loader.parse(FIXTURES / "multi_segment.gpx")
    assert track.metadata_name is None
    assert track.metadata_desc is None
    assert track.metadata_author is None
    assert track.track_name == "Multi-segment test"  # <trk><name> is set
    assert track.track_desc is None


def test_write_metadata_updates_file_and_reparses(tmp_path: Path) -> None:
    # write_metadata returns a fresh Track; the file on disk is
    # updated so a subsequent parse() returns the same values. Round-
    # trip is the contract.
    src = FIXTURES / "with_metadata.gpx"
    target = tmp_path / "rt.gpx"
    target.write_bytes(src.read_bytes())

    updated = gpx_loader.write_metadata(
        target,
        metadata_name="New name",
        track_desc="New trk desc",
    )
    assert updated.metadata_name == "New name"
    assert updated.track_desc == "New trk desc"
    # Other fields are untouched when not sent.
    assert updated.metadata_author == "Some Hiker"
    # Re-parsing the file from disk must agree.
    again = gpx_loader.parse(target)
    assert again.metadata_name == "New name"
    assert again.track_desc == "New trk desc"
    assert again.metadata_author == "Some Hiker"


def test_write_metadata_clear_with_empty_string(tmp_path: Path) -> None:
    # Empty string means "remove" (no <name> in the output). The
    # editor's "Clear" button sends ""; this is the rule.
    target = tmp_path / "clear.gpx"
    target.write_bytes((FIXTURES / "with_metadata.gpx").read_bytes())

    updated = gpx_loader.write_metadata(
        target,
        metadata_name="",
        track_name="",
    )
    assert updated.metadata_name is None
    assert updated.track_name is None
    # Other fields are unaffected.
    assert updated.metadata_author == "Some Hiker"


def test_write_metadata_unset_field_is_noop(tmp_path: Path) -> None:
    # None means "don't touch this field" (the editor sends only the
    # fields the user edited). Sending only metadata_name must not
    # clobber the existing author.
    target = tmp_path / "noop.gpx"
    target.write_bytes((FIXTURES / "with_metadata.gpx").read_bytes())

    updated = gpx_loader.write_metadata(target, metadata_name="Only name changed")
    assert updated.metadata_name == "Only name changed"
    assert updated.metadata_desc is not None and "France" in updated.metadata_desc
    assert updated.metadata_author == "Some Hiker"
    assert updated.track_name == "Day 1: Les Houches to Les Contamines"


def test_write_metadata_rejects_oversized_field(tmp_path: Path) -> None:
    # The 1000-char cap is enforced inside write_metadata too, not
    # only by the pydantic validator. Belt-and-braces: a direct call
    # (e.g. from a test) gets the same ValueError.
    target = tmp_path / "big.gpx"
    target.write_bytes((FIXTURES / "with_metadata.gpx").read_bytes())
    with pytest.raises(ValueError, match="metadata_desc is"):
        gpx_loader.write_metadata(target, metadata_desc="x" * 1001)


def test_write_metadata_leaves_original_on_failure(tmp_path: Path) -> None:
    # If the write fails (here: path is a directory), the user's
    # original file must still be on disk, untouched.
    target = tmp_path / "willfail.gpx"
    target.write_bytes((FIXTURES / "with_metadata.gpx").read_bytes())
    original_bytes = target.read_bytes()
    bad = tmp_path / "is_a_dir"
    bad.mkdir()
    with pytest.raises(Exception):
        gpx_loader.write_metadata(bad, metadata_name="will fail")
    # The original file is byte-identical.
    assert target.read_bytes() == original_bytes
