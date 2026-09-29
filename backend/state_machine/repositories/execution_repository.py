"""Repository layer for the ``plan_execution`` table.

The :class:`ExecutionRepository` is the single write/read path for
``plan_execution`` — the per-plan row that records the execution
metadata (current_phase, attempt_count, project_dir, stop_reason,
task_progress, next_run_at, card_state, flags, exec_pid, exec_status,
started_at, updated_at).

Design contract (anchored by ``test_execution_repository.py``):

  * **No in-memory cache.**  Architecture decision point 4 forbids
    maintaining any cache; a re-read after a sibling module's direct
    SQL UPDATE must reflect the new value.  ``test_repository_has_no_stale_cache``
    pins this.

  * **No cross-table writes.**  Every write method uses
    ``UPDATE plan_execution ... WHERE plan_id = ?`` and is scoped
    to that single table.  ``test_execution_write_never_touches_verification_row``
    pins this across all write methods.

  * **IMMEDIATE transactions.**  Every write is wrapped in
    ``BEGIN IMMEDIATE → write → COMMIT`` so partial writes are
    impossible and the read-modify-write of ``task_progress`` is
    serialised across writers.

  * **PlanNotFoundError on missing plans.**  Writes to a non-existent
    ``plan_id`` raise :class:`PlanNotFoundError` (not silent no-op).

  * **JSON-encoded columns are not parsed back into Python objects
    by the write path.**  ``task_progress`` / ``card_state`` / ``flags``
    are stored as raw JSON strings; only the read path parses them
    back to dicts.  This keeps the on-disk shape stable (no
    key-order drift, no float precision loss) and avoids the
    "parse-then-rewrite" trap that the spec explicitly forbids.

The connection must be in autocommit mode (``isolation_level =
None``), as produced by :func:`state_machine.db.connection.open`.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

__all__ = ["ExecutionRepository", "PlanNotFoundError"]


#: The set of columns this repository writes/reads directly.
#: Anything outside this set must NOT be referenced by the write
#: methods — that is how the bug-1 cross-table invariant is held
#: at the SQL layer (in addition to the parameterised test).
_WRITEABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "current_phase",
        "attempt_count",
        "project_dir",
        "stop_reason",
        "task_progress",
        "next_run_at",
        "card_state",
        "flags",
        "exec_pid",
        "exec_status",
        "started_at",
        "updated_at",
    }
)

#: JSON-encoded columns.  Writes accept dicts and serialise them
#: to JSON; reads parse the JSON back to dicts so API callers see
#: plain Python objects instead of raw strings.
_JSON_COLUMNS: frozenset[str] = frozenset({"task_progress", "card_state", "flags"})

#: Columns whose ``**fields`` writes pass through verbatim (no
#: JSON round-trip).  ``None`` is a valid value for each of these.
_PASSTHROUGH_COLUMNS: frozenset[str] = frozenset(
    _WRITEABLE_COLUMNS - _JSON_COLUMNS
)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix.

    The ``updated_at`` column uses this so cross-table ORDER BY
    comparisons stay deterministic.
    """
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class PlanNotFoundError(Exception):
    """Raised by every write method when the target plan does not exist.

    Distinct from "the row exists but the predicate failed" (which
    RoutingRepository surfaces as :class:`ConflictError`): here the
    failure mode is "no row at all", which is a caller bug
    (orphan retry, plan never created, etc.).
    """

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        super().__init__(f"PlanNotFoundError(plan_id={plan_id!r})")


class ExecutionRepository:
    """Full CRUD layer for the ``plan_execution`` table.

    Every write method is wrapped in a ``BEGIN IMMEDIATE`` transaction
    via :meth:`_txn`.  There is no in-memory cache; every read goes
    straight to SQLite.
    """

    # The columns returned by ``summary`` / ``snapshot_for_list``.
    # Anything outside this set is hidden from the API layer.
    _SUMMARY_COLUMNS: tuple[str, ...] = (
        "plan_id",
        "current_phase",
        "attempt_count",
        "project_dir",
        "stop_reason",
        "task_progress",
        "next_run_at",
        "card_state",
        "flags",
        "exec_pid",
        "exec_status",
        "started_at",
        "updated_at",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Transaction skeleton (write path)
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Wrap the body in ``BEGIN IMMEDIATE → COMMIT/ROLLBACK``.

        Every public write method goes through this skeleton so
        partial writes are impossible.  On any exception the txn is
        rolled back and the original exception is re-raised.

        The connection is in autocommit mode (``isolation_level =
        None``); ``BEGIN IMMEDIATE`` opens an explicit transaction
        that takes the write lock up-front, which is what makes
        the ``task_progress`` read-modify-write race-free.
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
    # Insert / bootstrap
    # ------------------------------------------------------------------

    def insert(
        self,
        plan_id: str,
        current_phase: str,
        **fields: Any,
    ) -> None:
        """Insert a new ``plan_execution`` row.

        ``current_phase`` is required (the schema declares it
        ``NOT NULL``).  Every other column accepts the canonical
        defaults and can be overridden via ``**fields``.

        Recognised field names must be in :data:`_WRITEABLE_COLUMNS`;
        any unknown name is rejected with ``ValueError`` so a typo
        in the caller never silently produces an unexpected column.

        The ``updated_at`` column is always set to the current
        UTC time; the caller cannot override it.
        """
        if "updated_at" in fields:
            # ``updated_at`` is owned by the repository — strip it
            # so the INSERT statement owns the timestamp.  A
            # ValueError here would also be defensible but a
            # silent override is the worse failure mode.
            fields.pop("updated_at")
        # Validate every field name against the schema-known set.
        for key in fields:
            if key not in _WRITEABLE_COLUMNS:
                raise ValueError(
                    f"unknown field {key!r}; must be one of "
                    f"{sorted(_WRITEABLE_COLUMNS)}"
                )
        # JSON-encode any dict passed for a JSON column so the
        # stored value is stable (no float/key-order drift).
        encoded = self._encode_fields(fields)

        columns = ("plan_id", "current_phase", *encoded.keys(), "updated_at")
        placeholders = ", ".join("?" for _ in columns)
        values = (
            plan_id,
            current_phase,
            *encoded.values(),
            _now_iso(),
        )
        self._conn.execute(
            f"INSERT INTO plan_execution ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            values,
        )

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def summary(self, plan_id: str) -> Optional[dict[str, Any]]:
        """Return the row for ``plan_id`` or ``None``.

        JSON columns are parsed back to dicts so API consumers
        don't have to know the on-disk shape.
        """
        cols = ", ".join(self._SUMMARY_COLUMNS)
        cur = self._conn.execute(
            f"SELECT {cols} FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        record = dict(zip(self._SUMMARY_COLUMNS, row))
        return self._decode_record(record)

    def progress(self, plan_id: str) -> Optional[dict[str, Any]]:
        """Aggregate per-task counts from the ``plan_tasks`` table.

        Plan v4 (2026-09-09): the legacy implementation read
        ``plan_execution.task_progress`` JSON column and returned the
        parsed map.  The new implementation runs ``SELECT status,
        COUNT(*) FROM plan_tasks WHERE plan_id = ? GROUP BY status``,
        a deterministic SQLite aggregation that ignores the legacy
        JSON column entirely.

        Returns ``None`` when no ``plan_tasks`` rows exist for the
        plan (the caller treats this as "no progress yet").
        """
        cur = self._conn.execute(
            "SELECT status, COUNT(*) FROM plan_tasks "
            "WHERE plan_id = ? GROUP BY status",
            (plan_id,),
        )
        counts = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "in_progress": 0,
            "pending": 0,
        }
        for status, n in cur.fetchall():
            if status in counts:
                counts[status] = n
            counts["total"] += n
        if counts["total"] == 0:
            return None
        return counts

    def snapshot_for_list(
        self, plan_ids: list[str]
    ) -> dict[str, Optional[dict[str, Any]]]:
        """Return ``{plan_id: row-dict-or-None}`` for the input list.

        Plans with no row appear as ``None``.  Plans not in the
        input do not appear in the result.
        """
        result: dict[str, Optional[dict[str, Any]]] = {
            pid: None for pid in plan_ids
        }
        if not plan_ids:
            return result

        cols = ", ".join(self._SUMMARY_COLUMNS)
        placeholders = ", ".join("?" for _ in plan_ids)
        cur = self._conn.execute(
            f"SELECT {cols} FROM plan_execution "
            f"WHERE plan_id IN ({placeholders})",
            tuple(plan_ids),
        )
        for row in cur.fetchall():
            record = dict(zip(self._SUMMARY_COLUMNS, row))
            result[record["plan_id"]] = self._decode_record(record)
        return result

    def card_table(self) -> list[dict[str, Any]]:
        """Return one row per plan for the planner UI card table.

        The card view is a strict subset of the summary columns:
        ``plan_id``, ``current_phase``, ``task_progress`` (parsed
        so the UI can read ``completed`` / ``total`` directly),
        ``updated_at``.  No ``exec_pid`` / ``exec_status`` / ``flags``
        leak into the UI surface.
        """
        cols = "plan_id, current_phase, task_progress, updated_at"
        cur = self._conn.execute(
            f"SELECT {cols} FROM plan_execution ORDER BY plan_id"
        )
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            plan_id, current_phase, task_progress, updated_at = row
            progress = None
            if task_progress:
                try:
                    progress = json.loads(task_progress)
                except (TypeError, ValueError):
                    progress = None
            out.append(
                {
                    "plan_id": plan_id,
                    "current_phase": current_phase,
                    "task_progress": progress,
                    "updated_at": updated_at,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def update_phase(
        self,
        plan_id: str,
        current_phase: Optional[str] = None,
        *,
        create_if_missing: bool = False,
        **fields: Any,
    ) -> None:
        """Mutate ``current_phase`` (and any extra fields) on the row.

        ``current_phase`` is written **only when it is not ``None``**.
        ``None`` means "leave that column alone" — the same convention
        :meth:`RoutingRepository.write_plan_state` uses for its ``phase``
        argument. The column is ``NOT NULL``, so ``None`` could never be
        a legitimate value to store and the two readings cannot be
        confused.

        2026-09-19: before this, ``current_phase`` was required and
        always written, which made :meth:`update_status` — a method whose
        entire job is to set ``exec_status`` — silently overwrite the
        phase with a hard-coded ``"executing"``. The startup recovery
        path hit that on every plan whose executor had died: the plan's
        real phase (e.g. ``ready``) was replaced by ``executing`` in
        ``plan_execution``, and ``GET /api/plans`` surfaces that column
        as ``current_phase``.

        Additional column writes are accepted via ``**fields``;
        each name must be in :data:`_WRITEABLE_COLUMNS`.

        ``create_if_missing`` (2026-09-13): when True, a missing row is
        INSERTed instead of raising — an atomic
        ``INSERT ... ON CONFLICT(plan_id) DO UPDATE``. This exists for
        the "legacy plan" path: ``POST /api/execution/{id}/start``
        tolerates plans whose ``plan_execution`` row was never created
        by the bootstrap (``RoutingPlanNotFoundError`` /
        ``ExecPlanNotFoundError``), and those plans' run state
        (``exec_pid`` / ``exec_status`` / ``project_dir`` /
        ``started_at``) has no other home. Before the flag existed the
        legacy branch logged and returned, so the state lived only in
        the server's in-memory ``_execution_state`` dict and
        ``_recover_execution_states`` could not restore it after a
        restart — the exact crash-recovery hole the retired
        ``execution.json`` write used to cover.

        Raises
        ------
        PlanNotFoundError
            If no row exists for ``plan_id`` and
            ``create_if_missing`` is False.
        ValueError
            If no column would be written at all, or if
            ``create_if_missing`` is combined with ``current_phase=None``
            (the INSERT would have nothing for a ``NOT NULL`` column),
            or if ``fields`` contains an unknown column name.
        """
        if "updated_at" in fields:
            fields.pop("updated_at")
        for key in fields:
            if key not in _WRITEABLE_COLUMNS:
                raise ValueError(
                    f"unknown field {key!r}; must be one of "
                    f"{sorted(_WRITEABLE_COLUMNS)}"
                )
        encoded = self._encode_fields(fields)
        if current_phase is not None:
            encoded = {"current_phase": current_phase, **encoded}
        if not encoded:
            raise ValueError(
                "update_phase was called with nothing to write: "
                "current_phase is None and no **fields were passed"
            )
        if create_if_missing and current_phase is None:
            raise ValueError(
                "create_if_missing needs an explicit current_phase — "
                "plan_execution.current_phase is NOT NULL, so an "
                "INSERT cannot omit it"
            )

        set_clause = ", ".join(f"{col} = ?" for col in encoded)
        values: list[Any] = [*encoded.values(), _now_iso(), plan_id]
        with self._txn() as c:
            if create_if_missing:
                columns = ("plan_id", *encoded.keys(), "updated_at")
                placeholders = ", ".join("?" for _ in columns)
                conflict_updates = ", ".join(
                    f"{col} = excluded.{col}"
                    for col in (*encoded.keys(), "updated_at")
                )
                c.execute(
                    f"INSERT INTO plan_execution ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT(plan_id) DO UPDATE SET {conflict_updates}",
                    (plan_id, *encoded.values(), _now_iso()),
                )
                cur = None
            else:
                cur = c.execute(
                    f"UPDATE plan_execution SET {set_clause}, updated_at = ? "
                    f"WHERE plan_id = ?",
                    values,
                )
            if cur is not None and cur.rowcount == 0:
                # The plan does not exist — raise so callers can
                # surface a 404 rather than silently no-op.
                raise PlanNotFoundError(plan_id)

        # State-change hook. Fires AFTER the SQLite _txn commits;
        # failures never roll back the write (publish_safe swallows).
        try:
            from notifications.state_events import (
                KIND_TASK_STATE_CHANGED,
                publish_safe,
            )
            publish_safe(
                KIND_TASK_STATE_CHANGED,
                plan_id,
                sub_kind="update_phase",
                phase=current_phase,
                exec_status=fields.get("exec_status"),
            )
        except Exception:
            # Best-effort — never raise into the executor's hot path.
            pass

    def update_status(self, plan_id: str, status: str) -> None:
        """Persist ``status`` to the row's ``exec_status`` column.

        Thin convenience wrapper around :meth:`update_phase` for the
        common case where the caller only has a new status string
        (no other columns to write). The ``exec_status`` column is
        what ``GET /api/plan/<id>/summary`` and the planner UI
        card consume for the "is this execution alive?" question.

        Idempotent: re-writing the same status is a no-op at the
        SQL level (``UPDATE`` returns ``rowcount == 1`` whether the
        value changed or not, and the helper does not raise on
        that).

        2026-09-19: this used to call
        ``update_phase(plan_id, current_phase="executing", ...)``, which
        made every status write stamp ``current_phase='executing'`` on
        the row — including the terminal ``failed``/``completed`` writes
        and the startup recovery path in ``_mark_failed_dead``. A plan
        sitting at ``ready`` whose executor had died came back from a
        restart reading ``current_phase='executing'``. ``update_phase``
        now treats ``current_phase=None`` as "leave it alone", so this
        method no longer touches the column.

        Raises
        ------
        PlanNotFoundError
            If no row exists for ``plan_id``.
        ValueError
            If ``status`` is not a recognised execution status.
        """
        recognised = {"not_started", "running", "completed", "failed", "stopped"}
        if status not in recognised:
            raise ValueError(
                f"unknown execution status {status!r}; must be one of "
                f"{sorted(recognised)}"
            )
        self.update_phase(plan_id, exec_status=status)

    def update_task_progress(
        self, plan_id: str, progress: dict[str, Any]
    ) -> None:
        """Persist ``progress`` as the row's ``task_progress`` JSON.

        The write is wrapped in ``BEGIN IMMEDIATE → COMMIT`` so the
        read-modify-write is race-free under WAL: a second writer
        is blocked until this transaction commits, at which point
        it sees the new value.

        The on-disk shape is a raw JSON string (NOT a
        parsed-then-rewritten dict).  This is the spec's explicit
        requirement — parsing-then-writing would risk float
        precision loss, key-order drift, or non-JSON-serialisable
        values silently corrupting the column.

        Raises
        ------
        PlanNotFoundError
            If no row exists for ``plan_id``.
        """
        encoded = json.dumps(progress, ensure_ascii=False)
        with self._txn() as c:
            cur = c.execute(
                "UPDATE plan_execution "
                "SET task_progress = ?, updated_at = ? "
                "WHERE plan_id = ?",
                (encoded, _now_iso(), plan_id),
            )
            if cur.rowcount == 0:
                raise PlanNotFoundError(plan_id)

    def update_next_run_at(
        self, plan_id: str, next_run_at: Optional[str]
    ) -> None:
        """Set or clear the ``next_run_at`` column.

        ``None`` clears the column (the scheduler treats ``NULL``
        as "no scheduled next run").
        """
        with self._txn() as c:
            cur = c.execute(
                "UPDATE plan_execution "
                "SET next_run_at = ?, updated_at = ? "
                "WHERE plan_id = ?",
                (next_run_at, _now_iso(), plan_id),
            )
            if cur.rowcount == 0:
                raise PlanNotFoundError(plan_id)

    def update_card_state(
        self, plan_id: str, card_state: dict[str, Any]
    ) -> None:
        """Persist ``card_state`` as the row's JSON column."""
        encoded = json.dumps(card_state, ensure_ascii=False)
        with self._txn() as c:
            cur = c.execute(
                "UPDATE plan_execution "
                "SET card_state = ?, updated_at = ? "
                "WHERE plan_id = ?",
                (encoded, _now_iso(), plan_id),
            )
            if cur.rowcount == 0:
                raise PlanNotFoundError(plan_id)

    def update_flags(
        self, plan_id: str, flags: dict[str, Any]
    ) -> None:
        """Persist ``flags`` as the row's JSON column."""
        encoded = json.dumps(flags, ensure_ascii=False)
        with self._txn() as c:
            cur = c.execute(
                "UPDATE plan_execution "
                "SET flags = ?, updated_at = ? "
                "WHERE plan_id = ?",
                (encoded, _now_iso(), plan_id),
            )
            if cur.rowcount == 0:
                raise PlanNotFoundError(plan_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_fields(fields: dict[str, Any]) -> dict[str, Any]:
        """Encode JSON columns in ``fields`` to JSON strings.

        Pass-through columns are returned unchanged.  The helper
        is the single place where "dict → JSON string" happens
        for ``task_progress`` / ``card_state`` / ``flags``, so a
        future change to the encoding strategy lands in one spot.
        """
        out: dict[str, Any] = {}
        for key, value in fields.items():
            if key in _JSON_COLUMNS and value is not None:
                out[key] = json.dumps(value, ensure_ascii=False)
            else:
                out[key] = value
        return out

    @staticmethod
    def _decode_record(record: dict[str, Any]) -> dict[str, Any]:
        """Decode JSON columns in ``record`` back to Python dicts.

        ``None`` and empty-string JSON columns decode to ``None``
        so callers can use a simple ``row["task_progress"] or {}``
        pattern without first checking for ``None``.
        """
        out = dict(record)
        for key in _JSON_COLUMNS:
            value = out.get(key)
            if value is None or value == "":
                out[key] = None
                continue
            try:
                out[key] = json.loads(value)
            except (TypeError, ValueError):
                # A corrupt column is preserved as ``None`` rather
                # than propagated as a parse error — the next write
                # overwrites it cleanly.
                out[key] = None
        return out
