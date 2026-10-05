"""The card header and body must never contradict each other.

2026-10-05, observed on a real plan (``20261004-PDT-Product-Developm``)
during its round-1 repair execution:

  * ``plan_routing.verification``      → ``{"status": "failed", round: 1}``
  * ``plan_verification``              → ``verification_status='running'``,
                                          ``updated_at`` 98 minutes earlier
  * ``plan_routing.current_phase``     → ``executing``
  * live ``verification_in_flight``    → ``False``
  * a ``repair-r1-*`` task             → ``in_progress``

``/api/plan/{id}/status`` takes its top-level ``verification_status``
from ``plan_verification``, and the card header is a pure function of
that snapshot. So the header rendered "🔄 验证中 round 1/4" directly
above a body reading "执行中 → 正在跑 [repair-r1-01]" — for the whole
repair round.

Root cause on the write side: every TERMINAL round exit goes through
``_record_terminal`` → ``complete_round``, but the REPAIR exits returned
to the executor without writing any verdict, leaving the column at the
``running`` value stamped at round start. Every state write in this
codebase is best-effort by design (a failed write must never break the
workflow), so the miss was silent.

Pinned here:

  * a terminal routing verdict overrides a stale non-terminal
    ``plan_verification`` value at the read site;
  * the override is logged, because a silent reconciliation is exactly
    the failure mode being fixed;
  * the drift is reported as a divergence, so the underlying miss stays
    visible instead of being papered over forever;
  * two stores both claiming a LIVE state is not a conflict and is left
    alone;
  * a live execution outranks a stale verification status in the header;
  * the degraded out-of-process path keeps its previous header.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notifications.cards import _resolve_header
from plan_status import PlanStatus, find_divergences


# ---------------------------------------------------------------------------
# Helpers — mirroring tests/unit/test_plan_status.py's seeders
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db)
    return db


def _seed_routing(db: Path, plan_id: str, phase: str, verification="null") -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    payload = verification if isinstance(verification, str) else json.dumps(verification)
    conn = open_db(db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, completed_phases, review_rounds, "
            " flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, phase, 0, "[]", "{}", "{}", payload,
             "2026-10-05T14:39:19Z", "2026-10-05T14:39:19Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_verification_row(db: Path, plan_id: str, status: str,
                           round_n: int = 1, max_rounds: int = 4) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.init_round(plan_id, round_n=round_n, max_rounds=max_rounds)
        conn.execute(
            "UPDATE plan_verification SET verification_status = ? WHERE plan_id = ?",
            (status, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tasks(db: Path, plan_id: str, rows) -> None:
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        for row in rows:
            fields = dict(row)
            task_id = str(fields.pop("id"))
            status = fields.pop("status", None)
            repo.add_task(plan_id, {"id": task_id, **fields})
            if status is not None:
                repo.update_task(plan_id, task_id, {"status": status})
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read-side reconciliation
# ---------------------------------------------------------------------------


def test_terminal_routing_verdict_overrides_a_stale_running_column(
    state_db, clean_execution_state,
):
    """The exact shape from the incident."""
    import server

    plan_id = "plan-stale"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 1, "max_rounds": 4},
    )
    _seed_verification_row(state_db, plan_id, "running")
    _seed_tasks(state_db, plan_id, [
        {"id": "repair-r1-01", "title": "fix the keychain path",
         "status": "in_progress"},
    ])

    st = server._build_plan_status(plan_id)

    assert st.verification_status == "failed", (
        "the stale 'running' column won; the card header will now "
        "contradict its own body"
    )


def test_reconciliation_carries_routing_round_and_budget(
    state_db, clean_execution_state,
):
    """Round and max_rounds travel with the verdict — the header's
    "round N/M" suffix reads them from the same place."""
    import server

    plan_id = "plan-stale-rounds"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 3, "max_rounds": 4},
    )
    _seed_verification_row(state_db, plan_id, "running", round_n=1, max_rounds=4)
    _seed_tasks(state_db, plan_id, [{"id": "1", "title": "t", "status": "in_progress"}])

    st = server._build_plan_status(plan_id)

    assert (st.verification_round, st.verification_max_rounds) == (3, 4)


def test_reconciliation_is_logged(state_db, clean_execution_state, caplog):
    """A silent reconciliation is the same failure with extra steps —
    the next occurrence would be invisible."""
    import logging

    import server

    plan_id = "plan-stale-logged"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 1, "max_rounds": 4},
    )
    _seed_verification_row(state_db, plan_id, "running")
    _seed_tasks(state_db, plan_id, [{"id": "1", "title": "t", "status": "in_progress"}])

    with caplog.at_level(logging.WARNING, logger="server"):
        server._build_plan_status(plan_id)

    assert any(
        "is stale" in r.getMessage() and plan_id in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_two_live_stores_are_not_treated_as_a_conflict(
    state_db, clean_execution_state,
):
    """Both saying "running" is the NORMAL shape of a round in progress.
    Picking a winner there would invent an answer neither store supports."""
    import server

    plan_id = "plan-both-live"
    _seed_routing(
        state_db, plan_id, "verification_running",
        verification={"status": "running", "round": 1, "max_rounds": 4},
    )
    _seed_verification_row(state_db, plan_id, "running")
    _seed_tasks(state_db, plan_id, [{"id": "1", "title": "t", "status": "pending"}])

    st = server._build_plan_status(plan_id)

    assert st.verification_status == "running"


def test_parsed_and_string_routing_verification_agree(
    state_db, clean_execution_state,
):
    """``plan_routing.verification`` is a TEXT column holding JSON.
    Whether the repository hands it back parsed is not something the
    reconciliation may depend on."""
    import server

    for i, payload in enumerate((
        {"status": "failed", "round": 2, "max_rounds": 4},
        json.dumps({"status": "failed", "round": 2, "max_rounds": 4}),
    )):
        plan_id = f"plan-shape-{i}"
        _seed_routing(state_db, plan_id, "executing", verification=payload)
        _seed_verification_row(state_db, plan_id, "running")
        _seed_tasks(state_db, plan_id, [
            {"id": "1", "title": "t", "status": "in_progress"},
        ])

        st = server._build_plan_status(plan_id)

        assert st.verification_status == "failed", payload
        assert st.verification_round == 2, payload


def test_repeated_reads_do_not_spam_the_log(
    state_db, clean_execution_state, caplog, monkeypatch,
):
    """``/api/plan/{id}/status`` is polled several times a minute per plan.
    An unconditional warning would bury the log for the length of the
    drift — which is a whole repair round."""
    import logging

    import server

    plan_id = "plan-stale-chatty"
    _seed_routing(
        state_db, plan_id, "executing",
        verification={"status": "failed", "round": 1, "max_rounds": 4},
    )
    _seed_verification_row(state_db, plan_id, "running")
    _seed_tasks(state_db, plan_id, [{"id": "1", "title": "t", "status": "in_progress"}])

    # Fresh rate-limit state — other tests share the module-level table.
    monkeypatch.setattr("routes.plans._RECONCILE_LOGGED", {})

    with caplog.at_level(logging.WARNING, logger="server"):
        for _ in range(20):
            server._build_plan_status(plan_id)

    hits = [r for r in caplog.records if "is stale" in r.getMessage()]
    assert len(hits) == 1, f"{len(hits)} warnings for 20 identical reads"


def test_the_rate_limit_state_stays_bounded(state_db, clean_execution_state, monkeypatch):
    """A long-lived server must not accumulate an entry per plan forever."""
    import server

    table: dict = {}
    monkeypatch.setattr("routes.plans._RECONCILE_LOGGED", table)
    monkeypatch.setattr("routes.plans._RECONCILE_LOG_MAX_ENTRIES", 8)

    for i in range(40):
        plan_id = f"plan-churn-{i}"
        _seed_routing(
            state_db, plan_id, "executing",
            verification={"status": "failed", "round": 1, "max_rounds": 4},
        )
        _seed_verification_row(state_db, plan_id, "running")
        _seed_tasks(state_db, plan_id, [
            {"id": "1", "title": "t", "status": "in_progress"},
        ])
        server._build_plan_status(plan_id)

    assert len(table) <= 8, len(table)


# ---------------------------------------------------------------------------
# Divergence reporting
# ---------------------------------------------------------------------------


def _status(**kw) -> PlanStatus:
    base = dict(
        plan_id="p", phase="executing", verification_status="running",
        verification_round=1, verification_max_rounds=4,
        execution_in_flight=True, verification_in_flight=False,
        tasks={"total": 68, "completed": 65, "failed": 2, "in_progress": 1},
    )
    base.update(kw)
    return PlanStatus(**base)


def test_stale_running_verification_during_execution_is_reported():
    codes = [d.code for d in find_divergences(_status())]

    assert "stale_running_verification_during_execution" in codes


def test_a_live_verification_round_is_not_a_divergence():
    """The normal in-round shape: both stores say running, and a round
    really is running."""
    codes = [
        d.code for d in find_divergences(
            _status(phase="verification_running", verification_in_flight=True)
        )
    ]

    assert "stale_running_verification_during_execution" not in codes


def test_a_terminal_verdict_is_not_a_divergence():
    codes = [
        d.code for d in find_divergences(_status(verification_status="failed"))
    ]

    assert "stale_running_verification_during_execution" not in codes


# ---------------------------------------------------------------------------
# Header resolution
# ---------------------------------------------------------------------------


def _header(**kw) -> str:
    st = _status(**kw)
    return _resolve_header(st, dict(st.tasks))["title"]


def test_a_live_execution_outranks_a_stale_running_verdict():
    """The contradiction itself. The header must describe the work that
    is actually happening."""
    title = _header(verification_status="running", execution_in_flight=True)

    assert "验证中" not in title
    assert "修复执行中" in title


def test_a_live_verification_round_still_says_verifying():
    title = _header(
        phase="verification_running", verification_status="running",
        execution_in_flight=False,
    )

    assert "🔄 验证中" in title


def test_the_degraded_out_of_process_path_keeps_its_header():
    """``_status_from_summary`` documents ``execution_in_flight=False``
    as the safe degraded direction. The header guard keys on that flag,
    so every shim-built and locally-constructed snapshot keeps the
    pre-existing label."""
    title = _header(verification_status="running", execution_in_flight=False)

    assert "🔄 验证中" in title


def test_repair_execution_header_carries_the_round():
    title = _header(verification_status="failed", execution_in_flight=True)

    assert "round 1/4" in title
