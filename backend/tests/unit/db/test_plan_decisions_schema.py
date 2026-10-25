"""HITL decision storage: the ``plan_decisions`` table and the model.

Background
----------
A human-in-the-loop (HITL) decision has to be a *fact*, not a piece of
process state: the requester (an executor thread, a verification thread,
a route handler) and the answerer (a human on another thread, possibly
another process) are two different actors that never share memory.  The
only place both of them can meet is the database the state-machine
already owns, so the decision request, its options, its deadline and its
answer all live in one SQLite table.

This module pins the two halves of that contract:

  1. **Storage** — ``state_machine.db.schema.migrate`` creates a
     ``plan_decisions`` table with a fixed 22-column shape plus the
     ``idx_plan_decisions_plan_status`` index (the lookup every reader
     does: "this plan's open decisions, by status"), and does it
     idempotently so a second ``migrate`` is a no-op rather than an
     ``OperationalError``.  A database that already carries the table
     in its older shape is upgraded in place: the four context columns
     (``vp_id`` / ``failure_class`` / ``round`` / ``answer_note``) are
     added to it, so an operator's existing ``state.db`` is not left
     behind by a newer backend.

  2. **Model** — :mod:`hitl.models` exposes the value objects that
     travel over that table: :class:`DecisionRequest` (what the caller
     asks), :class:`Decision` (what is stored), and the two closed
     vocabularies :class:`DecisionStatus` and :class:`DecisionMode`.
     An illegal ``mode`` / ``status`` is a programmer error and is
     rejected at construction time with ``ValueError`` — not written to
     the database and discovered later by a reader that does not know
     the word.

The column set is spelled out here rather than imported from the schema
module: the point of the gate is that the *shape* is a contract, so a
test that reads the shape from the code under test proves nothing.

Test command::

    pytest backend/tests/unit/db/test_plan_decisions_schema.py -v
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


#: The 22 columns ``plan_decisions`` must carry.  Spelled out by hand
#: (not derived from the DDL) so that dropping, renaming or reordering a
#: column is a test failure rather than a silent shape change.
#:
#: The four context columns — ``vp_id``, ``failure_class``, ``round``
#: and ``answer_note`` — are the ones the architecture adds to the
#: 18 the table shipped with: what the question is about (the
#: verification point, the failure class, the round), and the note
#: that travels with the decision.
EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "decision_id",
        "plan_id",
        "source",
        "task_id",
        "question",
        "options",
        "default_option",
        "mode",
        "timeout_sec",
        "status",
        "answer",
        "answered_by",
        "answered_at",
        "deadline_at",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "audit_seq",
        "vp_id",
        "failure_class",
        "round",
        "answer_note",
    }
)


#: The columns a ``plan_decisions`` table carried *before* the four
#: context columns were added.  Spelled out by hand — like
#: :data:`EXPECTED_COLUMNS`, and for the same reason: the upgrade path
#: has to be tested against a shape the test states, not one it reads
#: out of the code under test.
PRE_CONTEXT_COLUMNS_DDL = """
CREATE TABLE plan_decisions (
  decision_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  source TEXT NOT NULL,
  task_id TEXT,
  question TEXT NOT NULL,
  options TEXT NOT NULL,
  default_option TEXT,
  mode TEXT NOT NULL DEFAULT 'auto',
  timeout_sec INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  answer TEXT,
  answered_by TEXT,
  answered_at TEXT,
  deadline_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  heartbeat_at TEXT,
  audit_seq INTEGER NOT NULL DEFAULT 0
)
"""


def _hitl_models():
    """Return the :mod:`hitl.models` module, failing the test if absent.

    The import is done inside the test (rather than at module scope) so
    that a missing package fails *this* test with a readable message
    instead of turning the whole module into a collection error and
    hiding the schema failures behind it.
    """
    try:
        import hitl.models
    except ImportError as exc:  # pragma: no cover - only on the red run
        pytest.fail(f"hitl.models must exist and be importable: {exc}")
    return hitl.models


def _table_names(conn: sqlite3.Connection) -> list[str]:
    """Return the user table names on ``conn`` (sorted)."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    return [row[0] for row in cur.fetchall()]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of ``table`` in declaration order."""
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------------------
# Storage contract
# ---------------------------------------------------------------------------


def test_table_columns_and_index(tmp_path: Path) -> None:
    """A migrated database carries ``plan_decisions`` with its 22 columns.

    Steps:

      1. Create a fresh ``state.db`` through the production connection
         factory and run the canonical ``migrate``.
      2. Read the column set back with ``PRAGMA table_info``.
      3. Assert it equals :data:`EXPECTED_COLUMNS` exactly — no missing
         column, no extra one.
      4. Assert ``idx_plan_decisions_plan_status`` exists and is built
         over ``(plan_id, status)`` — the only lookup shape a reader
         needs ("this plan's decisions, grouped by state").
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)

        columns = _columns(conn, "plan_decisions")
        assert set(columns) == EXPECTED_COLUMNS, (
            f"plan_decisions must carry exactly {sorted(EXPECTED_COLUMNS)!r}; "
            f"got {sorted(columns)!r}"
        )
        assert len(columns) == 22, (
            f"plan_decisions must have 22 columns, got {len(columns)}"
        )

        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(plan_decisions)")
        }
        assert "idx_plan_decisions_plan_status" in indexes, (
            f"plan_decisions must carry the idx_plan_decisions_plan_status "
            f"index; got {sorted(indexes)!r}"
        )

        index_columns = [
            row[2]
            for row in conn.execute(
                "PRAGMA index_info(idx_plan_decisions_plan_status)"
            )
        ]
        assert index_columns == ["plan_id", "status"], (
            "idx_plan_decisions_plan_status must be built over "
            f"(plan_id, status); got {index_columns!r}"
        )
    finally:
        conn.close()


def test_migrate_idempotent(tmp_path: Path) -> None:
    """``migrate`` is idempotent, and the new table participates in it.

    Calling ``migrate`` twice must not raise (``CREATE TABLE`` without
    ``IF NOT EXISTS`` would raise ``OperationalError`` on the second
    call) and must not change the schema.  The assertion deliberately
    covers ``plan_decisions`` too: an idempotency check that ignores the
    table this change added would pass even if the new DDL were not
    idempotent.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        first = _table_names(conn)
        first_columns = _columns(conn, "plan_decisions")

        # Second call must NOT raise.
        migrate(conn)

        second = _table_names(conn)
        second_columns = _columns(conn, "plan_decisions")

        assert first == second, (
            f"a second migrate changed the table set: {first!r} -> {second!r}"
        )
        assert first_columns == second_columns, (
            f"a second migrate changed the plan_decisions columns: "
            f"{first_columns!r} -> {second_columns!r}"
        )
        assert "plan_decisions" in second, (
            "plan_decisions must exist after migrate; got "
            f"{second!r}"
        )
    finally:
        conn.close()


def test_migrate_idempotent_on_existing_database(tmp_path: Path) -> None:
    """A database that already carries the table survives a re-migrate.

    This is the upgrade path: a ``state.db`` written before this table
    existed is opened by a newer backend, which runs ``migrate`` against
    it.  The DDL must apply there too, and applying it twice must
    still be a no-op.
    """
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute("DELETE FROM plan_decisions")
        conn.execute("DROP INDEX idx_plan_decisions_plan_status")
        conn.execute("DROP TABLE plan_decisions")
        assert "plan_decisions" not in _table_names(conn)

        # The upgrade path: migrate against the pre-existing database.
        migrate(conn)
        assert "plan_decisions" in _table_names(conn)
        assert set(_columns(conn, "plan_decisions")) == EXPECTED_COLUMNS

        # And once more, on a database that now has the table.
        migrate(conn)
        assert "plan_decisions" in _table_names(conn)
    finally:
        conn.close()


def test_upgrade_adds_the_context_columns_to_an_existing_table(tmp_path: Path) -> None:
    """A table written before the context columns gains them on ``migrate``.

    This is the upgrade path an operator's own ``state.db`` takes: the
    backend that adds ``vp_id`` / ``failure_class`` / ``round`` /
    ``answer_note`` finds a table that already holds the 18 older
    columns, and the columns have to be *added* — a table that kept
    its old shape would reject every write that carries the new
    fields, and the decisions taken since would have nowhere to live.

    The pre-existing row must survive: an upgrade that loses the
    questions somebody is still waiting for is worse than no upgrade.
    """
    conn = open_db(tmp_path / "state.db")
    try:
        conn.execute(PRE_CONTEXT_COLUMNS_DDL)
        conn.execute(
            "INSERT INTO plan_decisions ("
            " decision_id, plan_id, source, question, options, mode,"
            " status, created_at, updated_at, audit_seq"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "d-old",
                "p1",
                "execution",
                "q",
                '["accept", "revise"]',
                "auto",
                "pending",
                "2026-10-21T00:00:00+00:00",
                "2026-10-21T00:00:00+00:00",
                0,
            ),
        )
        assert "vp_id" not in _columns(conn, "plan_decisions")

        migrate(conn)

        columns = _columns(conn, "plan_decisions")
        assert set(columns) == EXPECTED_COLUMNS, (
            "migrate must bring an existing plan_decisions table up to the "
            f"current shape; got {sorted(columns)!r}"
        )
        for name in ("vp_id", "failure_class", "round", "answer_note"):
            assert name in columns, (
                f"migrate must add {name} to an existing plan_decisions table"
            )

        row = conn.execute(
            "SELECT decision_id, question, vp_id, failure_class, round, "
            "answer_note FROM plan_decisions WHERE decision_id = ?",
            ("d-old",),
        ).fetchone()
        assert row is not None, "the upgrade must not lose the existing row"
        assert row[0] == "d-old"
        assert row[1] == "q"
        # A column that did not exist when the row was written reads
        # back as "no value", not as a guess.
        assert row[2] is None
        assert row[3] is None
        assert row[4] is None
        assert row[5] is None
    finally:
        conn.close()


def test_decision_row_round_trips_through_the_table(tmp_path: Path) -> None:
    """A pending decision can be written and read back verbatim.

    The table is the meeting point between the requester and the
    answerer, so a row that cannot survive the round trip is not a
    storage contract — it is a decoration.
    """
    conn = open_db(tmp_path / "state.db")
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
                "d-1",
                "p1",
                "execution",
                "T-3",
                "q",
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
        row = conn.execute(
            "SELECT status, mode, options FROM plan_decisions "
            "WHERE decision_id = ?",
            ("d-1",),
        ).fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == "auto"
        assert row[2] == '["accept", "revise"]'
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------


def test_invalid_mode_rejected() -> None:
    """A ``mode`` outside the closed vocabulary is a ``ValueError``.

    The vocabulary is two words wide (``auto`` / ``strict``).  A third
    one reaching the table would leave a reader guessing what to do
    with it, so the check happens where the mistake is made — at
    construction — not where it is finally noticed.
    """
    models = _hitl_models()

    with pytest.raises(ValueError):
        models.DecisionRequest(
            plan_id="p1",
            source="execution",
            task_id="T-3",
            question="q",
            options=["accept", "revise"],
            default_option="accept",
            mode="not-a-mode",
            timeout_sec=1800,
        )


def test_valid_modes_accepted() -> None:
    """Both words of the ``mode`` vocabulary are accepted."""
    models = _hitl_models()

    for mode in ("auto", "strict"):
        request = models.DecisionRequest(
            plan_id="p1",
            source="execution",
            task_id="T-3",
            question="q",
            options=["accept", "revise"],
            default_option="accept",
            mode=mode,
            timeout_sec=1800,
        )
        assert request.mode == mode


def test_status_vocabulary_is_closed() -> None:
    """The status vocabulary is exactly the five documented words."""
    models = _hitl_models()

    assert {s.value for s in models.DecisionStatus} == {
        "pending",
        "answered",
        "defaulted",
        "paused",
        "unroutable",
    }
    assert {m.value for m in models.DecisionMode} == {"auto", "strict"}


def test_invalid_status_rejected() -> None:
    """A ``status`` outside the closed vocabulary is a ``ValueError``."""
    models = _hitl_models()

    with pytest.raises(ValueError):
        models.Decision(
            decision_id="d-1",
            plan_id="p1",
            source="execution",
            question="q",
            options=["accept"],
            status="not-a-status",
        )


def test_decision_defaults_to_pending() -> None:
    """A freshly built decision is ``pending``.

    ``pending`` is the state a decision is in from the moment it is
    asked until somebody answers it (or it times out into
    ``defaulted``); nothing else is a legal starting point.
    """
    models = _hitl_models()

    request = models.DecisionRequest(
        plan_id="p1",
        source="execution",
        task_id="T-3",
        question="q",
        options=["accept", "revise"],
        default_option="accept",
        mode="auto",
        timeout_sec=1800,
    )
    decision = models.Decision.from_request(request)

    assert decision.status == "pending"
    assert decision.plan_id == "p1"
    assert decision.source == "execution"
    assert decision.task_id == "T-3"
    assert decision.options == ["accept", "revise"]
    assert decision.default_option == "accept"
    assert decision.mode == "auto"
    assert decision.timeout_sec == 1800
    assert decision.decision_id
    assert decision.created_at and decision.updated_at
    assert decision.deadline_at


def test_the_context_fields_survive_the_model_round_trip() -> None:
    """``vp_id`` / ``failure_class`` / ``round`` / ``answer_note`` travel.

    The four context columns are only worth having if the value that
    goes in comes back out: the question is asked with them, the row
    carries them, and a reader of that row gets them again.  A field
    that is dropped on the way to the row is a fact the audit trail
    cannot answer.
    """
    models = _hitl_models()

    request = models.DecisionRequest(
        plan_id="p1",
        source="verification",
        vp_id="VP-018",
        task_id="T-3",
        failure_class="criterion_defect",
        round=2,
        question="q",
        options=["accept", "revise"],
        default_option="accept",
        mode="auto",
        timeout_sec=1800,
        answer_note="raised by the triage",
    )
    decision = models.Decision.from_request(request)

    assert decision.vp_id == "VP-018"
    assert decision.failure_class == "criterion_defect"
    assert decision.round == 2
    assert decision.answer_note == "raised by the triage"

    row = decision.to_row()
    assert row["vp_id"] == "VP-018"
    assert row["failure_class"] == "criterion_defect"
    assert row["round"] == 2
    assert row["answer_note"] == "raised by the triage"

    read_back = models.Decision.from_row(row)
    assert read_back.vp_id == "VP-018"
    assert read_back.failure_class == "criterion_defect"
    assert read_back.round == 2
    assert read_back.answer_note == "raised by the triage"


def test_the_answer_note_is_stored_in_the_row(tmp_path: Path) -> None:
    """The human's note reaches the row, not only the jsonl mirror.

    The database is the source of truth and the mirror is a copy, so a
    note that only ever reaches the mirror can be lost — the mirror's
    write failures are swallowed on purpose, because a failed mirror
    must not turn a stored answer into an exception.  The answer's note
    therefore has to be a column of the row as well: the remark that
    explains why somebody answered is part of the answer.
    """
    from hitl.models import Decision, DecisionRequest
    from hitl.store import HitlStore

    store = HitlStore(tmp_path / "state.db", audit_dir=tmp_path / "audit")
    try:
        request = DecisionRequest(
            plan_id="p1",
            source="execution",
            question="q",
            options=["accept", "revise"],
            default_option="accept",
        )
        store.insert(Decision.from_request(request, decision_id="d-1"))

        assert store.answer_if_pending(
            "d-1", "accept", "alice", "the evidence holds"
        ), "the answer must be taken"

        conn = open_db(tmp_path / "state.db")
        try:
            row = conn.execute(
                "SELECT answer_note, answered_by FROM plan_decisions "
                "WHERE decision_id = ?",
                ("d-1",),
            ).fetchone()
        finally:
            conn.close()
    finally:
        store.close()

    assert row is not None
    assert row[0] == "the evidence holds", (
        "the answer's note must be stored in the row; it is the fact "
        "source, and the jsonl mirror is only a copy of it"
    )
    assert row[1] == "alice"
