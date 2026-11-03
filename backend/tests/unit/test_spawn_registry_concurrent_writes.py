"""The spawn registry is written by several processes at the same time.

Why this file exists
--------------------
The spawn registry is the list of the processes a run started, so that a
cleanup path can give them back.  It lives in one file, and that file is
written by everybody in the run: the gate writes itself down, the CI
runner does, and every service writes down the reaper daemon it starts.

Two of those writes used to race:

* ``register_spawned_process()`` appended a line to the file;
* ``_write_spawn_registry()`` rewrote the whole file with
  ``open(target, "w")``.

Neither took a lock.  So a rewrite that ran while another process was
appending truncated the file and wrote back only the entries it had
read -- and the line the other process had just appended was gone.
That is not a cosmetic loss: an entry that is missing is a process
nobody can give back, which is the whole failure the registry exists to
prevent.

The rewrite made the file unreadable as well.  A registry written in
place is a half-written file for as long as the write takes, and a
reader that cannot parse a line throws it away -- so a reader that
opened the registry at the wrong moment saw fewer entries than the
registry held.

What has to be true now
------------------------
* every write is serialised against every other write;
* every write is atomic: a reader sees the old file or the new one,
  never half of either;
* a cleanup pass removes the entries it acted on and *only* those -- an
  entry another process wrote down while the pass was waiting for a
  process to die has to survive;
* the owner filter still decides whose processes a pass gives back;
* a process whose owner is still there is not given back: the registry
  is shared by everything a run starts, so a pass that sweeps all of it
  would otherwise take the services of a run that is still going.

The tests below do not kill anything.  Every entry names a pid that
does not exist, and the liveness probe is injected, so the pass takes
the time it says it will without a signal ever reaching the kernel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import orphan_reaper as reaper  # noqa: E402


# --------------------------------------------------------------------------
# The children
# --------------------------------------------------------------------------
#
# The whole point of this file is about *processes*, so the writers are
# processes: a thread would share the interpreter's buffers and would
# not have shown the bug.  Each script is run with the interpreter this
# test runs under, so the child is the same code the parent is.


#: Write *count* entries down, one after another.
_APPEND_MANY = """
import os, sys
sys.path.insert(0, sys.argv[1])
import orphan_reaper as reaper

registry, tag, count = sys.argv[2], sys.argv[3], int(sys.argv[4])
for index in range(count):
    reaper.register_spawned_process(
        os.getpid(), kind=reaper.KIND_SERVICE,
        name="%s-%03d" % (tag, index), path=registry,
    )
"""


#: Write one entry down, after a delay.
_APPEND_ONE = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import orphan_reaper as reaper

delay, registry, name = float(sys.argv[2]), sys.argv[3], sys.argv[4]
time.sleep(delay)
reaper.register_spawned_process(
    os.getpid(), kind=reaper.KIND_SERVICE, name=name, path=registry,
)
"""


#: Rewrite the registry over and over, for a while.
_REWRITE_LOOP = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import orphan_reaper as reaper

registry, seconds = Path(sys.argv[2]), float(sys.argv[3])
entries = [
    {
        "pid": 100000 + index, "pgid": None, "kind": "service",
        "name": "synthetic-%04d-%s" % (index, "x" * 300),
        "owner_pid": 1, "start_epoch": "1", "registered_at": 0.0,
    }
    for index in range(300)
]
deadline = time.monotonic() + seconds
while time.monotonic() < deadline:
    reaper._write_spawn_registry(registry, entries)
"""


#: Write entries down over and over, for a while.
_APPEND_LOOP = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import orphan_reaper as reaper

registry, seconds = sys.argv[2], float(sys.argv[3])
deadline = time.monotonic() + seconds
index = 0
while time.monotonic() < deadline:
    reaper.register_spawned_process(
        os.getpid(), kind=reaper.KIND_SERVICE,
        name="appender-%05d" % index, path=registry,
    )
    index += 1
"""


def _child(script: str, *args: object) -> subprocess.Popen:
    """Run *script* in a process of its own."""
    return subprocess.Popen(
        [sys.executable, "-c", script, *[str(arg) for arg in args]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _finish(child: subprocess.Popen, timeout: float = 120.0) -> None:
    """Wait for *child* and insist that it succeeded."""
    stdout, stderr = child.communicate(timeout=timeout)
    assert child.returncode == 0, (
        f"the child process failed ({child.returncode}):\n"
        f"{stdout}\n{stderr}"
    )


def _stop(child: subprocess.Popen) -> None:
    """Stop *child* if it is still running."""
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        child.kill()


# --------------------------------------------------------------------------
# Every append is seen
# --------------------------------------------------------------------------


def test_parallel_appends_are_all_visible(tmp_path):
    """Eight processes, twenty-five entries each: two hundred entries.

    The registry is written by everybody in the run, and none of them
    knows about the others.  A line that is written and then lost is a
    process that is never given back, so every one of the two hundred
    lines has to be in the file at the end.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    children = 8
    per_child = 25
    procs = [
        _child(
            _APPEND_MANY, BACKEND_DIR, registry, f"child-{index}", per_child,
        )
        for index in range(children)
    ]
    try:
        for proc in procs:
            _finish(proc)
    finally:
        for proc in procs:
            _stop(proc)

    entries = reaper.read_spawn_registry(registry)
    assert len(entries) == children * per_child, (
        f"{len(entries)} entries are in the registry, "
        f"{children * per_child} were written: {entries}"
    )
    names = {entry["name"] for entry in entries}
    assert len(names) == children * per_child, (
        f"two of the writes landed on top of each other: {sorted(names)}"
    )


# --------------------------------------------------------------------------
# A cleanup pass does not drop what was written while it ran
# --------------------------------------------------------------------------


def test_a_reap_does_not_drop_an_entry_written_meanwhile(tmp_path, monkeypatch):
    """An entry written during a pass survives the pass.

    The pass reads the registry, gives the processes back -- which takes
    as long as the grace period says -- and then writes the registry
    back.  A process that starts in between is written down during
    exactly that window, and a pass that wrote back what *it* had read
    would take that new entry with it: the process would be running and
    unregistered, which is the state the registry exists to prevent.

    Nothing is killed here.  The entry names a pid that does not exist,
    the signal is stubbed out, and the liveness probe is injected, so
    the pass takes the time it says it will without a signal reaching
    the kernel.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    victim = {
        "pid": 4242, "pgid": None, "kind": "service", "name": "victim",
        "owner_pid": os.getpid(), "start_epoch": "100", "registered_at": 0.0,
    }
    registry.write_text(
        json.dumps(victim, ensure_ascii=False) + "\n", encoding="utf-8",
    )

    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(reaper.os, "killpg", lambda pgid, sig: None)

    started = threading.Event()
    alive_until = time.monotonic() + 1.7

    def alive(pid: int) -> bool:
        # The first call happens after the pass has read the registry,
        # so the test knows the snapshot has been taken.
        started.set()
        return time.monotonic() < alive_until

    reaped: list = []

    def run_pass() -> None:
        reaped.extend(
            reaper.reap_spawned_processes(
                registry, reason="the gate is over", grace_seconds=2.0,
                probe=lambda pid: "100", alive=alive,
            )
        )

    worker = threading.Thread(target=run_pass)
    worker.start()
    child = None
    try:
        assert started.wait(30), "the pass never got as far as the entry"
        child = _child(
            _APPEND_ONE, BACKEND_DIR, 0.0, registry, "written-meanwhile",
        )
        _finish(child)
    finally:
        if child is not None:
            _stop(child)
        worker.join(timeout=60)
    assert not worker.is_alive(), "the pass did not finish"

    entries = reaper.read_spawn_registry(registry)
    assert [entry["name"] for entry in entries] == ["written-meanwhile"], (
        f"the entry written during the pass was dropped: {entries}"
    )
    assert [entry["pid"] for entry in reaped] == [4242], (
        f"the pass gave back something else: {reaped}"
    )


# --------------------------------------------------------------------------
# A rewrite is atomic
# --------------------------------------------------------------------------


def test_the_rewrite_is_atomic(tmp_path):
    """A reader never sees half a line.

    The registry is rewritten while it is read -- by a cleanup pass, and
    by anything else that has to put entries back.  A registry written
    in place is a half-written file for as long as the write takes, and
    a reader that cannot parse a line throws it away.  So every line a
    reader sees has to be a whole line.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    rewriter = _child(_REWRITE_LOOP, BACKEND_DIR, registry, 2.0)
    appender = _child(_APPEND_LOOP, BACKEND_DIR, registry, 2.0)
    reads = 0
    try:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                raw = registry.read_bytes()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AssertionError(
                    "the registry was read half-written"
                ) from exc
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as exc:
                    raise AssertionError(
                        f"the registry was read half-written: {line!r}"
                    ) from exc
                assert entry.get("pid"), (
                    f"a whole line without a pid: {entry!r}"
                )
            reads += 1
    finally:
        _stop(rewriter)
        _stop(appender)

    assert reads > 10, (
        f"the reader did not get to read the registry: {reads} reads"
    )


# --------------------------------------------------------------------------
# The owner filter still decides whose processes are given back
# --------------------------------------------------------------------------


def test_owner_filtering_is_preserved(tmp_path):
    """A pass gives back the processes of one owner and nobody else's.

    The registry holds the processes of everybody in the run.  A pass
    that narrows itself to one owner -- the harness that spawned a
    server wants the daemon *that server* started, and not the services
    of the whole run -- must leave the other owners' entries alone.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    mine = {
        "pid": 4242, "pgid": None, "kind": "service", "name": "mine",
        "owner_pid": 111, "start_epoch": "100", "registered_at": 0.0,
    }
    theirs = {
        "pid": 4243, "pgid": None, "kind": "service", "name": "theirs",
        "owner_pid": 222, "start_epoch": "100", "registered_at": 0.0,
    }
    registry.write_text(
        json.dumps(mine, ensure_ascii=False) + "\n"
        + json.dumps(theirs, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    reaped = reaper.reap_spawned_processes(
        registry, reason="the gate is over", grace_seconds=1.0,
        owner_pid=111, alive=lambda pid: False,
    )

    entries = reaper.read_spawn_registry(registry)
    assert [entry["name"] for entry in entries] == ["theirs"], (
        f"the pass touched an entry that belongs to another owner: {entries}"
    )
    assert reaped == [], (
        f"a process that was already gone was reported as given back: {reaped}"
    )


# --------------------------------------------------------------------------
# A live owner keeps its processes
# --------------------------------------------------------------------------
#
# The registry is shared by everything a run starts, and a cleanup pass
# sweeps all of it -- not just the entries of the process that happens to
# be doing the sweeping.  Two runs can share one registry: a gate that
# runs inside another gate's pytest worker inherits the registry the
# outer run named, and the inner gate's own way out sweeps it.
#
# A sweep that does not ask *who* owns an entry therefore takes processes
# away from a run that is still going.  The services the outer worker
# started died that way -- signalled by a gate that was on its way out
# and had no business touching them.
#
# So an entry whose owner is still there is not this pass's to give back:
# no signal goes out, and the entry stays in the registry for the run
# that owns it.


def _record_signals(monkeypatch) -> list:
    """Record every signal the reaper sends, and send none of them.

    ``orphan_reaper`` reaches the kernel through the ``os`` module it
    imported, so patching ``os.kill`` / ``os.killpg`` here is what
    "did the pass signal anything?" is answered by.
    """
    calls: list = []

    def _kill(pid, sig):
        calls.append(("kill", pid, sig))

    def _killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))

    monkeypatch.setattr(reaper.os, "kill", _kill)
    monkeypatch.setattr(reaper.os, "killpg", _killpg)
    return calls


def _alive_for(probes: int):
    """A liveness probe that says *alive* for the first *probes* probes.

    The pass asks whether the process is there before it signals it, and
    then asks again while it waits for the process to go.  A probe that
    answers *alive* once and *gone* afterwards is enough to walk the
    whole path -- and it keeps the test from waiting out a grace period.
    """
    state = {"calls": 0}

    def probe(pid: int) -> bool:
        state["calls"] += 1
        return state["calls"] <= probes

    return probe


#: A pid that is not this process: the owner has to be somebody else.
_OTHER_OWNER = 424242


def _entry_for(owner_pid: int, name: str = "service") -> dict:
    """A registry entry that names *owner_pid* as the process that wrote it."""
    return {
        "pid": 4242, "pgid": None, "kind": "service", "name": name,
        "owner_pid": owner_pid, "start_epoch": "100", "registered_at": 0.0,
    }


def test_a_process_whose_owner_is_alive_is_not_given_back(tmp_path, monkeypatch):
    """The owner is still there: this pass does not give its process back.

    The entry belongs to a process that is still running, and that
    process is not this one.  It may still want the service it started
    -- it is the run that started it, and the run that has to give it
    back.  Signalling it here would take a live service away from a run
    that is still going.

    So: no signal goes out, and the entry stays in the registry.
    """
    calls = _record_signals(monkeypatch)
    registry = tmp_path / "spawn_registry.jsonl"
    entry = _entry_for(_OTHER_OWNER)
    registry.write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    assert _OTHER_OWNER != os.getpid(), (
        "the owner of the entry has to be another process"
    )

    reaped = reaper.reap_spawned_processes(
        registry, reason="the gate is over", grace_seconds=1.0,
        probe=lambda pid: "100", alive=_alive_for(1),
        owner_alive=lambda pid: True,
    )

    assert calls == [], (
        f"the pass signalled a process whose owner is still alive: {calls}"
    )
    assert reaped == [], (
        f"a process whose owner is still alive was given back: {reaped}"
    )
    survivors = reaper.read_spawn_registry(registry)
    assert [entry["name"] for entry in survivors] == ["service"], (
        f"the entry was dropped although nothing was given back: {survivors}"
    )


def test_a_process_whose_owner_is_gone_is_given_back(tmp_path, monkeypatch):
    """The owner is gone: the process is this pass's to give back.

    The owner has exited, so there is nobody left who could want the
    process it started.  The owner guard must not turn into a reason to
    leave a service running forever: the process is signalled, and the
    entry leaves the registry with it.
    """
    calls = _record_signals(monkeypatch)
    registry = tmp_path / "spawn_registry.jsonl"
    entry = _entry_for(_OTHER_OWNER)
    registry.write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8",
    )

    reaped = reaper.reap_spawned_processes(
        registry, reason="the gate is over", grace_seconds=1.0,
        probe=lambda pid: "100", alive=_alive_for(1),
        owner_alive=lambda pid: False,
    )

    assert calls, "the pass did not signal a process whose owner is gone"
    assert [int(entry["pid"]) for entry in reaped] == [4242], (
        f"the pass gave back something else: {reaped}"
    )
    assert reaper.read_spawn_registry(registry) == [], (
        "the entry of a process that was given back stayed in the registry"
    )


def test_a_process_the_caller_owns_is_given_back(tmp_path, monkeypatch):
    """The owner is this process: the process is ours to give back.

    The owner guard is about *another* run's processes.  The processes
    this process started are this pass's to give back, and that is the
    whole point of the registry: the run that started a service is the
    run that stops it.
    """
    calls = _record_signals(monkeypatch)
    registry = tmp_path / "spawn_registry.jsonl"
    entry = _entry_for(os.getpid())
    registry.write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8",
    )

    reaped = reaper.reap_spawned_processes(
        registry, reason="the gate is over", grace_seconds=1.0,
        probe=lambda pid: "100", alive=_alive_for(1),
        owner_alive=lambda pid: True,
    )

    assert calls, "the pass did not signal a process its own owner started"
    assert [int(entry["pid"]) for entry in reaped] == [4242], (
        f"the pass gave back something else: {reaped}"
    )
    assert reaper.read_spawn_registry(registry) == [], (
        "the entry of a process that was given back stayed in the registry"
    )


# --------------------------------------------------------------------------
# The edges the lock has to survive
# --------------------------------------------------------------------------


def test_the_registry_still_works_without_fcntl(tmp_path, monkeypatch):
    """A platform without ``fcntl`` writes the registry all the same.

    The lock is a ``flock``, and ``flock`` is POSIX.  A platform that
    has none must not raise: the registry is still written, and the
    write is still atomic -- it just carries no guarantee against a
    concurrent writer.
    """
    monkeypatch.setattr(reaper, "fcntl", None)
    registry = tmp_path / "spawn_registry.jsonl"

    reaper.register_spawned_process(
        os.getpid(), kind=reaper.KIND_SERVICE, name="one", path=registry,
    )
    reaper.register_spawned_process(
        os.getpid(), kind=reaper.KIND_SERVICE, name="two", path=registry,
    )

    assert [entry["name"] for entry in reaper.read_spawn_registry(registry)] == [
        "one", "two",
    ], "the registry was not written without fcntl"

    reaped = reaper.reap_spawned_processes(
        registry, grace_seconds=1.0, alive=lambda pid: False,
    )
    assert reaped == [], f"a process that was already gone was reaped: {reaped}"
    assert reaper.read_spawn_registry(registry) == [], (
        "the entries were not taken out of the registry"
    )


def test_a_registry_with_nothing_left_in_it_is_emptied_but_kept(tmp_path):
    """The registry is emptied; it is not removed.

    The file is named by the environment, and everything in the run
    inherits that name.  A path that is gone is a path that cannot be
    written to any more -- the next process to register something would
    have to create the directory again, and a reader that arrived in
    between would see no registry at all.
    """
    registry = tmp_path / "spawn_registry.jsonl"
    reaper.register_spawned_process(
        4242, kind=reaper.KIND_SERVICE, name="gone", path=registry,
    )
    assert registry.exists(), "the registry was not written"

    reaper.reap_spawned_processes(
        registry, grace_seconds=1.0, alive=lambda pid: False,
    )

    assert registry.exists(), "the registry was removed instead of emptied"
    assert registry.read_text(encoding="utf-8") == "", (
        "the registry still holds entries that were given back"
    )
