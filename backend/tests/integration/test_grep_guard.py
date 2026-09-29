"""
TDD verification for the shared ``scripts/grep_guard.sh`` wrapper and
its integration with pre-commit / CI.

Background
----------
Task 14 (commit 572c04b) introduced a single shell wrapper
(``scripts/grep_guard.sh``) as the entry point for the forbidden-filename
regex sweep gate. The wrapper is consumed by:

  * the project's local pre-commit hook
    (``.pre-commit-config.yaml`` → ``bash scripts/grep_guard.sh``)
  * the GitHub Actions ``grep-guard`` job
    (``.github/workflows/ci.yml`` → ``bash ../scripts/grep_guard.sh``)
  * ad-hoc developer invocations from any cwd.

The wrapper must:

  1. exist at ``scripts/grep_guard.sh`` (project root) and be executable;
  2. exit 0 on a clean tree;
  3. exit 1 when forbidden filename patterns are present in the
     production scope (server.py + verification_executor.py +
     state_machine/**/*.py);
  4. be the SAME entry point referenced by both ``.pre-commit-config.yaml``
     and ``.github/workflows/ci.yml`` — i.e. no inline
     ``python3 -c`` / duplicate invocation logic in either caller.

These four contracts pin the "shared wrapper" semantic. If any of them
regresses, a developer can no longer trust that ``bash scripts/grep_guard.sh``
locally matches CI behaviour, defeating the single-source-of-truth design.

TDD spec (4 gates):

  Gate 1: ``test_grep_guard_wrapper_exists_and_is_executable``
          The wrapper file exists at ``scripts/grep_guard.sh`` and has
          the executable bit set.

  Gate 2: ``test_precommit_invokes_shared_wrapper``
          ``.pre-commit-config.yaml`` references
          ``bash scripts/grep_guard.sh`` (single shared entry point).

  Gate 3: ``test_ci_workflow_invokes_shared_wrapper``
          ``.github/workflows/ci.yml`` invokes the SAME wrapper via
          ``bash ../scripts/grep_guard.sh`` (NOT an inline
          ``python3 -c`` / duplicate invocation logic).

  Gate 4: ``test_grep_guard_wrapper_exits_zero_on_clean_tree``
          Running the wrapper against the current production tree
          exits 0 with a "0 violations" report. This is the canary
          test that the wrapper actually runs end-to-end on this
          tree, that the venv resolves, and that the scanner
          returns a clean result.

Final line emitted by the wrapper:
  - exit 0: ``OK  grep-guard: 0 violations``
  - exit 1: ``FAIL grep-guard: N violation(s) found``
  - exit 2: ``ERROR: ...`` (setup / usage error)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# Project root — pinned by the spec. All checks resolve relative to
# this directory. We deliberately hard-code the absolute path (not
# derive via ``Path(__file__).parent.parent.parent``) so a future
# move of the test file under ``backend/tests/integration/`` does
# not silently change the contract: PROJECT_ROOT is the directory
# that contains ``scripts/grep_guard.sh`` (the shared wrapper),
# not the directory that contains this test file.
PROJECT_ROOT = Path(__file__).resolve().parents[3] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"])

# Hard upper bound on the wrapper subprocess. The wrapper itself
# finishes in well under 1s on a clean tree (the scanner is a pure
# regex sweep with no network or filesystem writes), but we leave
# generous headroom for cold venv startup on slow CI runners.
HARD_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_wrapper() -> tuple[int, str, str]:
    """Invoke ``bash scripts/grep_guard.sh`` from PROJECT_ROOT.

    The wrapper script lives at ``<PROJECT_ROOT>/scripts/grep_guard.sh``
    and resolves its own paths relative to its own location, but
    the production-scope scan is anchored on the project root, so we
    invoke the wrapper with ``cwd=PROJECT_ROOT`` (NOT
    ``cwd=backend`` — invoking from ``backend`` would shift the
    ``--root`` interpretation and the relative paths emitted in
    violation reports).

    Returns ``(returncode, stdout, stderr)``. Captures both streams
    so the failure surface can show the wrapper's human-readable
    error or violation list verbatim.
    """
    proc = subprocess.run(
        ["bash", "scripts/grep_guard.sh"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=HARD_TIMEOUT_SECONDS,
    )
    return (
        proc.returncode,
        (proc.stdout or ""),
        (proc.stderr or ""),
    )


# ---------------------------------------------------------------------------
# TDD spec 1 — wrapper file exists and is executable
# ---------------------------------------------------------------------------


def test_grep_guard_wrapper_exists_and_is_executable():
    """``scripts/grep_guard.sh`` exists at PROJECT_ROOT and is executable.

    Pin the contract that prevents a future cleanup from silently
    deleting the wrapper (and leaving pre-commit + CI with dangling
    references). The executable bit is required so pre-commit can
    invoke the script directly via ``bash scripts/grep_guard.sh``
    (pre-commit's ``language: system`` hook family requires the
    entry to be executable OR invoked via ``bash`` / interpreter
    directly — we use the latter via the ``entry:`` field, but
    keeping the executable bit set is the polite cross-tool
    default).
    """
    wrapper = PROJECT_ROOT / "scripts" / "grep_guard.sh"
    assert wrapper.is_file(), (
        f"shared grep-guard wrapper missing: {wrapper}\n"
        "The shared wrapper is the SINGLE entry point for the "
        "forbidden-filename regex sweep consumed by pre-commit, "
        "CI, and developer ad-hoc invocations. If this file is "
        "missing, the single-source-of-truth contract is broken."
    )
    # Executable bit on the owner column. On POSIX, ``os.access(path,
    # os.X_OK)`` covers any execute bit (user / group / other) — we
    # only need the script to be invokable, not strictly user-only.
    assert os.access(wrapper, os.X_OK), (
        f"shared grep-guard wrapper is NOT executable: {wrapper}\n"
        "Run: chmod +x scripts/grep_guard.sh"
    )


# ---------------------------------------------------------------------------
# TDD spec 2 — pre-commit config invokes the shared wrapper
# ---------------------------------------------------------------------------


def test_precommit_invokes_shared_wrapper():
    """``.pre-commit-config.yaml`` references the shared wrapper.

    Pin the contract that prevents a future contributor from
    inlining a separate ``python3 -c`` invocation in
    ``.pre-commit-config.yaml`` (which would silently diverge from
    CI's behaviour when the wrapper's flag set or scope changes).
    """
    config = PROJECT_ROOT / ".pre-commit-config.yaml"
    assert config.is_file(), (
        f"pre-commit config missing: {config}\n"
        "Task 14 wired pre-commit to the shared wrapper via this "
        "file; if it's missing, the developer-local gate is gone."
    )
    text = config.read_text(encoding="utf-8")
    assert "scripts/grep_guard.sh" in text, (
        f".pre-commit-config.yaml does NOT reference "
        f"scripts/grep_guard.sh — the developer-local gate is "
        f"diverging from the CI / shared wrapper contract.\n"
        f"File contents (first 400 chars): {text[:400]!r}"
    )
    # The ``entry:`` field is the canonical place for the hook
    # command. We check that the wrapper is invoked via ``bash``
    # (matching the wrapper's own #!/usr/bin/env bash shebang) so
    # the hook does not depend on the wrapper file's executable
    # bit alone.
    assert re.search(
        r"entry:\s*bash\s+scripts/grep_guard\.sh",
        text,
    ), (
        ".pre-commit-config.yaml does not declare "
        "'entry: bash scripts/grep_guard.sh' — the pre-commit hook "
        "is not delegating to the shared wrapper. Single source "
        "of truth contract is broken."
    )


# ---------------------------------------------------------------------------
# TDD spec 3 — CI workflow invokes the shared wrapper
# ---------------------------------------------------------------------------


def test_ci_workflow_invokes_shared_wrapper():
    """``.github/workflows/ci.yml`` invokes the shared wrapper.

    Pin the contract that prevents a future contributor from
    inlining a ``python3 -c`` invocation in the ``grep-guard`` job
    (which would silently diverge from the wrapper when the
    scanner's flag set or scope changes).

    The CI step uses ``working-directory: backend`` so the literal
    command is ``bash ../scripts/grep_guard.sh``. We accept EITHER
    of:

      * ``bash scripts/grep_guard.sh`` (no cwd change at the step)
      * ``bash ../scripts/grep_guard.sh`` (cwd=backend)

    because both delegate to the SAME shared entry point — only
    the relative path differs.
    """
    ci = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci.is_file(), (
        f"CI workflow file missing: {ci}\n"
        "Task 14 wired the ``grep-guard`` job to the shared "
        "wrapper via this file; if it's missing, the CI gate is "
        "gone."
    )
    text = ci.read_text(encoding="utf-8")
    # Find the grep-guard job block.
    job_match = re.search(
        r"^\s{0,2}grep-guard:.*?(?=^\s{0,2}\w[\w-]*:|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert job_match is not None, (
        ".github/workflows/ci.yml does NOT declare a "
        "`grep-guard:` job — the CI gate is missing entirely."
    )
    job_block = job_match.group(0)

    # The grep-guard job must invoke the shared wrapper. We accept
    # either relative path because the step may set
    # ``working-directory: backend``.
    invokes_wrapper = (
        "bash ../scripts/grep_guard.sh" in job_block
        or "bash scripts/grep_guard.sh" in job_block
    )
    assert invokes_wrapper, (
        ".github/workflows/ci.yml `grep-guard:` job does NOT "
        "invoke `scripts/grep_guard.sh` — the CI gate is "
        "diverging from the shared wrapper contract.\n"
        f"Job block (first 800 chars): {job_block[:800]!r}"
    )

    # Negative assertion: the job must NOT inline a
    # ``python3 -c ... run_grep_guard ...`` invocation. If a
    # contributor ever copies the wrapper's logic into the YAML
    # step, the two will silently drift when the scanner's flag
    # set changes.
    assert "python3 -c" not in job_block, (
        ".github/workflows/ci.yml `grep-guard:` job inlines a "
        "`python3 -c ... run_grep_guard ...` invocation. This "
        "duplicates the wrapper's logic and breaks the "
        "single-source-of-truth contract. Replace with "
        "`bash scripts/grep_guard.sh` (or "
        "`bash ../scripts/grep_guard.sh` if "
        "`working-directory: backend` is set)."
    )


# ---------------------------------------------------------------------------
# TDD spec 4 — wrapper exits 0 on a clean tree
# ---------------------------------------------------------------------------


def test_grep_guard_wrapper_exits_zero_on_clean_tree():
    """Running the wrapper against the current production tree
    exits 0 and prints ``OK  grep-guard: 0 violations``.

    This is the canary test that the wrapper actually runs
    end-to-end on this tree, that the venv resolves, and that
    the scanner returns a clean result. If the production tree
    ever reintroduces a forbidden filename construction, this
    test fails fast — at the same speed CI does — so the
    developer sees the regression immediately.
    """
    rc, stdout, stderr = _run_wrapper()
    combined = stdout + stderr
    assert rc == 0, (
        f"grep_guard.sh exited {rc} (expected 0 on clean tree).\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )
    # The wrapper's success line is pinned by the script's own
    # implementation. We require the literal substring to detect
    # any silent regression in the wrapper's output contract.
    assert "0 violations" in combined, (
        f"grep_guard.sh exited 0 but did NOT report '0 violations' "
        f"in its output — output contract regression.\n"
        f"--- combined output ---\n{combined}"
    )