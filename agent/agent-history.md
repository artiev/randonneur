# Agent History — randonneur

> Per-project running log. Updated as work happens; appended to, not edited
> after the fact. See `agent-behaviour.md` §9 for the bug-hunt record format
> and the rule that gotchas are the most valuable output of a session.

## Project

- **Name:** randonneur
- **Stack:** Python 3.12, hatchling, FastAPI + Uvicorn, gpxpy, watchfiles, httpx,
  click, rich. Frontend: static HTML/JS/CSS + Leaflet + Plotly. Entry point
  `randonneur = randonneur.cli:main`.
- **Layout:** `src/randonneur/` flat package + `agent/` + `pyproject.toml`.
- **Entry point:** `randonneur` (no args) → `serve` subcommand.

## Conventions specific to this project

- Python floor is **3.12** (installed via Homebrew; system Python is 3.9.6).
- Use `default_cmd`-style fall-through via `sys.argv` inspection in a `main()`
  wrapper — see Bugs & gotchas #1.
- Tile sources, labels, default, and rate limits live in
  `randonneur.config` (added in commit 9; `TILE_SOURCE_LABELS` and
  `DEFAULT_TILE_SOURCE` added in commit 11).

## Bugs & gotchas

### #1 — Click `invoke_without_command=True` double-fires the group callback

**Symptom:** `randonneur serve` printed "randonneur" twice; `randonneur` (no
args) printed nothing.

**Hypothesis (first attempt):** Click builds a parent context for the group
before dispatching to the subcommand, and my fall-through was firing in the
parent context as well as the no-arg context. Try to *falsify*: if that were
true, `ctx.invoked_subcommand` would reliably tell us which case we're in.
But the trace showed the group callback was being called with `ctx=None` (no
parent context at all) on the no-args path, and again with a parent context
on the explicit-subcommand path. So `ctx` is the unreliable signal.

**Hypothesis (second attempt):** Use `ctx.protected_args` — empty means no
subcommand was parsed. Try to *falsify*: in practice both invocation paths
populate `protected_args` correctly, but a deeper trace showed
`ctx.invoked_subcommand` can be `None` even on the parent-context path, and
the second invocation happens *after* the group callback returns, so the
group's body has no way to know "Click will dispatch again".

**Hypothesis (third attempt — correct):** `default_cmd` was added to Click
in 8.2. Try to *falsify*: Click 8.4.2's `Group.__init__` does not accept
`default_cmd`. So we're on Click 8.4.2 and that param isn't here.

**Root cause:** `invoke_without_command=True` has no clean way to distinguish
"user typed no subcommand" from "Click is going to dispatch to a subcommand
after this callback returns". The group callback can be reached from multiple
call sites inside Click, and the `ctx` it sees is not the same object in
each case.

**Fix:** Drop `invoke_without_command` entirely. Add a `main()` wrapper that
appends `"serve"` to `sys.argv` when called with no subcommand, and point
both the console-script entry (`pyproject.toml`) and `__main__.py` at it.
This is the only place that inspects `sys.argv`, so it's also the only
place that knows whether the user gave a subcommand.

**Why a comment, not a more elaborate fix:** Click's `default_cmd` *is* the
right answer in newer versions; upgrading is a one-liner. Until then, the
`main()` wrapper is the cheapest correct solution.

**Verified:** `randonneur`, `randonneur serve`, `python -m randonneur`,
`randonneur --version`, `randonneur --help`, `randonneur serve --help` all
print exactly the expected output and exit 0.

### #2 — `gpxpy.parse(str(path))` parses the *path string* as XML, not the file

**Symptom:** All 8 `test_parse_*` tests failed with
`gpxpy.gpx.GPXXMLSyntaxException: Error parsing XML: not well-formed
(invalid token): line 1, column 5`.

**Hypothesis:** A fixture file is malformed. Try to *falsify*: opened the
fixture in a fresh `python -c gpxpy.parse(open(...).read())` and it parsed
fine. The XML is valid; the test was failing because the test was reading
the *path* `tests/fixtures/elevation_gaps.gpx` as if it were XML.

**Root cause:** `gpxpy.parse` accepts either an XML string or a file
*object*, not a path. I called `gpxpy.parse(str(path))` — gpxpy was
happily parsing the string `tests/fixtures/elevation_gaps.gpx` as XML
content, which produced the "invalid token: line 1, column 5" error
(column 5 being the `:` after `tests`). I had been fooled because the
earlier manual test used `gpxpy.parse(data.decode('utf-8'))` with the
file *contents*, and the tests used `gpxpy.parse(str(path))` with the
*path*. Two different call shapes, only the manual one happened to work.

**Fix:** Open the file as a binary handle and pass the handle to
`gpxpy.parse`. Left a comment at the call site so it doesn't regress —
the failing-but-not-obvious shape is too easy to re-introduce.

**Verified:** All 12 tests in `tests/test_gpx_loader.py` pass; the real
`randonneur discover tests/fixtures` command lists both fixtures and
`randonneur discover /tmp/does-not-exist` returns an empty list (no
exception, as the contract says).

### #3 — Hand-computed test constants were wrong, masked by a loose tolerance

**Symptom:** First run of `test_profile.py` failed with
`0.11119 == 111.31949 ± 0.0111` — expected 333 km, got 333 m. The
production code (haversine) was correct; the *test* had its expected
values off by 1000×.

**Root cause:** I wrote the test using the "111.32 m per degree"
figure (WGS84 meridional radius), then divided by 1000 to get km.
But the fixture moves are 0.001°, not 1°, so the *correct* expected
value is 111.32 m × 0.001 = 0.111 m, not 111.32 m. The looseness
of my first tolerance (`rel=1e-4`) hid the fact that the constant was
three orders of magnitude wrong.

**A subtler issue, caught on the second iteration:** even after fixing
the magnitude, "111.32 m per degree" is the WGS84 approximation. The
haversine in `profile.py` uses a *sphere* of radius 6,371,008.8 m, so
the function returns 111.195 m for 0.001° at the equator — not 111.319 m.
The test's expected value matched the WGS84 figure, so it would have
flagged a perfectly correct haversine as "wrong". I replaced the
constant with the haversine-oracle value (111.19508 m) and tightened
the tolerance to `rel=1e-6`.

**Lesson:** when testing a trig/distance function, the expected value
must come from the *function's own convention*, not from a paper-map
or textbook figure that happens to be close. A loose relative tolerance
is a way to ship a tautology test that "passes" against a wrong
expectation.

**Verified:** All 21 tests in `tests/` pass; real `python -c` invocation
of `gpx_loader.parse` + `profile.compute_profile` on the multi-segment
fixture produces monotonic distances and aligned elevations.

### #4 — `RichHandler(highlight=False)` doesn't work on rich 15.x

**Symptom:** `randonneur serve` crashed at startup with
`TypeError: RichHandler.__init__() got an unexpected keyword argument 'highlight'`.
The first test run hadn't caught this because the test client doesn't
go through `configure_logging`.

**Root cause:** rich 13 renamed `RichHandler(highlight=...)` →
`RichHandler(highlighter=...)`. The `highlight` kwarg is no longer
accepted; passing `highlighter=NullHighlighter()` is the documented
replacement. rich 15.0.0 (what we have installed) is firmly in the new
shape.

**Fix:** Import `NullHighlighter` and pass `highlighter=NullHighlighter()`.
Comment at the call site explains the rename so it doesn't get
"reverted" by muscle memory later.

**Lesson:** Anything that depends on installed-software behaviour
(uicorn, click, rich, gpxpy…) needs an **end-to-end real run** at least
once per commit, not just unit tests. Tests covered the FastAPI app in
isolation; the entry point was never exercised until I tried to start
the server for real. The behaviour file's §8 ("Test against the real
thing, not only stubs") is exactly this — a TestClient doesn't
exercise the CLI surface.

**Verified:** `randonneur serve --no-browser --port 8765 --verbose`
boots, serves `/api/folder?path=tests/fixtures` (3 tracks, 0 errors),
`/api/folder` (no path → empty), `/api/folder?path=/tmp/missing` → 404,
`/` → 404 (static mount exists but no files yet, expected). SIGTERM
produces "Shutting down (signal 15) → Stopped". 28 tests pass.

### #5 — Profile-→-map sync math: assumed a "pure N" segment, got a diagonal

**Symptom:** First run of `tests/test_sync_math.py` failed with
`lon midpoint wrong: 11.4505` (expected 11.4500). The math was right;
the test expected a pure-N segment but the multi_segment fixture's
first move is diagonal (lat AND lon change).

**Root cause:** I designed the test with a mental model of the fixture
("first move is a tiny north step") that didn't match the actual
fixture (first move is a diagonal step from (46.5500, 11.4500) to
(46.5510, 11.4510)). This is a sibling of bug #3 — the same trap of
matching a hand-derived expected value against a different convention.
The function is right; the test was wrong.

**Fix:** Updated the expected midpoint to (46.5505, 11.4505). The
diagonal test is actually a *better* test of the algorithm than the
pure-N version would have been — it exercises both lat and lon
interpolation together.

**A second issue, same commit:** the `upper_bound_index` Python twin
and its JS counterpart both indexed `arr[0]` without checking for an
empty array. The test caught it; the fix is a one-liner guard at the
top of both. Made the JS and Python keep matching.

**Verified:** All 7 sync-math tests pass (midpoint, exact-hit, last
point, cross-segment boundary, out-of-range, short-inputs, empty
array). 44 tests total. The Python twin mirrors the JS line-for-line
so the algorithm has parity — a real browser run is still the only
proof the wiring is right, but the math itself is fully covered.

### #6 — "Drop the first batch (attach snapshot)" was correct in theory, flaky in practice

**Symptom:** Commit 10's WS-broadcast test was 60-90 % flaky. On
the failing runs, the listener received the first batch from
`watchfiles.awatch` and the batch contained the *new* file (b.gpx)
but *not* the pre-existing file (a.gpx) — and the test asserts on
the *first* message only, so it failed to find b.gpx when the
"attach snapshot" theory predicted b.gpx would arrive in a second
batch.

**Hypothesis 1:** The FSEvents watch was attached too late on slow
runs, so the first batch was the *change* batch instead of the
*snapshot* batch. Try to *falsify*: ran a standalone `awatch` with
`yield_on_timeout=True, step=200, rust_timeout=1000` and confirmed
that without `yield_on_timeout` the first batch is *never* a
snapshot — it's empty until a real change arrives. The "attach
snapshot" is delivered as the first non-empty batch, which on a
fast attach contains only existing files and on a slow attach is
contaminated by the post-attach write.

**Hypothesis 2:** Maybe the right move is to track which files
existed at attach time and only broadcast new ones. Try to
*falsify*: that requires the watcher to know the attach-time
contents, which it does internally (the Rust watcher maintains a
set of known files) but doesn't expose to Python. Without an API
hook, this would mean doing my own `os.scandir` at attach and
diffing — more code, more race windows, not better.

**Root cause:** The "first batch is the snapshot" assumption only
holds when nothing changes during the attach phase. When the test
(or the user) writes a file *between* the watcher attaching and
the first batch being delivered, that write shows up in the first
batch. Dropping the first batch is therefore *wrong*, not just
sometimes-wrong: it's a race that loses changes on the slow side
of the timing window.

**Fix:** Stop dropping the first batch. Always broadcast. The
client's `refreshFolder` is idempotent — re-fetching on the
attach snapshot just re-renders the same list. The only cost is
one extra `/api/folder` round-trip on every folder load, which is
a few ms. Both the in-process integration test and the
cross-process real-server smoke (`uvicorn` + a separate `cp`) now
work reliably, 10/10 full-suite runs pass.

**Lesson:** When the producer (watchfiles) and the consumer (the
test / the user) live on different timelines, you cannot
distinguish "the event I saw is the snapshot" from "the event I
saw is a real change" by timestamp alone. Either accept the
extra message or move the snapshot bookkeeping into the
producer. The extra message is cheaper than the bookkeeping.

**Verified:** `pytest tests/` passes 10/10 runs. Real-server
smoke: start `randonneur serve --no-browser` in one process,
connect a WebSocket listener in a second process, `cp` a new GPX
into the watched folder in a third process — listener receives
`{"type":"changed","folder":"...","files":[".../b.gpx"]}` within
~500 ms.

### #7 — Two bugs in one tile: 404 is *not* an upstream error, and an un-awaited future leaks

**Symptom (real run, `randonneur serve` over `data/2026-07-18_15-08_Sat.gpx`):**
the map fired off tile requests to OpenTopoMap for a polyline that
straddles the Mediterranean coastline. Five of the z=17 tiles
returned 404 from upstream ("no tile here at this zoom — outside
coverage"). The server logs filled with `Future exception was never
retrieved` warnings, one per 404, each tracing back through
`api_tile → fetch_tile_bytes → _fetch_with_coalesce → _do_fetch →
resp.raise_for_status()`. On a clean Ctrl-C shutdown, the warnings
scrolled past and the user couldn't tell what had actually gone
wrong.

**Hypothesis 1:** "the 404 is being treated as an upstream error
and re-raised, breaking the AsyncClient cleanup." Try to *falsify*:
read `_do_fetch` — it uses `async with httpx.AsyncClient(...) as
client`. The `async with` calls `client.aclose()` in `__aexit__`,
but if `__aexit__` is re-entered while the response stream's
read-future is still pending, that future gets GC'd with its
exception unretrieved. Confirmed: the traceback goes through
`raise_for_status` → `__aexit__` → orphan future. So the
`async with` is the proximate cause of the warning.

**Hypothesis 2:** "but more fundamentally, 404 is the *right*
answer for these tiles, not an error — the server should return
404 to the browser, not 502." Try to *falsify*: Leaflet 1.9's
`tileerror` handler fires on any non-2xx response; 502 makes
Leaflet show a red "broken tile" icon, 404 makes it show a clean
blank square. The latter is what the user expects over open
ocean at z=17. Also, 502 is semantically wrong: 502 means "I am
a gateway and my upstream is broken", but in this case the
upstream is fine — it just doesn't have the tile. The right HTTP
code is 404, the right tile-library exception is a custom
`TileNotFoundError` (not `httpx.HTTPStatusError`).

**Root cause:** two coupled mistakes.

1. `tile_cache._do_fetch` was calling `resp.raise_for_status()`
   unconditionally. That mapped *all* 4xx to the same
   `HTTPStatusError`, which the server's `except httpx.HTTPError`
   branch turned into 502 to the browser. A 404 from a tile
   server is "I have no tile for this location at this zoom" —
   a normal answer, not an error.
2. `_do_fetch` was an `async with httpx.AsyncClient`. When the
   404 raised, the client's `__aexit__` re-entered with a still-
   pending read-future on the response stream. That future was
   then GC'd with its exception unretrieved, producing the
   "never retrieved" warning.

**Fix:**

1. New `tile_cache.TileNotFoundError`. `_do_fetch` checks
   `resp.status_code == 404` *before* `raise_for_status()` and
   raises `TileNotFoundError` instead. The server handler
   catches `TileNotFoundError` and returns 404 to the browser.
   Other 4xx/5xx still go through `raise_for_status()` and the
   existing 502 path.
2. `_do_fetch` now uses an explicit `httpx.AsyncClient()` +
   `try / finally: await client.aclose()` instead of `async
   with`. `aclose()` awaits the connection cleanup, so the
   read-future is consumed before the exception escapes.
3. While testing the above, a *third* related leak surfaced in
   `_fetch_with_coalesce`: on the originator's path, the
   coalescing future had `set_exception(exc)` called on it
   before the originator re-raised, but no one ever `await`ed
   that future (the originator's `raise` propagates its own
   exception, not the future's). The future sat in `_in_flight`
   until the `finally` block popped it, then was GC'd with its
   exception unretrieved. Reworked `_fetch_with_coalesce` so
   both the originator and any waiters `await` the same future
   — populated via `task.add_done_callback` — so the future's
   result/exception is consumed in all paths.

**Why one exception, not a status-code sentinel:** the server
already special-cases `ValueError` (unknown source),
`RuntimeError` (unconfigured source), and `httpx.HTTPError`
(upstream 5xx). A `TileNotFoundError` joins that family: it's
"the upstream explicitly said no", distinct from
"something went wrong". Mixing it back into the
`httpx.HTTPError` branch would force the server to inspect
`e.response.status_code`, which is an httpx-specific detail
that doesn't belong in the FastAPI layer.

**Lesson (compounding with bugs #3 and #5):** an HTTP status
code is a contract. 4xx and 5xx mean very different things to
clients (a broken-tile icon vs a blank square, in Leaflet's
case). Treating them all as "upstream error" loses information
the upstream is deliberately sending. The right move when
writing a tile proxy is to *enumerate* the 4xx codes the
upstream uses for "this is not a tile" (just 404 today, but
possibly 410 Gone or 451 Unavailable-For-Legal-Reasons
tomorrow) and translate each to a distinct exception.

**Verified:** 89/89 tests pass. Real-server smoke: the five
tiles from the original failure (z=17, x=67924..67928,
y=47788) all return clean 404 from `/api/tiles/opentopomap/...`
with zero `Future exception was never retrieved` warnings. A
20-call burst through `_fetch_with_coalesce` (10 concurrent
waiters + 10 sequential originator calls, all 404) leaves the
event loop's "never retrieved" handler silent. A new
`test_tile_not_found_does_not_leak_future_under_concurrent_waiters`
asserts exactly that.

### #8 — Map is blank: `#map` has zero height because `.map-pane` isn't a flex container

**Symptom:** user reported "the map area is blank even when I
select a gpx file. The elevation works, so does the folder
discovery and the GPX file list." Confirmed on a real-server
run via a headless Chrome screenshot: the randonneur page
loaded, the sidebar listed the track, but the map pane was
just a white rectangle. The status line said "1 track(s)",
the elevation profile rendered correctly below.

**Hypothesis 1:** "the polyline never reached the client —
the API returned something the JS couldn't parse." Try to
*falsify*: `curl /api/tracks/<id>` returns a clean JSON
object with `points: [{lat, lon, ele}, ...]`, `color: "#4363d8"`,
`bbox: [6.5590769, 43.7299424, 6.5731413, 43.7613978]`, etc.
The data is fine.

**Hypothesis 2:** "Leaflet itself failed to init — the CDN
URL is broken or there's a CSP issue." Try to *falsify*: the
`L` global is present (`typeof L !== 'undefined' === true`
in the headless browser), the `#map` element has the
`leaflet-pane leaflet-map-pane` child, and the tile container
has 15 `<img class="leaflet-tile">` elements. Leaflet is
running fine and is happily requesting tiles.

**Hypothesis 3:** "JS error during boot." Try to *falsify*:
captured `Runtime.exceptionThrown` events via Chrome's
DevTools Protocol — zero. The page runs clean.

**Hypothesis 4:** "the `#map` element has zero size, so
Leaflet renders into a 0×0 area even though it doesn't
error." Try to *falsify*: from CDP,
`mapEl.getBoundingClientRect()` →
`{x: 240, y: 91, width: 1145, height: 0, ...}`. The
element has 1145px of width but 0px of height. **That's the
bug.**

**Root cause:** the layout CSS in commit 5 set
`#map { width: 100%; flex: 1; min-height: 0; }`. The
intention was that `#map` would expand to fill the map pane
via `flex: 1`. But `flex: 1` only takes effect if the parent
is a flex container. The profile pane was made a flex
container in commit 5 (`display: flex; flex-direction: column;`
on `.profile-pane`), so `#profile` correctly fills its 240px
row. The map pane was *not* given the same treatment —
`.map-pane` is just a plain grid item, with no `display: flex`.
So `#map`'s `flex: 1` is a no-op, and the element collapses
to its intrinsic height, which for a Leaflet container is 0
(Leaflet explicitly does not set its own size; it expects
the host element to have one).

This is a textbook "the rule that works in one place
silently does nothing in the other" bug — `#map` and
`#profile` shared a selector, the shared rule worked for
one (because of an outer rule on its parent) and not the
other (no outer rule on its parent). The unit tests
(`pytest tests/test_server.py`) caught the FastAPI surface
but not the layout, because layout requires a real browser.
The behaviour file's §8 ("test against the real thing, not
only stubs") is exactly this gap.

**Fix:** move `display: flex; flex-direction: column;` up
to the shared `.map-pane, .profile-pane` rule, and keep
`.profile-pane`'s override for `border-bottom: none`. Now
both panes are flex columns, `#map` and `#profile` get the
same `flex: 1` treatment, and a future pane added to the
right column gets the same layout for free.

**Found while there:** the JS `upperBoundIndex` function
(profile→map sync helper) was missing its
`let lo = 0, hi = arr.length - 1` declarations. The
`while (lo < hi)` body used `lo` and `hi` without ever
declaring them, so on the first call the function threw
a `ReferenceError` and the profile→map crosshair would
have silently broken (Leaflet's `tileerror`-style fallback
for JS errors is just "do nothing"). The Python twin in
`tests/test_sync_math.py` has the right initialization, so
this was a JS-only drift — visible to a real browser run
(hover the profile, see no crosshair) but invisible to
`pytest`. Fixed by adding the missing `let` declarations
and noting the parity contract in the JS comment.

**Lesson:** a CSS rule that *appears* to apply via a shared
selector is a trap. `#map` and `#profile` shared a rule, but
they lived under parents with different display values, so
one of them was effectively unruled. A future-proof version
of this is to *only* use `flex: 1` on children whose parent
is verifiably flex, and to test the layout in a real browser
(`--remote-debugging-port` + `getBoundingClientRect` is the
cheapest way). A bare `pytest` run that passes 89/89 tests
was not enough to catch this; the behaviour file's "real
run" rule was.

**Verified:** with the fix, `mapEl.getBoundingClientRect()`
returns `{width: 1145, height: 467, ...}` (was 0). 15 tile
`<img>` elements load. Headless Chrome screenshot shows the
OpenTopoMap basemap, the polyline, the sidebar track, and
the elevation profile. `pytest tests/` still 89/89.

### #9 — `log._SymbolFormatter` built a styled `Text` then returned `.plain`, throwing away every colour

**Symptom (latent — no user report):** the module docstring and
§5 of the behaviour file describe a coloured severity symbol
(dim `·` for INFO/DEBUG, yellow `⚠` for WARNING, red `✗` for
ERROR), but every log line rendered plain. The structure
(`· feature · message`) was right; only the colour was missing.

**Hypothesis:** "`RichHandler` honours the formatter's returned
string and rich re-parses it for styles." Try to *falsify*:
inspected the installed rich's `RichHandler.emit` — it calls
`self.format(record)`, then `render_message(record, message)`,
then `console.print(log_renderable)`. `render_message` wraps the
formatted message in a fresh `Text(message, style="log.message")`
when `markup=False` + `highlighter=NullHighlighter()`. So whatever
the formatter returns is treated as a *plain string* and re-wrapped
— any styles baked into a `Text` inside the formatter are
discarded the moment `.plain` is returned. Confirmed: our
`_SymbolFormatter.format` built a `Text` with `style=` segments and
then returned `text.plain`, i.e. the unstyled string. The colour
never reached `console.print`.

**Root cause:** the styling was done in the wrong layer. A
`logging.Formatter.format` must return a `str`, so building a
`Text` there is a dead end — the string is the only thing that
escapes, and rich treats it as literal. The intended extension
point for rich-styled output is `RichHandler.render_message`,
which returns a `ConsoleRenderable` that `console.print` renders
directly.

**Fix:** dropped `_SymbolFormatter` entirely. Subclassed
`RichHandler` as `_SymbolHandler` and overrode
`render_message(record, message)` to build and return the styled
`Text` (symbol + bold feature + dim "·" + literal message). No
formatter is set on the handler, so `emit`'s `self.format(record)`
returns `record.getMessage()` (the raw message) and
`render_message` decorates it. `markup=False` and
`highlighter=NullHighlighter()` stay, so message text containing
`[...]` (e.g. a bracketed folder path) is never mis-parsed as rich
markup — verified explicitly.

**Why not switch to `markup=True` + markup strings:** that would
re-introduce the `[...]`-in-messages hazard the `markup=False`
setting exists to prevent. Returning a renderable from
`render_message` keeps the message literal *and* gets the colour
through.

**Lesson (compounding with #4):** the original bug #4 (rich 13
renamed `highlight` → `highlighter`) was fixed by passing the
right kwarg, but the formatter-returns-`.plain` shape was already
discarding colour at the time and nobody noticed — because the
test client never goes through `configure_logging`, and a piped
`randonneur serve` run strips colour anyway (rich detects
non-TTY). The only way to see this was either (a) a real TTY run
or (b) capturing with `Console(force_terminal=True)`. The
behaviour file's "test against the real thing" rule applies to
*rendered output* too, not just to crashes — a colour bug is
invisible to every test that doesn't render to a terminal.

**Verified:** (1) In-process capture with
`Console(force_terminal=True, color_system="truecolor")` shows
ANSI escapes — `\x1b[2m` dim for `·`, `\x1b[1m` bold for the
feature, `\x1b[33m` yellow for `⚠`, `\x1b[31m` red for `✗` — and
`loaded [ bracketed ] path` passes through literally. (2) Real
`randonneur serve --no-browser --port 8801 --verbose`: boots
through `configure_logging`, `GET /api/folder?path=tests/fixtures`
→ HTTP 200 (3 tracks, 0 errors), clean SIGTERM ("Shutting down
(signal 15)" → "Stopped"), no exceptions. `pytest tests/` 90/90.

## Decisions

- 2026-07-17 — Created `agent/agent-history.md` as a fresh running log per the
  behaviour-file convention. Project body and stack are TBD; awaiting
  description from the human.
- 2026-07-17 — Stack and layout locked in the approved plan. (Plan file:
  `~/.claude/plans/curried-watching-popcorn.md`.)
- 2026-07-17 — Python floor set to 3.12 (system Python 3.9.6 is too old;
  installed 3.12.13 via Homebrew).
- 2026-07-17 — Commit 1: project bootstrap (pyproject, package skeleton,
  cli, README placeholder, .gitignore). Bug-hunt #1 resolved.
- 2026-07-17 — Commit 2: `gpx_loader.discover` + `parse`, debug CLI
  `randonneur discover <folder>`, 12 unit tests against two fixture GPX
  files. Bug-hunt #2 resolved.
- 2026-07-17 — Commit 3: `profile.compute_profile` (haversine cumulative
  distance, no smoothing, None-elevations → 0.0 to keep arrays aligned),
  9 new tests against a hand-checkable fixture; total 21 passing.
- 2026-07-17 — Commit 4: FastAPI server with `GET /api/folder`, CLI
  `randonneur serve` (uvicorn-in-thread, browser auto-open, SIGINT/TERM
  handling), `log.py` (rich-backed, re-callable), `config.py`. 7 new
  server tests using FastAPI's TestClient; total 28 passing. Bug-hunt
  #4 resolved.
- 2026-07-17 — Commit 5: static UI shell (`index.html`, `style.css`,
  `app.js`) with two-pane CSS grid layout, sidebar track list, "paste
  folder path" + `webkitdirectory` picker, error panel, ?path= deep
  link. No map yet — placeholder panes. 2 new server tests; total 30
  passing.
- 2026-07-17 — Commit 6: Leaflet map wired in via CDN. New endpoint
  `GET /api/tracks/{id}` returns full polyline + summary. Map draws
  every track as an `L.polyline` with permanent hover tooltip, click-
  to-select, auto-fit to selected (or to all on folder load). Added
  in-memory `_TRACK_CACHE` reset on each folder load. 3 new server
  tests; total 33 passing.
- 2026-07-17 — Commit 7: Plotly elevation profile wired in via CDN.
  New endpoint `GET /api/tracks/{id}/profile` returns aligned
  `(distances_km, elevations_m)` arrays (via `profile.compute_profile`).
  Sidebar click fetches and renders a filled line plot in the bottom
  pane, with title + stats line. Shared cache via `_get_cached()`
  helper. 4 new server tests; total 37 passing.
- 2026-07-17 — Commit 8: two-way hover sync. Profile hover drops a
  crosshair on the map (`L.circleMarker` at the interpolated lat/lon
  for the hovered distance); map polyline mousemove draws a vertical
  Plotly shape on the profile. Added `tests/test_sync_math.py` (a
  Python twin of the JS `latLonAtDistance` / `upperBoundIndex` so the
  algorithm is exercised against the real GPX fixtures). 7 new math
  tests; total 44 passing. Bug-hunt #5 resolved.
- 2026-07-18 — Commit 9: disk tile cache + per-host rate limiter.
  Browser tile requests now go through `GET /api/tiles/<source>/<z>/<x>/<y>.png`
  instead of hitting OpenTopoMap directly. `tile_cache.fetch_tile_bytes`
  serves from `~/.cache/randonneur/tiles/<source>/<z>/<x>/<y>.png` on
  hit, fetches and atomically writes (`tmp` + `os.replace`) on miss.
  `rate_limit.RateLimiter` is a per-host async gate: `limiter_for(source, rate)`
  returns a process-global singleton so concurrent fetches share the
  budget (verified: 10 concurrent requests for the same new tile
  coalesce into 1 network fetch via the `_in_flight` futures dict;
  5 sequential new tiles take 4 s at 1 req/s, the OpenTopoMap policy).
  `config.TILE_URL_TEMPLATES` + `TILE_RATE_LIMITS` whitelist only
  `opentopomap` and `thunderforest-outdoors`; unknown sources get 404
  so the endpoint can't be used as an open proxy. 7 new tile/rate
  unit tests + 3 integration tests (real OpenTopoMap fetch, marked
  `pytest -m integration`) + 4 server tests for the endpoint (200/404/
  502/422). Total: 60 unit + 3 integration passing.
- 2026-07-18 — Commit 10: hot-reload via `watchfiles.awatch` + a
  WebSocket broker. New module `randonneur.watcher` owns per-app
  state: the active folder, the long-lived watcher task, the list of
  subscribed WS clients (each with a bounded queue). The watcher task
  is started in the FastAPI lifespan (so it survives the TestClient
  per-request portal); a new `GET /api/folder` call updates
  `active_folder` and signals a change event. The WS handler at
  `/api/ws` registers the client with the broker and pumps the queue
  to the socket. JS `app.js` opens the WS on page load and on every
  "changed" message re-fetches `/api/folder`; the WS auto-reconnects
  after 1 s on close. 8 new watcher tests (6 unit + 1 set-folder
  switch + 1 integration real-file-change) + 3 server tests
  (WS receives change, disconnect unregisters, accept-before-folder).
  Total: 71 unit + 1 integration passing, stable across 10 consecutive
  full-suite runs. Real-server smoke (separate uvicorn process +
  separate `cp` to write a new file) now works too — see Bug-hunt
  #6 below. Bug-hunt #6 resolved.
- 2026-07-18 — Commit 11: settings panel + Thunderforest switch.
  New endpoint `GET /api/settings` returns the whitelist of tile
  sources with per-source `available` and `needs_key` flags — the
  panel can render Thunderforest as "missing key" instead of
  502ing on the first map pan. The Thunderforest API key is read
  from `RANDONNEUR_THUNDERFOREST_KEY` (lazy `os.environ.get`) and
  substituted into the URL only when the source is actually
  fetched; it never leaves the server (the browser only sees
  `/api/tiles/<source>/...`). The tile endpoint now distinguishes
  unknown (404) from unconfigured (503) from upstream-error (502),
  and `tile_cache.is_source_available` is the single source of
  truth for "can we fetch this right now". The UI has a ⚙ button
  in the header that opens a popover with a source radio list
  (disabled rows show the env-var hint) and a "show scale bar"
  checkbox wired to `L.control.scale`. On source change, the
  active `L.tileLayer` is swapped in-place so the map doesn't
  flash blank. The base URL is the server proxy
  (`/api/tiles/<source>/...`), not the upstream — that's the
  whole point of the proxy. 7 new tests: 5 unit (is_source_available
  for each state, _render_url with/without apikey, fetch rejects
  unconfigured) + 3 server (`/api/settings` shape, Thunderforest
  unavailable without key / available with key, tile endpoint 503).
  Total: 81 passing, stable across 10 consecutive runs. Real-server
  smoke: `curl /api/settings` returns the expected shape; with the
  key unset, `/api/tiles/thunderforest-outdoors/...` → 503; with
  the key set, → 502 (upstream rejects the fake key) — correct
  routing of "missing key" vs "key bad" vs "key good".
- 2026-07-18 — Commit 12: README with quickstart. Replaced the
  placeholder with a full `README.rst`: title + badge, Why this
  exists (GPXSee / gpx.studio landscape reference), Install (venv
  + `[dev]` extra), Run (`randonneur`, `--port`, `--no-browser`,
  `--host`, `-v`, SIGTERM/SIGINT shutdown, `discover` subcommand),
  Select your first folder (the textbox + `Pick…` "no absolute
  paths" caveat), What you get (two-pane ASCII sketch), Base
  layer and tiles (OpenTopoMap default, Thunderforest via
  `RANDONNEUR_THUNDERFOREST_KEY`, cache reset), Project layout,
  Development (`pytest`, `-m integration`, the "bare pytest is
  not a substitute" rule from the behaviour file, `docutils
  --strict` for README edits), Limitations and known sharp edges
  (no smoothing, picker caveat, in-memory cache, no auth, no
  editing), License (MIT). New `tests/test_docs.py` with 4
  guards: (1) in-process `docutils.publish_string` with
  `report_level=2`, `halt_level=2`, `writer='html'` (the
  non-deprecated spelling — `writer_name=` raises a
  PendingDeprecationWarning under docutils 0.23+); (2) the same
  command as a subprocess (`python -m docutils --strict README.rst`)
  so a failed install / wrong docutils version fails the same
  way the user would see it; (3) every CLI subcommand is named in
  the README; (4) every env var the app reads is documented. The
  in-process test was kept because, on second look, it does
  catch a malformed RST (e.g. short title underline) — my first
  read of the test had me thinking it was lenient, but the
  SystemMessage does escape `publish_string` and reach
  `pytest.fail`. The two tests cover different failure modes
  (in-process = fast, no subprocess overhead; subprocess =
  matches the exact command from `agent-behaviour.md` §1) so
  keeping both is the right shape. Total: 85 passing.
- 2026-07-18 — Post-commit fix (no version bump): Bug-hunt #7.
  Two coupled bugs surfaced together on a real-server run
  against a Mediterranean-coastline track that hit five
  z=17 tiles OpenTopoMap doesn't carry (404). (1) The cache
  was mapping every 4xx to 502; a 404 is a normal "no tile
  here" answer, not an upstream error, so a new
  `TileNotFoundError` carries it through the cache layer
  and the server turns it into a 404 to the browser (Leaflet
  shows a clean blank instead of a red broken-tile icon).
  (2) `_do_fetch` was `async with httpx.AsyncClient`; the
  404 escape path left the client's response-stream
  read-future GC'd with its exception unretrieved, producing
  the "Future exception was never retrieved" warnings. Replaced
  the `async with` with an explicit `aclose()` in `finally` so
  the read-future is awaited before the exception escapes.
  (3) While there, also fixed a parallel leak in
  `_fetch_with_coalesce`: the originator was setting the
  coalescing future's exception then re-raising its own, so
  the future was never `await`ed and was GC'd with the
  exception unretrieved. Both originator and waiters now
  `await` the same future (populated via
  `task.add_done_callback`). 4 new tests: 1 server
  (`/api/tiles/...` → 404 for outside-coverage) + 3 unit
  (404 raises `TileNotFoundError`, AsyncClient is closed
  before the exception escapes, no "never retrieved"
  warnings on the originator path or with 10 concurrent
  waiters). Total: 89 passing. Real-server smoke
  (`randonneur serve` + 5 curls against the originally
  failing tiles + a real Mediterranean track) is silent.
- 2026-07-18 — Post-commit fix (no version bump): Bug-hunt #8.
  User reported "the map area is blank even when I select a
  gpx file. The elevation works, so does the folder
  discovery and the GPX file list." Confirmed in a real
  browser via headless Chrome: `#map.getBoundingClientRect()`
  was `{width: 1145, height: 0, ...}`. The map element had
  zero height even though Leaflet was running and requesting
  tiles (15 `<img class="leaflet-tile">` elements present).
  Root cause: `style.css` set `#map { width: 100%; flex: 1;
  min-height: 0; }` but `.map-pane` (its parent) was not a
  flex container — the `display: flex; flex-direction: column;`
  was on `.profile-pane` only, where the rule worked because
  `#profile` happened to be a flex child. The shared
  selector silently did nothing in one of the two cases.
  Fix: moved `display: flex; flex-direction: column;` up to
  the shared `.map-pane, .profile-pane` rule, kept
  `.profile-pane`'s `border-bottom: none` override. Both
  panes are now flex columns, `#map` and `#profile` get the
  same `flex: 1` treatment, and any future right-column
  pane gets the layout for free. While there, also fixed a
  `ReferenceError` in JS `upperBoundIndex` (used by the
  profile→map crosshair): the function body used `lo` and
  `hi` without declaring them. The Python twin in
  `tests/test_sync_math.py` had the right initialization,
  so this was a JS-only drift — invisible to `pytest`,
  visible in a real browser. Added the missing
  `let lo = 0, hi = arr.length - 1` declarations and a
  parity comment. Verified via headless Chrome:
  `mapRect.height: 467` (was 0), 15 tiles loaded, 1
  polyline, OpenTopoMap basemap visible, polyline drawn in
  southern France. `pytest tests/` still 89/89.
- 2026-07-19 — Code-quality pass (approved plan:
  `~/.claude/plans/cached-roaming-boot.md`). Four units, each
  verified: (1) Per-app track cache — moved `server._TRACK_CACHE`
  from a module global into per-app `_AppState` keyed on `id(app)`,
  mirroring `watcher.state_for`; two apps in one process no longer
  share caches. Added `_state_for` / `reset_for_tests` and the
  `test_track_cache_is_per_app` regression test. (2) Dead-code &
  scratch cleanup — removed the unused `dead` list in
  `watcher._broadcast`, the unused `is_gpx_change` helper (and the
  `Change` import it needed), the dead `DEFAULT_HOST`/`DEFAULT_PORT`
  re-export in `server.__all__`, the in-function `import time` in
  `cli._wait_for_ready`, and the stale `tests/__pycache__/_repro*.pyc`.
  (3) Bug-hunt #9 — the log colour bug; see above. (4) README /
  pyproject consistency — `requires-python` bumped to `>=3.12`
  (matches the locked floor), README badge + alt updated, the
  Project-layout test-file list corrected
  (`test_rate_limit.py` → `test_tile_cache.py`), and commit-numbered
  comments trimmed to timeless "why" notes across `src/` and
  `tests/`. `pytest tests/` 90/90; README re-validated with
  `docutils --strict`; real `randonneur serve` smoke clean.
