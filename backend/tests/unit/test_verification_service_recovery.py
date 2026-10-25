"""The api_test service gate must be recoverable, not a one-shot verdict.

2026-10-24 (repair-r3-16): the round-start preflight decided **once**
whether the plan's declared service was up, and the per-VP gate turned
that single decision into the round's verdict for every VP that touched
the port.  A service that came up later in the round was still BLOCKED,
and VP-001's request and assertions never ran at all — the report
carried ``blocked_ports`` and no status code, five rounds in a row.

So the gate has to try again before it blocks, and when it does block
it has to say **why**: the machine-readable ``signal`` the four-class
triage reads (``service_not_ready`` → infra → retry, not counted) and
the diagnosis the service manager already produced (exit code, first
error, log tail).
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The diagnosis a service that refused to come up produces.  The
#: shapes are the ones ``service_manager._failure_detail`` emits — exit
#: code, first error, log tail — because a verdict that drops them
#: leaves the next round with nothing but ``blocked_ports``.
FAILED_START_DETAIL = (
    "started (pid 4242, pgid 4242) but not ready after 5s; rc=1; "
    "first error at line 3: RuntimeError: port already in use; "
    "log tail: ... EADDRINUSE ..."
)

#: The configuration the acceptance run copies for VP-010's plan.  It
#: is the file that says which mode and which deadline the plan's
#: decisions carry, so the fake service reads it rather than repeating
#: its values: a fake that hard-codes ``auto``/1800 would go on
#: answering ``auto``/1800 after the fixture changed.
VP010_FIXTURE_CONFIG = (
    _BACKEND_DIR
    / "tests"
    / "fixtures"
    / "plans"
    / "verify-hitl-api"
    / "hitl_config.json"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Service:
    """A real HTTP service on a real port.

    The gate is about *reaching* a service, so the test uses one: the
    assertions run against a real response, and a green verdict means
    the request really was sent and the answer really was checked.
    """

    def __init__(self, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.paths: list[str] = []
        #: The raw body of every request that carried one, so a test can
        #: prove the body really went out and not just the path.
        self.bodies: list[bytes] = []
        #: The plan whose configuration owns the mode and the deadline
        #: of the decisions it is asked about.  ``None`` means "no plan
        #: configuration to read", and the fake then answers with the
        #: built-in defaults — the same ones
        #: ``hooks.config.load_for_plan`` gives a plan that stores no
        #: usable configuration.
        self.plan_dir: Path | None = None
        self._started = False
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", self.port), self._handler(),
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )

    def plan_mode_and_timeout(self) -> tuple:
        """The plan's own mode and deadline — the ones the answer carries.

        Read through ``hooks.config.load_for_plan``, the reader the
        production service uses, so the fake cannot drift from it: a
        decision's mode and deadline belong to the plan's configuration
        and not to the request that asked the question (PRD decision
        point 7).
        """
        if self.plan_dir is None:
            return ("auto", 1800)
        from hooks.config import load_for_plan

        config = load_for_plan(self.plan_dir)
        return (config["mode"], config["timeout_sec"])

    def _handler(self):
        paths = self.paths
        bodies = self.bodies
        #: The service itself — ``self`` inside the request handler is
        #: the request, not the service.
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's own name
                paths.append(self.path)
                if self.path in ("/health", "/health/data"):
                    body = json.dumps({"status": "ok"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/hitl/verify-hitl-unknown/decisions":
                    # VP-003's endpoint: a plan that never created a
                    # decision has an **empty list**, not a 404.  The
                    # point of the assertion is that the caller can tell
                    # "no decisions yet" from "no such plan", so the
                    # answer is a 200 with ``[]``.
                    body = b"[]"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == (
                    "/api/hitl/verify-hitl-unknown/decisions?status=PENDING"
                ):
                    # VP-004's endpoint: a ``status`` filter outside the
                    # closed five-word vocabulary is **refused**, not
                    # ignored — 400, with the rule that refused it named
                    # in the body.
                    body = json.dumps({
                        "error": "invalid_status",
                        "detail": (
                            "status must be one of ['pending', 'answered', "
                            "'defaulted', 'paused', 'unroutable']; "
                            "got 'PENDING'"
                        ),
                    }).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/hitl/bad%20id/decisions":
                    # VP-005's endpoint: a plan id that is not a safe
                    # leaf is **refused** — 400, with the rule that
                    # refused it named in the body.  The path arrives
                    # percent-encoded, exactly as the plan spells it;
                    # the service decodes it and refuses what it finds.
                    if not self.headers.get("X-PDT-Request"):
                        body = json.dumps({
                            "error": "forbidden",
                            "detail": "missing X-PDT-Request header.",
                        }).encode("utf-8")
                        self.send_response(403)
                    else:
                        body = json.dumps({
                            "error": "Invalid plan id",
                            "detail": (
                                "plan_id 'bad id' contains unsafe "
                                "characters; only [A-Za-z0-9._-] are "
                                "allowed"
                            ),
                        }).encode("utf-8")
                        self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/plan/20260101-cleanup-sample/summary":
                    # VP-014's endpoint: the **plan summary** of a plan
                    # that has no outstanding human decision.  The
                    # ``pending_decisions`` block is always present —
                    # "nothing is outstanding" is an answer, not a
                    # missing key — and its two halves agree: a count of
                    # zero and an empty list.
                    body = json.dumps({
                        "plan_id": "20260101-cleanup-sample",
                        "state": "in_progress",
                        "pending_decisions": {"count": 0, "items": []},
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/plan/verify-hitl-nonexistent/summary":
                    # VP-015's endpoint: the summary of a plan that does
                    # **not exist** is a 404 — the way the real route
                    # refuses it ("Plan not found").  A 200 with an
                    # empty ``pending_decisions`` block would answer a
                    # different question ("nothing is outstanding"), and
                    # an operator would read "this plan does not exist"
                    # as "this plan has nothing to do".
                    body = json.dumps({
                        "detail": "Plan not found",
                    }).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802 - http.server's own name
                paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                bodies.append(self.rfile.read(length) if length else b"")
                if (
                    self.path == "/api/hitl/verify-hitl-auto/decisions"
                    and self.headers.get("X-PDT-Request")
                ):
                    # VP-006's endpoint: an ``auto`` decision with no
                    # ``default_option`` has no way to end, so it is
                    # **refused** — 400, with the rule that refused it
                    # named in the body.  The answer depends on the body,
                    # so a request that lost ``default_option`` on the
                    # way out cannot pass by accident.
                    try:
                        sent = json.loads(bodies[-1].decode("utf-8"))
                    except Exception:  # noqa: BLE001 - a broken body is 400
                        sent = {}
                    if (sent.get("mode") or "auto") == "auto" and (
                        sent.get("default_option") is None
                    ):
                        body = json.dumps({
                            "error": "auto_requires_default",
                            "detail": (
                                "an auto decision needs a default_option "
                                "to fall back to when the deadline passes; "
                                "none was given"
                            ),
                        }).encode("utf-8")
                        self.send_response(400)
                    else:
                        body = json.dumps({
                            "decision_id": "d-1",
                        }).encode("utf-8")
                        self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if (
                    self.path == "/api/hitl/verify-hitl-api/decisions"
                    and self.headers.get("X-PDT-Request")
                ):
                    # VP-007's, VP-008's and VP-009's endpoint: a
                    # decision that could never be answered, or that
                    # asked a question nobody can interpret, is
                    # **refused at the door** — 400, with the rule that
                    # refused it named in the body.  The three are told
                    # apart by the **body**, exactly as
                    # ``backend/hitl/service.py`` tells them apart: the
                    # options are checked first, the mode second, the
                    # deadline third.  The answer depends on the body,
                    # so a request that lost it on the way out cannot
                    # pass by accident.
                    try:
                        sent = json.loads(bodies[-1].decode("utf-8"))
                    except Exception:  # noqa: BLE001 - a broken body is 400
                        sent = {}
                    timeout = sent.get("timeout_sec")
                    if isinstance(timeout, bool) or not isinstance(
                        timeout, int
                    ):
                        timeout = None
                    mode = sent.get("mode")
                    if sent.get("options") == []:
                        body = json.dumps({
                            "error": "invalid_request",
                            "detail": (
                                "options must not be empty; a decision "
                                "needs at least one answer"
                            ),
                        }).encode("utf-8")
                        self.send_response(400)
                    elif mode is not None and mode not in ("auto", "strict"):
                        # VP-009: ``mode`` is a two-word closed
                        # vocabulary, so a mode outside it is **refused**
                        # — and refused, not silently degraded into
                        # ``auto``.  A decision that quietly became an
                        # ``auto`` one would answer on its own, which
                        # is exactly what the caller asked not to
                        # happen.
                        body = json.dumps({
                            "error": "invalid_request",
                            "detail": (
                                "mode must be one of ['auto', 'strict']; "
                                f"got {mode!r}"
                            ),
                        }).encode("utf-8")
                        self.send_response(400)
                    elif timeout is not None and timeout <= 0:
                        # VP-008: a deadline of zero has already passed,
                        # so the question can never be answered in time.
                        body = json.dumps({
                            "error": "invalid_timeout",
                            "detail": (
                                "timeout_sec must be a positive number "
                                "of seconds; got 0"
                            ),
                        }).encode("utf-8")
                        self.send_response(400)
                    else:
                        # VP-010's endpoint: a decision that can work —
                        # options to answer, a default to fall back to,
                        # a deadline in the future.  It is created, and
                        # it waits: a question somebody can be asked of
                        # is ``pending`` until a human answers it.
                        #
                        # The plan's configuration owns the mode and
                        # the deadline, so the answer carries **those**
                        # and not the request's defaults — the request
                        # named neither.
                        mode, timeout = service.plan_mode_and_timeout()
                        body = json.dumps({
                            "decision_id": "d-1",
                            "status": "pending",
                            "mode": mode,
                            "timeout_sec": timeout,
                            "options": sent.get("options"),
                            "default_option": sent.get("default_option"),
                        }).encode("utf-8")
                        self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if (
                    self.path == (
                        "/api/hitl/verify-hitl-api/decisions"
                        "/no-such-decision/answer"
                    )
                    and self.headers.get("X-PDT-Request")
                ):
                    # VP-012's endpoint: a decision that **does not
                    # exist** cannot be answered.  The answer is a 404
                    # whose body carries the error code ``not_found``,
                    # and no decision entity — there is no decision to
                    # report.
                    #
                    # The body is checked **first** (an answer without
                    # an ``option`` is a 422), the way the service
                    # checks it, so the answer depends on the **body**:
                    # a request that lost it on the way out gets a 422
                    # and cannot pass by accident.
                    try:
                        sent = json.loads(bodies[-1].decode("utf-8"))
                    except Exception:  # noqa: BLE001 - a broken body is 422
                        sent = {}
                    if sent.get("option") is None:
                        body = json.dumps({
                            "error": "invalid_answer",
                            "detail": (
                                "an answer needs an option; none was given"
                            ),
                        }).encode("utf-8")
                        self.send_response(422)
                    else:
                        body = json.dumps({
                            "error": "not_found",
                            "detail": (
                                "no decision with id 'no-such-decision'"
                            ),
                        }).encode("utf-8")
                        self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # The request guard's refusal, word for word the shape
                # ``backend/request_guard.py`` produces: a 403 whose body
                # names the header that is missing.
                body = json.dumps({
                    "error": "forbidden",
                    "detail": (
                        "missing X-PDT-Request header. This API is driven "
                        "by the bundled UI or by a local script; a browser "
                        "page from another origin cannot set this header "
                        "without a CORS preflight, which this server never "
                        "grants. Send 'X-PDT-Request: 1'."
                    ),
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args, **kwargs):  # pragma: no cover
                pass

        return Handler

    def start(self) -> "_Service":
        self._thread.start()
        self._started = True
        return self

    def stop(self) -> None:
        # ``shutdown()`` waits for the serve_forever loop to acknowledge it,
        # so it may only be called once the loop is actually running —
        # otherwise it blocks forever.
        if self._started:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        try:
            self._server.server_close()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


def _make_agent(tmp_path: Path):
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _declaration(port: int):
    from service_declaration import ServiceDeclaration

    return ServiceDeclaration(
        name="api",
        port=port,
        start_cmd="python -m backend.server",
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=5,
        log_path="tmp/api_server.log",
    )


def _vp001(port: int) -> dict:
    """VP-001 as the plan declares it — request and assertions verbatim."""
    return {
        "id": "VP-001",
        "title": "服务数据面健康检查：/health/data 能读通 state.db",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/health/data",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "数据面状态为 ok", "json_path": "$.status", "equals": "ok"},
        ],
    }


def _vp002(port: int) -> dict:
    """VP-002 as the plan declares it — request and assertions verbatim.

    VP-002 asks the declared api service to create a HITL decision
    **without** the ``X-PDT-Request`` header, so the request guard has
    to refuse it with a 403 whose body names the missing header.  The
    request and the two assertions are the plan's own; a guard that
    stopped refusing, or a body that stopped naming the header, has to
    show up as a failed assertion rather than as a blocked point.
    """
    return {
        "id": "VP-002",
        "title": "HITL 端点走统一 request_guard：缺 X-PDT-Request 必须 403",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-guard/decisions"
            ),
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
        },
        "assertions": [
            {"name": "状态码 403", "status": 403},
            {
                "name": "403 明确点名缺失的请求头",
                "body_contains": "X-PDT-Request",
            },
        ],
    }


def _vp003(port: int) -> dict:
    """VP-003 as the plan declares it — request and assertions verbatim.

    VP-003 asks the declared api service for the decision list of a plan
    that never created one, and asserts that the answer is a 200 with an
    **empty array** — not a 404.  The distinction is the whole criterion:
    a 404 would make the caller read "no decisions yet" as "no such
    plan".

    Unlike VP-002 this request **does** carry the ``X-PDT-Request``
    header, so it is allowed to reach the business logic.
    """
    return {
        "id": "VP-003",
        "title": "未知 plan 的决策列表返回空数组而不是 404",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-unknown/decisions"
            ),
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "返回空数组", "json_path": "$", "length_equals": 0},
        ],
    }


def _vp004(port: int) -> dict:
    """VP-004 as the plan declares it — request and assertions verbatim.

    VP-004 asks the declared api service for the decision list narrowed
    by a ``status`` filter that is **outside** the closed five-word
    vocabulary (``PENDING`` is not one of them — the vocabulary is lower
    case), and asserts that the answer is a 400 whose body names the
    rule that refused it: ``error`` is ``invalid_status``.

    The refusal is the whole criterion: a filter nobody can interpret
    would silently return the wrong set, and a caller asking "what is
    outstanding" would be told "nothing is".  The request carries the
    ``X-PDT-Request`` header, so it reaches the business logic and the
    vocabulary is what refuses it — not the request guard.
    """
    return {
        "id": "VP-004",
        "title": "非法 status 过滤值必须 400，而不是静默返回空集",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-unknown/decisions?status=PENDING"
            ),
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 400", "status": 400},
            {
                "name": "错误码为 invalid_status",
                "json_path": "$.error",
                "equals": "invalid_status",
            },
        ],
    }


def _vp005(port: int) -> dict:
    """VP-005 as the plan declares it — request and assertions verbatim.

    VP-005 asks the declared api service for the decision list of a plan
    whose id is **not a safe leaf** — ``bad id``, percent-encoded into
    the path as ``bad%20id`` — and asserts that the answer is a 400
    whose body names the rule that refused it: ``error`` is
    ``Invalid plan id``.

    The refusal is the whole criterion: a plan id that reached the
    filesystem unchecked would turn the decisions endpoint into an
    arbitrary-path write point, and a caller that spelled a plan id
    wrongly has to be told so instead of being handed somebody else's
    decisions.
    """
    return {
        "id": "VP-005",
        "title": "plan_id 路径边界：非法字符被 400 拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/bad%20id/decisions"
            ),
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 400", "status": 400},
            {
                "name": "错误信息为 Invalid plan id",
                "json_path": "$.error",
                "equals": "Invalid plan id",
            },
        ],
    }


def _vp006(port: int) -> dict:
    """VP-006 as the plan declares it — request and assertions verbatim.

    VP-006 asks the declared api service to **create** a decision whose
    mode is ``auto`` and whose ``default_option`` is missing, and
    asserts that the answer is a 400 whose body names the rule that
    refused it: ``error`` is ``auto_requires_default``.

    The refusal is the whole criterion: an ``auto`` decision means "when
    the deadline passes, use the default answer", so a decision with no
    default has no way to end.  Storing it would leave a question that
    can never resolve, so the service refuses it instead.
    """
    return {
        "id": "VP-006",
        "title": "auto 决策缺 default_option：400 auto_requires_default",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-auto/decisions"
            ),
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
        },
        "assertions": [
            {"name": "状态码 400", "status": 400},
            {
                "name": "拒绝原因为 auto_requires_default",
                "json_path": "$.error",
                "equals": "auto_requires_default",
            },
        ],
    }


def _vp007(port: int) -> dict:
    """VP-007 as the plan declares it — request and assertions verbatim.

    VP-007 asks the declared api service to **create** a decision whose
    option set is **empty**, and asserts that the answer is a 400 whose
    body names the rule that refused it: ``error`` is
    ``invalid_request``.

    The refusal is the whole criterion: a decision with no options has
    no way to be answered, so the service refuses the question at the
    door — before anything is written down.  A caller that spelled the
    options wrong has to be told so instead of being handed a question
    nobody can ever settle.
    """
    return {
        "id": "VP-007",
        "title": "空 options 的决策请求被拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-api/decisions"
            ),
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
        },
        "assertions": [
            {"name": "状态码 400", "status": 400},
            {
                "name": "错误码为 invalid_request",
                "json_path": "$.error",
                "equals": "invalid_request",
            },
        ],
    }


def _vp008(port: int) -> dict:
    """VP-008 as the plan declares it — request and assertions verbatim.

    VP-008 asks the declared api service to **create** a decision whose
    deadline is **zero**, and asserts that the answer is a 400 whose
    body names the rule that refused it: ``error`` is
    ``invalid_timeout``.

    The refusal is the whole criterion: a decision whose deadline has
    already passed can never be answered in time, so it must not be
    stored at all.  A caller that spelled the deadline wrong has to be
    told so instead of being handed a question that is dead on arrival.

    The endpoint is the same one VP-007 uses — the two are told apart
    by the **body**, exactly as ``backend/hitl/service.py`` tells them
    apart (the options first, the deadline second).
    """
    return {
        "id": "VP-008",
        "title": "timeout_sec <= 0 的决策请求被拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-api/decisions"
            ),
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
        },
        "assertions": [
            {"name": "状态码 400", "status": 400},
            {
                "name": "拒绝原因为 invalid_timeout",
                "json_path": "$.error",
                "equals": "invalid_timeout",
            },
        ],
    }


def _vp009(port: int) -> dict:
    """VP-009 as the plan declares it — request and assertions verbatim.

    VP-009 asks the declared api service to **create** a decision whose
    ``mode`` is **outside the closed two-word vocabulary**
    (``auto`` / ``strict``), and asserts that the answer is a 400 whose
    body names the rule that refused it: ``error`` is
    ``invalid_request``.

    The refusal is the whole criterion: a mode nobody can interpret
    must not be **silently degraded** into ``auto`` — an ``auto``
    decision answers itself when the deadline passes, so a caller who
    asked for something else would get an answer it never chose.

    The endpoint is the same one VP-007 and VP-008 use — the three are
    told apart by the **body**, exactly as ``backend/hitl/service.py``
    tells them apart (the options first, the mode second, the deadline
    third).
    """
    return {
        "id": "VP-009",
        "title": "mode 取值是两词封闭集合：未知模式被 400 拒绝",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-api/decisions"
            ),
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
        },
        "assertions": [
            {"name": "状态码 400", "status": 400},
            {
                "name": "错误码为 invalid_request",
                "json_path": "$.error",
                "equals": "invalid_request",
            },
        ],
    }


def _vp010(port: int) -> dict:
    """VP-010 as the plan declares it — request and assertions verbatim.

    VP-010 asks the declared api service to **create** a decision on a
    plan whose configuration says ``auto``/1800, and asserts that the
    answer carries the plan's mode and deadline, a generated
    ``decision_id``, a ``pending`` status and the default answer it was
    given.

    The request names neither the mode nor the deadline — that is the
    point of the criterion: those two belong to the **plan**, and the
    request's own defaults must not reach the stored decision (PRD
    decision point 7).
    """
    return {
        "id": "VP-010",
        "title": "auto 决策正常创建：pending + 配置冻结 mode/timeout",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-api/decisions"
            ),
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
                "Content-Type": "application/json",
            },
            "body": {
                "question": "是否按默认方案继续？",
                "options": ["approve", "reject"],
                "default_option": "approve",
                "source": "verification",
            },
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {
                "name": "决策状态为 pending",
                "json_path": "$.status",
                "equals": "pending",
            },
            {
                "name": "decision_id 已生成",
                "json_path": "$.decision_id",
                "exists": True,
            },
            {
                "name": "mode 冻结为 auto",
                "json_path": "$.mode",
                "equals": "auto",
            },
            {
                "name": "timeout 取自计划配置 1800",
                "json_path": "$.timeout_sec",
                "equals": 1800,
            },
            {
                "name": "default_option 原样保存",
                "json_path": "$.default_option",
                "equals": "approve",
            },
        ],
    }


def _vp012(port: int) -> dict:
    """VP-012 as the plan declares it — request and assertions verbatim.

    VP-012 asks the declared api service to **answer** a decision that
    does not exist (``no-such-decision``) and asserts that the answer
    is a 404 whose body carries the error code ``not_found``.

    The refusal is the whole criterion: an unknown ``decision_id`` must
    not be taken for a success — a write that answered a question
    nobody asked, and a caller that walks away believing it answered
    one.

    The request carries a real answer body, so the answer depends on
    the **body** — a request that lost it on the way out is refused
    with a 422 instead, and cannot pass by accident.
    """
    return {
        "id": "VP-012",
        "title": "回答不存在的决策返回 404，不产生任何写入",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-api/decisions"
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
        },
        "assertions": [
            {"name": "状态码 404", "status": 404},
            {
                "name": "错误码为 not_found",
                "json_path": "$.error",
                "equals": "not_found",
            },
        ],
    }


def _vp013(port: int) -> dict:
    """VP-013 as the plan declares it — request and assertions verbatim.

    VP-013 asks the declared api service to **answer** a decision
    **without naming an option**, and asserts that the request body
    validation refuses it with a 422.

    The criterion is about *where* the refusal happens: the body is
    validated **before** the decision is looked up, so an answer with
    no ``option`` is a 422 even though the decision named in the path
    does not exist either.  A service that looked the decision up
    first would answer 404, and the criterion — "an empty answer must
    not reach the store" — would never be checked at all.

    The request and the single assertion are the plan's own.
    """
    return {
        "id": "VP-013",
        "title": "回答缺少 option 字段时被请求体校验拒绝（422）",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "POST",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/hitl/verify-hitl-api/decisions"
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
        },
        "assertions": [
            {"name": "状态码 422", "status": 422},
        ],
    }


def _vp014(port: int) -> dict:
    """VP-014 as the plan declares it — request and assertions verbatim.

    VP-014 asks the declared api service for the **plan summary** of a
    plan that has no outstanding human decision, and asserts that the
    answer is a 200 whose body carries a ``pending_decisions`` block
    whose ``count`` is 0 and whose ``items`` is an empty array.

    The criterion is about the *shape* of that block: the front end's
    ``renderPendingDecisions`` consumes it directly, so a block that
    was missing, or whose two halves disagreed, would leave the
    operator unable to tell "nothing is waiting" from "the projection
    is broken".  An empty block is still an answer — "nothing is
    outstanding" — and the count has to say the same thing as the list.

    The request and the three assertions are the plan's own.
    """
    return {
        "id": "VP-014",
        "title": "plan summary 投影携带 pending_decisions{count, items}（PRD g）",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/plan/20260101-cleanup-sample/summary"
            ),
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
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
        ],
    }


def _vp015(port: int) -> dict:
    """VP-015 as the plan declares it — request and assertions verbatim.

    VP-015 asks the declared api service for the **plan summary** of a
    plan that **does not exist**, and asserts a 404.  The criterion is
    the resource's existence contract: "this plan does not exist" must
    not be dressed up as "this plan has nothing outstanding".  A 200
    carrying an empty ``pending_decisions`` block would answer a
    different question, and an operator reading "count: 0" would take
    a plan that is not there for one that has nothing to do.

    The request and the single assertion are the plan's own.
    """
    return {
        "id": "VP-015",
        "title": "不存在的计划请求 summary 返回 404",
        "verification_method": "api_test",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": (
                f"http://127.0.0.1:{port}"
                "/api/plan/verify-hitl-nonexistent/summary"
            ),
            "headers": {
                "Accept": "application/json",
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 404", "status": 404},
        ],
    }


def _runtime_map(port: int, *, ready: bool, detail: str = ""):
    import service_manager as sm

    return sm.ServiceRuntimeMap(runtimes={
        "api": sm.ServiceRuntime(
            name="api",
            port=port,
            pid=4242,
            origin="spawned" if ready else "failed",
            ready=ready,
            start_cmd="python -m backend.server",
            detail=detail or (
                f"started (pid 4242); ready within 5s" if ready else ""
            ),
        ),
    })


def _stub_probe(monkeypatch, state: dict) -> None:
    """Make the port probe deterministic — and *stateful*.

    The probe shells out to ``lsof``/``ps``; a machine without ``lsof``
    would answer "nothing is listening" for a service that is plainly
    up, and the test would then be testing the machine instead of the
    gate.

    The state is mutable because the interesting moment is the
    **transition**: the port is not listening when the round starts, and
    it is listening once the gate's recovery has started the service.
    """
    import service_freshness

    def fake_probe(port: int):
        listening = bool(state.get("listening"))
        return service_freshness.PortProbe(
            port=port, listening=listening,
            pids=[4242] if listening else [],
            cmdlines=["python -m backend.server"] if listening else [],
        )

    monkeypatch.setattr(service_freshness, "probe_port", fake_probe)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_vp001_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """A service that comes up on the retry must run the VP, not block it.

    The round-start preflight said "not ready"; by the time the gate
    looks, the service is there.  The gate has to notice, and the VP's
    **own** request and assertions have to run — a verdict that says
    PASSED because the gate let it through proves nothing, so the
    assertions are checked against a real service.
    """
    import service_manager as sm

    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp001(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 200
    assert verdict["evidence"]["assertions_failed"] == []
    # The original request really went out — the service saw it.
    assert "/health/data" in service.paths
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls


def test_vp001_gate_blocks_with_a_diagnosable_verdict(
    tmp_path: Path, monkeypatch,
):
    """A service that cannot come up blocks — but says why.

    The verdict has to carry the machine-readable signal the four-class
    triage reads (infra: retry, do not count) and the diagnosis the
    service manager already produced.  A verdict with only
    ``blocked_ports`` is what made this fail five rounds in a row with
    nothing to act on.
    """
    import service_manager as sm

    agent = _make_agent(tmp_path)
    port = 8010
    agent._service_declarations = {"api": _declaration(port)}
    agent._blocked_service_ports = {port}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            port, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    verdict = agent._service_gate_verdict(_vp001(port))

    assert verdict is not None
    assert verdict["status"] == "BLOCKED"
    evidence = verdict["evidence"]
    assert evidence["signal"] == "service_not_ready"
    assert evidence["blocked_ports"] == [8010]
    assert evidence["service_detail"]
    assert "rc=1" in evidence["service_detail"]
    assert verdict["failure_class"] == "infra"


def test_two_vps_hitting_the_gate_start_the_service_once(
    tmp_path: Path, monkeypatch,
):
    """The recovery is single-flight per port.

    Sixteen VPs touch the same declared service.  Without the
    single-flight each of them starts its own copy, and the copies
    fight over the port — which is how "the service can start" turns
    into "the service cannot start".
    """
    import service_manager as sm

    agent = _make_agent(tmp_path)
    port = _free_port()
    agent._service_declarations = {"api": _declaration(port)}
    agent._blocked_service_ports = {port}
    _stub_probe(monkeypatch, {"listening": False})

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        verdicts.append(agent._service_gate_verdict(_vp001(port)))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert verdicts == [None, None]
    assert len(calls) == 1, calls


def test_vp002_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-002 gets the same three answers VP-001 does.

    2026-10-24 (repair-r3-17): VP-002 of the same plan failed the same
    way VP-001 did — five rounds in a row, ``blocked_ports`` and nothing
    else, not one of its two assertions executed.  The gate was a
    one-shot decision taken at the round's start, so a service that came
    up later in the round still blocked the point.

    So, for VP-002:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (403, and a body naming
      the missing header);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp002(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 403, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it.
    assert "/api/hitl/verify-hitl-guard/decisions" in service.paths
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp002(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp002(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp003_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-003 gets the same three answers VP-001 and VP-002 do.

    2026-10-24 (repair-r3-18): VP-003 of the same plan failed the same
    way the two before it did — five rounds in a row, ``blocked_ports``
    and nothing else, not one of its two assertions executed.  The gate
    was a one-shot decision taken at the round's start, so a service
    that came up later in the round still blocked the point.

    So, for VP-003:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (200, and a body that is
      an empty array);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp003(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 200, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it.
    assert "/api/hitl/verify-hitl-unknown/decisions" in service.paths
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp003(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp003(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp004_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-004 gets the same three answers VP-001..VP-003 get.

    2026-10-24 (repair-r3-19): VP-004 of the same plan failed the same
    way the three before it did — five rounds in a row,
    ``blocked_ports`` and nothing else, not one of its two assertions
    executed.  The gate was a one-shot decision taken at the round's
    start, so a service that came up later in the round still blocked
    the point.

    So, for VP-004:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (400, and a body whose
      ``error`` is ``invalid_status``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp004(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 400, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it, with
    # the query string the criterion is about.
    assert (
        "/api/hitl/verify-hitl-unknown/decisions?status=PENDING"
        in service.paths
    ), service.paths
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp004(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp004(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp005_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-005 gets the same three answers VP-001..VP-004 get.

    2026-10-24 (repair-r3-20): VP-005 of the same plan failed the same
    way the four before it did — five rounds in a row,
    ``blocked_ports`` and nothing else, not one of its two assertions
    executed.  The gate was a one-shot decision taken at the round's
    start, so a service that came up later in the round still blocked
    the point.

    So, for VP-005:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (400, and a body whose
      ``error`` is ``Invalid plan id``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp005(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 400, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it, with
    # the percent-encoded plan id the criterion is about.
    assert "/api/hitl/bad%20id/decisions" in service.paths, service.paths
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp005(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp005(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp006_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-006 gets the same three answers VP-001..VP-005 get.

    2026-10-24 (repair-r3-21): VP-006 of the same plan failed the same
    way the five before it did — five rounds in a row,
    ``blocked_ports`` and nothing else, not one of its two assertions
    executed.  The gate was a one-shot decision taken at the round's
    start, so a service that came up later in the round still blocked
    the point.

    So, for VP-006:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (400, and a body whose
      ``error`` is ``auto_requires_default``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp006(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 400, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it, with
    # the body that has no ``default_option`` the criterion is about.
    assert "/api/hitl/verify-hitl-auto/decisions" in service.paths, (
        service.paths
    )
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    assert sent.get("default_option") is None, sent
    assert sent.get("mode") in (None, "auto"), sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp006(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp006(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp007_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-007 gets the same three answers VP-001..VP-006 get.

    2026-10-24 (repair-r3-22): VP-007 of the same plan failed the same
    way the six before it did — five rounds in a row,
    ``blocked_ports`` and nothing else, not one of its two assertions
    executed.  The gate was a one-shot decision taken at the round's
    start, so a service that came up later in the round still blocked
    the point.

    So, for VP-007:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (400, and a body whose
      ``error`` is ``invalid_request``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp007(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 400, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it, with
    # the empty option set the criterion is about.
    assert "/api/hitl/verify-hitl-api/decisions" in service.paths, (
        service.paths
    )
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    assert sent.get("options") == [], sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp007(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp007(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp008_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-008 gets the same three answers VP-001..VP-007 get.

    2026-10-24 (repair-r3-23): VP-008 of the same plan failed the same
    way the seven before it did — five rounds in a row,
    ``blocked_ports`` and nothing else, not one of its two assertions
    executed.  The gate was a one-shot decision taken at the round's
    start, so a service that came up later in the round still blocked
    the point.

    So, for VP-008:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (400, and a body whose
      ``error`` is ``invalid_timeout``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp008(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 400, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it, with
    # the zero deadline the criterion is about.
    assert "/api/hitl/verify-hitl-api/decisions" in service.paths, (
        service.paths
    )
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    assert sent.get("timeout_sec") == 0, sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp008(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp008(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp009_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-009 gets the same three answers VP-001..VP-008 get.

    2026-10-24 (repair-r3-24): VP-009 of the same plan failed the same
    way the eight before it did — five rounds in a row, a connection
    refused and nothing else, not one of its two assertions executed.
    The gate was a one-shot decision taken at the round's start, so a
    service that came up later in the round still blocked the point.

    So, for VP-009:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (400, and a body whose
      ``error`` is ``invalid_request``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp009(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 400, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw it, with
    # the unknown mode the criterion is about.
    assert "/api/hitl/verify-hitl-api/decisions" in service.paths, (
        service.paths
    )
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    assert sent.get("mode") == "someday", sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp009(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp009(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp010_gate_recovers_then_pending(tmp_path: Path, monkeypatch):
    """VP-010 gets the same three answers VP-001..VP-009 get.

    2026-10-24 (repair-r3-29): VP-010 of the same plan failed the same
    way the nine before it did — five rounds in a row,
    ``blocked_ports`` and nothing else, not one of its six assertions
    executed.  The gate was a one-shot decision taken at the round's
    start, so a service that came up later in the round still blocked
    the point.

    So, for VP-010:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (200, and a body that says
      ``pending``, ``auto``, 1800 and ``approve``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # The plan whose configuration owns the mode and the deadline the
    # answer carries: the very fixture the acceptance run copies.
    plan_dir = tmp_path / "plans" / "verify-hitl-api"
    plan_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(VP010_FIXTURE_CONFIG, plan_dir / "hitl_config.json")

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    service.plan_dir = plan_dir
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp010(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 200, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 6, verdict
    # The original request really went out — the service saw it, with
    # the default answer the criterion is about.
    assert "/api/hitl/verify-hitl-api/decisions" in service.paths, (
        service.paths
    )
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    assert sent.get("default_option") == "approve", sent
    # The request named neither the mode nor the deadline: those two
    # came from the plan's configuration, which is what the criterion
    # is about.
    assert "mode" not in sent, sent
    assert "timeout_sec" not in sent, sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp010(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp010(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp012_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-012 gets the same three answers VP-001..VP-009 get.

    2026-10-24 (repair-r3-25): VP-012 of the same plan failed the same
    way the nine before it did — five rounds in a row, a verdict that
    carried nothing but ``blocked_ports``, and not one of its two
    assertions executed.  The gate was a one-shot decision taken at the
    round's start, so a service that came up later in the round still
    blocked the point.

    So, for VP-012:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (404, and a body whose
      ``error`` is ``not_found``);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp012(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 404, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 2, verdict
    # The original request really went out — the service saw the answer
    # of the decision that does not exist.
    assert (
        "/api/hitl/verify-hitl-api/decisions/no-such-decision/answer"
        in service.paths
    ), service.paths
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    assert sent.get("option") == "approve", sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp012(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp012(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp013_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-013 gets the same three answers VP-001..VP-012 get.

    2026-10-24 (repair-r3-26): VP-013 of the same plan failed the same
    way the ones before it did — a verdict that carried nothing but
    ``blocked_ports``, and not one of its assertions executed.  The gate
    was a one-shot decision taken at the round's start, so a service
    that came up later in the round still blocked the point.

    So, for VP-013:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (422, the request-body
      validation refusing an answer that names no option);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp013(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 422, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 1, verdict
    # The original request really went out — the service saw the answer
    # that names no option.
    assert (
        "/api/hitl/verify-hitl-api/decisions/no-such-decision/answer"
        in service.paths
    ), service.paths
    assert service.bodies, "the POST really carried a body"
    sent = json.loads(service.bodies[-1].decode("utf-8"))
    # The body is the one the plan declares: an answer that names **no**
    # option.  A body that silently grew an ``option`` would make the
    # 422 the service's answer to something else entirely.
    assert sent.get("option") is None, sent
    assert sent.get("answered_by") == "verify-bot", sent
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp013(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp013(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


def test_vp015_gate_recovers_before_blocking(tmp_path: Path, monkeypatch):
    """VP-015 gets the same three answers VP-001..VP-013 get.

    2026-10-24 (repair-r3-28): VP-015 of the same plan failed the same
    way the ones before it did — a verdict that carried nothing but
    ``blocked_ports``, and not one of its assertions executed.  The gate
    was a one-shot decision taken at the round's start, so a service
    that came up later in the round still blocked the point.

    So, for VP-015:

    * the round starts with the service down and the gate's recovery
      brings it up → the VP's **own** request and assertions run against
      the real service and decide the verdict (404, the plan that does
      not exist refused instead of answered with somebody else's
      projection);
    * a service that never comes up still blocks — but the verdict says
      why: the machine-readable ``signal`` the four-class triage reads
      (``service_not_ready`` → infra → retry, not counted) and the
      diagnosis the service manager already produced;
    * two VPs that arrive together share **one** start attempt — a dozen
      start attempts do not make a service come up, they make a dozen
      copies fight over the port.
    """
    import service_manager as sm

    # --- (1) the service comes up on the retry: the VP runs ----------
    agent = _make_agent(tmp_path)
    port = _free_port()
    service = _Service(port)
    agent._service_declarations = {"api": _declaration(port)}
    # What the round-start preflight concluded.
    agent._blocked_service_ports = {port}
    state = {"listening": False}
    _stub_probe(monkeypatch, state)

    calls: list[list[str]] = []

    def fake_ensure(project_dir, declarations, **kwargs):
        calls.append(sorted(declarations))
        state["listening"] = True
        service.start()
        return _runtime_map(port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", fake_ensure)

    try:
        vp = _vp015(port)

        gate_verdict = agent._service_gate_verdict(vp)
        assert gate_verdict is None, (
            "the gate blocked a service that was up: "
            f"{gate_verdict}"
        )

        verdict = agent._run_api_test_vp(vp)
    finally:
        service.stop()

    assert verdict["status"] == "PASSED", verdict
    assert verdict["evidence"]["status_code"] == 404, verdict
    assert verdict["evidence"]["assertions_failed"] == [], verdict
    assert len(verdict["evidence"]["assertions_passed"]) == 1, verdict
    # The original request really went out — the service saw the plan
    # that does not exist and refused it.
    assert (
        "/api/plan/verify-hitl-nonexistent/summary" in service.paths
    ), service.paths
    # One start attempt: the gate did not ask twice.
    assert len(calls) == 1, calls

    # --- (2) the service never comes up: blocked, but diagnosable ----
    blocked_agent = _make_agent(tmp_path)
    blocked_agent._service_declarations = {"api": _declaration(8010)}
    blocked_agent._blocked_service_ports = {8010}
    _stub_probe(monkeypatch, {"listening": False})

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )

    blocked = blocked_agent._service_gate_verdict(_vp015(8010))

    assert blocked is not None
    assert blocked["status"] == "BLOCKED", blocked
    evidence = blocked["evidence"]
    assert evidence["signal"] == "service_not_ready", evidence
    assert evidence["blocked_ports"] == [8010], evidence
    assert evidence["service_detail"], evidence
    assert "rc=1" in evidence["service_detail"], evidence
    assert blocked["failure_class"] == "infra", blocked

    # --- (3) two VPs arriving together: one start attempt ------------
    shared_agent = _make_agent(tmp_path)
    shared_port = _free_port()
    shared_agent._service_declarations = {"api": _declaration(shared_port)}
    shared_agent._blocked_service_ports = {shared_port}
    _stub_probe(monkeypatch, {"listening": False})

    spawns: list[int] = []

    def slow_ensure(project_dir, declarations, **kwargs):
        spawns.append(shared_port)
        time.sleep(0.3)  # the window the other VP would slip into
        return _runtime_map(shared_port, ready=True)

    monkeypatch.setattr(sm, "ensure_services", slow_ensure)

    gate_verdicts: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        gate_verdicts.append(
            shared_agent._service_gate_verdict(_vp015(shared_port))
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert gate_verdicts == [None, None], gate_verdicts
    assert len(spawns) == 1, spawns


# ---------------------------------------------------------------------------
# ensure_services — one spawn per port
# ---------------------------------------------------------------------------


def test_concurrent_ensure_services_spawns_one_process_per_port(
    tmp_path: Path, monkeypatch,
):
    """Two callers, one port, one process.

    ``ensure_services`` is called from the preflight and from the
    per-VP recovery, and the round runs its VPs on a thread pool.  The
    callers have to serialise per port: the second one has to find the
    service the first one started and adopt it, not start a second
    copy on top of it.
    """
    import service_manager as sm
    from service_freshness import PortProbe

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    decl = _declaration(8765)

    started: list[int] = []
    state = {"listening": False}

    def fake_probe(port: int):
        return PortProbe(
            port=port,
            listening=state["listening"],
            pids=[4242] if state["listening"] else [],
            cmdlines=["python -m backend.server"] if state["listening"] else [],
        )

    def fake_start(declaration, project_dir, **kwargs):
        started.append(declaration.port)
        time.sleep(0.3)
        state["listening"] = True
        return sm.ServiceRuntime(
            name=declaration.name,
            port=declaration.port,
            pid=4242,
            origin="spawned",
            ready=True,
            detail="started (pid 4242); ready within 5s",
        )

    monkeypatch.setattr(sm, "probe_port", fake_probe)
    monkeypatch.setattr(sm, "start_service", fake_start)

    barrier = threading.Barrier(2)
    results: list[object] = []

    def worker() -> None:
        barrier.wait()
        results.append(
            sm.ensure_services(project, {"api": decl}, round_number=1)
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(started) == 1, started
    assert all(m.ready_names == ["api"] for m in results)


# ---------------------------------------------------------------------------
# The preflight has to say why a service did not come up
# ---------------------------------------------------------------------------


def test_preflight_event_carries_the_unready_service_detail(
    tmp_path: Path, monkeypatch,
):
    """The round-start verdict has to be readable after the round.

    The diagnosis used to stop in a ``logger.warning`` and die with the
    process; the next round inherited nothing but a port number.
    """
    import service_manager as sm

    agent = _make_agent(tmp_path)
    agent._normalize_service_declarations({
        "services": [{
            "name": "api",
            "port": 8010,
            "start_cmd": "python -m backend.server",
            "cwd": ".",
            "health_url": "http://127.0.0.1:8010/health",
            "log_path": "tmp/api_server.log",
        }],
        "verification_points": [],
    })
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )
    monkeypatch.setattr(
        sm, "ensure_services",
        lambda *a, **k: _runtime_map(
            8010, ready=False, detail=FAILED_START_DETAIL,
        ),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    logged: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        agent, "_safe_log_freshness_event",
        lambda vp_id, event_type, data: logged.append(
            (vp_id, event_type, data)
        ),
    )

    agent._preflight_declared_services({})

    services_ensured = [
        data for _vp, event, data in logged if event == "services_ensured"
    ]
    assert services_ensured, logged
    payload = services_ensured[0]
    assert payload["unready_ports"] == [8010]
    assert FAILED_START_DETAIL in json.dumps(payload, ensure_ascii=False)