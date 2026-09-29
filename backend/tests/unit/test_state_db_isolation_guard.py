"""A test run must never reach the operator's live ``state.db``.

Backstory (2026-09-13)
----------------------
``backend/tests/perf/test_repo_scheduler_refiner_watchdog.py`` resolved its
database as::

    db_env = os.environ.get("PDT_STATE_DB_PATH")
    conn = _open_db(db_env) if db_env else _open_db(
        Path(__file__).resolve().parents[3] / "state.db"
    )

The env var is set by a conftest, so the fallback looked inert — but any
invocation that did not collect that conftest wrote straight to
production. It ran on 2026-09-13 and wrote **200 ``plan_tasks`` rows**
into the operator's database, plus 3 more from ``test_same_id_loop_*``.
The plan id was pytest's ``tmp_path`` directory name (``test_repo_update_
status_wall_t0`` — a truncated test name), so the junk surfaced in the
plan list looking like a real plan. The rows were cleaned up on
2026-09-23; a backup is at
``/tmp/state.db.bak-before-testresidue-cleanup-*``.

Why the guard lives in the connection factory
---------------------------------------------
This repository has four test trees with four different rootdirs
(``tests/``, ``backend/tests/``, ``backend/state_machine/tests/``,
``tools/tests/``), so no single conftest is a last line of defence, and a
fifth tree can always appear. Every reader and writer of ``state.db``
already shares one chokepoint — ``state_machine.db.connection.open`` —
so that is where the refusal lives. ``server._state_db_path`` carries a
second one, because the bare ``sqlite3.connect`` calls in ``server.py``
never pass through the factory.

These tests pin both, and pin that the guard is *narrow*: it must not
touch the executor, the backend server, or an ordinary throwaway database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.db.connection import (  # noqa: E402
    ALLOW_REAL_DB_ENV,
    REAL_STATE_DB,
    _refuse_real_state_db_under_pytest,
    open as open_db,
)

import server  # noqa: E402

#: The unpatched resolver, captured before any fixture can replace it.
#: See ``TestServerResolverIsGuarded`` for why the module attribute is
#: not read inside the tests.
_REAL_SERVER_STATE_DB_PATH = server._state_db_path


class TestTheGuardTargetsTheRightFile:
    def test_real_state_db_points_at_the_declared_database(self):
        """If this constant were computed wrong, every assertion below
        would pass while the real database sat unprotected.

        Since 2026-09-28 the location is declared once, in
        ``config_paths.STATE_DB`` (``<repo>/.pdt/state.db``), and this
        constant is an alias for it. So the useful check is *not* that
        this module reproduces a literal path — it is that the alias did
        not drift away from the declaration, plus that the declaration
        still points at the private state directory. Both are asserted
        independently: equality alone would pass if the two drifted
        together.
        """
        from config_paths import STATE_DB, STATE_DIR

        assert REAL_STATE_DB.name == "state.db"
        assert REAL_STATE_DB == STATE_DB
        assert REAL_STATE_DB.parent == STATE_DIR
        assert STATE_DIR.name == ".pdt"
        assert STATE_DIR.parent == BACKEND_DIR.parent

    def test_the_suite_actually_runs_under_pytest(self):
        """The guard is keyed on ``PYTEST_CURRENT_TEST``; if pytest ever
        stopped setting it, every test here would pass vacuously."""
        assert os.environ.get("PYTEST_CURRENT_TEST"), (
            "PYTEST_CURRENT_TEST is unset — the guard would be a no-op and "
            "these tests would pass for the wrong reason"
        )


class TestOpeningTheLiveDatabaseIsRefused:
    def test_open_refuses_the_live_database(self):
        with pytest.raises(RuntimeError) as excinfo:
            open_db(REAL_STATE_DB)
        message = str(excinfo.value)
        assert "refusing to open the live state.db" in message
        assert "PDT_STATE_DB_PATH" in message, (
            "the error must tell the caller how to fix it"
        )

    def test_the_helper_refuses_it_directly(self):
        with pytest.raises(RuntimeError):
            _refuse_real_state_db_under_pytest(REAL_STATE_DB)

    def test_a_symlink_to_the_live_database_is_refused_too(self, tmp_path):
        """``resolve()`` rather than a string compare, so an indirect path
        cannot walk around the guard."""
        link = tmp_path / "innocent-looking.db"
        try:
            link.symlink_to(REAL_STATE_DB)
        except OSError:
            pytest.skip("symlinks unavailable on this filesystem")
        with pytest.raises(RuntimeError):
            open_db(link)


class TestTheGuardStaysNarrow:
    def test_a_throwaway_database_still_opens(self, tmp_path):
        conn = open_db(tmp_path / "state.db")
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

    def test_the_same_path_opens_outside_pytest(self, monkeypatch):
        """The backend server and the executor legitimately open this file."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        _refuse_real_state_db_under_pytest(REAL_STATE_DB)  # must not raise

    def test_the_escape_hatch_disables_the_refusal(self, monkeypatch):
        """For a deliberate, operator-approved one-off audit."""
        monkeypatch.setenv(ALLOW_REAL_DB_ENV, "1")
        _refuse_real_state_db_under_pytest(REAL_STATE_DB)  # must not raise

    def test_an_unnamed_path_is_not_mistaken_for_the_live_database(
        self, monkeypatch,
    ):
        monkeypatch.delenv("PDT_STATE_DB_PATH", raising=False)
        _refuse_real_state_db_under_pytest("/tmp/definitely-not-state.db")


class TestTheRefusalIsWhatBlocksIt:
    """Guard the guard, without ever touching the operator's file.

    Reverting the production fix turns this module into a collection
    error (the old ``connection.py`` does not export the guard), which
    is red for the wrong reason. So instead: point the module's notion
    of "the live database" at a scratch file, confirm the call is
    refused, then neutralise the guard and confirm the *same* call now
    succeeds. That isolates the refusal as the cause.
    """

    def test_neutralising_the_guard_lets_the_same_call_through(
        self, tmp_path, monkeypatch,
    ):
        import state_machine.db.connection as conn_mod

        decoy = tmp_path / "state.db"
        monkeypatch.setattr(conn_mod, "REAL_STATE_DB", decoy)

        with pytest.raises(RuntimeError):
            conn_mod.open(decoy)

        monkeypatch.setattr(
            conn_mod, "_refuse_real_state_db_under_pytest", lambda _path: None,
        )
        conn = conn_mod.open(decoy)
        conn.close()


class TestServerResolverIsGuarded:
    """``server.py`` opens the database with bare ``sqlite3.connect`` in
    three places, which bypass the connection factory — its own resolver
    has to refuse the default for them.

    ``_REAL_SERVER_STATE_DB_PATH`` is captured at import time on purpose.
    An autouse fixture in ``tests/conftest.py`` replaces
    ``server._state_db_path`` with a ``lambda: tmp_path / "state.db"``;
    looking the attribute up inside a test would exercise that stub and
    assert nothing about the resolver under test.
    """

    def test_the_default_is_refused_when_no_override_is_set(self, monkeypatch):
        monkeypatch.delenv("PDT_STATE_DB_PATH", raising=False)
        with pytest.raises(RuntimeError) as excinfo:
            _REAL_SERVER_STATE_DB_PATH()
        assert "refusing to open the live state.db" in str(excinfo.value)

    def test_an_explicit_override_is_honoured(self, tmp_path, monkeypatch):
        override = tmp_path / "state.db"
        monkeypatch.setenv("PDT_STATE_DB_PATH", str(override))
        assert _REAL_SERVER_STATE_DB_PATH() == override

    def test_the_captured_resolver_is_not_the_conftest_stub(self):
        """Guard the guard: if the conftest ever stops patching, or our
        capture starts happening too late, this test would silently start
        asserting against the stub. The real resolver has no closure."""
        assert _REAL_SERVER_STATE_DB_PATH.__closure__ is None
        assert _REAL_SERVER_STATE_DB_PATH.__name__ == "_state_db_path"

    def test_plan_state_resolver_agrees(self, tmp_path, monkeypatch):
        """``plan_state._state_db_path`` mirrors the server resolver; it
        routes through the guarded factory, so an override must win for
        both."""
        import plan_state

        override = tmp_path / "state.db"
        monkeypatch.setenv("PDT_STATE_DB_PATH", str(override))
        assert plan_state._state_db_path() == override
