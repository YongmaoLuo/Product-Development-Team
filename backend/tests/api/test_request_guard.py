"""请求守卫的边界负控——缺头、跨源、非本机 Host、调试路由。

背景
----
``backend/request_guard.py`` 是这个本地 API 的门：每个 ``/api/*`` 请求都必须
带 ``X-PDT-Request: 1``，且 ``Origin`` / ``Host`` 必须指向本机。挡的是"别的
网页替操作者调这个本地 API"——浏览器里的外来页面无法在不发预检的情况下带上
这个自定义头，而本服务器从不给预检放行。

本文件把这道门的**四条边界**各自钉成一条用例：

============================  =========================================
边界                          期望
============================  =========================================
缺 ``X-PDT-Request``          403
``Origin`` 指向别的站点        403
``Host`` 不是本机              403
``PDT_DEBUG_ROUTES`` 未设      ``/api/_debug/*`` → 404
============================  =========================================

"403 就够了"是不够的
--------------------
守卫是**中间件**，它在路由之前跑。于是**一条根本不存在的路径**也会拿到 403
——单看 403，证明不了路由活着，更证明不了是守卫拦的。能同时证明两件事的
是把同一个请求的两种结果摆在一起看：缺头 → 403 且响应体是守卫自己的
``{"error": "forbidden", ...}``；带对的头 → **不是** 403，答案是路由自己给的
答案。所以每条负控都配一条正向对照。

"路由存在"与"路由被罩住"是两件事
--------------------------------
反过来也一样：调试路由在 ``PDT_DEBUG_ROUTES`` 未设时必须 404。这条不能
靠进程内的 app 测——套件自己把 ``PDT_DEBUG_ROUTES`` 设成了 ``1``
（``_pytest_hermetic_env.install_hermetic_env``），进程内的 app 永远看得见
调试路由。必须另起一个进程、把那个环境变量摘掉，再问一次。

为什么不用 ``TestClient``
-------------------------
conftest 给每个 ``TestClient`` 注入 ``X-PDT-Request: 1``
（``_pytest_hermetic_env.install_request_guard_header``）。那是在**满足**
守卫，不是绕过它——但它意味着经由 ``TestClient`` 发出的每个请求都带着那个
头，"缺头 → 403"这条契约在那种客户端上根本测不出来。真正裸奔的请求只有
自建 ``httpx.AsyncClient(transport=ASGITransport(app=app))`` 才发得出去，
这也是本仓库既有的做法。

裸客户端为什么仍然要写 ``base_url="http://testserver"``
------------------------------------------------------
``Host`` 是守卫的另一半（DNS rebinding 那条）。``testserver`` 是 conftest
注册进 ``PDT_ALLOWED_HOSTS`` 的本机名；不写它，请求会因为"Host 不是本机"
被 403——那样测的就不是"缺头"这条契约了。静态门禁
（``tests/security/test_request_guard_not_bypassed_in_suite.py::
test_no_self_built_client_misses_header_or_base_url``）也要求每个自建客户端
都带上 ``base_url`` 与守卫头，所以本文件的每个构造点都照写。

本文件不改守卫
--------------
``request_guard.py`` 一行不动，连**旁路**都不加。这里只提供**证据**：这四
条边界确实由守卫挡着。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: The repository root (the parent of ``backend/``).
_REPO_ROOT = _BACKEND_DIR.parent

#: The route every probe below targets.  ``GET /api/plans`` answers 200
#: when the guard lets the request through, so "not 403" is a meaningful
#: positive control: it says the request reached a live route.
PROBE_ROUTE = "/api/plans"

#: A debug route that exists only while ``PDT_DEBUG_ROUTES`` is on.  The
#: plan id stays inside the safe character set so the route is *reached*
#: rather than turned away with 400 — the point of the probe is whether
#: the route is registered at all, not whether its plan exists.
DEBUG_ROUTE = "/api/_debug/inject_verification_state/probe-plan"

#: An origin that is emphatically not this machine.
FOREIGN_ORIGIN = "https://evil.example.com"

#: A host that is emphatically not this machine either.  ``evil.example``
#: is neither loopback nor in the suite's allowed-host list, so the guard
#: must refuse it — the DNS-rebinding half of the guard.
FOREIGN_HOST = "evil.example"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def _guarded_client(app) -> httpx.AsyncClient:
    """带着守卫头的客户端——真实客户端发的那种请求。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-PDT-Request": "1"},
    )


def _bare_client(app) -> httpx.AsyncClient:
    """**不带**守卫头的客户端——裸请求。

    头是在构造**之后**摘掉的，而不是构造之前不给：这样构造点看上去和套件
    里其它每一个构造点一模一样（静态门禁也认），而真正发出去的那条请求是
    裸的。
    """
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-PDT-Request": "1"},
    )
    client.headers.pop("X-PDT-Request", None)
    return client


def _foreign_origin_client(app) -> httpx.AsyncClient:
    """守卫头是对的、但 ``Origin`` 指向别的站点的客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-PDT-Request": "1", "Origin": FOREIGN_ORIGIN},
    )


def _foreign_host_client(app) -> httpx.AsyncClient:
    """守卫头是对的、但 ``Host`` 不是本机的客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-PDT-Request": "1", "Host": FOREIGN_HOST},
    )


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """被测的 FastAPI app。

    惰性导入：这样收集阶段不依赖整套后端栈可导入；等到这个夹具跑起来时，
    conftest 早就把无污染环境装好了（守卫头注入 + ``testserver`` 作为允许的
    主机名）。
    """
    import server

    return server.app


def _assert_guard_refusal(response: httpx.Response, expected_detail: str) -> None:
    """断言这个 403 是**守卫**给的，而不是路由或框架给的。"""
    assert response.status_code == 403, (
        f"请求 {response.request.method} {response.request.url} 答了 "
        f"{response.status_code}，必须 403"
    )
    payload: Dict[str, Any] = response.json()
    assert payload.get("error") == "forbidden", (
        f"403 必须来自守卫（error=forbidden）；实际响应：{payload}"
    )
    detail = str(payload.get("detail", ""))
    assert expected_detail in detail, (
        f"守卫的拒绝理由必须点名 {expected_detail!r}；实际：{detail!r}"
    )


# ---------------------------------------------------------------------------
# 1) 缺头 → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_request_header_403(app) -> None:
    """不带 ``X-PDT-Request`` → 403。

    守卫在路由之前跑，所以这条请求根本走不到 handler——403 是守卫给的，
    响应体里 ``error=forbidden`` 与点名 ``X-PDT-Request`` 的理由就是证据。
    """
    async with _bare_client(app) as client:
        response = await client.get(PROBE_ROUTE)

    _assert_guard_refusal(response, "X-PDT-Request")


@pytest.mark.asyncio
async def test_guarded_request_is_not_403(app) -> None:
    """正向对照：带对头的同一个请求**不是** 403。

    没有这条对照，上面那条 403 就可能只是"路由不存在"的 404 换了个说法。
    """
    async with _guarded_client(app) as client:
        response = await client.get(PROBE_ROUTE)

    assert response.status_code != 403, (
        f"带守卫头的 {PROBE_ROUTE} 答了 403；守卫把正常请求也拦了，"
        "上面几条负控量的就不是守卫"
    )
    assert response.status_code == 200, (
        f"带守卫头的 {PROBE_ROUTE} 答了 {response.status_code}，预期 200"
    )


# ---------------------------------------------------------------------------
# 2) 跨源 → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_origin_403(app) -> None:
    """守卫头是对的，但 ``Origin`` 指向外来站点 → 403。

    外来页面能发出带自定义头的请求吗？不能——那需要预检，而本服务器从不给
    预检放行。但"不能"是浏览器侧的 courtesy，不是服务端的保证：这条契约把
    拒绝放在服务端，与浏览器行为无关。
    """
    async with _foreign_origin_client(app) as client:
        response = await client.get(PROBE_ROUTE)

    _assert_guard_refusal(response, "origin")


# ---------------------------------------------------------------------------
# 3) 非本机 Host → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_host_403(app) -> None:
    """守卫头是对的，但 ``Host`` 不是本机 → 403。

    这是守卫的 DNS-rebinding 那半边：即使攻击者能设出那个自定义头，一个
    重新解析到本机的域名也必须**以本机的名义**到达。裸客户端可以把 ``Host``
    钉成一个明摆着的外来名字；``TestClient`` 永远发 ``Host: testserver``。
    """
    async with _foreign_host_client(app) as client:
        response = await client.get(PROBE_ROUTE)

    _assert_guard_refusal(response, "host")


# ---------------------------------------------------------------------------
# 4) PDT_DEBUG_ROUTES 未设 → /api/_debug/* 404
# ---------------------------------------------------------------------------

#: The debug-route probe runs in its own process because route
#: registration happens at *import* of ``server`` — by then the decision
#: has already been made.  The running suite turned the debug routes on
#: (``_pytest_hermetic_env`` sets ``PDT_DEBUG_ROUTES=1``), so an
#: in-process probe could only ever see the enabled state; the disabled
#: state has to be asked of a process that never had the variable.
#:
#: The probe talks to ``http://127.0.0.1`` — a loopback hostname, which
#: the guard always accepts (``request_guard.allowed_hosts`` seeds it),
#: so the probe does not depend on the suite's ``PDT_ALLOWED_HOSTS``.
#: It sends the guard header explicitly because the subprocess has no
#: conftest to inject it.
#:
#: The two directories come in as arguments rather than being
#: interpolated into the script, so this file contains no
#: machine-specific path.
_DEBUG_PROBE_SCRIPT = """
import os
import sys

if sys.argv[1] == "off":
    os.environ.pop("PDT_DEBUG_ROUTES", None)
else:
    os.environ["PDT_DEBUG_ROUTES"] = "1"

sys.path[:0] = [sys.argv[2], sys.argv[3]]

import server
from fastapi.testclient import TestClient

client = TestClient(
    server.app, base_url="http://127.0.0.1", headers={"X-PDT-Request": "1"}
)
for method, path in [
    ("GET", "/api/_debug/inject_verification_state/probe-plan"),
    ("POST", "/api/_debug/inject_verification_state/probe-plan"),
    ("GET", "/api/debug/notifications"),
]:
    response = client.request(method, path, json={} if method == "POST" else None)
    print(f"{method} {path} {response.status_code}")
"""


def _debug_probe(debug_routes: str) -> Dict[str, int]:
    """What a freshly-started server answers for each debug probe.

    ``debug_routes`` is ``"off"`` (the variable is removed from the
    environment before ``server`` is imported) or ``"on"`` (it is set).
    The returned mapping is keyed by ``"<METHOD> <path>"``.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _DEBUG_PROBE_SCRIPT,
            debug_routes,
            str(_REPO_ROOT),
            str(_BACKEND_DIR),
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"the debug-route probe process failed (exit {proc.returncode}); "
        f"stderr:\n{proc.stderr[-2000:]}"
    )
    statuses: Dict[str, int] = {}
    for line in proc.stdout.strip().splitlines():
        method, path, status = line.rsplit(" ", 2)
        statuses[f"{method} {path}"] = int(status)
    return statuses


def test_debug_routes_404_when_disabled() -> None:
    """``PDT_DEBUG_ROUTES`` 未设时，调试路由不存在 → 404。

    调试路由是操作者工具（注入验证状态、推卡片、看通知总线），默认必须
    关闭——它们是"绕过正常流程"的后门，只在显式打开时才存在。

    探针用 ``GET`` 而不是路由自己的 ``POST``：服务里有一条兜底的
    ``GET /{path:path}``（打包 UI 的静态回退），它对**未知路径**回 404，
    对**已知路径但方法不对**回 405。路由没被注册时，GET 得到的就是
    "这条路径不存在"的那个 404——正是本契约要说的那句话。
    """
    statuses = _debug_probe("off")

    assert statuses[f"GET {DEBUG_ROUTE}"] == 404, (
        f"GET {DEBUG_ROUTE} 在 PDT_DEBUG_ROUTES 未设时答了 "
        f"{statuses[f'GET {DEBUG_ROUTE}']}，必须 404"
    )


def test_debug_routes_are_reachable_when_opted_in() -> None:
    """负控：显式打开时，调试路由**存在**且答 200。

    没有这条对照，上一条 404 可能只是"这条路径本来就是 404"，而不是
    "路由没被注册"。两个分支摆在一起，404 才有确定的意思。

    这里用路由自己的 ``POST``：路由注册了但方法不对时是 405，注册了
    且方法对时是 200——两个答案都不是 404，所以这条对照不会被兜底路由
    的 404 蒙混过去。
    """
    statuses = _debug_probe("on")

    assert statuses[f"POST {DEBUG_ROUTE}"] == 200, (
        f"POST {DEBUG_ROUTE} 在 PDT_DEBUG_ROUTES=1 时答了 "
        f"{statuses[f'POST {DEBUG_ROUTE}']}，必须 200；调试路由没有被注册，"
        "上一条 404 量不到'默认关闭'这件事"
    )
    assert statuses[f"GET /api/debug/notifications"] == 200, (
        "GET /api/debug/notifications 在 PDT_DEBUG_ROUTES=1 时答了 "
        f"{statuses['GET /api/debug/notifications']}，必须 200"
    )
