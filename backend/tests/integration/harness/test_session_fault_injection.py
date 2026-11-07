"""TDD spec for the mcode session mapping's failure paths.

Why these faults are injected
=============================

Two failures of the session mapping are **late**. Nothing goes wrong at
the moment they happen, and everything goes wrong later, in a process
that has forgotten everything.

1. **The mapping is missing.** ``mcode`` mints the conversation id, so
   the only way back to a conversation is the pinned key → observed id
   mapping (task 16). A run that died *before* its registration left no
   mapping behind. The next process asks to resume, finds nothing, and
   has to decide: refuse, or start a new conversation. It must start a
   new one — and say so, because a resume that silently continued some
   *other* conversation would be worse than either.

2. **The mapping cannot be written.** The directory the mapping lives
   in is read-only, or the disk is full, or the file belongs to
   somebody else. The registration fails, and the call that was going
   to hand the observed id back must fail with it. Carrying on would
   produce a run whose conversation nobody can find again — and it
   would produce it *quietly*.

Both faults are delayed, so they cannot be provoked by waiting: the
window between "the CLI minted an id" and "the mapping was written" is
microseconds wide and never opens on its own. They are injected
directly instead — a directory whose mode forbids writing, and a
mapping file that is deleted underneath the caller.

What is pinned here
-------------------

1. A mapping that cannot be written **fails the call**. No id is
   forged, no random id is substituted, and the entry that was there
   is left exactly as it was.
2. A missing mapping **degrades into an explicit create**, and the
   degradation is **audited**: the audit names the pinned key, the
   observed session id, and the event
   ``session_map_missing_new_created``.
3. The degraded create **registers its new conversation under the same
   pinned key**, replacing the entry that was lost — one key, one
   conversation, the one that is alive.
4. The audit record carries **both** the pinned key and the observed
   session id, so a reader can tell which task degraded and which
   conversation it ended up in. The observed id is null while no
   conversation exists yet, and it is never a value that was not really
   observed.

The boundary
------------

The permission disproof only means something for a user that file
permissions bind: root writes into a read-only directory quite
happily, so there is nothing to disprove there. The case is skipped as
root rather than answered.

The observation point
---------------------

The degraded create is a **real run**: the adapter builds the command
line, the Runner spawns it, and the conversation id is read out of the
child's own event stream. The child is a fake CLI written by this spec
— it records the argv it was handed and reports a session id — because
the thing under test is what the mapping does when the mapping is
broken, not what ``mcode`` does when it works.

The caller above the mapping
----------------------------

The mapping is a file, and something has to read it. That something is
the *caller above*: it resolves the pinned key, decides whether the
resume can be honoured, runs the conversation, and writes the mapping
back. It is spelled out here — :func:`_resume_or_degrade` — because
this spec is its executable form. Every decision it makes is made by
the production code it calls:

* whether the mapping resolves — :meth:`SessionMap.lookup`;
* whether a resume is possible at all — the adapter refuses a resume
  that names no real conversation, so a miss cannot be papered over;
* what the run's command line is — :meth:`McodeHarness.build_invocation`;
* which conversation the run really created — the Runner's own reading
  of the child's stream;
* what the mapping says afterwards — :meth:`SessionMap.register`.

What the caller adds is the *policy*: a miss degrades into an explicit
create, and the degradation is audited.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from harness import mcode
from harness.contract import (
    HarnessConfigError,
    SessionMode,
    SessionRef,
    TaskInvocation,
)
from harness.mcode import McodeHarness, SessionMap
from harness.runner import Runner

pytestmark = pytest.mark.integration

#: The pinned key the caller insists on. In the product this is a uuid5
#: derived from the plan and the task; the short form is what the plan's
#: own example uses.
PINNED = "u1"

#: The conversation the first run really created: the ``mvs_`` prefix and
#: 32 hex digits, as measured on the CLI.
OBSERVED = "mvs_0ecaf7d26ff845a19fbaf6ecd45285e9"

#: The conversation the degraded create really created.
NEW_OBSERVED = "mvs_1f0c2d4b6a8e0c2d4f6a8e0c2d4f6a8b"

#: A conversation registered under the same pinned key after that one.
NEWEST_OBSERVED = "mvs_2a1b3c5d7e9f0a1b3c5d7e9f0a1b3c5d"

#: The event a degraded create is audited under.
SESSION_MAP_MISSING_NEW_CREATED = "session_map_missing_new_created"

#: The prompt the runs carry. The fake CLI reads it and throws it away.
PROMPT = "do the thing"


# ---------------------------------------------------------------------------
# The fake CLI
# ---------------------------------------------------------------------------

#: The fake ``mcode``: a real program, because the thing under test is
#: what the mapping does around a run, not what the run itself does.
_FAKE_CLI_PY = '''\
"""A fake ``mcode`` CLI: it records what it was handed, and reports a session.

The point of this program is that it is a *process*: the Runner spawns
it, the adapter parses the stream it writes, and the mapping under test
is fed a session id that a real child produced.
"""

import json
import os
import sys


def main():
    """Record the argv, read the prompt, and report a finished run."""
    argv_out = os.environ.get("FAKE_MCODE_ARGV_OUT")
    if argv_out:
        with open(argv_out, "w", encoding="utf-8") as handle:
            json.dump(sys.argv[1:], handle)

    # The prompt arrives on standard input and ends at EOF.
    sys.stdin.read()

    session_id = os.environ.get("FAKE_MCODE_SESSION_ID", "")
    print(json.dumps({"type": "session.started", "session_id": session_id}), flush=True)
    print(
        json.dumps(
            {
                "type": "exec.completed",
                "session_id": session_id,
                "result": {"status": "succeeded", "output": "ok"},
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

#: The launcher for the fake CLI. A shell wrapper rather than a shebang
#: on the program itself, so the path of the interpreter never has to
#: fit in a shebang line.
_FAKE_CLI_SH = '''#!/bin/sh
exec "@@PYTHON@@" "@@SCRIPT@@" "$@"
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _unsandboxed(tmp_path, monkeypatch):
    """No sandbox profile: the child is spawned the way it always is.

    A deployment that configures a Seatbelt profile would otherwise
    confine the fake CLI, and the profile's rules — not this spec —
    would decide whether the run happens.
    """
    monkeypatch.delenv("PDT_SANDBOX_PROFILE", raising=False)
    monkeypatch.setenv("PDT_CONFIG_DIR", str(tmp_path / "config"))


@pytest.fixture
def map_dir(tmp_path) -> Path:
    """The directory the mapping lives in."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    return directory


@pytest.fixture
def map_path(map_dir) -> Path:
    """The mapping file."""
    return map_dir / "session_map.json"


@pytest.fixture
def harness() -> McodeHarness:
    """The MiniMax Code adapter, with no profile bound."""
    return McodeHarness()


@pytest.fixture
def fake_cli(tmp_path, monkeypatch) -> Path:
    """Point the adapter at a fake ``mcode`` that reports a session id.

    The fake records the argv it was handed, so a test can see what the
    child was actually given rather than what the adapter meant to give
    it.
    """
    script = tmp_path / "fake_mcode.py"
    script.write_text(_FAKE_CLI_PY, encoding="utf-8")

    launcher = tmp_path / "fake_mcode"
    launcher.write_text(
        _FAKE_CLI_SH.replace("@@PYTHON@@", sys.executable).replace(
            "@@SCRIPT@@", str(script)
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    monkeypatch.setattr(mcode, "MCODE_BINARY", str(launcher))
    monkeypatch.setenv("FAKE_MCODE_SESSION_ID", NEW_OBSERVED)
    monkeypatch.setenv("FAKE_MCODE_ARGV_OUT", str(tmp_path / "argv.json"))

    return launcher


def _child_argv(tmp_path) -> List[str]:
    """The argv the fake CLI was actually handed."""
    record = tmp_path / "argv.json"
    if not record.exists():
        return []
    return json.loads(record.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The caller above the mapping
# ---------------------------------------------------------------------------


def _resume_or_degrade(
    harness: McodeHarness,
    session_map: SessionMap,
    pinned: str,
    *,
    run: bool = True,
) -> Dict[str, Any]:
    """Resume the conversation behind ``pinned``, or degrade into a create.

    The policy, in one place: a mapping that does not resolve is a miss,
    and a miss degrades into an explicit create that is audited. The
    decisions themselves belong to the production code called here — see
    the module docstring.

    Args:
        harness: The adapter that builds the command line.
        session_map: The mapping the caller reads and writes.
        pinned: The key the caller pinned.
        run: Whether the conversation is really run. ``False`` stops at
            the decision, which is what the audit of a degradation is
            about.

    Returns:
        A dict with:

        ``degraded``
            Whether the resume degraded into a create.
        ``audit``
            The audit record of the degradation, or ``None`` when
            nothing degraded.
        ``session_id``
            The conversation the run really used, read out of the
            child's own stream.
        ``argv``
            The command line the run used.

    Raises:
        OSError: The mapping could not be written. The call fails with
            it rather than handing back an id nobody can resolve.
    """
    observed = session_map.lookup(pinned)
    degraded = False
    audit: Optional[Dict[str, Any]] = None

    if observed is None:
        # The mapping does not resolve. A resume cannot be built out of
        # that, and the adapter says so rather than starting a
        # conversation the caller did not ask for.
        with pytest.raises(HarnessConfigError):
            harness.build_invocation(
                TaskInvocation(
                    prompt=PROMPT,
                    session=SessionRef(session_id=None, pinned_session_id=pinned),
                    mode=SessionMode.RESUME,
                )
            )

        degraded = True
        audit = {
            "pinned_session_id": pinned,
            "session_id": None,
            "event": SESSION_MAP_MISSING_NEW_CREATED,
        }

    invocation = harness.build_invocation(
        TaskInvocation(
            prompt=PROMPT,
            session=SessionRef(session_id=observed, pinned_session_id=pinned),
            mode=SessionMode.CREATE if degraded else SessionMode.RESUME,
        )
    )

    outcome: Dict[str, Any] = {
        "degraded": degraded,
        "audit": audit,
        "session_id": None,
        "argv": tuple(invocation.argv),
    }
    if not run:
        return outcome

    result = Runner().run(harness, invocation, stdin_text=PROMPT)
    outcome["session_id"] = result.session_id
    if audit is not None:
        # The conversation exists now, so the audit can name it.
        audit["session_id"] = result.session_id

    # The registration comes last: the id is only handed back once it is
    # on disk, and a mapping that cannot be written fails the call.
    session_map.register(pinned, result.session_id)

    return outcome


# ---------------------------------------------------------------------------
# 1. A mapping that cannot be written
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="root ignores directory permissions; there is nothing to disprove",
)
def test_readonly_dir_fails_without_fake_id(
    tmp_path, map_dir, map_path, harness, fake_cli
):
    """A mapping that cannot be written fails the call, and forges nothing.

    The conversation was really created — the child reported a real
    session id — but the mapping cannot record it. The call therefore
    fails: a run that cannot be resumed must not look like a run that
    can.

    What it must not do is invent an id, or quietly continue with a
    random one, or leave the old entry overwritten by a conversation
    that was never really registered.
    """
    session_map = SessionMap(map_path)
    session_map.register(PINNED, OBSERVED)
    before = map_path.read_bytes()

    map_dir.chmod(0o500)
    try:
        with pytest.raises(OSError):
            _resume_or_degrade(harness, session_map, PINNED)

        # The child really ran, and it was asked to continue the
        # conversation the pinned key stood for. This is what makes the
        # failure above the *registration* failing rather than a run that
        # never happened: a spawn that did not produce a child cannot
        # leave this argv behind, and a failure after the child ran can
        # only be the write.
        argv = _child_argv(tmp_path)
        assert "--session" in argv
        assert argv[argv.index("--session") + 1] == OBSERVED

        # The entry is exactly as it was: the conversation that really
        # exists, and not a substitute for the one that could not be
        # recorded.
        assert map_path.read_bytes() == before
        assert session_map.lookup(PINNED) == OBSERVED

        # The conversation the run really created was recorded nowhere:
        # the call failed before its id could be handed back, and
        # nothing was invented in its place.
        assert NEW_OBSERVED not in map_path.read_text(encoding="utf-8")

        # And nothing was left behind by the write that failed.
        assert sorted(p.name for p in map_dir.iterdir()) == [map_path.name]
    finally:
        map_dir.chmod(0o700)

    # The case leaves the directory as it found it: writable again, and
    # holding nothing but the mapping.
    assert stat.S_IMODE(os.stat(map_dir).st_mode) == 0o700
    assert sorted(p.name for p in map_dir.iterdir()) == [map_path.name]


# ---------------------------------------------------------------------------
# 2. A mapping that is not there
# ---------------------------------------------------------------------------


def test_missing_map_degrades_to_create(map_path, harness, fake_cli):
    """A mapping that is gone degrades into an explicit create, and says so.

    The crash window: the run died before it could record the mapping,
    so the process that comes back finds nothing. It must not resume
    *some* conversation — it has none — and it must not fail either: the
    work can be done again in a new conversation, as long as the
    degradation is on the record.
    """
    session_map = SessionMap(map_path)
    session_map.register(PINNED, OBSERVED)

    # The mapping is gone: the crash happened before the registration.
    map_path.unlink()

    outcome = _resume_or_degrade(harness, session_map, PINNED, run=False)

    assert outcome["degraded"] is True
    assert outcome["audit"] == {
        "pinned_session_id": PINNED,
        "session_id": None,
        "event": SESSION_MAP_MISSING_NEW_CREATED,
    }

    # The degraded run creates a conversation; it does not pretend to
    # resume one.
    assert "--session" not in outcome["argv"]

    # And the mapping is still empty: nothing was recorded for a
    # conversation that was never created.
    assert session_map.lookup(PINNED) is None


# ---------------------------------------------------------------------------
# 3. The degraded create writes the mapping back
# ---------------------------------------------------------------------------


def test_new_create_overwrites_entry(tmp_path, map_path, harness, fake_cli):
    """The degraded create registers its conversation under the same key.

    The pinned key is the task's handle on its conversation, and it does
    not change when the conversation behind it is replaced. The new
    conversation takes the entry over: one key, one conversation, the
    one that is alive — and the entry that was lost is not left behind
    as a second answer to the same key.

    That replacement is what keeps the *next* resume from walking into
    the conversation that is gone: a map that kept the old entry would
    send the next run back to a conversation that no longer exists, and
    the task would fail the same way for ever.
    """
    session_map = SessionMap(map_path)
    session_map.register(PINNED, OBSERVED)

    # The mapping is gone: the crash happened before the registration.
    map_path.unlink()

    outcome = _resume_or_degrade(harness, session_map, PINNED)

    assert outcome["degraded"] is True

    # The conversation the run really created — read out of the child's
    # own stream, not handed to it.
    assert outcome["session_id"] == NEW_OBSERVED

    # The audit names the conversation the task ended up in.
    assert outcome["audit"] == {
        "pinned_session_id": PINNED,
        "session_id": NEW_OBSERVED,
        "event": SESSION_MAP_MISSING_NEW_CREATED,
    }

    # The mapping holds the new conversation under the pinned key ...
    assert session_map.lookup(PINNED) == NEW_OBSERVED

    # ... and only that one: the entry that was lost was replaced, not
    # shadowed by a second answer to the same key.
    assert json.loads(map_path.read_text(encoding="utf-8")) == {
        PINNED: NEW_OBSERVED
    }
    assert OBSERVED not in map_path.read_text(encoding="utf-8")

    # The run really created the conversation: the child was not asked
    # to resume anything.
    assert "--session" not in _child_argv(tmp_path)

    # The replacement itself. The delete above emptied the map, so the
    # registration that follows the degraded create is a *later*
    # registration for the same key — and it replaces the entry rather
    # than adding a second one. The map never holds two answers to the
    # same pinned key, and the answer it holds is the newest
    # conversation.
    session_map.register(PINNED, NEWEST_OBSERVED)

    assert session_map.lookup(PINNED) == NEWEST_OBSERVED
    assert json.loads(map_path.read_text(encoding="utf-8")) == {
        PINNED: NEWEST_OBSERVED
    }
