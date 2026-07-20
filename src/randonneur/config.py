"""Static project constants.

Keep this module import-only (no side effects) so anything else in the
project can import from it without paying a startup cost.
"""

from __future__ import annotations

import os
from pathlib import Path

# Re-export the track palette from gpx_loader so config.py stays the
# single import point for "what does the project look like".
from randonneur.gpx_loader import _PALETTE as PALETTE  # noqa: F401

# Server defaults. Bound to 127.0.0.1 by design (personal tool, no auth).
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8765

# ─── Tile sources ─────────────────────────────────────────────────────────────
#
# Each entry is the URL template Leaflet would use if it were talking
# to the provider directly. We pass the same template through the
# server so the browser sees ``/api/tiles/<source>/{z}/{x}/{y}.png``
# instead of the upstream hostname.
#
# ``{s}`` is the OSM-style a/b/c subdomain; ``_subdomain_for`` in
# ``tile_cache`` picks one. The templates use HTTPS — OpenTopoMap and
# Thunderforest both serve tiles over TLS in 2026.
#
# Why a whitelist rather than a generic proxy: prevents the server
# from being an open relay if a future bug ever feeds an attacker-
# controlled source into the URL.
TILE_URL_TEMPLATES: dict[str, str] = {
    "opentopomap": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    # Thunderforest Outdoors is a paper-map-style layer with contours,
    # POIs and trails. Free tier (no key) gets you 30-day stale tiles;
    # the live tile feed needs a free API key. v1 doesn't expose
    # Thunderforest through the UI; the entry is here so the rate-limit
    # and URL tables are in one place when it ships.
    "thunderforest-outdoors": "https://{s}.tile.thunderforest.com/outdoors/{z}/{x}/{y}.png"
    + "?apikey={apikey}",
}

# Human-readable name for each source — used by the settings panel.
# Kept here (next to the URL table) so the source ↔ display-name
# mapping is in one place.
TILE_SOURCE_LABELS: dict[str, str] = {
    "opentopomap": "OpenTopoMap",
    "thunderforest-outdoors": "Thunderforest Outdoors",
}

# The default source on first launch. Listed by id, not by URL.
DEFAULT_TILE_SOURCE: str = "opentopomap"

# Per-host request rate (requests per second). These are the *long-run*
# rates we aim for; the RateLimiter smooths bursts to that mean.
#
# OpenTopoMap's published policy asks for ≤1 req/s/client. Exceeding
# this gets the IP throttled or blocked; the cache makes the practical
# rate effectively zero after the first load, so the limiter only
# matters during the initial folder open.
#
# Thunderforest's free tier allows ~8 req/s; 8 is also what the official
# Leaflet example uses.
TILE_RATE_LIMITS: dict[str, float] = {
    "opentopomap": 1.0,
    "thunderforest-outdoors": 8.0,
}

# Env var that holds the Thunderforest API key. Optional; if unset,
# the source is "unavailable" in the settings panel.
THUNDERFOREST_API_KEY_ENV: str = "RANDONNEUR_THUNDERFOREST_KEY"


def thunderforest_api_key() -> str | None:
    """Return the Thunderforest API key from the env, or None.

    Read lazily so tests can monkeypatch ``os.environ`` without
    having to reload the module. The key is treated as a secret:
    callers should never put it in logs or JSON responses.
    """
    return os.environ.get(THUNDERFOREST_API_KEY_ENV) or None


def cache_dir() -> Path:
    """Return the directory for tile caches and similar local state.

    Honours ``$XDG_CACHE_HOME`` if set, otherwise falls back to
    ``~/.cache/randonneur``. Imported lazily so config.py stays free
    of side effects at import time (just a function call, not I/O).
    """
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "randonneur"
    return Path.home() / ".cache" / "randonneur"
