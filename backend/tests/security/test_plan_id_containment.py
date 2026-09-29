"""A request-supplied ``plan_id`` never escapes the plans root.

Regression guard for the traversal closed on 2026-09-25.

What was wrong
--------------
``server.py`` built plan paths as a bare ``PLANS_DIR / plan_id`` at 73
call sites. ``validate_plan_id`` existed in ``framework/ids.py`` and its
docstring claimed that *"every place that touches ``plans/{plan_id}``
MUST funnel through it"* — but no HTTP handler called it. The guard was
wired into the watchdog and the signal writer; the API never got it.

``{plan_id}`` cannot smuggle a literal ``/`` (the router splits on it
and the route stops matching), but it *can* be ``..``. Measured against
the app as it was::

    GET /api/plan/%2e%2e/status   -> 200  {"plan_id": "..", ...}
    GET /api/plan/%2e%2e/summary  -> 200  {"plan_id": "..", ...}

``PLANS_DIR / ".."`` is the repository root, so reads reported the
checkout as if it were a plan — and the write routes (PRD / arch / test
generation, interview) would have written ``prd.json``,
``plan_state.json`` and friends *beside the source tree*, outside the one
directory the design says plans live in.

Three kinds of guard, because they fail differently:

* a **static** one over ``server.py``'s source, so a new bare join fails
  at the line that introduced it;
* a **behavioural** one that enumerates every ``GET`` route carrying
  ``{plan_id}`` and sends it a traversal — new routes are covered the
  moment they are registered; and
* a **source-level** one for the write routes, which are checked by
  reading their body rather than by calling them. Sending ``POST
  /api/execution/../start`` would be safe today (the id check runs
  first) but would have real side effects the day someone removes that
  check — a test whose failure mode is "starts an execution" is worse
  than no test.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


SERVER_PY = Path(server.__file__).resolve()

#: Both ways a handler is allowed to admit a plan id.
_GUARDS = ("_plan_dir(plan_id)", "_validated_plan_id(plan_id)")


@pytest.fixture
def plans_root(tmp_path, monkeypatch):
    """Point the server at a scratch plans root.

    ``PLANS_DIR`` is read as a module global at call time, so patching it
    redirects every route — and keeps these tests off the developer's
    real ``plans/`` tree.
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", root)
    return root


# ---------------------------------------------------------------------------
# Static gate over the whole module
# ---------------------------------------------------------------------------


def _plans_dir_join_lines(source: str) -> list[int]:
    """Line numbers of every ``PLANS_DIR / <expr>`` **in code**.

    Parsed rather than grepped on purpose: ``_plan_dir``'s own docstring
    quotes the historical ``PLANS_DIR / plan_id`` pattern when explaining
    what went wrong, and a text scan cannot tell that sentence apart from
    the bug it describes.
    """
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        left = node.left
        if isinstance(left, ast.Name) and left.id == "PLANS_DIR":
            lines.append(node.lineno)
    return sorted(lines)


def test_the_only_plans_dir_join_is_the_validated_one():
    """Any new ``PLANS_DIR / <something>`` in ``server.py`` fails here.

    This is the guard that would have caught the original bug at review
    time, and the one that stops it growing back one handler at a time.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    lines = source.splitlines()
    joins = _plans_dir_join_lines(source)
    where = [f"  line {n}: {lines[n - 1].strip()}" for n in joins]
    assert len(joins) == 1, (
        f"server.py contains {len(joins)} ``PLANS_DIR /`` joins; exactly "
        "one is expected (inside ``_plan_dir``). A bare join is how a "
        "request-supplied plan_id escaped the plans root.\n" + "\n".join(where)
    )
    assert "_validated_plan_id(plan_id)" in lines[joins[0] - 1], (
        f"the single ``PLANS_DIR`` join does not go through the validator: "
        f"{where[0].strip()!r}"
    )


def test_the_shared_validator_is_the_one_used():
    """Pinned so a future edit cannot swap in a weaker local check.

    ``framework/ids.validate_plan_id`` is the same guard the watchdog and
    the signal writer use; having one implementation is the point.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    assert "from framework.ids import" in source
    assert "validate_plan_id" in source, (
        "server.py no longer imports the shared plan-id validator"
    )


# ---------------------------------------------------------------------------
# Every route with a {plan_id}: source-level and behavioural
# ---------------------------------------------------------------------------


def _plan_id_routes():
    """(methods, path, endpoint) for every route carrying ``{plan_id}``.

    Enumerated through ``server.iter_app_routes()`` rather than
    ``server.app.routes``: routers extracted into ``backend/routes/`` land as
    a single ``_IncludedRouter`` entry (FastAPI ≥ 0.141), so walking
    ``app.routes`` alone would quietly stop seeing every route that moved —
    and this whole module is only as strong as the set it enumerates.
    """
    out = []
    for route in server.iter_app_routes():
        path = getattr(route, "path", "")
        if "{plan_id}" not in path:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        out.append((sorted(getattr(route, "methods", None) or []), path, endpoint))
    return sorted(out, key=lambda r: r[1])


def test_there_are_routes_to_check():
    """Guard against the enumerator silently matching nothing."""
    routes = _plan_id_routes()
    assert len(routes) >= 40, (
        f"only {len(routes)} plan_id routes found — the enumerator is "
        "probably broken and every assertion below would be vacuous"
    )


@pytest.mark.parametrize("methods,path,endpoint", _plan_id_routes(),
                         ids=[r[1] for r in _plan_id_routes()])
def test_every_write_route_checks_the_id(methods, path, endpoint):
    """A route that can be handed a ``plan_id`` must admit it through one
    of the two guards — checked by reading the body, not by calling it."""
    if "GET" in methods:
        pytest.skip("GET routes are covered behaviourally below")
    source = inspect.getsource(endpoint)
    assert any(g in source for g in _GUARDS), (
        f"{path} → {endpoint.__name__} accepts a plan_id without admitting "
        f"it through {' or '.join(_GUARDS)}. Every path this route could "
        f"build from that id inherits the hole."
    )


def _get_plan_id_routes():
    return [(p, e) for m, p, e in _plan_id_routes() if "GET" in m]


@pytest.mark.parametrize("path,endpoint", _get_plan_id_routes(),
                         ids=[p for p, _ in _get_plan_id_routes()])
def test_no_get_route_accepts_a_traversal(plans_root, path, endpoint):
    """``..`` is refused by every read route that takes a plan id.

    The property under test is *refused*, not a particular status code:
    most routes reject the id outright (400), while a couple look the
    plan up first and report it missing (404). Both are correct; what
    would not be is 200 — the app treating ``..`` as a plan it knows.
    """
    url = re.sub(r"\{[^}]+\}", "0", path.replace("{plan_id}", "%2e%2e"))
    # No context manager: entering it runs the lifespan and starts the
    # supervisor fleet, which a routing test has no business doing.
    client = TestClient(server.app)
    got = client.get(url)
    assert got.status_code in (400, 404), (
        f"GET {url} returned {got.status_code} — a plan id of '..' must be "
        f"refused, not answered as a plan"
    )


# ---------------------------------------------------------------------------
# The validator, and the happy path it must not break
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["..", ".", "../etc", "/etc/passwd", "a/b",
                                 "a\\b", "", "plan\x00id", "plan id"])
def test_validator_rejects_unsafe_ids(bad):
    with pytest.raises(HTTPException) as excinfo:
        server._validated_plan_id(bad)
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize("good", ["20260925-plan", "resume-test-1790215922380",
                                  "a", "a.b_c-d"])
def test_validator_accepts_real_plan_ids(good):
    assert server._validated_plan_id(good) == good


def test_plan_dir_lands_inside_the_plans_root(plans_root):
    assert server._plan_dir("20260925-plan") == plans_root / "20260925-plan"


def test_a_real_plan_still_resolves(plans_root):
    """The fix must not cost access to plans that actually exist."""
    (plans_root / "20260925-plan").mkdir()
    (plans_root / "20260925-plan" / "tasks.json").write_text("{}")
    assert server._plan_dir("20260925-plan").is_dir()
