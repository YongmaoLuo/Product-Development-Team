"""The framework-fix API guards, seen from the outside.

What this file pins
-------------------
* a plan **held** by a framework defect cannot start a verification
  round — 409, and the refusal names the defect it is held on;
* a verification point of such a plan cannot be **settled with a skip**:
  the review surface refuses the skip (409) without writing the review
  state, and the result table refuses the ``skipped`` verdict — so the
  point stays unsettled while the defect is still there;
* a plan that is **not** held keeps its ordinary behaviour: the skip
  lands, and a verdict that is not a skip still lands;
* no parameter combination talks that guard out of it: the guard is
  read before the request's own parameters are looked at, so a body
  that asks for a bigger budget, another tool or a forced run is
  refused exactly like an empty body is;
* an injected value never reaches a path: a payload that is not the
  shape the endpoint accepts is refused by the request-validation
  layer (422) before any handler logic runs, and a ``kind`` — the
  filter is a **closed set** — is refused with 422 wherever the
  application has a slot to fill it;
* a dismissal without a reason is 400 (a dismissal nobody can explain
  is not a dismissal), and a plan nobody knows about is 404;
* **no refusal carries internals**: no stack trace, no SQL fragment,
  no absolute path — the answer says what happened, not what the
  server was doing when it found out;
* ``auto_merge`` is off unless somebody explicitly turned it on, and
  while it is off there is no automatic merge path anywhere.

The data is real
----------------
The hold is written through the production writer
(``PlanState.enter_block`` — the same call the framework-fix lane
makes), the registry rows come from the production registry, and both
land in the same migrated SQLite file the endpoints read (the
``state_db`` fixture).  Nothing here is a double: a guard that only
holds against a fake database is not a guard.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


#: The fingerprint the held plans in this file are held on.  It is a
#: **framework defect** fingerprint (the one the plan is blocked on),
#: not a tree fingerprint.
HELD_FINGERPRINT = "sha256:" + "f" * 64

#: The error word a held plan's refusal carries.
BLOCKED = "blocked_on_framework_fix"

#: The error word a guard that could not read the hold state answers
#: with.  It is deliberately not :data:`BLOCKED`: the guard does not
#: know whether the plan is held, and saying it is would dress a guess
#: up as an answer.
GUARD_UNAVAILABLE = "framework_fix_guard_unavailable"

#: The error word the dismissal endpoint refuses with when nobody said
#: why the entry is going away.
MISSING_REASON = "missing_reason"

#: The error word the dismissal endpoint answers with for a plan it
#: has never heard of.
PLAN_NOT_FOUND = "plan_not_found"

#: The error word the dismissal endpoint answers with while somebody
#: is still repairing the defect.
FIX_ACTIVE = "framework_fix_active"

#: The path and the query the guard's failure carries behind it.  They
#: are the things an error body must never repeat back: a path on this
#: machine, and a piece of SQL.
SECRET_PATH = "/srv/pdt/state.db"
SECRET_SQL = "SELECT framework_fix FROM plan_routing WHERE plan_id = ?"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plans tree of this test's own."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.PLANS_DIR", root)
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


def _seed_plan(
    plans_root: Path,
    plan_id: str,
    plan_sqlite_seeder: Any = None,
    *,
    phase: str = "executing",
) -> Path:
    """A plan in this test's tree, materialised in both stores."""
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    if plan_sqlite_seeder is not None:
        plan_sqlite_seeder(plan_dir, plan_id, phase=phase)
    return plan_dir


def _hold(plan_dir: Path, plan_id: str) -> None:
    """Hold the plan on a framework defect — through the production writer."""
    from plan_state import PlanState

    PlanState(plan_dir).enter_block(
        plan_id,
        fingerprint=HELD_FINGERPRINT,
        fix_plan_id="fx-fix-0001",
    )


def _release(plan_dir: Path, plan_id: str) -> None:
    """Release the plan from the framework defect — through the production writer."""
    from plan_state import PlanState

    PlanState(plan_dir).exit_block(plan_id)


def _registry() -> Any:
    """The registry, pointed at the test's own state database."""
    from framework_fix.registry import FrameworkFixRegistry

    return FrameworkFixRegistry()


def _register_defect(registry: Any) -> str:
    """Register a defect the way the lane does — status ``open``."""
    return registry.register_hit(
        {
            "plan_id": "biz-plan",
            "vp_id": "VP-1",
            "framework_files": ["backend/framework_fix/registry.py"],
            "error_class": "AssertionError",
            "first_error_line": "AssertionError: expected 1, got 2",
            "repo": "fixture-repo",
        }
    )


def _seed_block(plan_dir: Path, plan_id: str, *, fingerprint: str) -> None:
    """Give the plan a framework-fix block — through the production writer."""
    from plan_state import PlanState

    PlanState(plan_dir).enter_block(
        plan_id, fingerprint=fingerprint, fix_plan_id="fx-fix-0001"
    )


def _routing_phase(plan_id: str) -> Optional[str]:
    """The plan's routing phase, as stored."""
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _dismiss(plan_id: str, **body: Any) -> Any:
    """Dismiss an entry with the given body."""
    return client.post(
        f"/api/plan/{plan_id}/framework-fix/dismiss",
        json=body or None,
    )


def _kind_query_slots() -> List[Tuple[str, str]]:
    """Every place the application lets a caller name a ``kind``.

    Read from the application's own OpenAPI document, so the answer is
    the contract the server publishes rather than a guess about which
    route owns the filter.
    """
    slots: List[Tuple[str, str]] = []
    for path, operations in app.openapi().get("paths", {}).items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters") or []:
                if parameter.get("name") == "kind" and parameter.get(
                    "in"
                ) == "query":
                    slots.append((path, method))
    return sorted(slots)


# ---------------------------------------------------------------------------
# 1. The hold guard on the verification surface
# ---------------------------------------------------------------------------
def test_start_guard_cannot_bypass(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """被挂起的计划起不了验证 —— 任何参数组合都是 409。

    The guard is read before the request's own parameters are looked
    at, so a body that asks for a bigger budget, another tool or a
    forced run buys nothing: every one of them is refused exactly like
    an empty body is, and the refusal always names the defect.
    """
    plan_id = "p1"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _hold(plan_dir, plan_id)

    bodies: List[Optional[Dict[str, Any]]] = [
        None,
        {},
        {"force": True},
        {"auto_fix": False},
        {"max_rounds": 3},
        {"max_rounds": 0},
        {"max_parallel": 8},
        {"tool": "../"},
        {"force": True, "auto_fix": True, "max_rounds": 1000},
    ]

    for body in bodies:
        if body is None:
            response = client.post(f"/api/verification/{plan_id}/start")
        else:
            response = client.post(
                f"/api/verification/{plan_id}/start", json=body
            )

        assert response.status_code == 409, (
            f"被挂起的计划不许起验证（body={body!r}）；实际 "
            f"{response.status_code} {response.text}"
        )
        payload = response.json()
        assert payload["error"] == BLOCKED, (
            f"拒绝得说清是框架缺陷挡的；实际 {payload!r}"
        )
        assert payload["fingerprint"] == HELD_FINGERPRINT, (
            f"拒绝得带上挂住它的那个缺陷的指纹；实际 {payload!r}"
        )

    # Nothing got through: the refusal happens before the routing CAS,
    # so the plan is still in the phase it was in.
    assert _routing_phase(plan_id) == "executing", (
        "被拒绝的启动不许改动计划的相位；实际 "
        f"{_routing_phase(plan_id)!r}"
    )


# ---------------------------------------------------------------------------
# 2. An injected value never reaches a path
# ---------------------------------------------------------------------------
def test_kind_injection_422() -> None:
    """非法 kind → 422：注入的值进不了处理器，更进不了路径。

    The kind filter is a **closed set** — ``all`` / ``plans`` /
    ``framework_fix`` — so a value that tries to walk out of it
    (``../``) is not a kind at all.  Two shapes of the same rule are
    pinned here:

    * an injected **payload** — a body that is not the object the
      endpoint accepts — is refused by the request-validation layer
      with 422, before any handler logic runs;
    * an injected **query value** is refused with 422 wherever the
      application publishes a ``kind`` slot to fill.  While this branch
      publishes no such slot, the injection is inert instead: there is
      nothing to bind it to, and the answer is the answer one gets
      without the parameter — byte for byte.

    Either way the injected value is never *honoured*: it is refused
    where there is a slot for it, and ignored where there is none.
    """
    # --- an injected payload is refused by the validation layer --------
    for url in (
        "/api/plan/p1/framework-fix/dismiss",
        "/api/verification/p1/start",
    ):
        response = client.post(url, json="../")

        assert response.status_code == 422, (
            f"注入的载荷必须被校验层拒掉（url={url!r}）；实际 "
            f"{response.status_code} {response.text}"
        )
        # The refusal names the offending slot and nothing else: no
        # stack trace, no path of this machine, no SQL.
        assert "Traceback" not in response.text
        assert SECRET_PATH not in response.text
        assert SECRET_SQL not in response.text

    # --- an injected kind is refused where there is a slot for it ------
    slots = _kind_query_slots()
    for path, method in slots:
        response = client.request(method, path, params={"kind": "../"})

        assert response.status_code == 422, (
            f"非法 kind 必须被拒（path={path!r}）；实际 "
            f"{response.status_code} {response.text}"
        )
        assert "Traceback" not in response.text
        assert SECRET_PATH not in response.text

    if not slots:
        # No slot, so nothing for the injection to be bound to: the
        # answer is the answer one gets without the parameter.
        baseline = client.get("/api/scheduler/state").json()
        injected = client.get(
            "/api/scheduler/state", params={"kind": "../"}
        ).json()
        # The snapshot stamps the moment it was taken; that is the one
        # field the two answers are allowed to disagree on.
        baseline.pop("generated_at", None)
        injected.pop("generated_at", None)
        assert injected == baseline, (
            "注入的 kind 不许改变任何东西；实际 "
            f"{injected!r} != {baseline!r}"
        )

    # --- and the framework-fix surfaces ignore an injected kind --------
    without = client.post("/api/plan/p1/framework-fix/dismiss", json={})
    with_kind = client.post(
        "/api/plan/p1/framework-fix/dismiss", params={"kind": "../"}, json={}
    )
    assert with_kind.status_code == without.status_code == 400, (
        f"注入的 kind 不许改变拒绝的答案；实际 {with_kind.status_code} "
        f"{with_kind.text} vs {without.status_code} {without.text}"
    )
    assert with_kind.json() == without.json()


# ---------------------------------------------------------------------------
# 3. The dismissal's own guards
# ---------------------------------------------------------------------------
def test_dismiss_missing_reason_400(plans_root: Path, state_db: Path) -> None:
    """缺 reason → 400：没人能解释的删除不是删除。"""
    plan_id = "p2"
    _seed_plan(plans_root, plan_id)

    bodies: List[Any] = [
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": None},
        {"reason": 42},
    ]

    for body in bodies:
        response = _dismiss(plan_id, **body) if body else client.post(
            f"/api/plan/{plan_id}/framework-fix/dismiss", json=body
        )
        assert response.status_code == 400, (
            f"缺 reason 必须被拒（body={body!r}）；实际 "
            f"{response.status_code} {response.text}"
        )
        assert response.json()["error"] == MISSING_REASON, (
            f"拒绝得说清是缺原因；实际 {response.json()!r}"
        )

    # No body at all is the same refusal.
    response = client.post(f"/api/plan/{plan_id}/framework-fix/dismiss")
    assert response.status_code == 400, (
        f"没有 body 也是缺 reason；实际 {response.status_code} {response.text}"
    )
    assert response.json()["error"] == MISSING_REASON


def test_dismiss_unknown_plan_404(plans_root: Path, state_db: Path) -> None:
    """计划不存在 → 404。"""
    response = client.post(
        "/api/plan/no-such-plan/framework-fix/dismiss",
        json={"reason": "noise"},
    )

    assert response.status_code == 404, (
        f"没人知道的计划 → 404；实际 {response.status_code} {response.text}"
    )
    assert response.json()["error"] == PLAN_NOT_FOUND


def test_dismiss_active_defect_409(plans_root: Path, state_db: Path) -> None:
    """缺陷还在修 → 409，条目一条不少。"""
    registry = _registry()
    fingerprint = _register_defect(registry)  # status stays ``open``

    plan_id = "p3"
    plan_dir = _seed_plan(plans_root, plan_id)
    _seed_block(plan_dir, plan_id, fingerprint=fingerprint)

    response = _dismiss(plan_id, reason="noise")

    assert response.status_code == 409, (
        f"还在修的缺陷不许删；实际 {response.status_code} {response.text}"
    )
    assert response.json()["error"] == FIX_ACTIVE


# ---------------------------------------------------------------------------
# 4. The refusals say nothing about the server's innards
# ---------------------------------------------------------------------------
def test_error_no_leak(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """错误响应不回显堆栈、SQL 与绝对路径。

    Every refusal the framework-fix surface can produce is collected
    here and read as raw text: the answers must say **what happened**,
    not what the server was doing when it found out.  The last one is
    the interesting one — a guard that could not read the hold state
    fails closed with 500, and the exception behind it names a path
    and a query; neither may travel to the caller.
    """
    plan_id = "p4"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _hold(plan_dir, plan_id)

    refusals = [
        client.post(f"/api/verification/{plan_id}/start", json={}),
        client.post(f"/api/plan/{plan_id}/framework-fix/dismiss", json={}),
        client.post(
            "/api/plan/no-such-plan/framework-fix/dismiss",
            json={"reason": "noise"},
        ),
    ]

    # The guard that could not read the hold state fails closed, and its
    # refusal is as quiet as the others: the exception behind it names a
    # path and a query, and neither may travel to the caller.
    import plan_state

    def _explode(_plan_id: str) -> Any:
        raise RuntimeError(
            f"the guard could not read {SECRET_PATH}: {SECRET_SQL}"
        )

    monkeypatch.setattr(plan_state, "_read_routing_framework_fix", _explode)
    refusals.append(client.post(f"/api/verification/{plan_id}/start", json={}))

    for response in refusals:
        text = response.text
        assert response.status_code in (400, 404, 409, 500), (
            f"这一批回答都是拒绝；实际 {response.status_code} {text}"
        )
        assert "Traceback" not in text, f"不许带堆栈；实际 {text!r}"
        assert 'File "' not in text, f"不许带堆栈；实际 {text!r}"
        assert SECRET_PATH not in text, f"不许带绝对路径；实际 {text!r}"
        assert SECRET_SQL not in text, f"不许带 SQL；实际 {text!r}"
        for fragment in ("sqlite3", "SELECT ", "INSERT ", "plan_routing"):
            assert fragment not in text, (
                f"不许带数据库的内部细节（{fragment!r}）；实际 {text!r}"
            )
        assert str(plans_root) not in text, (
            f"不许带这台机器上的路径；实际 {text!r}"
        )

    # And the guard that could not read the state did refuse, rather
    # than letting the start through.
    unavailable = refusals[-1]
    assert unavailable.status_code == 500, (
        f"守卫读不到状态就不许放行；实际 {unavailable.status_code} "
        f"{unavailable.text}"
    )
    assert unavailable.json()["error"] == "framework_fix_guard_unavailable"


# ---------------------------------------------------------------------------
# 5. The merge switch is off, and nothing merges by itself
# ---------------------------------------------------------------------------
def test_auto_merge_off_has_no_auto_merge_path() -> None:
    """auto_merge 没显式打开时，既是关的，也没有自动合并的路。

    Three facts, in the order they matter:

    * the switch is **off** when nobody said anything — the shipped
      configuration and a configuration that says nothing at all both
      answer ``False``;
    * a value that *looks* like an attempt to turn it on (a word, a
      number) leaves it off: only the boolean ``true`` turns it on;
    * and nothing outside the configuration parser reads the switch, so
      there is no path on which a fix merges itself while the switch is
      off.  A reader that appears elsewhere has to be reviewed — it is
      the one place where somebody's work could be merged without them.
    """
    from config import load_config

    assert load_config({})["framework_fix"]["auto_merge"] is False, (
        "没人配置时合并开关必须是关的"
    )
    assert load_config()["framework_fix"]["auto_merge"] is False, (
        "仓库自带的配置里合并开关必须是关的"
    )
    for impostor in ("true", "True", "yes", 1, "on"):
        assert load_config(
            {"framework_fix": {"auto_merge": impostor}}
        )["framework_fix"]["auto_merge"] is False, (
            f"只有布尔 true 才算打开（{impostor!r}）"
        )

    # No production module reads the switch: the configuration parser
    # is the only place the word appears.
    backend_dir = Path(__file__).resolve().parents[2]
    readers = []
    for path in sorted(backend_dir.rglob("*.py")):
        parts = path.parts
        if "tests" in parts or ".venv" in parts or "__pycache__" in parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "auto_merge" in source:
            readers.append(str(path.relative_to(backend_dir)))
    assert readers == ["config.py"], (
        "合并开关只允许配置解析器读它 —— 别的模块读它，就是一条自动合并的"
        f"路；实际读到它的模块：{readers!r}"
    )


# ---------------------------------------------------------------------------
# 6. A held verification point cannot be settled with a skip
# ---------------------------------------------------------------------------
def _verification_plan_doc(plan_dir: Path) -> Path:
    """Give the plan a reviewable verification plan — the canonical one."""
    from tests.unit.test_verification_plan_review import CANONICAL_MD

    path = plan_dir / "verification-plan.md"
    path.write_bytes(CANONICAL_MD.encode("utf-8"))
    return path


def _declare_vps(plan_dir: Path, *vp_ids: str) -> Path:
    """Declare the plan's verification points — the file the lane reads.

    ``_remaining_vps`` reads two sources: the declared points (this
    file) and the judged ones (the result table).  A declared point
    that has never been judged is still owed.
    """
    path = plan_dir / "verification_plan.json"
    path.write_text(
        json.dumps(
            {"verification_points": [{"id": vp_id} for vp_id in vp_ids]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _review_items(plan_id: str) -> List[Dict[str, Any]]:
    """The plan's review items, read from the endpoint."""
    response = client.get(f"/api/verification-plan/{plan_id}/review/items")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _first_vp_index(items: List[Dict[str, Any]]) -> int:
    """The index of the first verification point in a list of items."""
    return next(
        item["index"]
        for item in items
        if str(item["id"]).startswith("VP-")
    )


def test_held_plan_review_skip_returns_409(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """被挂起的验证点不许被 skip 结清 —— 409，审核状态一个字都不改。

    A skip says "this verification point does not have to be verified".
    While the plan is held by a framework defect, that is exactly the
    sentence nobody may write: the point is held *because* it has not
    been verified, and skipping it would settle the debt while the
    defect that caused the hold is still there.

    The refusal happens before the review state is written, so the file
    on disk still says ``pending``.
    """
    plan_id = "p5"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _verification_plan_doc(plan_dir)
    _hold(plan_dir, plan_id)

    vp_index = _first_vp_index(_review_items(plan_id))

    response = client.post(
        f"/api/verification-plan/{plan_id}/review/item/{vp_index}",
        json={"action": "skip", "note": "held by a framework defect"},
    )

    assert response.status_code == 409, (
        f"被挂起的验证点不许被 skip；实际 {response.status_code} "
        f"{response.text}"
    )
    assert response.json().get("error") == BLOCKED, (
        f"拒绝得说清是框架缺陷挡的；实际 {response.json()!r}"
    )

    entry = next(
        item for item in _review_items(plan_id) if item["index"] == vp_index
    )
    assert entry["status"] == "pending", (
        f"被拒绝的 skip 不许写进审核状态；实际 {entry!r}"
    )


def test_released_plan_skip_still_allowed(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """解挂之后同一个 skip 请求照旧能落 —— 守卫拦的是挂起，不是 skip 本身。

    这是闸门的另一半：挂起时拒绝，解挂后放行。守卫要是拦死了 skip，
    缺陷修好之后那个验证点就再也没人能结清 —— 计划会永远欠着它。
    """
    plan_id = "p6"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _verification_plan_doc(plan_dir)
    _hold(plan_dir, plan_id)

    vp_index = _first_vp_index(_review_items(plan_id))
    body = {"action": "skip", "note": "not applicable to this plan"}

    held = client.post(
        f"/api/verification-plan/{plan_id}/review/item/{vp_index}", json=body
    )
    assert held.status_code == 409, (
        f"挂起期间 skip 得被拒；实际 {held.status_code} {held.text}"
    )

    _release(plan_dir, plan_id)

    response = client.post(
        f"/api/verification-plan/{plan_id}/review/item/{vp_index}", json=body
    )

    assert response.status_code == 200, (
        f"解挂之后同一个 skip 请求得成功；实际 {response.status_code} "
        f"{response.text}"
    )
    entry = next(
        item for item in _review_items(plan_id) if item["index"] == vp_index
    )
    assert entry["status"] == "skipped", (
        f"skip 落盘后状态得是 skipped；实际 {entry!r}"
    )


def test_held_plan_append_verification_result_rejects_skipped(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """被挂起的计划不许用一条 skipped 结论把验证点结清。

    The result table is append-only, and the verification loop writes
    one row per verification point per round.  A ``skipped`` row for a
    point that is **held** says "this point does not have to be
    verified" — the same sentence the review surface is not allowed to
    say.  The write is refused with an error the caller can map to 409,
    and no row lands: the point stays unsettled, and the plan keeps
    owing it.
    """
    from framework_fix.lifecycle import FrameworkFixLifecycle
    from verification_persistence import (
        BlockedOnFrameworkFix,
        append_verification_result,
        read_verification_results,
    )

    plan_id = "p7"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _declare_vps(plan_dir, "VP-1")
    _hold(plan_dir, plan_id)

    with pytest.raises(BlockedOnFrameworkFix) as raised:
        append_verification_result(
            plan_id=plan_id,
            vp_id="VP-1",
            tree_fingerprint="sha256:" + "a" * 64,
            verdict="skipped",
            round_no=1,
            db_path=state_db,
        )

    assert raised.value.error == BLOCKED, (
        f"拒绝得说清是框架缺陷挡的；实际 {raised.value.error!r}"
    )
    assert raised.value.status_code == 409, (
        f"这个错误得能映射成 409；实际 {raised.value.status_code!r}"
    )
    # The refusal names the plan and the defect it is held on.  The
    # verification loop catches this exception and logs it, so the
    # message is the only thing that tells the operator **which** defect
    # to go and fix.
    assert plan_id in str(raised.value), (
        f"拒绝得说清是哪个计划；实际 {str(raised.value)!r}"
    )
    assert HELD_FINGERPRINT in str(raised.value), (
        f"拒绝得说清是哪个缺陷挡的；实际 {str(raised.value)!r}"
    )
    assert read_verification_results(plan_id=plan_id, db_path=state_db) == [], (
        "被拒绝的写入不许落行"
    )

    # The point is still owed.  A refused write must not settle the
    # debt the hold was created for.
    remaining = FrameworkFixLifecycle()._remaining_vps(plan_id)
    assert "VP-1" in remaining, (
        f"被拒绝的 skip 不许结清欠账；实际剩下 {remaining!r}"
    )

    # The negative control: the same write on a plan that is **not**
    # held does land, and does settle the point.  Without this the
    # assertion above could be vacuous — ``_remaining_vps`` might list
    # every declared point whatever the results say.
    control_id = "p7-control"
    control_dir = _seed_plan(plans_root, control_id, plan_sqlite_seeder)
    _declare_vps(control_dir, "VP-1")
    append_verification_result(
        plan_id=control_id,
        vp_id="VP-1",
        tree_fingerprint="sha256:" + "a" * 64,
        verdict="skipped",
        round_no=1,
        db_path=state_db,
    )
    assert "VP-1" not in FrameworkFixLifecycle()._remaining_vps(control_id), (
        "没被挂起时一条 skipped 结论确实会结清这个验证点 —— 上面的断言"
        "才不是空转"
    )


def test_held_plan_non_skip_verdict_unaffected(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
) -> None:
    """挂起期间与 skip 无关的结论保持原语义。

    A hold says "this plan's work is blocked by a framework defect".
    It does not say "this plan may not record what it observed": a
    verdict that is not a skip still lands, exactly as it did before
    the hold.
    """
    from verification_persistence import (
        append_verification_result,
        read_verification_results,
    )

    plan_id = "p8"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _hold(plan_dir, plan_id)

    append_verification_result(
        plan_id=plan_id,
        vp_id="VP-1",
        tree_fingerprint="sha256:" + "a" * 64,
        verdict="passed",
        round_no=1,
        db_path=state_db,
    )
    append_verification_result(
        plan_id=plan_id,
        vp_id="VP-2",
        tree_fingerprint="sha256:" + "a" * 64,
        verdict="failed",
        round_no=1,
        db_path=state_db,
    )

    rows = read_verification_results(plan_id=plan_id, db_path=state_db)
    assert [row["verdict"] for row in rows] == ["passed", "failed"], (
        f"与 skip 无关的结论照旧落盘；实际 {rows!r}"
    )


def test_hold_state_unreadable_skip_fails_closed(
    plans_root: Path,
    state_db: Path,
    plan_sqlite_seeder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起状态读不出来时，skip 也不许放行 —— 500，不改审核状态。

    "读不到" 不是 "没挂起"。  A guard that answered "not held" when
    it could not read would let a held plan through the very gate that
    exists to hold it back — and the caller would have no way to tell
    the difference between "there is no hold" and "I could not look".
    """
    import plan_state

    plan_id = "p9"
    plan_dir = _seed_plan(plans_root, plan_id, plan_sqlite_seeder)
    _verification_plan_doc(plan_dir)
    _hold(plan_dir, plan_id)

    vp_index = _first_vp_index(_review_items(plan_id))

    original = plan_state._read_routing_framework_fix

    def _unreadable(plan_id: str) -> Any:
        raise RuntimeError("the routing state could not be read")

    monkeypatch.setattr(plan_state, "_read_routing_framework_fix", _unreadable)
    try:
        response = client.post(
            f"/api/verification-plan/{plan_id}/review/item/{vp_index}",
            json={"action": "skip", "note": "held by a framework defect"},
        )
    finally:
        monkeypatch.setattr(
            plan_state, "_read_routing_framework_fix", original
        )

    assert response.status_code == 500, (
        f"读不到挂起状态时守卫必须拒绝放行；实际 {response.status_code} "
        f"{response.text}"
    )
    assert response.json().get("error") == GUARD_UNAVAILABLE, (
        f"拒绝得说清是守卫读不到状态；实际 {response.json()!r}"
    )

    entry = next(
        item for item in _review_items(plan_id) if item["index"] == vp_index
    )
    assert entry["status"] == "pending", (
        f"被拒绝的 skip 不许写进审核状态；实际 {entry!r}"
    )
