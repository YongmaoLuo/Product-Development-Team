"""Startup recovery must not leak SQLite connections.

Backstory (2026-09-23)
----------------------
The backend server opened **194 live connections** to ``state.db`` and held
them for the whole life of the process — 388 file descriptors, roughly
half the process's total. The survivors were not a gradual trickle;
they were the residue of one boot.

``server.py`` restores in-memory state at startup by scanning every
plan directory::

    _recover_execution_states(PLANS_DIR)
    _recover_verification_states(PLANS_DIR)

The two are twins, and one of them was written correctly:

* :func:`_recover_execution_states` opens a **single** connection for
  the whole scan (``_open_state_machine()``) and reuses it.
* :func:`_recover_verification_states` called
  ``_load_verification_runtime_state()`` once per plan, and that helper
  opened a connection whose ``close()`` existed on **no exit path at
  all** — all four ``return`` statements dropped it on the floor.

With 331 directories under ``plans/``, one boot leaked up to 331
handles (two descriptors each: the database plus its WAL). The count
that actually survived was 194 — neither all of them nor none — because
a leaked ``sqlite3.Connection`` has no surviving reference, so it is
reclaimed only when CPython's **cyclic** GC happens to run. That is also
why the process's file-descriptor count drifted up and down by a few
instead of growing monotonically, which is what made it look like a
slow drip rather than a single burst.

Why this is worth a pin rather than a one-line fix
--------------------------------------------------
The same intermediate state that hid the leak also hid the outage: the
backend server kept answering ``/health`` with 200 while every data-plane
endpoint returned 500, so the supervisor reported it "running" and the
scheduler reported the workflow "not running". Anything that depends on
file descriptors has to be pinned by a test, because the failure mode
is silent and it looks like something else.

The guard is deliberately narrow: it asserts on connections the
recovery scan itself opened, so it cannot be satisfied by unrelated
cleanup elsewhere.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server as _server_mod  # noqa: E402
from state_machine.db import connection as _conn_mod  # noqa: E402

#: Enough plan directories that a per-plan leak is unmistakable (40
#: leaked connections is ~80 descriptors), while staying fast enough
#: that running this on every PR costs nothing.
_PLAN_COUNT = 40


def _make_plan_dirs(root: Path, count: int) -> Path:
    """Create ``count`` empty plan directories under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / f"plan-fd-{i:03d}").mkdir()
    return root


def _open_fds() -> int:
    """Return the number of descriptors this process currently holds."""
    return len(os.listdir("/dev/fd"))


def _is_closed(conn: sqlite3.Connection) -> bool:
    """True when ``conn`` has been closed.

    ``sqlite3`` refuses any statement on a closed handle with
    ``ProgrammingError``; there is no public ``is_closed`` attribute,
    so the exception is the documented probe.
    """
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class _ConnectionSpy:
    """Record every connection the code under test opens."""

    def __init__(self) -> None:
        self.opened: list[sqlite3.Connection] = []
        self._real_open = _conn_mod.open

    def __call__(self, db_path):  # noqa: ANN001 - mirrors connection.open
        conn = self._real_open(db_path)
        self.opened.append(conn)
        return conn

    def leaked(self) -> list[sqlite3.Connection]:
        return [c for c in self.opened if not _is_closed(c)]


@pytest.fixture
def spy(monkeypatch) -> _ConnectionSpy:
    """Patch the single chokepoint every reader goes through.

    ``_load_verification_runtime_state`` imports ``open`` from this
    module *at call time* (``from state_machine.db.connection import
    open as open_db`` inside the function body), so patching the module
    attribute is enough to see every connection it opens.
    """
    s = _ConnectionSpy()
    monkeypatch.setattr(_conn_mod, "open", s)
    monkeypatch.setattr(_server_mod, "_verification_state", {})
    monkeypatch.setattr(_server_mod, "_execution_state", {})
    return s


class TestVerificationRecoveryClosesItsConnections:
    """The leak that produced 194 orphaned handles on 2026-09-23."""

    def test_one_connection_per_plan_is_opened_and_closed(self, spy, tmp_path):
        plans = _make_plan_dirs(tmp_path / "plans", _PLAN_COUNT)

        _server_mod._recover_verification_states(plans)

        # Vacuity guard first: if the scan stopped opening per-plan
        # connections (e.g. it was refactored to share one), the
        # "nothing leaked" assertion below would hold trivially and
        # this file would stop testing anything.
        assert len(spy.opened) == _PLAN_COUNT, (
            f"expected the recovery scan to open one connection per plan "
            f"({_PLAN_COUNT}); observed {len(spy.opened)}. Either the scan "
            f"short-circuited (check that every directory was created) or "
            f"the traversal changed shape — this pin needs updating."
        )

        leaked = spy.leaked()
        assert not leaked, (
            f"{len(leaked)} of {_PLAN_COUNT} connections opened by "
            f"_recover_verification_states were never closed. Each holds two "
            f"file descriptors (the database and its WAL) until the cyclic "
            f"GC happens to reclaim it, so a real plans/ tree leaks hundreds. "
            f"Close the connection on every exit path in "
            f"_load_verification_runtime_state."
        )

    def test_the_fd_count_returns_to_its_starting_value(self, spy, tmp_path):
        """End-to-end restatement in the unit the outage was measured in.

        The connection-object assertion above is precise; this one is
        the operator-visible symptom, and it also catches a future
        regression that closes the ``sqlite3.Connection`` while leaving
        an underlying descriptor open.
        """
        plans = _make_plan_dirs(tmp_path / "plans", _PLAN_COUNT)

        before = _open_fds()
        _server_mod._recover_verification_states(plans)
        after = _open_fds()

        assert after <= before, (
            f"the recovery scan grew the process's descriptor count from "
            f"{before} to {after} (+{after - before}) across {_PLAN_COUNT} "
            f"plan directories. A plans/ tree two orders of magnitude larger "
            f"leaks proportionally more."
        )

    def test_a_plan_with_a_persisted_record_closes_too(self, monkeypatch, tmp_path):
        """The success path is a separate ``return`` — pin it as well.

        Every directory in a real ``plans/`` tree takes the
        ``record is None`` branch, because most plans have no
        verification row. The branch that *returns data* leaked just as
        hard, and a future edit could easily fix only the common one.
        """
        plans = _make_plan_dirs(tmp_path / "plans", 3)
        spy = _ConnectionSpy()
        monkeypatch.setattr(_conn_mod, "open", spy)

        from state_machine.repositories import verification_repository as _vr

        record = {
            "runtime_state": '{"verification_status": "running"}',
            "verification_status": "running",
            "round": 1,
            "max_rounds": 3,
            "results": None,
            "started_at": None,
            "updated_at": None,
            "verification_stop_reason": None,
        }
        monkeypatch.setattr(
            _vr, "VerificationRepository",
            lambda conn: type("R", (), {"current": staticmethod(lambda plan_id: record)})(),
        )

        data = _server_mod._load_verification_runtime_state("plan-fd-000")

        assert data is not None, (
            "the stub repo should have produced a record — if this fails the "
            "test is exercising the empty path and proving nothing"
        )
        assert len(spy.opened) == 1, (
            f"expected exactly one connection, got {len(spy.opened)}"
        )
        assert not spy.leaked(), (
            "the data-returning path did not close its connection"
        )


class TestExecutionRecoveryKeepsItsSharedConnection:
    """Pin the *correct* twin, so the fix for one cannot break the other.

    ``_recover_execution_states`` deliberately opens once and reuses.
    Someone "harmonising" the two helpers by giving this one the same
    per-plan shape as its sibling would reintroduce the leak from the
    other direction, so the shared-connection property is pinned here.
    """

    def test_the_scan_opens_exactly_one_connection_for_many_plans(
        self, spy, tmp_path,
    ):
        plans = _make_plan_dirs(tmp_path / "plans", _PLAN_COUNT)

        # _open_state_machine short-circuits to None when the state.db
        # file is absent (fresh install), so materialise it first.
        # Do it through the spy's own opener so the setup connection is
        # counted, then clear the ledger — the assertion below is about
        # what the scan opens, not what the fixture did.
        setup = spy(_server_mod._state_db_path())
        setup.close()
        spy.opened.clear()

        _server_mod._recover_execution_states(plans)

        assert len(spy.opened) == 1, (
            f"_recover_execution_states must open a single connection for the "
            f"whole scan (see its docstring / _open_state_machine); it opened "
            f"{len(spy.opened)} for {_PLAN_COUNT} plans."
        )
        assert not spy.leaked(), (
            "the shared connection was still open when the scan returned — "
            "_recover_execution_states must close it via "
            "_close_state_machine."
        )


class TestPerRequestHelpersCloseTheirConnection:
    """The same contract for the short-lived call sites.

    ``_get_project_dir`` runs at the top of ``/execution/{id}/progress``
    (before the early-return shortcut for an active plan), and
    ``_is_placeholder_prd`` runs on every PRD-phase write. Each opens a
    connection; each must release it.
    """

    def test_get_project_dir_releases_its_connection(self, spy):
        setup = spy(_server_mod._state_db_path())
        setup.close()
        spy.opened.clear()

        _server_mod._get_project_dir("plan-that-is-not-loaded")

        assert spy.opened, (
            "_get_project_dir did not open a connection — the in-memory "
            "shortcut answered instead, so this test proves nothing"
        )
        assert not spy.leaked(), (
            "_get_project_dir leaked its connection on the fall-through path"
        )

    def test_is_placeholder_prd_releases_its_connection(self, spy, tmp_path):
        plan_dir = tmp_path / "plan-placeholder"
        plan_dir.mkdir()
        (plan_dir / "prd.json").write_text('{"_placeholder": true}')

        setup = spy(_server_mod._state_db_path())
        setup.close()
        spy.opened.clear()

        _server_mod._is_placeholder_prd(plan_dir, plan_id="plan-placeholder")

        assert spy.opened, (
            "_is_placeholder_prd did not open a connection — it fell back to "
            "the on-disk PRD, so this test proves nothing"
        )
        assert not spy.leaked(), (
            "_is_placeholder_prd leaked its connection"
        )


class TestEveryCallSiteReleasesTheConnection:
    """Static inventory over the whole module.

    ``_open_state_machine`` returned only the four repositories until
    2026-09-23, so **no caller could close the connection even in
    principle**. It now hands back the handle as the first tuple
    element, and the runtime tests above cover the sites that existed
    when this was written.

    This test is the part that keeps working after the next endpoint is
    added: it walks the module and fails as soon as a function calls
    ``_open_state_machine`` without also calling
    ``_close_state_machine``.
    """

    def test_every_open_state_machine_call_site_closes_it(self):
        source = (BACKEND_DIR / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        def enclosing(lineno: int):
            best = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.lineno <= lineno <= (node.end_lineno or 0):
                        if best is None or node.lineno > best.lineno:
                            best = node
            return best

        call_sites = sorted(
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "_open_state_machine"
        )
        assert call_sites, (
            "no _open_state_machine call sites found — the helper was "
            "renamed or removed; update this guard with it"
        )

        offenders = []
        for lineno in call_sites:
            fn = enclosing(lineno)
            assert fn is not None, f"call site at line {lineno} has no function"
            segment = ast.get_source_segment(source, fn) or ""
            if "_close_state_machine" not in segment:
                offenders.append(f"{fn.name} (def at line {fn.lineno})")

        assert not offenders, (
            "these functions call _open_state_machine without a matching "
            "_close_state_machine, so every call leaks a SQLite handle "
            "(two file descriptors) for the life of the process: "
            + "; ".join(offenders)
        )
