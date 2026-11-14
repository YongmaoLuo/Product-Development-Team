"""Unit tests: finding this plan's **own** service instances (2026-11-13).

The incident (2026-11-13): a round that had to
relocate its service left its own instance behind on the free port it
moved to.  Rule 3 of :mod:`service_manager` only ever looked at the
**declared** port, so that leftover was invisible; it kept the plan's
instance lock, and every later attempt to start a service was refused
with rc=3.

:func:`service_manager.own_service_instances` is the finding half of the
fix — this plan's own live instances, on whatever port they sit.  It
makes no placement decision: what to do with an instance it finds
(adopt it, reap it, relocate around it) belongs to the caller.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import process_ledger  # noqa: E402
import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


#: The port the plan declared.
DECLARED_PORT = 3000
#: The port the round had to move to when the declared one was taken.
MOVED_PORT = 38921
#: The pid of the instance this plan started.
PID = 4242
#: When that process started.
START_EPOCH = 1760000000.0
#: The command the declaration starts, on the declared port.
START_CMD = "npx next dev -p 3000"
#: The command line the running process carries (the relocated port).
CMDLINE = ("npx", "next", "dev", "-p", "38921")


def _decl(**overrides) -> ServiceDeclaration:
    """The declaration of the ``api`` service."""
    base = dict(name="api", port=DECLARED_PORT, start_cmd=START_CMD, cwd=".")
    base.update(overrides)
    return ServiceDeclaration(**base)


def _pin(**overrides) -> Dict[str, Any]:
    """A round pin, as :func:`service_manager.pin_services_for_round` writes it."""
    base = {
        "name": "api",
        "pid": PID,
        "pgid": PID,
        "boot_id": f"{PID}@{int(START_EPOCH)}",
        "port": MOVED_PORT,
        "declared_port": DECLARED_PORT,
        "round_token": "",
        "writer_pid": 1,
        "created_at": START_EPOCH,
        "expires_at": START_EPOCH + 600.0,
    }
    base.update(overrides)
    return base


def _ledger_entry(**overrides) -> Dict[str, Any]:
    """A ledger row, as ``ServiceRuntime.to_dict`` writes it."""
    base = {
        "name": "api",
        "port": MOVED_PORT,
        "declared_port": DECLARED_PORT,
        "pid": PID,
        "pgid": PID,
        "start_epoch": START_EPOCH,
        "start_cmd": "npx next dev -p 38921",
        "cwd": ".",
        "log_path": "",
        "origin": "spawned",
        "ready": True,
        "owner_token": "run-123",
    }
    base.update(overrides)
    return base


def _record(
    pid: int = PID, cmdline: tuple = CMDLINE, **overrides,
) -> process_ledger.ProcessRecord:
    """The live process record the unified ledger holds for our instance."""
    base = dict(
        plan_id="plan-x",
        role="vp_service",
        pid=pid,
        pgid=pid,
        create_time=START_EPOCH,
        cmdline=tuple(cmdline),
        cwd="/tmp/plan-x-project",
        owner_pid=1,
        owner_role="backend",
    )
    base.update(overrides)
    return process_ledger.ProcessRecord(**base)


def _install(
    monkeypatch,
    *,
    pins: Optional[List[Dict[str, Any]]] = None,
    ledger: Optional[Dict[str, Any]] = None,
    records: Optional[Dict[int, Any]] = None,
    epoch: Optional[float] = START_EPOCH,
) -> None:
    """Say what the plan's own bookkeeping says.

    Every read is replaced, so the case is about the *decision* and not
    about the files on disk.
    """
    monkeypatch.setattr(
        sm, "live_round_pins", lambda plan_dir: list(pins or []),
    )
    monkeypatch.setattr(
        sm, "read_ledger", lambda plan_dir: dict(ledger or {}),
    )
    monkeypatch.setattr(
        sm.process_ledger, "live_records",
        lambda plan_dir: dict(records or {}),
    )
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: epoch)


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


def test_pin_for_our_service_is_found(tmp_path, monkeypatch):
    """The relocated instance is found — it is on 38921, not on 3000.

    The declaration says 3000; the round moved to 38921; the pin says
    so.  A finder that only looked at the declared port would answer
    "nothing" here, which is exactly how the instance kept the plan's
    instance lock on 2026-11-13.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        pins=[_pin()],
        records={PID: _record()},
    )

    found = sm.own_service_instances(plan_dir, _decl(), tmp_path)

    assert found == [{
        "name": "api",
        "pid": PID,
        "port": MOVED_PORT,
        "declared_port": DECLARED_PORT,
        "source": "pin",
        "start_epoch": START_EPOCH,
        "owner_token": "",
    }]


def test_ledger_entry_for_our_service_is_found(tmp_path, monkeypatch):
    """The ledger is the second witness — and it carries the owner token."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        ledger={"services": [_ledger_entry()]},
        records={PID: _record()},
    )

    found = sm.own_service_instances(plan_dir, _decl(), tmp_path)

    assert found == [{
        "name": "api",
        "pid": PID,
        "port": MOVED_PORT,
        "declared_port": DECLARED_PORT,
        "source": "ledger",
        "start_epoch": START_EPOCH,
        "owner_token": "run-123",
    }]


def test_foreign_service_is_never_returned(tmp_path, monkeypatch):
    """Another service of the plan is not this service.

    The command line matches on purpose: the case is about the *name*,
    not about the program.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        pins=[_pin(name="worker", declared_port=3001, port=39999)],
        records={PID: _record()},
    )

    assert sm.own_service_instances(plan_dir, _decl(), tmp_path) == []


def test_dead_pid_is_dropped(tmp_path, monkeypatch):
    """A record that names a process which is gone names nothing."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        pins=[_pin()],
        records={PID: _record()},
        epoch=None,
    )

    assert sm.own_service_instances(plan_dir, _decl(), tmp_path) == []


def test_pin_and_ledger_dedupe(tmp_path, monkeypatch):
    """The same process in both books is one process."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        pins=[_pin()],
        ledger={"services": [_ledger_entry()]},
        records={PID: _record()},
    )

    found = sm.own_service_instances(plan_dir, _decl(), tmp_path)

    assert len(found) == 1, found
    assert found[0]["pid"] == PID
    assert found[0]["port"] == MOVED_PORT


def test_missing_plan_dir_is_empty(tmp_path):
    """A plan without a directory has no bookkeeping to read."""
    assert sm.own_service_instances(None, _decl(), tmp_path) == []


def test_cmdline_mismatch_is_not_ours(tmp_path, monkeypatch):
    """The name says ``api``; the command line says something else.

    The command line is the identity: a stranger that happens to sit on
    our port, or a process of ours that was replaced, must not be
    adopted as our own instance.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        pins=[_pin()],
        records={PID: _record(cmdline=("sleep", "1000"))},
    )

    assert sm.own_service_instances(plan_dir, _decl(), tmp_path) == []


# ---------------------------------------------------------------------------
# The boundary conditions
# ---------------------------------------------------------------------------


def test_recycled_pid_is_dropped(tmp_path, monkeypatch):
    """A pin whose boot id disagrees with the live process is about a stranger.

    The pid was recycled: the number is the same, the process is not.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        pins=[_pin(boot_id=f"{PID}@{int(START_EPOCH - 600)}")],
        records={PID: _record()},
    )

    assert sm.own_service_instances(plan_dir, _decl(), tmp_path) == []


def test_a_failing_pin_read_returns_what_was_collected(tmp_path, monkeypatch):
    """A damaged pin file costs the pins, not the ledger."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()

    def _boom(plan_dir):
        raise OSError("damaged pin file")

    monkeypatch.setattr(sm, "live_round_pins", _boom)
    monkeypatch.setattr(
        sm, "read_ledger", lambda plan_dir: {"services": [_ledger_entry()]},
    )
    monkeypatch.setattr(
        sm.process_ledger, "live_records",
        lambda plan_dir: {PID: _record()},
    )
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: START_EPOCH)

    found = sm.own_service_instances(plan_dir, _decl(), tmp_path)

    assert [entry["source"] for entry in found] == ["ledger"]


def test_a_failing_ledger_read_returns_what_was_collected(tmp_path, monkeypatch):
    """A damaged ledger costs the ledger, not the pins."""
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()

    def _boom(plan_dir):
        raise OSError("damaged ledger")

    monkeypatch.setattr(sm, "live_round_pins", lambda plan_dir: [_pin()])
    monkeypatch.setattr(sm, "read_ledger", _boom)
    monkeypatch.setattr(
        sm.process_ledger, "live_records",
        lambda plan_dir: {PID: _record()},
    )
    monkeypatch.setattr(sm, "_process_start_epoch", lambda pid: START_EPOCH)

    found = sm.own_service_instances(plan_dir, _decl(), tmp_path)

    assert [entry["source"] for entry in found] == ["pin"]


def test_the_interpreter_spelling_does_not_hide_our_instance(
    tmp_path, monkeypatch,
):
    """``python3`` in the declaration, homebrew's ``Python`` in the process.

    The real instance of 2026-11-13: the declaration says
    ``python3 -m backend.server`` and the running process is
    ``.../Versions/3.11/Resources/Python.app/Contents/MacOS/Python -m
    backend.server``.  A version suffix is not an identity — it is the
    same program, and a finder that cannot see it is the bug this
    function exists to fix.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    decl = ServiceDeclaration(
        name="pdt", port=18096,
        start_cmd=(
            "source /venv/bin/activate && export PDT_PORT=18096 && "
            "exec python3 -m backend.server"
        ),
        cwd=".",
    )
    _install(
        monkeypatch,
        pins=[_pin(name="pdt", port=18096, declared_port=18096)],
        records={PID: _record(cmdline=(
            "/opt/homebrew/Cellar/python@3.11/3.11.13/Resources/Python.app/"
            "Contents/MacOS/Python",
            "-m",
            "backend.server",
        ))},
    )

    found = sm.own_service_instances(plan_dir, decl, tmp_path)

    assert [entry["port"] for entry in found] == [18096]


def test_the_recorded_command_is_the_fallback_witness(tmp_path, monkeypatch):
    """A plan whose ledger holds no process records is still readable.

    The retired whole-file ledger carries the command we ran and no
    process records at all — and that command is still a witness.
    """
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    _install(
        monkeypatch,
        ledger={"services": [_ledger_entry()]},
        records={},
    )

    found = sm.own_service_instances(plan_dir, _decl(), tmp_path)

    assert [entry["source"] for entry in found] == ["ledger"]


def test_a_plan_dir_that_does_not_exist_is_empty(tmp_path):
    """A plan that has not written its books yet has no instances."""
    assert sm.own_service_instances(
        tmp_path / "never-written", _decl(), tmp_path,
    ) == []
