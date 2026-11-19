"""端到端验收：验证收敛闭环（2026-11-01「refiner / 基线漂移」计划 §1–§5）。

前面十几个任务各自改了一处：归属表（谁认领了这个失败文件）、轮次归还
（没测到东西的一轮不消耗轮次号）、心跳（长 LLM 调用不算卡死）、轮次戳
（关卡只读本轮结论）。每条线都有自己的单测，但"每条线都绿"不等于"整个闭环
收敛"——真正要证明的是：这些改动**串在同一个进程里**跑，回路仍然往前走。

四条用例（对应上游架构决策点 1–5）
----------------------------------
1. ``test_blocked_round_keeps_number`` —— 基线漂移挡下的那一轮**不消耗轮次号**
   （决策点 2）：轮次号被归还，``plan_verification.round`` 回到 0，释放记录写进
   ``executor_state["released_rounds"]``。
2. ``test_unclaimed_file_gets_repair_task`` —— 失败文件**无人认领**时，归属表把
   它交给 refiner，refiner 产出新任务，回路继续（决策点 1）：本轮不停在"没人
   认领"这个事实上。
3. ``test_heartbeat_prevents_stale_and_gate_uses_current_round`` —— 心跳救回长
   LLM 调用（决策点 4），且关卡只读**本轮**结论（决策点 3 / 5）：上一轮的
   FAILED 不得让本轮的 Phase 2 被判 DEFERRED。
4. ``test_released_round_reused_not_resumed`` —— 归还的轮次号下一次 ``/start``
   **重新跑一遍**（不是 resume）（决策点 2 / 5）。

驱动方式
--------
进程内 ASGI（``fastapi.testclient.TestClient``）—— 等价于起一个后端服务，但不
占端口、不依赖外部进程。"用户点开始验证" = ``POST
/api/verification/{plan_id}/start``，请求头带 ``X-PDT-Request: 1``（后端的对内
请求护栏）。

边界
----
每个用例自建临时 plans 目录与 SQLite；假执行器 / 假 coding_tool 在 ``finally``
里 release + join；任何断言失败都会把该轮 ``execution.log`` 的最后 20 行打出来。

产物
----
``acceptance_report.json`` 落在 plan 目录里，五个字段就是这一轮闭环的结论：
``blocked_round_advanced`` / ``unclaimed_refined`` / ``stale_on_heartbeat`` /
``phase2_deferred`` / ``round_final``。
"""

from __future__ import annotations

import functools
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: it imports ``verification_loop`` and puts it into
# ``sys.modules`` — the module-level names below must be the SAME objects
# the server holds, or a monkeypatch would land on a copy.
import server as _server  # noqa: E402
from server import app  # noqa: E402
import verification_loop  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.execution_repository import (  # noqa: E402
    ExecutionRepository,
)
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)
from verification_executor import VerificationExecutor  # noqa: E402
from verification_phases import PHASE_CORE, PHASE_FINAL_GATE  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The plan under test. Date-prefixed so ``classify_plan`` does not read it
#: as an archived plan.
PLAN_ID = "20261102-convergence-acceptance"

#: The request header the backend's guard expects on an internal call.
REQUEST_HEADERS = {"X-PDT-Request": "1"}

#: The acceptance artefact, written into the plan directory.
ACCEPTANCE_REPORT_NAME = "acceptance_report.json"

#: The five facts this acceptance run is about.
REPORT_FIELDS = (
    "blocked_round_advanced",
    "unclaimed_refined",
    "stale_on_heartbeat",
    "phase2_deferred",
    "round_final",
)

#: The one failing case of the round.
RUN_OUTPUT = "FAILED backend/coding_tool.py::test_a - assert 1 == 2"

#: The file that failed — inside the project, declared by no task.
UNCLAIMED_FILE = "backend/coding_tool.py"

#: The plan's own task. It declares a DIFFERENT file, so the failing file
#: has no owner and lands in the "unclaimed" column of the table.
PLAN_TASKS = [
    {
        "id": "3",
        "title": "实现 agent",
        "description": "既有任务，声明的是另一个文件",
        "files_to_modify": ["backend/agent.py"],
    }
]

#: The report a base-drift-blocked round carries. The round dispatched no
#: verification point at all: the branch had drifted away from its base and
#: merging the base back left conflicts.
BASE_DRIFT_REPORT: Dict[str, Any] = {
    "overall_status": "FAILED",
    "stop_reason": "base_drift_conflicts",
    "base_drift_conflicts": {
        "status": "conflicts",
        "base_branch": "main",
        "base_commit": "0badc0de",
        "conflicting_files": ["backend/agent.py"],
    },
    "verification_results": [],
    "requirement_deviations": [],
    "summary": "基线漂移，本轮没有派发任何验证点",
}

#: The verification plan the failed round ran against. ``api_test`` — not
#: ``automated_test`` — so the VP-split judge never asks the model anything.
VERIFICATION_PLAN: Dict[str, Any] = {
    "verification_points": [
        {
            "id": "VP-1",
            "title": "coding tool 用例通过",
            "verification_method": "api_test",
        }
    ]
}

#: The report of a round that measured something and found a failure.
FAILED_REPORT: Dict[str, Any] = {
    "overall_status": "FAILED",
    "verification_results": [
        {
            "id": "VP-1",
            "title": "coding tool 用例通过",
            "status": "FAILED",
            "actual_result": RUN_OUTPUT,
            "evidence": RUN_OUTPUT,
        }
    ],
    "requirement_deviations": [],
    "summary": "1 个验证点失败",
}

#: What the refiner answers when the round hands it an unclaimed file.
REPAIR_RESPONSE: Dict[str, Any] = {
    "tasks": [
        {
            "failed_vp_id": "VP-1",
            "title": f"修复 {UNCLAIMED_FILE} 的失败用例",
            "description": (
                f"{UNCLAIMED_FILE}::test_a 失败，且没有任何任务声明这个文件；"
                "按归属表为它产出修复任务"
            ),
            "acceptance_criteria": f"{UNCLAIMED_FILE}::test_a 通过",
            "files_to_modify": [UNCLAIMED_FILE],
            "test_command": "pytest backend/tests/test_coding_tool.py::test_a -q",
        }
    ]
}

#: The acceptance verdict, accumulated across the four cases.
_ACCEPTANCE: Dict[str, Any] = {field: None for field in REPORT_FIELDS}

#: Every plan directory this session wrote an acceptance report into.
_ACCEPTANCE_PLAN_DIRS: List[Path] = []


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """A coding tool that answers with a canned envelope.

    The repair path's only model edge is ``query_json`` — see
    ``repair_generator.query_json_in_session``, which forwards to
    ``tool.query_json``. Every prompt the round sent is kept, because
    "the unclaimed file reached the refiner" is exactly the claim under
    test.
    """

    def __init__(self, response: Optional[Dict[str, Any]] = None) -> None:
        self.response = response if response is not None else {"tasks": []}
        self.prompts: List[str] = []
        self.closed = False

    def query_json(self, prompt: Any = None, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(str(prompt or ""))
        return json.loads(json.dumps(self.response))

    def release(self) -> None:
        self.closed = True

    def join(self, timeout: Optional[float] = None) -> None:
        return None


class _FakeVerificationAgent:
    """The round's agent, replaced by one that returns a canned report.

    The agent IS the round's verdict, so faking it fakes the verdict —
    everything behind it (the orchestrator, the repair generator, the
    scope-claims table, the state machine) is the real thing.
    """

    def __init__(
        self,
        plan_dir: Any,
        project_dir: Any,
        coding_tool: Any = None,
        verif_repo: Any = None,
        *,
        report: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)
        self.coding_tool = coding_tool
        self.verif_repo = verif_repo
        self.report = report if report is not None else {}
        self.files = files or {}
        self.calls: List[Dict[str, Any]] = []

    def run_full_verification(
        self, round_number: int = 1, resume: bool = False, **kwargs: Any
    ) -> Dict[str, Any]:
        self.calls.append({"round_number": round_number, "resume": resume})
        # The real agent writes the report to disk; the repair path reads
        # it from there (not from the return value), so the fake has to
        # write it too.
        for name, payload in list(self.files.items()) + [
            ("verification_report.json", self.report),
        ]:
            path = self.plan_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return json.loads(json.dumps(self.report))


class _FakeRepairExecutor:
    """The repair executor's dispatch, recorded instead of run.

    The real one spawns a subprocess; here the dispatch itself is the
    thing under test (the round must hand the plan to the executor), so
    the subprocess is replaced by a thread that parks until released.
    """

    def __init__(self) -> None:
        self.dispatches: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __call__(self, plan_id: str, project_dir: Any, **kwargs: Any) -> Dict[str, Any]:
        self.dispatches.append(
            {"plan_id": plan_id, "project_dir": str(project_dir), **kwargs}
        )
        self._thread = threading.Thread(
            target=self._stop.wait,
            name=f"fake-repair-{plan_id}",
            daemon=True,
        )
        self._thread.start()
        return {"status": "started", "pid": 0}

    def release(self) -> None:
        self._stop.set()

    def join(self, timeout: Optional[float] = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class _LiveThread:
    """A thread that stays alive until the test releases it."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._stop.wait, name="fake-verification-thread", daemon=True
        )
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def release(self) -> None:
        self._stop.set()

    def join(self, timeout: Optional[float] = 5.0) -> None:
        self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# The environment each case runs in
# ---------------------------------------------------------------------------


class _AcceptanceEnv:
    """What a case gets: its own plans directory, its own state.db."""

    def __init__(
        self,
        plans_dir: Path,
        db_path: Path,
        registry: List[Any],
    ) -> None:
        self.plans_dir = plans_dir
        self.db_path = db_path
        self._registry = registry

    def register(self, fake: Any) -> Any:
        """Put a fake under the fixture's release + join contract."""
        self._registry.append(fake)
        return fake

    def seed_plan(
        self,
        *,
        phase: str = "executing",
        max_rounds: int = 3,
        tasks: Optional[List[Dict[str, Any]]] = None,
    ):
        """Create the plan directory and its three state rows.

        ``tasks.json`` is written because a plan with no task list is
        short-circuited by the orchestrator before any round runs — the
        case would then prove nothing about the round.
        """
        plan_dir = self.plans_dir / PLAN_ID
        (plan_dir / "logs").mkdir(parents=True, exist_ok=True)
        project_dir = plan_dir / "project"
        (project_dir / "backend").mkdir(parents=True, exist_ok=True)
        (project_dir / UNCLAIMED_FILE).write_text("", encoding="utf-8")

        (plan_dir / "tasks.json").write_text(
            json.dumps(
                {"tasks": tasks if tasks is not None else PLAN_TASKS},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        conn = open_db(self.db_path)
        try:
            migrate(conn)
            RoutingRepository(conn).insert(PLAN_ID, phase)
            ExecutionRepository(conn).insert(
                plan_id=PLAN_ID, project_dir=str(project_dir),
            )
            VerificationRepository(conn).insert(
                PLAN_ID, verification_status="pending", max_rounds=max_rounds,
            )
        finally:
            conn.close()
        return plan_dir, project_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def acceptance_report():
    """The five-field verdict, and every place it is written.

    The four cases each observe one fact; this fixture collects them
    and, at the end of the session, writes the COMPLETE verdict into
    every plan directory the cases used — so each plan directory ends
    up carrying the same five-field report.
    """
    _ACCEPTANCE.update({field: None for field in REPORT_FIELDS})
    _ACCEPTANCE_PLAN_DIRS.clear()
    yield _ACCEPTANCE
    payload = json.dumps(_ACCEPTANCE, ensure_ascii=False, indent=2)
    for plan_dir in _ACCEPTANCE_PLAN_DIRS:
        try:
            (plan_dir / ACCEPTANCE_REPORT_NAME).write_text(
                payload, encoding="utf-8",
            )
        except OSError:
            pass
    print(f"\n[acceptance_report] {json.dumps(_ACCEPTANCE, ensure_ascii=False)}")


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    """A per-case plans directory + state.db, and the fake registry.

    Everything the case registers is released and joined in the
    ``finally`` — a fake that outlives its case is a leaked thread.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(_server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(_server, "_state_db_path", lambda request=None: db_path)

    _server._execution_state.clear()
    _server._verification_state.clear()
    _server._WATCHDOG_STATS["actions"].clear()
    _server._WATCHDOG_STATS["per_plan_last_action_ts"].clear()
    verification_loop._VERIFICATION_STALE_EVENTS.clear()

    registry: List[Any] = []
    env = _AcceptanceEnv(plans_dir=plans_dir, db_path=db_path, registry=registry)
    try:
        yield env
    finally:
        for fake in registry:
            try:
                fake.release()
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                pass
            try:
                fake.join()
            except Exception:  # noqa: BLE001
                pass
        _server._execution_state.clear()
        _server._verification_state.clear()
        _server._WATCHDOG_STATS["actions"].clear()
        _server._WATCHDOG_STATS["per_plan_last_action_ts"].clear()
        verification_loop._VERIFICATION_STALE_EVENTS.clear()
        conn.close()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_fake_agent(
    monkeypatch,
    *,
    tool: Any,
    report: Dict[str, Any],
    files: Optional[Dict[str, Any]] = None,
) -> None:
    """Replace the round's agent with one that returns ``report``.

    The coding tool is patched at the same time: the round builds one
    itself, and a real one would try to reach a provider.
    """

    def _factory(
        plan_dir: Any,
        project_dir: Any,
        coding_tool: Any = None,
        verif_repo: Any = None,
        **kwargs: Any,
    ) -> _FakeVerificationAgent:
        return _FakeVerificationAgent(
            plan_dir,
            project_dir,
            coding_tool,
            verif_repo,
            report=report,
            files=files,
        )

    monkeypatch.setattr("verification.orchestrator.VerificationAgent", _factory)
    monkeypatch.setattr(
        _server, "create_coding_tool", lambda *args, **kwargs: tool,
    )


def _verification_row(db_path: Path) -> Dict[str, Any]:
    """The plan's ``plan_verification`` row, read back from the state.db."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        row = VerificationRepository(conn).current(PLAN_ID)
    finally:
        conn.close()
    assert row is not None, "the plan has no plan_verification row"
    return row


def _plan_tasks(db_path: Path) -> Dict[str, Dict[str, Any]]:
    """The plan's ``plan_tasks`` rows, read back from the state.db."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        return PlanTaskRepository(conn).load_all(PLAN_ID)
    finally:
        conn.close()


def _join_verification_thread(plan_id: str, timeout: float = 60.0) -> None:
    """Wait for the verification loop thread to finish, then join it.

    The round runs on its own thread, so an assertion about "what the
    round did" has to wait for that thread — otherwise it races it.
    """
    deadline = time.time() + timeout
    thread = None
    while time.time() < deadline:
        state = _server._verification_state.get(plan_id) or {}
        candidate = state.get("thread")
        if candidate is not None and hasattr(candidate, "is_alive"):
            thread = candidate
            break
        time.sleep(0.02)
    assert thread is not None, f"the verification loop thread for {plan_id} never started"
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "the verification loop thread did not finish in time"


def _publish_acceptance(plan_dir: Path, **fields: Any) -> Dict[str, Any]:
    """Record the observed facts and write them into the plan directory."""
    for key, value in fields.items():
        assert key in REPORT_FIELDS, f"unknown acceptance field {key!r}"
        _ACCEPTANCE[key] = value
    plan_dir = Path(plan_dir)
    if plan_dir not in _ACCEPTANCE_PLAN_DIRS:
        _ACCEPTANCE_PLAN_DIRS.append(plan_dir)
    report = {field: _ACCEPTANCE[field] for field in REPORT_FIELDS}
    (plan_dir / ACCEPTANCE_REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report


def _print_execution_log_tail(lines: int = 20) -> None:
    """Print the last ``lines`` lines of the round's logs.

    Two logs, because "the round's log" is two files:
    ``<plan>/execution.log`` (the plan's own journal) and
    ``<plan>/logs/*.log`` (the per-round log the watchdog triages).

    Silence would be indistinguishable from "the printer is broken", so
    a plan that produced **no** log at all is reported as such — with
    the files it did produce, which is what tells the reader whether
    the round ran at all.
    """
    plans_dir = Path(_server.PLANS_DIR)
    if not plans_dir.exists():
        print(f"\n===== no plans dir at {plans_dir} =====\n")
        return

    printed = 0
    for log_path in sorted(
        list(plans_dir.glob("*/execution.log"))
        + list(plans_dir.glob("*/logs/*.log"))
    ):
        try:
            content = log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            continue
        printed += 1
        print(f"\n===== execution.log tail: {log_path} =====")
        for line in content[-lines:]:
            print(line)
        print("===== end execution.log tail =====\n")

    if not printed:
        print(f"\n===== no execution.log under {plans_dir} =====")
        for plan_dir in sorted(plans_dir.iterdir()):
            if not plan_dir.is_dir():
                continue
            produced = sorted(
                p.name for p in plan_dir.iterdir() if p.is_file()
            )
            print(f"===== {plan_dir.name} produced: {produced} =====")
        print("===== end (no execution.log) =====\n")


def _with_execution_log_tail(func):
    """Print the round's execution.log tail when the case fails."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except BaseException:
            _print_execution_log_tail()
            raise

    return wrapper


def _write_heartbeat(plan_dir: Path, *, age_sec: float = 0.0) -> None:
    """Write a coding-query heartbeat into the plan's execution.log.

    The stamp is naive UTC — the convention ``execution_logger`` writes
    and ``_log_entry_epoch`` reads back.
    """
    moment = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    entry = {
        "ts": moment.replace(tzinfo=None).isoformat(),
        "event": "coding_query_progress",
        "message": (
            f"{verification_loop._CODING_QUERY_PROGRESS_MARKER} "
            "waiting for the provider"
        ),
    }
    with open(plan_dir / "execution.log", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_stale_round_log(plan_dir: Path, vp_id: str) -> None:
    """A round log that says a verification point started and never finished."""
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    entry = {
        "event_type": "vp_start",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verification_point_id": vp_id,
    }
    (logs / "verification_round1.log").write_text(
        json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8",
    )


def _write_stale_results(plan_dir: Path, age_sec: float) -> None:
    """A ``verification_execution_results.json`` old enough to look dead."""
    path = plan_dir / "verification_execution_results.json"
    path.write_text(
        json.dumps({"verification_results": []}), encoding="utf-8",
    )
    stamp = time.time() - age_sec
    os.utime(path, (stamp, stamp))


def _phase1_vp(vp_id: str) -> Dict[str, Any]:
    """A Phase 1 (ordinary) verification point."""
    return {
        "id": vp_id,
        "title": f"verify {vp_id}",
        "verification_method": "automated_test",
        "verification_phase": 1,
    }


def _phase2_gate_vp(vp_id: str = "VP-GATE") -> Dict[str, Any]:
    """A Phase 2 gate (the gate itself, not a precondition of it)."""
    return {
        "id": vp_id,
        "title": "end to end",
        "verification_method": "e2e",
        "verification_phase": 2,
        "phase_order": 1,
    }


# ---------------------------------------------------------------------------
# 1) a round blocked before it measured anything keeps its number
# ---------------------------------------------------------------------------


@_with_execution_log_tail
def test_blocked_round_keeps_number(
    acceptance, client, monkeypatch, acceptance_report,
):
    """基线漂移挡下的那一轮不消耗轮次号。

    轮次号是计划与外部约定的契约（结论、报告、修复任务都按它写）。一轮
    什么都没测就把号用掉，等于让计划白白烧掉一次预算 —— 这一轮必须把号
    **还回来**。

    证据：
      * 轮次号被归还 → ``plan_verification.round`` 回到 0；
      * 归还的理由留在 ``executor_state["released_rounds"]`` 里。
    """
    plan_dir, _project_dir = acceptance.seed_plan(phase="executing")
    tool = acceptance.register(_FakeCodingTool())
    _install_fake_agent(
        monkeypatch, tool=tool, report=BASE_DRIFT_REPORT,
    )

    resp = client.post(
        f"/api/verification/{PLAN_ID}/start", json={}, headers=REQUEST_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["round"] == 1, body
    assert body["resumed"] is False, body

    _join_verification_thread(PLAN_ID)

    row = _verification_row(acceptance.db_path)
    released = (row.get("executor_state") or {}).get("released_rounds") or []
    assert [entry["round"] for entry in released] == [1], released
    assert released[0]["reason"] == "base_drift_conflicts", released
    # The number went back: the counter sits one below the attempt.
    assert row["round"] == 0, row

    report = _publish_acceptance(plan_dir, blocked_round_advanced=False)
    assert report["blocked_round_advanced"] is False


# ---------------------------------------------------------------------------
# 2) an unclaimed failing file gets a repair task, and the loop continues
# ---------------------------------------------------------------------------


@_with_execution_log_tail
def test_unclaimed_file_gets_repair_task(
    acceptance, client, monkeypatch, acceptance_report,
):
    """无人认领的失败文件由 refiner 接手，回路继续走。

    门禁失败时，一个**没有任何任务声明**的失败文件是活着的：谁都没打算改它。
    2026-11-01 之前，这一轮就此判死；之后，归属表把它交给 refiner，由它产出
    新任务（或把文件并进某个任务的 files_to_modify），回路继续迭代。

    证据：
      * 归属表落盘，``unclaimed`` 里就是那个文件；
      * 那个文件**进了 refiner 的 prompt**（不是只落盘）；
      * 产出的修复任务写进了 ``plan_tasks``；
      * 执行器被派发出去 —— 回路没有停在"没人认领"这个事实上。
    """
    plan_dir, project_dir = acceptance.seed_plan(phase="executing")
    tool = acceptance.register(_FakeCodingTool(response=REPAIR_RESPONSE))
    _install_fake_agent(
        monkeypatch,
        tool=tool,
        report=FAILED_REPORT,
        files={"verification_plan.json": VERIFICATION_PLAN},
    )
    executor = acceptance.register(_FakeRepairExecutor())
    monkeypatch.setattr(_server, "_run_repair_execution_async", executor)

    resp = client.post(
        f"/api/verification/{PLAN_ID}/start", json={}, headers=REQUEST_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["round"] == 1, resp.json()

    _join_verification_thread(PLAN_ID)

    # The ownership table.
    claims_path = plan_dir / "verification_scope_claims.json"
    assert claims_path.exists(), "the ownership table was not written"
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    assert claims["unclaimed"] == [UNCLAIMED_FILE], claims

    # The unclaimed file reached the refiner.
    assert tool.prompts, "the refiner was never asked for repair content"
    assert any(
        UNCLAIMED_FILE in prompt for prompt in tool.prompts
    ), "the unclaimed file did not reach the refiner's prompt"

    # A repair task for it landed in the state machine.
    tasks = _plan_tasks(acceptance.db_path)
    repair_rows = [
        row for row in tasks.values()
        if UNCLAIMED_FILE in (row.get("files_to_modify") or [])
    ]
    assert repair_rows, f"no repair task names {UNCLAIMED_FILE}: {tasks}"

    # And the loop handed the plan to the executor instead of parking it.
    assert executor.dispatches, "the repair executor was never dispatched"
    assert executor.dispatches[0]["plan_id"] == PLAN_ID

    report = _publish_acceptance(plan_dir, unclaimed_refined=True)
    assert report["unclaimed_refined"] is True


# ---------------------------------------------------------------------------
# 3) the heartbeat keeps a waiting round alive; the gate reads THIS round
# ---------------------------------------------------------------------------


@_with_execution_log_tail
def test_heartbeat_prevents_stale_and_gate_uses_current_round(
    acceptance, monkeypatch, acceptance_report,
):
    """心跳救回等待中的轮次；关卡只读本轮结论。

    两件事，都发生在同一个进程里：

    * **心跳**（决策点 4）：一轮在等 provider 时只写心跳、不写轮次日志。
      看门狗只看得到"沉默"，于是把一个健康的轮次判成卡死。心跳是那一拍的
      证据：心跳还新鲜 → 这一轮活着，不判死。
      负控：同样的现场没有心跳 → 越过硬上限，这一轮被判死。
    * **轮次戳**（决策点 3 / 5）：上一轮的 FAILED 不是本轮的结论。关卡只读
      本轮的结论，所以本轮 PASSED 的 Phase 1 不会让 Phase 2 被判 DEFERRED。
    """
    plan_dir, _project_dir = acceptance.seed_plan(phase="executing")
    vp_id = "VP-HEARTBEAT-ACC"

    # --- the round that looks silent -----------------------------------
    _write_stale_round_log(plan_dir, vp_id)
    _write_stale_results(
        plan_dir, age_sec=_server.VERIFICATION_HARD_STALE_SECONDS + 120.0,
    )
    _write_heartbeat(plan_dir)

    live = acceptance.register(_LiveThread())
    _server._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "verification_round": 1,
        "thread": live,
        "started_at": (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        ).isoformat(),
        "started_at_ts": time.time() - 3600.0,
    }
    _server._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)

    verification_loop._lazy_check_verification(PLAN_ID)

    state = _server._verification_state.get(PLAN_ID) or {}
    assert state.get("verification_status") == "running", (
        "the heartbeat did not keep the waiting round alive: "
        f"{state.get('verification_status')}"
    )
    assert state.get("stop_reason") is None, state

    # The stale judgement itself was recorded — with the heartbeat in it.
    stale_events = list(verification_loop._VERIFICATION_STALE_EVENTS)
    assert stale_events, "no stale judgement was recorded at all"
    assert stale_events[-1]["heartbeat_seen"] is True, stale_events[-1]

    # --- negative control: same scene, no heartbeat --------------------
    _write_stale_results(
        plan_dir, age_sec=_server.VERIFICATION_HARD_STALE_SECONDS + 120.0,
    )
    (plan_dir / "execution.log").write_text("", encoding="utf-8")
    _server._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "verification_round": 1,
        "thread": live,
        "started_at": (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        ).isoformat(),
        "started_at_ts": time.time() - 3600.0,
    }
    _server._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)

    verification_loop._lazy_check_verification(PLAN_ID)

    state = _server._verification_state.get(PLAN_ID) or {}
    assert state.get("verification_status") == "failed", (
        "without a heartbeat the silent round was not judged dead: "
        f"{state.get('verification_status')}"
    )

    # --- the gate reads THIS round's verdict ---------------------------
    executor = VerificationExecutor(
        verification_plan={
            "vps": [_phase1_vp("VP-009"), _phase2_gate_vp()],
        },
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: {
            "status": "PASSED", "reasons": [], "evidence": {},
        },
        round_no=3,
    )
    layers = executor._build_execution_layers(executor._build_items_from_plan())
    assert executor._phase_of_layer == [PHASE_CORE, PHASE_FINAL_GATE]

    # Last round FAILED; this round PASSED.
    executor._verdicts = {"VP-009": {"status": "FAILED", "round": 2}}
    executor._round_verdicts_history = {
        "VP-009": [{"status": "FAILED", "round": 2}],
    }
    executor.record_verdict(
        "VP-009", {"status": "PASSED", "reasons": [], "evidence": {}},
    )

    assert executor._verdicts["VP-009"]["round"] == 3
    assert executor._terminal_status_of("VP-009") == "PASSED"
    # The Phase 2 gate is not blocked by the previous round's failure.
    assert executor._should_short_circuit_layer(0, []) is False
    assert executor._deferred_vps == []
    assert not [
        vp_id for vp_id, verdict in executor._verdicts.items()
        if isinstance(verdict, dict) and verdict.get("status") == "DEFERRED"
    ]

    report = _publish_acceptance(
        plan_dir, stale_on_heartbeat=False, phase2_deferred=False,
    )
    assert report["stale_on_heartbeat"] is False
    assert report["phase2_deferred"] is False


# ---------------------------------------------------------------------------
# 4) a released round number is run again — fresh, not resumed
# ---------------------------------------------------------------------------


@_with_execution_log_tail
def test_released_round_reused_not_resumed(
    acceptance, client, monkeypatch, acceptance_report,
):
    """归还的轮次号下一次 /start **重新跑一遍**，不是 resume。

    被归还的号不是"做了一半的轮次"，是"一次什么都没测的尝试"：没有可接的
    活，所以它以**新的一轮**重跑。把当成 resume 就会永远卡在同一个号上。

    证据：第二次 ``/start`` 返回的轮次号与第一次相同，``resumed`` 为假。
    """
    plan_dir, _project_dir = acceptance.seed_plan(phase="executing")
    tool = acceptance.register(_FakeCodingTool())
    _install_fake_agent(monkeypatch, tool=tool, report=BASE_DRIFT_REPORT)

    first = client.post(
        f"/api/verification/{PLAN_ID}/start", json={}, headers=REQUEST_HEADERS,
    )
    assert first.status_code == 200, first.text
    assert first.json()["round"] == 1, first.json()
    _join_verification_thread(PLAN_ID)

    row = _verification_row(acceptance.db_path)
    assert row["round"] == 0, row
    released = (row.get("executor_state") or {}).get("released_rounds") or []
    assert [entry["round"] for entry in released] == [1], released

    second = client.post(
        f"/api/verification/{PLAN_ID}/start", json={}, headers=REQUEST_HEADERS,
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["round"] == 1, body
    assert body["resumed"] is False, body
    _join_verification_thread(PLAN_ID)

    report = _publish_acceptance(plan_dir, round_final=body["round"])
    assert report["round_final"] == 1
