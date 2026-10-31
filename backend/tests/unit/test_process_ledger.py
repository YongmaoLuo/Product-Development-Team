"""The ledger is the only durable truth about which pid belongs to whom.

Why these tests exist
---------------------
The backend spawns a four-layer process tree and every layer detaches.
The in-memory handles die with the backend process, so after a restart
the only thing that can still answer "which pid did this plan start,
and is it still alive" is what was written to disk *at spawn time*.

That makes the ledger a correctness-critical file rather than a log:

* it must be **append-only** — a torn tail line from a crash must not
  cost the events that came before it;
* it must be **durable before it is relied upon** — a spawn recorded
  but not flushed is a process nobody owns, which is precisely the
  failure the ledger exists to prevent;
* it must be **readable without exploding** — a corrupt line is a
  local accident, not a reason to lose the whole ledger;
* and it must distinguish "this pid is gone" from "this pid was never
  ours" — the second must not silently make the first look clean.

Each test below pins one of those.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import process_ledger as ledger  # noqa: E402


def _record(**overrides) -> ledger.ProcessRecord:
    """A record with every field spelled out, so overrides are explicit."""
    kwargs = dict(
        plan_id="p1",
        role="executor",
        pid=200,
        pgid=200,
        create_time=1700000000.0,
        cmdline=["python", "cli.py"],
        cwd="/tmp/p1",
        owner_pid=100,
        owner_role="backend",
    )
    kwargs.update(overrides)
    return ledger.ProcessRecord(**kwargs)


# ---------------------------------------------------------------------------
# The happy path: spawn is recorded, and the record is live.
# ---------------------------------------------------------------------------
def test_record_spawn_then_live_records_contains_it(tmp_path):
    rec = _record()
    ledger.record_spawn(tmp_path, rec)

    live = ledger.live_records(tmp_path)
    assert 200 in live
    assert live[200].role == "executor"
    assert live[200].pgid == 200
    assert live[200].plan_id == "p1"
    assert live[200].owner_pid == 100
    assert live[200].owner_role == "backend"
    assert live[200].create_time == 1700000000.0
    assert live[200].cwd == "/tmp/p1"
    # cmdline is normalised to a tuple so a caller cannot mutate the
    # ledger's copy of it through the object it handed in.
    assert live[200].cmdline == ("python", "cli.py")

    events = ledger.read_events(tmp_path)
    assert [e["event"] for e in events] == ["spawn"]


# ---------------------------------------------------------------------------
# An exit removes the pid from the live set.
# ---------------------------------------------------------------------------
def test_record_exit_removes_from_live_records(tmp_path):
    ledger.record_spawn(tmp_path, _record())
    assert 200 in ledger.live_records(tmp_path)

    assert ledger.record_exit(tmp_path, 200, "exit", exit_code=0) is True

    assert ledger.live_records(tmp_path) == {}
    exit_events = [
        e for e in ledger.read_events(tmp_path) if e["event"] == "exit"
    ]
    assert len(exit_events) == 1
    assert exit_events[0]["pid"] == 200
    assert exit_events[0]["exit_code"] == 0


# ---------------------------------------------------------------------------
# A missing ledger is an empty ledger, never an exception.
# ---------------------------------------------------------------------------
def test_missing_ledger_reads_as_empty(tmp_path):
    plan_dir = tmp_path / "no-such-plan"
    assert ledger.read_events(plan_dir) == []
    assert ledger.live_records(plan_dir) == {}


# ---------------------------------------------------------------------------
# A role outside the whitelist is a programming error and says so.
# ---------------------------------------------------------------------------
def test_invalid_role_raises(tmp_path):
    with pytest.raises(ValueError):
        _record(role="not_a_role")

    # The guard is not only at construction: a caller that mutated the
    # role afterwards must not be able to smuggle an unknown role into
    # the ledger either.
    rec = _record()
    rec.role = "not_a_role"
    with pytest.raises(ValueError):
        ledger.record_spawn(tmp_path, rec)
    assert ledger.read_events(tmp_path) == []


# ---------------------------------------------------------------------------
# A corrupt line is skipped; the rest of the ledger still reads.
# ---------------------------------------------------------------------------
def test_corrupt_line_is_skipped_not_fatal(tmp_path):
    ledger.record_spawn(tmp_path, _record())
    path = tmp_path / ledger.LEDGER_FILENAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write("\n")
    ledger.record_spawn(tmp_path, _record(pid=201, pgid=201))

    events = ledger.read_events(tmp_path)
    assert [e["event"] for e in events] == ["spawn", "spawn"]

    live = ledger.live_records(tmp_path)
    assert sorted(live) == [200, 201]


# ---------------------------------------------------------------------------
# Exiting a pid we never spawned is reported as such — and still logged.
# ---------------------------------------------------------------------------
def test_record_exit_unknown_pid_returns_false(tmp_path):
    assert ledger.record_exit(tmp_path, 999, "exit", exit_code=1) is False

    events = ledger.read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["event"] == "exit"
    assert events[0]["pid"] == 999
    assert ledger.live_records(tmp_path) == {}


# ---------------------------------------------------------------------------
# The two remaining boundary conditions of the ledger contract.
# ---------------------------------------------------------------------------
def test_respawn_of_same_pid_overwrites_live_record_but_keeps_both_events(
    tmp_path,
):
    ledger.record_spawn(tmp_path, _record(cwd="/tmp/first"))
    ledger.record_spawn(tmp_path, _record(cwd="/tmp/second"))

    events = ledger.read_events(tmp_path)
    assert [e["event"] for e in events] == ["spawn", "spawn"]

    live = ledger.live_records(tmp_path)
    assert list(live) == [200]
    assert live[200].cwd == "/tmp/second"


def test_spawn_appends_one_json_line_per_call(tmp_path):
    ledger.record_spawn(tmp_path, _record())
    ledger.record_spawn(tmp_path, _record(pid=201, pgid=201))

    lines = (tmp_path / ledger.LEDGER_FILENAME).read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 2
    # Every line is a complete JSON object on its own — the ledger is
    # JSONL, so a reader can stop at the first bad line and keep the
    # ones before it.
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_ledger_filename_and_roles_are_the_contracted_constants():
    assert ledger.LEDGER_FILENAME == "process_ledger.jsonl"
    assert ledger.ROLES == (
        "executor",
        "repair_executor",
        "subagent",
        "vp_service",
        "tool",
    )