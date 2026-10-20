"""
Tests for resume-session seeding on the per-task executor session.

Background
----------
Pause/resume (arch decision point 4 / core contract 3): when an
``/execution/start`` picks up a plan whose tasks were interrupted, the
recovery point carries the ``session_id`` of each interrupted task's
transcript, and the executor must hand that id to the subagent so the
first dispatch of the resumed attempt runs ``claude --resume <id>``
instead of re-reading the world from scratch.

The executor's task session used to be pinned to ``JsonSession()`` —
empty, i.e. "open a fresh session" — and nothing ever seeded it, so the
contract could not be honoured. The transport is an environment
variable, ``PDT_RESUME_SESSIONS``: a JSON object mapping task id →
session id, set by the start-continuation path on the executor
process. Why an env var rather than a parameter: the session is created
deep inside ``_execute_task`` while the mapping is only known at plan
resume time, and the executor is already spawned as a child process
carrying ``PDT_*`` state (``PDT_TASK_ID``, ``PDT_PLAN_ID``, …).

Seeding only affects the FIRST binding. After that the handle is owned
by ``coding_tool``'s ``_remember``, which rewrites ``session_id`` after
every dispatch so attempt 2 of the same task keeps reusing attempt 1's
transcript even when the recovery map said nothing about it.

Failures here are never fatal — a resume id we cannot use must degrade
to a fresh session, not abort a run.

TDD spec — 4 contract tests
----------------------------
1. ``test_load_resume_sessions_parses_env``:
   ``{"3": "sess-abc"}`` → ``{"3": "sess-abc"}``; a missing or empty
   variable → ``{}``.
2. ``test_load_resume_sessions_bad_json``:
   ``not-json`` and a JSON array both → ``{}``, without raising.
3. ``test_resume_session_id_for_hit_and_miss``:
   a mapped task id yields its session id; an unmapped id — and an id
   mapped to a non-string / empty value — yields ``None``.
4. ``test_task_session_seeded``:
   ``_make_task_session("3")`` carries the mapped id, and an unmapped
   task id yields a handle with ``session_id is None`` (fresh session).
"""

import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so ``import agent`` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import (  # noqa: E402
    RESUME_SESSIONS_ENV,
    _load_resume_sessions,
    _make_task_session,
    _resume_session_id_for,
)


@pytest.fixture
def clean_env(monkeypatch):
    """No resume mapping unless a test writes one."""
    monkeypatch.delenv(RESUME_SESSIONS_ENV, raising=False)
    return monkeypatch


def test_load_resume_sessions_parses_env(clean_env):
    """A well-formed mapping is handed back verbatim; absence is ``{}``."""
    assert _load_resume_sessions() == {}

    clean_env.setenv(RESUME_SESSIONS_ENV, "")
    assert _load_resume_sessions() == {}

    clean_env.setenv(RESUME_SESSIONS_ENV, '{"3": "sess-abc", "4": "sess-def"}')
    assert _load_resume_sessions() == {"3": "sess-abc", "4": "sess-def"}


def test_load_resume_sessions_bad_json(clean_env):
    """Unparseable JSON and non-object JSON both degrade to ``{}``."""
    clean_env.setenv(RESUME_SESSIONS_ENV, "not-json")
    assert _load_resume_sessions() == {}

    # A JSON array parses fine but is not the contract's shape.
    clean_env.setenv(RESUME_SESSIONS_ENV, '["sess-abc"]')
    assert _load_resume_sessions() == {}


def test_resume_session_id_for_hit_and_miss(clean_env):
    """Only a real, non-empty string value is a hit."""
    clean_env.setenv(RESUME_SESSIONS_ENV, '{"3": "sess-abc", "5": "", "6": 42}')
    assert _resume_session_id_for("3") == "sess-abc"
    # Not in the mapping at all.
    assert _resume_session_id_for("4") is None
    # Present but unusable — treated as a miss, i.e. a fresh session.
    assert _resume_session_id_for("5") is None
    assert _resume_session_id_for("6") is None


def test_task_session_seeded(clean_env):
    """The handle handed to the subagent carries the recovered id."""
    clean_env.setenv(RESUME_SESSIONS_ENV, '{"3": "sess-abc"}')
    assert _make_task_session("3").session_id == "sess-abc"
    # No entry → empty handle → the first dispatch opens a fresh session.
    assert _make_task_session("4").session_id is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
