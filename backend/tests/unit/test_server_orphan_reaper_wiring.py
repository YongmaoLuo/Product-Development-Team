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
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    """
    monkeypatch.setattr(server_mod, "_recover_execution_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "_recover_verification_states", lambda plans_dir: None)
    monkeypatch.setattr(server_mod, "load_providers", lambda: [])
    monkeypatch.setattr(server_mod, "heartbeat_monitor", MagicMock())

    fake_app = MagicMock()
    fake_app.state = MagicMock()

    async def _enter() -> None:
        async with server_mod._lifespan(fake_app):
            pass

    asyncio.run(_enter())


# --------------------------------------------------------------------------
# The lifespan calls both hooks
# --------------------------------------------------------------------------


def test_lifespan_calls_both_hooks_in_order(monkeypatch):
    """The boot marks the process first, and starts the reaper after.

    The order is the whole contract: the marker has to be in place
    before anything spawns a child, and the reaper's scan belongs after
    the ledger sweep, so that the processes the ledger knows about are
    dealt with by the layer that knows about them.
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
