"""``mark_parked`` parks a verification round WITHOUT failing it.

Background (arch decision R1 — 停放不等于失败): when every provider
runs out of quota mid-round, the verification round has to stop *for
now*.  Writing that as ``failed`` says the round ended for good — the
plan's verification lifecycle is closed and the operator sees a verdict
that never happened.  ``mark_stopped`` cannot express the pause either:
it only accepts the terminal/running statuses (``waiting_for_quota`` is
a ``ValueError``) and it publishes ``plan_closed``, i.e. it treats a
parked round as a closed plan.

``mark_parked`` is the non-terminal write entry point.  Contract
exercised here:

  * ``verification_status`` becomes ``waiting_for_quota`` and
    ``verification_stop_reason`` records why the round parked;
  * ``round`` / ``max_rounds`` / ``verdicts`` / ``results`` are kept —
    the round resumes under the SAME number;
  * NO ``plan_closed`` is published — a parked plan is not a closed
    one;
  * a row that already reached a terminal status
    (``passed`` / ``failed`` / ``loop_stopped``) is NOT overwritten;
  * a missing row raises ``KeyError``, exactly like ``mark_stopped``;
  * parking twice is idempotent;
  * the method is registered in the write-method list the
    cross-table isolation check parameterises over.
"""

from __future__ import annotations

import importlib.util

from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import VerificationRepository


PARKED_STATUS = "waiting_for_quota"

#: The module that keeps the list of every ``VerificationRepository``
#: write method the isolation check parameterises over.
_WRITE_METHOD_REGISTRY = Path(__file__).with_name(
    "test_every_write_method_is_registered_for_isolation_check.py"
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state.db"
    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def repo(db_path):
    conn = open_db(db_path)
    try:
        yield VerificationRepository(conn)
    finally:
        conn.close()


def test_mark_parked_writes_waiting_status(repo):
    """A running round parked on quota reads as parked, not as failed."""
    repo.insert("p1", "running", round=1, max_rounds=3)

    repo.mark_parked("p1", reason=PARKED_STATUS)

    row = repo.current("p1")
    assert row["verification_status"] == PARKED_STATUS
    assert row["verification_stop_reason"] == PARKED_STATUS


def test_mark_parked_keeps_round_and_verdicts(repo):
    """The round resumes under the SAME number, with its verdicts intact."""
    repo.insert(
        "p1",
        "running",
        round=2,
        max_rounds=5,
        verdicts=[{"seq": 1, "verdict": "fail"}],
    )

    repo.mark_parked("p1", reason=PARKED_STATUS)

    row = repo.current("p1")
    assert row["round"] == 2
    assert row["max_rounds"] == 5
    assert row["verdicts"] == [{"seq": 1, "verdict": "fail"}]
    assert row["results"] is None


def test_mark_parked_refuses_terminal_row(repo):
    """A round that already ended keeps its verdict — parking is not a verdict."""
    repo.insert(
        "p1", "failed", verification_stop_reason="base_drift_conflicts",
    )

    repo.mark_parked("p1", reason=PARKED_STATUS)

    row = repo.current("p1")
    assert row["verification_status"] == "failed"
    assert row["verification_stop_reason"] == "base_drift_conflicts"


def test_mark_parked_does_not_publish_plan_closed(repo, monkeypatch):
    """A parked plan is not a closed plan: no ``plan_closed`` is fired."""
    published = []
    monkeypatch.setattr(
        repo,
        "_publish_plan_closed",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    repo.insert("p1", "running")

    repo.mark_parked("p1")

    assert published == []


def test_mark_parked_missing_row_raises_keyerror(repo):
    """No row, no write — the same ``KeyError`` ``mark_stopped`` raises."""
    with pytest.raises(KeyError):
        repo.mark_parked("missing")


def test_mark_parked_is_idempotent(repo):
    """Parking twice writes the same values and does not raise."""
    repo.insert("p1", "running", round=1, max_rounds=3)

    repo.mark_parked("p1")
    repo.mark_parked("p1")

    row = repo.current("p1")
    assert row["verification_status"] == PARKED_STATUS
    assert row["verification_stop_reason"] == PARKED_STATUS
    assert row["round"] == 1


def test_mark_parked_registered_in_write_method_list():
    """The new write method is registered for the isolation check."""
    spec = importlib.util.spec_from_file_location(
        "verification_write_method_registry", _WRITE_METHOD_REGISTRY,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    names = [name for name, _ in module.VERIFICATION_WRITE_METHODS]
    assert "mark_parked" in names
