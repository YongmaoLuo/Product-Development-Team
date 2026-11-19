"""A refused write OUTSIDE this run's project dir is not this plan's failure.

Why this file exists
--------------------
``undeclared_write`` means "the task reached for a path its
``files_to_modify`` never named", and the refiner's answer to that is
precise: declare the path and run the task again
(``REFINE_WITH_DETAIL``). That answer is only correct when the path
belongs to THIS plan.

A refused path that lives outside the run's ``project_dir`` belongs to
somebody else -- another plan's worktree, the operator's checkout, a
scratch tree. Nobody in this plan declared it, so "declare this path" is
not a fix: it would put another plan's file into this plan's
declaration, and the fix round would be editing a file nobody here owns.

So an out-of-scope refusal is not a write-contract failure at all. It is
"we cannot tell what this is" -- which is what ``unknown`` means, and
``unknown``'s remedy is a retry, not a refinement. The paths that were
outside are recorded on ``scope_violation`` so the run record says which
files the plan never owned.

What is pinned here
-------------------
* a refused path outside the project dir downgrades to ``unknown``;
* a refused path inside it stays ``undeclared_write`` (the common case
  must not change);
* without a project dir there is nothing to compare against, so the
  existing behaviour stands;
* a downgraded failure never carries the refine remedy;
* one foreign path among several is enough to downgrade, and EVERY
  foreign path is recorded;
* a relative path that climbs out with ``..`` is foreign too;
* the downgrade applies to a REFUSED WRITE and to nothing else -- a
  failure of another kind that merely quotes the refusal keeps its kind;
* a failure stays hashable, because its payload is a dict for the
  out-of-scope case.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from failure_classification import (  # noqa: E402
    INFRA,
    REFINE_WITH_DETAIL,
    REPORT_FORMAT,
    RETRY_THEN_SPLIT,
    UNDECLARED_WRITE,
    UNKNOWN,
    classify_failure,
    is_split_allowed,
    remedy_for,
)


#: This run's project dir -- the worktree the plan is allowed to touch.
PROJECT_DIR = "/tmp/plan-worktree"

#: A path in ANOTHER plan's worktree. Refused, but not ours.
FOREIGN_PATH = "/tmp/other-worktree/backend/tests/unit"


# ---------------------------------------------------------------------------
# the downgrade
# ---------------------------------------------------------------------------
def test_foreign_path_downgrades_to_unknown():
    """A path outside the project dir is not a write-contract failure.

    ``undeclared_write`` buys the refiner a precise instruction ("declare
    this path"). Applied to another plan's file that instruction is
    wrong, so the failure must lose the kind that carries it.
    """
    failure = classify_failure(
        f"undeclared_write: {FOREIGN_PATH}",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == UNKNOWN, failure.kind
    assert failure.scope_violation == [FOREIGN_PATH]
    assert failure.detail == {"foreign_paths": [FOREIGN_PATH]}


def test_in_project_path_still_undeclared_write():
    """The common case must not change.

    A refused path inside the project dir is exactly what
    ``undeclared_write`` is for, and it must keep the kind, the detail
    and the refine remedy.
    """
    inside = f"{PROJECT_DIR}/backend/app.py"
    failure = classify_failure(
        f"undeclared_write: {inside}",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == UNDECLARED_WRITE, failure.kind
    assert failure.scope_violation == []
    assert failure.detail == inside
    assert remedy_for(failure.kind) == REFINE_WITH_DETAIL


def test_no_project_dir_keeps_behaviour():
    """Without a project dir there is nothing to compare against.

    The rule needs the run's project dir to know where "outside" is.
    Without it the module must not guess, and the pre-existing
    behaviour stands.
    """
    failure = classify_failure(f"undeclared_write: {FOREIGN_PATH}")
    assert failure.kind == UNDECLARED_WRITE, failure.kind
    assert failure.scope_violation == []
    assert failure.detail == FOREIGN_PATH


def test_downgraded_failure_never_reaches_refine():
    """The downgrade must not merely relabel the failure.

    ``REFINE_WITH_DETAIL`` is the remedy that tells the refiner to
    declare the refused path. If a downgraded failure kept it, the
    refinement would be handed the very path it must not touch.
    """
    failure = classify_failure(
        f"undeclared_write: {FOREIGN_PATH}",
        project_dir=PROJECT_DIR,
    )
    assert remedy_for(failure.kind) != REFINE_WITH_DETAIL
    assert failure.remedy != REFINE_WITH_DETAIL
    assert remedy_for(failure.kind) == RETRY_THEN_SPLIT


# ---------------------------------------------------------------------------
# what the downgrade records
# ---------------------------------------------------------------------------
def test_downgraded_failure_is_retryable():
    """``unknown``'s remedy is a retry, and the failure says so."""
    failure = classify_failure(
        f"undeclared_write: {FOREIGN_PATH}",
        project_dir=PROJECT_DIR,
    )
    assert failure.retryable is True


def test_a_mixed_list_downgrades_and_records_every_foreign_path():
    """One foreign path among several is enough -- and all of them count.

    The executor joins up to five refused paths into one message. If any
    of them is outside, the refusal is not a write-contract failure of
    this plan, and the ones that were inside must not hide the ones that
    were not.
    """
    inside = f"{PROJECT_DIR}/backend/app.py"
    second_foreign = "/tmp/second-worktree/backend/other.py"
    failure = classify_failure(
        f"undeclared_write: {inside}, {FOREIGN_PATH}, {second_foreign}",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == UNKNOWN, failure.kind
    assert failure.scope_violation == [FOREIGN_PATH, second_foreign]
    assert failure.detail == {
        "foreign_paths": [FOREIGN_PATH, second_foreign],
    }


def test_a_relative_path_that_climbs_out_is_foreign():
    """``../`` is an escape, not a spelling.

    A relative path is resolved against the project dir, so one that
    climbs out of it points outside exactly like an absolute path does.
    """
    failure = classify_failure(
        "undeclared_write: ../other-worktree/backend/tests/unit",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == UNKNOWN, failure.kind
    assert failure.scope_violation == [
        "../other-worktree/backend/tests/unit",
    ]


def test_the_guard_wording_downgrades_too():
    """The sub-agent's own spelling of the refusal counts the same.

    Which shape arrives is not something the executor controls: the
    guard's marker survives as ``undeclared_write: <paths>`` when the
    sub-agent relays the refusal, it arrives as the guard's sentence
    otherwise. Both name the same refused path.
    """
    failure = classify_failure(
        "[pdt] refusing to edit a file this task did not declare: "
        f"{FOREIGN_PATH}. This is not a lock conflict and waiting will "
        "not clear it. The task's files_to_modify must list this path "
        "(relative to the project dir) if it really needs to change it.",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == UNKNOWN, failure.kind
    assert failure.scope_violation == [FOREIGN_PATH]


def test_other_kinds_carry_no_scope_violation():
    """Only an out-of-scope refusal has foreign paths to report."""
    failure = classify_failure(
        "AssertionError: expected 3 got 4",
        project_dir=PROJECT_DIR,
    )
    assert failure.scope_violation == []
    assert failure.detail == ""


def test_a_downgraded_failure_is_not_split_by_the_refine_gate():
    """The gate asks the kind's remedy; ``unknown`` is not a refine.

    This is the same fact as the remedy assertion above, seen through
    the one API the refiner actually calls.
    """
    failure = classify_failure(
        f"undeclared_write: {FOREIGN_PATH}",
        project_dir=PROJECT_DIR,
    )
    assert is_split_allowed(failure) is (failure.remedy == RETRY_THEN_SPLIT)
    assert failure.remedy != REFINE_WITH_DETAIL


# ---------------------------------------------------------------------------
# the downgrade belongs to the refused write
# ---------------------------------------------------------------------------
def test_a_refusal_quoted_inside_another_failure_is_not_downgraded():
    """The downgrade belongs to a refused write, not to any text quoting one.

    A second-pass audit rejection can repeat what the guard said while
    rejecting the report for a different reason. The quoted text names a
    path outside the project dir, but the failure is not a write-contract
    failure: downgrading it to ``unknown`` would throw away the report
    problem the audit actually reported, and hand the run a retry instead
    of the answer the audit gave.
    """
    failure = classify_failure(
        "audit_second_pass_failed: the report does not name the files the "
        "task changed. The sub-agent said: refusing to edit a file this "
        f"task did not declare: {FOREIGN_PATH}. This is not a lock "
        "conflict and waiting will not clear it.",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == REPORT_FORMAT, failure.kind
    assert failure.scope_violation == []


def test_a_timeout_that_quotes_a_refusal_is_not_downgraded():
    """Same rule, the executor's own spelling of the refusal.

    A run that ran out of time reports the timeout, and the refusal
    sentence can be part of what it was doing when the clock ran out.
    The failure is an ``infra`` one -- nothing was judged -- and the
    refusal quoted inside it must not turn it into an out-of-scope write.
    """
    failure = classify_failure(
        f"Timeout: undeclared_write: {FOREIGN_PATH}",
        project_dir=PROJECT_DIR,
    )
    assert failure.kind == INFRA, failure.kind
    assert failure.scope_violation == []


def test_a_failure_stays_hashable():
    """A failure must remain usable as a dict key or a set member.

    The out-of-scope payload is a dict and ``scope_violation`` is a
    list, so a hash that folds in every field raises ``TypeError:
    unhashable type``. The executor counts and compares failures, and a
    value it cannot put in a set is a value it cannot count.
    """
    failure = classify_failure(
        f"undeclared_write: {FOREIGN_PATH}",
        project_dir=PROJECT_DIR,
    )
    plain = classify_failure(
        "AssertionError: expected 3 got 4",
        project_dir=PROJECT_DIR,
    )
    assert isinstance(hash(failure), int)
    assert isinstance(hash(plain), int)
    assert len({failure, plain}) == 2
