"""``GET /api/scheduler/plan/{plan_id}/detail`` — the failure review section.

What is pinned here
-------------------
A plan that failed at the end was announced to a human as a **decision**
(``backend/failure_review.py``): the announcement left a sidecar behind
(``<plan_dir>/failure_review.json``) that says **what went wrong** and
**what should happen about it**.

The plan-detail view draws the answer's ``sections``, and it has to show
**that same paragraph** (R5) — the problem and the suggestion the
announcement wrote, not a second phrasing of them.  So the detail
endpoint:

1. **Carries the review.**  The answer grows a ``failure_review`` key
   with the four recorded fields, and the ``sections`` list grows a
   ``kind='failure_review'`` section — **first**, because it is the one
   thing the operator has to act on.  The sections that were there are
   still there, in the same order, below it.
2. **Says nothing when there is nothing to say.**  A plan with no
   sidecar (or a broken one) is answered with ``failure_review=None``
   and no such section: the review is a section of the answer, not a
   new thing the panel has to hide.
3. **Reads.**  Opening a plan changes no byte of it and answers no
   decision: the sidecar's bytes and the decision's status are the
   same before the call and after it.

The request carries ``X-PDT-Request: 1`` — the guard every ``/api/*``
request has to satisfy, and the header the bundled UI sends.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: it is the application, and it is what puts the
# routers on the app.  ``routes.plans`` is imported **after** it — the
# two modules import each other, and the application's own import order
# is the one that resolves the cycle.
import server  # noqa: E402
from config_paths import resolve_state_db_path  # noqa: E402
from failure_review import FAILURE_REVIEW_FILENAME, announce_failure  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from notifications import cards  # noqa: E402
from routes import plans as routes_plans  # noqa: E402

client = TestClient(server.app)

#: The endpoint under test.
DETAIL_PATH = "/api/scheduler/plan/{plan_id}/detail"

#: The header every ``/api/*`` request carries (``request_guard``).
GUARD_HEADERS = {"X-PDT-Request": "1"}

#: The plan under test.  Its id is its own: the failure announcement
#: below creates a **decision** in the session's store, and that store
#: is shared by the whole run — a plan id of its own keeps this test's
#: decision out of another test's way.
PLAN_ID = "failure-review-detail"

#: The kind of the failure review's section.
FAILURE_REVIEW = "failure_review"

#: The title the section carries.
FAILURE_REVIEW_TITLE = "失败分析"

#: What the announcement said — the paragraph both readers show.
PROBLEM = "验证失败：登录接口 500"
SUGGESTION = "建议修复：修复登录接口"

#: When the announcement was made.
CREATED_AT = "2026-11-19T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_plans_dir(monkeypatch, tmp_path):
    """Point ``PLANS_DIR`` at a tmp tree, and keep the listing cache cold."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    server.invalidate_plans_cache()
    yield
    server.invalidate_plans_cache()


@pytest.fixture
def plan_dir() -> Path:
    """The plan's own directory, under the plans root the code reads."""
    path = server.PLANS_DIR / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def stub_status(monkeypatch):
    """Hand the endpoint the plan status the test wants it to read.

    The endpoint asks ``routes.plans.get_plan_status`` for the plan's
    snapshot; this replaces that one call with the payload the test
    hands in, so that the facts under test are the ones the test wrote
    down.  A **failed** plan is the one that can carry a failure
    review, so that is the status these tests hand in.
    """

    def _stub(payload: Dict[str, Any]) -> None:
        monkeypatch.setattr(
            routes_plans,
            "get_plan_status",
            lambda plan_id: dict(payload),
        )

    return _stub


def _seed_sidecar(plan_dir: Path, **overrides: Any) -> Dict[str, Any]:
    """Write the sidecar the failure announcement leaves behind.

    The record is the one :func:`failure_review.announce_failure` writes:
    the four fields a reader needs, plus the announcement's own
    bookkeeping.
    """
    record: Dict[str, Any] = {
        "decision_id": "ab12",
        "problem": PROBLEM,
        "suggestion": SUGGESTION,
        "created_at": CREATED_AT,
        "plan_id": PLAN_ID,
        "round": 1,
        "question": f"[failure_review] {PROBLEM}\n建议：{SUGGESTION}",
    }
    record.update(overrides)
    (plan_dir / FAILURE_REVIEW_FILENAME).write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return record


def _detail(plan_id: str = PLAN_ID):
    """Ask for one plan's detail."""
    return client.get(
        DETAIL_PATH.format(plan_id=plan_id), headers=dict(GUARD_HEADERS)
    )


# ---------------------------------------------------------------------------
# 1 — the review is in the answer
# ---------------------------------------------------------------------------


def test_detail_includes_failure_review_section(stub_status, plan_dir: Path) -> None:
    """失败复盘出现在详情里：``sections[0]`` 是它，顶层也带着那四个字段。

    详情视图要显示**同一段** problem + suggestion（R5）—— 通告写了什么，
    这里就显示什么，不是另写一遍。段落放在**首位**：它是操作员唯一要动
    的东西，应该先看见。已有的段落一个不少、一段不改，只是排在它后面。
    """
    stub_status({"current_phase": "failed"})
    record = _seed_sidecar(plan_dir)

    response = _detail()

    assert response.status_code == 200, response.text
    body = response.json()

    section = body["sections"][0]
    assert section["kind"] == FAILURE_REVIEW, body["sections"]
    assert section["title"] == FAILURE_REVIEW_TITLE, section
    assert section["problem"] == PROBLEM, section
    assert section["suggestion"] == SUGGESTION, section

    assert body["failure_review"] == {
        "decision_id": record["decision_id"],
        "problem": PROBLEM,
        "suggestion": SUGGESTION,
        "created_at": CREATED_AT,
    }, body

    # The sections that were there are still there, in the card's own
    # order — the review is inserted, nothing is rewritten.
    expected = cards.build_progress_sections(
        {"current_phase": "failed"},
        body["running_sec"],
        body["phase_label"],
    )
    assert body["sections"][1:] == expected, body["sections"]


def test_detail_without_record_has_no_section(stub_status, plan_dir: Path) -> None:
    """没有旁路记录 → ``failure_review`` 是 None，也没有这一段。

    失败复盘是答案里的一段，不是面板要另外藏起来的东西：没有通告过的
    计划，详情里就没有它。
    """
    stub_status({"current_phase": "failed"})

    response = _detail()

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["failure_review"] is None, body
    kinds = [section.get("kind") for section in body["sections"]]
    assert FAILURE_REVIEW not in kinds, kinds


def test_detail_with_a_broken_record_has_no_section(
    stub_status, plan_dir: Path
) -> None:
    """坏掉的旁路记录 → 没有这一段，也不抛异常。

    面板每几秒就会读一次详情：一个读不出来的文件不能让详情整个挂掉。
    """
    stub_status({"current_phase": "failed"})
    (plan_dir / FAILURE_REVIEW_FILENAME).write_text("{not json", encoding="utf-8")

    response = _detail()

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["failure_review"] is None, body
    kinds = [section.get("kind") for section in body["sections"]]
    assert FAILURE_REVIEW not in kinds, kinds


# ---------------------------------------------------------------------------
# 2 — reading is reading
# ---------------------------------------------------------------------------


def test_detail_is_read_only(stub_status, plan_dir: Path) -> None:
    """打开详情不改计划的一个字节，也不回答那条决策。

    详情端点是**读**：它读出通告留下的旁路记录就交出去。写一份、或者
    把决策改成「已回答」，都是第二个写方 —— 那样面板上的等待行和这条
    决策就会各说各话。
    """
    stub_status({"current_phase": "failed"})

    # The production path: the announcement creates the decision and
    # leaves the sidecar behind.
    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)
    assert decision_id, "the failure must be announced as a decision"

    sidecar = plan_dir / FAILURE_REVIEW_FILENAME
    before_bytes = sidecar.read_bytes()
    with HitlStore(resolve_state_db_path()) as store:
        before_status = store.get(decision_id).status

    response = _detail()
    assert response.status_code == 200, response.text

    assert sidecar.read_bytes() == before_bytes, "the sidecar was written to"
    with HitlStore(resolve_state_db_path()) as store:
        after_status = store.get(decision_id).status
    assert after_status == before_status, (before_status, after_status)
