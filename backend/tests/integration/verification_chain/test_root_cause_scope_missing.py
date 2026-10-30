"""TDD spec —— 根因报告缺「改动范围」时，链判 blocked 并留下**看得见**的告警。

背景
----
根因链（``_root_cause_chain_decision``）里有一条分支：根因报告回来了，但既
没有 ``## 改动范围`` 段，也没有 JSON 正文 —— 范围是空的。猜范围就是让修复
任务去动错的文件，所以这一步直接转人工（``BLOCKED_ON_USER`` /
``root_cause_report_without_scope``）。

问题在于：**这件事只改了内存里的 entry**。不写日志，round report 里也没有
字段 —— 于是「为什么停了」只有一个读了链记录的人才看得见。不读后端日志就
看不到为什么停；全库 20 份根因报告有 11 条卡在这里。

两份契约
--------
1. ``test_report_without_scope_blocks_and_warns`` —— 缺范围的报告让链判
   blocked（``root_cause_scope_missing == ["VP-3"]``、``blocked_on_user``、
   不派新任务），并且**留下一个可查的告警事件**：事件名稳定，四个字段齐全。
2. ``test_scope_report_is_not_flagged`` —— 报告带了 ``## 改动范围`` 时，
   **没有**这个告警事件，而且照常派实现任务（负控）。

轮次序列
--------
和根因任务流那套一样，每轮一个**全新**的 orchestrator（生产就是这样跑的：
自动循环每修完一次就重建 orchestrator，所以一轮记得的东西必须能从磁盘上
回来）::

    第 1 轮  fail aaa   → 一个普通修复任务
    第 2 轮  fail bbb   → 根因任务（指纹变了，不是第三个盲修复）
    第 3 轮  报告已交付 → 有范围 → 实现任务 / 没范围 → 转人工 + 告警

边界条件
--------
* 告警走既有的 ``self._log_audit``：优先 ``self.logger``，缺省用 module
  logger；日志失败不得中断 round。
* 转 blocked 之后下轮不再进这个分支 —— 同一个点只告警一次。
* 报告带 scope 时不告警，照常派 implementation。
* 空链返回同样带 ``scope_missing: []``。

测试命令::

    pytest backend/tests/integration/verification_chain/test_root_cause_scope_missing.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


pytestmark = pytest.mark.integration


#: The point the rounds below fail on.
VP_ID = "VP-3"

#: The failure class the rounds carry. It counts (it is not ``infra`` /
#: ``out_of_scope``), so the old key's counter moves.
FAILURE_CLASS = "deliverable_defect"

#: The chain's record — the file that says where the escalation stands.
CHAIN_FILENAME = "verification_root_cause_chain.json"

#: The stage the chain reaches when a human has to take over.
CHAIN_STAGE_BLOCKED_ON_USER = "BLOCKED_ON_USER"

#: The file the root cause report lands in (the report the diagnosis
#: task delivers, and the implementation task reads).
ROOT_CAUSE_REPORT_NAME = f"root-cause-{VP_ID}.md"

#: The scope the root cause report delivers — the implementation task's
#: ``files_to_modify`` comes from exactly these paths.
REPORT_SCOPE = ["backend/app/service.py"]

#: The stable event name the warning carries. It is the name an operator
#: greps for; it must not change with the wording of the message.
SCOPE_MISSING_EVENT = "root_cause_scope_missing"

#: The four fields the event's data carries.
SCOPE_MISSING_FIELDS = {"plan_id", "vp_id", "report_path", "reason"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "root-cause-scope-missing"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "root-cause-scope-missing"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Keep the orchestrator's state machine out of the real ``state.db``."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the round's rows are observable.

    The generator **echoes** the candidates it was handed, so what the
    round decided about a row is what the caller can read back. The
    assembler is the real one — the chain's tasks do not go through it
    (they carry their own ids and their own ``kind``), and the plain
    repair rows must look exactly like the ones production builds.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator:
        mock_generator.return_value.generate_repair_contents.side_effect = (
            lambda candidates, *args, **kwargs: [
                {
                    "failed_vp_id": str(candidate.get("id")),
                    "title": f"fix {candidate.get('id')}",
                    "description": f"fix {candidate.get('id')}",
                    "files_to_modify": ["src/app.py"],
                    "test_command": "pytest -q",
                }
                for candidate in candidates
            ]
        )
        yield mock_generator


class RecordingLogger:
    """A structured logger that keeps the events it was handed.

    The orchestrator's audit lines go to ``self.logger`` when one is
    injected, in the structured dialect: ``warning(event, message,
    data={...})``. This recorder speaks exactly that dialect, so the
    events the round emitted can be read back after the fact.
    """

    def __init__(self):
        self.events = []

    def warning(self, event, message="", data=None):
        self.events.append({
            "event": event,
            "message": message,
            "data": dict(data or {}),
        })

    def events_named(self, event):
        """The events carrying one name."""
        return [item for item in self.events if item["event"] == event]


# ---------------------------------------------------------------------------
# The scenario on disk
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the orchestrator reads on construction."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8",
    )


def _write_plan(plan_dir):
    """The verification plan — one point, the one the rounds fail on."""
    payload = {
        "plan_id": plan_dir.name,
        "verification_points": [
            {
                "id": VP_ID,
                "title": "the endpoint returns 200",
                "verification_method": "api_test",
                "related_prd_criteria": "AC-1",
                "expected_result": "the endpoint returns 200",
                "verification_phase": 1,
            },
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _fail_report(fp="aaa"):
    """A round report: the point failed, with the given fingerprint label.

    The fingerprint is a hash of the failure's evidence, so the label is
    carried by the error line — two rounds with two different labels are
    two different failures under the same name, which is exactly the
    case the old decision key cannot see.
    """
    error = f"AssertionError: fingerprint {fp}"
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "failure_class": FAILURE_CLASS,
                "actual_result": error,
                "evidence": error,
            },
        ],
        "requirement_deviations": [],
    }


def _write_report(plan_dir, report):
    """The round's report on disk — what the repair path reads."""
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_root_cause_report(plan_dir, scope=()):
    """The root cause report the diagnosis task delivers.

    ``scope`` empty → the report names **no** file to change: neither a
    ``改动范围`` section nor a JSON body. That is the diagnosis that came
    back without saying what to change — nobody can act on it, so it goes
    to a human.

    ``scope`` non-empty → the report names those paths under a
    ``改动范围`` heading, and that section is the implementation task's
    ``files_to_modify``.
    """
    reports_dir = plan_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    body = [
        f"# {VP_ID} 根因报告",
        "",
        "## 根因",
        "断言读到 401 而不是 200 —— 鉴权中间件在校验之前就短路了。",
        "",
    ]
    if scope:
        body.append("## 改动范围")
        body.extend(f"- `{path}`" for path in scope)
        body.append("")
    body.extend([
        "## 验收方式",
        "pytest -q backend/tests/test_service.py",
        "",
    ])
    (reports_dir / ROOT_CAUSE_REPORT_NAME).write_text(
        "\n".join(body), encoding="utf-8",
    )


def _read_chain(plan_dir):
    """The chain's record on disk, parsed."""
    path = plan_dir / CHAIN_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_orchestrator_rounds(rounds, plan_dir, project_dir, logger=None):
    """Run a sequence of rounds; return ``[(orchestrator, result), ...]``.

    ``rounds`` is the brief's sequence — one entry per round, in order:

    * ``("fail", fingerprint)`` —— the point fails, with that fingerprint;
    * ``("report_without_scope", fingerprint)`` —— the diagnosis landed,
      but the report names no file to change;
    * ``("report_with_scope", fingerprint)`` —— the diagnosis landed and
      the report says what to change.

    Every round runs through a **fresh** orchestrator: that is how
    production runs it — the auto-loop rebuilds the orchestrator after
    every repair, so anything a round remembers has to come back from
    disk.
    """
    results = []
    for round_number, (kind, fingerprint) in enumerate(rounds, start=1):
        if kind == "report_without_scope":
            _write_root_cause_report(plan_dir, scope=())
        elif kind == "report_with_scope":
            _write_root_cause_report(plan_dir, scope=REPORT_SCOPE)
        report = _fail_report(fingerprint)
        _write_report(plan_dir, report)
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
        if logger is not None:
            orch.logger = logger
        orch.start_verification_cycle(round_number=round_number)
        result = orch.check_cycle_conditions(
            report, round_number=round_number,
        )
        results.append((orch, result))
    return results


def _kinds(tasks):
    """The kinds of a round's tasks.

    A plain repair row carries no ``kind`` — the repair generator's rows
    never had one — so it reads as ``"repair"`` here.
    """
    return {str(task.get("kind") or "repair") for task in tasks or []}


# ---------------------------------------------------------------------------
# 1. 缺范围的报告 → 判 blocked + 一条看得见的告警
# ---------------------------------------------------------------------------


def test_report_without_scope_blocks_and_warns(
    plan_dir, project_dir, repair_mocks,
):
    """根因报告没说要改哪里 → 链判 blocked，并且**留下告警**。

    报告回来了，但既没有 ``## 改动范围`` 段，也没有 JSON 正文。范围是空
    的 —— 没有范围就没有「改哪里」，猜出来的范围会让修复任务去动错的文
    件，所以这一步转人工。

    但「转人工」不能只活在内存里：不写日志、汇总里没有字段的话，不读后
    端日志就看不到为什么停。所以这一轮必须同时留下：
      * 汇总里的 ``root_cause_scope_missing``（哪些点卡在这里）；
      * 一条事件名稳定、四个字段齐全的告警（为什么）。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    logger = RecordingLogger()
    results = run_orchestrator_rounds(
        [
            ("fail", "aaa"),
            ("fail", "bbb"),
            ("report_without_scope", "bbb"),
        ],
        plan_dir, project_dir, logger=logger,
    )
    _, r2 = results[1]
    _, r3 = results[2]

    # 第 2 轮：指纹变了 → 派根因任务（诊断还没交付）。
    assert _kinds(r2["repair_tasks"]) == {"root_cause"}, (
        f"the second round dispatches the diagnosis: {r2['repair_tasks']!r}"
    )

    # 第 3 轮：诊断交付了，但报告没说改哪里 → 转人工。
    assert r3["root_cause_scope_missing"] == [VP_ID], (
        "the round must say which point is stuck on a scope-less report: "
        f"{r3.get('root_cause_scope_missing')!r}"
    )
    assert r3["blocked_on_user"] is True, (
        f"the round ends with a person in the loop: {r3!r}"
    )
    assert r3["repair_tasks"] == [], (
        f"no task may be dispatched: {r3['repair_tasks']!r}"
    )
    assert r3["root_cause_chain"]["stage"] == CHAIN_STAGE_BLOCKED_ON_USER, (
        f"the chain stops at the human: {r3['root_cause_chain']!r}"
    )

    # 告警：事件名稳定，四个字段齐全，一个点只告警一次。
    events = logger.events_named(SCOPE_MISSING_EVENT)
    assert len(events) == 1, (
        "the scope-less report must warn exactly once: "
        f"{logger.events!r}"
    )
    data = events[0]["data"]
    assert set(data) == SCOPE_MISSING_FIELDS, (
        f"the event must carry exactly these four fields: {data!r}"
    )
    assert data["plan_id"] == plan_dir.name
    assert data["vp_id"] == VP_ID
    assert data["report_path"], (
        f"the event must say which report was read: {data!r}"
    )
    assert ROOT_CAUSE_REPORT_NAME in str(data["report_path"]), (
        f"the report path must survive the warning: {data!r}"
    )
    assert data["reason"], f"the event must say why: {data!r}"


# ---------------------------------------------------------------------------
# 2. 带范围的报告 → 不告警，照常派实现任务（负控）
# ---------------------------------------------------------------------------


def test_scope_report_is_not_flagged(plan_dir, project_dir, repair_mocks):
    """报告带了 ``## 改动范围`` → 没有告警，照常派实现任务。

    这是上面那条契约的**负控**：如果缺范围的报告和带范围的报告都告警，那
    告警就不再是「出了什么事」的信号，而是一段噪声 —— 它指向的问题也就
    没有被定位。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    logger = RecordingLogger()
    results = run_orchestrator_rounds(
        [
            ("fail", "aaa"),
            ("fail", "bbb"),
            ("report_with_scope", "bbb"),
        ],
        plan_dir, project_dir, logger=logger,
    )
    _, r3 = results[2]

    assert not logger.events_named(SCOPE_MISSING_EVENT), (
        "a report that says what to change must not warn: "
        f"{logger.events!r}"
    )
    assert r3.get("root_cause_scope_missing") == [], (
        "no point is stuck on a scope-less report: "
        f"{r3.get('root_cause_scope_missing')!r}"
    )

    # 照常派实现任务 —— 范围就是报告里的那几行。
    tasks = r3["repair_tasks"]
    assert _kinds(tasks) == {"implementation"}, (
        f"the report with scope dispatches the implementation: {tasks!r}"
    )
    assert sorted(tasks[0]["files_to_modify"]) == sorted(REPORT_SCOPE), (
        f"the scope comes from the report: {tasks[0]['files_to_modify']!r}"
    )


# ---------------------------------------------------------------------------
# 3. 转人工之后不再重复告警
# ---------------------------------------------------------------------------


def test_scope_missing_warns_only_once(
    plan_dir, project_dir, repair_mocks,
):
    """同一个点只告警一次 —— 转人工之后下轮不再进这个分支。

    链一旦把点交给人，磁盘上的 stage 就是 ``BLOCKED_ON_USER``：下一轮读
    到它就直接跳过，不会再走一遍「报告缺范围」的判断。所以告警不会每一
    轮刷一条 —— 一个点一次。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    logger = RecordingLogger()
    run_orchestrator_rounds(
        [
            ("fail", "aaa"),
            ("fail", "bbb"),
            ("report_without_scope", "bbb"),
            ("fail", "bbb"),
        ],
        plan_dir, project_dir, logger=logger,
    )

    events = logger.events_named(SCOPE_MISSING_EVENT)
    assert len(events) == 1, (
        f"the point must be warned about once, not once per round: "
        f"{logger.events!r}"
    )

    chain = _read_chain(plan_dir)
    assert chain["stage"] == CHAIN_STAGE_BLOCKED_ON_USER, (
        f"the chain's record says where it stopped: {chain!r}"
    )
