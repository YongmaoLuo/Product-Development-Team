"""Repository layer for the ``plan_routing`` hot row.

The :class:`RoutingRepository` is the state-machine's startup-mutex
layer: ``plan_routing`` is the single row that records the current
phase / substage / version for each plan.  Two concurrent
``start execution`` calls (e.g. a user click + the executor's
auto-restart) MUST resolve to one winner — the predicate that
:meth:`try_mark_phase` enforces is exactly that contract.

One workflow-state column
-------------------------
``plan_routing`` carries exactly one workflow-state column:
``current_phase``.  Before schema v5 it carried two — ``stage`` *and*
``current_phase`` — which had to agree, were written by disjoint code
paths, and drifted apart in production (a live row read
``stage='tasks_ready'`` while ``current_phase='verification_repairing'``,
so the scheduler and the API disagreed about whether the plan was
runnable).  ``current_phase`` survived because ``stage`` was a lossy
projection of it.  See
``state_machine.db.schema._v5_collapse_routing_stage``.  Do NOT add a
second state column here.

CAS contract
------------
``try_mark_phase(plan_id, expected_phases, new_phase, substage=None)``

Succeeds only when the current row satisfies BOTH:

  1. ``current_phase`` is in ``expected_phases``
  2. ``version`` has not advanced since the row was read by the
     caller (i.e. we read ``v0`` in the same transaction, and the
     row's ``version`` at update time is still ``v0``)

When both hold the row is updated to ``(new_phase, substage)`` and
``version`` is incremented by 1.  When either fails the row is left
untouched and :class:`ConflictError` is raised (a 409 to the API
caller).

Concurrency safety
------------------
SQLite WAL is in autocommit mode (``isolation_level=None``) and we
use ``BEGIN IMMEDIATE`` so we hold the write lock for the entire
predicate-check + update window.  Two concurrent CAS attempts
serialise: the second one wakes up after the first commits, sees
the bumped ``version``, and the ``WHERE version = :v0`` predicate
fails → :class:`ConflictError`.

The ``BEGIN IMMEDIATE`` skeleton is wrapped in a small
``@contextmanager _txn()`` so every write path shares the exact
same ``BEGIN IMMEDIATE → UPDATE → COMMIT`` lifecycle (and a single
exception handler that ROLLBACKs if anything in the body raises).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from state_machine.db.archive_scan import scan_archived_plans

__all__ = [
    "RoutingRepository",
    "ConflictError",
    "PlanNotFoundError",
]


# Sentinel for "row missing" in a SELECT.  We use this rather than
# ``None`` because the row itself is returned as a dict and ``None``
# is the documented "not found" return value of ``find`` / ``current``.
_MISSING = object()


class ConflictError(Exception):
    """Raised by :meth:`RoutingRepository.try_mark_phase` on CAS failure.

    The two failure modes that produce this:

      * The current ``current_phase`` is not in ``expected_phases``
        (predicate mismatch).
      * The current ``version`` differs from the version the row
        carried when the caller computed ``expected_phases``
        (lost-update race).

    Both leave the row untouched.  The caller is expected to
    surface a 409 to the API consumer.
    """

    def __init__(
        self,
        plan_id: str,
        reason: str,
        current_phase: Optional[str] = None,
        current_version: Optional[int] = None,
    ) -> None:
        self.plan_id = plan_id
        self.reason = reason
        self.current_phase = current_phase
        self.current_version = current_version
        super().__init__(
            f"ConflictError(plan_id={plan_id!r}, reason={reason!r}, "
            f"current_phase={current_phase!r}, current_version={current_version!r})"
        )


class PlanNotFoundError(Exception):
    """Raised by :meth:`RoutingRepository.try_mark_phase` on missing plan.

    Distinct from :class:`ConflictError` because the failure mode is
    different: this is "the plan does not exist" (caller typo /
    orphan retry), not "the plan exists but the CAS predicate
    failed".  API consumers should NOT collapse the two into a
    single 409.
    """

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        super().__init__(f"PlanNotFoundError(plan_id={plan_id!r})")


class RoutingRepository:
    """CRUD + CAS layer for the ``plan_routing`` table.

    Consumes the autocommit-mode connection produced by
    :func:`state_machine.db.connection.open` and migrated by
    :func:`state_machine.db.schema.migrate`.  Every write method
    goes through :meth:`_txn`, which wraps the body in
    ``BEGIN IMMEDIATE`` + ``COMMIT``/``ROLLBACK`` so partial writes
    are impossible.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Set ``row_factory`` to :class:`sqlite3.Row` so SELECT
        # results are column-indexable both by ordinal (row[0]) and
        # by name (row["version"]).  The connection comes from
        # :func:`state_machine.db.connection.open` in autocommit
        # mode WITHOUT a row factory — repositories are responsible
        # for installing the one their code depends on.  This
        # keeps the base ``open()`` free of repository-layer
        # concerns and lets future repos (e.g. ones that want
        # ``Row`` vs. ``dict``-returning helpers) install their
        # own factory independently.
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Transaction skeleton (write path)
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Wrap the body in a ``BEGIN IMMEDIATE`` transaction.

        On normal exit we ``COMMIT``; on any exception we
        ``ROLLBACK`` and re-raise so callers see the original
        error.  Using ``BEGIN IMMEDIATE`` (not ``DEFERRED``) means
        we acquire the write lock up-front, which is what makes the
        CAS predicate race-free under WAL.

        The connection is expected to be in autocommit mode
        (``isolation_level = None``); ``BEGIN IMMEDIATE`` is a
        no-op there because the implicit transaction is replaced
        by an explicit one — but it also serves as documentation
        that this block is a transaction.
        """
        busy_exc: Optional[sqlite3.OperationalError] = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            yield self._conn
            self._conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            # SQLITE_BUSY / "database is locked" bubbles out of the
            # body OR out of BEGIN IMMEDIATE itself when busy_timeout
            # expires.  Capture it; we will translate to ConflictError
            # below so the API layer can map it to HTTP 409 instead of
            # leaking a low-level sqlite3.OperationalError to the
            # caller.  All other OperationalError variants (e.g. schema
            # problems) keep their original type.
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                busy_exc = exc
            # Roll back before re-raising.
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # ROLLBACK can itself fail if the txn is in a
                # weird state; the original exception is what
                # matters, so swallow this one.
                pass
            if busy_exc is not None:
                # Translate to ConflictError so the business layer
                # never surfaces an unhandled OperationalError to
                # the API.  The plan_id is unknown here (the busy
                # error could surface anywhere in the txn); the
                # caller of the repository method that triggered
                # this _txn can re-raise a more specific
                # ConflictError if it has better context.
                raise ConflictError(
                    plan_id="<unknown>",
                    reason=f"SQLITE_BUSY: {exc}",
                ) from exc
            raise
        except BaseException:
            # Roll back before re-raising; use ``execute`` rather
            # than ``rollback()`` because the connection is in
            # autocommit mode (rollback() expects an implicit
            # transaction).
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # ROLLBACK can itself fail if the txn is in a
                # weird state; the original exception is what
                # matters, so swallow this one.
                pass
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC time as an ISO-8601 string.

        We pin UTC explicitly (not localtime) so a Docker container
        in a different timezone does not desync the ``updated_at``
        value used by the archived-derivation cutoff.
        """
        return (
            datetime.now(tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a :class:`sqlite3.Row` into a plain dict.

        The repository contract is "return dicts", not "return
        sqlite3.Row" — keeping the boundary here lets callers stay
        agnostic of the connection's ``row_factory``.
        """
        return {key: row[key] for key in row.keys()}

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def find(self, plan_id: str) -> Optional[dict[str, Any]]:
        """Return the ``plan_routing`` row for ``plan_id`` or ``None``.

        Returns a plain dict covering the full schema, including the
        plan-state columns (``current_phase`` / ``completed_phases`` /
        ``review_rounds`` / ``flags`` / ``verification`` /
        ``last_updated``) so :class:`PlanState` can hydrate its
        ``_state`` shape from the row.
        """
        cur = self._conn.execute(
            "SELECT plan_id, current_phase, substage, version, "
            "completed_phases, review_rounds, flags, verification, "
            "last_updated, updated_at "
            "FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def current(self, plan_id: str) -> Optional[dict[str, Any]]:
        """Convenience accessor — same contract as :meth:`find`.

        Per the interface spec ``current`` is the read-path the API
        layer uses when answering "what phase is this plan in
        right now?".  We pin it as a thin alias for :meth:`find`
        so future refactors (e.g. caching) can diverge one without
        the other.
        """
        return self.find(plan_id)

    def current_version(self, plan_id: str) -> Optional[int]:
        """Return the current ``version`` for ``plan_id`` or ``None``.

        Used by the predicate layer to snapshot the version BEFORE
        calling :meth:`try_mark_phase`.  The actual CAS check
        happens inside the transaction; this method is just the
        "read for snapshot" step.
        """
        cur = self._conn.execute(
            "SELECT version FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return int(row[0])

    def list_all(
        self,
        data_dir: Optional[Union[str, Path]] = None,
    ) -> list[dict[str, Any]]:
        """Return every plan row, with ``archived`` derived at the cutoff.

        The merged list has two sections, in this order:

          1. Every ``plan_routing`` row (routing dimensions:
             ``plan_id``, ``current_phase``, ``substage``, ``version``,
             ``updated_at``, ``archived=False``), sorted by
             ``updated_at`` DESC.
          2. Every directory-derived archived row
             (``plan_id``, ``requirement_first_line``,
             ``created_at``, ``archived=True``), sorted by
             ``created_at`` DESC.  The workflow-state fields are
             **deliberately omitted** — archived plans do not enter
             the SQLite state machine.

        The two sections never interleave because the sort keys are
        heterogeneous (routing has ``updated_at``; archive has
        ``created_at``); this matches the spec's "new first, then
        archived by created_at desc" ordering.

        Archive merge rules:

          * ``data_dir is None`` -> archive scan is skipped, the
            result is the SQLite-only section.
          * ``data_dir`` path does not exist -> the archive scan
            returns ``[]`` (safe side), so the result is the
            SQLite-only section.
          * If the same ``plan_id`` appears in BOTH sections (a
            "stale bootstrap" — the plan was archived but the
            SQLite row was never cleaned up), the **archived
            version wins**.  The SQLite row is left untouched
            (no DELETE); this layer simply refuses to surface it
            twice by filtering SQLite rows whose ``plan_id`` is
            in the archived set.
          * The ``archived`` flag is derived at read-time ONLY —
            it is NOT persisted to ``plan_routing``.

        Parameters
        ----------
        data_dir:
            Optional path to the plans directory tree
            (e.g. ``backend/plans``).  When provided, the archive
            scan runs against it; when ``None``, the archive
            section is skipped.
        """
        # ---- Step 1: SQLite rows ----
        # Sort by ``updated_at`` DESC at the SQL layer so the order
        # is deterministic regardless of insertion timing (two
        # rows inserted in the same second otherwise have identical
        # ``updated_at`` strings, and SQLite's natural order is
        # implementation-defined).  When two rows tie on
        # ``updated_at``, we fall back to ``plan_id`` DESC for a
        # fully-deterministic order.
        cur = self._conn.execute(
            "SELECT plan_id, current_phase, substage, version, updated_at "
            "FROM plan_routing "
            "ORDER BY updated_at DESC, plan_id ASC"
        )
        sqlite_rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            d = self._row_to_dict(row)
            d["archived"] = False
            sqlite_rows.append(d)

        # ---- Step 2: archived directory scan ----
        archived_rows: list[dict[str, Any]] = []
        if data_dir is not None:
            archived_rows = scan_archived_plans(Path(data_dir))

        # Sort archived rows by created_at DESC (already a string
        # ``YYYY-MM-DD``, which is lexicographically sortable).
        archived_rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)

        # ---- Step 3: archive-wins dedup ----
        # If a plan_id is in BOTH sections (stale bootstrap), drop
        # the SQLite row — the archive row is authoritative.
        if archived_rows:
            archived_ids = {r["plan_id"] for r in archived_rows}
            sqlite_rows = [
                r for r in sqlite_rows if r["plan_id"] not in archived_ids
            ]

        # ---- Step 4: assemble merged list ----
        # SQLite rows first (new plans), then archived rows.
        return [*sqlite_rows, *archived_rows]

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def insert(
        self,
        plan_id: str,
        phase: str,
        substage: Optional[str] = None,
    ) -> None:
        """Insert a brand-new ``plan_routing`` row.

        Used by the bootstrap path (a plan that just entered the
        state-machine goes through ``insert`` first, then
        :meth:`try_mark_phase` to advance).  ``version`` starts at
        0; ``updated_at`` is the current UTC time.
        """
        with self._txn():
            self._conn.execute(
                "INSERT INTO plan_routing "
                "(plan_id, current_phase, substage, version, updated_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (plan_id, phase, substage, self._now_iso()),
            )

    def write_plan_state(
        self,
        plan_id: str,
        *,
        phase: Optional[str] = None,
        completed_phases: list,
        review_rounds: dict,
        flags: dict,
        verification: dict,
        last_updated: Optional[str],
    ) -> None:
        """Write the ``PlanState``-owned columns.

        Owns: ``completed_phases`` / ``review_rounds`` / ``flags`` /
        ``verification`` / ``last_updated``, plus ``current_phase``
        **only when** the caller passes one.

        Does NOT own — and must never touch — ``version`` and
        ``substage``.  ``version`` is the CAS counter that
        :meth:`try_mark_phase` bumps; ``substage`` is that CAS's
        companion output.

        ``phase=None`` (the default) leaves ``current_phase`` alone.
        That is the important half of this contract: a ``PlanState``
        instance caches the row it loaded, so a metadata-only write
        (``set_verification_max_rounds``, ``enable_arch``, …) that
        blindly re-stated its cached phase would drag the column back
        over a phase another writer had CAS'd in the meantime.  The
        2026-09-15 incident was exactly that shape — ``/reset_rounds``
        ends with ``set_verification_max_rounds``, which snapped the
        routing value back and made the next ``/start`` answer
        ``409 stage_mismatch``.  Pre-v5 a special case in the caller
        papered over it for the ``stage`` column; now the rule lives
        here, where it applies to the only column there is.

        ``PlanState`` therefore passes ``phase`` only when *it* moved
        the phase (a ``transition_to`` / ``force_set_phase`` / …), and
        omits it on pure metadata writes.

        This method used to be an ``INSERT OR REPLACE`` over the whole
        row, which silently reset ``version`` to 0 and wiped
        ``substage`` on every call.  ``ON CONFLICT DO UPDATE`` with an
        explicit column list fixes that cause: the conflicting row keeps
        every column this method does not name.

        The ``INSERT`` arm only fires for a plan that has no routing row
        yet (a plan bootstrapped by a code path that skipped
        :meth:`insert`); it seeds ``version = 0`` and, when no ``phase``
        is given, the column default.
        """
        params = (
            plan_id,
            json.dumps(completed_phases),
            json.dumps(review_rounds),
            json.dumps(flags),
            json.dumps(verification),
            last_updated,
            self._now_iso(),
        )
        _updates = (
            " completed_phases = excluded.completed_phases,"
            " review_rounds = excluded.review_rounds,"
            " flags = excluded.flags,"
            " verification = excluded.verification,"
            " last_updated = excluded.last_updated,"
            " updated_at = excluded.updated_at"
        )
        if phase is None:
            # Metadata-only write: ``current_phase`` is absent from both
            # the INSERT column list (the NOT NULL DEFAULT covers the
            # bootstrap case) and the conflict arm.
            sql = (
                "INSERT INTO plan_routing ("
                " plan_id, completed_phases, review_rounds, flags,"
                " verification, last_updated, version, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET" + _updates
            )
        else:
            sql = (
                "INSERT INTO plan_routing ("
                " plan_id, completed_phases, review_rounds, flags,"
                " verification, last_updated, version, current_phase,"
                " updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET"
                " current_phase = excluded.current_phase,"
                + _updates
            )
            params = (
                plan_id,
                json.dumps(completed_phases),
                json.dumps(review_rounds),
                json.dumps(flags),
                json.dumps(verification),
                last_updated,
                phase,
                self._now_iso(),
            )
        with self._txn():
            self._conn.execute(sql, params)

    def try_mark_phase(
        self,
        plan_id: str,
        expected_phases: tuple[str, ...],
        new_phase: str,
        substage: Optional[str] = None,
    ) -> bool:
        """CAS the row's phase; return True on success, raise on failure.

        Predicate: the current ``current_phase`` must be in
        ``expected_phases`` AND the row's ``version`` must equal the
        version we observed in this same transaction.  On success
        the row is updated to ``(new_phase, substage)`` and
        ``version`` is incremented by 1.  On any failure mode
        (missing plan, predicate mismatch, version mismatch) the
        row is left untouched and an exception is raised.

        Returns
        -------
        bool
            ``True`` on success (caller can proceed knowing the
            update is durable).

        Raises
        ------
        ValueError
            If ``expected_phases`` is empty (a programmer error —
            an empty tuple would mean "match any phase", which is
            the silent-failure mode the API contract rejects).
        PlanNotFoundError
            If no row exists for ``plan_id``.
        ConflictError
            If the predicate or version check fails.
        """
        if not expected_phases:
            raise ValueError(
                "expected_phases must be a non-empty tuple; an empty "
                "tuple would silently match any current phase and is "
                "the bug 2 regression mode."
            )

        try:
            with self._txn() as c:
                # Step 1: read the row INSIDE the same transaction.  This
                # pins the version we're going to CAS against.  With
                # ``BEGIN IMMEDIATE`` no other writer can sneak in
                # between this SELECT and the UPDATE below.
                cur = c.execute(
                    "SELECT current_phase, version FROM plan_routing "
                    "WHERE plan_id = ?",
                    (plan_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise PlanNotFoundError(plan_id)
                current_phase, current_version = row[0], int(row[1])

                # Step 2: predicate check.
                if current_phase not in expected_phases:
                    # No UPDATE; txn COMMITs the (empty) work, which is
                    # fine — no business row was touched.
                    raise ConflictError(
                        plan_id=plan_id,
                        reason=(
                            f"predicate mismatch: current phase "
                            f"{current_phase!r} is not in expected_phases="
                            f"{tuple(expected_phases)!r}"
                        ),
                        current_phase=current_phase,
                        current_version=current_version,
                    )

                # Step 3: optimistic-lock check.  The version we just
                # read IS the current version because we're inside
                # ``BEGIN IMMEDIATE``, so the only way this can fail is
                # if the caller pre-snapshotted a version that is now
                # out-of-date.  We expose that as a separate guard so
                # the failure message is clear, but in practice the
                # CAS happens against current_version which is what
                # the DB holds right now.
                #
                # We still do ``WHERE version = ?`` on the UPDATE so
                # the predicate is enforced at the SQL layer too — if
                # a future refactor drops ``BEGIN IMMEDIATE`` the row
                # is still safe.
                new_version = current_version + 1
                cur = c.execute(
                    "UPDATE plan_routing "
                    "SET current_phase = ?, substage = ?, version = ?, "
                    "updated_at = ? "
                    "WHERE plan_id = ? AND version = ?",
                    (
                        new_phase,
                        substage,
                        new_version,
                        self._now_iso(),
                        plan_id,
                        current_version,
                    ),
                )

                # ``rowcount`` is 0 if the WHERE clause matched no row.
                # With our BEGIN IMMEDIATE that means a concurrent
                # writer managed to bump version between our SELECT
                # and our UPDATE — extremely rare but not impossible
                # if the BEGIN IMMEDIATE was downgraded to BEGIN
                # DEFERRED in a future refactor.
                if cur.rowcount == 0:
                    # Re-read to surface the new current version.
                    post = c.execute(
                        "SELECT current_phase, version FROM plan_routing "
                        "WHERE plan_id = ?",
                        (plan_id,),
                    ).fetchone()
                    post_phase = post[0] if post is not None else None
                    post_version = int(post[1]) if post is not None else None
                    raise ConflictError(
                        plan_id=plan_id,
                        reason=(
                            f"version mismatch: row was bumped between "
                            f"read and update (expected version "
                            f"{current_version})"
                        ),
                        current_phase=post_phase,
                        current_version=post_version,
                    )

        except ConflictError as exc:
            # The ConflictError may have come from inside ``_txn``
            # (where it was synthesised from an OperationalError on
            # busy_timeout expiry); enrich the error context with the
            # plan_id if _txn did not have it.
            if exc.plan_id == "<unknown>":
                raise ConflictError(
                    plan_id=plan_id,
                    reason=(
                        f"SQLITE_BUSY contention while CASing plan_id="
                        f"{plan_id!r}: {exc.reason}"
                    ),
                ) from exc
            raise

        # State-change hook ( fires the event AFTER the SQLite _txn
        # commits, so subscribers see the new phase value when they
        # re-read the row). Failure of the publish path never rolls
        # back the CAS — ``publish_safe`` swallows its own errors.
        from notifications.state_events import (
            KIND_PLAN_PHASE_CHANGED,
            publish_safe,
        )
        publish_safe(
            KIND_PLAN_PHASE_CHANGED,
            plan_id,
            phase=new_phase,
            previous_phase=current_phase,
            substage=substage,
        )

        return True
