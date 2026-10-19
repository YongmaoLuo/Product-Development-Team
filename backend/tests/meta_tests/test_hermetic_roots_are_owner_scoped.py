"""A pytest session may remove only the scratch roots it minted.

Under ``pytest-xdist`` every worker inherits one ``PDT_LOCK_ROOT`` from the
controller: the controller mints it before forking, and a worker that sees
the variable already set reuses it.  The session-scoped teardown used to
remove that root unconditionally in every worker, so the first worker to run
out of tests deleted the directory out from under the workers still running
-- taking their live broker sockets with it.  The symptom was a contended
edit failing with ``[pdt-lock] broker unavailable (exit 4)`` while the same
tree passed on the next run: a flake that was really a cross-process
deletion race.

The rule this pins: only the PID that minted a root may remove it.  A nested
pytest session inherits the variables and is exactly the "did not mint it"
case; it must leave them alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: A file under ``tests/`` with one trivial test.  The nested session must
#: actually *run* a test (not just collect): the session-scoped autouse
#: fixture that does the removal is never instantiated under
#: ``--collect-only``.
_NESTED_TARGET = "tests/meta_tests/test_owner_scope_probe.py"

#: Kept out of the nested environment: a nested run has no worker id, and
#: inheriting the parent's would make the nested session skip the very
#: teardown this test needs to exercise.
_WORKER_ID_VAR = "PYTEST_XDIST_WORKER"


def _nested_session(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop(_WORKER_ID_VAR, None)
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", _NESTED_TARGET, "-q", "--no-header"],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_a_nested_session_keeps_the_roots_it_inherited(tmp_path: Path) -> None:
    lock_root = tmp_path / "lk-shared"
    lock_root.mkdir()
    lock_sentinel = lock_root / "pdt-lock-0123456789abcdef.sock"
    lock_sentinel.write_text("", encoding="utf-8")

    state_root = Path(os.environ["PDT_TEST_SESSION_ROOT"])
    state_sentinel = state_root / "owner-scope-sentinel"
    state_sentinel.write_text("", encoding="utf-8")

    proc = _nested_session(
        PDT_LOCK_ROOT=str(lock_root),
        PDT_TEST_SESSION_ROOT=str(state_root),
    )

    assert proc.returncode == 0, (
        "the nested session did not finish cleanly.\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert lock_sentinel.exists(), (
        "a nested pytest session removed a lock root it did not mint.  Under "
        "xdist the same removal runs in every worker, so the first worker to "
        "finish deletes the live broker sockets of the workers still running "
        "-- the 'broker unavailable (exit 4)' flake in "
        "tests/integration/test_edit_write_lock_hook.py."
    )
    assert state_sentinel.exists(), (
        "a nested pytest session removed the state root it inherited; the "
        "same rule applies, and the loss is silent until a later database "
        "open fails."
    )
