"""``GET /api/scheduler/state`` — the parked plans, labelled like the rest.

What is pinned here
-------------------
A plan parked for quota holds no slot and is in no line: it is not in
the queue (it is not waiting for a slot) and it is not running (it
holds no capacity).  It is the one plan an operator has to be able to
see — it starts again by itself once the window opens — so the read
model carries it in a list of its own, ``parked``.

1. **A parked row carries the label of its phase.**  The label is a
   **supplement** to the phase name, never a replacement for it, and
   it is worked out by the one shared helper
   (``routes.plans._phase_label``) — the same one the queued and the
   running rows are labelled with.  A parked row that was left out of
   the labelling would show the panel a phase with no word beside it,
   which is a shape the panel cannot render.
2. **An idle scheduler parks nothing.**  The key is there and its
   value is an empty list — never ``None`` and never a missing key —
   so a caller can walk the list without asking whether it is there.
3. **The other two lists are untouched.**  A parked row gaining a
   label must not change the shape of the rows that already had one:
   the queue and the running rows keep exactly the keys they had.

The parked list itself is the read model's business — the snapshot
assembles it (``plan_scheduler.snapshot.build``), and this module only
pins what the **route** does with the rows it is handed.
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

#: The keys a queued row carries — the shape the panel renders.
_QUEUE_ROW_KEYS = {
    "position",
    "plan_id",
    "seq",
    "priority",
    "promote_order",
    "due",
    "reason",
    "phase",
    "phase_label",
}

#: The keys a running row carries.
#:
#: 2026-11-15: ``priority`` joined the set.  The running view carries the
#: weight the caller gave the plan — the number the eviction rule
#: compares the running plans by — and it is a **baseline** field of the
#: read model, not something the quota-park work introduced: the key set
#: in this file was the pre-merge shape and had drifted behind the
#: production ``_running_view``.  The production side is the baseline and
#: is not touched; only the expectation is brought back in line.
_RUNNING_ROW_KEYS = {
    "plan_id",
    "acquired_at",
    "elapsed_sec",
    "running_sec",
    "started_at",
    "evictable",
    "workspace",
    "priority",
    "phase",
    "phase_label",
}


class _FakeEngine:
    """The engine's read side, with a state to read.

    The production engine answers ``snapshot()`` with the read model
    assembled by :func:`plan_scheduler.snapshot.build` — the same
    function, the same rows.  This one does the same thing with a
    state the test chooses, so the endpoint is exercised against the
    very shape the scheduler itself produces.
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


def _state_with_parked() -> Dict[str, Any]:
    """A state with one plan parked for quota.

    The parked plan carries the phase it stopped in — the phase is the
    routing layer's own word and it travels as it is, so the label is
    worked out further up, by the one shared helper.
    """
    return {
        "capacity": 4,
        "used": 0,
        "now": 1000.0,
        "min_runtime_seconds": 600,
        "queue": [],
        "running": [],
        "parked": [
            {
                "plan_id": "p9",
                "kind": "execution",
                "round": 2,
                "reason": "waiting_for_quota",
                "phase": "waiting_for_quota",
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1 — a parked row carries the label of its phase
# ---------------------------------------------------------------------------


def test_parked_rows_are_labeled(install_engine):
    """A parked row carries the label of the phase it reports.

    The label is the one the shared helper produces for that very
    phase — the panel is shown what the rest of the system calls it,
    not a word invented here.  A parked row left out of the labelling
    would show the panel a phase with no word beside it.
    """
    install_engine(_state_with_parked())

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["parked"]) == 1, body["parked"]
    parked_row = body["parked"][0]

    assert parked_row["plan_id"] == "p9", parked_row
    assert parked_row["phase"] == "waiting_for_quota", parked_row
    assert parked_row["phase_label"] == routes_plans._phase_label(
        "waiting_for_quota"
    ), parked_row
    # The label is not an empty string: the phase has a name, and the
    # panel shows that name beside the phase.
    assert parked_row["phase_label"], parked_row


def test_parked_label_comes_from_plans_module(monkeypatch, install_engine):
    """The helper in ``routes.plans`` is the one that answers.

    The helper is replaced with one that says something else, and the
    parked row's label changes with it.  A label computed for the
    parked rows alone — a second copy of the table, a literal string
    in this module — would not move when the table moves, and the two
    copies would drift apart without anything failing.
    """

    def _fake_label(phase: Optional[str]) -> str:
        return f"label-of-{phase}"

    monkeypatch.setattr(routes_plans, "_phase_label", _fake_label)
    install_engine(_state_with_parked())

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["parked"][0]["phase_label"] == "label-of-waiting_for_quota", (
        body["parked"][0]
    )


# ---------------------------------------------------------------------------
# 2 — an idle scheduler parks nothing
# ---------------------------------------------------------------------------


def test_empty_snapshot_has_parked_key(monkeypatch):
    """No engine, no facts: ``parked`` is there and it is empty.

    The key is part of the read model, so a caller can walk it without
    asking whether it is there.  An empty list is the answer — not
    ``None``, and not a missing key.
    """
    monkeypatch.delattr(
        server.app.state, "scheduler_engine", raising=False
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert "parked" in body, sorted(body)
    assert body["parked"] == [], body["parked"]


def test_state_without_parked_plans_has_empty_parked_list(install_engine):
    """A busy scheduler with nothing parked still answers with a list.

    Nothing parked is a normal thing to look at: the engine is running,
    plans are queued and running, and no plan is waiting for quota.  The
    list is still there, and it is still empty.
    """
    install_engine(
        {
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
                    "phase": "queued",
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
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["parked"] == [], body["parked"]


# ---------------------------------------------------------------------------
# 3 — the other two lists keep their shape
# ---------------------------------------------------------------------------


def test_queue_and_running_rows_keep_keys(install_engine):
    """The queued and the running rows keep exactly the keys they had.

    A parked row gaining a label must not change the shape of the rows
    that already carried one: the panel renders all three lists, and a
    row that grew or lost a key would break the rendering of the other
    two.
    """
    install_engine(
        {
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
                    "phase": "queued",
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
            "parked": [
                {
                    "plan_id": "p9",
                    "kind": "execution",
                    "round": 2,
                    "reason": "waiting_for_quota",
                    "phase": "waiting_for_quota",
                }
            ],
        }
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body["queue"][0]) == _QUEUE_ROW_KEYS, sorted(body["queue"][0])
    assert set(body["running"][0]) == _RUNNING_ROW_KEYS, sorted(
        body["running"][0]
    )
