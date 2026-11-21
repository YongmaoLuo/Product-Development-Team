"""The status snapshot's verification fields have exactly one source.

``GET /api/plan/{id}/status`` answers with four verification fields —
``verification_status`` / ``verification_round`` /
``verification_max_rounds`` / ``verification_stop_reason`` — and the
card header is a pure function of them.  A stale value in them puts a
wrong word on the operator's card, above a body that says something
else.

They used to have TWO sources.  ``plan_verification`` is the
authoritative table; ``plan_routing.verification`` is a mirror column
that was CAS-advanced at every round boundary.  The read path
reconciled them: a TERMINAL mirror verdict overrode a non-terminal
``plan_verification`` value, and said so in a rate-limited warning.

That reconciliation is gone, because the mirror is no longer a source
at all — the write path no longer advances it (see the plan's tasks
2 / 8 / 10).  The column itself still exists, and still has a writer
(``RoutingRepository.write_verification_baseline`` freezes the reviewed
baseline into it), but a mirror nobody advances is a mirror that is
always stale, and a stale mirror allowed to override an authoritative
row is worse than no reconciliation at all: it publishes a verdict that
no write path supports, and the round it describes never happened.

The rule this file pins:

    the four verification fields come from ``plan_verification``, and
    from nothing else.

Pinned here:

  * a terminal mirror verdict cannot override an authoritative
    ``running`` — the row wins, and so do its round and budget;
  * ALL FOUR fields are pinned with a DIFFERENT value on each side
    (status / round / budget / stop reason), because a field the two
    stores happen to agree on proves nothing about which one the
    snapshot read: the removed overlay copied all four;
  * a plan with no authoritative row answers with the documented
    defaults (empty / 0 / 0) and never falls back to the mirror;
  * a plan in the ``verification_running`` phase still reads as
    running — the phase rule and the authoritative row agree, and the
    mirror cannot turn that into a terminal verdict;
  * the phase rule itself still forces ``running`` where it lives —
    the verification status endpoint and the summary body's
    verification block — because a test that seeds a row already saying
    ``running`` cannot tell whether the rule is still there;
  * the mirror is not read: changing it changes nothing;
  * the reconciliation machinery is gone from the module;
  * the divergence rule that watched the same row still fires — its
    coverage moved here from the file this change deleted;
  * the card header — the consumer these four fields exist for — still
    reads them the way it did; its four cases moved here too;
  * the three rules the deletion was not allowed to touch are still
    in force: the plan-doc review default, the routing row's
    authority over the phase, and the liveness flag's own source;
  * the read path's own docstring does not name the mirror as a
    source, because the function that assembles the snapshot is where
    the next reader looks to learn where the fields come from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notifications.cards import _resolve_header
from plan_status import PlanStatus, find_divergences


# ---------------------------------------------------------------------------
# Helpers — mirroring tests/unit/test_plan_status.py's seeders
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db)
    return db


def _seed_routing(db: Path, plan_id: str, phase: str, verification="null") -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    payload = verification if isinstance(verification, str) else json.dumps(verification)
    conn = open_db(db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, completed_phases, review_rounds, "
            " flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, phase, 0, "[]", "{}", "{}", payload,
             "2026-10-05T14:39:19Z", "2026-10-05T14:39:19Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_verification_row(db: Path, plan_id: str, status: str,
                           round_n: int = 1, max_rounds: int = 4,
                           stop_reason=None) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.init_round(plan_id, round_n=round_n, max_rounds=max_rounds)
        conn.execute(
            "UPDATE plan_verification SET verification_status = ? WHERE plan_id = ?",
            (status, plan_id),
        )
        if stop_reason is not None:
            conn.execute(
                "UPDATE plan_verification SET verification_stop_reason = ? "
                "WHERE plan_id = ?",
                (stop_reason, plan_id),
            )
        conn.commit()
    finally:
        conn.close()


def _write_mirror(db: Path, plan_id: str, verification) -> None:
    """Overwrite ONLY the mirror column, leaving every other row alone."""
    from state_machine.db.connection import open as open_db

    payload = verification if isinstance(verification, str) else json.dumps(verification)
    conn = open_db(db)
    try:
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (payload, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The four fields come from plan_verification
# ---------------------------------------------------------------------------


def test_routing_terminal_cannot_override_repo_running(state_db, clean_execution_state):
    """The exact shape from the incident, inverted.

    ``plan_verification`` says a round is running (round 2, budget 4);
    the mirror says the previous round failed (round 9, budget 3, and
    that round stopped because it ran out of rounds).  The mirror is
    stale by construction — nothing advances it any more — so the row
    wins, and the round and budget travel with it.

    Every one of the FOUR fields is given a different value on each
    side, because a field the two stores agree on proves nothing about
    where the snapshot read it from: with the mirror's budget also at 3,
    an assertion of ``max_rounds == 3`` would pass whether the value
    came from the row or from the mirror.  The removed overlay copied
    all four (status / round / budget / stop reason), so all four have
    to be pinned.
    """
    import server

    plan_id = "plan-mirror-loses"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 3,
            "stop_reason": "max_rounds_reached",
        },
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=2, max_rounds=4)

    st = server._build_plan_status(plan_id)

    assert (st.verification_status, st.verification_round) == ("running", 2), (
        f"the stale mirror overrode the authoritative row: {st.to_dict()!r}"
    )
    assert st.verification_max_rounds == 4, (
        f"the budget came from the mirror (3), not from the row (4): "
        f"{st.to_dict()!r}"
    )
    assert st.verification_stop_reason is None, (
        f"the mirror's stop reason reached the snapshot: {st.to_dict()!r}"
    )


def test_the_rows_stop_reason_reaches_the_snapshot(state_db, clean_execution_state):
    """The row's stop reason is reported — the mirror's is not consulted.

    The stop reason is the one field the removed overlay copied that
    the status field alone would never expose: a plan can carry a stop
    reason with no terminal verdict at all.  The row is the source, so
    the row's reason is what the snapshot must carry.
    """
    import server

    plan_id = "plan-row-stop-reason"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification_row(
        state_db, plan_id, "failed", round_n=1, max_rounds=4,
        stop_reason="max_rounds_reached",
    )

    st = server._build_plan_status(plan_id)

    assert st.verification_status == "failed", st.to_dict()
    assert st.verification_stop_reason == "max_rounds_reached", st.to_dict()
    assert st.verification_round == 1, st.to_dict()
    assert st.verification_max_rounds == 4, st.to_dict()


def test_no_row_does_not_fall_back_to_column(state_db, clean_execution_state):
    """No authoritative row → the documented defaults, never the mirror.

    The mirror is a stale copy of a value this codebase used to write.
    A plan that never ran a round has no round to report, and the
    honest answer is "none", not a round nobody can point at.
    """
    import server

    plan_id = "plan-no-row"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )

    st = server._build_plan_status(plan_id)

    assert st.verification_status == "", st.to_dict()
    assert st.verification_round == 0, st.to_dict()
    assert st.verification_max_rounds == 0, st.to_dict()
    assert st.verification_stop_reason is None, st.to_dict()


def test_running_phase_forces_running(state_db, clean_execution_state):
    """A plan in the ``verification_running`` phase reads as running.

    The phase is the plan's own statement that a round is in flight,
    and the authoritative row agrees with it.  The mirror's terminal
    verdict must not contradict that — and the snapshot must not report
    a phase/verdict disagreement for a plan that is simply verifying.
    """
    import server

    plan_id = "plan-running-phase"
    _seed_routing(
        state_db, plan_id, "verification_running",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=2, max_rounds=4)

    st = server._build_plan_status(plan_id)

    assert st.phase == "verification_running", st.to_dict()
    assert st.verification_status == "running", st.to_dict()
    assert st.verification_round == 2, st.to_dict()
    assert st.verification_max_rounds == 4, st.to_dict()
    assert "phase_and_verdict_disagree" not in [d.code for d in st.divergences], (
        st.to_dict()
    )


def test_the_phase_rule_still_forces_running_where_it_lives(
    state_db, clean_execution_state
):
    """``phase == verification_running`` still forces ``running``.

    ``test_running_phase_forces_running`` above cannot carry this
    boundary condition on its own: it seeds an authoritative row that
    ALREADY says ``running``, so its assertion holds whether the phase
    rule exists or not.  It pins "the mirror cannot defeat the row"; it
    does not pin "the phase rule still forces running" — and that is one
    of the boundary conditions this task was told to keep.

    The rule lives in the two readers that own a verdict of their own:

      * ``routes.verification._verification_status_from_db`` — the
        verification status endpoint;
      * ``routes.plans._authoritative_verification_block`` — the
        summary body's verification block.

    The plan-status snapshot is deliberately NOT one of them.  Its four
    verification fields come from the ``plan_verification`` row, and
    ``plan_status.find_divergences`` Rule 2 uses the phase/verdict
    disagreement as a detector: a phase that says "running" over a
    terminal verdict is the drift it exists to report.  Forcing
    ``running`` in the snapshot would silence that detector, so the
    snapshot reports the row and lets the disagreement be reported.

    The plan here has NO authoritative row and a terminal mirror, so
    every assertion below is about the phase rule and nothing else.
    """
    import server
    from routes.plans import _authoritative_verification_block
    from routes.verification import _verification_status_from_db
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_id = "plan-phase-rule"
    _seed_routing(
        state_db, plan_id, "verification_running",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )

    # The snapshot has no row to report, so it reports the documented
    # defaults — and still refuses the mirror's terminal verdict.
    st = server._build_plan_status(plan_id)
    assert st.verification_status == "", st.to_dict()
    assert st.verification_round == 0, st.to_dict()
    assert st.verification_max_rounds == 0, st.to_dict()

    # The verification status endpoint owns the phase rule.
    payload = _verification_status_from_db(plan_id)
    assert payload["verification_status"] == "running", payload

    # So does the summary body's verification block.
    conn = open_db(state_db)
    try:
        block = _authoritative_verification_block(
            plan_id,
            RoutingRepository(conn),
            VerificationRepository(conn),
            {},
        )
    finally:
        conn.close()
    assert block["status"] == "running", block


# ---------------------------------------------------------------------------
# The mirror is not read at all
# ---------------------------------------------------------------------------


def test_changing_the_mirror_changes_nothing(state_db, clean_execution_state):
    """The column is not a source, so writing to it moves no output.

    This is the property the reconciliation broke: a column nobody
    writes must not be able to change what a reader answers.
    """
    import server

    plan_id = "plan-mirror-inert"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "not_started", "round": 0},
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=2, max_rounds=3)

    before = server._build_plan_status(plan_id).to_dict()

    _write_mirror(state_db, plan_id, {"status": "failed", "round": 9, "max_rounds": 3})

    after = server._build_plan_status(plan_id).to_dict()

    assert before == after, (
        f"the mirror column still reaches the snapshot: {before!r} -> {after!r}"
    )


def test_the_reconciliation_overlay_is_gone(state_db, clean_execution_state):
    """The overlay machinery is removed, not merely unused.

    A rate-limited warning table that no longer has a writer is the
    kind of leftover that invites the next reader to "just use it" —
    and the constant is the only place the old rule is still written
    down.
    """
    from routes import plans as routes_plans

    for name in (
        "_RECONCILE_LOGGED",
        "_RECONCILE_LOG_WINDOW_SECONDS",
        "_RECONCILE_LOG_MAX_ENTRIES",
        "_TERMINAL_VERIFICATION_STATUSES",
    ):
        assert not hasattr(routes_plans, name), (
            f"routes.plans still carries {name!r}"
        )


# ---------------------------------------------------------------------------
# The divergence rule that watched the same row
# ---------------------------------------------------------------------------
#
# ``find_divergences`` Rule 3 reads the snapshot's OWN verification
# fields — never the mirror — so it still works exactly as it did.  It
# lived in the file this change deleted, and ``test_plan_status.py``
# (which does not cover it) is outside this task's file list, so its
# coverage moves here rather than disappearing.


def _status(**kw) -> PlanStatus:
    base = dict(
        plan_id="p", phase="executing", verification_status="running",
        verification_round=1, verification_max_rounds=4,
        execution_in_flight=True, verification_in_flight=False,
        tasks={"total": 68, "completed": 65, "failed": 2, "in_progress": 1},
    )
    base.update(kw)
    return PlanStatus(**base)


def test_stale_running_verification_during_execution_is_reported():
    codes = [d.code for d in find_divergences(_status())]

    assert "stale_running_verification_during_execution" in codes


def test_a_live_verification_round_is_not_a_divergence():
    """The normal in-round shape: the row says running, and a round
    really is running."""
    codes = [
        d.code for d in find_divergences(
            _status(phase="verification_running", verification_in_flight=True)
        )
    ]

    assert "stale_running_verification_during_execution" not in codes


def test_a_terminal_verdict_is_not_a_divergence():
    codes = [d.code for d in find_divergences(_status(verification_status="failed"))]

    assert "stale_running_verification_during_execution" not in codes


# ---------------------------------------------------------------------------
# The card header — the consumer these four fields exist for
# ---------------------------------------------------------------------------
#
# The header is a pure function of the snapshot, so a stale verification
# field is a wrong word on the operator's card.  These four cases were
# in the file this change deleted; they pin the header's reading of the
# four fields, and they move here rather than disappear with it.


def _header(**kw) -> str:
    st = _status(**kw)
    return _resolve_header(st, dict(st.tasks))["title"]


def test_a_live_execution_outranks_a_stale_running_verdict():
    """The contradiction itself. The header must describe the work that
    is actually happening."""
    title = _header(verification_status="running", execution_in_flight=True)

    assert "验证中" not in title
    assert "修复执行中" in title


def test_a_live_verification_round_still_says_verifying():
    title = _header(
        phase="verification_running", verification_status="running",
        execution_in_flight=False,
    )

    assert "🔄 验证中" in title


def test_the_degraded_out_of_process_path_keeps_its_header():
    """``_status_from_summary`` documents ``execution_in_flight=False``
    as the safe degraded direction. The header guard keys on that flag,
    so every shim-built and locally-constructed snapshot keeps the
    pre-existing label."""
    title = _header(verification_status="running", execution_in_flight=False)

    assert "🔄 验证中" in title


def test_the_repair_execution_header_carries_the_round():
    title = _header(verification_status="failed", execution_in_flight=True)

    assert "round 1/4" in title


# ---------------------------------------------------------------------------
# The three rules the deletion was not allowed to disturb
# ---------------------------------------------------------------------------


def test_the_plan_doc_review_phase_still_defaults_to_pending(state_db, clean_execution_state):
    """A plan waiting for a human says ``pending`` — and says it alone.

    The review gate sits BEFORE the verification sub-machine, so a plan
    that has not started a round still has a verdict: "pending".  The
    mirror column is not consulted for it either: a stale terminal
    verdict there would overwrite that default.
    """
    import server

    plan_id = "plan-doc-review"
    _seed_routing(
        state_db, plan_id, "verification_plan_review",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )

    st = server._build_plan_status(plan_id)

    assert st.phase == "verification_plan_review", st.to_dict()
    assert st.verification_status == "pending", st.to_dict()


def test_the_routing_row_still_decides_the_phase(state_db, clean_execution_state):
    """The routing row is still the phase authority.

    The deletion removed a read of that row's ``verification`` column.
    It must not have removed the row from the phase rule.
    """
    import server

    plan_id = "plan-phase-authority"
    _seed_routing(state_db, plan_id, "ready")

    st = server._build_plan_status(plan_id)

    assert st.phase == "ready", st.to_dict()
    assert st.phase_source == "routing", st.to_dict()


def test_verification_in_flight_still_comes_from_the_verification_state(state_db, clean_execution_state):
    """The liveness flag reads the in-process record — not any column.

    ``verification_in_flight`` is serving-process state, and the
    deletion touched neither the flag nor its source.
    """
    import server

    plan_id = "plan-liveness"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=2, max_rounds=3)

    server._verification_state[plan_id] = {"verification_status": "running"}

    st = server._build_plan_status(plan_id)

    assert st.verification_in_flight is True, st.to_dict()
    assert st.verification_status == "running", st.to_dict()


# ---------------------------------------------------------------------------
# The read path's own documentation
# ---------------------------------------------------------------------------
#
# The function that assembles the snapshot is where a reader looks to
# learn where the four fields come from.  It used to say — truthfully
# then — that the read path reconciled ``plan_routing.verification``
# with ``plan_verification``.  After the overlay was deleted that
# sentence became false, and a false sentence in the docstring of the
# function that builds the snapshot is how the next reader ends up
# "restoring" the mirror.


def test_the_read_path_docstring_does_not_name_the_mirror_as_a_source():
    import inspect

    from routes import plans as routes_plans

    doc = inspect.getdoc(routes_plans._build_plan_status) or ""

    assert "plan_routing.verification" not in doc, (
        "the snapshot's docstring still names the mirror column as a "
        f"source of the verification fields: {doc!r}"
    )


# ---------------------------------------------------------------------------
# The column is alive — it holds the frozen baseline, not a verdict
# ---------------------------------------------------------------------------
#
# The overlay was deleted; the column was not.  It still exists and it
# still has a writer: ``RoutingRepository.write_verification_baseline``
# freezes the reviewed baseline — and the hash of the document it was
# frozen from — into it.  That is the only thing in the column now, and
# it is the only place the baseline lives.
#
# Two consequences, and the read path's own account of itself has to
# state both:
#
#   * the column cannot answer "what is this plan's verdict", so there
#     is nothing in it for an overlay to reconcile against — the
#     removal did not paper over a live second source, it deleted a
#     read of a column that carries a frozen baseline;
#   * the column's real content must not leak into the snapshot's four
#     fields.  Its live shape on an already-verified plan is the frozen
#     baseline NEXT TO the legacy verdict the old writer left there:
#     ``write_verification_baseline`` overwrites only the two baseline
#     keys and keeps every other key in the column verbatim, so the
#     legacy keys survive on exactly the rows that still hold them.


def test_the_production_shape_of_the_column_is_ignored(state_db, clean_execution_state):
    """Baseline and legacy verdict in one blob — the row still answers.

    This is the column as it really is on a plan that was verified
    before the mirror stopped being advanced: the frozen baseline the
    column exists for, and the old ``status`` / ``round`` /
    ``max_rounds`` / ``stop_reason`` beside it, kept verbatim by the
    baseline writer.

    A reader of the column would take the verdict; the authoritative
    row is the only thing that may answer, so the whole blob is
    ignored — baseline included, verdict included.
    """
    import server

    plan_id = "plan-production-column-shape"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={
            "status": "failed",
            "round": 9,
            "max_rounds": 3,
            "stop_reason": "max_rounds_reached",
            "baseline": {
                "repo_dir": "/srv/repo",
                "ref": "refs/heads/main",
                "commit_sha": "0" * 40,
                "pr_number": 7,
            },
            "baseline_doc_sha256": "a" * 64,
        },
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=2, max_rounds=4)

    st = server._build_plan_status(plan_id)

    assert (st.verification_status, st.verification_round) == ("running", 2), (
        f"the column answered instead of the row: {st.to_dict()!r}"
    )
    assert st.verification_max_rounds == 4, st.to_dict()
    assert st.verification_stop_reason is None, st.to_dict()


def test_the_read_path_does_not_claim_the_column_is_never_written():
    """The read path's own account of itself has to be true.

    The column is not dead.  It still has a writer —
    ``RoutingRepository.write_verification_baseline`` freezes the
    reviewed baseline into it, and that is the only place the baseline
    lives.  A docstring that says "no write path touches it any more"
    is wrong, and wrong in the dangerous direction: a reader who
    believes it may delete the baseline writer (and with it the only
    copy of the baseline), or "restore" the column as a source,
    because the sentence says the column is empty.

    The truth is stronger than the claim it replaces: the column's one
    writer stores a frozen baseline and a document hash — never a
    verdict — so there is nothing in it to reconcile against.
    """
    import inspect

    from routes import plans as routes_plans

    doc = inspect.getdoc(routes_plans._build_plan_status) or ""

    assert "no longer written by any write path" not in doc, (
        "the read path still claims nothing writes the mirror column: "
        f"{doc!r}"
    )
    assert "write_verification_baseline" in doc, (
        "the read path's docstring does not name the column's one "
        "remaining writer, so it cannot say what the column now "
        f"holds: {doc!r}"
    )


def test_the_divergence_rule_does_not_claim_the_column_is_never_written():
    """Same truth, same place: the comment that explains Rule 3.

    Rule 3 watches the authoritative row for a round that claims to be
    live when none is.  Its comment used to justify the absence of a
    second store by claiming the column is written by nobody — the
    same false claim, in the module that every reader of the snapshot's
    divergences is pointed at.
    """
    from pathlib import Path

    import plan_status

    src = Path(plan_status.__file__).read_text(encoding="utf-8")

    assert "no longer written by any write path" not in src, (
        "plan_status.py still claims nothing writes the mirror column"
    )


# ---------------------------------------------------------------------------
# The interface contract, in the form that cannot rot
# ---------------------------------------------------------------------------
#
# The contract this task produces is not "the four fields look right" but
# "the four fields ARE what ``VerificationRepository`` says".  A snapshot
# that happens to agree with the row today can still disagree tomorrow if
# somebody starts transforming the value on the way through — clamping a
# round, defaulting a budget, or (the failure this file exists to catch)
# preferring another store.  Comparing the snapshot against the
# repository's own read, field by field, is the form of that claim that
# does not rot.


def test_the_snapshot_agrees_with_the_repository_field_by_field(
    state_db, clean_execution_state
):
    """Four fields, four equalities — against the repository's own read.

    The row is written through the repository's narrow door (not raw
    SQL), so the comparison is between two readers of one row: the
    snapshot, and ``VerificationRepository.current()``.  The mirror
    beside it holds a stale TERMINAL verdict, which is the shape the
    removed overlay used to prefer over the row.
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    for label, status, stop_reason in (
        ("running", "running", None),
        ("failed", "failed", "max_rounds_reached"),
    ):
        plan_id = f"plan-same-source-{label}"
        _seed_routing(
            state_db, plan_id, "executing",
            verification={
                "status": "failed",
                "round": 9,
                "max_rounds": 3,
                "stop_reason": "max_rounds_reached",
            },
        )

        conn = open_db(state_db)
        try:
            repo = VerificationRepository(conn)
            repo.init_round(plan_id, round_n=2, max_rounds=4)
            repo.set_state(
                plan_id, status=status, stop_reason=stop_reason,
            )
            authoritative = repo.current(plan_id)
        finally:
            conn.close()

        st = server._build_plan_status(plan_id)

        assert authoritative is not None, plan_id
        assert st.verification_status == authoritative["verification_status"], (
            f"{label}: status is not the repository's: {st.to_dict()!r} "
            f"vs {authoritative!r}"
        )
        assert st.verification_round == authoritative["round"], (
            f"{label}: round is not the repository's: {st.to_dict()!r} "
            f"vs {authoritative!r}"
        )
        assert st.verification_max_rounds == authoritative["max_rounds"], (
            f"{label}: budget is not the repository's: {st.to_dict()!r} "
            f"vs {authoritative!r}"
        )
        assert st.verification_stop_reason == (
            authoritative["verification_stop_reason"] or None
        ), (
            f"{label}: stop reason is not the repository's: "
            f"{st.to_dict()!r} vs {authoritative!r}"
        )


def test_the_in_memory_liveness_record_is_not_a_fifth_source(
    state_db, clean_execution_state
):
    """The process-local record carries a status too — and is not one.

    ``_verification_state`` is this process's note that a verification
    round is in flight, and it carries a ``verification_status`` key of
    its own.  It is a LIVENESS fact: it disappears with the process and
    says nothing about what the row says.  It may feed
    ``verification_in_flight``; it may not feed the four fields.
    """
    import server

    plan_id = "plan-in-memory-is-not-a-source"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=2, max_rounds=4)

    server._verification_state[plan_id] = {
        "verification_status": "failed",
        "verification_round": 9,
    }

    st = server._build_plan_status(plan_id)

    assert st.verification_status == "running", st.to_dict()
    assert st.verification_round == 2, st.to_dict()
    assert st.verification_max_rounds == 4, st.to_dict()


# ---------------------------------------------------------------------------
# No reader of the column — as statements, not as prose
# ---------------------------------------------------------------------------
#
# The behavioural pins above say the snapshot does not ANSWER from the
# column.  This one says the column is not even READ in the module that
# assembles the snapshot: every row handle the module takes from
# ``.current(...)`` is checked for a ``"verification"`` subscript.
#
# Why statements and not text: the module documents the removal, and a
# text scan would report the explanation as the violation — the trap the
# repository's own writer gate describes when it says that the AST can
# tell a docstring from a statement.


def _row_handles(tree):
    """The names bound from a ``.current(...)`` call — the row handles."""
    import ast

    handles = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "current"
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                handles.add(target.id)
    return handles


def _receiver_name(node):
    """The name a subscript reads through, or ``None``."""
    import ast

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _receiver_name(node.value)
    if isinstance(node, ast.BoolOp):
        # ``(route or {}).get("verification")`` — the handle is on the
        # left of the ``or``.
        return _receiver_name(node.values[0])
    return None


def test_no_statement_in_the_read_path_reads_the_mirror_column():
    """The column has no readers left in the module that builds the snapshot.

    This is the statement-level form of "the mirror is not a source"
    (arch decision point 3).  It fails if anybody reads the column off a
    row handle again — which is how the removed overlay read it.
    """
    import ast
    from pathlib import Path

    from routes import plans as routes_plans

    tree = ast.parse(Path(routes_plans.__file__).read_text(encoding="utf-8"))
    handles = _row_handles(tree)

    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "verification"
        ):
            name = _receiver_name(node.func.value)
            if name in handles:
                violations.append((node.lineno, f"{name}.get('verification')"))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "verification"
        ):
            name = _receiver_name(node.value)
            if name in handles:
                violations.append((node.lineno, f"{name}['verification']"))

    assert not violations, (
        "the read path reads the mirror column again: "
        f"{violations!r} (row handles: {sorted(handles)!r})"
    )


def test_the_module_docstring_does_not_present_the_mirror_as_a_current_store():
    """The incident table is history; the docstring has to say so.

    ``plan_status`` opens with the 2026-09-23 incident: six stores, six
    answers, all live at once.  The table lists
    ``plan_routing.verification.status`` among them — true for that
    afternoon, and a trap afterwards, because a table of "store → answer"
    read as a map of today's stores is how the mirror gets treated as a
    source again.
    """
    import plan_status

    doc = plan_status.__doc__ or ""

    assert "retired as a source" in doc, (
        "plan_status.py's module docstring lists the mirror column among "
        "the stores without saying the list is the 2026-09-23 incident "
        f"and the column has since been retired: {doc[:400]!r}"
    )
