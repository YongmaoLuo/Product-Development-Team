"""The convergence rate has to be comparable with itself over time.

Background
----------
Once the metric drifts, nothing is left to show that "80%" means the
same thing before and after.  Task 9 shipped the classification table
(:func:`classify_plan` and :func:`summarize` in
``verification.convergence_metrics``); this file is the REPLAY BASELINE
for it: a fixed set of nine plan rows, one per branch of that table,
replayed every time the table is touched.

What is asserted here
---------------------
* Every sample classifies to the category THIS FILE declares.  The
  expectations are written out literally: they are the baseline the
  table is compared against, so they must not be imported from the
  module under test and then called an agreement.
* The denominator and the numerator of the replayed set equal the hand
  count, so a change that moves a row between buckets shows up as a
  number change and not only as a label change.
* Every sample declares an ``expected_category``.  A sample that forgot
  it would be a row that asserts nothing, which is worse than a missing
  sample: it looks covered.

What this file does NOT do
--------------------------
It does not re-run any historical plan.  The rows are literal constants;
nothing here touches the database, re-runs a plan, or reads the wall
clock.  The subject of the assertion is the CLASSIFICATION and the
arithmetic over it — not the history that produced the row.

When the table changes
----------------------
Task 9's decision table is the thing under test, so changing it means
changing this file in the same commit.  A new vocabulary entry, or a
re-worded one, turns this file red until the expectations are updated
here.  That is the entire point of a replay baseline: the drift is
caught by the test, instead of being discovered later by trying to line
up two reports that nobody can line up.
"""

from __future__ import annotations

from verification.convergence_metrics import classify_plan, summarize


#: A fixed "now" so the window never depends on the wall clock.
NOW = "2026-10-30T00:00:00"

#: The window the replay is summarized over.  Every sample below falls
#: inside it.
WINDOW_DAYS = 30

#: The six categories task 9's table can produce.  Written out literally:
#: this file is the baseline, so it must not import the table's own
#: vocabulary and call the import an agreement.
KNOWN_CATEGORIES = frozenset(
    {
        "passed",
        "user_stopped",
        "env_blocked",
        "infra_flake",
        "code_defect",
        "unclassified",
    }
)

#: Nine plan rows, one per branch of task 9's ordered decision table.
#:
#: ``expected_category`` rides along inside the row on purpose: the
#: expectation travels WITH the sample, so a row can never be added
#: without saying what it is supposed to become.
ROWS = [
    # The upstream material's §1 plan.  Its failure was an
    # infrastructure one — the repair batch never drained, the verdict
    # never became available — so it is NOT a code defect.  This is the
    # row that decides whether "the plan failed" and "the code is
    # wrong" are being counted as the same thing.
    {
        "plan_id": "20261023-supervisor-CC-Connec-2",
        "verification_status": "FAILED",
        "verification_stop_reason": (
            "repair_batch_not_drained:repair_non_zero_exit:1_pending"
        ),
        "updated_at": "2026-10-23T18:36:00",
        "expected_category": "infra_flake",
    },
    # verdict_unavailable — the verdict round produced nothing usable.
    {
        "plan_id": "20261023-verdict-unavailable",
        "verification_status": "FAILED",
        "verification_stop_reason": "verdict_unavailable",
        "updated_at": "2026-10-24T09:15:00",
        "expected_category": "infra_flake",
    },
    # repair_never_drained:<inner reason>:<n>x — the batch never drained
    # and the chain stopped after three redispatches.  The production
    # shape nests the batch reason inside the "never drained" one.
    {
        "plan_id": "20261023-repair-never-drained",
        "verification_status": "FAILED",
        "verification_stop_reason": (
            "repair_never_drained:"
            "repair_batch_not_drained:repair_non_zero_exit:1_pending:3x"
        ),
        "updated_at": "2026-10-25T11:20:00",
        "expected_category": "infra_flake",
    },
    # repair_gate_red — the gate went red: a verdict about the CODE.
    {
        "plan_id": "20261023-repair-gate-red",
        "verification_status": "FAILED",
        "verification_stop_reason": "repair_gate_red",
        "updated_at": "2026-10-26T08:05:00",
        "expected_category": "code_defect",
    },
    # repair_task_failed — a repair task itself broke: also about the
    # CODE.
    {
        "plan_id": "20261023-repair-task-failed",
        "verification_status": "FAILED",
        "verification_stop_reason": "repair_task_failed",
        "updated_at": "2026-10-27T14:45:00",
        "expected_category": "code_defect",
    },
    # service_preflight_failed — the ENVIRONMENT blocked the plan.
    {
        "plan_id": "20261023-service-preflight-failed",
        "verification_status": "FAILED",
        "verification_stop_reason": "service_preflight_failed",
        "updated_at": "2026-10-28T07:30:00",
        "expected_category": "env_blocked",
    },
    # user_stopped — a human stopped it.  The production shape is
    # status='loop_stopped' + stop_reason='user_stopped'
    # (``VerificationRepository.mark_stopped`` writes exactly that), so
    # that is the shape replayed here.
    {
        "plan_id": "20261023-user-stopped",
        "verification_status": "loop_stopped",
        "verification_stop_reason": "user_stopped",
        "updated_at": "2026-10-28T16:10:00",
        "expected_category": "user_stopped",
    },
    # passed — it converged.
    {
        "plan_id": "20261023-passed",
        "verification_status": "PASSED",
        "verification_stop_reason": None,
        "updated_at": "2026-10-29T10:00:00",
        "expected_category": "passed",
    },
    # A stop_reason nobody enumerated.  ``process_died_unexpectedly`` is
    # a reason the verification loop really writes, and it is NOT in task
    # 9's table — which is exactly what the ``unclassified`` bucket is
    # for.  If a later version of the table adds it, THIS sample goes
    # red, and the change gets recorded here on purpose.
    {
        "plan_id": "20261023-unknown-stop-reason",
        "verification_status": "FAILED",
        "verification_stop_reason": "process_died_unexpectedly",
        "updated_at": "2026-10-29T19:50:00",
        "expected_category": "unclassified",
    },
]

#: The hand count of the replayed set, written out before the code ran.
#:
#: Denominator: 9 concluded rows − 1 user_stopped − 1 env_blocked = 7.
#: Numerator: the one ``passed`` row.  Rate: 1/7.
HAND_COUNT = {
    "entered": 9,
    "concluded": 9,
    "running": 0,
    "denominator": 7,
    "numerator": 1,
    "rate": 1 / 7,
    # ``passed`` is not a failure category, so it is not in the histogram.
    "by_category": {
        "code_defect": 2,
        "env_blocked": 1,
        "infra_flake": 3,
        "unclassified": 1,
        "user_stopped": 1,
    },
}

#: The unconverged list, newest first: every concluded row that is
#: neither ``passed`` nor ``user_stopped`` — seven of the nine rows.
HAND_UNCONVERGED = [
    {"plan_id": "20261023-unknown-stop-reason", "category": "unclassified"},
    {"plan_id": "20261023-service-preflight-failed", "category": "env_blocked"},
    {"plan_id": "20261023-repair-task-failed", "category": "code_defect"},
    {"plan_id": "20261023-repair-gate-red", "category": "code_defect"},
    {"plan_id": "20261023-repair-never-drained", "category": "infra_flake"},
    {"plan_id": "20261023-verdict-unavailable", "category": "infra_flake"},
    {"plan_id": "20261023-supervisor-CC-Connec-2", "category": "infra_flake"},
]


# ---------------------------------------------------------------------------
# The replayed categories
# ---------------------------------------------------------------------------


def test_replay_categories_are_stable():
    """Every replayed row still lands in the category this file declares.

    This is the drift alarm.  Task 9's table is free to change — but
    when it does, this file has to change with it, and the change has to
    be a decision somebody wrote down rather than a number that quietly
    moved on its own.
    """
    for row in ROWS:
        expected = row.get("expected_category")
        actual = classify_plan(row)
        assert actual == expected, (
            f"{row['plan_id']}: stop_reason="
            f"{row['verification_stop_reason']!r} classified as {actual!r}, "
            f"the replay baseline expects {expected!r}"
        )

    # The nine samples cover every branch of the table — including the
    # bucket nobody enumerated.  If a sample is dropped, or a category
    # stops being exercised, this goes red rather than the baseline
    # quietly covering less than it claims to.
    assert {row.get("expected_category") for row in ROWS} == KNOWN_CATEGORIES


# ---------------------------------------------------------------------------
# The replayed numbers
# ---------------------------------------------------------------------------


def test_replay_summary_numbers_match_hand_count():
    """The denominator and the numerator equal the hand count.

    Labels alone are not enough: a table that moves one row from
    ``infra_flake`` to ``code_defect`` keeps every individual label
    plausible while changing what the rate means.  So the arithmetic is
    pinned too — the denominator excludes the human stop and the
    environment block, and the numerator is the one plan that passed.
    """
    summary = summarize(ROWS, window_days=WINDOW_DAYS, now_iso=NOW)

    for key, expected in HAND_COUNT.items():
        assert summary[key] == expected, (
            f"{key}: {summary[key]!r} != the hand count {expected!r}"
        )

    # The unconverged list, newest first, is the "what to look at next"
    # list — its contents and its order are part of the baseline.
    assert summary["unconverged"] == HAND_UNCONVERGED


# ---------------------------------------------------------------------------
# The sample set is well-formed
# ---------------------------------------------------------------------------


def test_every_sample_declares_expected_category():
    """A sample without an expectation asserts nothing, so it fails here.

    The failure is deliberate rather than silent: a row that forgot its
    ``expected_category`` would still be counted by ``summarize``, so
    without this check it would move the denominator and the numerator
    while asserting nothing about the classification.
    """
    assert len(ROWS) == 9, (
        f"the replay baseline holds {len(ROWS)} samples, one per branch "
        f"of the decision table — expected 9"
    )

    for row in ROWS:
        assert "expected_category" in row, (
            f"{row['plan_id']} declares no expected_category"
        )
        expected = row["expected_category"]
        assert isinstance(expected, str) and expected.strip(), (
            f"{row['plan_id']} has an empty expected_category"
        )
        assert expected in KNOWN_CATEGORIES, (
            f"{row['plan_id']} expects {expected!r}, which is not one of "
            f"the six categories {sorted(KNOWN_CATEGORIES)}"
        )