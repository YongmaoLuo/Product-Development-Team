"""Tests for the data-plane **identity** gate: who serves, and what file.

Background
----------
The 2026-11-19 verification round (``20261118-plan-mode-production``) was
blocked in full by the data-plane identity gate, on two independent
defects:

  * **the wrong baseline** -- the record phase resolved *the backend's
    own* state database and compared every service against it, although
    the round's api service declared
    ``PDT_STATE_DB_PATH=$PWD/tmp/pdt-verify/state.db`` (a fixture with
    2 rows) in its ``instance`` block.  The record phase therefore
    reported a ``data_plane_fixture_mismatch`` that was not one;
  * **a mutable number used as an identity** -- the guard's main test
    is the row count, so a service that legitimately **added** a row
    through its own import API (2 -> 3) was reported as "another data
    plane", and the round never even reached the immutable identity
    check (boot id + database inode).

The identity of a data plane is **boot id + the inode of the database
file**.  The row count is mutable state.  These tests pin the repaired
behaviour:

  1. the record phase reads the expected count from the database the
     service **itself** declares, and reports no mismatch when the
     service answers with that database's count;
  2. the record phase still reports a **real** contradiction -- the
     service answers with a count that is not the one in the database
     it declared -- and then stops carrying the expectation;
  3. the guard lets a row-count **growth** through when the instance
     and the database file are the same, and follows the new count;
  4. a growth on **another** database file is still blocked (negative
     control);
  5. a growth with **no recorded instance** is still blocked (negative
     control: the old behaviour is unchanged);
  6. the re-ensure calls a grown-but-same-file instance **restored**.

No network, no real port: ``_health_data_instance`` and
``probe_data_plane`` are stubbed.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import config_paths  # noqa: E402
import service_freshness  # noqa: E402
import verification_agent  # noqa: E402
from service_freshness import DataPlaneFingerprint  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402


#: The event the record phase writes when the instance contradicts the
#: database it declared.
MISMATCH_EVENT = "data_plane_fixture_mismatch"

#: The event the guard writes when the row count grew on the same
#: instance reading the same file.
GROWTH_EVENT = "data_plane_state_growth"

#: The reason code a blocked data-plane verdict carries.
BLOCK_REASON_CODE = "service_data_plane_mismatch"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_db(path: Path, rows: int) -> Path:
    """A state database with *rows* ``plan_routing`` rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE plan_routing (id INTEGER PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO plan_routing (id) VALUES (?)",
            [(i,) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _probe_returning(rows: int):
    """A ``probe_data_plane`` that answers with one fixed count.

    The real probe carries the expected count it was handed on the
    fingerprint, so the stub does the same.
    """
    def _probe(port, expected_rows=None):
        return DataPlaneFingerprint(
            port=port, pid=4242, routing_rows=rows,
            expected_rows=expected_rows, status="ok",
        )
    return _probe


def _record_agent(events: list, declarations: dict, instances: dict):
    """An ``Orchestrator`` with only what the record phase touches."""
    agent = Orchestrator.__new__(Orchestrator)
    agent._service_declarations = {}
    agent._data_plane_fingerprints = {}
    agent._service_instance_identity = {}
    agent._effective_declarations = lambda: {
        name: SimpleNamespace(port=port)
        for name, port in declarations.items()
    }

    def _record(name, port):
        key = str(name).lower()
        instance = (instances or {}).get(key)
        if instance is None:
            agent._service_instance_identity.pop(key, None)
        else:
            agent._service_instance_identity[key] = dict(instance)

    agent._record_service_instance_identity = _record
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    return agent


def _guard_agent(events: list, decl, recorded, observed_rows: int):
    """An ``Orchestrator`` with only what the data-plane guard touches."""
    agent = Orchestrator.__new__(Orchestrator)
    agent._service_declarations = {"svc": decl}
    agent._data_plane_fingerprints = {"svc": recorded}
    agent._service_instance_identity = {}
    agent._observed_instances = {}
    agent._data_plane_reensure_spent = set()
    agent._service_unreachable = {}
    agent._service_runtime_map = None
    agent._vp_service_targets = lambda vp: {"svc": decl}

    async def _probe(decl_arg, recorded_arg):
        return DataPlaneFingerprint(
            port=int(decl_arg.port), pid=4242, routing_rows=observed_rows,
            expected_rows=getattr(recorded_arg, "expected_rows", None),
            status="ok",
        )

    agent._probe_data_plane = _probe
    agent._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append(
            (vp_id, event_type, data)
        )
    )
    # The re-ensure machinery is not what these tests are about: the
    # services are stubbed, so the re-ensure can only fail.
    agent._ensure_declared_services = lambda services: {}
    agent._adopt_reensure_runtimes = lambda vp_id, runtime_map: {}
    agent._refresh_resolved_service_placeholders = lambda: None
    agent._effective_declarations = lambda: {"svc": decl}
    return agent


def _instance(boot_id: str, inode: int, db_path: str = "") -> dict:
    """An ``instance`` block as a service publishes it."""
    block = {"boot_id": boot_id, "pid": 4242, "state_db_inode": inode}
    if db_path:
        block["state_db_path"] = db_path
    return block


# ---------------------------------------------------------------------------
# 1. The record phase reads the database the service declares
# ---------------------------------------------------------------------------


def test_the_record_phase_reads_the_database_the_service_declares(
    tmp_path: Path, monkeypatch,
):
    """The expected count comes from the service's **own** database.

    The backend's own database holds 49 rows; the fixture the round's
    service really serves holds 7.  The record phase has to compare
    against the **7** -- otherwise it declares a mismatch that is not
    one, and blocks the whole round on it.
    """
    backend_db = _make_db(tmp_path / "backend.db", 49)
    service_db = _make_db(tmp_path / "service.db", 7)

    events: list = []
    agent = _record_agent(
        events, {"svc": 8137},
        {"svc": _instance("boot-a", 11, str(service_db))},
    )

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: backend_db,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(7),
    )

    fingerprints = agent._capture_data_plane_fingerprints()

    assert fingerprints["svc"].expected_rows == 7
    assert [
        event for event in events if event[1] == MISMATCH_EVENT
    ] == [], "the service's own database was reported as a mismatch"


# ---------------------------------------------------------------------------
# 2. The record phase still reports a real contradiction
# ---------------------------------------------------------------------------


def test_the_record_phase_still_reports_a_real_contradiction(
    tmp_path: Path, monkeypatch,
):
    """The instance answers with a count its own database does not hold.

    That is a genuine contradiction -- the service names one database
    and serves another -- and the record phase has to say so, once,
    naming the file it read.
    """
    backend_db = _make_db(tmp_path / "backend.db", 49)
    service_db = _make_db(tmp_path / "service.db", 7)

    events: list = []
    agent = _record_agent(
        events, {"svc": 8137},
        {"svc": _instance("boot-a", 11, str(service_db))},
    )

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: backend_db,
    )
    monkeypatch.setattr(
        service_freshness, "probe_data_plane", _probe_returning(9),
    )

    fingerprints = agent._capture_data_plane_fingerprints()

    mismatches = [event for event in events if event[1] == MISMATCH_EVENT]
    assert len(mismatches) == 1, mismatches
    _vp_id, _event_type, data = mismatches[0]
    assert data["service"] == "svc"
    assert data["db_path"] == str(service_db)
    assert data["expected_rows"] == 7
    assert data["reported_rows"] == 9
    # The contradiction is not carried on the fingerprint any more: every
    # later comparison falls back to "recorded value vs observed value".
    assert fingerprints["svc"].expected_rows is None


# ---------------------------------------------------------------------------
# 3. The guard accepts a growth on the same instance and the same file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_guard_accepts_a_growth_on_the_same_instance(
    monkeypatch,
):
    """The count grew, the instance and the file did not: let it through.

    The service added a row through its own import API.  The boot id
    and the database inode are the same, so this is a **state change**,
    not another data plane -- and the round's fingerprint follows the
    new count.
    """
    decl = SimpleNamespace(port=8137)
    recorded = DataPlaneFingerprint(
        port=8137, pid=4242, routing_rows=2, expected_rows=None, status="ok",
    )
    events: list = []
    agent = _guard_agent(events, decl, recorded, observed_rows=3)
    agent._service_instance_identity = {
        "svc": _instance("boot-a", 777),
    }
    monkeypatch.setattr(
        verification_agent, "_health_data_instance",
        lambda port: _instance("boot-a", 777),
    )

    verdict = await agent._guard_api_test_data_plane("VP-1", {})

    assert verdict is None, verdict
    assert agent._data_plane_fingerprints["svc"].routing_rows == 3
    growths = [event for event in events if event[1] == GROWTH_EVENT]
    assert len(growths) == 1, events
    assert growths[0][0] == "VP-1"
    assert growths[0][2]["service"] == "svc"
    assert growths[0][2]["recorded"]["routing_rows"] == 2
    assert growths[0][2]["observed"]["routing_rows"] == 3
    # The mismatch event must **not** have been written: the round did
    # not block.
    assert [
        event for event in events if event[1] == "data_plane_mismatch"
    ] == []


# ---------------------------------------------------------------------------
# 4. Negative control: another database file is still a mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_growth_on_another_database_file_is_still_blocked(
    monkeypatch,
):
    """Same boot id, **other** file: the old blocking behaviour stands.

    The inode is what says "same database"; a different inode is a
    different file, i.e. another data plane.
    """
    decl = SimpleNamespace(port=8137)
    recorded = DataPlaneFingerprint(
        port=8137, pid=4242, routing_rows=2, expected_rows=None, status="ok",
    )
    events: list = []
    agent = _guard_agent(events, decl, recorded, observed_rows=3)
    agent._service_instance_identity = {
        "svc": _instance("boot-a", 777),
    }
    monkeypatch.setattr(
        verification_agent, "_health_data_instance",
        lambda port: _instance("boot-a", 888),
    )

    verdict = await agent._guard_api_test_data_plane("VP-1", {})

    assert verdict is not None, "the growth on another file was let through"
    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("reason_code") == BLOCK_REASON_CODE, evidence
    assert [
        event for event in events if event[1] == GROWTH_EVENT
    ] == []


# ---------------------------------------------------------------------------
# 5. Negative control: no recorded instance -> the old behaviour stands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_growth_without_a_recorded_instance_is_still_blocked(
    monkeypatch,
):
    """Nothing recorded, nothing to compare: the round keeps blocking.

    "Not comparable" is not "comparable and fine".  A service the round
    never recorded an instance for is judged the way it was before.
    """
    decl = SimpleNamespace(port=8137)
    recorded = DataPlaneFingerprint(
        port=8137, pid=4242, routing_rows=2, expected_rows=None, status="ok",
    )
    events: list = []
    agent = _guard_agent(events, decl, recorded, observed_rows=3)
    agent._service_instance_identity = {}
    monkeypatch.setattr(
        verification_agent, "_health_data_instance",
        lambda port: _instance("boot-a", 777),
    )

    verdict = await agent._guard_api_test_data_plane("VP-1", {})

    assert verdict is not None, "the growth without a baseline was let through"
    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("reason_code") == BLOCK_REASON_CODE, evidence
    assert [
        event for event in events if event[1] == GROWTH_EVENT
    ] == []


# ---------------------------------------------------------------------------
# 6. The re-ensure calls a grown-but-same-file instance restored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reensure_calls_a_grown_instance_restored(
    tmp_path: Path, monkeypatch,
):
    """The service came back with more rows in the **same** file.

    The re-ensure's verdict has to agree with the guard's: same
    database file, more rows -> restored.
    """
    decl = SimpleNamespace(port=8137)
    recorded = DataPlaneFingerprint(
        port=8137, pid=4242, routing_rows=2, expected_rows=2, status="ok",
    )
    events: list = []
    agent = _guard_agent(events, decl, recorded, observed_rows=3)
    agent._service_instance_identity = {
        "svc": _instance("boot-a", 777),
    }
    monkeypatch.setattr(
        verification_agent, "_health_data_instance",
        lambda port: _instance("boot-a", 777),
    )
    # The backend's own database is not part of the expectation here.
    monkeypatch.setattr(
        config_paths, "resolve_state_db_path",
        lambda: tmp_path / "does-not-exist.db",
    )

    fingerprint, restored = await agent._recover_data_plane_instance(
        "VP-1", "svc", decl, recorded,
    )

    assert fingerprint is not None
    assert fingerprint.routing_rows == 3
    assert restored is True, "the grown instance was not called restored"
