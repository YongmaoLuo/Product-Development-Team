"""HTTP contract: ``/api/plan/{plan_id}/status`` on a missing plan, on an
unsearchable plans tree, and on a malformed id.

The criterion this file pins
----------------------------
The test-design criterion is:

    *input validation and exception paths must have an explicit contract.*

Concretely, for ``GET /api/plan/{plan_id}/status``:

* a **legal but unknown** plan id answers ``404`` with the detail
  ``Plan not found`` — the caller named a plan that does not exist, and
  the answer says so;
* a **malformed** plan id answers ``400``, with the *same* body the
  execution routes answer it with — one uniform contract for every
  route that accepts a plan id, rather than "21 of 23 routes reject
  this and two of them do not";
* a plans tree this instance **cannot search** answers ``503`` — and
  not ``404``, because "this plan does not exist" and "this instance is
  looking in the wrong tree" are different facts, and only the second
  one is true when the tree is empty.

Why the third case matters
---------------------------
``404 Plan not found`` used to mean two different things:

* the plan really is not there;
* the instance is pointed at a tree that is not there, or at one that
  is **empty** — and an empty tree makes *every* plan id answer
  ``"Plan not found"``.

A caller that reads the second one as the first one never learns that
the instance is looking in the wrong place; it concludes the plan was
deleted.  So the tree is checked before the plan is, and a tree that
cannot be searched is answered ``503`` with the directory that was
searched in the body — ``{"detail": "plans dir unavailable",
"searched_dir": "<absolute path>"}``.

Both answers echo the directory they searched, so the two are told
apart by the paths they carry rather than by their status codes
alone: a ``404`` means "the tree was searched, and the plan is not in
it"; a ``503`` means "the tree could not be searched at all".

Why hermetic
------------
The earlier probes for this criterion were made against a live server
and produced no evidence at all::

    GET /api/plan/20261024-vp-quota-missing/status  ->  connection refused

That is a fact about the *service's* lifetime, not about the route: the
backend had been recycled by the time the request was made, so the
answer says nothing whatsoever about the plan or about the route.  A
one-shot probe against a shared port is not evidence about a route's
contract — it is evidence about the port.

This file therefore never opens a socket.  It drives the ASGI app
in-process through ``TestClient``, with the plans tree under the test's
own ``tmp_path`` (the autouse ``isolated_plans_dir`` fixture points
``server.PLANS_DIR`` and ``PDT_PLANS_DIR`` at it), so the operator's
live ``plans/`` tree and ``state.db`` are never touched.

One test at the end is the exception, and it earns it.  The round's own
probe was made against a **service** — a process that outlives the
backend that started it — and the contract that section pins is about
what that process answers while it is still up.  That test starts its
own service, on its own port, and waits for the port to answer before
it probes: the port belongs to this test, so a refusal is evidence
about the route rather than about somebody else's process lifetime.

What "the route rejects the id itself" means
--------------------------------------------
``_server._plan_dir`` happens to validate the id on its way to
``PLANS_DIR / plan_id``, so the route's refusal is *observable* today
even though the route does not ask for it.  That is exactly the kind of
accident a contract must not rest on: the day ``_plan_dir`` is relaxed
— and it is a plausible relaxation, because the codebase already carries
``_is_renderable_plan_id`` for stored ids that predate the traversal
guard — the status route would start answering ``200`` with
``plan_id: ".."`` and the repository root's contents as if they were a
plan.  So this file pins both halves of the contract:

* the refusal is **observable over HTTP** (400, and the same body the
  execution route answers with), and
* the route **states the guard itself** — its own body admits the id
  through ``_server._validated_plan_id``, which is the shared validator
  every other plan-id route uses.

The second half is a source-level assertion, and deliberately so: a
behavioural probe cannot tell "the route refused this" from "the route
refused this by accident, because a helper it calls happened to
validate".  The repo already uses this shape for the same reason — see
``backend/tests/security/test_plan_id_containment.py``, whose
``test_every_write_route_checks_the_id`` reads a handler's body rather
than calling it, so that a test whose failure mode is "starts an
execution" is never written.
"""

from __future__ import annotations

import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Tuple

import pytest
from fastapi.testclient import TestClient

import server


#: The plan id the failed probes used.  It is a *legal* id — it simply
#: names no plan — so it must be answered 404, not 400.  Reusing it
#: keeps the criterion and its evidence about the same request the
#: earlier probes made.
MISSING_PLAN_ID = "20261024-vp-quota-missing"

#: The detail a route answers with when the plans tree cannot be
#: searched.  Spelled out here as well as in the route, so a change to
#: either one shows up in this file rather than in a log.
PLANS_DIR_UNAVAILABLE_DETAIL = "plans dir unavailable"

#: ``(raw id, url segment)`` for every malformed id under test.
#:
#: The URL segment is written out rather than computed with
#: ``urllib.parse.quote``: quote leaves ``.`` alone, and a literal
#: ``..`` segment is normalised away by the HTTP client before the
#: request reaches the router — so a computed encoding would silently
#: test the routing table (404 "Not found") instead of the handler.
_MALFORMED_IDS: Tuple[Tuple[str, str], ...] = (
    ("..", "%2e%2e"),            # traversal component
    (".", "%2e"),                # traversal component
    ("plan id", "plan%20id"),    # whitespace — outside [A-Za-z0-9._-]
    ("plan\x00id", "plan%00id"),  # control byte
    ("a\\b", "a%5Cb"),           # windows path separator
    ("plan?", "plan%3F"),        # unsafe character
    ("a" * 100, "a" * 100),      # longer than MAX_PLAN_ID_LEN
)


@pytest.fixture
def plans_root(tmp_path: Path) -> Path:
    """The plans tree this file reads — never the operator's.

    The autouse ``isolated_plans_dir`` fixture already points
    ``server.PLANS_DIR`` at ``tmp_path / "plans"``.  This fixture makes
    that dependency explicit, creates the directory, and asserts the
    two agree — so the test fails loudly rather than reading the
    operator's real plans tree if the isolation ever stops working.

    The tree it creates is **empty**, which is the state the ``503``
    contract is about: an empty tree cannot answer "this plan is not
    here" — it can only answer "there is nothing here at all".
    """
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    assert server.PLANS_DIR == root, (
        f"server.PLANS_DIR is {server.PLANS_DIR!r}, not this test's "
        f"{root!r} — the isolation fixture stopped working and this test "
        f"would read the operator's real plans tree"
    )
    return root


@pytest.fixture
def populated_plans_root(plans_root: Path) -> Path:
    """The plans tree with a plan in it — the state a search can answer.

    A ``404`` is only an honest answer when the tree was searched and
    the plan was not in it.  An empty tree cannot say that, so the
    ``404`` cases below run against a tree that holds one *other* plan.
    """
    (plans_root / "20261024-some-other-plan").mkdir(parents=True, exist_ok=True)
    return plans_root


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot and starts the supervisor fleet.  Skipping it is deliberate:
    this contract is about what the route does with the id it is given,
    and a routing test has no business starting a fleet.
    """
    return TestClient(server.app)


def _plan_status_endpoint() -> Any:
    """The function behind ``GET /api/plan/{plan_id}/status``.

    Enumerated through ``server.iter_app_routes()`` rather than
    ``server.app.routes``: routers extracted into ``backend/routes/``
    land as a single ``_IncludedRouter`` entry (FastAPI >= 0.141), so
    walking ``app.routes`` alone would quietly stop seeing every route
    that moved.
    """
    for route in server.iter_app_routes():
        if getattr(route, "path", "") != "/api/plan/{plan_id}/status":
            continue
        endpoint = getattr(route, "endpoint", None)
        assert endpoint is not None, (
            "GET /api/plan/{plan_id}/status is registered without an "
            "endpoint — there is nothing to pin a contract to"
        )
        return endpoint
    raise AssertionError(
        "GET /api/plan/{plan_id}/status is not registered — the contract "
        "this file pins has no route to pin it to"
    )


# ---------------------------------------------------------------------------
# A plan that does not exist
# ---------------------------------------------------------------------------


def test_missing_plan_returns_404_contract(populated_plans_root: Path) -> None:
    """A legal plan id that names no plan answers 404 "Plan not found".

    The distinction the contract draws is between *bad request* and
    *missing plan*.  A legal id is not a bad request, so it must not be
    answered 400; and the answer must name the reason, so the caller
    can tell "you asked about a plan that does not exist" from "your URL
    is malformed" and from "the plan exists but its status could not be
    read".

    The tree holds another plan, so the ``404`` is an answer about
    *this* plan and not about an empty data plane (which is answered
    ``503`` — see the tests below).

    The negative control at the end is what makes the 404 mean
    something: the same request against a plan that *does* exist must
    not be answered 404.  Without it, a route that refused everything
    would pass the first half of this test.
    """
    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 404, (
        f"a legal plan id that names no plan must be answered 404; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == "Plan not found", (
        f"the 404 must name the reason ('Plan not found'); got {body!r}"
    )
    assert "Plan not found" in resp.text, (
        f"the response body must contain 'Plan not found'; got {resp.text!r}"
    )

    # A read must not materialise the plan it was asked about.
    assert not (populated_plans_root / MISSING_PLAN_ID).exists(), (
        "answering 404 must not create the plan directory it just said "
        "does not exist"
    )

    # Negative control: the same route, with the plan actually there.
    (populated_plans_root / MISSING_PLAN_ID).mkdir(parents=True, exist_ok=True)
    found = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")
    assert found.status_code == 200, (
        f"a plan that exists must not be answered 404; got "
        f"{found.status_code}: {found.text!r}"
    )
    assert found.json().get("plan_id") == MISSING_PLAN_ID, (
        f"the answer must name the plan it is about; got {found.text!r}"
    )


# ---------------------------------------------------------------------------
# A plan that does not exist says WHERE it looked
# ---------------------------------------------------------------------------


def test_missing_plan_404_reports_the_plans_dir_in_use(
    populated_plans_root: Path,
) -> None:
    """A 404 says **where** it looked.

    "Plan not found" is a true answer about a plan that is not there —
    and a false answer about a plan that IS there, in another
    instance's tree.  The two are told apart by the paths the answer
    carries: the plans root this process is configured with, and the
    plan directory it resolved inside it.

    The ``detail`` stays what it was — a client that keys off the
    message keeps working — and no stack trace is added: the point of
    these fields is to say which directory was searched, not to explain
    the search.

    ``searched_dir`` is the spelling the ``503`` answer uses as well,
    so a caller can compare the two answers field by field.
    """
    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 404, (
        f"a legal plan id that names no plan must be answered 404; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == "Plan not found", (
        f"the 404 must still name the reason; got {body!r}"
    )
    assert body.get("plans_dir") == str(Path(server.PLANS_DIR)), (
        f"the 404 must name the plans root it searched; got {body!r}"
    )
    assert body.get("plan_dir") == str(
        Path(server.PLANS_DIR) / MISSING_PLAN_ID
    ), (
        f"the 404 must name the plan directory it resolved; got {body!r}"
    )
    assert body.get("searched_dir") == str(Path(server.PLANS_DIR)), (
        f"the 404 must echo the directory it searched under the same "
        f"spelling the 503 answer uses; got {body!r}"
    )
    assert "Traceback" not in resp.text, (
        f"the answer must not carry a stack trace: {resp.text!r}"
    )


# ---------------------------------------------------------------------------
# A plans tree that cannot be searched
# ---------------------------------------------------------------------------


def test_an_empty_plans_dir_answers_503_not_404(plans_root: Path) -> None:
    """An empty data plane cannot say "the plan is not there".

    This is the case the ``404`` used to swallow: an instance whose
    plans tree is empty answers ``"Plan not found"`` for **every** plan
    id, and a caller that believes it is looking for a plan that is
    genuinely gone never learns that the instance is looking in the
    wrong place.

    So the tree is checked before the plan is, and an empty tree is
    answered ``503`` — a state this instance can be asked about again,
    once there is a tree to search.
    """
    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 503, (
        f"an empty plans tree must be answered 503; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == PLANS_DIR_UNAVAILABLE_DETAIL, (
        f"the 503 must name the reason ('{PLANS_DIR_UNAVAILABLE_DETAIL}'); "
        f"got {body!r}"
    )
    assert body.get("searched_dir") == str(Path(server.PLANS_DIR)), (
        f"the 503 must name the directory that could not be searched; "
        f"got {body!r}"
    )
    assert "Plan not found" not in resp.text, (
        f"an unsearchable tree must not be answered as a missing plan: "
        f"{resp.text!r}"
    )

    # Negative control: the same request against a tree with a plan in
    # it is answered 404 — the two states are told apart.
    (plans_root / "20261024-some-other-plan").mkdir(
        parents=True, exist_ok=True,
    )
    found = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")
    assert found.status_code == 404, (
        f"a searchable tree must answer 404 for a missing plan; got "
        f"{found.status_code}: {found.text!r}"
    )


def test_a_missing_plans_dir_answers_503(monkeypatch, tmp_path: Path) -> None:
    """A tree that is not there at all is answered 503 too.

    The instance was pointed at a directory that does not exist.  That
    is not the same fact as "the plan does not exist", and the answer
    says which one it is.
    """
    gone = tmp_path / "plans-that-are-not-there"
    monkeypatch.setattr(server, "PLANS_DIR", gone)

    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 503, (
        f"a plans tree that is not there must be answered 503; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == PLANS_DIR_UNAVAILABLE_DETAIL, (
        f"the 503 must name the reason; got {body!r}"
    )
    assert body.get("searched_dir") == str(gone), (
        f"the 503 must name the directory that could not be searched; "
        f"got {body!r}"
    )


def test_an_unreadable_plans_dir_answers_503(
    monkeypatch, plans_root: Path,
) -> None:
    """A tree that cannot be listed is answered 503.

    Permission is simulated rather than arranged: a directory whose
    mode is 000 is still readable by root, and a test that depends on
    the test-runner's uid is a test that fails in the wrong place.

    The refusal is raised from ``os.scandir`` — the call the route
    actually makes to look in the tree — so the case is exercised
    rather than passed for another reason.
    """
    real_scandir = os.scandir

    def _denied(path):
        if Path(path) == plans_root:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _denied)

    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 503, (
        f"an unreadable plans tree must be answered 503; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == PLANS_DIR_UNAVAILABLE_DETAIL, (
        f"the 503 must name the reason; got {body!r}"
    )
    assert body.get("searched_dir") == str(plans_root), (
        f"the 503 must name the directory that could not be searched; "
        f"got {body!r}"
    )


def test_a_plans_dir_that_is_not_a_directory_answers_503(
    monkeypatch, tmp_path: Path,
) -> None:
    """Something that is not a tree is not a tree to search."""
    not_a_dir = tmp_path / "plans-file"
    not_a_dir.write_text("not a directory\n")
    monkeypatch.setattr(server, "PLANS_DIR", not_a_dir)

    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 503, (
        f"a plans path that is not a directory must be answered 503; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("detail") == PLANS_DIR_UNAVAILABLE_DETAIL, (
        f"the 503 must name the reason; got {body!r}"
    )
    assert body.get("searched_dir") == str(not_a_dir), (
        f"the 503 must name the path that could not be searched; got {body!r}"
    )


# ---------------------------------------------------------------------------
# A plan id that is malformed
# ---------------------------------------------------------------------------


def test_malformed_plan_id_is_rejected_uniformly(plans_root: Path) -> None:
    """A malformed plan id is refused with 400, exactly as the execution route refuses it.

    Every route that accepts a plan id refuses a malformed one the same
    way.  The execution routes are the reference: they call
    ``_server._validated_plan_id`` explicitly, so their answer is the
    contract the plan-status route has to match — status code *and*
    body.  A route that answered 404 for a malformed id would send the
    caller hunting for a plan that never existed; one that answered 200
    would be reporting a plan it cannot name.

    The id is judged before the tree is: a malformed id is a bad
    request whatever the tree looks like, so the empty tree of this
    fixture does not turn the ``400`` into a ``503``.

    The last assertion is the structural half: the route has to state
    the guard in its own body.  The refusal is observable today only
    because ``_plan_dir`` validates on the way to ``PLANS_DIR / plan_id``
    — an accident, and the kind of accident that disappears the day a
    helper is relaxed.
    """
    client = _client()

    for raw, segment in _MALFORMED_IDS:
        resp = client.get(f"/api/plan/{segment}/status")

        assert resp.status_code == 400, (
            f"a malformed plan id ({raw!r}) must be refused with 400; got "
            f"{resp.status_code}: {resp.text!r}"
        )
        body = resp.json()
        assert body.get("error") == "Invalid plan id", (
            f"the 400 must say the id was invalid; got {body!r}"
        )

        reference = client.get(f"/api/execution/{segment}/status")
        assert reference.status_code == resp.status_code, (
            f"the plan-status route answered {resp.status_code} for "
            f"{raw!r} while the execution route answered "
            f"{reference.status_code} — the two must agree"
        )
        assert reference.json() == body, (
            f"the plan-status route and the execution route answered "
            f"{raw!r} differently: {body!r} vs {reference.json()!r}"
        )

    source = inspect.getsource(_plan_status_endpoint())
    assert "_validated_plan_id(plan_id)" in source, (
        "GET /api/plan/{plan_id}/status does not admit the plan id "
        "through the shared validator itself — its refusal is an accident "
        "of _plan_dir validating on the way to PLANS_DIR / plan_id, and "
        "it disappears the moment that helper is relaxed"
    )


# ---------------------------------------------------------------------------
# The answer, as the round's probe sees it
# ---------------------------------------------------------------------------
#
# The round starts its services **detached**: they outlive the backend
# that started them — "Start the service detached so it outlives you"
# (``service_restart_agent``).  The probe that failed on 2026-10-26 at
# 02:24:59 was made against such a service, on a port another instance
# took over mid-round, and it got ``connection refused``.
#
# So the question this section asks is the one the round asked: when the
# service is still up — its starter long gone — what does it answer for
# a plan id that names no plan?
#
# The answer has to be a **404 that says "Plan not found"**, and it has
# to say WHICH instance gave it: the tree it searched, the state
# database it reads (by inode — a different file at the same path is a
# different data plane), and the plan ids it can see.  An answer that
# cannot be attributed to an instance is what turned "the wrong instance
# is answering" into "the plan is gone": in two earlier rounds the
# endpoint answered "Plan not found" for **every** plan id, because the
# instance on the port was looking at another round's data plane.

#: The plan the round's own service knows about.  Its row is what makes
#: the instance's state plane non-empty — an instance that has never
#: routed a plan has nothing to compare a missing plan against.
ROUND_PLAN_ID = "20261026-vp-round-plan"

#: The plan id the round's probe asked about — a legal id, naming no
#: plan in that instance.
PROBED_PLAN_ID = "20261026-vp-probed-missing"

#: The starter hands the service to a session of its own and exits, so
#: the service is left running with no starter at all.
_STARTER_SOURCE = """
import os
import subprocess
import sys

env = dict(os.environ)
env.update({env!r})
log = open({log!r}, "wb")
proc = subprocess.Popen(
    {cmd!r},
    cwd={cwd!r},
    env=env,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
open({pid_file!r}, "w").write(str(proc.pid))
"""


def _free_port() -> int:
    """A port nobody is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(port: int, timeout: float = 120.0) -> bool:
    """Wait until *port* accepts a connection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _http_get(port: int, path: str) -> Tuple[int, str]:
    """A GET against a running server, with the request guard's header."""
    import request_guard

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={
            request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _start_service_that_outlives_its_starter(
    port: int, plans_dir: Path, state_db: Path, root: Path,
) -> int:
    """Start the round's service through a starter that exits at once.

    This is the shape the round itself uses: the service is started
    detached, so it keeps answering after the backend that started it is
    gone.  The starter here is a process of its own — it spawns the
    service into a new session, writes its pid down, and exits — so
    what answers the port is a service whose starter no longer exists.

    Returns the service's pid.
    """
    pid_file = root / "service.pid"
    log_file = root / "service.log"
    source = _STARTER_SOURCE.format(
        env={
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_PORT": str(port),
            # The child inherits this process's environment, and the
            # hermetic env pins ``PDT_PROVIDER_MODE=cc-switch`` for the
            # pytest process.  The child must not inherit that mode:
            # CC Switch is a plugin of one operator's machine, and on a
            # clean runner nothing is listening on its port, so the
            # child's startup aborts ("Application startup failed") and
            # the service never comes up.  Pin the mode here rather
            # than trust the machine the suite happens to run on.
            "PDT_PROVIDER_MODE": "inherit",
        },
        log=str(log_file),
        cmd=[sys.executable, "-m", "backend.server"],
        cwd=str(Path(__file__).resolve().parents[3]),
        pid_file=str(pid_file),
    )
    starter = subprocess.Popen([sys.executable, "-c", source])
    starter.wait(timeout=120)
    assert starter.returncode == 0, (
        f"the starter failed to hand the service over; it exited with "
        f"{starter.returncode}"
    )
    return int(pid_file.read_text().strip())


def _stop_service(pid: int) -> None:
    """Stop the service, and everything it started with it."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _seed_routing_row(state_db: Path, plan_id: str) -> None:
    """Give the instance a plan of its own.

    A routing row is what makes the state plane non-empty: it is the
    instance's own record that it has plans at all, and a state plane
    that holds nothing cannot say that a plan it does not hold is
    missing.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(state_db)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "waiting_for_quota")
    finally:
        conn.close()


def _clear_plan_rows(state_db: Path) -> None:
    """Empty the state plane: the database is there and holds no plans.

    This is the state a leftover of another round is in — a state
    database that exists, and that says nothing about the tree this
    instance serves.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    conn = db_open(state_db)
    try:
        migrate(conn)
        conn.execute("DELETE FROM plan_routing")
        conn.execute("DELETE FROM plan_execution")
    finally:
        conn.close()


def test_the_missing_plan_answer_stays_reachable_after_the_round_service_outlives_its_starter(
    tmp_path: Path,
) -> None:
    """The missing-plan answer stays reachable when the service outlives its starter.

    The round's probe was made against a service that was started
    detached — the service outlives the backend that started it — and
    the answer it got was ``connection refused``: a fact about the
    port's lifetime, not about the plan.  So the contract this pins is
    what that service answers **while it is still up**: a plan id that
    names no plan is answered ``404`` with the detail ``Plan not
    found`` — not a ``500``, not an empty ``200``, and not a refusal.

    And the answer says **who** gave it.  That is the half that was
    missing in the rounds where the endpoint answered "Plan not found"
    for every plan id: the instance on the port was looking at another
    round's data plane, and nothing in the answer said so.  So the body
    carries the tree that was searched, the state database that was
    read (by path and by inode — a different file at the same path is a
    different data plane), and the plan ids this instance can see.

    The second half is the negative control that makes the ``404``
    mean something: the same service, with its state plane emptied, is
    an instance that cannot see whether the plan exists — and that is
    ``503``, not a ``404`` that claims the plan is gone.
    """
    plans_dir = tmp_path / "plans"
    (plans_dir / ROUND_PLAN_ID).mkdir(parents=True)
    state_db = tmp_path / "state.db"
    _seed_routing_row(state_db, ROUND_PLAN_ID)

    port = _free_port()
    pid = _start_service_that_outlives_its_starter(
        port, plans_dir, state_db, tmp_path,
    )
    try:
        assert _wait_listening(port), (
            f"the round's service never started listening on port {port}; "
            f"its log says:\n"
            f"{(tmp_path / 'service.log').read_text(errors='replace')}"
        )

        status, text = _http_get(port, f"/api/plan/{PROBED_PLAN_ID}/status")

        assert status == 404, (
            f"a plan id that names no plan must be answered 404 while the "
            f"service is up; got {status}: {text!r}"
        )
        body = json.loads(text)
        assert body.get("detail") == "Plan not found", (
            f"the 404 must name the reason ('Plan not found'); got {body!r}"
        )
        assert "Plan not found" in text, (
            f"the response body must contain 'Plan not found'; got {text!r}"
        )
        assert "Traceback" not in text, (
            f"the answer must not carry a stack trace: {text!r}"
        )

        # -- which instance answered -----------------------------------
        assert body.get("searched_dir") == str(plans_dir), (
            f"the 404 must name the tree that was searched; got {body!r}"
        )
        assert body.get("state_db_inode") == state_db.stat().st_ino, (
            f"the 404 must name the state database that was read, by "
            f"inode; got {body!r}"
        )
        available = body.get("available_plan_ids")
        assert isinstance(available, list), (
            f"the 404 must carry the plan ids the answering instance can "
            f"see; got {body!r}"
        )
        assert ROUND_PLAN_ID in available, (
            f"the answering instance holds {ROUND_PLAN_ID!r}, so the list "
            f"of the ids it can see must name it; got {available!r}"
        )
        assert len(available) <= 20, (
            f"the list of visible plan ids is capped at 20; got "
            f"{len(available)}: {available!r}"
        )

        # -- the negative control: the same service, its state emptied -
        _clear_plan_rows(state_db)

        status, text = _http_get(port, f"/api/plan/{PROBED_PLAN_ID}/status")

        assert status == 503, (
            f"an instance whose state plane holds no plan cannot say that "
            f"a plan it does not hold is missing; got {status}: {text!r}"
        )
        body = json.loads(text)
        assert "Plan not found" not in text, (
            f"an instance that cannot see the plan must not be answered as "
            f"a missing plan: {text!r}"
        )
        assert body.get("searched_dir") == str(plans_dir), (
            f"the 503 must name the tree that was searched; got {body!r}"
        )
        assert body.get("state_db_inode") == state_db.stat().st_ino, (
            f"the 503 must name the state database that was read, by "
            f"inode; got {body!r}"
        )
        assert isinstance(body.get("available_plan_ids"), list), (
            f"the 503 must carry the plan ids the answering instance can "
            f"see; got {body!r}"
        )
    finally:
        _stop_service(pid)


def test_a_missing_plan_404_carries_the_serving_instance_identity(
    populated_plans_root: Path, tmp_path: Path,
) -> None:
    """A 404 says which instance gave it, and what that instance can see.

    "Plan not found" is a true answer about a plan that is not there —
    and a false answer about a plan that IS there, in another
    instance's tree.  The two are told apart by the facts the answer
    carries: the tree that was searched, the state database that was
    read, and the plan ids the answering instance can see.
    """
    state_db = tmp_path / "state.db"
    _seed_routing_row(state_db, "20261024-some-other-plan")

    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 404, (
        f"a legal plan id that names no plan must be answered 404; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("searched_dir") == str(populated_plans_root), (
        f"the 404 must name the tree that was searched; got {body!r}"
    )
    assert body.get("state_db_inode") == state_db.stat().st_ino, (
        f"the 404 must name the state database that was read, by inode; "
        f"got {body!r}"
    )
    available = body.get("available_plan_ids")
    assert isinstance(available, list), (
        f"the 404 must carry the plan ids the answering instance can see; "
        f"got {body!r}"
    )
    assert "20261024-some-other-plan" in available, (
        f"the answering instance holds that plan, so the list of the ids "
        f"it can see must name it; got {available!r}"
    )
    assert len(available) <= 20, (
        f"the list of visible plan ids is capped at 20; got "
        f"{len(available)}: {available!r}"
    )


def test_an_instance_whose_state_plane_holds_no_plan_answers_503(
    populated_plans_root: Path, tmp_path: Path,
) -> None:
    """A state plane that holds nothing cannot say a plan is missing.

    The state database is there and holds no plan rows: this instance
    has no plans of its own, so the plan the caller asked about may
    well be somewhere else.  "Plan not found" would be a claim it is not
    entitled to make — and it would be the same claim for every plan
    id, which is exactly what the earlier rounds ran into.
    """
    state_db = tmp_path / "state.db"
    _clear_plan_rows(state_db)

    client = _client()

    resp = client.get(f"/api/plan/{MISSING_PLAN_ID}/status")

    assert resp.status_code == 503, (
        f"an instance whose state plane holds no plan must be answered "
        f"503; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert "Plan not found" not in resp.text, (
        f"an instance that cannot see the plan must not be answered as a "
        f"missing plan: {resp.text!r}"
    )
    assert body.get("searched_dir") == str(populated_plans_root), (
        f"the 503 must name the tree that was searched; got {body!r}"
    )
    assert body.get("state_db_inode") == state_db.stat().st_ino, (
        f"the 503 must name the state database that was read, by inode; "
        f"got {body!r}"
    )
    assert isinstance(body.get("available_plan_ids"), list), (
        f"the 503 must carry the plan ids the answering instance can see; "
        f"got {body!r}"
    )
