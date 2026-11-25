"""``POST /api/plan/{plan_id}/scheduler/pause`` — the pause that times out.

What is pinned here
-------------------
The pause that **hangs** is the one that waits for the scheduler to
answer: the engine's wait is bounded (thirty seconds by default), and
when the bound runs out the command is still in the mailbox.  The
operator who clicked the button is left staring at a page that says
"loading failed", because the answer that comes back is a 202 the panel
cannot use — it says "pending" and nothing else.

So the route says what is actually true:

* a pause that timed out **with its command recorded** is answered
  ``stopping`` — the command is on the record, the worker still carries
  it out, and the read model's stopping list will name the plan.  The
  panel can say 「停止中」 at once, and the page does not wait;
* a pause that timed out **without a command row** is answered
  ``pending`` and its detail carries ``command_not_recorded``: nothing
  was written down, so there is no stopping row to wait for, and
  claiming ``stopping`` would be a lie the panel would show;
* the other actions are not touched: their ``pending`` answer stays
  ``pending`` — the panel's word for a plan that comes back to the
  queue is *requeue*, and that is the ``resume`` endpoint here.

The endpoint is driven through the **mounted app**, for the reason the
sibling files give: the failure this guards against is a route that was
never registered, or one registered after the frontend catch-all, where
the request is answered with a 404 instead of the scheduler's answer.

The engine behind it is a **stand-in that answers with the result it is
given**: what is under test here is the route's translation of the
engine's answer, and the engine's own waiting is pinned by its own
files.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from plan_scheduler.commands import APPLIED, CommandResult  # noqa: E402
from plan_scheduler.engine import PENDING_ANSWER, EngineCommandResult  # noqa: E402

client = TestClient(server.app)

#: The endpoint under test.
PAUSE_URL = "/api/plan/{0}/scheduler/pause"

#: The other endpoint the same rule must not touch: a plan that comes
#: back to the queue is the panel's *requeue*, and it is ``resume`` here.
RESUME_URL = "/api/plan/{0}/scheduler/resume"


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class _StubEngine:
    """The engine the route sees: it answers with the result it was given.

    The route reads three things off the engine — whether it is on, what
    the command did, and which command it was — and this stand-in
    answers all three.  The calls are kept, so a case can say what the
    route asked for.
    """

    def __init__(self, result: Any) -> None:
        self.enabled = True
        self.result = result
        self.calls: List[Tuple[str, str]] = []

    def submit_command(self, action: str, plan_id: str) -> Any:
        """Answer with the result the case gave."""
        self.calls.append((action, plan_id))
        return self.result


@pytest.fixture()
def install_engine(monkeypatch) -> Callable[[Any], _StubEngine]:
    """Put the stand-in engine on the app — and take it away afterwards.

    The handle is ``app.state.scheduler_engine``: the same one the
    lifespan hangs the engine on, and the one the route reads.
    """

    def _install(result: Any) -> _StubEngine:
        engine = _StubEngine(result)
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        return engine

    return _install


def _pending(command_id: Optional[int]) -> EngineCommandResult:
    """The engine's answer when its bounded wait ran out.

    ``command_id`` is the id of the command's row — the row that makes
    the command visible to the read model.  It is ``None`` when the
    command was never written down.
    """
    return EngineCommandResult(
        outcome=PENDING_ANSWER,
        command_id=command_id,
        detail="the command is still in the mailbox",
        plan_id="p1",
        action="pause",
    )


# ---------------------------------------------------------------------------
# 1 — a pause that timed out with its command recorded
# ---------------------------------------------------------------------------


def test_pause_timeout_returns_stopping(install_engine) -> None:
    """A pause that timed out is answered ``stopping``.

    The command **is** on the record: the worker wrote it down before it
    carried the action out, so the id the answer carries names a row
    that exists.  The read model's stopping list will therefore name the
    plan, and the answer has to say so — a ``pending`` answer tells the
    operator nothing they can act on, and the page keeps waiting.

    The answer is still a **202**: the command has not been carried out
    yet, and a 200 would claim that it has.
    """
    engine = install_engine(_pending(command_id=11))

    response = client.post(PAUSE_URL.format("p1"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "stopping", body
    assert body["plan_id"] == "p1", body
    # The command is not withdrawn: the id the answer carries still
    # names the row the worker will finish.
    assert body["command_id"] == 11, body
    assert engine.calls == [("pause", "p1")], engine.calls


# ---------------------------------------------------------------------------
# 2 — the other actions keep their own answer
# ---------------------------------------------------------------------------


def test_non_pause_timeout_still_pending(install_engine) -> None:
    """A timed-out action that is not a pause stays ``pending``.

    The rule is about the **pause**: it is the action that stops a
    working plan, so it is the one whose "on its way" means "stopping".
    The other actions are left exactly as they were — a plan that comes
    back to the queue (the panel's *requeue*) is answered ``pending``,
    and the answer does not carry a plan id it never had.
    """
    install_engine(_pending(command_id=12))

    response = client.post(RESUME_URL.format("p1"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending", body
    assert "plan_id" not in body, body


# ---------------------------------------------------------------------------
# 3 — a pause the engine answered at once
# ---------------------------------------------------------------------------


def test_pause_applied_returns_200(install_engine) -> None:
    """A pause the engine answered is a 200 that says ``paused``.

    The fast answer is the ordinary one and it is not changed: the plan
    is parked, the answer is a 200, and the body says what happened.
    """
    install_engine(
        CommandResult(
            outcome=APPLIED,
            command_id=13,
            detail="paused",
            plan_id="p1",
            action="pause",
        )
    )

    response = client.post(PAUSE_URL.format("p1"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "applied", body
    assert body["status"] == "paused", body


# ---------------------------------------------------------------------------
# 4 — a pause whose command was never written down
# ---------------------------------------------------------------------------


def test_pause_timeout_without_command_row_stays_pending(install_engine) -> None:
    """A pause with no command row stays ``pending`` and says why.

    The row is what makes the command visible: without it there is no
    stopping row in the read model, and an answer that said
    ``stopping`` would send the operator to wait for a row that is not
    there.  So the answer says ``pending`` — and its detail says
    ``command_not_recorded``, so the difference between the two kinds of
    timeout is not something the caller has to guess.
    """
    install_engine(_pending(command_id=None))

    response = client.post(PAUSE_URL.format("p1"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending", body
    assert "command_not_recorded" in body["detail"], body
    assert "plan_id" not in body, body
