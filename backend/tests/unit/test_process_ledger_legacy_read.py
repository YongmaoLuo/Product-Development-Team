"""Reading a plan's ownership ledger must survive the format switch.

Why these tests exist
---------------------
Ownership bookkeeping is moving from a whole-file dict
(``managed_services.json``, written by ``service_manager``) to an
append-only event log (``process_ledger.jsonl``). Plans started before
the switch still have only the old file on disk, and a backend that
restarts in the middle of the migration has to be able to answer "these
pids are ours" from *either* shape — otherwise the processes it
spawned are never reaped and leak for the lifetime of the machine.

The rules that matter:

* the old file is normalised into the *same* records the new ledger
  produces, so everything downstream sees one kind of record and the
  reaper does not need a second code path;
* an entry without a pid is skipped, not invented — a service that
  never came up has no process to kill, and a phantom in the live set
  is worse than a missing one;
* the event log wins when both exist — it is what the current code
  writes, and a stale pid in the old file must not be mistaken for a
  live one (pids get reused);
* a damaged old file reads as empty. It must not raise: the caller is
  a shutdown path that is already dealing with a failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import process_ledger as ledger  # noqa: E402


#: One service entry in the shape the old ledger wrote it.
_WEB_SERVICE = {
    "name": "web",
    "port": 3000,
    "pid": 200,
    "pgid": 200,
    "start_epoch": 1700000000.0,
    "start_cmd": ["python", "-m", "http.server"],
    "cwd": "/tmp/p1",
    "log_path": "/tmp/p1/web.log",
    "origin": "verification",
    "ready": True,
    "reap_on_exit": True,
}


def _write_legacy(plan_dir, payload) -> Path:
    """Write a legacy ``managed_services.json`` and return its path."""
    path = plan_dir / ledger.LEGACY_LEDGER_FILENAME
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _spawn_record(**overrides) -> ledger.ProcessRecord:
    """A record in the *new* ledger's shape, for the jsonl side."""
    kwargs = dict(
        plan_id="p1",
        role="executor",
        pid=300,
        pgid=300,
        create_time=1700000500.0,
        cmdline=["python", "cli.py"],
        cwd="/tmp/p1",
        owner_pid=100,
        owner_role="backend",
    )
    kwargs.update(overrides)
    return ledger.ProcessRecord(**kwargs)


# ---------------------------------------------------------------------------
# The old file is understood and normalised into the new record shape.
# ---------------------------------------------------------------------------
def test_legacy_ledger_maps_services_to_vp_service_records(tmp_path):
    _write_legacy(tmp_path, {"plan_id": "p1", "services": [_WEB_SERVICE]})

    # The old file is not an event log, so the reader has to synthesise
    # the events the rest of the module knows how to fold.
    events = ledger.read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["event"] == ledger.EVENT_SPAWN
    assert events[0]["pid"] == 200

    live = ledger.live_records(tmp_path)
    assert 200 in live
    record = live[200]
    # The old ledger only ever recorded VP services.
    assert record.role == "vp_service"
    assert record.create_time == 1700000000.0
    assert record.pid == 200
    assert record.pgid == 200
    assert record.cmdline == ("python", "-m", "http.server")
    assert record.cwd == "/tmp/p1"
    assert record.plan_id == "p1"


# ---------------------------------------------------------------------------
# An entry with no pid is skipped: there is no process to act on.
# ---------------------------------------------------------------------------
def test_legacy_ledger_skips_entries_without_pid(tmp_path):
    _write_legacy(
        tmp_path,
        {
            "services": [
                {"name": "no-key", "port": 3001, "start_epoch": 1.0},
                {"name": "null-pid", "port": 3002, "pid": None},
                {"name": "zero-pid", "port": 3003, "pid": 0},
                {
                    "name": "web",
                    "port": 3000,
                    "pid": 200,
                    "pgid": 200,
                    "start_epoch": 1700000000.0,
                    "start_cmd": ["python", "-m", "http.server"],
                },
            ]
        },
    )

    live = ledger.live_records(tmp_path)
    assert list(live) == [200]
    assert live[200].role == "vp_service"


# ---------------------------------------------------------------------------
# The event log wins when both exist.
# ---------------------------------------------------------------------------
def test_jsonl_wins_when_both_exist(tmp_path):
    _write_legacy(tmp_path, {"plan_id": "p1", "services": [_WEB_SERVICE]})
    ledger.record_spawn(tmp_path, _spawn_record())

    live = ledger.live_records(tmp_path)
    # Only the event log's process is live: the old file's pid is not
    # merged in, because a pid recorded there may have been reaped long
    # ago and may since have been handed to somebody else.
    assert list(live) == [300]
    assert live[300].role == "executor"
    assert live[300].create_time == 1700000500.0


# ---------------------------------------------------------------------------
# A damaged old file reads as empty — and never raises.
# ---------------------------------------------------------------------------
def test_corrupt_legacy_ledger_reads_as_empty(tmp_path):
    (tmp_path / ledger.LEGACY_LEDGER_FILENAME).write_text(
        "{not json at all", encoding="utf-8"
    )

    assert ledger.read_events(tmp_path) == []
    assert ledger.live_records(tmp_path) == {}

    # A file that parses but is not the expected shape is just as
    # unreadable as one that does not parse at all.
    _write_legacy(tmp_path, ["not", "a", "dict"])
    assert ledger.read_events(tmp_path) == []
    assert ledger.live_records(tmp_path) == {}


# ---------------------------------------------------------------------------
# The command line was written both as a string and as a token list.
# ---------------------------------------------------------------------------
def test_start_cmd_as_string_or_list_becomes_a_tuple(tmp_path):
    _write_legacy(
        tmp_path,
        {
            "services": [
                {
                    "name": "web",
                    "pid": 200,
                    "start_epoch": 1700000000.0,
                    "start_cmd": ["python", "-m", "http.server"],
                },
                {
                    "name": "api",
                    "pid": 201,
                    "start_epoch": 1700000001.0,
                    "start_cmd": "python -m api",
                },
            ]
        },
    )

    live = ledger.live_records(tmp_path)
    assert live[200].cmdline == ("python", "-m", "http.server")
    # A bare string is one command, not a sequence of characters.
    assert live[201].cmdline == ("python -m api",)


# ---------------------------------------------------------------------------
# The files already on disk carry more than the minimal shape: they name
# the plan that owns the service and the process that started it.
# ---------------------------------------------------------------------------
def test_legacy_entry_keeps_the_owner_and_plan_it_records(tmp_path):
    _write_legacy(
        tmp_path,
        {
            "plan_id": "pgprobe",
            "project_dir": "/srv/app",
            "round": 1,
            "updated_at": "2026-10-25T09:46:09.037306",
            "owner": {"id": "pgprobe", "round": 1, "pid": 71708},
            "lease": {"owner": "pgprobe", "owner_pid": 71708},
            "services": [
                {
                    "name": "api",
                    "port": 53749,
                    "declared_port": 53749,
                    "pid": 71712,
                    "pgid": 71712,
                    "start_epoch": 1792892763.0562649,
                    "start_cmd": "set -e\nsource backend/.venv/bin/activate\n",
                    "cwd": ".",
                    "log_path": "/tmp/s.log",
                    "origin": "spawned",
                    "ready": True,
                    "reap_on_exit": True,
                    "owner": "pgprobe",
                    "owner_round": 1,
                    "owner_pid": 71708,
                    "plan_id": "pgprobe",
                    "round": 1,
                }
            ],
        },
    )

    record = ledger.live_records(tmp_path)[71712]
    assert record.role == "vp_service"
    assert record.plan_id == "pgprobe"
    # The pid that started the service is kept, and its role is the
    # backend's — the backend is what wrote the file.
    assert record.owner_pid == 71708
    assert record.owner_role == "backend"
    assert record.create_time == 1792892763.0562649
    # A multi-line shell script is one command, not a list of words.
    assert record.cmdline == (
        "set -e\nsource backend/.venv/bin/activate\n",
    )
