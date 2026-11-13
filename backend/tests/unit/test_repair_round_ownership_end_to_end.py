"""End-to-end acceptance of the round-instance ownership chain (2026-11-13).

The criterion this file pins
----------------------------
``_make_repair_on_complete`` builds the callback that re-enters
verification after a repair execution finished.  That re-entry is a
ROUND INSTANCE of its own: it mints a name, claims the plan's "current
service owner" role, opens its scope in the process ledger, dispatches
the round, and — when the round really ended — releases the role and
reaps the services it started.

The unit-level file (``test_repair_round_instance_wiring.py``) pins
each of those steps in isolation.  This file pins the CHAIN: the same
production callback, driven twice in a row on ONE plan directory, with
the round number unchanged — the situation a real repair chain produces
when a round hands the plan to the next instance of the same round.

The four things that have to hold, end to end:

  1. **identity** — two instances of the same round are two names.
     A cleanup aimed at one of them must not reach the other, and
     neither may inherit the token of the round that scheduled the
     repair.
  2. **ownership** — the services the round's preflight brings up
     belong to the instance that is in flight.  The owner the plan's
     registry names AT THE MOMENT OF THE DISPATCH is that instance's
     own token.
  3. **no leak** — when the chain ends, the registry names nobody.
     A role left behind is a role the next round may not take, and the
     services of a round that is over would stay recorded under it.
  4. **reap** — every terminal instance reaps the services it started,
     under its own token, exactly once.

What is NOT here
----------------
No service is started, no process is signalled and no port is opened:
the reap is a recorder and the dispatch is a stub.  The dispatch stub
samples the plan's owner registry at the moment of the dispatch, which
is the only moment at which "whose services are these?" has an answer.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the owner
registry, the process ledger.  The plan directory the callback resolves
is patched to the test's own, so the real plans root is never written
to.  The ledger is process-wide state, so it is reset around the test.

No second server process is started: the module import below proves the
import surface is intact, and nothing in this file starts a server.
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

#: The token of the round that scheduled the repair.  It is the token
#: the callback is HANDED — the round that dispatched the executor is
#: over by the time the callback runs, so this name belongs to a dead
#: round and no instance may inherit it.
_PREVIOUS_ROUND_TOKEN = "verification_loop-PREV"


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


class _Chain:
    """What the chain did, recorded as it happened."""

    def __init__(
        self, plan_id: str, plan_dir: Path, project_dir: Path,
    ) -> None:
        self.plan_id = plan_id
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        #: one entry per dispatch: the round number it ran under, the
        #: owner the registry named AT THE MOMENT OF THE DISPATCH, and
        #: the raw registry value behind that owner
        self.dispatched: List[Dict[str, Any]] = []
        #: every reap the chain caused
        self.reaps: List[Dict[str, Any]] = []
        #: every terminal verdict the chain wrote
        self.terminals: List[Dict[str, Any]] = []
        #: every hand-off to the repair executor
        self.hand_offs: List[Dict[str, Any]] = []

    @property
    def instance_tokens(self) -> List[str]:
        """The token each dispatched round ran under."""
        return [str(entry.get("owner") or "") for entry in self.dispatched]

    @property
    def reaped_tokens(self) -> List[str]:
        """The token each reap was aimed at."""
        return [str(entry.get("owner_token") or "") for entry in self.reaps]

    @property
    def round_numbers(self) -> List[int]:
        """The round number each dispatched round ran under."""
        return [int(entry.get("round_number") or 0) for entry in self.dispatched]


# ---------------------------------------------------------------------------
# The fixture: the callback's world, with the outside world stubbed out
# ---------------------------------------------------------------------------


@pytest.fixture
def chain(monkeypatch: Any, tmp_path: Path):
    """Wire the callback's world and hand back the recorder."""
    plan_id = "p-round-ownership-e2e"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    rec = _Chain(plan_id, plan_dir, project_dir)

    round_process_ledger.reset_for_tests()

    # --- the plan ---------------------------------------------------------
    # The callback resolves the plan through the server; the patch keeps
    # every write inside ``tmp_path``.
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
            # A round that ends: passed, with nothing left to repair.
            return {"status": "passed", "repair_tasks": []}

        def confirm_repair_and_rerun(self) -> None:
            return None

    monkeypatch.setattr(server, "VerificationOrchestrator", _Orchestrator)

    # --- the terminal write, the reap, the hand-off ------------------------
    def _record_terminal(
        _pid: str, status: str, stop_reason: Optional[str], **kwargs: Any,
    ) -> None:
        rec.terminals.append({
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
        rec.reaps.append({
            "reason": reason, "owner_token": owner_token,
        })

    monkeypatch.setattr(server, "_reap_managed_services", _reap)

    def _hand_off(*_a: Any, **kwargs: Any) -> None:
        rec.hand_offs.append(kwargs)

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
    # The round number the chain continues from.  Both instances run the
    # SAME round: the round did not advance, only the instance did.
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
    # The stub samples the plan's owner registry AT THE MOMENT OF THE
    # DISPATCH: that is the only moment at which "whose services are
    # these?" has an answer.
    def _dispatch(
        _pid: str, dispatch_plan_dir: Path, _orch: Any,
        round_number: int, **_kwargs: Any,
    ):
        owner = service_manager.current_service_owner(dispatch_plan_dir)
        rec.dispatched.append({
            "round_number": round_number,
            "owner": owner,
            "registry_owner": service_manager.read_owners(
                dispatch_plan_dir,
            ).get("current_owner"),
            # The round's ledger scope, sampled under the name the
            # registry holds: the tools this round registered are the
            # processes a kill aimed at the round has to reach.
            "ledger_tools": list(
                round_process_ledger.tools_for_round(
                    dispatch_plan_dir, owner,
                ),
            ),
        })
        return {}, None

    monkeypatch.setattr(vl, "_dispatch_verification_round", _dispatch)

    yield rec

    round_process_ledger.reset_for_tests()


def _callback(chain: _Chain):
    """The repair-complete callback, as the auto-loop builds it."""
    return vl._make_repair_on_complete(
        chain.plan_id, chain.project_dir,
        round_token=_PREVIOUS_ROUND_TOKEN,
    )


def _run_two_instances(chain: _Chain) -> None:
    """Drive two instances of the same round on the same plan.

    Two callbacks, built the way the auto-loop builds them, both handed
    the token of the round that scheduled the repair.  Each of them
    enters a round instance of its own.
    """
    _callback(chain)(0)
    _callback(chain)(0)


# ---------------------------------------------------------------------------
# 1. Identity — two instances of one round are two names
# ---------------------------------------------------------------------------


def test_two_round_instances_on_one_plan_never_share_identity(
    chain: _Chain,
) -> None:
    """Two instances of the same round carry different names.

    The round number did not advance — only the instance did — so the
    two runs are two instances of ONE round.  A cleanup aimed at one of
    them must not reach the other, and neither may inherit the name of
    the round that scheduled the repair: that round is over by the time
    the callback runs, and a service recorded under its name belongs to
    a round nobody is running.
    """
    _run_two_instances(chain)

    assert len(chain.dispatched) == 2, (
        f"expected two dispatched rounds, got {chain.dispatched!r}"
    )
    first, second = chain.instance_tokens

    assert first and second, (
        "an unnamed instance cannot be addressed — nothing could own the "
        "services it brought up, and nothing could clean them up"
    )
    assert first != second, (
        f"two live instances shared one name ({first!r}) — a cleanup "
        "aimed at one of them would reach the other"
    )
    assert first != _PREVIOUS_ROUND_TOKEN, (
        "the first instance inherited the name of the round that "
        "scheduled the repair"
    )
    assert second != _PREVIOUS_ROUND_TOKEN, (
        "the second instance inherited the name of the round that "
        "scheduled the repair"
    )

    # The scenario under test: the SAME round, twice.
    assert chain.round_numbers[0] == chain.round_numbers[1], (
        f"the two instances ran different rounds ({chain.round_numbers!r}) "
        "— the scenario is two instances of ONE round"
    )

    # Each instance ran its own ledger scope, and closed it on the way
    # out: a scope nobody closed is a scope the next registration
    # lands in, and a scope that outlived its round would put the next
    # round's processes within reach of the wrong kill.
    for index, dispatch in enumerate(chain.dispatched):
        assert dispatch["ledger_tools"], (
            f"instance {index} ({chain.instance_tokens[index]!r}) "
            "registered nothing in the process ledger — a kill aimed at "
            "the round would not reach the round's own processes"
        )
    for token in chain.instance_tokens:
        assert round_process_ledger.tools_for_round(
            chain.plan_dir, token,
        ) == [], (
            f"the ledger still carries the scope of {token!r} after the "
            "chain ended — the instance did not close its round"
        )


# ---------------------------------------------------------------------------
# 2. Ownership — the services belong to the instance in flight
# ---------------------------------------------------------------------------


def test_services_at_dispatch_time_belong_to_the_instance_in_flight(
    chain: _Chain,
) -> None:
    """The owner named at the moment of the dispatch is this instance.

    The round's preflight brings the plan's services up, and they are
    recorded under the plan's "current service owner".  If that name is
    not the instance that is running, the services belong to a round
    that is over — and a service nobody owns is a service nobody may
    reap.
    """
    _run_two_instances(chain)

    for index, dispatch in enumerate(chain.dispatched):
        instance_token = chain.instance_tokens[index]
        assert dispatch["owner"], (
            f"instance {index} dispatched with no current service owner — "
            "the services its preflight brought up would belong to nobody"
        )
        assert dispatch["owner"] == instance_token, (
            f"instance {index} dispatched under {dispatch['owner']!r} "
            f"instead of its own name {instance_token!r} — the services "
            "its preflight brought up would be recorded under another "
            "round"
        )
        assert dispatch["registry_owner"] == instance_token, (
            f"the registry named {dispatch['registry_owner']!r} while "
            f"instance {index} ({instance_token!r}) was dispatching — the "
            "role was held by somebody else"
        )
        assert dispatch["ledger_tools"], (
            f"instance {index} ({instance_token!r}) dispatched with an "
            "empty ledger scope — the processes of this round would be "
            "out of reach of a kill aimed at the round"
        )

    # And the instance's own name is the one the verdict was filed
    # under: the round that ran and the round that was recorded are
    # the same round.
    assert chain.terminals, "the chain wrote no verdict"
    for index, terminal in enumerate(chain.terminals):
        assert terminal["round_token"] == chain.instance_tokens[index], (
            f"verdict {index} was filed under {terminal['round_token']!r} "
            f"instead of the instance that produced it "
            f"({chain.instance_tokens[index]!r})"
        )


# ---------------------------------------------------------------------------
# 3. No leak — the chain ends with nobody owning the plan
# ---------------------------------------------------------------------------


def test_registry_has_no_current_owner_after_the_chain_ends(
    chain: _Chain,
) -> None:
    """When the chain is over, the registry names nobody.

    A role left behind is a role the next round may not take: the
    claim is refused while the holder looks alive, and the services of
    a round that is over stay recorded under a name that belongs to
    nobody.  Both instances released the role on their way out — that
    is what makes the second instance's claim possible at all.
    """
    _run_two_instances(chain)

    registry = service_manager.read_owners(chain.plan_dir)
    assert registry.get("current_owner") in (None, ""), (
        f"the registry still names {registry.get('current_owner')!r} as "
        "the current owner after the chain ended — the next round would "
        "inherit a role that belongs to a round that is over"
    )

    # Neither instance left its name behind.
    remaining = {
        str(entry.get("token") or "")
        for entry in (registry.get("owners") or [])
    }
    for token in chain.instance_tokens:
        assert token not in remaining, (
            f"the owner registry still carries {token!r} after the chain "
            "ended — the instance did not release its role"
        )


# ---------------------------------------------------------------------------
# 4. Reap — every terminal instance reaps its own services
# ---------------------------------------------------------------------------


def test_each_terminal_instance_reaps_its_own_token(
    chain: _Chain,
) -> None:
    """Each round that ended reaped the services it started.

    The reap is aimed at a token: the services of a round that is
    released are reapable by anybody, so a reap aimed at the wrong
    token stops somebody else's services — or none of this one's.  One
    reap per terminal instance, each under its own name.
    """
    _run_two_instances(chain)

    assert chain.reaped_tokens == chain.instance_tokens, (
        f"the reaps ({chain.reaped_tokens!r}) do not match the instances "
        f"that ran ({chain.instance_tokens!r}) — a reap aimed at the "
        "wrong token stops somebody else's services"
    )
    assert len(chain.reaped_tokens) == 2, (
        f"expected one reap per terminal instance, got "
        f"{chain.reaped_tokens!r}"
    )
