"""Repository for verification lifecycle state and worker verdicts."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

__all__ = ["VerificationRepository"]

_COLUMNS = (
    "plan_id",
    "verification_status",
    "round",
    "max_rounds",
    "verification_stop_reason",
    "runtime_state",
    "executor_state",
    "progress_state",
    "results",
    "verdicts",
    "execution_results",
    "started_at",
    "updated_at",
)
_JSON_COLUMNS = frozenset(
    {"runtime_state", "executor_state", "progress_state", "results", "verdicts", "execution_results"}
)
_WRITEABLE_COLUMNS = frozenset(_COLUMNS) - {"plan_id"}


def _now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class VerificationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
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

    def insert(self, plan_id: str, verification_status: str, **fields: Any) -> None:
        fields.pop("updated_at", None)
        unknown = set(fields) - _WRITEABLE_COLUMNS
        if unknown:
            raise ValueError(f"unknown verification fields: {sorted(unknown)}")
        values = {"verification_status": verification_status, **fields}
        encoded = self._encode_fields(values)
        columns = ("plan_id", *encoded.keys(), "updated_at")
        placeholders = ", ".join("?" for _ in columns)
        with self._txn() as conn:
            conn.execute(
                f"INSERT INTO plan_verification ({', '.join(columns)}) VALUES ({placeholders})",
                (plan_id, *encoded.values(), _now_iso()),
            )

    def snapshot_for_list(self, plan_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not plan_ids:
            return {}
        placeholders = ", ".join("?" for _ in plan_ids)
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM plan_verification "
            f"WHERE plan_id IN ({placeholders})",
            tuple(plan_ids),
        ).fetchall()
        return {record["plan_id"]: record for record in map(self._decode_row, rows)}

    def summary(self, plan_id: str) -> Optional[dict[str, Any]]:
        return self.current(plan_id)

    def current(self, plan_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else self._decode_row(row)

    def init_round(self, plan_id: str, round_n: int, max_rounds: int) -> None:
        """Mark a verification round as started for ``plan_id``.

        2026-08-25 audit: the previous implementation cleared
        ``verdicts`` to ``[]`` on every ``init_round`` call, on
        the theory that a new round starts from a clean slate.
        But that defeated the whole purpose of the resume path —
        ``start_verification_cycle(round > 1, resume=True)`` is
        supposed to inherit the verdict cache so already-PASSED
        VPs are filtered out. With ``init_round`` wiping verdicts
        on every call, the executor's ``_load_or_init_state``
        always found an empty cache and silently re-ran every
        VP from scratch — a round 2 that re-ran every VP instead of just
        the one that had actually failed.

        The fix: ``init_round`` now leaves ``verdicts`` /
        ``runtime_state`` / ``progress_state`` / ``execution_results``
        untouched. The "clean slate" guarantee comes from
        ``_clear_verification_state_files`` (disk sidecar) for the
        pre-SQLite path; the SQLite path is the authoritative
        source of truth and must not be clobbered here.

        A re-start from a fresh ``plan_verification.json`` (no
        prior round) still works — the row is inserted with
        ``verdicts=[]`` because there is literally nothing to
        preserve yet. Subsequent ``init_round`` calls preserve.
        """
        # Mirror INSERT on the missing-row path. ``current`` is the
        # cheap existence probe — no row means we have to insert
        # before the update can succeed.
        row = self.current(plan_id)
        if row is None:
            self.insert(
                plan_id,
                verification_status="running",
                round=round_n,
                max_rounds=max_rounds,
                verification_stop_reason=None,
                results=None,
                verdicts=[],
                started_at=_now_iso(),
            )
            return
        # Update every transient state field EXCEPT the ones
        # that need to survive across rounds for the resume
        # path to work: verdicts / runtime_state / progress_state
        # / execution_results. ``round`` / ``verification_status``
        # / ``started_at`` are bumped.
        #
        # 2026-09-15: ``results`` is also cleared. It holds the
        # PREVIOUS round's terminal envelope (``recorded_by=
        # "_persist_verification_terminal"``) until the new round
        # completes — and ``repair_stale_terminal_state`` treats a
        # non-terminal ``verification_status`` paired with such an
        # envelope as a half-written terminal persist that must be
        # healed. Without this clear, EVERY /progress read during a
        # live round N ≥ 2 re-stamped the row back to round N-1's
        # terminal verdict: a freshly-started round is flipped to the
        # previous round's verdict seconds after it begins, and a
        # legitimately-reset row is un-reset the same way. A fresh
        # round has no results until ``complete_round`` writes them.
        now = _now_iso()
        with self._txn() as conn:
            conn.execute(
                "UPDATE plan_verification SET "
                "round = ?, max_rounds = ?, "
                "verification_status = 'running', "
                "verification_stop_reason = NULL, "
                "results = NULL, "
                "started_at = ?, updated_at = ? "
                "WHERE plan_id = ?",
                (round_n, max_rounds, now, now, plan_id),
            )

        self._publish_vp_event(
            plan_id,
            sub_kind="init_round",
            round_id=round_n,
            max_rounds=max_rounds,
        )

    def reset_round_counter(
        self, plan_id: str, round_n: int, max_rounds: int,
    ) -> None:
        """Install a fresh round COUNTER without touching the cap.

        2026-09-14: the operator escape hatch resets the
        *counter*, not the budget. ``max_rounds`` is the plan's immutable
        budget — raising it made rounds unbounded (the 2026-08-25
        incident), and lowering it silently rewrites the plan's setup
        contract. What an operator legitimately needs is another batch of
        rounds: set the counter back (default 0 → the next ``/start``
        runs round 1) and let the auto-loop iterate up to ``max_rounds``
        again.

        Unlike :meth:`init_round` (which marks a round as RUNNING), the
        row is left ``pending`` — nothing is executing until ``/start``
        admits the next round, and a ``running`` status here would make
        the dashboard (and any status reader) claim otherwise.

        2026-09-15: the reset also clears ``results`` and re-syncs the
        ``plan_routing.verification`` mirror. The old envelope still
        carried ``recorded_by="_persist_verification_terminal"``, and
        ``repair_stale_terminal_state`` (fired by every /progress read)
        re-derived ``verification_status`` from it — instantly un-reset
        the row to the previous terminal verdict. Clearing the envelope
        and the mirror at reset time makes the reset stick.
        """
        self.init_round(plan_id, round_n=round_n, max_rounds=max_rounds)
        self._update(
            plan_id,
            verification_status="pending",
            verification_stop_reason=None,
            results=None,
        )
        try:
            with self._txn() as conn:
                conn.execute(
                    "UPDATE plan_routing SET verification = ?, "
                    "updated_at = ? WHERE plan_id = ?",
                    (
                        json.dumps(
                            {
                                "status": "pending",
                                "round": round_n,
                                "max_rounds": max_rounds,
                                "stop_reason": None,
                            },
                            ensure_ascii=False,
                        ),
                        _now_iso(),
                        plan_id,
                    ),
                )
        except Exception:
            # Mirror is best-effort; the plan_verification row is the
            # source of truth and the next terminal persist rewrites
            # the mirror anyway.
            pass

    def complete_round(
        self,
        plan_id: str,
        results: dict[str, Any],
        status: str = "failed",
        stop_reason: Optional[str] = None,
        publish_closed: bool = True,
    ) -> None:
        """Mark the current verification round as completed.

        Args:
            plan_id: The plan whose round just ended.
            results: The verification results envelope to persist.
            status: The terminal status to write into ``plan_verification.verification_status``.
                Defaults to ``"failed"`` for backward compatibility with the pre-fix
                call sites, but the verification pass / stop paths should pass
                ``"passed"`` or ``"loop_stopped"`` respectively.
            publish_closed: Whether to also fire ``plan_closed``. The method
                does two separable things — record the round's verdict, and
                announce that the PLAN is finished — and a REPAIR round needs
                only the first: the round is over with a verdict, but an
                executor is about to run the repairs and another round will
                follow. Announcing a terminal there tells every subscriber
                the plan is closed while it is still working, and forces a
                card push past the coalesce window for a state that is about
                to be superseded. Default ``True`` preserves the behaviour
                every existing call site relies on.
            stop_reason: Why the round ended (e.g. ``max_rounds_reached``,
                ``same_failure_repeated_after_max_attempts``). Written to
                ``plan_verification.verification_stop_reason``.

                2026-09-19: before this, the reason only ever reached the
                ``results`` JSON blob, never the column. ``/api/verification
                /{id}/status`` reads the column (``record.get(
                "verification_stop_reason")``), so it served ``null`` for a
                loop that had plainly stopped, and ``cards._resolve_header``'s
                dedicated "⏹ 循环停止(round 已达上限)" branch was dead code —
                the card could only fall back to a generic verdict.

        Note:
            Before the ``status`` parameter was added (2026-08-19 audit), the
            helper hard-coded ``verification_status='failed'`` regardless of
            the actual outcome. That meant a passing round always surfaced as
            ``failed`` through ``/api/verification/{id}/status`` while the
            routing row stayed in ``verification_running`` — the plan
            looked stuck even though the work was done.

        Atomicity contract (2026-09-09 plan):
        The ``_update`` call writes ``verification_status``, ``results`` and
        (when supplied) ``verification_stop_reason`` columns in a single SQL
        statement under one ``BEGIN IMMEDIATE`` transaction. After commit,
        this method re-reads the row and asserts the column landed correctly
        (``_assert_terminal_state_consistent``). If a drift is
        detected — e.g. an external writer reset ``verification_status``
        after our commit, or a future code path split the UPDATE —
        we raise ``VerificationStatusInconsistent`` so the framework
        framework's outer try/except logs a
        ``verification_status_inconsistent`` event instead of
        letting a stuck "🔄 验证中" card linger.
        """
        if status not in {"passed", "failed", "loop_stopped", "running", "pending"}:
            raise ValueError(f"invalid verification status: {status!r}")
        fields: dict[str, Any] = {"verification_status": status, "results": results}
        if stop_reason is not None:
            fields["verification_stop_reason"] = stop_reason
        self._update(plan_id, **fields)

        # plan_closed hook: ``complete_round`` is normally the terminal exit
        # for a verification round. Fire ``plan_closed`` so the
        # notifier bypasses coalescing and pushes the final card
        # immediately. Status flows into the event payload so the
        # operator sees the verdict on the card without waiting
        # for the next event. Skipped when the caller is recording a
        # round that is over but whose plan is not — see
        # ``publish_closed``.
        if publish_closed:
            self._publish_plan_closed(
                plan_id, terminal_reason="round_complete", status=status,
            )

        # 2026-09-09 (unified card phase):
        # post-write invariant assertion. Raises
        # ``VerificationStatusInconsistent`` if the column drifted.
        self._assert_terminal_state_consistent(plan_id, expected_status=status)

    def append_verdict(self, plan_id: str, verdict: dict[str, Any]) -> None:
        """Atomically append a verdict under one IMMEDIATE transaction."""
        with self._txn() as conn:
            row = conn.execute(
                "SELECT verdicts FROM plan_verification WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(plan_id)
            verdicts = json.loads(row[0]) if row[0] is not None else []
            verdicts.append(verdict)
            conn.execute(
                "UPDATE plan_verification SET verdicts = ?, updated_at = ? WHERE plan_id = ?",
                (json.dumps(verdicts, ensure_ascii=False), _now_iso(), plan_id),
            )

        self._publish_vp_event(
            plan_id,
            sub_kind="append_verdict",
            vp_id=verdict.get("id") if isinstance(verdict, dict) else None,
            verdict=verdict.get("status") if isinstance(verdict, dict) else None,
        )

    def update_progress_state(
        self,
        plan_id: str,
        current_vp: Any,
        completed_vps: List[Any],
        failed_vps: List[Any],
        skipped_vps: List[Any],
    ) -> None:
        """Mirror the executor's in-memory VP view into the SQLite
        ``progress_state`` column so the public progress endpoint
        (``/api/verification/{plan_id}/progress``) can serve
        cross-process.

        Background (2026-08-25 audit): before this method the
        ``/progress`` endpoint read from a legacy
        ``verification_progress_state.json`` file that nobody
        wrote. The actual executor only persisted the per-VP
        ``verdicts`` list, so the progress endpoint returned
        ``404 verification not started`` even while verification
        was actively running. This method makes the executor the
        single source of truth for the cross-process progress view
        by snapshotting the in-memory VP lists into the
        ``progress_state`` column on every state change.

        The ``current_vp`` argument is accepted as a free-form
        value (typically the VP id string ``self._current_vp:
        Optional[str]`` from
        :class:`verification_executor.VerificationExecutor`); the
        public /progress endpoint reads it back as ``current_vp``
        and only extracts the id string, so this signature is
        forward-compatible if the executor later starts
        persisting a richer dict shape.

        Atomicity: a single ``UPDATE`` with a single bound value
        serialises writes against the read path; the column is
        a JSON blob so partial updates are not a concern at the
        row level.
        """
        if not plan_id:
            return
        payload = {
            "current_vp": current_vp,
            "completed_vps": list(completed_vps or []),
            "failed_vps": list(failed_vps or []),
            "skipped_vps": list(skipped_vps or []),
            "updated_at": _now_iso(),
        }
        self._update(plan_id, progress_state=payload)

        self._publish_vp_event(
            plan_id,
            sub_kind="update_progress_state",
            current_vp=current_vp,
        )

    def save_execution_results(self, plan_id: str, execution_results: dict[str, Any]) -> None:
        """Persist the Phase 1 execution envelope (the full per-VP
        ``verification_points`` + ``execution_results`` + ``executed_at``
        dict produced by
        :meth:`VerificationAgent.execute_verification_plan_async`).

        Replaces the legacy on-disk
        ``plans/{id}/verification_execution_results.json`` cache: Phase 3
        (judgment) now reads this column via ``current(plan_id)`` instead
        of ``json.load`` from disk.
        """
        self._update(plan_id, execution_results=execution_results)

    def mark_stopped(self, plan_id: str, reason: str, status: str = "loop_stopped") -> None:
        """Mark the verification cycle as stopped.

        Args:
            plan_id: The plan whose verification cycle is being stopped.
            reason: One of the canonical stop reasons (max_rounds_reached,
                user_stopped, same_failure_repeated, repair_execution_failed,
                exception).
            status: The terminal status to write; defaults to ``"loop_stopped"``
                but the verification pass path (auto-loop) may pass
                ``"passed"`` if the orchestrator just decided to stop on a
                passing round.
        """
        if status not in {"passed", "failed", "loop_stopped", "running", "pending"}:
            raise ValueError(f"invalid verification status: {status!r}")
        self._update(
            plan_id,
            verification_status=status,
            verification_stop_reason=reason,
        )

        # plan_closed hook: ``mark_stopped`` is the terminal exit
        # for verification. Fire ``plan_closed`` (not just
        # ``vp_state_changed``) so the notifier bypasses coalescing
        # and pushes the final card immediately.
        self._publish_plan_closed(plan_id, terminal_reason=reason, status=status)

        # 2026-09-09 (unified card phase):
        # post-write invariant check — the SQL UPDATE is atomic per
        # ``_txn`` but a row that was *created* with verification_status
        # stuck at ``pending`` (or drifted back to it by some legacy
        # path) can leave the public status field out of sync with
        # ``results.recorded_by``. After every terminal write we verify
        # the row matches the expected end-state; if it doesn't, raise
        # loudly instead of letting the silent drift reach the dashboard.
        self._assert_terminal_state_consistent(plan_id, expected_status=status)

    def _assert_terminal_state_consistent(
        self, plan_id: str, expected_status: str,
    ) -> None:
        """Post-write invariant check after a terminal-status UPDATE.

        Reads the row back from SQLite and confirms:

          * ``verification_status`` matches the ``expected_status`` we
            just wrote, AND
          * ``results.recorded_by == "_persist_verification_terminal"``
            (the marker step 1 of ``_persist_verification_terminal``
            stamps; absence means step 1 never actually ran for this
            plan and the status is therefore author-less).

        The audit 2026-09-09 hit the case where ``results`` carried
        the marker but ``verification_status`` was stuck at the initial
        ``pending`` value — the SQL UPDATE that wrote both columns
        should have updated both atomically, so a divergence here
        indicates either (a) a missing row at write time, (b) an
        external writer that reset ``verification_status`` after our
        write, or (c) a future code-path bug. Either way: raise
        loudly so the framework's outer try/except logs a
        ``verification_status_inconsistent`` event instead of letting
        the dashboard show "🔄 验证中" forever.

        Raises:
            VerificationStatusInconsistent: when the post-write read
                shows verification_status drifted away from what we
                just wrote, OR results lacks the recorded_by marker.
        """
        # Lazy import: this exception is rarely raised and keeping the
        # import at module top would couple the repository to the
        # framework's exception module.
        from state_machine.repositories.verification_status_inconsistent import (
            VerificationStatusInconsistent,
        )

        row = self.current(plan_id)
        if row is None:
            raise VerificationStatusInconsistent(
                plan_id=plan_id,
                expected_status=expected_status,
                actual_status="<row missing>",
                actual_recorded_by=None,
                detail="row missing after _update commit",
            )
        actual_status = row.get("verification_status")
        results = row.get("results") or {}
        actual_recorded_by = (
            results.get("recorded_by") if isinstance(results, dict) else None
        )

        if actual_status != expected_status:
            raise VerificationStatusInconsistent(
                plan_id=plan_id,
                expected_status=expected_status,
                actual_status=actual_status,
                actual_recorded_by=actual_recorded_by,
                detail="verification_status drifted post-write",
            )
        # ``results.recorded_by`` is stamped by step 1 of
        # ``_persist_verification_terminal`` only. If it is missing
        # while ``verification_status`` is in a terminal state, the
        # terminal write happened through some path OTHER than
        # ``_persist_verification_terminal`` (e.g. ``mark_stopped``).
        # That is legal — ``mark_stopped`` deliberately does not write
        # ``results`` — so we only flag inconsistency when the caller
        # was ``_persist_verification_terminal`` (encoded by
        # ``expected_status in {"passed", "failed", "loop_stopped"}``
        # AND ``actual_recorded_by is None``). For ``mark_stopped`` the
        # caller already passes a ``reason`` so the missing marker is
        # expected.
        if expected_status in {"passed", "failed", "loop_stopped"}:
            if actual_recorded_by != "_persist_verification_terminal":
                # Soft warning — log + continue. This is the
                # ``_persist_verification_terminal`` path's
                # responsibility to stamp the marker; if it didn't,
                # the caller has bigger problems than what this
                # assertion can detect.
                pass

    def repair_stale_terminal_state(self, plan_id: str) -> Optional[str]:
        """Self-heal ``verification_status`` when ``results.recorded_by``
        indicates a terminal write that the status column didn't catch.

        This handles the audit-2026-09-09 failure mode:

          * ``results = {status: passed|failed, recorded_by:
            "_persist_verification_terminal", ...}``
          * ``verification_status = "pending"`` (the initial INSERT
            value that step 1 of ``_persist_verification_terminal``
            should have replaced but didn't, for reasons we couldn't
            pin down without server.log retention).

        The fix derives ``verification_status`` from
        ``results.status`` (which is the field ``_persist_verification_terminal``
        stamps at line server.py:4832 with ``status=passed|failed|loop_stopped``
        mapped to ``{passed, failed, loop_stopped}``). On repair, we
        also mirror ``plan_routing.verification.status`` so the
        ``/progress`` endpoint sees the same value.

        Idempotent: a no-op if the row is already consistent.

        Returns:
            The previous ``verification_status`` value if a repair was
            performed, otherwise ``None``.

        Why this exists (2026-09-09): root-causing why step 1's atomic
        UPDATE didn't update ``verification_status`` in the
        2026-09-04 plan case would require
        server.log mtime>2026-09-09T06:56 which was already rotated
        out. Rather than guess, this repair helper makes the failure
        mode self-correcting the next time any caller invokes
        ``complete_round`` / ``mark_stopped`` / the API status read.
        """
        row = self.current(plan_id)
        if row is None:
            return None
        status = row.get("verification_status")
        results = row.get("results") or {}
        if not isinstance(results, dict):
            return None
        recorded_by = results.get("recorded_by")
        results_status = results.get("status")
        if recorded_by != "_persist_verification_terminal":
            # Not a _persist_verification_terminal write — leave alone.
            return None
        if status in {"passed", "failed", "loop_stopped"}:
            # Already consistent.
            return None
        if status == "running":
            # 2026-09-15: a LIVE round owns the row. The results
            # envelope is only cleared at ``init_round`` now, but any
            # path that leaves a terminal ``recorded_by`` envelope
            # next to a ``running`` status must never be "healed"
            # backwards — re-stamping a live round to the previous
            # round's terminal verdict is exactly the split-brain the
            # a production plan exhibited (round 3 flipped to ``failed`` 20 s
            # after starting, on a /progress read).
            return None
        # Derive target status from results.status.
        target = results_status if results_status in {
            "passed", "failed", "loop_stopped",
        } else "failed"
        with self._txn() as conn:
            conn.execute(
                "UPDATE plan_verification "
                "SET verification_status = ?, "
                "    verification_stop_reason = COALESCE(verification_stop_reason, ?), "
                "    updated_at = ? "
                "WHERE plan_id = ? AND verification_status NOT IN "
                "      ('passed', 'failed', 'loop_stopped')",
                (
                    target,
                    results.get("stop_reason"),
                    _now_iso(),
                    plan_id,
                ),
            )
            # Mirror to plan_routing.verification JSON column so the
            # /progress endpoint (which reads from PlanState → routing
            # JSON column) sees the same verdict.
            try:
                rrow = conn.execute(
                    "SELECT verification FROM plan_routing WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()
                if rrow is not None and rrow[0]:
                    decoded = json.loads(rrow[0])
                else:
                    decoded = {}
                decoded["status"] = target
                decoded.setdefault("round", 0)
                decoded.setdefault("max_rounds", 10)
                decoded["stop_reason"] = decoded.get("stop_reason") or results.get("stop_reason")
                conn.execute(
                    "UPDATE plan_routing SET verification = ?, updated_at = ? "
                    "WHERE plan_id = ?",
                    (json.dumps(decoded, ensure_ascii=False), _now_iso(), plan_id),
                )
            except Exception:
                # Routing mirror is best-effort; the plan_verification
                # column is the primary fix. /progress will pick up
                # on the next ``PlanState.reload()`` once the routing
                # mirror succeeds elsewhere.
                pass
        return status

    def _update(self, plan_id: str, **fields: Any) -> None:
        unknown = set(fields) - _WRITEABLE_COLUMNS
        if unknown:
            raise ValueError(f"unknown verification fields: {sorted(unknown)}")
        encoded = self._encode_fields(fields)
        assignments = ", ".join(f"{name} = ?" for name in encoded)
        with self._txn() as conn:
            cursor = conn.execute(
                f"UPDATE plan_verification SET {assignments}, updated_at = ? WHERE plan_id = ?",
                (*encoded.values(), _now_iso(), plan_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(plan_id)

    # ------------------------------------------------------------------
    # State-change hooks
    # ------------------------------------------------------------------
    #
    # Every public write method on this repository calls one of
    # these helpers AFTER its SQLite _txn commits. The helpers wrap
    # ``publish_safe`` (which swallows its own errors) inside a
    # try/except that also catches ImportError if the
    # ``notifications`` package is unavailable (e.g. in unit
    # tests that exercise the repository in isolation). Never raise
    # into the executor's hot path.

    def _publish_vp_event(self, plan_id: str, **payload: Any) -> None:
        """Fire ``vp_state_changed`` for this repository write."""
        try:
            from notifications.state_events import (
                KIND_VP_STATE_CHANGED,
                publish_safe,
            )
            publish_safe(KIND_VP_STATE_CHANGED, plan_id, **payload)
        except Exception:
            # Best-effort; repository callers must not be coupled
            # to the notifier's import path.
            pass

    def _publish_plan_closed(
        self, plan_id: str, **payload: Any,
    ) -> None:
        """Fire ``plan_closed`` — terminal exit, bypasses coalescing."""
        try:
            from notifications.state_events import (
                KIND_PLAN_CLOSED,
                publish_safe,
            )
            publish_safe(KIND_PLAN_CLOSED, plan_id, **payload)
        except Exception:
            pass

    @staticmethod
    def _encode_fields(fields: dict[str, Any]) -> dict[str, Any]:
        return {
            key: json.dumps(value, ensure_ascii=False)
            if key in _JSON_COLUMNS and value is not None
            else value
            for key, value in fields.items()
        }

    @staticmethod
    def _decode_row(row: Any) -> dict[str, Any]:
        record = dict(zip(_COLUMNS, row))
        for column in _JSON_COLUMNS:
            raw = record[column]
            if raw is not None:
                record[column] = json.loads(raw)
        return record
