"""The verification round's token, and the coding tools it owns (2026-11-01).

A verification round already has a name: the service-owner token the
workflow lease is taken with.  What it did not have was a **ledger** — a
record of which of the round's coding-tool subprocesses are alive — so a
round judged dead could only take down the VP handles and the ``/tmp``
orphans, and the orchestrator's own ``coding_query_*`` child kept running
for hours after the plan had been written off as failed.

This file pins the wiring between the loop and that ledger:

  * the loop begins the round with the service-owner token it took the
    lease with — one identity, not two;
  * the in-memory verification state carries that token, because the
    watchdog reads it there to learn *which* round to clean up;
  * the orchestrator's own coding tool is registered under that round, so
    a kill aimed at the round reaches the round's processes;
  * the round is closed in a ``finally``, so the path where the loop
    raises leaves no round behind — a round nobody can address is a
    round nobody can clean up.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the project
directory and the state database.  No coding tool is started (the fake
one stands in for it), no process is spawned, and the ledger — which is
process-wide state — is reset around every test so one test's round can
never lend its tool to the next one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import round_process_ledger  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """The orchestrator's coding tool, reduced to what the ledger sees.

    The ledger holds the tool so it can reach the tool's process; this
    test never starts one, so the tool only has to be a distinguishable
    object.
    """

    def __init__(self, name: str = "tool") -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - a debugging aid
        return f"<FakeCodingTool {self.name}>"


class _Boom(BaseException):
    """Not an ``Exception``: the loop's own handlers must not swallow it.

    The inner loop catches ``Exception`` around everything it does, so a
    test that wants to reach the ``finally`` — the path where the round
    has to be closed even though the loop died — has to raise something
    the loop does not handle.
    """


class _FakePlanState:
    """The plan's state machine, reduced to what the loop calls.

    A real ``PlanState`` writes into the operator's plan tree; the loop
    only asks it for a handful of verbs, and none of them is what this
    file is about.
    """

    def __init__(self, plan_dir: Path) -> None:
        self.plan_dir = plan_dir

    def ensure_row(self) -> None:
        # 2026-11-11: the loop asks for the routing row to exist before
        # its CAS (constructing a PlanState no longer creates it).
        pass

    def set_verification_max_rounds(self, max_rounds: int) -> None:
        pass

    def reload(self) -> None:
        pass

    def begin_verification(self, round_n: Optional[int] = None) -> None:
        pass

    def transition_to(self, phase: str) -> None:
        pass

    def get_current_phase(self) -> str:
        return "verification"


def _plan_dir(tmp_path: Path, plan_id: str) -> Path:
    """The plan's directory — the ledger is keyed by it."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_dir(tmp_path: Path) -> Path:
    """The project directory — where the coding tool would have run."""
    path = tmp_path / "proj"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _orchestrator_factory(
    capture: Dict[str, Any], token: str, boom: Optional[BaseException] = None,
) -> Any:
    """Build a fake orchestrator that reads the ledger as it is built.

    The coding tool is registered *before* the orchestrator is built, so
    the constructor is the first moment at which "is the tool in the
    round?" is a fact that can be read rather than inferred.
    """

    class _FakeOrchestrator:
        def __init__(
            self, plan_dir: Path, project_dir: Path,
            coding_tool: Any = None, verif_repo: Any = None,
        ) -> None:
            self.plan_dir = plan_dir
            self.coding_tool = coding_tool
            capture["tool"] = coding_tool
            capture["tools"] = round_process_ledger.tools_for_round(
                plan_dir, token,
            )
            if boom is not None:
                raise boom

        def start_verification_cycle(self, *args: Any, **kwargs: Any) -> Dict:
            return {"verification_results": []}

    return _FakeOrchestrator


def _wire_inner(
    monkeypatch: Any,
    tmp_path: Path,
    plan_dir: Path,
    token: str,
    tool: Any,
    capture: Dict[str, Any],
    boom: Optional[BaseException] = None,
) -> None:
    """Everything the inner loop touches that is not the loop itself.

    The loop is production code and is not touched here.  What is
    replaced is everything that would reach the outside world: the
    coding tool (a real one spawns a subprocess), the plan state (a real
    one writes into the operator's plan tree), the state database and
    the orchestrator.
    """

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no verification repository in this test")

    monkeypatch.setattr(srv, "_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(srv, "PlanState", _FakePlanState)
    monkeypatch.setattr(srv, "_open_verification_state", _refuse)
    monkeypatch.setattr(
        srv, "_record_verification_terminal", lambda *a, **k: None,
    )
    monkeypatch.setattr(srv, "create_coding_tool", lambda *a, **k: tool)
    monkeypatch.setattr(
        srv, "VerificationOrchestrator",
        _orchestrator_factory(capture, token, boom),
    )


@pytest.fixture(autouse=True)
def _clean_ledger():
    """The ledger is process-wide state — a test must not lend its round
    to the next one."""
    round_process_ledger.reset_for_tests()
    yield
    round_process_ledger.reset_for_tests()


# ---------------------------------------------------------------------------
# 1. The round is begun with the service-owner token
# ---------------------------------------------------------------------------


def test_loop_begins_the_round_with_the_service_owner_token(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The round is opened with the token the lease was taken with.

    One identity: the lease, the ledger and the state all name the same
    round, so a kill aimed at the round reaches that round's processes
    and nobody else's.

    The body of the loop is replaced — this is about the wiring around
    it — and the replacement registers a tool from inside the loop: a
    registration that cannot be attributed to a round is dropped, so a
    tool that shows up in the round is proof that the round was open
    while the loop ran.
    """
    plan_id = "plan-round-token"
    plan_dir = _plan_dir(tmp_path, plan_id)
    project_dir = _project_dir(tmp_path)

    token = "verification_loop-4242-abcd"
    monkeypatch.setattr(
        srv, "_new_service_owner_token", lambda prefix="run": token,
    )
    monkeypatch.setattr(
        srv, "_reap_managed_services", lambda *a, **k: None,
    )

    seen: Dict[str, Any] = {}

    def fake_inner(plan_id_: str, plan_dir_: Path, project_dir_: Path,
                   **kwargs: Any) -> None:
        seen["round_token"] = kwargs.get("round_token")
        round_process_ledger.register_tool(plan_dir_, _FakeCodingTool())
        seen["tools"] = round_process_ledger.tools_for_round(
            plan_dir_, seen["round_token"],
        )

    monkeypatch.setattr(srv, "_run_auto_verification_loop_inner", fake_inner)

    outcome = vl._run_auto_verification_loop(
        plan_id, plan_dir, project_dir,
    )

    assert outcome.refused is False, (
        f"the round was refused: {outcome!r} — the lease is the only thing "
        f"that may refuse a round, and nothing else holds the plan"
    )
    assert seen["round_token"] == token, (
        f"the loop body was handed {seen['round_token']!r} instead of the "
        f"service-owner token {token!r} — the round's name and the round "
        f"the lease was taken with have to be the same name"
    )
    assert len(seen["tools"]) == 1, (
        f"the tool registered from inside the loop did not land in the "
        f"round ({seen['tools']!r}) — the round was not open while the "
        f"loop ran"
    )


# ---------------------------------------------------------------------------
# 2. The state carries the round's token
# ---------------------------------------------------------------------------


def test_state_carries_the_round_token(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The in-memory state carries the token, because that is where the
    watchdog reads it.

    The watchdog does not hold the ledger: it reads
    ``_verification_state[plan_id]`` and asks "which round is this?" to
    find out what to clean up.  A state without the token cannot answer
    that, and the cleanup falls back to killing nothing.
    """
    plan_id = "plan-round-state"
    plan_dir = _plan_dir(tmp_path, plan_id)
    project_dir = _project_dir(tmp_path)

    token = "verification_loop-7777-beef"
    srv._verification_state.pop(plan_id, None)

    capture: Dict[str, Any] = {}
    _wire_inner(
        monkeypatch, tmp_path, plan_dir, token,
        _FakeCodingTool(), capture,
    )

    vl._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, round_token=token,
    )

    state = srv._verification_state.get(plan_id) or {}
    assert state.get("round_token") == token, (
        f"the verification state carries {state.get('round_token')!r} "
        f"instead of {token!r} — the watchdog reads the token from here"
    )


# ---------------------------------------------------------------------------
# 3. The orchestrator's own tool is registered under the round
# ---------------------------------------------------------------------------


def test_orchestrator_tool_is_registered_under_the_round(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The orchestrator's coding tool is IN the round.

    The tool is the one process population the old cleanup could not
    reach: it is not a registry handle, so nothing outside the tool
    knew it existed, and its ``coding_query_*`` child outlived the round
    that started it.  Registering it is what puts it within reach.
    """
    plan_id = "plan-round-tool"
    plan_dir = _plan_dir(tmp_path, plan_id)
    project_dir = _project_dir(tmp_path)

    token = "verification_loop-1357-acef"
    srv._verification_state.pop(plan_id, None)

    tool = _FakeCodingTool("orchestrator")
    capture: Dict[str, Any] = {}
    _wire_inner(
        monkeypatch, tmp_path, plan_dir, token, tool, capture,
        boom=RuntimeError("stop the loop right here"),
    )

    vl._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, round_token=token,
    )

    assert capture["tool"] is tool, (
        "the orchestrator was not built with the tool the loop created — "
        "the test is not measuring the tool it thinks it is"
    )
    assert list(capture["tools"]) == [tool], (
        f"the orchestrator's tool is not in the round ({capture['tools']!r}) "
        f"— a kill aimed at the round would not reach it"
    )


# ---------------------------------------------------------------------------
# 4. The round is closed in a finally
# ---------------------------------------------------------------------------


def test_loop_ends_the_round_in_finally(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The round is closed even when the loop dies.

    The loop has a dozen exits and one of them is an exception.  A round
    that is still open after such an exit is a round the next round's
    registration would land in — and a round nobody closed is a round
    nobody can address.
    """
    plan_id = "plan-round-finally"
    plan_dir = _plan_dir(tmp_path, plan_id)
    project_dir = _project_dir(tmp_path)

    token = "verification_loop-2468-dead"
    srv._verification_state.pop(plan_id, None)

    tool = _FakeCodingTool("orchestrator")
    capture: Dict[str, Any] = {}
    _wire_inner(
        monkeypatch, tmp_path, plan_dir, token, tool, capture,
        boom=_Boom("the loop exploded"),
    )

    with pytest.raises(_Boom):
        vl._run_auto_verification_loop_inner(
            plan_id, plan_dir, project_dir, round_token=token,
        )

    assert list(capture["tools"]) == [tool], (
        "the round was not open while the loop ran — the closing below "
        "would prove nothing"
    )

    # The round is closed: a registration now lands nowhere, because a
    # tool that belongs to no round is a tool nobody may kill.
    round_process_ledger.register_tool(plan_dir, _FakeCodingTool("after"))
    assert round_process_ledger.tools_for_round(plan_dir, token) == [], (
        "the round survived the loop's death — the finally did not close it"
    )


# ---------------------------------------------------------------------------
# 5. The boundary conditions
# ---------------------------------------------------------------------------


def test_the_boundary_conditions_register_nothing(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """A missing plan directory and an empty token both register nothing.

    Two of the brief's boundary conditions, and both of them have to be
    *quiet*: the ledger is bookkeeping around a verification, and
    bookkeeping that raises takes the verification down with it.

    The missing directory is the one a caller can hand over by accident
    — the plan tree is not always where it was a moment ago — so the
    registration is skipped instead of being attempted.

    The empty token is what every caller that predates this wiring
    passes, because ``round_token`` defaults to ``""``.  Such a caller
    gets the loop it had before: no round, no token in the state.
    """
    plan_id = "plan-round-boundary"
    missing_dir = tmp_path / "plans" / plan_id  # never created
    project_dir = _project_dir(tmp_path)

    token = "verification_loop-9999-0000"

    # A plan directory that does not exist: nothing is registered and
    # nothing raises.
    vl._open_round_in_ledger(missing_dir, token)
    vl._register_round_tool(missing_dir, token, _FakeCodingTool())
    assert not missing_dir.exists(), (
        "the boundary case created the plan directory it was given"
    )
    assert round_process_ledger.tools_for_round(missing_dir, token) == [], (
        "a round was opened for a plan directory that does not exist"
    )

    # The default token: the old callers keep the old behaviour.
    plan_dir = _plan_dir(tmp_path, plan_id)
    srv._verification_state.pop(plan_id, None)

    capture: Dict[str, Any] = {}
    _wire_inner(
        monkeypatch, tmp_path, plan_dir, "", _FakeCodingTool(), capture,
    )

    vl._run_auto_verification_loop_inner(plan_id, plan_dir, project_dir)

    state = srv._verification_state.get(plan_id) or {}
    assert "round_token" not in state, (
        f"the state grew a round_token ({state.get('round_token')!r}) for a "
        f"caller that named no round — the old callers must be untouched"
    )
