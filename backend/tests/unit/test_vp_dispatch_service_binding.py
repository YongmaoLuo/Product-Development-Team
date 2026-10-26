"""The VP dispatch must be bound to the instance the round ensured.

2026-10-25 (repair-r1-22)
--------------------------
The 2026-10-24 production round started the plan's ``api`` service on
port 58396 (the declared 18090 was held by a foreign process, so the
service was relocated) and then — twenty minutes later — twelve
``api_test`` VPs all failed with a connection refusal.  The address
those VPs used was the one the ensure had produced, but **nothing
pinned it there**: the address was re-derived from
``_service_runtime_map`` every time the plan was resolved, and the map
is a record that anything merging into it may rewrite.  A rewritten
map moves the address under a VP that is talking to a perfectly
healthy instance, and the log then shows an address nobody can
account for.

So the round now keeps a **binding**: the runtime object the ensure
returned, per service, and every VP's request is built from it.  The
map is still written (it is the record of what happened), but it no
longer decides where a request goes.

The contract these tests pin:

  1. A bound service keeps its address when the runtime map is
     rewritten — the binding wins.
  2. A VP's request URL is resolved against the bound instance, and a
     dispatch never uses an address the round did not bind.
  3. A recovery that really produced a live instance **rebinds** the
     round (the address is allowed to move exactly once, explicitly).
  4. A recovery that failed does **not** become the dispatch address:
     the round falls back to the declared port, which the
     blocked-port gate then turns into a BLOCKED verdict.
  5. Every dispatch says which instance it was bound to
     (``service_dispatch_bound``), so a log reader can answer "which
     service did this VP talk to" without guessing.

The end-to-end version of the same contract — a **real** HTTP service,
brought up by the round's own ``ensure``, answered by real ``api_test``
requests, with the runtime map rewritten underneath the round — lives
next door, in ``test_service_manager.py``
(``test_declared_service_stays_healthy_until_the_last_vp``): the plan
names that file when it asks whether the declared service stays healthy
until the last VP.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from service_declaration import ServiceDeclaration  # noqa: E402


DECLARED_PORT = 18090
BOUND_PORT = 58396
FOREIGN_PORT = 51725


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


def _decl(name: str = "api", port: int = DECLARED_PORT) -> ServiceDeclaration:
    return ServiceDeclaration(
        name=name,
        port=port,
        start_cmd="true",
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
    )


def _runtime(
    name: str = "api",
    port: int = BOUND_PORT,
    pid: Any = 4242,
    ready: bool = True,
    start_epoch: float = 1_000.0,
):
    from service_manager import ServiceRuntime

    return ServiceRuntime(
        name=name,
        port=port,
        declared_port=DECLARED_PORT,
        pid=pid,
        pgid=pid,
        start_epoch=start_epoch,
        start_cmd="true",
        cwd=".",
        origin="spawned",
        ready=ready,
    )


def _runtime_map(runtimes: Dict[str, Any]):
    from service_manager import ServiceRuntimeMap

    return ServiceRuntimeMap(runtimes=dict(runtimes))


def _plan(url: str = "{{svc.api.url}}/health") -> dict:
    return {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "api_test",
                "uses_services": ["api"],
                "request": {"method": "GET", "url": url},
                "assertions": [{"status": 200}],
            }
        ]
    }


def _bound_agent(tmp_path: Path):
    """An agent whose round already ensured (and bound) the service."""
    agent = _make_agent(tmp_path)
    agent._service_declarations = {"api": _decl()}
    runtime = _runtime()
    agent._service_runtime_map = _runtime_map({"api": runtime})
    agent._bind_service_runtime("api", runtime)
    return agent


# ---------------------------------------------------------------------------
# 1. The binding wins over the map
# ---------------------------------------------------------------------------


def test_a_rewritten_runtime_map_cannot_move_a_bound_service(tmp_path: Path):
    """The map is a record; the binding decides the address.

    The historical failure: the ensure produced 58396, something
    rewrote the map to 51725, and the VPs followed the map.
    """
    agent = _bound_agent(tmp_path)

    agent._service_runtime_map.runtimes["api"] = _runtime(
        port=FOREIGN_PORT, pid=999,
    )

    assert agent._effective_declarations()["api"].port == BOUND_PORT


def test_vp_url_uses_same_runtime_as_ensure(tmp_path: Path):
    """The URL a VP carries names the instance the ensure produced."""
    agent = _bound_agent(tmp_path)

    resolved = agent._resolve_service_placeholders(_plan())
    url = resolved["verification_points"][0]["request"]["url"]

    assert url == f"http://127.0.0.1:{BOUND_PORT}/health"


def test_no_second_resolution_between_ensure_and_dispatch(tmp_path: Path):
    """Nothing between the ensure and the dispatch re-reads the map.

    The plan is resolved once, from the bound instance.  A map that is
    rewritten afterwards must not be able to move the address the VP
    is about to be sent to.
    """
    agent = _bound_agent(tmp_path)

    agent._service_runtime_map.runtimes["api"] = _runtime(
        port=FOREIGN_PORT, pid=999,
    )

    resolved = agent._resolve_service_placeholders(_plan())
    url = resolved["verification_points"][0]["request"]["url"]

    assert url == f"http://127.0.0.1:{BOUND_PORT}/health"


# ---------------------------------------------------------------------------
# 2. The dispatch never uses an address the round did not bind
# ---------------------------------------------------------------------------


def test_a_dispatch_retargets_a_request_to_the_bound_instance(
    tmp_path: Path,
):
    """A request that carries a foreign port is pointed at the bound
    instance before it is sent — the request construction and the
    ensure result are bound to the same object."""
    agent = _bound_agent(tmp_path)

    vp = {
        "id": "VP-001",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{FOREIGN_PORT}/health",
        },
    }
    bound_vp = agent._bind_vp_service_address(vp)

    assert bound_vp["request"]["url"] == f"http://127.0.0.1:{BOUND_PORT}/health"


def test_a_dispatch_leaves_a_remote_url_alone(tmp_path: Path):
    """Only a local address is this service's address to rewrite."""
    agent = _bound_agent(tmp_path)

    vp = {
        "id": "VP-001",
        "uses_services": ["api"],
        "request": {
            "method": "GET",
            "url": f"http://example.com:{FOREIGN_PORT}/health",
        },
    }
    bound_vp = agent._bind_vp_service_address(vp)

    assert bound_vp["request"]["url"] == f"http://example.com:{FOREIGN_PORT}/health"


def test_a_vp_that_names_no_bound_service_is_left_alone(tmp_path: Path):
    agent = _bound_agent(tmp_path)

    vp = {
        "id": "VP-900",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{FOREIGN_PORT}/other",
        },
    }
    bound_vp = agent._bind_vp_service_address(vp)

    assert bound_vp["request"]["url"] == f"http://127.0.0.1:{FOREIGN_PORT}/other"


# ---------------------------------------------------------------------------
# 3. + 4. A recovery may move the address — but only a live instance
# ---------------------------------------------------------------------------


def test_a_recovered_instance_rebinds_the_round(tmp_path: Path):
    """The one case where the address moves: the recovery really
    produced a live instance."""
    agent = _bound_agent(tmp_path)

    agent._merge_service_runtime_map(
        _runtime_map({"api": _runtime(port=FOREIGN_PORT, pid=777)}),
    )

    assert agent._effective_declarations()["api"].port == FOREIGN_PORT


def test_a_failed_recovery_is_not_adopted_as_the_dispatch_address(
    tmp_path: Path,
):
    """A runtime that is not ready is not an address a VP may be sent
    to: the round falls back to the declared port, which the
    blocked-port gate turns into a BLOCKED verdict."""
    agent = _bound_agent(tmp_path)

    agent._merge_service_runtime_map(
        _runtime_map({"api": _runtime(port=FOREIGN_PORT, pid=None, ready=False)}),
    )

    assert "api" not in agent._service_bindings
    assert agent._effective_declarations()["api"].port == DECLARED_PORT


def test_binding_a_runtime_that_never_came_up_is_refused(tmp_path: Path):
    agent = _make_agent(tmp_path)

    assert agent._bind_service_runtime(
        "api", _runtime(ready=False),
    ) is None
    assert "api" not in agent._service_bindings


def test_binding_a_runtime_without_a_port_is_refused(tmp_path: Path):
    agent = _make_agent(tmp_path)

    runtime = _runtime()
    runtime.port = None

    assert agent._bind_service_runtime("api", runtime) is None
    assert "api" not in agent._service_bindings


# ---------------------------------------------------------------------------
# 5. The dispatch says which instance it was bound to
# ---------------------------------------------------------------------------


def test_dispatch_bound_logged_for_every_vp(tmp_path: Path):
    """Every dispatch says which instance it was bound to.

    One log line per VP — not one per round: the question a log reader
    has is "which service did **this** VP talk to", and the answer has
    to name the instance (port, pid, boot id), not just the port.
    """
    agent = _bound_agent(tmp_path)
    agent.start_verification_round(1)

    vp_ids = ["VP-001", "VP-002", "VP-003"]
    for vp_id in vp_ids:
        agent._bind_vp_service_address({
            "id": vp_id,
            "uses_services": ["api"],
            "request": {
                "method": "GET",
                "url": f"http://127.0.0.1:{BOUND_PORT}/health",
            },
        })

    entries = _log_entries(agent.plan_dir)
    bound = [e for e in entries if e.get("event_type") == "service_dispatch_bound"]
    assert len(bound) == len(vp_ids), entries
    assert [e["data"]["vp_id"] for e in bound] == vp_ids
    for entry in bound:
        data = entry["data"]
        assert data["plan_id"] == agent.plan_dir.name
        assert data["port"] == BOUND_PORT
        assert data["pid"] == 4242
        assert data["boot_id"]
        assert data["runtime_id"]


def _log_entries(plan_dir: Path):
    entries = []
    for log_file in sorted((plan_dir / "logs").glob("verification_*.log")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover - defensive
                continue
    return entries


# ---------------------------------------------------------------------------
