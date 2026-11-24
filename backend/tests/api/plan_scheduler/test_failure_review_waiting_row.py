"""``GET /api/scheduler/state`` — the failure review's waiting row.

What is pinned here
-------------------
A plan that failed at the end is announced to a human as a **decision**
(``backend/failure_review.py``).  The panel polls the scheduler's read
model to find out what is waiting for an answer, so the announced
failure has to show up in that read model's ``waiting`` list — and the
row has to say enough for the panel to draw it.

1. **The row is in the read model.**  The endpoint is driven through
   the mounted app — a real request, never a direct call into
   ``routes.scheduler`` — because the failure this guards against is a
   route that was never registered, or one registered *after* the
   frontend catch-all, where a GET is answered with a 404 instead of
   the read model.  The row names the plan, the decision it is
   (``item_id``), the kind of wait, the question as it was asked, and
   when the question was asked (``created_at``).
2. **The row disappears once the decision is answered** (R7).  A row
   that stayed after the answer would say "this is waiting" about
   something that is over — and the panel would offer to extend a
   question nobody can answer any more.
3. **The row is not extendable** (R4).  A failure review is not a
   countdown: there is no deadline to push out, and the row says so
   rather than inviting the operator to try.

Two further pins ride along: a sidecar that cannot be read is skipped
rather than fatal — the panel polls this endpoint every few seconds,
so a broken file must not take the read model down with it — and a
decision that is not in the store is skipped for the same reason.

The failure review's own sidecar
--------------------------------
The row is built out of ``<plan_dir>/failure_review.json`` — the record
:func:`failure_review.announce_failure` leaves behind — and the
*pending* state is read out of the decision store.  The sidecar says
what was announced; the store says whether it is still waiting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from failure_review import FAILURE_REVIEW_FILENAME, announce_failure  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from plan_scheduler.snapshot import build  # noqa: E402
from postprocess.progress import PROGRESS_FILENAME  # noqa: E402

client = TestClient(server.app)

#: The endpoint under test.
STATE_PATH = "/api/scheduler/state"

#: The plan whose failure is announced.
PLAN_ID = "p1"

#: The kind of wait a failure review is.
FAILURE_REVIEW = "failure_review"


class _FakeEngine:
    """The engine's read side, with a state to read."""

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self._state = state if state is not None else {}

    def snapshot(self) -> Dict[str, Any]:
        return build(self._state)


@pytest.fixture
def install_engine(monkeypatch):
    """Put an engine on the app — and take it away afterwards."""

    def _install(state: Optional[Dict[str, Any]] = None) -> _FakeEngine:
        engine = _FakeEngine(state)
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        return engine

    return _install


@pytest.fixture
def plan_dir() -> Path:
    """The plan's own directory, under the plans root the code reads."""
    path = resolve_plans_dir() / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def announced(plan_dir: Path) -> Dict[str, Any]:
    """A plan whose failure has been announced, and is still pending.

    The announcement is the production path: it creates the decision
    and leaves the sidecar behind.  The sidecar is what the waiting row
    is built out of, so a row that only the test could produce would
    not prove anything about the real thing.
    """
    (plan_dir / PROGRESS_FILENAME).write_text(
        json.dumps({"spec_sha256": "", "started_at": "", "items": {}}),
        encoding="utf-8",
    )
    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)
    assert decision_id, "the failure must be announced as a decision"

    sidecar = json.loads(
        (plan_dir / FAILURE_REVIEW_FILENAME).read_text(encoding="utf-8")
    )
    return {
        "decision_id": decision_id,
        "created_at": sidecar["created_at"],
        "question": sidecar["question"],
        "plan_dir": plan_dir,
    }


def _failure_review_rows(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The read model's failure-review rows."""
    return [
        row
        for row in body.get("waiting") or []
        if isinstance(row, dict) and row.get("wait_type") == FAILURE_REVIEW
    ]


def _state() -> Dict[str, Any]:
    """The read model, as the panel reads it."""
    response = client.get(STATE_PATH)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1 — the row is in the read model
# ---------------------------------------------------------------------------


def test_state_includes_failure_review_row(install_engine, announced) -> None:
    """待答的失败复盘出现在面板的等待表里，五个字段齐全。

    等待行由面板画出来：它得说清楚**哪个计划**、**哪一条决策**
    （``item_id``）、**什么种类**、**问的是什么**，以及**什么时候问的**
    （``created_at``）—— 读模型自己的 ``_waiting_view`` 不带
    ``created_at``，所以这一行必须在 ``build()`` 之外单独构造。
    """
    install_engine()

    body = _state()
    rows = _failure_review_rows(body)

    assert len(rows) == 1, body.get("waiting")
    row = rows[0]
    assert row["plan_id"] == PLAN_ID, row
    assert row["item_id"] == announced["decision_id"], row
    assert row["wait_type"] == FAILURE_REVIEW, row
    assert row["question"] == announced["question"], row
    assert row["created_at"] == announced["created_at"], row


# ---------------------------------------------------------------------------
# 2 — the row goes away with the answer (R7)
# ---------------------------------------------------------------------------


def test_row_disappears_after_answer(install_engine, announced) -> None:
    """决策被回答后 → 行不再出现。

    一行还在说「这个在等人」的东西，其实已经不等了 —— 面板会拿它去
    延长一个谁也答不了的问题。
    """
    install_engine()

    assert len(_failure_review_rows(_state())) == 1

    with HitlStore(resolve_state_db_path()) as store:
        answered = store.answer_if_pending(
            announced["decision_id"], "ack", answered_by="tester"
        )
    assert answered, "the decision must have been answered"

    assert _failure_review_rows(_state()) == []


# ---------------------------------------------------------------------------
# 3 — the row is not extendable (R4)
# ---------------------------------------------------------------------------


def test_row_is_not_extendable(install_engine, announced) -> None:
    """失败复盘不参与延长：``extendable`` 为 False，``deadline`` 为 None。

    失败复盘不是倒计时：没有截止时间可以推后，这一行就直说没有，
    而不是让操作员去试一个不存在的按钮。
    """
    install_engine()

    rows = _failure_review_rows(_state())
    assert len(rows) == 1

    row = rows[0]
    assert row["extendable"] is False, row
    assert row["deadline"] is None, row


# ---------------------------------------------------------------------------
# 4 — a broken sidecar is skipped, not fatal
# ---------------------------------------------------------------------------


def test_broken_sidecar_is_skipped(install_engine, plan_dir: Path) -> None:
    """坏掉的旁路记录被跳过：面板每几秒轮询一次，不能被一个文件带崩。

    读不出、缺 ``decision_id``、JSON 坏了 —— 三种都只是「没有可显示的
    东西」，不是「端点报错」。
    """
    install_engine()

    (plan_dir / FAILURE_REVIEW_FILENAME).write_text("{not json", encoding="utf-8")

    body = _state()
    assert _failure_review_rows(body) == [], body.get("waiting")


def test_sidecar_without_a_decision_is_skipped(
    install_engine, plan_dir: Path
) -> None:
    """旁路记录里的 ``decision_id`` 查不到 → 跳过。

    记录说有过一次失败通告，库里却没有这条决策：那就是一条**不在等人**
    的记录 —— 行不画出来。
    """
    install_engine()

    (plan_dir / FAILURE_REVIEW_FILENAME).write_text(
        json.dumps(
            {
                "decision_id": "no-such-decision",
                "problem": "问题",
                "suggestion": "建议",
                "created_at": "2026-11-19T12:00:00+00:00",
                "question": "[failure_review] 问题\n建议：建议",
            }
        ),
        encoding="utf-8",
    )

    body = _state()
    assert _failure_review_rows(body) == [], body.get("waiting")


def test_missing_plans_root_is_not_an_error(
    install_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """计划根目录不存在 → 空列表，不抛异常。

    端点每几秒被轮询一次：根目录不在，是「没有东西在等」，不是「服务
    出错」。
    """
    install_engine()
    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "no-such-plans"))

    body = _state()
    assert _failure_review_rows(body) == [], body.get("waiting")


def test_row_is_not_duplicated_when_the_snapshot_already_has_it(
    install_engine, announced
) -> None:
    """引擎 snapshot 里已有同一个 (plan_id, item_id) → 不重复添加。

    一行在两个地方出现就是两行：面板会把同一个问题画两遍。已有的那
    行留下，失败复盘不再另起一行。
    """
    install_engine(
        {
            "waits": [
                {
                    "plan_id": PLAN_ID,
                    "item_id": announced["decision_id"],
                    "wait_type": "some_other_wait",
                    "question": "快照里已有的那一行",
                }
            ]
        }
    )

    body = _state()
    rows = [
        row
        for row in body.get("waiting") or []
        if row.get("item_id") == announced["decision_id"]
    ]
    assert len(rows) == 1, body.get("waiting")
    assert rows[0]["wait_type"] == "some_other_wait", rows[0]
