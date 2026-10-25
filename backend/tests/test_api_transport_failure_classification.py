"""VP-001's, VP-002's and VP-003's transport failure is an infrastructure
failure.

Background
----------
``VP-001`` of the PRD plan ``20261021-PDT-HITL-1-infra-cri`` asks the
declared api service for ``GET {{svc.api.url}}/health/data`` and asserts
a 200 whose ``$.status`` is ``"ok"``.

In the 2026-10-24 round the request never reached the service: the TCP
connection was refused, so **not one of the declared assertions ran**
(``assertions_failed`` was ``null``) — and the verdict still came out
as a plain ``FAILED``.

That verdict is wrong in a way that costs the round. A plain failure is
a ``deliverable_defect`` — the class that counts against the same-cause
threshold — so a service that dropped its listener produced a repair
task against the deliverable and pushed the plan towards the "same
cause, three times" escalation. The environment was broken; the
deliverable was not.

``VP-002`` of the same plan failed the same way, five rounds in a row,
with the same ``blocked_ports``-only evidence: it asks the declared api
service for ``POST {{svc.api.url}}/api/hitl/verify-hitl-guard/decisions``
**without** the ``X-PDT-Request`` header and asserts a 403 whose body
names the missing header. It never reached the service either, so its
two assertions never ran.

``VP-003`` of the same plan failed the same way once more: it asks the
declared api service for the decision list of a plan that never created
one — ``GET {{svc.api.url}}/api/hitl/verify-hitl-unknown/decisions``,
**with** the ``X-PDT-Request`` header — and asserts a 200 whose body is
an empty array. Its two assertions never ran either, so the criterion
("no decisions yet" must not read as "no such plan") was never checked.

``VP-004`` of the same plan failed the same way once more: it asks the
declared api service for the same decision list narrowed by a ``status``
filter that is **outside** the closed five-word vocabulary —
``GET {{svc.api.url}}/api/hitl/verify-hitl-unknown/decisions?status=PENDING``
— and asserts a 400 whose body carries the error code
``invalid_status``. Its two assertions never ran either, so the criterion
("a filter nobody can interpret must be refused, not silently ignored")
was never checked.

``VP-005`` of the same plan failed the same way once more: it asks the
declared api service for the decision list of a plan whose id is **not a
safe leaf** — ``bad id``, percent-encoded into the path as ``bad%20id`` —
``GET {{svc.api.url}}/api/hitl/bad%20id/decisions``, and asserts a 400
whose body carries the error message ``Invalid plan id``. Its two
assertions never ran either, so the criterion ("the decisions endpoint
must not become an arbitrary-path write point") was never checked.

``VP-006`` of the same plan failed the same way once more: it asks the
declared api service to **create** a decision whose mode is ``auto`` and
whose ``default_option`` is missing —
``POST {{svc.api.url}}/api/hitl/verify-hitl-auto/decisions`` — and
asserts a 400 whose body carries the refusal slug
``auto_requires_default``. Its two assertions never ran either, so the
criterion ("an auto decision with no fallback has no way to end, so it
must be refused rather than stored") was never checked.

``VP-007`` of the same plan failed the same way once more: it asks the
declared api service to **create** a decision whose option set is
**empty** — ``POST {{svc.api.url}}/api/hitl/verify-hitl-api/decisions`` —
and asserts a 400 whose body carries the error code
``invalid_request``. Its two assertions never ran either, so the
criterion ("a decision with no options has no way to be answered, so it
must be refused at the door") was never checked.

``VP-008`` of the same plan failed the same way once more: it asks the
declared api service to **create** a decision whose deadline is **zero**
— the same endpoint as VP-007, but with ``timeout_sec`` 0 — and asserts
a 400 whose body carries the refusal slug ``invalid_timeout``. Its two
assertions never ran either, so the criterion ("a decision whose
deadline has already passed must not be stored") was never checked.

``VP-009`` of the same plan failed the same way once more: it asks the
declared api service to **create** a decision whose ``mode`` is
**outside the closed two-word vocabulary** (``auto`` / ``strict``) —
``someday`` — and asserts a 400 whose body carries the error code
``invalid_request``. Its two assertions never ran either, so the
criterion ("a mode nobody can interpret must be refused, not silently
degraded into ``auto``") was never checked.

``VP-012`` of the same plan failed the same way once more: it asks the
declared api service to **answer** a decision that does not exist
(``no-such-decision``) —
``POST {{svc.api.url}}/api/hitl/verify-hitl-api/decisions/no-such-decision/answer``
— and asserts a 404 whose body carries the error code ``not_found``.
Its two assertions never ran either, so the criterion ("an unknown
``decision_id`` must not be taken for a success") was never checked.

``VP-013`` of the same plan failed the same way once more: it asks the
declared api service to **answer** a decision **without naming an
option** — the same endpoint as VP-012, but with a body that carries
``answered_by`` and no ``option`` — and asserts a 422. Its single
assertion never ran either, so the criterion ("an answer that names no
option must be refused by the request-body validation, before it can
reach the store") was never checked.

``VP-014`` of the same plan failed the same way once more: it asks the
declared api service for the **plan summary** of a plan that has no
outstanding human decision —
``GET {{svc.api.url}}/api/plan/20260101-cleanup-sample/summary`` — and
asserts a 200 whose body carries a ``pending_decisions`` block whose
``count`` is 0 and whose ``items`` is an empty array. Its three
assertions never ran either, so the criterion ("the summary projection
must say how many human decisions are waiting, in a shape the front end
can consume directly") was never checked.

``VP-015`` of the same plan failed the same way once more: it asks the
declared api service for the **plan summary** of a plan that **does not
exist** —
``GET {{svc.api.url}}/api/plan/verify-hitl-nonexistent/summary`` — and
asserts a 404. Its single assertion never ran either, so the criterion
("a plan nobody created must be refused, not answered with some other
plan's projection") was never checked.

VP-007, VP-008 and VP-009 share one endpoint and are told apart by the
**body**: the first carries an empty ``options`` list, the second a zero
``timeout_sec``, the third a mode outside the vocabulary. That is how
the service itself tells them apart (``backend/hitl/service.py`` checks
the options first, the mode second and the deadline third), so the
fixtures below refuse on the body as well — a request that lost its body
on the way out cannot pass by accident.

What this file pins
-------------------
(a) With no listener on the declared port the verdict says *that*:
    a machine-readable ``signal``, the ``blocked_ports`` behind it, and
    the class ``infra`` — which does not count.
(a2) The same holds for the **round-start gate** — the gate that
    decided, once, that the port was dead and handed that decision to
    every VP that touched it. Its verdict carries the same
    ``signal``/``blocked_ports``/``service_detail``, so a round that
    starts against a dead service leaves a diagnosis behind instead of
    a bare port number.
(b) With the service reachable, the very same request and the very same
    assertions are still judged by the original criteria: 200 and
    ``$.status == "ok"`` for VP-001; 403 and a body naming
    ``X-PDT-Request`` for VP-002; 200 and an empty array for VP-003;
    400 and ``invalid_status`` for VP-004; 400 and ``Invalid plan id``
    for VP-005; 400 and ``auto_requires_default`` for VP-006;
    400 and ``invalid_request`` for VP-007; 400 and
    ``invalid_timeout`` for VP-008; 400 and ``invalid_request`` for
    VP-009; 404 and ``not_found`` for VP-012; 422 for VP-013; 200 with
    a ``pending_decisions`` block whose ``count`` is 0 and whose
    ``items`` is an empty array for VP-014; 404 for VP-015.

The request and the assertions are the VPs' own, copied verbatim from
the plan's ``verification_plan.json``. The plan lives outside the
repository, so the fixture is pinned here rather than read from disk:
the test has to mean the same thing on a machine that has never run
that plan.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


# Ensure ``backend/`` is on sys.path so ``import verification_agent``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: VP-001's request, verbatim from the plan. The URL is the only part
#: that cannot be pinned: the plan spells it as the ``{{svc.api.url}}``
#: placeholder, which resolves to ``http://127.0.0.1:<declared port>``.
VP001_REQUEST_TEMPLATE = {
    "method": "GET",
    "url": "{base_url}/health/data",
    "headers": {"Accept": "application/json"},
    "body": None,
    "timeout_seconds": 30,
}

#: VP-001's assertions, verbatim from the plan.
VP001_ASSERTIONS = [
    {"name": "状态码 200", "status": 200},
    {"name": "数据面状态为 ok", "json_path": "$.status", "equals": "ok"},
]

#: VP-002's request, verbatim from the plan: a HITL decision created
#: **without** the ``X-PDT-Request`` header, so the request guard has
#: to refuse it before the request reaches the business logic.
VP002_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": "{base_url}/api/hitl/verify-hitl-guard/decisions",
    "headers": {
        "Accept": "application/json",
        "Content-Type": "application/json",
    },
    "body": {
        "question": "守卫验证：这条请求不应到达业务层",
        "options": ["accept", "reject"],
        "default_option": "accept",
    },
    "timeout_seconds": 30,
}

#: VP-002's assertions, verbatim from the plan.
VP002_ASSERTIONS = [
    {"name": "状态码 403", "status": 403},
    {"name": "403 明确点名缺失的请求头", "body_contains": "X-PDT-Request"},
]

#: VP-003's request, verbatim from the plan: the decision list of a plan
#: that never created a decision.  The request **does** carry the
#: ``X-PDT-Request`` header, so unlike VP-002 it is allowed to reach the
#: business logic — what it checks is that "no decisions yet" is a 200
#: with an empty array and not a 404.
VP003_REQUEST_TEMPLATE = {
    "method": "GET",
    "url": "{base_url}/api/hitl/verify-hitl-unknown/decisions",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
    },
    "body": None,
    "timeout_seconds": 30,
}

#: VP-003's assertions, verbatim from the plan.
VP003_ASSERTIONS = [
    {"name": "状态码 200", "status": 200},
    {"name": "返回空数组", "json_path": "$", "length_equals": 0},
]


#: VP-004's request, verbatim from the plan: the decision list narrowed
#: by a ``status`` filter that is **outside** the closed vocabulary.  The
#: query string survives the ``{{svc.api.url}}`` resolution — the
#: refusal is about the *filter*, not about the path.
VP004_REQUEST_TEMPLATE = {
    "method": "GET",
    "url": "{base_url}/api/hitl/verify-hitl-unknown/decisions?status=PENDING",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
    },
    "body": None,
    "timeout_seconds": 30,
}

#: VP-004's assertions, verbatim from the plan.
VP004_ASSERTIONS = [
    {"name": "状态码 400", "status": 400},
    {
        "name": "错误码为 invalid_status",
        "json_path": "$.error",
        "equals": "invalid_status",
    },
]


#: VP-005's request, verbatim from the plan: the decision list of a plan
#: whose id is **not a safe leaf** — ``bad id``, percent-encoded into the
#: path as ``bad%20id``.  The encoding survives the ``{{svc.api.url}}``
#: resolution, so the request that goes out is the one the plan wrote.
VP005_REQUEST_TEMPLATE = {
    "method": "GET",
    "url": "{base_url}/api/hitl/bad%20id/decisions",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
    },
    "body": None,
    "timeout_seconds": 30,
}

#: VP-005's assertions, verbatim from the plan.
VP005_ASSERTIONS = [
    {"name": "状态码 400", "status": 400},
    {
        "name": "错误信息为 Invalid plan id",
        "json_path": "$.error",
        "equals": "Invalid plan id",
    },
]


#: VP-006's request, verbatim from the plan: the creation of a decision
#: whose mode is ``auto`` and whose ``default_option`` is missing.  An
#: ``auto`` decision means "when the deadline passes, use the default
#: answer", so a decision with no default has no way to end — the
#: service refuses it instead of storing a question that could never
#: resolve.
VP006_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": "{base_url}/api/hitl/verify-hitl-auto/decisions",
    "headers": {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-PDT-Request": "1",
    },
    "body": {
        "question": "守卫验证：auto 决策没有默认答案，应当被拒绝",
        "options": ["accept", "revise"],
        "default_option": None,
    },
    "timeout_seconds": 30,
}

#: VP-006's assertions, verbatim from the plan.
VP006_ASSERTIONS = [
    {"name": "状态码 400", "status": 400},
    {
        "name": "拒绝原因为 auto_requires_default",
        "json_path": "$.error",
        "equals": "auto_requires_default",
    },
]


#: VP-007's request, verbatim from the plan: the creation of a decision
#: whose option set is **empty**.  A decision with no options has no way
#: to be answered, so the service refuses the question at the door —
#: before anything is written down.
VP007_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": "{base_url}/api/hitl/verify-hitl-api/decisions",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
        "Content-Type": "application/json",
    },
    "body": {
        "question": "没有选项的问题",
        "options": [],
        "default_option": "accept",
    },
    "timeout_seconds": 30,
}

#: VP-007's assertions, verbatim from the plan.
VP007_ASSERTIONS = [
    {"name": "状态码 400", "status": 400},
    {
        "name": "错误码为 invalid_request",
        "json_path": "$.error",
        "equals": "invalid_request",
    },
]


#: VP-008's request, verbatim from the plan: the creation of a decision
#: whose deadline is **zero**.  A decision whose deadline has already
#: passed can never be answered in time, so the service refuses it
#: instead of storing a question that is dead on arrival.
#:
#: The endpoint is the same one VP-007 uses — the two are told apart by
#: the **body**, exactly as the service tells them apart.
VP008_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": "{base_url}/api/hitl/verify-hitl-api/decisions",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
        "Content-Type": "application/json",
    },
    "body": {
        "question": "零超时的问题",
        "options": ["accept", "reject"],
        "default_option": "accept",
        "timeout_sec": 0,
    },
    "timeout_seconds": 30,
}

#: VP-008's assertions, verbatim from the plan.
VP008_ASSERTIONS = [
    {"name": "状态码 400", "status": 400},
    {
        "name": "拒绝原因为 invalid_timeout",
        "json_path": "$.error",
        "equals": "invalid_timeout",
    },
]


#: VP-009's request, verbatim from the plan: the creation of a decision
#: whose ``mode`` is **outside the closed two-word vocabulary**
#: (``auto`` / ``strict``).  A mode nobody can interpret must be
#: **refused** — and refused, not silently degraded into ``auto``: an
#: ``auto`` decision answers itself when the deadline passes, so a
#: caller who asked for something else would get an answer it never
#: chose.
#:
#: The endpoint is the same one VP-007 and VP-008 use — the three are
#: told apart by the **body**, exactly as the service tells them apart.
VP009_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": "{base_url}/api/hitl/verify-hitl-api/decisions",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
        "Content-Type": "application/json",
    },
    "body": {
        "question": "未知模式的问题",
        "options": ["accept", "reject"],
        "default_option": "accept",
        "mode": "someday",
    },
    "timeout_seconds": 30,
}

#: VP-009's assertions, verbatim from the plan.
VP009_ASSERTIONS = [
    {"name": "状态码 400", "status": 400},
    {
        "name": "错误码为 invalid_request",
        "json_path": "$.error",
        "equals": "invalid_request",
    },
]


#: VP-012's request, verbatim from the plan: the **answer** of a decision
#: that does not exist (``no-such-decision``).  An unknown
#: ``decision_id`` must not be taken for a success — the answer is a 404
#: whose body carries the error code ``not_found``, and no decision
#: entity, because there is no decision to report.
#:
#: The request carries a real answer body, so the answer depends on the
#: **body**: an answer without an ``option`` is refused with a 422
#: first, exactly as the service refuses it.  A request that lost its
#: body on the way out therefore cannot pass by accident.
VP012_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": (
        "{base_url}/api/hitl/verify-hitl-api/decisions"
        "/no-such-decision/answer"
    ),
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
        "Content-Type": "application/json",
    },
    "body": {
        "option": "approve",
        "answered_by": "verify-bot",
    },
    "timeout_seconds": 30,
}

#: VP-012's assertions, verbatim from the plan.
VP012_ASSERTIONS = [
    {"name": "状态码 404", "status": 404},
    {
        "name": "错误码为 not_found",
        "json_path": "$.error",
        "equals": "not_found",
    },
]

#: VP-013's request, verbatim from the plan: the **answer** of a
#: decision whose body names **no option** — only who answered it.  The
#: request-body validation refuses it with a 422, and it does so
#: **before** the decision is looked up, so the answer is a 422 even
#: though the decision named in the path does not exist either.
#:
#: The refusal depends on the **body**: an answer that carries an
#: ``option`` gets past the validation and is refused with a 404
#: instead.  A request that lost its body on the way out therefore
#: cannot pass by accident.
VP013_REQUEST_TEMPLATE = {
    "method": "POST",
    "url": (
        "{base_url}/api/hitl/verify-hitl-api/decisions"
        "/no-such-decision/answer"
    ),
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
        "Content-Type": "application/json",
    },
    "body": {
        "answered_by": "verify-bot",
    },
    "timeout_seconds": 30,
}

#: VP-013's assertions, verbatim from the plan.
VP013_ASSERTIONS = [
    {"name": "状态码 422", "status": 422},
]


#: VP-014's request, verbatim from the plan: the **plan summary** of a
#: plan that has no outstanding human decision.  The projection is the
#: one the front end's ``renderPendingDecisions`` consumes, so the
#: answer has to carry the block in the shape the UI reads directly.
VP014_REQUEST_TEMPLATE = {
    "method": "GET",
    "url": "{base_url}/api/plan/20260101-cleanup-sample/summary",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
    },
    "body": None,
    "timeout_seconds": 30,
}

#: VP-014's assertions, verbatim from the plan.
VP014_ASSERTIONS = [
    {"name": "状态码 200", "status": 200},
    {
        "name": "pending_decisions.count 为 0",
        "json_path": "$.pending_decisions.count",
        "equals": 0,
    },
    {
        "name": "pending_decisions.items 为空数组",
        "json_path": "$.pending_decisions.items",
        "length_equals": 0,
    },
]

#: VP-015's request, verbatim from the plan: the **plan summary** of a
#: plan that does not exist.  The projection has to say the plan is not
#: there — a 200 carrying an empty ``pending_decisions`` block would
#: answer a different question ("nothing is outstanding"), and an
#: operator reading ``count: 0`` would take a plan that is not there
#: for one that has nothing to do.
VP015_REQUEST_TEMPLATE = {
    "method": "GET",
    "url": "{base_url}/api/plan/verify-hitl-nonexistent/summary",
    "headers": {
        "Accept": "application/json",
        "X-PDT-Request": "1",
    },
    "body": None,
    "timeout_seconds": 30,
}

#: VP-015's assertions, verbatim from the plan.
VP015_ASSERTIONS = [
    {"name": "状态码 404", "status": 404},
]


def _free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _vp001(port: int) -> dict:
    """VP-001 with its placeholder resolved against *port*."""
    request = dict(VP001_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-001",
        "title": "服务数据面健康检查：/health/data 能读通 state.db",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP001_ASSERTIONS],
    }


def _vp002(port: int) -> dict:
    """VP-002 with its placeholder resolved against *port*."""
    request = dict(VP002_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-002",
        "title": "HITL 端点走统一 request_guard：缺 X-PDT-Request 必须 403",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP002_ASSERTIONS],
    }


def _vp003(port: int) -> dict:
    """VP-003 with its placeholder resolved against *port*."""
    request = dict(VP003_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-003",
        "title": "未知 plan 的决策列表返回空数组而不是 404",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP003_ASSERTIONS],
    }


def _vp004(port: int) -> dict:
    """VP-004 with its placeholder resolved against *port*."""
    request = dict(VP004_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-004",
        "title": "非法 status 过滤值必须 400，而不是静默返回空集",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP004_ASSERTIONS],
    }


def _vp005(port: int) -> dict:
    """VP-005 with its placeholder resolved against *port*."""
    request = dict(VP005_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-005",
        "title": "plan_id 路径边界：非法字符被 400 拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP005_ASSERTIONS],
    }


def _vp006(port: int) -> dict:
    """VP-006 with its placeholder resolved against *port*."""
    request = dict(VP006_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-006",
        "title": "auto 决策缺 default_option：400 auto_requires_default",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP006_ASSERTIONS],
    }


def _vp007(port: int) -> dict:
    """VP-007 with its placeholder resolved against *port*."""
    request = dict(VP007_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-007",
        "title": "空 options 的决策请求被拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP007_ASSERTIONS],
    }


def _vp008(port: int) -> dict:
    """VP-008 with its placeholder resolved against *port*."""
    request = dict(VP008_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-008",
        "title": "timeout_sec <= 0 的决策请求被拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP008_ASSERTIONS],
    }


def _vp009(port: int) -> dict:
    """VP-009 with its placeholder resolved against *port*."""
    request = dict(VP009_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-009",
        "title": "mode 取值是两词封闭集合：未知模式被 400 拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP009_ASSERTIONS],
    }


def _vp012(port: int) -> dict:
    """VP-012 with its placeholder resolved against *port*."""
    request = dict(VP012_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-012",
        "title": "回答不存在的决策返回 404，不产生任何写入",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP012_ASSERTIONS],
    }


def _vp013(port: int) -> dict:
    """VP-013 with its placeholder resolved against *port*."""
    request = dict(VP013_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-013",
        "title": "回答缺少 option 字段时被请求体校验拒绝（422）",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP013_ASSERTIONS],
    }


def _vp014(port: int) -> dict:
    """VP-014 with its placeholder resolved against *port*."""
    request = dict(VP014_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-014",
        "title": "plan summary 投影携带 pending_decisions{count, items}（PRD g）",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP014_ASSERTIONS],
    }


def _vp015(port: int) -> dict:
    """VP-015 with its placeholder resolved against *port*."""
    request = dict(VP015_REQUEST_TEMPLATE)
    request["url"] = request["url"].format(
        base_url=f"http://127.0.0.1:{port}",
    )
    return {
        "id": "VP-015",
        "title": "不存在的计划请求 summary 返回 404",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": request,
        "assertions": [dict(a) for a in VP015_ASSERTIONS],
    }


class _FakeApiHandler(BaseHTTPRequestHandler):
    """The api service's two endpoints, as VP-001 sees them."""

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            body = {"status": "ok"}
        elif self.path == "/health/data":
            body = {"status": "ok", "plan_routing_rows": 0}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeGuardApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-002 sees it.

    ``/health`` answers, so the service counts as usable; the decisions
    endpoint refuses a request that carries no ``X-PDT-Request`` header,
    exactly the way ``backend/request_guard.py`` refuses it — a 403 whose
    body names the header that is missing.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        payload = json.dumps({
            "error": "forbidden",
            "detail": (
                "missing X-PDT-Request header. This API is driven by the "
                "bundled UI or by a local script; a browser page from "
                "another origin cannot set this header without a CORS "
                "preflight, which this server never grants. Send "
                "'X-PDT-Request: 1'."
            ),
        }).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeDecisionsApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-003 sees it.

    The decisions endpoint answers a plan that never created a decision
    with a 200 and an **empty array** — the whole point of VP-003 is that
    "no decisions yet" must not read as "no such plan", so a 404 would
    be the wrong answer.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it: a request without the ``X-PDT-Request`` header is refused with
    a 403.  That makes the test prove the *header* really went out, and
    not only that the path was right.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/hitl/verify-hitl-unknown/decisions":
            if not self.headers.get("X-PDT-Request"):
                payload = json.dumps({
                    "error": "forbidden",
                    "detail": "missing X-PDT-Request header.",
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeInvalidStatusApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-004 sees it.

    ``status`` is a closed vocabulary of decision states, so a filter
    outside it is **refused** — a 400 whose body names the rule that
    refused the request (``error`` is ``invalid_status``) — rather than
    silently answered with the wrong set.  That is the whole criterion:
    a caller asking "what is outstanding" must not be told "nothing is"
    because it spelled the filter wrong.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went out
    and that the **vocabulary** is what refuses the request.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == (
            "/api/hitl/verify-hitl-unknown/decisions?status=PENDING"
        ):
            if not self.headers.get("X-PDT-Request"):
                payload = json.dumps({
                    "error": "forbidden",
                    "detail": "missing X-PDT-Request header.",
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = json.dumps({
                "error": "invalid_status",
                "detail": (
                    "status must be one of ['pending', 'answered', "
                    "'defaulted', 'paused', 'unroutable']; "
                    "got 'PENDING'"
                ),
            }).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeInvalidPlanIdApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-005 sees it.

    A plan id is a **safe leaf**: it has to survive being joined onto
    the plans root, so a value that is not a leaf is refused — a 400
    whose body names the rule that refused it (``error`` is
    ``Invalid plan id``) — instead of being turned into a path.  That is
    the whole criterion: the decisions endpoint must not become an
    arbitrary-path write point.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went out
    and that the **plan id** is what refuses the request.

    The path arrives percent-encoded (``bad%20id``), exactly as the plan
    spells it, and the service decodes it before it refuses it.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/hitl/bad%20id/decisions":
            if not self.headers.get("X-PDT-Request"):
                payload = json.dumps({
                    "error": "forbidden",
                    "detail": "missing X-PDT-Request header.",
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = json.dumps({
                "error": "Invalid plan id",
                "detail": (
                    "plan_id 'bad id' contains unsafe characters; only "
                    "[A-Za-z0-9._-] are allowed"
                ),
            }).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeAutoDefaultOptionApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-006 sees it.

    ``auto`` means "when the deadline passes, use the default answer",
    so an ``auto`` decision with no ``default_option`` has no way to
    end.  The service refuses it: a 400 whose body names the rule that
    refused it (``error`` is ``auto_requires_default``), so a caller
    can tell this refusal from another one.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.  And the answer depends on the **body**: a request that
    carried a ``default_option`` would be accepted instead of refused,
    so the test is discriminating — a request that lost its body on the
    way out cannot pass by accident.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/hitl/verify-hitl-auto/decisions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.headers.get("X-PDT-Request"):
            payload = json.dumps({
                "error": "forbidden",
                "detail": "missing X-PDT-Request header.",
            }).encode("utf-8")
            self.send_response(403)
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - a broken body is a 400
                body = {}
            mode = body.get("mode") or "auto"
            if mode == "auto" and body.get("default_option") is None:
                payload = json.dumps({
                    "error": "auto_requires_default",
                    "detail": (
                        "an auto decision needs a default_option to fall "
                        "back to when the deadline passes; none was given"
                    ),
                }).encode("utf-8")
                self.send_response(400)
            else:
                payload = json.dumps({"decision_id": "d-1"}).encode("utf-8")
                self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""




class _FakeEmptyOptionsApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-007 sees it.

    A decision with **no options** has no way to be answered, so the
    service refuses the question at the door: a 400 whose body names
    the rule that refused it (``error`` is ``invalid_request``), and
    nothing is written down.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.  And the answer depends on the **body**: only a request that
    really carried an empty ``options`` list is refused, so a request
    that lost its body on the way out cannot pass by accident.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/hitl/verify-hitl-api/decisions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.headers.get("X-PDT-Request"):
            payload = json.dumps({
                "error": "forbidden",
                "detail": "missing X-PDT-Request header.",
            }).encode("utf-8")
            self.send_response(403)
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - a broken body is a 400
                body = {}
            if body.get("options") == []:
                payload = json.dumps({
                    "error": "invalid_request",
                    "detail": (
                        "options must not be empty; a decision needs at "
                        "least one answer"
                    ),
                }).encode("utf-8")
                self.send_response(400)
            else:
                payload = json.dumps({"decision_id": "d-1"}).encode("utf-8")
                self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""



class _FakeInvalidTimeoutApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-008 sees it.

    A decision whose deadline is **zero** can never be answered in
    time, so the service refuses the question at the door: a 400 whose
    body names the rule that refused it (``error`` is
    ``invalid_timeout``), and nothing is written down.

    The endpoint is the same one VP-007 uses, so the answer has to be
    decided by the **body**: only a request that really carried a zero
    ``timeout_sec`` is refused, so a request that lost its body on the
    way out cannot pass by accident.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/hitl/verify-hitl-api/decisions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.headers.get("X-PDT-Request"):
            payload = json.dumps({
                "error": "forbidden",
                "detail": "missing X-PDT-Request header.",
            }).encode("utf-8")
            self.send_response(403)
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - a broken body is a 400
                body = {}
            timeout = body.get("timeout_sec")
            if isinstance(timeout, bool) or not isinstance(timeout, int):
                timeout = None
            if timeout is not None and timeout <= 0:
                payload = json.dumps({
                    "error": "invalid_timeout",
                    "detail": (
                        "timeout_sec must be a positive number of "
                        "seconds; got 0"
                    ),
                }).encode("utf-8")
                self.send_response(400)
            else:
                payload = json.dumps({"decision_id": "d-1"}).encode("utf-8")
                self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeUnknownModeApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-009 sees it.

    ``mode`` is a **closed vocabulary of two words** (``auto`` /
    ``strict``), so a mode outside it is **refused** — a 400 whose body
    names the rule that refused the request (``error`` is
    ``invalid_request``) — rather than silently degraded into ``auto``.
    That is the whole criterion: an ``auto`` decision answers itself
    when the deadline passes, so a caller who asked for a third mode has
    to be told "no" instead of being handed an answer it never chose.

    The endpoint is the same one VP-007 and VP-008 use, so the answer
    has to be decided by the **body**: only a request that really
    carried a mode outside the vocabulary is refused, so a request that
    lost its body on the way out cannot pass by accident.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/hitl/verify-hitl-api/decisions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.headers.get("X-PDT-Request"):
            payload = json.dumps({
                "error": "forbidden",
                "detail": "missing X-PDT-Request header.",
            }).encode("utf-8")
            self.send_response(403)
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - a broken body is a 400
                body = {}
            mode = body.get("mode")
            if mode is not None and mode not in ("auto", "strict"):
                payload = json.dumps({
                    "error": "invalid_request",
                    "detail": (
                        "mode must be one of ['auto', 'strict']; "
                        f"got {mode!r}"
                    ),
                }).encode("utf-8")
                self.send_response(400)
            else:
                payload = json.dumps({"decision_id": "d-1"}).encode("utf-8")
                self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeNotFoundDecisionApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-012 sees it.

    VP-012 asks the service to **answer** a decision that does not
    exist (``no-such-decision``).  The answer is a 404 whose body
    carries the error code ``not_found`` — and no decision entity,
    because there is no decision to report.  An unknown
    ``decision_id`` must never be taken for a success: the caller would
    walk away believing it answered a question nobody asked.

    The body is checked **first** (an answer without an ``option`` is a
    422), the way the service checks it, so the answer depends on the
    **body**: a request that lost it on the way out cannot pass by
    accident.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != (
            "/api/hitl/verify-hitl-api/decisions"
            "/no-such-decision/answer"
        ):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.headers.get("X-PDT-Request"):
            payload = json.dumps({
                "error": "forbidden",
                "detail": "missing X-PDT-Request header.",
            }).encode("utf-8")
            self.send_response(403)
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - a broken body is a 422
                body = {}
            if body.get("option") is None:
                payload = json.dumps({
                    "error": "invalid_answer",
                    "detail": (
                        "an answer needs an option; none was given"
                    ),
                }).encode("utf-8")
                self.send_response(422)
            else:
                payload = json.dumps({
                    "error": "not_found",
                    "detail": (
                        "no decision with id 'no-such-decision'"
                    ),
                }).encode("utf-8")
                self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeMissingOptionApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-013 sees it.

    VP-013 asks the service to **answer** a decision **without naming
    an option** — the body carries ``answered_by`` and nothing else.
    The request-body validation refuses it with a **422**, and it does
    so **before** the decision is looked up: the decision in the path
    does not exist either, and a 404 would mean the service checked
    the wrong thing first.

    The refusal depends on the **body**: an answer that carries an
    ``option`` gets past the validation and is refused with a 404
    instead.  A request that lost its body on the way out therefore
    cannot pass by accident.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.
    """

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != (
            "/api/hitl/verify-hitl-api/decisions"
            "/no-such-decision/answer"
        ):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.headers.get("X-PDT-Request"):
            payload = json.dumps({
                "error": "forbidden",
                "detail": "missing X-PDT-Request header.",
            }).encode("utf-8")
            self.send_response(403)
        else:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - a broken body is a 422
                body = {}
            if body.get("option") is None:
                # The shape the request-body validation produces for a
                # missing required field — and it names the field.
                payload = json.dumps({
                    "detail": [{
                        "type": "missing",
                        "loc": ["body", "option"],
                        "msg": "Field required",
                        "input": body,
                    }],
                }).encode("utf-8")
                self.send_response(422)
            else:
                payload = json.dumps({
                    "error": "not_found",
                    "detail": (
                        "no decision with id 'no-such-decision'"
                    ),
                }).encode("utf-8")
                self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


class _FakeSummaryApiHandler(BaseHTTPRequestHandler):
    """The api service as VP-014 sees it.

    The **plan summary** of a plan that has no outstanding human
    decision answers 200 and carries a ``pending_decisions`` block
    whose ``count`` is 0 and whose ``items`` is an empty array.  The
    block is always present: "nothing is outstanding" is an answer, not
    a missing key — a consumer that has to check for the key before it
    can read it has learned two things, and the front end's
    ``renderPendingDecisions`` consumes the block directly.

    The block is **computed** from the plan's decision table rather
    than written down, so its two halves cannot drift apart: a plan
    that does have a pending decision gets ``count`` 1 and that
    decision listed.  A service that answered with a fixed
    ``{"count": 0, "items": []}`` would still satisfy VP-014 — but one
    that dropped the block, or reported a count that disagrees with the
    list, would not.

    A plan that does not exist is a **404**, the way the real route
    refuses it: VP-014 asks about a plan this service knows, VP-015
    asks about one it deliberately does not.

    The endpoint is guarded the way ``backend/request_guard.py`` guards
    it, so the test proves the ``X-PDT-Request`` header really went
    out.
    """

    #: The plan this VP asks about — one with nothing outstanding.
    _VP_PLAN_ID = "20260101-cleanup-sample"

    #: The plans this service knows about.  The second one is waiting
    #: on a human, so the block's two halves can be checked against
    #: each other.
    _KNOWN_PLANS = ("20260101-cleanup-sample", "verify-hitl-waiting")

    #: The decisions each known plan is waiting on.  The VP's own plan
    #: is deliberately absent: "no decisions yet" is the answer the
    #: criterion is about.
    _PENDING_DECISIONS = {
        "verify-hitl-waiting": [
            {
                "decision_id": "d-1",
                "question": "还在等一个人回答的问题",
                "source": "verification",
                "created_at": "2026-10-24T00:00:00Z",
            },
        ],
    }

    def _pending_block(self, plan_id: str) -> dict:
        """The ``pending_decisions`` block for *plan_id*."""
        items = self._PENDING_DECISIONS.get(plan_id, [])
        return {"count": len(items), "items": list(items)}

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
        if self.path == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        prefix = "/api/plan/"
        suffix = "/summary"
        if self.path.startswith(prefix) and self.path.endswith(suffix):
            if not self.headers.get("X-PDT-Request"):
                payload = json.dumps({
                    "error": "forbidden",
                    "detail": "missing X-PDT-Request header.",
                }).encode("utf-8")
                self.send_response(403)
            else:
                plan_id = self.path[len(prefix):-len(suffix)]
                if plan_id in self._KNOWN_PLANS:
                    payload = json.dumps({
                        "plan_id": plan_id,
                        "state": "in_progress",
                        "pending_decisions": self._pending_block(plan_id),
                    }).encode("utf-8")
                    self.send_response(200)
                else:
                    payload = json.dumps({
                        "detail": "Plan not found",
                    }).encode("utf-8")
                    self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Keep the test output clean."""


def test_vp001_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """An unreachable service is ``infra``; a reachable one is judged on
    its assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (b) The service answers: the same request and the same assertions
 are judged by the original criteria, unchanged.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp001(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp001(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 200, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence


def test_vp002_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-002: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 403, and a
        body that names the header the request left out.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp002(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGuardApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp002(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 403, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The refusal has to name the header that is missing — naming it is
    # the second half of VP-002's criterion, and a body that stopped
    # naming it would otherwise pass unnoticed.
    assert "X-PDT-Request" in (evidence.get("response_body") or ""), evidence


def test_vp003_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-003: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 200, and a
        body that is an empty array.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp003(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDecisionsApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp003(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 200, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-003's criterion: the body is an **empty
    # array**, not a 404 and not an object.
    assert (evidence.get("response_body") or "").strip() == "[]", evidence


def test_vp004_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-004: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 400, and an
        error code that names the rule that refused the request.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp004(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeInvalidStatusApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp004(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 400, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-004's criterion: the body names the rule
    # that refused the request, so a caller can tell a bad filter from
    # a bad plan.
    assert "invalid_status" in (evidence.get("response_body") or ""), evidence


def test_vp005_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-005: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 400, and an
        error message that names the rule that refused the plan id.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp005(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeInvalidPlanIdApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp005(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 400, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-005's criterion: the body names the rule
    # that refused the plan id, so a caller can tell a bad id from a
    # plan that does not exist.
    assert "Invalid plan id" in (evidence.get("response_body") or ""), evidence


def test_vp006_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-006: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 400, and a
        refusal slug that names the rule that refused the question.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp006(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body, five rounds in a row: a
    #      verdict that says *which* environment failed and *why* it
    #      stayed down, so the next round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp006(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeAutoDefaultOptionApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp006(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 400, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-006's criterion: the body names the rule
    # that refused the question, so a caller can tell this refusal from
    # another one.
    assert "auto_requires_default" in (
        evidence.get("response_body") or ""
    ), evidence




def test_vp007_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-007: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 400, and a
        refusal slug that names the rule that refused the question.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp007(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body, five rounds in a row: a
    #      verdict that says *which* environment failed and *why* it
    #      stayed down, so the next round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp007(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeEmptyOptionsApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp007(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 400, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-007's criterion: the body names the rule
    # that refused the question, so a caller can tell this refusal from
    # another one.
    assert "invalid_request" in (
        evidence.get("response_body") or ""
    ), evidence



def test_vp008_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-008: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    2026-10-24 (repair-r3-23): VP-008 of the same plan failed the same
    way the seven before it did — five rounds in a row with only
    ``blocked_ports`` behind the verdict and not one of its two
    assertions executed.  The round-start gate decided once, at the
    round's start, that the port was dead, and handed that decision to
    every VP that touched it.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 400, and a
        refusal slug that names the rule that refused the question
        (``invalid_timeout``).
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp008(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body, five rounds in a row: a
    #      verdict that says *which* environment failed and *why* it
    #      stayed down, so the next round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp008(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeInvalidTimeoutApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp008(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 400, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-008's criterion: the body names the rule
    # that refused the question, so a caller can tell this refusal from
    # another one.
    assert "invalid_timeout" in (
        evidence.get("response_body") or ""
    ), evidence


def test_vp009_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-009: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    2026-10-24 (repair-r3-24): VP-009 of the same plan failed the same
    way the eight before it did — five rounds in a row with only
    ``blocked_ports`` behind the verdict and not one of its two
    assertions executed.  The round-start gate decided once, at the
    round's start, that the port was dead, and handed that decision to
    every VP that touched it.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 400, and a
        refusal that names the rule that refused the question
        (``invalid_request``), with the unknown mode **not** silently
        degraded into ``auto``.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp009(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body, five rounds in a row: a
    #      verdict that says *which* environment failed and *why* it
    #      stayed down, so the next round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp009(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeUnknownModeApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp009(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 400, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-009's criterion: the refusal names the rule
    # that refused the question, so a caller can tell this refusal from
    # another one.
    assert "invalid_request" in (
        evidence.get("response_body") or ""
    ), evidence


def test_vp012_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-012: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    2026-10-24 (repair-r3-25): VP-012 of the same plan failed the same
    way the nine before it did — five rounds in a row with only
    ``blocked_ports`` behind the verdict and not one of its two
    assertions executed.  The round-start gate decided once, at the
    round's start, that the port was dead, and handed that decision to
    every VP that touched it.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 404, and a
        refusal that names the rule that refused the answer
        (``not_found``), with the unknown ``decision_id`` never taken
        for a success.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp012(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body, five rounds in a row: a
    #      verdict that says *which* environment failed and *why* it
    #      stayed down, so the next round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp012(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeNotFoundDecisionApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp012(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 404, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 2, evidence
    # The second half of VP-012's criterion: the refusal names the rule
    # that refused the answer, so a caller can tell this refusal from
    # another one.
    assert "not_found" in (
        evidence.get("response_body") or ""
    ), evidence


def test_vp013_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-013: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    2026-10-24 (repair-r3-26): VP-013 of the same plan failed the same
    way the ones before it did — five rounds in a row with only
    ``blocked_ports`` behind the verdict and not one of its assertions
    executed.  The round-start gate decided once, at the round's start,
    that the port was dead, and handed that decision to every VP that
    touched it.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertion
        are judged by the original criteria, unchanged — a 422, because
        an answer that names no option is refused by the request-body
        validation before it can reach the store.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp013(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before giving up.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body, five rounds in a row: a
    #      verdict that says *which* environment failed and *why* it
    #      stayed down, so the next round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp013(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeMissingOptionApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp013(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 422, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 1, evidence
    # The second half of VP-013's criterion: the refusal comes from the
    # request-body validation, and it names the field that was missing —
    # a 422 from any other rule would not do.
    assert "option" in (
        evidence.get("response_body") or ""
    ), evidence


def test_vp014_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-014: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    2026-10-24 (repair-r3-27): VP-014 of the same plan failed the same
    way the ones before it did — the request never reached the service,
    so none of its three assertions ran, and the verdict said nothing
    about *which* environment failed.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 200 whose
        ``pending_decisions`` block says ``count`` 0 and ``items`` is
        empty, because that block is what the front end reads to know
        how many human decisions are waiting.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp014(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before it blocks.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body: a verdict that says *which*
    #      environment failed and *why* it stayed down, so the next
    #      round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp014(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _FakeSummaryApiHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp014(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 200, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 3, evidence
    # The second half of VP-014's criterion: the block is not merely
    # present, it says the right thing — a count of zero and an empty
    # list, which is what "nothing is outstanding" has to look like for
    # the front end to render it without a special case.
    body = json.loads(evidence.get("response_body") or "{}")
    assert body.get("pending_decisions") == {"count": 0, "items": []}, body


def test_vp015_api_transport_failure_is_infra(tmp_path, monkeypatch):
    """VP-015: an unreachable service is ``infra``; a reachable one is
    judged on its own assertions.

    2026-10-24 (repair-r3-28): VP-015 of the same plan failed the same
    way the ones before it did — the request never reached the service,
    so not one of its assertions ran, and the verdict said nothing
    about *which* environment failed.

    (a) The port has no listener and the service cannot be started:
        the verdict must name the environment as the cause — a
        ``signal``, the ``blocked_ports`` behind it, and the class
        ``infra``, which does not move the same-cause counter.

    (a2) The round-start gate — the path the production report actually
        took — hands out the same diagnosable verdict, so a round that
        starts against a dead service leaves a diagnosis behind instead
        of a bare port number.

    (b) The service answers: the same request and the same assertions
        are judged by the original criteria, unchanged — a 404, because
        "this plan does not exist" must not be answered with somebody
        else's projection.
    """
    from service_declaration import ServiceDeclaration
    from service_manager import ServiceRuntime, ServiceRuntimeMap
    from verification.failure_triage import (
        INFRA,
        classify_deterministic,
        disposition_for,
        is_counted,
    )
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # ------------------------------------------------------------------
    # (a) The declared port has no listener.
    # ------------------------------------------------------------------
    dead_port = _free_port()
    dead_decl = ServiceDeclaration(
        name="api",
        port=dead_port,
        start_cmd="exit 1",
        cwd=".",
        health_url=f"http://127.0.0.1:{dead_port}/health",
        ready_timeout_seconds=2,
        log_path="tmp/api.log",
    )

    orch = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    orch._service_declarations = {"api": dead_decl}

    ensure_calls: list = []

    def _unready_ensure_services(project_dir_arg, declarations, **kwargs):
        ensure_calls.append((project_dir_arg, sorted(declarations)))
        runtime_map = ServiceRuntimeMap()
        for name, decl in declarations.items():
            runtime_map.runtimes[name] = ServiceRuntime(
                name=name,
                port=decl.port,
                start_cmd=decl.start_cmd,
                cwd=decl.cwd,
                ready=False,
                detail="spawn failed: the service never came up",
            )
        return runtime_map

    monkeypatch.setattr(
        "service_manager.ensure_services", _unready_ensure_services,
    )

    verdict = orch._run_api_test_vp(_vp015(dead_port))

    assert verdict["status"] == "BLOCKED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("blocked_ports") == [dead_port], evidence

    signal = evidence.get("signal")
    assert signal, (
        f"an unreachable service must carry a machine-readable signal: "
        f"{evidence!r}"
    )
    # Tier A must know this signal by name — an unknown signal is a
    # ``None``, which sends the case to the semantic tier and lands on
    # the conservative default (``deliverable_defect``).
    assert classify_deterministic(signal, "") == INFRA, signal

    assert verdict.get("failure_class") == INFRA, verdict
    assert verdict.get("failure_class_source") == "deterministic", verdict

    # The class that counts must not count: an infra failure is worth
    # another try, not a repair task.
    assert disposition_for(INFRA) == "retry_not_counted"
    assert is_counted(INFRA) is False
    assert is_counted("deliverable_defect") is True

    # The guard has to try to bring the service up before it blocks.
    assert ensure_calls, (
        "the api_test guard must ask service_manager to start the "
        "declared service before it blocks the point"
    )

    # ------------------------------------------------------------------
    # (a2) The round-start gate — the path the production report
    #      actually took.  Its verdict was ``{"blocked_ports": [8010]}``
    #      with no status code and no body: a verdict that says *which*
    #      environment failed and *why* it stayed down, so the next
    #      round has something to act on.
    # ------------------------------------------------------------------
    gate_agent = Orchestrator(plan_dir=plan_dir, project_dir=project_dir)
    gate_agent._service_declarations = {"api": dead_decl}
    gate_agent._blocked_service_ports = {dead_port}

    gate_verdict = gate_agent._service_gate_verdict(_vp015(dead_port))

    assert gate_verdict is not None, (
        "a service that is down must block the point that needs it"
    )
    assert gate_verdict["status"] == "BLOCKED", gate_verdict
    gate_evidence = gate_verdict.get("evidence") or {}
    assert gate_evidence.get("signal") == "service_not_ready", gate_evidence
    assert gate_evidence.get("blocked_ports") == [dead_port], gate_evidence
    assert gate_evidence.get("service_detail"), (
        f"a blocked verdict must carry the diagnosis, not only a port: "
        f"{gate_evidence!r}"
    )
    assert gate_verdict.get("failure_class") == INFRA, gate_verdict
    assert gate_verdict.get("failure_class_source") == "deterministic", (
        gate_verdict
    )

    # ------------------------------------------------------------------
    # (b) The service answers.
    # ------------------------------------------------------------------
    seen: list[str] = []

    class _RecordingSummaryHandler(_FakeSummaryApiHandler):
        """The same service, plus the paths it was asked about."""

        def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler API
            seen.append(self.path)
            return super().do_GET()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _RecordingSummaryHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live_port = int(server.server_address[1])
        live_decl = ServiceDeclaration(
            name="api",
            port=live_port,
            start_cmd="exit 1",
            cwd=".",
            health_url=f"http://127.0.0.1:{live_port}/health",
            ready_timeout_seconds=2,
            log_path="tmp/api.log",
        )
        orch._service_declarations = {"api": live_decl}

        def _must_not_restart(*args, **kwargs):
            pytest.fail(
                "a live service must not be restarted by the api_test guard"
            )

        monkeypatch.setattr(
            "service_manager.ensure_services", _must_not_restart,
        )

        verdict = orch._run_api_test_vp(_vp015(live_port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert verdict["status"] == "PASSED", verdict
    evidence = verdict.get("evidence") or {}
    assert evidence.get("status_code") == 404, evidence
    assert evidence.get("assertions_failed") == [], evidence
    assert len(evidence.get("assertions_passed") or []) == 1, evidence
    # The original request really went out, and it was the plan that
    # does not exist that was asked about.
    assert "/api/plan/verify-hitl-nonexistent/summary" in seen, seen
    # The refusal is the real route's: a 404 whose body names the plan
    # that is not there.
    body = json.loads(evidence.get("response_body") or "{}")
    assert body.get("detail") == "Plan not found", body


def test_every_transport_signal_is_classified_as_infra():
    """No signal the runner emits may fall through to the default.

    ``classify_deterministic`` returns ``None`` for a signal it does
    not know, and the caller then hands the case to the semantic tier,
    which lands on ``deliverable_defect`` — the class that counts. So
    every transport signal the runner can produce needs an explicit
    mapping to ``infra``.
    """
    from verification import failure_triage
    from verification_api_runner import TRANSPORT_SIGNALS

    assert TRANSPORT_SIGNALS, "the runner must publish its signal names"
    for signal in sorted(TRANSPORT_SIGNALS):
        assert failure_triage.classify_deterministic(signal, "") == "infra", (
            f"transport signal {signal!r} must classify as infra"
        )