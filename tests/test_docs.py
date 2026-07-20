"""Documentation tests.

A single guard test: the README must parse as valid reStructuredText
under ``python -m docutils --strict``. This is the project's working
contract (see ``agent/agent-behaviour.md`` §1) — RST errors break
PyPI's long-description rendering, so we'd rather catch a typo at
``pytest`` time than after a release.

Why one test, not one per section: docutils has no per-section API.
A single ``publish_string`` call validates the whole document.

Why skip if docutils isn't installed: docutils isn't a runtime
dep (only PyPI needs it for the long-description). Falling back
to "test skipped" keeps the test suite usable in minimal envs.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.rst"


def test_readme_is_valid_rst() -> None:
    # First try the in-process API — faster, gives a Python
    # exception with location info if the document is malformed.
    try:
        from docutils.core import publish_string  # type: ignore[import-not-found]
        from docutils.utils import SystemMessage  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("docutils not installed; install it to lint the README")

    # ``report_level=2`` matches ``--strict`` (warnings become
    # errors); ``halt_level=2`` raises on the first warning.
    # ``writer='html'`` is the new-style (non-deprecated) arg;
    # ``writer_name`` is the pre-docutils-2.0 spelling.
    settings = {"report_level": 2, "halt_level": 2, "writer": "html"}
    try:
        publish_string(README.read_text(encoding="utf-8"), settings_overrides=settings)
    except SystemMessage as e:
        pytest.fail(f"README.rst is not valid RST under --strict: {e}")


def test_readme_passes_docutils_strict_cli() -> None:
    # The exact command from the behaviour file, run as a
    # subprocess so the test fails the same way the docutils
    # docs would. Skipped if docutils isn't on PATH (e.g. a
    # minimal CI image that only has the runtime deps).
    if shutil.which(sys.executable) is None:
        pytest.skip("no python on PATH")
    result = subprocess.run(
        [sys.executable, "-m", "docutils", "--strict", str(README)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"docutils --strict README.rst failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\n"
        f"stderr: {result.stderr[:1000]}"
    )


def test_readme_mentions_every_cli_subcommand() -> None:
    # The README is the user's primary reference for what
    # ``randonneur`` can do. If we add a subcommand and forget
    # to document it, the user can't find it. Pin the names of
    # the two current subcommands.
    text = README.read_text(encoding="utf-8")
    for cmd in ("serve", "discover"):
        assert f"randonneur {cmd}" in text or f"randonneur.{cmd}" in text, (
            f"README.rst does not mention the {cmd!r} subcommand"
        )


def test_readme_documents_required_cli_options() -> None:
    # The folder is now a required ``--directory`` flag on ``serve``;
    # a user who skips the README and types bare ``randonneur serve``
    # gets a clear Click error, but the README is still the
    # authoritative reference for how to start the server. Pin the
    # flag here so a future flag rename is caught in CI.
    text = README.read_text(encoding="utf-8")
    assert "--directory" in text, "README.rst does not document the --directory flag"


def test_readme_mentions_every_env_var() -> None:
    # The Thunderforest env var is the only one the app reads;
    # if we add another, this test will fail until the README
    # is updated, which is the right direction.
    text = README.read_text(encoding="utf-8")
    for env in ("RANDONNEUR_THUNDERFOREST_KEY",):
        assert env in text, f"README.rst does not document env var {env!r}"
