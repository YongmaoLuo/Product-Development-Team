"""ArtifactRepository — pointer-only persistence for plan_artifacts.

The state-machine refactor stores the **paths** of every plan-level
artifact (interview, PRD, arch-design, test-design, tasks, execution
metadata, etc.) in the ``plan_artifacts`` SQLite table; the artifact
bodies live on disk and are NOT mirrored into the database. This
class is the single write/read path for that table.

Design contract (anchored by ``test_artifact_repository.py``):

  * The primary key is the pair ``(plan_id, artifact_type)``. The
    ``upsert`` SQL form is
    ``INSERT ... ON CONFLICT(plan_id, artifact_type) DO UPDATE`` so a
    second call updates the existing row in place — no duplicate
    rows, no row churn.

  * Only the **pointer** (file path + content hash) lives in the
    table. The body stays on disk. The "10MB path string -> DB
    grows < 1KB" test pins this: even a pathologically long
    ``file_path`` does not bloat the database.

  * The ``status`` column is restricted to the canonical enum
    ``{pending, generated, stale, missing}``. Any other value
    raises :class:`ValueError` *before* the SQL executes so a typo
    in the caller never produces a corrupt row.

  * ``upsert`` does not read the file. The hash is supplied by the
    caller (computed AFTER the file lands on disk by the writer
    layer). This keeps the repository a pure pointer store.

The connection must be in autocommit mode (``isolation_level =
None``), as produced by :func:`state_machine.db.connection.open`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["ArtifactRepository", "VALID_STATUSES"]


#: The canonical status enum for the ``plan_artifacts.status`` column.
#: Anything outside this set is rejected with :class:`ValueError` at
#: :meth:`ArtifactRepository.upsert` time.
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "generated", "stale", "missing"}
)


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix.

    SQLite stores ``TEXT`` for ``updated_at`` / ``created_at``; we
    emit a deterministic, sortable shape so the column can be
    consumed by external tools without timezone math.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ArtifactRepository:
    """Pointer-only repository over the ``plan_artifacts`` table.

    Parameters
    ----------
    conn:
        An open :class:`sqlite3.Connection` in autocommit mode
        (``isolation_level = None``), as produced by
        :func:`state_machine.db.connection.open`. The connection
        must already have the ``plan_artifacts`` table — typically
        via :func:`state_machine.db.schema.migrate`.
    """

    # Columns we read back. Kept as a class constant so callers and
    # tests can verify the record shape against a single source of
    # truth.
    _COLUMNS: tuple[str, ...] = (
        "plan_id",
        "artifact_type",
        "file_path",
        "status",
        "content_hash",
        "created_at",
        "updated_at",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert(
        self,
        plan_id: str,
        artifact_type: str,
        file_path: str,
        status: str,
        content_hash: Optional[str] = None,
    ) -> None:
        """Insert a new artifact row, or update the existing one in place.

        The ``status`` argument is validated against
        :data:`VALID_STATUSES` *before* the SQL executes — a typo in
        the caller never produces a corrupt row.

        The hash is supplied by the caller (it has just landed on
        disk and the writer layer computed the hash); the repository
        does NOT read the file body.

        Parameters
        ----------
        plan_id:
            The plan that owns this artifact (foreign key in spirit
            to the other ``plan_*`` tables; not enforced at the DB
            level here to keep the schema simple).
        artifact_type:
            The artifact kind — e.g. ``"interview"``, ``"prd"``,
            ``"arch"``, ``"test"``, ``"tasks"``, ``"execution"``.
            Combined with ``plan_id`` this is the primary key.
        file_path:
            Filesystem path to the artifact on disk. Treated as a
            pointer — its body is NOT read or stored in the table.
        status:
            One of :data:`VALID_STATUSES`. Any other value raises
            :class:`ValueError`.
        content_hash:
            The sha256 (or similar) hash of the file body, computed
            by the caller after the file is written. ``None`` is
            accepted for in-flight rows whose body has not yet
            landed.
        """
        if status not in VALID_STATUSES:
            raise ValueError(
                f"invalid artifact status {status!r}; "
                f"must be one of {sorted(VALID_STATUSES)}"
            )

        now = _utcnow_iso()
        # ``INSERT ... ON CONFLICT(plan_id, artifact_type) DO UPDATE``
        # is the canonical SQLite upsert. The ``created_at`` column
        # is set on the first insert and preserved on subsequent
        # updates (so the row's age is anchored at first write).
        # ``excluded`` refers to the values that *would* have been
        # inserted — i.e. the incoming row.
        self._conn.execute(
            """
            INSERT INTO plan_artifacts (
                plan_id, artifact_type, file_path, status,
                content_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id, artifact_type) DO UPDATE SET
                file_path = excluded.file_path,
                status = excluded.status,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at
            """,
            (
                plan_id,
                artifact_type,
                file_path,
                status,
                content_hash,
                now,
                now,
            ),
        )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def find(
        self, plan_id: str, artifact_type: str
    ) -> Optional[dict]:
        """Return the row for ``(plan_id, artifact_type)`` or ``None``.

        The returned dict carries the full row (including
        ``created_at`` and ``updated_at``); callers that only need
        the four "user-facing" columns can ignore the timestamps.
        """
        cur = self._conn.execute(
            "SELECT {cols} FROM plan_artifacts "
            "WHERE plan_id = ? AND artifact_type = ?".format(
                cols=", ".join(self._COLUMNS)
            ),
            (plan_id, artifact_type),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip(self._COLUMNS, row))

    def list_for_plan(self, plan_id: str) -> list[dict]:
        """Return every artifact row for ``plan_id`` (or ``[]``)."""
        cur = self._conn.execute(
            "SELECT {cols} FROM plan_artifacts "
            "WHERE plan_id = ? "
            "ORDER BY artifact_type ASC".format(
                cols=", ".join(self._COLUMNS)
            ),
            (plan_id,),
        )
        return [dict(zip(self._COLUMNS, row)) for row in cur.fetchall()]

    def list_for_plans(
        self, plan_ids: list[str]
    ) -> dict[str, list[dict]]:
        """Group artifacts by plan_id; missing plans map to ``[]``.

        Returns a dict keyed by every input ``plan_id``. Plans in
        the input that have no rows appear as empty lists — the
        caller does not need to disambiguate "missing plan" vs
        "empty plan".
        """
        # Pre-seed every input plan_id so the result is a complete
        # mapping (not just "plans that have rows").
        result: dict[str, list[dict]] = {pid: [] for pid in plan_ids}
        if not plan_ids:
            return result

        # SQLite's IN clause with a tuple of placeholders scales
        # up to ~1000 items without performance concerns; we use
        # a parameterised list rather than a hard-coded number of
        # ``?`` markers so callers can pass any length.
        placeholders = ", ".join("?" for _ in plan_ids)
        cur = self._conn.execute(
            "SELECT {cols} FROM plan_artifacts "
            "WHERE plan_id IN ({ph}) "
            "ORDER BY plan_id ASC, artifact_type ASC".format(
                cols=", ".join(self._COLUMNS), ph=placeholders
            ),
            tuple(plan_ids),
        )
        for row in cur.fetchall():
            record = dict(zip(self._COLUMNS, row))
            pid = record["plan_id"]
            # Use ``setdefault`` rather than ``[pid] += ...`` so
            # plans that were NOT in the input (e.g. appeared
            # through a join elsewhere) are silently dropped — the
            # contract is "input plan_ids -> result".
            result.setdefault(pid, []).append(record)
        return result



    # ------------------------------------------------------------------
    # Body read path (VP-005)
    # ------------------------------------------------------------------

    def _read_body(self, plan_id: str, artifact_type: str) -> Optional[dict]:
        """Load the artifact body JSON from its stored ``file_path``.

        Resolves the on-disk path through :meth:`find` so the read
        always honours the ``plan_artifacts`` row (the pointer) rather
        than reconstructing ``plan_dir / "{artifact_type}.json"`` at
        the call site. Returns ``None`` when there is no row, when
        the file is missing, or when the body cannot be parsed as
        JSON — the caller treats this as "not available" and falls
        back to empty defaults.
        """
        row = self.find(plan_id, artifact_type)
        if not row:
            return None
        file_path = row.get("file_path")
        if not file_path:
            return None
        path = Path(file_path)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def query_interview(self, plan_id: str) -> Optional[dict]:
        """Return the parsed ``interview.json`` body, or ``None``.

        Thin convenience wrapper over :meth:`_read_body` for the
        ``"interview"`` artifact kind. Replaces the legacy
        ``open(plan_dir / "interview.json")`` pattern at the
        server.py call site (VP-005).
        """
        return self._read_body(plan_id, "interview")

    def query_prd(self, plan_id: str) -> Optional[dict]:
        """Return the parsed ``prd.json`` body, or ``None``.

        Thin convenience wrapper over :meth:`_read_body` for the
        ``"prd"`` artifact kind. Replaces the legacy
        ``open(plan_dir / "prd.json")`` pattern at the
        server.py call site (VP-005).
        """
        return self._read_body(plan_id, "prd")

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def mark_stale(self, plan_id: str, artifact_type: str) -> None:
        """Flip the artifact's status to ``"stale"`` in place.

        Only the ``status`` and ``updated_at`` columns are touched;
        ``file_path`` and ``content_hash`` are preserved. This is
        the canonical "the on-disk body has been re-generated and
        the cached pointer is now stale" transition.
        """
        self._set_status(plan_id, artifact_type, "stale")

    def mark_missing(self, plan_id: str, artifact_type: str) -> None:
        """Flip the artifact's status to ``"missing"`` in place.

        Used when the writer layer discovers the body is gone (or
        was never written). Like :meth:`mark_stale`, only the
        status column changes.
        """
        self._set_status(plan_id, artifact_type, "missing")

    def _set_status(
        self, plan_id: str, artifact_type: str, new_status: str
    ) -> None:
        """Shared update path for ``mark_stale`` / ``mark_missing``.

        The status enum check is intentionally NOT repeated here —
        ``mark_stale`` / ``mark_missing`` pass a hard-coded string
        that is in :data:`VALID_STATUSES` by construction. If a
        future contributor adds a new mark_* method they MUST
        validate the new status value before calling this helper.
        """
        if new_status not in VALID_STATUSES:
            raise ValueError(
                f"internal: _set_status called with invalid status "
                f"{new_status!r}; must be one of {sorted(VALID_STATUSES)}"
            )
        now = _utcnow_iso()
        self._conn.execute(
            "UPDATE plan_artifacts "
            "SET status = ?, updated_at = ? "
            "WHERE plan_id = ? AND artifact_type = ?",
            (new_status, now, plan_id, artifact_type),
        )
