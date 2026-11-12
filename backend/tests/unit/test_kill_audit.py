"""The kill audit module, tested on its own.

``kill_audit`` is the single recorder every process that sends signals
can install.  Its contract is small, and each part of it is a way for
the evidence to go missing:

* where the records go (the environment, or the platform temp dir);
* what one record contains (the target, the signal, the outcome);
* the cap, and the promise that a record that cannot be written never
  costs the signal it describes.

The recorder installs its hook at import time.  That is deliberate --
the hook has to be in place before the first signal -- so these tests
talk to the pure record functions and to the directory resolution
rather than re-installing it.

The second half of the contract is the **receiving** side.  A ledger
that only records what this process *sent* cannot answer "who sent the
signal that killed me" -- the answer to that question is on the other
side of the signal, and a conclusion drawn from the sending side alone
is a conclusion drawn from an absence.  So the audit can also record
the signals this process receives, and the promise it makes about them
is narrow: it records, it puts the default disposition back, and it
sends the same signal to **itself** -- the process still dies of it,
exactly as it would have without the audit, and no signal path to any
other process is created.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import kill_audit  # noqa: E402


#: The fields a **send** record carries.  The set is exact: the service
#: manager's own audit writes the same fields, and the two ledgers are
#: read together as one account of the signals a run sent -- so a
#: record that carried more than the contract names could not be
#: compared with the records written next to it.
SEND_RECORD_FIELDS = {
    "kind", "target", "signal", "outcome", "pid", "ts", "stack",
}

#: The **outcome** record carries the same fields plus the errno the
#: kernel answered with.  It is the second record of one signal
#: attempt: the hook records that the signal was about to be sent, and
#: the sender writes down what became of it.
SEND_OUTCOME_RECORD_FIELDS = SEND_RECORD_FIELDS | {"errno"}


def _records(directory: Path) -> list:
    """Every record under *directory*, oldest first."""
    records = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _dead_pid() -> int:
    """A pid that is certainly not running any more."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


def test_dir_honours_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path / "audit"))
    assert kill_audit.kill_audit_dir() == tmp_path / "audit"


def test_dir_falls_back_to_the_platform_temp_dir(monkeypatch):
    monkeypatch.delenv(kill_audit.KILL_AUDIT_DIR_ENV, raising=False)
    assert kill_audit.kill_audit_dir() == (
        Path(tempfile.gettempdir()) / kill_audit.KILL_AUDIT_DIR_NAME
    )


def test_write_kill_audit_writes_one_json_line(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("kill", 1234, 15)

    records = _records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert set(record) == SEND_RECORD_FIELDS
    assert record["kind"] == "kill"
    assert record["target"] == 1234
    assert record["signal"] == 15
    assert record["outcome"] == kill_audit.OUTCOME_ATTEMPTED
    assert record["pid"] == os.getpid()
    assert record["stack"], "a record without a caller names no sender"


def test_write_kill_outcome_carries_outcome_and_errno(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_outcome(
        "killpg", 4321, 9, kill_audit.OUTCOME_REFUSED, errno=1,
    )

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_OUTCOME_RECORD_FIELDS
    assert record["kind"] == "killpg"
    assert record["target"] == 4321
    assert record["signal"] == 9
    assert record["outcome"] == kill_audit.OUTCOME_REFUSED
    assert record["errno"] == 1


def test_the_cap_stops_writing_after_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(kill_audit, "KILL_AUDIT_MAX_RECORDS", 2)
    monkeypatch.setattr(kill_audit, "_kill_audit_count", 0)

    for index in range(5):
        kill_audit.write_kill_audit("kill", index, 15)

    assert len(_records(tmp_path)) == 2


def test_an_unwritable_directory_does_not_raise(tmp_path, monkeypatch):
    """A record nobody can write must not cost the signal it describes."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(
        kill_audit.KILL_AUDIT_DIR_ENV, str(blocker / "audit"),
    )

    # The path's parent is a regular file: mkdir and open both fail.
    kill_audit.write_kill_audit("kill", 1, 15)
    kill_audit.write_kill_outcome("kill", 1, 15, kill_audit.OUTCOME_SENT)


# ---------------------------------------------------------------------------
# The fields a reader queries
# ---------------------------------------------------------------------------


def test_the_sent_record_carries_the_fields_a_reader_queries(
    tmp_path, monkeypatch,
):
    """A record whose fields have to be *guessed* cannot be queried.

    The set of fields is exact: which call was made (``kind``), what
    was aimed at (``target``), with which signal (``signal``), what
    became of it (``outcome``), who sent it (``pid``), when (``ts``) and
    from where (``stack``) -- and nothing else.  The service manager's
    own audit writes the same fields, and the two ledgers are read
    together, so a record that carried more than the contract names
    could not be compared with the records written next to it.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("killpg", 68639, 15)

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_RECORD_FIELDS
    assert record["kind"] == "killpg"
    assert record["target"] == 68639
    assert record["signal"] == 15
    assert record["outcome"] == kill_audit.OUTCOME_ATTEMPTED
    assert record["pid"] == os.getpid()
    assert record["ts"]
    assert record["stack"]


def test_a_single_pid_send_names_the_pid_and_not_a_group(
    tmp_path, monkeypatch,
):
    """A pid and a group are different things, and the record says which.

    ``kind`` is what tells them apart: ``kill`` names one pid and
    ``killpg`` names a whole group.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    dead = _dead_pid()
    kill_audit.write_kill_audit("kill", dead, 15)

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_RECORD_FIELDS
    assert record["kind"] == "kill"
    assert record["target"] == dead


def test_the_old_field_names_are_still_there(tmp_path, monkeypatch):
    """The names the audit has always written are the ones it writes.

    ``kind`` says which call was made, ``target`` says what was aimed
    at, ``outcome`` says what became of it and ``pid`` says who sent
    it -- the readers that query these names keep working.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("kill", 18618, 15)

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_RECORD_FIELDS
    assert record["kind"] == "kill"
    assert record["target"] == 18618
    assert record["outcome"] == kill_audit.OUTCOME_ATTEMPTED
    assert record["pid"] == os.getpid()


def test_the_record_keeps_the_shape_of_the_older_ledgers(
    tmp_path, monkeypatch,
):
    """The record's shape is the contract, and the contract is exact.

    The older ledger wrote ``{ts, event, args, pid, ppid, pgid,
    stack}``; this one writes ``{kind, target, signal, outcome, pid,
    ts, stack}``.  A record that carried both would be two contracts
    in one line -- and the reader that compares this ledger with the
    service manager's own audit could not tell which fields the shape
    is made of.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("kill", 1234, 15)

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_RECORD_FIELDS
    assert record["target"] == 1234
    assert record["signal"] == 15
    assert record["pid"] == os.getpid()


def test_the_outcome_record_keeps_that_shape_too(tmp_path, monkeypatch):
    """The outcome record is the same shape, plus the errno.

    The hook records that a signal was *about* to be sent; the sender
    writes what became of it next to it.  The errno the kernel
    answered with is the only thing the second record adds -- and it is
    added even when the kernel did not answer with one.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_outcome("kill", 1234, 15, kill_audit.OUTCOME_SENT)

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_OUTCOME_RECORD_FIELDS
    assert record["kind"] == "kill"
    assert record["target"] == 1234
    assert record["signal"] == 15
    assert record["outcome"] == kill_audit.OUTCOME_SENT
    assert record["errno"] is None
    assert record["pid"] == os.getpid()
    assert record["ts"]
    assert record["stack"]


def test_the_target_pgid_is_the_group_the_target_is_in(
    tmp_path, monkeypatch,
):
    """The record says whether the whole group was in reach.

    A signal to one pid and a signal to a whole group are different
    acts, and ``kind`` is what tells them apart: ``kill`` names one
    pid, ``killpg`` names the group -- and the group's id is the
    target.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("kill", os.getpid(), 15)
    kill_audit.write_kill_audit("killpg", os.getpgrp(), 15)

    single, group = _records(tmp_path)
    assert single["kind"] == "kill"
    assert single["target"] == os.getpid()
    assert group["kind"] == "killpg"
    assert group["target"] == os.getpgrp()


def test_a_target_that_is_gone_has_no_group_to_name(tmp_path, monkeypatch):
    """A target that is already dead is still named by its pid.

    The record says what the signal was *aimed at*; it never guesses
    what was found -- and it carries no field that could hold such a
    guess.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    dead = _dead_pid()
    kill_audit.write_kill_audit("kill", dead, 15)

    record = _records(tmp_path)[-1]
    assert set(record) == SEND_RECORD_FIELDS
    assert record["kind"] == "kill"
    assert record["target"] == dead


def test_sender_is_self_is_true_only_for_a_signal_to_this_process(
    tmp_path, monkeypatch,
):
    """The record names the sender, and the sender is this process.

    A signal to this process is a signal whose target is the sender
    itself -- and the record says so with the two fields it has:
    ``pid`` names the sender and ``target`` names what was aimed at.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    dead = _dead_pid()
    kill_audit.write_kill_audit("kill", os.getpid(), 15)
    kill_audit.write_kill_audit("kill", dead, 15)
    kill_audit.write_kill_audit("killpg", os.getpgrp(), 15)

    records = _records(tmp_path)
    assert all(r["pid"] == os.getpid() for r in records)
    assert records[0]["target"] == records[0]["pid"], (
        "a signal to this process is not recorded as one"
    )
    assert records[1]["target"] != records[1]["pid"]
    assert records[2]["target"] != records[2]["pid"]


def test_the_file_is_named_after_the_process_that_wrote_it(
    tmp_path, monkeypatch,
):
    """One file per process, and the record names that same process.

    Two processes never write into the same file, so their records
    cannot interleave -- and a reader that has the file in hand knows
    which process wrote it, from the file's name and from the record.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    kill_audit.write_kill_audit("kill", 1234, 15)

    files = sorted(tmp_path.glob("kill-audit-*.jsonl"))
    assert len(files) == 1, f"the audit wrote {len(files)} files"
    assert files[0].name == f"kill-audit-{os.getpid()}.jsonl"
    record = _records(tmp_path)[-1]
    assert record["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# The receiving side
# ---------------------------------------------------------------------------


#: The child that installs the receiving side and then signals itself.
#: It sends the signal to **its own** pid and to nothing else.
_RECEIVER_CHILD = """
import os
import signal
import sys

sys.path.insert(0, {backend_dir!r})
import kill_audit

kill_audit.install_signal_receiver_audit()
os.kill(os.getpid(), signal.SIGTERM)
"""


def _spawn_receiver(tmp_path: Path, *, through_bash: bool = False):
    """Run a child that records the signal it receives and dies of it."""
    audit_dir = tmp_path / "audit"
    script = tmp_path / "receiver_child.py"
    script.write_text(
        _RECEIVER_CHILD.format(backend_dir=str(BACKEND_DIR)),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env[kill_audit.KILL_AUDIT_DIR_ENV] = str(audit_dir)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    command = [sys.executable, str(script)]
    if through_bash:
        # ``bash -c`` execs a lone command, so the shell would *become*
        # the child and die of the signal itself.  The trailing command
        # keeps the shell in the picture, and then the shell's exit
        # status is the number a reader of the gate log sees: 143.
        command = ["bash", "-c", " ".join(command) + "; exit $?"]
    completed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=60,
    )
    return completed, audit_dir


def test_the_received_record_names_the_signal_and_the_receiver(tmp_path):
    """The receiving side records what the sending side cannot: which
    signal arrived, who received it, and in which second."""
    completed, audit_dir = _spawn_receiver(tmp_path)

    assert completed.returncode == -signal.SIGTERM, completed.stderr
    records = _records(audit_dir)
    received = [r for r in records if r.get("event") == "signal.received"]
    assert len(received) == 1
    record = received[0]
    assert record["signal"] == signal.SIGTERM
    assert record["signal_name"] == "SIGTERM"
    assert record["receiver_pid"] > 0
    assert record["receiver_pgid"] > 0
    assert record["received_at_second"]
    assert isinstance(record["group_members"], list)
    if not record.get("group_census_error"):
        pids = [member["pid"] for member in record["group_members"]]
        assert record["receiver_pid"] in pids


def test_the_process_still_dies_of_the_signal_it_received(tmp_path):
    """143 is the shell's way of saying "died of signal 15".

    The audit must not change that: the process has to end the way it
    would have ended without the record, or the record changes the very
    thing it is recording.
    """
    completed, _audit_dir = _spawn_receiver(tmp_path, through_bash=True)

    assert completed.returncode == 143, completed.stderr


def test_the_receiver_only_ever_signals_itself():
    """No new path to a stranger's process or process group.

    The receiving side exists to write down a signal; it must not be
    able to send one to anything it did not create.
    """
    source = (BACKEND_DIR / "kill_audit.py").read_text(encoding="utf-8")
    assert "os.killpg(" not in source
    for line in source.splitlines():
        if "os.kill(" not in line:
            continue
        assert "os.kill(os.getpid()" in line, line


# ---------------------------------------------------------------------------
# The receiving side is effective by default
# ---------------------------------------------------------------------------


#: The child that only **imports** the module and then signals itself.
#: Nobody asks for the receiving side: the default install path has to
#: make it effective, or the forensics run has nothing to reconcile.
_DEFAULT_INSTALL_CHILD = """
import os
import signal
import sys

sys.path.insert(0, {backend_dir!r})
import kill_audit

os.kill(os.getpid(), signal.SIGTERM)
"""


def _spawn_default_install_child(tmp_path: Path):
    """Run a child that imports the audit and signals itself."""
    audit_dir = tmp_path / "audit"
    script = tmp_path / "default_child.py"
    script.write_text(
        _DEFAULT_INSTALL_CHILD.format(backend_dir=str(BACKEND_DIR)),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env[kill_audit.KILL_AUDIT_DIR_ENV] = str(audit_dir)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    completed = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True,
        timeout=60,
    )
    return completed, audit_dir


def test_the_default_install_path_records_what_the_process_receives(tmp_path):
    """Importing the module is enough.

    The receiving side has to be effective without anybody asking for
    it: a forensics run that installs nothing would leave the ledger
    without a record of the signal that ended the process.
    """
    completed, audit_dir = _spawn_default_install_child(tmp_path)

    assert completed.returncode == -signal.SIGTERM, completed.stderr
    received = [
        r for r in _records(audit_dir)
        if r.get("event") == kill_audit.EVENT_RECEIVED
    ]
    assert len(received) == 1
    record = received[0]
    assert record["signal"] == signal.SIGTERM
    assert record["signal_name"] == "SIGTERM"
    assert record["receiver_pid"] > 0
    assert record["receiver_pgid"] > 0
    assert record["received_at_second"]
    assert isinstance(record["group_members"], list)


def test_the_received_record_does_not_ask_the_host_for_a_census(
    tmp_path, monkeypatch,
):
    """The handler must not fork.

    A signal that arrives while the process is forking leaves the fork
    half-done, and the process hangs instead of dying.  The census is
    sampled in the background; the handler only reads the cache.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    def explode(*args, **kwargs):
        raise AssertionError("the receiving side asked the host for a census")

    monkeypatch.setattr(kill_audit, "_group_census", explode)

    kill_audit.write_signal_received(signal.SIGTERM)

    received = [
        r for r in _records(tmp_path)
        if r.get("event") == kill_audit.EVENT_RECEIVED
    ]
    assert len(received) == 1
    assert received[0]["signal_name"] == "SIGTERM"


def test_the_receiving_side_does_not_wait_for_the_sending_side_lock(
    tmp_path, monkeypatch,
):
    """A signal that interrupts this process while it is writing the
    ledger must not deadlock on the lock that write already holds.

    The receiving side has its own lock: the sending side's lock is
    held by the interrupted thread, and a process that waits for it can
    never die.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))

    held = threading.Event()
    release = threading.Event()

    def hold():
        with kill_audit._kill_audit_lock:
            held.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(timeout=30), "the holder never took the lock"
        written = threading.Event()

        def write():
            kill_audit.write_signal_received(signal.SIGTERM)
            written.set()

        writer = threading.Thread(target=write)
        writer.start()
        assert written.wait(timeout=30), (
            "the receiving side waited for the sending side's lock"
        )
    finally:
        release.set()
        holder.join(timeout=30)


#: The child that owns SIGTERM before the audit is imported.
_CHAINED_CHILD = """
import os
import signal
import sys

sys.path.insert(0, {backend_dir!r})


def other(signum, frame):
    with open({marker!r}, "w", encoding="utf-8") as handle:
        handle.write("called")


signal.signal(signal.SIGTERM, other)

import kill_audit

os.kill(os.getpid(), signal.SIGTERM)
"""


def test_an_existing_handler_is_chained_and_not_replaced(tmp_path):
    """The audit must never take a signal away from whoever owns it.

    Replacing the server's graceful-shutdown handler would turn every
    shutdown into a 143; so the audit records the signal and then hands
    control back to the handler that was already there.
    """
    audit_dir = tmp_path / "audit"
    marker = tmp_path / "other-handler-ran"
    script = tmp_path / "chained_child.py"
    script.write_text(
        _CHAINED_CHILD.format(
            backend_dir=str(BACKEND_DIR), marker=str(marker),
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env[kill_audit.KILL_AUDIT_DIR_ENV] = str(audit_dir)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    completed = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.is_file(), "the handler that owned SIGTERM never ran"
    received = [
        r for r in _records(audit_dir)
        if r.get("event") == kill_audit.EVENT_RECEIVED
    ]
    assert len(received) == 1
    assert received[0]["signal_name"] == "SIGTERM"


# ---------------------------------------------------------------------------
# The census does not fork
# ---------------------------------------------------------------------------


def test_the_census_reads_the_process_table_without_asking_the_host(
    monkeypatch,
):
    """A census that forks is a census that can leave a fork half-done.

    The process table can be read directly, in a millisecond, without
    asking the host for anything -- so the census does that, and the
    host is only asked when the table cannot be read at all.
    """
    pytest.importorskip("psutil")

    def explode(*args, **kwargs):
        raise AssertionError("the census asked the host for a census")

    monkeypatch.setattr(kill_audit.subprocess, "run", explode)

    members, error, backend = kill_audit._group_census(os.getpgrp())

    assert error == ""
    assert backend == "psutil"
    assert os.getpid() in [member["pid"] for member in members]


def test_the_census_names_the_members_of_the_group_and_nobody_else():
    """A census that lists a process that is in no group invents a
    member -- and an invented member is worse than no member.

    ``getpgid(0)`` answers with the *caller's* group, so pid 0 has to
    be left out of a membership list.
    """
    pytest.importorskip("psutil")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        members, error, backend = kill_audit._group_census(os.getpgrp())

        pids = [member["pid"] for member in members]
        assert error == ""
        assert os.getpid() in pids
        assert child.pid in pids
        assert 0 not in pids, "pid 0 is not a member of this group"
    finally:
        child.kill()
        child.wait(timeout=30)


def _load_reconciliation_script():
    """The tool that reads the ledgers, loaded from its file."""
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts" / "kill_signal_reconciliation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "kill_signal_reconciliation_under_test", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_event_the_audit_writes_is_one_the_reconciliation_tool_looks_for():
    """The tool that reads the ledger searches for the event name the
    audit writes.

    A record whose event name the reader does not look for is a record
    the reader cannot find -- and a reader that cannot find it answers
    "I found no sender" while looking rigorous.
    """
    recon = _load_reconciliation_script()

    assert kill_audit.EVENT_SENT in recon.SENT_EVENTS, (
        "the reconciliation tool does not look for the event name the "
        "audit writes (%r)" % (kill_audit.EVENT_SENT,)
    )

    # The send records this module writes carry no event name: they
    # name themselves by ``kind``, because the field set is exact.  The
    # tool still finds them -- a record the reader cannot find answers
    # "I found no sender" while looking rigorous.
    assert recon.is_sent({
        "kind": "kill", "target": 1234, "signal": 15,
        "outcome": kill_audit.OUTCOME_ATTEMPTED, "pid": os.getpid(),
        "ts": "2026-11-09T00:00:00", "stack": "kill_audit.py:1 f",
    }), "the tool cannot find a send record that names itself by kind"


def test_the_received_record_says_where_the_snapshot_came_from(
    tmp_path, monkeypatch,
):
    """A snapshot nobody can trace is a snapshot nobody can check.

    The membership in the record is evidence about the moment the
    signal arrived, and the record has to say where that membership
    came from -- otherwise a reader cannot tell a census from a
    guess.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        kill_audit, "_group_census",
        lambda pgid: ([{
            "pid": os.getpid(), "ppid": os.getppid(),
            "pgid": pgid, "command": "python",
        }], "", "psutil"),
    )

    kill_audit._refresh_census()
    kill_audit.write_signal_received(signal.SIGTERM)

    record = [
        r for r in _records(tmp_path)
        if r.get("event") == kill_audit.EVENT_RECEIVED
    ][0]
    assert record["group_census_backend"] == "psutil"
    assert record["group_members_complete"] is True


def test_a_census_that_failed_is_not_recorded_as_a_complete_group(
    tmp_path, monkeypatch,
):
    """A census that could not be taken is not a census.

    "I saw nobody" and "I could not look" are two different facts, and
    only one of them is evidence of an empty group.  When the census
    fails the membership is simply unknown -- so the record has to say
    the membership is **incomplete**, or a reader will read the empty
    member list as "the group was empty" and draw a conclusion out of
    an absence.
    """
    monkeypatch.setenv(kill_audit.KILL_AUDIT_DIR_ENV, str(tmp_path))
    # The census cache is module-level state shared with the background
    # sampler: pin it here and let monkeypatch put it back afterwards,
    # so this test cannot pollute the ones that follow it.
    monkeypatch.setattr(kill_audit, "_census_members", [])
    monkeypatch.setattr(kill_audit, "_census_error", "")
    monkeypatch.setattr(kill_audit, "_census_taken_at", None)
    monkeypatch.setattr(kill_audit, "_census_backend", "")
    monkeypatch.setattr(
        kill_audit, "_group_census",
        lambda pgid: (
            [],
            "the census file could not be read: [Errno 2] No such file "
            "or directory",
            "file",
        ),
    )

    kill_audit._refresh_census()
    kill_audit.write_signal_received(signal.SIGTERM)

    record = [
        r for r in _records(tmp_path)
        if r.get("event") == kill_audit.EVENT_RECEIVED
    ][0]
    assert record["group_census_error"], (
        "the failed census did not say why it failed (%r)" % (record,)
    )
    assert record["group_members_complete"] is False, (
        "a census that could not be taken was recorded as a complete "
        "group (group_members_complete=%r)" % (
            record["group_members_complete"],
        )
    )


def test_the_census_falls_back_to_the_host_when_the_table_cannot_be_read(
    monkeypatch,
):
    """Without the process table the census asks the host -- and says so.

    The record has to be able to say where the membership came from: a
    membership list nobody can trace is a list that cannot be checked.
    """
    monkeypatch.setattr(kill_audit, "_load_psutil", lambda: None)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0,
            "  101   1  101 python -c pass\n"
            "  102 101  101 python -c pass\n"
            "  103   1  999 someone else\n",
        )

    monkeypatch.setattr(kill_audit.subprocess, "run", fake_run)

    members, error, backend = kill_audit._group_census(101)

    assert error == ""
    assert backend == "ps"
    assert [member["pid"] for member in members] == [101, 102]


def test_the_census_from_the_host_survives_output_that_is_not_text(
    monkeypatch,
):
    """A host with a process whose name is not valid text must not take
    the census down with it."""
    monkeypatch.setattr(kill_audit, "_load_psutil", lambda: None)
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "  101   1  101 python\n")

    monkeypatch.setattr(kill_audit.subprocess, "run", fake_run)

    members, error, backend = kill_audit._group_census(101)

    assert backend == "ps"
    assert [member["pid"] for member in members] == [101]
    assert captured.get("errors") == "replace"