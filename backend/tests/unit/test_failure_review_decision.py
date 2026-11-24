"""failure_review 的失败通告：一条待答决策 + 同轮幂等 + 绝不抛异常。

What this pins
--------------
1. **一条待答决策。** 计划终态失败时，:func:`announce_failure` 建一条
   ``source='failure_review'`` 的决策，轮次与来源都对，状态是
   ``pending`` —— 问题要能在面板上被回答，所以不能是 ``unroutable``。
2. **同 plan 同轮幂等。** 连续两次调用返回同一个 ``decision_id``，
   库里只有一条 —— 失败通告会被重复触发，不能刷屏。
3. **绝不外抛。** 创建决策失败时返回 ``None``，异常不外泄：失败通告
   是旁路，不能把主流程带崩。
4. **旁路记录。** 通告同时落一份 ``failure_review.json``，
   :func:`read_review` 读回它 —— 只读端点与等待行读的是它，不查库。
5. **等人回答，不等时钟。** 失败复盘要的是人工确认，所以这条决策是
   ``strict`` 且没有 deadline：``expire_due`` 扫过两小时后也不能把它
   结算成 ``defaulted`` —— 那等于系统在替操作者回答。

Test command::

    pytest backend/tests/unit/test_failure_review_decision.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, List

import pytest

import failure_review
from config_paths import resolve_state_db_path
from failure_review import (
    FAILURE_REVIEW_FILENAME,
    announce_failure,
    build_question,
    read_review,
)
from hitl.models import DecisionMode, DecisionRequest, DecisionStatus
from hitl.service import answer_decision, create_decision, expire_due
from hitl.store import HitlStore

#: The plan the failure is announced for.
PLAN_ID = "p1"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(isolated_plans_dir) -> Path:
    """A plan directory with nothing in it — no report, no repair tasks."""
    path = Path(isolated_plans_dir) / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def store() -> Iterator[HitlStore]:
    """A store on the per-test database."""
    opened = HitlStore(resolve_state_db_path())
    try:
        yield opened
    finally:
        opened.close()


def _review_decisions(store: HitlStore) -> List[Any]:
    """The plan's failure-review decisions, oldest first."""
    return [
        decision
        for decision in store.list_by_plan(PLAN_ID)
        if decision.source == "failure_review"
    ]


# ---------------------------------------------------------------------------
# 1. One pending decision
# ---------------------------------------------------------------------------


def test_creates_one_pending_decision(plan_dir: Path, store: HitlStore) -> None:
    """一次失败通告 → 一条 pending 的 failure_review 决策。

    状态必须是 ``pending``：问题是要在面板上被回答的，settle 成
    ``unroutable`` 之后就再也答不了。
    """
    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=2)

    assert decision_id, "announce_failure must report the decision it created"
    decisions = _review_decisions(store)
    assert len(decisions) == 1, decisions
    decision = decisions[0]
    assert decision.decision_id == decision_id
    assert decision.source == "failure_review"
    assert decision.round == 2
    assert decision.status == DecisionStatus.PENDING.value


# ---------------------------------------------------------------------------
# 2. Idempotence
# ---------------------------------------------------------------------------


def test_same_plan_same_round_is_idempotent(plan_dir: Path, store: HitlStore) -> None:
    """同一 plan 同一轮再通告一次 → 同一个 id，库里仍然只有一条。

    失败通告会被重复触发（重试、轮询、第二次收尾）。重复触发只能
    复用同一条决策，不能再问一遍。
    """
    first = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)
    second = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    assert first is not None
    assert second == first
    assert len(_review_decisions(store)) == 1


def test_another_round_gets_its_own_decision(plan_dir: Path, store: HitlStore) -> None:
    """换一轮 → 另一条决策：幂等的键是 (plan, round)，不是 plan。"""
    first = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)
    second = announce_failure(PLAN_ID, plan_dir=plan_dir, round=2)

    assert first is not None
    assert second is not None
    assert second != first
    assert len(_review_decisions(store)) == 2


# ---------------------------------------------------------------------------
# 3. Never raises
# ---------------------------------------------------------------------------


def test_never_raises_when_create_fails(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建决策抛异常 → 返回 None，异常不外泄。

    失败通告是**旁路**：主流程已经到终态了，通告失败绝不能把终态
    写入变成一次崩溃。
    """

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the decision store is not reachable")

    monkeypatch.setattr(failure_review, "create_decision", boom)

    result = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    assert result is None


# ---------------------------------------------------------------------------
# 4. The sidecar
# ---------------------------------------------------------------------------


def test_sidecar_written_for_read_review(plan_dir: Path) -> None:
    """通告落一份 failure_review.json，read_review 读回它。"""
    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    review = read_review(plan_dir)

    assert review is not None
    assert review["decision_id"] == decision_id
    assert review["problem"], "the sidecar must name what went wrong"
    assert review["suggestion"], "the sidecar must name what to do about it"
    assert review["created_at"], "the sidecar must say when it was written"
    assert review["plan_id"] == PLAN_ID
    assert review["round"] == 1
    assert review["question"].startswith("[failure_review] ")


def test_sidecar_file_is_named_failure_review_json(plan_dir: Path) -> None:
    """旁路记录的文件名是发布出去的常量。"""
    announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    assert FAILURE_REVIEW_FILENAME == "failure_review.json"
    assert (plan_dir / FAILURE_REVIEW_FILENAME).exists()


def test_plan_dir_defaults_to_the_plans_root(
    isolated_plans_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 plan_dir → 用 ``resolve_plans_dir() / plan_id``。

    计划根目录只有**一个**解析器（``config_paths.resolve_plans_dir``），
    这里读的就是它 —— 旁路记录不能落到别处去。
    """
    monkeypatch.setattr(
        failure_review, "resolve_plans_dir", lambda: Path(isolated_plans_dir)
    )

    decision_id = announce_failure(PLAN_ID, round=1)

    assert decision_id
    assert (
        Path(isolated_plans_dir) / PLAN_ID / FAILURE_REVIEW_FILENAME
    ).exists()


def test_sidecar_write_failure_still_returns_the_decision_id(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旁路记录写不下去 → 只记日志，decision_id 照样返回。

    决策已经问出去了；记录写不进去不该把「问过了」这件事也吞掉。
    """

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("the disk is full")

    monkeypatch.setattr(failure_review, "atomic_write_json", boom)

    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    assert decision_id
    assert not (plan_dir / FAILURE_REVIEW_FILENAME).exists()


def test_read_review_returns_none_without_a_sidecar(plan_dir: Path) -> None:
    """没有旁路记录 → None（不是一个空 dict）。"""
    assert read_review(plan_dir) is None


def test_read_review_returns_none_when_the_sidecar_is_broken(plan_dir: Path) -> None:
    """旁路记录损坏 → None：只读端点不能因为坏 JSON 崩掉。"""
    (plan_dir / FAILURE_REVIEW_FILENAME).write_text("{not json", encoding="utf-8")

    assert read_review(plan_dir) is None


def test_read_review_returns_none_when_a_field_is_missing(plan_dir: Path) -> None:
    """旁路记录缺字段 → None。"""
    (plan_dir / FAILURE_REVIEW_FILENAME).write_text(
        '{"decision_id": "ab12", "problem": "坏了"}', encoding="utf-8"
    )

    assert read_review(plan_dir) is None


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------


def test_build_question_prefixes_the_kind_and_appends_the_suggestion() -> None:
    """问题文本 = '[failure_review] ' + problem + '\\n建议：' + suggestion。

    schema 里没有 ``kind`` 字段，所以「这是一条失败通告」只能靠前缀
    标注；``kind`` 本身由决策的 ``source`` 字段承载。
    """
    assert build_question("验证失败：登录接口 500", "建议修复：修复登录接口") == (
        "[failure_review] 验证失败：登录接口 500\n建议：建议修复：修复登录接口"
    )


# ---------------------------------------------------------------------------
# 5. The question waits for a human, not for the clock
# ---------------------------------------------------------------------------


def _control_request(source: str = "control_test") -> DecisionRequest:
    """An ordinary question — one that *is* allowed to fall back."""
    return DecisionRequest(
        plan_id=PLAN_ID,
        source=source,
        question="[control] 一个普通问题",
        options=["ack"],
        default_option="ack",
    )


def test_the_failure_review_decision_has_no_deadline(
    plan_dir: Path, store: HitlStore
) -> None:
    """失败通告的决策是 ``strict`` 且没有 deadline。

    ``expire_due`` 结算的是「到期的 auto 决策」：一道有截止时间、
    到点就退回默认答案的题。失败复盘问的是「你打算怎么处理这次失败」
    —— 系统替操作者回答「ack」等于把复盘吞掉了。所以这条决策必须
    既没有模式上的退路，也没有时间上的死线。
    """
    announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    decision = _review_decisions(store)[0]
    assert decision.mode == DecisionMode.STRICT.value, decision.mode
    assert decision.timeout_sec is None, decision.timeout_sec
    assert decision.deadline_at is None, decision.deadline_at


def test_the_clock_does_not_settle_the_failure_review_decision(
    plan_dir: Path, store: HitlStore
) -> None:
    """两小时后扫过期：失败通告的决策仍然是 pending，且没有被回答过。

    这是本轮要修的那条回归：决策按计划的默认配置建成 ``auto`` +
    1800 秒，于是 30 分钟后 ``expire_due`` 把它结算成 ``defaulted``
    （``answered_by`` 为空、答案是 ``ack``）—— 系统替操作者回答了一个
    要求人工确认的问题，等待行也就没了。

    **负控在同一个用例里。** 同一个 ``plan_dir`` 上直接建一条普通 auto
    决策（带 ``default_option``），同一条扫描必须仍然把它结算成
    ``defaulted``。否则「失败通告没有被结算」可能只是因为扫描根本没跑
    或者被短路了。
    """
    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)
    decision = _review_decisions(store)[0]
    created = datetime.fromisoformat(decision.created_at)

    moved = expire_due(now=created + timedelta(hours=2), store=store)

    assert moved == 0, "the sweep settled a decision nobody had answered"
    stored = store.get(decision_id)
    assert stored is not None
    assert stored.status == DecisionStatus.PENDING.value, stored.status
    assert stored.answer is None, stored.answer
    assert stored.answered_by is None, stored.answered_by

    # 负控：同一条扫描机，跑得动。
    control = create_decision(
        _control_request(),
        plan_dir,
        store=store,
        settle_unrouted=False,
    )

    moved = expire_due(now=created + timedelta(hours=4), store=store)

    assert moved == 1, "the sweep stopped settling anything"
    control_row = store.get(control.decision_id)
    assert control_row is not None
    assert control_row.status == DecisionStatus.DEFAULTED.value, control_row.status
    stored = store.get(decision_id)
    assert stored is not None
    assert stored.status == DecisionStatus.PENDING.value, (
        "the sweep touched the failure-review decision"
    )


def test_the_pending_decision_can_still_be_answered(
    plan_dir: Path, store: HitlStore
) -> None:
    """答案 API 仍然收这条决策：没有死线不等于不能回答。

    R7 要求「回答只更新 decision、回答后等待行消失」。这里钉住前半句
    —— 决策停在 ``pending``，但它仍然是**可回答**的：答案 API 取走它
    并写成 ``answered``。
    """
    decision_id = announce_failure(PLAN_ID, plan_dir=plan_dir, round=1)

    status, code = answer_decision(decision_id, "ack", store=store)

    assert (status, code) == (DecisionStatus.ANSWERED.value, 200)


def test_require_answer_is_opt_in(plan_dir: Path, store: HitlStore) -> None:
    """其余调用方不受影响：普通决策仍然是 auto + 计划配置的 timeout。

    ``require_answer`` 是显式 opt-in：不传它的调用方看到的和以前
    逐字一样 —— 计划的配置仍然决定 mode 与 timeout。
    """
    decision = create_decision(
        _control_request(),
        plan_dir,
        store=store,
        settle_unrouted=False,
    )

    assert decision.mode == DecisionMode.AUTO.value, decision.mode
    assert decision.timeout_sec == 1800, decision.timeout_sec
    assert decision.deadline_at is not None, decision.deadline_at


def test_require_answer_overrides_the_plan_configuration(
    plan_dir: Path, store: HitlStore
) -> None:
    """``require_answer=True`` → strict + 没有 deadline。

    哪怕计划的配置写着 ``auto`` / 1800：这道题等的是一个人，不是一个
    兜底答案。
    """
    decision = create_decision(
        _control_request("require_answer_test"),
        plan_dir,
        store=store,
        settle_unrouted=False,
        require_answer=True,
    )

    assert decision.mode == DecisionMode.STRICT.value, decision.mode
    assert decision.timeout_sec is None, decision.timeout_sec
    assert decision.deadline_at is None, decision.deadline_at
