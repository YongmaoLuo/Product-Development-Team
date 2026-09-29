"""Critical regression test files existence guard.

The state-machine refactor rollout is split into ~15 tasks; each task
produces one or more test files.  If any one of those files silently
disappears — typically via a destructive ``git reset --hard`` on a
parallel branch or an over-eager cleanup — the backend's
acceptance report would happily pass even though the corresponding
regression is no longer pinned.  This guard fails loudly the moment
one of the 13 critical files is missing, regardless of why.

The contract:

  * Every file in :data:`CRITICAL_TEST_FILES` exists on disk.
  * Every file is non-empty (size > 0).
  * The fail message names the missing file path so a human
    operator can find and restore it immediately.

The 13 critical files come from the test-design decision point 9 of
the state-machine plan, which enumerates the regression anchors
that the acceptance suite must keep alive across the entire
refactor rollout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Project root — the parent of the ``backend/`` directory.
BACKEND_DIR = Path(__file__).resolve().parents[3]
PROJECT_ROOT = BACKEND_DIR.parent

# The 13 critical test files pinned by the test-design contract.
# Each entry is a path relative to PROJECT_ROOT (so the test works
# regardless of where pytest is invoked from).  The list is
# intentionally explicit; do NOT glob — the test must fail loudly
# if a file is renamed or moved.
CRITICAL_TEST_FILES: tuple[str, ...] = (
    "backend/state_machine/tests/unit/test_db_schema.py",
    "backend/state_machine/tests/unit/test_routing_repository.py",
    "backend/state_machine/tests/unit/test_execution_repository.py",
    "backend/state_machine/tests/unit/test_verification_repository.py",
    "backend/state_machine/tests/unit/test_artifact_repository.py",
    "backend/state_machine/tests/unit/test_archive_scan.py",
    "backend/state_machine/tests/unit/test_scheduler_support.py",
    "backend/state_machine/tests/unit/test_json_static_gate.py",
    "backend/state_machine/tests/unit/test_crash_recovery.py",
    "backend/state_machine/tests/integration/test_plan_routes.py",
    "backend/state_machine/tests/integration/test_verification_routes.py",
    "backend/state_machine/tests/integration/test_execution_routes.py",
    "backend/state_machine/tests/integration/test_subagent_verdict.py",
)


@pytest.mark.parametrize("relative_path", CRITICAL_TEST_FILES)
def test_critical_regression_files_exist(relative_path: str) -> None:
    """The critical test file must exist on disk and be non-empty.

    The test runs once per entry in :data:`CRITICAL_TEST_FILES`.  A
    missing or empty file fails this test, with a message that
    points the operator at the exact file path to restore.

    Why we check size > 0:

      A file that exists but is empty (e.g. someone ran
      ``git reset --hard`` to a commit where the file had been
      removed but the directory survived) would still let
      ``pytest --collect-only`` succeed.  The size check is a
      cheap, deterministic way to make that regression visible.
    """
    full_path = (PROJECT_ROOT / relative_path).resolve()
    assert full_path.exists(), (
        f"critical test file missing: {full_path} (relative: "
        f"{relative_path!r}).  This file is a regression anchor; "
        f"restore it before merging."
    )
    assert full_path.is_file(), (
        f"critical test file path is not a regular file: {full_path} "
        f"(relative: {relative_path!r})"
    )
    size = full_path.stat().st_size
    assert size > 0, (
        f"critical test file is empty (size=0): {full_path} "
        f"(relative: {relative_path!r}).  Restore its contents before "
        f"merging."
    )


def test_critical_files_collection_collects_all() -> None:
    """Sanity: every critical file contributes at least one collected test.

    This is a coarse end-to-end check that the 13 critical files are
    not just present but also importable / parseable by pytest.  A
    file that is present but contains only invalid Python (e.g. a
    stray ``git reset`` left a half-written blob) would fail
    collection; this test catches that case alongside the
    parametrised per-file existence test.

    Implementation: we run ``pytest --collect-only -q`` against the
    test layer and assert that the count of collected items is at
    least 1 per file (a loose lower bound — the real test for each
    file is its individual existence + the test it owns).
    """
    import subprocess
    import time

    start = time.time()
    proc = subprocess.run(
        [
            str(BACKEND_DIR / ".venv" / "bin" / "python3"),
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "state_machine/tests",
        ],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=60,
    )
    elapsed = time.time() - start
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")

    if proc.returncode not in (0, 5):
        pytest.fail(
            f"pytest --collect-only on state_machine/tests returned "
            f"{proc.returncode}; output: {output[:500]!r}"
        )

    # Count how many of the 13 critical files have at least one
    # collected test (i.e., appear in the output).
    missing: list[str] = []
    for relative_path in CRITICAL_TEST_FILES:
        if relative_path not in output:
            missing.append(relative_path)
    assert not missing, (
        f"{len(missing)}/{len(CRITICAL_TEST_FILES)} critical test files "
        f"have ZERO collected tests:\n  - " + "\n  - ".join(missing) +
        f"\ncollection elapsed {elapsed:.2f}s; verify the files are "
        f"present and importable"
    )
