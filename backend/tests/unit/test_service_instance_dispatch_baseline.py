"""RED regression tests: the dispatch gate compares with the **recorded** instance.

Background
----------
The gate every verification point passes through before its request is
sent is ``_guard_api_test_data_plane()`` -> ``_service_instance_mismatch()``
-> ``_compare_service_instance()``.  It answers one question: *is the
instance answering for this service still the instance this round
started?*  A "no" becomes a BLOCKED verdict and the request is never
sent -- a verdict about somebody else's data plane is worse than no
verdict at all.

The comparison is over three facts about the answering instance:

  * the **boot id** -- compared against the value the round recorded;
  * the **workspace** -- the ``project_dir`` the recorded instance
    named about itself: the repository root it belongs to.  That is
    what the instance *is*;
  * the **state database's inode** -- the inode of the database the
    recorded instance really reads.

The plans tree is none of them: ``plans_dir`` says "which tree is
this process pointed at **right now**", it is relocatable through
``PDT_PLANS_DIR``, and the per-test fixture redirects it on every
single test.  An instance that publishes no workspace at all -- an old
process, or one rolled back to an old build -- falls back to the plans
tree, still against the **recorded** tree and against nothing else.

Only the boot id used to use the right baseline.  The other two asked
a question about *this round* instead of about the service: the
product under test serves its own plans tree and reads its own
database, so the gate reported a mismatch that is not one.  This is
the production shape of 2026-10-27 -- a preflight that answered
``identity_mismatch`` for an instance that had not changed at all, and
a plan whose verification points never ran.

The right baseline is already recorded.
``_record_service_instance_identity()`` keeps the whole ``instance``
block the service published, and that block names the tree, the
workspace and the database the instance really serves.  The comparison
has to use it -- recorded value against observed value -- exactly like
the boot id already does.

What these tests pin
--------------------
  1. a service whose **recorded** plans tree is not this round's own
     tree is not gated: the verification point is dispatched and its
     verdict is about the product;
  2. a service whose recorded **workspace** changed is still blocked --
     the gate is not switched off by the fix, it is pointed at the
     right baseline (R3 / I4).

Test 1 is the reproduction; test 2 is the negative control that keeps
the fix from turning the gate off.

Why the reproduction carries a marker
-------------------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits
non-zero is a **failed task**, and a failed task blocks every task that
depends on it.  The fix is a later task and it depends on this one, so
a permanently red command here would strand the whole chain behind it.

The marker is therefore a ratchet rather than a waiver.  It applies
only while the comparison still reads the round's own tree and this
process's own database, and the moment the recorded baseline is in it
switches itself off and the assertions below become hard ones again.
An implementation that lands with the wrong value therefore fails
these tests outright instead of being absorbed by the marker: the
marker is ``strict``, so a test that starts passing while the marker
still applies is reported as a failure.

The ratchet's condition is the sibling reproduction's oracle
(``_implementation_is_missing()``) -- one question ("has the recorded
baseline landed?") must have one answer for the whole plan, or the two
files could disagree about when the fix arrived.

To read the raw red on the base point, run the module with the
markers switched off::

    pytest tests/unit/test_service_instance_dispatch_baseline.py --runxfail -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The service, the instance block, the agent and the state database
# come from the sibling reproduction of the same gate -- one HTTP
# server, one block, one agent, so this file and that one cannot drift
# apart.  The ratchet below is that file's oracle as well.
from tests.unit.test_service_instance_identity_baseline import (  # noqa: E402
    _Service,
    _implementation_is_missing,
    _instance_block,
    _make_agent,
    _state_db,
)

# The plan the round verifies with -- one ``api_test`` VP against the
# declared service.  It lives with the helpers copied below.
from tests.unit.test_vp_dispatch_identity import (  # noqa: E402
    _api_test_plan,
)


# ---------------------------------------------------------------------------
# The ratchet -- is the recorded baseline in yet?
# ---------------------------------------------------------------------------


#: The marker the reproduction carries until the recorded baseline
#: lands.  See the module docstring: it is a ratchet, not a waiver.
_RECORDED_BASELINE_NOT_LANDED = pytest.mark.xfail(
    strict=True,
    reason=(
        "the recorded instance is not the baseline yet: the plans tree "
        "and the state database's inode are compared against this "
        "round's own plan directory and this process's own database, so "
        "every service that is not this round's own process is blocked "
        "as a foreign instance"
    ),
    condition=_implementation_is_missing(),
)


# ---------------------------------------------------------------------------
# Helpers -- copied from test_vp_dispatch_identity.py (the dispatch-level
# gate of the same round), so the two files cannot drift apart either.
# ---------------------------------------------------------------------------


def _fingerprint(port: int, rows: int = 4, expected: int = 4):
    from service_freshness import DataPlaneFingerprint

    return DataPlaneFingerprint(
        port=port, pid=100, routing_rows=rows, expected_rows=expected,
        status="ok",
    )


def _runtime_map(port: int):
    import service_manager as sm

    return sm.ServiceRuntimeMap(runtimes={
        "api": sm.ServiceRuntime(
            name="api", port=port, pid=100, origin="spawned", ready=True,
            start_cmd="x",
        ),
    })


def _prepare(tmp_path: Path, monkeypatch, service: _Service, *, listening=True):
    """An agent whose ``api`` service is the real HTTP server."""
    agent = _make_agent(tmp_path)
    plan = _api_test_plan(service.port)
    agent._normalize_service_declarations(plan)
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    from service_freshness import PortProbe

    monkeypatch.setattr(
        "service_freshness.probe_port",
        lambda port: PortProbe(port=port, listening=listening),
    )
    monkeypatch.setattr(
        "service_freshness.probe_data_plane",
        lambda port, **kwargs: _fingerprint(port),
    )
    monkeypatch.setattr(
        "service_freshness.expected_routing_rows", lambda *a, **k: 4,
    )

    import service_manager as sm

    monkeypatch.setattr(
        sm, "ensure_services",
        lambda project, declarations, round_number=0, **kwargs:
            _runtime_map(service.port),
    )
    monkeypatch.setattr(sm, "record_runtimes", lambda *a, **k: None)

    agent.start_verification_round(1)
    return agent, plan


def _spy_api_runner(monkeypatch, dispatched: List[str]) -> None:
    """Record every ``api_test`` dispatch; answer PASSED."""
    import verification_api_runner as var

    class _Result:
        status = "PASSED"
        evidence = {"status_code": 200}

        def to_verdict_dict(self):
            return {
                "status": "PASSED",
                "reasons": ["ok"],
                "evidence": dict(self.evidence),
            }

    def fake_run(vp, **kwargs):
        dispatched.append(str(vp.get("id")))
        return _Result()

    monkeypatch.setattr(var, "run_api_verification", fake_run)


def _run_all_vps(executor) -> list:
    """Run every VP of *executor* through the runner, in order."""
    async def _drive():
        return [
            await executor.sub_agent_runner(vp)
            for vp in executor.verification_plan["vps"]
        ]

    return asyncio.run(_drive())


def _log_entries(plan_dir: Path) -> list:
    """Every entry the round wrote to its verification log."""
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


def _mismatch_events(agent) -> list:
    return [
        entry for entry in _log_entries(agent.plan_dir)
        if entry.get("event_type") == "service_instance_mismatch"
    ]


def _product_db(root: Path, name: str = "product.db") -> Path:
    """The database the service under test reads -- with a real inode."""
    path = root / name
    path.write_bytes(b"")
    return path


def _recorded_block(
    boot_id: str, plans_dir: Path, product_db: Path, **extra: Any,
) -> Dict[str, Any]:
    """The ``instance`` block the service under test publishes.

    It describes **the service**: its own plans tree, its own
    database, and -- when it is a modern process -- the workspace it
    belongs to.  None of them is this round's plan directory or this
    process's database -- that is the whole point of the fixture.
    """
    return _instance_block(boot_id, plans_dir, product_db, **extra)


# ---------------------------------------------------------------------------
# 1. The reproduction -- a recorded tree that is not the round's own
# ---------------------------------------------------------------------------


@_RECORDED_BASELINE_NOT_LANDED
def test_a_recorded_plans_tree_that_is_not_the_rounds_own_still_dispatches(
    tmp_path: Path, monkeypatch,
):
    """The service answers as the instance the round recorded → it runs.

    The service under test is another process: it serves the product's
    own plans tree and reads the product's own database.  The round
    recorded that instance during the preflight, so comparing the
    answer against **this round's** tree and **this process's**
    database reports a mismatch that is not one -- and the
    verification point is BLOCKED before a single request goes out.

    The verdict the round ends up with is then about nothing: the
    product was never asked.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)   # this process's own db
        product_db = _product_db(tmp_path)             # the service's own db
        agent, plan = _prepare(tmp_path, monkeypatch, service)

        assert product_db.stat().st_ino != state_db.stat().st_ino, (
            "被测服务的库与本进程的库必须是两个文件，否则测不到这条路径"
        )
        assert str(tmp_path / "product-plans") not in {
            str(agent.plan_dir), str(agent.plan_dir.parent),
        }, "记录下来的树必须不是本轮自己的树，否则测不到这条路径"

        # The round's preflight records the instance that answers ...
        service.publish(
            _recorded_block("boot-1", tmp_path / "product-plans", product_db),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        agent._preflight_service_freshness(plan)

        # ... and the service has not changed: it is still itself.
        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == ["VP-001"], (
        "记录下来的实例就是基线：请求必须照常派发，实际派发的是 "
        f"{dispatched!r}"
    )
    assert results[0]["status"] == "PASSED", results[0]
    assert _mismatch_events(agent) == []


# ---------------------------------------------------------------------------
# 2. The negative control -- the gate is not switched off
# ---------------------------------------------------------------------------


@_RECORDED_BASELINE_NOT_LANDED
def test_a_recorded_project_dir_that_changed_still_blocks_the_vp(
    tmp_path: Path, monkeypatch,
):
    """The recorded workspace changed → the verification point is blocked.

    The fix must not turn the gate off, it must point it at the right
    baseline.  The instance the round recorded belonged to one
    workspace; the instance answering now belongs to another one, and
    that is exactly what the gate exists to catch.

    Only the workspace differs here -- the boot id, the plans tree and
    the database are the ones the round recorded -- so ``project_dir``
    must be the **only** mismatched field.
    """
    service = _Service()
    try:
        state_db = _state_db(tmp_path, monkeypatch)   # this process's own db
        product_db = _product_db(tmp_path)             # the service's own db
        agent, plan = _prepare(tmp_path, monkeypatch, service)

        # The round records this instance ...
        service.publish(
            _recorded_block(
                "boot-1", tmp_path / "product-plans", product_db,
                project_dir=str(tmp_path / "product"),
            ),
        )
        dispatched: List[str] = []
        _spy_api_runner(monkeypatch, dispatched)

        agent._preflight_service_freshness(plan)

        # ... and then the instance behind the port moved to another
        # workspace.
        service.publish(
            _recorded_block(
                "boot-1", tmp_path / "product-plans", product_db,
                project_dir=str(tmp_path / "another-product"),
            ),
        )

        executor = agent._build_verification_executor(
            agent._resolve_service_placeholders(plan)
        )
        results = _run_all_vps(executor)
    finally:
        service.close()

    assert dispatched == [], "换了 workspace 的实例不得收到请求"
    assert results[0]["status"] == "BLOCKED", results[0]
    assert results[0]["status"] != "FAILED", results[0]
    evidence = results[0]["evidence"]
    assert evidence["error_kind"] == "service_instance_mismatch", evidence
    assert evidence["mismatched_fields"] == ["project_dir"], (
        "只有 workspace 变了，mismatched_fields 必须只含 project_dir，实际是 "
        f"{evidence['mismatched_fields']!r}"
    )

    events = _mismatch_events(agent)
    assert len(events) == 1, events
    assert events[0]["data"]["mismatched_fields"] == ["project_dir"], events[0]