"""Consistency invariants for the state-machine SQLite database.

This module implements :func:`assert_db_consistent`, the single
authoritative check that every crash-recovery / kill-point test in
``tests/unit/test_crash_recovery.py`` runs after the cold-start
replay. The five invariants (I1-I5) are the architecture decision
point 5 + test design decision point 5 contract — they cover every
table the state-machine refactor owns and every state-machine
invariant the bug 1-4 anchors pin.

The five invariants
-------------------

  I1: stage vs verification status semantic compatibility.

    The ``plan_routing.current_phase`` value (e.g. ``executing``,
    ``prd_review``, ``verification_running``) and the
    ``plan_verification.verification_status`` value
    (e.g. ``pending``, ``running``, ``passed``, ``failed``,
    ``loop_stopped``) MUST be jointly compatible. Concretely:

      * If ``stage == "verification_running"`` then
        ``verification_status`` MUST be ``"running"`` (the
        verification worker is the active writer).
      * If ``stage == "verification_passed"`` then
        ``verification_status`` MUST be ``"passed"``.
      * If ``stage == "verification_failed"`` then
        ``verification_status`` MUST be ``"failed"``.
      * If ``stage == "verification_loop_stopped"`` then
        ``verification_status`` MUST be ``"loop_stopped"``.

    Any other (stage, verification_status) pair is a recoverable
    "in-flight" combination and MUST NOT raise. Only the
    semantically contradictory pair — "we are running the verifier"
    but "the verifier reports passed" — fails the assertion.

  I2: no CAS committed but business table not written (orphan).

    A successful CAS on ``plan_routing`` bumps the row's
    ``version`` and rewrites ``stage``/``substage``. If the same
    transition that triggered the CAS should have written a row in
    one of the sibling tables (``plan_execution``,
    ``plan_verification``), the sibling row MUST exist. Concretely:

      * Every plan_id that has a row in ``plan_routing`` and
        whose ``stage ∈ {"executing", "verification_running",
        "verification_passed", "verification_failed",
        "verification_loop_stopped"}`` MUST also have a row in
        ``plan_execution``.

      * Every plan_id whose ``stage ∈ {"verification_running",
        "verification_passed", "verification_failed",
        "verification_loop_stopped"}`` MUST also have a row in
        ``plan_verification``.

    The orphan invariant is the bug-3 anchor: a successful CAS
    followed by a crash before the business table is written
    would otherwise leave the routing layer pointing at a
    plan that does not exist in the other tables.

  I3: version monotonic, no gaps, no inversions.

    The ``version`` column on ``plan_routing`` is the optimistic
    lock token. Across the entire ``plan_routing`` table:

      * For any given plan_id, successive ``updated_at`` rows
        MUST have strictly non-decreasing ``version`` values
        when sorted by ``updated_at``.
      * The ``updated_at`` sequence MUST be non-decreasing.

    (The pre-refactor implementation tracked version in-memory
    and occasionally rewrote the row with a stale version after
    recovery — bug 3 anchor.)

  I4: no plan in two mutually exclusive activity states.

    The state machine forbids the two truly impossible pairs:

      1. ``plan_verification.verification_status`` is in
         ``{passed, failed, loop_stopped}`` (a terminal state)
         AND the routing layer STILL claims the plan is in
         an executor-owned stage (``executing``, ``ready``,
         ``tasks_done``).  The terminal verification result must
         have advanced the routing stage to the matching
         verification-terminal stage — leaving the plan in an
         executor-owned stage means the executor has not yet
         observed the verdict.

      2. ``plan_execution.current_phase`` is in a verification-
         owned phase AND the routing layer is in an executor-
         owned stage AND ``plan_verification.verification_status``
         is ``"pending"``.  Pending means the verification round
         has not started; an executor-owned routing stage with
         a verification-owned current_phase means the two
         layers disagree about which worker is active.  Active
         (non-pending) verification_status is allowed to coexist
         with an executor-owned routing stage because the
         dispatcher's normal transition window briefly puts the
         plan in this state.

    The check is deliberately NOT a full "no overlap" invariant
    — that would flag every normal mid-transition. The check
    pins only the two truly impossible states described above.
    The CAS layer (routing.try_mark_phase) is responsible for
    preventing concurrent activity on the LIVE path; I4 is a
    post-hoc "the result is still self-consistent after a crash"
    check.

  I5: plan_artifacts pointer integrity.

    Every ``plan_artifacts`` row whose ``status == "generated"``
    MUST have a non-empty ``file_path`` and a non-empty
    ``content_hash``.  (The repository validates the status enum
    but does not require the pointer + hash columns to be non-NULL;
    this invariant closes that gap.)

    The integrity check is *table-only* — we do NOT verify the
    file exists on disk because the on-disk layout is not
    available in unit tests.  Production callers can layer an
    additional file-existence check on top of this function if
    needed.

Design notes
------------

The implementation is deliberately a single top-level function
(:func:`assert_db_consistent`) rather than a class. The crash-
recovery tests call it many times across the 5 kill points and
want the assertion failure to point at the invariant that failed
without an extra class hierarchy to navigate. A list of
``(code, message)`` tuples is returned for tests that want to
inspect the failure mode; the default ``raise=True`` raises
:class:`ConsistencyInvariantViolation` (a subclass of
:class:`AssertionError` so ``pytest`` reports it cleanly).

The invariant evaluator opens its OWN connection against ``db_path``
in autocommit mode so it does not interfere with any in-flight
transaction held by the crash-recovery test. This also matches
the "cold-start replay" semantics: the test simulates a process
restart, so the invariants are checked against the on-disk
database file rather than the live in-memory state of the test
process's connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

__all__ = [
    "assert_db_consistent",
    "ConsistencyInvariantViolation",
]


DbPath = Union[str, Path]


class ConsistencyInvariantViolation(AssertionError):
    """Raised by :func:`assert_db_consistent` when an invariant fails.

    Subclassing :class:`AssertionError` (rather than introducing a
    new exception type) keeps ``pytest`` happy: a ``fail()``-style
    message is shown in the test report without any extra
    exception-class plumbing.
    """

    def __init__(self, invariant_code: str, message: str) -> None:
        self.invariant_code = invariant_code
        super().__init__(f"[{invariant_code}] {message}")


# Routing stages that imply a verification worker is in the loop.
# Used by I1 (semantic compatibility) and I4 (mutually exclusive
# activity states). Kept as a module-level frozenset so reflection /
# grep can audit the cross-references.
_VERIFICATION_STAGES: frozenset[str] = frozenset(
    {
        "verification_running",
        "verification_passed",
        "verification_failed",
        "verification_loop_stopped",
    }
)

# (stage, verification_status) → True means compatible (no I1 violation).
# The mapping is intentionally restrictive: only the 4 "the verifier is
# the active worker" stages are pinned; any other stage (executing,
# ready, prd_review, …) is paired with whatever verification_status
# happens to be (typically "pending" or absent) and is NOT an I1 failure.
_STAGE_TO_VERIFICATION_STATUS = {
    "verification_running": "running",
    "verification_passed": "passed",
    "verification_failed": "failed",
    "verification_loop_stopped": "loop_stopped",
}


def _open_readonly(db_path: DbPath) -> sqlite3.Connection:
    """Open a read-only autocommit connection against ``db_path``.

    Read-only is sufficient for invariant evaluation and avoids
    accidental writes if a future contributor wires the helper
    into a larger code path.  The connection is in autocommit mode
    (``isolation_level = None``) — same contract as the production
    ``state_machine.db.connection.open`` helper, so pragma shape
    matches.
    """
    path_str = str(db_path)
    if not path_str:
        raise ValueError("db_path must not be empty")

    # ``uri=True`` enables the ``file:`` URI scheme; ``mode=ro`` opens
    # read-only. This is the canonical SQLite read-only mode and avoids
    # creating a WAL sidecar that would interfere with the test's
    # main connection.
    uri = f"file:{path_str}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
    except sqlite3.OperationalError as exc:
        # If the DB does not exist yet, surface a clear error rather
        # than letting the SELECT raise mid-evaluation.
        raise FileNotFoundError(
            f"db_path {path_str!r} is not an existing SQLite database: {exc}"
        ) from exc
    return conn


def _check_i1_stage_vs_verification_status(
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Return an error message if I1 fails, else ``None``.

    Walks every plan that has BOTH a ``plan_routing`` and a
    ``plan_verification`` row and asserts the (stage, status) pair
    is semantically compatible.
    """
    cur = conn.execute(
        "SELECT r.plan_id, r.current_phase, v.verification_status "
        "FROM plan_routing r "
        "JOIN plan_verification v ON v.plan_id = r.plan_id"
    )
    for plan_id, routing_phase, verification_status in cur.fetchall():
        expected = _STAGE_TO_VERIFICATION_STATUS.get(routing_phase)
        if expected is None:
            # Non-verification stage — no I1 constraint.
            continue
        if verification_status != expected:
            return (
                f"plan_id={plan_id!r} current_phase={routing_phase!r} requires "
                f"verification_status={expected!r} but found "
                f"verification_status={verification_status!r}"
            )
    return None


def _check_i2_no_cas_orphan(
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Return an error message if I2 fails, else ``None``.

    Enforces:

      * every ``plan_routing`` row whose stage is in the
        ``active`` set MUST have a matching ``plan_execution`` row.
      * every ``plan_routing`` row whose stage is in
        ``_VERIFICATION_STAGES`` MUST have a matching
        ``plan_verification`` row.

    "Active" here means any stage other than the planning phases
    (interview, prd_generation, prd_review, …).  The exact set is
    the union of the executor-side stages (``ready``,
    ``executing``, ``tasks_done``) and the verification stages.
    """
    # 2a: routing → execution
    cur = conn.execute(
        "SELECT r.plan_id, r.current_phase FROM plan_routing r "
        "LEFT JOIN plan_execution e ON e.plan_id = r.plan_id "
        "WHERE e.plan_id IS NULL"
    )
    # Only fail if the stage implies an execution row should exist.
    execution_stages = frozenset({"ready", "executing", "tasks_done"})
    execution_stages |= _VERIFICATION_STAGES
    for plan_id, routing_phase in cur.fetchall():
        if routing_phase in execution_stages:
            return (
                f"plan_id={plan_id!r} has plan_routing.current_phase="
                f"{routing_phase!r} but NO matching plan_execution row "
                f"(orphan CAS)"
            )

    # 2b: routing → verification
    cur = conn.execute(
        "SELECT r.plan_id, r.current_phase FROM plan_routing r "
        "LEFT JOIN plan_verification v ON v.plan_id = r.plan_id "
        "WHERE v.plan_id IS NULL"
    )
    for plan_id, routing_phase in cur.fetchall():
        if routing_phase in _VERIFICATION_STAGES:
            return (
                f"plan_id={plan_id!r} has plan_routing.current_phase="
                f"{routing_phase!r} but NO matching plan_verification row "
                f"(orphan CAS)"
            )
    return None


def _check_i3_version_monotonic(
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Return an error message if I3 fails, else ``None``.

    For every plan_id, walk the row's history (sorted by
    ``updated_at`` ASC) and verify ``version`` is non-decreasing
    and the timestamps are non-decreasing.

    Note: the table only holds the LATEST row per plan_id (it's a
    hot-row design), so this check is effectively "version >= 0
    and updated_at is non-empty". The structure is preserved so
    a future audit-table migration can drop in without changing
    the invariant evaluator.
    """
    cur = conn.execute(
        "SELECT plan_id, version, updated_at FROM plan_routing "
        "ORDER BY plan_id ASC, updated_at ASC"
    )
    last_per_plan: dict[str, tuple[int, str]] = {}
    for plan_id, version, updated_at in cur.fetchall():
        if plan_id not in last_per_plan:
            last_per_plan[plan_id] = (int(version), str(updated_at))
            if int(version) < 0:
                return (
                    f"plan_id={plan_id!r} has negative version={version}"
                )
            continue
        prev_version, prev_updated_at = last_per_plan[plan_id]
        if int(version) < prev_version:
            return (
                f"plan_id={plan_id!r} version went backwards: "
                f"{prev_version} -> {version} (I3 version-monotonic violated)"
            )
        if str(updated_at) < prev_updated_at:
            return (
                f"plan_id={plan_id!r} updated_at went backwards: "
                f"{prev_updated_at!r} -> {updated_at!r}"
            )
        last_per_plan[plan_id] = (int(version), str(updated_at))
    return None


def _check_i4_no_mutually_exclusive_activity(
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Return an error message if I4 fails, else ``None``.

    Two truly-impossible (stage, verification_status, current_phase)
    triples are flagged.  Transient mid-transition states (e.g.
    ``stage='executing'`` AND ``verification_status='running'``
    during a normal dispatch) are NOT flagged.

    Anti-pattern 1: terminal verification result, but routing
    layer still in an executor-owned stage.

        verification_status ∈ {passed, failed, loop_stopped}
        AND stage ∈ {ready, executing, tasks_done}

    Anti-pattern 2: verification-owned current_phase + pending
    verification_status + executor-owned stage.  Pending means the
    round has not started; this triple means the executor is
    pretending to be the verifier.

        current_phase ∈ verification_stages
        AND stage ∈ executor_stages
        AND verification_status == "pending"

    "Verification stages" = :data:`_VERIFICATION_STAGES`.
    "Executor stages" = ``{"ready", "executing", "tasks_done"}``.
    """
    executor_stages = frozenset({"ready", "executing", "tasks_done"})
    terminal_statuses = frozenset({"passed", "failed", "loop_stopped"})

    # Anti-pattern 1: terminal verification status but executor-owned routing.
    cur = conn.execute(
        "SELECT v.plan_id, v.verification_status, r.current_phase "
        "FROM plan_verification v "
        "JOIN plan_routing r ON r.plan_id = v.plan_id"
    )
    for plan_id, verification_status, routing_phase in cur.fetchall():
        if (
            verification_status in terminal_statuses
            and routing_phase in executor_stages
        ):
            return (
                f"plan_id={plan_id!r} verification_status="
                f"{verification_status!r} (terminal) but "
                f"plan_routing.current_phase="
                f"{routing_phase!r} (executor-owned); the routing "
                f"layer has not advanced past the verification result"
            )

    # Anti-pattern 2: verification-owned execution phase + pending
    # verification status + executor-owned routing stage.
    cur = conn.execute(
        "SELECT e.plan_id, e.current_phase, v.verification_status, "
        "       r.current_phase "
        "FROM plan_execution e "
        "JOIN plan_verification v ON v.plan_id = e.plan_id "
        "JOIN plan_routing r ON r.plan_id = e.plan_id"
    )
    # 2026-09-17 (schema v5): ``plan_execution.current_phase`` and
    # ``plan_routing.current_phase`` now share a column NAME but are
    # still two different tables' columns — the executor's own
    # sub-state vs the workflow phase. Bind them to distinct Python
    # names so the comparison below stays legible.
    for plan_id, current_phase, verification_status, routing_phase \
            in cur.fetchall():
        if (
            current_phase in _VERIFICATION_STAGES
            and routing_phase in executor_stages
            and verification_status == "pending"
        ):
            return (
                f"plan_id={plan_id!r} execution.current_phase="
                f"{current_phase!r} + verification_status='pending' + "
                f"plan_routing.current_phase={routing_phase!r} — the "
                f"executor is impersonating the verifier before any "
                f"verification round started"
            )
    return None


def _check_i5_artifact_pointer_integrity(
    conn: sqlite3.Connection,
) -> Optional[str]:
    """Return an error message if I5 fails, else ``None``.

    Every ``plan_artifacts`` row whose ``status == "generated"``
    must have a non-empty ``file_path`` and a non-empty
    ``content_hash``.
    """
    cur = conn.execute(
        "SELECT plan_id, artifact_type, file_path, content_hash "
        "FROM plan_artifacts WHERE status = 'generated'"
    )
    for plan_id, artifact_type, file_path, content_hash in cur.fetchall():
        if file_path is None or file_path == "":
            return (
                f"plan_id={plan_id!r} artifact_type={artifact_type!r} "
                f"status='generated' but file_path is empty"
            )
        if content_hash is None or content_hash == "":
            return (
                f"plan_id={plan_id!r} artifact_type={artifact_type!r} "
                f"status='generated' but content_hash is empty"
            )
    return None


_INVARIANT_CHECKS = (
    ("I1", _check_i1_stage_vs_verification_status),
    ("I2", _check_i2_no_cas_orphan),
    ("I3", _check_i3_version_monotonic),
    ("I4", _check_i4_no_mutually_exclusive_activity),
    ("I5", _check_i5_artifact_pointer_integrity),
)


def assert_db_consistent(
    db_path: DbPath,
    *,
    raise_on_failure: bool = True,
) -> list[tuple[str, str]]:
    """Assert that the on-disk SQLite database at ``db_path`` satisfies
    every state-machine consistency invariant (I1-I5).

    Parameters
    ----------
    db_path:
        Path to the SQLite database to evaluate.  The function
        opens its own **read-only** autocommit connection so it
        does not interfere with any in-flight transaction held
        by the caller.  This matches the "cold-start replay"
        semantics — the caller is expected to simulate a process
        restart and then ask "does the on-disk state satisfy
        every invariant?"
    raise_on_failure:
        When ``True`` (default), any invariant violation raises
        :class:`ConsistencyInvariantViolation`.  When ``False``,
        the function returns the list of ``(invariant_code,
        message)`` tuples that failed (empty list on success).
        The latter is convenient for tests that want to
        introspect the failure mode without try/except.

    Returns
    -------
    list[tuple[str, str]]
        Empty list when every invariant holds.  Otherwise a list
        of ``(invariant_code, message)`` tuples — one entry per
        failed invariant.  The list is short-circuited at the
        first failure per invariant; multiple invariant failures
        return multiple tuples.

    Raises
    ------
    ConsistencyInvariantViolation
        Subclass of ``AssertionError``.  Raised when at least one
        invariant fails AND ``raise_on_failure`` is ``True``.
    FileNotFoundError
        When ``db_path`` does not exist or is not a SQLite file.
    """
    conn = _open_readonly(db_path)
    try:
        failures: list[tuple[str, str]] = []
        for code, check in _INVARIANT_CHECKS:
            try:
                failure = check(conn)
            except sqlite3.OperationalError as exc:
                # A missing table means the schema was not migrated
                # yet — surface that as an invariant failure so
                # crash-recovery tests catch "the DB is empty after
                # replay" as a failure mode.
                failure = f"invariant evaluator raised {type(exc).__name__}: {exc}"
            if failure is not None:
                failures.append((code, failure))
                if raise_on_failure:
                    raise ConsistencyInvariantViolation(code, failure)
        return failures
    finally:
        try:
            conn.close()
        except sqlite3.OperationalError:
            pass
