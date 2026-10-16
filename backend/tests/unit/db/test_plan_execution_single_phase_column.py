"""VP-v6 anchor: the database carries exactly ONE workflow-state column.

Background
----------
Schema v5 collapsed ``plan_routing``'s internal ``stage`` / ``current_phase``
split and left the *other* half of the same idea standing:
``plan_execution.current_phase``, documented as a "display-only mirror" of
the routing phase.

It never worked as one, and the shape of the failure is the part that
matters.  All five of its writers passed a **routing** vocabulary word --
``executing`` (:mod:`routes.execution` start + spawn-failure),
``verification`` (the ``/reset`` mirror), ``ready`` (the
verification-stopped rollback) -- and **none of them ever wrote a terminal
phase**.  A column that cannot represent "this plan finished" cannot be a
sub-state of anything; it is a lag indicator.  Measured on the real
``state.db`` before this migration:

  * 18 routing rows, 5 execution rows, 13 plans with no execution row
  * 4 of those 5 disagreed -- all of them stuck on ``executing`` while
    the routing column had already advanced to ``completed`` / ``failed``
  * the mirror held exactly ONE distinct value across all rows

so the single reader (``GET /api/plans``, which read
``exec_row.get("current_phase") or status``) reported finished plans as
executing.  ``execution_repository.update_status`` made it worse, writing
a hard-coded ``"executing"`` on every status change until 2026-09-19.

v6 drops the column.  ``plan_routing.current_phase`` is the only
workflow-state value in the database.

Contract pinned here
--------------------
1. A fresh database has no ``plan_execution.current_phase``.
2. The declared DDL itself has no such column -- so it cannot quietly
   reappear, which is how ``plan_routing.stage`` came back once before.
3. Collapsing a v5 database preserves every other column byte-for-byte and
   DISCARDS the mirror rather than merging it forward.  The discard is the
   load-bearing choice and is asserted as such: a backfill would copy the
   lag into the authority.
4. No row can be stranded without a phase: every row that had an
   execution row still has a routing row.
5. Re-running ``migrate`` after v6 is a no-op.
6. A database that still has the column but no ``schema_version`` row is
   still collapsed (the guard is column presence, not the logbook).
7. A rebuild that fails part-way leaves no half-built table behind.

NOTE: the vocabulary in the v5-database fixtures below is deliberately the
state a real pre-v6 database contained -- including the stale
``executing`` values on plans the routing column had already finished.  A
repo-wide search-and-replace that "updates" those fixtures to agree with
routing destroys the thing being tested.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from state_machine.db.schema import (
    CURRENT_SCHEMA_VERSION,
    _DDL_PLAN_EXECUTION,
    _v6_drop_execution_current_phase,
    migrate,
)

#: The pre-v6 column list, hand-written rather than imported -- the whole
#: point of the migration is that the old shape no longer exists in the
#: source tree, so the fixture has to spell it out.
_V5_EXECUTION_DDL = """
CREATE TABLE plan_execution (
  plan_id TEXT PRIMARY KEY,
  current_phase TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  project_dir TEXT,
  stop_reason TEXT,
  task_progress TEXT,
  next_run_at TEXT,
  card_state TEXT,
  flags TEXT,
  exec_pid INTEGER,
  exec_status TEXT,
  started_at TEXT,
  updated_at TEXT NOT NULL
)
"""

#: Post-v5 ``plan_routing``: already collapsed, so the fixture isolates v6.
_V5_ROUTING_DDL = """
CREATE TABLE plan_routing (
  plan_id TEXT PRIMARY KEY,
  current_phase TEXT NOT NULL DEFAULT 'interview',
  substage TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  completed_phases TEXT,
  review_rounds TEXT,
  flags TEXT,
  verification TEXT,
  last_updated TEXT,
  updated_at TEXT NOT NULL
)
"""

#: The plan the API test drives end to end.  Named up here because it is
#: a fixture row too -- the end-to-end test needs BOTH halves of the
#: divergence (a routing row AND an execution row) or the endpoint is
#: reading a directory-derived row with no phase at all and the test
#: proves nothing.
#:
#: The date prefix is load-bearing and must stay after 2026-08-05:
#: ``classify_plan`` parses a ``YYYYMMDD-*`` dirname and pins anything at
#: or before the archive cutoff as ``archived``; ``list_all`` then lets
#: the archive row *win* over the SQLite row, and an archived entry
#: deliberately carries no ``current_phase`` at all.  A pre-cutoff id here
#: would silently turn this into a test of the archive path.
_DIVERGENT_PLAN = "20261016-phase-split-plan"

#: ``(plan_id, routing_phase, stale_mirror, exec_status)``.
#:
#: ``p-done`` and ``p-failed`` are the bug verbatim: the mirror is stuck on
#: ``executing`` for plans the authority has already finished.  If the
#: migration merged the mirror forward, these two would land as
#: ``executing`` and the discard test below would catch it.
_V5_ROWS: tuple[tuple[str, str, str | None, str], ...] = (
    ("p-running", "executing", "executing", "running"),
    ("p-ready", "ready", None, "stopped"),
    ("p-done", "completed", "executing", "completed"),
    ("p-failed", "failed", "executing", "failed"),
    ("p-verify", "verification_passed", None, "completed"),
    # The API-proof plan: same divergence, but it also has a directory
    # tree on disk, so /api/plans can be driven against it for real.
    (_DIVERGENT_PLAN, "completed", "executing", "completed"),
)

#: Every non-phase column, one distinct value so a rebuild that mixed rows
#: up would be visible rather than plausible-looking.
_CARRIED: dict[str, object] = {
    "attempt_count": 3,
    "project_dir": "/abs/project",
    "stop_reason": "user_requested",
    "task_progress": '{"tasks": {"1-1": {"status": "completed"}}}',
    "next_run_at": "2099-01-01T00:00:00Z",
    "card_state": '{"tab": "tasks"}',
    "flags": '{"arch_enabled": true}',
    "exec_pid": 4242,
    "exec_status": "running",
    "started_at": "2026-10-16T00:00:00Z",
    "updated_at": "2026-10-16T00:00:00Z",
}

_CARRIED_SELECT = (
    "plan_id, attempt_count, project_dir, stop_reason, task_progress,"
    " next_run_at, card_state, flags, exec_pid, exec_status, started_at,"
    " updated_at"
)


def _build_v5_db(path: Path, *, with_version_row: bool = True) -> sqlite3.Connection:
    """Create a pre-v6 ``state.db`` carrying :data:`_V5_ROWS`."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(_V5_ROUTING_DDL)
    conn.execute(_V5_EXECUTION_DDL)
    if with_version_row:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version (version) VALUES (5)")

    for plan_id, routing_phase, mirror, _status in _V5_ROWS:
        conn.execute(
            "INSERT INTO plan_routing ("
            " plan_id, current_phase, substage, version, completed_phases,"
            " review_rounds, flags, verification, last_updated, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, routing_phase, f"sub-{plan_id}", 7,
                '["interview"]', '{"prd": 2}', '{"arch_enabled": true}',
                '{"status": "pending"}', "2026-10-16T00:00:00Z",
                "2026-10-16T00:00:00Z",
            ),
        )
        if mirror is None:
            # Mirrors the real database: most plans never ran, so they
            # have a routing row and no execution row at all.
            continue
        conn.execute(
            "INSERT INTO plan_execution ("
            " plan_id, current_phase, attempt_count, project_dir,"
            " stop_reason, task_progress, next_run_at, card_state, flags,"
            " exec_pid, exec_status, started_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, mirror, _CARRIED["attempt_count"],
                f"/abs/{plan_id}", _CARRIED["stop_reason"],
                _CARRIED["task_progress"], _CARRIED["next_run_at"],
                _CARRIED["card_state"], _CARRIED["flags"],
                _CARRIED["exec_pid"], _CARRIED["exec_status"],
                _CARRIED["started_at"], _CARRIED["updated_at"],
            ),
        )
    return conn


def _exec_columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(plan_execution)")]


def _ddl_column_names(ddl: str) -> list[str]:
    """Declared column names from a ``CREATE TABLE`` string.

    Parsed rather than substring-matched on purpose: the v6 DDL
    legitimately *mentions* ``current_phase`` in its explanatory comment
    block, so ``"current_phase" in ddl`` would always be true and the
    test would pass for the wrong reason.
    """
    body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
    names: list[str] = []
    for raw in body.splitlines():
        line = raw.split("--", 1)[0].strip().rstrip(",")
        if not line:
            continue
        names.append(line.split()[0])
    return names


# ---------------------------------------------------------------------------
# 1 + 2 -- the declared shape
# ---------------------------------------------------------------------------


def test_fresh_database_has_no_execution_phase_column(tmp_path: Path) -> None:
    """A brand-new ``state.db`` is born without the mirror."""
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    migrate(conn)
    try:
        cols = _exec_columns(conn)
        assert "current_phase" not in cols, (
            "a fresh install must not create plan_execution.current_phase; "
            "the whole point of v6 is that the column stops existing"
        )
        # The authority is untouched and still there.
        routing = [r[1] for r in conn.execute("PRAGMA table_info(plan_routing)")]
        assert "current_phase" in routing
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_declared_ddl_has_no_execution_phase_column() -> None:
    """The DDL constant must not declare the column.

    This is the ``plan_routing.stage`` lesson applied forward: a column
    that quietly reappears in the DDL comes back with it on the next fresh
    install, and the migration's guard is column *presence* -- so a
    re-declared column would be collapsed again and again, forever.
    """
    names = _ddl_column_names(_DDL_PLAN_EXECUTION)
    assert "current_phase" not in names, (
        "the declared DDL still creates plan_execution.current_phase; the "
        "column can reappear this way exactly as plan_routing.stage did"
    )
    # Sanity: the parser works, and the surviving columns are still there.
    assert "exec_pid" in names and "project_dir" in names


# ---------------------------------------------------------------------------
# 3 -- the migration preserves data and discards the lag
# ---------------------------------------------------------------------------


def test_migration_preserves_every_other_column_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The rebuild is data-preserving for everything except the mirror."""
    conn = _build_v5_db(tmp_path / "state.db")
    try:
        before = {
            row[0]: row
            for row in conn.execute(f"SELECT {_CARRIED_SELECT} FROM plan_execution")
        }
        assert before, "fixture must actually contain execution rows"

        migrate(conn)

        assert "current_phase" not in _exec_columns(conn)
        after = {
            row[0]: row
            for row in conn.execute(f"SELECT {_CARRIED_SELECT} FROM plan_execution")
        }
        assert set(before) == set(after), "the rebuild lost or added rows"
        for plan_id, row in before.items():
            assert after[plan_id] == row, (
                f"non-phase columns changed for {plan_id}: "
                f"{before[plan_id]!r} -> {after[plan_id]!r}"
            )
    finally:
        conn.close()


def test_migration_discards_the_stale_mirror_rather_than_merging_it(
    tmp_path: Path,
) -> None:
    """The routing column must come out of the migration UNCHANGED.

    This is the assertion that fails if someone "helpfully" adds a
    backfill.  A backfill means ``UPDATE plan_routing SET current_phase =
    (SELECT current_phase FROM plan_execution ...)`` -- the reverse merge,
    where the lag indicator overwrites the authority and turns
    ``completed`` back into ``executing``.  Two of the fixture rows exist
    to catch exactly that.
    """
    conn = _build_v5_db(tmp_path / "state.db")
    try:
        before = dict(
            conn.execute("SELECT plan_id, current_phase FROM plan_routing")
        )
        # Prove the fixture really does reproduce the split it claims to.
        stale = conn.execute(
            "SELECT r.plan_id FROM plan_execution e"
            " JOIN plan_routing r ON r.plan_id = e.plan_id"
            " WHERE e.current_phase <> r.current_phase"
        ).fetchall()
        assert {r[0] for r in stale} == {"p-done", "p-failed", _DIVERGENT_PLAN}, (
            f"fixture no longer reproduces the split-brain; got {stale!r}"
        )

        migrate(conn)

        after = dict(
            conn.execute("SELECT plan_id, current_phase FROM plan_routing")
        )
        assert after == before, (
            "the migration altered the authority column; the mirror must be "
            f"discarded, not merged forward. before={before!r} after={after!r}"
        )
        # Specifically: the finished plans are still finished.
        assert after["p-done"] == "completed"
        assert after["p-failed"] == "failed"
    finally:
        conn.close()


def test_no_plan_is_stranded_without_a_phase(tmp_path: Path) -> None:
    """Every row that had an execution row still has a routing row.

    This is what makes discarding safe: the drop cannot leave a plan with
    no workflow state at all.  On the real database this held for every
    row (0 orphans) before the migration.
    """
    conn = _build_v5_db(tmp_path / "state.db")
    try:
        migrate(conn)
        orphans = conn.execute(
            "SELECT e.plan_id FROM plan_execution e"
            " LEFT JOIN plan_routing r ON r.plan_id = e.plan_id"
            " WHERE r.plan_id IS NULL"
        ).fetchall()
        assert orphans == [], f"rows stranded with no phase: {orphans!r}"
    finally:
        conn.close()


def test_the_split_can_no_longer_be_expressed(tmp_path: Path) -> None:
    """After the migration there is no SQL that can disagree with itself.

    The v5 failure mode was a JOIN selecting both phase columns off the
    same row.  That query no longer parses -- which is the strongest
    available statement that the split is gone, and it is what
    ``_consistency_invariants`` I4 anti-pattern 2 used to run.
    """
    conn = _build_v5_db(tmp_path / "state.db")
    try:
        migrate(conn)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "SELECT e.current_phase, r.current_phase"
                " FROM plan_execution e JOIN plan_routing r"
                " ON r.plan_id = e.plan_id"
            ).fetchall()
        # The one-column version of the same question answers cleanly, and
        # the answer is the authority's: p-done/p-failed are finished, not
        # the ``executing`` the mirror used to report for them.
        rows = conn.execute(
            "SELECT r.plan_id, r.current_phase FROM plan_execution e"
            " JOIN plan_routing r ON r.plan_id = e.plan_id"
            " ORDER BY r.plan_id"
        ).fetchall()
        # For every plan that HAD a mirror row, the join now reports the
        # authority's phase. Derived from the fixture so adding a row
        # cannot quietly make this assertion stale.
        expected = sorted(
            (plan_id, routing_phase)
            for plan_id, routing_phase, mirror, _status in _V5_ROWS
            if mirror is not None
        )
        assert len(expected) == 4, f"fixture lost a mirror row: {expected!r}"
        assert sorted(rows) == expected
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5 + 6 -- idempotence and the shape of the guard
# ---------------------------------------------------------------------------


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Running ``migrate`` again changes nothing."""
    conn = _build_v5_db(tmp_path / "state.db")
    try:
        migrate(conn)
        snapshot = conn.execute(
            f"SELECT {_CARRIED_SELECT} FROM plan_execution ORDER BY plan_id"
        ).fetchall()
        routing = conn.execute(
            "SELECT plan_id, current_phase FROM plan_routing ORDER BY plan_id"
        ).fetchall()

        migrate(conn)
        migrate(conn)

        assert conn.execute(
            f"SELECT {_CARRIED_SELECT} FROM plan_execution ORDER BY plan_id"
        ).fetchall() == snapshot
        assert conn.execute(
            "SELECT plan_id, current_phase FROM plan_routing ORDER BY plan_id"
        ).fetchall() == routing
        assert "current_phase" not in _exec_columns(conn)
    finally:
        conn.close()


def test_column_without_schema_version_row_is_still_collapsed(
    tmp_path: Path,
) -> None:
    """The guard is column presence, not the logbook.

    Same reasoning as v5: an install that crashed between the rebuild and
    the bookkeeping carries the old column with no ``schema_version`` row
    at all, and must still be repaired.
    """
    conn = _build_v5_db(tmp_path / "state.db", with_version_row=False)
    try:
        assert "current_phase" in _exec_columns(conn)
        migrate(conn)
        assert "current_phase" not in _exec_columns(conn)
        assert conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_survives_a_failed_rebuild(tmp_path: Path) -> None:
    """A failure part-way through leaves no half-built table behind.

    The rebuild is a four-step DDL dance
    (``CREATE ... v6`` / ``INSERT SELECT`` / ``DROP old`` / ``RENAME``).
    If step 2 fails, the ``except BaseException`` handler must roll the
    whole thing back -- including the temp table -- rather than leave
    ``plan_execution_v6`` sitting next to a stale ``plan_execution``.
    """
    conn = _build_v5_db(tmp_path / "state.db")
    try:
        # Make the INSERT..SELECT fail: it names ``attempt_count``, which a
        # corrupted table will not have.  Done OUTSIDE the migration's own
        # transaction, so only the migration's work is under test.
        conn.execute("ALTER TABLE plan_execution RENAME TO plan_execution_saved")
        conn.execute(
            "CREATE TABLE plan_execution ("
            " plan_id TEXT PRIMARY KEY, current_phase TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO plan_execution VALUES ('p1', 'executing', 'z')"
        )

        with pytest.raises(sqlite3.OperationalError):
            _v6_drop_execution_current_phase(conn)

        # Rolled back: the temp table is gone and the pre-migration table
        # is byte-identical to what the caller handed in.
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE type='table' AND name='plan_execution_v6'"
        ).fetchone()[0] == 0, "the half-built temp table survived the rollback"
        assert "current_phase" in _exec_columns(conn)
        assert conn.execute(
            "SELECT plan_id, current_phase, updated_at FROM plan_execution"
        ).fetchall() == [("p1", "executing", "z")]
        # And the rows the caller had are all still recoverable.
        assert conn.execute(
            "SELECT count(*) FROM plan_execution_saved"
        ).fetchone()[0] == len([r for r in _V5_ROWS if r[2] is not None])
    finally:
        conn.close()


def test_caller_owned_transaction_is_not_committed_underneath(
    tmp_path: Path,
) -> None:
    """Under a caller-owned transaction the migration does not COMMIT.

    ``owns_txn`` is ``isolation_level is None``; a caller that opened its
    own ``BEGIN`` keeps control of the commit boundary, so the migration's
    write stays inside the caller's transaction.  Same contract as v5.
    """
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level="")
    try:
        conn.execute(_V5_ROUTING_DDL)
        conn.execute(_V5_EXECUTION_DDL)
        conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at)"
            " VALUES ('p1', 'completed', 'z')"
        )
        conn.execute(
            "INSERT INTO plan_execution (plan_id, current_phase, updated_at)"
            " VALUES ('p1', 'executing', 'z')"
        )
        conn.commit()

        conn.execute("BEGIN")
        _v6_drop_execution_current_phase(conn)
        # No COMMIT was issued underneath the caller: the txn is still ours.
        assert conn.in_transaction
        conn.rollback()

        # Rolled back with the caller's transaction -- the mirror is back.
        assert "current_phase" in _exec_columns(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The end-to-end proof: GET /api/plans serves the authority
# ---------------------------------------------------------------------------
#
# The two tests above prove the database has one column. This one proves
# the *only* consumer of that column changed with it, and it is the test
# that would go red if someone reverted ``routes/plans.py`` while leaving
# the schema collapsed -- the negative control for the whole change.
#
# The fixture is deliberately a pre-v6 database whose plan_routing row
# says ``completed`` while its plan_execution mirror says ``executing``.
# That is the real shape of the bug: 4 of the 5 execution rows on the live
# database were exactly this. After migrate(), the mirror is gone, so the
# endpoint can only answer from the authority.

def _seed_divergent_plans_dir(plans_dir: Path) -> None:
    """On-disk plan tree for the divergent plan, as the boot sequence left it.

    ``plan_state.json`` is a third copy of the phase and is pinned to the
    AUTHORITY value too -- if the endpoint regressed to reading it, the
    response would still be right and the test would be green for the
    wrong reason, so the two on-disk/DB copies must be the *same* value
    and the assertion below must additionally reject the mirror value.
    """
    import json as _json

    plan_dir = plans_dir / _DIVERGENT_PLAN
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(_json.dumps({"tasks": []}), encoding="utf-8")
    (plan_dir / "plan_state.json").write_text(
        _json.dumps(
            {
                "plan_id": _DIVERGENT_PLAN,
                "current_phase": "completed",
                "completed_phases": ["interview", "prd", "review", "arch", "test", "tasks"],
                "review_rounds": {"prd": 2, "arch": 1, "test": 1},
                "flags": {"arch_enabled": True, "test_enabled": True},
            }
        ),
        encoding="utf-8",
    )


def test_api_plans_current_phase_comes_from_the_authority(
    tmp_path: Path, capsys
) -> None:
    """GET /api/plans answers with plan_routing's value, not the mirror's.

    Load-bearing in both directions:

      * the plan is seeded as a *pre-v6* database, so the migration really
        runs and really discards the ``executing`` mirror;
      * the request is made through the real ASGI app, so the assertion
        covers the route, the cache, the terminal filter and the JSON
        shape -- not a hand-called internal.

    A response of ``"executing"`` is the v5 failure and is rejected
    explicitly, not merely by inequality with the expected value.
    """
    import json as _json

    from fastapi.testclient import TestClient

    import server
    from state_machine.db.schema import migrate as _migrate

    # --- seed the pre-v6 database -------------------------------------
    db_path = tmp_path / "state.db"
    seed = _build_v5_db(db_path)
    seed.close()
    _seed_divergent_plans_dir(tmp_path / "plans")
    server.PLANS_DIR.mkdir(parents=True, exist_ok=True)
    # ``isolated_plans_dir`` already pointed server.PLANS_DIR and both env
    # knobs at tmp_path; re-assert them so this test does not depend on
    # fixture ordering if it is ever moved out of tests/unit/db/.
    server.PLANS_DIR = tmp_path / "plans"
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    os.environ["PDT_PLANS_DIR"] = str(tmp_path / "plans")
    server._PLANS_CACHE.clear()

    # --- prove the pre-state is the bug, from the live API ------------
    # (a v5 database read by a v5-shaped reader; the point is that the
    # mirror and the authority are genuinely different values here)
    pre = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    pre_mirror = pre.execute(
        "SELECT current_phase FROM plan_execution WHERE plan_id=?",
        (_DIVERGENT_PLAN,),
    ).fetchone()
    pre_authority = pre.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id=?",
        (_DIVERGENT_PLAN,),
    ).fetchone()
    pre.close()
    assert pre_mirror == ("executing",), pre_mirror
    assert pre_authority == ("completed",), pre_authority

    # --- the boot sequence the server performs ------------------------
    boot = sqlite3.connect(db_path, isolation_level=None)
    _migrate(boot)
    boot.close()

    # --- the request, end to end --------------------------------------
    client = TestClient(server.app)
    url = "/api/plans?include_terminal=true"
    resp = client.get(url)
    capsys.readouterr()

    body = resp.json()
    entry = next((p for p in body if p.get("id") == _DIVERGENT_PLAN), None)
    raw = _json.dumps(
        {
            "id": entry.get("id") if entry else None,
            "status": entry.get("status") if entry else None,
            "current_phase": entry.get("current_phase") if entry else None,
        }
    )

    assert entry is not None, (
        f"{_DIVERGENT_PLAN} missing from the listing: {body!r}"
    )
    assert entry["current_phase"] == "completed", (
        "GET /api/plans must serve plan_routing.current_phase. got "
        f"{entry['current_phase']!r} (the pre-v6 mirror said 'executing' "
        "and that value is what this endpoint used to return for a "
        f"finished plan). Raw entry: {raw}"
    )
    assert entry["current_phase"] != pre_mirror[0], (
        "the discarded mirror value is still being served"
    )


def test_api_plans_default_listing_hides_the_finished_plan(
    tmp_path: Path,
) -> None:
    """The divergence had a second, user-visible consequence: ghost plans.

    The terminal filter runs on the same derived ``current_phase``. While
    the mirror said ``executing``, a finished plan stayed in the default
    sidebar forever. With the authority restored it is filtered out
    again -- which is why this change alters the *default* listing, not
    just the field value.
    """
    from fastapi.testclient import TestClient

    import server
    from state_machine.db.schema import migrate as _migrate

    db_path = tmp_path / "state.db"
    seed = _build_v5_db(db_path)
    seed.close()
    _seed_divergent_plans_dir(tmp_path / "plans")
    server.PLANS_DIR = tmp_path / "plans"
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    os.environ["PDT_PLANS_DIR"] = str(tmp_path / "plans")
    server._PLANS_CACHE.clear()

    boot = sqlite3.connect(db_path, isolation_level=None)
    _migrate(boot)
    boot.close()

    client = TestClient(server.app)
    default_body = client.get("/api/plans").json()
    full_body = client.get("/api/plans?include_terminal=true").json()

    default_ids = {p.get("id") for p in default_body}
    full_ids = {p.get("id") for p in full_body}

    assert _DIVERGENT_PLAN in full_ids, (
        f"the finished plan must still be reachable with "
        f"include_terminal=true; got {sorted(full_ids)!r}"
    )
    assert _DIVERGENT_PLAN not in default_ids, (
        "a completed plan is being served in the default sidebar listing; "
        "its current_phase must be the authority's, not the mirror's"
    )
    # The plans that are genuinely in flight stay visible.
    assert "p-running" in default_ids
