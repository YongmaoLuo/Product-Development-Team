"""Storage for human-in-the-loop decisions.

Why this module exists
----------------------
:mod:`hitl.models` describes *what* a decision is; this module is the
only place that writes one down.  A decision is asked by one thread
(an executor, a verification round, a route handler) and answered by
another (a human, minutes later, possibly through a different
process), so the question and the answer have to meet somewhere both
can reach.  That place is the ``plan_decisions`` table of the SQLite
database the state machine already owns.

Three guarantees, and the reason for each
-----------------------------------------

**The answer is a compare-and-set.**
:meth:`HitlStore.answer_if_pending` updates the row only while it is
still ``pending``::

    UPDATE plan_decisions SET ... WHERE decision_id=? AND status='pending'

Two humans clicking "accept" at the same second therefore produce
*one* stored answer: the write lock serialises them, and the loser
finds the predicate already false.  The loser gets ``False`` — the
same answer it would have given had it arrived a minute later.  This
is the property the whole HITL feature rests on: an answer must be
atomic (never half-written) and idempotent (re-delivery is a no-op,
not a second answer).

**Only a ``pending`` decision moves.**
``answered`` / ``defaulted`` / ``paused`` / ``unroutable`` are
terminal.  Every transition here carries ``WHERE status='pending'``,
so a late answer cannot overwrite a defaulted decision's answer, a
second pause cannot re-pause, and the columns ``answered_by`` /
``answered_at`` keep pointing at whoever actually resolved it.

**The JSONL file is a mirror, not the truth.**
Every *successful* transition appends exactly one line to
``<plans_root>/<plan_id>/hitl_decisions.jsonl``.  The database
decides; the file records.  So a failed audit write is logged and
swallowed rather than raised — a full disk must not turn a correctly
stored answer into an exception the caller would retry.  The mirror's
line shape is a fixed whitelist (:data:`AUDIT_FIELDS`), pinned
because an audit trail that grows new keys whenever a model grows a
field stops being comparable across versions.

Why the store owns the connection
---------------------------------
Other repositories take a live ``sqlite3.Connection`` and leave
opening (and migrating) to their caller.  A HITL store is reached
from route handlers, worker threads and CLIs that each want *a* store
and none of the ceremony, so :class:`HitlStore` opens its own
connection through :func:`state_machine.db.connection.open` (which is
what pins WAL, ``busy_timeout`` and autocommit — the CAS depends on
all three) and runs :func:`state_machine.db.schema.migrate` so the
``plan_decisions`` table is there.

Two locks, two jobs
-------------------
``self._lock`` serialises this store's own transactions.  It is not
cosmetic: the connection is opened with ``check_same_thread=False`` so
a worker thread can reuse it, and two threads issuing
``BEGIN IMMEDIATE`` on the *same* connection would interleave into one
transaction.  Separate ``HitlStore`` instances (and separate
processes) are serialised by SQLite's own write lock plus
``busy_timeout`` — that is the cross-actor case, and it is what
``test_answer_cas`` exercises.

``_AUDIT_LOCK`` is module-level because an audit line must be whole:
one ``write()`` of a complete line under ``O_APPEND``.  It is
independent of the transaction locks on purpose — a slow audit write
must not hold the database write lock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union

from config_paths import resolve_plans_dir
from hitl.models import Decision, DecisionStatus
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate

__all__ = [
    "AUDIT_FIELDS",
    "AUDIT_FILENAME",
    "HitlStore",
]

logger = logging.getLogger(__name__)

DbPath = Union[str, Path]

#: Name of the per-plan audit mirror, written next to the other plan
#: artifacts so it travels with the plan directory.
AUDIT_FILENAME = "hitl_decisions.jsonl"

#: The fixed shape of an audit line, in the order it is written.
#: Anything not named here is dropped by :meth:`HitlStore.append_audit`
#: — in particular the question text, which can carry source excerpts
#: the mirror has no business holding.
AUDIT_FIELDS: tuple[str, ...] = (
    "ts",
    "event",
    "decision_id",
    "plan_id",
    "status",
    "answer",
    "answered_by",
    "note",
    "audit_seq",
)

#: Serialises the append of whole audit lines within this process.
_AUDIT_LOCK = threading.Lock()

_INSERT_SQL = """
INSERT OR REPLACE INTO plan_decisions (
  decision_id, plan_id, source, task_id, question, options,
  default_option, mode, timeout_sec, status, answer, answered_by,
  answered_at, deadline_at, created_at, updated_at, heartbeat_at,
  audit_seq, vp_id, failure_class, round, answer_note
) VALUES (
  :decision_id, :plan_id, :source, :task_id, :question, :options,
  :default_option, :mode, :timeout_sec, :status, :answer, :answered_by,
  :answered_at, :deadline_at, :created_at, :updated_at, :heartbeat_at,
  :audit_seq, :vp_id, :failure_class, :round, :answer_note
)
"""

#: The CAS.  The ``status = 'pending'`` predicate is the whole point:
#: it is what makes a second answer a no-op instead of an overwrite.
#:
#: ``answer_note`` is written with ``COALESCE``: a note that came with
#: the answer wins, and an answer that carries no note does not erase
#: the note the question was created with.  The note is a column and
#: not only a line in the jsonl mirror because the mirror is a copy —
#: its write failures are swallowed on purpose, so a note that lived
#: only there could be lost while the answer it explains stayed
#: committed.
_TRANSITION_SQL = """
UPDATE plan_decisions
   SET status      = :status,
       answer      = :answer,
       answered_by = :answered_by,
       answered_at = :answered_at,
       answer_note = COALESCE(:answer_note, answer_note),
       updated_at  = :updated_at,
       audit_seq   = audit_seq + 1
 WHERE decision_id = :decision_id
   AND status     = 'pending'
"""

#: The CAS for the transition that settles a question nobody was asked.
#:
#: The predicate is the *open* states rather than ``pending``: a
#: question that was asked and then paused (its ``strict`` deadline
#: passed) is still a question a human may answer, and a question that
#: was never asked is exactly as un-answerable as one that was.  Only
#: a settled question (``answered`` / ``defaulted`` / ``unroutable``)
#: is left alone, so the transition can be attempted twice without
#: writing two resolutions.
#:
#: The answer columns are cleared rather than carried over: nobody
#: answered, so there is no answer, no answerer and no answer time.
_UNROUTABLE_SQL = """
UPDATE plan_decisions
   SET status      = :status,
       answer      = NULL,
       answered_by = NULL,
       answered_at = NULL,
       updated_at  = :updated_at,
       audit_seq   = audit_seq + 1
 WHERE decision_id = :decision_id
   AND status      IN ('pending', 'paused')
"""

#: The states a decision may still be in while it is open — waiting
#: for an answer (``pending``) or waiting for a human who may still
#: come (``paused``).  A settled decision is in none of them.
_OPEN_STATUSES = ("pending", "paused")

#: The CAS for the liveness stamp.
#:
#: The predicate is the *open* states, for the same reason the
#: unroutable transition uses them: a question that is still open is a
#: question somebody may still be waiting for, and a settled one is
#: not.  Stamping a settled row would say "somebody is waiting" about
#: a question nobody waits for any more.
#:
#: Only ``heartbeat_at`` and ``updated_at`` move.  A heartbeat is not a
#: resolution: the status, the answer and the answerer are untouched,
#: and nothing is written to the audit mirror — a beat every minute
#: would drown a mirror whose lines are the record of what *happened*.
_HEARTBEAT_SQL = """
UPDATE plan_decisions
   SET heartbeat_at = :heartbeat_at,
       updated_at  = :updated_at
 WHERE decision_id = :decision_id
   AND status      IN ('pending', 'paused')
"""


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with an offset.

    The same spelling :mod:`hitl.models` uses for ``created_at``, so a
    timestamp written by the model and one written by a transition can
    be compared as plain strings.
    """
    return datetime.now(timezone.utc).isoformat()


def _encode_answer(answer: Any) -> Optional[str]:
    """JSON-encode a stored answer; ``None`` stays ``NULL``."""
    if answer is None:
        return None
    return json.dumps(answer, ensure_ascii=False)


class HitlStore:
    """Read/write access to ``plan_decisions`` plus its audit mirror.

    Parameters
    ----------
    db_path:
        SQLite database holding ``plan_decisions``.  Opened through
        :func:`state_machine.db.connection.open`, so the WAL /
        ``busy_timeout`` / autocommit contract the CAS relies on is
        applied here rather than assumed.
    audit_dir:
        Root of the audit mirror; each plan's line file lives at
        ``<audit_dir>/<plan_id>/hitl_decisions.jsonl``.  Defaults to
        :func:`config_paths.resolve_plans_dir`, i.e. the repo's
        ``plans/`` tree (or whatever ``PDT_PLANS_DIR`` points at) —
        the same seam every other plan-artifact writer uses.
    """

    def __init__(self, db_path: DbPath, *, audit_dir: Optional[DbPath] = None) -> None:
        self._conn = open_db(db_path)
        # Idempotent, and the reason a fresh (empty) database file is a
        # legal thing to hand this constructor: the table is created on
        # first use rather than being a precondition the caller must
        # satisfy.  Cheap on a migrated database — a version read plus
        # one sqlite_master lookup.
        migrate(self._conn)
        self._conn.row_factory = sqlite3.Row
        self._audit_root = (
            Path(audit_dir) if audit_dir is not None else resolve_plans_dir()
        )
        # Per-store transaction serialisation.  See the module
        # docstring: the connection is shared across threads
        # (check_same_thread=False), so two threads must not drive one
        # transaction.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> "HitlStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, decision_id: str) -> Optional[Decision]:
        """Return the decision with ``decision_id``, or ``None``.

        ``None`` is the documented answer for "no such decision" — an
        unknown id is an ordinary outcome (a stale card, a CLI pointed
        at a database that was reset), not an error.
        """
        row = self._conn.execute(
            "SELECT * FROM plan_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return Decision.from_row(row)

    def list_by_plan(
        self,
        plan_id: str,
        *,
        status: Optional[Union[str, DecisionStatus]] = None,
    ) -> List[Decision]:
        """Return a plan's decisions, oldest first, optionally by status.

        ``status`` narrows to one state — the ``pending`` list is what a
        plan's UI polls for, and the index
        ``idx_plan_decisions_plan_status`` is built for exactly this
        lookup.
        """
        sql = "SELECT * FROM plan_decisions WHERE plan_id = ?"
        params: List[Any] = [plan_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(DecisionStatus(status).value)
        sql += " ORDER BY created_at, decision_id"
        return [Decision.from_row(row) for row in self._conn.execute(sql, params)]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def insert(self, decision: Decision) -> Decision:
        """Store ``decision`` as given and return it.

        The write is a full-row write, so re-inserting an existing
        ``decision_id`` replaces it — which is what makes a retried
        create idempotent rather than a constraint violation.  It is
        deliberately *not* a transition: creating a decision does not
        resolve one, so this never rewrites an answer on its own.
        """
        row = decision.to_row()
        record = {
            "event": "created",
            "decision_id": row["decision_id"],
            "plan_id": row["plan_id"],
            "status": row["status"],
            "answer": decision.answer,
            "answered_by": row["answered_by"],
            "note": None,
            "audit_seq": row["audit_seq"],
        }
        with self._lock:
            with self._txn():
                self._conn.execute(_INSERT_SQL, row)
            self._write_audit(record)
        return decision

    def answer_if_pending(
        self,
        decision_id: str,
        answer: Any,
        answered_by: Optional[str] = None,
        note: str = "",
    ) -> bool:
        """Record a human answer, but only while the decision is pending.

        Returns ``True`` when this call is the one that resolved the
        decision and ``False`` when it did not — because the id is
        unknown, or because somebody (possibly this very call,
        delivered twice) already resolved it.  A ``False`` return
        leaves ``answer``, ``answered_by`` and ``answered_at`` exactly
        as the winner left them.

        The ``note`` is stored in the row's ``answer_note`` column as
        well as in the audit mirror: the remark that explains the
        answer is part of the answer, and the mirror is only a copy of
        the row.
        """
        return self._transition(
            decision_id,
            new_status=DecisionStatus.ANSWERED,
            event="answered",
            answer=answer,
            answered_by=answered_by,
            answered_at=_now_iso(),
            note=note,
            answer_note=note or None,
        )

    def mark_defaulted(
        self,
        decision_id: str,
        answer: Any = None,
        note: str = "",
    ) -> bool:
        """Resolve a decision by falling back, only while it is pending.

        With no explicit ``answer`` the stored ``default_option`` is
        used, which is what makes an ``auto`` decision self-resolving
        when its deadline passes.  A pending decision with neither an
        explicit answer nor a ``default_option`` has nothing to fall
        back to, so this raises ``ValueError`` rather than storing a
        resolution with no answer — that case is ``unroutable``, a
        different transition, not a silent "defaulted to nothing".

        ``answered_by`` stays ``NULL``: nobody answered, and the column
        names the human who did.
        """
        return self._transition(
            decision_id,
            new_status=DecisionStatus.DEFAULTED,
            event="defaulted",
            answer=answer,
            use_default_option=answer is None,
            answered_by=None,
            answered_at=_now_iso(),
            note=note,
        )

    def mark_paused(self, decision_id: str, note: str = "") -> bool:
        """Suspend a still-pending decision; ``False`` if it is resolved.

        Paused is what a strict decision becomes when its owner stops
        waiting (the verification thread writes ``pause_state.json`` and
        exits).  The question is neither answered nor defaulted — hence
        no ``answered_by`` and no ``answered_at``.
        """
        return self._transition(
            decision_id,
            new_status=DecisionStatus.PAUSED,
            event="paused",
            answer=None,
            answered_by=None,
            answered_at=None,
            note=note,
        )

    def mark_unroutable(self, decision_id: str, note: str = "") -> bool:
        """Settle a question nobody was asked; ``False`` if it is settled.

        ``unroutable`` is what a question becomes when there was nobody
        to ask: the plan's outlet names no destination, or every
        destination it names refused the question.  The question is
        settled *without being answered* — there is no answer, no
        answerer and no answer time, because no human ever saw it.

        Only an **open** decision moves (``pending`` or ``paused``).
        A question that was answered, defaulted or already marked
        unroutable is left exactly as it is, and the call says so by
        returning ``False`` — which is what makes the transition safe
        to attempt twice: the second attempt finds the row settled and
        writes nothing, so the audit mirror records one resolution and
        not two.
        """
        record: Optional[Dict[str, Any]] = None
        with self._lock:
            with self._txn():
                row = self._conn.execute(
                    "SELECT status, plan_id, audit_seq "
                    "FROM plan_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if row is None or row["status"] not in _OPEN_STATUSES:
                    # Unknown id, or already settled by somebody else.
                    record = None
                else:
                    self._conn.execute(
                        _UNROUTABLE_SQL,
                        {
                            "decision_id": decision_id,
                            "status": DecisionStatus.UNROUTABLE.value,
                            "updated_at": _now_iso(),
                        },
                    )
                    record = {
                        "event": "unroutable",
                        "decision_id": decision_id,
                        "plan_id": row["plan_id"],
                        "status": DecisionStatus.UNROUTABLE.value,
                        "answer": None,
                        "answered_by": None,
                        "note": note or None,
                        "audit_seq": (row["audit_seq"] or 0) + 1,
                    }
            # Outside the transaction on purpose: the row is committed
            # and authoritative, so a mirror that cannot be written is
            # logged rather than raised at the caller.
            if record is not None:
                self._write_audit(record)
        return record is not None

    # ------------------------------------------------------------------
    # The heartbeat
    # ------------------------------------------------------------------

    def mark_heartbeat(
        self,
        decision_id: str,
        at: Optional[str] = None,
    ) -> bool:
        """Stamp the decision's heartbeat; ``False`` when it is settled.

        A heartbeat says "somebody is still waiting for this question".
        It is not an answer and not a resolution: the row's status, its
        answer and its answerer are left exactly as they were, and
        nothing is appended to the audit mirror — a beat every minute
        would drown a mirror whose lines are the record of what
        happened.

        Only an **open** decision is stamped (``pending`` or
        ``paused``).  A question nobody waits for any more has no
        heartbeat to give, and the compare-and-set is what says so: the
        second beat of a settled question writes nothing and says so by
        returning ``False``.

        Parameters
        ----------
        decision_id:
            The decision being waited for.
        at:
            The instant to stamp, as an ISO-8601 string.  Defaults to
            now, so a caller that does not care about the exact reading
            has nothing to pass.

        Returns
        -------
        bool
            ``True`` when the stamp was written, ``False`` when the
            decision is unknown or already settled.
        """
        stamp = at or _now_iso()
        with self._lock:
            with self._txn():
                cursor = self._conn.execute(
                    _HEARTBEAT_SQL,
                    {
                        "decision_id": decision_id,
                        "heartbeat_at": stamp,
                        "updated_at": _now_iso(),
                    },
                )
                written = cursor.rowcount > 0
        return written

    # ------------------------------------------------------------------
    # The transition itself
    # ------------------------------------------------------------------

    def _transition(
        self,
        decision_id: str,
        *,
        new_status: DecisionStatus,
        event: str,
        answer: Any,
        answered_by: Optional[str],
        answered_at: Optional[str],
        note: str,
        answer_note: Optional[str] = None,
        use_default_option: bool = False,
    ) -> bool:
        """Apply a pending-only transition and mirror it to the audit log.

        The single place the CAS lives.  Everything the decision is
        needed for — is it still pending, what is its default, which
        plan does it belong to — is read inside the same
        ``BEGIN IMMEDIATE`` that performs the update, so the values
        that decide the write cannot be stale by the time it lands.

        ``answer_note`` is the note stored in the row's
        ``answer_note`` column; it is the *answer's* note, so only the
        answer transition passes one.  A transition that passes none
        leaves the column as it was — a defaulting or a pause is not
        an answer and does not overwrite the remark of one.
        """
        record: Optional[Dict[str, Any]] = None
        with self._lock:
            with self._txn():
                row = self._conn.execute(
                    "SELECT status, default_option, plan_id, audit_seq "
                    "FROM plan_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if row is None or row["status"] != DecisionStatus.PENDING.value:
                    # Unknown id, or already resolved by somebody else.
                    # Nothing to write, so nothing to audit.
                    record = None
                else:
                    if use_default_option and answer is None:
                        answer = row["default_option"]
                        if answer is None:
                            raise ValueError(
                                f"cannot default decision {decision_id!r}: no "
                                "answer given and the decision has no "
                                "default_option to fall back to"
                            )
                    self._conn.execute(
                        _TRANSITION_SQL,
                        {
                            "decision_id": decision_id,
                            "status": new_status.value,
                            "answer": _encode_answer(answer),
                            "answered_by": answered_by,
                            "answered_at": answered_at,
                            "answer_note": answer_note,
                            "updated_at": _now_iso(),
                        },
                    )
                    record = {
                        "event": event,
                        "decision_id": decision_id,
                        "plan_id": row["plan_id"],
                        "status": new_status.value,
                        "answer": answer,
                        "answered_by": answered_by,
                        "note": note or None,
                        "audit_seq": (row["audit_seq"] or 0) + 1,
                    }
            # Outside the transaction on purpose: the row is committed
            # and authoritative, so a mirror that cannot be written is
            # logged rather than raised at the caller.
            if record is not None:
                self._write_audit(record)
        return record is not None

    @contextlib.contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Wrap a write in ``BEGIN IMMEDIATE`` … ``COMMIT``.

        ``IMMEDIATE`` (not ``DEFERRED``) takes SQLite's write lock up
        front, so the read that decides whether a transition applies and
        the write that applies it cannot be split by another writer.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    # ------------------------------------------------------------------
    # Audit mirror
    # ------------------------------------------------------------------

    def audit_path(self, plan_id: str) -> Path:
        """Return where ``plan_id``'s audit lines live."""
        return self._audit_root / plan_id / AUDIT_FILENAME

    def append_audit(self, record: Mapping[str, Any]) -> Path:
        """Append one whitelisted line to ``plan_id``'s audit mirror.

        Only :data:`AUDIT_FIELDS` is written, in that order, with a
        missing field recorded as ``null`` rather than omitted — so
        every line in the file has the same shape and can be read with
        one comprehension.  ``ts`` defaults to now; anything the caller
        passes that is not on the whitelist is dropped.
        """
        payload: Dict[str, Any] = {name: record.get(name) for name in AUDIT_FIELDS}
        plan_id = payload.get("plan_id")
        if not plan_id:
            raise ValueError(
                "an audit record must carry a plan_id — it is what selects "
                f"the file; got {record.get('plan_id')!r}"
            )
        if not payload["ts"]:
            payload["ts"] = _now_iso()
        return self._append_line(str(plan_id), payload)

    def _write_audit(self, record: Dict[str, Any]) -> None:
        """Mirror a transition, degrading to a log line on failure.

        The database is the source of truth here: the decision is
        already committed when this runs, and a mirror that cannot be
        written must not be reported to the caller as a failed answer
        (they would retry, and the retry is a second answer).
        """
        try:
            self.append_audit(record)
        except OSError as exc:
            logger.warning(
                "hitl audit mirror write failed for decision %s: %s",
                record.get("decision_id"),
                exc,
            )

    def _append_line(self, plan_id: str, payload: Dict[str, Any]) -> Path:
        path = self.audit_path(plan_id)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with _AUDIT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            # One write of a whole line under O_APPEND: concurrent
            # writers cannot tear it in half or overwrite each other's
            # offsets.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        return path