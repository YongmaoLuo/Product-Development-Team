from __future__ import annotations

import json
import threading

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import VerificationRepository


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state.db"
    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


def _repo(db_path):
    conn = open_db(db_path)
    return conn, VerificationRepository(conn)


def test_append_verdict_appends_to_json_array(db_path):
    conn, repo = _repo(db_path)
    repo.insert("p1", "running")
    for seq in range(3):
        repo.append_verdict("p1", {"seq": seq})
    raw = conn.execute("SELECT verdicts FROM plan_verification WHERE plan_id='p1'").fetchone()[0]
    assert json.loads(raw) == [{"seq": 0}, {"seq": 1}, {"seq": 2}]
    conn.close()


def test_append_verdict_committed_visible_to_second_connection(db_path):
    conn1, repo = _repo(db_path)
    repo.insert("p1", "running")
    repo.append_verdict("p1", {"seq": 1})
    conn2 = open_db(db_path)
    assert len(json.loads(conn2.execute("SELECT verdicts FROM plan_verification WHERE plan_id='p1'").fetchone()[0])) == 1
    conn1.close()
    conn2.close()


@pytest.mark.bug_5
def test_concurrent_append_verdict_no_lost_update(db_path):
    conn, repo = _repo(db_path)
    repo.insert("p1", "running")
    conn.close()
    barrier = threading.Barrier(8)
    errors = []

    def worker(worker_id):
        worker_conn, worker_repo = _repo(db_path)
        try:
            barrier.wait()
            for seq in range(25):
                worker_repo.append_verdict("p1", {"worker_id": worker_id, "seq": seq})
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker_conn.close()

    threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    check = open_db(db_path)
    verdicts = json.loads(check.execute("SELECT verdicts FROM plan_verification WHERE plan_id='p1'").fetchone()[0])
    assert len(verdicts) == 200
    assert {(v["worker_id"], v["seq"]) for v in verdicts} == {(w, s) for w in range(8) for s in range(25)}
    check.close()


def test_append_verdict_initializes_null_verdicts_to_empty_array(db_path):
    conn, repo = _repo(db_path)
    repo.insert("p1", "running", verdicts=None)
    repo.append_verdict("p1", {"vp_id": "VP-001"})
    assert repo.current("p1")["verdicts"] == [{"vp_id": "VP-001"}]
    conn.close()


@pytest.mark.bug_1
@pytest.mark.parametrize("operation", ["init_round", "complete_round", "append_verdict", "mark_stopped"])
def test_verification_write_never_touches_execution_row(db_path, operation):
    conn, repo = _repo(db_path)
    repo.insert("p1", "pending")
    conn.execute("INSERT INTO plan_execution(plan_id,current_phase,updated_at) VALUES('p1','executing','before')")
    before = conn.execute("SELECT * FROM plan_execution WHERE plan_id='p1'").fetchone()
    if operation == "init_round":
        repo.init_round("p1", 2, 5)
    elif operation == "complete_round":
        repo.complete_round("p1", {"passed": 2})
    elif operation == "append_verdict":
        repo.append_verdict("p1", {"seq": 1})
    else:
        repo.mark_stopped("p1", "user_stopped")
    assert conn.execute("SELECT * FROM plan_execution WHERE plan_id='p1'").fetchone() == before
    conn.close()


def test_read_contract_and_round_lifecycle(db_path):
    conn, repo = _repo(db_path)
    repo.insert("p1", "pending", runtime_state={"worker": 1})
    assert repo.summary("p1")["runtime_state"] == {"worker": 1}
    assert repo.snapshot_for_list(["p1"])["p1"]["verification_status"] == "pending"
    repo.init_round("p1", 1, 4)
    assert repo.current("p1")["round"] == 1
    repo.complete_round("p1", {"passed": 3})
    assert repo.current("p1")["results"] == {"passed": 3}
    repo.mark_stopped("p1", "manual")
    current = repo.current("p1")
    assert current["verification_status"] == "loop_stopped"
    assert current["verification_stop_reason"] == "manual"
    conn.close()


def test_complete_round_persists_the_stop_reason_column(db_path):
    """``complete_round`` must write ``verification_stop_reason``.

    2026-09-19: the reason only ever reached the ``results`` JSON blob.
    ``/api/verification/{id}/status`` reads the *column*
    (``record.get("verification_stop_reason")``), so a loop that had
    plainly stopped served ``stop_reason: null`` — and the card header's
    "⏹ 循环停止(round 已达上限)" branch was dead code.
    """
    conn, repo = _repo(db_path)
    repo.insert("p1", "pending")

    repo.complete_round(
        "p1",
        {"status": "loop_stopped", "stop_reason": "max_rounds_reached"},
        status="loop_stopped",
        stop_reason="max_rounds_reached",
    )

    current = repo.current("p1")
    assert current["verification_status"] == "loop_stopped"
    assert current["verification_stop_reason"] == "max_rounds_reached"
    conn.close()


def test_complete_round_leaves_the_stop_reason_alone_when_omitted(db_path):
    """Omitting ``stop_reason`` must not clear a previously recorded one."""
    conn, repo = _repo(db_path)
    repo.insert("p1", "pending")

    repo.complete_round("p1", {"status": "failed"}, status="failed")
    assert repo.current("p1")["verification_stop_reason"] is None
    conn.close()
