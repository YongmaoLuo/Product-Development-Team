"""The round has to read **its own** service ledger (2026-11-20).

Why this file exists
--------------------
A verification round starts the services its points talk to, and it
writes down what it started — the plan's **ledger**
(``process_ledger.jsonl``).  The ledger is the only record that says
"this process, on this port, is ours".

The round's ``ensure`` did not get to read it.  ``ensure_services``
takes the plan's directory as an optional argument, and the round left
it out, so the ownership question ("is the holder of the declared port
*our* leftover?") fell back to a scan of the whole plans tree — a
guess, made from a directory that does not even hold this plan's
ledger.  The guess found nothing, so the holder was taken for a
stranger, the reclaim was attempted without an identity anchor, and
the stop was refused: the port stayed held and the round never got
its service.

The fix is one argument: the round hands its own plan directory to the
call that starts its services.  The two calls that do that are the
round-start preflight and the re-ensure that brings a dead service
back.

What this file pins
-------------------
Both calls receive the plan's **own** directory and the plan's id —
the two things that let ``ensure_services`` read the plan's own
ledger instead of guessing.

The case under test is the real method on a real agent; only the
``service_manager`` module is stood in for, because what is asserted
is the **call**, not what the manager does with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
import verification_agent  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402


class _RecordingServiceManager:
    """A ``service_manager`` stand-in that records what it was asked.

    The real module starts processes and writes ledgers; neither is
    what this file is about.  What matters is the **arguments** the
    round passes, so they are recorded and nothing else is done.
    """

    def __init__(self) -> None:
        self.ensure_calls: list = []
        self.record_calls: list = []

    def ensure_services(self, project_dir, declarations, **kwargs):
        self.ensure_calls.append({
            "project_dir": project_dir,
            "declarations": declarations,
            "kwargs": dict(kwargs),
        })
        return sm.ServiceRuntimeMap()

    def record_runtimes(self, plan_dir, plan_id, runtime_map, **kwargs):
        self.record_calls.append({
            "plan_dir": plan_dir,
            "plan_id": plan_id,
            "kwargs": dict(kwargs),
        })
        return None


def _agent(tmp_path: Path) -> VerificationAgent:
    """An agent with an empty plan directory and an empty project."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir()
    project_dir.mkdir()
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )
    agent._service_declarations = {}
    return agent


def test_round_start_ensure_receives_the_plans_own_directory(
    tmp_path: Path, monkeypatch,
):
    """The round's ensure is told which plan's ledger to read.

    The round-start preflight is the call that brings the declared
    services up.  It has to hand the plan's own directory over, or the
    ownership question is answered by a scan of a plans tree that does
    not hold this plan's ledger — and the round's own leftover is then
    taken for a stranger's.

    The re-ensure is pinned here too: it is the same call with the same
    question, and a round that fixed one and not the other would still
    lose the port the second time.
    """
    agent = _agent(tmp_path)
    fake = _RecordingServiceManager()
    monkeypatch.setattr(
        verification_agent, "_load_service_manager",
        lambda project_dir=None: fake,
    )
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    agent._preflight_declared_services({"services": []})

    assert fake.ensure_calls, (
        "the round start never ensured its declared services"
    )
    kwargs = fake.ensure_calls[0]["kwargs"]
    assert kwargs.get("plan_dir") == agent.plan_dir, (
        f"the round start did not hand its own plan directory over: {kwargs}"
    )
    assert kwargs.get("plan_id") == agent.plan_dir.name, (
        f"the round start did not name its own plan: {kwargs}"
    )

    # The re-ensure: the same call, asked the same question.
    agent._ensure_declared_services({})

    assert len(fake.ensure_calls) == 2, fake.ensure_calls
    kwargs = fake.ensure_calls[1]["kwargs"]
    assert kwargs.get("plan_dir") == agent.plan_dir, (
        f"the re-ensure did not hand its own plan directory over: {kwargs}"
    )
    assert kwargs.get("plan_id") == agent.plan_dir.name, (
        f"the re-ensure did not name its own plan: {kwargs}"
    )


def test_the_round_start_ensure_still_records_the_runtimes(
    tmp_path: Path, monkeypatch,
):
    """Telling the manager about the plan does not stop the books.

    The ledger is written after the ensure, from the same plan
    directory.  A change that made the round pass its plan directory
    to the manager and forgot to write the ledger down would leave the
    next round with nothing to read.
    """
    agent = _agent(tmp_path)
    fake = _RecordingServiceManager()
    monkeypatch.setattr(
        verification_agent, "_load_service_manager",
        lambda project_dir=None: fake,
    )
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    agent._preflight_declared_services({"services": []})

    assert fake.record_calls, "the round start wrote no ledger"
    call = fake.record_calls[0]
    assert call["plan_dir"] == agent.plan_dir, call
    assert call["plan_id"] == agent.plan_dir.name, call


@pytest.mark.parametrize(
    "method_name",
    ["_preflight_declared_services", "_ensure_declared_services"],
)
def test_the_ensure_is_given_the_plan_directory_and_nothing_else(
    tmp_path: Path, monkeypatch, method_name: str,
):
    """The plan's directory travels as the two named arguments.

    The manager's signature takes the plan's directory and the plan's
    id; the round passes them by keyword.  A round that passed the
    directory positionally — or passed the plan's id as something else
    — would still be guessing.
    """
    agent = _agent(tmp_path)
    fake = _RecordingServiceManager()
    monkeypatch.setattr(
        verification_agent, "_load_service_manager",
        lambda project_dir=None: fake,
    )
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    if method_name == "_preflight_declared_services":
        agent._preflight_declared_services({"services": []})
    else:
        agent._ensure_declared_services({})

    kwargs = fake.ensure_calls[0]["kwargs"]
    assert kwargs.get("plan_dir") == agent.plan_dir, kwargs
    assert kwargs.get("plan_id") == agent.plan_dir.name, kwargs


class _NarrowServiceManager:
    """A manager whose ``ensure_services`` predates the plan's directory.

    The manager the round talks to is not always the harness' own:
    the **project's** copy of ``service_manager`` is preferred, and an
    older copy of that module was written before the manager knew
    about a plan's directory.  Its ``ensure_services`` takes the
    project and the declarations and nothing else.
    """

    def __init__(self) -> None:
        self.ensure_calls: list = []

    def ensure_services(self, project_dir, declarations, round_number=0):
        self.ensure_calls.append({
            "project_dir": project_dir,
            "declarations": declarations,
            "round_number": round_number,
        })
        return sm.ServiceRuntimeMap()

    def record_runtimes(self, plan_dir, plan_id, runtime_map, **kwargs):
        return None


def test_a_manager_that_cannot_take_the_plan_directory_is_still_called(
    tmp_path: Path, monkeypatch,
):
    """An older manager keeps the call it has always been given.

    Handing such a manager an argument it does not accept raises
    ``TypeError`` — and the preflight swallows that ("preflight
    errored: … proceeding"), so the round goes on **without its
    services**.  That reads as a service that will not start, not as a
    version mismatch.

    So the plan's directory is offered to a manager that can take it,
    and only to such a manager.
    """
    agent = _agent(tmp_path)
    fake = _NarrowServiceManager()
    monkeypatch.setattr(
        verification_agent, "_load_service_manager",
        lambda project_dir=None: fake,
    )
    monkeypatch.setattr(
        agent, "_maybe_rebuild_before_service_start", lambda: None,
    )

    agent._preflight_declared_services({"services": []})

    assert fake.ensure_calls, (
        "the round start never ensured its services: the manager with the "
        "narrower signature was not called at all"
    )
    assert fake.ensure_calls[0]["project_dir"] == agent.project_dir, (
        fake.ensure_calls
    )
