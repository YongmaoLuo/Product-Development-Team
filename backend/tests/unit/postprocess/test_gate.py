"""收尾门控：验证通过出口上的那**一个**判定。

What this file witnesses
------------------------
验证通过之后，计划原本直接置 ``completed`` 就算结束。可选的收尾阶段
给「验证通过」与「真的收完」之间留了一个位置，而**进不进那个位置**
只由一件事决定：这个计划有没有声明结构化收尾要求书
（``plans/{id}/postprocess.json``）。

所以这个文件钉住三件事：

1. **判定的两个方向** —— 有要求书 → ``True``；没有 → ``False``。而
   **写坏了**的要求书（不是 JSON、条目为空、条目不合法）也是
   ``False``：门控要是抛异常，验证通过的出口就被一颗坏文件炸掉了。
2. **零侵入基线** —— 没有要求书时，既有路径**逐行**不变：相位仍是
   ``completed``、终态仍记一次、收尾编排器一次都没被叫起来、计划
   目录里也不许凭空多出收尾阶段的文件、人工决策表里也不许凭空多出
   一行（**没有人被问过**，所以没有决策可等）。
3. **有要求书时的出口** —— 相位写成 ``post_processing``、``completed``
   一个字都没写、收尾编排器被叫起来恰好一次。

外加一条**静态合同**：整个验证回路里 ``has_postprocess_spec(`` 只允许
出现一个调用点。门控的价值全在「唯一」上 —— 两个判定点就意味着两条
真相，而两条真相里必有一条是错的。

Why the fake plan state consults the real transition tables
-----------------------------------------------------------
门控写的 ``post_processing`` 是从一个**验证相位**出发的，而那条边在
不在，是状态机的事实，不是测试替身的事实。所以这里的替身不自己编
合法转移表 —— 它去问 :mod:`plan_state` 的真表。一个什么都肯接受的
替身会把「生产状态机会拒绝的相位写入」藏起来。

Why the terminal record is not written on the gated path
--------------------------------------------------------
命中门控的计划**还没收完**：收尾编排器还在跑，这时候发「计划关闭」
是不对的 —— 关闭是编排器终态出口的活。所以命中分支一次终态都不记。

Why the decision table is read through the production facade
------------------------------------------------------------
替身能看见相位、能看见编排器有没有被叫起来，但看不见**决策表** ——
「有没有人等着回答这个问题」这件事记在 ``plan_decisions`` 里，
而替身替不了这张表。所以这一条用生产门面
(:class:`hitl.store.HitlStore`)去读真实的 SQLite：读的库由全局
``isolated_plans_dir`` fixture 重定向到 ``tmp_path``，而且那个
fixture 已经断言过这张表开局是空的 —— 于是「零行」只能是这次门控
写出来的零行，不会是别处的残留。

零行本身还不够：**没人被问过**这句话要么成立，要么不成立，不能由
一个恒真的断言来担保。所以配一条正向对照行（同一个 ``plan_id``、
同一张表、同一个门面写进去），再读一次：读得到，对照就成立；读不到，
「零行」就只是「查不到」的另一种说法。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import plan_state  # noqa: E402

from config_paths import resolve_state_db_path  # noqa: E402
from hitl.models import Decision, DecisionRequest  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from postprocess import spec as spec_module  # noqa: E402
from tests.fixtures.postprocess.builders import make_item, make_spec  # noqa: E402


#: The plan id every case uses — a plain, valid leaf.
PLAN_ID = "p1"

#: The file the requirement book lives in.
SPEC_FILENAME = "postprocess.json"

#: The phase the plan is in while its verification round runs.
VERIFICATION_PHASE = "verification_running"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _plan_dir(tmp_path: Path) -> Path:
    """The plan's own directory — where the requirement book would live."""
    path = tmp_path / "plans" / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_book(plan_dir: Path, items: List[Dict[str, Any]]) -> Path:
    """Write a requirement book into the plan directory.

    Args:
        plan_dir: The plan's directory.
        items: The entries of the book.

    Returns:
        The path of the book that was written.
    """
    path = plan_dir / SPEC_FILENAME
    path.write_text(
        json.dumps(make_spec(items), ensure_ascii=False), encoding="utf-8"
    )
    return path


def _valid_items() -> List[Dict[str, Any]]:
    """One legal entry — enough for the book to load."""
    return [make_item(id="open-pr", action="开一个 PR")]


class _FakePlanState:
    """The plan's state machine, reduced to the two verbs the gate uses.

    The legality of a transition is decided by the **real** tables
    (:mod:`plan_state`), not by this double: the gate writes
    ``post_processing`` out of a verification phase, and whether that
    edge exists is a fact about the state machine.  A double that
    accepted everything would hide a phase write the production state
    machine would have refused.

    Attributes:
        written: Every phase this double was asked to write, in order —
            through :meth:`transition_to` and through
            :meth:`force_set_phase` alike.
    """

    def __init__(self, current_phase: str = VERIFICATION_PHASE) -> None:
        self.current_phase = current_phase
        self.written: List[str] = []

    def transition_to(self, phase: str) -> None:
        """Move to ``phase`` — but only along an edge that really exists."""
        if phase == self.current_phase:
            return
        allowed = set(plan_state.PHASE_TRANSITIONS.get(self.current_phase, []))
        allowed.update(
            plan_state.VERIFICATION_PHASE_TRANSITIONS.get(self.current_phase, [])
        )
        if phase not in allowed:
            raise ValueError(
                f"illegal transition: {self.current_phase} -> {phase}"
            )
        self.written.append(phase)
        self.current_phase = phase

    def force_set_phase(self, phase: str) -> None:
        """Write ``phase`` directly, the way the real state machine does."""
        self.written.append(phase)
        self.current_phase = phase


class _FakeRouting:
    """The routing repository, reduced to the CAS the passed round does."""

    def __init__(self) -> None:
        self.cases: List[Tuple[Any, ...]] = []

    def try_mark_phase(
        self, plan_id: str, expected: Any, phase: str
    ) -> None:
        """Record the phase CAS the round attempted."""
        self.cases.append((plan_id, expected, phase))


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Arm the module: a plan root, a fake routing CAS, two recorders.

    The plan root is redirected at the server module's ``PLANS_DIR`` so
    the round resolves *this* test's plan directory — and nothing of
    the round touches a real state database: the routing CAS and the
    terminal write are both faked.

    Attributes:
        plan_dir: The plan's directory.
        routing: The fake routing repository.
        terminals: Every terminal record the round wrote.
        started: Every plan id the finishing stage was asked to run.
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

    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    return SimpleNamespace(
        plan_dir=plan_dir,
        routing=routing,
        terminals=terminals,
        started=started,
    )


# ---------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------


def test_gate_true_for_plan_with_spec(tmp_path: Path):
    """A plan that declares a requirement book enters the finishing stage."""
    plan_dir = _plan_dir(tmp_path)
    _write_book(plan_dir, _valid_items())

    assert spec_module.has_postprocess_spec(plan_dir) is True


def test_gate_false_without_spec(tmp_path: Path):
    """A plan that declares nothing does not enter the finishing stage."""
    plan_dir = _plan_dir(tmp_path)

    assert spec_module.has_postprocess_spec(plan_dir) is False


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"items": []}', id="no-items"),
        pytest.param('{"items": [{"id": "x"}]}', id="illegal-item"),
    ],
)
def test_gate_false_for_a_broken_book(tmp_path: Path, content: str):
    """A book that does not load is not a book — and it raises nothing.

    The gate decides whether a plan enters the finishing stage.  A gate
    that raised would turn a typo in one file into a verification round
    that cannot record its own verdict.
    """
    plan_dir = _plan_dir(tmp_path)
    (plan_dir / SPEC_FILENAME).write_text(content, encoding="utf-8")

    assert spec_module.has_postprocess_spec(plan_dir) is False


def test_gate_false_for_a_missing_directory(tmp_path: Path):
    """A plan directory that is not there at all is not a book."""
    missing = tmp_path / "plans" / "no-such-plan"

    assert spec_module.has_postprocess_spec(missing) is False


# ---------------------------------------------------------------------
# The zero-intrusion baseline
# ---------------------------------------------------------------------


def test_no_spec_path_keeps_completed_and_records_once(armed: SimpleNamespace):
    """No requirement book — the old path, line for line."""
    plan_state_double = _FakePlanState()

    vl._handle_passed_round(PLAN_ID, 2, plan_state_double)

    assert plan_state_double.written == ["completed"]
    assert len(armed.terminals) == 1
    plan_id, status, stop_reason, _kwargs = armed.terminals[0]
    assert plan_id == PLAN_ID
    assert status == "passed"
    assert stop_reason is None


def test_no_spec_path_never_starts_the_finishing_stage(
    armed: SimpleNamespace,
):
    """No requirement book — the finishing stage is never called."""
    vl._handle_passed_round(PLAN_ID, 2, _FakePlanState())

    assert armed.started == []


def test_no_spec_path_writes_no_finishing_stage_file(
    armed: SimpleNamespace,
):
    """No requirement book — the plan directory gains no finishing file."""
    before = sorted(p.name for p in armed.plan_dir.iterdir())

    vl._handle_passed_round(PLAN_ID, 2, _FakePlanState())

    after = sorted(p.name for p in armed.plan_dir.iterdir())
    assert after == before
    assert not any(name.startswith("postprocess") for name in after)


def test_no_spec_path_creates_no_hitl_decision(armed: SimpleNamespace):
    """No requirement book — nobody is asked, so no decision row exists.

    The baselines above are visible to the **substitutes**: they see
    which phase was written and whether the finishing stage was
    called.  What a substitute cannot see is the *decision table* —
    the place where a plan that needs a human leaves a row behind, and
    the place a waiting plan is read from.  A plan without a
    requirement book must leave that table exactly as it found it: it
    asks nobody, so it waits for nobody.

    So this case reads the table through the **production facade** —
    :class:`hitl.store.HitlStore`, the class the routes and the
    finishing stage use — against the per-test database the suite
    redirects (``PDT_STATE_DB_PATH`` → ``tmp_path``).  That is a real
    SQLite query against a real table, not a stub: the autouse
    ``isolated_plans_dir`` fixture has already asserted that the table
    starts empty, so the rows counted here can only be rows this test
    caused.

    The count is proved load-bearing twice over:

      * a **control row** — inserted through the same store, with the
        same ``plan_id`` — is read back through the same query, so a
        zero means "no rows *of this plan, as this query sees it*"
        rather than "the query cannot see this plan at all";
      * the gate is driven a **second** time and the control row is
        still the only one, so the gate itself wrote nothing.

    The two branch assertions (the phase, and the stage that was not
    started) are what make the negative control bite: answering "yes,
    there is a requirement book" moves the plan into the finishing
    stage, and this case has to go red when it does.
    """
    with HitlStore(resolve_state_db_path()) as store:
        first = _FakePlanState()
        vl._handle_passed_round(PLAN_ID, 2, first)

        # The no-spec branch was taken: the plan completes, and the
        # finishing stage — the thing that asks humans — never runs.
        assert first.written == ["completed"]
        assert armed.started == []

        # Nobody was asked, so the decision table holds no row.
        assert store.list_by_plan(PLAN_ID) == []

        # The control row: same table, same plan, written through the
        # same store.  This is what turns the assertion above from
        # "no rows" into "no rows of this plan, as this query sees it".
        control = Decision.from_request(
            DecisionRequest(
                plan_id=PLAN_ID,
                source="test",
                question="Does this control row show up?",
                options=["yes", "no"],
            )
        )
        store.insert(control)

        assert [
            row.decision_id for row in store.list_by_plan(PLAN_ID)
        ] == [control.decision_id]

        # Drive the gate once more: the control row must still be the
        # only one — the gate adds no decision of its own.
        second = _FakePlanState()
        vl._handle_passed_round(PLAN_ID, 3, second)

        assert second.written == ["completed"]
        assert armed.started == []
        assert [
            row.decision_id for row in store.list_by_plan(PLAN_ID)
        ] == [control.decision_id]


# ---------------------------------------------------------------------
# The gated path
# ---------------------------------------------------------------------


def test_spec_path_writes_post_processing_and_skips_completed(
    armed: SimpleNamespace,
):
    """A requirement book — the plan waits for the finishing stage."""
    _write_book(armed.plan_dir, _valid_items())
    plan_state_double = _FakePlanState()

    vl._handle_passed_round(PLAN_ID, 2, plan_state_double)

    assert "post_processing" in plan_state_double.written
    assert "completed" not in plan_state_double.written


def test_spec_path_starts_the_finishing_stage_exactly_once(
    armed: SimpleNamespace,
):
    """A requirement book — the finishing stage is handed the plan once."""
    _write_book(armed.plan_dir, _valid_items())

    vl._handle_passed_round(PLAN_ID, 2, _FakePlanState())

    assert armed.started == [PLAN_ID]


def test_spec_path_records_no_terminal_while_the_stage_runs(
    armed: SimpleNamespace,
):
    """A requirement book — the plan is not closed while it still finishes.

    The terminal record is what fires ``plan_closed``.  A plan whose
    finishing stage is still running is not closed, so the record is
    not written: closing the plan is the orchestrator's terminal exit.
    """
    _write_book(armed.plan_dir, _valid_items())

    vl._handle_passed_round(PLAN_ID, 2, _FakePlanState())

    assert armed.terminals == []


# ---------------------------------------------------------------------
# The static contract
# ---------------------------------------------------------------------


def _gate_call_sites(function: ast.AST) -> List[ast.Call]:
    """Every call of the gate predicate inside ``function``."""
    sites: List[ast.Call] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "has_postprocess_spec":
            sites.append(node)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "has_postprocess_spec"
        ):
            sites.append(node)
    return sites


def _function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    """The function ``name`` in ``tree`` — the one under test."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def test_gate_is_single_predicate():
    """One decision point — inside the passed round, and nowhere else.

    Two call sites would mean two truths about whether a plan has a
    finishing stage, and one of them would be wrong.
    """
    source = (_BACKEND_DIR / "verification_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    passed_round = _function_named(tree, "_handle_passed_round")
    assert len(_gate_call_sites(passed_round)) == 1

    module_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == "has_postprocess_spec"
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "has_postprocess_spec"
            )
        )
    ]
    assert len(module_sites) == 1
