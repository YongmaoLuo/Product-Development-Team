"""请求守卫在验证分支新端点上的负向契约。

背景
----
``backend/request_guard.py`` 规定：每个 ``/api/*`` 请求都必须带
``X-PDT-Request: 1``，且 ``Origin`` / ``Host`` 必须指向本机。挡的是"别的网页
替操作者调这个本地 API"——浏览器里的外来页面无法在不发预检的情况下带上这
个自定义头，而本服务器从不给预检放行。

已有的守卫测试（``tests/security/test_request_guard_not_bypassed_in_suite.py``）
盯的是 ``/api/plan/{id}/status``——一条**开发分支**的既有端点。验证分支新开的
四条路由（本文件的被测对象）必须被同一道守卫罩住：

============  ============================================================
生成          ``POST /api/verification-plan/{plan_id}/generate``
查询          ``GET  /api/verification-plan/{plan_id}``
审核          ``POST /api/verification-plan/{plan_id}/review/item/{item_index}``
启动          ``POST /api/verification/{plan_id}/start``
============  ============================================================

"端点存在"与"端点被罩住"是两件事
--------------------------------
守卫是**中间件**，它在路由之前跑。于是**一条根本不存在的路径**也会拿到
403——单看 403，证明不了路由活着，更证明不了是守卫拦的。

能同时证明两件事的是把同一个请求的两种结果摆在一起看：

* 缺头 → 403，且响应体是守卫自己的 ``{"error": "forbidden", ...}``；
* 带对的头 → **不是** 403，而且答案是**路由自己给的** 404
  （``plan_not_found`` / ``Plan not found``），不是 Starlette 兜底的
  ``{"detail": "Not Found"}``。

后者证明请求真的走进了路由、路由也确实存在；前者证明挡住它的是守卫而不是
路由。所以正向对照断的不是"200"，而是"**路由级**的答案"。

为什么不用 ``TestClient``
-------------------------
conftest 给每个 ``TestClient`` 注入 ``X-PDT-Request: 1``
（``_pytest_hermetic_env.install_request_guard_header``）。那是在**满足**
守卫，不是绕过它——但它意味着经由 ``TestClient`` 发出的每个请求都带着那个
头，"缺头 → 403"这条契约在那种客户端上根本测不出来。真正裸奔的请求只有
自建 ``httpx.AsyncClient(transport=ASGITransport(app=app))`` 才发得出去，这
也是本仓库既有的做法。

裸客户端为什么仍然要写 ``base_url="http://testserver"``
------------------------------------------------------
``Host`` 是守卫的另一半（DNS rebinding 那条）。``testserver`` 是 conftest
注册进 ``PDT_ALLOWED_HOSTS`` 的本机名；不写它，请求会因为"Host 不是本机"
被 403——那样测的就不是"缺头"这条契约了。静态门禁
（``tests/security/test_request_guard_not_bypassed_in_suite.py::
test_no_self_built_client_misses_header_or_base_url``）也要求每个自建客户端
都带上 ``base_url`` 与守卫头，所以本文件的三个构造点都照写。

本文件不改守卫
--------------
``request_guard.py`` 一行不动。这里只提供**证据**：四条新端点在缺头与跨源
两种情况下确实被守卫挡住，带对头时确实放行。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: Plan id used for every probe.  It never exists on disk, so the four
#: routes answer 404 —— a **route-level** answer, which is what the
#: positive control needs.  The id stays inside the safe character set
#: (``[A-Za-z0-9._-]``) so ``_plan_dir`` accepts it and the route is
#: reached instead of being turned away with 400.
PROBE_PLAN_ID = "guard-probe-plan"

#: An origin that is emphatically not this machine.
FOREIGN_ORIGIN = "https://evil.example.com"

#: The four endpoint kinds the verification branch added, with the
#: method, the path template and the body a well-formed client sends.
#:
#: The bodies matter for the positive control: FastAPI validates the
#: request model *before* the handler runs, so a malformed body would
#: answer 422 and the "not 403" assertion would pass for the wrong
#: reason (the model answered, not the route).
ENDPOINTS: Tuple[Tuple[str, str, str, Optional[Dict[str, Any]]], ...] = (
    (
        "generate",
        "POST",
        "/api/verification-plan/{plan_id}/generate",
        {"feedback": None},
    ),
    (
        "query",
        "GET",
        "/api/verification-plan/{plan_id}",
        None,
    ),
    (
        "review",
        "POST",
        "/api/verification-plan/{plan_id}/review/item/0",
        {"action": "accept"},
    ),
    (
        "start",
        "POST",
        "/api/verification/{plan_id}/start",
        {},
    ),
)


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
    裸的。这正是 ``tests/security/test_attack_path_matrix.py`` 对
    ``TestClient.headers`` 用的那一招。
    """
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-PDT-Request": "1"},
    )
    client.headers.pop("X-PDT-Request", None)
    return client


def _cross_origin_client(app, origin: str) -> httpx.AsyncClient:
    """守卫头是对的、但 ``Origin`` 指向别的站点的客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-PDT-Request": "1", "Origin": origin},
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


async def _send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: Optional[Dict[str, Any]],
):
    """发请求；GET 不带体，其余带体。"""
    if method == "GET":
        return await client.get(path)
    return await client.post(path, json=body)


# ---------------------------------------------------------------------------
# 1) 缺头 → 403（生成 / 查询 / 审核 / 启动）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_header_403(app) -> None:
    """不带 ``X-PDT-Request`` → 四类端点全部 403。

    守卫在路由之前跑，所以这四条请求根本走不到 handler——403 是守卫给的，
    响应体里 ``error=forbidden`` 与点名 ``X-PDT-Request`` 的理由就是证据。
    """
    for kind, method, template, body in ENDPOINTS:
        path = template.format(plan_id=PROBE_PLAN_ID)
        async with _bare_client(app) as client:
            resp = await _send(client, method, path, body)

        assert resp.status_code == 403, (
            f"{method} {path} 缺 X-PDT-Request 时答了 {resp.status_code}，"
            f"必须 403"
        )
        payload = resp.json()
        assert payload.get("error") == "forbidden", (
            f"{method} {path} 的 403 必须来自守卫（error=forbidden）；"
            f"实际响应：{payload}"
        )
        detail = str(payload.get("detail", ""))
        assert "X-PDT-Request" in detail, (
            f"守卫的拒绝理由必须点名那个缺失的头；实际：{detail!r}"
        )


# ---------------------------------------------------------------------------
# 2) 跨源 → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_origin_403(app) -> None:
    """守卫头是对的，但 ``Origin`` 指向外来站点 → 403。

    这是守卫的另外半边：一个同源但**别的**页面可以把头设对（它是同源的），
    所以 ``Origin`` 必须单独成立。理由文本以 ``origin`` 开头——这样能证明
    403 是由 ``Origin`` 检查触发的，而不是碰巧缺头。
    """
    for kind, method, template, body in ENDPOINTS:
        path = template.format(plan_id=PROBE_PLAN_ID)
        async with _cross_origin_client(app, FOREIGN_ORIGIN) as client:
            resp = await _send(client, method, path, body)

        assert resp.status_code == 403, (
            f"{method} {path} 的 Origin 是 {FOREIGN_ORIGIN} 时答了 "
            f"{resp.status_code}，必须 403"
        )
        payload = resp.json()
        assert payload.get("error") == "forbidden", (
            f"{method} {path} 的 403 必须来自守卫（error=forbidden）；"
            f"实际响应：{payload}"
        )
        detail = str(payload.get("detail", ""))
        assert detail.lower().startswith("origin "), (
            f"403 必须由 Origin 检查触发（理由以 'origin ' 开头），"
            f"而不是缺头检查；实际理由：{detail!r}"
        )


# ---------------------------------------------------------------------------
# 3) 带对的头 → 不是 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_header_not_forbidden(app) -> None:
    """带对的头 + 本机 Host → 不是 403，而且答案是**路由自己给的**。

    正向对照。它断言的与其说是"200"，不如说是"**路由级**的答案"：四条端点
    在计划不存在时各自报 404（``plan_not_found`` / ``Plan not found``），
    而 Starlette 兜底的路由未命中是 ``{"detail": "Not Found"}``。所以这里
    断两件事——守卫放行了（不是 403），而且请求真的走进了路由（是路由的
    404，不是路由器的兜底）。少了后半句，"端点存在"这件事就没被证明。
    """
    for kind, method, template, body in ENDPOINTS:
        path = template.format(plan_id=PROBE_PLAN_ID)
        async with _guarded_client(app) as client:
            resp = await _send(client, method, path, body)

        assert resp.status_code != 403, (
            f"{method} {path} 带着正确的守卫头却答了 403；守卫应当放行"
        )
        assert resp.status_code == 404, (
            f"{method} {path} 应当由路由回答 404（计划不存在）；"
            f"实际 {resp.status_code}"
        )
        payload = resp.json()
        assert payload.get("error") in ("plan_not_found", "Plan not found"), (
            f"{method} {path} 的 404 必须来自路由本身（plan_not_found），"
            f"而不是路由未命中的兜底；实际响应：{payload}"
        )