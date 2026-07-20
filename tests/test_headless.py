"""Headless-Chrome regression tests for layout fixes.

The CSS-pinning tests in ``test_server.py`` catch the *static* contract
(no more ``calc(100vh - 49px)`` etc.), but the actual user-facing
assertion is "the page doesn't introduce a vertical scrollbar" — that
needs a real browser to measure. These tests are marked
``integration`` because they require a Chrome binary on the host
(skip with the default ``pytest`` run, opt in with
``pytest -m integration``). When the binary isn't found, the tests
skip with a clear reason — CI images without a browser still pass.

A small wrapper around the Chrome DevTools Protocol handles the
boilerplate (find the binary, spawn with a temp profile, navigate,
evaluate, screenshot, tear down). Each test gets a fresh Chrome so
cookies / caches from a previous test don't bleed across.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Standard install locations for Chrome / Chromium. The test skips
# cleanly when none of these is present (CI without a browser).
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    shutil.which("chrome"),
    shutil.which("google-chrome"),
    shutil.which("chromium"),
)


def _find_chrome() -> str | None:
    for path in _CHROME_CANDIDATES:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _cdp_send(ws, msg_id: int, method: str, params: dict | None = None) -> dict:
    """Send a CDP command over the websocket; return the response dict.

    The protocol is: send {"id": N, "method": "...", "params": {...}}
    as JSON, then read frames until one with matching id arrives.
    A small per-call queue keeps the response matching in order even
    when other events (e.g. Page.loadEventFired) interleave.
    """
    import json as _json
    ws.send(_json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = ws.recv()
        msg = _json.loads(raw)
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise RuntimeError(f"CDP {method} failed: {msg['error']}")
            return msg


@pytest.fixture
def chrome_proc():
    """Spawn a headless Chrome and yield a CDP client; tear down on exit."""
    chrome = _find_chrome()
    if chrome is None:
        pytest.skip(
            "no Chrome/Chromium binary on PATH; install one to run the "
            "headless regression tests"
        )

    # The devtools port and the user-data-dir have to be unique per
    # test (Chrome refuses to share either). 9300 + the test's pid
    # is a low-collision choice; tempfile keeps the profile off the
    # real user data dir.
    port = 9300 + (os.getpid() % 1000)
    profile = tempfile.mkdtemp(prefix="randonneur-chrome-")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the devtools endpoint to come up. Polling the port
    # is faster on a warm binary and self-corrects on a cold one.
    import socket
    for _ in range(40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("Chrome did not open the devtools port within 4s")

    # Get the WebSocket URL from /json/version. In Chrome 150 the
    # browser-level webSocketDebuggerUrl refuses page-level methods
    # (Emulation.setDeviceMetricsOverride returns -32601), so we
    # open a *page* target via /json and connect to that. The
    # websocket-client package is the standard-lib-friendly choice;
    # we fall back to skipping if it's missing.
    import json as _json
    import urllib.request
    info = _json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read())
    page = next((t for t in info if t.get("type") == "page"), None)
    if page is None:
        proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)
        pytest.fail("no page target found in Chrome's /json discovery")
    ws_url = page["webSocketDebuggerUrl"]

    try:
        from websocket import create_connection  # type: ignore
        ws = create_connection(ws_url, timeout=5)
    except ImportError:
        # No websocket-client; bail with a clear message rather than
        # silently failing.
        proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)
        pytest.skip("websocket-client is required for the headless tests; "
                    "install it via `pip install websocket-client`")

    next_id = [0]

    def send(method: str, params: dict | None = None) -> dict:
        next_id[0] += 1
        return _cdp_send(ws, next_id[0], method, params)

    def wait_for(event: str, timeout: float = 5.0) -> None:
        # Drain events until the named one arrives or we time out.
        deadline = time.time() + timeout
        ws.settimeout(timeout)
        while time.time() < deadline:
            raw = ws.recv()
            msg = _json.loads(raw)
            if msg.get("method") == event:
                return
        raise TimeoutError(f"CDP event {event!r} did not arrive within {timeout}s")

    def set(method: str, params: dict | None = None) -> None:
        # Many CDP "set" methods return an empty ack we don't need.
        send(method, params)

    try:
        yield type("CDP", (), {"send": staticmethod(send),
                                "wait_for": staticmethod(wait_for),
                                "set": staticmethod(set)})
    finally:
        try:
            ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def _start_server(tmp_path: Path, port: int):
    """Spawn the randonneur server pointed at ``tmp_path``."""
    env = os.environ.copy()
    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-m", "randonneur", "serve",
         "--directory", str(tmp_path), "--port", str(port),
         "--no-browser"],
        cwd=repo, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the HTTP port to come up.
    import socket
    for _ in range(40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail(f"randonneur did not start on port {port} within 4s")
    return proc


@pytest.fixture
def live_server(tmp_path):
    """Run a real randonneur server on a temp port; tear down on exit."""
    fixtures = Path(__file__).parent / "fixtures"
    # Copy at least one fixture so /api/folder returns something.
    (tmp_path / "track.gpx").write_bytes((fixtures / "elevation_gaps.gpx").read_bytes())

    port = 8800 + (os.getpid() % 200)
    proc = _start_server(tmp_path, port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
def test_page_height_does_not_overflow_viewport(chrome_proc, live_server) -> None:
    """The page must not introduce a vertical scrollbar.

    Commit 7 fixed a 30px overflow caused by ``height: calc(100vh -
    49px)`` on ``.app-grid`` — the real header is taller than 49px
    once the settings tab + status line + folder-info were added, so
    the grid spilled below the viewport. The fix switches the body
    to a flex column with ``.app-grid { flex: 1 }`` so the grid
    takes the remaining viewport regardless of the header's actual
    height. This test is the regression guard: open the page in
    headless Chrome at three common viewport sizes and assert
    ``document.body.scrollHeight <= window.innerHeight`` for each.
    """
    import json as _json
    for width, height in [(1200, 800), (1920, 1080), (900, 600)]:
        # setDeviceMetricsOverride and Page.navigate work in current
        # Chrome without an explicit domain enable call; the older
        # pattern of "Emulation.enable first" is no longer required
        # and -32601s on Chrome 150.
        chrome_proc.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height,
            "deviceScaleFactor": 1, "mobile": False,
        })
        chrome_proc.send("Page.enable", {})
        chrome_proc.send("Page.navigate", {"url": live_server + "/"})
        chrome_proc.wait_for("Page.loadEventFired", timeout=5)
        # The folder fetch + drawAllTracks happens asynchronously
        # after load; give it a beat so the final layout is settled
        # before we measure.
        time.sleep(1.0)

        result = chrome_proc.send("Runtime.evaluate", {
            "expression": (
                "JSON.stringify({"
                "  win: window.innerHeight,"
                "  body: document.body.scrollHeight,"
                "  overflow: document.body.scrollHeight - window.innerHeight"
                "})"
            ),
            "returnByValue": True,
        })
        # CDP's Runtime.evaluate response nests: result.result.value
        # (the outer is the call result, the inner is the RemoteObject
        # envelope, the value is the returnByValue payload).
        m = _json.loads(result["result"]["result"]["value"])
        assert m["overflow"] <= 0, (
            f"at {width}x{height}: body={m['body']}, "
            f"win={m['win']}, overflow={m['overflow']}px"
        )
