"""The convergence rate is a NUMBER, not a feeling.

Background
----------
"80% of plans converge on their own" was a claim nobody could check: there
was no metric at all.  The reason strings that explain a non-converging
plan were named one by one (``verdict_unavailable`` from the verdict
round, ``repair_batch_not_drained`` / ``repair_gate_red`` from the repair
round, ``repair_never_drained`` from the re-dispatch round), but nothing
turned those names into a rate.

This module is the pure kernel that does the counting:

* :func:`classify_plan` maps one plan row to ONE category.
* :func:`summarize` filters a set of rows by a time window and returns the
  rate, the per-category histogram and the list of plans that did not
  converge.

Contract pinned here
--------------------
* The DENOMINATOR is "plans that entered verification, reached a
  conclusion, and were not stopped by a human or blocked by the
  environment".  A plan the user stopped, or one the environment blocked,
  is excluded — neither is evidence that the product converges.
* The NUMERATOR is the subset of the denominator that actually passed.
* A rate of ``None`` means "no denominator" — never ``0.0``.  Zero means
  "every plan failed"; ``None`` means "we have not measured anything", and
  the two must not be confused.
* Every status / stop reason is compared after ``str(x).lower()`` — the
  rows come from SQLite, where the casing is whatever the writer typed.
* The ordered decision table is "first match wins", and the categories are
  read in the order the failures actually happen.
"""

from __future__ import annotations

from verification.convergence_metrics import classify_plan, summarize


#: A fixed "now" so every assertion below is about the arithmetic and
#: never about the wall clock.
NOW = "2026-10-30T00:00:00"


def _row(plan_id, status, reason=None, updated_at=NOW):
    """One plan row in the shape the metrics kernel consumes."""
    return {
        "plan_id": plan_id,
        "verification_status": status,
        "verification_stop_reason": reason,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# The denominator
# ---------------------------------------------------------------------------

#: Five plans, five different situations:
#:   p_pass    — converged (counts in the denominator AND the numerator)
#:   p_user    — a human stopped it (concluded, but NOT in the denominator)
#:   p_env     — the environment blocked it (concluded, but NOT in the denominator)
#:   p_defect  — the code is wrong (concluded, in the denominator, not passed)
#:   p_running — still going (entered, but not concluded)
FIXED_ROWS = [
    _row("p_pass", "PASSED"),
    _row("p_user", "loop_stopped", "user_stopped"),
    _row("p_env", "FAILED", "service_preflight_failed"),
    _row("p_defect", "FAILED", "repair_gate_red"),
    _row("p_running", "running"),
]


def test_denominator_excludes_user_stopped_and_env_blocked():
    """Only plans that really got a verdict count towards the rate.

    A plan a human stopped and a plan the environment blocked are both
    *conclusions* — they have a terminal status — but neither says
    anything about whether the product converges.  Counting them would
    make the rate a measure of who pressed stop, not of the pipeline.
    """
    summary = summarize(FIXED_ROWS, window_days=30, now_iso=NOW)

    assert summary["entered"] == 5
    assert summary["concluded"] == 4
    assert summary["running"] == 1

    # Denominator: p_pass + p_defect.  Numerator: p_pass.
    assert summary["denominator"] == 2
    assert summary["numerator"] == 1
    assert summary["rate"] == 0.5

    # The histogram covers every concluded row — user_stopped and
    # env_blocked included — but a PASSED row is not a failure category.
    assert summary["by_category"] == {
        "user_stopped": 1,
        "env_blocked": 1,
        "code_defect": 1,
    }

    # The unconverged list is "concluded, and neither passed nor stopped
    # by a human" — so the user's own stop is NOT an unconverged plan,
    # but the blocked one IS (it did not converge).
    assert summary["unconverged"] == [
        {"plan_id": "p_env", "category": "env_blocked"},
        {"plan_id": "p_defect", "category": "code_defect"},
    ]


# ---------------------------------------------------------------------------
# The classification table
# ---------------------------------------------------------------------------

#: One row per rule of the ordered decision table, plus the two spellings
#: of a human stop (``status='user_stopped'`` and the production shape
#: ``status='loop_stopped'`` + ``stop_reason='user_stopped'``, which is
#: what ``mark_stopped`` actually writes).
HAND_ROWS = [
    _row("p_passed", "PASSED"),
    _row("p_user_status", "user_stopped"),
    _row("p_user_reason", "loop_stopped", "user_stopped"),
    _row("p_preflight", "FAILED", "service_preflight_failed"),
    _row("p_env_blocked", "FAILED", "environment_blocked"),
    _row("p_verdict_unavailable", "FAILED", "verdict_unavailable"),
    _row("p_verdict_parse", "FAILED", "verdict_parse_error"),
    _row("p_inconclusive", "FAILED", "inconclusive"),
    _row("p_stale", "FAILED", "stale"),
    _row("p_timeout", "FAILED", "timeout"),
    _row("p_never_drained", "FAILED", "repair_never_drained"),
    _row(
        "p_batch_not_drained",
        "FAILED",
        "repair_batch_not_drained:repair_non_zero_exit:1_pending",
    ),
    _row("p_unfinished", "FAILED", "repair_exited_with_unfinished_tasks"),
    _row("p_nonzero", "FAILED", "repair_non_zero_exit"),
    _row("p_gate_red", "FAILED", "repair_gate_red"),
    _row("p_task_failed", "FAILED", "repair_task_failed"),
    _row("p_unknown", "FAILED", "a_reason_nobody_enumerated"),
]

HAND_EXPECTED = {
    "p_passed": "passed",
    "p_user_status": "user_stopped",
    "p_user_reason": "user_stopped",
    "p_preflight": "env_blocked",
    "p_env_blocked": "env_blocked",
    "p_verdict_unavailable": "infra_flake",
    "p_verdict_parse": "infra_flake",
    "p_inconclusive": "infra_flake",
    "p_stale": "infra_flake",
    "p_timeout": "infra_flake",
    "p_never_drained": "infra_flake",
    "p_batch_not_drained": "infra_flake",
    "p_unfinished": "infra_flake",
    "p_nonzero": "infra_flake",
    "p_gate_red": "code_defect",
    "p_task_failed": "code_defect",
    "p_unknown": "unclassified",
}


def test_categories_match_hand_count():
    """The histogram equals the hand count of the decision table.

    Seventeen plans, classified by hand: one passed, two user-stopped
    (both spellings), two environment-blocked, nine infrastructure
    flakes, two genuine code defects and one reason nobody enumerated.
    """
    for row in HAND_ROWS:
        assert classify_plan(row) == HAND_EXPECTED[row["plan_id"]], (
            row["plan_id"]
        )

    summary = summarize(HAND_ROWS, window_days=30, now_iso=NOW)

    assert summary["entered"] == 17
    assert summary["concluded"] == 17
    assert summary["running"] == 0
    assert summary["by_category"] == {
        "user_stopped": 2,
        "env_blocked": 2,
        "infra_flake": 9,
        "code_defect": 2,
        "unclassified": 1,
    }

    # 17 concluded − 2 user-stopped − 2 environment-blocked = 13, and only
    # the one passed plan is in the numerator.
    assert summary["denominator"] == 13
    assert summary["numerator"] == 1
    assert summary["rate"] == 1 / 13


# ---------------------------------------------------------------------------
# No denominator → no rate
# ---------------------------------------------------------------------------


def test_empty_window_rate_is_none():
    """No denominator → ``rate`` is ``None``, never ``0.0``.

    "Nothing to measure" and "everything failed" are different facts;
    returning ``0.0`` for both would make an empty window look like a
    total collapse.
    """
    empty = summarize([], window_days=30, now_iso=NOW)
    assert empty["entered"] == 0
    assert empty["concluded"] == 0
    assert empty["running"] == 0
    assert empty["denominator"] == 0
    assert empty["numerator"] == 0
    assert empty["rate"] is None
    assert empty["by_category"] == {}
    assert empty["unconverged"] == []

    # A window that only holds a still-running plan has no conclusion
    # either — the rate is still unknown, not zero.
    running_only = summarize(
        [_row("p_running", "running")], window_days=30, now_iso=NOW,
    )
    assert running_only["entered"] == 1
    assert running_only["concluded"] == 0
    assert running_only["running"] == 1
    assert running_only["rate"] is None


# ---------------------------------------------------------------------------
# The unclassified bucket
# ---------------------------------------------------------------------------


def test_unclassified_is_listed_separately():
    """A reason nobody enumerated is its own bucket, and it is visible.

    It must not be silently folded into a neighbouring category (that
    would hide a gap in the reason taxonomy), and it must not be dropped
    either — an unknown reason is exactly the thing the "80% converge"
    claim needs to surface.
    """
    rows = [_row("p_odd", "FAILED", "a_reason_nobody_enumerated")]

    assert classify_plan(rows[0]) == "unclassified"

    summary = summarize(rows, window_days=30, now_iso=NOW)
    assert summary["by_category"]["unclassified"] == 1
    assert summary["unconverged"] == [
        {"plan_id": "p_odd", "category": "unclassified"},
    ]

    # It IS a conclusion, so it stays in the denominator — the rate is a
    # number somebody can be held to.
    assert summary["denominator"] == 1
    assert summary["numerator"] == 0
    assert summary["rate"] == 0.0


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_window_filter_uses_updated_at():
    """The window is an inequality on ``updated_at``.

    A row is IN the window when ``updated_at >= now - window_days`` — the
    edge is inclusive — and a row outside it is absent from every count,
    not just from ``entered``.
    """
    rows = [
        _row("p_in", "PASSED", updated_at="2026-10-29T12:00:00"),
        _row("p_out", "FAILED", "repair_gate_red", updated_at="2026-10-20T12:00:00"),
        # exactly on the cutoff (2026-10-30T00:00:00 − 7 days)
        _row("p_edge", "FAILED", "repair_task_failed", updated_at="2026-10-23T00:00:00"),
    ]

    summary = summarize(rows, window_days=7, now_iso=NOW)

    assert summary["entered"] == 2
    assert summary["concluded"] == 2
    assert summary["denominator"] == 2
    assert summary["numerator"] == 1
    # p_out is outside the window, so it is absent from the histogram too
    # — and p_in passed, which is not a failure category.
    assert summary["by_category"] == {"code_defect": 1}
    assert summary["unconverged"] == [
        {"plan_id": "p_edge", "category": "code_defect"},
    ]


# ---------------------------------------------------------------------------
# The unconverged list
# ---------------------------------------------------------------------------


def test_unconverged_is_recent_first_and_capped():
    """The unconverged list is newest-first and capped by ``recent_n``.

    It is the "what should I look at next" list, so the most recent
    failure has to be at the top, and the list has to stay short enough
    to be read.
    """
    rows = [
        _row("p_old", "FAILED", "repair_gate_red", updated_at="2026-10-25T00:00:00"),
        _row("p_new", "FAILED", "repair_task_failed", updated_at="2026-10-29T00:00:00"),
        _row("p_mid", "FAILED", "verdict_unavailable", updated_at="2026-10-27T00:00:00"),
    ]

    summary = summarize(rows, window_days=30, recent_n=2, now_iso=NOW)

    assert [entry["plan_id"] for entry in summary["unconverged"]] == [
        "p_new",
        "p_mid",
    ]
    assert summary["by_category"] == {
        "code_defect": 2,
        "infra_flake": 1,
    }