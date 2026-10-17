"""The suite must never read the operator's own CC Switch database.

What went wrong
---------------

``server._lifespan`` calls ``load_providers()``, which asks
``cc_switch.get_provider()`` about every name in the fallback chain, and
``load_providers`` **re-raises** ``CCSwitchError`` rather than degrade.  A
machine without CC Switch -- which is every CI runner, and is the audience
this repository is written for -- therefore failed every test that boots the
app:

    cc_switch.CCSwitchDBNotFoundError: CC Switch database not found:
    ~/.cc-switch/cc-switch.db            (a CI runner's home)

while the same commit passed on a maintainer's machine, which has the desktop
app installed.  The suite's verdict was decided by an application this
repository does not ship.

The fix is ``_pytest_hermetic_env.install_hermetic_env()`` -- already the one
installer both pytest roots call at import -- pointing ``$HOME`` at a
throwaway directory that holds a minimal ``cc-switch.db``.  These tests pin
that arrangement.  Without them, deleting the database build would leave the
suite green on any developer machine that still has ``~/.cc-switch`` and red
on CI, which is the failure this file exists to make loud instead of silent.

What is deliberately NOT asserted
---------------------------------

That a provider resolves.  The installed database holds the ``providers``
table but **no rows**, so ``get_provider`` answers ``None`` and
``load_providers`` starts with an empty provider list -- see
``write_minimal_cc_switch_db``.  A row here would be a fabricated provider
with an endpoint and a credential that does not exist, and any test asserting
a provider was resolved would be asserting against this file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import cc_switch

from _pytest_hermetic_env import REAL_HOME_ENV


def test_the_launch_home_is_published_before_it_is_redirected():
    """``$HOME`` is a scratch directory, so the real one has to be recorded.

    Not a convenience. The redirect puts ``$HOME`` under the temp root, and
    the Edit/Write containment guard allows the whole temp root
    unconditionally. Any test whose premise is "this path is outside tempdir"
    therefore gets a path that is *inside* it once the redirect lands, and
    stops testing the branch it was written for -- either failing, or (worse)
    passing because every path is allowed.

    Anchored to the env var rather than to the literal operator home: this
    file must not assert anything about the machine it runs on.
    """
    real_home = Path(os.environ[REAL_HOME_ENV])

    assert real_home != Path.home(), "the redirect did not happen -- this pin is vacuous"
    assert Path(tempfile.gettempdir()) not in real_home.parents, (
        "the recorded launch home is itself a tempdir path, so it cannot "
        "anchor a case that needs a path outside the scratch zone"
    )


def test_the_launch_home_survives_a_reinstall_of_the_environment():
    """``setdefault``, so a nested pytest session keeps the *outer* home.

    A child inherits both this variable and an already-redirected ``$HOME``.
    Recording again on the way in would store the parent's fake home as the
    "real" one, and every case anchored to it would be quietly vacuous in
    exactly the nested runs that already had trouble.
    """
    from _pytest_hermetic_env import install_hermetic_env

    before = os.environ[REAL_HOME_ENV]
    install_hermetic_env()  # second call in this process -- must be a no-op
    assert os.environ[REAL_HOME_ENV] == before

    with tempfile.TemporaryDirectory(prefix="fake-home-") as d:
        os.environ["HOME"] = d  # stand in for an inherited, already-redirected home
        try:
            install_hermetic_env()
            assert os.environ[REAL_HOME_ENV] == before, (
                "the redirect's own scratch home was recorded as the launch home"
            )
        finally:
            os.environ["HOME"] = str(Path(os.environ["PDT_TEST_FAKE_HOME"]))


def test_the_session_home_is_a_throwaway_directory():
    """``$HOME`` names a scratch directory, not the operator's.

    The redirect is what makes the rest of this file possible: with the real
    home in place, ``cc_switch`` finds a developer's real database and the
    suite quietly depends on whatever that database happens to hold.
    """
    home = Path.home()

    assert home == Path(os.environ["PDT_TEST_FAKE_HOME"])
    assert home != Path(tempfile.gettempdir())
    # macOS resolves TMPDIR to a per-user directory under /var/folders, so the
    # containment check is on the temp root rather than on an exact prefix.
    assert Path(tempfile.gettempdir()) in home.parents


def test_a_usable_cc_switch_database_is_installed_under_that_home():
    """The installed file passes ``cc_switch``'s own validator.

    ``probe()`` is the project's "is this database actually usable" check --
    it opens the file read-only, reads ``sqlite_master``, and requires the
    stock ``providers`` columns.  Asserting on it means the fixture has to
    satisfy the real reader's contract rather than merely exist.
    """
    status = cc_switch.probe()

    assert status.available is True, status.detail
    assert status.path == Path.home() / ".cc-switch" / "cc-switch.db"


def test_resolution_lands_inside_the_session_home():
    """``resolve_db_path`` -- the single resolver every reader goes through --
    points at the throwaway database and nowhere else.

    Anchored to ``PDT_TEST_FAKE_HOME`` rather than to ``Path.home()`` on
    purpose: with the database absent, resolution falls through to
    ``_default_db_path()``, which is *also* ``$HOME``-derived, so an
    assertion written against ``Path.home()`` passes vacuously on a runner
    whose home is empty -- which is exactly the case this file guards.
    """
    session_home = Path(os.environ["PDT_TEST_FAKE_HOME"])
    resolved = cc_switch.resolve_db_path()

    assert resolved == session_home / ".cc-switch" / "cc-switch.db"
    assert session_home in resolved.parents
    assert resolved.exists(), "resolution names a database nothing created"


def test_the_installed_database_names_no_provider():
    """No row, so nothing can mistake this fixture for a resolved provider.

    ``server.load_providers`` skips a ``None`` provider by name and starts
    with an empty list; it raises on an unreadable database.  An empty table
    is the one shape that is both readable and providerless.
    """
    assert cc_switch.list_provider_names() == []
    assert cc_switch.get_provider("any-name-at-all") is None
