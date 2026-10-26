"""Tests for ``hooks.config`` — the hook a plan stores is a command.

What is being pinned
--------------------
A plan's HITL configuration names *who gets asked*.  The answer used to
be a transport name plus the fields that transport needs
(``{"kind": "cc_connect", "destination": ..., "session_id": ...}``) — a
description, which the dispatcher then had to interpret.  It does not
interpret that description: the dispatcher's transports are ``http``
and ``cmd``, so a ``cc_connect`` hook could never be delivered and the
decision fell to ``unroutable``.

The hook is therefore a **command**: the delivery entry point, named
with the interpreter that is running and the destination the plan
names, written down once when the plan is created.  Four properties
carry the weight:

* **One plan, one hook.**  A plan's origin names exactly one place to
  ask, so the hook list is exactly one entry — not a name plus a
  command plus a second copy of the destination.
* **The command is the delivery CLI, with the plan's own arguments.**
  The hook is what the dispatcher runs, so what it runs has to be the
  module that delivers (``backend.hooks.session_send``), the
  interpreter that can import it, and the project and session the
  plan was created for.
* **A missing field is no hook at all.**  A hook needs a session to
  have come from, a place to be delivered to and a source that says so.
  Any one of them missing means there is nobody to ask, and a plan
  with no hooks is a state the plan already knows how to be in.
* **A metacharacter is data.**  The destination is somebody else's
  string; it is quoted, so a destination that looks like a shell
  command stays one argument and cannot become one.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.config import HOOK_KIND_CMD, write_for_plan  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _origin(**overrides):
    """A plan origin with all three fields a hook needs."""
    origin = {
        "source": "plan_creation",
        "session_id": "s-1",
        "destination": "grp-1",
    }
    origin.update(overrides)
    return origin


# ---------------------------------------------------------------------------
# The hook a complete origin produces
# ---------------------------------------------------------------------------


def test_origin_produces_single_cmd_hook(tmp_path):
    """A plan with an origin gets exactly one hook, and it is a command."""
    plan_dir = tmp_path / "plan-1"

    written = write_for_plan(plan_dir, _origin())

    assert HOOK_KIND_CMD == "cmd"
    assert len(written["hooks"]) == 1
    assert written["hooks"][0]["kind"] == HOOK_KIND_CMD
    assert written["hooks"][0]["command"]


def test_command_argv(tmp_path):
    """The command runs the delivery CLI with the plan's destination.

    The interpreter is the one running the process — never a path
    written down on another machine — and the two values the plan
    carries are the project to send to and the session to send in.
    """
    plan_dir = tmp_path / "plan-2"

    written = write_for_plan(plan_dir, _origin())
    command = written["hooks"][0]["command"]

    assert shlex.split(command) == [
        sys.executable,
        "-m",
        "backend.hooks.session_send",
        "--project",
        "grp-1",
        "--session",
        "s-1",
    ]


# ---------------------------------------------------------------------------
# The origins that ask nobody
# ---------------------------------------------------------------------------


def test_any_missing_field_yields_no_hooks(tmp_path):
    """A hook needs all three fields; any one of them missing is no hook.

    A plan created by a bare ``curl`` has no session and no source, and
    a plan with a destination but no session has nowhere to send a
    reply to.  None of them is an error: they are the answer for a plan
    that has nobody to ask.
    """
    for missing in ("source", "destination", "session_id"):
        plan_dir = tmp_path / "plan-missing-{0}".format(missing)

        written = write_for_plan(plan_dir, _origin(**{missing: ""}))

        assert written["hooks"] == [], missing


# ---------------------------------------------------------------------------
# The destination is data, not a command
# ---------------------------------------------------------------------------


def test_metacharacters_stay_one_argument(tmp_path):
    """A destination that looks like a shell command stays one argument.

    The destination is somebody else's string.  It is quoted, so it
    reaches the delivery CLI as one argument and cannot become a
    command of its own.
    """
    destination = "grp; touch /tmp/x"
    plan_dir = tmp_path / "plan-metacharacters"

    written = write_for_plan(plan_dir, _origin(destination=destination))
    command = written["hooks"][0]["command"]

    assert shlex.split(command)[4] == destination
