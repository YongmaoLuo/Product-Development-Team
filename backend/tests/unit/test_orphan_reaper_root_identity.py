"""A daemon has to leave when the root it watches is a different root.

Why this file exists
--------------------
The reaper daemon decides that its world is gone by asking whether the
plan-artifact root still **exists**.  Existence is not identity.

A root can be deleted and another one created in its place, and the
new directory has the same name: the path is unchanged, so the
existence check says everything is fine.  The daemon goes on watching a
directory it was never started for -- and, worse, it keeps holding a
lock on a file that is no longer there.

That is not a hypothetical.  On 2026-11-02 one root carried fifteen
daemons at once: ``.orphan_reaper/reaper.lock`` was deleted and
recreated, so every daemon saw a lock file that was not the one it
held, and every one of them believed it was alone.

The judgement
-------------
A directory is identified by the filesystem's own name for it: its
device and its inode.  The daemon records that pair when it starts and
compares it on every pass; a root that answers with a different pair
is a different root, and the daemon leaves.

Uncertainty resolves towards **staying**, as it does everywhere else
in this module:

* a root that cannot be stat'ed is *unknown*, not *replaced* -- an
  unreadable directory is not a reason to stop reclaiming memory;
* a root that is simply not there is the case the existence check
  already covers, so it reads as "replaced" here too;
* a daemon that never managed to record an identity cannot judge a
  replacement, so it does not try.

Nothing in this file touches a real process table or sends a signal.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import ReaperConfig, ScanResult  # noqa: E402


class _Loop:
    """A stop event that never stops and never blocks.

    The daemon has to be testable without waiting for a real interval
    and without a real signal, so the loop is driven by a stand-in
    that answers immediately.
    """

    def __init__(self, stop_after_waits: int = 0) -> None:
        self.stopped = False
        self.waits = 0
        self.stop_after_waits = stop_after_waits

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout=None) -> bool:
        self.waits += 1
        if self.stop_after_waits and self.waits >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


def _ticks(values):
    """A clock that walks *values* and then stands still.

    The daemon reads its clock once before the loop and once per pass;
    a clock that ran out would otherwise raise, and the daemon's reader
    turns a raising clock into the previous reading.
    """
    remaining = list(values)
    state = {"index": 0}

    def clock():
        index = state["index"]
        state["index"] += 1
        if index < len(remaining):
            return remaining[index]
        return remaining[-1] if remaining else 0.0

    return clock


def _no_reaping(verdicts, config):
    """A reaping stage that signals nothing.

    The daemon's own cleanup would otherwise act on the fixture
    verdicts, and a fixture pid is a real pid on the machine the test
    runs on.
    """
    return []


def _recreate(root: Path) -> None:
    """Delete *root* and put a **different** directory in its place.

    The replacement is built while the original is still there, so the
    two exist at the same moment and the filesystem cannot hand out the
    same inode twice.  A plain "delete, then create" can land on the
    same inode number again on a filesystem that reuses them -- and the
    test would then be measuring the filesystem's allocator instead of
    the daemon's judgement.
    """
    replacement = root.with_name(root.name + "-replacement")
    replacement.mkdir()
    shutil.rmtree(root)
    os.rename(replacement, root)


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


def test_same_directory_is_not_replaced(tmp_path):
    """A root that has not changed is not a root that was replaced.

    The ordinary case: the daemon recorded the identity of the
    directory it was started for, the directory is still that one, and
    the daemon keeps working.
    """
    root = tmp_path / "plans"
    root.mkdir()

    identity = reaper._plans_root_identity(root)

    assert identity is not None
    assert reaper._plans_root_replaced(root, identity) is False


def test_recreated_directory_is_replaced(tmp_path):
    """A root that was deleted and re-created is a different root.

    The path is the same and the directory is there, so existence says
    everything is fine -- but the daemon is now watching a directory
    it was never started for, and it has to notice.
    """
    root = tmp_path / "plans"
    root.mkdir()

    identity = reaper._plans_root_identity(root)
    _recreate(root)

    assert reaper._plans_root_identity(root) != identity, (
        "the filesystem handed out the same inode again"
    )
    assert reaper._plans_root_replaced(root, identity) is True


def test_unreadable_root_is_not_replaced(tmp_path, monkeypatch):
    """A root that cannot be read is unknown, not replaced.

    The filesystem declined to answer.  That is not the same as
    answering "this is a different directory", and a daemon that leaves
    on a guess stops reclaiming memory on a machine that still needs
    it.
    """
    root = tmp_path / "plans"
    root.mkdir()

    identity = reaper._plans_root_identity(root)

    class _Unreadable:
        """A path whose ``stat`` fails the way an unreadable one does."""

        def __init__(self, path):
            self._path = path

        def stat(self):
            raise OSError("the directory cannot be read")

    monkeypatch.setattr(reaper, "Path", _Unreadable)

    assert reaper._plans_root_replaced(root, identity) is False


def test_missing_root_counts_as_replaced(tmp_path):
    """A root that is not there is the case existence already covers.

    The two checks have to agree: a root that is gone reads as "gone"
    to the existence check and as "replaced" here, and the daemon
    leaves for it in either case.
    """
    root = tmp_path / "plans"
    root.mkdir()

    identity = reaper._plans_root_identity(root)
    shutil.rmtree(root)

    assert reaper._plans_root_replaced(root, identity) is True


def test_a_root_without_an_identity_is_never_replaced(tmp_path):
    """No recorded identity means no judgement.

    The daemon that never managed to record an identity cannot tell a
    replacement from the root it started with, so it does not pretend
    it can.
    """
    root = tmp_path / "plans"
    root.mkdir()

    assert reaper._plans_root_identity(root) is not None
    assert reaper._plans_root_replaced(root, None) is False


def test_an_unreadable_root_has_no_identity(tmp_path, monkeypatch):
    """A root that cannot be stat'ed has no identity to record.

    ``None`` is the honest answer: the daemon knows nothing about the
    directory it was started for, and says so.
    """
    class _Unreadable:
        def __init__(self, path):
            self._path = path

        def stat(self):
            raise OSError("the directory cannot be read")

    monkeypatch.setattr(reaper, "Path", _Unreadable)

    assert reaper._plans_root_identity(tmp_path) is None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_loop_leaves_when_root_replaced(tmp_path):
    """The daemon leaves once the root it watches is a different root.

    The check is at the top of the loop, so the daemon leaves on the
    first pass that sees the replacement -- and it does not scan the
    directory it was not started for.
    """
    root = tmp_path / "plans"
    root.mkdir()
    scans = []

    def fake_scan(_config):
        scans.append(1)
        _recreate(root)
        return ScanResult(verdicts=[], owners_alive=1)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=root, interval=1, idle_exit=1800),
        stop=_Loop(stop_after_waits=5),
        clock=_ticks([0.0, 1000.0, 2000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert len(scans) == 1, (
        "the daemon went on scanning a root that was replaced"
    )


def test_the_loop_keeps_going_while_the_root_stays(tmp_path):
    """A root that stays is a root the daemon keeps watching.

    The other half of the contract: the door out is a *replacement*,
    not the passage of time.
    """
    root = tmp_path / "plans"
    root.mkdir()
    scans = []

    def fake_scan(_config):
        scans.append(1)
        return ScanResult(verdicts=[], owners_alive=1)

    code = reaper.run_daemon(
        ReaperConfig(plans_root=root, interval=1, idle_exit=1800),
        stop=_Loop(stop_after_waits=3),
        clock=_ticks([0.0, 1000.0, 2000.0, 3000.0]),
        scan_fn=fake_scan,
        reap_fn=_no_reaping,
    )

    assert code == 0
    assert len(scans) == 3
