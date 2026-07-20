"""Command-line entry point for randonneur.

Currently exposes a single ``serve`` subcommand (which is also the default
when ``randonneur`` is invoked with no arguments): start the local server
and open the UI in the default browser. The subcommand shape exists so we
can add ``discover`` and friends later without breaking the no-arg form.

The "no args → serve" dispatch is done in ``main()`` rather than via a
group-callback fall-through: Click's ``invoke_without_command`` mechanism
double-fires when the group callback is reached twice (once with the
parent context, once with no context), and there is no reliable signal
in the callback to tell the two apart. Inspecting ``sys.argv`` directly
is the only way to keep the no-arg form working without printing
"randonneur" twice when the user actually types ``randonneur serve``.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

import click
import uvicorn

from randonneur import __version__, gpx_loader, log
from randonneur.config import DEFAULT_HOST, DEFAULT_PORT
from randonneur.server import create_app, static_files_dir

_log = log.get_logger("cli")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="randonneur")
def cli() -> None:
    """randonneur — plot GPX tracks on a hiking map."""


@cli.command()
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Bind address.")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int, help="Bind port.")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose (DEBUG) logging.")
def serve(host: str, port: int, no_browser: bool, verbose: bool) -> None:
    """Start the local server and open the UI (default)."""
    log.configure_logging(verbose=verbose)
    _log.info("Starting randonneur %s on http://%s:%d", __version__, host, port)

    app = create_app(static_dir=static_files_dir())
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    # Run uvicorn in a background thread so the main thread can hold the
    # process alive (and handle Ctrl-C cleanly) until the server stops.
    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()

    # Wait for the server to be actually accepting connections before
    # opening the browser — otherwise the user gets a "can't connect"
    # page if startup takes more than a fraction of a second.
    _wait_for_ready(server, timeout=10.0)

    url = f"http://{host}:{port}"
    if not no_browser:
        _log.info("Opening %s in your default browser", url)
        webbrowser.open_new_tab(url)

    # Block the main thread until the server stops (Ctrl-C, error, etc.).
    stop_event = threading.Event()

    def _on_signal(signum: int, frame: object) -> None:
        _log.info("Shutting down (signal %d)", signum)
        server.should_exit = True
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        stop_event.wait()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5.0)
        _log.info("Stopped")


def _wait_for_ready(server: uvicorn.Server, timeout: float) -> None:
    """Block until uvicorn is serving or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return
        if server.should_exit:
            return
        time.sleep(0.05)
    _log.warning("Server did not signal ready within %.1fs; opening browser anyway", timeout)


@cli.command()
@click.argument("folder", type=click.Path(exists=False, file_okay=False, path_type=Path))
def discover(folder: Path) -> None:
    """List every .gpx file under FOLDER (debug aid; the UI does this too)."""
    paths = gpx_loader.discover(folder)
    click.echo(f"Found {len(paths)} GPX file(s) under {folder}:")
    for p in paths:
        click.echo(f"  {p}")


def main() -> None:
    """Standalone entry point: route to ``serve`` if no subcommand given."""
    # Help / version flags go to Click. Everything else with no subcommand
    # falls through to serve.
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    cli.main()


if __name__ == "__main__":
    main()
