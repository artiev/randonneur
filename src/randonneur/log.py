"""Project-wide logging, configured once and re-callable.

The format is ``<symbol> <feature> · <message>`` per the behaviour file:
a coloured severity symbol (only WARNING+ signals severity; INFO/DEBUG
share a dim middle-dot), the short feature name taken from the logger
suffix after ``randonneur.``, and the message. rich renders the colour;
``markup=False`` + a ``NullHighlighter`` keep the message text from being
auto-styled by rich's console markup (a GPX path or log arg containing
``[...]`` would otherwise be mis-parsed).

``configure_logging`` is safe to call more than once: it removes any
existing handlers on the ``randonneur`` parent before installing its own,
so toggling verbosity doesn't stack handlers.
"""

from __future__ import annotations

import logging
from typing import Final

from rich.highlighter import NullHighlighter as _NullHighlighter
from rich.logging import RichHandler
from rich.text import Text

# Parent logger — every ``logging.getLogger("randonneur.foo")`` ends up here.
_PARENT_NAME: Final = "randonneur"

# (level, symbol, style). INFO and DEBUG share a dim middle-dot because
# severity at low levels is conveyed by verbosity, not by colour.
_SYMBOLS: Final[dict[int, tuple[str, str]]] = {
    logging.DEBUG: ("·", "dim"),
    logging.INFO: ("·", "dim"),
    logging.WARNING: ("⚠", "yellow"),
    logging.ERROR: ("✗", "red"),
    logging.CRITICAL: ("✗", "bold red"),
}

# Render-time toggle so tests can silence the log without touching config.
_QUIET: bool = False


# ─── Public API ───────────────────────────────────────────────────────────────


def configure_logging(verbose: bool = False) -> None:
    """Install the rich handler on the ``randonneur`` parent logger.

    Re-callable: any existing handler on the parent (or its ancestors'
    propagation path) is removed first so this is idempotent.

    ``verbose=True`` sets the whole randonneur tree to DEBUG; the default
    is INFO.
    """
    parent = logging.getLogger(_PARENT_NAME)
    parent.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Drop any existing handlers so repeated calls don't stack.
    for h in list(parent.handlers):
        parent.removeHandler(h)
    # And stop inheriting root-configured handlers — the rich handler
    # below is the single source of log output for the randonneur tree.
    parent.propagate = False

    # No formatter is set: ``logging.Handler.format`` then returns
    # ``record.getMessage()`` (the raw message), and ``_SymbolHandler``
    # decorates it with the symbol + feature in ``render_message``.
    handler = _SymbolHandler(
        console=None,  # rich picks the default console
        show_path=False,
        show_time=False,
        show_level=False,
        markup=False,
        # The kwarg was renamed `highlight` → `highlighter` in rich 13;
        # passing a NullHighlighter is the documented way to keep log
        # messages out of rich's auto-styling. See rich changelog.
        highlighter=_NullHighlighter(),
    )
    parent.addHandler(handler)


def get_logger(feature: str) -> logging.Logger:
    """Return a child logger named ``randonneur.<feature>``."""
    return logging.getLogger(f"{_PARENT_NAME}.{feature}")


def set_quiet(quiet: bool) -> None:
    """Temporarily silence all randonneur logs (used by tests)."""
    global _QUIET
    _QUIET = quiet
    logging.getLogger(_PARENT_NAME).disabled = quiet


# ─── Internals ────────────────────────────────────────────────────────────────


class _SymbolHandler(RichHandler):
    """RichHandler that prefixes each line with a coloured severity symbol.

    The default ``RichHandler.render_message`` wraps the formatted message
    in a plain ``Text`` and lets ``highlighter``/``markup`` style it — but
    we set both off so the message stays literal. We override it here to
    build the styled ``Text`` ourselves (symbol + feature + "· " + message)
    so the colour actually reaches ``console.print``.

    The previous shape ran this styling through a ``logging.Formatter``
    that returned ``text.plain``, which threw the styles away — the line
    rendered plain. Returning a renderable from ``render_message`` is the
    intended extension point, so this is the fix.
    """

    def render_message(self, record: logging.LogRecord, message: str) -> Text:
        symbol, style = _SYMBOLS.get(record.levelno, ("·", "dim"))
        feature = record.name
        # Strip the "randonneur." prefix for compactness.
        if feature.startswith(f"{_PARENT_NAME}."):
            feature = feature[len(_PARENT_NAME) + 1 :]
        text = Text()
        text.append(f"{symbol} ", style=style)
        text.append(f"{feature} ", style="bold")
        text.append("· ", style="dim")
        # ``message`` is record.getMessage() (no formatter is set). Append
        # without a style so user text is never coloured by us; markup is
        # off and the highlighter is null, so rich won't style it either.
        text.append(message)
        return text
