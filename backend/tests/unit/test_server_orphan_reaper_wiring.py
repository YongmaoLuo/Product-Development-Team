"""The backend's two hooks into the orphan reaper.

The reaper (``backend/orphan_reaper.py``) is a bottom layer: it reclaims
the memory of processes that outlived the thing that started them.  It
can only do that if the processes it looks at carry the ``PDT_SPAWN_*``
marker, and it can only be started if somebody starts it.

The backend is both of those things' owner:

  * it **writes the owner marker** into its own environment at startup,
    so every child it spawns inherits it — a spawn point that was never
    taught about the reaper still produces a traceable child;
  * it **makes one scan and starts the resident daemon**, so a backend
    that boots onto a machine full of leftovers reclaims them, and keeps
    reclaiming them after it is gone.

Three properties of that wiring are pinned here, and each of them is a
way the wiring can quietly go wrong:

  1. **Importing the backend must not write to the environment.**  The
     marker is written in the *lifespan*, not at import time: a module
     that stamped ``os.environ`` on import would mark every process the
     test suite spawns as a child of the test runner, and the reaper
     would then judge those processes by a marker nobody meant to set.
  2. **A failure in the reaper must not cost the backend its boot.**
     The reaper is a convenience; the backend is the product.  A scan
     that raises costs the scan and nothing else — in particular the
     daemon still has to be started, because the daemon is the part that
     keeps working after the backend is gone.
  3. **A reaper that is switched off does nothing at all** — no marker,
     no scan, no daemon.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import orphan_reaper as reaper
import server as server_mod

#: The repository root — the directory that holds the ``backend`` package.
#: Derived from this file's own location so the test never depends on
#: where it was started from.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The three markers the reaper reads.  A test that inherits one of them
#: from an earlier test is not testing the backend, it is testing the
#: order the tests happen to run in.
SPAWN_MARK_KEYS = (
    "PDT_SPAWN_OWNER_PID",
    "PDT_SPAWN_OWNER_START",
    "PDT_SPAWN_KIND",
)


@pytest.fixture
def clean_spawn_env(monkeypatch):
    """Remove any ``PDT_SPAWN_*`` marker for the duration of one test.

    ``monkeypatch`` restores the previous state on teardown — including
    the *absence* of the variable — so a marker written by the test
    cannot leak into the next one.

    The ``setenv``/``delenv`` pair is deliberate: ``delenv`` on a
    variable that is *not* set records nothing to restore, so a marker
    written by the test afterwards would survive the teardown.  Setting
    it first (which records "was not set") and deleting it again (which
    records the temporary value) makes the undo restore the original
    state in both directions.
    """
    for key in SPAWN_MARK_KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return monkeypatch


class _Config:
    """The one field of :class:`ReaperConfig` these hooks read."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _drive_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enter ``server._lifespan`` against a fake app.

    The recovery helpers and the heartbeat are stubbed so the lifespan
    body does not depend on the live state database; what is under test
    is the wiring, not the recovery semantics.

    The two boot sweeps are stubbed as well.  They run on a background
    thread now — they must not sit on the readiness path — so without
    a stand-in this machine's own temp roots and service ledger would
    be swept for real by every test in this file.  The thread is
    captured and joined before the helper returns, so no test leaves a
    sweep running behind it.
    """
    monkeypatch.setattr(server_mod, "_recover_execution_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "_recover_verification_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "load_providers", lambda: [])
    monkeypatch.setattr(server_mod, "heartbeat_monitor", MagicMock())
    monkeypatch.setattr(server_mod, "_reap_all_plans", lambda *a, **k: [])
    monkeypatch.setattr(
        server_mod, "_sweep_credential_residue", lambda apply=True: ([], 0, 0)
    )

    sweep_threads: list[threading.Thread] = []
    original_start = server_mod._start_boot_sweeps

    def _capture() -> threading.Thread:
        thread = original_start()
        sweep_threads.append(thread)
        return thread

    monkeypatch.setattr(server_mod, "_start_boot_sweeps", _capture)

    fake_app = MagicMock()
    fake_app.state = MagicMock()

    async def _enter() -> None:
        async with server_mod._lifespan(fake_app):
            pass

    asyncio.run(_enter())

    for thread in sweep_threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), (
            "the boot-sweep thread outlived the test — it must be joined"
        )


# --------------------------------------------------------------------------
# The lifespan calls both hooks
# --------------------------------------------------------------------------


def test_lifespan_calls_both_hooks_in_order(monkeypatch):
    """The boot marks the process first, and starts the reaper after.

    The order is the whole contract: the marker has to be in place
    before anything spawns a child.  The reaper's scan no longer has
    an order against the ledger sweep — the sweeps run concurrently on
    their own thread, so that the boot does not wait for them — and it
    does not need one: the ledger layer stops the processes a plan
    declared and can still identify, the reaper reclaims the ones
    nobody declared any more, and each judges by its own record.
    """
    calls = []

    monkeypatch.setattr(server_mod, "_orphan_reaper_may_act", lambda: True)
    monkeypatch.setattr(
        server_mod, "_install_orphan_reaper_marks", lambda: calls.append("marks")
    )
    monkeypatch.setattr(
        server_mod, "_start_orphan_reaper", lambda: calls.append("start")
    )

    _drive_lifespan(monkeypatch)

    assert calls == ["marks", "start"]


def test_lifespan_does_not_act_on_the_machine_from_a_test(monkeypatch, clean_spawn_env):
    """A test process must not let the reaper act.

    The suite boots the app — the lifespan tests enter ``_lifespan``
    directly — and a boot that reaped the developer's own processes, or
    left a detached daemon behind, would be a test suite with side
    effects on the machine it runs on.
    """
    calls = []

    monkeypatch.setattr(
        server_mod, "_install_orphan_reaper_marks", lambda: calls.append("marks")
    )
    monkeypatch.setattr(
        server_mod, "_start_orphan_reaper", lambda: calls.append("start")
    )

    _drive_lifespan(monkeypatch)

    assert calls == []
    assert "PDT_SPAWN_OWNER_PID" not in os.environ


def test_lifespan_really_starts_the_reaper_outside_a_test_process(monkeypatch):
    """Outside a test process the boot reaches the reaper's entry points.

    This is the negative control for the test guard above: the hook is
    not permanently disabled, it is disabled *for tests*.  The reaper's
    own functions are replaced here, so the chain is exercised without
    scanning the machine or starting a daemon.
    """
    calls = []
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        server_mod, "_install_orphan_reaper_marks", lambda: calls.append("marks")
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "config_from_env",
        lambda: _Config(enabled=True),
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "run_once",
        lambda cfg: calls.append("run_once"),
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "ensure_daemon",
        lambda cfg: calls.append("ensure_daemon"),
    )

    _drive_lifespan(monkeypatch)

    assert calls == ["marks", "run_once", "ensure_daemon"]


# --------------------------------------------------------------------------
# The environment is stamped in the lifespan, never at import time
# --------------------------------------------------------------------------


def test_import_server_does_not_stamp_environ():
    """Importing the backend must not mark the process it runs in.

    The marker means "the process that spawned me".  Written at import
    time it would be a lie about every process the importer spawns, and
    the reaper would believe it.

    The check runs in a **fresh interpreter**, because in-process the
    environment has already been touched by earlier tests — a marker
    left behind by a previous lifespan would be inherited here and the
    answer would depend on the order the tests ran in.  The inherited
    environment is therefore cleaned of the three markers before the
    child starts, and the child reports what it finds *after* the
    import.

    The child also reports **whether it managed to import the reaper
    at all**.  That is not decoration: the marker is written by the
    reaper, so a child that could not import the reaper has nothing
    that *could* write a marker, and "no marker in the environment"
    would then be true for a reason that has nothing to do with the
    backend's behaviour.  The assertion below would pass — vacuously —
    in exactly the case where it is least worth anything.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in SPAWN_MARK_KEYS
    }
    script = (
        "import os\n"
        "import backend.server as server\n"
        "print('|'.join(os.environ.get(key, '<absent>') for key in "
        "('PDT_SPAWN_OWNER_PID', 'PDT_SPAWN_OWNER_START', "
        "'PDT_SPAWN_KIND')))\n"
        "print('reaper=' + ('resolved' if server.orphan_reaper is not None "
        "else 'missing'))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, (
        "importing the backend failed, so the marker question could not "
        f"be answered.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )

    stdout_lines = completed.stdout.strip().splitlines()

    assert stdout_lines[0] == "<absent>|<absent>|<absent>", (
        "importing the backend wrote a spawn marker into the "
        f"environment: {stdout_lines[0]!r}"
    )
    assert stdout_lines[-1] == "reaper=resolved", (
        "the child could not import the reaper, so the absence of the "
        "marker proves nothing — with no reaper there is nothing that "
        f"could have written one. The child said: {stdout_lines[-1]!r}"
    )


def test_install_marks_writes_the_owner_marker(clean_spawn_env):
    """The owner marker is written into the backend's own environment.

    That is the whole point of it: every child the backend spawns
    inherits the marker through the environment, so a spawn point that
    knows nothing about the reaper still leaves a traceable child.
    """
    marks = server_mod._install_orphan_reaper_marks()

    assert marks is not None
    assert os.environ["PDT_SPAWN_OWNER_PID"] == str(os.getpid())
    assert os.environ["PDT_SPAWN_OWNER_START"]


def test_install_marks_is_idempotent(clean_spawn_env):
    """Calling it twice writes the same marks.

    The lifespan runs once per process, but the function is a plain
    function: making it safe to call twice costs nothing and removes a
    whole class of surprise.
    """
    first = server_mod._install_orphan_reaper_marks()
    second = server_mod._install_orphan_reaper_marks()

    assert first == second
    assert os.environ["PDT_SPAWN_OWNER_PID"] == str(os.getpid())


def test_install_marks_writes_no_kind(clean_spawn_env):
    """Only the owner is written; the *kind* is left to the spawn point.

    "Who spawned this" and "what is this" are different questions, and
    a process that owns children is not thereby a service or a reaper.
    """
    server_mod._install_orphan_reaper_marks()

    assert "PDT_SPAWN_KIND" not in os.environ


# --------------------------------------------------------------------------
# The daemon is started by the backend
# --------------------------------------------------------------------------


def test_start_calls_run_once_and_ensure_daemon(monkeypatch):
    """Starting the backend makes one pass and starts the resident reaper.

    The pass answers "what is on this machine right now" — the orphans
    a previous life left behind.  The daemon answers "what will be on
    this machine in a minute" — and it has to outlive the backend, which
    is the whole reason it is a separate process.
    """
    calls = []
    config = _Config(enabled=True)

    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "config_from_env",
        lambda: calls.append("config_from_env") or config,
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "run_once",
        lambda cfg: calls.append(("run_once", cfg)),
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "ensure_daemon",
        lambda cfg: calls.append(("ensure_daemon", cfg)) or 4321,
    )

    server_mod._start_orphan_reaper()

    assert calls == [
        "config_from_env",
        ("run_once", config),
        ("ensure_daemon", config),
    ]


def test_disabled_env_skips_both(monkeypatch, clean_spawn_env):
    """``PDT_ORPHAN_REAPER=0`` means the reaper does nothing at all.

    Not "does the work quietly" — *nothing*: no marker on the
    environment, no scan, no daemon.  A deployment that switched the
    reaper off must not get a reaper anyway.
    """
    calls = []
    monkeypatch.setenv("PDT_ORPHAN_REAPER", "0")
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "run_once",
        lambda cfg: calls.append("run_once"),
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "ensure_daemon",
        lambda cfg: calls.append("ensure_daemon"),
    )

    server_mod._install_orphan_reaper_marks()
    server_mod._start_orphan_reaper()

    assert calls == []
    assert "PDT_SPAWN_OWNER_PID" not in os.environ


def test_run_once_failure_does_not_abort_startup(monkeypatch, caplog):
    """A scan that raises costs the scan and nothing else.

    The daemon still has to be started: it is the part that keeps
    reclaiming memory after this backend is gone, and a startup that
    gave up on it would leave the orphans behind for good.
    """
    calls = []
    config = _Config(enabled=True)

    def boom(_config):
        calls.append("run_once")
        raise RuntimeError("the machine is on fire")

    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "config_from_env",
        lambda: config,
    )
    monkeypatch.setattr(server_mod.orphan_reaper, "run_once", boom)
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "ensure_daemon",
        lambda cfg: calls.append("ensure_daemon"),
    )

    with caplog.at_level(logging.WARNING):
        server_mod._start_orphan_reaper()

    assert calls == ["run_once", "ensure_daemon"]
    assert any("orphan_reaper" in record.message for record in caplog.records)


def test_ensure_daemon_failure_does_not_abort_startup(monkeypatch, caplog):
    """A daemon that cannot be started costs the daemon and nothing else."""
    config = _Config(enabled=True)

    def boom(_config):
        raise RuntimeError("the daemon cannot be started")

    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "config_from_env",
        lambda: config,
    )
    monkeypatch.setattr(
        server_mod.orphan_reaper,
        "run_once",
        lambda cfg: None,
    )
    monkeypatch.setattr(server_mod.orphan_reaper, "ensure_daemon", boom)

    with caplog.at_level(logging.WARNING):
        server_mod._start_orphan_reaper()

    assert any("orphan_reaper" in record.message for record in caplog.records)


def test_missing_reaper_module_is_a_no_op(monkeypatch, clean_spawn_env):
    """A reaper that could not be imported costs the reaper and nothing else.

    ``orphan_reaper`` is resolved at import time and may be ``None`` when
    the module is missing.  Both hooks then do nothing — no marker, no
    scan, no daemon, and above all no exception: the backend boots.
    """
    monkeypatch.setattr(server_mod, "orphan_reaper", None)

    assert server_mod._install_orphan_reaper_marks() is None
    assert server_mod._start_orphan_reaper() is None
    assert "PDT_SPAWN_OWNER_PID" not in os.environ


# --------------------------------------------------------------------------
# The scan the backend starts must not cost the machine a probe per process
# --------------------------------------------------------------------------
#
# The startup scan runs on the *readiness path*: uvicorn binds the
# socket only after the lifespan returns, so every second the scan
# spends is a second in which the API does not answer.  The scan reads
# the environment of every process on the machine, and the way it used
# to do that — two ``ps`` children per process — made the scan outlive
# the processes it was judging.
#
# So the *number of probes* is a property of the wiring, and it is
# pinned here: a scan that asks the machine once per process cannot
# finish before the processes it judges have exited.


#: How many processes the bulk-read fixtures describe.
BULK_PROCESS_COUNT = 200

#: The first pid the bulk fixtures use.  High enough not to collide
#: with a real process on the machine running the test.
BULK_FIRST_PID = 900000


def _bulk_process_table(count=BULK_PROCESS_COUNT):
    """A ``ps -axo`` table with *count* processes."""
    rows = []
    for index in range(count):
        pid = BULK_FIRST_PID + index
        rows.append(
            "%7d %5d %7d %5d %7d /usr/bin/python3 -m worker --slot %d"
            % (pid, 1, pid, os.getuid(), 4096, index)
        )
    return "\n".join(rows) + "\n"


def _bulk_env_table(count=BULK_PROCESS_COUNT):
    """A ``ps -E -A`` listing with *count* processes and their environments."""
    lines = ["  PID TTY           TIME CMD"]
    for index in range(count):
        pid = BULK_FIRST_PID + index
        lines.append(
            "%7d ??         0:00.03 /usr/bin/python3 -m worker --slot %d "
            "PATH=/usr/bin:/bin BULK_SLOT=%d" % (pid, index, index)
        )
    return "\n".join(lines) + "\n"


def _fake_ps_machine(monkeypatch, count=BULK_PROCESS_COUNT):
    """Replace ``ps`` with a machine that answers both questions at once.

    The returned list records every call, so a case can count how many
    times the machine was asked.
    """
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "-E" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_bulk_env_table(count), stderr="")
        if "-axo" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_bulk_process_table(count), stderr="")
        if "-o" in cmd:
            pid = int(cmd[cmd.index("-p") + 1])
            index = pid - BULK_FIRST_PID
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="/usr/bin/python3 -m worker --slot %d\n" % index,
                stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)
    return calls


def test_the_bulk_read_answers_for_every_process_at_once(monkeypatch):
    """One ``ps -E -A`` answers for every process, and the environment
    is the one that belongs to the process that was asked about.

    The command line the process table carries says where the
    environment starts inside the line; the environment after it is
    that process' own, and not the environment of the line above or
    below it.
    """
    calls = _fake_ps_machine(monkeypatch)
    procs = reaper.list_processes()

    envs = reaper.read_process_environ_bulk(procs)

    assert envs is not None, "the bulk read did not happen at all"
    bulk_calls = [call for call in calls if "-E" in call]
    assert len(bulk_calls) == 1, (
        "the bulk read asked the machine %d times; it is supposed to ask "
        "once" % len(bulk_calls)
    )
    assert set(envs) == {BULK_FIRST_PID + index for index in range(BULK_PROCESS_COUNT)}
    assert envs[BULK_FIRST_PID]["BULK_SLOT"] == "0"
    last = BULK_FIRST_PID + BULK_PROCESS_COUNT - 1
    assert envs[last]["BULK_SLOT"] == str(BULK_PROCESS_COUNT - 1)


def test_a_scan_asks_the_machine_a_constant_number_of_times(monkeypatch, tmp_path):
    """A scan of two hundred processes must not ask ``ps`` four hundred times.

    The per-process read costs two ``ps`` children per process.  A scan
    that asks the machine once per process is *right* and useless: it
    spends longer asking than the processes live, so the processes it
    judges have already gone, and their environment reads back as
    unreadable rather than as the unmarked environment it was.

    This pins the number of probes, because the number of probes is
    what the cost is.
    """
    calls = _fake_ps_machine(monkeypatch)
    config = reaper.ReaperConfig(plans_root=tmp_path, dry_run=True)

    result = reaper.scan(config, self_uid=os.getuid())

    assert len(result.verdicts) == BULK_PROCESS_COUNT
    assert len(calls) <= 5, (
        "the scan asked the machine %d times for %d processes; a scan that "
        "asks once per process cannot finish before the processes it "
        "judges have exited"
        % (len(calls), BULK_PROCESS_COUNT)
    )
    reasons = {verdict.proc.pid: verdict.reason for verdict in result.verdicts}
    assert reasons[BULK_FIRST_PID] == reaper.REASON_UNMARKED, (
        "the bulk read did not deliver the environment: the first process "
        "was judged %r, which says its environment could not be read"
        % reasons[BULK_FIRST_PID]
    )


def test_an_injected_environment_reader_is_not_second_guessed(monkeypatch, tmp_path):
    """A caller that brings its own reader keeps it.

    The bulk read is an optimisation of the *default* reader, not a
    replacement for a decision the caller made: a caller that injects a
    reader is asking a specific question, and the scan must answer that
    question.
    """
    asked = []

    def fake_reader(pid):
        asked.append(pid)
        return {"BULK_SLOT": "injected"}

    _fake_ps_machine(monkeypatch)
    config = reaper.ReaperConfig(plans_root=tmp_path, dry_run=True)

    result = reaper.scan(config, env_reader=fake_reader, self_uid=os.getuid())

    assert asked, "the injected reader was never asked"
    assert len(asked) == BULK_PROCESS_COUNT
    reasons = {verdict.proc.pid: verdict.reason for verdict in result.verdicts}
    assert reasons[BULK_FIRST_PID] == reaper.REASON_UNMARKED


def test_a_failed_bulk_read_falls_back_to_the_per_process_reader(monkeypatch, tmp_path):
    """A ``ps -E -A`` that cannot be read costs the bulk read and nothing else.

    The fallback is the per-process reader, which is slower but answers
    the same question.  Without it a failed bulk read would report every
    process on the machine as *unreadable* — a silent, total loss of the
    reaper's evidence, dressed up as a scan that ran.
    """
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "-E" in cmd and "-A" in cmd:
            # The bulk read fails.
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no")
        if "-E" in cmd:
            pid = int(cmd[cmd.index("-p") + 1])
            index = pid - BULK_FIRST_PID
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="%7d ??         0:00.03 /usr/bin/python3 -m worker "
                "--slot %d PATH=/usr/bin:/bin BULK_SLOT=%d\n"
                % (pid, index, index),
                stderr="")
        if "-axo" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_bulk_process_table(), stderr="")
        if "-o" in cmd:
            pid = int(cmd[cmd.index("-p") + 1])
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="/usr/bin/python3 -m worker --slot %d\n"
                % (pid - BULK_FIRST_PID),
                stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    monkeypatch.setattr(reaper, "_IS_DARWIN", True)
    config = reaper.ReaperConfig(plans_root=tmp_path, dry_run=True)

    result = reaper.scan(config, self_uid=os.getuid())

    assert any("-A" in call for call in calls), (
        "the bulk read was never attempted, so this case proves nothing "
        "about what happens when it fails"
    )
    reasons = {verdict.proc.pid: verdict.reason for verdict in result.verdicts}
    assert reasons[BULK_FIRST_PID] == reaper.REASON_UNMARKED, (
        "the per-process reader did not take over after the bulk read "
        "failed; the process was judged %r instead"
        % reasons[BULK_FIRST_PID]
    )


# --------------------------------------------------------------------------
# Reading a thousand environments must not cost a thousand shell lexers
# --------------------------------------------------------------------------
#
# The bulk read asks the machine once, but it still has to read what the
# machine said: one blob of ``KEY=VALUE`` tokens per process.  Splitting
# that is the expensive part — the shell lexer builds a word one
# character at a time, which is quadratic in the length of the word, and
# an environment is full of long values — and most of what it is asked to
# split needs no lexer at all: a blob with no quote and no backslash is
# split by whitespace alone, and the lexer gives exactly that answer.
#
# Three cases carry the win, and all three are pinned here: a blob
# without a quote, a line that cannot hold a variable at all, and the
# split itself, which has to keep answering what the lexer answers.


def test_a_quote_free_blob_is_split_without_the_lexer(monkeypatch):
    """A blob with no quote and no backslash is split by whitespace alone.

    The lexer would give the same answer, and it costs a great deal
    more: on a machine with a thousand processes the bulk read spends
    most of its time in the lexer.
    """
    def explode(*args, **kwargs):
        raise AssertionError(
            "the lexer was used for a blob that needs no lexer"
        )

    monkeypatch.setattr(reaper, "_posix_split", explode)

    assert reaper._split_env_blob("PATH=/usr/bin:/bin HOME=/root") == [
        "PATH=/usr/bin:/bin",
        "HOME=/root",
    ]


def test_the_cheap_split_is_the_same_split_the_lexer_gives():
    """The cheap split must be the lexer's answer, token for token.

    A cheaper split that splits differently is not a cheaper split, it
    is a different reading of the machine's answer — and the reaper
    reads the environment out of that answer.
    """
    for text in (
        "PATH=/usr/bin:/bin HOME=/root",
        "A=1  B=2",
        "A=1\tB=2",
        "A=1\nB=2",
        "A=1 B=2",
        "",
        "   ",
        "A=1 'B=2'",
        "A=1 B=\"2 3\"",
    ):
        assert reaper._split_env_blob(text) == shlex.split(text), (
            "the cheap split and the shell lexer disagree about %r" % text
        )

    # An unbalanced quote falls back to a plain split rather than raising:
    # an apostrophe inside a value is not a reason to lose the whole blob.
    assert reaper._split_env_blob("A=it's B=2") == "A=it's B=2".split()


def test_the_split_is_the_shell_lexers_split():
    """The lexer is the specification; the split has to match it.

    The reaper reads a machine's answer out of a blob of variables, and
    the blob is split the way a shell would split it.  The lexer is
    what defines that, so the lexer is what the split is checked
    against — on the shapes a quote and a backslash make, and on every
    short combination of them, because those are the shapes where two
    readers of the same text can part ways.

    A disagreement is not a slower answer, it is a different answer: the
    environment a process was launched with would be read as something
    else, and the reaper decides what to kill from what it reads.
    """
    def outcome(text):
        try:
            return ("words", shlex.split(text))
        except ValueError as exc:
            return ("error", type(exc).__name__)

    def ours(text):
        try:
            return ("words", reaper._posix_split(text))
        except ValueError as exc:
            return ("error", type(exc).__name__)

    shapes = [
        "", " ", "  ", "\t", "\n",
        "A=1", "A=1 B=2", "A=1  B=2", "A=1\tB=2", "A=1\nB=2",
        "A='x y'", 'A="x y"', "A='x", 'A="x', "A=x\\ y", "A=x\\",
        "A=''", 'A=""', "A='a'\"b\"", "A='a'b'c'", "A=\"a\\\"b\"",
        "A=\"a\\\\b\"", "A=\"a\\nb\"", "A='a\\'b'", "A=\\x", "A=x\\'y",
        "A='a b' 'c d'", "A=\"a b\"'c d'", "a'b'c", 'a"b"c',
    ]
    for text in shapes:
        assert ours(text) == outcome(text), (
            "the split and the shell lexer disagree about %r: %r vs %r"
            % (text, ours(text), outcome(text))
        )

    # Every combination of the shapes that make quoting hard, up to
    # three characters long: 4**3 is small enough to be exhaustive, and
    # exhaustive is the point — the interesting disagreements are short.
    alphabet = ("a", " ", "'", '"', "\\")
    for length in range(1, 4):
        for combination in itertools.product(alphabet, repeat=length):
            text = "".join(combination)
            assert ours(text) == outcome(text), (
                "the split and the shell lexer disagree about %r: %r vs %r"
                % (text, ours(text), outcome(text))
            )


def test_a_line_without_an_equals_sign_costs_no_tokenising(monkeypatch):
    """A line that cannot hold a variable is *unreadable*, and saying so
    must cost nothing.

    Most process lines on a macOS machine carry no environment at all —
    the binary is protected by SIP — so there is no ``k=v`` in them to
    find.  A line with no ``=`` cannot yield a single pair, in the
    segment or in the whole line, so the answer is the same whether the
    blob is tokenised or not.
    """
    def explode(*args, **kwargs):
        raise AssertionError(
            "the blob was tokenised even though the line carries no '='"
        )

    monkeypatch.setattr(reaper, "_posix_split", explode)

    line = "  123 ??         0:00.01 /usr/bin/python3 -m worker"
    assert reaper._environ_from_darwin_line(line, "/usr/bin/python3 -m worker") is None
