"""The VP service ledger is the unified process ledger.

Why these tests exist
---------------------
The backend used to keep two ledgers: ``managed_services.json`` — one
whole-file dict, written by :mod:`service_manager` — and, since
2026-10-30, ``process_ledger.jsonl`` — the append-only log every layer
of the process tree writes to. Two ledgers mean two answers to "which
pid does this plan own", and the sweep that kills orphans only knows
one of them.

The switch makes the unified ledger the only file. A service is
recorded as a ``vp_service`` spawn record, and the view the old
callers expect is *rebuilt* from that ledger rather than stored beside
it. Rebuilding is the whole point: a view that is stored twice drifts
from the records it claims to describe, and the drift is what makes a
sweep kill the wrong process.

Each test below pins one property that makes the switch safe:

* the record is a ledger record — the reaper can act on it without
  knowing anything about services;
* the view is rebuilt faithfully — the old callers keep working;
* nothing is written when there is nothing to record;
* re-recording the same pid leaves one owner, not two;
* a plan started before the switch still reads back its services.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import process_ledger as ledger  # noqa: E402
import service_manager as sm  # noqa: E402


def _runtime(**overrides) -> sm.ServiceRuntime:
    """A runtime as :func:`service_manager.start_service` leaves it."""
    kwargs = dict(
        name="web",
        port=3000,
        pid=200,
        pgid=200,
        start_epoch=1700000000.0,
        start_cmd="npx next dev -p 3000",
        cwd="/tmp/p1",
        origin="spawned",
        ready=True,
    )
    kwargs.update(overrides)
    return sm.ServiceRuntime(**kwargs)


def _record(*runtimes, plan_dir: Path, plan_id: str = "p1",
            round_number: int = 0):
    """Write ``runtimes`` into the ledger, the way a round does."""
    runtime_map = sm.ServiceRuntimeMap(
        runtimes={r.name: r for r in runtimes},
    )
    return sm.record_runtimes(
        plan_dir, plan_id, runtime_map,
        project_dir=plan_dir, round_number=round_number,
    )


# ---------------------------------------------------------------------------
# The write path: a service is a ``vp_service`` record in the one ledger.
# ---------------------------------------------------------------------------
def test_record_runtimes_appends_vp_service_records(tmp_path: Path):
    runtime = _runtime()

    path = _record(runtime, plan_dir=tmp_path)

    assert path is not None
    # The ledger is the unified one — the same file the executor and the
    # sub-agents write to, not a service ledger of its own.
    assert sm.LEDGER_FILENAME == "process_ledger.jsonl"
    assert path == tmp_path / "process_ledger.jsonl"
    assert sm.ledger_path(tmp_path) == path

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "one service, one record"
    event = json.loads(lines[0])
    assert event["event"] == "spawn"
    assert event["role"] == "vp_service"
    assert event["pid"] == 200

    # The payload is a ledger record, so the reaper can act on it
    # without knowing anything about services.
    record = event["record"]
    assert record["role"] == "vp_service"
    assert record["pid"] == 200
    assert record["pgid"] == 200
    assert record["plan_id"] == "p1"
    assert record["owner_role"] == "backend"

    # And the unified fold sees it as a live process of this plan.
    live = ledger.live_records(tmp_path)
    assert live[200].role == "vp_service"
    assert live[200].owner_role == "backend"


def test_empty_runtimes_writes_no_file(tmp_path: Path):
    # An adopted listener is not ours: recording it would claim
    # ownership of a process the backend did not start.
    adopted = _runtime(name="adopted", port=8080, pid=22, origin="adopted")

    assert _record(adopted, plan_dir=tmp_path) is None
    assert not sm.ledger_path(tmp_path).exists()
    assert not (tmp_path / "managed_services.json").exists()


def test_rerecord_same_pid_does_not_duplicate(tmp_path: Path):
    runtime = _runtime()

    _record(runtime, plan_dir=tmp_path)
    _record(runtime, plan_dir=tmp_path)

    # The ledger is append-only: the second record does not overwrite
    # the first one, it is appended after it.
    lines = sm.ledger_path(tmp_path).read_text(
        encoding="utf-8",
    ).strip().splitlines()
    assert len(lines) == 2

    # One pid, one owner: the fold collapses the duplicate.
    assert sorted(ledger.live_records(tmp_path)) == [200]
    view = sm.read_ledger(tmp_path)
    assert [s["pid"] for s in view["services"]] == [200]


# ---------------------------------------------------------------------------
# The read path: the view is rebuilt from the ledger.
# ---------------------------------------------------------------------------
def test_read_ledger_rebuilds_services_view(tmp_path: Path):
    runtime = _runtime()

    _record(runtime, plan_dir=tmp_path)

    # The view is rebuilt, not stored: what is on disk is the event
    # log, and the entries come back out of it.
    raw = json.loads(
        sm.ledger_path(tmp_path).read_text(
            encoding="utf-8",
        ).strip().splitlines()[0],
    )
    assert raw["event"] == "spawn"

    view = sm.read_ledger(tmp_path)
    assert sorted(view) == ["services"], (
        "the view is the service list, not the event that carried it"
    )
    assert [s["name"] for s in view["services"]] == ["web"]
    entry = view["services"][0]
    assert entry["pid"] == 200
    assert entry["port"] == 3000
    assert entry["pgid"] == 200
    assert entry["start_cmd"] == "npx next dev -p 3000"
    assert entry["reap_on_exit"] is True


def test_read_ledger_reads_a_pre_migration_plan(tmp_path: Path):
    """A plan started before the switch still has the retired file."""
    (tmp_path / "managed_services.json").write_text(
        json.dumps({
            "plan_id": "p1",
            "services": [
                {"name": "web", "port": 3000, "pid": 200, "pgid": 200},
            ],
        }),
        encoding="utf-8",
    )

    view = sm.read_ledger(tmp_path)

    assert [s["name"] for s in view["services"]] == ["web"]
    assert view["services"][0]["pid"] == 200


def test_read_ledger_of_a_damaged_file_is_empty(tmp_path: Path):
    sm.ledger_path(tmp_path).write_text("{not json", encoding="utf-8")

    assert sm.read_ledger(tmp_path) == {}


# ---------------------------------------------------------------------------
# The sweep: a plan is found by the ledger it has.
# ---------------------------------------------------------------------------
def test_reap_all_plans_sweeps_a_pre_migration_plan(tmp_path: Path):
    """The sweep looks for the retired file name too.

    The file the service ledger used to live in is still the only
    ledger a plan started before the switch has. A sweep that stopped
    looking for it would leave that plan's services running for ever.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / "p1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "managed_services.json").write_text(
        json.dumps({
            "plan_id": "p1",
            "services": [
                {
                    "name": "web", "port": port, "pid": 4242, "pgid": 4242,
                    "start_epoch": 1.0, "start_cmd": "true",
                    "reap_on_exit": True,
                },
            ],
        }),
        encoding="utf-8",
    )

    reports = sm.reap_all_plans(plans_dir, reason="startup_sweep")

    assert [r.plan_id for r in reports] == ["p1"]
    assert reports[0].reaped == ["web"]
