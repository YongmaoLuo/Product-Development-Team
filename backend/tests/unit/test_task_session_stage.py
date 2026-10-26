"""The stage a task's session was bound at.

Background
----------
The executor binds a session to a task before the task's first dispatch,
and that binding is one of three things: a session that already exists
and is re-opened, or a deterministic id the first dispatch CREATES the
session under. A caller — the dispatcher, which writes a
``task_session_bound`` event before the dispatch starts — cannot tell
those two apart from the handle alone: both carry a session id, and
"there is an id" does not say whether it names an existing session or
one that does not exist yet.

``JsonSession.stage`` is that distinction, carried on the handle:

* ``"resumed"`` — the id names a session that exists and is re-opened
  (``--resume``). Two things can produce it: the recovery mapping
  (``PDT_RESUME_SESSIONS``) and the plan's sidecar.
* ``"pinned"`` — the id is derived from the plan and the task, so the
  first dispatch creates the session under it (``--session-id``).
* ``None`` — no id at all. Nothing was bound, so nothing can be
  labelled.

TDD spec — 4 contract tests
---------------------------
1. ``test_stage_pinned_when_nothing_recorded``:
   nothing recorded + a plan id → ``stage == "pinned"`` and a
   non-empty ``pinned_session_id``.
2. ``test_stage_resumed_from_sidecar``:
   the sidecar records the task's session → ``stage == "resumed"``.
3. ``test_stage_resumed_from_recovery_map``:
   ``PDT_RESUME_SESSIONS`` names the task → ``stage == "resumed"``.
4. ``test_stage_none_without_plan_id``:
   no plan id → ``stage is None`` (and every existing caller that
   builds a bare ``JsonSession()`` still gets a handle with no stage).
"""

import json
import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so ``import agent`` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import agent as agent_module  # noqa: E402
from coding_tool import JsonSession  # noqa: E402
from plan_sessions import save_session_id  # noqa: E402

PLAN_ID = "20261026-PDT"
TASK_ID = "3"


@pytest.fixture(autouse=True)
def no_resume_mapping(monkeypatch):
    """No recovery mapping unless a test writes one.

    The mapping is read from the process environment, so a mapping left
    behind by another test would decide the answer before the test's own
    arrangement could.
    """
    monkeypatch.delenv("PDT_RESUME_SESSIONS", raising=False)


def test_stage_pinned_when_nothing_recorded(tmp_path):
    """With nothing recorded, the id is derived and the stage is "pinned".

    There is no session to re-opens yet: the first dispatch CREATES one
    under the derived id (``--session-id``), so the binding is a pin,
    not a resume.
    """
    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert handle.stage == "pinned", (
        f"expected stage 'pinned', got {handle.stage!r}"
    )
    assert handle.pinned_session_id, (
        "a pinned binding must carry the derived session id"
    )
    assert handle.session_id is None, (
        "nothing was recorded, so there is no existing session to re-open"
    )


def test_stage_resumed_from_sidecar(tmp_path):
    """A session id recorded in the sidecar is re-opened → "resumed"."""
    save_session_id(tmp_path, f"task:{TASK_ID}", "session-from-sidecar")

    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert handle.stage == "resumed", (
        f"expected stage 'resumed', got {handle.stage!r}"
    )
    assert handle.session_id == "session-from-sidecar", (
        "the sidecar's id must be the session the task re-opens; got "
        f"{handle.session_id!r}"
    )


def test_stage_resumed_from_recovery_map(tmp_path, monkeypatch):
    """A session named by the recovery mapping is re-opened → "resumed"."""
    monkeypatch.setenv(
        "PDT_RESUME_SESSIONS", json.dumps({TASK_ID: "session-from-mapping"})
    )

    handle = agent_module._make_task_session(TASK_ID, tmp_path, PLAN_ID)

    assert handle.stage == "resumed", (
        f"expected stage 'resumed', got {handle.stage!r}"
    )
    assert handle.session_id == "session-from-mapping", (
        "the mapping names the session that was really running; got "
        f"{handle.session_id!r}"
    )


def test_stage_none_without_plan_id(tmp_path):
    """No plan id → no id to bind, so there is no stage.

    A handle built directly — every existing caller that does
    ``JsonSession()`` — is unaffected: it carries no stage either.
    """
    handle = agent_module._make_task_session(TASK_ID, tmp_path, "")

    assert handle.stage is None, (
        f"expected stage None, got {handle.stage!r}"
    )
    assert handle.session_id is None
    assert handle.pinned_session_id is None

    assert JsonSession().stage is None, (
        "a handle built without a stage must still construct"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
