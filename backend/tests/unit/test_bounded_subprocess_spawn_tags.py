"""Every ``bounded_subprocess`` child must carry the PDT spawn marker.

§3 injection point 3 of the orphan-reaper architecture: pytest,
executor and verification all spawn their children through
``bounded_subprocess._child_environment``.  That function is the single
place where a child environment is *explicitly constructed*, so it is
the single place where the spawn marker can be stamped for every one of
those call sites.

The marker is what the reaper later reads out of a process's
environment (``PDT_SPAWN_OWNER_PID`` / ``PDT_SPAWN_OWNER_START`` /
``PDT_SPAWN_KIND``).  A child that reaches the reaper without the
marker is invisible to it — it cannot be attributed to an owner, so it
cannot be told apart from a process somebody started by hand.

The cases below pin the *contract* of the stamp, not just its presence:

* the caller's own mapping is never mutated (a caller may reuse it for
  a second child),
* ``env=None`` means "inherit", so the inherited environment is the
  base the marker is written onto,
* ``cwd=None, env=None`` — the "no arguments at all" call site — is
  **still** stamped.  That branch used to return ``None`` early and a
  test that always passed a ``cwd`` would never have noticed.
* a real child sees the marker, end to end.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from bounded_subprocess import _child_environment, run_bounded  # noqa: E402
import bounded_subprocess  # noqa: E402

MARKER_OWNER_PID = "PDT_SPAWN_OWNER_PID"
MARKER_OWNER_START = "PDT_SPAWN_OWNER_START"
MARKER_KIND = "PDT_SPAWN_KIND"

MARKER_KEYS = (MARKER_OWNER_PID, MARKER_OWNER_START, MARKER_KIND)


def test_child_env_stamps_marks():
    """The three marker keys are present, and the caller's keys survive."""
    result = _child_environment("/tmp", {"A": "1"})

    assert result is not None
    for key in MARKER_KEYS:
        assert key in result, "missing spawn marker %r" % key
    assert result["A"] == "1"
    assert result[MARKER_KIND] == "subprocess"
    assert result[MARKER_OWNER_PID] == str(os.getpid())
    assert result[MARKER_OWNER_START]


def test_child_env_does_not_mutate_caller():
    """The caller's mapping is copied, not stamped in place."""
    caller_env = {"A": "1"}

    _child_environment("/tmp", caller_env)

    assert len(caller_env) == 1
    assert caller_env == {"A": "1"}


def test_child_env_none_uses_osenviron():
    """``env=None`` inherits the parent environment as the base."""
    result = _child_environment("/tmp", None)

    assert result is not None
    assert "PATH" in result
    assert result["PATH"] == os.environ.get("PATH")


def test_child_env_none_none_is_stamped():
    """The "nothing at all" call site is stamped too, and is a dict."""
    result = _child_environment(None, None)

    assert result is not None
    assert isinstance(result, dict)
    for key in MARKER_KEYS:
        assert key in result, "missing spawn marker %r" % key
    assert result[MARKER_KIND] == "subprocess"


def test_run_bounded_child_sees_mark(tmp_path):
    """End to end: the child process really has the marker in its env."""
    completed = run_bounded(
        "env | grep PDT_SPAWN_KIND",
        cwd=str(tmp_path),
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PDT_SPAWN_KIND=subprocess" in completed.stdout


def test_child_env_overwrites_an_inherited_marker():
    """A marker inherited from the parent is replaced, not kept."""
    result = _child_environment(
        "/tmp",
        {
            MARKER_OWNER_PID: "999999",
            MARKER_OWNER_START: "0",
            MARKER_KIND: "service",
        },
    )

    assert result[MARKER_OWNER_PID] == str(os.getpid())
    assert result[MARKER_KIND] == "subprocess"
    assert result[MARKER_OWNER_START] != "0"


def test_child_env_survives_a_broken_orphan_reaper(monkeypatch):
    """A missing :mod:`orphan_reaper` must not stop the command.

    The marker is a diagnostic.  If it cannot be attached, the child is
    still spawned — a command that refuses to run because a diagnostic
    failed is the worse failure.
    """
    monkeypatch.setitem(sys.modules, "orphan_reaper", None)

    result = _child_environment("/tmp", {"A": "1"})

    assert result is not None
    assert result["A"] == "1"
    assert result["PWD"] == "/tmp"
    for key in MARKER_KEYS:
        assert key not in result


def test_child_env_says_once_that_the_marker_was_lost(monkeypatch, caplog):
    """Losing the marker is said out loud -- once, not once per child.

    A child the reaper cannot see is a blind spot, and a blind spot
    nobody hears about is a blind spot nobody fixes.  The other spawn
    point in the tree (``service_manager``) says it the same way, so
    the two injection points fail the same way.
    """
    # The "already said" set is module state, so it is emptied here:
    # this test asserts the *first* complaint, and a test that ran
    # earlier in the session may already have used the topic up.
    monkeypatch.setattr(bounded_subprocess, "_SPAWN_MARKER_WARNED", set())
    monkeypatch.setitem(sys.modules, "orphan_reaper", None)

    with caplog.at_level(logging.WARNING):
        _child_environment("/tmp", {"A": "1"})
        _child_environment("/tmp", {"A": "1"})

    said = [r for r in caplog.records if "orphan_reaper" in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in caplog.records]