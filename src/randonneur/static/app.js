/* ─── randonneur UI client ──────────────────────────────────────────────────
 *
 * Vanilla JS, no framework. The IIFE below holds all module state and
 * is the only place in the file that uses `let`/`const`. It wires the
 * Leaflet map and the Plotly elevation profile into a single selection
 * flow, with a two-way hover crosshair between them.
 *
 * Folder selection: the folder is fixed for the server's lifetime
 * (``randonneur serve --directory <path>``). The UI just shows the
 * active path read-only in the header and re-fetches /api/folder when
 * the watcher fires a change.
 */

(() => {
  "use strict";

  // ─── State ────────────────────────────────────────────────────────────────

  /** @type {Array<object>} list returned by GET /api/folder */
  let tracks = [];
  /** @type {string|null} id of the currently focused track */
  let selectedTrackId = null;
  /** @type {Set<string>} collapsed subfolder keys in the TRACKS panel.
   * In-memory only: survives hot-reload re-fetches (refreshFolder doesn't
   * touch it) but resets on a full page reload. A key present means the
   * group's track rows are hidden; absent means expanded (the default). */
  let collapsedFolders = new Set();
  /** @type {string|null} currently loaded folder path (from /api/folder) */
  let currentFolder = null;
  /** @type {Map<string, object>} id → fetched track detail (with polyline) */
  const trackDetails = new Map();
  /** @type {Map<string, L.Polyline>} id → Leaflet polyline on the map */
  const polylines = new Map();
  /** @type {Map<string, L.CircleMarker>} id → crosshair marker on the map */
  const crosshairs = new Map();
  /** @type {Map<string, object>} id → fetched profile payload */
  const profileData = new Map();
  /** @type {WebSocket|null} hot-reload channel; null when not connected. */
  let hotReloadWs = null;
  /** @type {Array<{id: string, name: string, available: boolean, needs_key: boolean}>} */
  let tileSources = [];
  /** @type {string} id of the active base layer. */
  let currentSource = "opentopomap";
  /** @type {L.TileLayer|null} the active tile layer. Replaced on switch. */
  let baseLayer = null;
  /** @type {L.Control.Scale|null} the scale bar, or null when hidden. */
  let scaleControl = null;
  /** @type {boolean} whether the scale bar is on. */
  let showScaleBar = true;
  /** @type {number} elevation smoothing half-window in samples (5/10/15).
   *  Gain/loss is precomputed for each on the server and shipped as a
   *  list; this picks which one the stat lines display. In-memory only
   *  (matches the other settings — no localStorage). */
  let smoothHalf = 10;

  // ─── DOM refs (resolved once on load) ────────────────────────────────────

  const folderPath = document.getElementById("folder-path");
  const folderStatus = document.getElementById("folder-status");
  const trackList = document.getElementById("track-list");
  const tracksCount = document.getElementById("tracks-count");
  const errorsSection = document.getElementById("errors");
  const errorsList = document.getElementById("errors-list");
  const mapEl = document.getElementById("map");
  const profileEl = document.getElementById("profile");
  const profileTitle = document.getElementById("profile-title");
  const profileStats = document.getElementById("profile-stats");
  const tabButtonSettings = document.getElementById("tab-button-settings");
  const tabButtonEdit = document.getElementById("tab-button-edit");
  const sidePanel = document.getElementById("side-panel");
  const sidePanelClose = document.getElementById("side-panel-close");
  const sidePanelTitle = document.getElementById("side-panel-title");
  const settingsSources = document.getElementById("settings-sources");
  const settingsScale = document.getElementById("settings-scale");
  const settingsSmooth = document.getElementById("settings-smooth");
  // Metadata editor. The editor lives in the Edit view of the right-side
  // tab; the fieldset is shown by renderMetadataEditor (when a track is
  // selected) and hidden by clearMetadataEditor when the selection clears.
  // #metadata-empty is the no-track-selected placeholder shown in its place.
  const metadataGroup = document.getElementById("metadata-group");
  const metadataEmpty = document.getElementById("metadata-empty");
  const metadataTarget = document.getElementById("metadata-target");
  const metadataTrackName = document.getElementById("metadata-track-name");
  const metadataTrackDesc = document.getElementById("metadata-track-desc");
  const metadataMetaName = document.getElementById("metadata-meta-name");
  const metadataMetaDesc = document.getElementById("metadata-meta-desc");
  const metadataMetaAuthor = document.getElementById("metadata-meta-author");
  const metadataSave = document.getElementById("metadata-save");
  const metadataClear = document.getElementById("metadata-clear");
  const metadataStatus = document.getElementById("metadata-status");

  // ─── Map (Leaflet) ───────────────────────────────────────────────────────

  // Default view: somewhere central in the Alps (the typical randonneur
  // target). Auto-fit to the first folder load overwrites this.
  const map = L.map(mapEl, { zoomControl: true, attributionControl: true })
    .setView([46.5, 11.4], 12);

  // Per-source attribution strings. Hard-coded here rather than sent
  // over the wire from /api/settings: the panel only needs to know
  // the source's *name* and *availability*, and the attribution
  // contract is a UI concern. OpenTopoMap and Thunderforest both
  // require attribution per their tile-usage policies.
  const ATTRIBUTION = {
    "opentopomap":
      'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ' +
      '<a href="https://opentopomap.org">SRTM</a> | ' +
      'Map style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
    "thunderforest-outdoors":
      'Maps &copy; <a href="https://www.thunderforest.com">Thunderforest</a>, ' +
      'data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  };

  function buildBaseLayer(sourceId) {
    // Construct (but don't add) a tile layer for the given source.
    // The URL points at the server proxy, not the upstream — that's
    // the whole point of /api/tiles (rate limit, cache, key hiding).
    return L.tileLayer(`/api/tiles/${sourceId}/{z}/{x}/{y}.png`, {
      maxZoom: 17,
      attribution: ATTRIBUTION[sourceId] || "",
    });
  }

  function setBaseLayer(sourceId) {
    // Swap the active tile layer in-place. We don't remove the
    // existing one until the new one is built, so the map never
    // flashes blank between switches. L.tileLayer's tiles are
    // already cached on the server per source, so the new layer
    // starts showing tiles from the first visible viewport.
    if (baseLayer && currentSource === sourceId) return;
    if (!tileSources.find((s) => s.id === sourceId && s.available)) {
      // Refuse to switch to a known-unavailable source. The radio
      // in the panel should already be disabled, but defend in depth.
      return;
    }
    const newLayer = buildBaseLayer(sourceId);
    newLayer.addTo(map);
    if (baseLayer) baseLayer.remove();
    baseLayer = newLayer;
    currentSource = sourceId;
  }

  setBaseLayer(currentSource);

  // ─── Rendering ───────────────────────────────────────────────────────────

  function renderTrackList() {
    trackList.innerHTML = "";
    // Update the count next to the "Tracks" header. Empty string
    // when there's no folder (avoids a lonely "(0)" before the
    // user has loaded anything).
    tracksCount.textContent = tracks.length > 0 ? `(${tracks.length})` : "";
    if (tracks.length === 0) {
      const li = document.createElement("li");
      li.className = "empty-hint";
      li.textContent = currentFolder
        ? "No GPX files in this folder."
        : "No folder loaded.";
      trackList.appendChild(li);
      return;
    }
    // Group tracks by subfolder. The backend sorts the folder list by
    // full path, so tracks in the same subfolder are already contiguous —
    // emit a collapsible group header when the subfolder changes. Root
    // tracks (subfolder "") list with no header. The `tracks` array stays
    // flat (this is a render-only grouping) so every other spot that does
    // tracks.find()/.some()/.map() by id keeps working unchanged.
    const groupCounts = new Map();
    for (const t of tracks) {
      groupCounts.set(t.subfolder, (groupCounts.get(t.subfolder) || 0) + 1);
    }
    let currentGroup = null;
    for (const t of tracks) {
      if (t.subfolder !== currentGroup) {
        currentGroup = t.subfolder;
        if (t.subfolder) {
          trackList.appendChild(makeGroupHeader(t.subfolder, groupCounts.get(t.subfolder)));
        }
      }
      trackList.appendChild(makeTrackRow(t));
    }
  }

  function makeGroupHeader(subfolder, count) {
    const li = document.createElement("li");
    li.className = "track-group-header";
    li.dataset.folder = subfolder;
    if (collapsedFolders.has(subfolder)) li.classList.add("collapsed");
    const label = document.createElement("span");
    label.className = "track-group-label";
    label.textContent = subfolder;
    const n = document.createElement("span");
    n.className = "track-group-count";
    n.textContent = `(${count})`;
    li.append(label, n);
    // Clicking the header toggles the group's collapse state and
    // re-renders. State lives in the module-level `collapsedFolders`
    // set, which survives hot-reload re-fetches but resets on a full
    // page reload.
    li.addEventListener("click", () => {
      if (collapsedFolders.has(subfolder)) collapsedFolders.delete(subfolder);
      else collapsedFolders.add(subfolder);
      renderTrackList();
    });
    return li;
  }

  function makeTrackRow(t) {
    const li = document.createElement("li");
    li.dataset.trackId = t.id;
    if (t.id === selectedTrackId) li.classList.add("selected");
    if (t.subfolder) li.classList.add("in-group");
    // Hide the row when its group is collapsed. `hidden` (display:none)
    // keeps refreshStats' li[data-track-id] selector from touching visible
    // rows only — updating a hidden row's .stats is harmless.
    if (t.subfolder && collapsedFolders.has(t.subfolder)) li.hidden = true;

    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = t.color;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = t.name;

    const stats = document.createElement("span");
    stats.className = "stats";
    stats.textContent = formatStats(t);

    li.append(swatch, name, stats);
    li.addEventListener("click", () => selectTrack(t.id));
    return li;
  }

  function renderErrors(errors) {
    if (!errors || errors.length === 0) {
      errorsSection.hidden = true;
      errorsList.innerHTML = "";
      return;
    }
    errorsSection.hidden = false;
    errorsList.innerHTML = "";
    for (const msg of errors) {
      const li = document.createElement("li");
      li.textContent = msg;
      errorsList.appendChild(li);
    }
  }

  function setStatus(text, isError = false) {
    // Surfaces folder-load status (errors, "no folder configured")
    // in the sidebar's folder-status line. The right-side folder
    // status line in the header is gone — the track count moved
    // to the sidebar header.
    folderStatus.textContent = text;
    folderStatus.classList.toggle("error", isError);
  }

  // ─── Map / profile two-way sync ────────────────────────────────────────

  // Plotly's `relayout` shape on the profile; we move it (or hide it)
  // rather than redrawing the whole plot. The shape is added the first
  // time we draw a profile, then reused.
  const PROFILE_SHAPE_INDEX = 0;

  function ensureProfileCrosshair(data) {
    // Plotly.react with the new trace resets the shapes array, so we
    // add the shape here every time we render a profile. Single shape
    // per plot is enough — the crosshair is one vertical line.
    if (!profileEl.layout) return;
    const shape = {
      type: "line",
      xref: "x",
      yref: "paper",  // span the full plot height regardless of y range
      x0: 0, x1: 0,
      y0: 0, y1: 1,
      line: { color: data.color, width: 1, dash: "dot" },
      visible: false,
    };
    // Layout.shapes may be undefined on the first react; merge.
    const existing = profileEl.layout.shapes || [];
    Plotly.relayout(profileEl, { shapes: [shape, ...existing.filter((s) => s !== shape)] });
  }

  function showProfileCrosshair(x) {
    if (!profileEl.layout) return;
    // Plotly mutates layout.shapes in place; find the crosshair by
    // reference and update its x0/x1 + visibility.
    const shapes = (profileEl.layout.shapes || []).slice();
    let found = false;
    for (let i = 0; i < shapes.length; i++) {
      if (shapes[i] && shapes[i].type === "line" && shapes[i].yref === "paper" && shapes[i].xref === "x") {
        // Reuse the first matching shape as "the crosshair" — there is
        // exactly one in our plots.
        shapes[i] = { ...shapes[i], x0: x, x1: x, visible: true };
        found = true;
        break;
      }
    }
    if (!found) return;
    Plotly.relayout(profileEl, { shapes });
  }

  function hideProfileCrosshair() {
    if (!profileEl.layout) return;
    const shapes = (profileEl.layout.shapes || []).map((s) =>
      s && s.type === "line" && s.yref === "paper" && s.xref === "x"
        ? { ...s, visible: false }
        : s
    );
    Plotly.relayout(profileEl, { shapes });
  }

  // Binary search: index of the largest element in `arr` that is <= `x`.
  // arr is assumed non-decreasing. Returns -1 if x < arr[0] or arr is
  // empty. Mirrors ``upper_bound_index`` in tests/test_sync_math.py
  // — change both together.
  function upperBoundIndex(arr, x) {
    if (!arr || arr.length === 0 || x < arr[0]) return -1;
    let lo = 0, hi = arr.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (arr[mid] <= x) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  // Convert a distance (km) along a track into a (lat, lon) by linearly
  // interpolating the segment that brackets it. Returns null if the
  // track is too short or the distance is out of range.
  function latLonAtDistance(data, distance_km) {
    const dists = data.profile ? data.profile.distances_km : null;
    const pts = data.points;
    if (!dists || dists.length < 2 || pts.length !== dists.length) return null;
    if (distance_km < dists[0] || distance_km > dists[dists.length - 1]) return null;
    const i = upperBoundIndex(dists, distance_km);
    if (i < 0) return null;
    // Exact hit on a point, or at the very end.
    if (i === dists.length - 1 || dists[i] === distance_km) {
      return [pts[i].lat, pts[i].lon];
    }
    // Linear interpolation within segment (i, i+1).
    const d0 = dists[i], d1 = dists[i + 1];
    const t = (distance_km - d0) / (d1 - d0 || 1);
    return [
      pts[i].lat + t * (pts[i + 1].lat - pts[i].lat),
      pts[i].lon + t * (pts[i + 1].lon - pts[i].lon),
    ];
  }

  // Find the polyline index whose cumulative distance is nearest to the
  // given lat/lon (cheaper than latLonAtDistance for the map→profile
  // direction; the lat/lon is a known polyline vertex in practice).
  function nearestPointIndex(pts, lat, lon) {
    let best = 0, bestD = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const dlat = pts[i].lat - lat, dlon = pts[i].lon - lon;
      const d = dlat * dlat + dlon * dlon;
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  function ensureMapCrosshair(trackId, color) {
    let m = crosshairs.get(trackId);
    if (m) return m;
    // Hidden initially (radius 0). The sync handlers set radius + latlng.
    m = L.circleMarker([0, 0], {
      radius: 0,
      color: color,
      weight: 2,
      fillColor: color,
      fillOpacity: 0.6,
      interactive: false,
    }).addTo(map);
    crosshairs.set(trackId, m);
    return m;
  }

  function placeMapCrosshair(trackId, lat, lon) {
    const m = crosshairs.get(trackId);
    if (!m) return;
    m.setLatLng([lat, lon]);
    m.setRadius(7);
  }

  function clearMapCrosshair(trackId) {
    const m = crosshairs.get(trackId);
    if (!m) return;
    m.setRadius(0);
  }

  function clearMap() {
    for (const line of polylines.values()) {
      line.remove();
    }
    polylines.clear();
    trackDetails.clear();
    for (const m of crosshairs.values()) {
      m.remove();
    }
    crosshairs.clear();
  }

  function drawAllTracks() {
    clearMap();
    if (tracks.length === 0) return;

    // Fetch each track's polyline in parallel, then draw + auto-fit.
    // We fire-and-forget: the per-track click handler will fetch on
    // demand if the user clicks before the background load finishes.
    const fetches = tracks.map((t) =>
      fetch(`/api/tracks/${encodeURIComponent(t.id)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((detail) => {
          if (!detail) return;
          trackDetails.set(t.id, detail);
          drawPolyline(t.id, detail);
        })
        .catch(() => {
          // Network glitch on one track: keep the others on screen.
        })
    );
    Promise.all(fetches).then(() => {
      // After everything is drawn, fit to whichever track is selected,
      // or to all tracks if none is.
      if (selectedTrackId) {
        fitToTrack(selectedTrackId);
      } else {
        fitToAllTracks();
      }
    });
  }

  function drawPolyline(trackId, detail) {
    if (polylines.has(trackId)) return; // already drawn
    const latlngs = detail.points.map((p) => [p.lat, p.lon]);
    const isSelected = trackId === selectedTrackId;
    const line = L.polyline(latlngs, {
      color: detail.color,
      weight: isSelected ? 5 : 3,
      opacity: isSelected ? 1.0 : 0.7,
      lineJoin: "round",
    }).addTo(map);
    // Sticky hover tooltip: follows the cursor along the line and hides
    // on mouseout. The two-way profile/map sync is wired separately via
    // the mousemove/mouseout handlers below.
    line.bindTooltip(`${detail.name} · ${detail.distance_km.toFixed(1)} km`, {
      sticky: true,
      direction: "top",
    });
    line.on("click", () => selectTrack(trackId));
    // Map → profile sync: as the mouse moves over the polyline, set
    // the profile crosshair to the corresponding distance. Only the
    // currently selected track's profile reacts; we check at fire time
    // because the user can click a track *while* their mouse is already
    // over a different polyline.
    line.on("mousemove", (ev) => {
      if (selectedTrackId !== trackId) return;
      const profile = profileData.get(trackId);
      if (!profile) return;
      // ev.latlng is the literal mouse position; we want the nearest
      // polyline vertex instead (the user expects "the line, not the
      // cursor between vertices").
      const idx = nearestPointIndex(detail.points, ev.latlng.lat, ev.latlng.lng);
      const dist_km = profile.distances_km[idx];
      showProfileCrosshair(dist_km);
    });
    line.on("mouseout", () => {
      if (selectedTrackId !== trackId) return;
      hideProfileCrosshair();
    });
    polylines.set(trackId, line);
  }

  function restyleSelection() {
    // Re-apply the selected line's weight/opacity without redrawing.
    for (const [id, line] of polylines.entries()) {
      const isSelected = id === selectedTrackId;
      line.setStyle({ weight: isSelected ? 5 : 3, opacity: isSelected ? 1.0 : 0.7 });
      if (isSelected) line.bringToFront();
    }
  }

  function fitToTrack(trackId) {
    const line = polylines.get(trackId);
    if (!line) return;
    // Some tracks are single-point (degenerate GPX); fitBounds would
    // throw on an empty bounds. Guard with a length check.
    const b = line.getBounds();
    if (b.isValid()) map.fitBounds(b, { padding: [24, 24] });
  }

  function fitToAllTracks() {
    const all = Array.from(polylines.values()).map((l) => l.getBounds()).filter((b) => b.isValid());
    if (all.length === 0) return;
    const merged = all.reduce((acc, b) => acc.extend(b), all[0]);
    map.fitBounds(merged, { padding: [24, 24] });
  }

  // ─── Selection ───────────────────────────────────────────────────────────

  function selectTrack(id) {
    // Clear crosshairs on tracks that aren't the new selection, so a
    // stale circle marker doesn't linger after the user switches focus.
    for (const trackId of crosshairs.keys()) {
      if (trackId !== id) clearMapCrosshair(trackId);
    }
    selectedTrackId = id;
    // A selection can arrive from the map (or a collapsed group's row,
    // via keyboard) even when its group is collapsed. Auto-expand the
    // selected track's group so the selection highlight stays visible.
    const sel = tracks.find((t) => t.id === id);
    if (sel && sel.subfolder) collapsedFolders.delete(sel.subfolder);
    renderTrackList();
    restyleSelection();
    if (id && polylines.has(id)) {
      fitToTrack(id);
    }
    // Profile fetch subscribes here.
    if (id) {
      fetchProfile(id);
      // Pre-create the crosshair for the selected track so the first
      // hover on the profile doesn't have to lazy-create the marker.
      const detail = trackDetails.get(id);
      if (detail) ensureMapCrosshair(id, detail.color);
    } else {
      clearProfile();
      hideProfileCrosshair();
    }
    // The Edit view follows the selection only while it's the active
    // view — selecting a track does not auto-open the tab. When the
    // tab is closed, the form populates the next time Edit is opened.
    if (activeView === "edit") renderEditView();
  }

  // ─── Profile (Plotly) ────────────────────────────────────────────────────

  function clearProfile() {
    profileTitle.textContent = "No track selected";
    profileStats.textContent = "";
    // Plotly.purge removes the plot and its event listeners cleanly.
    // Without this, switching tracks would stack old traces.
    if (profileEl.data) {
      Plotly.purge(profileEl);
    }
  }

  async function fetchProfile(trackId) {
    profileTitle.textContent = "Loading…";
    profileStats.textContent = "";
    try {
      const resp = await fetch(`/api/tracks/${encodeURIComponent(trackId)}/profile`);
      if (!resp.ok) {
        profileTitle.textContent = `Error: HTTP ${resp.status}`;
        return;
      }
      const data = await resp.json();
      profileData.set(trackId, data);
      renderProfile(data);
      // The polyline's detail (points) may not have arrived yet if the
      // user clicked a track before drawAllTracks() finished. We don't
      // wait — the plotly_hover handler checks trackDetails at fire
      // time, so it'll just no-op until the polyline lands.
    } catch (err) {
      profileTitle.textContent = `Network error: ${err.message}`;
    }
  }

  function renderProfile(data) {
    profileTitle.textContent = data.name;
    profileStats.textContent = formatProfileStats(data);

    const trace = {
      x: data.distances_km,
      y: data.elevations_m,
      type: "scatter",
      mode: "lines",
      line: { color: data.color, width: 2, shape: "linear" },
      fill: "tozeroy",
      fillcolor: hexToRgba(data.color, 0.18),
      hovertemplate: "%{x:.2f} km<br>%{y:.0f} m<extra>" + data.name + "</extra>",
      name: data.name,
    };
    const layout = {
      margin: { l: 48, r: 12, t: 8, b: 32 },
      xaxis: {
        title: { text: "km", font: { size: 11 } },
        gridcolor: "#eaeef2",
        zeroline: false,
        showline: true,
        linecolor: "#d0d7de",
      },
      yaxis: {
        title: { text: "m", font: { size: 11 } },
        gridcolor: "#eaeef2",
        zeroline: false,
        showline: true,
        linecolor: "#d0d7de",
      },
      paper_bgcolor: "#ffffff",
      plot_bgcolor: "#ffffff",
      showlegend: false,
      // 'x unified' would show all traces at the same x; with one trace
      // the default 'closest' is cleaner.
      hovermode: "closest",
    };
    const config = {
      displayModeBar: false,
      responsive: true,
    };
    Plotly.react(profileEl, [trace], layout, config);

    // Wire up profile → map sync. Plotly fires plotly_hover on every
    // mousemove over the trace; we resolve the distance to a (lat, lon)
    // via the cached polyline detail and place a crosshair on the map.
    // plotly_unhover hides it.
    if (!profileEl._syncWired) {
      profileEl.on("plotly_hover", (ev) => {
        if (selectedTrackId !== data.id) return;
        const point = ev.points[0];
        if (!point) return;
        const dist_km = point.x;
        const detail = trackDetails.get(data.id);
        if (!detail) return;
        const ll = latLonAtDistance(
          { profile: data, points: detail.points },
          dist_km
        );
        if (!ll) return;
        ensureMapCrosshair(data.id, data.color);
        placeMapCrosshair(data.id, ll[0], ll[1]);
      });
      profileEl.on("plotly_unhover", () => {
        if (selectedTrackId !== data.id) return;
        clearMapCrosshair(data.id);
      });
      profileEl._syncWired = true;
    }

    // Add the crosshair shape used by the map → profile direction. It
    // starts hidden; the polyline's mousemove handler shows it.
    ensureProfileCrosshair(data);
  }

  function formatProfileStats(data) {
    const total = data.distances_km[data.distances_km.length - 1] || 0;
    // Gain/loss come straight from the server — gap-aware (a GPS
    // dropout's missing <ele> is skipped, not treated as 0) and
    // precomputed for each smoothing window. pickGainLoss selects the
    // active window so changing the setting updates this line without
    // a refetch. The elevations_m array still carries 0.0 for gaps
    // (index-aligned for Plotly + the hover-sync), so these must not
    // be recomputed client-side from it. min/max are raw extremes.
    const gl = pickGainLoss(data.elev_gain_loss_m);
    const gain = Math.round(gl.gain_m);
    const loss = Math.round(gl.loss_m);
    const range =
      data.elev_min_m == null || data.elev_max_m == null
        ? "—"
        : `${Math.round(data.elev_min_m)}–${Math.round(data.elev_max_m)} m`;
    // The sampling rate lets the user read the ±N-sample window in
    // real time (±10 samples at 5 s/pt = ±50 s). Omitted when the
    // track has no <time> stamps.
    const rate = data.sample_interval_s == null
      ? ""
      : ` · ${data.sample_interval_s}s/pt`;
    return `${total.toFixed(2)} km · ↑ ${gain} m · ↓ ${loss} m · ${range}${rate}`;
  }

  // Pick the gain/loss pair for the active smoothing window from the
  // server-shipped list, falling back to the default (10) then the
  // first available — robust if the server's window set ever differs
  // from the radio set in the HTML.
  function pickGainLoss(glList) {
    if (!glList || !glList.length) return { gain_m: 0, loss_m: 0 };
    return (
      glList.find((x) => x.half === smoothHalf) ||
      glList.find((x) => x.half === 10) ||
      glList[0]
    );
  }

  // Re-render every stat line for the active smoothing window from the
  // already-shipped per-window gain/loss — no refetch. Called when the
  // smoothing radio changes.
  function refreshStats() {
    for (const li of trackList.querySelectorAll("li[data-track-id]")) {
      const t = tracks.find((x) => x.id === li.dataset.trackId);
      if (t) {
        const stats = li.querySelector(".stats");
        if (stats) stats.textContent = formatStats(t);
      }
    }
    if (selectedTrackId) {
      const data = profileData.get(selectedTrackId);
      if (data) profileStats.textContent = formatProfileStats(data);
    }
  }

  function hexToRgba(hex, alpha) {
    // "#rrggbb" → "rgba(r, g, b, alpha)". No validation; data.color is
    // server-controlled and already validated by the palette.
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  // ─── Settings panel ────────────────────────────────────────────────────

  function setScaleBar(on) {
    // L.control.scale is a singleton-ish control — to hide it we
    // call .remove(); to show it we add a fresh one. Re-creating
    // on every toggle is cheaper than tracking the previous
    // position, and the user can't see a difference (it's a few
    // px in a corner).
    if (scaleControl) {
      scaleControl.remove();
      scaleControl = null;
    }
    if (on) {
      scaleControl = L.control.scale({ imperial: false, position: "bottomleft" })
        .addTo(map);
    }
    showScaleBar = on;
  }

  function renderSettingsPanel() {
    // One radio row per source. Disabled rows show a hint about
    // what's missing (e.g. "needs RANDONNEUR_THUNDERFOREST_KEY").
    settingsSources.innerHTML = "";
    for (const src of tileSources) {
      const id = `src-${src.id}`;
      const label = document.createElement("label");
      label.className = "settings-source" + (src.available ? "" : " disabled");
      label.setAttribute("for", id);

      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "tile-source";
      radio.id = id;
      radio.value = src.id;
      radio.checked = src.id === currentSource;
      radio.disabled = !src.available;
      radio.addEventListener("change", () => {
        if (radio.checked) setBaseLayer(src.id);
      });

      const text = document.createElement("div");
      text.className = "settings-source-label";
      const name = document.createElement("span");
      name.textContent = src.name;
      text.appendChild(name);
      if (!src.available && src.needs_key) {
        const hint = document.createElement("span");
        hint.className = "settings-source-hint";
        hint.textContent = "Set RANDONNEUR_THUNDERFOREST_KEY on the server";
        text.appendChild(hint);
      }
      label.append(radio, text);
      settingsSources.appendChild(label);
    }
    settingsScale.checked = showScaleBar;
  }

  // Which view the right-side tab is showing, or null when the tab is
  // closed. The two header buttons each toggle a view; only one view is
  // shown at a time, and clicking the active view's button closes the tab.
  let activeView = null;

  function showView(view) {
    // Reveal only the requested .tab-view; hide the other. The panel's
    // data-view also drives the header title.
    for (const v of sidePanel.querySelectorAll(".tab-view")) {
      v.hidden = v.dataset.view !== view;
    }
    sidePanel.dataset.view = view;
    sidePanelTitle.textContent = view === "settings" ? "Settings" : "Edit metadata";
  }

  function openTab(view) {
    activeView = view;
    showView(view);
    sidePanel.classList.add("open");
    document.body.classList.add("tab-open");
    tabButtonSettings.setAttribute("aria-expanded", String(view === "settings"));
    tabButtonEdit.setAttribute("aria-expanded", String(view === "edit"));
    if (view === "settings") renderSettingsPanel();
    else renderEditView();
  }

  function closeTab() {
    activeView = null;
    sidePanel.classList.remove("open");
    document.body.classList.remove("tab-open");
    tabButtonSettings.setAttribute("aria-expanded", "false");
    tabButtonEdit.setAttribute("aria-expanded", "false");
  }

  function isTabOpen() {
    return sidePanel.classList.contains("open");
  }

  tabButtonSettings.addEventListener("click", () => {
    if (activeView === "settings") closeTab();
    else openTab("settings");
  });
  tabButtonEdit.addEventListener("click", () => {
    if (activeView === "edit") closeTab();
    else openTab("edit");
  });
  sidePanelClose.addEventListener("click", closeTab);
  settingsScale.addEventListener("change", () => {
    setScaleBar(settingsScale.checked);
  });
  if (settingsSmooth) {
    settingsSmooth.addEventListener("change", (ev) => {
      if (ev.target.name === "elev-smooth") {
        smoothHalf = Number(ev.target.value);
        // Re-render every stat line from the already-shipped per-window
        // gain/loss — no refetch. The sidebar list and the selected
        // track's profile stat both update instantly.
        refreshStats();
      }
    });
  }
  // Escape closes the tab — standard affordance for a dialog.
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && isTabOpen()) {
      closeTab();
      ev.preventDefault();
    }
  });

  async function loadSettings() {
    // Fetch the available sources. Failure is non-fatal: the panel
    // will just show whatever is in the static fallback (just
    // OpenTopoMap) and the user can still use the rest of the app.
    try {
      const resp = await fetch("/api/settings");
      if (!resp.ok) return;
      const body = await resp.json();
      tileSources = body.sources;
      currentSource = body.current_source;
      // If the server's default differs from the one we built at
      // boot (e.g. a future change makes Thunderforest the default
      // when its key is set), swap to it.
      setBaseLayer(currentSource);
    } catch {
      // Network blip — keep going with the default layer.
    }
  }

  // ─── Metadata editor ───────────────────────────────────────────────────────
  // ─── (lives in the Edit view of the right-side tab) ────────────────────────

  function renderEditView() {
    // The Edit view shows the metadata form when a track is selected,
    // or the #metadata-empty placeholder when none is (or the selected
    // track is no longer in the folder list). Called on openTab("edit")
    // and whenever the selection changes while the Edit view is the
    // active view (selectTrack / watcher refresh).
    const summary = selectedTrackId ? tracks.find((t) => t.id === selectedTrackId) : null;
    if (summary) {
      metadataEmpty.hidden = true;
      renderMetadataEditor(selectedTrackId);
    } else {
      clearMetadataEditor();
      metadataEmpty.hidden = false;
    }
  }

  function renderMetadataEditor(trackId) {
    // Populate the form from the folder list's TrackSummary (the
    // metadata fields are inlined on the summary so we don't need a
    // second fetch). The file stem is shown at the top as a "you're
    // editing…" reminder; the form fields show the GPX-side values.
    //
    // The status line is left alone here — it's owned by the
    // save/clear flow, not the render flow. (Without this guard, the
    // watcher's debounced "changed" event after a save would wipe
    // the "Saved." message before the user could see it.)
    const summary = tracks.find((t) => t.id === trackId);
    if (!summary) {
      clearMetadataEditor();
      return;
    }
    metadataGroup.hidden = false;
    metadataTarget.innerHTML = "";
    const stem = document.createElement("span");
    stem.append("Editing: ");
    const strong = document.createElement("strong");
    strong.textContent = summary.name + ".gpx";
    stem.appendChild(strong);
    metadataTarget.appendChild(stem);
    // The form is the source of truth for the editor; the server's
    // last-saved values seed it. Empty string vs placeholder: the
    // input's placeholder is the dim hint for an empty field; the
    // value is the actual content. (gpxpy normalises missing
    // elements to None, which the API surfaces as null, which becomes
    // undefined in JS — we use "" so the user sees a clear input
    // rather than the placeholder when the field is genuinely empty
    // after a clear.)
    metadataTrackName.value = summary.track_name || "";
    metadataTrackDesc.value = summary.track_desc || "";
    metadataMetaName.value = summary.metadata_name || "";
    metadataMetaDesc.value = summary.metadata_desc || "";
    metadataMetaAuthor.value = summary.metadata_author || "";
  }

  function clearMetadataEditor() {
    // Hide the fieldset and reset the form. Called when the user
    // deselects (clicks the same row again, or refreshes) so the
    // editor doesn't show stale data for a track that's no longer
    // focused. Also clears the status line — the success/fail
    // message is no longer meaningful once the editor is gone.
    metadataGroup.hidden = true;
    metadataTrackName.value = "";
    metadataTrackDesc.value = "";
    metadataMetaName.value = "";
    metadataMetaDesc.value = "";
    metadataMetaAuthor.value = "";
    metadataStatus.textContent = "";
    metadataStatus.classList.remove("error", "success");
  }

  async function saveMetadata() {
    // PATCH the edited fields. Each field is sent as the trimmed
    // string the user typed, or "" if they cleared it (the server
    // treats "" as remove). Empty-after-trim is sent as "" too —
    // there's no "don't touch" affordance for a single field, since
    // the only no-op case is "the user didn't open the editor", and
    // the Save button wouldn't have been pressed in that case.
    if (!selectedTrackId) return;
    const id = selectedTrackId;
    const body = {
      track_name: metadataTrackName.value.trim(),
      track_desc: metadataTrackDesc.value.trim(),
      metadata_name: metadataMetaName.value.trim(),
      metadata_desc: metadataMetaDesc.value.trim(),
      metadata_author: metadataMetaAuthor.value.trim(),
    };
    metadataSave.disabled = true;
    metadataStatus.textContent = "Saving…";
    metadataStatus.classList.remove("error", "success");
    try {
      const resp = await fetch(
        `/api/tracks/${encodeURIComponent(id)}/metadata`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      if (!resp.ok) {
        const detail = (await resp.json().catch(() => ({}))).detail
          || `HTTP ${resp.status}`;
        metadataStatus.textContent = `Save failed: ${detail}`;
        metadataStatus.classList.add("error");
        return;
      }
      // Server returned the updated summary. Replace the entry in
      // the local tracks array so the next /api/folder / re-render
      // shows the new values; we also re-render the editor so the
      // form is in sync with what's now on disk.
      const updated = await resp.json();
      const idx = tracks.findIndex((t) => t.id === id);
      if (idx >= 0) tracks[idx] = updated;
      // The editor is necessarily open (Save is only reachable from
      // the Edit view), so re-render it in place to stay in sync with
      // what's now on disk.
      renderEditView();
      metadataStatus.textContent = "Saved.";
      metadataStatus.classList.add("success");
    } catch (err) {
      metadataStatus.textContent = `Save failed: ${err.message}`;
      metadataStatus.classList.add("error");
    } finally {
      metadataSave.disabled = false;
    }
  }

  metadataSave.addEventListener("click", saveMetadata);
  metadataClear.addEventListener("click", () => {
    // Empty the form fields. The user still has to click Save to
    // persist the clear; we don't auto-save (clearing is destructive
    // and worth a confirmation click).
    metadataTrackName.value = "";
    metadataTrackDesc.value = "";
    metadataMetaName.value = "";
    metadataMetaDesc.value = "";
    metadataMetaAuthor.value = "";
    metadataStatus.textContent = "Cleared (click Save to persist)";
    metadataStatus.classList.remove("error", "success");
  });

  // ─── Folder loading ──────────────────────────────────────────────────────

  function connectHotReload() {
    // Open the WebSocket. The server accepts immediately; the watcher
    // starts producing messages as soon as /api/folder has been called
    // at least once. The first message on a fresh watch is the
    // directory's current state (the "attach snapshot") — the client
    // treats it as a normal "changed" event and re-fetches, which is
    // a no-op refresh if nothing has actually changed yet. A failed
    // connect (e.g. server not yet up after a page reload) is retried
    // after a short delay so the user doesn't have to manually
    // reload to get hot-reload back.
    if (hotReloadWs && hotReloadWs.readyState <= WebSocket.OPEN) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
    hotReloadWs = ws;
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg && msg.type === "changed") {
        // Server debounces; one message per batch. Re-list the folder
        // and let the normal render path take over. We don't act on
        // the specific files — the server is the single source of
        // truth for what's in the folder. An early "changed" message
        // (before the initial /api/folder has completed) just shows
        // the new state when it lands.
        refreshFolder();
      }
    });
    ws.addEventListener("close", () => {
      if (hotReloadWs === ws) hotReloadWs = null;
      // Auto-reconnect after 1s if the page is still alive. Don't
      // retry if the user is navigating away (the beforeunload
      // handler set a flag).
      if (!pageNavigatingAway) setTimeout(connectHotReload, 1000);
    });
    ws.addEventListener("error", () => {
      // Browsers don't surface the actual error; just let close fire.
    });
  }

  let pageNavigatingAway = false;
  window.addEventListener("beforeunload", () => {
    pageNavigatingAway = true;
    if (hotReloadWs) hotReloadWs.close();
  });

  async function refreshFolder() {
    // Re-fetch /api/folder (the active folder is set at server start;
    // we don't pass a path). Used both for the initial load and for
    // hot-reload messages from the watcher. Renders the folder path
    // in the header on the first successful response (the "Folder"
    // label is empty until the server confirms a folder is loaded).
    try {
      const resp = await fetch(`/api/folder`);
      if (resp.status === 404) {
        const detail = (await resp.json()).detail || "folder not found";
        setStatus(detail, true);
        return;
      }
      if (!resp.ok) {
        setStatus(`Error: HTTP ${resp.status}`, true);
        return;
      }
      const body = await resp.json();
      // Update the read-only folder path display. When the server
      // was started without --directory, body.path is null and we
      // show a dim placeholder.
      if (body.path) {
        folderPath.textContent = body.path;
        folderPath.classList.remove("empty");
        folderPath.title = body.path;
      } else {
        folderPath.textContent = "no folder configured";
        folderPath.classList.add("empty");
        folderPath.removeAttribute("title");
      }
      // If the active folder changed (a future feature), drop the
      // selection — a track id from the old folder won't exist in
      // the new one.
      if (body.path !== currentFolder && selectedTrackId) {
        selectedTrackId = null;
      }
      currentFolder = body.path;
      tracks = body.tracks || [];
      // Drop the selection if the previously-selected track is gone.
      if (!tracks.some((t) => t.id === selectedTrackId)) {
        selectedTrackId = null;
      }
      renderTrackList();
      renderErrors(body.errors);
      drawAllTracks();
      if (selectedTrackId) {
        fetchProfile(selectedTrackId);
      } else {
        clearProfile();
      }
      // Re-render the Edit view from the (potentially updated) folder
      // list, but only if it's the active view — a folder change while
      // the tab is closed shouldn't populate a hidden form.
      if (activeView === "edit") renderEditView();
      if (body.path) {
        // Path lives in the header; track count lives in the
        // sidebar heading. The sidebar's folder-status only surfaces
        // something on error, so it stays empty in the happy path.
        if (body.errors.length) {
          setStatus(`${body.errors.length} parse error(s)`, true);
        } else {
          setStatus("");
        }
      } else {
        setStatus("No folder configured — start the server with --directory <path>.");
      }
    } catch (err) {
      setStatus(`Network error: ${err.message}`, true);
    }
  }

  // ─── Utils ───────────────────────────────────────────────────────────────

  function formatStats(t) {
    const km = `${t.distance_km.toFixed(1)} km`;
    // Elevation is always shown in metres — the old ≥1000→km branch was
    // a copy-paste from the distance formatter (elevation gain in km is
    // nonsensical). Gain and loss are separate stats now, both in
    // metres; pickGainLoss selects the active smoothing window's pair
    // from the server-shipped list.
    const gl = pickGainLoss(t.elev_gain_loss_m);
    const gain = `${Math.round(gl.gain_m)} m`;
    const loss = `${Math.round(gl.loss_m)} m`;
    return `${km} · ↑${gain} · ↓${loss}`;
  }

  // ─── Boot ────────────────────────────────────────────────────────────────

  // Connect the hot-reload channel before the initial folder load so
  // any change made *during* the load is also delivered.
  connectHotReload();

  // Fetch the available tile sources and apply the user's saved
  // preferences. Settings is a fire-and-forget: if it fails the
  // default (OpenTopoMap, scale bar on) stands.
  loadSettings();
  setScaleBar(showScaleBar);

  // Load the active folder (configured at server start). The server
  // returns the path, the track list, and any per-file parse errors
  // in one round-trip; refreshFolder renders the path in the header
  // and re-uses the same render path on every subsequent refresh.
  refreshFolder();

  // Leaflet needs an invalidateSize() after the layout settles, otherwise
  // the map renders half-width if the pane was hidden during init. The
  // pane is always visible here, but the resize still helps after the
  // initial paint.
  setTimeout(() => map.invalidateSize(), 0);
})();
