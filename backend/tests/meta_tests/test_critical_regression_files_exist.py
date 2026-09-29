"""VP-025 / critical regression files existence meta-test.

Contract: the named critical regression-binding files (bug anchor cases,
JSON-absent static gate, table-column isolation assertions, cross-process
verdict conservation, kill-harness cutpoints) must exist on disk under
``backend/tests/``.  If any of them is missing, the regression lock for
the corresponding invariant is silently lost — and a future ``git
reset`` / branch rebase that drops a file will not be caught by any
other test in the suite, because the tests themselves are gone.

This meta-test acts as a self-preservation guard: it does not exercise
any invariant, it just asserts that the tests that *do* exercise those
invariants are still present in the working tree.  CI fails fast on a
missing file before the silent-coverage-loss reaches production.

The list below is the authoritative inventory; if a new critical
regression file is added to the suite, register it here.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# Each entry: relative path under backend/tests/.
# This is the authoritative inventory for VP-025.
CRITICAL_REGRESSION_FILES = (
    # bug_2 anchor (CAS 409 on concurrent start)
    "test_bug_2_cas_409_anchor.py",
    # bug_1 anchor (cross-table isolation — execution must not touch verification rows)
    "unit/repositories/test_cross_table_isolation.py",
    # bug_3 / bug_4 anchors (kill-harness cutpoints for crash recovery)
    "crash_recovery/test_kill_before_commit_leaves_no_trace.py",
    "crash_recovery/test_kill_after_commit_persists_change.py",
    "crash_recovery/test_kill_mid_scheduler_tick_no_ghost_start.py",
    "crash_recovery/test_kill_mid_execution_recovers_consistent_progress.py",
    "crash_recovery/test_kill_mid_verification_recovers_consistent_round.py",
    # bug_4 anchor (scheduler re-reads external DB change)
    "unit/scheduler/test_scheduler_tick_reads_external_db_change.py",
    # bug_5 anchor (cross-process verdict conservation / atomic append)
    "concurrency/test_concurrent_verdict_append_no_lost_update.py",
    # acceptance_4 anchors (JSON state-file extinction gate)
    "static_gates/test_no_json_state_filename_in_backend_source.py",
    "static_gates/test_e2e_lifecycle_produces_no_state_json.py",
)


@pytest.fixture(scope="module")
def backend_tests_root() -> Path:
    return Path(__file__).resolve().parent.parent  # backend/tests/


def test_every_critical_regression_file_exists(
    backend_tests_root: Path,
) -> None:
    """Every named critical regression-binding file must exist on disk.

    A missing file means the regression lock for that invariant is
    silently gone — future refactors can break the contract and CI will
    stay green because the test that would have caught it is no longer
    in the tree.  Fail loudly so the loss is visible at PR time.
    """
    missing: list = []
    for rel_path in CRITICAL_REGRESSION_FILES:
        abs_path = backend_tests_root / rel_path
        if not abs_path.is_file():
            missing.append(rel_path)
    assert not missing, (
        "the following critical regression-binding files are missing from "
        "backend/tests/; their regression locks have been silently lost "
        "(a git reset / branch rebase / accidental rm likely).  Restore "
        "each file or remove its entry from this meta-test's inventory:\n  "
        + "\n  ".join(missing)
    )


def test_critical_regression_inventory_is_non_empty() -> None:
    """Sanity: the inventory itself must not be empty.

    An empty inventory means someone deleted every entry from
    ``CRITICAL_REGRESSION_FILES`` — which would silently disable this
    meta-test's self-preservation guarantee.  We pin a hard floor so
    that "delete all entries to make CI green" cannot work.
    """
    assert len(CRITICAL_REGRESSION_FILES) >= 5, (
        "CRITICAL_REGRESSION_FILES inventory shrank below the floor of 5; "
        "the meta-test's self-preservation guarantee has been compromised"
    )
