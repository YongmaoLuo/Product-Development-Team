"""端到端：确定性路径零 LLM ＋ 主题外删除必有归档（真服务 + 真 HTTP 入口）。

背景
----
A4 之后剩下的最后一条「不烧 token 的端到端链路」是这条：**确定性路径**
（``api_test`` / ``full_ci`` / ``e2e``）由 runner 判定，一次 LLM 会话都不开；
**主题外删除**（一条验证点声明的验收标准不在这份 PRD 的判据表里）先归档、
后移除，移除集每轮从审计文件重建。

这两条各自都有钉子（任务 9 的管线顺序、任务 16 的确定性分派、任务 21 的
归因），但只有**真实链路**能同时观察到它们：真的服务、真的 HTTP 入口、
真的状态库、真的审计文件。

真实的部分
----------
* **被测服务** —— 一个真的 HTTP 服务：独立进程、真的端口、真的请求。
* **后端** —— ``python -m backend.server`` 真的起进程；
  ``POST /api/verification/{plan_id}/start``（带 ``X-PDT-Request: 1``）
  真的走 HTTP 入口；轮次真的在服务端跑完并落盘。
* **状态** —— 真的 SQLite（``plan_verification`` / ``plan_routing``）、真的
  plan 目录（``verification_plan.json``、``vp_removal_audit.jsonl``、
  ``llm_sessions.jsonl``、``logs/vp_attempts/*.log``）。
* **判定** —— ``VerificationExecutor``（真的类）、主题判定的 fail-open
  路径（``llm_batch=None``）、``DETERMINISTIC_RUNNERS`` 的确定性分派
  （真的 runner 跑真的请求 / 真的命令）。

替身化的部分
------------
* **第一轮的 runner 是替身** —— 它只把这一轮的裁决落成 FAILED（红灯），
  好让第二轮（真 HTTP 入口 ＋ 真确定性分派）必然重跑这些验证点。
  「确定性路径零 LLM」的证据**全部**来自第二轮，不来自这个替身。
* **模型不可用**是被测前提的一部分：``PDT_LLM_MODE=scripted`` 且脚本目录
  里没有登记任何应答（``llm_batch=None`` 的 fail-open 路径要的就是这个）。
* 服务与门禁脚本是测试自己写的最小实现（一个 JSON 接口 ＋ 两个退出码 0
  的脚本），但它们是**真的进程 / 真的命令**。

为什么服务必须比源码「新」
--------------------------
服务新鲜度预检（``service_freshness``）拿监听进程的启动时刻和最新的源码
mtime 比：进程比源码旧 ⇒ 判「要重启」⇒ 派重启代理 —— 那条路要 LLM，
scripted 模式下必然失败 ⇒ 整轮被挡住（一条验证点都跑不了）。所以被测
服务是**独立进程**，且在项目源码写完之后才起（``ps -o lstart`` 只有秒级
精度 ⇒ 至少隔 1.2 秒）。

为什么第一次进入是进程内的
--------------------------
移除判据（``_has_broken_reference``）要读验证点**自己**声明的
``related_prd_criteria``。但生产代码里唯一构造执行器的地方
（``verification_agent._convert_plan_to_executor_schema``）的字段清单
**不含**这一项 —— 转换后的验证点没有它，于是「声明的判据编号一条都对不
上」这条确定性矛盾在真 HTTP 入口里永远不成立。

这不是这份测试绕过去的事，是它**如实记下来**的事：第一次进入用进程内的
``VerificationExecutor``（喂**原始计划**，验证点带着
``related_prd_criteria``）把归档与移除做出来；第二次进入用真 HTTP 入口，
证明移除**跨轮次**仍然生效、且这一轮的四条确定性验证点真的走了确定性
派发。转换器少带一个字段这件事，属于生产代码的缺口 —— 记在验收报告里。

TDD contract —— 4 cases
------------------------
1. ``test_topic_outside_vp_archived_and_removed`` —— 主题外的那条被归档
   （审计文件恰好一行）且不再出现在活动集合里。
2. ``test_deterministic_vps_zero_llm_sessions`` —— 四条确定性验证点跑完，
   会话账本零行；非空洞的证据是**路由**：``api_test`` / ``full_ci`` 走执
   行器自己的本地路径（轮次日志里那条执行事件，压根不进子代理），
   ``e2e`` 走子代理但被分派表判为确定性（attempt 日志里路由是
   ``verification_ci_runner``，不是 ``llm``）。
3. ``test_orphan_path_untouched`` —— 孤儿裁决走既有隔离，且不进移除集。
4. ``test_removal_survives_second_entry`` —— 第二次进入（真 HTTP 入口）
   之后移除仍然生效：不复活、不重复归档、不进报告。

边界条件
--------
* ``llm_batch`` 传 ``None`` —— 架构定义的 fail-open 路径，不是 mock。
* **不引入任何「抑制 LLM 会话」的开关**：零会话是跑出来的，不是关出来的。
* 夹具：``api_test``×2（VP-001/VP-002）、``full_ci``（VP-003）、
  ``e2e``（VP-004）、主题外 1 条（VP-900）、孤儿裁决 1 条（VP-777）。
* 服务、后端进程、读日志的线程都在 ``finally`` 里收干净。
* 失败时打印审计文件、会话账本、attempt 日志、后端日志的**路径与内容**。

测试命令::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/e2e/verification/test_deterministic_plan_flow.py -v
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest

# ``backend/tests/e2e/verification/`` → ``backend/``。``backend/`` 进
# ``sys.path``，``import verification.*``（生产包）才成立。
BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (str(BACKEND_DIR), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.execution_repository import (  # noqa: E402
    ExecutionRepository,
)
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)
from verification.vp_removal_audit import (  # noqa: E402
    AUDIT_FILENAME,
    load_removed_vp_ids,
)
from verification_executor import VerificationExecutor  # noqa: E402


#: 整个文件带 ``e2e`` 标记：它起真的进程、真的服务、真的 HTTP 入口。
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本测试用的计划 id（与夹具目录里的 id 区分开 —— 夹具本身是只读的）。
PLAN_ID = "e2e-deterministic-plan-flow"

#: 主题外的那条：它声明的 ``AC-NOT-IN-PRD`` 不在本计划的判据表里。
OFF_TOPIC_VP = "VP-900"

#: 孤儿裁决：计划**没有定义**这个 id（上一轮遗留的裁决）。
ORPHAN_VP = "VP-777"

#: 四条确定性验证点：两条 api_test、一条 full_ci、一条 e2e。
DETERMINISTIC_VPS = ("VP-001", "VP-002", "VP-003", "VP-004")

#: 每条验证点「没开 LLM 会话」的**可核对证据**。
#:
#: 生产代码里有两条确定性的路，证据落在不同的地方：
#:
#: * ``api_test`` / ``full_ci`` —— 执行器**自己**跑（``verification_agent``
#:   的本地路径：真发请求、真跑命令），证据是轮次日志里那条执行事件
#:   （``api_test_executed`` / ``full_ci_executed``）。它们压根不进子代理，
#:   也就没有会话可开。
#: * ``e2e`` —— 走子代理，但分派表判它是确定性的（``verification_ci_runner``），
#:   证据是 attempt 日志里那条 ``deterministic_verdict``：**路由是 runner，
#:   不是 ``llm``**。
EXPECTED_EVIDENCE: Dict[str, Dict[str, str]] = {
    "VP-001": {"kind": "round_event", "type": "api_test_executed", "status": "PASSED"},
    "VP-002": {"kind": "round_event", "type": "api_test_executed", "status": "PASSED"},
    "VP-003": {"kind": "round_event", "type": "full_ci_executed", "status": "PASSED"},
    "VP-004": {"kind": "attempt_log", "route": "verification_ci_runner"},
}

#: 本计划的判据表 —— 编号式（``id`` ＋ 正文）。主题判定拿它当「这个计划
#: 到底要什么」的权威；一条验证点声明的编号在这里不存在，就是一条可核对
#: 的矛盾（``_has_broken_reference``）。
CRITERIA: List[Dict[str, str]] = [
    {"id": "AC-001", "text": "条目列表接口返回 200 且响应体里有 id"},
    {"id": "AC-002", "text": "创建条目接口返回 201 且至少写入一条记录"},
    {"id": "AC-003", "text": "整仓门禁入口退出码为 0"},
    {"id": "AC-004", "text": "端到端链路从列表页走到结算页"},
]

#: 夹具计划（任务 7 的交付物）。
FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures" / "verification"

#: 会话账本（``plans/<id>/llm_sessions.jsonl``）。
LEDGER_FILENAME = "llm_sessions.jsonl"

#: 验证报告（``plans/<id>/verification_report.json``）。
REPORT_FILENAME = "verification_report.json"

#: 跑后端用的解释器：优先仓库自己的 venv（CI 的每一条 lane 都用它）。
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: 一轮的预算（秒）。超时即失败：这份测试的价值在于它当场知道链路断了，
#: 而不是十分钟后有人想起来看一眼。
ROUND_BUDGET_SECONDS = 180

#: 后端进程起起来的预算（秒）。后端启动要做的事不少（迁移、provider
#: 初始化、通知器…），机器忙的时候要几十秒 —— 预算给够，别让「机器慢」
#: 变成「链路断了」。
SERVER_BOOT_BUDGET_SECONDS = 120

#: 服务比源码「新」所需的时间（``ps -o lstart`` 只有秒级精度 ⇒ > 1 秒）。
SERVICE_FRESHNESS_MARGIN_SECONDS = 1.2


# ---------------------------------------------------------------------------
# 被测服务：一个真的 HTTP 服务（独立进程）
# ---------------------------------------------------------------------------

#: 被测服务的源码。它被写进临时目录、真的起一个进程、真的应答请求。
#: 端口由内核给（``bind(..., 0)``）⇒ 不会撞上任何人的端口。
SERVICE_SOURCE = '''"""被测服务 —— 一个最小的 JSON 接口（测试自己写的，跑的是真的进程）。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/items":
            self._send(200, {"id": "item-001", "items": [{"id": "item-001"}]})
        else:
            self._send(200, {"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._send(201, {"items": [{"id": "item-001"}]})

    def log_message(self, *args):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(httpd.server_address[1], flush=True)
httpd.serve_forever()
'''

#: 两条门禁脚本 —— 真的命令、真的退出码 0。整仓门禁与 e2e 都靠退出码判定。
CI_SCRIPT_SOURCE = 'print("ci gate ok")\n'
E2E_SCRIPT_SOURCE = 'print("e2e ok")\n'


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """一个没人占的端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 60.0) -> bool:
    """等端口开始应答（TCP 接受连接 = 过了 bind/listen）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _http(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Tuple[int, str]:
    """一次真的 HTTP 请求。

    ``X-PDT-Request: 1`` 是回环入口的通行证（``request_guard``）：没有它，
    请求会被当成外部请求挡下来。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"X-PDT-Request": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


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


def _ledger_lines(plan_dir: Path) -> List[str]:
    """会话账本的行数（文件不在 ⇒ 零行）。"""
    path = plan_dir / LEDGER_FILENAME
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _attempt_events(plan_dir: Path, vp_id: str) -> List[Dict[str, Any]]:
    """一条验证点的 attempt 日志里的事件（JSON 行）。

    日志落在 ``logs/vp_attempts/vp_attempt_<VP>_<ts>.log``（每条验证点一次
    尝试一个文件）。这里用 ``rglob`` 找 —— 落盘位置是生产代码的事，这份
    测试要钉的是**事件**，不是路径。
    """
    events: List[Dict[str, Any]] = []
    for path in sorted(plan_dir.rglob(f"vp_attempt_{vp_id}_*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def _round_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """轮次日志里的事件（``logs/verification_<round>_<ts>.log``）。

    两种行形状都在这儿归一：

    * ``{"event": ..., "data": {...}}`` —— 轮次级的（``round_start``）；
    * ``{"verification_point_id": ..., "event_type": ..., "data": {...}}``
      —— 验证点级的（``api_test_executed`` / ``vp_start`` / …）。

    归一之后每条事件都是 ``{"vp_id": ..., "type": ..., "data": {...}}``。
    """
    events: List[Dict[str, Any]] = []
    for path in sorted(plan_dir.glob("logs/verification_*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if "event_type" in payload:
                events.append(
                    {
                        "vp_id": str(payload.get("verification_point_id") or ""),
                        "type": str(payload.get("event_type")),
                        "data": payload.get("data") or {},
                    }
                )
            elif "event" in payload:
                events.append(
                    {
                        "vp_id": str(payload.get("verification_point_id") or ""),
                        "type": str(payload.get("event")),
                        "data": payload.get("data") or {},
                    }
                )
    return events


def _db_summary(db_path: Path) -> Dict[str, Any]:
    """状态库这一行（新的连接 —— 读的是**落盘**的状态）。"""
    conn = open_db(db_path)
    try:
        return VerificationRepository(conn).summary(PLAN_ID) or {}
    finally:
        conn.close()


def _verdict_ids(summary: Dict[str, Any]) -> List[str]:
    """状态库里记着的裁决 id（按落盘顺序）。"""
    raw = summary.get("verdicts") or []
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    out: List[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("vp_id"):
            out.append(str(item["vp_id"]))
    return out


def _verdict_status(summary: Dict[str, Any], vp_id: str) -> Optional[str]:
    """某条验证点在状态库里的**最后一条**裁决状态。"""
    raw = summary.get("verdicts") or []
    status: Optional[str] = None
    items = raw.items() if isinstance(raw, dict) else enumerate(raw)
    for _key, item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("vp_id") or item.get("id") or "") != vp_id:
            continue
        status = str(item.get("status") or "") or None
    return status


def _projection_ids(summary: Dict[str, Any], plan_dir: Path) -> Set[str]:
    """报告投影里出现的验证点 id。

    投影有两处落法：状态库的 ``execution_results``（服务端那轮写的）和
    报告文件里的 ``verification_results``（Phase 3 的兜底报告）。读得到
    哪个用哪个 —— 这份测试要钉的是「投影里**没有**它」。
    """
    raw = summary.get("execution_results") or {}
    ids: Set[str] = set()
    if isinstance(raw, dict):
        for item in raw.get("verification_results") or raw.get("execution_results") or []:
            if isinstance(item, dict):
                vp_id = str(item.get("id") or item.get("vp_id") or "")
                if vp_id:
                    ids.add(vp_id)
    if ids:
        return ids
    report = plan_dir / REPORT_FILENAME
    if report.is_file():
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except ValueError:
            payload = {}
        for item in (payload.get("verification_results") or []):
            if isinstance(item, dict):
                vp_id = str(item.get("id") or item.get("vp_id") or "")
                if vp_id:
                    ids.add(vp_id)
    return ids


def _diagnostics(workspace: "Workspace") -> str:
    """失败时给人看的现场：每个产物的路径 ＋ 内容尾部。"""
    blocks: List[str] = []
    targets = (
        ("审计文件", workspace.plan_dir / AUDIT_FILENAME),
        ("会话账本", workspace.plan_dir / LEDGER_FILENAME),
        ("验证报告", workspace.plan_dir / REPORT_FILENAME),
        ("attempt 日志目录", workspace.plan_dir / "logs" / "vp_attempts"),
        ("后端日志", workspace.server_log),
        ("被测服务日志", workspace.service_log),
        ("计划文件", workspace.plan_dir / "verification_plan.json"),
    )
    for label, path in targets:
        blocks.append(f"--- {label}: {path}")
        if path.is_dir():
            for item in sorted(path.glob("*")):
                blocks.append(f"    {item.name} ({item.stat().st_size} bytes)")
        elif path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text[-4000:].splitlines():
                blocks.append(f"    {line}")
        else:
            blocks.append("    (不存在)")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    """一次端到端跑批的工作区。"""

    root: Path
    plans_dir: Path
    plan_dir: Path
    project_dir: Path
    db_path: Path
    script_dir: Path
    service_script: Path
    server_log: Path
    service_log: Path


@dataclass
class Chain:
    """两次进入跑完之后的状态。"""

    workspace: Workspace
    plan: Dict[str, Any]
    base_url: str
    server_port: int
    executor: VerificationExecutor
    first_entry_ids: List[str] = field(default_factory=list)
    start_status: int = 0
    start_body: str = ""


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    """工作区：计划目录、项目目录、状态库、脚本目录、日志文件。

    用 ``tmp_path_factory`` 而不是 ``tempfile.mkdtemp``：失败信息里会印出
    这些产物的路径，pytest 的临时目录在整个会话里都留着 ⇒ 印出来的路径
    真的点得开。
    """
    root = tmp_path_factory.mktemp("deterministic-plan-flow")
    plans_dir = root / "plans"
    plan_dir = plans_dir / PLAN_ID
    project_dir = plan_dir / "project"
    (project_dir / "scripts").mkdir(parents=True)
    script_dir = root / "llm-script"
    script_dir.mkdir()

    # 项目源码 —— 整仓门禁与 e2e 的入口脚本（真的命令、真的退出码）。
    (project_dir / "scripts" / "ci_local.py").write_text(
        CI_SCRIPT_SOURCE, encoding="utf-8"
    )
    (project_dir / "scripts" / "e2e_local.py").write_text(
        E2E_SCRIPT_SOURCE, encoding="utf-8"
    )

    # 被测服务的源码（写在工作区里，不在项目目录里 —— 项目目录的最新源码
    # mtime 是服务新鲜度判定的基准，服务自己的脚本不该搅进去）。
    service_script = root / "tiny_service.py"
    service_script.write_text(SERVICE_SOURCE, encoding="utf-8")

    # scripted 模式：脚本目录里**没有**登记任何应答 —— 模型不可用是被测
    # 前提的一部分（fail-open 路径要的就是这个）。
    (script_dir / "registry.json").write_text(
        json.dumps({"prompts": {}}), encoding="utf-8"
    )

    return Workspace(
        root=root,
        plans_dir=plans_dir,
        plan_dir=plan_dir,
        project_dir=project_dir,
        db_path=root / "state.db",
        script_dir=script_dir,
        service_script=service_script,
        server_log=root / "server.log",
        service_log=root / "service.log",
    )


@pytest.fixture(scope="module")
def api_service(workspace: Workspace) -> Iterator[str]:
    """被测服务：真的进程、真的端口。

    服务必须比项目源码「新」：新鲜度预检拿监听进程的启动时刻和最新的源码
    mtime 比，进程比源码旧就判「要重启」⇒ 派重启代理（要 LLM）⇒ scripted
    模式下必然失败 ⇒ 整轮被挡住。所以这里先睡过 1.1 秒再起进程。
    """
    time.sleep(SERVICE_FRESHNESS_MARGIN_SECONDS)
    log = open(workspace.service_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-u", str(workspace.service_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: List[str] = []
    reader = threading.Thread(target=_drain, args=(proc, lines, log), daemon=True)
    reader.start()
    try:
        port = _await_service_port(lines, proc)
        base_url = f"http://127.0.0.1:{port}"
        yield base_url
    finally:
        _stop_process(proc)
        reader.join(timeout=10)
        log.close()


def _drain(
    proc: subprocess.Popen, sink: List[str], log: Any
) -> None:
    """把子进程的输出收进 ``sink``，同时抄进日志文件。

    服务用它报出自己的端口（第一行），日志文件留给人看。
    """
    stream = proc.stdout
    if stream is None:
        return
    for line in stream:
        sink.append(line.rstrip())
        try:
            log.write(line)
            log.flush()
        except (ValueError, OSError):
            return


def _await_service_port(lines: List[str], proc: subprocess.Popen) -> int:
    """等被测服务报出自己的端口（它的第一行输出）。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"被测服务死了: {proc.returncode}\n" + "\n".join(lines[-40:])
            )
        for line in lines:
            stripped = line.strip()
            if stripped.isdigit():
                return int(stripped)
        time.sleep(0.1)
    raise AssertionError("被测服务没有报出自己的端口:\n" + "\n".join(lines[-40:]))


def _stop_process(proc: subprocess.Popen) -> None:
    """收干净一个子进程（terminate → 等 → kill）。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _build_plan(base_url: str) -> Dict[str, Any]:
    """本轮的计划：夹具的五条验证点 ＋ 一份编号式判据表。

    夹具（任务 7 的交付物）给的是「这个验证点验什么」；环境相关的东西
    （服务端口、解释器路径）在这里改写 —— 改的只有地址和入口命令，验证
    点的语义一个字没动。
    """
    points: List[Dict[str, Any]] = []
    wanted = ("VP-001", "VP-002", "VP-003", "VP-004", "VP-900")
    for fixture in ("vp-fixture-001", "vp-fixture-002"):
        payload = json.loads(
            (FIXTURES_DIR / fixture / "verification_plan.json").read_text(
                encoding="utf-8"
            )
        )
        for vp in payload["verification_points"]:
            if vp.get("id") in wanted:
                points.append(copy.deepcopy(vp))

    interpreter = shlex.quote(str(sys.executable))
    for vp in points:
        request = vp.get("request")
        if isinstance(request, dict) and isinstance(request.get("url"), str):
            request["url"] = re.sub(
                r"http://127\.0\.0\.1:\d+", base_url, request["url"]
            )
        if vp.get("id") == "VP-003":
            # 整仓门禁的入口：夹具里写的是 ``python``，这里换成真的解释器
            # （这台机器上不一定有 ``python`` 这个名字）。
            vp["ci_entry"] = f"{interpreter} scripts/ci_local.py"
        if vp.get("id") == "VP-004":
            # e2e 走的是确定性 CI runner（``DETERMINISTIC_RUNNERS``）⇒ 它要
            # 自己的入口；``target_url`` 指向被测服务。
            vp["ci_entry"] = f"{interpreter} scripts/e2e_local.py"
            vp["target_url"] = f"{base_url}/checkout"

    return {
        "plan_id": PLAN_ID,
        "verification_points": points,
        "criteria": [dict(criterion) for criterion in CRITERIA],
    }


def _seed_state(workspace: Workspace) -> None:
    """状态库：计划行、路由行、验证行 ＋ 两条起始裁决。

    两条起始裁决各有各的理由：

    * ``VP-900``（主题外那条）—— **上一轮**验过并通过了：那一轮 PRD 里
      还有 ``AC-NOT-IN-PRD``。这一轮 PRD 里没有它了 ⇒ 主题外 ⇒ 归档并
      移除。它有裁决还有个副作用：Phase 2 的关卡要求「它之前的一切都已
      终态成功」，一条**被移除**的验证点没有裁决 ⇒ 关卡永远不开。
    * ``VP-777``（孤儿裁决）—— 计划**没有定义**这个 id：走既有隔离路径
      （不进报告、不进任何桶），且**绝不**进移除集。
    """
    conn = open_db(workspace.db_path)
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            PLAN_ID, project_dir=str(workspace.project_dir)
        )
        RoutingRepository(conn).insert(PLAN_ID, "executing")
        repo = VerificationRepository(conn)
        repo.insert(PLAN_ID, verification_status="not_started", max_rounds=3)
        repo.append_verdict(
            PLAN_ID,
            {
                "vp_id": OFF_TOPIC_VP,
                "status": "PASSED",
                "reasons": ["上一轮已验证通过（那一轮 PRD 里还有这条判据）"],
                "evidence": {},
            },
        )
        repo.append_verdict(
            PLAN_ID,
            {"vp_id": ORPHAN_VP, "status": "FAILED", "reasons": ["boom"], "evidence": {}},
        )
        conn.commit()
    finally:
        conn.close()


async def _first_round_runner(vp_node: Dict[str, Any]) -> Dict[str, Any]:
    """第一轮的 runner —— 替身：只把这一轮落成 FAILED（红灯）。

    为什么是红灯而不是绿灯：第二轮（真 HTTP 入口 ＋ 真确定性分派）要
    必然重跑这些验证点，证据才落在**真实的**派发路径上。绿灯会被「定义
    没变 ⇒ 继承上一轮裁决」的续跑规则吃掉，第二轮就什么都不跑了。
    """
    return {
        "status": "FAILED",
        "reasons": ["第一轮：占位裁决（红灯）—— 第二轮重跑"],
        "evidence": {},
    }


def _run_first_entry(
    workspace: Workspace, plan: Dict[str, Any]
) -> Tuple[VerificationExecutor, List[str]]:
    """第一次进入：真的 ``VerificationExecutor``（进程内）。

    ``topic_relevance_llm_batch=None`` —— 架构定义的 fail-open 路径：没有
    模型可问，判定只能靠确定性层；模型不可用时，「声明的判据编号一条都
    对不上」就是一条**可核对**的矛盾。

    这条路径走的是**原始计划**（``verification_points``），所以验证点带着
    ``related_prd_criteria`` —— 移除判据要读的就是它。
    """
    conn = open_db(workspace.db_path)
    try:
        repo = VerificationRepository(conn)
        ran: List[str] = []

        async def _runner(vp_node: Dict[str, Any]) -> Dict[str, Any]:
            ran.append(str(vp_node.get("id")))
            return await _first_round_runner(vp_node)

        executor = VerificationExecutor(
            verification_plan=plan,
            plan_id=PLAN_ID,
            plan_dir=workspace.plan_dir,
            sub_agent_runner=_runner,
            verif_repo=repo,
            topic_relevance_llm_batch=None,
        )
        asyncio.run(executor.run(max_parallel=1))
        conn.commit()
        return executor, ran
    finally:
        conn.close()


def _start_server(workspace: Workspace) -> Tuple[subprocess.Popen, int]:
    """起真的后端进程（``python -m backend.server``）。

    环境里的 ``PDT_*`` 全部清掉再逐个写回去：环境里残留的别的 plan 目录
    / 状态库 / LLM 模式会把这轮带偏。
    """
    port = _free_port()
    env = {key: value for key, value in os.environ.items() if not key.startswith("PDT_")}
    env.update(
        {
            "PDT_PLANS_DIR": str(workspace.plans_dir),
            "PDT_STATE_DB_PATH": str(workspace.db_path),
            "PDT_PORT": str(port),
            "PDT_HOST": "127.0.0.1",
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(workspace.script_dir),
            "PDT_DEBUG_ROUTES": "1",
            # 后端启动时要问 CC Switch 要 provider 链；答不上来它就**拒绝
            # 启动**（这是有意的：没有 provider 知识的服务器不该悄悄起来）。
            # CC Switch 是这台机器上的环境事实，不是这条链路的依赖 ——
            # 钉住 inherit 模式（CI 上就是它），后端启动才是确定的。
            # 注意：这只摘掉 provider 这一项依赖，**模型**那一条路仍然是
            # 通的（scripted 模式下它会真的去问、真的报错 —— 负控靠它）。
            "PDT_PROVIDER_MODE": "inherit",
        }
    )
    log = open(workspace.server_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-u", "-m", "backend.server"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc._pdt_log = log  # type: ignore[attr-defined]  # noqa: SLF001
    if not _wait_for_port(port, timeout=SERVER_BOOT_BUDGET_SECONDS):
        _stop_process(proc)
        raise AssertionError(
            f"后端没能在 {SERVER_BOOT_BUDGET_SECONDS} 秒内监听 {port}"
            f"\n{_diagnostics(workspace)}"
        )
    return proc, port


def _run_second_entry(
    workspace: Workspace, server_port: int
) -> Tuple[int, str]:
    """第二次进入：真的 HTTP 入口（``POST /api/verification/{id}/start``）。

    ``auto_fix=False`` ⇒ 单轮语义（不自动串修复与下一轮）。轮次在服务端
    跑完 ⇒ 落盘（裁决进状态库、attempt 日志进 plan 目录、报告进 plan
    目录）。这一轮跑的是**真的**确定性分派：api_test 真发请求，full_ci
    与 e2e 真跑命令。
    """
    url = f"http://127.0.0.1:{server_port}/api/verification/{PLAN_ID}/start"
    status, body = _http("POST", url, {"auto_fix": False})
    if status != 200:
        raise AssertionError(
            f"HTTP 入口返回 {status}: {body}\n{_diagnostics(workspace)}"
        )
    _wait_for_round(workspace, server_port)
    return status, body


def _wait_for_round(workspace: Workspace, server_port: int) -> None:
    """等这一轮落盘。

    落盘的标志是报告文件（Phase 3 的兜底报告）：它写出来的时候，四条
    确定性验证点的裁决早已进了状态库，attempt 日志也早已落在 plan 目录
    里。再加一条状态库里的投影作为第二道标志（它在裁决之后写）。
    """
    deadline = time.monotonic() + ROUND_BUDGET_SECONDS
    report = workspace.plan_dir / REPORT_FILENAME
    while time.monotonic() < deadline:
        if report.is_file():
            summary = _db_summary(workspace.db_path)
            if summary.get("execution_results"):
                return
        try:
            status, _body = _http(
                "GET",
                f"http://127.0.0.1:{server_port}/api/verification/{PLAN_ID}/status",
                timeout=10,
            )
        except OSError:
            status = 0
        if status == 0:
            # 后端进程没了 —— 这一轮不会再有产物了。
            raise AssertionError(
                f"后端进程不在了\n{_diagnostics(workspace)}"
            )
        time.sleep(1.0)
    raise AssertionError(
        f"这一轮在 {ROUND_BUDGET_SECONDS} 秒内没落盘\n{_diagnostics(workspace)}"
    )


@pytest.fixture(scope="module")
def chain(
    workspace: Workspace, api_service: str
) -> Iterator[Chain]:
    """跑完整条链路：第一次进入（进程内）→ 第二次进入（真 HTTP 入口）。

    顺序是承重的：第一次进入负责**归档并移除**（移除集只有它自己重建得
    出来），第二次进入负责证明移除**跨轮次仍然生效**，并且在这一轮里让
    四条确定性验证点真的走一遍确定性派发。
    """
    plan = _build_plan(api_service)
    (workspace.plan_dir / "verification_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _seed_state(workspace)

    executor, first_entry_ids = _run_first_entry(workspace, plan)

    server: Optional[subprocess.Popen] = None
    try:
        server, server_port = _start_server(workspace)
        start_status, start_body = _run_second_entry(workspace, server_port)
        yield Chain(
            workspace=workspace,
            plan=plan,
            base_url=api_service,
            server_port=server_port,
            executor=executor,
            first_entry_ids=first_entry_ids,
            start_status=start_status,
            start_body=start_body,
        )
    finally:
        if server is not None:
            _stop_process(server)
            log = getattr(server, "_pdt_log", None)
            if log is not None:
                log.close()


# ---------------------------------------------------------------------------
# 1. 主题外的那条：归档了，且不再活动
# ---------------------------------------------------------------------------


def test_topic_outside_vp_archived_and_removed(chain: Chain):
    """主题外的那条先归档、再从活动集合里消失。

    审计文件里**恰好一行** —— 归档是移除的前提（写不进去就留着那条验证
    点：一次没有证据的移除是一次静悄悄的严格度损失）。移除集由审计文件
    重建，所以审计里有它、活动集合里就没有它。
    """
    workspace = chain.workspace
    rows = _audit_rows(workspace.plan_dir)

    assert len(rows) == 1, (
        f"审计文件应当恰好一行（归档了主题外的那条）\n{_diagnostics(workspace)}"
    )
    row = rows[0]
    assert row.get("vp_id") == OFF_TOPIC_VP, row
    assert row.get("component") == "verification_executor", row
    assert row.get("plan_id") == PLAN_ID, row
    assert row.get("decision"), row
    assert row.get("tier"), row

    # 移除集 = 审计文件里的那一条。
    assert load_removed_vp_ids(workspace.plan_dir) == {OFF_TOPIC_VP}

    # 活动集合里没有它了（第一轮的投影）。
    active = [
        str(vp.get("id"))
        for vp in chain.executor._build_items_from_plan()  # noqa: SLF001
    ]
    assert OFF_TOPIC_VP not in active, active

    # 第一轮它一次都没跑（判定在执行之前：主题外的东西一次都不该跑）。
    assert OFF_TOPIC_VP not in chain.first_entry_ids, chain.first_entry_ids


# ---------------------------------------------------------------------------
# 2. 确定性路径零 LLM 会话
# ---------------------------------------------------------------------------


def test_deterministic_vps_zero_llm_sessions(chain: Chain):
    """四条确定性验证点跑完，会话账本零行，而且**没走过 LLM 路径**。

    账本零行在 scripted 模式下**是**结构性的（会话账本只有真的
    ``ClaudeCodingTool`` 会写，scripted 模式用的是 ``ScriptedCodingTool``），
    所以它自己**不能**当证据 —— 真正非空洞的证据有三条：

    1. **每条验证点的路由** —— ``api_test`` / ``full_ci`` 走执行器自己的
       本地路径（轮次日志里那条执行事件，压根不进子代理）；``e2e`` 走子
       代理，但分派表判它是确定性的（attempt 日志里路由是
       ``verification_ci_runner``，不是 ``llm``）。
    2. **裁决全绿** —— scripted 模式下 LLM 路径是**死路**（没有登记任何
       应答 ⇒ 抛 ``ScriptedLLMError``）：走了 LLM 路径的验证点必然
       FAILED。四条都 PASSED ⇒ 都没走那条路。
    3. **子代理自报里没有 provider / model** —— 走过 LLM 的子代理会带上
       它用的是哪个 provider、哪个模型。

    负控：同一轮里 Phase 3 的兜底报告**真的**去问了模型（scripted 模式
    下报错）—— LLM 通路是活的，确定性验证点只是没碰它。
    """
    workspace = chain.workspace
    summary = _db_summary(workspace.db_path)
    round_events = _round_events(workspace.plan_dir)

    # 账本零行。
    assert _ledger_lines(workspace.plan_dir) == [], (
        f"确定性路径不该开任何 LLM 会话\n{_diagnostics(workspace)}"
    )

    for vp_id in DETERMINISTIC_VPS:
        status = _verdict_status(summary, vp_id)
        assert status == "PASSED", (
            f"{vp_id} 应当跑过并通过（实际 {status!r}）"
            f"\n{_diagnostics(workspace)}"
        )

        expectation = EXPECTED_EVIDENCE[vp_id]
        if expectation["kind"] == "round_event":
            matched = [
                event
                for event in round_events
                if event["vp_id"] == vp_id
                and event["type"] == expectation["type"]
            ]
            assert matched, (
                f"{vp_id} 没有 {expectation['type']} 事件 —— "
                f"它没走确定性路径\n{_diagnostics(workspace)}"
            )
            assert matched[-1]["data"].get("status") == expectation["status"], (
                f"{vp_id} 的 {expectation['type']} 不是通过: {matched[-1]}"
            )
        else:
            routes = [
                str(event.get("data", {}).get("route"))
                for event in _attempt_events(workspace.plan_dir, vp_id)
                if event.get("event") == "deterministic_verdict"
            ]
            assert routes == [expectation["route"]], (
                f"{vp_id} 应当走确定性 runner（{expectation['route']}），"
                f"实际 {routes!r}\n{_diagnostics(workspace)}"
            )
            assert "llm" not in routes, f"{vp_id} 走了 LLM 路径: {routes!r}"

    # 子代理的自报里没有 provider / model ⇒ 没有哪个验证点开过会话。
    for event in round_events:
        if event["type"] != "sub_agent_completed":
            continue
        data = event["data"]
        assert not data.get("provider"), (
            f"{event['vp_id']} 的子代理带着 provider（{data.get('provider')!r}）"
            f" ⇒ 它开过 LLM 会话\n{_diagnostics(workspace)}"
        )
        assert not data.get("chosen_model"), (
            f"{event['vp_id']} 的子代理带着模型（{data.get('chosen_model')!r}）"
            f" ⇒ 它开过 LLM 会话\n{_diagnostics(workspace)}"
        )

    # 负控：LLM 通路在同一轮里是活的（Phase 3 的兜底报告去问了模型）。
    server_log = (
        workspace.server_log.read_text(encoding="utf-8", errors="replace")
        if workspace.server_log.is_file()
        else ""
    )
    assert "scripted LLM mode" in server_log, (
        "负控失败：这一轮里 LLM 通路一次都没被走到 —— "
        "「零会话」就只是「模型没被叫过」，不是「确定性路径没开会话」"
        f"\n{_diagnostics(workspace)}"
    )


# ---------------------------------------------------------------------------
# 3. 孤儿路径原样
# ---------------------------------------------------------------------------


def test_orphan_path_untouched(chain: Chain):
    """孤儿裁决走既有隔离，且**绝不**进移除集。

    两种「孤儿」在这里：

    * 计划**没有定义**的 id（``VP-777``，上一轮遗留的裁决）—— 走既有的
      隔离路径：不进报告、不进任何桶；
    * 移除的语义是「这条验证点不属于本计划」，孤儿的语义是「这条裁决没有
      判据支撑」—— 不是一回事。

    两条都不许变成「被移除」。
    """
    workspace = chain.workspace
    summary = _db_summary(workspace.db_path)

    # 移除集里只有主题外的那条。
    rows = _audit_rows(workspace.plan_dir)
    assert [row.get("vp_id") for row in rows] == [OFF_TOPIC_VP], rows
    assert load_removed_vp_ids(workspace.plan_dir) == {OFF_TOPIC_VP}

    # 孤儿裁决还在状态库里（它没被删），但它不在报告投影里。
    assert ORPHAN_VP in _verdict_ids(summary), _verdict_ids(summary)
    projection = _projection_ids(summary, workspace.plan_dir)
    assert ORPHAN_VP not in projection, projection

    # 它也不在任何桶里。
    assert ORPHAN_VP not in chain.executor.failed_vps, chain.executor.failed_vps
    assert ORPHAN_VP not in chain.executor.completed_vps, chain.executor.completed_vps
    assert ORPHAN_VP not in chain.executor.skipped_vps, chain.executor.skipped_vps


# ---------------------------------------------------------------------------
# 4. 第二次进入：移除仍然生效
# ---------------------------------------------------------------------------


def test_removal_survives_second_entry(chain: Chain):
    """第二次进入（真 HTTP 入口）之后，移除仍然生效。

    * **不复活** —— 计划文件里那条验证点还在，服务端那一轮却没有为它
      产出任何裁决、没有任何 attempt 日志 ⇒ 它被移除了。
    * **不重复归档** —— 审计文件仍然恰好一行（移除集每轮从审计文件重建，
      已经移除过的那条不再进判定，也就不会被归档第二次）。
    * **不进报告** —— 服务端那一轮的投影里没有它。
    """
    workspace = chain.workspace
    summary = _db_summary(workspace.db_path)

    # 计划文件里它还在（移除的是「活动集合」，不是「计划」）。
    plan_ids = {
        str(vp.get("id")) for vp in chain.plan.get("verification_points", [])
    }
    assert OFF_TOPIC_VP in plan_ids, plan_ids

    # 服务端那一轮没有为它跑过任何东西（轮次日志里没有它的事件，attempt
    # 日志里也没有它的文件）。
    touched = [
        event
        for event in _round_events(workspace.plan_dir)
        if event["vp_id"] == OFF_TOPIC_VP
    ]
    assert touched == [], (
        f"第二轮不该碰被移除的那条验证点: {touched!r}"
        f"\n{_diagnostics(workspace)}"
    )
    assert _attempt_events(workspace.plan_dir, OFF_TOPIC_VP) == []

    # 审计仍然恰好一行（没有重复归档）。
    rows = _audit_rows(workspace.plan_dir)
    assert len(rows) == 1, (
        f"第二轮不该重复归档（审计文件是只增的）\n{_diagnostics(workspace)}"
    )
    assert rows[0].get("vp_id") == OFF_TOPIC_VP, rows

    # 报告投影里没有它。
    projection = _projection_ids(summary, workspace.plan_dir)
    assert OFF_TOPIC_VP not in projection, projection

    # 而四条确定性验证点都在投影里（这一轮它们真的跑了）。
    for vp_id in DETERMINISTIC_VPS:
        assert vp_id in projection, (
            f"{vp_id} 应当在第二轮的投影里\n{_diagnostics(workspace)}"
        )