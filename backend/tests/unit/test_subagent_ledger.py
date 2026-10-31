"""A sub-agent's process has to be on record while it is still running.

Why this file exists
--------------------
The backend spawns its sub-agents as detached subprocesses: the
``claude`` CLI is started with ``start_new_session=True``, so it leads
a process group of its own and outlives the Python that started it.
The process group id is only knowable *at the moment of the spawn* —
after that, nobody can work out which pid was ours.

So a sub-agent that is started and never recorded is an orphan the
moment the backend dies: there is no pid on record, no group to kill,
and nothing to reconcile against. The process ledger
(``backend/process_ledger.py``) is the durable answer, and this file
pins the two ends of it for the sub-agent path:

* **the spawn is recorded** — with the pid, the process group it
  leads, the kernel's start time and the command line, so a later
  sweep can tell "our process" from "somebody else's process that
  happens to have the same pid";
* **the exit is recorded** — every exit path (a clean finish, a
  timeout kill, the watchdog's kill) ends at the same place, and the
  ledger's live set must not keep a pid that is already gone.

Three things make this more than a log:

* a **failed spawn writes nothing**. A ``Popen`` that raised never
  produced a process, and a ledger entry for a pid that never existed
  is a lie the reaper would later act on;
* **the registry and the ledger agree**. The registry holds the
  handle in memory, the ledger holds the same facts on disk, and a
  handle that points at a process the ledger does not know about (or
  the other way round) means one of the two views is stale;
* **the owner keeps its handle**. The VP sub-agent, the rebuild agent
  and the service-restart agent each register their *own* handle
  before they dispatch, and the dispatch that finds it must not take
  it away — the watchdog needs it. The process still has to be
  recorded, though: the handle it belongs to is the one that knows
  which process is running.

The tests drive the production functions (``_register_dispatch`` /
``_unregister_dispatch``) and the real spawn point
(``ClaudeCodingTool._run_claude_interactive``) with a fake ``claude``
on ``PATH``. Nothing here reaches the network.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
import process_ledger as ledger  # noqa: E402
import process_reaper  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from sub_agent_registry import sub_agent_registry  # noqa: E402


PLAN = "plan-subagent-ledger"

#: The fake CLI holds its answer behind a gate file so the test can
#: look at the ledger while the sub-agent is still running. The
#: ledger's live set only says anything useful *during* the
#: sub-agent's life; after it, the exit event has already closed it.
_GATED_CLAUDE = """#!/bin/sh
while [ ! -f '{gate}' ]; do sleep 0.05; done
echo '{{"type":"result","result":"{{}}"}}'
"""

#: The fake CLI for a sub-agent that answers and exits on its own.
_FINISHING_CLAUDE = """#!/bin/sh
echo '{"type":"result","result":"ok"}'
"""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _use_plan_dir(tmp_path, monkeypatch, plan_id: str = PLAN) -> Path:
    """Point the plans root at ``tmp_path`` and return the plan's directory.

    ``resolve_plans_dir()`` re-reads the environment on every call, so
    the override lands even though the modules were imported before
    the fixture ran.
    """
    plans_root = tmp_path / "plans"
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))
    return plan_dir


def _install_fake_claude(tmp_path, monkeypatch, script: str) -> None:
    """Put an executable ``claude`` first on ``PATH``."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )


class _StandInTool:
    """The part of a coding tool the ledger path actually reads.

    The dispatch records *the process the tool just started*, so the
    only thing that matters here is that the tool can be asked for it.
    """

    def __init__(self, process, cwd: str):
        self._current_process = process
        self.cwd = cwd


def _spawn_holder():
    """A real process that stays alive until it is told to stop.

    Real, because the ledger records what the *kernel* says: the
    process group and the start time are read from the running
    process, not made up.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _reap(process) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        pass


def _spawn_events(plan_dir: Path):
    return [
        event for event in ledger.read_events(plan_dir)
        if event.get("event") == ledger.EVENT_SPAWN
    ]


def _exit_events(plan_dir: Path):
    return [
        event for event in ledger.read_events(plan_dir)
        if event.get("event") == ledger.EVENT_EXIT
    ]


def _wait_for_spawn(plan_dir: Path, timeout: float = 60.0):
    """Wait until the ledger reports a live sub-agent, then return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = ledger.live_records(plan_dir)
        if live:
            return next(iter(live.values()))
        time.sleep(0.05)
    raise AssertionError(
        "the ledger never reported a live sub-agent; events: %r"
        % (ledger.read_events(plan_dir),)
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    """Leave the module-level registry as the test found it."""
    before = {
        plan_id: list(handles)
        for plan_id, handles in sub_agent_registry._by_plan.items()
    }
    yield
    for plan_id, handles in list(sub_agent_registry._by_plan.items()):
        for handle in handles:
            sub_agent_registry.unregister(plan_id, handle)
    sub_agent_registry._by_plan.clear()
    sub_agent_registry._by_plan.update(before)


# ---------------------------------------------------------------------------
# 1. The spawn is recorded.
# ---------------------------------------------------------------------------
def test_subagent_spawn_writes_record_with_pgid(tmp_path, monkeypatch):
    """A running sub-agent is on the ledger, with the group it leads.

    Driven through the real spawn point: the fake ``claude`` holds its
    answer behind a gate, so the ledger can be read while the
    sub-agent is genuinely still running — which is the only moment
    the live set says anything.
    """
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    gate = tmp_path / "gate"
    _install_fake_claude(
        tmp_path, monkeypatch, _GATED_CLAUDE.format(gate=gate),
    )
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)

    tool = ClaudeCodingTool()
    answer = {}
    thread = threading.Thread(
        target=_dispatch, args=(tool, answer), daemon=True,
    )
    thread.start()
    try:
        record = _wait_for_spawn(plan_dir)
    finally:
        gate.write_text("go", encoding="utf-8")
        thread.join(timeout=120)

    assert record.role == "subagent", (
        "the ledger's role decides what a sweep is allowed to do with "
        f"the process; the record says {record.role!r}"
    )
    assert record.pgid == record.pid, (
        "the dispatch starts the child with start_new_session=True, so "
        "it leads a group of its own — that group is what a kill has "
        f"to target; the record says pgid={record.pgid} pid={record.pid}"
    )
    assert record.create_time > 0, (
        "the kernel's start time is what tells a recycled pid from the "
        "process we started"
    )
    assert record.cmdline, (
        "the command line is the second half of the identity check"
    )
    assert record.plan_id == PLAN
    assert record.owner_pid == os.getpid()
    assert record.owner_role == "backend"


def _dispatch(tool, answer) -> None:
    """Run one dispatch on its own thread and record how it ended."""
    try:
        answer["result"] = tool._run_claude_interactive(
            "hello", idle_timeout=30, total_timeout=30,
        )
    except BaseException as exc:  # pragma: no cover - surfaced by the test
        answer["error"] = exc


# ---------------------------------------------------------------------------
# 2. The exit is recorded.
# ---------------------------------------------------------------------------
def test_unregister_writes_exit_event(tmp_path, monkeypatch):
    """Unregistering the handle closes the pid in the ledger."""
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    process = _spawn_holder()
    tool = _StandInTool(process, str(tmp_path))
    try:
        registry, handle = coding_tool._register_dispatch(tool, PLAN)
        assert registry is not None
        assert handle is not None
        assert process.pid in ledger.live_records(plan_dir), (
            "the spawn was not recorded, so there is nothing to close"
        )

        coding_tool._unregister_dispatch(registry, PLAN, handle)
    finally:
        _reap(process)

    exits = _exit_events(plan_dir)
    assert len(exits) == 1, (
        f"expected exactly one exit event, got {exits!r}"
    )
    assert exits[0]["pid"] == process.pid
    assert exits[0]["terminal"] is True
    assert process.pid not in ledger.live_records(plan_dir), (
        "a pid that is gone must not stay in the live set — the next "
        "sweep would go looking for a process that no longer exists"
    )


def test_owner_registered_handle_still_records_the_exit(tmp_path, monkeypatch):
    """The VP sub-agent's own handle is the one that closes the pid.

    The VP sub-agent, the rebuild agent and the service-restart agent
    each register their own handle *before* they dispatch. The
    dispatch finds that handle and must not take it away — the
    watchdog needs it — but the process still belongs to the plan and
    still has to be on the ledger, and its exit still has to be
    recorded when the owner cleans up.
    """
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    process = _spawn_holder()
    tool = _StandInTool(process, str(tmp_path))
    try:
        owner_handle = sub_agent_registry.register_sub_agent(
            PLAN, "VP-1", 0, scoped_tool=tool,
        )
        registry, handle = coding_tool._register_dispatch(tool, PLAN)

        assert (registry, handle) == (None, None), (
            "the dispatch must leave the owner's handle alone — the "
            "watchdog reaches the subprocess through it"
        )
        assert owner_handle.pid == process.pid
        assert process.pid in ledger.live_records(plan_dir), (
            "the process the owner is running has to be on the ledger "
            "even though the dispatch did not create the handle"
        )

        sub_agent_registry.unregister(PLAN, owner_handle)
    finally:
        _reap(process)

    exits = _exit_events(plan_dir)
    assert len(exits) == 1, (
        f"the owner's cleanup is an exit path too; got {exits!r}"
    )
    assert exits[0]["pid"] == process.pid
    assert process.pid not in ledger.live_records(plan_dir)


def test_dispatch_that_finishes_writes_the_exit_event(tmp_path, monkeypatch):
    """A dispatch that ends on its own closes its ledger entry.

    The clean end is the ordinary case, and it is the one the ledger
    has to get right: a spawn that is never closed stays in the live
    set forever, and the next sweep goes looking for a process that
    finished minutes ago.
    """
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    _install_fake_claude(tmp_path, monkeypatch, _FINISHING_CLAUDE)
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)

    tool = ClaudeCodingTool()
    tool._run_claude_interactive("hello", idle_timeout=30, total_timeout=30)

    spawns = _spawn_events(plan_dir)
    exits = _exit_events(plan_dir)
    assert len(spawns) == 1, f"expected one spawn, got {spawns!r}"
    assert len(exits) == 1, (
        "the dispatch ended but the ledger still holds it open: %r"
        % (exits,)
    )
    assert exits[0]["pid"] == spawns[0]["pid"]
    assert spawns[0]["pid"] not in ledger.live_records(plan_dir), (
        "the pid is gone from the kernel but still on the ledger"
    )


def test_timeout_kill_writes_the_exit_event(tmp_path, monkeypatch):
    """A sub-agent killed for being silent still closes its ledger entry.

    The timeout kill is the same exit as any other — the dispatch is
    over and the process is gone — so the ledger has to hear about it
    too. A killed sub-agent that stays on the ledger is exactly the
    process a later sweep would try to kill a second time.

    The kill comes from the dispatch's total-timeout watcher, which
    ``SIGTERM``s the child: closing the read end from another thread
    does not wake a blocked read, so the watcher that signals the
    process is the one that ends the wait.
    """
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    # A gate that is never opened: the fake CLI stays silent, so the
    # dispatch's timeout watcher fires and kills it.
    _install_fake_claude(
        tmp_path, monkeypatch,
        _GATED_CLAUDE.format(gate=tmp_path / "never-opened"),
    )
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)

    tool = ClaudeCodingTool()
    with pytest.raises(TimeoutError):
        tool._run_claude_interactive("hello", idle_timeout=2, total_timeout=10)

    spawns = _spawn_events(plan_dir)
    exits = _exit_events(plan_dir)
    assert len(spawns) == 1, f"expected one spawn, got {spawns!r}"
    assert len(exits) == 1, (
        "the timeout kill is an exit like any other, but the ledger was "
        "not told: %r" % (exits,)
    )
    assert exits[0]["pid"] == spawns[0]["pid"]
    assert spawns[0]["pid"] not in ledger.live_records(plan_dir)


def test_watchdog_kill_writes_the_exit_event(tmp_path, monkeypatch):
    """A sub-agent killed from outside still closes its ledger entry.

    The watchdog kills a wedged sub-agent from another thread, not
    from inside the dispatch. The dispatch then finds its child gone,
    leaves the read loop, and closes the ledger entry on the way out —
    the same exit as every other one.
    """
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    _install_fake_claude(
        tmp_path, monkeypatch,
        _GATED_CLAUDE.format(gate=tmp_path / "never-opened"),
    )
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)

    tool = ClaudeCodingTool()
    failures = []

    def _run() -> None:
        try:
            tool._run_claude_interactive(
                "hello", idle_timeout=30, total_timeout=60,
            )
        except BaseException as exc:  # pragma: no cover - the kill may raise
            failures.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        record = _wait_for_spawn(plan_dir)
        # The watchdog's move: the whole process group, so the child's
        # own children go with it.
        os.killpg(record.pgid, signal.SIGKILL)
    finally:
        thread.join(timeout=60)

    assert not thread.is_alive(), "the dispatch did not come back"
    spawns = _spawn_events(plan_dir)
    exits = _exit_events(plan_dir)
    assert len(spawns) == 1, f"expected one spawn, got {spawns!r}"
    assert len(exits) == 1, (
        "the watchdog kill is an exit like any other, but the ledger was "
        "not told: %r" % (exits,)
    )
    assert exits[0]["pid"] == spawns[0]["pid"]
    assert spawns[0]["pid"] not in ledger.live_records(plan_dir)


# ---------------------------------------------------------------------------
# 3. A spawn that failed leaves nothing behind.
# ---------------------------------------------------------------------------
def test_popen_failure_writes_no_record(tmp_path, monkeypatch):
    """A ``Popen`` that raised never produced a process to record."""
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    _install_fake_claude(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)

    real_popen = subprocess.Popen

    def _boom(cmd, *args, **kwargs):
        if any("claude" in str(part) for part in (cmd or ())):
            raise OSError(2, "No such file or directory", "claude")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(coding_tool.subprocess, "Popen", _boom)

    tool = ClaudeCodingTool()
    with pytest.raises(OSError):
        tool._run_claude_interactive("hello", idle_timeout=30, total_timeout=30)

    assert ledger.read_events(plan_dir) == [], (
        "a failed spawn wrote %r — there is no process behind those "
        "events" % (ledger.read_events(plan_dir),)
    )


# ---------------------------------------------------------------------------
# 4. The two views agree.
# ---------------------------------------------------------------------------
def test_registry_and_ledger_stay_in_sync(tmp_path, monkeypatch):
    """The handle in memory and the record on disk describe one process."""
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    process = _spawn_holder()
    tool = _StandInTool(process, str(tmp_path))
    try:
        registry, handle = coding_tool._register_dispatch(tool, PLAN)

        handles = sub_agent_registry.all_handles(PLAN)
        assert len(handles) == 1, (
            f"the registry holds {len(handles)} handles, not the one the "
            "dispatch registered"
        )
        assert handles[0] is handle

        live = ledger.live_records(plan_dir)
        assert set(live) == {process.pid}, (
            f"the ledger's live set is {sorted(live)}, not the one pid "
            "the dispatch started"
        )
        record = live[process.pid]
        assert record.pgid == handle.pgid
        assert record.create_time == handle.create_time
        assert record.cmdline == handle.cmdline

        coding_tool._unregister_dispatch(registry, PLAN, handle)
    finally:
        _reap(process)

    assert sub_agent_registry.all_handles(PLAN) == []
    assert ledger.live_records(plan_dir) == {}, (
        "the handle is gone and the pid is still on record — one of the "
        "two views did not get the news"
    )


# ---------------------------------------------------------------------------
# 5. Registering the same tool twice records one spawn.
# ---------------------------------------------------------------------------
def test_repeated_registration_writes_one_spawn(tmp_path, monkeypatch):
    """The same process is not on the ledger twice."""
    plan_dir = _use_plan_dir(tmp_path, monkeypatch)
    process = _spawn_holder()
    tool = _StandInTool(process, str(tmp_path))
    try:
        coding_tool._register_dispatch(tool, PLAN)
        coding_tool._register_dispatch(tool, PLAN)
    finally:
        _reap(process)

    spawns = _spawn_events(plan_dir)
    assert len(spawns) == 1, (
        f"the same process was recorded {len(spawns)} times: "
        f"{spawns!r}"
    )
    assert spawns[0]["pid"] == process.pid


# ---------------------------------------------------------------------------
# 6. The one ledger a plan has.
# ---------------------------------------------------------------------------
#
# Everything above is about *what* is written.  This section is about
# *where* — and the where is a contract of its own: a plan has one
# ledger, and every process that works for that plan writes into it.
#
# The executor runs with its own working directory (``backend/``) and the
# plans root it resolves can be relative, so a process below the
# executor that rebuilds the plan's directory from the environment can
# name a different directory than the one the backend reads.  Two
# ledgers for one plan is not a cosmetic problem: the backend's sweep
# cannot see the sub-agents, so the plan's live set never reaches zero
# and the order the tree was collected in cannot be read off the ledger
# at all.
#
# So the executor hands its descendants the *absolute* path of the plan
# it belongs to, and the registry prefers it over the plans root.


def test_the_ledger_follows_the_plan_directory_the_executor_named(
    tmp_path, monkeypatch
):
    """``PDT_PLAN_DIR`` wins over the plans root.

    The directory the executor names is the plan's own directory — the
    one the backend sweeps.  When it is there, it is the ledger's home;
    the plans root is only the fallback for a process that was not told
    where the plan lives.
    """
    plan_id = "20261031-plan-dir-handoff"
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    backend_plan_dir = plans_root / plan_id
    backend_plan_dir.mkdir()
    executor_plan_dir = tmp_path / "executor-plans" / plan_id
    executor_plan_dir.mkdir(parents=True)

    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))
    monkeypatch.setenv("PDT_PLAN_DIR", str(executor_plan_dir))

    process = _spawn_holder()
    tool = _StandInTool(process, str(tmp_path))
    try:
        coding_tool._register_dispatch(tool, plan_id)
    finally:
        _reap(process)

    assert ledger.live_records(executor_plan_dir), (
        "the spawn was not written into the plan directory the executor "
        "named — the backend's sweep will not see this sub-agent"
    )
    assert not ledger.read_events(backend_plan_dir), (
        "the spawn was written into the plans root as well — one plan, "
        "two ledgers"
    )


def test_a_plan_directory_that_names_another_plan_is_ignored(
    tmp_path, monkeypatch
):
    """``PDT_PLAN_DIR`` only speaks for the plan it is named after.

    The environment is inherited by everything the executor starts, so
    the value is only trustworthy for the plan the executor belongs to.
    A directory that is named after a *different* plan says nothing about
    this one, and following it would put this plan's records in another
    plan's directory.
    """
    plan_id = "20261031-plan-dir-handoff"
    other_plan_id = "20261031-somebody-else"
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    plan_dir = plans_root / plan_id
    plan_dir.mkdir()
    other_plan_dir = plans_root / other_plan_id
    other_plan_dir.mkdir()

    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))
    monkeypatch.setenv("PDT_PLAN_DIR", str(other_plan_dir))

    process = _spawn_holder()
    tool = _StandInTool(process, str(tmp_path))
    try:
        coding_tool._register_dispatch(tool, plan_id)
    finally:
        _reap(process)

    assert ledger.live_records(plan_dir), (
        "the spawn was not written into the plan's own directory"
    )
    assert not ledger.read_events(other_plan_dir), (
        "the spawn was written into another plan's directory"
    )


def test_the_executor_hands_its_plan_directory_to_its_descendants(
    tmp_path, monkeypatch
):
    """``cli.py`` exports the plan's absolute directory.

    The executor knows which plan it is running — the plan whose
    ``tasks.json`` it was handed — and it is the only process in the
    tree that knows it for certain.  It says so in the environment, so
    that everything it starts writes its ledger records into the same
    directory the backend reads, and the plan has one ledger rather
    than one per working directory.
    """
    import cli as cli_module

    plan_dir = tmp_path / "plans" / "20261031-plan-dir-handoff"
    plan_dir.mkdir(parents=True)
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "plan dir handoff", "tasks": []}),
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    monkeypatch.delenv("PDT_PLAN_DIR", raising=False)
    monkeypatch.setattr(cli_module, "autonomous_coding", lambda *a, **k: 0)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cli.py",
            "--recover",
            "-w", str(project_dir),
            "--tasks-file", str(tasks_file),
        ],
    )

    cli_module.main()

    assert os.environ.get("PDT_PLAN_DIR") == str(plan_dir), (
        "the executor did not hand its plan directory to the processes it "
        "starts — a descendant rebuilds it from the environment and lands "
        "in a different tree"
    )


# ---------------------------------------------------------------------------
# 7. The vocabulary of a record the sweep collected.
# ---------------------------------------------------------------------------
#
# A plan's ledger is read to answer one question for the operator: "did
# the sweep get this plan's tree?".  A record the sweep signalled leaves
# a ``killed`` event.  A record whose process had already ended when the
# sweep reached it left an ``exit`` event — and that says the process
# ended, not that the sweep accounted for it.  With both words in one
# ledger, "the plan's processes were collected, children first" cannot
# be read off it, and that is exactly what acceptance N4 asks for.
#
# So a record the sweep reaches and finds already gone is *collected*:
# it leaves the live set with the same named event every other
# collected record gets, and the detail says that nothing was signalled
# — because nothing was: there was nothing left to signal.


def _a_process_that_has_ended():
    """A process that ran and is gone — the kernel answers for its pid."""
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child.wait(timeout=30)
    return child


def _record_a_finished_process(plan_dir, plan_id, role, pid):
    """Write the spawn record of a process that has already ended."""
    ledger.record_spawn(
        plan_dir,
        ledger.ProcessRecord(
            plan_id=plan_id,
            role=role,
            pid=pid,
            pgid=pid,
            create_time=time.time(),
            cmdline=["python", "-c", "pass"],
            cwd=str(plan_dir),
            owner_pid=os.getpid(),
            owner_role="backend",
        ),
    )


def _collected_events(plan_dir):
    return [
        event for event in ledger.read_events(plan_dir)
        if event.get("event") == process_reaper.EVENT_KILLED
    ]


def test_a_record_whose_process_had_already_ended_is_closed_as_collected(
    tmp_path,
):
    """A record the sweep reached, whose process had already ended.

    Nothing was signalled — there was nothing left to signal — but the
    sweep did account for the record, and the ledger has to say so with
    the named event every other collected record gets.  Without it the
    plan's live set reaches zero and the reader still cannot tell which
    of the plan's processes the sweep took responsibility for.
    """
    plan_dir = tmp_path / "plans" / "20261031-collected-record"
    plan_dir.mkdir(parents=True)
    plan_id = plan_dir.name

    child = _a_process_that_has_ended()
    _record_a_finished_process(plan_dir, plan_id, "subagent", child.pid)

    outcome = process_reaper.reap_plan(plan_dir, plan_id, reason="stop")

    # Nothing was signalled: there was nothing left to signal.
    assert outcome.reaped == []
    assert outcome.failed == []
    assert outcome.unknown == []

    # The record left the live set ...
    assert ledger.live_records(plan_dir) == {}

    # ... with the named event every collected record gets, carrying the
    # role it was recorded under, and a detail that says no signal was
    # sent.
    collected = _collected_events(plan_dir)
    assert [event.get("pid") for event in collected] == [child.pid], (
        "the sweep accounted for the record without naming it: %r"
        % (ledger.read_events(plan_dir),)
    )
    assert collected[0].get("role") == "subagent"
    assert "nothing was signalled" in (collected[0].get("detail") or "")


def test_the_collected_records_are_closed_child_first(tmp_path):
    """The collected records are closed in the order the sweep reached them.

    A child before its parent: the same order the sweep signals in, and
    the order the plan's own tree was collected in.  The ledger is where
    that order is read off, so it has to be the order the events were
    written in.
    """
    plan_dir = tmp_path / "plans" / "20261031-collected-order"
    plan_dir.mkdir(parents=True)
    plan_id = plan_dir.name

    subagent = _a_process_that_has_ended()
    executor = _a_process_that_has_ended()
    # Recorded the other way round on purpose: the order of the records
    # is not the order of the events.
    _record_a_finished_process(plan_dir, plan_id, "executor", executor.pid)
    _record_a_finished_process(plan_dir, plan_id, "subagent", subagent.pid)

    outcome = process_reaper.reap_plan(plan_dir, plan_id, reason="stop")

    assert outcome.reaped == []
    assert [event.get("role") for event in _collected_events(plan_dir)] == [
        "subagent", "executor",
    ], (
        "the collected records are not in child-first order: %r"
        % (_collected_events(plan_dir),)
    )
    assert ledger.live_records(plan_dir) == {}
