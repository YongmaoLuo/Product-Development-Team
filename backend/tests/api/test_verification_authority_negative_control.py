"""Negative control: the ``plan_routing.verification`` mirror is inert.

Background
----------
``plan_routing.verification`` was a *mirror* column the verification
loop wrote on its way past.  It has now been demoted: the summary and
``GET /api/verification/{id}/progress`` read the authoritative
``plan_verification`` row instead, and the mirror writes are gone.

Demoting a column is a claim about the code.  This file turns it into a
claim about the **outputs**, which is the only kind that survives the
next refactor:

1. **The write is inert.**  A plan whose mirror column is overwritten
   with raw SQL — the way a stale writer still overwrites it — reports
   exactly the same summary, exactly the same card status and exactly
   the same "does this card carry content" answer as before the write.
   The test proves the write landed before it proves the outputs did
   not move; otherwise it would pass for the wrong reason.

2. **The consumers read the authoritative value.**  The summary's
   ``state.verification`` block is what the card builders read
   (``cards._status_from_summary``), what the notifier's content guard
   reads (``FeishuNotifier._has_operator_content``) and what
   ``FeishuNotifier._rebuild_card`` falls back to when the status
   snapshot carries no verification status of its own.  If any of them
   reached back for the routing column, the verdict on the card would
   disagree with the verdict in the table.

3. **No row means "never started".**  A plan with no
   ``plan_verification`` row reports ``not_started`` — never the
   mirror's value, which would be a verdict nobody recorded.

What this file pins:

1. ``test_card_status_uses_authoritative_values`` — the card's
   ``PlanStatus`` carries the authoritative status / round / cap.
2. ``test_has_operator_content_uses_authoritative_values`` — a plan
   whose only content is its verification round still renders a card.
3. ``test_rebuild_card_fallback_uses_authoritative_value`` — the
   notifier's fallback (no verification status on the snapshot) lands
   on the authoritative value, and the card it builds says so.
4. ``test_routing_write_does_not_change_outputs`` — the negative
   control: overwriting the mirror moves nothing.
5. ``test_missing_row_ignores_routing_value`` — no authoritative row
   means ``not_started``, whatever the mirror says.
6. ``test_running_phase_forces_running_in_summary_and_card`` — a plan
   in the ``verification_running`` phase reads as running in the
   summary *and* in the card.

Test command::

    pytest backend/tests/api/test_verification_authority_negative_control.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from notifications.cards import _status_from_summary  # noqa: E402
from notifications.feishu_notifier import FeishuNotifier  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str) -> Path:
    """Create a minimal plan directory and return it.

    The summary reads the plan's own directory first and answers 404
    when it is missing, so every case here has to build one.
    """
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "ready",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def _stale_lifecycle(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The four lifecycle keys, as a stale writer left them in the column.

    The mirror column no longer carries the verification state; these
    cases exist to prove the reader does not pick the column up, so the
    keys have to be *in* it — written the way the old writer wrote them.
    """
    return {
        key: value
        for key, value in payload.items()
        if key not in ("baseline", "baseline_doc_sha256")
    }


def _seed_routing(
    plan_id: str,
    *,
    phase: str = "ready",
    verification: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the plan's ``plan_routing`` row.

    The ``verification`` argument is the *mirror* column — the value the
    summary used to report.  Cases that need the mirror and the
    authoritative table to disagree write the wrong value here.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        repo.write_plan_state(
            plan_id,
            phase=phase,
            completed_phases=[],
            review_rounds={"prd": 0, "arch": 0, "test": 0},
            flags={},
            last_updated="2026-11-04T00:00:00Z",
        )
        # 过期写者留在镜像列里的形态：生命周期四键不该被读侧采信，
        # 所以照旧写者的手笔用裸 SQL 写进去。基线另走列现在唯一的写者。
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (json.dumps(_stale_lifecycle(verification or {})), plan_id),
        )
        conn.commit()
        baseline = (verification or {}).get("baseline")
        if baseline is not None:
            repo.write_verification_baseline(
                plan_id,
                baseline,
                (verification or {}).get("baseline_doc_sha256", ""),
            )
    finally:
        conn.close()


def _seed_verification(
    plan_id: str,
    *,
    status: str = "passed",
    round_n: int = 2,
    max_rounds: int = 3,
    stop_reason: Optional[str] = None,
) -> None:
    """Write the authoritative ``plan_verification`` row."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = server._state_db_path(None)
    conn = open_db(db_path)
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id,
            verification_status=status,
            round=round_n,
            max_rounds=max_rounds,
            verification_stop_reason=stop_reason,
        )
    finally:
        conn.close()


def _write_mirror_column(plan_id: str, mirror: Dict[str, Any]) -> None:
    """Overwrite the ``plan_routing.verification`` mirror with raw SQL.

    Raw SQL on purpose: the repository layer is not the thing under
    test here — the *reader* is.  Writing the column the way a stale
    writer still writes it is what must not move any output.
    """
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path(None))
    try:
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (json.dumps(mirror), plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _read_mirror_column(plan_id: str) -> Any:
    """The raw ``plan_routing.verification`` column, as stored."""
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path(None))
    try:
        row = conn.execute(
            "SELECT verification FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _mirror(plan_id: str) -> Dict[str, Any]:
    """The mirror column, parsed.

    Every case here is a *conflict* case, so the test has to show the
    conflict exists: a mirror that happened to agree with the
    authoritative row would make the assertion vacuous.
    """
    stored = _read_mirror_column(plan_id)
    assert stored is not None, "the mirror column is empty; nothing to conflict"
    parsed = json.loads(stored)
    assert isinstance(parsed, dict), f"the mirror column is not a dict: {stored!r}"
    return parsed


def _summary_body(plan_id: str) -> Dict[str, Any]:
    """The whole ``GET /api/plan/{id}/summary`` payload."""
    resp = client.get(f"/api/plan/{plan_id}/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _summary_verification(plan_id: str) -> Dict[str, Any]:
    """The ``state.verification`` block of a plan summary."""
    return _summary_body(plan_id)["state"]["verification"]


# ---------------------------------------------------------------------------
# 1. The card's snapshot carries the authoritative values
# ---------------------------------------------------------------------------


def test_card_status_uses_authoritative_values() -> None:
    """``cards._status_from_summary`` reads the authoritative row.

    This is the compatibility path the card builders use when the
    caller holds a summary but no ``PlanStatus`` of its own.  It reads
    ``summary.state.verification`` — the block the summary builds from
    ``plan_verification`` — so the card's verdict is the table's
    verdict, not the stale mirror's.
    """
    plan_id = "20261104-neg-card-status"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification(plan_id, status="passed", round_n=2, max_rounds=3)

    summary = _summary_body(plan_id)
    status = _status_from_summary(plan_id, summary)

    # The conflict has to be real, or the assertions below prove nothing.
    assert _mirror(plan_id).get("status") == "failed", (
        f"the mirror must disagree with the authoritative row; got "
        f"{_mirror(plan_id)!r}"
    )

    assert status.verification_status == "passed", (
        f"the card snapshot must carry the authoritative status; got "
        f"{status!r}"
    )
    assert status.verification_round == 2, (
        f"the card snapshot must carry the authoritative round; got {status!r}"
    )
    assert status.verification_max_rounds == 3, (
        f"the card snapshot must carry the authoritative cap; got {status!r}"
    )


# ---------------------------------------------------------------------------
# 2. The content guard sees the round
# ---------------------------------------------------------------------------


def test_has_operator_content_uses_authoritative_values() -> None:
    """The notifier's content guard reads the authoritative block.

    ``_has_operator_content`` drops a card whose every content source
    is empty.  The mirror here holds the fresh-plan default — so a
    consumer that read the mirror would call this plan contentless and
    push nothing.  The plan *did* run a verification round, so it has
    something to show.
    """
    plan_id = "20261104-neg-operator-content"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "not_started", "round": 0, "max_rounds": 3},
    )
    _seed_verification(plan_id, status="passed", round_n=2, max_rounds=3)

    summary = _summary_body(plan_id)

    # The tasks are empty on purpose: the verification round is the
    # only thing this card could carry.
    assert summary["tasks"]["total"] == 0, (
        f"this case is only meaningful without tasks; got {summary['tasks']!r}"
    )
    assert summary["state"]["verification"]["status"] == "passed", (
        f"the summary must report the authoritative status; got "
        f"{summary['state']['verification']!r}"
    )
    # The mirror holds the fresh-plan default, so a consumer that read
    # it would call this plan contentless.
    assert _mirror(plan_id).get("status") == "not_started", (
        f"the mirror must hold the contentless default; got "
        f"{_mirror(plan_id)!r}"
    )

    assert FeishuNotifier._has_operator_content(summary, None) is True, (
        "a plan with a verification round on record has content to show; "
        "the guard must read the authoritative block, not the mirror"
    )


# ---------------------------------------------------------------------------
# 3. The notifier's fallback
# ---------------------------------------------------------------------------


def test_rebuild_card_fallback_uses_authoritative_value() -> None:
    """``_rebuild_card``'s fallback lands on the authoritative value.

    The notifier prefers the status snapshot and falls back to
    ``summary.state.verification`` when the snapshot carries no
    verification status of its own
    (``feishu_notifier._rebuild_card``).  The fallback has to land on
    the *summary's* block — the authoritative one — and never on the
    routing mirror's stale verdict.

    The card the notifier builds is checked too: it has to say
    ``passed`` and round 2, not the mirror's ``failed`` / round 9.
    """
    plan_id = "20261104-neg-rebuild-fallback"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification(plan_id, status="passed", round_n=2, max_rounds=3)

    summary = _summary_body(plan_id)

    # The status snapshot carries no verification status at all — the
    # shape the fallback exists for.  Everything else is what the
    # server would send.
    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "phase": "ready",
        "summary": summary,
        "execution": {"plan_id": plan_id, "status": "", "tasks": []},
        "verification": None,
        "execution_in_flight": False,
        "verification_in_flight": False,
        "tasks": summary.get("tasks") or {},
    }

    with patch(
        "notifications.feishu_notifier.fetch_plan_status",
        return_value=payload,
    ):
        card, _phase, verification_status, _summary = FeishuNotifier()._rebuild_card(
            plan_id, set()
        )

    # The premise of the branch: the snapshot really carries no
    # verification status, so the fallback is what answered.
    from plan_status import from_payload as _status_from_payload

    assert _status_from_payload(payload).verification_status == "", (
        "this case is only meaningful when the snapshot carries no "
        "verification status"
    )
    # The conflict has to be real, or the assertions below prove nothing.
    assert _mirror(plan_id).get("status") == "failed", (
        f"the mirror must disagree with the authoritative row; got "
        f"{_mirror(plan_id)!r}"
    )

    assert verification_status == "passed", (
        "the fallback must read the summary's authoritative value, not the "
        f"routing mirror's 'failed'; got {verification_status!r}"
    )
    assert card is not None, (
        "the notifier must still build a card for a plan whose only "
        "content is a verification round"
    )

    text = json.dumps(card, ensure_ascii=False)
    assert "round 2/3" in text, (
        f"the card must report the authoritative round; got {text!r}"
    )
    assert "passed" in text, (
        f"the card must report the authoritative status; got {text!r}"
    )
    assert "round 9" not in text, (
        f"the mirror's round must not reach the card; got {text!r}"
    )


# ---------------------------------------------------------------------------
# 4. The negative control: the mirror write moves nothing
# ---------------------------------------------------------------------------


def test_routing_write_does_not_change_outputs() -> None:
    """Overwriting the mirror changes nothing that is reported.

    The mirror column is written by writers outside the verification
    loop's own bookkeeping.  Once the summary reads the authoritative
    row, such a write must not move a single reported value — the
    summary, the card snapshot the builders derive from it, or the
    notifier's content verdict.

    The write is proved to have landed before the outputs are compared.
    A negative control that cannot show the write happened is a test
    that passes for the wrong reason.
    """
    plan_id = "20261104-neg-routing-write"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification(plan_id, status="passed", round_n=2, max_rounds=3)

    before_summary = _summary_body(plan_id)
    before_status = _status_from_summary(plan_id, before_summary)
    before_has_content = FeishuNotifier._has_operator_content(before_summary, None)

    bogus = {"status": "failed", "round": 99, "max_rounds": 3}
    _write_mirror_column(plan_id, bogus)

    stored = _read_mirror_column(plan_id)
    assert stored is not None, "the mirror write did not land at all"
    assert json.loads(stored) == bogus, (
        f"the mirror column must actually hold the bogus value; got {stored!r}"
    )

    after_summary = _summary_body(plan_id)
    after_status = _status_from_summary(plan_id, after_summary)
    after_has_content = FeishuNotifier._has_operator_content(after_summary, None)

    assert after_summary == before_summary, (
        "the summary must be completely unchanged by a mirror write; "
        f"{before_summary!r} -> {after_summary!r}"
    )
    assert after_status == before_status, (
        "the card snapshot must be unchanged by a mirror write; "
        f"{before_status!r} -> {after_status!r}"
    )
    assert after_has_content == before_has_content, (
        "the content verdict must be unchanged by a mirror write; "
        f"{before_has_content!r} -> {after_has_content!r}"
    )
    assert after_has_content is True, (
        f"the plan has a verification round on record; got {after_has_content!r}"
    )


# ---------------------------------------------------------------------------
# 5. No authoritative row
# ---------------------------------------------------------------------------


def test_missing_row_ignores_routing_value() -> None:
    """No ``plan_verification`` row means "never started".

    The mirror still holds a verdict — a stale one, written by a writer
    that has since been removed.  A plan with no authoritative row has
    never run a round, and the mirror's verdict is nobody's record of
    anything.  Reporting it would be inventing history.
    """
    plan_id = "20261104-neg-missing-row"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    # No ``plan_verification`` row on purpose.

    block = _summary_verification(plan_id)

    # The mirror holds a verdict the plan never recorded — that is the
    # value this case must refuse to report.
    assert _mirror(plan_id).get("status") == "failed", (
        f"the mirror must disagree with the missing row; got "
        f"{_mirror(plan_id)!r}"
    )

    assert block["status"] == "not_started", (
        f"a plan without an authoritative row must read as not_started; "
        f"got {block!r}"
    )
    assert block["round"] == 0, f"got {block!r}"
    assert block["max_rounds"] == server.DEFAULT_MAX_VERIFICATION_ROUNDS, (
        f"got {block!r}"
    )

    summary = _summary_body(plan_id)
    status = _status_from_summary(plan_id, summary)
    assert status.verification_status == "not_started", (
        f"the card snapshot must agree with the summary; got {status!r}"
    )
    assert status.verification_round == 0, f"got {status!r}"


# ---------------------------------------------------------------------------
# 6. A round in flight
# ---------------------------------------------------------------------------


def test_running_phase_forces_running_in_summary_and_card() -> None:
    """A plan in the ``verification_running`` phase reads as running.

    The round row is stamped when the round *ends*; the phase is what
    says a round is in flight.  A plan between the two must read as
    running — in the summary and in the card built from it.  The
    mirror's stale verdict must not turn that into "failed".
    """
    plan_id = "20261104-neg-running-phase"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="verification_running",
        verification={"status": "failed", "round": 9, "max_rounds": 3},
    )
    _seed_verification(
        plan_id, status="not_started", round_n=0, max_rounds=4,
    )

    block = _summary_verification(plan_id)
    assert block["status"] == "running", (
        f"a plan in the verification_running phase must read as running; "
        f"got {block!r}"
    )

    summary = _summary_body(plan_id)
    status = _status_from_summary(plan_id, summary)
    assert status.verification_status == "running", (
        f"the card snapshot must agree with the summary; got {status!r}"
    )
