"""TDD spec —— 移除审计的**故障路径**（真实文件系统 + 故障注入 + 崩溃）。

背景
----
「先归档、后移除」这条契约在正常路径上是显然的：归档写成功了才移除。真正
的风险全在**写不进去**的那一侧 —— 只读目录、磁盘满、fsync 失败、进程被
杀掉。这些只有在**真实文件系统**上才成立：把 ``open`` mock 掉，等于把
「会不会留下半行」「崩溃之后读回来是什么」这两个问题一起 mock 掉了。

所以这一组测试全部用真的目录、真的文件、真的子进程。故障注入只用来模拟
**环境**的故障（fsync 失败），不 mock 生产代码的行为。

TDD contract —— 5 cases
------------------------
1. ``test_readonly_dir_keeps_vp`` —— 只读目录 → 归档写不进去 → 移除不发生，
   那条验证点照跑不误。
2. ``test_fsync_error_aborts_removal`` —— fsync 抛错 → 归档失败 → 移除不发生，
   审计文件里也没有半行。
3. ``test_crash_recovery_no_raise`` —— 子进程写完首行后被 ``SIGKILL`` →
   重启读回来不抛错，且**只**认完整行（宁可重验，不可漏验）。
4. ``test_concurrent_append_no_interleave`` —— 8 个线程并发追加 → 8 行，
   每行都是合法 JSON，没有交错。
5. ``test_record_fields_complete`` —— 归档行的 13 个字段齐全且类型正确。

边界条件
--------
* 目录只读（``chmod 0o500``）→ 删除中止、VP 保留。
* 故障注入只在模拟环境故障时使用，``finally`` 里恢复。
* 崩溃恢复：读路径不抛错，返回集合 ⊆ 已完整写入的行。
* 审计文件是唯一的持久化真相：归档失败 ⇒ 移除不发生。

测试命令::

    pytest backend/tests/integration/verification_chain/test_vp_removal_audit.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

# ``backend/`` 与仓库根进 sys.path，``import verification.*`` 才成立 ——
# 与仓库里其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)
from verification.vp_removal_audit import (  # noqa: E402
    AUDIT_FILENAME,
    archive_removal,
    load_removed_vp_ids,
)
from verification_executor import VerificationExecutor  # noqa: E402


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 场景常量
# ---------------------------------------------------------------------------

#: 夹具计划（任务 7 的交付物）：一条主题外、一条孤儿、一条正常。
FIXTURE_PLAN_PATH = (
    _BACKEND_DIR
    / "tests"
    / "fixtures"
    / "verification"
    / "vp-fixture-002"
    / "verification_plan.json"
)

#: 被测的审计模块本身（子进程按文件路径加载它）。
_MODULE_PATH = _BACKEND_DIR / "verification" / "vp_removal_audit.py"

#: 主题外的那条：它声明的 ``AC-NOT-IN-PRD`` 不在本计划的判据表里。
OFF_TOPIC_VP = "VP-900"

#: 正常的那条：声明的 ``AC-001`` 在判据表里。
NORMAL_VP = "VP-901"

#: 本测试用的计划 id（与夹具的 id 区分开 —— 夹具目录本身是只读的）。
PLAN_ID = "vp-removal-audit-faults"

#: 计划自己的判据表（编号式）。
CRITERIA: List[Dict[str, str]] = [
    {"id": "AC-001", "text": "用户可以用邮箱和密码登录"},
    {"id": "AC-002", "text": "用户可以导出 CSV 报表"},
]

#: 归档行的 13 个字段 —— 生产代码 ``VerificationExecutor._removal_record``
#: 的形状。字段**多一个少一个**都是契约破了，所以断言的是集合相等，不是
#: 「至少包含」。
EXPECTED_FIELDS: Dict[str, type] = {
    "schema_version": int,
    "ts": str,
    "plan_id": str,
    "round": int,
    "component": str,
    "vp_id": str,
    "decision": str,
    "tier": str,
    "reason": str,
    "evidence": dict,
    "vp": dict,
    "verdict": dict,
    "allocated_vp_ids_preserved": bool,
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """计划目录 —— 审计文件就落在它里面。"""
    d = tmp_path / PLAN_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def verif_repo(tmp_path: Path) -> VerificationRepository:
    """真的 SQLite 仓库 —— 裁决落在真实的持久化状态上。"""
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(PLAN_ID, verification_status="running")
    conn.commit()
    return repo


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _plan() -> Dict[str, Any]:
    """夹具计划 + 一份编号式判据表。"""
    payload = json.loads(FIXTURE_PLAN_PATH.read_text(encoding="utf-8"))
    return {
        "plan_id": PLAN_ID,
        "verification_points": [
            dict(vp) for vp in payload["verification_points"]
        ],
        "criteria": [dict(c) for c in CRITERIA],
    }


def _executor(
    plan_dir: Path,
    repo: VerificationRepository,
    ran: List[str],
) -> VerificationExecutor:
    """执行器本体。``topic_relevance_llm_batch`` 缺省 = None（没有模型）。"""

    async def _run(vp_node: Dict[str, Any]) -> Dict[str, Any]:
        ran.append(str(vp_node.get("id")))
        return {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    return VerificationExecutor(
        verification_plan=_plan(),
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=_run,
        verif_repo=repo,
    )


def _active_ids(executor: VerificationExecutor) -> List[str]:
    """活动集合 —— 这一轮**会跑**的验证点（计划里的，不是已移除的）。"""
    return [str(vp.get("id")) for vp in executor._build_items_from_plan()]


def _audit_rows(plan_dir: Path) -> List[Dict[str, Any]]:
    """审计文件里的每一行（坏行跳过，与读路径同一套容错）。"""
    path = plan_dir / AUDIT_FILENAME
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _record(vp_id: str, **overrides: Any) -> Dict[str, Any]:
    """一条移除记录 —— 生产代码归档的那种形状。"""
    record: Dict[str, Any] = {
        "schema_version": 1,
        "ts": "2026-10-27T00:00:00Z",
        "plan_id": PLAN_ID,
        "round": 1,
        "component": "verification_executor",
        "vp_id": vp_id,
        "decision": "llm_irrelevant",
        "tier": "llm",
        "reason": "主题外",
        "evidence": {"matched_criterion_ids": []},
        "vp": {"id": vp_id},
        "verdict": {"status": "pending"},
        "allocated_vp_ids_preserved": True,
    }
    record.update(overrides)
    return record


@contextmanager
def _fsync_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """让 ``os.fsync`` 抛错 —— 模拟环境的 fsync 故障。

    故障只在**模拟环境故障**时存在：``finally`` 里 ``monkeypatch.undo()``
    立刻还原，后面的人拿到的是好的 ``fsync``。
    """
    monkeypatch.setattr(
        os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("boom")),
    )
    try:
        yield
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# 1. 只读目录 → 删除中止、VP 保留
# ---------------------------------------------------------------------------


def test_readonly_dir_keeps_vp(
    plan_dir: Path,
    verif_repo: VerificationRepository,
) -> None:
    """只读目录 → 归档写不进去 → 移除不发生，那条验证点照跑不误。

    这是「归档写不进去 ⇒ 移除不发生」在真实文件系统上的样子：把计划目录的
    写位去掉，审计文件就写不进去。写不进去就必须**留着**那条验证点 ——
    一次没有证据的移除是一次静悄悄的严格度损失。

    「留着」不等于「这一轮不算它」：它仍然要跑，仍然要出裁决。
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip(
            "root ignores the directory's write bit — the fault cannot be injected"
        )

    ran: List[str] = []
    executor = _executor(plan_dir, verif_repo, ran)

    plan_dir.chmod(0o500)
    try:
        asyncio.run(executor.run(max_parallel=1))
    finally:
        # 恢复写位 —— 否则 pytest 清理 tmp_path 时会失败。
        plan_dir.chmod(0o700)

    # 移除没有发生：审计文件里没有移除集。
    assert load_removed_vp_ids(plan_dir) == set(), _audit_rows(plan_dir)
    assert _audit_rows(plan_dir) == []

    # 它还在活动集合里（正常的那条也在 —— 活动集合没有被削掉什么）。
    assert OFF_TOPIC_VP in _active_ids(executor), _active_ids(executor)
    assert NORMAL_VP in _active_ids(executor), _active_ids(executor)

    # 而且它这一轮照跑不误。
    assert OFF_TOPIC_VP in ran, ran


# ---------------------------------------------------------------------------
# 2. fsync 失败 → 移除不发生、无半行
# ---------------------------------------------------------------------------


def test_fsync_error_aborts_removal(
    plan_dir: Path,
    verif_repo: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fsync 抛错 → 归档失败 → 移除不发生，文件里也没有半行。

    fsync 是「归档写成功了」这句话的**兑现点**：它保证记录真的落盘，而不只是
    躺在页缓存里。它一失败，这次归档就没有兑现 —— 于是那条验证点必须留着。

    更要紧的是**审计文件不能留下半行**：半行读不出来，等于「归档过」的证据
    没了，而文件里多了一行看不懂的东西。写失败就得让文件回到原样。
    """
    ran: List[str] = []
    executor = _executor(plan_dir, verif_repo, ran)

    with _fsync_fails(monkeypatch):
        # 直接调归档：异常一路冒上去（不吞、不降级）。
        with pytest.raises(OSError):
            archive_removal(_record(OFF_TOPIC_VP), plan_dir)

        # 执行器那一侧：归档失败 → 留着 → 照跑。
        asyncio.run(executor.run(max_parallel=1))

    # 移除没有发生 —— 审计文件里没有移除集。
    assert load_removed_vp_ids(plan_dir) == set(), _audit_rows(plan_dir)

    # 它还在活动集合里，而且这一轮跑了。
    assert OFF_TOPIC_VP in _active_ids(executor), _active_ids(executor)
    assert OFF_TOPIC_VP in ran, ran

    # 没有半行：文件要么不存在，要么每一行都是**完整**的 JSON 对象。
    audit = plan_dir / AUDIT_FILENAME
    if audit.is_file():
        for line in audit.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            json.loads(line)  # 解析不了 = 半行 = 契约破了


# ---------------------------------------------------------------------------
# 3. 崩溃恢复
# ---------------------------------------------------------------------------


#: 子进程脚本：写完一整行 → 写半行 → 把自己硬杀掉。
_CHILD_SCRIPT = '''
import importlib.util, os, signal, sys

module_path, plan_dir = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("vp_removal_audit", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# 1. 完整的一行。
module.archive_removal({"vp_id": "VP-CRASH-1"}, plan_dir)

# 2. 写到一半 —— 模拟「进程在写的中途被杀」。
audit = os.path.join(plan_dir, module.AUDIT_FILENAME)
with open(audit, "a", encoding="utf-8") as handle:
    handle.write('{"vp_id": "VP-CRASH-2", "decision": "llm_irrele')
    handle.flush()
    os.fsync(handle.fileno())

# 3. 硬杀 —— 没有 finally，没有 flush，没有收尾。
os.kill(os.getpid(), signal.SIGKILL)
'''


def test_crash_recovery_no_raise(plan_dir: Path) -> None:
    """子进程写完首行后被 ``SIGKILL`` → 重启读回来不抛错，且只认完整行。

    审计文件是追加写的，所以它的尾部可能因为写入进程被杀而留下半行。读的时候
    逐行解析，**坏行跳过、不抛异常** —— 半行不是缺失，缺失是「没有这个文件」。

    宁可重验，不可漏验：半行那条验证点的 id **不在**移除集里，所以下一轮它
    还会被验证 —— 比起悄悄少验一条，多验一条只是多花点钱。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(_MODULE_PATH), str(plan_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == -signal.SIGKILL, (
        proc.returncode, proc.stdout, proc.stderr,
    )

    audit = plan_dir / AUDIT_FILENAME
    assert audit.is_file(), "the child must have written the first line"

    raw = audit.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 2, lines
    # 第二行确实是半行（写了一半就被杀了）。
    with pytest.raises(ValueError):
        json.loads(lines[1])

    # 读回来不抛错。
    removed = load_removed_vp_ids(plan_dir)

    # 只认完整行。
    assert removed == {"VP-CRASH-1"}, removed
    assert "VP-CRASH-2" not in removed, removed


# ---------------------------------------------------------------------------
# 4. 并发追加不交错
# ---------------------------------------------------------------------------


def test_concurrent_append_no_interleave(plan_dir: Path) -> None:
    """8 个线程并发追加 → 8 行，每行都是合法 JSON，没有交错。

    审计文件是很多条移除路径共享的落点：并发写进去的两行一旦交错，两个
    JSON 对象会粘成一行 —— 那行读不出来，两条移除记录一起丢。两条移除记录
    一起丢 = 两条验证点被漏验（或者更糟：一条被误认成另一条）。

    所以这里断言的是**每一行都能单独解析**，而不只是「行数对」。
    """
    errors: List[BaseException] = []
    expected: List[str] = []

    for round_no in range(5):
        ids = [f"VP-R{round_no}-{index}" for index in range(8)]
        expected.extend(ids)
        barrier = threading.Barrier(len(ids))

        def _append(vp_id: str, _barrier: threading.Barrier = barrier) -> None:
            try:
                _barrier.wait(timeout=60)
                archive_removal(_record(vp_id), plan_dir)
            except BaseException as exc:  # noqa: BLE001 - 收集起来一起报
                errors.append(exc)

        threads = [threading.Thread(target=_append, args=(vp_id,)) for vp_id in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
            assert not thread.is_alive(), "a writer thread hung"

        assert not errors, errors

        lines = (plan_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(expected), (len(lines), len(expected))

        # 每一行都必须是**合法**的 JSON —— 交错会在这里现形。
        parsed = [json.loads(line) for line in lines]
        assert sorted(str(row["vp_id"]) for row in parsed) == sorted(expected), parsed

    assert load_removed_vp_ids(plan_dir) == set(expected)


# ---------------------------------------------------------------------------
# 5. 归档行的字段
# ---------------------------------------------------------------------------


def test_record_fields_complete(
    plan_dir: Path,
    verif_repo: VerificationRepository,
) -> None:
    """归档行的 13 个字段齐全，且类型正确。

    这行是这次移除**唯一**的持久化真相：它得能回答「谁、什么时候、哪一轮、
    依据什么判的、这条验证点当时是什么状态」。少一个字段，下一次有人来读
    这个文件时就少一个能问出来的问题。
    """
    ran: List[str] = []
    executor = _executor(plan_dir, verif_repo, ran)

    asyncio.run(executor.run(max_parallel=1))

    rows = _audit_rows(plan_dir)
    assert len(rows) == 1, rows
    row = rows[0]

    assert set(row) == set(EXPECTED_FIELDS), (
        sorted(set(row) ^ set(EXPECTED_FIELDS)),
    )
    for field, expected_type in EXPECTED_FIELDS.items():
        value = row[field]
        assert isinstance(value, expected_type), (field, value, expected_type)
        if expected_type is int:
            # ``bool`` 是 ``int`` 的子类 —— round 不许是个布尔。
            assert not isinstance(value, bool), (field, value)

    assert row["vp_id"] == OFF_TOPIC_VP, row
    assert row["plan_id"] == PLAN_ID, row
    assert row["component"] == "verification_executor", row
    assert row["vp"] == {"id": OFF_TOPIC_VP}, row
    assert row["allocated_vp_ids_preserved"] is True, row
    assert row["verdict"]["status"] in {"pending", "PASSED"}, row