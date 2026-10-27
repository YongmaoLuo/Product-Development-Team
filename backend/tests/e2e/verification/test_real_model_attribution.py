"""真实模型 lane 的归因证据（2026-10-27）。

背景
----
台账的归因坐标（``vp_id`` / ``round``）是**别处**证明的：任务 21 用一个
记账替身把每一行对上号，钉住「谁的那次会话写着谁的号」。但那份证据有一个
它自己看不见的洞 —— **替身不会真的发请求**。

替身记账，于是「链路绿」和「模型真的被调过」成了同一件事的两副面孔：台账
里有一行、号也对、测试也绿 —— 但没有一个字节离开过这台机器。夜里那一批
``real_model`` 长期报 PASS，真调用其实一次都没发生：根因在
``backend/_pytest_hermetic_env.py`` —— 它把 ``ANTHROPIC_BASE_URL`` 摘掉、
把 ``HOME`` 换掉，真调用的子进程因此拿不到端点，用例被 **skip** 掉，而
skip 不让退出码变红。一个「什么都没发生」的批次，和一个「什么都发生了」
的批次，在 summary 里长得一模一样。

这份文件补的就是那一段：**真的调一次模型，并且从代理侧证明请求真的发出
去了**。归因坐标从来不只是「该写在哪」的问题 —— 它是「写下来的那一行背后，
是不是真的有一次 provider 往返」的问题。

真实的东西
----------
* **真的 CLI** —— ``claude`` 子进程真的被起一次，命令与生产路径同一条
  （``--verbose --output-format stream-json --permission-mode
  bypassPermissions``，提示词走 stdin）。
* **真的 provider** —— 请求真的发到真的 provider 端点（不是 mock，不是
  回放脚本）。
* **真的代理** —— 中间那个本地代理是真的 HTTP 服务器：它收下 CLI 的请求、
  转发给 provider、并把**上游真的答了**的请求**数**下来。计数为 0 就是
  「没发出去」。
* **真的台账** —— 那一行是 ``usage_registry`` 真的写进
  ``plans/<plan_id>/llm_sessions.jsonl`` 的那一行。

替身化的部分
------------
只有**指针**，没有替代品：

* ``ANTHROPIC_BASE_URL`` 指到本地代理（代理再转发给真 provider）。
* ``ANTHROPIC_AUTH_TOKEN`` 指到代理自己发的一把一次性令牌 —— **不是**真
  provider 的钥匙。钥匙只活在代理的进程里：子进程拿到的凭证没有资格直接
  打通 provider。
* ``PDT_PLANS_DIR`` 指到临时目录，不碰操作员的 ``plans/``。
* ``PDT_PROVIDER_MODE=inherit`` —— 端点由上面那个指针决定，不走 CC Switch
  的场景路由（那条路要一张 provider 行表，而这张表的行由机器决定，不该由
  这份测试决定）。
* ``CLAUDE_SANDBOX_POLICY`` 指到一份「默认不沙箱」的策略文件 —— 有些机器
  上的 ``claude`` 是一层本地启动器，它在净化过的 ``HOME`` 下找不到策略文件
  时会走一条坏掉的错误分支而退出 1。不进沙箱也就是净化过的 ``HOME`` 下
  **本来**就会得到的行为，不是新放宽。
* ``CLAUDE_SETTINGS_PATH`` 指到一份空设置文件 —— 免得继承调用方（一个正在
  跑的任务子会话）自己的钩子与设置。这份文件测的是归因，不是别人的钩子。

TDD contract — 3 cases
-----------------------
1. ``test_skips_without_env`` —— 没设环境变量 → 这道门把这批判定为「跳过」。
   门是真门：把环境变量拿走，门必须真的跳过；把它放回去，门必须放行。
2. ``test_row_carries_vp_and_round`` —— 台账那一行的 ``vp_id`` /
   ``round`` 都不是空的，且就是这一条验证点与这一轮。
3. ``test_provider_was_actually_called`` —— 代理侧数到的请求数 > 0。

边界条件
--------
* 未设置 ``PDT_E2E_REAL_MODEL`` → 整份文件跳过（不跑一个「看起来绿」的
  空跑）。
* 代理请求计数为 0 → **失败**。「没有回归」不是「模型被调过」，一份把
  no-regression 当成功的证据比没有证据更坏。
* 不进 PR 分片，只在夜间 lane：文件带 ``e2e`` 与 ``real_model`` 标记 ——
  PR 的 root 分片按 ``not e2e`` 把它整个排除，``e2e-on-demand`` 分片在
  ``CI=true`` 下按 ``real_model`` 把它跳过，只有本机的夜间 lane（``-m
  real_model``）会真的跑它。

跑法::

    PDT_E2E_REAL_MODEL=1 \\
    PDT_E2E_REAL_MODEL_BASE_URL=<provider 端点> \\
    PDT_E2E_REAL_MODEL_API_KEY=<provider 钥匙> \\
    pytest backend/tests/e2e/verification/test_real_model_attribution.py -v

provider 端点为什么必须显式给：套件的环境净化（``_pytest_hermetic_env``）
会把进程里的 ``ANTHROPIC_BASE_URL`` 摘掉，所以环境里那份端点在这一层已经
不存在了 —— 留着它只会让人以为「配过了」。钥匙不用显式给：净化不摘
``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_API_KEY``。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ``backend/tests/e2e/verification/`` → ``backend/``。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import usage_registry  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from verification_executor import VerificationExecutor  # noqa: E402
from verification_subagent import VerificationSubAgent  # noqa: E402


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 这道门的名字。没设它 = 这份文件整批跳过。
_REAL_MODEL_ENV = "PDT_E2E_REAL_MODEL"

#: 真 provider 的端点。必须显式给：套件的环境净化把进程里的
#: ``ANTHROPIC_BASE_URL`` 摘掉了，环境里那份在这一层已经不存在。
_UPSTREAM_BASE_URL_ENV = "PDT_E2E_REAL_MODEL_BASE_URL"

#: 真 provider 的钥匙。不给就退回进程里的 ``ANTHROPIC_AUTH_TOKEN`` /
#: ``ANTHROPIC_API_KEY``（这两个没有被净化摘掉）。
_UPSTREAM_KEY_ENV = "PDT_E2E_REAL_MODEL_API_KEY"

#: 真 provider 的模型名。不给就用环境里那份。
_UPSTREAM_MODEL_ENV = "PDT_E2E_REAL_MODEL_MODEL"

#: 这个计划的 id —— 台账按 plan id 分目录。
PLAN_ID = "real-model-attribution"

#: 这一轮的那条验证点。
VP_ID = "VP-1"

#: 这一轮的轮次号。
ROUND_NO = 1

#: 代理转发给上游时的超时（秒）。一次 provider 往返不该比这更慢。
_UPSTREAM_TIMEOUT_SECONDS = 180

#: 转发时**不**照抄的请求头：逐跳头（连接级的）由代理自己管，认证头由
#: 代理换成真 provider 的钥匙。
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "accept-encoding",
        "authorization",
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-api-key",
    }
)


def _is_model_call(path: str) -> bool:
    """这个请求是一次**模型调用**吗？

    Anthropic 兼容端点的模型调用是 ``/v1/messages``。CLI 还会去问别的
    端点（列模型、数 token），那些也是「上游答了」，但它们不是一次模型
    调用 —— 把它们算进计数，一个「从没调过模型」的批次也能凑出计数 > 0。
    """
    return urllib.parse.urlparse(path).path.rstrip("/").endswith("/messages")


#: 整个文件带 ``e2e`` 与 ``real_model`` 标记，并被那道门挡着。
#:
#: ``e2e`` 让 PR 的 root 分片（``-m "not e2e and ..."``）整个不收它；
#: ``real_model`` 让 ``e2e-on-demand`` 分片在 ``CI=true`` 下跳过它（见
#: ``backend/tests/conftest.py``）；两道门都过了的只有本机夜间 lane。
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.real_model,
    pytest.mark.skipif(
        not os.environ.get(_REAL_MODEL_ENV),
        reason=(
            f"opt-in real-model lane: set {_REAL_MODEL_ENV}=1 to run. "
            "This lane really calls a provider and costs real tokens."
        ),
    ),
]


# ---------------------------------------------------------------------------
# 门
# ---------------------------------------------------------------------------


def _require_real_model() -> None:
    """这道门：没有环境变量就没有真模型 lane。

    文件顶上的 ``skipif`` 与它是同一道门的两半：``skipif`` 在**收集期**
    判定（整批跳过），这个函数在**运行期**再判一次（所以它可测）。门要是
    只在收集期生效，「没设变量就跳过」这条就没人能验证了。
    """
    if not os.environ.get(_REAL_MODEL_ENV):
        pytest.skip(
            f"opt-in real-model lane: set {_REAL_MODEL_ENV}=1 to run "
            f"(a real provider call costs real tokens)."
        )


# ---------------------------------------------------------------------------
# 代理：一个真的本地 HTTP 服务器，数请求
# ---------------------------------------------------------------------------


class _ProviderProxy:
    """真的本地代理：收下 CLI 的请求，转发给真 provider，并把请求数下来。

    为什么自己起一个，而不是去读 CC Switch 的 ``proxy_request_logs``：
    那张表只在**请求真的经过 CC Switch 的本地代理**时才有行。机器上把
    provider 直连（不经 CC Switch）是常见配置 —— 那种配置下「表里没行」
    和「没发请求」长得一模一样，而这份测试的全部意义就是把这两种情况
    分开。自己站在中间数，计数就是「有多少个请求真的离开了本机」。

    它数的是**上游真的答了一次模型调用**的请求，不是「代理收到了」的请求。
    这两者差一步，但差的那一步正好是这份测试的全部意义：代理收到了请求、
    转发却失败了（端点写错、钥匙不对、连接被拒），provider 一个字节都没
    答 —— 那不是「模型被调过」，那是一次没成的调用。数「收到了」会把这种
    失败记成成功，于是又是一次假绿。

    只数**模型调用**那条路径（``/v1/messages``）：CLI 还会去问些别的端点
    （列模型、数 token），那些也是「上游答了」，但它们不是一次模型调用 ——
    把它们算进来，一个「从没调过模型」的批次也能凑出计数 > 0。

    凭证分两把：给 CLI 的那把是这里现发的一次性令牌，真 provider 的钥匙
    只留在代理的进程里 —— 子进程拿到的凭证没有资格直接打通 provider。
    """

    def __init__(self, upstream_base_url: str, upstream_api_key: str) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self.upstream_api_key = upstream_api_key
        #: 给 CLI 的那把 —— 现发的一次性令牌。
        self._client_token = f"pdt-e2e-{secrets.token_hex(16)}"
        self._lock = threading.Lock()
        #: 上游真的答了一次模型调用的请求。
        self._requests: List[Dict[str, Any]] = []
        #: 代理收到过的请求（不论上游答没答）—— 只用来诊断。
        self._attempts: List[Dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        # 处理器要回到代理身上拿上游地址与钥匙。
        self._server.provider_proxy = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="pdt-e2e-provider-proxy",
            daemon=True,
        )

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    # -- 对外 --------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """CLI 该指向的地址（回环 + 临时端口）。"""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def client_token(self) -> str:
        """给 CLI 的那把一次性令牌。"""
        return self._client_token

    def requests(self) -> List[Dict[str, Any]]:
        """上游真的答了的每一个请求（方法、路径、请求体长度）。"""
        with self._lock:
            return list(self._requests)

    def attempts(self) -> List[Dict[str, Any]]:
        """代理收到过的每一个请求（不论上游答没答）—— 只用来诊断。"""
        with self._lock:
            return list(self._attempts)

    def request_count(self) -> int:
        return len(self.requests())

    # -- 内部 --------------------------------------------------------------

    def record_attempt(
        self, method: str, path: str, body_len: int, outcome: str = ""
    ) -> None:
        """代理收到过的每一次请求（不论上游答没答）—— 只用来诊断。"""
        with self._lock:
            self._attempts.append(
                {
                    "method": method,
                    "path": path,
                    "body_len": body_len,
                    "outcome": outcome,
                }
            )

    def record_served(self, method: str, path: str, body_len: int) -> None:
        """上游真的答了一次**模型调用** —— 这才是一次「模型被调过」。"""
        if not _is_model_call(path):
            return
        with self._lock:
            self._requests.append(
                {"method": method, "path": path, "body_len": body_len}
            )

    def upstream_url(self, path: str) -> str:
        return f"{self._upstream_base_url}{path}"


class _ProxyHandler(BaseHTTPRequestHandler):
    """把请求转发给上游，把上游的答复原样送回来。"""

    protocol_version = "HTTP/1.1"

    @property
    def _proxy(self) -> _ProviderProxy:
        return self.server.provider_proxy  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
        self._forward()

    def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
        self._forward()

    def _read_body(self) -> bytes:
        """读请求体 —— ``Content-Length`` 与分块两种形状都认。"""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _forward(self) -> None:
        body = self._read_body()

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS
        }
        upstream = urllib.parse.urlparse(self._proxy.upstream_url(""))
        headers["Host"] = upstream.netloc
        # 认证头换成真 provider 的钥匙：CLI 那一份是给代理用的，代理自己
        # 才是这一段的对端。
        headers["Authorization"] = f"Bearer {self._proxy.upstream_api_key}"
        headers["x-api-key"] = self._proxy.upstream_api_key
        headers["Accept-Encoding"] = "identity"

        request = urllib.request.Request(
            self._proxy.upstream_url(self.path),
            data=body or None,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(
                request, timeout=_UPSTREAM_TIMEOUT_SECONDS
            )
        except urllib.error.HTTPError as exc:
            reason = f"upstream answered {exc.code}: {exc.read()!r}"
            self._proxy.record_attempt(self.command, self.path, len(body), reason)
            self._reply_failure(reason)
            return
        except Exception as exc:  # noqa: BLE001 - 代理要把失败说给 CLI 听
            reason = f"{type(exc).__name__}: {exc}"
            self._proxy.record_attempt(self.command, self.path, len(body), reason)
            self._reply_failure(reason)
            return
        # 上游真的答了 —— 这一条才计入「模型被调过」。
        self._proxy.record_attempt(
            self.command, self.path, len(body), "served by upstream"
        )
        self._proxy.record_served(self.command, self.path, len(body))
        with response:
            self._reply_stream(response)

    def _reply_stream(self, response: Any) -> None:
        """把上游的答复**流式**送回去。

        分块转发而不是攒成一坨：CLI 读的是 SSE，一坨会让「第一个 token 到
        达」变成「整个答复到齐」，而那正是超时判据量的东西。
        """
        content_type = response.headers.get("Content-Type", "application/json")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def _reply_failure(self, reason: str) -> None:
        """这一次转发失败了 —— 告诉 CLI「别再试了」，并说清为什么。

        状态码选 **400** 是一次**重试策略**的选择，不是对上游的转述：
        CLI 拿到 401 会重试 11 次、182 秒（实测），拿到 400 会在 0.3 秒里
        放弃。一份夜里跑的用例不该把三分钟花在重试上，更不该在重试里把
        「provider 根本不可用」这件事拖到超时才暴露 —— 那时候失败的是
        超时，不是「模型没被调过」这条断言。

        真正的原因写在**响应体**里，一个字不少：上游的状态码、上游的
        原文、连接错误本身。诊断信息因此没有丢，只是换了个位置。
        """
        payload = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"provider proxy: {reason}",
                },
            }
        ).encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """代理不写访问日志 —— 它是测试的一部分，不是服务。"""


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def _set_env(**overrides: Optional[str]) -> Dict[str, Optional[str]]:
    """装环境变量，返回一份「装之前长什么样」（用来还原）。"""
    saved: Dict[str, Optional[str]] = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return saved


def _restore_env(saved: Dict[str, Optional[str]]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _resolve_upstream_key() -> str:
    """真 provider 的钥匙：显式给的优先，否则用进程里那份。"""
    key = (
        os.environ.get(_UPSTREAM_KEY_ENV, "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )
    if not key:
        pytest.fail(
            f"{_REAL_MODEL_ENV} is set but no provider key is available: set "
            f"{_UPSTREAM_KEY_ENV}, or ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY.",
            pytrace=False,
        )
    # 「PROXY MANAGED」是 CC Switch 的哨兵值 —— 它不是一把钥匙，转发出去
    # 只会被上游拒。与其让 CLI 报一个看不懂的 401，不如在这里说清楚。
    if key.upper().replace("_", " ").strip() == "PROXY MANAGED":
        pytest.fail(
            f"the provider key is CC Switch's 'PROXY MANAGED' sentinel, not a "
            f"real key — set {_UPSTREAM_KEY_ENV} to the key of the provider "
            f"this lane is meant to call.",
            pytrace=False,
        )
    return key


def _install_provider_env(
    proxy: _ProviderProxy,
    root: Path,
    plans_dir: Path,
) -> Dict[str, Optional[str]]:
    """把子进程要用的 provider 配置装好。

    ``CLAUDE_SANDBOX_POLICY`` 指向一份「默认不沙箱」的策略：有些机器上的
    ``claude`` 是一层本地启动器，它在找不到策略文件时会走一条坏掉的错误
    分支而退出 1 —— 而套件的环境净化把 ``HOME`` 换掉了，那份策略文件因此
    「找不到」。

    ``CLAUDE_SETTINGS_PATH`` 指向一份空设置：调用方自己（一个正在跑的任务
    子会话）的钩子与设置不属于这次验证会话。
    """
    sandbox_policy = root / "claude-sandbox-policy.toml"
    sandbox_policy.write_text("default = false\n", encoding="utf-8")

    empty_settings = root / "claude-settings.json"
    empty_settings.write_text("{}\n", encoding="utf-8")

    overrides: Dict[str, Optional[str]] = {
        "ANTHROPIC_BASE_URL": proxy.base_url,
        "ANTHROPIC_AUTH_TOKEN": proxy.client_token,
        "ANTHROPIC_API_KEY": proxy.client_token,
        # 端点由上面两个指针决定，不走 CC Switch 的场景路由。
        "PDT_PROVIDER_MODE": "inherit",
        "PDT_PLANS_DIR": str(plans_dir),
        "CLAUDE_SANDBOX_POLICY": str(sandbox_policy),
        "CLAUDE_SETTINGS_PATH": str(empty_settings),
    }
    model = os.environ.get(_UPSTREAM_MODEL_ENV, "").strip()
    if model:
        overrides["ANTHROPIC_MODEL"] = model
        overrides["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
        overrides["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
        overrides["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
    return _set_env(**overrides)


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def _read_ledger(path: Path) -> List[Dict[str, Any]]:
    """台账的每一行（文件不存在 = 空台账）。"""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


@dataclass(frozen=True)
class _RealModelRun:
    """一次真调用留下的两份证据。"""

    #: 台账的行。
    rows: List[Dict[str, Any]] = field(default_factory=list)
    #: 上游真的答了的请求。
    requests: List[Dict[str, Any]] = field(default_factory=list)
    #: 代理收到过的请求（不论上游答没答）—— 只用来诊断。
    attempts: List[Dict[str, Any]] = field(default_factory=list)


def last_session_row(run: _RealModelRun) -> Dict[str, Any]:
    """台账里最后一行 —— 这次真调用写下的那一行。"""
    assert run.rows, (
        "台账是空的：这一轮一次 LLM 会话都没开，所以没有行可以断言归因。"
    )
    return run.rows[-1]


def provider_request_count(run: _RealModelRun) -> int:
    """代理侧数到的请求数。0 = 一个字节都没离开过本机。"""
    return len(run.requests)


# ---------------------------------------------------------------------------
# 跑一轮真的验证
# ---------------------------------------------------------------------------


def _vp(vp_id: str) -> Dict[str, Any]:
    """一条验证点（``code_review`` —— 没有确定性 runner，走 LLM 路径）。

    不带 ``evidence_artifact_dir``：那份契约要求子会话**写**一份
    ``citations.json``，而这份文件要证明的是「请求真的发出去了」，不是
    「子会话的产物被框架收下了」。
    """
    return {
        "id": vp_id,
        "title": "add() 把两个数加起来",
        "verification_method": "code_review",
        "expected_result": "app.py 里的 add(a, b) 返回 a + b",
    }


#: 被审的那份代码 —— 一份小到「读完只要一眼」的项目，让这次会话的花费
#: 花在**证明请求发出去了**上，而不是花在读一个仓库上。
_PROJECT_FILE = '''\
def add(a, b):
    """Add two numbers."""
    return a + b
'''


def run_real_model_vp(
    *,
    plan_dir: Path,
    log_dir: Path,
    project_dir: Path,
) -> None:
    """跑一条真的 ``code_review`` 验证点。

    形状与生产一致：执行器（轮次循环）→ 分派 → 子 agent → ``claude`` CLI
    → 代理 → provider。归因坐标在两处绑定 —— 轮次在执行器的分派点、验证点
    在子 agent 发起会话的那一层 —— 所以台账里那一行必须**同时**带着两个号。
    """
    tool = ClaudeCodingTool(cwd=str(project_dir), scene="verification")

    async def _dispatch(vp: Dict[str, Any]) -> Dict[str, Any]:
        agent = VerificationSubAgent(
            method="code_review",
            max_retries=0,
            model_complexity="simple",
            scene="verification",
            plan_id=PLAN_ID,
            registry=None,
        )
        verdict = await agent.run(
            vp_node=vp,
            coding_tool=tool,
            log_dir=log_dir,
            project_dir=project_dir,
            plan_id=PLAN_ID,
        )
        return {
            "status": verdict.verdict,
            "reasons": list(verdict.reasons),
            "evidence": {"vp_id": vp.get("id")},
        }

    executor = VerificationExecutor(
        verification_plan={"vps": [_vp(VP_ID)]},
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        sub_agent_runner=_dispatch,
        round_no=ROUND_NO,
    )
    asyncio.run(executor.run(max_parallel=0))


@pytest.fixture(scope="module")
def real_model_run(tmp_path_factory: Any) -> _RealModelRun:
    """跑一次真的验证会话，把两份证据交给两条断言。

    一次就够：两条断言问的是**同一次调用**的两面（台账那一行写着谁、代理
    那边有没有真的收到请求）。跑两次会多花一倍的钱，也会让「代理没收到
    请求」这件事有两种可能的解释。

    这个 fixture 是 **module 作用域**的，而套件里 ``isolated_plans_dir`` 是
    **函数作用域**的自动 fixture —— 它每个用例都把 ``PDT_PLANS_DIR`` 改
    一次。所以本 fixture 自己钉住 ``PDT_PLANS_DIR``，并且**在自己的 setup
    里**就把台账读出来：证据在 setup 结束的那一刻取走，之后谁再改这个
    环境变量都动不了它。
    """
    _require_real_model()

    upstream_base_url = os.environ.get(_UPSTREAM_BASE_URL_ENV, "").strip()
    if not upstream_base_url:
        pytest.fail(
            f"{_REAL_MODEL_ENV} is set but {_UPSTREAM_BASE_URL_ENV} is not. "
            "This lane needs the real provider endpoint to forward to: the "
            "suite's hermetic environment pops the ambient "
            "ANTHROPIC_BASE_URL (backend/_pytest_hermetic_env.py), so the "
            "endpoint has to be named explicitly.",
            pytrace=False,
        )
    upstream_api_key = _resolve_upstream_key()

    root = tmp_path_factory.mktemp("real-model-attribution")
    plans_dir = root / "plans"
    plan_dir = root / "plan"
    log_dir = root / "logs"
    project_dir = root / "project"
    for directory in (plans_dir, plan_dir, log_dir, project_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (project_dir / "app.py").write_text(_PROJECT_FILE, encoding="utf-8")

    proxy = _ProviderProxy(upstream_base_url, upstream_api_key)
    proxy.start()
    saved_env = _install_provider_env(proxy, root, plans_dir)
    try:
        with usage_registry.plan_usage_context(plan_id=PLAN_ID):
            run_real_model_vp(
                plan_dir=plan_dir,
                log_dir=log_dir,
                project_dir=project_dir,
            )
        rows = _read_ledger(plans_dir / PLAN_ID / "llm_sessions.jsonl")
    finally:
        _restore_env(saved_env)
        proxy.stop()

    return _RealModelRun(
        rows=rows,
        requests=proxy.requests(),
        attempts=proxy.attempts(),
    )


# ---------------------------------------------------------------------------
# 1. 没设环境变量 → 跳过
# ---------------------------------------------------------------------------


def test_skips_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """没设环境变量 → 这道门把这批判定为「跳过」。

    门是真门：把环境变量拿走，门必须真的跳过（``pytest.skip`` 抛的是
    ``Skipped``，不是别的异常）；把它放回去，门必须放行。只断言「文件顶上
    有一个 skipif」是不够的 —— 那个 skipif 可能在某次重构里被摘掉，而
    「摘掉之后这份文件会在任何机器上白烧真 provider 的钱」这件事，只有
    这条断言能发现。
    """
    monkeypatch.delenv(_REAL_MODEL_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _require_real_model()

    monkeypatch.setenv(_REAL_MODEL_ENV, "1")
    _require_real_model()


# ---------------------------------------------------------------------------
# 2. 台账那一行带着验证点与轮次
# ---------------------------------------------------------------------------


def test_row_carries_vp_and_round(real_model_run: _RealModelRun) -> None:
    """台账那一行的 ``vp_id`` / ``round`` 都不是空的，且就是这一条这一轮。

    这一条问的是「号写对了没有」，不问「模型答了没有」—— 后者是下一条
    的事，而且只有它能区分「模型真的被调过」与「什么都没发生」。
    """
    row = last_session_row(real_model_run)
    assert row["vp_id"] == VP_ID, row
    assert row["round"] == ROUND_NO, row


# ---------------------------------------------------------------------------
# 3. 代理侧真的收到了请求
# ---------------------------------------------------------------------------


def test_provider_was_actually_called(real_model_run: _RealModelRun) -> None:
    """代理侧数到的请求数 > 0。

    这一条是整份文件存在的理由。前两条可以在**零请求**的情况下也绿：一个
    台账替身照样能写出一行带号的行，代理照样能数出 0 个请求，而「没发过
    请求」这件事在 summary 里和「跑通了」长得一模一样。所以「没有回归」
    不许被当成「模型被调过」—— 计数为 0 就是失败。
    """
    count = provider_request_count(real_model_run)
    assert count > 0, (
        "the provider answered zero requests: this round never reached the "
        "provider, so a green run here would be a no-regression green, not a "
        "real-model green. proxy attempts: "
        f"{real_model_run.attempts}; served: {real_model_run.requests}"
    )
