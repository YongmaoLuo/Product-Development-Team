"""收尾相位变化要发事件：卡片才能从「🔄 验证中」里醒过来。

2026-11-23 的事故：收尾开始后卡片再没更新过 —— 一直显示「🔄 验证中」；
收尾失败后卡片更是永久冻结。

相位变化本身不会让卡片动 —— 卡片是被 ``KIND_PLAN_PHASE_CHANGED``
事件推着重建的。而收尾那两个相位转换**都不走**会发事件的那条路：

* 进 ``post_processing``：``PlanState.transition_to`` 只写库，不发事件；
* 进 ``postprocess_failed``：``_record_verification_terminal`` 里的路由
  CAS 期望计划还停在验证相位（那时它已经停在 ``post_processing`` 了），
  CAS 落空，事件也就没发。

所以这两个转换各自补发一条事件。这个文件钉住：

* 进 ``post_processing`` → 发一条，``phase=post_processing``；
* 进 ``postprocess_failed`` → 发一条，``phase=postprocess_failed``；
* 没有要求书的 ``passed`` 路径**不发**这条事件（回归）；
* 发事件失败不许把收尾流程带崩（通知层的失败不是收尾的失败）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402

from notifications import state_events  # noqa: E402
from notifications.state_events import KIND_PLAN_PHASE_CHANGED  # noqa: E402


PLAN_ID = "p-postprocess-phase-events"

#: The file the requirement book lives in.
SPEC_FILENAME = "postprocess.json"

#: A minimal but legal requirement book.
VALID_BOOK: Dict[str, Any] = {
    "items": [
        {
            "id": "publish-notes",
            "action": "发布说明",
            "acceptance_predicate": {"kind": "exit_code"},
            "evidence_requirement": {"kind": "exit_code"},
            "external": False,
            "required": True,
            "wait": {"type": "none"},
            "command": ["echo", "ok"],
        },
    ],
}


class _FakeRouting:
    """The routing repository, reduced to the CAS the passed round does."""

    def __init__(self) -> None:
        self.cases: List[Tuple[Any, ...]] = []

    def try_mark_phase(self, plan_id: str, expected: Any, phase: str) -> None:
        self.cases.append((plan_id, expected, phase))


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Arm the module and capture every phase event the code publishes.

    Attributes:
        plan_dir: The plan's directory.
        routing: The fake routing repository.
        terminals: Every terminal record the round wrote.
        started: Every plan id the finishing stage was asked to run.
        events: Every ``publish_safe`` call, as ``(kind, plan_id, payload)``.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "PLANS_DIR", plans_root)

    routing = _FakeRouting()
    monkeypatch.setattr(
        srv, "_open_verification_state", lambda: (None, routing, None)
    )

    terminals: List[Tuple[Any, ...]] = []

    def _record_terminal(
        plan_id: str, status: str, stop_reason: Optional[str], **kwargs: Any
    ) -> None:
        terminals.append((plan_id, status, stop_reason, kwargs))

    monkeypatch.setattr(srv, "_record_verification_terminal", _record_terminal)

    started: List[str] = []
    monkeypatch.setattr(
        vl, "_start_postprocess", lambda plan_id: started.append(plan_id)
    )

    events: List[Tuple[str, str, Dict[str, Any]]] = []

    def _publish(kind: str, plan_id: str, **payload: Any) -> None:
        events.append((kind, plan_id, payload))

    monkeypatch.setattr(state_events, "publish_safe", _publish)

    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    return SimpleNamespace(
        plan_dir=plan_dir,
        routing=routing,
        terminals=terminals,
        started=started,
        events=events,
    )


def _write_book(plan_dir: Path) -> Path:
    """Write the legal requirement book into the plan directory."""
    path = plan_dir / SPEC_FILENAME
    path.write_text(
        json.dumps(VALID_BOOK, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _write_report(plan_dir: Path, terminal_status: str) -> Path:
    """Write a terminal finishing-stage report into the plan directory."""
    path = plan_dir / "postprocess_report.json"
    path.write_text(
        json.dumps(
            {
                "spec_sha256": "0" * 64,
                "terminal_status": terminal_status,
                "items": [
                    {
                        "id": "publish-notes",
                        "verdict": "PASS" if terminal_status == "completed"
                        else "FAIL",
                        "evidence_excerpt": "",
                        "audit": {
                            "action": "发布说明",
                            "command": "echo ok",
                            "target": "notes",
                        },
                    },
                ],
                "failure_context_ref": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _phase_events(events) -> List[Dict[str, Any]]:
    """Only the phase-change events, in order."""
    return [
        payload
        for kind, _plan_id, payload in events
        if kind == KIND_PLAN_PHASE_CHANGED
    ]


# ---------------------------------------------------------------------------
# 进收尾中
# ---------------------------------------------------------------------------


def test_entering_post_processing_publishes_one_phase_event(
    armed: SimpleNamespace,
):
    """有要求书 → 相位写成 ``post_processing`` → 发一条相位事件。"""
    _write_book(armed.plan_dir)
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert ps.get_current_phase() == "post_processing"
    events = _phase_events(armed.events)
    assert len(events) == 1, armed.events
    assert events[0]["phase"] == "post_processing"
    assert events[0]["previous_phase"] == "verification_running"
    assert armed.events[0][1] == PLAN_ID


def test_the_plain_passed_path_publishes_no_finishing_event(
    armed: SimpleNamespace,
):
    """没有要求书 → 直接 ``completed`` → **不发**收尾相位事件（回归）。"""
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert ps.get_current_phase() == "completed"
    assert _phase_events(armed.events) == [], armed.events


def test_a_broken_publish_does_not_break_the_finishing_stage(
    armed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
):
    """通知层炸了不许把收尾带崩 —— 阶段照跑，相位照写。"""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the bus is on fire")

    monkeypatch.setattr(state_events, "publish_safe", _boom)
    _write_book(armed.plan_dir)
    ps = srv.PlanState(armed.plan_dir)

    vl._handle_passed_round(PLAN_ID, 2, ps)

    assert ps.get_current_phase() == "post_processing"
    assert armed.started == [PLAN_ID]


# ---------------------------------------------------------------------------
# 进收尾失败
# ---------------------------------------------------------------------------


def test_a_failed_stage_publishes_the_postprocess_failed_event(
    armed: SimpleNamespace,
):
    """收尾失败 → 终态记 ``failed`` + 相位事件发 ``postprocess_failed``。"""
    _write_report(armed.plan_dir, "postprocess_failed")

    vl._settle_postprocess_phase(PLAN_ID, armed.plan_dir)

    # The terminal record: failed, parked (no plan_closed), phase target.
    assert len(armed.terminals) == 1, armed.terminals
    _plan_id, status, _stop_reason, kwargs = armed.terminals[0]
    assert status == "failed"
    assert kwargs["phase_target"] == "postprocess_failed"
    assert kwargs["publish_closed"] is False

    events = _phase_events(armed.events)
    assert len(events) == 1, armed.events
    assert events[0]["phase"] == "postprocess_failed"
    assert events[0]["previous_phase"] == "post_processing"


def test_a_completed_stage_publishes_no_failed_event(
    armed: SimpleNamespace,
):
    """收尾成功 → 计划正常关闭 → 不发 ``postprocess_failed`` 事件（回归）。"""
    _write_report(armed.plan_dir, "completed")

    vl._settle_postprocess_phase(PLAN_ID, armed.plan_dir)

    assert len(armed.terminals) == 1, armed.terminals
    assert armed.terminals[0][1] == "passed"
    assert _phase_events(armed.events) == [], armed.events
