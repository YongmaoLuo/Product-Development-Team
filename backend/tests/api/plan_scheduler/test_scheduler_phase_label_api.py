"""``GET /api/scheduler/state`` — every row carries the label of its phase.

What is pinned here
-------------------
The scheduler's read model passes the **phase** of each queued and each
running plan on as it is — ``verification_command_audit`` and the rest —
because the phase is the routing layer's own word and the read path has
no business rewriting it.

The *label* a human reads is a different thing.  It lives in one table
(``plan_state.PHASE_LABELS``) and is worked out by one function
(``routes.plans._phase_label``), so that the same phase cannot be
labelled two ways in two endpoints.  The panel reads the label the
backend computed; it does not translate anything itself.

1. **Every row carries a label.**  Both lists — ``queue`` and
   ``running`` — get a ``phase_label`` next to the raw ``phase``, and the
   value is the label of that very phase.
2. **The label comes from ``routes.plans``.**  The helper is replaced
   with one that says something else, and the endpoint's answer changes
   with it.  A label computed here — a second copy of the table, a
   literal Chinese string in this module — would not move when the table
   moves, and the two copies would drift.
3. **A row without a phase gets an empty label.**  A state written
   before the phase existed is a normal thing to read: the row says
   ``None`` for the phase, and the label is the empty string rather than
   a missing key or an exception.

The phase is left exactly as it was read — the label is a **supplement**
to the phase name, never a replacement for it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: it is the application, and it is what puts the
# routers on the app.  ``routes.plans`` is imported **after** it — the
# two modules import each other, and the application's own import order
# is the one that resolves the cycle.
import server  # noqa: E402
from routes import plans as routes_plans  # noqa: E402
from plan_scheduler.snapshot import build  # noqa: E402


client = TestClient(server.app)

#: The endpoint under test.
STATE_PATH = "/api/scheduler/state"


class _FakeEngine:
    """The engine's read side, with a state to read.

    The production engine answers ``snapshot()`` with the read model
    assembled by :func:`plan_scheduler.snapshot.build` — the same
    function, the same rows.  This one does the same thing with a state
    the test chooses, so the endpoint is exercised against the very
    shape the scheduler itself produces.
    """

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self._state = state if state is not None else {}

    def snapshot(self) -> Dict[str, Any]:
        return build(self._state)


@pytest.fixture
def install_engine(monkeypatch):
    """Put an engine on the app — and take it away afterwards.

    The handle is ``app.state.scheduler_engine``: the same one the
    lifespan hangs the engine on.
    """

    def _install(state: Optional[Dict[str, Any]] = None) -> _FakeEngine:
        engine = _FakeEngine(state)
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        return engine

    return _install


def _state_with_phases() -> Dict[str, Any]:
    """A state with one queued plan and one running plan, both phased.

    The queued plan carries a phase that has no stable label of its own
    (the label is then the phase name itself, by
    ``routes.plans._phase_label``'s own rule); the running plan carries
    the waiting phase, which does have one.
    """
    return {
        "capacity": 4,
        "used": 1,
        "now": 1000.0,
        "min_runtime_seconds": 600,
        "queue": [
            {
                "plan_id": "p-queued",
                "seq": 1,
                "priority": 0,
                "promote_order": None,
                "reason": "waiting_for_slot",
                "phase": "verification_command_audit",
            }
        ],
        "running": [
            {
                "plan_id": "p-running",
                "acquired_at": 400.0,
                "workspace": "/ws",
                "phase": "queued",
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1 — every row carries a label
# ---------------------------------------------------------------------------


def test_state_carries_phase_label(install_engine):
    """Both rows carry the label of the phase they report.

    The label is the one the shared helper produces for that very
    phase — the panel is shown what the rest of the system calls it,
    not a word invented here.
    """
    install_engine(_state_with_phases())

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    queue_row = body["queue"][0]
    running_row = body["running"][0]

    assert queue_row["phase"] == "verification_command_audit", queue_row
    assert queue_row["phase_label"] == routes_plans._phase_label(
        "verification_command_audit"
    ), queue_row

    assert running_row["phase"] == "queued", running_row
    assert running_row["phase_label"] == routes_plans._phase_label(
        "queued"
    ), running_row
    # The stable label of the waiting phase — the Chinese word the
    # backend publishes for it, not the raw phase name.
    assert running_row["phase_label"] == "排队中", running_row


# ---------------------------------------------------------------------------
# 2 — the label comes from ``routes.plans``
# ---------------------------------------------------------------------------


def test_phase_label_comes_from_plans_module(monkeypatch, install_engine):
    """The helper in ``routes.plans`` is the one that answers.

    The helper is replaced with one that says something else, and the
    endpoint's answer changes with it.  A label computed in this module —
    a second copy of the table, a literal Chinese string — would not
    move when the table moves, and the two copies would drift apart
    without anything failing.
    """

    def _fake_label(phase: Optional[str]) -> str:
        return f"label-of-{phase}"

    monkeypatch.setattr(routes_plans, "_phase_label", _fake_label)
    install_engine(_state_with_phases())

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["queue"][0]["phase_label"] == "label-of-verification_command_audit", (
        body["queue"][0]
    )
    assert body["running"][0]["phase_label"] == "label-of-queued", (
        body["running"][0]
    )


# ---------------------------------------------------------------------------
# 3 — a row without a phase
# ---------------------------------------------------------------------------


def test_missing_phase_gives_empty_label(install_engine):
    """A row that carries no phase gets an empty label.

    A state written before the phase existed is a normal thing to read:
    the phase is ``None`` and the label is the empty string.  A missing
    key would leave the panel guessing, and a raised exception would
    take the whole read model down for one old row.
    """
    install_engine(
        {
            "capacity": 4,
            "used": 1,
            "now": 1000.0,
            "queue": [{"plan_id": "p-queued", "seq": 1, "reason": "waiting_for_slot"}],
            "running": [{"plan_id": "p-running", "acquired_at": 400.0}],
        }
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["queue"][0]["phase"] is None, body["queue"][0]
    assert body["queue"][0]["phase_label"] == "", body["queue"][0]
    assert body["running"][0]["phase"] is None, body["running"][0]
    assert body["running"][0]["phase_label"] == "", body["running"][0]
