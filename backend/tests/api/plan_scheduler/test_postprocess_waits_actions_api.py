"""The two wait actions: push a deadline out, give up on a wait.

What is pinned here
-------------------
The finishing stage can wait — for a human, or for something outside —
and every waiting row in the operator panel carries two actions:
**extend** (push the deadline out) and **terminate** (give up on it).
Both change a plan's state, so both are answers the operator reads
back, and what each of them answers is pinned here:

* **200** — the change was carried out.  The extend answer says how
  much time is left and how many times the deadline has been pushed
  out; the terminate answer says the wait is over;
* **404** — there is no such waiting item;
* **409** — the wait is not pending any more: it was given up on, or it
  was answered.  A wait that is over cannot be pushed out, and a wait
  that is over cannot be given up on twice;
* **422** — the request asks for a number of seconds that is not one:
  negative, zero, fractional, a string, or a number too large to be an
  extension;
* **400** — the item id is not a name.  It may not carry a separator
  and it may not be a traversal, because an item id names a row of the
  plan's own progress file and a row's name is not a path;
* **403** — the request never reached a handler.  The guard is
  middleware over every ``/api/`` path, so a request without the
  header, a request from a foreign origin and a request with a forged
  host are all refused before the endpoint is even reached.

The requests are real HTTP requests
-----------------------------------
The cases drive the **mounted app**, not the route functions: the
answer an operator gets is the answer the HTTP layer gives, status
code and body together.  A case that called the function directly
would prove the body and not the status, and the status is half of
what this contract is.

The waits are made by the production registry
---------------------------------------------
A wait is created by :class:`postprocess.waits.WaitRegistry`, which
writes it into the plan's own progress file.  The cases create their
waits the same way, so what the endpoint reads is a record the
production code wrote — not a hand-made file that happens to look
like one.

The rows the panel sees are the production rows
-----------------------------------------------
A row is not a copy of the record: it is what the read model
(:func:`plan_scheduler.snapshot.build`) makes of it.  The cases read
the rows through the same producer the endpoint uses, so "the row is
gone" is a statement about the panel's own view and not about a
second copy of the record kept by the test.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from postprocess.progress import load_progress  # noqa: E402
from postprocess.waits import WaitRegistry  # noqa: E402
from routes.scheduler import plan_waiting_rows  # noqa: E402


client = TestClient(server.app)

#: The wait the cases act on.
ITEM_ID = "approve"


# ---------------------------------------------------------------------------
# The fixtures the cases act through
# ---------------------------------------------------------------------------


def make_wait(plan_id: str, item_id: str = ITEM_ID) -> None:
    """Give one plan one waiting item, through the production registry.

    The registry is the only thing that may create a wait, and it
    writes the record into the plan's own progress file — which is
    where the endpoint looks for it.
    """
    WaitRegistry(plan_id).create_wait(
        {"id": item_id, "wait": {"type": "human"}},
        question="批准合入？",
    )


def wait_record(plan_id: str, item_id: str = ITEM_ID) -> Dict[str, Any]:
    """One wait's durable record, as it sits in the progress file."""
    return load_progress(plan_id)["items"][item_id]["wait"]


def rows(plan_id: str) -> List[Dict[str, Any]]:
    """The panel's waiting rows for one plan."""
    return plan_waiting_rows(plan_id)


def extend_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The extend endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/extend"


def terminate_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The terminate endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/terminate"


# ---------------------------------------------------------------------------
# 1 — extend
# ---------------------------------------------------------------------------


def test_extend_pending_returns_200_and_increases_remaining() -> None:
    """An extension is carried out, and the wait has more time left.

    Two things are checked, and they are checked apart: the **deadline**
    moved by exactly the number of seconds that were asked for, and the
    **answer** says the wait has more time left than it had.  The
    deadline is the exact one; the remaining time is a countdown read
    against the clock, so it is allowed to be a second or two out.
    """
    plan_id = "waits-extend-ok"
    make_wait(plan_id)
    before = wait_record(plan_id)["deadline"]

    response = client.post(extend_url(plan_id), json={"seconds": 3600})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_id"] == plan_id
    assert body["item_id"] == ITEM_ID
    assert body["extensions_count"] == 1, (
        "one extension leaves exactly one extension on the wait"
    )

    after = wait_record(plan_id)
    moved = datetime.fromisoformat(after["deadline"]) - datetime.fromisoformat(before)
    assert moved == timedelta(seconds=3600), (
        "the deadline moved by exactly the seconds that were asked for"
    )

    assert body["remaining_seconds"] >= 604800 + 3600 - 2, (
        "the answer says the wait has more time left than it had"
    )
    assert len(after["extensions"]) == 1, (
        "the extension is written down as one audit entry"
    )
    entry = after["extensions"][0]
    assert entry["from"] == before
    assert entry["to"] == after["deadline"]
    assert entry["operator"], "the audit entry names who extended the wait"


def test_extend_non_pending_returns_409() -> None:
    """A wait that is over cannot be pushed out.

    The wait is given up on first — through the endpoint, so the state
    the extension meets is the state the production code left behind.
    """
    plan_id = "waits-extend-conflict"
    make_wait(plan_id)

    terminated = client.post(terminate_url(plan_id))
    assert terminated.status_code == 200, terminated.text

    response = client.post(extend_url(plan_id), json={"seconds": 3600})

    assert response.status_code == 409, (
        f"extending a wait that is over is a conflict, got "
        f"{response.status_code}: {response.text}"
    )


def test_extend_invalid_seconds_returns_422() -> None:
    """A number of seconds that is not one is refused, not guessed.

    Six ways of asking for something that is not a number of seconds:
    a negative number, zero, a fraction, a string, a boolean, and a
    number so large it is not an extension.  And a body that says
    nothing at all.
    """
    plan_id = "waits-extend-invalid"
    make_wait(plan_id)
    before = wait_record(plan_id)["deadline"]

    for body in (
        {"seconds": -1},
        {"seconds": 0},
        {"seconds": 1.5},
        {"seconds": "3600"},
        {"seconds": True},
        {"seconds": 10 ** 12},
        {},
    ):
        response = client.post(extend_url(plan_id), json=body)
        assert response.status_code == 422, (
            f"{body!r} is not a number of seconds and was answered "
            f"{response.status_code}: {response.text}"
        )

    assert wait_record(plan_id)["deadline"] == before, (
        "a refused extension leaves the deadline where it was"
    )


def test_concurrent_extends_write_one_audit_row_each() -> None:
    """Four extensions at once leave four audit rows, and no lost update.

    The read-modify-write of a deadline is not atomic: the deadline is
    read, moved and written back.  Two requests doing that at the same
    time would both read the same deadline, and one of the two
    extensions would be lost — the audit would name an extension that
    the deadline does not carry.
    """
    plan_id = "waits-extend-concurrent"
    make_wait(plan_id)
    before = datetime.fromisoformat(wait_record(plan_id)["deadline"])

    answers: List[int] = []
    lock = threading.Lock()

    def extend() -> None:
        response = client.post(extend_url(plan_id), json={"seconds": 60})
        with lock:
            answers.append(response.status_code)

    threads = [threading.Thread(target=extend) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert answers == [200, 200, 200, 200], (
        f"every extension was carried out, got {answers}"
    )

    record = wait_record(plan_id)
    assert len(record["extensions"]) == 4, (
        f"four extensions leave four audit rows, got "
        f"{len(record['extensions'])}"
    )
    assert datetime.fromisoformat(record["deadline"]) - before == timedelta(
        seconds=240
    ), "no extension was lost on the way to the file"

    pairs = {(entry["from"], entry["to"]) for entry in record["extensions"]}
    assert len(pairs) == 4, (
        f"the four audit rows are four different extensions, got {pairs}"
    )


# ---------------------------------------------------------------------------
# 2 — terminate
# ---------------------------------------------------------------------------


def test_terminate_pending_removes_row() -> None:
    """Giving up on a wait takes its row off the panel.

    The wait is over, so it is not waiting any more: the row leaves the
    panel's waiting list, and the durable record says the wait was
    terminated rather than left pending.
    """
    plan_id = "waits-terminate-ok"
    make_wait(plan_id)
    assert len(rows(plan_id)) == 1, "the wait is on the panel before the answer"

    response = client.post(terminate_url(plan_id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_id"] == plan_id
    assert body["item_id"] == ITEM_ID
    assert body["status"] == "terminated"

    assert rows(plan_id) == [], (
        "a terminated wait is not waiting any more, so it has no row"
    )
    assert wait_record(plan_id)["status"] == "terminated", (
        "the durable record says the wait was terminated"
    )


def test_terminate_twice_returns_409() -> None:
    """A termination is final: the second one is a conflict.

    The operator changed their mind; the endpoint does not.  Giving up
    twice would leave two terminations in the audit for one wait, and
    the second one would claim the first never happened.
    """
    plan_id = "waits-terminate-twice"
    make_wait(plan_id)

    first = client.post(terminate_url(plan_id))
    assert first.status_code == 200, first.text

    second = client.post(terminate_url(plan_id))

    assert second.status_code == 409, (
        f"terminating a wait that is over is a conflict, got "
        f"{second.status_code}: {second.text}"
    )


# ---------------------------------------------------------------------------
# 3 — the refusals
# ---------------------------------------------------------------------------


def test_unknown_item_returns_404() -> None:
    """An item that is not waiting is not found.

    The plan waits for another item; this one names an item that has
    never waited.  "There is no such wait" is the answer — not a 409
    (nothing conflicts) and not a 200 (nothing was extended).
    """
    plan_id = "waits-unknown-item"
    make_wait(plan_id)

    response = client.post(extend_url(plan_id, "never-created"), json={"seconds": 60})

    assert response.status_code == 404, (
        f"there is no such waiting item, got {response.status_code}: "
        f"{response.text}"
    )


def test_missing_request_guard_returns_403() -> None:
    """A request without the guard header never reaches the endpoint.

    The guard is middleware over every ``/api/`` path, so the refusal
    happens before the endpoint is reached — and the endpoint's own
    answer to "which item?" never gets to matter.
    """
    plan_id = "waits-extend-unguarded"
    make_wait(plan_id)

    response = client.post(
        extend_url(plan_id),
        json={"seconds": 60},
        headers={"X-PDT-Request": ""},
    )

    assert response.status_code == 403, (
        f"a request without the guard header is refused, got "
        f"{response.status_code}: {response.text}"
    )
    assert wait_record(plan_id)["extensions"] == [], (
        "the refused request changed nothing"
    )


def test_foreign_origin_returns_403() -> None:
    """A page from another origin cannot drive the endpoint."""
    plan_id = "waits-extend-foreign-origin"
    make_wait(plan_id)

    response = client.post(
        extend_url(plan_id),
        json={"seconds": 60},
        headers={"Origin": "http://evil.example.com"},
    )

    assert response.status_code == 403, (
        f"a request from a foreign origin is refused, got "
        f"{response.status_code}: {response.text}"
    )


def test_forged_host_returns_403() -> None:
    """A request that claims to be another machine is refused."""
    plan_id = "waits-extend-forged-host"
    make_wait(plan_id)

    response = client.post(
        extend_url(plan_id),
        json={"seconds": 60},
        headers={"Host": "evil.example.com"},
    )

    assert response.status_code == 403, (
        f"a request with a forged host is refused, got "
        f"{response.status_code}: {response.text}"
    )


def test_item_id_with_a_separator_returns_400() -> None:
    """An item id that carries a separator is refused.

    An item id names a row of the plan's own progress file.  One that
    carries a path separator is not a name, and it is refused rather
    than answered with whatever it points at.
    """
    plan_id = "waits-extend-separator"
    make_wait(plan_id)

    response = client.post(
        extend_url(plan_id, "a%2Fb"), json={"seconds": 60}
    )

    assert response.status_code == 400, (
        f"an item id with a separator is a bad request, got "
        f"{response.status_code}: {response.text}"
    )


def test_item_id_with_a_traversal_returns_400() -> None:
    """An item id that is a traversal is refused."""
    plan_id = "waits-extend-traversal"
    make_wait(plan_id)

    response = client.post(
        extend_url(plan_id, "%2e%2e"), json={"seconds": 60}
    )

    assert response.status_code == 400, (
        f"an item id that is a traversal is a bad request, got "
        f"{response.status_code}: {response.text}"
    )