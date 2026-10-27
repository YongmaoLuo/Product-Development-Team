"""Every VP's evidence records **where it was sent**.

2026-10-25 (repair-r1-31)
-------------------------
The production round pointed its ``api_test`` VPs at an address, and
the evidence said exactly one thing about it: ``Errno 61``.  That is
a transport's complaint — it is not an address, and it cannot answer
the two questions a reader actually has:

  * **which port** did the request go to?
  * **which instance** was listening on that port — the one this
    round's ``ensure`` started, or a stranger that took the port
    behind the round's back?

Without those two answers the round cannot tell a **product** defect
from an **environment** defect.  The same ``Errno 61`` means "the
service is down" when the port was the round's own instance, and "the
request never reached the code under test" when it was not — and the
report cannot tell those apart, so the repair loop repairs the wrong
thing.

So every VP's recorded evidence carries the address facts:

  ``requested_url``   the URL the request actually went to
  ``resolved_port``   the port that URL names
  ``ensure_port``     the port this round's ``ensure`` bound for it
  ``pid``             the process behind that binding
  ``boot_id``         that process's boot identity (pid + start epoch)
  ``attempt_no``      how many times the request was attempted
  ``error_kind``      the classification code (``service_unreachable``)
  ``errno``           the errno the transport reported
  ``request_id``      one id shared by **every** VP of the round

and, when a response came back, the **answering** instance's own boot
id (``answering_boot_id``) — the fact that separates "the round's own
instance answered" from "somebody else's instance answered".  It is
the round's own observation of that port (the block it reads from
``/health/data`` while it gates on the instance), not a new question
put to the service.

The contract these tests pin:

  1. A transport failure keeps its verdict (BLOCKED, not FAILED) **and**
     gains the whole address block.
  2. The address block survives the environment-blocked rewrite — the
     round's own rewrite must not drop the facts it just recorded.
  3. A successful response records the responder's own boot id.
  4. Every VP of one round shares one ``request_id``; the next round
     gets a new one.
  5. A VP that never dispatched says so (``attempt_no == 0``).
  6. Recording the address does not change what the service is asked:
     the service under test sees the plan's requests and nothing else.
"""

from __future__ import annotations

import errno as errno_module
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _closed_port() -> int:
    """A port nothing listens on.

    Bound and released again, so the connect that follows is refused —
    the production shape (``Errno 61``), reproduced without hard-coding
    a port some other process might own.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Service:
    """A real HTTP server that answers like the backend does.

    ``/health/data`` publishes an ``instance`` block (the boot id the
    answering process was minted with); everything else answers 200.
    The identity check talks to the service over HTTP — the same road
    a VP's request travels — so the test needs a service that really
    answers.
    """

    def __init__(self, boot_id: str = "boot-answering"):
        self.boot_id = boot_id
        self.plans_dir = ""
        self.state_db_path = ""
        self.requests: List[str] = []
        service = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - the handler API's own name
                service.requests.append(self.path)
                if self.path.startswith("/health/data"):
                    body: Dict[str, Any] = {
                        "status": "ok",
                        "plan_routing_rows": 4,
                        "instance": {
                            "boot_id": service.boot_id,
                            "pid": 4242,
                            "plans_dir": service.plans_dir,
                            "state_db_path": service.state_db_path,
                            "state_db_inode": 1,
                        },
                    }
                else:
                    body = {"signal_type": "trend"}
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # noqa: A003 - silence the log
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class _FakeExecutor:
    """The executor's verdict table, without the executor."""

    def __init__(self, verdicts: List[dict]):
        self._verdicts = verdicts

    def collect_verdicts(self) -> List[dict]:
        return [dict(v) for v in self._verdicts]

    def _plan_retired_vps(self) -> dict:
        return {}


def _api_vp(url: str, vp_id: str = "VP-001", services=None) -> dict:
    """An ``api_test`` VP — the shape the framework grades directly."""
    vp = {
        "id": vp_id,
        "verification_method": "api_test",
        "request": {"method": "GET", "url": url},
        "assertions": [{"status": 200}],
    }
    if services is not None:
        vp["uses_services"] = list(services)
    return vp


def _plan(count: int = 1, url: str = "") -> dict:
    return {
        "verification_points": [
            {
                "id": f"VP-{index:03d}",
                "title": f"vp {index}",
                "verification_method": "api_test",
                "request": {"method": "GET", "url": url},
                "assertions": [{"status": 200}],
            }
            for index in range(1, count + 1)
        ]
    }


def _bind_service(agent, port: int, pid: int = 4242,
                  start_epoch: int = 1700000000, name: str = "api"):
    """Bind the round's ``api`` service to *port*, the way ``ensure`` does.

    The round's binding is what a VP's request address is built from
    (repair-r1-22), and it is the only place the pid and the boot id
    live — so the test has to bind a real one rather than fake the
    evidence.
    """
    from verification_agent import _BoundService

    agent._service_bindings[name] = _BoundService(
        name=name,
        port=int(port),
        pid=int(pid),
        health_url=f"http://127.0.0.1:{int(port)}/health",
        boot_id=f"{int(pid)}@{int(start_epoch)}",
        runtime_id=f"{name}:{int(port)}:{int(pid)}",
    )
    return agent._service_bindings[name]


def _evidence_of(envelope: dict, vp_id: str) -> dict:
    """The recorded evidence of one VP, parsed."""
    for result in envelope["execution_results"]:
        if result["id"] == vp_id:
            return json.loads(result["evidence"])
    raise AssertionError(f"{vp_id} is not in the envelope")


# ---------------------------------------------------------------------------
# 1. A transport failure records the whole address
# ---------------------------------------------------------------------------


def test_a_transport_failure_records_the_whole_address(tmp_path: Path):
    """``Errno 61`` plus the address it happened at.

    The production evidence said only ``Errno 61``.  This says: the
    request went to this URL, on this port, which is the port the
    round's ``ensure`` bound, which belongs to this process, which was
    minted with this boot id — and this was attempt number one, of
    round ``request_id``.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    _bind_service(agent, port)
    url = f"http://127.0.0.1:{port}/items"

    verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))

    # The verdict itself is unchanged: a request that never got an
    # answer is BLOCKED, not a product failure.
    assert verdict["status"] == "BLOCKED", verdict

    envelope = agent._build_execution_results_from_executor(
        _plan(1, url),
        _FakeExecutor([{
            "vp_id": "VP-001",
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        }]),
    )
    evidence = _evidence_of(envelope, "VP-001")

    assert evidence["requested_url"] == url
    assert evidence["resolved_port"] == port
    assert evidence["ensure_port"] == port
    assert evidence["pid"] == 4242
    assert evidence["boot_id"] == "4242@1700000000"
    assert evidence["attempt_no"] == 1
    assert evidence["error_kind"] == "service_unreachable"
    assert evidence["errno"] == errno_module.ECONNREFUSED  # 61
    assert evidence["request_id"]
    # No response came back, so there was no answering instance to
    # name — ``None`` says that, an absent field would not.
    assert evidence["answering_boot_id"] is None


def test_the_address_survives_the_environment_blocked_rewrite(
    tmp_path: Path,
):
    """The round's own rewrite must not drop the facts it just recorded.

    An environment-blocked round rewrites every result's evidence —
    and the address block has to come through that rewrite, or the
    production round would have been no better off.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    _bind_service(agent, port)
    url = f"http://127.0.0.1:{port}/items"

    verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))
    assert verdict["status"] == "BLOCKED"

    envelope = agent._build_execution_results_from_executor(
        _plan(1, url),
        _FakeExecutor([{
            "vp_id": "VP-001",
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        }]),
    )
    evidence = _evidence_of(envelope, "VP-001")

    assert evidence["environment_blocked"] is True
    assert evidence["resolved_port"] == port
    assert evidence["ensure_port"] == port
    assert evidence["boot_id"] == "4242@1700000000"
    assert evidence["errno"] == errno_module.ECONNREFUSED
    assert evidence["request_id"]


# ---------------------------------------------------------------------------
# 2. A successful response records the responder's own boot id
# ---------------------------------------------------------------------------


def test_a_successful_response_records_the_answering_boot_id(
    tmp_path: Path,
):
    """The response came back — so the responder can be named.

    The round's ``boot_id`` is what the round *expected*; the
    answering instance's own ``boot_id`` is what the round observed on
    that port.  When they differ, the answer came from somebody else,
    and the evidence has to say so.

    The observation is the one the round reads anyway while it gates
    on the instance's identity — the evidence quotes it rather than
    asking the service a second time.
    """
    service = _Service(boot_id="boot-answering")
    try:
        agent = _make_agent(tmp_path)
        agent.start_verification_round(1)
        service.plans_dir = str(agent.plan_dir)
        _bind_service(agent, service.port)
        url = f"http://127.0.0.1:{service.port}/items"

        # The round reads the instance block while it gates on the
        # instance: that read is the round's observation of the port.
        agent._record_service_instance_identity("api", service.port)
        before = len(service.requests)

        verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))
        assert verdict["status"] == "PASSED", verdict

        envelope = agent._build_execution_results_from_executor(
            _plan(1, url),
            _FakeExecutor([{
                "vp_id": "VP-001",
                "status": verdict["status"],
                "reasons": verdict.get("reasons", []),
                "evidence": verdict.get("evidence", {}),
            }]),
        )
        evidence = _evidence_of(envelope, "VP-001")

        assert evidence["status_code"] == 200
        assert evidence["boot_id"] == "4242@1700000000"
        assert evidence["answering_boot_id"] == "boot-answering"
        assert evidence["resolved_port"] == service.port
        assert evidence["ensure_port"] == service.port
        assert evidence["attempt_no"] == 1
        assert evidence["request_id"]
        # The bookkeeping field did not make the service answer a new
        # question: the only request after the observation is the VP's
        # own.
        assert service.requests[before:] == ["/items"], service.requests
    finally:
        service.close()


def test_the_address_record_does_not_add_a_request_to_the_service(
    tmp_path: Path,
):
    """A report field must not change what the service is asked.

    The service under test must see exactly the requests the plan
    asked for.  A "harmless" bookkeeping field that makes the round
    probe ``/health/data`` again is a change in the product's
    behaviour — and it is a change nobody asked for.
    """
    service = _Service(boot_id="boot-answering")
    try:
        agent = _make_agent(tmp_path)
        agent.start_verification_round(1)
        _bind_service(agent, service.port)
        url = f"http://127.0.0.1:{service.port}/items"

        verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))
        assert verdict["status"] == "PASSED", verdict

        assert service.requests == ["/items"], service.requests
    finally:
        service.close()


def test_the_pre_dispatch_identity_check_fills_the_answering_boot_id(
    tmp_path: Path, monkeypatch,
):
    """The observation is taken by the check that runs before the request.

    Otherwise the field would only ever be filled in a test that seeds
    it by hand, and the production round would carry ``None`` — the
    very gap this whole change is about.
    """
    service = _Service(boot_id="boot-answering")
    try:
        agent = _make_agent(tmp_path)
        agent.start_verification_round(1)
        service.plans_dir = str(agent.plan_dir)
        _bind_service(agent, service.port)
        url = f"http://127.0.0.1:{service.port}/items"

        # The round's own gate: it reads the instance block of the
        # service the VP is about to talk to, and compares it with what
        # the round expects.  The state database of this test is not
        # the round's, so that comparison is switched off.
        monkeypatch.setattr(agent, "_expected_state_db_inodes", lambda name: [])
        declaration = SimpleNamespace(port=service.port)
        mismatch = agent._service_instance_mismatch("api", declaration)
        assert mismatch is None, mismatch

        verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))
        assert verdict["status"] == "PASSED", verdict

        envelope = agent._build_execution_results_from_executor(
            _plan(1, url),
            _FakeExecutor([{
                "vp_id": "VP-001",
                "status": verdict["status"],
                "reasons": verdict.get("reasons", []),
                "evidence": verdict.get("evidence", {}),
            }]),
        )
        evidence = _evidence_of(envelope, "VP-001")

        assert evidence["answering_boot_id"] == "boot-answering"
        assert evidence["boot_id"] == "4242@1700000000"
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 3. One request id per round
# ---------------------------------------------------------------------------


def test_every_vp_of_a_round_shares_one_request_id(tmp_path: Path):
    """The round's VPs hang on one thread, so they can be compared.

    Twelve VPs that each failed on "some port" are twelve facts; the
    question "were they all the same address?" needs one id to ask
    against.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    port = _closed_port()
    _bind_service(agent, port)
    url = f"http://127.0.0.1:{port}/items"

    verdicts = []
    for index in (1, 2, 3):
        vp_id = f"VP-{index:03d}"
        verdict = agent._run_api_test_vp(
            _api_vp(url, vp_id=vp_id, services=["api"])
        )
        verdicts.append({
            "vp_id": vp_id,
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        })

    envelope = agent._build_execution_results_from_executor(
        _plan(3, url), _FakeExecutor(verdicts),
    )

    request_ids = {
        _evidence_of(envelope, f"VP-{index:03d}")["request_id"]
        for index in (1, 2, 3)
    }
    assert len(request_ids) == 1, request_ids
    assert next(iter(request_ids))


def test_the_next_round_gets_a_new_request_id(tmp_path: Path):
    """A request id that spans rounds would mix two rounds' evidence."""
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    first = agent._round_request_id
    agent.start_verification_round(2)
    second = agent._round_request_id

    assert first
    assert second
    assert first != second


# ---------------------------------------------------------------------------
# 4. A VP that never dispatched says so
# ---------------------------------------------------------------------------


def test_a_vp_that_never_dispatched_records_attempt_no_zero(
    tmp_path: Path,
):
    """No request went out — the evidence must not imply one did.

    ``attempt_no == 0`` is the difference between "the request failed"
    and "the request was never made", and the two are graded very
    differently.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    url = "http://127.0.0.1:9/items"

    envelope = agent._build_execution_results_from_executor(
        _plan(1, url),
        _FakeExecutor([{
            "vp_id": "VP-001",
            "status": "BLOCKED",
            "reasons": ["blocked before dispatch"],
            "evidence": {"vp_id": "VP-001"},
        }]),
    )
    evidence = _evidence_of(envelope, "VP-001")

    assert evidence["attempt_no"] == 0
    assert evidence["requested_url"] == url
    assert evidence["resolved_port"] == 9
    assert evidence["error_kind"] is None
    assert evidence["errno"] is None
    assert evidence["request_id"]
    # The field set is the same for every VP of the round — a reader
    # that knows the list never has to ask "is this field missing, or
    # is it empty?".
    from verification_agent import ADDRESS_EVIDENCE_FIELDS

    assert set(ADDRESS_EVIDENCE_FIELDS) <= set(evidence), (
        f"the evidence is missing {sorted(set(ADDRESS_EVIDENCE_FIELDS) - set(evidence))}"
    )
# ---------------------------------------------------------------------------
# 5. The address, the boot id, the mismatch — and the id the round shares
# ---------------------------------------------------------------------------


def test_evidence_contains_requested_url_and_ensure_port(tmp_path: Path):
    """The evidence names the address the request **really** went to.

    The plan's URL was resolved when the plan was resolved; the round's
    ``ensure`` bound the instance afterwards, and the request goes to
    the bound instance.  So the evidence has to name that address — the
    one the transport saw — and the port the round's ``ensure`` bound.

    An evidence that names the *planned* address instead points the
    reader at a port nothing ever talked to, which is exactly the
    "Errno 61 on some port" gap the address block was added to close.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    bound_port = _closed_port()
    _bind_service(agent, bound_port)
    planned_port = _closed_port()
    url = f"http://127.0.0.1:{planned_port}/items"

    verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))
    assert verdict["status"] == "BLOCKED", verdict

    envelope = agent._build_execution_results_from_executor(
        _plan(1, url),
        _FakeExecutor([{
            "vp_id": "VP-001",
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        }]),
    )
    evidence = _evidence_of(envelope, "VP-001")

    assert evidence["requested_url"] == f"http://127.0.0.1:{bound_port}/items", (
        f"the evidence names {evidence['requested_url']!r}, which is not the "
        f"address the request went to: {evidence!r}"
    )
    assert evidence["resolved_port"] == bound_port, (
        f"the evidence does not name the port the request went to: "
        f"{evidence!r}"
    )
    assert evidence["ensure_port"] == bound_port, (
        f"the evidence does not name the port the round's ensure bound: "
        f"{evidence!r}"
    )


def test_evidence_contains_boot_id_on_success(tmp_path: Path):
    """A healthy answer carries the boot id — the round's and the answering one.

    When the instance that answers **is** the one the round started, the
    two boot ids are the same value, and the evidence carries both.  A
    reader that holds only one of them cannot tell "the round's own
    instance answered" from "somebody answered" — and that difference is
    the difference between a product defect and an environment one.
    """
    service = _Service(boot_id="4242@1700000000")
    try:
        agent = _make_agent(tmp_path)
        agent.start_verification_round(1)
        service.plans_dir = str(agent.plan_dir)
        _bind_service(agent, service.port, pid=4242, start_epoch=1700000000)
        url = f"http://127.0.0.1:{service.port}/items"

        # The round reads the instance block while it gates on the
        # instance: that read is the round's observation of the port.
        agent._record_service_instance_identity("api", service.port)

        verdict = agent._run_api_test_vp(_api_vp(url, services=["api"]))
        assert verdict["status"] == "PASSED", verdict

        envelope = agent._build_execution_results_from_executor(
            _plan(1, url),
            _FakeExecutor([{
                "vp_id": "VP-001",
                "status": verdict["status"],
                "reasons": verdict.get("reasons", []),
                "evidence": verdict.get("evidence", {}),
            }]),
        )
        evidence = _evidence_of(envelope, "VP-001")

        assert evidence["status_code"] == 200
        assert evidence["boot_id"] == "4242@1700000000", (
            f"the evidence does not name the round's own boot id: {evidence!r}"
        )
        assert evidence["answering_boot_id"] == "4242@1700000000", (
            f"the evidence does not name the answering instance: {evidence!r}"
        )
        assert evidence["boot_id"] == evidence["answering_boot_id"], (
            f"the round's boot id ({evidence['boot_id']!r}) and the answering "
            f"one ({evidence['answering_boot_id']!r}) disagree — the answer "
            f"came from another instance: {evidence!r}"
        )
        assert evidence["resolved_port"] == service.port
        assert evidence["ensure_port"] == service.port
    finally:
        service.close()


def test_evidence_flags_port_mismatch(tmp_path: Path):
    """A mismatch is flagged, with both sides of the comparison.

    Two halves, one contract:

      * a VP aimed at a port **no** round service ensured says so —
        ``ensure_port`` is empty and ``resolved_port`` is the port the
        request went to, so nobody reads "the round's own instance" into
        an address the round never bound;
      * a stranger answering on the round's port is flagged as what it
        is: the evidence names the classification, the field that
        mismatched, and both boot ids — expected and actual — so the
        reader can tell "the service was restarted behind the round's
        back" from "the service serves another tree".
    """
    # --- half one: an address the round never bound --------------------
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    unbound_port = _closed_port()
    url = f"http://127.0.0.1:{unbound_port}/items"

    verdict = agent._run_api_test_vp(_api_vp(url))
    assert verdict["status"] == "BLOCKED", verdict

    envelope = agent._build_execution_results_from_executor(
        _plan(1, url),
        _FakeExecutor([{
            "vp_id": "VP-001",
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        }]),
    )
    evidence = _evidence_of(envelope, "VP-001")

    assert evidence["resolved_port"] == unbound_port, (
        f"the evidence does not name the port the request went to: "
        f"{evidence!r}"
    )
    assert evidence["ensure_port"] is None, (
        f"the evidence claims the round's ensure bound port "
        f"{evidence['ensure_port']!r} — it bound none: {evidence!r}"
    )

    # --- half two: a stranger on the round's port ----------------------
    service = _Service(boot_id="4242@1700000000")
    try:
        agent = _make_agent(tmp_path)
        agent.start_verification_round(1)
        service.plans_dir = str(agent.plan_dir)
        _bind_service(agent, service.port, pid=4242, start_epoch=1700000000)
        bound_url = f"http://127.0.0.1:{service.port}/items"

        # The round's own observation of the port: the instance it
        # started, recorded while it gated on the instance.
        agent._record_service_instance_identity("api", service.port)

        # ... and then a stranger takes the port behind the round's back.
        service.boot_id = "boot-stranger"
        service.plans_dir = "/somewhere/else"

        mismatch = agent._service_instance_mismatch(
            "api", SimpleNamespace(port=service.port),
        )
        assert mismatch is not None, (
            "a stranger on the round's port was not detected as a mismatch"
        )

        verdict = agent._build_service_instance_mismatch_verdict(
            "VP-001", mismatch,
        )
        assert verdict["status"] == "BLOCKED", verdict

        envelope = agent._build_execution_results_from_executor(
            _plan(1, bound_url),
            _FakeExecutor([{
                "vp_id": "VP-001",
                "status": verdict["status"],
                "reasons": verdict.get("reasons", []),
                "evidence": verdict.get("evidence", {}),
            }]),
        )
        evidence = _evidence_of(envelope, "VP-001")

        assert evidence["error_kind"] == "service_instance_mismatch", (
            f"the mismatch was not classified: {evidence!r}"
        )
        assert "boot_id" in evidence["mismatched_fields"], (
            f"the evidence does not say which field mismatched: {evidence!r}"
        )
        assert evidence["expected_boot_id"] == "4242@1700000000", (
            f"the evidence does not name the instance the round started: "
            f"{evidence!r}"
        )
        assert evidence["actual_boot_id"] == "boot-stranger", (
            f"the evidence does not name the instance that answered: "
            f"{evidence!r}"
        )
        # And the port is named in the reason — the address is half of
        # the fact.
        assert str(service.port) in " ".join(verdict["reasons"]), (
            f"the reason does not name the port: {verdict['reasons']!r}"
        )
    finally:
        service.close()


def test_all_vps_share_request_id(tmp_path: Path):
    """One id for the whole round — even across different addresses.

    The id is the thread the round's evidence hangs on.  It has to hold
    across *different* addresses, or it answers "were they all talking
    to the same place?" for the round instead of for one address — and
    the whole point of the id is to let a reader line the round's facts
    up against each other.
    """
    agent = _make_agent(tmp_path)
    agent.start_verification_round(1)
    # Three **different** addresses: the round bound one of them, and
    # the other two belong to no service the round ensured.
    ports: List[int] = []
    while len(ports) < 3:
        candidate = _closed_port()
        if candidate not in ports:
            ports.append(candidate)
    _bind_service(agent, ports[0])

    verdicts = []
    for index, port in enumerate(ports, start=1):
        vp_id = f"VP-{index:03d}"
        verdict = agent._run_api_test_vp(
            _api_vp(f"http://127.0.0.1:{port}/items", vp_id=vp_id)
        )
        verdicts.append({
            "vp_id": vp_id,
            "status": verdict["status"],
            "reasons": verdict.get("reasons", []),
            "evidence": verdict.get("evidence", {}),
        })

    envelope = agent._build_execution_results_from_executor(
        _plan(3), _FakeExecutor(verdicts),
    )

    addresses = {
        _evidence_of(envelope, f"VP-{index:03d}")["resolved_port"]
        for index in (1, 2, 3)
    }
    assert len(addresses) == 3, (
        f"the VPs did not talk to three different addresses ({addresses}) — "
        f"the test does not exercise the case it is about"
    )
    request_ids = {
        _evidence_of(envelope, f"VP-{index:03d}")["request_id"]
        for index in (1, 2, 3)
    }
    assert len(request_ids) == 1, request_ids
    assert next(iter(request_ids))
