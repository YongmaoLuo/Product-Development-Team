"""
test_agent_execute_task_first5.py — verify the **first 5 of 6** E2E
tests in ``test_agent_execute_task.py`` can be run independently and
all pass.

Background:
  The 6 end-to-end tests in ``test_agent_execute_task.py`` cover the
  full SubagentConfig → ``write_tmp_settings`` → ``ClaudeCodingTool``
  chain. Tests 1-5 exercise the per-task bootstrap-and-execute
  happy path (settings path, file on disk, 7 env fields, hook_stdin
  payload, task completion). Test 6 is the "2 tasks = 2 unique
  tmpfiles" invariant, which depends on the per-task regeneration
  refactor (commit 30f945b) and is structurally different
  (``num_tasks=2`` rather than ``num_tasks=1``).

  This module is the **second** of the 7-6 split — it isolates
  the first 5 tests from the 6th, so a failure in test 6 does
  not mask the pass/fail status of tests 1-5.

  Tests 1-5 share a single-task flow (a mock Claude query returns
  ``"TEST_RESULT: PASSED\\n"`` and the agent commits). They are
  expected to:
    * Collect cleanly (no import / fixture / syntax errors).
    * All print explicit ``PASSED`` lines.
    * Not have any ``FAILED`` / ``ERROR`` lines.
    * Complete in well under 30s wall time (they use fully mocked
      LLM, so the 30s budget is the backend harness's safety net, not
      a tight test-side constraint).

TDD spec (mirrors task 7-6-2):
  - ``test_first_5_tests_collect_without_error``:
      ``pytest --collect-only`` on the 5 selected tests exits 0
      and emits exactly 5 ``<Function>`` nodes with the expected
      names.
  - ``test_first_5_tests_all_pass``:
      ``pytest -v`` on the 5 selected tests exits 0 and emits
      exactly 5 ``... PASSED`` lines (one per test).
  - ``test_first_5_tests_wall_under_30s``:
      total elapsed wall time < 30s (no real LLM leak).
  - ``test_first_5_tests_no_failed_lines``:
      no ``... FAILED`` and no ``... ERROR`` lines for the 5
      selected tests in the pytest output.

Key constraints:
  * No modification to ``test_agent_execute_task.py`` or
    ``agent.py`` — this is a verification-only split.
  * No dependency on other test files (each gate runs pytest
    afresh with the same ``-k`` selector).
  * If a real ``CodingTool`` mock fails, it surfaces as a
    subprocess ``FAILED`` line — never silently swallowed.
  * The selector string must be a single ``-k`` argument so a
    typo in the regex is caught by pytest's own parser (exit 4).

Companion tasks:
  * 7-6-1 (this directory) — static structural existence check.
  * 7-6-2 (this file) — independent pass of the first 5 tests.
  * 7-6-3 — independent pass of the 6th (per-task tmpfile) test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

import pytest

# Every test here spawns ``pytest`` as a subprocess against the real task
# pipeline — that is the ``integration`` marker's definition ("spawns local
# subprocesses / drives the real server, but needs NO model"), and it is
# what this file was missing.
#
# Unmarked, it was collected by the *unit* shards, whose marker expression
# is ``not e2e and not integration`` and whose per-test ceiling is
# ``--timeout=60``. Three nested interpreter starts plus a full backend
# import do not fit in 60 seconds on a 2-core hosted runner, so
# ``test_first_5_tests_all_pass`` was killed mid-``subprocess.run`` on every
# CI run — and because that shard collects ``tests/`` in name order, it died
# at ~9% and never reached ``static_gates`` at all. The integration lane
# runs ``-m "integration and not e2e"`` with ``--timeout=120``, which is
# where this file belongs.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------


# This file lives at backend/tests/integration/<this>.py; the target
# file is its sibling. The project root is two levels up from this
# file (backend/tests/integration/<this>.py → backend/ → .).
_THIS_FILE = Path(__file__).resolve()
_INTEGRATION_DIR = _THIS_FILE.parent
_BACKEND_DIR = _INTEGRATION_DIR.parent
_PROJECT_ROOT = _BACKEND_DIR.parent

TARGET_FILE = _INTEGRATION_DIR / "test_agent_execute_task.py"

# The 5 tests we exercise. Order does not matter for pass/fail
# counting, but we list them in source-file order for human
# readability of the test output. The 6th test
# (test_execute_single_task_writes_tmpfile_unique_per_task) is
# deliberately excluded — it is the "2 tasks = 2 unique tmpfiles"
# invariant that depends on the per-task refactor.
SELECTED_TEST_NAMES: List[str] = [
    "test_execute_single_task_passes_settings_to_coding_tool",
    "test_execute_single_task_settings_file_exists",
    # 2026-09-14: renamed in the target file (has_7_env_fields →
    # has_endpoint_and_credentials) when env-field pinning moved to
    # CC Switch (model management left this repository entirely, commit 9717073).
    "test_execute_single_task_settings_has_endpoint_and_credentials",
    "test_execute_single_task_passes_hook_stdin",
    "test_execute_single_task_completes_on_test_pass",
]

# Build the ``-k`` selector exactly as the task spec dictates. A
# single ``-k or`` chain (not a comma) so a typo is caught by
# pytest's own parser rather than silently matching nothing.
SELECTOR = " or ".join(SELECTED_TEST_NAMES)

# Hard wall-time budget. The 5 tests use a fully-mocked coding
# tool, so the real runtime is ~1s; 30s is the backend harness
# safety net that any test-side timing assertion would use.
WALL_TIME_BUDGET_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_pytest(*args: str) -> Tuple[int, str, float]:
    """Run pytest as a subprocess and return ``(returncode, output, elapsed)``.

    The cwd is the project root so the project's ``pytest.ini``
    (``addopts = -v --strict-markers --tb=short ...``) is honored
    exactly as in the original test command. We invoke pytest
    through ``sys.executable`` (the current interpreter — which is
    the project's ``backend/.venv`` Python when the test is run
    under that venv) so the resolved imports match the parent
    pytest session.

    ``PDT_PROVIDER_PRIORITY`` is pinned in the subprocess env so the
    5 nested tests (which run ``autonomous_coding()`` against a
    fake DB whose provider IDs differ from the production
    ``provider-order.json``) are not derailed by the time-of-day
    vendor-b peak-hour degradation rule.  Pinned: 2026-06-17.
    """
    start = time.time()
    sub_env = dict(os.environ)
    sub_env["PDT_PROVIDER_PRIORITY"] = "vendor-a-pro,vendor-b"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        env=sub_env,
        timeout=60,  # hard cap; the real run is ~1s
    )
    elapsed = time.time() - start
    return (
        proc.returncode,
        (proc.stdout or "") + "\n" + (proc.stderr or ""),
        elapsed,
    )


def _parse_collected_names(output: str) -> List[str]:
    """Extract the list of collected test function names from
    ``pytest --collect-only`` output.

    pytest 8.4 emits a tree::

        <Module test_agent_execute_task.py>
          <Function test_execute_single_task_passes_settings_to_coding_tool>
          ...

    We also accept the flat ``tests/...py::TestClass::name`` form
    (older pytest versions) for robustness.
    """
    found: List[str] = []
    for name in SELECTED_TEST_NAMES:
        tree_pattern = rf"<\s*Function\s+{re.escape(name)}\s*>"
        flat_pattern = f"test_agent_execute_task.py::{name}"
        if re.search(tree_pattern, output) or flat_pattern in output:
            found.append(name)
    return found


def _parse_passed_lines(output: str) -> List[str]:
    """Return the list of ``... PASSED`` lines for the 5 expected tests.

    pytest -v emits one line per test, e.g.::

        backend/tests/integration/test_agent_execute_task.py::test_execute_single_task_passes_settings_to_coding_tool PASSED [ 20%]

    We require the literal ``::`` class separator immediately
    before the test name (which only pytest's status line carries),
    so a test name like ``test_failed_returns_minimal_report``
    containing the substring "FAILED" is not misread as a
    failure.
    """
    # NOTE: $ is the regex end-of-line anchor; do NOT escape it.
    passed_re = re.compile(
        rf"test_agent_execute_task\.py::(?P<name>\S+) PASSED"
        rf"(?: \[\s*\d+%\]|$)",
        re.MULTILINE,
    )
    found: List[str] = []
    seen: set = set()
    for match in passed_re.finditer(output):
        name = match.group("name")
        if name in SELECTED_TEST_NAMES and name not in seen:
            found.append(match.group(0).rstrip())
            seen.add(name)
    return found


def _parse_failed_lines(output: str) -> List[str]:
    """Return the list of ``... FAILED`` lines for the 5 expected tests.

    Same shape as the PASSED parser. The ``::`` class separator
    anchors the match so test-name substrings cannot trigger
    false positives.
    """
    # NOTE: $ is the regex end-of-line anchor; do NOT escape it.
    failed_re = re.compile(
        rf"test_agent_execute_task\.py::(?P<name>\S+) FAILED"
        rf"(?: -|$)",
        re.MULTILINE,
    )
    return [
        match.group(0).rstrip()
        for match in failed_re.finditer(output)
        if match.group("name") in SELECTED_TEST_NAMES
    ]


def _parse_error_lines(output: str) -> List[str]:
    """Return the list of ``... ERROR`` lines for the 5 expected tests."""
    # NOTE: $ is the regex end-of-line anchor; do NOT escape it.
    error_re = re.compile(
        rf"test_agent_execute_task\.py::(?P<name>\S+) ERROR"
        rf"(?: -|$)",
        re.MULTILINE,
    )
    return [
        match.group(0).rstrip()
        for match in error_re.finditer(output)
        if match.group("name") in SELECTED_TEST_NAMES
    ]


def _has_collection_error(output: str) -> bool:
    """Detect ``ERROR`` lines or ``errors during collection`` markers.

    ``ERROR <path>`` at the start of a line is pytest's report for
    a collection error (import, fixture not found, syntax). It
    is distinct from a per-test ``... ERROR`` outcome (which
    ``_parse_error_lines`` already captures).
    """
    if re.search(r"^ERROR\s+", output, re.MULTILINE):
        return True
    if "errors during collection" in output:
        return True
    return False


# ---------------------------------------------------------------------------
# Pre-flight — target file must exist
# ---------------------------------------------------------------------------


def _assert_target_file_exists() -> None:
    """Fail fast with a clear message if the target file is missing.

    The 7-6 fix (commit 30f945b) introduced ``test_agent_execute_task.py``
    and we depend on it. A regression that reverts that commit
    must surface here, not as a confusing "0 tests collected" from
    pytest.
    """
    assert TARGET_FILE.exists(), (
        f"Target test file does not exist at {TARGET_FILE}. "
        "Re-apply commit 30f945b (test(agent): execute_single_task "
        "端到端集成测试 — SubagentConfig 真落盘) or recreate the file. "
        "The 7-6-2 verification cannot run without the 6 E2E tests."
    )


# ---------------------------------------------------------------------------
# Test 1 — collection
# ---------------------------------------------------------------------------


def test_first_5_tests_collect_without_error() -> None:
    """``pytest --collect-only`` on the 5 tests exits 0 and lists 5 names.

    This catches:
      * ImportError / SyntaxError in the target test file.
      * ``MissingFixture`` for ``tmp_path`` / ``monkeypatch``
        (would show up as ``fixture ... not found``).
      * Bad ``-k`` selector regex — pytest would error out with
        "ERROR: '-k' must not be empty" or similar.

    The 5 collected names must be exactly the 5 expected ones
    (in any order, but typically source-file order).
    """
    _assert_target_file_exists()

    returncode, output, elapsed = _run_pytest(
        str(TARGET_FILE.relative_to(_PROJECT_ROOT)),
        "--collect-only",
        "-q",
        "-k", SELECTOR,
    )

    assert returncode == 0, (
        f"pytest --collect-only returned {returncode} (expected 0). "
        f"Output (first 1000 chars):\n{output[:1000]}"
    )

    assert not _has_collection_error(output), (
        f"pytest reported a collection error. "
        f"Output (first 1000 chars):\n{output[:1000]}"
    )

    found = _parse_collected_names(output)
    assert len(found) == len(SELECTED_TEST_NAMES), (
        f"Expected {len(SELECTED_TEST_NAMES)} collected tests, "
        f"got {len(found)}: {found}. "
        f"Expected: {SELECTED_TEST_NAMES}. "
        f"Output (first 1000 chars):\n{output[:1000]}"
    )

    missing = [n for n in SELECTED_TEST_NAMES if n not in found]
    assert not missing, (
        f"Collector missed {len(missing)} expected test(s): {missing}. "
        f"Found: {found}. "
        f"Check the ``-k`` selector in SELECTED_TEST_NAMES."
    )

    # Collection is fast — should complete in well under 10s.
    assert elapsed < 10, (
        f"pytest --collect-only took {elapsed:.2f}s, exceeds 10s budget"
    )


# ---------------------------------------------------------------------------
# Test 2 — all 5 tests pass
# ---------------------------------------------------------------------------


def test_first_5_tests_all_pass() -> None:
    """``pytest -v`` on the 5 tests exits 0 and prints 5 ``PASSED`` lines.

    The subprocess must use the exact same ``-k`` selector as
    the task spec. The mocked LLM should make this run in ~1s.
    """
    _assert_target_file_exists()

    returncode, output, elapsed = _run_pytest(
        str(TARGET_FILE.relative_to(_PROJECT_ROOT)),
        "-v",
        "--tb=short",
        "--no-header",
        "-k", SELECTOR,
    )

    assert returncode == 0, (
        f"pytest returned {returncode} (expected 0). "
        f"Output (first 2000 chars):\n{output[:2000]}"
    )

    passed_lines = _parse_passed_lines(output)
    assert len(passed_lines) == len(SELECTED_TEST_NAMES), (
        f"Expected exactly {len(SELECTED_TEST_NAMES)} PASSED lines, "
        f"got {len(passed_lines)}: {passed_lines}. "
        f"Output (first 2000 chars):\n{output[:2000]}"
    )

    # Also: the pytest summary at the end must say "5 passed".
    # This guards against a scenario where individual PASSED lines
    # print but the summary is missing or says something else
    # (rare, but possible with custom addopts overrides).
    summary_match = re.search(r"=+ (\d+ passed)", output)
    assert summary_match, (
        f"pytest summary line not found in output. "
        f"Output (first 2000 chars):\n{output[:2000]}"
    )
    summary_count = int(summary_match.group(1).split()[0])
    assert summary_count == len(SELECTED_TEST_NAMES), (
        f"pytest summary says {summary_count} passed, "
        f"expected {len(SELECTED_TEST_NAMES)}. "
        f"Output (first 2000 chars):\n{output[:2000]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — wall time
# ---------------------------------------------------------------------------


def test_first_5_tests_wall_under_30s() -> None:
    """Total wall time for the 5 tests is well under 30s.

    The mocked ``ClaudeCodingTool.query`` returns instantly and
    no real LLM is invoked. A real LLM call would blow the 30s
    budget — if this test fails after the 6th-test fix, it is a
    strong signal that the mock was bypassed.
    """
    _assert_target_file_exists()

    returncode, output, elapsed = _run_pytest(
        str(TARGET_FILE.relative_to(_PROJECT_ROOT)),
        "-v",
        "--no-header",
        "-k", SELECTOR,
    )

    assert returncode == 0, (
        f"pytest returned {returncode} (expected 0); elapsed={elapsed:.2f}s. "
        f"Output (first 2000 chars):\n{output[:2000]}"
    )

    assert elapsed < WALL_TIME_BUDGET_SECONDS, (
        f"5 tests took {elapsed:.2f}s, exceeds {WALL_TIME_BUDGET_SECONDS}s "
        f"budget. A real LLM call likely leaked into the tests — "
        f"check the CodingTool mock fixture."
    )


# ---------------------------------------------------------------------------
# Test 4 — no FAILED / ERROR lines
# ---------------------------------------------------------------------------


def test_first_5_tests_no_failed_lines() -> None:
    """No ``... FAILED`` and no ``... ERROR`` lines for the 5 selected tests.

    The ``::`` separator anchors the regex so test names containing
    the substring "FAILED" or "ERROR" cannot trigger a false
    positive.
    """
    _assert_target_file_exists()

    returncode, output, _elapsed = _run_pytest(
        str(TARGET_FILE.relative_to(_PROJECT_ROOT)),
        "-v",
        "--no-header",
        "-k", SELECTOR,
    )

    failed = _parse_failed_lines(output)
    errored = _parse_error_lines(output)

    assert not failed, (
        f"Found {len(failed)} FAILED line(s) for the 5 selected tests: "
        f"{failed}. Output (first 2000 chars):\n{output[:2000]}"
    )

    assert not errored, (
        f"Found {len(errored)} ERROR line(s) for the 5 selected tests: "
        f"{errored}. Output (first 2000 chars):\n{output[:2000]}"
    )

    # returncode must also be 0 (defense in depth: pytest may exit
    # non-zero even when individual lines look clean — e.g.
    # collection warnings under --strict-markers).
    assert returncode == 0, (
        f"pytest returned {returncode} (expected 0) even though no "
        f"FAILED/ERROR lines were found. "
        f"Output (first 2000 chars):\n{output[:2000]}"
    )
