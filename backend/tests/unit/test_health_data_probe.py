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


# ---------------------------------------------------------------------------
# 2026-10-24 — the round's data-plane acceptance criterion (VP-002)
# ---------------------------------------------------------------------------
#
# ``/health/data`` has to read **this round's fixture**: four
# ``plan_routing`` rows, one per plan of the acceptance set.  A probe
# that answered "0 rows" would be a false pass — every later
# phase/plan assertion would then have run against an empty database,
# which is exactly what the production round did.
#
# The request goes over a real socket, through the same HTTP path an
# ``api_test`` VP uses, because that is the path the verification
# agent's data-plane identity check rides on: a probe that took a
# different route could answer a different question than the VP's
# request does.


def test_vp_002_health_data_reports_fixture_routing_rows(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from service_freshness import probe_data_plane
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    # The fixture: four routing rows, one per plan of the acceptance set.
    db = tmp_path / "state.db"
    conn = open_db(db)
    try:
        migrate(conn)
        for index in range(4):
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES (?, 'interview', '2026-10-24T00:00:00Z')",
                (f"fixture-plan-{index}",),
            )
        conn.commit()
    finally:
        conn.close()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's API
            if self.path.split("?")[0] != "/health/data":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            result = server.health_data_check()
            body = getattr(result, "body", None)
            if body is None:
                status = 200
                body = json.dumps(result).encode("utf-8")
            else:
                status = int(getattr(result, "status_code", 200))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A003 - silence the access log
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(httpd.server_address[1])
        fingerprint = probe_data_plane(port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert fingerprint.answered, fingerprint.to_dict()
    assert fingerprint.status == "ok", fingerprint.to_dict()
    assert fingerprint.routing_rows == 4, fingerprint.to_dict()


# ---------------------------------------------------------------------------
# 2026-10-25 — the instance block: WHICH process answered (VP-006)
# ---------------------------------------------------------------------------
#
# ``/health/data`` used to answer with one fact about the data plane:
# how many ``plan_routing`` rows it could read.  That is enough to tell
# a broken database from a working one — and not enough to tell WHOSE
# answer it is.
#
# The 2026-10-24 round hit exactly that gap: a probe reached a backend
# on a busy port, that backend answered 200 with ``status: ok`` and an
# empty routing table, and the round could not say whether the answer
# came from its own instance or from the stranger that had taken the
# port.  The probe now answers the identity question too: an
# ``instance`` block naming the process, the tree it serves and the
# database it reads.
#
# Comparing ``plans_dir`` and ``state_db_path`` against the ones the
# round configured is what settles "is this the round's instance?" — and
# the plan ids explain an empty data plane instead of leaving it as a
# mystery count.


class TestHealthDataInstanceBlock:
    """The probe must say which process answered.

    The first four names are the gate names for this criterion; the
    fifth pins the degraded case, where the identity matters most.
    """

    def test_health_data_reports_instance_identity(self):
        import os

        resp = _client().get("/health/data")

        assert resp.status_code == 200, resp.text
        instance = resp.json()["instance"]
        assert instance["pid"] == os.getpid()
        assert instance["pgid"] == os.getpgid(0)
        assert instance["cwd"] == os.getcwd()
        assert instance["boot_id"]
        assert instance["started_at"]
        # The two paths are the identity a round compares against the
        # ones it configured: the plans root served, and the database
        # read.
        assert instance["plans_dir"] == str(Path(server.PLANS_DIR))
        assert instance["state_db_path"] == str(server._state_db_path())

    def test_instance_identity_stable_across_requests(self):
        """Two answers, one instance.

        A field that changed between two requests would make every
        comparison of two answers meaningless — so the whole block is
        the same, byte for byte, when nothing about the process
        changed.
        """
        first = _client().get("/health/data").json()["instance"]
        second = _client().get("/health/data").json()["instance"]

        assert first == second, (
            f"the instance block must be the same for two requests; got "
            f"{first!r} against {second!r}"
        )

    def test_identity_reflects_real_data_plane(self):
        """The block reports the data plane it really reads.

        The count and the plan ids are read from the database this
        process serves, not from anything the request said — so an
        empty data plane can be told apart from a foreign one: the
        first is a count of zero, the second is a count of zero too but
        with somebody else's paths beside it.
        """
        import os

        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate

        db = server._state_db_path()
        conn = open_db(db)
        try:
            migrate(conn)
            for index in range(4):
                conn.execute(
                    "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                    "VALUES (?, 'interview', '2026-10-25T00:00:00Z')",
                    (f"fixture-plan-{index}",),
                )
            conn.commit()
        finally:
            conn.close()

        resp = _client().get("/health/data")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        instance = body["instance"]
        assert instance["plan_routing_rows"] == 4
        assert instance["fixture_plan_ids"] == [
            f"fixture-plan-{index}" for index in range(4)
        ]
        # The inode pins WHICH file was read: another file at the same
        # path is another data plane.
        assert instance["state_db_inode"] == os.stat(db).st_ino
        # The field that was there before keeps its own meaning.
        assert body["plan_routing_rows"] == 4

    def test_identity_changes_after_restart(self):
        """A restart is a new instance, so it gets a new boot id.

        The boot id is what tells "the same instance answered twice"
        apart from "an instance answered, and then another one
        answered".  A restart that kept the old id would make a stale
        answer look like a live one.

        The restart is a real one: a second interpreter imports the
        module and asks for the identity it minted.
        """
        import os
        import subprocess

        probe = "import server; print(server._INSTANCE_BOOT_ID)"
        # The module is imported flat (``backend/`` on the path) and it
        # reaches into the package (``backend.framework.prompts``), so
        # the fresh interpreter needs BOTH roots — the same two the
        # test run itself is given.
        python_path = os.pathsep.join(
            [str(BACKEND_DIR.parent), str(BACKEND_DIR)]
            + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(BACKEND_DIR),
            env={**os.environ, "PYTHONPATH": python_path},
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, (
            f"a fresh interpreter must be able to import the module; got "
            f"{result.returncode}: {result.stderr[-2000:]!r}"
        )
        restarted_boot_id = result.stdout.strip().splitlines()[-1]
        assert restarted_boot_id, f"the restarted process said nothing: {result.stdout!r}"
        assert restarted_boot_id != server._INSTANCE_BOOT_ID, (
            f"a restarted process must not keep the previous boot id; got "
            f"{restarted_boot_id!r} against {server._INSTANCE_BOOT_ID!r}"
        )

    def test_instance_block_is_present_when_the_data_plane_is_degraded(self):
        """A broken data plane still says WHO is broken.

        The counts are unknown then, and the answer says so rather than
        inventing a zero — but the identity beside them is exactly what
        a reader needs at that moment.
        """
        import os

        db = server._state_db_path()
        db.write_bytes(b"this is not a sqlite database at all")

        resp = _client().get("/health/data")

        assert resp.status_code == 503, resp.text
        body = resp.json()
        instance = body["instance"]
        assert instance["pid"] == os.getpid()
        assert instance["boot_id"]
        assert instance["plans_dir"] == str(Path(server.PLANS_DIR))
        assert instance["plan_routing_rows"] is None
        assert instance["fixture_plan_ids"] is None
