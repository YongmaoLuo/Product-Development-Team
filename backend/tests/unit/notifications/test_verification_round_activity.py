"""The verification round names its sub-step when no VP is in flight.

2026-10-05. ``current_vp`` is the executor's semaphore-primary slot, so
it is set only while a VP is executing. A round spends most of its
wall-clock time elsewhere, and in those windows the card fell through
to a bare "🔄 验证中" that named nothing.

Two consequences were measured on a real plan
(``20261004-PDT-Product-Developm``, round 1):

  * two of seven verification pushes were nameless — one at round
    start before the first ``vp_start``, one at round close after the
    last ``vp_complete``;
  * the judgment phase ran 98 minutes (20:53 → 22:31), walking
    ``judgment_heartbeat`` across VP-001…VP-017. Every heartbeat
    published ``KIND_VP_STATE_CHANGED``, so the card WAS rebuilt
    throughout — but nothing it renders moved, so the fingerprint dedup
    discarded all of them and the card sat frozen the whole time.

Pinned here:

  * the sub-step is derived from the round log (planning / running /
    judging / summarizing) and is ``None`` while a VP really is in
    flight, so the two never both claim the same moment;
  * ``current_vp``'s existing semantics are untouched — it is still
    ``None`` outside a VP, which several tests depend on;
  * the card names the sub-step when ``current_vp`` is absent, and
    still prefers ``current_vp`` when both are present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from notifications.cards import _unified_phase_section


@pytest.fixture(scope="module")
def round_mod():
    """``routes.verification`` under test, imported lazily.

    ``server`` has to be imported first: it pulls in the routers at its
    own module scope, and reaching ``routes.verification`` directly
    trips a circular import (``ArchivedPlanError`` is still being
    defined when the router re-enters). Same ordering the app itself
    uses, and the same ordering ``tests/unit/test_plan_status.py``
    relies on.
    """
    import server  # noqa: F401
    import routes.verification as rv

    return rv


def _derive(rv, plan_dir, running=None):
    """Parse the round log once, then derive — as the endpoint does.

    Exercises the real pairing: the derivation is handed the same
    single-pass result the caller already has, so a second parse cannot
    hide behind the test.
    """
    activity, judgment, last_vp = rv._read_latest_round_state(plan_dir)
    return rv._verification_round_activity(
        running if running is not None else rv._running_vps_from_activity(activity),
        activity, judgment, last_vp,
    )


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir(parents=True)
    return tmp_path


def _write(plan_dir: Path, entries: list) -> None:
    (plan_dir / "logs" / "verification_1_x.log").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def _vp_start(vp: str, ts: str) -> dict:
    return {"verification_point_id": vp, "event_type": "vp_start",
            "timestamp": ts, "data": {}}


def _vp_complete(vp: str, ts: str) -> dict:
    return {"verification_point_id": vp, "event_type": "vp_complete",
            "timestamp": ts, "data": {"status": "PASSED"}}


def _judgment(vp: str, ts: str) -> dict:
    return {"verification_point_id": vp,
            "event_type": "judgment_heartbeat",
            "timestamp": ts,
            "data": {"stage": "supplement_review", "vp_id": vp}}


def _activity_from(entries: list, running: Optional[set] = None) -> dict:
    act = {}
    for e in entries:
        if e.get("event_type") in ("vp_start", "vp_complete"):
            slot = act.setdefault(e["verification_point_id"], {"start": "", "complete": ""})
            slot["start" if e["event_type"] == "vp_start" else "complete"] = e["timestamp"]
    return act


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_planning_before_any_vp_starts(plan_dir: Path, round_mod):
    """Round start: the VP set is being assembled, so there is no VP to
    name — but there is still a definite answer."""
    _write(plan_dir, [])

    result = _derive(round_mod, plan_dir, running=set())

    assert result is not None
    assert result["kind"] == "planning"
    assert result["label"]


def test_none_while_a_vp_is_genuinely_in_flight(plan_dir: Path, round_mod):
    """A VP in flight is the caller's job to name via ``current_vp``.
    Emitting a second, vaguer line about the same moment would be noise."""
    entries = [_vp_start("VP-001", "2026-10-05T20:40:00")]
    _write(plan_dir, entries)

    result = _derive(round_mod, plan_dir, running={"VP-001"})

    assert result is None


def test_judging_names_the_vp_under_review(plan_dir: Path, round_mod):
    """The 98-minute case. Without this the card said nothing at all."""
    entries = [
        _vp_start("VP-001", "2026-10-05T20:40:00"),
        _vp_complete("VP-001", "2026-10-05T20:47:00"),
        _judgment("VP-001", "2026-10-05T20:53:36"),
        _judgment("VP-007", "2026-10-05T21:35:13"),
    ]
    _write(plan_dir, entries)

    result = _derive(round_mod, plan_dir, running=set())

    assert result["kind"] == "judging"
    assert "[VP-007]" in result["label"]
    assert result["since"] == "2026-10-05T21:35:13"


def test_summarizing_between_last_vp_and_judgment(plan_dir: Path, round_mod):
    """Every VP done, report not yet aggregated — the round-close window
    that produced the second nameless card."""
    entries = [
        _vp_start("VP-001", "2026-10-05T20:40:00"),
        _vp_complete("VP-001", "2026-10-05T20:47:00"),
    ]
    _write(plan_dir, entries)

    result = _derive(round_mod, plan_dir, running=set())

    assert result["kind"] == "summarizing"


def test_judgment_then_more_judgment_keeps_the_latest(plan_dir: Path, round_mod):
    entries = [
        _vp_start("VP-001", "2026-10-05T20:40:00"),
        _vp_complete("VP-001", "2026-10-05T20:47:00"),
        _judgment("VP-001", "2026-10-05T20:53:36"),
        _judgment("VP-017", "2026-10-05T22:31:13"),
    ]
    _write(plan_dir, entries)

    result = _derive(round_mod, plan_dir, running=set())

    assert "[VP-017]" in result["label"]


def test_unreadable_log_degrades_to_planning(plan_dir: Path, round_mod):
    """No raise, and no invented verdict — the round has not visibly
    started, which is what an empty log means."""
    result = _derive(round_mod, plan_dir, running=set())

    assert result is not None
    assert result["kind"] == "planning"


def test_a_vp_starting_after_the_last_heartbeat_is_not_judging(
    plan_dir: Path, round_mod,
):
    """Ordering, not mere presence, decides.

    A heartbeat earlier in the round does not make the round "judging"
    once a VP has started again — a re-check is a re-check, and naming a
    phase that already ended is the same defect this function exists to
    remove.
    """
    entries = [
        _vp_start("VP-001", "2026-10-05T20:40:00"),
        _vp_complete("VP-001", "2026-10-05T20:47:00"),
        _judgment("VP-001", "2026-10-05T20:53:36"),
        _vp_start("VP-001", "2026-10-05T21:10:00"),
        _vp_complete("VP-001", "2026-10-05T21:20:00"),
    ]
    _write(plan_dir, entries)

    result = _derive(round_mod, plan_dir, running=set())

    assert result["kind"] == "summarizing", result


def test_a_vp_in_flight_beats_an_earlier_heartbeat(plan_dir: Path, round_mod):
    entries = [
        _judgment("VP-001", "2026-10-05T20:53:36"),
        _vp_start("VP-001", "2026-10-05T21:10:00"),
    ]
    _write(plan_dir, entries)

    assert _derive(round_mod, plan_dir, running={"VP-001"}) is None


def test_one_parse_serves_both_views(round_mod, plan_dir: Path):
    """The VP lifecycle and the judgment phase are two views of one
    timeline. Reading them in separate passes would double the cost of
    every poll of ``/api/verification/{id}/progress``."""
    _write(plan_dir, [
        _vp_start("VP-001", "2026-10-05T20:40:00"),
        _vp_complete("VP-001", "2026-10-05T20:47:00"),
        _judgment("VP-007", "2026-10-05T21:35:13"),
    ])

    activity, judgment, last_vp = round_mod._read_latest_round_state(plan_dir)

    assert activity["VP-001"] == {
        "start": "2026-10-05T20:40:00", "complete": "2026-10-05T20:47:00",
    }
    assert judgment["vp_id"] == "VP-007"
    assert judgment["stage"] == "supplement_review"
    assert last_vp["vp_id"] == "VP-001"
    # The old entry point still answers the old question.
    assert round_mod._read_latest_round_vp_activity(plan_dir) == activity


def test_a_heartbeat_without_a_timestamp_is_not_a_position(
    plan_dir: Path, round_mod,
):
    """A malformed heartbeat must not overwrite a good earlier one, and
    must not become the reported phase."""
    entries = [
        _vp_start("VP-001", "2026-10-05T20:40:00"),
        _vp_complete("VP-001", "2026-10-05T20:47:00"),
        _judgment("VP-007", "2026-10-05T21:35:13"),
        {"verification_point_id": "VP-009", "event_type": "judgment_heartbeat",
         "timestamp": None, "data": {}},
    ]
    _write(plan_dir, entries)

    result = _derive(round_mod, plan_dir, running=set())

    assert result["kind"] == "judging"
    assert "[VP-007]" in result["label"]


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------


def _unified(current_vp, verification_activity) -> str:
    out = _unified_phase_section(
        "verification_running",
        verification_status="running",
        verification_round=1,
        verification_max_rounds=4,
        current_vp=current_vp,
        verification_activity=verification_activity,
    )
    return out[0]["text"]["content"] if out else ""


def test_card_names_the_judgment_vp():
    line = _unified(None, "⚖️ 正在复核验证结论 `[VP-007]`")

    assert "复核验证结论" in line
    assert "[VP-007]" in line


def test_current_vp_still_wins_over_the_sub_step():
    """``current_vp`` is the more precise answer; the sub-step is only
    ever a fallback."""
    line = _unified(
        {"id": "VP-014", "title": "CLI：secrets verify"},
        "⚖️ 正在复核验证结论 `[VP-007]`",
    )

    assert "正在验证 `[VP-014]`" in line
    assert "复核验证结论" not in line


def test_no_vp_and_no_activity_keeps_the_bare_label():
    """The degraded path — an older server, or a round log that could
    not be read. Must still render something truthful."""
    line = _unified(None, None)

    assert "验证中" in line
