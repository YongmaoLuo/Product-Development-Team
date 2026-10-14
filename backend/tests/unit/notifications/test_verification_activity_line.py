"""The verification column names what the round is doing, not just the VP id.

Two defects met here, and both are silent — the card renders, the
round is genuinely running, and the operator is told nothing:

  * ``current_vp`` is the executor's semaphore-primary slot, so it is
    ``None`` for every window in which no VP is *executing* — planning,
    judging, summarizing. The verification section gated its "正在验证"
    line on ``current_vp`` alone, so those windows showed a progress bar
    and a verdict list and no current activity, reading as though the
    round had stopped. The execution section has had the mirror-image
    "当前任务" block for the same gap.
  * even where the round's sub-step *was* reported, the ``activity``
    label carried a bare ``[VP-006]``. An id identifies a VP to
    something that has already read the plan; it tells an operator
    nothing about which requirement is being adjudicated.

Pinned here:

  * the activity line renders in the verification column when
    ``current_vp`` is ``None``, and carries the label verbatim;
  * the label itself names the VP by title, not id alone (derivation
    side — the endpoint appends it, so every consumer agrees);
  * exactly one of the two lines renders when both sources are present,
    so the section never double-reports the same moment;
  * neither renders when both are absent — no empty heading, which
    would assert a state that does not exist.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from notifications.cards import build_card
from status_payload import status_from


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _summary() -> Dict[str, Any]:
    return {
        "plan_id": "test-plan",
        "state": {
            "current_phase": "verification_running",
            "stage": "verification",
            "verification": {
                "status": "running",
                "round": 1,
                "max_rounds": 4,
                "stop_reason": None,
            },
        },
        "tasks": {
            "total": 0, "completed": 0, "failed": 0,
            "pending": 0, "in_progress": 0, "skipped": 0,
        },
        "execution": {},
    }


def _progress(activity: Any = None, current_vp: Any = None) -> Dict[str, Any]:
    """A live verification payload with no VP in flight by default."""
    return {
        "plan_id": "test-plan",
        "verification_status": "running",
        "verification_round": 1,
        "max_rounds": 4,
        "stop_reason": None,
        "current_vp": current_vp,
        "activity": activity,
        "completed_vps": ["VP-001"],
        "failed_vps": [],
        "skipped_vps": [],
        "pending_vps": ["VP-002"],
        "vps": [],
        "counts": {"total": 2, "completed": 1, "failed": 0,
                   "skipped": 0, "in_progress": 0, "pending": 1},
    }


def _card_lines(progress: Optional[Dict[str, Any]]) -> List[str]:
    """Render the verification card and return its div text lines."""
    card = build_card(
        "test-plan",
        status_from(_summary(), verification=_progress()),
        _summary(),
        execution_progress={"tasks": []},
        verification_progress=progress,
    )
    return [
        (el.get("text") or {}).get("content") or ""
        for el in card["elements"]
        if el.get("tag") == "div"
    ]


def _verification_column_lines(progress: Optional[Dict[str, Any]]) -> List[str]:
    """Only the lines under the "verification 状态" heading.

    The unified header at the top of the card names the same activity
    from the same payload, so a whole-card filter counts every line
    twice and cannot tell the header apart from the column.
    """
    lines = _card_lines(progress)
    for idx, line in enumerate(lines):
        if "verification 状态" in line:
            return lines[idx + 1:]
    return []


def _activity_lines(progress: Optional[Dict[str, Any]]) -> List[str]:
    """The verification column's "what is happening now" lines."""
    return [
        ln for ln in _verification_column_lines(progress)
        if "正在验证" in ln or "复核验证结论" in ln
    ]


_JUDGING = {
    "kind": "judging",
    "label": "⚖️ 正在复核验证结论 `[VP-006]` 前端契约：审批弹窗三类证据渲染与默认拒绝",
    "vp_id": "VP-006",
    "since": "2026-10-11T20:32:47",
}

_IN_FLIGHT = {
    "id": "VP-006",
    "title": "前端契约：审批弹窗三类证据渲染与默认拒绝",
    "started_at": "2026-10-11T20:40:00",
}


# ---------------------------------------------------------------------------
# Card rendering — the activity fallback
# ---------------------------------------------------------------------------


def test_activity_renders_when_no_vp_is_in_flight():
    """The gap this closes: a live round with nothing executing still
    has to say what it is doing."""
    payload = _progress(activity=_JUDGING)

    assert _activity_lines(payload), _card_lines(payload)


def test_activity_line_carries_the_label_verbatim():
    """The label already states the sub-step, the VP and its title.
    Re-formatting it here would put this site out of step with the
    unified header, which renders the same string."""
    joined = "\n".join(_activity_lines(_progress(activity=_JUDGING)))

    assert _JUDGING["label"] in joined
    assert "[VP-006]" in joined
    assert "前端契约：审批弹窗三类证据渲染与默认拒绝" in joined


def test_activity_line_is_symmetric_with_the_execution_line():
    """Same prefix and shape as the execution section's "当前任务"
    block, so the two columns read alike."""
    line = _activity_lines(_progress(activity=_JUDGING))[0]

    assert line.startswith("🔄 **正在验证：**"), line


def test_current_vp_branch_is_unchanged():
    """A VP genuinely executing keeps its richer line — id, title and
    start time. The activity fallback must not displace it."""
    lines = _activity_lines(_progress(current_vp=dict(_IN_FLIGHT)))

    assert len(lines) == 1, lines
    assert "正在验证：`[VP-006]`" in lines[0], lines
    assert "开始于 2026-10-11T20:40:00" in lines[0], lines


def test_both_present_renders_exactly_one_line():
    """Both sources present is a normal moment (a VP can start while a
    judgment heartbeat is still the newest log entry). Reporting the
    same round twice — once under each name — reads as two VPs."""
    lines = _activity_lines(_progress(
        activity=dict(_JUDGING), current_vp=dict(_IN_FLIGHT),
    ))

    assert len(lines) == 1, lines


@pytest.mark.parametrize("payload", [
    _progress(activity=None, current_vp=None),
    _progress(activity={}, current_vp=None),
    _progress(activity={"kind": "judging", "label": "   "}, current_vp=None),
    _progress(activity=42, current_vp=None),
])
def test_neither_source_renders_no_line(payload):
    """The degraded path: an older server, or a round whose log could
    not be read. An empty heading would assert an activity that does
    not exist, which is worse than silence."""
    assert _activity_lines(payload) == []


def test_bare_string_activity_is_accepted():
    """Defensive: a caller that hands over a string rather than the
    endpoint's dict must not drop the section."""
    payload = _progress(activity="⚖️ 正在复核验证结论 `[VP-006]` 前端契约")

    assert _activity_lines(payload)
    assert "复核验证结论" in "\n".join(_activity_lines(payload))


def test_activity_section_survives_a_terminal_plan():
    """The endpoint only emits ``activity`` for a running round, so a
    terminal payload carries none. The render side must not invent
    one — an empty heading on a finished round would assert a
    current activity that is over."""
    terminal = _progress(activity=None, current_vp=None)
    terminal["verification_status"] = "passed"

    assert _activity_lines(terminal) == []


# ---------------------------------------------------------------------------
# Label construction (endpoint side)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def round_mod():
    """``routes.verification``, imported lazily.

    ``server`` must be imported first: it pulls in the routers at its
    own module scope, and reaching ``routes.verification`` directly
    trips a circular import.
    """
    import server  # noqa: F401
    import routes.verification as rv

    return rv


def _judgment(vp: str, ts: str) -> dict:
    """The parsed judgment view the round-log reader hands back
    (``{"stage", "vp_id", "ts"}``) — not the raw log entry."""
    return {"stage": "supplement_review", "vp_id": vp, "ts": ts}


def test_judging_label_names_the_vp_by_title(round_mod):
    """An id identifies a VP to something that has already read the
    plan. On the card it names no requirement at all."""
    result = round_mod._verification_round_activity(
        running=set(),
        activity={"VP-001": {"start": "2026-10-11T20:00:00",
                              "complete": "2026-10-11T20:10:00"}},
        judgment=_judgment("VP-006", "2026-10-11T20:32:47"),
        last_vp={"vp_id": "VP-001", "ts": "2026-10-11T20:10:00"},
        vp_titles={"VP-006": "前端契约：审批弹窗三类证据渲染与默认拒绝"},
    )

    assert result["kind"] == "judging"
    assert "[VP-006]" in result["label"]
    assert "前端契约：审批弹窗三类证据渲染与默认拒绝" in result["label"]


def test_judging_label_without_a_title_degrades_to_the_id(round_mod):
    """A VP the plan file does not carry (a re-check of a superseded
    id) must still produce a usable label rather than raise — this
    payload is polled by the card on every heartbeat."""
    result = round_mod._verification_round_activity(
        running=set(),
        activity={"VP-001": {"start": "2026-10-11T20:00:00",
                              "complete": "2026-10-11T20:10:00"}},
        judgment=_judgment("VP-999", "2026-10-11T20:32:47"),
        last_vp={"vp_id": "VP-001", "ts": "2026-10-11T20:10:00"},
        vp_titles={"VP-006": "前端契约"},
    )

    assert "[VP-999]" in result["label"]


def test_no_titles_argument_keeps_the_previous_label(round_mod):
    """``vp_titles`` is optional, so the existing four-argument callers
    keep working unchanged."""
    result = round_mod._verification_round_activity(
        running=set(),
        activity={"VP-001": {"start": "2026-10-11T20:00:00",
                              "complete": "2026-10-11T20:10:00"}},
        judgment=_judgment("VP-006", "2026-10-11T20:32:47"),
        last_vp={"vp_id": "VP-001", "ts": "2026-10-11T20:10:00"},
    )

    assert result["label"] == "⚖️ 正在复核验证结论 `[VP-006]`"


def test_titles_are_flattened_and_bounded(round_mod):
    """Titles are prose — multi-line, and several clauses long. The
    label is a one-line status string: an untruncated title wraps until
    it is no longer scannable."""
    result = round_mod._verification_round_activity(
        running=set(),
        activity={"VP-001": {"start": "2026-10-11T20:00:00",
                              "complete": "2026-10-11T20:10:00"}},
        judgment=_judgment("VP-006", "2026-10-11T20:32:47"),
        last_vp={"vp_id": "VP-001", "ts": "2026-10-11T20:10:00"},
        vp_titles={"VP-006": "第一行\n第二行  " + "长" * 200},
    )
    label = result["label"]

    assert "\n" not in label, label
    title = label.split("`[VP-006]`", 1)[1].strip()
    assert title.endswith("…"), title
    assert len(title) <= round_mod._VERIFICATION_ACTIVITY_TITLE_MAX + 1


def test_kinds_without_a_vp_id_are_unchanged(round_mod):
    """``planning`` and ``summarizing`` name no VP, so there is no
    title to append — and inventing one would misdescribe them."""
    planning = round_mod._verification_round_activity(
        running=set(), activity={}, judgment=None, last_vp=None,
        vp_titles={"VP-006": "前端契约"},
    )
    summarizing = round_mod._verification_round_activity(
        running=set(),
        activity={"VP-001": {"start": "2026-10-11T20:00:00",
                              "complete": "2026-10-11T20:10:00"}},
        judgment=None,
        last_vp={"vp_id": "VP-001", "ts": "2026-10-11T20:10:00"},
        vp_titles={"VP-006": "前端契约"},
    )

    assert planning["label"] == (
        round_mod._VERIFICATION_ACTIVITY_LABELS["planning"])
    assert summarizing["label"] == (
        round_mod._VERIFICATION_ACTIVITY_LABELS["summarizing"])


def test_in_flight_round_still_emits_nothing(round_mod):
    """Unchanged contract: a VP in flight is the caller's job to name.
    The title does not turn this into a second line about one moment."""
    assert round_mod._verification_round_activity(
        running={"VP-001"},
        activity={"VP-001": {"start": "2026-10-11T20:00:00",
                              "complete": ""}},
        judgment=_judgment("VP-006", "2026-10-11T20:32:47"),
        last_vp={"vp_id": "VP-001", "ts": "2026-10-11T20:00:00"},
        vp_titles={"VP-006": "前端契约"},
    ) is None
