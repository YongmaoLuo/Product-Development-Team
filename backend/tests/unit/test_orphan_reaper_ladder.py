"""The verdict ladder: six rungs, and every one of them only stops a kill.

The reaper's whole safety argument rests on one shape: **the ladder only
ever subtracts**.  Every rung is a reason not to kill; there is no rung
that turns a skip into a kill.  The only way to reach the last rung —
*orphan* — is to have passed all the others, and the last rung is the
only one that may lead to a signal.

That shape is what makes the two controls below the most important tests
in the file:

* the **positive control** — the owner is alive and is the process that
  spawned the candidate → the candidate is skipped.  A reaper that kills
  a service somebody is using is the worst thing it could do.
* the **negative control** — the owner is dead → the candidate is an
  orphan → it may be killed.  A reaper that never kills anything is
  useless, and the failure mode is invisible until the box dies.

The other rungs are the ones that catch the mistakes that would make
the negative control *unsafe*:

* a pid that was **recycled** by the kernel is a different process that
  happens to carry the same number → never touched;
* a process with **no marker** is not ours → never touched;
* a process **holding a protected port** is probably a service somebody
  depends on → never touched;
* the **reaper itself** is never a candidate;
* a process belonging to **another user** is never ours to touch;
* an environment that **could not be read** is *unknown*, not *empty* —
  and "unknown" must never degrade into "this process carries no
  marker", because that degradation is exactly how an unrelated process
  ends up on the kill list.

:class:`~orphan_reaper.classify` is a **pure** function: it reads what
it is given and returns a verdict.  It sends no signal, writes no file
and mutates no global state — which is what lets the controls above be
two lines of fixture instead of two subprocesses.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402
from orphan_reaper import (  # noqa: E402
    SPAWN_KIND,
    SPAWN_OWNER_PID,
    SPAWN_OWNER_START,
    OwnerState,
    ProcInfo,
    ReaperConfig,
    classify,
    scan,
)

#: The uid the tests pretend to run as.
SELF_UID = os.getuid()


def make_proc(pid=4242, uid=SELF_UID, env=None):
    """Build a candidate process.

    The defaults describe a service that was started by somebody: it has
    a parent (pid 1 — already re-parented, which is what an orphan
    looks like), its own process group, and a command line.
    """
    return ProcInfo(
        pid=pid,
        ppid=1,
        pgid=pid,
        uid=uid,
        rss_kb=1024,
        argv="/usr/bin/python3 -m backend.server",
        env=env,
    )


def marked_env(owner_pid=999, owner_start="17", kind="service"):
    """Build the marker a PDT-spawned process carries.

    Passing ``None`` for a field leaves it out entirely, which is how a
    process with a *partial* marker is described.
    """
    env = {}
    if owner_pid is not None:
        env[SPAWN_OWNER_PID] = str(owner_pid)
    if owner_start is not None:
        env[SPAWN_OWNER_START] = str(owner_start)
    if kind is not None:
        env[SPAWN_KIND] = str(kind)
    return env


def make_config(**kwargs):
    """A configuration with the default protected ports."""
    kwargs.setdefault("protect_ports", frozenset({8000, 8002, 3000, 3001, 8651}))
    return ReaperConfig(**kwargs)


# --------------------------------------------------------------------------
# The two controls
# --------------------------------------------------------------------------


def test_ladder_positive_control():
    """A live owner whose start time matches is never a candidate.

    This is the case that matters most in production: the service is
    running, somebody is using it, and the reaper must not touch it.
    """
    proc = make_proc(env=marked_env(owner_pid=999, owner_start="17"))

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=True, start_epoch="17"),
        port_map={},
    )

    assert verdict.decision == "skip"
    assert verdict.reason == "owner_alive"
    assert verdict.owner_pid == 999
    assert verdict.owner_start == "17"
    assert verdict.kind == "service"


def test_ladder_negative_control():
    """A dead owner makes the candidate an orphan — the one kill reason."""
    proc = make_proc(env=marked_env(owner_pid=999, owner_start="17"))

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
    )

    assert verdict.decision == "kill"
    assert verdict.reason == "orphan"
    assert verdict.owner_pid == 999
    assert verdict.owner_start == "17"
    assert verdict.kind == "service"
    assert verdict.signal == ""


# --------------------------------------------------------------------------
# The rungs that stop a kill
# --------------------------------------------------------------------------


def test_ladder_pid_reused():
    """A live owner pid whose start time differs is a different process.

    The kernel recycles pids.  A candidate whose recorded owner start
    time no longer matches the live pid's start time belongs to whoever
    owns that pid *now* — which is not us.
    """
    proc = make_proc(env=marked_env(owner_pid=999, owner_start="100"))

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=True, start_epoch="160"),
        port_map={},
    )

    assert verdict.decision == "skip"
    assert verdict.reason == "pid_reused"


def test_ladder_unmarked_and_protected():
    """Two different ways of not being a candidate."""
    unmarked = make_proc(pid=1001, env={})
    protected = make_proc(pid=1002, env=marked_env(owner_pid=999, owner_start="17"))

    unmarked_verdict = classify(
        unmarked,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
    )
    protected_verdict = classify(
        protected,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={1002: [8000]},
    )

    assert unmarked_verdict.decision == "skip"
    assert unmarked_verdict.reason == "unmarked"
    assert protected_verdict.decision == "skip"
    assert protected_verdict.reason == "protected_port"


def test_ladder_reaper_never_kills_itself():
    """A reaper is never a candidate, however dead its owner is."""
    proc = make_proc(
        pid=4243, env=marked_env(owner_pid=999, owner_start="17", kind="reaper")
    )

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
    )

    assert verdict.decision == "skip"
    assert verdict.reason == "self_reaper"


def test_ladder_other_uid_skipped():
    """A process belonging to another user is never ours to touch.

    The owner being dead makes no difference: the candidate is skipped
    before the ladder is even walked.
    """
    proc = make_proc(
        pid=4244,
        uid=SELF_UID + 1,
        env=marked_env(owner_pid=999, owner_start="17"),
    )

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
        self_uid=SELF_UID,
    )

    assert verdict.decision == "skip"
    assert verdict.reason == "other_uid"


def test_ladder_unreadable_env_is_not_unmarked():
    """An environment that cannot be read is *unknown*, never *empty*.

    ``read_process_environ`` returns ``None`` for a process it could not
    read — an Apple platform binary, or one this user may not inspect.
    Treating that as "carries no marker" would put an unrelated process
    on the kill list, so the two states must stay apart: the reason is
    ``env_unreadable`` and never ``unmarked``.
    """
    proc = make_proc(pid=4245, env=None)

    result = scan(
        make_config(),
        list_procs=lambda: [proc],
        env_reader=lambda pid: None,
        owner_probe=lambda pid: OwnerState(alive=False),
        port_map_provider=lambda: {},
        self_uid=SELF_UID,
    )

    assert len(result.verdicts) == 1
    verdict = result.verdicts[0]
    assert verdict.reason == "env_unreadable"
    assert verdict.reason != "unmarked"
    assert verdict.decision == "skip"


def test_ladder_unreadable_env_directly_is_not_unmarked():
    """The same rule, stated on :func:`classify` itself."""
    proc = make_proc(pid=4246, env=None)

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
        self_uid=SELF_UID,
    )

    assert verdict.reason == "env_unreadable"
    assert verdict.reason != "unmarked"
    assert verdict.decision == "skip"


def test_ladder_self_is_never_a_candidate():
    """The reaper must not kill itself, and must not kill pid 1."""
    own = make_proc(pid=os.getpid(), env=marked_env(owner_pid=999, owner_start="17"))
    init = make_proc(pid=1, env=marked_env(owner_pid=999, owner_start="17"))

    own_verdict = classify(
        own,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
        self_uid=SELF_UID,
    )
    init_verdict = classify(
        init,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
        self_uid=SELF_UID,
    )

    assert own_verdict.decision == "skip"
    assert own_verdict.reason == "self"
    assert init_verdict.decision == "skip"
    assert init_verdict.reason == "self"


def test_ladder_unknown_ports_never_kill():
    """A port map that could not be read is *unknown*, never *empty*."""
    proc = make_proc(env=marked_env(owner_pid=999, owner_start="17"))

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map=None,
        self_uid=SELF_UID,
    )

    assert verdict.decision == "skip"
    assert verdict.reason == "ports_unknown"


def test_ladder_owner_alive_without_start_is_unverified():
    """A live owner whose start time cannot be compared is still skipped.

    The marker carries no start time (or the probe could not determine
    one), so the owner cannot be *confirmed* — but it is alive, and
    uncertainty resolves towards not killing.
    """
    proc = make_proc(env=marked_env(owner_pid=999, owner_start=None))

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=True, start_epoch="17"),
        port_map={},
        self_uid=SELF_UID,
    )

    assert verdict.decision == "skip"
    assert verdict.reason == "owner_alive_unverified"


def test_classify_sends_no_signal(monkeypatch):
    """The ladder is a pure function — it never signals anything.

    Even the one verdict that *may* lead to a kill does not kill by
    itself: the signalling half is a separate stage that re-checks the
    owner before it acts.
    """
    signals = []
    monkeypatch.setattr(reaper.os, "kill", lambda *args, **kwargs: signals.append(args))
    proc = make_proc(env=marked_env(owner_pid=999, owner_start="17"))

    verdict = classify(
        proc,
        config=make_config(),
        owner_state=OwnerState(alive=False),
        port_map={},
        self_uid=SELF_UID,
    )

    assert verdict.decision == "kill"
    assert signals == []


# --------------------------------------------------------------------------
# scan — one pass over the machine
# --------------------------------------------------------------------------


def _scan_fixture():
    """Three candidates: one with a live owner, one with a dead owner, one unmarked."""
    envs = {
        5001: marked_env(owner_pid=9001, owner_start="17"),
        5002: marked_env(owner_pid=9002, owner_start="17"),
        5003: {},
    }
    procs = [make_proc(pid=pid, env=None) for pid in sorted(envs)]

    def owner_probe(pid):
        if pid == 9001:
            return OwnerState(alive=True, start_epoch="17")
        return OwnerState(alive=False)

    return procs, envs, owner_probe


def test_scan_judges_every_process():
    """One scan judges every process it sees and says why."""
    procs, envs, owner_probe = _scan_fixture()

    result = scan(
        make_config(),
        list_procs=lambda: procs,
        env_reader=lambda pid: envs.get(pid),
        owner_probe=owner_probe,
        port_map_provider=lambda: {},
        self_uid=SELF_UID,
    )

    assert [v.proc.pid for v in result.verdicts] == [5001, 5002, 5003]
    assert [v.reason for v in result.verdicts] == ["owner_alive", "orphan", "unmarked"]
    assert [v.decision for v in result.verdicts] == ["skip", "kill", "skip"]


def test_scan_counts_owners_alive():
    """The scan reports how many candidates still belong to a live owner."""
    procs, envs, owner_probe = _scan_fixture()

    result = scan(
        make_config(),
        list_procs=lambda: procs,
        env_reader=lambda pid: envs.get(pid),
        owner_probe=owner_probe,
        port_map_provider=lambda: {},
        self_uid=SELF_UID,
    )

    assert result.owners_alive == 1


def test_scan_respects_pid_scope():
    """A configured scope restricts the scan to the pids in it."""
    procs, envs, owner_probe = _scan_fixture()

    result = scan(
        make_config(pid_scope=(5002,)),
        list_procs=lambda: procs,
        env_reader=lambda pid: envs.get(pid),
        owner_probe=owner_probe,
        port_map_provider=lambda: {},
        self_uid=SELF_UID,
    )

    assert [v.proc.pid for v in result.verdicts] == [5002]


def test_scan_with_unknown_ports_skips_everything():
    """A failed port probe skips every candidate — it never kills blind.

    The candidate whose owner is dead is the one that reaches the port
    rung, and an unreadable port map stops it there: it must not become
    an orphan while the ports it holds are unknown.
    """
    procs, envs, owner_probe = _scan_fixture()

    result = scan(
        make_config(),
        list_procs=lambda: procs,
        env_reader=lambda pid: envs.get(pid),
        owner_probe=owner_probe,
        port_map_provider=lambda: None,
        self_uid=SELF_UID,
    )

    assert result.verdicts
    assert all(v.decision == "skip" for v in result.verdicts)
    by_pid = {v.proc.pid: v.reason for v in result.verdicts}
    assert by_pid[5001] == "owner_alive"
    assert by_pid[5002] == "ports_unknown"
    assert by_pid[5003] == "unmarked"
