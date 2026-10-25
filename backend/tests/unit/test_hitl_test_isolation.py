"""Test isolation for the HITL persistence surface.

Background
----------
The HITL feature (human-in-the-loop decisions) gives the backend a second
persistence surface: the ``plan_decisions`` table in the state database,
plus the per-plan artifacts a decision asks for.  Both outlive the call
that created them — a decision is asked by one thread and answered by
another, minutes later — so a decision row that survives the test that
wrote it is read by the *next* test as if it were a live question.  That
is an order-dependent failure: it looks like a flake, it is reproducible
only on the runs where the two tests happen to be adjacent, and the
evidence points at the wrong code.

The suite's answer is the ``isolated_plans_dir`` fixture in
``backend/tests/conftest.py``, which redirects every persistence root at
the per-test ``tmp_path``.  This module pins that contract for the HITL
surface, in three parts:

  1. ``test_plans_root_redirected`` — the plans root (where the
     per-plan artifacts of a decision live) resolves to the per-test
     tmp root, through the *production* resolver every leaf utility
     calls, not merely through the environment variable.
  2. ``test_decision_table_starts_empty`` — the ``plan_decisions`` table
     of the per-test database holds no rows when a test starts, so a
     decision asked by one test cannot be answered by another.
  3. ``test_repo_plans_untouched`` — the repository's own ``plans/`` tree
     is not written to by a test, which is the operator-visible half of
     the same contract.

Two further tests pin the *mechanism* rather than the outcome:

  4. ``test_unregistered_persistence_knob_is_rejected`` — a persistence
     knob the fixture does not know about is refused, loudly.  A HITL
     module that grows a new ``PDT_*`` knob without teaching the fixture
     to redirect it is exactly the "new persistence path that was not
     redirected" case; catching it at session start is the difference
     between a named failure and a flake nobody can reproduce.
  5. ``test_request_guard_injection_still_installed`` — the request-guard
     header injection the suite depends on is still in place.  The
     isolation work in this area must not come at the cost of the guard.

Test command::

    pytest backend/tests/unit/test_hitl_test_isolation.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Reaching the conftest
# ---------------------------------------------------------------------------
#
# ``import conftest`` does not work: pytest loads the file under a dotted
# name derived from the rootdir, and the plugin manager registers it
# under a name of its own, so neither ``import conftest`` nor
# ``get_plugin`` finds it.  Looking for the loaded module by the
# attribute this file's contract is expressed in survives every rootdir
# the suite is invoked from — the same trick ``test_process_utils.py``
# uses to reach the killpg tripwire.


def _conftest_module():
    """Return the loaded ``backend/tests/conftest.py`` module."""
    for name, module in list(sys.modules.items()):
        if name.endswith("conftest") and hasattr(module, "_HITL_PERSISTENCE_ROOTS"):
            return module
    raise AssertionError(
        "the backend conftest is not loaded — no conftest module exposes "
        "_HITL_PERSISTENCE_ROOTS, so the HITL isolation contract cannot "
        "be checked"
    )


def _repo_plans_listing() -> tuple[str, ...]:
    """Return the names under the repository's ``plans/`` tree.

    Empty when the tree does not exist, which is the case in CI (the
    directory is gitignored) and the case the contract cares about: a
    test that creates it has written into the operator's checkout.
    """
    import config_paths

    repo_plans = config_paths.PROJECT_ROOT / "plans"
    if not repo_plans.is_dir():
        return ()
    return tuple(sorted(p.name for p in repo_plans.iterdir()))


# ---------------------------------------------------------------------------
# 1. The plans root is redirected
# ---------------------------------------------------------------------------


def test_plans_root_redirected(isolated_plans_dir, tmp_path):
    """The plans root resolves to the per-test tmp root.

    Three things have to agree, and the fixture is what makes them
    agree:

      * the environment variable every leaf utility reads
        (``config_paths.resolve_plans_dir`` honours it);
      * the resolver itself — the function the *application* calls, so
        this is not a statement about the env var but about where the
        backend would actually write;
      * ``server.PLANS_DIR``, the module global the HTTP routes use.

    The last assertion is the fixture's own guard: it is the check the
    fixture runs on every test, and running it here proves the guard
    accepts the state the fixture creates (the guard-on-guard test
    below proves it rejects a state outside the tmp root).
    """
    import config_paths
    import server

    expected = tmp_path / "plans"

    assert os.environ["PDT_PLANS_DIR"] == str(expected), (
        f"PDT_PLANS_DIR must point at the per-test plans root; got "
        f"{os.environ.get('PDT_PLANS_DIR')!r}, expected {str(expected)!r}"
    )
    assert config_paths.resolve_plans_dir() == expected, (
        "the production plans resolver must resolve to the per-test tmp "
        f"root; got {config_paths.resolve_plans_dir()!s}"
    )
    assert server.PLANS_DIR == expected, (
        f"server.PLANS_DIR must point at the per-test plans root; got "
        f"{server.PLANS_DIR!s}"
    )

    # The guard the fixture runs on every test must accept this state.
    _conftest_module()._assert_hitl_persistence_isolated(tmp_path)


# ---------------------------------------------------------------------------
# 2. The decision table starts empty
# ---------------------------------------------------------------------------


def test_decision_table_starts_empty(isolated_plans_dir, tmp_path):
    """The ``plan_decisions`` table holds no rows when a test starts.

    The guarantee is structural: the state database is a *per-test*
    file, so a decision row written by one test cannot be in the file
    the next test opens.  The fixture asserts that the file it points
    at is empty (and refuses to continue if it is not), and the
    assertions below pin the two halves of the guarantee: the database
    is the per-test one, and the table it carries starts with zero
    rows.
    """
    import config_paths
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = config_paths.resolve_state_db_path()
    assert db_path == tmp_path / "state.db", (
        "the state database must be the per-test file; got "
        f"{db_path!s}"
    )
    assert not db_path.exists(), (
        f"{db_path} already exists when the test starts — a database "
        f"that is already there carries the previous test's rows, which "
        f"is how a decision leaks from one test into the next"
    )

    # The fixture's own check: it must accept a database with no
    # decision rows (and refuse one that has them).
    _conftest_module()._assert_decision_table_starts_empty(db_path)

    conn = open_db(db_path)
    try:
        migrate(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM plan_decisions"
        ).fetchone()[0]
        assert count == 0, (
            f"the plan_decisions table must start empty; it holds {count} "
            f"row(s)"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. The repository's plans tree is not written to
# ---------------------------------------------------------------------------


def test_repo_plans_untouched(isolated_plans_dir, tmp_path):
    """A test that exercises the HITL surface leaves the repo alone.

    The test writes all three halves of the HITL persistence surface —
    a plan artifact and the plan's ``hitl_config.json`` under the
    plans root, and a decision row in the state database — and then
    checks two things: the repository's own ``plans/`` tree has the
    same file listing it had before, and the artifacts landed under the
    per-test tmp root.  The second half is what makes the first
    meaningful: the repository tree is untouched *because* the writes
    went to the tmp root, not because the test wrote nothing.
    """
    import config_paths
    from hooks import write_for_plan
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    before = _repo_plans_listing()

    plan_id = "hitl-isolation-probe"
    plan_dir = config_paths.resolve_plans_dir() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        '{"current_phase": "prd_review"}', encoding="utf-8"
    )
    # The HITL hook configuration is a *plan property* that lives in the
    # plan's own directory, so it is exactly the file that would be
    # left behind in the operator's tree if the plans root were not
    # redirected.
    write_for_plan(
        plan_dir,
        {
            "source": "plan_creation",
            "session_id": "s-isolation",
            "destination": "grp-isolation",
        },
    )

    db_path = config_paths.resolve_state_db_path()
    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_decisions ("
            " decision_id, plan_id, source, task_id, question, options,"
            " default_option, mode, timeout_sec, status, answer,"
            " answered_by, answered_at, deadline_at, created_at, updated_at,"
            " heartbeat_at, audit_seq"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "d-hitl-isolation",
                plan_id,
                "execution",
                "T-1",
                "proceed?",
                '["accept", "revise"]',
                "accept",
                "auto",
                1800,
                "pending",
                None,
                None,
                None,
                "2026-10-21T00:00:00+00:00",
                "2026-10-21T00:00:00+00:00",
                "2026-10-21T00:00:00+00:00",
                None,
                0,
            ),
        )
    finally:
        conn.close()

    after = _repo_plans_listing()

    assert after == before, (
        "the repository's plans/ tree changed during a test: "
        f"{sorted(set(after) - set(before))!r} appeared, "
        f"{sorted(set(before) - set(after))!r} disappeared"
    )
    # A targeted check as well, so the assertion above cannot be
    # satisfied by a listing that happens to be unchanged while this
    # test's own plan directory is hiding in it.
    repo_plans = config_paths.PROJECT_ROOT / "plans"
    assert not (repo_plans / plan_id).exists(), (
        f"the test's plan directory landed in the repository's plans/ tree: "
        f"{repo_plans / plan_id}"
    )
    # The writes landed in the per-test tmp root — that is *why* the
    # repository tree is untouched.
    assert plan_dir == tmp_path / "plans" / plan_id, (
        f"the plan artifact must land under the per-test plans root; got "
        f"{plan_dir!s}"
    )
    assert (plan_dir / "plan_state.json").exists()
    assert (plan_dir / "hitl_config.json").exists(), (
        "the HITL hook configuration must be written next to the plan, "
        f"inside the per-test plans root: {plan_dir!s}"
    )
    assert db_path == tmp_path / "state.db"


# ---------------------------------------------------------------------------
# 4. An unregistered persistence knob is refused
# ---------------------------------------------------------------------------


def test_unregistered_persistence_knob_is_rejected(tmp_path):
    """A knob the fixture cannot redirect must be refused, not ignored.

    The HITL package will grow its own persistence (a decision
    configuration file, for one).  The day it reads a ``PDT_*``
    variable, that variable has to be redirectable — otherwise the
    suite writes to the operator's tree and the failure shows up as a
    flake three tests later.  The session-scoped guard in the conftest
    is what turns that into a named failure; these assertions pin that
    the guard both *detects* the knob and *reports* it.
    """
    conftest = _conftest_module()

    # A module that reads a knob the fixture does not know about.
    sample = tmp_path / "hitl_sample.py"
    sample.write_text(
        "import os\n"
        "\n"
        "def config_path():\n"
        "    return os.environ.get('PDT_HITL_CONFIG')\n",
        encoding="utf-8",
    )

    found = conftest._scan_pdt_knobs(tmp_path)
    assert "PDT_HITL_CONFIG" in found, (
        f"the knob scanner must find PDT_HITL_CONFIG; it found {found!r}"
    )

    unregistered = conftest._unregistered_hitl_knobs(found)
    assert unregistered == ["PDT_HITL_CONFIG"], (
        "an unregistered knob must be reported by name; got "
        f"{unregistered!r}"
    )

    # And the knobs the fixture *does* redirect must not be reported.
    registered = {env_var for _name, env_var, _resolve in
                  conftest._HITL_PERSISTENCE_ROOTS}
    assert conftest._unregistered_hitl_knobs(registered) == []


# ---------------------------------------------------------------------------
# 5. The request-guard injection is still installed
# ---------------------------------------------------------------------------


def test_request_guard_injection_still_installed():
    """The suite's request-guard header injection must stay in place.

    The isolation work in this area touches the same conftest that
    installs the guard header on every ``TestClient``.  The guard is
    what keeps the suite's own requests from being answered with 403,
    and it is installed by wrapping ``TestClient.__init__`` — a change
    to the conftest can silently drop it, and the symptom (a wall of
    403s) points at the routes rather than at the fixture.
    """
    from starlette.testclient import TestClient

    assert getattr(TestClient, "_pdt_guard_installed", False) is True, (
        "the request-guard header injection is no longer installed — every "
        "TestClient request would be answered with 403"
    )


# ---------------------------------------------------------------------------
# 6. The guards can actually fail
# ---------------------------------------------------------------------------
#
# A guard that cannot fail is not a guard: if these two checks passed
# for a state they are supposed to refuse, the "explicit failure" the
# isolation contract promises would be a comment.


def test_guard_rejects_a_persistence_root_outside_the_tmp_root(
    tmp_path, monkeypatch,
):
    """A root that escaped the redirect must be refused, by name."""
    conftest = _conftest_module()

    # ``tmp_path.parent`` is the pytest base directory — outside this
    # test's root, but not the operator's tree either, so nothing is
    # written even if the guard were broken.
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path.parent / "not-the-root"))

    with pytest.raises(AssertionError) as excinfo:
        conftest._assert_hitl_persistence_isolated(tmp_path)

    assert "plans root" in str(excinfo.value), (
        f"the failure must name the surface that escaped; got "
        f"{excinfo.value}"
    )


def test_guard_rejects_a_decision_table_that_already_has_rows(tmp_path):
    """A decision row left over from another test must be refused."""
    conftest = _conftest_module()

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE plan_decisions (decision_id TEXT PRIMARY KEY)"
        )
        conn.execute("INSERT INTO plan_decisions VALUES ('d-stranger')")
        # ``close()`` does not commit: without this the insert is rolled
        # back and the guard would have nothing to find.
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssertionError) as excinfo:
        conftest._assert_decision_table_starts_empty(db_path)

    assert "plan_decisions" in str(excinfo.value), (
        f"the failure must name the table that leaked; got "
        f"{excinfo.value}"
    )
