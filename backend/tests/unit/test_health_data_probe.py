"""Tests for the ``/health/data`` data-plane probe (2026-09-23).

Backstory
---------
The backend server once answered ``/health`` with 200 while every data
endpoint returned 500 ``database disk image is malformed`` — the
process was alive, its SQLite connections were wedged, and there was
no probe that could tell the difference.  The supervisor kept reporting
the server "running" and the scheduler's card said "未运行" for 40
minutes while the real fault went unexamined.

:func:`server.health_data_check` closes that gap: it exercises the
exact read path a real request depends on and answers 503 ``degraded``
when the data plane is broken.  These tests pin the three states.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: E402


def _client():
    from fastapi.testclient import TestClient

    return TestClient(server.app)


class TestHealthDataProbe:
    """The probe must distinguish healthy / fresh-install / broken."""

    def test_healthy_database_reports_ok(self, tmp_path):
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate

        db = tmp_path / "state.db"
        conn = open_db(db)
        try:
            migrate(conn)
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES ('p1', 'interview', '2026-09-23T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

        resp = _client().get("/health/data")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["plan_routing_rows"] == 1

    def test_fresh_install_reports_ok(self, tmp_path):
        """No state.db yet is a legitimate state, not a degradation."""
        resp = _client().get("/health/data")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"

    def test_unreadable_database_reports_degraded_not_a_500(self, tmp_path):
        """The probe must convert a broken DB into a clean 503, not blow up."""
        db = tmp_path / "state.db"
        db.write_bytes(b"this is not a sqlite database at all")

        resp = _client().get("/health/data")

        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body["status"] == "degraded"
        assert "reason" in body and body["reason"]

    def test_process_liveness_endpoint_stays_unaffected(self, tmp_path):
        """/health keeps answering 200 even when the data plane is broken.

        Deliberate: a load balancer / startup probe must not flap on a
        data-plane fault.  The distinction between "process up" and
        "data plane up" is the whole point of having two endpoints.
        """
        db = tmp_path / "state.db"
        db.write_bytes(b"garbage")

        resp = _client().get("/health")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok"}
