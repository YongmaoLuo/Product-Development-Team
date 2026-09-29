"""Boundary matrix for the ``plan_id`` validator and every route that
takes one.

This module pins the **accepted** and **rejected** cases around
``framework.ids.validate_plan_id`` — the single shared guard the API
uses (via ``server._validated_plan_id`` / ``server._plan_dir``), the
watchdog uses, and the signal writer uses. One implementation, one
contract; this matrix is the contract.

Two layers of coverage, on purpose
----------------------------------

1. **Behavioural matrix against ``GET /api/plan/{plan_id}/status``**
   (the canonical read route — every other ``{plan_id}`` route funnels
   through the same validator via :func:`server._plan_dir`). Bad ids
   must come back as 4xx — never 500 — and the state-machine SQLite
   database must not be mutated. The 4xx band is loose on purpose:
   ``400`` when the validator rejects outright, ``404`` when the route
   looks up a missing plan, ``422`` for FastAPI's own pydantic failure
   on something the validator never saw. The property under test is
   "the request is refused, the server is still up, the state is
   untouched" — not "the response code is exactly 400".

2. **Length-boundary matrix** against the validator itself. The
   ``MAX_PLAN_ID_LEN = 64`` cap is the fix this module landed:
   before the cap the byte-set check let a 300-char id through, so
   every path under ``_plan_dir`` would happily join a 300-byte leaf
   onto the plans root. The 64/65 boundary pins both sides of the
   inequality — accepting 64, rejecting 65 — so a future "let's be
   generous" relaxation fails loudly here rather than at runtime.

Why ``TestClient`` and not raw ``httpx``
----------------------------------------

``request_guard`` is the production posture: every ``/api/*`` request
must carry ``X-PDT-Request: 1`` and ``Host: testserver``. A test that
builds its own ``httpx.AsyncClient(transport=ASGITransport(...))``
sends neither and is 403-rejected before its assertions even run.
``conftest.py`` already injects the header into every ``TestClient``;
using the existing wrapper is what keeps the suite honest about the
guard rather than skipping it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from framework.ids import (  # noqa: E402
    MAX_PLAN_ID_LEN,
    InvalidPlanIdError,
    validate_plan_id,
)


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


#: Every route this module exercises. ``GET /api/plan/{plan_id}/status``
#: is the canonical read path (most route handlers funnelling through
#: ``_plan_dir`` look almost identical from the validator's point of view),
#: and pinning one well-known route keeps the matrix small. Add more if a
#: future extraction changes the validator's caller list.
CANONICAL_ROUTE = "/api/plan/{plan_id}/status"

#: An obviously safe plan id that derives from ``derive_plan_id``'s shape
#: (``YYYYMMDD-slug``) but is hand-picked so the test cannot drift with
#: the calendar. ``plan-20260926-abc`` is the literal from the task brief.
HAPPY_PLAN_ID = "plan-20260926-abc"


@pytest.fixture
def plans_root(tmp_path, monkeypatch):
    """Point ``PLANS_DIR`` at a scratch tree; the suite stays off the
    developer's real ``plans/`` directory.

    The autouse ``isolated_plans_dir`` fixture in ``conftest.py`` already
    redirects ``PDT_PLANS_DIR`` and ``PDT_STATE_DB_PATH``; this fixture is
    here so the directory is explicitly created before any route is hit,
    and so a test that wants the path back can read it.
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", root)
    return root


@pytest.fixture
def client():
    """A ``TestClient`` whose request-guard header is already injected.

    See ``conftest.py``'s ``_install_request_guard_header`` — every
    ``TestClient`` carries ``X-PDT-Request: 1`` and ``Host: testserver``
    by construction, which is what ``request_guard`` requires. Using the
    wrapper rather than a raw ``httpx`` client keeps the guard covered
    rather than skipped.
    """
    return TestClient(server.app)


def _expect_rejected(client: TestClient, plan_id: str) -> int:
    """Hit ``CANONICAL_ROUTE`` with ``plan_id`` and return the status.

    The test must hold three properties on the response:

    * status code is in ``{400, 404, 422}`` — never 500, never 200;
    * the state-machine SQLite database has no new rows for this id;
    * the plans directory still has no leaf named after this id.

    The actual status code is returned so individual tests can assert
    finer bands (e.g. the validator-level test below expects 400, the
    HTTP-level tests accept any 4xx).
    """
    url = CANONICAL_ROUTE.format(plan_id=quote(plan_id, safe=""))
    response = client.get(url)
    assert response.status_code in (400, 404, 422), (
        f"GET {url!r} returned {response.status_code}; "
        f"a malformed plan_id must be refused with a 4xx, never answered "
        f"as if it were a plan"
    )
    # 200 sneaking in is the most dangerous failure mode: a route that
    # answers 200 for ``..`` is exactly the 2026-09-25 regression this
    # whole surface exists to keep closed. Re-assert it explicitly so a
    # future change to the 4xx tuple above cannot quietly permit 200.
    assert response.status_code != 200, (
        f"GET {url!r} answered 200 for a known-bad plan id; the validator "
        f"either was bypassed or its contract drifted"
    )
    return response.status_code


def _state_db_row_count(db_path: Path, plan_id: str) -> int:
    """Sum ``plan_*`` rows for ``plan_id`` in the test's ``state.db``.

    ``server._state_db_path`` is redirected to ``tmp_path / state.db`` by
    the autouse ``isolated_plans_dir`` fixture, so this read targets the
    database the route under test just wrote to — not the developer's
    live one.
    """
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        total = 0
        for table in (
            "plan_routing",
            "plan_execution",
            "plan_verification",
            "plan_artifacts",
            "plan_tasks",
        ):
            try:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?",
                    (plan_id,),
                )
                total += cur.fetchone()[0]
            except sqlite3.OperationalError:
                # Table missing — the schema is migrated lazily; absent
                # means there cannot be a row for any plan id.
                continue
        return total
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy path — pin the *accepted* side so a future tightening of the
# validator does not silently regress real plans.
# ---------------------------------------------------------------------------


def test_valid_plan_id_is_accepted(client, plans_root):
    """A canonical ``YYYYMMDD-slug`` plan with the plan directory on disk
    returns 200 from the read route.

    The test materialises ``HAPPY_PLAN_ID`` under ``plans_root`` first
    because ``get_plan_status`` answers 404 when the plan is missing
    (see ``routes/plans.py::get_plan_status``). A 200 here means the
    id survived validation AND the route found the directory.
    """
    (plans_root / HAPPY_PLAN_ID).mkdir()

    response = client.get(CANONICAL_ROUTE.format(plan_id=HAPPY_PLAN_ID))

    assert response.status_code == 200, (
        f"GET /api/plan/{HAPPY_PLAN_ID}/status returned {response.status_code}; "
        f"a plan that exists on disk must be answered 200 — either the "
        f"validator rejected it (over-tightening) or the route misread the "
        f"plan directory"
    )


def test_max_len_boundary_is_exact():
    """``MAX_PLAN_ID_LEN = 64`` is exact: 64 chars accepted, 65 rejected.

    Pins both sides of the inequality so a future "let's bump it to 128"
    relaxation fails here on the 65-char assertion. Without this gate a
    later relaxation could ship unnoticed because every other test in
    this module stays under 64 chars.

    The validator-level check is deliberate: the route path is exercised
    by ``test_oversized_plan_id_is_rejected`` and ``test_valid_*``; this
    test pins the constant itself, which is the boundary the validator
    enforces.
    """
    id_64 = "a" * MAX_PLAN_ID_LEN
    assert validate_plan_id(id_64) == id_64, (
        f"validator rejected a {MAX_PLAN_ID_LEN}-char id; the boundary "
        f"should be inclusive on the lower edge"
    )

    id_65 = "a" * (MAX_PLAN_ID_LEN + 1)
    with pytest.raises(InvalidPlanIdError) as excinfo:
        validate_plan_id(id_65)
    assert str(MAX_PLAN_ID_LEN) in str(excinfo.value), (
        f"validator rejected a {MAX_PLAN_ID_LEN + 1}-char id with a message "
        f"that does not name the limit: {excinfo.value!s}"
    )


# ---------------------------------------------------------------------------
# Rejection matrix — the seven "every bad input is refused" properties.
# ---------------------------------------------------------------------------


def test_empty_plan_id_is_rejected(client):
    """Empty string is refused (validator returns ``InvalidPlanIdError``,
    the route translates to ``400``).

    The fastapi router cannot actually match an empty segment — ``{}``
    must have content — so the route answers ``404`` (route not found)
    rather than ``400`` (validation rejected). Both are correct; the
    contract is "no 200, no 500, no state change".
    """
    status = _expect_rejected(client, "")
    assert status in (400, 404, 422)


def test_oversized_plan_id_is_rejected(client, tmp_path):
    """A 300-character plan id is refused, the state db is unchanged,
    the plans root has no 300-char leaf.

    This is the case the ``MAX_PLAN_ID_LEN`` cap exists to close.
    Before the cap, a 300-char id passed the byte-set guard and was
    joined onto the plans root by ``_plan_dir`` — the request would
    then 404 because no such directory exists, but the validator had
    already silently let a clearly-attacker-controlled length through.
    """
    bad_id = "a" * 300
    before = _state_db_row_count(tmp_path / "state.db", bad_id)
    plans_before = set((tmp_path / "plans").iterdir()) if (tmp_path / "plans").exists() else set()

    status = _expect_rejected(client, bad_id)

    after = _state_db_row_count(tmp_path / "state.db", bad_id)
    plans_after = set((tmp_path / "plans").iterdir()) if (tmp_path / "plans").exists() else set()
    assert before == after, (
        f"a 300-char plan id mutated state.db ({before} → {after} rows); "
        f"the validator must reject before any state write"
    )
    assert plans_before == plans_after, (
        f"a 300-char plan id created a leaf on disk "
        f"(before={plans_before}, after={plans_after})"
    )
    # Most routes reach the validator and answer 400; ``status`` itself
    # never escapes 4xx (see _expect_rejected).
    assert status in (400, 404, 422)


@pytest.mark.parametrize(
    "bad",
    [
        "../etc",
        "..%2f..",  # URL-encoded traversal — percent-decoded by Starlette
        "plans/../../x",
        "a\\b",  # Windows-style separator
        "..",
        ".",
    ],
)
def test_path_traversal_sequences_are_rejected(client, tmp_path, bad):
    """Every traversal-shaped id is refused, the state db is unchanged.

    ``_expect_rejected`` enforces the 4xx-band + no-200 contract; this
    test re-asserts the *state-db zero-change* contract because that is
    the property ``test_plan_id_containment.py`` already pins for the
    ``..`` case — here we extend it to the full traversal vector set
    AND confirm that no plan_execution / plan_routing / plan_verification
    row leaks in even when the request gets further than the validator.
    """
    # ``quote(safe="")`` so the percent-encoded variant reaches the
    # route as ``..%2f..`` and the unencoded ones reach it as their
    # literal chars; Starlette's router handles both shapes.
    before = _state_db_row_count(tmp_path / "state.db", bad)
    plans_before = set((tmp_path / "plans").iterdir()) if (tmp_path / "plans").exists() else set()

    status = _expect_rejected(client, bad)

    after = _state_db_row_count(tmp_path / "state.db", bad)
    plans_after = set((tmp_path / "plans").iterdir()) if (tmp_path / "plans").exists() else set()
    assert before == after, (
        f"traversal id {bad!r} mutated state.db ({before} → {after} rows)"
    )
    assert plans_before == plans_after, (
        f"traversal id {bad!r} created a leaf on disk"
    )
    assert status in (400, 404, 422)


@pytest.mark.parametrize(
    "bad",
    [
        "../etc",
        "..%2f..%2fetc%2fpasswd",
        "..%2F..%2Fetc%2Fpasswd",
        "plans/../../etc/passwd",
        "..",
        ".",
        # 2026-09-27 follow-through: the original criterion names the
        # canonical ``..%2F..%2Fetc%2Fpasswd`` only — pin every
        # alternative spelling the brief calls out so a future decoder
        # change cannot quietly let one through. Each variant reaches
        # the validator as ``../etc/passwd`` after URL decoding (or
        # the validator rejects before decoding), and the refusal must
        # still not echo the system-file contents.
        #
        # ``%2e%2e%2f`` is the fully percent-encoded spelling of
        # ``../`` — the dots and the slash are each percent-encoded so
        # the URL has no ASCII letters a human can pick out. Without
        # this row, a bug that accepts ``..%2F..`` but refuses
        # ``%2e%2e%2f%2e%2e%2f`` ships unnoticed.
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "%2E%2E%2F%2E%2E%2Fetc%2Fpasswd",
        # Double-encoded — every percent sign is itself percent-encoded
        # to ``%25``. Starlette's router decodes once, which yields
        # ``%2e%2e%2f`` (the previous variant) on the second decode, so
        # a "decode once" bypass turns this into a single-encoded
        # request, and a "decode twice" implementation would resolve
        # it back to the same canonical traversal. Either way the
        # request must be refused with no system-file bytes in the
        # body — that is the *negative* the criterion pins.
        "%252e%252e%252fetc%252fpasswd",
        "%252E%252E%252F%252E%252E%252Fetc%252Fpasswd",
    ],
)
def test_path_traversal_response_body_does_not_leak_system_file_content(
    client, plans_root, bad,
):
    """The 4xx *status* alone is not enough — the body must not carry the
    contents of a system file the traversal id would have resolved to.

    The status-band check (``_expect_rejected``) pins the "request is
    refused" half of the contract; this test pins the *other* half:
    a route that refused with 200 + the contents of ``/etc/passwd``
    would still be a breach. The body comes back from the test client
    as raw text (we deliberately do not parse it as JSON — the contract
    is "the bytes are not system-file bytes", which is independent of
    whether the response is JSON, plain text, or anything else).
    """
    url = CANONICAL_ROUTE.format(plan_id=quote(bad, safe=""))
    response = client.get(url)

    assert response.status_code in (400, 404, 422), (
        f"GET {url!r} returned {response.status_code}; a traversal-shaped "
        f"plan id must be refused with a 4xx"
    )
    body = response.text
    # ``/etc/passwd`` opens with a line of the form
    # ``root:x:0:0:root:/root:/bin/bash``; pin the well-known substring
    # so a future regression that streams the file's bytes into the
    # response body fails loudly here rather than only at the status band.
    assert "root:x:0:0" not in body, (
        f"GET {url!r} answered 4xx but the body still contains "
        f"'root:x:0:0'; a traversal-shaped plan id must not leak "
        f"system-file contents even when the status is a refusal. "
        f"Body: {body!r}"
    )


def test_control_characters_are_rejected(client, tmp_path):
    """A NUL byte inside an otherwise-valid id is refused.

    The byte-set guard ``[A-Za-z0-9._-]`` rejects NUL, newline and
    space alike. The state-db must not change.
    """
    bad = "p\x00lan"
    before = _state_db_row_count(tmp_path / "state.db", bad)

    status = _expect_rejected(client, bad)

    after = _state_db_row_count(tmp_path / "state.db", bad)
    assert before == after, (
        f"NUL-containing id mutated state.db ({before} → {after} rows)"
    )
    assert status in (400, 404, 422)


def test_unicode_normalised_traversal_is_rejected(client, tmp_path):
    """A Unicode sequence that NFKC-normalises to ``..`` is refused.

    Full-width period ``\\uff0e`` is a letter outside ``[A-Za-z0-9._-]``
    so the byte-set guard rejects the raw input. The defence-in-depth
    worry would be a downstream consumer that NFKC-normalises first
    and then evaluates ``..`` as a path component; this test pins the
    upstream contract so a future "normalise before validate" patch
    fails here before it reaches a route handler.
    """
    bad = "．．"
    # NFKC of full-width period is the ASCII period, so the normalised
    # form would be ``..`` — the canonical traversal component.
    import unicodedata
    assert unicodedata.normalize("NFKC", bad) == "..", (
        f"NFKC normalisation of {bad!r} is not '..' — "
        f"the test premise is wrong, not the validator"
    )

    before = _state_db_row_count(tmp_path / "state.db", bad)

    status = _expect_rejected(client, bad)

    after = _state_db_row_count(tmp_path / "state.db", bad)
    assert before == after, (
        f"NFKC-normalised traversal id mutated state.db "
        f"({before} → {after} rows)"
    )
    assert status in (400, 404, 422)