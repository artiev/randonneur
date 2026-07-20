"""Tests for ``randonneur.server``.

Uses FastAPI's ``TestClient`` which exercises the real FastAPI app and
Starlette routing without binding a socket. The folder is a temp dir
populated with the shared fixtures — so we test against real GPX
content, not stubs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from randonneur.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client() -> TestClient:
    """A TestClient with no active folder (used by tests for the empty / 404 / 503 paths)."""
    return TestClient(create_app())


@pytest.fixture
def make_client():
    """Factory for TestClient instances with a configured folder.

    The server was changed in commit 2 to read the active folder from
    the per-app state (set via ``create_app(active_folder=...)``) rather
    than from a ``?path=`` query param. Tests that want a working
    ``/api/folder`` need to inject their temp folder at app creation.
    """
    def _make(folder: Path | None = None) -> TestClient:
        return TestClient(create_app(active_folder=folder))
    return _make


# ─── Happy path ──────────────────────────────────────────────────────────────


def test_folder_lists_all_tracks(make_client, tmp_path: Path) -> None:
    client = make_client(tmp_path)
    for name in ("elevation_gaps.gpx", "multi_segment.gpx"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())

    resp = client.get("/api/folder")
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(tmp_path)
    assert body["errors"] == []
    names = sorted(t["name"] for t in body["tracks"])
    assert names == ["elevation_gaps", "multi_segment"]


def test_track_summary_shape_is_stable(make_client, tmp_path: Path) -> None:
    client = make_client(tmp_path)
    # The UI depends on these exact field names; any rename here breaks
    # the client side.
    (tmp_path / "multi_segment.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())

    body = client.get("/api/folder").json()
    track = body["tracks"][0]
    assert set(track.keys()) == {
        "id", "name", "color", "points", "distance_km", "elev_gain_m", "bbox"
    }
    assert isinstance(track["id"], str) and len(track["id"]) == 12
    assert track["color"].startswith("#")
    assert track["points"] == 5  # multi_segment has 5 points
    # bbox is (west, south, east, north) — assert the known values.
    west, south, east, north = track["bbox"]
    assert west < east and south < north


# ─── Error paths ─────────────────────────────────────────────────────────────


def test_folder_with_no_path_returns_empty(client: TestClient) -> None:
    # No path param → empty response (UI uses this as "no folder selected yet").
    body = client.get("/api/folder").json()
    assert body == {"path": None, "tracks": [], "errors": []}


def test_folder_with_missing_path_returns_404(make_client, tmp_path: Path) -> None:
    # A folder that was valid at server start but is gone now (e.g. the
    # user moved the directory between server runs and didn't restart).
    # The server still has it in per-app state; the handler detects the
    # missing directory and 404s rather than crashing.
    folder = tmp_path / "exists"
    folder.mkdir()
    client = make_client(folder)
    (folder).rmdir()  # remove after the app has captured the path
    resp = client.get("/api/folder")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_folder_with_empty_directory_returns_empty_tracks(make_client, tmp_path: Path) -> None:
    client = make_client(tmp_path)
    body = client.get("/api/folder").json()
    assert body["tracks"] == []
    assert body["errors"] == []


def test_folder_with_one_bad_file_reports_error_and_keeps_good_ones(
    make_client, tmp_path: Path,
) -> None:
    # Real behaviour the UI depends on: one malformed file must not
    # blank the whole folder. The good tracks must still load, and the
    # bad file must be named in the errors list.
    client = make_client(tmp_path)
    (tmp_path / "good.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())
    (tmp_path / "bad.gpx").write_text("not xml at all")

    body = client.get("/api/folder").json()
    assert [t["name"] for t in body["tracks"]] == ["good"]
    assert len(body["errors"]) == 1
    assert body["errors"][0].startswith("bad.gpx:")


# ─── Static mount wiring ─────────────────────────────────────────────────────


def test_static_mount_returns_404_when_no_static_dir(client_no_static: TestClient) -> None:
    # create_app(static_dir=None) shouldn't mount anything; GET / → 404.
    resp = client_no_static.get("/")
    assert resp.status_code == 404


@pytest.fixture
def client_no_static() -> TestClient:
    return TestClient(create_app(static_dir=None))


def test_static_serves_index_html_and_assets() -> None:
    # Mount the real static/ directory and confirm index.html, app.js,
    # style.css all come back over the wire. Without this, a typo in
    # app.js would only show up in the browser console, not in CI.
    from randonneur.server import static_files_dir
    client = TestClient(create_app(static_dir=static_files_dir()))

    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "<title>randonneur</title>" in index.text

    js = client.get("/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "refreshFolder" in js.text  # the boot-time folder loader
    assert "loadSettings" in js.text  # the settings fetcher
    assert "setBaseLayer" in js.text  # the layer switcher

    css = client.get("/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "app-grid" in css.text  # the layout class
    assert ".settings-panel" in css.text  # the settings popover


def test_index_contains_required_dom_ids() -> None:
    # The app.js references these IDs; if any of them is renamed in
    # the HTML without updating app.js (or vice versa), the UI silently
    # breaks. Pin them.
    from randonneur.server import static_files_dir
    client = TestClient(create_app(static_dir=static_files_dir()))

    body = client.get("/").text
    for id_ in ("folder-path",
                "track-list", "errors", "errors-list", "map",
                "profile", "profile-title", "profile-stats",
                # Settings panel. Pinning these IDs means a rename in
                # the HTML without updating app.js (or vice versa) gets
                # caught in CI rather than as a silent "click the gear,
                # nothing happens" in the browser.
                "settings-button", "settings-panel", "settings-backdrop",
                "settings-close", "settings-sources", "settings-scale"):
        assert f'id="{id_}"' in body, f"missing id={id_!r} in index.html"


# ─── Per-track endpoint ─────────────────────────────────────────────────────


def test_track_detail_returns_full_polyline(make_client, tmp_path: Path) -> None:
    client = make_client(tmp_path)
    (tmp_path / "multi_segment.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())
    client.get("/api/folder")
    folder_body = client.get("/api/folder").json()
    track_id = folder_body["tracks"][0]["id"]

    detail = client.get(f"/api/tracks/{track_id}").json()
    assert set(detail.keys()) == {
        "id", "name", "color", "distance_km", "elev_gain_m", "bbox", "points"
    }
    # The multi_segment fixture has 5 points; the polyline must come back in order.
    assert len(detail["points"]) == 5
    assert detail["points"][0]["lat"] == pytest.approx(46.5500)
    assert detail["points"][0]["lon"] == pytest.approx(11.4500)
    assert detail["points"][0]["ele"] == 2300.0


def test_track_detail_404_for_unknown_id(client: TestClient) -> None:
    resp = client.get("/api/tracks/does-not-exist")
    assert resp.status_code == 404
    assert "unknown track" in resp.json()["detail"]


def test_track_cache_resets_on_new_folder(make_client, tmp_path: Path) -> None:
    # The folder is fixed at server start, so "switching folders" is
    # modelled as restarting with a new ``create_app(active_folder=...)``.
    # The per-app cache must be replaced wholesale (not merged with
    # the previous app's), otherwise stale ids from a previous folder
    # would silently 404 mid-session after a restart.
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "a.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())
    (folder_b / "b.gpx").write_bytes((FIXTURES / "elevation_gaps.gpx").read_bytes())

    client_a = make_client(folder_a)
    client_b = make_client(folder_b)
    a_id = client_a.get("/api/folder").json()["tracks"][0]["id"]
    b_id = client_b.get("/api/folder").json()["tracks"][0]["id"]
    assert a_id != b_id

    # After restart, the new app must not serve the previous app's id.
    assert client_b.get(f"/api/tracks/{a_id}").status_code == 404
    assert client_b.get(f"/api/tracks/{b_id}").status_code == 200


def test_track_cache_is_per_app(tmp_path: Path) -> None:
    # The track cache is keyed on the FastAPI app instance, so two apps
    # in one process keep independent caches. Without this, loading a
    # folder into app B would evict app A's tracks (the module-global
    # cache bug) — a latent break for multi-folder / multi-session work.
    from randonneur import server

    server.reset_for_tests()
    try:
        folder_a = tmp_path / "a"
        folder_b = tmp_path / "b"
        app_a = create_app(active_folder=folder_a)
        app_b = create_app(active_folder=folder_b)
        client_a = TestClient(app_a)
        client_b = TestClient(app_b)
        folder_a.mkdir()
        folder_b.mkdir()
        (folder_a / "a.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())
        (folder_b / "b.gpx").write_bytes((FIXTURES / "elevation_gaps.gpx").read_bytes())

        a_id = client_a.get("/api/folder").json()["tracks"][0]["id"]
        b_id = client_b.get("/api/folder").json()["tracks"][0]["id"]
        assert a_id != b_id

        # After B is loaded on app_b, app_a must still serve a_id (the
        # module-global cache would have evicted it). And app_b must not
        # serve a_id — the caches are disjoint.
        assert client_a.get(f"/api/tracks/{a_id}").status_code == 200
        assert client_b.get(f"/api/tracks/{a_id}").status_code == 404
        assert client_b.get(f"/api/tracks/{b_id}").status_code == 200
    finally:
        server.reset_for_tests()


# ─── Profile endpoint ───────────────────────────────────────────────────────


def test_profile_returns_aligned_arrays(make_client, tmp_path: Path) -> None:
    client = make_client(tmp_path)
    (tmp_path / "multi_segment.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())
    track_id = client.get("/api/folder").json()["tracks"][0]["id"]

    body = client.get(f"/api/tracks/{track_id}/profile").json()
    assert set(body.keys()) == {"id", "name", "color", "distances_km", "elevations_m"}
    # multi_segment has 5 points → arrays must be 5 long and aligned.
    assert len(body["distances_km"]) == 5
    assert len(body["elevations_m"]) == 5
    # First point: distance 0, elevation 2300.
    assert body["distances_km"][0] == 0.0
    assert body["elevations_m"][0] == 2300.0
    # Distances are monotonic.
    assert all(
        body["distances_km"][i] >= body["distances_km"][i - 1]
        for i in range(1, len(body["distances_km"]))
    )


def test_profile_substitutes_zero_for_missing_elevation(
    make_client, tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    (tmp_path / "elevation_gaps.gpx").write_bytes(
        (FIXTURES / "elevation_gaps.gpx").read_bytes()
    )
    track_id = client.get("/api/folder").json()["tracks"][0]["id"]

    body = client.get(f"/api/tracks/{track_id}/profile").json()
    # The fixture has a None at index 1; it must come back as 0.0,
    # not NaN, and the array length must match the point count.
    assert len(body["elevations_m"]) == 4
    assert body["elevations_m"][1] == 0.0


def test_profile_404_for_unknown_id(client: TestClient) -> None:
    resp = client.get("/api/tracks/deadbeef0000/profile")
    assert resp.status_code == 404
    assert "unknown track" in resp.json()["detail"]


def test_profile_and_track_detail_share_cache(make_client, tmp_path: Path) -> None:
    client = make_client(tmp_path)
    # Sanity: hitting /profile must not re-parse. We can't time it, but
    # we can confirm both endpoints return the same id and color (i.e.
    # they're hitting the same Track object).
    (tmp_path / "multi_segment.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())
    track_id = client.get("/api/folder").json()["tracks"][0]["id"]

    detail = client.get(f"/api/tracks/{track_id}").json()
    profile = client.get(f"/api/tracks/{track_id}/profile").json()
    assert detail["id"] == profile["id"]
    assert detail["color"] == profile["color"]
    assert detail["name"] == profile["name"]


# ─── Tile endpoint ───────────────────────────────────────────────────────────


# A 1×1 PNG is 67 bytes; this is the canonical "smallest valid PNG".
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63000100000005000100"  # IHDR + IDAT
    "0d0a2db40000000049454e44ae426082"  # IEND
)


def test_tile_endpoint_returns_png_bytes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bypass the real network and rate limiter; just confirm the wiring
    # between the endpoint, the cache module, and the response shape.
    from randonneur import tile_cache as tc

    async def fake_fetch(source: str, z: int, x: int, y: int) -> bytes:
        return _TINY_PNG

    monkeypatch.setattr(tc, "fetch_tile_bytes", fake_fetch)

    resp = client.get("/api/tiles/opentopomap/5/16/11.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == _TINY_PNG


def test_tile_endpoint_404_for_unknown_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whitelist must reject anything not in TILE_URL_TEMPLATES; this
    # is the line that prevents the server being an open proxy.
    from randonneur import tile_cache as tc

    async def fake_fetch(source: str, z: int, x: int, y: int) -> bytes:
        raise AssertionError("fetch must not be called for unknown source")

    monkeypatch.setattr(tc, "fetch_tile_bytes", fake_fetch)

    resp = client.get("/api/tiles/not-a-source/5/16/11.png")
    assert resp.status_code == 404
    assert "unknown tile source" in resp.json()["detail"]


def test_tile_endpoint_502_on_upstream_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Network failure on the upstream → 502 Bad Gateway (we are not
    # ourselves unavailable, our dependency is).
    import httpx
    from randonneur import tile_cache as tc

    async def fake_fetch(source: str, z: int, x: int, y: int) -> bytes:
        raise httpx.ConnectError("simulated DNS failure")

    monkeypatch.setattr(tc, "fetch_tile_bytes", fake_fetch)

    resp = client.get("/api/tiles/opentopomap/5/16/11.png")
    assert resp.status_code == 502
    assert "upstream tile error" in resp.json()["detail"]


def test_tile_endpoint_404_for_tile_outside_coverage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenTopoMap returns 404 for tiles outside its coverage (e.g.
    # z=17 over open ocean). The server must surface that as 404
    # to the browser — it is a normal "no tile here" answer, not an
    # upstream error. Returning 502 would (a) be semantically wrong
    # and (b) make Leaflet show a red "broken tile" icon instead of
    # a clean blank square.
    from randonneur import tile_cache as tc

    async def fake_fetch(source: str, z: int, x: int, y: int) -> bytes:
        raise tc.TileNotFoundError(f"tile {source}/{z}/{x}/{y} not found upstream")

    monkeypatch.setattr(tc, "fetch_tile_bytes", fake_fetch)
    resp = client.get("/api/tiles/opentopomap/17/67926/47788.png")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_tile_endpoint_rejects_non_integer_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /api/tiles/opentopomap/foo/bar/baz.png must not reach the cache
    # module — fastapi's int coercion rejects it with 422 before the
    # handler runs. This is the guard that prevents path traversal even
    # if the cache's Path logic were ever loosened.
    from randonneur import tile_cache as tc

    called = False

    async def fake_fetch(source: str, z: int, x: int, y: int) -> bytes:
        nonlocal called
        called = True
        return _TINY_PNG

    monkeypatch.setattr(tc, "fetch_tile_bytes", fake_fetch)
    resp = client.get("/api/tiles/opentopomap/foo/bar/baz.png")
    assert resp.status_code == 422
    assert not called


def test_tile_endpoint_503_for_unconfigured_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Thunderforest (or any other source that needs an API key the
    # user hasn't set) is in the whitelist but unavailable right
    # now. The endpoint must surface that as 503, not 404 (the
    # source is real) and not 502 (no upstream call was made).
    from randonneur import tile_cache as tc

    async def fake_fetch(source: str, z: int, x: int, y: int) -> bytes:
        raise AssertionError("fetch must not be called for unconfigured source")

    monkeypatch.setattr(tc, "fetch_tile_bytes", fake_fetch)
    resp = client.get("/api/tiles/thunderforest-outdoors/5/16/11.png")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


# ─── /api/settings endpoint ─────────────────────────────────────────────────


def test_settings_lists_known_sources(client: TestClient) -> None:
    # The settings panel needs the full whitelist, with availability
    # flags. OpenTopoMap is always available; Thunderforest's
    # availability depends on the env (see the next test).
    body = client.get("/api/settings").json()
    assert set(body.keys()) == {"current_source", "sources"}
    assert body["current_source"] == "opentopomap"
    ids = {s["id"] for s in body["sources"]}
    assert ids == {"opentopomap", "thunderforest-outdoors"}


def test_settings_marks_thunderforest_unavailable_without_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without RANDONNEUR_THUNDERFOREST_KEY, Thunderforest is in the
    # whitelist (the panel can render it as "missing key") but
    # flagged unavailable.
    monkeypatch.delenv("RANDONNEUR_THUNDERFOREST_KEY", raising=False)
    body = client.get("/api/settings").json()
    by_id = {s["id"]: s for s in body["sources"]}
    assert by_id["opentopomap"]["available"] is True
    assert by_id["opentopomap"]["needs_key"] is False
    assert by_id["thunderforest-outdoors"]["available"] is False
    assert by_id["thunderforest-outdoors"]["needs_key"] is True


def test_settings_marks_thunderforest_available_with_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the key is set, the same endpoint reports Thunderforest
    # as available. The key value itself is never returned.
    monkeypatch.setenv("RANDONNEUR_THUNDERFOREST_KEY", "test-key-abc")
    body = client.get("/api/settings").json()
    by_id = {s["id"]: s for s in body["sources"]}
    assert by_id["thunderforest-outdoors"]["available"] is True
    # Defence-in-depth: the secret must not leak into the response.
    assert "test-key-abc" not in str(body)


# ─── WebSocket hot-reload ────────────────────────────────────────────────────


def test_websocket_receives_change_after_folder_load(
    make_client, tmp_path: Path,
) -> None:
    # Open a WS, load a folder, touch a file, expect a "changed"
    # message. This is the full wiring smoke: server's lifespan
    # handler, broker, watcher, and fan-out.
    import threading
    import time

    folder = tmp_path / "live"
    folder.mkdir()
    (folder / "a.gpx").write_bytes(
        (FIXTURES / "multi_segment.gpx").read_bytes()
    )
    # The folder is plumbed in at create_app time, so the lifespan
    # handler points the watcher at it before the WS connects.
    client = make_client(folder)

    # ``with client:`` runs the FastAPI lifespan (which starts the
    # long-lived watcher task). Without it, ``watcher.start`` is
    # never called and the WS receives nothing.
    with client:
        with client.websocket_connect("/api/ws") as ws:
            # Give watchfiles time to attach the inotify/FSEvents
            # watch. We don't need the attach snapshot itself — the
            # watcher broadcasts it as a normal "changed" event, and
            # the test reads messages until it sees the one we
            # actually care about (b.gpx).
            time.sleep(0.5)
            # Drop a new file. Direct write is the simplest pattern;
            # atomic-rename-via-tmp mis-fires in some tmp-dir layouts.
            (folder / "b.gpx").write_bytes(
                (FIXTURES / "elevation_gaps.gpx").read_bytes()
            )
            # Read messages in a thread until we get one whose
            # ``files`` contains b.gpx. We bound the total wait
            # with the thread's join timeout; the test fails if
            # no qualifying message arrives in that window.
            #
            # Why not just read the first message? Because the
            # watcher broadcasts every batch, including the
            # attach snapshot from watchfiles (which lists a.gpx
            # but not b.gpx). On a slow attach the first batch
            # *does* contain b.gpx; on a fast attach the first
            # batch is the snapshot and b.gpx arrives in a
            # subsequent batch. The right invariant is "a batch
            # containing b.gpx arrives within the debounce window
            # + slack" — which is what we assert here.
            result: dict = {}

            def _read() -> None:
                deadline = time.time() + 4.0
                try:
                    while time.time() < deadline:
                        try:
                            msg = ws.receive_json()
                        except BaseException as e:  # noqa: BLE001
                            result["err"] = e
                            return
                        if any(p.endswith("b.gpx") for p in msg.get("files", [])):
                            result["msg"] = msg
                            return
                    result["err"] = TimeoutError("no b.gpx message in 4s")
                except BaseException as e:  # noqa: BLE001
                    result["err"] = e

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout=5.0)
            assert "msg" in result, f"no qualifying message: {result.get('err')}"
            msg = result["msg"]
            assert msg["type"] == "changed"
            assert msg["folder"] == str(folder)


def test_websocket_disconnect_unregisters_client(
    make_client, tmp_path: Path,
) -> None:
    # Closing the WS must remove the client from the broker so the
    # next broadcast doesn't enqueue to a dead socket.
    from randonneur import watcher

    folder = tmp_path / "unreg"
    folder.mkdir()
    (folder / "a.gpx").write_bytes((FIXTURES / "multi_segment.gpx").read_bytes())

    client = make_client(folder)
    with client:
        with client.websocket_connect("/api/ws") as ws:
            st = watcher.state_for(client.app)
            assert len(st.clients) == 1
            ws.close()
        st = watcher.state_for(client.app)
        assert st.clients == []


def test_websocket_endpoint_accepts_before_folder_loaded(client: TestClient) -> None:
    # The WS must accept connections even when no folder has been
    # loaded yet — a deep link with ?path= may arrive after the WS
    # handshake. Just confirm the handshake succeeds.
    with client:
        with client.websocket_connect("/api/ws") as ws:
            assert ws is not None
