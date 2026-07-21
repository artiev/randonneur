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

### #10 — Page-height overflow: `calc(100vh - 49px)` hard-coded a header height that no longer matched

**Symptom:** a phantom vertical scrollbar on a page that had
nothing to scroll to. The grid spilled ~30 px below the viewport.

**Hypothesis:** "some element has an explicit height larger than
its container." Try to *falsify*: read `style.css` —
`.app-grid { height: calc(100vh - 49px) }`. The `49px` is a
hand-picked offset for the header. The header has never been
49 px since the settings tab landed: the right-side tab panel,
the status line, the folder-info span, and the FA gear icon
all moved in and grew it to ~79 px. So `calc(100vh - 49px)`
oversizes the grid by the ~30 px the header grew, and the grid
extends past the viewport. **That's the bug.**

**Root cause:** the layout pinned the header height in CSS
arithmetic instead of letting the browser compute it. A header
is a fluid thing — every chrome change (a status line, a new
control, an icon swap) silently breaks a `calc(100vh - Npx)`
grid. This is a sibling of bug-hunt #8: a CSS rule that worked
once and silently stopped working when the surrounding layout
changed, invisible to `pytest` because layout requires a real
browser.

**Fix:** turn the body into a flex column so the header takes
its natural height, and give `.app-grid { flex: 1 1 0;
min-height: 0 }` so the grid takes exactly the remaining
viewport regardless of how tall the header actually is.
`min-height: 0` is the crucial half — without it, a flex item
won't shrink below its content height and the overflow
returns. `min-height: 100dvh` on the body (with a `100vh`
fallback) handles mobile browsers whose URL bar collapses:
the grid anchors to the visible viewport, not the inflated
one. The hardcoded `calc(100vh - 49px)` is gone; a regression
test asserts this so a future revert is caught in CI.

**Why flex over `calc(100vh - HEADER_H)`:** hand-picking the
header height is exactly what broke. `flex: 1` means the grid
gets the leftover space, so any future header change is
silently absorbed instead of silently breaking.

**Lesson (compounding with #8):** never hard-code a
sibling element's height in CSS arithmetic. The header and
the grid are siblings under the body; the grid's height
should be a *function of the body's remaining space*, not a
guess at the header's height. `flex: 1 + min-height: 0` is
that function. And — again — a bare `pytest` run that passes
was not enough to catch this; the new
`tests/test_headless.py` opens the page in headless Chrome at
three viewport sizes and asserts
`document.body.scrollHeight <= window.innerHeight` for each.
It's marked `integration` (needs a Chrome binary) and skips
cleanly when absent, so the default `pytest` run still passes
on CI images without a browser.

**Verified:** `pytest tests/` 110/110. Headless Chrome at
1200×800, 1920×1080, and 900×600 all report
`scrollHeight <= innerHeight`. The brittle `calc(100vh - N)`
pattern is gone from the stylesheet.

### #11 — Settings-tab open: a third grid column for a `position: fixed` tab reshuffled the profile pane out of the bottom row

**Symptom:** opening the right-side settings tab made the
elevation profile graph jump up out of its bottom row.

**Hypothesis:** "the settings panel is covering the profile."
Try to *falsify*: the panel is `position: fixed` at
`top: 49px; right: 0; bottom: 0; width: 300px` — it occupies
the right 300px, but the profile pane lives in the bottom-left
(grid-area: profile), so the panel doesn't overlap it. The
profile didn't get covered; it *moved*.

**Root cause:** `body.settings-open .app-grid` changed
`grid-template-columns` from `240px 1fr` to `240px 1fr 300px`,
intended to "make room" for the right-side tab. But the tab is
`position: fixed` — it is taken out of flow and does not
participate in the grid at all, so the third column was a
no-op for the tab. What it *did* do was change the grid from
two columns to three, and `.profile-pane` (whose
`grid-template-areas` only names two areas — `"sidebar map"` /
`"sidebar profile"`) had no third-area assignment, so it
auto-flowed into the new right column instead of staying in
the bottom row. The graph visually jumped up the right edge
the moment the tab opened. A textbook "fix the wrong layer"
shape: the author reached for grid columns to size around a
fixed element, but fixed elements are invisible to the grid.

**Fix:** drop the column change. Replace it with
`margin-right: 300px` on `.app-grid` when the tab is open.
The fixed tab doesn't participate in the grid, and a right
margin on the grid shrinks the sidebar + map + profile
uniformly to the left of the tab — no item reflows into a
different row. The grid template stays two columns / two rows
in both states.

**Why margin and not a third column:** the only thing that
needs to "make room" for the fixed tab is the grid's *box*
itself, not its internal track structure. A margin sizes the
box; the tracks inside are unchanged. A new column would only
be right if the tab were an in-flow grid item, which it isn't.

**Lesson (compounding with #8 and #10):** `position: fixed`
takes an element out of flow — it is invisible to the grid
and to flex layout. "Add a grid column to make room for the
fixed thing" is always wrong: it doesn't move the fixed thing
and it reshuffles the in-flow siblings. The right lever for
"make room for an out-of-flow element" is a margin/padding on
the in-flow container, not a new track. And again — a bare
`pytest` run that passes 110/110 did not catch this; a
headless-Chrome check that toggled `.settings-open` and
measured the profile pane's rect did. The profile's `top`
staying below the map's `bottom` is the assertion that pins
it.

**Verified:** `pytest tests/` 110/110. Headless Chrome with
the settings tab open: `.app-grid` computed `margin-right:
300px`, `.profile-pane` top stays below the map's bottom
(does not auto-flow up), profile height intact. (One-off
check run via a temporary integration test, since removed;
the fix is guarded going forward by the existing layout
tests.)

### #12 — Profile stat line disagreed with the sidebar by ~2000 m whenever a track had an elevation gap

**Symptom:** for the `elevation_gaps` fixture (elevations
2000, None, 2050, 2030), the sidebar showed `↑50 m · ↓20 m`
(correct) while the profile pane showed `↑ 2050 m · ↓ 2020 m`
with a bogus `0–2050 m` range — two displays of the same track
disagreeing by ~2000 m of climbing.

**Hypothesis:** "the client-side gain/loss recompute has a bug."
Try to *falsify*: the JS `formatProfileStats` loop itself is
fine — it sums positive/negative deltas the same way the
backend does. The bug isn't in the loop; it's in what the loop
iterates over. The profile endpoint (`profile.compute_profile`)
substitutes `None → 0.0` to keep the elevation array
index-aligned with distances for Plotly and the hover-sync, so
`elevations_m` arrives as `[2000, 0, 2050, 2030]`. The JS has
no way to tell a real 0 m reading from a GPS dropout — the
`None` information is destroyed server-side before the client
sees it. The recompute then dutifully sums `2000→0` (-2000)
and `0→2050` (+2050): a phantom 2000 m plunge and 2050 m climb
that never happened, plus a 0 m minimum. The existing
`formatProfileStats` comment even *claimed* it "skips the
None→0 substitutes" — but it can't; they're already substituted.

**Root cause:** duplicated computation with asymmetric gap
handling. The backend `_elevation_gain_loss` skips `None`
gaps (correct — a dropout isn't a 0 m sample); the client
recomputes the same stat from an array where the gaps have
already been replaced by 0.0 (wrong). One source of truth, two
different views of it, and the wire format silently rewrote the
data between them. The same root cause corrupted the client-side
min/max (`Math.min(...elevations_m)` returned 0 for any gapped
track).

**Fix:** one source of truth. The profile endpoint now ships
the backend's gap-aware `elev_gain_m` / `elev_loss_m` (computed
once at parse time) plus a new gap-aware `elev_min_m` /
`elev_max_m` from a new `gpx_loader._elevation_min_max` helper
(skips `None`, returns `(None, None)` when no point has an
elevation). `formatProfileStats` reads those four fields
directly and stops recomputing from `elevations_m` — the
array still carries 0.0 for gaps (Plotly + the hover-sync need
the index alignment), but it's no longer used for stats. The
profile stat line now matches the sidebar by construction (same
numbers, same source), and the range is the real 2000–2050, not
0–2050. The duplicated client-side math is retired, so no
`test_sync_math.py` parity twin is needed — the parity gap is
removed, not twinned.

**Lesson (compounding with #5):** when the same quantity is
computed in two places, the wire format between them is a
silent rewrite boundary. Here the rewrite was `None → 0.0` —
harmless for the Plotly trace (which just draws a dip), fatal
for a downstream stat that assumed it was seeing real
elevations. The fix isn't to make the downstream stat
gap-aware too (it can't be, once the `None` is gone); the fix
is to not recompute — ship the authoritative number and display
it. And a comment that asserts behaviour the code can't
perform ("skips the None→0 substitutes") is worse than no
comment: it talks the next reader out of looking. The regression
guard is a value-level pin on the profile response
(`elev_gain_m == 50`, `elev_min_m == 2000` — not the 0/2050 the
old recompute produced), plus edge-case unit tests on the
helper directly (empty, single, all-None, monotonic up/down,
flat, leading/trailing/consecutive gaps, mixed).

**Verified:** `pytest tests/` 123/123 (13 new). Headless Chrome
on the gapped fixture: sidebar `0.4 km · ↑50 m · ↓20 m`,
profile `0.40 km · ↑ 50 m · ↓ 20 m · 2000–2050 m` — gain/loss
agree, range is real. (One-off check via a temporary
integration test, since removed; the fix is guarded going
forward by the profile-endpoint value pins and the helper unit
tests.)

### #13 — Elevation gain/loss inflated by GPS jitter: raw per-sample delta-sum counts every wobble as climbing

**Symptom:** the Sunday track (`data/2026-07-19_11-09_Sun.gpx`)
reported gain 642.8 m / loss 631.7 m, but the human's reference
tool read ~435 m / ~424 m — a ~210 m over-count on each side.

**Hypothesis:** "the gain/loss math is wrong." Try to *falsify*:
the net (gain − loss) was +11.1 m, and the track's real net
climb (last − first ele) is +11.1 m (939.2 → 950.3). The math
is arithmetically correct — the *signal* it's summing is wrong.
The track has 3007 samples, 1241 up-deltas vs 1240 down, mean
|step| 0.42 m: nearly symmetric GPS jitter riding on top of the
real ascent. Summing raw per-sample deltas counts every
sub-metre wobble as climbing, inflating both gain and loss by
the full noise amplitude while the net stays right (435 − 424 =
+11, matching). So the over-count is pure noise, evenly split.

**Root cause:** no noise floor. `_elevation_gain_loss` summed
deltas straight off the raw elevation series. GPS elevation
jitter is sub-metre and roughly symmetric, so it accumulates
into both totals. The profile chart's "no smoothing, keep it
honest" philosophy (profile.py) is right for a *visual* but
wrong for a *delta-sum*: a chart shows the noisy signal for
inspection, a delta-sum treats every wobble as real climbing.

**Fix:** smooth the elevation series with a centred moving
average before summing, in a new `_smooth_elevations` helper
(±10 samples ≈ ±10 s at 1 Hz ≈ ~14 m of track at hiking pace;
prefix sums over (value, count) keep it O(n) and gap-aware —
short dropouts are bridged by averaging neighbours, a gap
longer than the window stays `None` and is skipped). The old
delta-sum becomes `_sum_gain_loss` (the gap-skipping rule,
unchanged), and `_elevation_gain_loss` is now
`_sum_gain_loss(_smooth_elevations(eles))`. The window was
tuned against the human's reference: ±10 gives 435.7/422.7 m
on the Sunday track (matches ~435/424); ±5 gives 458/446,
±15 gives 422/409. The human picked ±10.

**Gotcha — short tracks:** a moving average with a window
larger than the series flattens it to its mean (erasing real
climbs). The 4-point `elevation_gaps` fixture (a genuine 50 m
climb) would report 0 gain under w=21. Guard: smooth only when
`len(eles) >= 2*half+1`; otherwise fall back to the raw
`_sum_gain_loss`. Real hikes (1000+ samples at 1 Hz) always
smooth; the synthetic fixture and any very short track stay
raw. The existing 50/20 pin still holds.

**Scope kept tight:** only the gain/loss stat is smoothed.
`_elevation_min_max` (the profile range) stays raw — the
highest/lowest point reached is an honest extreme, not a
delta to be filtered, and smoothing would under-report the
true max. The chart trace is untouched (still draws the raw
0.0-substituted signal). README "Limitations" notes the split
(chart raw, totals smoothed) so the next reader isn't
surprised the two don't match point-for-point.

**Lesson:** a delta-sum is a high-pass operation — it
amplifies noise. "Sum every positive change" is only
meaningful over a signal whose noise floor is below the
features you care about, and raw GPS elevation isn't that
signal. The fix isn't a cleverer sum; it's to low-pass the
input first. And the net (gain − loss) is the diagnostic that
tells you the issue is noise, not a bug: net matched the real
start-to-end climb exactly, so the *shape* was right and only
the *magnitude* was inflated — the signature of symmetric
jitter. Confirmed against the real entry point: `randonneur
serve --directory data` → `/api/folder` and
`/api/tracks/{id}/profile` both return 435.7/422.7 for the
Sunday track (min 911.3 / max 1183.0, raw extremes).

**Verified:** `pytest tests/` 127/127 (4 new — centred-window
averaging, short-dropout-bridges vs long-gap-skips, jitter
reduces smoothed gain to the real ramp, short-series raw
fallback). README re-validated with `docutils --strict`. Real
server on the actual `data/` tracks confirms 435.7/422.7 m
(Sunday) via both the folder summary and the profile endpoint.

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
- 2026-07-20 — Behaviour-rule addition: **never terminate a
  process you did not start**. The rule was added to
  `agent-behaviour.md` §2 (Working mode) after a session in which
  the agent issued `kill` / `pkill` against a long-running Chrome
  instance owned by another session, and a server it had itself
  started but lost track of. The rule applies even in
  auto / unattended mode, and even when a process appears to be
  "blocking" something — wait for the human, or work around it.
  The only processes the agent may signal are ones it spawned in
  *this* session with a captured PID or a known background-task
  ID; everything else is the human's. The rule is a hard contract
  and cannot be relaxed without specific, case-by-case user
  agreement.

### Backfill — commits 13–19 (2026-07-20, recorded 2026-07-21)

> The following seven commits landed on 2026-07-20 without a
> matching history entry the same session. Backfilled per the §2
> rule that a commit without its history entry is incomplete work.
> Test counts are quoted from each commit message; the suite ends
> this run at 110/110.

- 2026-07-20 — Commit 13 (`31fd429`): **Remove the Pick… file
  picker button.** The `webkitdirectory` picker is dead weight
  once the folder path is the source of truth — the browser
  security model never returns the absolute path, so the picker
  is at best a "this folder has 12 GPX files" hint that still
  leaves the user reaching for the path textbox. Dropped the
  button, the input, the picker CSS, and the JS handler that
  surfaced the file count. Pinned the "no folder-picker"
  contract in the test that lists expected DOM IDs so a
  re-introduction is caught at CI time. README dropped the
  matching caveats. The path textbox is now the only way to
  load a folder (until commit 14 makes it read-only).

- 2026-07-20 — Commit 14 (`b5ad825`): **Take the folder as a
  `--directory` CLI option.** The browser can't expose an
  absolute path from a native picker (File System Access API
  returns opaque handles; `webkitdirectory` is a file-count
  hint at best), so the user had to paste the path every
  session. Moved the source of truth to the server start
  command: `randonneur serve --directory <path>` (alias `-d`,
  required, must exist and be a directory), mirroring the
  photogravy CLI shape. The resolved absolute path lives in
  `_AppState.active_folder` keyed on the FastAPI app id; the
  `/api/folder` handler reads it from there and the lifespan
  points the watcher at it before the first HTTP request. The
  `?path=` query parameter on `/api/folder` is gone (a stale
  URL gets a sensible empty response). UI: the form/input/Load
  button replaced with a read-only `<span id="folder-path">`;
  "no folder configured" shows as a dim placeholder when the
  server started without `--directory`. Tests gained a
  `make_client` factory that injects the active folder at
  `create_app()` time; the old "two folders through one app"
  tests reframed as two apps; new
  `test_readme_documents_required_cli_options` pins `--directory`
  in the README.

- 2026-07-20 — Commit 15 (`94e2500`): **Settings as a right-side
  tab (not a top-right popover).** The popover sat *behind*
  Leaflet's tile-pane (~200) / overlay-pane (~400) in some
  browsers, so a polyline crossing its rectangle visually
  overlapped it. Replaced with a fixed-width column flush to
  the right edge, slid in/out with a transform transition;
  the map/profile panes resize in lockstep via a third grid
  column on `.app-grid` so the tab has its own room (z-index
  1000, well above any Leaflet pane). The backdrop is gone —
  with the tab as a real column the only "outside" clicks are
  the ⚙ button, the ×, and Escape, all already wired. The
  `hidden` attribute is gone too; CSS visibility via `.open`
  is the single source of truth. `settings-backdrop` dropped
  from the pinned ID list.
  *Note:* the "third grid column" approach here was itself a
  latent bug — see commit 19 / bug-hunt #10's sibling: the
  third column caused `.profile-pane` to auto-flow into the
  right column. The uncommitted working-tree fix replaces it
  with `margin-right: 300px`.

- 2026-07-20 — Commit 16 (`8b8716c`): **GPX metadata — read,
  display, and edit.** GPX 1.1 carries five human-readable
  fields randonneur was parsing but not surfacing: the
  top-level `<metadata>` block (`<name>`, `<desc>`,
  `<author><name>`) and the per-track
  `<trk><name>`/`<trk><desc>`. Added them to the data model,
  shipped them on the wire in `/api/folder` and
  `/api/tracks/{id}`, and exposed a metadata editor in the
  right-side settings tab. `Track` dataclass gains five
  optional string fields; `parse()` populates them from
  gpxpy's `gpx.name` / `gpx.description` / `gpx.author_name`
  and the first `<trk>`'s name/desc. The file-stem `name`
  stays as the sidebar/polyline label — the GPX track name is
  a *display* name, only meaningful once the file is
  identified. The PATCH endpoint is the one place writes
  happen: a `MetadataPatch` body where every field is
  optional, `""` is the editor's "clear" signal, `None`
  (missing) is a no-op; each field capped at 1000 chars by a
  pydantic validator. `write_metadata()` does the round-trip
  via gpxpy's set-or-None convention and writes atomically
  (tmp + `os.replace`), so a crash mid-save leaves the
  user's original GPX on disk; the cache is refreshed in the
  same call. Eight new loader tests (parse extraction,
  write round-trip, clear / no-op semantics, 1000-char cap,
  atomic-write-on-failure) + six new server tests (PATCH,
  empty-string-clears, 404/422, no-op empty body, inlined
  metadata on the folder list) + two updated shape tests.
  README gains a Metadata editing section.
  *Gotcha surfaced later:* this editor re-saves the user's
  GPX with gpxpy's formatting (single→double quotes,
  attribute reordering), which is what produced the big
  `data/*.gpx` diffs that triggered adding `data/` to
  `.gitignore` (2026-07-21).

- 2026-07-20 — Commit 17 (`37a7576`): **Hiking favicon,
  `static/media/`, Font Awesome Free icons.** The tab showed
  a blank document icon and the header ⚙ / close × / Save /
  Clear buttons were unicode glyphs — placeholder chrome on
  a viewer whose reason for existing is outdoor maps.
  Favicon: a hand-drawn mountain-peak-with-trail-marker SVG
  at `/media/favicon.svg` (inline SVG, no binary asset; paper
  backplate, sienna peak, dotted white trail; drawn to read
  at 16 px). Icons: Font Awesome 6 Free from cdnjs, one
  `<link>` in `index.html` (icons CC BY 4.0 — attribution in
  the CSS header; font SIL OFL 1.1; no env var / key / Python
  dep). No JS change — FA icons live in the same `<button>`
  elements the existing CSS targets and handlers attach to
  the buttons, not the icons. Two new tests pin the favicon
  URL/SVG, the FA CDN reference, and the four icon classes
  (the latter is the regression guard against a future move
  off FA Free).

- 2026-07-20 — Commit 18 (`9c80fca`): **Earthy accent palette
  via CSS custom properties.** The bright Bootstrap-blue
  selection/focus (`#0969da`) and bright-green submit
  (`#2da44e`) read as "generic web app" on a warm-paper
  outdoor-map viewer. Pulled the accents into a small set of
  `:root` custom properties with a sienna identity (deep rust,
  pale tint, olive success, bark-red error); the rest of the
  stylesheet references the variables. Why custom properties:
  a single place to change the palette — a "forest green"
  variant is six value edits away (the forest values live in a
  comment block next to the `:root` rule). Why sienna over
  forest: the favicon (commit 17) is a sienna peak on a
  paper backplate, so selection = the colour of the map
  marker, not an unrelated app convention. Neutrals (whites,
  greys, the warn yellow on parse errors) left alone —
  semantic, not accent. One new test pins the four custom
  property values and asserts the four `var(...)` references
  exist, so a palette change is a deliberate test update, not
  silent drift.

- 2026-07-20 — Commit 19 (`3a6a2a5`): **Fix: Page height
  overflow (no more phantom vertical scrollbar).** See
  bug-hunt #10 above for the full root-cause / fix / lesson.
  In short: `.app-grid { height: calc(100vh - 49px) }`
  hard-coded a header offset that no longer matched once the
  settings tab + status line + folder-info + FA icon grew
  the header to ~79 px, spilling ~30 px below the viewport.
  Replaced with `body { display: flex; flex-direction: column;
  min-height: 100dvh }` + `.app-grid { flex: 1 1 0;
  min-height: 0 }` so the grid takes the leftover space
  regardless of header height. New
  `test_app_grid_flexes_to_remaining_viewport_height` pins the
  absence of the `calc(100vh - N)` pattern; new
  `tests/test_headless.py` (marked `integration`, skips
  without a Chrome binary) asserts
  `document.body.scrollHeight <= window.innerHeight` at three
  viewport sizes. `websocket-client` added to the `[dev]`
  extra as the only new dep. Suite ends this run at 110/110.

- 2026-07-21 — Added `data/` to `.gitignore` and untracked the
  two real GPX files (`git rm --cached`, files kept on disk).
  `data/` is the user's own track data, not project content;
  the metadata editor's gpxpy re-save (commit 16) was
  producing large `data/*.gpx` diffs that don't belong in
  commits. Also added a §2 rule + a §7 workflow step to
  `agent-behaviour.md` making `agent-history.md` maintenance
  part of the commit, not an afterthought — and backfilled
  this section for the commits that landed without entries.
- 2026-07-21 — Commit 20 (`f0e8c34`): **Fix: Settings-tab
  layout broke the profile pane; status line moves to the
  sidebar.** See bug-hunt #11 for the full root-cause/fix/
  lesson. In short: `body.settings-open .app-grid` had grown
  a third grid column (`240px 1fr 300px`) to "make room" for
  the right-side settings tab, but the tab is `position:
  fixed` and out of flow, so the column was a no-op for the
  tab and instead reshuffled `.profile-pane` (no third-area
  assignment) into the right column — the elevation graph
  jumped up out of the bottom row when the tab opened.
  Replaced the column change with `margin-right: 300px` on
  `.app-grid`; the grid template stays two columns / two rows
  in both states. Same commit also moved the folder-status
  line out of the header and into the sidebar (between the
  "Tracks" heading and the list), moved the track count into
  the heading as a dim "(N)", and re-styled `.folder-status`
  for the sidebar: inset 12px to align with the heading and
  list items, wrap instead of truncate (the old header rule's
  `nowrap` + ellipsis would chop the long "No folder
  configured — start the server with --directory <path>"
  hint), and `:empty` drops the bottom margin so the line
  takes no space in the happy path. Pinned `tracks-count` and
  `folder-status` in `test_index_contains_required_dom_ids`
  (both are written by `app.js` and were unpinned). Recorded
  immediately after the commit, same session — the gap the
  new §2/§7 rule exists to prevent; flagged here honestly
  rather than glossed. `pytest tests/` 110/110; headless
  Chrome confirmed both the settings-tab profile-row fix and
  the sidebar status-line inset/wrap.
- 2026-07-21 — Commit 21 (`Feat`): **Split the right-side tab
  into Settings and Edit views.** The single gear button had
  opened one panel that mingled app settings (base layer, scale
  bar) with the GPX metadata editor — "tune the map" and "edit
  the track" in one scrolling list. The rework separates them:
  the header gets two buttons (gear = Settings, pen-to-square =
  Edit) that open the *same* right-side panel but show one of two
  mutually-exclusive views via a `data-view` attribute on
  `#side-panel` (renamed from `#settings-panel`). Only the active
  `.tab-view` is not `hidden`. The panel header title swaps
  ("Settings" / "Edit metadata"). Toggle semantics (confirmed with
  the human): clicking the active view's button closes the tab;
  clicking the other swaps; × and Escape close. Selecting a track
  does **not** auto-open Edit (confirmed) — the Edit view follows
  the selection only while it is the active view; when the tab is
  closed the form populates on the next open. Edit with no track
  selected shows a `#metadata-empty` placeholder instead of a
  blank form. `selectTrack`, the watcher-refresh path, and the
  save-success path all guard their metadata rendering on
  `activeView === "edit"` via a new `renderEditView()` (which
  reuses the existing `renderMetadataEditor` /
  `clearMetadataEditor`). CSS: `.header-actions` holds the two
  buttons; `.settings-panel` → `.side-panel`; `body.settings-open`
  → `body.tab-open` (the `margin-right: 300px` rule, unchanged in
  effect — the bug-hunt #11 invariant still holds). Tests: the
  DOM-ID pin list swaps `settings-button/panel/close` for
  `tab-button-settings/tab-button-edit/side-panel/side-panel-close/
  side-panel-title` + `metadata-empty`; the CSS pin `.settings-panel`
  → `.side-panel`; the FA pin gains `fa-pen-to-square`. No server /
  endpoint / data-model change. `pytest tests/` 110/110; a
  headless-Chrome drive confirmed gear→Settings, pen→Edit swap,
  empty-state with no track, form populates on selection while
  Edit is open, no reopen on selection while closed, active-button
  toggle-close, Escape close, × close, the 300px margin + profile
  bottom-row invariant, and no viewport overflow.
- 2026-07-21 — Commit 22 (`Chore`): **Use the favicon SVG as a
  logo left of the title; capitalize the title RANDONNEUR.** The
  header's first grid cell is now a `.brand` inline-flex holding
  the existing `/media/favicon.svg` (rendered at 20×20,
  `alt=""` — decorative, since the `<h1>` carries the text) beside
  the `<h1>RANDONNEUR</h1>`. The h1 gains a 0.08em letter-spacing
  so the all-caps wordmark reads as a logo, not a shouty label.
  No test pinned the h1 text (only `<title>randonneur</title>` in
  `<head>`, unchanged), so the capitalize is text content, not a
  CSS transform — the DOM text is accurate. `pytest tests/`
  110/110; headless Chrome confirmed the SVG loads
  (`naturalWidth > 0`), renders at 20px, the title is "RANDONNEUR",
  the logo + title share one brand box, and the header change
  introduced no viewport overflow.
- 2026-07-21 — Commit 23 (`Feat`): **Separate total elevation gain
  and loss instead of a single cumulated figure.** The stats had
  shown only total ascent (`↑ gain`); the rework adds total descent
  as a separate stat so the two directions are visible
  independently. `gpx_loader.Track` gains `elev_loss_m` (sum of
  negative deltas as a positive magnitude); `_elev_gain` is
  replaced by `_elevation_gain_loss(points) -> (gain, loss)` — one
  pass, so gain and loss share the same gap-skipping rule and can't
  drift apart. `TrackSummary` and `TrackDetail` carry `elev_loss_m`,
  shipped on the wire in the folder list, track detail, and PATCH
  response. UI: the sidebar `formatStats` and the profile
  `formatProfileStats` both show `↑ gain m · ↓ loss m` (the profile
  line keeps the min–max range too; it recomputes gain/loss
  client-side from the elevation array, as it already did for gain).
  While there, fixed a latent quirk in the sidebar formatter: gain
  ≥ 1000 m was being rendered as *km* (`1.5 km`) — a copy-paste from
  the distance formatter; elevation in km is nonsensical. Both gain
  and loss are now always metres. Tests: the elevation_gaps test
  asserts `elev_loss_m == 20.0` (2000/None/2050/2030 → +50 gain,
  -20 loss) and is renamed; the two server shape tests include
  `elev_loss_m`; the README ASCII sketch stat line shows
  `↑ 870 ↓ 420 m`. No net-elevation concept was added — gain and
  loss are independent magnitudes, which is what hikers read.
  `pytest tests/` 110/110; README re-validated with
  `docutils --strict`; headless Chrome confirmed both the sidebar
  and the profile stats lines show separate ↑ and ↓ metre values
  (and no `km` misused for elevation).
- 2026-07-21 — Commit 24 (`Fix`): **Make the profile stat line
  match the sidebar for tracks with elevation gaps.** The gain/loss
  separation (commit 23) exposed a latent parity bug: the sidebar
  read gap-aware `elev_gain_m` / `elev_loss_m` from the backend
  (None dropouts skipped), but the profile `formatProfileStats`
  recomputed gain/loss *and* min/max client-side from
  `elevations_m` — an array where `profile.compute_profile` had
  already substituted `None → 0.0` for index alignment with
  Plotly and the hover-sync. So any track with an elevation
  dropout showed ~2000 m of phantom climbing on the profile pane
  and a bogus 0 m minimum, while the sidebar showed the real
  (small) numbers. Root cause: duplicated computation across a
  wire format that silently rewrites `None` to `0.0`; the client
  cannot be gap-aware once the `None` is gone. Fix is one source
  of truth — the profile endpoint now ships the backend's
  `elev_gain_m` / `elev_loss_m` plus a new gap-aware
  `elev_min_m` / `elev_max_m` (from a new
  `gpx_loader._elevation_min_max` helper, skips `None`, returns
  `(None, None)` when no point has an elevation), and
  `formatProfileStats` displays those directly instead of
  recomputing. The `elevations_m` array is unchanged (Plotly +
  the hover-sync still need the 0.0 alignment); it's just no
  longer the source of the stats. The duplicated client-side
  math is retired, so no `test_sync_math.py` parity twin is
  needed. See bug-hunt #12 for the full hunt. Tests: 13 new —
  edge cases on `_elevation_gain_loss` / `_elevation_min_max`
  (empty, single point, all-None, monotonic ascent/descent,
  flat, leading/trailing/consecutive gaps, mixed up-and-down,
  min/max over gaps and the all-None `None` pair), plus a
  value-level pin on the profile endpoint for the gapped
  fixture (`elev_gain_m == 50`, `elev_loss_m == 20`,
  `elev_min_m == 2000`, `elev_max_m == 2050` — not the
  2050/2020/0 the old recompute produced) and the updated
  profile-response key set. `pytest tests/` 123/123; headless
  Chrome confirmed the sidebar and profile stat lines now agree
  (gain 50, loss 20, real 2000–2050 m range).
- 2026-07-21 — Commit 25 (`Fix`): **Smooth GPS elevation jitter
  out of the gain/loss totals.** The Sunday track reported
  642.8/631.7 m of gain/loss vs the human's ~435/424 m reference.
  Root cause: `_elevation_gain_loss` summed raw per-sample deltas,
  and GPS elevation carries sub-metre symmetric jitter (3007
  samples: 1241 up-wobbles vs 1240 down, mean |step| 0.42 m) that
  a delta-sum counts as climbing — inflating both totals by the
  full noise amplitude while the net (gain − loss = +11.1 m)
  matched the real start-to-end climb exactly. The math was
  arithmetically right; the signal wasn't. Fix: a centred moving
  average (`_smooth_elevations`, ±10 samples ≈ ±10 s at 1 Hz,
  O(n) via prefix sums, gap-aware) low-passes the series before
  `_sum_gain_loss` sums the deltas. Window tuned to the human's
  reference (±10 → 435.7/422.7; ±5 → 458/446; ±15 → 422/409);
  human picked ±10. Short tracks (`len < 2*half+1`) skip
  smoothing and take the raw sum, so the 4-point `elevation_gaps`
  fixture still pins 50/20 (a w=21 MA would flatten it to 0).
  Scope: only the gain/loss stat is smoothed — `_elevation_min_max`
  (the range) and the profile chart trace stay raw (honest
  extremes / honest signal). See bug-hunt #13. Tests: 4 new
  (centred-window averaging, short-dropout-bridges vs long-gap-
  skips, jitter reduction recovers the real ramp, short-series
  raw fallback); existing edge-case + fixture pins unchanged.
  README "Limitations" notes the chart-raw / totals-smoothed
  split, re-validated with `docutils --strict`. `pytest tests/`
  127/127; real `randonneur serve --directory data` confirms
  435.7/422.7 m on the Sunday track via both `/api/folder` and
  `/api/tracks/{id}/profile`.
- 2026-07-21 — Commit 26 (`Feat`): **Configurable elevation
  smoothing window (Settings tab) + sampling-rate display.** The
  ±10 window from commit 25 is rate-dependent in *physical*
  terms: ±10 samples is ±50 s at a 5 s cadence but ±10 s at 1 Hz,
  and the human mixes sampling rates between tracks. So the
  window is now a setting, and the track's sampling rate is shown
  so the right N is pickable. Backend: `gpx_loader.Track` replaces
  the single `elev_gain_m`/`elev_loss_m` pair with
  `elev_gain_loss: dict[int, tuple[float, float]]` precomputed at
  parse time for every window in `_ELEV_SMOOTH_WINDOWS = (5, 10,
  15)` (default 10) — one source of truth, all three windows
  shipped, so the client toggles instantly with no refetch.
  `_elevation_gain_loss` / `_smooth_elevations` take a `half`
  param; `_ELEV_SMOOTH_HALF` kept as a legacy alias for the
  existing tests' default-arg. New `sample_interval_s` is the
  **median** (not mean — robust to pauses/dropouts) inter-point
  interval, rounded to the second, `None` when the track is
  untimed; `_raw_points(gpx)` (renamed from `_flatten_points`)
  returns the raw gpxpy points so `p.time` is available.
  Server: a new `ElevGainLoss` model (`half`/`gain_m`/`loss_m`);
  `TrackSummary` / `TrackDetail` / `TrackProfile` carry
  `elev_gain_loss_m: list[ElevGainLoss]` (sorted by half) instead
  of the two scalars; `TrackProfile` also carries
  `sample_interval_s`. A `_elev_gain_loss_models(t)` helper is
  the single construction site used by all four response paths.
  Frontend: a third fieldset in the Settings tab ("Elevation
  smoothing") with three radios ±5 / ±10 / ±15 (±10 checked by
  default); `smoothHalf` state; `pickGainLoss(glList)` selects
  the window matching `smoothHalf` (falling back to 10, then the
  first); `refreshStats()` re-renders every sidebar stat line +
  the selected track's profile stat from the already-shipped
  per-window list — no refetch, no chart redraw. The profile
  stat line appends `· {sample_interval_s}s/pt` when the track is
  timed (e.g. `5s/pt` for the Sunday track). README
  "Limitations" rewritten: window is ±N *samples* (5/10/15,
  default 10, set in Settings), rate-dependent, profile stat
  line shows the median interval so you can pick N for the rate,
  chart stays raw, short tracks fall back to raw, min/max always
  real extremes — and corrects the prior wrong "≈ ±10 s at 1 Hz"
  claim (the real tracks are 5 s cadence).
  *Stale-cache fix (same commit, amended in):* the scalar→list
  reshape of the gain/loss field silently broke browsers that had
  the pre-commit app.js heuristically cached — the old JS read the
  now-missing `elev_gain_m` as `undefined` against the new JSON and
  rendered `↑ NaN m · ↓ NaN m` in every stat line (a hard refresh
  cleared it, but "stale JS, fresh API" is a silent, recurring
  class). Root cause: Starlette's `StaticFiles` sends `ETag` /
  `Last-Modified` but no `Cache-Control`, so browsers fall back to
  heuristic caching and may serve a stale `app.js` without
  revalidating for an unpredictable window. Fix: a small
  `_NoCacheStaticFiles(StaticFiles)` subclass overrides
  `get_response` to add `Cache-Control: no-cache` — "revalidate
  before using", not "don't cache", so the browser sends a
  conditional GET and StaticFiles returns 304 (unchanged,
  sub-millisecond, no body) or 200 (changed, new bytes). Verified
  on the wire: `app.js` and `/` both carry `cache-control: no-cache`
  alongside the existing ETag/Last-Modified, and an `If-None-Match`
  re-request returns 304. The tile endpoint is a separate route,
  not this mount, so tiles keep their own disk-cache path. New
  `test_static_assets_force_revalidation` pins the header on both
  `/app.js` and `/`. The code itself was never wrong — a fresh
  headless-Chrome load (no cache) rendered both tracks' stat lines
  and the Plotly chart with zero NaN before the fix; the fix is what
  stops a *returning* browser from seeing the NaN.
  *Gotcha (test harness, not product):* while verifying the ±15
  toggle with a temporary headless-Chrome test, the second
  `pick(half)` call silently no-op'd — the radio never checked.
  Root cause: CDP `Runtime.evaluate` runs each eval in the
  *same* global lexical environment, so a top-level `const r`
  persists across evals and the second `const r = ...` throws a
  silent `SyntaxError: Identifier 'r' has already been declared`
  (the script fails to parse, `Runtime.evaluate` returns
  `undefined`, no Python exception). `pick(5)` worked only
  because it was the *first* declaration. Fix: wrap the eval body
  in an IIFE so `const r` is function-scoped. The product was
  never affected — the default (10) and ±5 toggled correctly and
  the numbers matched the backend precompute exactly (436/423 @10,
  458/446 @5); the ±15 toggle verified once the IIFE scoping was
  in. Lesson for future headless tests: never use a top-level
  `const`/`let` in a `Runtime.evaluate` expression you'll issue
  more than once — wrap in an IIFE, or the second call dies
  silently and the assertion failure looks like a product bug.
  Tests: 4 new — precompute-for-each-window, window-param
  changes the result (synthetic 1000-pt ramp: gain@5 > @10 > @15,
  default == half=10), `sample_interval_s` from timestamps
  (median=5 with a 10 s outlier), `None` when untimed; 6 updated
  for the new `elev_gain_loss_m` list + `sample_interval_s` keys
  and the `settings-smooth` DOM-ID pin, + the
  `test_static_assets_force_revalidation` cache-control pin (part
  of the stale-cache fix above). `pytest tests/` 132/132;
  headless Chrome on the real `data/` Sunday track confirmed all
  three windows (±10 → 436/423 with `5s/pt`, ±5 → 458/446, ±15 →
  422/409) and the revert-to-10, both sidebar and profile, with
  zero NaN in the stat lines and the Plotly trace. README
  re-validated with `docutils --strict`.
- 2026-07-21 — **Reversed: `data/` is tracked again.** The user
  cleared the `data/` folder and will now actively maintain the
  GPX dataset inside the git repo, so the prior 2026-07-21
  `.gitignore` rule (`data/`) is removed. The original reason for
  ignoring — the metadata editor's gpxpy re-save producing noisy
  `data/*.gpx` diffs — is accepted as a cost of in-repo data going
  forward. No README / test / behaviour-file reference to the old
  ignore rule existed, so the only edit is `.gitignore` itself.
- 2026-07-21 — Commit 27 (`Chore`): **Track the GPX dataset
  in-repo.** First tracked dataset under `data/FR-83/`: two
  routes around Bargème (Var, FR-83) — `Bargème (long trip)`
  (11.4 km) and `Bargème (short trip)` (9.6 km), both exported
  from onthegomap.com. **Sharp edge:** these files have **no
  `.gpx` extension** (onthegomap names them after the route, not
  `*.gpx`), so the current `discover()` (`*.gpx` glob) does not
  find them yet — the subfolder-listing feature (next commit)
  must discover by content, not extension, or these will never
  appear in the TRACKS panel. Committed together with the
  `.gitignore` un-ignore. `.DS_Store` stays ignored.
- 2026-07-21 — Commit 28 (`Feat`): **List subfolders (collapsible)
  and their GPX files in the TRACKS panel.** The dataset is now
  organised into subfolders (`data/FR-83/…`); the flat track list no
  longer reflected that. The panel now groups tracks by their
  subfolder, each subfolder a collapsible group (▾/▸), in-memory
  state (`collapsedFolders: Set<string>` — survives hot-reload
  re-fetches, resets on full page reload; default all-expanded).
  Discovery stays **extension-only `*.gpx`** (confirmed with the
  human, who renamed the commit-27 files to `.gpx` — so the
  commit-27 "must discover by content" sharp edge is resolved by
  rename, not by a content sniff; `gpx_loader.discover()` is
  unchanged, already recursive via `rglob("*.gpx")`). Backend:
  `TrackSummary` gains `subfolder: str` (`""` for a file directly in
  the served root — the "no group header" sentinel); a new
  `_track_summary(t, folder)` helper centralises `TrackSummary`
  construction (mirrors the existing `_elev_gain_loss_models`), used
  by **both** `/api/folder` and the PATCH metadata response —
  load-bearing because the frontend does `tracks[idx] = updated` on
  a metadata save, so the PATCH response must carry `subfolder` or
  the track drops out of its group on the next render. `subfolder` is
  computed from the path (`parent.relative_to(folder.resolve())`,
  `""` when `.`), never stored on `Track`. The `errors` list now
  carries the path **relative to the served folder** (new
  `_relname` helper) instead of bare `f.name`, so two same-named
  bad files in different subfolders are distinguishable; the
  root-file case still satisfies `errors[0].startswith("bad.gpx:")`.
  Frontend: `renderTrackList()` is the only function that changes —
  grouping is **render-only** on the flat `tracks` array (the
  backend sorts by full path so a subfolder's tracks are already
  contiguous; a pre-pass counts each group for the header). A
  `<li class="track-group-header">` (same muted-uppercase tone as
  the "TRACKS" sidebar heading; ▾/▸ marker in `::before`, flips on
  `.collapsed`; dim per-group count) precedes each non-root group's
  rows; the **root group renders with no header**. Grouped track
  `<li>`s get an `in-group` indent (24px vs 12px); rows in a
  collapsed group get `hidden = true`. `selectTrack(id)` now
  auto-expands the selected track's group (`collapsedFolders.delete
  (subfolder)`) before re-rendering, so a selection made from the
  map (whose group may be collapsed) keeps its highlight visible.
  Every other flat-array consumer (`drawAllTracks`, `refreshStats`,
  `renderEditView`, `renderMetadataEditor`, `saveMetadata`,
  `refreshFolder`) is **unchanged** — all id-keyed, nesting-agnostic;
  `li[data-track-id]` matches rows anywhere in the nested DOM. No
  `index.html` structural change (headers generated into the
  existing `#track-list`); no watcher change (already recursive);
  no `TrackDetail`/`TrackProfile` change. Same commit also stages
  the working-tree `.gpx` renames (the two commit-27 Bargème files
  renamed to add `.gpx`) **and** a third track added to the dataset,
  `data/FR-83/Brenon (short trip).gpx` (a route around Brenon, also
  FR-83) — so the served folder now has three tracks, all under
  `FR-83`. Tests: 2 new server (`test_folder_lists_tracks_grouped_
  by_subfolder`, `test_folder_error_includes_relative_path`) +
  `test_track_summary_shape_is_stable` gains `subfolder` + a root→
  `""` assertion; 1 new loader (`test_discover_ignores_extensionless
  _gpx_content` — pins the extension-only contract so a future
  content-sniff is a deliberate test update, not silent drift). The
  existing `test_folder_with_one_bad_file_*` pin
  (`errors[0].startswith("bad.gpx:")`) still holds — a root file's
  `_relname` is its bare name. `pytest tests/` 135/135; a temporary
  headless-Chrome drive against the real `data/` (appended to
  `test_headless.py`, then reverted pre-commit per the established
  workflow) confirmed: the FR-83 group header renders with a ▾
  marker and all three tracks under it; collapse hides the rows and
  flips the marker to ▸; expand restores them; selecting a track
  from a collapsed group auto-expands it; `document.body.scrollHeight
  <= window.innerHeight` (no layout regression from the indented
  grouped rows). IIFE-wrapped every multi-use `const` in the
  `Runtime.evaluate` expressions (the commit-26 CDP
  redeclaration gotcha).
- 2026-07-21 — Commit 29 (`Feat`): **Click an already-selected
  track's row to deselect it.** The panel row click was a plain
  `selectTrack(t.id)` — clicking the highlighted track again was a
  no-op (it just re-set the same id). Now the row click toggles:
  `selectTrack(t.id === selectedTrackId ? null : t.id)`. `selectTrack
  (null)` already had a clean deselect path (clears all map
  crosshairs, drops `selectedTrackId`, clears the profile, hides the
  profile crosshair), so no new "clear" function was needed. Scope:
  the toggle is in the **panel row click handler only** — map-polyline
  clicks and the watcher's hot-reload re-selection still call
  `selectTrack(id)` as a plain set, so a re-click on the map or a
  post-reload restore doesn't unexpectedly drop the selection.
  `selectTrack`'s auto-expand guard (`if (sel && sel.subfolder)`) is
  naturally skipped on `null` (no track matches), so deselecting
  leaves the group's collapsed/expanded state alone. `pytest tests/`
  135/135; a temporary headless-Chrome drive against `data/`
  (appended to `test_headless.py`, then reverted pre-commit)
  confirmed first click selects, second click on the same row
  deselects.
- 2026-07-21 — Commit 30 (`Data`): **Add the Brenon (long trip)
  track.** Third route under `data/FR-83/`, alongside the two
  Bargème trips — `Bargème (long trip)` (11.4 km), `Bargème (short
  trip)` (9.6 km), and now `Brenon (long trip)` (9.1 km, 393 pts,
  ±10-smoothed gain/loss 313/312 m), all exported from
  onthegomap.com. Per the new §6 rule (committed next), each new
  GPX track is its own `Data:` commit — data additions are kept
  separate from code so a track import doesn't ride along on a
  feature/fix commit and clutter its diff.
