randonneur
==========

Plot GPX tracks from a folder onto a hiking-styled map, with an
elevation profile below and a two-way hover crosshair between them.
Hot-reloads on file save; no browser extension, no Electron, no
build step.

.. image:: https://img.shields.io/badge/python-3.12%2B-blue
   :alt: Python 3.12+
   :target: https://www.python.org/

----

Why this exists
---------------

GPXSee_ is the lightweight reference viewer. gpx.studio_ is the
UX reference for click-to-focus. randonneur borrows the paper-map
aesthetic from the first and the "open the folder, see every track
at once" flow from the second, and runs entirely as a local
FastAPI app + a single static page in the browser. No build step,
no JS framework.

.. _GPXSee: https://www.gpxsee.org/
.. _gpx.studio: https://gpx.studio/


Install
-------

randonneur is a regular Python package; install it into a venv
from the repo root::

    git clone <repo> randonneur
    cd randonneur
    python -m venv .venv
    .venv/bin/pip install -e ".[dev]"

The ``[dev]`` extra pulls in pytest + pytest-asyncio so you can
also run the test suite. If you only want to use the app, plain
``.venv/bin/pip install -e .`` is enough.


Run
---

Start the server and open the UI in your default browser. The folder
containing your ``.gpx`` files is required at startup — the browser
cannot expose an absolute path from a native picker, so the server
picks the folder and the UI shows it read-only::

    .venv/bin/randonneur serve --directory /Users/you/Tracks

The server binds to ``http://127.0.0.1:8765`` by default. To pick
a different port (e.g. when 8765 is already in use)::

    .venv/bin/randonneur serve --directory /Users/you/Tracks --port 8800

Other useful flags:

- ``--no-browser`` — start the server without auto-opening the
  browser. Useful on headless boxes or when you want to drive the
  UI from another machine via SSH tunnel.
- ``--host 0.0.0.0`` — bind on all interfaces instead of the
  loopback only. The server has no auth; **don't do this on a
  shared network** unless you know who's on it.
- ``-v`` / ``--verbose`` — DEBUG-level logging for the watcher,
  tile fetcher, etc.

To change the folder, restart the server with a new ``--directory``
path. The folder is fixed for the server's lifetime — the watcher,
the track cache, and the UI all bind to the same path for the
duration of the run.

The bare ``randonneur`` (no args) and ``randonneur serve`` (no
``--directory``) both error with "Missing option '--directory'" —
restart with the flag to point the server at your tracks.

The server shuts down on Ctrl-C / SIGTERM. The watcher task is
stopped cleanly in the lifespan handler, so you won't be left
with a dangling ``awatch`` coroutine.

A second subcommand, ``discover``, lists every ``.gpx`` file under
a folder — useful for "is this folder shaped right?" sanity checks
without firing up the browser::

    .venv/bin/randonneur discover /Users/you/Tracks


Select your first folder
------------------------

Open the UI in a browser (either via the auto-open, or by visiting
``http://127.0.0.1:8765`` yourself). The header shows the loaded
folder as read-only information; the track list is on the left; the
map fills the upper-right; the elevation profile fills the lower-right.

::

    randonneur  Folder  /Users/you/Tracks                 ⚙

The status line under the header shows the track count and any
per-file parse errors. To switch folders, restart the server with
a new ``--directory``.

Once a folder is loaded:

- **Click a track** in the sidebar to focus it. The map auto-fits
  to its bounds; the profile pane renders its elevation chart.
- **Hover the profile** — a small crosshair drops on the map at
  the corresponding point on the track.
- **Hover the polyline on the map** — a vertical line appears on
  the profile at the corresponding distance.
- **Edit a GPX file in the folder and save it** — the UI re-loads
  the folder and updates the track list / map / profile in place.
  No page refresh.
- **Click ⚙ in the header** to switch the base layer, toggle the
  scale bar, or edit the selected track's metadata (the GPX
  ``<name>`` / ``<desc>`` / ``<author>`` and the per-track
  ``<trk><name>`` / ``<trk><desc>``). See `Base layer and tiles`_
  and `Metadata editing`_ below.


What you get
------------

Two-pane layout, no JS framework, no build step. The map is
Leaflet via CDN, the profile is Plotly via CDN, the backend is
FastAPI + Uvicorn.

::

    ┌────────────────────────────────────────────────────────────┐
    │ randonneur   Folder  /Users/you/Tracks                 ⚙    │
    ├──────────────┬─────────────────────────────────────────────┤
    │ ☑ tour1.gpx  │                                             │
    │ ☐ tour2.gpx  │              MAP (Leaflet)                  │
    │ ☐ tour3.gpx  │   OpenTopoMap tiles + polyline overlays     │
    │              │   hover tooltip · click to focus            │
    │ 12.4 km      │                                             │
    │ ↑ 870 m      │                                             │
    │              │                                             │
    ├──────────────┴─────────────────────────────────────────────┤
    │         ELEVATION PROFILE (Plotly)                         │
    │ ▲ m                                                        │
    │ │      ╱╲                                                  │
    │ │     ╱  ╲___                                              │
    │ └─────────────────▶ km          tour2.gpx · 12.4 km        │
    └──────────────────────────────────────────────────────────────┘

The split between map and profile is fixed at 1fr / 240 px. Making
it draggable is cheap to add later if you want to.


Base layer and tiles
--------------------

The default base layer is **OpenTopoMap** — a paper-map-style layer
with contours, trails and POIs. Tiles are fetched through the
server (``/api/tiles/<source>/...``) so we can rate-limit per host
and cache to disk under ``~/.cache/randonneur/tiles/``. To reset
the cache::

    rm -rf ~/.cache/randonneur/tiles

A second source, **Thunderforest Outdoors**, is whitelisted but
disabled by default. It needs a free API key — sign up at
https://www.thunderforest.com/ and set::

    export RANDONNEUR_THUNDERFOREST_KEY=your-key-here

before starting the server. The key never leaves the server: the
browser only sees ``/api/tiles/thunderforest-outdoors/{z}/{x}/{y}.png``.
With the env var set, the ⚙ panel will offer Thunderforest as an
alternative base layer; without it, the entry is shown as
"unavailable" so you don't have to discover the missing-config
problem via a 503 on the first map pan.

OpenTopoMap's policy is "≤ 1 request per second per client". The
built-in rate limiter smooths bursts to that mean; in practice
the on-disk cache means the limiter only matters during the first
load of a brand-new area.


Metadata editing
----------------

The right-side settings tab (click ⚙) has a **Metadata** section
that appears when a track is selected. It surfaces the five
human-readable fields a GPX 1.1 file can carry:

- ``<metadata><name>`` and ``<metadata><desc>`` — the top-level
  trip name and description.
- ``<metadata><author><name>`` — the trip author.
- ``<trk><name>`` and ``<trk><desc>`` — the per-track display
  name and description (randonneur shows the first ``<trk>`` of
  a multi-track file; that's the one a typical recording has).

Each field has a 1000-character cap, both client-side and in the
``PATCH /api/tracks/{id}/metadata`` endpoint. Save writes the
file atomically (a ``tmp + os.replace`` so a crash mid-write
leaves the original on disk), refreshes the in-memory track
cache, and re-fetches the folder list so other open browser
windows see the change via the watcher's hot-reload channel.
**Clear all** empties the form (a second click on Save is needed
to persist — clearing is destructive and worth a confirmation
click).

The file stem (e.g. ``tour1.gpx``) is what's used as the
sidebar's per-track label; the GPX ``<name>`` is the display name
that lives in the metadata editor. This separation matches how
most people name GPX files (after the date or the recording
device) and the actual trip they recorded.


Project layout
--------------

::

    randonneur/
    ├── pyproject.toml          # hatchling build, deps, entry point
    ├── README.rst              # you are here
    ├── agent/                  # working notes, history, behaviour contract
    │   ├── agent-behaviour.md
    │   └── agent-history.md
    ├── src/randonneur/
    │   ├── __init__.py
    │   ├── __main__.py         # `python -m randonneur`
    │   ├── cli.py              # click group, `randonneur` (no args) → serve
    │   ├── log.py              # rich-backed logging, configured once
    │   ├── config.py           # tile sources, palette, rate limits, paths
    │   ├── gpx_loader.py       # discover() + parse() — folder → Track
    │   ├── profile.py          # compute_profile(track) → (distances, elevations)
    │   ├── rate_limit.py       # async token-bucket per host
    │   ├── tile_cache.py       # disk-cached, rate-limited tile fetch
    │   ├── watcher.py          # folder watcher + WebSocket broker
    │   ├── server.py           # FastAPI app: /api/* + static mount
    │   └── static/
    │       ├── index.html      # two-pane layout
    │       ├── app.js          # all client logic
    │       └── style.css
    └── tests/
        ├── fixtures/           # small hand-built GPX files
        ├── test_gpx_loader.py
        ├── test_profile.py
        ├── test_server.py
        ├── test_tile_cache.py  # tile cache + rate limiter
        ├── test_watcher.py
        ├── test_sync_math.py   # Python twin of the JS hover-sync math
        └── test_docs.py        # README validates as RST


Development
-----------

Run the test suite::

    .venv/bin/pytest

The default run is unit tests only. A few tests hit the real
OpenTopoMap tile service and are slow + network-dependent; they
opt in via a marker::

    .venv/bin/pytest -m integration

Per the project's working agreement, a bare ``pytest`` is **not**
a substitute for a real run when a commit changes behaviour. For
UI commits, also open the browser, load the fixtures, click each
track, hover the profile, and confirm the crosshair lands on the
polyline.

If you change the README, validate it::

    .venv/bin/python -m docutils --strict README.rst


Limitations and known sharp edges
---------------------------------

- **No smoothing on the profile.** GPS samples are dense enough on
  a hike that the raw elevation signal is the honest one; a
  smoother can mislead about how steep a section actually was.
  Easy to add a moving average later if you want one.
- **In-memory track cache.** ``/api/tracks/{id}`` is populated as
  the folder is listed and shared with ``/api/tracks/{id}/profile``
  so a click doesn't re-parse. The cache is reset on every folder
  load; a server restart is a clean slate.
- **Server has no auth.** It binds to 127.0.0.1 by default. Don't
  expose it on a shared network.
- **No track editing, merging, or export.** Out of scope for a
  viewer. Easy to layer on later if needed.


License
-------

MIT. See ``pyproject.toml``.
