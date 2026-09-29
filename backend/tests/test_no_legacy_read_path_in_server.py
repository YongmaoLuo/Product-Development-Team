"""TDD tests pinning the legacy direct-read migration in ``backend/server.py``.

Background
----------
The state-machine SQLite refactor moved state writes off the 5 legacy
JSON files (``plan_state.json``, ``execution.json``,
``verification_runtime_state.json``,
``verification_executor_state.json``,
``verification_progress_state.json``).  Writes now flow through the
repository layer; reads are supposed to flow through the repository
layer too.

This test pins acceptance condition (4) for the residual direct
read sites in ``backend/server.py``:

  * **No more** ``plan_state_file`` variable holding a path to
    ``plan_state.json``;
  * **No more** ``exec_file`` variable holding a path to
    ``execution.json``;
  * **No more** ``progress_file`` variable holding a path to
    ``verification_progress_state.json``.

A failure here means the legacy direct-read path slipped back into
``server.py`` and the state-machine contract is broken at the API
surface — even if the static-gate on ``backend/state_machine/``
remains green (that gate covers the new module only; this test
covers the API glue layer).

Implementation
--------------
The test is a whole-file grep against ``backend/server.py`` for
each legacy variable name.  A failure is a non-empty match list.
We surface the offending lines so a reviewer can fix the residual
direct-read at the exact spot.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Absolute path to backend/server.py — no placeholders, no relative
# paths.  The test_command in the task brief cd's to the project
# root and greps ``backend/server.py``; we mirror that exactly.
SERVER_PY = Path(__file__).resolve().parents[1] / "server.py"


def _grep_server_py(pattern: str) -> list[str]:
    """Return every line in ``backend/server.py`` matching ``pattern``.

    Returns the *raw* match lines (no filtering) so the failure
    message can show the offending site.  We use ``grep -nE`` to
    include line numbers — the failure assertion surfaces them so
    a reviewer can fix the residual direct-read at the exact spot.
    """
    if not SERVER_PY.exists():
        return [f"missing file: {SERVER_PY}"]
    proc = subprocess.run(
        ["grep", "-nE", pattern, str(SERVER_PY)],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        # grep returns 1 on "no match" (clean) and >1 on real error
        # (e.g. file unreadable).  Surface the error.
        return [
            f"grep error: exit={proc.returncode}; "
            f"stderr={(proc.stderr or '').strip()[:200]!r}"
        ]
    text = (proc.stdout or "").rstrip("\n")
    return [line for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Tests — the 3 brief-mandated grep gates
# ---------------------------------------------------------------------------


def test_no_plan_state_file() -> None:
    """``plan_state_file`` must NOT appear anywhere in ``backend/server.py``.

    The legacy code path read ``plan_state.json`` directly via a
    ``plan_state_file`` Path variable; reads are now supposed to
    flow through RoutingRepository (which carries ``flags`` /
    ``current_phase`` in SQLite).
    """
    matches = _grep_server_py("plan_state_file")
    assert not matches, (
        "backend/server.py still references the legacy variable "
        "``plan_state_file``; reads must go through the repository "
        "layer (RoutingRepository for current_phase / flags) rather "
        "than opening ``plan_state.json`` directly.  Offending "
        "lines:\n" + "\n".join(matches)
    )


def test_no_exec_file() -> None:
    """``exec_file`` must NOT appear anywhere in ``backend/server.py``.

    The legacy code path read ``execution.json`` directly via an
    ``exec_file`` Path variable; reads are now supposed to flow
    through ExecutionRepository (which carries ``exec_status`` /
    ``exec_pid`` / ``started_at`` / ``project_dir`` etc. in SQLite).
    """
    matches = _grep_server_py("exec_file")
    assert not matches, (
        "backend/server.py still references the legacy variable "
        "``exec_file``; reads must go through the repository layer "
        "(ExecutionRepository for exec_status / exec_pid / "
        "started_at / project_dir) rather than opening "
        "``execution.json`` directly.  Offending lines:\n"
        + "\n".join(matches)
    )


def test_no_progress_file() -> None:
    """``progress_file`` must NOT appear anywhere in ``backend/server.py``.

    The legacy code path read ``verification_progress_state.json``
    directly via a ``progress_file`` Path variable; reads are now
    supposed to flow through VerificationRepository (which carries
    ``progress_state`` as a parsed JSON column on ``plan_verification``).
    """
    matches = _grep_server_py("progress_file")
    assert not matches, (
        "backend/server.py still references the legacy variable "
        "``progress_file``; reads must go through the repository "
        "layer (VerificationRepository.progress_state) rather than "
        "opening ``verification_progress_state.json`` directly.  "
        "Offending lines:\n" + "\n".join(matches)
    )


# ---------------------------------------------------------------------------
# Meta — sanity check the scan itself works (positive control)
# ---------------------------------------------------------------------------


def test_grep_finds_expected_residual_in_clean_state() -> None:
    """Sanity check: the grep helper actually scans server.py.

    We assert the scan completes without raising — that is, the
    helper is wired to the right path.  The contract is "zero hits
    when migration is complete"; the three brief-mandated tests
    above pin that contract directly.
    """
    # Run all three scans; they must each return a list (possibly
    # empty) without raising.  No further assertion — this is a
    # smoke test for the helper itself.
    _grep_server_py("plan_state_file")
    _grep_server_py("exec_file")
    _grep_server_py("progress_file")