"""The repair callback's round instance owns its own identity (2026-11-13).

The criterion this file pins
----------------------------
``_make_repair_on_complete`` builds the callback that re-enters
verification after a repair execution finished.  That re-entry is a
ROUND of its own — it preflights the plan's services, dispatches
verification points and, when it ends, releases and reaps what it
started.

Before 2026-11-13 the re-entry carried the token of the loop that
scheduled the repair.  That loop was already over: its round was
closed, its token released.  So the re-entered round:

  * opened its scope in the process ledger under a name that belonged
    to a dead round;
  * never registered itself as the plan's "current service owner", so
    the services its preflight brought up were recorded with no owner
    at all — and a service nobody owns is a service nobody may reap;
  * wrote its verdict under the dead round's name.

On 2026-11-13 (plan 20261027-main-2, round 3) that left a service
running with no owner: the round that started it was gone, and the
round that could have stopped it was looking at a name that was not
its own.

So every re-entry mints its OWN instance token and then owns it for
exactly as long as it lives:

  * **mint** — a fresh UUID, never the outer loop's token;
  * **claim** — the plan's "current service owner" role, REFUSED when
    a live round already holds it (a running round keeps the role);
  * **use** — the ledger scope, the state stamp, the preflight's
    services, the verdict;
  * **release** — always, on every exit path, including the exception
    path;
  * **reap** — only when the round really ended (passed / failed /
    loop_stopped).  A hand-off (the repair executor is still in
    flight) releases but does NOT reap: the workflow has not exited.

What this file pins:

  1. the re-entry claims the role under its OWN token — not the outer
     loop's;
  2. two re-entries of the same round number get DIFFERENT tokens;
  3. a terminal round reaps with its own instance token;
  4. a hand-off does not reap;
  5. the terminal write carries the instance token;
  6. a live round that holds the role aborts the re-entry, and the
     verdict says why.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the owner
registry, the workflow lease and the process ledger are the test's
own.  No service is started, no process is signalled — the reap is a
recorder, and the ledger is reset around every test.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server  # noqa: E402
import service_manager  # noqa: E402
import verification_loop as vl  # noqa: E402
import round_process_ledger  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _World:
    """Everything the callback touches, recorded as it happens."""

    def __init__(
        self, plan_id: str, plan_dir: Path, project_dir: Path,
    ) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        #: one entry per dispatch: the round number it ran under
        self.dispatched: List[Dict[str, Any]] = []
        #: the plan's "current service owner" AT the moment of dispatch
        self.owners_at_dispatch: List[str] = []
        #: every reap the callback caused
        self.reaps: List[Dict[str, Any]] = []
        #: every terminal verdict the callback wrote
        self.terminals: List[Dict[str, Any]] = []
        #: every hand-off to the repair executor
        self.hand_offs: List[Dict[str, Any]] = []
        #: what the orchestrator's judgment phase returns
        self.result: Dict[str, Any] = {
            "status": "passed", "repair_tasks": [],
        }
        #: verdicts handed out one per call, when the test needs the
        #: chain to answer differently the second time round
        self.results: List[Dict[str, Any]] = []

    @property
    def instance_tokens(self) -> List[str]:
        """The token each dispatched round ran under."""
        return list(self.owners_at_dispatch)

    @property
    def last_terminal(self) -> Dict[str, Any]:
        return self.terminals[-1]


def _write_lease(plan_dir: Path, round_token: str) -> Path:
    """Write the plan's workflow lease, held by ``round_token``.

    A fresh heartbeat and this process' pid: the round behind the lease
    is alive, which is what makes the role untouchable.
    """
    path = vl._verification_workflow_lease_path(plan_dir)
    path.write_text(
        json.dumps({
            "round_token": round_token,
            "run_id": "run-other",
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(),
            "heartbeat_at": datetime.now().isoformat(),
        }),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def world(monkeypatch: Any, tmp_path: Path):
    """Wire the callback's world and hand back the recorder."""
    plan_id = "p-round-instance-wiring"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    w = _World(plan_id, plan_dir, project_dir)

    round_process_ledger.reset_for_tests()

    # --- the plan ---------------------------------------------------------
    monkeypatch.setattr(server, "_plan_dir", lambda _pid: plan_dir)
    monkeypatch.setattr(
        server, "_state_db_path", lambda: tmp_path / "state.db",
    )
    monkeypatch.setattr(
        server, "PlanState", lambda *_a, **_kw: MagicMock(),
    )
    monkeypatch.setattr(
        server, "create_coding_tool", lambda *_a, **_kw: MagicMock(),
    )

    def _no_verification_state():
        raise RuntimeError("this test has no verification state")

    monkeypatch.setattr(
        server, "_open_verification_state", _no_verification_state,
    )

    # --- the orchestrator -------------------------------------------------
    class _Orchestrator:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def check_cycle_conditions(
            self, _report: Any, round_number: Optional[int] = None,
        ) -> Dict[str, Any]:
            if w.results:
                return dict(w.results.pop(0))
            return dict(w.result)

        def confirm_repair_and_rerun(self) -> None:
            return None

    monkeypatch.setattr(server, "VerificationOrchestrator", _Orchestrator)

    # --- the terminal write, the reap, the hand-off ------------------------
    def _record_terminal(
        _pid: str, status: str, stop_reason: Optional[str], **kwargs: Any,
    ) -> None:
        w.terminals.append({
            "status": status,
            "stop_reason": stop_reason,
            **kwargs,
        })

    monkeypatch.setattr(
        server, "_record_verification_terminal", _record_terminal,
    )

    def _reap(
        _pid: str, _plan_dir: Path, reason: str,
        owner_token: Optional[str] = None, **_kwargs: Any,
    ) -> None:
        w.reaps.append({
            "reason": reason, "owner_token": owner_token,
        })

    monkeypatch.setattr(server, "_reap_managed_services", _reap)

    def _hand_off(*_a: Any, **kwargs: Any) -> None:
        w.hand_offs.append(kwargs)

    monkeypatch.setattr(
        server, "_run_repair_execution_async", _hand_off,
    )

    # --- the repair path ---------------------------------------------------
    monkeypatch.setattr(vl, "_repair_path_stop_reason", lambda _pid: "")
    monkeypatch.setattr(
        vl, "_get_pending_repair_tasks", lambda *_a, **_kw: [],
    )

    import verification_cancel as _vc_mod

    monkeypatch.setattr(_vc_mod, "is_cancelled", lambda _pid: False)

    # --- the state DB ------------------------------------------------------
    import state_machine.db.connection as _conn_mod
    import state_machine.db.schema as _schema_mod
    import state_machine.repositories.verification_repository as _vr_mod

    fake_conn = MagicMock(name="conn")
    monkeypatch.setattr(_conn_mod, "open", lambda *_a, **_kw: fake_conn)
    monkeypatch.setattr(_schema_mod, "migrate", lambda *_a, **_kw: None)

    class _VerificationRepository:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def current(self, _pid: str) -> Dict[str, Any]:
            return {"round": 1, "max_rounds": 5}

        def init_round(
            self, _pid: str, round_n: int, max_rounds: int,
        ) -> None:
            return None

    monkeypatch.setattr(
        _vr_mod, "VerificationRepository", _VerificationRepository,
    )

    # --- the dispatch ------------------------------------------------------
    def _dispatch(
        _pid: str, dispatch_plan_dir: Path, _orch: Any,
        round_number: int, **_kwargs: Any,
    ):
        w.dispatched.append({"round_number": round_number})
        w.owners_at_dispatch.append(
            service_manager.current_service_owner(dispatch_plan_dir),
        )
        return {}, None

    monkeypatch.setattr(vl, "_dispatch_verification_round", _dispatch)

    yield w

    round_process_ledger.reset_for_tests()


def _callback(world: _World):
    """The repair-complete callback, as the auto-loop builds it."""
    return vl._make_repair_on_complete(
        world.plan_id, world.project_dir,
        round_token="verification_loop-PREV",
    )


# ---------------------------------------------------------------------------
# The named cases
# ---------------------------------------------------------------------------


def test_reentry_claims_its_own_owner_token(world: _World) -> None:
    """The re-entered round owns the role under a name of its own."""
    _callback(world)(0)

    assert world.dispatched, "the re-entered round never dispatched"
    owner = world.owners_at_dispatch[-1]
    assert owner, (
        "the re-entered round dispatched with no current service owner — "
        "the services its preflight brings up would belong to nobody"
    )
    assert owner != "verification_loop-PREV", (
        "the re-entered round reused the outer loop's token — a cleanup "
        "aimed at the new instance would reach the dead round through it"
    )


def test_two_instances_same_round_number_differ(world: _World) -> None:
    """Two re-entries of the same round number are two instances."""
    cb = _callback(world)
    cb(0)
    cb(0)

    assert len(world.instance_tokens) == 2, "both rounds should have run"
    first, second = world.instance_tokens
    assert first and second, "an unnamed instance cannot be addressed"
    assert first != second, (
        "two live instances shared one name — a cleanup aimed at one of "
        "them would reach the other"
    )
    # The ROUND number is the same both times: the round did not advance,
    # only the instance did.
    assert (
        world.dispatched[0]["round_number"]
        == world.dispatched[1]["round_number"]
    )


def test_terminal_exit_reaps_with_instance_token(world: _World) -> None:
    """A terminal round reaps the services under its own token."""
    world.result = {"status": "passed", "repair_tasks": []}

    _callback(world)(0)

    assert world.reaps, "the terminal round never reaped its services"
    assert world.reaps[-1]["owner_token"] == world.instance_tokens[-1], (
        "the reap was aimed at a token that is not this instance's — it "
        "would stop somebody else's services, or none of this one's"
    )


def test_handoff_does_not_reap(world: _World) -> None:
    """A hand-off releases the role but leaves the services running."""
    world.result = {
        "status": "failed",
        "repair_tasks": [{"id": "RP-1", "title": "fix it"}],
    }

    _callback(world)(0)

    assert world.hand_offs, "the repair hand-off never happened"
    assert not world.reaps, (
        "a hand-off reaped the services — the repair executor is still "
        "in flight and the workflow has not exited"
    )


def test_terminal_write_carries_instance_token(world: _World) -> None:
    """The verdict is filed under the instance that produced it."""
    world.result = {"status": "passed", "repair_tasks": []}

    _callback(world)(0)

    assert world.terminals, "no terminal verdict was written"
    terminal = world.last_terminal
    assert terminal["status"] == "passed"
    assert terminal["round_token"] == world.instance_tokens[-1], (
        "the verdict was filed under another round's name"
    )
    assert terminal["round_token"] != "verification_loop-PREV"


def test_aborts_when_a_live_round_holds_ownership(world: _World) -> None:
    """A live round keeps the role: the re-entry refuses to start."""
    other = "verification_loop-OTHER"
    service_manager.register_service_owner(
        world.plan_dir, other, current=True,
    )
    _write_lease(world.plan_dir, other)

    _callback(world)(0)

    assert not world.dispatched, (
        "the re-entry dispatched although a live round holds the role"
    )
    assert world.terminals, "the refusal was not written down"
    terminal = world.last_terminal
    assert terminal["stop_reason"] == "service_owner_claim_refused"
    assert terminal["status"] == "failed"

    # The live round still holds the role — nothing was taken from it.
    registry = service_manager.read_owners(world.plan_dir)
    assert registry.get("current_owner") == other
    # And the refused instance left no residue of its own.
    owners = registry.get("owners") or []
    assert all(
        str(entry.get("token")) != terminal["round_token"]
        for entry in owners
    ), "the refused instance left its name in the owner registry"


def test_the_handing_over_round_does_not_block_the_reentry(
    world: _World,
) -> None:
    """The round that scheduled this repair handed the plan over.

    It is over the moment it dispatched the executor: its verdict is
    written and the chain continues in THIS callback.  The ownership it
    leaves behind is therefore stale — the role answers "which round is
    starting services RIGHT NOW", and the round that starts them is
    this one.

    A role left behind by the handing-over round would refuse every
    re-entry, and the repair chain would stop at its first round: the
    re-entry would read the predecessor's live lease, refuse to start,
    and record ``service_owner_claim_refused`` — the plan would end
    after the repair that was supposed to continue it.
    """
    previous = "verification_loop-PREV"
    service_manager.register_service_owner(
        world.plan_dir, previous, current=True,
    )
    _write_lease(world.plan_dir, previous)

    _callback(world)(0)

    assert world.dispatched, (
        "the re-entry refused to start because the round that scheduled "
        "the repair still held the role — the repair chain stops at its "
        "first re-entry"
    )
    owner = world.owners_at_dispatch[-1]
    assert owner and owner != previous, (
        f"the re-entry dispatched under {owner!r} instead of a name of "
        "its own — the services its preflight brings up would belong to "
        "the round that handed over"
    )


def test_the_refusal_reason_ends_the_chain() -> None:
    """``service_owner_claim_refused`` is a verdict, not a stray string.

    The recorder classifies a terminal write by its STATUS; the stop
    reason only describes it.  This reason ends the chain in its own
    right as well — a refused round dispatched nothing, and the plan
    is left with no round of its own — so a caller that ever reported
    it with a non-terminal status must not leave the plan parked in
    the verification family, looking as though a round were still
    running behind it.
    """
    reason = vl.SERVICE_OWNER_CLAIM_REFUSED_STOP_REASON
    assert reason == "service_owner_claim_refused"
    assert vl._terminal_write_ends_the_chain(
        "failed", reason, None,
    ) is True
    assert vl._terminal_write_ends_the_chain(
        "running", reason, None,
    ) is True
    # An explicit answer from the caller still wins: a mid-loop write
    # must not end the chain behind the executor's back.
    assert vl._terminal_write_ends_the_chain(
        "failed", reason, False,
    ) is False


def test_start_resume_path_mints_its_own_token(world: _World) -> None:
    """``/start``'s repair-resume path names its round too.

    That path has no round to inherit — it is handed an empty token —
    so before 2026-11-13 its round was unnamed AND unowned: nothing
    was opened, nothing was registered, and the services its preflight
    brought up belonged to nobody.
    """
    callback = vl._make_repair_on_complete(
        world.plan_id, world.project_dir, round_token="",
    )
    callback(0)

    assert world.dispatched, "the re-entered round never dispatched"
    owner = world.instance_tokens[-1]
    assert owner, (
        "the /start resume path dispatched an unnamed round — nothing "
        "could own the services it brought up"
    )


def test_pure_split_recursion_mints_a_second_token(world: _World) -> None:
    """A pure split re-enters the callback — under a SECOND name.

    The recursion happens inside the SAME closure, so a name kept in
    the enclosing scope would be overwritten by the inner call: the
    outer instance would end up releasing (and reaping) the inner
    one's name.
    """
    world.results = [
        {
            "status": "failed",
            "repair_tasks": [],
            "vp_splits": [{"vp_id": "VP-1"}],
        },
        {"status": "passed", "repair_tasks": []},
    ]

    _callback(world)(0)

    assert len(world.instance_tokens) == 2, (
        "the pure split did not run a second round"
    )
    first, second = world.instance_tokens
    assert first and second, "an unnamed instance cannot be addressed"
    assert first != second, (
        "the recursive call reused the outer instance's name — the outer "
        "instance would release (and reap) the inner one's"
    )
    # Only the round that really ended reaps.
    assert len(world.reaps) == 1, (
        f"expected exactly one reap, got {world.reaps!r}"
    )
    assert world.reaps[0]["owner_token"] == second
