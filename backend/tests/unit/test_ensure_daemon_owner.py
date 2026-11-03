"""Who the reaper daemon belongs to: the caller, or nobody.

A reaper daemon is started *for* somebody.  It has to know who, because
a daemon whose starter is gone has nothing left to do -- and a daemon
that keeps running anyway is the leak this whole work is about.

So the caller says which of the two it wants:

  * **attached** (the default): the daemon records the caller as its
    owner and leaves when that owner is gone.  A test fixture, a
    verification round, a shell -- all of them are over when they end,
    and the daemon they started should end with them.
  * **detached**: the daemon is exempt from the owner rule
    (``--survive-owner``).  Exactly one caller wants this: the
    **server**, whose daemon exists for the case where the server dies.

The default is the safe one.  A caller that forgets to ask for the
exemption gets a daemon that leaves with it; a caller that asks for the
exemption has to say so out loud, in the source, where a reader can see
it.

This file pins the three halves of that contract:

  1. the default call records the owner (pid *and* start time, so a
     recycled pid is not mistaken for the owner);
  2. the exempted call carries ``--survive-owner``;
  3. the server's two call sites ask for the exemption -- both of them,
     because the server starts the daemon at boot *and* puts it back
     when it has stopped by itself.

Nothing here starts a process: the launcher is replaced by a recorder,
and the server is read as source rather than run.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import ReaperConfig  # noqa: E402

#: The backend's own source -- read, not imported, for the wiring check.
SERVER_SOURCE = BACKEND_DIR / "server.py"


class _FakePopen:
    """Records how the daemon was launched instead of launching it."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(pid=self.pid)

    @property
    def only(self):
        assert len(self.calls) == 1, "expected exactly one launch, got %d" % len(
            self.calls
        )
        return self.calls[0]


def _config(plans_root: Path, **overrides) -> ReaperConfig:
    kwargs = {"enabled": True, "interval": 17, "plans_root": plans_root}
    kwargs.update(overrides)
    return ReaperConfig(**kwargs)


def _launched_cmd(tmp_path: Path, **kwargs) -> list:
    """Launch a daemon with a recorder and return the command line."""
    fake = _FakePopen()

    reaper.ensure_daemon(_config(tmp_path), repo_root=tmp_path, popen=fake, **kwargs)

    args, _kwargs = fake.only
    return list(args[0])


# ---------------------------------------------------------------------------
# The default: the daemon belongs to whoever started it
# ---------------------------------------------------------------------------


def test_attached_daemon_records_owner(tmp_path, monkeypatch):
    """The default call tells the daemon who its owner is.

    The owner is recorded as a pid **and** a start time: a pid on its
    own is not an identity, because the kernel recycles pids.  A
    daemon that recorded only the pid would one day find that pid
    alive, belonging to somebody else, and stay for ever.

    The pid is the caller's -- the process that runs
    :func:`ensure_daemon` -- and the start time is the caller's own
    start time, read from the machine.
    """
    monkeypatch.setattr(reaper, "_self_start_epoch", lambda: "987654.25")

    cmd = _launched_cmd(tmp_path)

    assert cmd[-4:] == [
        "--owner-pid",
        str(os.getpid()),
        "--owner-start",
        "987654.25",
    ]
    assert "--survive-owner" not in cmd, (
        "an attached daemon must not claim to outlive its owner"
    )


def test_unknown_owner_start_is_still_passed_through(tmp_path, monkeypatch):
    """A start time that could not be read is passed on as unknown.

    An empty start time means "could not be determined", which is what
    an empty string means everywhere else in this module: *unknown*,
    never "different".  The daemon resolves an unknown owner towards
    staying, so the daemon that is launched is not one that leaves on
    a guess.

    What must not happen is the flag being dropped: a daemon that was
    told nothing about its owner would fall back to "my parent", which
    is not the process that asked for it.
    """
    monkeypatch.setattr(reaper, "_self_start_epoch", lambda: "")

    cmd = _launched_cmd(tmp_path)

    assert cmd[-4:] == ["--owner-pid", str(os.getpid()), "--owner-start", ""]


# ---------------------------------------------------------------------------
# The exemption: the server's daemon
# ---------------------------------------------------------------------------


def test_detached_daemon_carries_survive_flag(tmp_path):
    """The exempted call carries ``--survive-owner``.

    The flag is what the daemon reads to decide that it may outlive its
    starter.  It is passed on the command line rather than left to the
    daemon's own default, because the daemon's default is the opposite:
    a daemon that was not told about an owner takes its parent, and
    that parent is usually a process that is about to end.

    No owner is recorded alongside the flag: the exemption is the whole
    statement, and an owner recorded next to it would be a second,
    contradicting answer to the same question.
    """
    cmd = _launched_cmd(tmp_path, detach_owner=True)

    assert "--survive-owner" in cmd
    assert "--owner-pid" not in cmd, (
        "a daemon that survives its owner must not also record one"
    )


# ---------------------------------------------------------------------------
# The wiring: the server asks for the exemption, both times
# ---------------------------------------------------------------------------


def test_server_start_requests_detached_owner():
    """Both of the server's call sites ask for a detached daemon.

    The server starts the daemon at boot and puts it back when it has
    stopped by itself.  Both of those are the *same* daemon, and both
    have to outlive the server -- that is the entire reason the server
    runs one at all.  A single call site that forgot the exemption
    would leave a daemon that dies with the server, and the orphans
    would come back.

    The source is read rather than the code run: the question is about
    what the server *says* when it asks, and running it would only
    answer for one of the two call sites at a time.
    """
    tree = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "ensure_daemon"
    ]

    assert len(calls) == 2, (
        "the server starts the daemon at boot and puts it back afterwards; "
        "expected two call sites, found %d" % len(calls)
    )

    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "detach_owner" in keywords, (
            "the server's daemon must outlive the server; the call at line "
            "%d does not say so" % call.lineno
        )
        value = keywords["detach_owner"]
        assert isinstance(value, ast.Constant) and value.value is True, (
            "the server's daemon must outlive the server; the call at line "
            "%d asks for the opposite" % call.lineno
        )
