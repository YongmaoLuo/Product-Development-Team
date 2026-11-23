"""``GET /api/scheduler/finishing`` — the plans that are being finished.

What is pinned here
-------------------
A plan that has a requirement book enters the finishing stage
(``post_processing``) and leaves it as ``completed`` — or stays in
``postprocess_failed`` when a required item did not pass.  Neither
phase holds a slot and neither is a run, so those plans are in no
queue and in no running line: the read model of ``/api/scheduler/state``
cannot name them, and an operator cannot see that a plan has reached
the finishing stage at all.

This endpoint is where they are named, and it is pinned here for four
things:

1. **A finishing plan is listed, and its phase is labelled.**  The
   label is what an operator reads (收尾中 / 收尾失败), and it is said
   by the server — the panel does not make up a word for a phase.
2. **The terminal report comes with it.**  A plan whose finishing stage
   failed carries the report's own ``terminal_status``, so the panel
   cannot tell a plan that is still working on one that has failed.
3. **The waits come with it.**  The pending waits of a finishing plan
   are the very rows the waiting table draws, so the finishing block
   and the waiting block cannot tell two different stories about what
   a plan is waiting for.
4. **No finishing plan is an empty list, not an error.**  The panel
   polls this endpoint every five seconds; a 500 would leave the
   operator with a broken panel and no way to tell an idle stage from
   a broken one.

The requests are real HTTP requests
-----------------------------------
The cases drive the **mounted app**, not the route functions: the
answer an operator gets is the answer the HTTP layer gives, status
code and body together.

The facts come from the production writers
------------------------------------------
The phase is written through the routing repository — the same table
the plans listing reads — and the wait is created by the production
registry, and the report is written by the production report writer.
So what the endpoint reads is a record the production code wrote, not
a hand-made row that happens to look like one.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Iterator, List

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from postprocess.report import write_report  # noqa: E402
from postprocess.waits import WaitRegistry  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)


client = TestClient(server.app)

#: The endpoint under test.
URL = "/api/scheduler/finishing"

#: The question the wait carries — the panel must show it.
QUESTION = "要求书第 3 条可以合入吗？"

#: The wait's item id.
ITEM_ID = "item-3"


# ---------------------------------------------------------------------------
# The fixtures the cases act through
# ---------------------------------------------------------------------------


@pytest.fixture
def routing_row() -> Iterator[Any]:
    """Write a routing row, and take it away again afterwards.

    The state database is shared by the whole session, so a plan left
    behind in it would show up in another test's listing.
    """
    created: List[str] = []
    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        repository = RoutingRepository(conn)

        def _write(plan_id: str, phase: str) -> None:
            repository.insert_if_missing(plan_id, phase)
            created.append(plan_id)

        yield _write
    finally:
        for plan_id in created:
            conn.execute(
                "DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,)
            )
        conn.commit()
        conn.close()


@pytest.fixture
def finishing_plan() -> Iterator[str]:
    """A plan in the finishing stage, with a wait and a report."""
    plan_id = "finishing-stage-plan"

    registry = WaitRegistry(plan_id)
    registry.create_wait(
        {"id": ITEM_ID, "wait": {"type": "human"}},
        question=QUESTION,
    )
    write_report(
        plan_id,
        frozen_spec={"spec_sha256": "0" * 64},
        item_verdicts=[
            {
                "id": "lint",
                "verdict": "FAIL",
                "evidence_excerpt": "lint failed",
                "audit": {
                    "action": "lint",
                    "command": ["make", "lint"],
                    "target": ".",
                },
            }
        ],
        terminal_status="postprocess_failed",
    )

    yield plan_id

    plan_dir = Path(server.PLANS_DIR) / plan_id
    if plan_dir.is_dir():
        shutil.rmtree(plan_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1 — a finishing plan is listed
# ---------------------------------------------------------------------------


def test_finishing_plan_is_listed_with_its_label(
    routing_row: Any, finishing_plan: str
) -> None:
    """A plan in the finishing stage is named, with the phase's label.

    The label is the server's word for the phase — 收尾中 for a plan
    that is still being finished — and it is what an operator reads.
    """
    routing_row(finishing_plan, "post_processing")

    response = client.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert "plans" in body, f"the answer has no 'plans' key: {body}"

    plans = [p for p in body["plans"] if p["plan_id"] == finishing_plan]
    assert len(plans) == 1, (
        f"the finishing plan is not in the answer: {body['plans']}"
    )
    plan = plans[0]
    assert plan["phase"] == "post_processing"
    assert plan["phase_label"] == "收尾中"
    assert plan["updated_at"], "the answer says when the plan was last touched"


def test_failed_plan_is_labelled_as_failed(routing_row: Any) -> None:
    """A plan whose finishing stage failed is labelled as such.

    A failed finishing stage is the one an operator has to act on, and
    it must not be drawn the same way as a plan that is still working.
    """
    plan_id = "finishing-stage-failed"
    routing_row(plan_id, "postprocess_failed")

    response = client.get(URL)

    assert response.status_code == 200, response.text
    plans = [p for p in response.json()["plans"] if p["plan_id"] == plan_id]
    assert len(plans) == 1, (
        f"the failed finishing plan is not in the answer: "
        f"{response.json()['plans']}"
    )
    assert plans[0]["phase_label"] == "收尾失败"


# ---------------------------------------------------------------------------
# 2 — the terminal report comes with the plan
# ---------------------------------------------------------------------------


def test_failed_plan_carries_the_terminal_status(
    routing_row: Any, finishing_plan: str
) -> None:
    """A failed plan carries the report's own ``terminal_status``.

    The summary is read out of ``postprocess_report.json`` — the same
    file the report itself was written to — so the panel and the report
    cannot tell two different stories about how the stage ended.
    """
    routing_row(finishing_plan, "postprocess_failed")

    response = client.get(URL)

    assert response.status_code == 200, response.text
    plans = [p for p in response.json()["plans"] if p["plan_id"] == finishing_plan]
    assert len(plans) == 1
    summary = plans[0]["postprocess"]
    assert summary, "the failed plan carries no terminal summary"
    assert summary["terminal_status"] == "postprocess_failed"
    verdicts = summary["item_verdicts"]
    assert [item["id"] for item in verdicts] == ["lint"]
    assert verdicts[0]["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# 3 — the waits come with the plan
# ---------------------------------------------------------------------------


def test_finishing_plan_carries_its_pending_waits(
    routing_row: Any, finishing_plan: str
) -> None:
    """The pending waits of a finishing plan are listed with it.

    The wait is the question the finishing stage is blocked on, and
    the question's own text is in the row — a question the operator
    cannot read is a question nobody can answer.
    """
    routing_row(finishing_plan, "post_processing")

    response = client.get(URL)

    assert response.status_code == 200, response.text
    plans = [p for p in response.json()["plans"] if p["plan_id"] == finishing_plan]
    assert len(plans) == 1
    waits = plans[0]["waits"]
    assert waits, "the finishing plan carries no wait"
    assert waits[0]["question"] == QUESTION
    assert waits[0]["item_id"] == ITEM_ID
    assert waits[0]["plan_id"] == finishing_plan


# ---------------------------------------------------------------------------
# 4 — no finishing plan is an empty list
# ---------------------------------------------------------------------------


def test_no_finishing_plan_answers_an_empty_list() -> None:
    """The answer names the finishing plans and nothing else.

    The answer for "none of them" is an empty list — not a 404, and
    not a 500.  The panel polls this endpoint, and an endpoint that
    fails when there is nothing to report leaves the operator with a
    broken panel.
    """
    response = client.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert isinstance(body.get("plans"), list)
    assert all(
        p["phase"] in ("post_processing", "postprocess_failed")
        for p in body["plans"]
    ), "the answer names a phase that is not a finishing phase"
