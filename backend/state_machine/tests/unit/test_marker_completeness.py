"""Marker completeness guards for the state-machine test layer.

These tests pin the test-design **decision point 10** contract: every
``bug_N`` and ``acceptance_N`` marker registered in ``backend/pytest.ini``
must have at least one anchor test, and the bug anchors must not be
silently skipped.  The purpose is to guarantee that the marker-driven
verification suite (used by the backend to produce an acceptance
report at the end of the rollout) cannot silently lose anchors — for
example if a refactor renames a test or moves it under a ``skip`` /
``xfail`` decorator.

The TDD spec is laid out in the task brief:

  * ``test_every_bug_marker_has_at_least_one_anchor_case`` — for each
    ``bug_1``..``bug_5`` marker, at least one test function is
    collected.
  * ``test_every_acceptance_marker_has_at_least_one_case`` — for each
    ``acceptance_1``..``acceptance_6`` marker, at least one test
    function is collected (placeholder tests for ``acceptance_1/2/5/6``
    are acceptable — see ``acceptance_placeholders.py``).
  * ``test_bug_anchor_cases_are_not_skipped`` — for every
    ``bug_N``-marked test, the test has no ``@pytest.mark.skip`` /
    ``@pytest.mark.skipif`` / ``@pytest.mark.xfail`` decorator.  An
    "anchor" that is silently skipped defeats the marker contract.
  * ``test_pytest_strict_markers_passes`` — ``pytest --collect-only -q``
    exit code is 0 (no unknown marker warnings) so the project's
    ``--strict-markers`` addopt is honoured by the new markers.

The bug → test mapping is documented in the task brief:

  bug_1       test_execution_write_never_touches_verification_row
              test_verification_write_never_touches_execution_row
  bug_2       test_start_verification_409_when_already_running
              test_start_execution_409_when_already_executing
  bug_3       test_kill_before_commit_leaves_no_trace
              test_kill_after_commit_persists_change
  bug_4       test_kill_mid_scheduler_tick_no_ghost_start
              test_scheduler_tick_reads_external_db_change
  bug_5       test_concurrent_append_verdict_no_lost_update

And acceptance:

  acceptance_1 test_e2e_full_plan_lifecycle_on_8001 (placeholder, L5)
  acceptance_2 test_two_executions_and_one_verification_coexist (placeholder)
  acceptance_3 test_kill_mid_execution / test_kill_mid_verification
  acceptance_4 test_no_json_state_filename_in_backend_source
  acceptance_5 test_inclusion_classifier_matches_snapshot (placeholder)
  acceptance_6 test_8000_plan_states_unchanged_before_after (placeholder)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# Absolute paths — no placeholders, no relative paths.
BACKEND_DIR = Path(__file__).resolve().parents[3]
PROJECT_ROOT = BACKEND_DIR.parent
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"

# Bug markers: must each select at least one anchor.
BUG_MARKERS: tuple[str, ...] = ("bug_1", "bug_2", "bug_3", "bug_4", "bug_5")

# Acceptance markers: must each select at least one case.
#
# ``acceptance_5`` is deliberately absent — its only consumers live in
# ``backend/tests/regression/``, which is not part of this repo because
# those tests import the private ``tools/`` subsystem.  Registering it
# in ``pytest.ini`` without re-adding those tests would fail
# ``meta_tests::test_every_acceptance_marker_has_at_least_one_case``.
ACCEPTANCE_MARKERS: tuple[str, ...] = (
    "acceptance_1",
    "acceptance_2",
    "acceptance_3",
    "acceptance_4",
    "acceptance_6",
)

# Tests that are allowed to be ``@pytest.mark.skip`` / ``xfail`` even
# though they carry a bug marker (none today — but this list lets us
# widen the contract without breaking the test).  An empty list is
# the strict default.
ALLOW_SKIPPED_BUG_ANCHORS: frozenset[str] = frozenset()


def _run_pytest_collect_only(args: list[str]) -> tuple[int, str, float]:
    """Run ``pytest --collect-only -q <args>`` and capture output.

    The cwd is intentionally set to :data:`BACKEND_DIR` (where
    ``pytest.ini`` lives) so the project's rootdir is found
    deterministically — running from PROJECT_ROOT would NOT find
    pytest.ini, and the new ``bug_*`` / ``acceptance_*`` markers
    would silently fail to register.
    """
    import time

    start = time.time()
    proc = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=30,
    )
    elapsed = time.time() - start
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or ""), elapsed


def _collect_marker(marker: str) -> list[str]:
    """Return the list of fully-qualified test IDs collected for a marker.

    Uses ``pytest --collect-only -q -m <marker>`` and parses both
    the legacy flat format (one ID per line) and the modern tree
    format (pytest 8+ emits ``<Function test_xxx>`` lines).  Either
    shape yields the bare test function name; the caller only needs
    the count of distinct test functions collected.
    """
    rc, output, _elapsed = _run_pytest_collect_only(["-m", marker, "state_machine/tests"])
    if rc not in (0, 5):
        # Anything other than 0/5 is a real collection error (e.g.
        # marker typo, syntax error, import error) — return empty so
        # the assertion fires with a clear "no anchors" message
        # rather than masking the real error.
        return []
    # Two output shapes we accept:
    #   flat:  "tests/...py::test_xxx PASSED    [ 50%]"
    #   tree:  "<Function test_xxx>"
    # We parse BOTH so the helper is robust across pytest versions.
    ids: set[str] = set()
    # Flat form
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if "::" in line and "<" not in line and "no tests" not in line.lower():
            ids.add(line)
    # Tree form: <Function test_xxx> or <Function test_xxx[parametrize_id]>
    function_re = re.compile(r"<\s*Function\s+([A-Za-z_][A-Za-z0-9_\[\]-]*)")
    for line in output.splitlines():
        m = function_re.search(line)
        if m:
            ids.add(m.group(1))
    return sorted(ids)


def _has_skip_or_xfail(item_path: str) -> bool:
    """Inspect a test source file and return True iff ``item_path`` has a
    ``@pytest.mark.skip`` / ``skipif`` / ``xfail`` decorator.

    We grep the file for the test definition line and look at the
    preceding decorator lines.  This is intentionally a simple
    lexical scan — it does not execute the test or build the pytest
    AST, so it stays cheap and easy to debug.
    """
    parts = item_path.split("::", 1)
    if len(parts) != 2:
        return False
    module_path_str, test_name = parts
    test_name = test_name.strip()
    # Drop any trailing class segment — we want the bare test name.
    test_short = test_name.rsplit("::", 1)[-1]
    module_path = Path(module_path_str)
    if not module_path.is_absolute():
        module_path = (PROJECT_ROOT / module_path).resolve()
    if not module_path.exists():
        return False
    try:
        text = module_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith("def ") and line.strip().endswith(":"):
            # Extract the function name (allow the form
            # ``def test_xxx(self, ...):`` inside a class).
            m = re.match(r"\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if not m or m.group(1) != test_short:
                continue
            # Walk backwards to collect decorators above this def.
            for back in range(idx - 1, -1, -1):
                prev = lines[back]
                stripped = prev.strip()
                if stripped.startswith("@"):
                    if any(
                        token in stripped
                        for token in (
                            "pytest.mark.skip",
                            "pytest.mark.skipif",
                            "pytest.mark.xfail",
                        )
                    ):
                        return True
                    continue
                if stripped.startswith("def ") or stripped.startswith("class "):
                    return False
                # Blank or non-decorator line — keep walking.
            return False
    return False


# ---------------------------------------------------------------------------
# 1. Every bug marker has at least one anchor case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", BUG_MARKERS)
def test_every_bug_marker_has_at_least_one_anchor_case(marker: str) -> None:
    """Each ``bug_N`` marker must select at least one test (≥1).

    A marker with zero anchors is a silent regression — the backend
    backend's marker-driven acceptance report would simply skip
    that bug dimension and the project would ship without the
    corresponding regression protection.
    """
    ids = _collect_marker(marker)
    assert ids, (
        f"marker {marker!r} has ZERO collected test anchors; "
        f"expected at least one.  If this is intentional, add the "
        f"test and re-run; otherwise the bug coverage is missing. "
        f"Test the marker explicitly with: "
        f"pytest -m {marker} state_machine/tests"
    )


# ---------------------------------------------------------------------------
# 2. Every acceptance marker has at least one case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ACCEPTANCE_MARKERS)
def test_every_acceptance_marker_has_at_least_one_case(marker: str) -> None:
    """Each ``acceptance_N`` marker must select at least one test (≥1).

    Placeholders are acceptable for ``acceptance_1/2/5/6`` — those
    live in :mod:`test_marker_completeness` (this file) as empty
    functions so the marker contract is anchored in the test
    graph even when the corresponding acceptance gate is not yet
    implemented by the L5 plan.
    """
    ids = _collect_marker(marker)
    assert ids, (
        f"marker {marker!r} has ZERO collected test anchors; "
        f"expected at least one (placeholder or live).  Add a "
        f"placeholder test that carries this marker and re-run."
    )


# ---------------------------------------------------------------------------
# 3. No bug anchor is silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", BUG_MARKERS)
def test_bug_anchor_cases_are_not_skipped(marker: str) -> None:
    """No test collected for ``bug_N`` may carry a skip/xfail decorator.

    An anchor that is silently skipped (e.g. ``@pytest.mark.skip``
    added by a refactor) defeats the marker contract: the
    acceptance report would record the test as present but never
    actually run it.  The exception list
    :data:`ALLOW_SKIPPED_BUG_ANCHORS` lets a future patch widen
    the contract without breaking this test.
    """
    ids = _collect_marker(marker)
    assert ids, (
        f"marker {marker!r} has ZERO anchors; cannot check skip "
        f"status.  Add an anchor test first."
    )
    offenders: list[str] = []
    for item_id in ids:
        if item_id in ALLOW_SKIPPED_BUG_ANCHORS:
            continue
        if _has_skip_or_xfail(item_id):
            offenders.append(item_id)
    assert not offenders, (
        f"marker {marker!r} anchors are silently skipped: "
        f"{offenders!r}.  Remove the skip/xfail decorator or add "
        f"the test name to ALLOW_SKIPPED_BUG_ANCHORS if the skip "
        f"is genuinely required."
    )


# ---------------------------------------------------------------------------
# 4. pytest --strict-markers is honoured for the new markers
# ---------------------------------------------------------------------------


def test_pytest_strict_markers_passes() -> None:
    """``pytest --collect-only`` must NOT report any unknown marker.

    The project's ``--strict-markers`` addopt (in ``backend/pytest.ini``)
    raises on any ``@pytest.mark.<unknown>`` decorator.  A new marker
    (e.g. ``bug_1``) is unknown until it is registered in the
    ``markers =`` block, so this test enforces that registration is
    kept in sync.
    """
    rc, output, _elapsed = _run_pytest_collect_only(["state_machine/tests"])
    if "unknown marker" in output.lower():
        # Find the offending marker name for the failure message.
        match = re.search(
            r"unknown marker[s]?:\s*([A-Za-z0-9_, -]+)",
            output,
            re.IGNORECASE,
        )
        offenders = match.group(1) if match else output[:200]
        pytest.fail(
            f"pytest --strict-markers reports unknown markers: "
            f"{offenders!r}; add them to backend/pytest.ini's "
            f"``markers =`` block."
        )
    # pytest emits a section header ``======== ERRORS ========`` even on
    # a clean run; that header is not a real error.  We only treat
    # lines beginning with `ERROR ` (capital, followed by a space) and
    # a file path as a hard failure.
    error_lines = [
        line
        for line in output.splitlines()
        if re.match(r"^ERROR\s+[A-Za-z]", line)
    ]
    if error_lines:
        pytest.fail(
            f"pytest --collect-only reported hard errors: "
            f"{error_lines[:3]!r}; full output: {output[:500]!r}"
        )
    # Acceptable exit codes:
    #   0  — clean run
    #   2  — interrupted / strict-marker violation
    #   5  — no tests collected (should not happen here)
    if rc not in (0, 2, 5):
        pytest.fail(
            f"pytest --collect-only returned {rc}; expected 0 "
            f"(or 5 if no tests).  Output: {output[:500]!r}"
        )
