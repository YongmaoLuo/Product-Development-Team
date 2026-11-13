"""The repair callback's round instance is named in every ledger (2026-11-13).

The criterion this file pins
----------------------------
A round that re-enters verification after a repair has three places
that must name the SAME round:

  * the **process ledger** scope it opens (the tools it creates belong
    to it — a tool that belongs to no round is a tool nobody may kill);
  * the **plan's "current service owner"** role it holds (the services
    its preflight brings up are recorded under that name — a service
    recorded with no owner is a service nobody may reap);
  * the **in-memory state**'s ``round_token`` (the watchdog reads it to
    learn which round to clean up).

Before 2026-11-13 the re-entry opened its ledger scope under the token
of the loop that scheduled the repair — a loop that had already ended —
and never held the owner role at all.  The services its preflight
brought up were therefore recorded with ``token=""``: ownerless, and
so unreapable by anybody.

So the order is fixed, and the order is the contract:

    mint → claim the owner role → open the ledger scope → stamp the
    state → preflight / dispatch

Reversing it puts the preflight's services back into the ownerless
state the whole change exists to remove: the role has to be held
BEFORE the services are brought up, not after.

What this file pins:

  1. the ledger scope is opened under a FRESH, non-empty token — not
     the outer loop's;
  2. the owner role is ALREADY held, under that same token, at the
     moment the ledger scope opens;
  3. the state's stamp carries that same token.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the owner
registry and the process ledger are the test's own.  No service is
started and no process is signalled.
"""

from __future__ import annotations

import sys
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
    """The ledgers the callback writes to, recorded as it writes them."""

    def __init__(
        self, plan_id: str, plan_dir: Path, project_dir: Path,
    ) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        #: one entry per ledger scope the callback opened
        self.opened: List[Dict[str, Any]] = []
        #: one entry per state stamp the callback wrote
        self.stamped: List[Dict[str, Any]] = []
        #: the owner role at the moment each ledger scope opened
        self.owner_at_open: List[str] = []


@pytest.fixture
def world(monkeypatch: Any, tmp_path: Path):
    """Wire the callback's world and hand back the recorder."""
    plan_id = "p-round-instance-ledger"
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
            return {"status": "passed", "repair_tasks": []}

        def confirm_repair_and_rerun(self) -> None:
            return None

    monkeypatch.setattr(server, "VerificationOrchestrator", _Orchestrator)

    # --- the writes this file watches -------------------------------------
    monkeypatch.setattr(
        server, "_record_verification_terminal",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        server, "_reap_managed_services", lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        server, "_run_repair_execution_async", lambda *_a, **_kw: None,
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
    monkeypatch.setattr(
        vl, "_dispatch_verification_round",
        lambda *_a, **_kw: ({}, None),
    )

    # --- the ledgers this file watches -------------------------------------
    def _open_round(open_plan_dir: Path, round_token: str) -> None:
        w.opened.append({
            "plan_dir": open_plan_dir, "round_token": round_token,
        })
        # What the role reads AS the scope opens — the owner has to be
        # held by then, or the services the preflight brings up are
        # ownerless.
        w.owner_at_open.append(
            service_manager.current_service_owner(open_plan_dir),
        )

    monkeypatch.setattr(vl, "_open_round_in_ledger", _open_round)

    def _stamp(plan_id: str, round_token: str) -> None:
        w.stamped.append({
            "plan_id": plan_id, "round_token": round_token,
        })

    monkeypatch.setattr(vl, "_stamp_round_token", _stamp)

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


def test_open_round_receives_a_fresh_nonempty_token(world: _World) -> None:
    """The ledger scope is opened under a token of this instance's own."""
    _callback(world)(0)

    assert world.opened, "the re-entered round opened no ledger scope"
    token = world.opened[-1]["round_token"]
    assert token, (
        "the ledger scope was opened with an empty token — a tool "
        "registered under it belongs to no round and nobody may kill it"
    )
    assert token != "verification_loop-PREV", (
        "the ledger scope reused the outer loop's token — the round that "
        "owns it is over"
    )


def test_owner_is_registered_before_the_round_opens(world: _World) -> None:
    """The owner role is held BEFORE the ledger scope opens."""
    _callback(world)(0)

    assert world.opened, "the re-entered round opened no ledger scope"
    token = world.opened[-1]["round_token"]
    owner = world.owner_at_open[-1]
    assert owner == token, (
        "the owner role was not held when the round opened — the "
        f"services its preflight brings up would be recorded under "
        f"{owner!r} instead of {token!r}"
    )


def test_round_token_is_stamped_with_the_same_token(world: _World) -> None:
    """The state's stamp names the same round the ledger scope does."""
    _callback(world)(0)

    assert world.opened, "the re-entered round opened no ledger scope"
    assert world.stamped, "the re-entered round stamped no round token"
    assert world.stamped[-1]["round_token"] == world.opened[-1][
        "round_token"
    ], (
        "the state names a different round than the ledger scope — the "
        "watchdog would clean up a round that is not this one"
    )
