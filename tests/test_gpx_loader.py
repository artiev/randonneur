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


# ─── _elevation_gain_loss / _elevation_min_max edge cases ─────────────────────
#
# The fixture above covers one path (a single interior gap). The helper
# is the single source of the sidebar's gain/loss and the profile pane's
# min/max, so its edge cases matter: empty input, a lone point, all-None
# samples, monotonic runs, flats, and gaps at the ends. We call the
# helpers directly with constructed Points so the tests don't depend on
# hand-building a GPX fixture per case.


def _pts(*eles: float | None) -> list[gpx_loader.Point]:
    """Build a point list from an elevation sequence (lat/lon arbitrary)."""
    return [gpx_loader.Point(lat=0.0, lon=float(i), ele=e) for i, e in enumerate(eles)]


def test_gain_loss_empty_points() -> None:
    assert gpx_loader._elevation_gain_loss([]) == (0.0, 0.0)


def test_gain_loss_single_point() -> None:
    # No deltas to sum → both zero.
    assert gpx_loader._elevation_gain_loss(_pts(1500.0)) == (0.0, 0.0)


def test_gain_loss_all_none() -> None:
    # No real samples → nothing to climb or descend.
    assert gpx_loader._elevation_gain_loss(_pts(None, None, None)) == (0.0, 0.0)


def test_gain_loss_monotonic_ascent_only() -> None:
    gain, loss = gpx_loader._elevation_gain_loss(_pts(100.0, 200.0, 300.0))
    assert gain == pytest.approx(200.0)
    assert loss == pytest.approx(0.0)


def test_gain_loss_monotonic_descent_only() -> None:
    gain, loss = gpx_loader._elevation_gain_loss(_pts(300.0, 200.0, 100.0))
    assert gain == pytest.approx(0.0)
    assert loss == pytest.approx(200.0)


def test_gain_loss_flat_is_zero() -> None:
    assert gpx_loader._elevation_gain_loss(_pts(500.0, 500.0, 500.0)) == (0.0, 0.0)


def test_gain_loss_mixed_up_and_down_independently() -> None:
    # 100 → 250 (+150) → 200 (-50) → 230 (+30) → 180 (-50).
    # gain = 150 + 30 = 180; loss = 50 + 50 = 100.
    gain, loss = gpx_loader._elevation_gain_loss(_pts(100.0, 250.0, 200.0, 230.0, 180.0))
    assert gain == pytest.approx(180.0)
    assert loss == pytest.approx(100.0)


def test_gain_loss_leading_gap_ignores_it() -> None:
    # None, None, 100, 200 → the first real sample seeds prev; only
    # the 100→200 delta counts. The leading Nones contribute nothing.
    gain, loss = gpx_loader._elevation_gain_loss(_pts(None, None, 100.0, 200.0))
    assert gain == pytest.approx(100.0)
    assert loss == pytest.approx(0.0)


def test_gain_loss_trailing_gap_ignores_it() -> None:
    # 100, 200, None, None → the 100→200 delta counts; trailing Nones
    # don't manufacture a plunge to 0.
    gain, loss = gpx_loader._elevation_gain_loss(_pts(100.0, 200.0, None, None))
    assert gain == pytest.approx(100.0)
    assert loss == pytest.approx(0.0)


def test_gain_loss_consecutive_gaps_bridge_to_next_real() -> None:
    # 100, None, None, 130 → the delta bridges across the gap to the
    # next real sample: +30 of gain, not a -100 loss then +130 gain.
    # This is the gap-skipping rule that keeps the sidebar and the
    # profile stat line in agreement (see the server profile endpoint).
    gain, loss = gpx_loader._elevation_gain_loss(_pts(100.0, None, None, 130.0))
    assert gain == pytest.approx(30.0)
    assert loss == pytest.approx(0.0)


def test_min_max_skips_none() -> None:
    lo, hi = gpx_loader._elevation_min_max(_pts(2000.0, None, 2050.0, 2030.0))
    assert lo == pytest.approx(2000.0)
    assert hi == pytest.approx(2050.0)


def test_min_max_empty_or_all_none_is_none_pair() -> None:
    assert gpx_loader._elevation_min_max([]) == (None, None)
    assert gpx_loader._elevation_min_max(_pts(None, None)) == (None, None)


def test_min_max_single_real_sample() -> None:
    lo, hi = gpx_loader._elevation_min_max(_pts(None, 1234.0, None))
    assert lo == pytest.approx(1234.0)
    assert hi == pytest.approx(1234.0)


# ─── smoothing (GPS jitter correction) ────────────────────────────────────────
#
# Real GPS tracks carry sub-metre elevation jitter that inflates a raw
# delta-sum (a 3007-point hike: 643/632 m raw, 436/423 m smoothed).
# _elevation_gain_loss smooths the series with a centred moving average
# before summing, but only when there are enough samples to fill the
# window — short tracks take the raw path (the 4-point fixture above
# still pins 50/20). These tests cover the smoothing layer directly.


def test_smooth_averages_a_window_and_is_centred() -> None:
    # A single 100 m spike in the middle of a long flat series: the
    # centred average spreads the spike over ±_ELEV_SMOOTH_HALF
    # samples. Far from the spike the output is back to the flat
    # 1000 m; at the spike it's raised toward 1100 but averaged down
    # by its neighbours, so it never reaches the raw 1100.
    half = gpx_loader._ELEV_SMOOTH_HALF
    n = 1000
    eles = [1000.0] * n
    eles[n // 2] = 1100.0
    out = gpx_loader._smooth_elevations(eles)
    assert out[0] == pytest.approx(1000.0)              # outside the spike's reach
    assert out[n // 2 + half + 1] == pytest.approx(1000.0)  # one past the window edge
    assert out[n // 2] > 1000.0                          # spike raises its sample
    assert out[n // 2] < 1100.0                          # ...but not to the raw value


def test_smooth_bridges_short_dropout_but_skips_long_gap() -> None:
    # A 3-sample None dropout is shorter than the ±10 window, so the
    # neighbours' real elevations bleed in and the gap is interpolated
    # (no None in the output). A 30-sample gap is longer than the
    # window, so its middle has no real sample in range → None, which
    # _sum_gain_loss then skips.
    half = gpx_loader._ELEV_SMOOTH_HALF
    short = [1000.0] + [None] * 3 + [1100.0]
    out = gpx_loader._smooth_elevations(short)
    assert all(o is not None for o in out)  # bridged
    long = [1000.0] + [None] * 30 + [1000.0]
    out_l = gpx_loader._smooth_elevations(long)
    assert out_l[len(long) // 2] is None  # gap too wide to bridge
    # And the gain/loss sum skips the None run (no phantom plunge).
    assert gpx_loader._sum_gain_loss(out_l) == (0.0, 0.0)


def test_smoothing_reduces_jitter_inflated_gain() -> None:
    # A steady 100 m climb (0.1 m/step over 1000 samples) buried under
    # ±1 m sinusoidal jitter. The raw delta-sum counts every up-jitter
    # as climbing (~350 m of "gain"); the smoothed sum recovers the
    # real ~100 m ramp. This is the whole point of the smoothing layer.
    import math
    n = 1000
    eles = [0.1 * i + 1.0 * math.sin(i) for i in range(n)]
    raw_gain, _ = gpx_loader._sum_gain_loss(eles)
    smoothed_gain, smoothed_loss = gpx_loader._sum_gain_loss(
        gpx_loader._smooth_elevations(eles)
    )
    assert raw_gain > 2 * smoothed_gain      # jitter inflated the raw sum
    assert smoothed_gain == pytest.approx(100.0, abs=5.0)  # real ramp recovered
    assert smoothed_loss == pytest.approx(0.0, abs=5.0)    # no real descent


def test_short_series_takes_raw_path_not_smoothed() -> None:
    # Fewer samples than the window (2*half+1 = 21) → no smoothing, so
    # a short series with a real climb reports the raw gain, not the
    # window mean (which would flatten it to ~0). Guards the rule that
    # keeps the 4-point elevation_gaps fixture honest.
    half = gpx_loader._ELEV_SMOOTH_HALF
    # 5-point steady climb of 40 m, well under the 21-sample window.
    pts = _pts(1000.0, 1010.0, 1020.0, 1030.0, 1040.0)
    assert len(pts) < 2 * half + 1
    gain, loss = gpx_loader._elevation_gain_loss(pts)
    assert gain == pytest.approx(40.0)
    assert loss == pytest.approx(0.0)


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
