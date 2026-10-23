"""The global start resumes the batch the global stop recorded — and only it.

Restarting the backend is a two-step operation: stop everything that is
running, then start everything that was paused. The second half used to
read *every* pause checkpoint on disk, which is not the same thing as
"start what I stopped":

  * a checkpoint is durable. It survives the restart the endpoints
    exist for — and it also survives the next day, the next plan, the
    next unrelated stop. A machine with thirteen checkpoints on disk is
    not a machine that wants thirteen plans resumed.
  * a start that wakes up a plan nobody asked for is worse than a start
    that wakes up nothing: it burns tokens, it touches the operator's
    project directory, and it reports "resumed" for work that was never
    part of the restart.

So the global stop now writes down *which* plans it stopped (the batch),
and the global start reads that record and restores nothing else. The
record is consumed on the way through, so a second start is a no-op
rather than a second resume.

What is pinned here:

1. **The stop writes the batch.** Exactly the plans it actually stopped
   — a plan that refused to stop is not in it, because it has nothing
   to resume.
2. **A stop that stopped nothing still writes a batch** — an empty one.
   "Nothing was running" and "nothing was ever stopped" are different
   answers, and only the first one may resume a plan.
3. **The start resumes the batch and nothing else.** Three checkpoints
   on disk, one plan in the batch: one plan is started.
4. **No batch means no resume.** The answer says so explicitly
   (``batch: null``) instead of silently starting nothing.
5. **The batch is consumed.** A second start is a no-op.
6. **A damaged batch degrades to "no batch".** Bad JSON, an unknown
   version — no exception, no resume.
7. **A batch member with nothing to resume is one failed row.** The
   other plans of the batch are still started.
8. **The recovered-verification source is filtered by the batch too** —
   a verification whose thread a hard kill took away is only restarted
   if it was part of the batch.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import pause_state
import server
from routes import execution as routes_execution
from routes import verification as routes_verification
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate

#: The batch record's file name — the same one the backend writes.
BATCH_FILENAME = "stop_all_batch.json"

EXEC_OK = "20261023-batch-exec-ok"
EXEC_FAIL = "20261023-batch-exec-fail"
VERIF_OK = "20261023-batch-verif-ok"

#: Checkpoints on disk that the batch does not name.
STALE_A = "20261023-batch-stale-a"
STALE_B = "20261023-batch-stale-b"
STALE_C = "20261023-batch-stale-c"

ORPHAN_IN_BATCH = "20261023-batch-orphan-in"
ORPHAN_NOT_IN_BATCH = "20261023-batch-orphan-out"


class Recorder:
    """Stands in for a stop/start route: records what it was asked to do.

    ``errors`` maps a plan id to the exception that plan's call raises —
    the way a conflicting CAS does. Every other plan succeeds.
    """

    def __init__(self, errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        plan_id = args[0] if args else kwargs.get("plan_id")
        self.calls.append({"plan_id": plan_id, "args": args, "kwargs": kwargs})
        error = self.errors.get(plan_id)
        if error is not None:
            raise error
        return {"plan_id": plan_id, "ok": True}

    @property
    def plan_ids(self) -> List[Any]:
        return [call["plan_id"] for call in self.calls]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation, both state dicts emptied.

    The state database lives directly in ``tmp_path``, so the batch
    record the backend writes lands next to it — which is exactly the
    path the tests read.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, tmp_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batch_path(root: Path) -> Path:
    """Where the backend writes the batch record: next to the state.db."""
    return root / BATCH_FILENAME


def _write_batch(root: Path, plan_ids: List[str], version: int = 1) -> Path:
    """Put a batch record on disk, the way the stop would."""
    path = _batch_path(root)
    path.write_text(
        json.dumps({
            "version": version,
            "written_at": "2026-10-23T12:00:00+00:00",
            "plan_ids": list(plan_ids),
        }),
        encoding="utf-8",
    )
    return path


def _mark_execution_running(plans_dir: Path, plan_id: str) -> Path:
    """A plan the executor is working on right now."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._execution_state[plan_id] = {
        "status": "running",
        "process": None,
        "ended_at": None,
        "logs": [],
        "project_dir": str(plan_dir / "project"),
        "started_at": "2026-10-23T11:00:00",
        "pid": None,
    }
    return plan_dir


def _mark_verification_running(plans_dir: Path, plan_id: str) -> Path:
    """A plan whose verification round is cycling right now."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._verification_state[plan_id] = {
        "verification_status": "running",
        "verification_round": 1,
        "started_at": "2026-10-23T11:00:00",
        "updated_at": "2026-10-23T11:00:00",
        "thread": None,
    }
    return plan_dir


def _write_checkpoint(
    plans_dir: Path,
    plan_id: str,
    *,
    kind: str = "execution",
) -> Path:
    """A paused plan directory: the checkpoint is what is left behind."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    pause_state.write_pause_checkpoint(
        plan_dir,
        kind=kind,
        project_dir=str(plan_dir / "project"),
        reason="user_requested",
        interrupted_tasks=[
            {
                "task_id": "1",
                "status": "in_progress",
                "phase": kind,
                "session_id": "sess-1",
                "paused_at": None,
                "strategy": "session_resume",
                "reason": "session_id_available",
            },
        ],
        unfinished_tasks=[{"task_id": "2", "status": "pending"}],
    )
    return plan_dir


def _read_batch(root: Path) -> Dict[str, Any]:
    """The batch record the backend wrote."""
    return json.loads(_batch_path(root).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) the stop writes the batch
# ---------------------------------------------------------------------------


def test_stop_all_writes_the_batch_of_the_plans_it_stopped(
    env, client, monkeypatch
):
    """The batch holds exactly the plans that were really stopped."""
    plans_dir, root = env
    _mark_execution_running(plans_dir, EXEC_OK)
    _mark_execution_running(plans_dir, EXEC_FAIL)
    _mark_verification_running(plans_dir, VERIF_OK)

    # One plan refuses to stop — it is a failed row, and a plan that
    # did not stop has nothing to resume.
    exec_stop = Recorder(errors={EXEC_FAIL: HTTPException(409, "conflict")})
    verif_stop = Recorder()
    monkeypatch.setattr(routes_execution, "stop_execution", exec_stop)
    monkeypatch.setattr(routes_verification, "stop_verification", verif_stop)

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    results = {row["plan_id"]: row for row in body["results"]}
    assert results[EXEC_FAIL]["ok"] is False, body
    assert results[EXEC_OK]["ok"] is True, body
    assert results[VERIF_OK]["ok"] is True, body

    batch = _read_batch(root)
    assert batch["version"] == 1, batch
    assert set(batch["plan_ids"]) == {EXEC_OK, VERIF_OK}, batch
    assert EXEC_FAIL not in batch["plan_ids"], batch

    # The stamp is an ISO-8601 UTC instant, not a local one.
    written_at = batch["written_at"]
    assert isinstance(written_at, str) and written_at, batch
    parsed = datetime.fromisoformat(written_at)
    assert parsed.utcoffset() is not None, batch
    assert parsed.utcoffset().total_seconds() == 0, batch


def test_stop_all_with_nothing_running_writes_an_empty_batch(
    env, client, monkeypatch
):
    """A stop that stopped nothing still writes a batch — an empty one.

    Without it, the start that follows could not tell "nothing was
    running" from "nothing was ever stopped", and the second reading
    is the one that must not resume a stale checkpoint.
    """
    _plans_dir, root = env
    monkeypatch.setattr(routes_execution, "stop_execution", Recorder())
    monkeypatch.setattr(routes_verification, "stop_verification", Recorder())

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == [], resp.text

    batch = _read_batch(root)
    assert batch["version"] == 1, batch
    assert batch["plan_ids"] == [], batch


def test_stop_all_writes_the_batch_next_to_the_state_db(
    env, client, monkeypatch
):
    """The record lands beside the state database, not in a fixed place."""
    plans_dir, root = env
    _mark_execution_running(plans_dir, EXEC_OK)
    monkeypatch.setattr(routes_execution, "stop_execution", Recorder())

    resp = client.post("/api/system/stop_all")
    assert resp.status_code == 200, resp.text

    # ``root`` *is* the state.db's directory in this fixture.
    assert _batch_path(root).exists(), sorted(p.name for p in root.iterdir())
    assert not (root / "plans" / BATCH_FILENAME).exists()


# ---------------------------------------------------------------------------
# 2) the start resumes the batch and nothing else
# ---------------------------------------------------------------------------


def test_start_all_resumes_only_the_plans_in_the_batch(
    env, client, monkeypatch
):
    """Three checkpoints on disk, one plan in the batch: one plan starts."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, STALE_A)
    _write_checkpoint(plans_dir, STALE_B)
    _write_checkpoint(plans_dir, EXEC_OK)
    _write_batch(root, [EXEC_OK])

    exec_start = Recorder()
    verif_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert set(results) == {EXEC_OK}, body
    assert results[EXEC_OK] == {
        "plan_id": EXEC_OK, "type": "execution", "ok": True,
    }, body

    assert exec_start.plan_ids == [EXEC_OK], exec_start.calls
    assert verif_start.calls == [], verif_start.calls


def test_start_all_dispatches_a_batch_plan_by_its_checkpoint_kind(
    env, client, monkeypatch
):
    """A batch plan is started by the route that owns its kind."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, EXEC_OK, kind="execution")
    _write_checkpoint(plans_dir, VERIF_OK, kind="verification")
    # A checkpoint of the same kind that the batch does not name: it
    # must not be started either.
    _write_checkpoint(plans_dir, STALE_C, kind="execution")
    _write_batch(root, [EXEC_OK, VERIF_OK])

    exec_start = Recorder()
    verif_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text

    assert exec_start.plan_ids == [EXEC_OK], exec_start.calls
    assert verif_start.plan_ids == [VERIF_OK], verif_start.calls


def test_start_all_without_a_batch_resumes_nothing(env, client, monkeypatch):
    """No batch on record: no plan is started, and the answer says so."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, STALE_A)
    _write_checkpoint(plans_dir, STALE_B)

    exec_start = Recorder()
    verif_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["results"] == [], body
    assert body["batch"] is None, body
    assert body["detail"] == "no stop_all batch on record", body

    assert exec_start.calls == [], exec_start.calls
    assert verif_start.calls == [], verif_start.calls


def test_start_all_consumes_the_batch(env, client, monkeypatch):
    """The batch is spent by the start that used it: a second is a no-op."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, EXEC_OK)
    _write_batch(root, [EXEC_OK])

    exec_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", Recorder())

    first = client.post("/api/system/start_all")
    assert first.status_code == 200, first.text
    assert len(first.json()["results"]) == 1, first.text
    assert exec_start.plan_ids == [EXEC_OK], exec_start.calls

    assert not _batch_path(root).exists(), "the batch was not consumed"

    second = client.post("/api/system/start_all")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["results"] == [], body
    assert body["batch"] is None, body
    assert body["detail"] == "no stop_all batch on record", body
    assert exec_start.plan_ids == [EXEC_OK], exec_start.calls


def test_start_all_consumes_the_batch_even_when_a_row_failed(
    env, client, monkeypatch
):
    """A failed row does not leave the batch behind for a second run."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, EXEC_OK)
    _write_batch(root, [EXEC_OK])

    exec_start = Recorder(errors={EXEC_OK: HTTPException(409, "conflict")})
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", Recorder())

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["ok"] is False, body

    assert not _batch_path(root).exists(), "the batch was not consumed"


# ---------------------------------------------------------------------------
# 3) a damaged batch degrades to "no batch"
# ---------------------------------------------------------------------------


def test_start_all_ignores_a_corrupt_batch(env, client, monkeypatch):
    """A batch that is not JSON costs a resume, never an exception."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, EXEC_OK)
    _batch_path(root).write_text("{not json at all", encoding="utf-8")

    exec_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", Recorder())

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == [], body
    assert body["batch"] is None, body
    assert body["detail"] == "no stop_all batch on record", body
    assert exec_start.calls == [], exec_start.calls


def test_start_all_ignores_a_batch_of_an_unknown_version(
    env, client, monkeypatch
):
    """A batch written by a newer backend is not half-read."""
    plans_dir, root = env
    _write_checkpoint(plans_dir, EXEC_OK)
    _write_batch(root, [EXEC_OK], version=99)

    exec_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", Recorder())

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == [], body
    assert body["batch"] is None, body
    assert exec_start.calls == [], exec_start.calls


# ---------------------------------------------------------------------------
# 4) a batch member with nothing to resume is one failed row
# ---------------------------------------------------------------------------


def test_start_all_reports_a_batch_member_without_a_checkpoint(
    env, client, monkeypatch
):
    """One plan of the batch has nothing to resume: one failed row.

    The rest of the batch is still started — a plan that cannot be
    resumed must not cost the other plans their resume.
    """
    plans_dir, root = env
    _write_checkpoint(plans_dir, EXEC_OK)
    _write_batch(root, [EXEC_OK, STALE_A])

    exec_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", Recorder())

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    results = {row["plan_id"]: row for row in body["results"]}
    assert set(results) == {EXEC_OK, STALE_A}, body
    assert results[STALE_A]["ok"] is False, body
    assert results[STALE_A]["error"] == "no_checkpoint", body
    assert results[EXEC_OK] == {
        "plan_id": EXEC_OK, "type": "execution", "ok": True,
    }, body

    assert exec_start.plan_ids == [EXEC_OK], exec_start.calls


# ---------------------------------------------------------------------------
# 5) the recovered-verification source is filtered by the batch too
# ---------------------------------------------------------------------------


def test_start_all_restores_a_recovered_verification_of_the_batch(
    env, client, monkeypatch
):
    """A hard-killed verification is restarted only if it was in the batch."""
    plans_dir, root = env
    for plan_id in (ORPHAN_IN_BATCH, ORPHAN_NOT_IN_BATCH):
        (plans_dir / plan_id).mkdir(parents=True, exist_ok=True)
        server._verification_state[plan_id] = {
            "verification_status": "running",
            "verification_round": 2,
            "recovered": True,
            "thread": None,
        }
    _write_batch(root, [ORPHAN_IN_BATCH])

    exec_start = Recorder()
    verif_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", verif_start)

    resp = client.post("/api/system/start_all")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["results"] == [
        {
            "plan_id": ORPHAN_IN_BATCH, "type": "verification", "ok": True,
        },
    ], body
    assert verif_start.plan_ids == [ORPHAN_IN_BATCH], verif_start.calls
    assert exec_start.calls == [], exec_start.calls


# ---------------------------------------------------------------------------
# 6) the round trip: stop, then start
# ---------------------------------------------------------------------------


def test_stop_then_start_restores_exactly_what_was_stopped(
    env, client, monkeypatch
):
    """The whole point, end to end.

    A stale checkpoint from an earlier stop is on disk; the current stop
    touches one plan; the start that follows resumes that plan and not
    the stale one.
    """
    plans_dir, root = env
    _write_checkpoint(plans_dir, STALE_A)
    _mark_execution_running(plans_dir, EXEC_OK)

    monkeypatch.setattr(routes_execution, "stop_execution", Recorder())
    monkeypatch.setattr(routes_verification, "stop_verification", Recorder())
    stop = client.post("/api/system/stop_all")
    assert stop.status_code == 200, stop.text

    # The stop left a checkpoint of its own, next to the stale one.
    _write_checkpoint(plans_dir, EXEC_OK)

    exec_start = Recorder()
    monkeypatch.setattr(routes_execution, "start_execution", exec_start)
    monkeypatch.setattr(routes_verification, "start_verification", Recorder())
    start = client.post("/api/system/start_all")
    assert start.status_code == 200, start.text
    body = start.json()

    assert [row["plan_id"] for row in body["results"]] == [EXEC_OK], body
    assert exec_start.plan_ids == [EXEC_OK], exec_start.calls
