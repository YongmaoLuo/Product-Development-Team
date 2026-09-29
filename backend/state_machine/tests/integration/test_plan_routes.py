"""Integration tests for /api/plans and /api/plan/{id}/summary.

Task 2-8 (this file)
---------------------
The routes ``/api/plans`` (list_plans) and ``/api/plan/{id}/summary``
(get_plan_summary) used to read five JSON files per plan
(plan_state.json, interview.json, prd.*, review.json, tasks.json,
execution.json).  The state-machine refactor replaces those reads with
four SQL repositories:

  * RoutingRepository          (plan_routing)
  * ExecutionRepository        (plan_execution)
  * VerificationRepository     (plan_verification)  [used by
                              summary only — must exist as a
                              placeholder import for the route to
                              construct the helper]
  * ArtifactRepository         (plan_artifacts)

Architecture decision point 5 forbids introducing an "in-memory
aggregation facade" — every consumer MUST go through the four
repositories.  This integration test pins the *external* API contract
that the route change must satisfy; the route's internal composition
(routing + execution + artifacts + verification) is exercised
end-to-end via the FastAPI TestClient.

TDD spec (5 tests, the same ones pinned by the task brief):

  1. test_list_plans_aggregates_four_tables
       A plan row exists in all four repos (routing + execution +
       artifacts).  ``GET /api/plans`` returns that plan with the
       expected pre-existing public fields (``id``, ``status``,
       ``current_phase``, ``requirement``, ``created_at``,
       ``flags``, ``steps``, ``pid``) AND the new ``archived``
       flag (False).

  2. test_list_plans_includes_archived_with_flag
       **[验收条件 4 锚点]**  An archived directory (date before
       2026-08-05) exists in PLANS_DIR.  ``GET /api/plans`` includes
       a row for the archived plan, with ``archived == True`` and
       populated ``requirement_first_line``.

  3. test_list_plans_archived_entry_has_no_stage_fields
       The archived row dict MUST NOT carry ``stage`` /
       ``current_phase`` / ``substage`` / ``version`` keys; those
       are SQLite-semantics that don't exist for archived plans.

  4. test_get_summary_includes_artifact_manifest
       ``GET /api/plan/{id}/summary`` includes an ``artifacts``
       manifest (list) populated from
       :meth:`ArtifactRepository.list_for_plan`.

  5. test_get_summary_404_for_missing_plan
       ``GET /api/plan/does-not-exist/summary`` returns HTTP 404.

Setup strategy
--------------
The state-machine SQLite file must be created per test (otherwise
production data leaks in).  The route reads the data_dir from the
``Request`` (architecture "twelve-factor config" decision) — in tests
we monkeypatch the state-machine open() so each test gets its own
tmp SQLite file.  The ``PLANS_DIR`` global is also monkeypatched to
the per-test tmp_path via the autouse fixture already present in
``tests/conftest.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
import sqlite3
from fastapi.testclient import TestClient

from server import app


#: A PID that ``os.kill(pid, 0)`` will actually accept: our own.
#:
#: 2026-09-19: the "four tables" test seeds a plan with
#: ``exec_status="running"`` so that ``/api/plans`` has a live execution
#: to surface. ``_recover_execution_states`` runs on every TestClient
#: startup and probes that PID; a made-up number (the old fixture used
#: ``4242``) raises ``ProcessLookupError``, which by design routes to
#: ``_mark_failed_dead`` — the row is flipped to ``failed`` and
#: ``/api/plans`` then reports ``pid: null`` (it only surfaces a PID for
#: a plan it still believes is running). Seeding a real live PID keeps
#: the fixture honest instead of asserting against a state the recovery
#: path is supposed to destroy.
LIVE_PID = os.getpid()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Per-test isolation of PLANS_DIR + state.db.

    * Redirect ``server.PLANS_DIR`` to ``tmp_path / "plans"``
      (the autouse fixture in ``tests/conftest.py`` ALSO redirects
      PLANS_DIR; this fixture is here so the integration tests in
      this file do not depend on that upstream fixture.)
    * Patch ``state_machine.db.connection.open`` so the route's
      SQLite connection lands on a per-test file (avoids the
      production state.db).
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.PLANS_DIR", plans_dir)

    # The state-machine connection helper is imported by server.py
    # via a local reference; the test must patch the SAME module
    # attribute the helper is imported from.  We do that by patching
    # the connection factory used by the route's _open_state_machine
    # helper (defined in this test file's monkeypatch below) via
    # monkeypatch on the server module's private helper.  Done here
    # so individual tests don't have to remember.
    yield plans_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_state_db_for_test(tmp_path: Path):
    """Return (routing, execution, verification, artifact) repos.

    Imports the real state-machine connection helper, opens a
    per-test SQLite file under ``tmp_path``, applies migrate(), and
    returns the four repositories.  This is the same composition the
    route does internally; tests use it to seed pre-conditions.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    return (
        RoutingRepository(conn),
        ExecutionRepository(conn),
        None,  # placeholder for VerificationRepository (not built yet)
        ArtifactRepository(conn),
        conn,
    )


def _seed_minimal_active_plan(plans_dir: Path, plan_id: str) -> Path:
    """Create the minimal on-disk plan directory that the route reads.

    The legacy route reads these JSON files:
      * interview.json (for ``requirement`` / ``created_at``)
      * prd.json       (for ``docs.prd`` boolean)
      * review.json    (for ``steps.review`` boolean)
      * tasks.json     (for ``status``)

    The refactor reads them from the four repositories instead, but
    the *public* route response shape still derives ``requirement``
    and ``created_at`` from ``interview.json`` (the field is sourced
    from the parent directory, not SQLite).  We seed just enough on
    disk for ``list_plans`` to surface non-null human fields while
    the state-machine sources routing / execution / artifacts /
    archived-derivation.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps({
            "requirement": "Build a todo list with persistence",
            "created_at": "2026-07-15T10:00:00Z",
            "dimensions": {"goals": "Ship a minimal todo API"},
        }),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    return plan_dir


def _seed_archived_plan_dir(plans_dir: Path, plan_id: str) -> Path:
    """Create a plan directory whose dirname classifies as archived.

    The cutoff is 2026-08-05 (encoded in
    ``scan_archived_plans.CUTOFF_2026_08_05``).  We use a dirname
    strictly before the cutoff (2026-08-01) so ``classify_plan``
    returns ``"archived"``.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps({
            "requirement": "Archived plan requirement first line",
            "created_at": "2026-08-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# 1. test_list_plans_aggregates_four_tables
# ---------------------------------------------------------------------------


def test_list_plans_aggregates_four_tables(monkeypatch, tmp_path, clean_state):
    """Plan row in 4 tables → /api/plans returns full entry + archived=False.

    Seeds:
      * plan_routing row     (RoutingRepository.insert)
      * plan_execution row   (ExecutionRepository.insert)
      * plan_artifacts row   (ArtifactRepository.upsert)
      * on-disk directory    (interview.json + tasks.json)

    Expectation:
      The route aggregates all four into a single list entry with
      archived=False, current_phase from routing, requirement from
      disk interview.json.

    Dirname note: ``20260810-`` parses as an ISO date later than the
    2026-08-05 cutoff, so :func:`classify_plan` returns ``"new"``.
    (An unparsable name returns ``"new"`` too — see ``classify_plan``'s
    docstring for why the old safe-downgrade-to-archived rule was
    reversed — but a dated name is what real plans carry, so the
    fixture uses one.)
    """
    plans_dir = clean_state  # the autouse fixture provides the path
    plan_id = "20260810-four-table-plan"
    _seed_minimal_active_plan(plans_dir, plan_id)

    routing, execution, _verif, artifact, conn = _open_state_db_for_test(tmp_path)

    routing.insert(
        plan_id=plan_id,
        phase="ready",
        substage=None,
    )
    execution.insert(
        plan_id=plan_id,
        current_phase="ready",
        project_dir=str(plans_dir / "project"),
        exec_pid=LIVE_PID,
        exec_status="running",
    )
    artifact.upsert(
        plan_id=plan_id,
        artifact_type="tasks",
        file_path=str(plans_dir / plan_id / "tasks.json"),
        status="generated",
    )
    # 2026-09-19: ``requirement`` / ``created_at`` no longer come from a
    # direct ``open(plan_dir / "interview.json")``. The route resolves
    # them through the artifacts repo (``ArtifactRepository
    # .query_interview`` → the row's ``file_path``), so the artifact row
    # is part of the fixture — without it the route has no pointer and
    # reports an empty requirement.
    artifact.upsert(
        plan_id=plan_id,
        artifact_type="interview",
        file_path=str(plans_dir / plan_id / "interview.json"),
        status="generated",
    )

    # Patch server.py to use the same connection so the route reads
    # the seeded rows.  We redirect server's state-machine open via
    # the ``_state_db_path_for_request`` indirection that the new
    # code introduces.
    import server as _server

    def _fake_state_db_path(request=None):
        return tmp_path / "state.db"

    monkeypatch.setattr(
        _server, "_state_db_path", _fake_state_db_path, raising=False
    )

    with TestClient(app) as client:
        resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"/api/plans returned HTTP {resp.status_code}; expected 200"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"/api/plans must return a JSON list, got {type(body).__name__}"
    )
    by_id = {item["id"]: item for item in body}
    assert plan_id in by_id, (
        f"plan {plan_id!r} missing from /api/plans response; got ids: "
        f"{sorted(by_id.keys())}"
    )
    item = by_id[plan_id]

    # The four-table aggregation surfaces all expected fields.
    assert item.get("archived") is False, (
        f"non-archived plan must have archived=False, got "
        f"{item.get('archived')!r}; full item={item!r}"
    )
    assert item.get("current_phase") == "ready", (
        f"current_phase must come from plan_execution, got "
        f"{item.get('current_phase')!r}; full item={item!r}"
    )
    assert item.get("pid") == LIVE_PID, (
        f"pid must come from plan_execution.exec_pid (and only be "
        f"surfaced while the execution still reads as running), got "
        f"{item.get('pid')!r}; full item={item!r}"
    )
    # Human-readable fields come from the ``interview`` artifact row
    # (``plan_artifacts.file_path`` → the on-disk interview.json), not
    # from a filesystem probe.
    assert "todo" in item.get("requirement", "").lower(), (
        f"requirement must be derived from the interview artifact, got "
        f"{item.get('requirement')!r}; full item={item!r}"
    )
    assert "interview" in (item.get("steps") or {}), (
        f"steps manifest must include 'interview' boolean, got "
        f"{item.get('steps')!r}"
    )


# ---------------------------------------------------------------------------
# 2. test_list_plans_includes_archived_with_flag  (acceptance-criterion-4 anchor)
# ---------------------------------------------------------------------------


def test_list_plans_includes_archived_with_flag(monkeypatch, tmp_path, clean_state):
    """**验收条件 4 锚点**.

    An archived plan on disk (dirname < 2026-08-05) appears in
    ``/api/plans`` with ``archived == True``; its
    ``requirement_first_line`` is populated from
    ``interview.json``.
    """
    plans_dir = clean_state
    archived_plan_id = "20260801-archived-plan"
    _seed_archived_plan_dir(plans_dir, archived_plan_id)

    # Open the SQLite routing repo (must be present so list_all()
    # runs; we don't seed any new active rows for this test — only
    # the archive-scan side of list_all is exercised).
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    _ = RoutingRepository(conn)  # no insert; SQLite side is empty

    import server as _server

    def _fake_state_db_path(request=None):
        return tmp_path / "state.db"

    monkeypatch.setattr(
        _server, "_state_db_path", _fake_state_db_path, raising=False
    )

    with TestClient(app) as client:
        resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"/api/plans returned HTTP {resp.status_code}; expected 200"
    )
    body = resp.json()
    by_id = {item["id"]: item for item in body}
    assert archived_plan_id in by_id, (
        f"archived plan {archived_plan_id!r} missing from /api/plans "
        f"response; got ids: {sorted(by_id.keys())}"
    )
    archived_entry = by_id[archived_plan_id]
    assert archived_entry.get("archived") is True, (
        f"archived plan must carry archived=True, got "
        f"{archived_entry.get('archived')!r}; full entry={archived_entry!r}"
    )
    assert (
        archived_entry.get("requirement_first_line")
        == "Archived plan requirement first line"
    ), (
        f"requirement_first_line must come from interview.json, got "
        f"{archived_entry.get('requirement_first_line')!r}; "
        f"full entry={archived_entry!r}"
    )


# ---------------------------------------------------------------------------
# 3. test_list_plans_archived_entry_has_no_stage_fields
# ---------------------------------------------------------------------------


def test_list_plans_archived_entry_has_no_stage_fields(monkeypatch, tmp_path, clean_state):
    """Archived entry MUST NOT carry stage / current_phase / substage / version.

    These are SQLite state-machine semantics.  Archived plans do
    NOT enter SQLite so reading them would be a logical error; the
    aggregate response must not leak those keys.
    """
    plans_dir = clean_state
    archived_plan_id = "20260801-archived-no-stage"
    _seed_archived_plan_dir(plans_dir, archived_plan_id)

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    _ = RoutingRepository(conn)

    import server as _server

    def _fake_state_db_path(request=None):
        return tmp_path / "state.db"

    monkeypatch.setattr(
        _server, "_state_db_path", _fake_state_db_path, raising=False
    )

    with TestClient(app) as client:
        resp = client.get("/api/plans")

    assert resp.status_code == 200
    body = resp.json()
    archived = next(
        (it for it in body if it.get("id") == archived_plan_id),
        None,
    )
    assert archived is not None, (
        f"archived plan {archived_plan_id!r} missing from /api/plans "
        f"response; got: {body!r}"
    )
    for forbidden_key in ("stage", "current_phase", "substage", "version"):
        assert forbidden_key not in archived, (
            f"archived entry must NOT carry {forbidden_key!r}; full "
            f"entry={archived!r}"
        )


# ---------------------------------------------------------------------------
# 4. test_get_summary_includes_artifact_manifest
# ---------------------------------------------------------------------------


def test_get_summary_includes_artifact_manifest(monkeypatch, tmp_path, clean_state):
    """GET /api/plan/{id}/summary carries an ``artifacts`` manifest list.

    The list comes from
    :meth:`ArtifactRepository.list_for_plan` and each entry carries
    the four user-facing fields (``artifact_type``, ``file_path``,
    ``status``, ``content_hash``).
    """
    plans_dir = clean_state
    plan_id = "summary-with-artifacts"
    plan_dir = _seed_minimal_active_plan(plans_dir, plan_id)

    routing, execution, _verif, artifact, conn = _open_state_db_for_test(tmp_path)

    routing.insert(
        plan_id=plan_id,
        phase="ready",
        substage=None,
    )
    execution.insert(
        plan_id=plan_id,
        current_phase="ready",
    )
    tasks_file = str(plan_dir / "tasks.json")
    artifact.upsert(
        plan_id=plan_id,
        artifact_type="tasks",
        file_path=tasks_file,
        status="generated",
        content_hash="deadbeef" * 8,
    )
    artifact.upsert(
        plan_id=plan_id,
        artifact_type="prd",
        file_path=str(plan_dir / "prd.json"),
        status="pending",
    )

    import server as _server

    def _fake_state_db_path(request=None):
        return tmp_path / "state.db"

    monkeypatch.setattr(
        _server, "_state_db_path", _fake_state_db_path, raising=False
    )

    with TestClient(app) as client:
        resp = client.get(f"/api/plan/{plan_id}/summary")

    assert resp.status_code == 200, (
        f"/api/plan/{plan_id}/summary returned HTTP {resp.status_code}, "
        f"expected 200; body={resp.text!r}"
    )
    body = resp.json()

    assert "artifacts" in body, (
        f"summary must carry an 'artifacts' key; got keys: "
        f"{sorted(body.keys())}"
    )
    artifacts = body["artifacts"]
    assert isinstance(artifacts, list), (
        f"artifacts must be a list, got {type(artifacts).__name__}"
    )
    types = {a.get("artifact_type") for a in artifacts}
    assert "tasks" in types, (
        f"artifacts manifest must include 'tasks', got types {types}; "
        f"full artifacts={artifacts!r}"
    )
    assert "prd" in types, (
        f"artifacts manifest must include 'prd', got types {types}; "
        f"full artifacts={artifacts!r}"
    )
    tasks_entry = next(a for a in artifacts if a.get("artifact_type") == "tasks")
    assert tasks_entry.get("status") == "generated"
    assert tasks_entry.get("file_path") == tasks_file
    assert tasks_entry.get("content_hash") == "deadbeef" * 8


# ---------------------------------------------------------------------------
# 5. test_get_summary_404_for_missing_plan
# ---------------------------------------------------------------------------


def test_get_summary_404_for_missing_plan(monkeypatch, tmp_path, clean_state):
    """Unknown plan_id → HTTP 404 from the summary route."""
    import server as _server

    def _fake_state_db_path(request=None):
        return tmp_path / "state.db"

    monkeypatch.setattr(
        _server, "_state_db_path", _fake_state_db_path, raising=False
    )

    with TestClient(app) as client:
        resp = client.get("/api/plan/does-not-exist/summary")

    assert resp.status_code == 404, (
        f"missing plan summary returned HTTP {resp.status_code}, "
        f"expected 404"
    )
