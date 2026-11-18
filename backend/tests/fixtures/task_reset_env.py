"""任务重置接口的测试环境（2026-11-12）。

这个模块是**夹具**，不是产品代码：它只把「一条失败过的任务」摆到
测试里，然后让测试去读回来。

摆出来的东西
------------
``seed_plan`` 造的是三样：

* ``plans/<plan_id>/tasks.json`` —— 任务的**静态定义**（规格）。
  重置不许改它，所以它必须存在，且内容有辨识度（不然「没被改动」
  这条断言什么也证明不了）。
* ``plan_tasks`` 行 —— 任务的**执行记账**。重置前它是 ``failed``：
  ``attempt=3``、有 ``failure_reason`` / ``end_ts`` / ``commit_sha``。
* ``plan_routing`` 行 —— 计划的**相位**。重置不许碰它，所以它必须
  在场，好让「相位没变」这条断言有东西可比。

为什么这些行要种在 SQLite 里而不是 JSON 里
------------------------------------------
重置接口写的就是 ``plan_tasks`` 表。JSON 里的静态定义是**规格**，
SQLite 里的行才是**执行记账** —— 两者分开存放，正是「重置只动记账、
不动规格」这条契约的物理形态。

其它助手
--------
``read_row`` 走仓储（解码过的 dict），``read_raw_row`` 走裸 SQL
（未经解码的原始列）。两个都要：规格字段的「逐字段相等」要用原始
列比 —— 解码后的 ``files_to_modify`` 是 list，比的是值；原始列比的是
**字节**。契约说的是「一个字节都不动」，那就用能看见字节的那条路。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: 默认计划号。带日期前缀，和仓库里其它计划的命名一致。
DEFAULT_PLAN_ID = "20261112-task-reset"

#: 失败那条的记账原值 —— ``cleared`` 必须原样回显它们。
FAILED_END_TS = "2026-11-12T01:00:00Z"
FAILED_REASON = "boom"

#: **规格**字段 —— 重置一个字节都不许动（对应 ``task_reset.SPEC_FIELDS``）。
SPEC_FIELDS = (
    "title",
    "description",
    "files_to_modify",
    "test_command",
    "depends_on",
    "project_dir",
)


def _task_spec(task_id: str, project_dir: Optional[str]) -> Dict[str, Any]:
    """这条任务的规格（静态定义）。

    值都挑了有辨识度的：标题不是空串，``files_to_modify`` 不是空表，
    ``test_command`` 不是空串 —— 一条「什么都是空」的任务会让
    「规格没被改动」这条断言变成空转。
    """
    return {
        "id": task_id,
        "title": "重置接口的主路径",
        "description": "规格字段：重置不得改动其中任何一个字节。",
        "files_to_modify": ["backend/routes/execution.py"],
        "test_command": "pytest backend/tests/api/test_task_reset_happy.py",
        "depends_on": [],
        "project_dir": project_dir,
    }


def seed_plan(
    plans_dir: Any,
    db_path: Any,
    *,
    plan_id: str = DEFAULT_PLAN_ID,
    task_id: str = "1",
    status: str = "failed",
    attempt: int = 3,
    commit_sha: str = "a" * 40,
    phase: str = "executing",
    project_dir: Optional[str] = None,
) -> Tuple[Path, Path]:
    """种一条「失败过的任务」：目录 + 规格文件 + 两行 SQLite。

    返回 ``(plans_dir, db_path)`` —— 两个路径原样回传，好让调用方
    接着往同一个环境里读。
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    plans_dir = Path(plans_dir)
    db_path = Path(db_path)

    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    spec = _task_spec(task_id, project_dir or str(plan_dir))
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [spec]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
        repo = PlanTaskRepository(conn)
        repo.add_task(plan_id, spec)
        repo.update_task(
            plan_id=plan_id,
            task_id=task_id,
            fields={
                "status": status,
                "attempt": attempt,
                "commit_sha": commit_sha,
                "failure_reason": FAILED_REASON,
                "end_ts": FAILED_END_TS,
            },
        )
    finally:
        conn.close()

    return plans_dir, db_path


def seed_execution(
    db_path: Any,
    plan_id: str,
    *,
    exec_status: str = "stopped",
    exec_pid: Optional[int] = None,
    project_dir: Optional[str] = None,
) -> None:
    """种一行 ``plan_execution``（一次执行的记录）。

    ``project_dir`` 缺省用 ``plans/<plan_id>`` —— 那才是这条计划真正
    住的地方。
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    db_path = Path(db_path)
    conn = open_db(db_path)
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            plan_id=plan_id,
            project_dir=project_dir or str(db_path.parent / "plans" / plan_id),
            exec_status=exec_status,
            exec_pid=exec_pid,
        )
    finally:
        conn.close()


def seed_running_execution(db_path: Any, plan_id: str, pid: Optional[int] = None) -> None:
    """种一行「还在跑」的 ``plan_execution``。

    ``pid`` 缺省用本进程的 pid —— 一个**确实活着**的 pid，才让
    「活进程持有时拒绝重置」那条守卫有东西可查。
    """
    seed_execution(
        db_path,
        plan_id,
        exec_status="running",
        exec_pid=pid if pid is not None else os.getpid(),
    )


def read_row(db_path: Any, plan_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    """读回 ``plan_tasks`` 的一行（经仓储，list 字段已解码）。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    db_path = Path(db_path)
    conn = open_db(db_path)
    try:
        migrate(conn)
        return PlanTaskRepository(conn).get_task(plan_id, task_id)
    finally:
        conn.close()


def read_raw_row(db_path: Any, plan_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    """读回 ``plan_tasks`` 的一行（裸 SQL，未经解码的原始列）。

    ``SELECT *`` —— 每一列都要看得见，包括没被 ``get_task`` 读的那
    几列。规格字段的「逐字段相等」要用它比：**字节**级别的相等，
    而不是解码之后的「值」级别的相等。
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
    finally:
        conn.close()


def post_reset(client: Any, plan_id: str, task_id: str, **body: Any):
    """打重置端点（``POST /api/execution/{plan_id}/tasks/{task_id}/reset``）。"""
    return client.post(
        f"/api/execution/{plan_id}/tasks/{task_id}/reset", json=body
    )


# ---------------------------------------------------------------------------
# 真进程：起一个真的 ``backend/server.py``
# ---------------------------------------------------------------------------
#
# 上面的种子只往磁盘上摆东西，**没有服务在跑**。要证明「一次真的重置
# 能走完」，还得有一个真的后端进程：真的监听一个端口、真的从磁盘读
# 那棵 ``plans/`` 和那份 ``state.db``、真的把审计写回磁盘。
#
# 下面两个助手就是那份「怎么起」的知识：环境怎么摆、什么时候算就绪。


def live_server_env(tmp_path: Any) -> Tuple[Dict[str, str], str]:
    """在 ``tmp_path`` 下摆好一个真服务进程要认的东西。

    摆出来的是三样，加一个端口号：

    * ``<tmp>/plans/`` —— 计划目录的根（``PDT_PLANS_DIR``）。
    * ``<tmp>/state.db`` —— 状态库的位置（``PDT_STATE_DB_PATH``，
      ``server._state_db_path`` 的第一优先级）。
    * ``<tmp>/scheduler.yaml`` —— 内容是 ``scheduler: {enabled: false}``，
      调度器不许在这个进程里替我们启动什么（``PDT_SCHEDULER_FILE``）。
    * 一个**空闲端口** —— ``PDT_PORT``。默认的 8000 上可能已经有服务在
      跑，所以这里自己挑一个：绑 ``127.0.0.1:0`` 让内核给一个号，
      拿到号立刻把套接字关掉。

    返回 ``(env, base_url)``：``env`` 是**子进程**的环境（从
    ``os.environ`` 复制而来），``base_url`` 是 ``http://127.0.0.1:<port>``。

    几处**故意**不同于继承来的环境：

    * ``PDT_DEBUG_ROUTES`` 被剔除 —— 调试路由不归这条链路管；
    * 五个变量被**显式**覆盖 —— 继承来的值指向操作者的机器
      （仓库的 ``plans/``、操作者的 ``state.db``、已经在跑的 8000 端口）；
    * 孤儿回收被关掉（``PDT_ORPHAN_REAPER=0``）：这个进程没有孤儿可收，
      而收孤儿要扫遍整台机器；
    * ``PYTHONPATH`` 补上仓库根 —— ``backend/prompts.py`` 写的是
      ``from backend.framework.prompts import ...``，那需要根在路径上；
    * provider 加载被**钉成 inherit 模式**（``PDT_PROVIDER_MODE``、
      ``PDT_PROVIDER_CAPACITY_FILE``），并且 ``ANTHROPIC_BASE_URL`` 与
      全部 ``PDT_SECRET_FD_*`` 被剔除 —— 理由见下面那段注释。
    """
    tmp_path = Path(tmp_path)
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "state.db"
    scheduler_file = tmp_path / "scheduler.yaml"
    scheduler_file.write_text(
        "scheduler:\n  enabled: false\n", encoding="utf-8"
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    # ``PYTHONPATH``：服务进程是以 ``server.py`` 为脚本起的，所以
    # ``sys.path[0]`` 是 ``backend/``。但 ``backend/prompts.py`` 写的是
    # ``from backend.framework.prompts import ...`` —— 那需要**仓库根**
    # 在路径上。两个都写上，哪一个都别缺。
    backend_dir = Path(__file__).resolve().parents[2]
    repo_root = backend_dir.parent
    inherited_path = os.environ.get("PYTHONPATH", "")

    child_env = dict(os.environ)
    child_env.pop("PDT_DEBUG_ROUTES", None)
    child_env.update(
        {
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_STATE_DB_PATH": str(db_path),
            "PDT_PORT": str(port),
            "PDT_SCHEDULER_FILE": str(scheduler_file),
            "AC_DISABLE_KEYCHAIN_SECRETS": "1",
            # 钥匙串那把开关在代码里的真名是
            # ``PDT_DISABLE_KEYCHAIN_SECRETS``（``backend/credentials.py``
            # 的 ``_SWITCH_ENV_KEY``）；两个都写，是为了让「不去碰钥匙串」
            # 这件事不取决于读者记住的是哪一个名字。
            "PDT_DISABLE_KEYCHAIN_SECRETS": "1",
            # 这个进程没有孤儿可收，而收孤儿要扫遍整台机器。
            "PDT_ORPHAN_REAPER": "0",
            # -----------------------------------------------------------------
            # provider 加载：钉成 inherit 模式。
            #
            # 服务的启动路径（``server.py`` 的 ``_lifespan`` → ``load_providers()``）
            # 会去问 CC Switch 要 provider 链。问不到就**启动失败**：
            # ``ProviderOrderError: api_unreachable`` → ``Application startup failed``。
            # 这台机器上没有 CC Switch 时（CI 就是这样），``127.0.0.1:4000``
            # 不可达，服务根本起不来，这条 e2e 连断言都到不了。
            #
            # ``PDT_PROVIDER_MODE`` 固定写 ``inherit``（不是别的值）：一个
            # 认不出来的值会退回 ``cc_switch.probe()``，而套件的 hermetic
            # 环境种的是一份空的 CC Switch 库，probe 于是报告一个其实并不
            # 存在的 fleet —— 那正是「问 :4000，然后被拒绝」的那条路。
            # 同一约定见 ``tests/e2e/test_instance_lock_e2e.py``（建子进程
            # env 的那一段）。
            "PDT_PROVIDER_MODE": "inherit",
            # 容量文件指向一个**不存在**的文件 =「没有配任何容量规则」。
            # 同一约定见 ``tests/e2e/test_instance_lock_e2e.py``。
            "PDT_PROVIDER_CAPACITY_FILE": str(tmp_path / "absent.yaml"),
            "PYTHONPATH": os.pathsep.join(
                [str(repo_root), str(backend_dir), inherited_path]
            ).rstrip(os.pathsep),
        }
    )

    # ``ANTHROPIC_BASE_URL`` 是继承来的**操作者**环境（指向操作者自己的
    # 端点）。这个进程不该用它，也不需要它；留着会让「服务连的是谁」
    # 取决于跑测试的那台机器。同一约定见
    # ``tests/e2e/test_instance_lock_e2e.py``。
    child_env.pop("ANTHROPIC_BASE_URL", None)

    # 这个子进程没有秘密可以接收，而一个**过期的**描述符编号绝不该被
    # 一个从不拥有它的进程关掉 —— 整个 ``PDT_SECRET_FD_*`` 家族都剔除。
    # 同一约定见 ``tests/e2e/test_instance_lock_e2e.py``。
    for key in [k for k in child_env if k.startswith("PDT_SECRET_FD_")]:
        child_env.pop(key, None)

    return child_env, f"http://127.0.0.1:{port}"


def wait_for_server(base_url: str, timeout: float = 60.0) -> None:
    """等那个真服务进程**开始回答**。

    「进程存在」不等于「服务起来了」：进程起来之后还要一会儿才在监听
    端口。所以判据是**它答了 200** —— 轮询 ``GET {base_url}/api/plans``
    （带 ``X-PDT-Request: 1``：``/api/*`` 全受请求守卫管，缺了这个头
    只会得到一个 403）。

    超时抛 :class:`RuntimeError`，并带上最后一次看到的答案 —— 一个
    死在半路的进程会说自己为什么死在那里。
    """
    import httpx

    deadline = time.monotonic() + timeout
    last: Any = None
    headers = {"X-PDT-Request": "1"}
    # ``trust_env=False``：这个请求只去本机，不该被 ``HTTP_PROXY`` 之类
    # 的环境变量送去某个代理（那会把「服务起来了没有」量成一个代理的
    # 502）。
    with httpx.Client(timeout=5.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/api/plans", headers=headers)
            except Exception as exc:  # noqa: BLE001 - 连接还没建立也是答案
                last = repr(exc)
            else:
                if response.status_code == 200:
                    return
                last = (response.status_code, response.text[:200])
            time.sleep(0.2)
    raise RuntimeError(
        f"{base_url}/api/plans did not answer 200 within {timeout}s; "
        f"last={last!r}"
    )


def assert_task_reset_took_effect(db_path: Any, plan_id: str, task_id: str) -> None:
    """断言这一行确实被重置成「还没跑过」的样子。

    断言的是**执行记账**那 5 个字段：状态回到 ``pending``、重试预算
    归零、上一轮留下的失败痕迹（原因 / 结束时间 / commit）全部清空。

    每条断言的消息里都带 ``status`` 这个词 —— 负控（把实现改回
    「保持原状态」）要靠 ``pytest.raises(match="status")`` 认出这条
    失败，消息里没有这个词，负控就变成「因为别的原因红了」。
    """
    row = read_row(db_path, plan_id, task_id)
    assert row is not None, (
        f"status 的重置必须有落点：plan {plan_id!r} 的 task {task_id!r} "
        f"在库里查无此行"
    )
    assert row.get("status") == "pending", (
        f"status 重置之后必须是 pending（重新排队）；实际是 "
        f"{row.get('status')!r}"
    )
    assert row.get("attempt") == 0, (
        f"status 重置必须把重试预算还回去：attempt 应为 0；实际是 "
        f"{row.get('attempt')!r}"
    )
    assert row.get("failure_reason") is None, (
        f"status 重置必须清掉上一轮的失败原因；实际是 "
        f"{row.get('failure_reason')!r}"
    )
    assert row.get("end_ts") is None, (
        f"status 重置必须清掉上一轮的结束时间；实际是 {row.get('end_ts')!r}"
    )
    assert row.get("commit_sha") is None, (
        f"status 重置必须清掉上一轮的 commit；实际是 {row.get('commit_sha')!r}"
    )
