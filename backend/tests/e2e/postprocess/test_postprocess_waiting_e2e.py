"""E2E：等待与崩溃恢复链路（S4-S6）。

这一份是收尾阶段的**等待**链路的端到端验收：等一个人、等一个外部条件、
在等待期间把调度槽位让出来，以及进程被 ``kill -9`` 之后重启不重复对外动作。

它复用任务 24 的启动契约（同一个真服务器、同一个 ``gh`` 桩、同一个裸仓、
同一份状态库），所以每一条断言读到的都是**进程外**的事实：HTTP 路由的回答、
HITL 表里的决策行、裸仓里的分支、桩日志里的调用、状态库里的槽位行。

等待本身由生产代码驱动：``postprocess.waits.WaitRegistry`` 是生产的那一个，
``postprocess.orchestrator.PostProcessOrchestrator`` 是生产的那一个，
``routes.scheduler.plan_waiting_rows`` 是生产的读模型，
``hooks.dispatcher`` 是生产的通道。

两处生产缺口在这一份里是**绕过去**而不是**改掉**的（改它们不在本任务的
文件范围内），绕法都写在各自的注释里：

1. 服务端自己的收尾流程没有把等待登记进 HITL（``verification_loop``
   构造编排器时没有给 ``waits=``），所以等待由测试用生产注册表登记，
   登记之后服务端**看得见**它 —— 延长期限的路由读的是同一条持久化记录；
2. 注册表写进进度文件的指纹是要求书**字节**的指纹
   （``progress._spec_fingerprint``），而编排器比的是**规范化**指纹
   （``spec._spec_digest``）—— 两者不是同一个值，所以指纹由测试用生产的
   ``progress.mark`` 写进去，写法与收尾阶段自己写它时一模一样。
"""

from __future__ import annotations

import importlib.util
import os
import signal
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

#: ``backend/`` —— 仓库自己的布局，不写死任何机器上的路径。
BACKEND_DIR = Path(__file__).resolve().parents[3]

#: The plan this file seeds — the same plan task 24 seeds.
PLAN_ID = "p1"

#: The plan that arrives while the first one is waiting.
SECOND_PLAN_ID = "p2"

#: How long the scheduler may take to notice a fact: a slot given back, a
#: slot taken.  The tick is the scheduler's own clock, so this is the
#: budget for "the next tick happens".
SCHEDULER_TIMEOUT_SECONDS = 30.0

#: How long the finishing stage may take to settle its items.
STAGE_TIMEOUT_SECONDS = 120.0


def _load_lifecycle_module():
    """Load task 24's E2E module — the boot contract this file reuses.

    The module is loaded by path rather than copied: the boot contract
    (the environment, the readiness probe, the git workspace, the bare
    repo, the ``gh`` stub) has to be **the same** contract, and a copy of
    it would be a second contract that could drift away from the first.

    Returns:
        The loaded module.
    """
    path = (
        BACKEND_DIR
        / "tests"
        / "e2e"
        / "postprocess"
        / "test_postprocess_lifecycle_e2e.py"
    )
    spec = importlib.util.spec_from_file_location(
        "pdt_postprocess_lifecycle_e2e", path
    )
    assert spec is not None and spec.loader is not None, (
        f"the lifecycle E2E module could not be loaded from {path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Task 24's module — the harness, the helpers and the books it defines.
lifecycle = _load_lifecycle_module()


# ---------------------------------------------------------------------------
# The requirement books
# ---------------------------------------------------------------------------

#: The question the waiting item asks.
QUESTION = "批准合入？"

#: The item that waits for a human.
WAIT_ITEM = "await-approval"

#: The branch the waiting item publishes once it is answered.
APPROVED_BRANCH = "approved"

#: The branch the outward item publishes — the action a crash must not
#: repeat.
PUBLISHED_BRANCH = "published"


def _item(
    item_id: str,
    action: str,
    command: List[str],
    target: str,
    *,
    wait: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One requirement item, in the shape the requirement book declares.

    The seven keys are the ones ``postprocess/spec.py`` requires.  An
    item that carries a ``command`` must be ``external`` — a local item
    has no command to run, so the executor has nothing to do.

    The acceptance predicate is ``exit_code == 0``: the command's exit
    code decides, and the evidence requirement is the same.  Both
    commands used here print something on success, which matters: the
    judge treats a record with no output as the *absence* of evidence,
    and an item with no usable evidence is a FAIL.

    Args:
        item_id: The item's id.
        action: What the item does, in words.
        command: The argv the item runs.
        target: What the item acts on.
        wait: The wait the item declares.  ``None`` means "no wait".

    Returns:
        The item, ready to be put into a requirement book.
    """
    return {
        "id": item_id,
        "action": action,
        "command": list(command),
        "target": target,
        "acceptance_predicate": {"kind": "exit_code", "equals": 0},
        "evidence_requirement": {"kind": "exit_code"},
        "external": True,
        "required": True,
        "wait": wait if wait is not None else {"type": "none"},
    }


def _book_one_waits() -> Dict[str, Any]:
    """A book with one item that waits for a human, and one that acts.

    The waiting item's command is **not** one of the executor's
    approval-gated shapes: a push into ``approved`` is an ordinary
    outward action, so once the human has answered, the item really can
    run — and the branch it publishes is the proof that the answer woke
    the stage up.
    """
    return {
        "items": [
            _item(
                WAIT_ITEM,
                "publish the approved branch",
                ["git", "push", "origin", f"HEAD:refs/heads/{APPROVED_BRANCH}"],
                APPROVED_BRANCH,
                wait={"type": "human"},
            ),
            _item(
                "read-head",
                "read the head commit",
                ["git", "rev-parse", "--verify", "HEAD"],
                "HEAD",
            ),
        ]
    }


def _book_external_waits() -> Dict[str, Any]:
    """A book with two items that wait for something outside.

    Both declare an external wait, and an external wait must say what it
    rechecks (``postprocess/spec.py`` refuses an external wait with no
    ``check``).  The first item's condition is satisfied by the second
    recheck; the second item's condition never comes true, so its
    deadline is what ends it — and the command behind it fails, because
    the thing it waited for never came.
    """
    return {
        "items": [
            _item(
                "await-satisfied",
                "wait for the external check to pass",
                ["git", "rev-parse", "--verify", "HEAD"],
                "HEAD",
                wait={"type": "external", "check": "external-condition"},
            ),
            _item(
                "await-expiry",
                "wait for a condition that never comes",
                ["git", "rev-parse", "--verify", "refs/heads/does-not-exist"],
                "refs/heads/does-not-exist",
                wait={"type": "external", "check": "external-condition"},
            ),
        ]
    }


def _book_outward_actions_then_a_wait() -> Dict[str, Any]:
    """A book whose first two items act on the world, and whose third waits.

    The two outward actions are the ones a crash must not repeat: a
    branch pushed into the bare repo, and a ``gh`` call recorded by the
    stub.  The third item waits for a human, so the stage is suspended
    when the process is killed — the shape the crash-recovery scenario
    needs.
    """
    return {
        "items": [
            _item(
                "publish-branch",
                "publish the finishing branch",
                ["git", "push", "origin", f"HEAD:refs/heads/{PUBLISHED_BRANCH}"],
                PUBLISHED_BRANCH,
            ),
            _item(
                "open-pr",
                "open the pull request",
                ["gh", "pr", "create", "--title", "finishing", "--body", "finishing"],
                "pull-request",
            ),
            _item(
                WAIT_ITEM,
                "publish the approved branch",
                ["git", "push", "origin", f"HEAD:refs/heads/{APPROVED_BRANCH}"],
                APPROVED_BRANCH,
                wait={"type": "human"},
            ),
        ]
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """A clock the test moves — the registry's own injection seam.

    The registry reads the moment from the clock it is given, so a test
    can put a wait's deadline in the past without waiting for it.
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        """The current moment, as the registry sees it."""
        return self._moment

    def advance(self, delta: timedelta) -> None:
        """Move the clock on."""
        self._moment = self._moment + delta


def _recheck(answers: Sequence[bool]) -> Callable[[], bool]:
    """A recheck that answers with the given sequence, one call at a time.

    Args:
        answers: What each successive call answers.

    Returns:
        The recheck callable.
    """
    remaining = list(answers)

    def _check() -> bool:
        if not remaining:
            return False
        return remaining.pop(0)

    return _check


def _never_satisfied() -> Callable[[], bool]:
    """A recheck whose condition never comes true."""

    def _check() -> bool:
        return False

    return _check


# ---------------------------------------------------------------------------
# The harness: task 24's harness plus the waiting lifecycle
# ---------------------------------------------------------------------------


class _WaitingHarness(lifecycle._Harness):
    """Task 24's harness, plus the three things the waiting scenarios need.

    1. a restart that boots a **new** process on a **new** port, and a
       ``kill -9`` that leaves nothing behind;
    2. the production waiting lifecycle — the registry, the HITL store
       behind it, the plan's own channel in front of it, and the read
       model the panel reads;
    3. the production finishing stage, driven in this process over the
       plan's own directory.
    """

    def __init__(self, work: Path, book: Optional[Dict[str, Any]]) -> None:
        super().__init__(work, book)
        self._prepared = False
        self._stores: List[Any] = []
        self.ports: List[int] = []
        self.port = 0

    # -- setup ---------------------------------------------------------

    def prepare(self) -> None:
        """Write the plan's directory **once**.

        A restart must not rebuild the plan's directory: the progress
        file, the requirement book and the git workspace are what
        survived the crash, and a restart that rewrote them would be
        testing nothing.
        """
        if self._prepared:
            return
        super().prepare()
        self._prepared = True

    # -- the server ----------------------------------------------------

    def start(self) -> None:
        """Boot the server on a port this harness has not used before.

        The port is remembered: a restart must never reuse the port the
        killed process was listening on, and the only way to know which
        port that was is to keep it.
        """
        previous = self.port
        original = lifecycle._free_port

        def _not_the_previous() -> int:
            port = original()
            while port == previous:
                port = original()
            return port

        lifecycle._free_port = _not_the_previous
        try:
            super().start()
        finally:
            lifecycle._free_port = original
        self.port = int(urlsplit(self.base_url).port or 0)
        self.ports.append(self.port)

    def kill9(self) -> None:
        """Kill the server the hard way — ``SIGKILL``, no shutdown.

        The whole process group goes, because the server starts its own
        session: a graceful stop would let the finishing stage write its
        report, and the scenario is about a process that was killed
        mid-flight.
        """
        proc, self.proc = self.proc, None
        if proc is None:
            return
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=30)

    def restart(self) -> None:
        """Boot the server again — a new process, on a new port.

        The first boot's log is kept: what the killed process said is
        part of the evidence.
        """
        self.kill9()
        if self.log_path.exists():
            self.log_path.replace(self.work / f"server-{len(self.ports)}.log")
        self.start()

    def port_is_closed(self, port: int) -> bool:
        """Whether nothing is listening on ``port`` any more.

        A restart that answers on the old port would be the old
        process, not a new one.
        """
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=5
            ):
                return False
        except (urllib.error.URLError, OSError):
            return True

    def http(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """One HTTP call against the running server.

        Args:
            method: The HTTP method.
            path: The path, with the plan's own placeholders already
                spelled out by the caller.
            body: The request body, when there is one.

        Returns:
            ``(status, decoded body)``.
        """
        return lifecycle._http_json(method, f"{self.base_url}{path}", body)

    # -- the waiting lifecycle -----------------------------------------

    def registry(self, *, clock: Any = None) -> Any:
        """The production waiting lifecycle of this plan.

        The registry is the production one: the same class the finishing
        stage uses, with the same HITL store behind it — the table the
        server's own answer route writes — and the same channel in front
        of it, the plan's own hook binding.

        Args:
            clock: The clock the registry reads the moment from.

        Returns:
            The registry.
        """
        from config_paths import resolve_state_db_path
        from hitl.store import HitlStore
        from postprocess.waits import WaitRegistry

        store = HitlStore(resolve_state_db_path())
        self._stores.append(store)
        return WaitRegistry(
            self.plan_id,
            clock=clock,
            store=store,
            deliver=self._deliver,
        )

    def _deliver(self, payload: Dict[str, Any]) -> None:
        """Deliver a question through the plan's own route.

        This is the production delivery: the existing hook dispatcher,
        the plan's own ``hitl_config.json`` binding, the same event the
        existing HITL outlet carries.
        """
        from hitl.service import EVENT_DECISION_CREATED
        from hooks.dispatcher import dispatch

        dispatch(EVENT_DECISION_CREATED, payload, self.plan_dir)

    def waiting_rows(self) -> List[Dict[str, Any]]:
        """The plan's waiting rows — the production read model.

        The rows are built by the production builder out of the plan's
        own durable wait records, so the row and the record cannot tell
        two different stories.
        """
        import server  # noqa: F401 — imported first: the routes import it
        from routes.scheduler import plan_waiting_rows

        return plan_waiting_rows(self.plan_id)

    def pin_fingerprint(self, item_id: str, status: str) -> None:
        """State the frozen requirement book's fingerprint in the progress file.

        The finishing stage reads the progress file only when the
        fingerprint in it is the one of the book it froze
        (``orchestrator._progress_statuses``), and the registry writes
        the fingerprint of the book's **bytes**
        (``progress._spec_fingerprint``) — a different digest from the
        canonical one the stage compares against (``spec._spec_digest``).

        So a wait that was registered outside the stage's own run is
        invisible to the stage until the fingerprint is stated through
        the same production API the stage uses to state it.  The call
        below is that call, with the same argument the stage passes.

        Args:
            item_id: The item whose progress entry is stated.
            status: The status the entry carries.
        """
        from postprocess.progress import mark
        from postprocess.spec import freeze, load_spec

        frozen = freeze(load_spec(self.plan_dir))
        mark(self.plan_id, item_id, status, spec_sha256=frozen.spec_sha256)

    # -- the finishing stage -------------------------------------------

    def stage(self, *, waits: Any = None) -> Any:
        """The production finishing stage, driven in this process.

        The server's own finishing flow cannot register a wait — the
        orchestration in ``verification_loop`` builds the orchestrator
        without a registry, so a question cannot be stored and the wait
        is recorded in the progress file alone.  So the stage is driven
        here, with the production orchestrator, the production executor,
        the production judge and the production notification channel;
        everything it touches is the plan's own directory and the same
        git workspace the server's own stage would have used.

        Args:
            waits: The registry the stage registers its waits with.

        Returns:
            The orchestrator.
        """
        from postprocess.executor import CommandRunner
        from postprocess.orchestrator import PostProcessOrchestrator
        from postprocess.spec import freeze, load_spec
        from verification_loop import _postprocess_notification_sink

        frozen = freeze(load_spec(self.plan_dir))
        executor = CommandRunner(frozen, workspace=self.project)
        return PostProcessOrchestrator(
            self.plan_id,
            plan_dir=self.plan_dir,
            executor=executor,
            waits=waits,
            sink=_postprocess_notification_sink(self.plan_dir),
        )

    # -- the scheduler --------------------------------------------------

    def slot_rows(self) -> List[Dict[str, Any]]:
        """The scheduler's slot rows, read through the production store."""
        from plan_scheduler.store import SqliteSchedulerStore
        from state_machine.db.connection import open as db_open

        conn = db_open(self.work / "state.db")
        try:
            return SqliteSchedulerStore().list_slots(conn)
        finally:
            conn.close()

    def seed_slot(self, plan_id: str) -> bool:
        """Record that a plan holds a slot — as the scheduler's own store does.

        Returns:
            Whether the plan holds the slot afterwards.
        """
        from plan_scheduler.store import SqliteSchedulerStore
        from state_machine.db.connection import open as db_open
        from state_machine.db.schema import migrate

        conn = db_open(self.work / "state.db")
        try:
            migrate(conn)
            return SqliteSchedulerStore().upsert_slot(conn, plan_id)
        finally:
            conn.close()

    def start_second_plan(self) -> None:
        """Make a second plan look like it is working.

        The plan's routing row says it is executing, its execution row
        says it is running, and the pid is **alive** — this process.  That
        is what the scheduler's own facts reader calls a plan that holds
        compute.
        """
        from state_machine.db.connection import open as db_open
        from state_machine.db.schema import migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = db_open(self.work / "state.db")
        try:
            migrate(conn)
            RoutingRepository(conn).insert(SECOND_PLAN_ID, "executing")
            ExecutionRepository(conn).insert(
                SECOND_PLAN_ID,
                project_dir=str(self.project),
                exec_status="running",
                exec_pid=os.getpid(),
            )
        finally:
            conn.close()

    # -- observing -----------------------------------------------------

    def wait_for_progress(
        self,
        expected: Dict[str, str],
        timeout: float = STAGE_TIMEOUT_SECONDS,
    ) -> Optional[Dict[str, Any]]:
        """Wait until the progress file records these statuses.

        Args:
            expected: The status each item is expected to carry.
            timeout: How long the stage may take.

        Returns:
            The progress document, or ``None`` when the budget ran out.
        """

        def _reached() -> Optional[Dict[str, Any]]:
            progress = self.progress()
            if not isinstance(progress, dict):
                return None
            items = progress.get("items") or {}
            for item_id, status in expected.items():
                entry = items.get(item_id)
                if not isinstance(entry, dict):
                    return None
                if entry.get("status") != status:
                    return None
            return progress

        return lifecycle._wait_for(_reached, timeout)

    def wait_for_scheduler(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        timeout: float = SCHEDULER_TIMEOUT_SECONDS,
    ) -> Optional[Dict[str, Any]]:
        """Wait until the scheduler's read model satisfies a predicate.

        Args:
            predicate: What the read model has to satisfy.
            timeout: How long the scheduler may take.

        Returns:
            The read model, or ``None`` when the budget ran out.
        """

        def _read() -> Optional[Dict[str, Any]]:
            state = self.scheduler_state()
            return state if predicate(state) else None

        return lifecycle._wait_for(_read, timeout)

    def wait_for_slot(
        self,
        plan_id: str,
        timeout: float = SCHEDULER_TIMEOUT_SECONDS,
    ) -> bool:
        """Wait until the scheduler's own store says a plan holds a slot.

        The read model answers "who is working" from the plan facts, so
        it turns true the moment the facts turn true; the slot row is
        the scheduler's own record of the acquisition, and the tick
        writes it.  So "the plan holds a slot" is two facts, and the
        second one arrives a tick later.

        Args:
            plan_id: The plan the slot belongs to.
            timeout: How long the scheduler may take.

        Returns:
            Whether the slot row is there.
        """

        def _held() -> bool:
            return any(
                row.get("plan_id") == plan_id for row in self.slot_rows()
            )

        return bool(lifecycle._wait_for(_held, timeout))

    def wait_for_slot_release(
        self,
        plan_id: str,
        timeout: float = SCHEDULER_TIMEOUT_SECONDS,
    ) -> bool:
        """Wait until the scheduler's own store says a plan holds no slot.

        The release is the scheduler's decision, taken on its own tick:
        a plan that waits for an answer and has nothing running any more
        gives its slot back.  So the row is gone a tick after it was
        taken — and the test waits for that tick instead of reading the
        table in the window between the two.

        Args:
            plan_id: The plan the slot belongs to.
            timeout: How long the scheduler may take.

        Returns:
            Whether the slot row is gone.
        """

        def _released() -> bool:
            return not any(
                row.get("plan_id") == plan_id for row in self.slot_rows()
            )

        return bool(lifecycle._wait_for(_released, timeout))

    def close(self) -> None:
        """Give back what this harness opened."""
        for store in self._stores:
            try:
                store.close()
            except Exception:  # noqa: BLE001 — the store is ours
                pass
        self._stores.clear()


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def boot_plan(tmp_path, monkeypatch):
    """Start a real server for one plan; the caller says which book it has.

    The scheduler is off unless the caller asks for it: the scheduler's
    settings are read from the file ``PDT_SCHEDULER_FILE`` names, and a
    scenario that is about the scheduler says so by naming one.

    The server is stopped on the way out, so a test that fails half way
    through still gives its port back.
    """
    started: List[_WaitingHarness] = []

    def _boot(
        book: Optional[Dict[str, Any]] = None,
        *,
        scheduler_enabled: bool = False,
    ) -> _WaitingHarness:
        if scheduler_enabled:
            settings = tmp_path / "scheduler.yaml"
            settings.write_text(
                "scheduler:\n"
                "  enabled: true\n"
                "  dry_run: false\n"
                "  capacity: 4\n"
                "  tick_seconds: 0.5\n",
                encoding="utf-8",
            )
            monkeypatch.setenv("PDT_SCHEDULER_FILE", str(settings))
        harness = _WaitingHarness(tmp_path, book)
        harness.start()
        started.append(harness)
        return harness

    yield _boot

    for harness in started:
        harness.stop()
        harness.close()


# ---------------------------------------------------------------------------
# S4 —— 等一个人
# ---------------------------------------------------------------------------


def test_human_wait_appears_in_waiting_and_resumes_after_answer(boot_plan):
    """S4 —— 等一个人：等待行出现 → 回答 → 继续 → 终态。

    这一条钉的是等待的**可见性**与**唤醒**：等待登记下来之后，它出现在
    生产的等待读模型里（面板读的那一个），服务端进程也看得见它 —— 延长
    期限的路由读的是同一条持久化记录，所以延长之后 ``extensions_count``
    变成 1。

    回答走的是真的 HTTP 路由（``POST /api/hitl/{plan}/decisions/{id}/answer``），
    回答之后历史仍然可以从 ``GET /api/hitl/{plan}/decisions`` 回看 —— 等待
    行消失了，历史还在。

    回答之后收尾阶段继续跑到终态：等待的那一条真的执行了，它推出去的分支
    出现在裸仓里。
    """
    plan = boot_plan(_book_one_waits())
    registry = plan.registry()

    # --- the wait is registered ---------------------------------------
    record = registry.create_human_wait(
        {"id": WAIT_ITEM, "wait": {"type": "human"}}, QUESTION
    )
    assert record["decision_id"], "the question was stored without a decision id"
    plan.pin_fingerprint(WAIT_ITEM, "waiting")

    # --- the wait is visible in the read model ------------------------
    rows = plan.waiting_rows()
    assert len(rows) == 1, (
        f"the plan is waiting for {len(rows)} items, not one: {rows!r}"
    )
    row = rows[0]
    assert row["plan_id"] == PLAN_ID, f"the row names another plan: {row!r}"
    assert row["item_id"] == WAIT_ITEM, f"the row names another item: {row!r}"
    assert row["wait_type"] == "human", f"the row is not a human wait: {row!r}"
    assert row["question"] == QUESTION, f"the row carries another question: {row!r}"
    assert row["remaining_seconds"] > 0, f"the row has no time left: {row!r}"
    assert row["extendable"] is True, f"the row cannot be extended: {row!r}"
    assert row["extensions_count"] == 0, f"the row was extended already: {row!r}"

    # --- the server process sees the same wait ------------------------
    status, body = plan.http(
        "POST",
        f"/api/scheduler/{PLAN_ID}/waits/{WAIT_ITEM}/extend",
        {"seconds": 600},
    )
    assert status == 200, f"the extension was refused: {status} {body!r}"
    assert body["extensions_count"] == 1, f"the extension was not recorded: {body!r}"
    assert body["remaining_seconds"] > 0, f"the row has no time left: {body!r}"

    # --- the question reached the plan's own channel ------------------
    deliveries = plan.channel_deliveries()
    assert len(deliveries) == 1, f"the question was delivered {len(deliveries)} times"
    assert deliveries[0].get("question") == QUESTION, (
        f"the delivered question is not the one that was asked: {deliveries[0]!r}"
    )

    # --- the answer, over HTTP ----------------------------------------
    status, body = plan.http(
        "POST",
        f"/api/hitl/{PLAN_ID}/decisions/{record['decision_id']}/answer",
        {"option": "proceed"},
    )
    assert status == 200, f"the answer was refused: {status} {body!r}"
    assert body["status"] == "answered", f"the decision is not answered: {body!r}"

    # --- the history is still there -----------------------------------
    status, decisions = plan.http("GET", f"/api/hitl/{PLAN_ID}/decisions")
    assert status == 200, f"the history was refused: {status} {decisions!r}"
    assert len(decisions) == 1, f"the history holds {len(decisions)} decisions"
    assert decisions[0]["status"] == "answered", (
        f"the decision is not answered: {decisions[0]!r}"
    )
    assert decisions[0]["answer"] == "proceed", (
        f"the answer was not kept: {decisions[0]!r}"
    )

    # --- the waiting row is gone --------------------------------------
    resolved = registry.resolve_human_wait(WAIT_ITEM, "proceed")
    assert resolved["status"] == "answered", f"the wait is not answered: {resolved!r}"
    assert plan.waiting_rows() == [], (
        f"the plan is still waiting: {plan.waiting_rows()!r}"
    )

    # --- the stage continues to a terminal state ----------------------
    result = plan.stage(waits=registry).resume()
    assert result["terminal_status"] == "completed", (
        f"the stage did not complete: {result!r}\n{plan.server_output()}"
    )
    assert APPROVED_BRANCH in plan.bare_branches(), (
        f"the bare repo holds {plan.bare_branches()!r}, not the approved branch"
    )
    report = plan.report()
    assert isinstance(report, dict), f"the stage wrote no report: {report!r}"
    verdicts = {item["id"]: item["verdict"] for item in report["items"]}
    assert verdicts.get(WAIT_ITEM) == "PASS", (
        f"the waiting item did not pass: {verdicts!r}"
    )


# ---------------------------------------------------------------------------
# S5 —— 等外部
# ---------------------------------------------------------------------------


def test_external_wait_fails_at_deadline(boot_plan):
    """S5 —— 等外部：复查第二次满足 → PASS；到期 → FAIL + 一条通知审计。

    等外部的东西没有人在 answering，所以外部等待登记的是**复查**：复查说
    条件满足了，等待才结束。这里复查第一次不满足、第二次满足，于是等待
    结束，而它背后的条目跑起来并通过。

    另一个等待的复查永远不满足，于是它的期限到了：等待变成 failed，条目
    也变成 failed（它等的东西没有来），收尾阶段重试它一次、还是失败，
    终态是 ``failed``，并且**恰好一条**通知审计行落盘 —— 一行就是一次实投，
    两行就是通知了两次。
    """
    from postprocess.orchestrator import TERMINAL_STATUS_FAILED

    plan = boot_plan(_book_external_waits())
    clock = _FakeClock(datetime.now(timezone.utc))
    registry = plan.registry(clock=clock)

    # --- the two external waits are registered ------------------------
    registry.create_external_wait(
        {"id": "await-satisfied", "wait": {"type": "external", "check": "c"}},
        _recheck([False, True]),
    )
    registry.create_external_wait(
        {"id": "await-expiry", "wait": {"type": "external", "check": "c"}},
        _never_satisfied(),
    )

    # --- the first recheck is not satisfied yet ------------------------
    first = registry.check_external_wait("await-satisfied")
    assert first["satisfied"] is False, f"the first recheck was satisfied: {first!r}"
    assert first["status"] == "pending", f"the wait is not pending any more: {first!r}"

    # --- the second one is ---------------------------------------------
    second = registry.check_external_wait("await-satisfied")
    assert second["satisfied"] is True, f"the second recheck was not satisfied: {second!r}"
    assert second["status"] == "answered", f"the wait is not answered: {second!r}"

    # --- the other wait's deadline passes ------------------------------
    remaining = registry.remaining_seconds("await-expiry")
    clock.advance(timedelta(seconds=remaining + 1))
    expired = registry.expire_due()
    assert [record["item_id"] for record in expired] == ["await-expiry"], (
        f"the expired waits are {expired!r}"
    )
    assert expired[0]["status"] == "failed", (
        f"the expired wait is not failed: {expired[0]!r}"
    )

    plan.pin_fingerprint("await-satisfied", "pending")
    plan.pin_fingerprint("await-expiry", "failed")

    # --- the stage runs and the terminal state is a failure ------------
    result = plan.stage(waits=registry).resume()
    assert result["terminal_status"] == TERMINAL_STATUS_FAILED, (
        f"the stage did not fail: {result!r}\n{plan.server_output()}"
    )

    report = plan.report()
    assert isinstance(report, dict), f"the stage wrote no report: {report!r}"
    verdicts = {item["id"]: item["verdict"] for item in report["items"]}
    assert verdicts.get("await-satisfied") == "PASS", (
        f"the satisfied item did not pass: {verdicts!r}"
    )
    assert verdicts.get("await-expiry") == "FAIL", (
        f"the expired item did not fail: {verdicts!r}"
    )

    # --- the failure notification was delivered exactly once -----------
    lines = plan.notification_lines()
    assert len(lines) == 1, f"the audit file holds {len(lines)} lines: {lines!r}"
    assert lines[0].get("plan_id") == PLAN_ID, (
        f"the notification names another plan: {lines[0]!r}"
    )
    assert "await-expiry" in (lines[0].get("failed_items") or []), (
        f"the notification names other items: {lines[0]!r}"
    )
    deliveries = plan.channel_deliveries()
    assert len(deliveries) == 1, f"the channel received {len(deliveries)} messages"


# ---------------------------------------------------------------------------
# S6 —— 等待期间的槽位
# ---------------------------------------------------------------------------


def test_waiting_plan_releases_slot(boot_plan):
    """S6 —— 等待期间槽位占用为 0，新 plan 立刻拿到槽位。

    一个等着人回答的 plan 不占着机器：它没有活着的执行进程，所以调度器
    的读模型里它既不算 ``running``，也不占容量。等它把槽位让出来之后，
    新来的 plan 立刻拿到那个槽位 —— 不需要等容量释放，也不需要排队。

    断言的是调度器**自己的读模型**（``GET /api/scheduler/state``）和状态库
    里的槽位行，不是引擎的内部状态。
    """
    plan = boot_plan(_book_one_waits(), scheduler_enabled=True)

    # --- the plan waits for a human, and it held a slot ----------------
    registry = plan.registry()
    registry.create_human_wait(
        {"id": WAIT_ITEM, "wait": {"type": "human"}}, QUESTION
    )
    plan.pin_fingerprint(WAIT_ITEM, "waiting")
    assert plan.seed_slot(PLAN_ID) is True, "the slot was not written"

    # --- the scheduler gives the slot back on its own tick --------------
    assert plan.wait_for_slot_release(PLAN_ID), (
        f"the waiting plan still holds a slot: {plan.slot_rows()!r}\n"
        f"{plan.server_output()}"
    )

    state = plan.wait_for_scheduler(
        lambda read: read.get("capacity", {}).get("used") == 0
        and not read.get("running")
    )
    assert state is not None, (
        "the waiting plan kept its slot:\n"
        f"{plan.scheduler_state()!r}\n{plan.server_output()}"
    )
    assert state["capacity"]["used"] == 0, (
        f"the waiting plan occupies capacity: {state!r}"
    )
    assert state["running"] == [], f"the waiting plan is still running: {state!r}"
    assert plan.slot_rows() == [], (
        f"the waiting plan still holds a slot: {plan.slot_rows()!r}"
    )

    # --- a new plan takes the slot straight away -----------------------
    plan.start_second_plan()
    state = plan.wait_for_scheduler(
        lambda read: read.get("capacity", {}).get("used") == 1
        and [row.get("plan_id") for row in read.get("running") or []]
        == [SECOND_PLAN_ID]
    )
    assert state is not None, (
        f"the new plan did not get a slot: {plan.scheduler_state()!r}\n"
        f"{plan.server_output()}"
    )
    assert state["capacity"]["used"] == 1, f"the capacity is wrong: {state!r}"
    assert [row.get("plan_id") for row in state["running"]] == [SECOND_PLAN_ID], (
        f"the slot belongs to another plan: {state!r}"
    )
    assert plan.wait_for_slot(SECOND_PLAN_ID), (
        f"the new plan does not hold the slot: {plan.slot_rows()!r}"
    )
    assert [row.get("plan_id") for row in plan.slot_rows()] == [SECOND_PLAN_ID], (
        f"the new plan does not hold the slot: {plan.slot_rows()!r}"
    )


# ---------------------------------------------------------------------------
# S4 + S6 —— 崩溃与恢复
# ---------------------------------------------------------------------------


def test_kill_and_restart_does_not_repeat_outbound_action(boot_plan):
    """S4/S6 —— kill -9 之后重启：只处理未完成的条目，对外动作不重复。

    这一条钉的是**恢复**的边界：进程被 ``kill -9`` 之后，重启起来的那个
    进程只处理还没做完的条目。已经做过的对外动作 —— 推进裸仓的那条分支、
    调过 ``gh`` 的那一次 —— 一个都不许重复。

    重启本身也走就绪探针（``GET /api/health``），而且**不复用上一个进程的
    端口**：端口上还在答话的，就是旧进程，不是新进程。
    """
    plan = boot_plan(_book_outward_actions_then_a_wait())

    # --- the waiting item is registered before the round starts --------
    registry = plan.registry()
    record = registry.create_human_wait(
        {"id": WAIT_ITEM, "wait": {"type": "human"}}, QUESTION
    )
    plan.pin_fingerprint(WAIT_ITEM, "waiting")

    # --- the round runs the two outward items, and waits on the third --
    plan.start_verification()
    progress = plan.wait_for_progress(
        {
            "publish-branch": "passed",
            "open-pr": "passed",
            WAIT_ITEM: "waiting",
        }
    )
    assert progress is not None, (
        f"the stage did not settle its items:\n{plan.server_output()}"
    )
    branches = plan.bare_branches()
    gh_calls = plan.gh_calls()
    assert PUBLISHED_BRANCH in branches, (
        f"the bare repo holds {branches!r}, not the published branch"
    )
    assert len(gh_calls) == 1, f"the gh stub was called {len(gh_calls)} times"
    assert plan.report() is None, (
        "a stage that is waiting wrote a terminal report: "
        f"{plan.report()!r}"
    )

    # --- the process is killed the hard way ----------------------------
    old_port = plan.port
    plan.kill9()
    assert plan.port_is_closed(old_port), (
        f"something is still answering on the old port {old_port}"
    )

    # --- a new process, on a new port, through the readiness probe ----
    plan.restart()
    assert plan.port != old_port, (
        f"the restart reused the old port {old_port}"
    )
    status, body = plan.http("GET", "/api/health")
    assert status == 200, f"the restarted server is not ready: {status} {body!r}"

    # --- the restart repeated nothing ---------------------------------
    assert plan.bare_branches() == branches, (
        f"the restart published again: {plan.bare_branches()!r} != {branches!r}"
    )
    assert plan.gh_calls() == gh_calls, (
        f"the restart called gh again: {plan.gh_calls()!r} != {gh_calls!r}"
    )

    # --- the resumed stage repeats nothing either ----------------------
    result = plan.stage(waits=registry).resume()
    assert result["terminal_status"] is None, (
        f"the resumed stage finished while an item was waiting: {result!r}"
    )
    assert plan.bare_branches() == branches, (
        f"the resumed stage published again: {plan.bare_branches()!r}"
    )
    assert plan.gh_calls() == gh_calls, (
        f"the resumed stage called gh again: {plan.gh_calls()!r}"
    )
    assert plan.report() is None, (
        f"the resumed stage wrote a report: {plan.report()!r}"
    )

    # --- the answer wakes the waiting item up --------------------------
    status, body = plan.http(
        "POST",
        f"/api/hitl/{PLAN_ID}/decisions/{record['decision_id']}/answer",
        {"option": "proceed"},
    )
    assert status == 200, f"the answer was refused: {status} {body!r}"
    registry.resolve_human_wait(WAIT_ITEM, "proceed")
    assert plan.waiting_rows() == [], (
        f"the plan is still waiting: {plan.waiting_rows()!r}"
    )

    result = plan.stage(waits=registry).resume()
    assert result["terminal_status"] == "completed", (
        f"the stage did not complete after the answer: {result!r}"
    )
    assert APPROVED_BRANCH in plan.bare_branches(), (
        f"the bare repo holds {plan.bare_branches()!r}, not the approved branch"
    )
    assert plan.gh_calls() == gh_calls, (
        f"the stage called gh again: {plan.gh_calls()!r}"
    )
