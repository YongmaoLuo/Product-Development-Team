"""HTTP contract: the plans list keeps a waiting-for-quota plan visible.

The criterion this file pins
----------------------------
The capacity-wait PRD says (import acceptance, negative control):

    *import 验收·负控：plan 进入 waiting_for_quota（非终态），仍可被重启；
    failed 才是终态*
    ("a plan that entered waiting_for_quota is not in a terminal state and
    can still be restarted; failed is the terminal state")

The list is where that distinction reaches the operator: the sidebar is
the only "is this plan still alive?" surface there is.  A plan parked
for capacity is **alive** — it will resume when the quota window opens —
so it must stay in the list.  A plan that actually failed is a
historical artifact and must be out of the default list (it is still
reachable with ``?include_terminal=true``, for audit and cleanup).

Why the previous round produced no evidence
-------------------------------------------
The probes for this criterion hit a **recycled backend**:

    GET /api/plans  ->  connection refused

and one probe that did reach a server answered ``[]`` — because it had
landed on a *different worktree's* empty database.  Neither answer says
anything about the contract: the first is a fact about the service's
lifetime, the second is a fact about which database was asked.  What the
criterion needs is a **repeatable** answer, produced by the test itself
against its own plans tree and its own state database.

The contract this file freezes
------------------------------
``GET /api/plans`` answers with two views, and this file pins both:

  * the **default** view — ``waiting_for_quota`` is NOT terminal, so a
    parked plan stays in the list, while a plan whose routing phase is
    ``failed`` is filtered out;
  * the **``?include_terminal=true``** view — the failed plan comes back,
    because "filtered out of the sidebar" and "gone" are different things.

The third test pins the **degradation gap** next to them.  When the
state database cannot be opened, the listing degrades to a directory
scan: no phase can be read, so the terminal-phase filter cannot be
applied.  Such a listing answers 200 with *every* plan it can see — a
"full list" that a reader could easily mistake for "the terminal plans
were filtered out".  The answer must therefore say, explicitly, that it
degraded.

Hermeticity
------------
The plans tree and the state database live under the test's own
``tmp_path`` — the autouse ``isolated_plans_dir`` fixture points
``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at it — so the
operator's live database and ``plans/`` tree are never touched.  The
rows are written through the real repositories, so the endpoint reads
back the shapes production writes.

The plan ids are the ones the PRD verification itself seeds
(``20261024-vp-quota-*``), so the evidence produced here and the
evidence produced against a live service are about the same plans.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

import server


# ---------------------------------------------------------------------------
# The fixture plans
# ---------------------------------------------------------------------------

#: Parked for a quota window: the plan is alive, waiting for capacity.
#: It is NOT terminal, so the default list must keep it.
PARKED_ID = "20261024-vp-quota-parked"

#: Parked at the hard limit: the operator has to top the quota up by
#: hand.  Also not terminal — it is the same shape as the parked plan
#: with a different reason, and it must stay in the list too.
TOPUP_ID = "20261024-vp-quota-topup"

#: A normal plan at the manual gate.  It is the negative control for
#: "the parked plans are in the list because they are alive" — if this
#: one vanished, the list would be broken for everybody, not just for
#: the parked plans.
NORMAL_ID = "20261024-vp-quota-normal"

#: A plan that really failed.  ``failed`` IS terminal, so the default
#: list must not show it.
CRASH_ID = "20261024-vp-quota-crash"

#: The phase that must never be treated as terminal: the plan is parked,
#: not dead.  The list filters on this value, so it is the whole
#: criterion in one string.
PARKED_PHASE = "waiting_for_quota"

#: The terminal phase the default list filters on.
FAILED_PHASE = "failed"

#: The response header that carries the list-wide degradation verdict.
DEGRADED_HEADER = "X-PDT-Plans-List-Degraded"

#: The per-row marker, for a consumer that reads the body and not the
#: headers.
DEGRADED_KEY = "list_degraded"

#: The reason the listing gives for having degraded.
DEGRADED_REASON = "state_db_unavailable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_ids(plans: Iterable[Dict[str, Any]]) -> List[str]:
    """The ids of a rendered listing, in the order the endpoint gave them."""
    return [
        str(row.get("id"))
        for row in plans
        if isinstance(row, dict)
    ]


def _row_for(plans: Iterable[Dict[str, Any]], plan_id: str) -> Optional[Dict[str, Any]]:
    """The rendered row of one plan, or ``None`` when it is not listed."""
    for row in plans:
        if isinstance(row, dict) and row.get("id") == plan_id:
            return row
    return None


def _data_source_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """The data-source block a row carries.

    The block is the answer's provenance: where the rows came from
    (``kind``) and WHICH data plane produced them (the database by path
    and by inode, the process that answered, how many rows it found).
    A row that carries no block cannot be told apart from a row of
    another instance's answer, so the shape is asserted rather than
    assumed.
    """
    source = row.get("data_source")
    assert isinstance(source, dict), (
        f"a row must carry the data source of the listing it came from; "
        f"got {source!r}"
    )
    return source


def _write_plan_dir(plans_dir: Path, plan_id: str) -> Path:
    """Create the on-disk half of a plan: its directory and its tasks.

    ``tasks.json`` is what makes the plan read as a plan at all (the
    listing derives ``status`` from which documents exist on disk), and
    the directory is what the degraded listing falls back to.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "tasks": [
                    {"id": "1", "title": "implement capacity wait"},
                    {"id": "2", "title": "wire plan state"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir


def _seed_state_db(
    db_path: Path,
    rows: Tuple[Tuple[str, str, str], ...],
) -> None:
    """Write the routing + execution rows of the fixture plans.

    Every row goes through the repository production writes through, so
    the endpoint reads back exactly the shapes production produces.

    Each row is ``(plan_id, routing phase, execution status)``.  The
    routing phase is the authority the listing filters on (schema v6:
    ``plan_routing.current_phase``); the execution status is the row's
    own verdict about the run.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(db_path)
    try:
        migrate(conn)
        routing = RoutingRepository(conn)
        execution = ExecutionRepository(conn)
        for plan_id, phase, exec_status in rows:
            routing.insert(plan_id, phase)
            execution.insert(
                plan_id=plan_id,
                exec_status=exec_status,
                project_dir=str(db_path.parent / "project" / plan_id),
            )
    finally:
        conn.close()


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot.  Skipping it is deliberate: the listing has to be readable
    **from the durable rows alone**, so a plan that the process happens
    to hold in memory cannot decide what the answer contains.
    """
    server._execution_state.clear()
    server.invalidate_plans_cache()
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# The fixture: this test's own plans tree and state database
# ---------------------------------------------------------------------------


@pytest.fixture
def quota_plans(tmp_path: Path) -> Dict[str, Any]:
    """The plans tree of this test, with a readable state database.

    The four plans are the ones the PRD verification seeds: two parked
    for quota, one normal, one failed.  The failed one is seeded in the
    state the crash recovery leaves it in — the routing phase says
    ``failed`` — because the listing filters on the routing phase, and
    that is the state the live service reaches.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    for plan_id in (PARKED_ID, TOPUP_ID, NORMAL_ID, CRASH_ID):
        _write_plan_dir(plans_dir, plan_id)

    _seed_state_db(
        server._state_db_path(),
        (
            (PARKED_ID, PARKED_PHASE, PARKED_PHASE),
            (TOPUP_ID, PARKED_PHASE, PARKED_PHASE),
            (NORMAL_ID, "ready", "not_started"),
            (CRASH_ID, FAILED_PHASE, FAILED_PHASE),
        ),
    )

    return {"plans_dir": plans_dir, "db_path": server._state_db_path()}


# ---------------------------------------------------------------------------
# The default view: parked stays, failed goes
# ---------------------------------------------------------------------------


def test_default_list_keeps_waiting_and_hides_failed(quota_plans: Dict[str, Any]) -> None:
    """The default list keeps a parked plan and drops a failed one.

    Both halves of the criterion, asserted on the same answer:

      * the two parked plans are in the list — ``waiting_for_quota`` is
        not a terminal phase, and a plan that is only waiting for
        capacity is still alive;
      * the failed plan is not — ``failed`` IS terminal, and a plan
        nobody is going to resume does not belong in the sidebar.

    The normal plan is the control that keeps the first half honest: if
    it were missing too, the list would be broken for everybody and the
    parked plans would be in it for the wrong reason.
    """
    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"the plans list must answer; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"the plans list must be a JSON array; got {type(body).__name__}"
    )

    ids = _plan_ids(body)

    assert PARKED_ID in ids, (
        f"a plan parked for quota is not in a terminal state, so it must "
        f"stay in the list; got {ids!r}"
    )
    assert TOPUP_ID in ids, (
        f"a plan parked at the hard quota limit is not in a terminal "
        f"state either; got {ids!r}"
    )
    assert NORMAL_ID in ids, (
        f"a normal plan at the manual gate must stay in the list; got "
        f"{ids!r}"
    )
    assert CRASH_ID not in ids, (
        f"a plan that failed is in a terminal state and must be filtered "
        f"out of the default list; got {ids!r}"
    )

    parked = _row_for(body, PARKED_ID)
    assert parked is not None, f"the parked plan vanished from the list: {ids!r}"
    assert parked.get("current_phase") == PARKED_PHASE, (
        f"the parked plan must report the phase it is parked in; got "
        f"{parked.get('current_phase')!r}"
    )

    # -- the listing is NOT degraded -------------------------------------
    # The marker below only means something if it is absent when the
    # listing could read the phases.  A marker that is always on pins
    # nothing.
    assert DEGRADED_HEADER not in resp.headers, (
        f"a listing that read the phases must not be marked degraded; got "
        f"{DEGRADED_HEADER}={resp.headers.get(DEGRADED_HEADER)!r}"
    )
    for row in body:
        assert not row.get(DEGRADED_KEY), (
            f"a listing that read the phases must not mark its rows as "
            f"degraded; got {row!r}"
        )


# ---------------------------------------------------------------------------
# The include_terminal view: the failed plan comes back
# ---------------------------------------------------------------------------


def test_include_terminal_reveals_failed(quota_plans: Dict[str, Any]) -> None:
    """``?include_terminal=true`` brings the failed plan back.

    "Filtered out of the sidebar" and "gone" are different things: the
    failed plan is still on disk, still in the database, and an operator
    who wants to audit or clean it up has to be able to see it.  The
    parked plans stay visible in this view too — they were never
    terminal, so including the terminal ones changes nothing for them.
    """
    client = _client()
    resp = client.get("/api/plans?include_terminal=true")

    assert resp.status_code == 200, (
        f"the plans list must answer; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"the plans list must be a JSON array; got {type(body).__name__}"
    )

    ids = _plan_ids(body)

    assert CRASH_ID in ids, (
        f"a failed plan must be visible when the caller asks for the "
        f"terminal ones; got {ids!r}"
    )
    assert PARKED_ID in ids, (
        f"the parked plan must not disappear from the unfiltered list; got "
        f"{ids!r}"
    )
    assert NORMAL_ID in ids, (
        f"the normal plan must not disappear from the unfiltered list; got "
        f"{ids!r}"
    )

    crash = _row_for(body, CRASH_ID)
    assert crash is not None, f"the failed plan is not in the list: {ids!r}"
    assert crash.get("current_phase") == FAILED_PHASE, (
        f"the failed plan must report the phase it failed in; got "
        f"{crash.get('current_phase')!r}"
    )


# ---------------------------------------------------------------------------
# The degradation gap: a listing that could not read the phases
# ---------------------------------------------------------------------------


def test_list_fallback_is_marked_degraded(tmp_path: Path) -> None:
    """A listing that fell back to the directory scan must say so.

    The state database is not there, so no phase can be read and the
    terminal-phase filter cannot be applied.  The listing then answers
    200 with every plan it can see — including the failed one, which the
    default view is supposed to hide.

    That is not a bug by itself; it is the price of a listing that
    cannot see the phases.  It IS a bug if the answer does not say so,
    because "200 + full list" is exactly what a reader takes for
    "the terminal plans were filtered out".

    So the degraded listing must announce itself, both on the response
    (the header) and on its rows (the marker) — and the healthy listing
    of the very same plans must not carry either.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for plan_id in (PARKED_ID, TOPUP_ID, NORMAL_ID, CRASH_ID):
        _write_plan_dir(plans_dir, plan_id)

    # No state database: ``_open_state_machine`` cannot open one that is
    # not there, so the listing degrades to the directory scan.
    assert not server._state_db_path().exists(), (
        "this test is about the degraded listing; the state database must "
        "not exist"
    )

    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"a degraded listing still answers; got {resp.status_code}: "
        f"{resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"the plans list must be a JSON array; got {type(body).__name__}"
    )

    ids = _plan_ids(body)
    assert PARKED_ID in ids, (
        f"the degraded listing still lists the plans it can see; got {ids!r}"
    )
    assert CRASH_ID in ids, (
        f"this is the whole point of the marker: the degraded listing "
        f"cannot filter the failed plan out, so the failed plan is in the "
        f"list; got {ids!r}"
    )

    # -- the response says it degraded ------------------------------------
    assert resp.headers.get(DEGRADED_HEADER) == DEGRADED_REASON, (
        f"a listing that could not read the phases must say so in the "
        f"response; got {DEGRADED_HEADER}={resp.headers.get(DEGRADED_HEADER)!r}"
    )

    # -- and so does every row -------------------------------------------
    for row in body:
        assert row.get(DEGRADED_KEY) is True, (
            f"every row of a degraded listing must carry the degradation "
            f"marker; got {row!r}"
        )
        assert row.get(f"{DEGRADED_KEY}_reason") == DEGRADED_REASON, (
            f"every row of a degraded listing must say why it degraded; got "
            f"{row!r}"
        )


def test_healthy_listing_is_not_marked_degraded(quota_plans: Dict[str, Any]) -> None:
    """The negative control for the degradation marker.

    Same plans, but the state database IS there, so the listing reads
    the phases and filters the terminal ones.  A marker that is always
    on would make the degraded case pass while telling the operator
    nothing — so the healthy listing must carry neither the header nor
    the row marker.
    """
    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"the plans list must answer; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert resp.headers.get(DEGRADED_HEADER) is None, (
        f"a listing that read the phases must not be marked degraded; got "
        f"{DEGRADED_HEADER}={resp.headers.get(DEGRADED_HEADER)!r}"
    )
    for row in body:
        assert not row.get(DEGRADED_KEY), (
            f"a listing that read the phases must not mark its rows as "
            f"degraded; got {row!r}"
        )


# ---------------------------------------------------------------------------
# VP-010: the listing says which tree it read, and the persisted
# execution verdict is part of the terminal decision
# ---------------------------------------------------------------------------
#
# The previous round pinned the LIST BEHAVIOUR — a parked plan stays, a
# failed plan goes — but left two holes behind it:
#
#   * the answer said nothing about WHERE it came from, so an empty
#     list from a process pointed at another worktree's plans root was
#     the same shape as "this instance really has no plans";
#   * the terminal decision read the routing phase alone, so a plan
#     whose durable execution row says ``failed`` — the verdict the
#     crash path persists — stayed in the list as long as its workflow
#     phase had not caught up with the run.
#
# The contract this section freezes:
#
#   * the listing names its data source (``state_db`` when the phases
#     came from the state database, ``directory`` when the listing
#     degraded to a directory scan) and the plans root it read;
#   * the persisted execution verdict is part of the terminal decision:
#     ``failed`` / ``stopped`` end a plan even when the routing phase
#     does not say so;
#   * ``waiting_for_quota`` is not a terminal phase — a plan parked
#     for capacity is alive.

#: The response header carrying the listing's data source.
DATA_SOURCE_HEADER = "X-PDT-Plans-Data-Source"

#: The response header carrying the plans root the listing read.
PLANS_DIR_HEADER = "X-PDT-Plans-Dir"

#: A listing that read the state database: the phases are authoritative
#: and the terminal filter was applied.
SOURCE_STATE_DB = "state_db"

#: A listing that degraded to a directory scan: no phase could be read
#: and no terminal filter could be applied.
SOURCE_DIRECTORY = "directory"

#: A plan whose workflow phase never caught up with its run: the routing
#: row says ``ready``, the durable execution row says ``failed``.  Only
#: the execution row knows the run is over.
EXEC_FAILED_ID = "20261024-vp-quota-exec-failed"


@pytest.fixture
def vp_010_plans(tmp_path: Path) -> Dict[str, Any]:
    """The plans tree of this test: two parked, one normal, one failed.

    The failed plan is the interesting one: its routing phase says
    ``ready`` — the workflow never advanced past the gate — while its
    durable execution row says ``failed``.  A listing that filters on
    the routing phase alone keeps it; a listing that reads the
    persisted verdict as well hides it.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    for plan_id in (PARKED_ID, TOPUP_ID, NORMAL_ID, EXEC_FAILED_ID):
        _write_plan_dir(plans_dir, plan_id)

    _seed_state_db(
        server._state_db_path(),
        (
            (PARKED_ID, PARKED_PHASE, PARKED_PHASE),
            (TOPUP_ID, PARKED_PHASE, PARKED_PHASE),
            (NORMAL_ID, "ready", "not_started"),
            (EXEC_FAILED_ID, "ready", "failed"),
        ),
    )

    return {"plans_dir": plans_dir, "db_path": server._state_db_path()}


def test_vp_010_plans_list_keeps_waiting_and_hides_failed(
    vp_010_plans: Dict[str, Any],
) -> None:
    """The parked plans stay, the failed plan goes, and the answer says where it came from.

    Three things, asserted on the same answer:

      * the two parked plans are in the list — ``waiting_for_quota`` is
        a pause, not an end, and a plan that is only waiting for
        capacity is still alive;
      * the failed plan is not — and it is failed by its DURABLE
        EXECUTION ROW, not by its routing phase, whose workflow never
        caught up with the run;
      * the listing names the tree it read, so an empty answer from
        another instance's plans root cannot be mistaken for "there
        are no plans here".
    """
    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"the plans list must answer; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"the plans list must be a JSON array; got {type(body).__name__}"
    )

    ids = _plan_ids(body)

    assert PARKED_ID in ids, (
        f"a plan parked for quota is not in a terminal state, so it must "
        f"stay in the list; got {ids!r}"
    )
    assert TOPUP_ID in ids, (
        f"a plan parked at the hard quota limit is not in a terminal "
        f"state either; got {ids!r}"
    )
    assert NORMAL_ID in ids, (
        f"a normal plan at the manual gate must stay in the list; got "
        f"{ids!r}"
    )
    assert EXEC_FAILED_ID not in ids, (
        f"a plan whose durable execution row says the run failed is over, "
        f"whatever its workflow phase says; got {ids!r}"
    )

    # -- the listing says which tree it read -----------------------------
    # Without this, the answer of a process pointed at another
    # worktree's plans root is the same shape as the answer of an
    # instance that really has no plans.
    assert resp.headers.get(DATA_SOURCE_HEADER) == SOURCE_STATE_DB, (
        f"the listing must say which data source produced it; got "
        f"{DATA_SOURCE_HEADER}={resp.headers.get(DATA_SOURCE_HEADER)!r}"
    )
    assert resp.headers.get(PLANS_DIR_HEADER) == str(vp_010_plans["plans_dir"]), (
        f"the listing must say which plans root it read; got "
        f"{PLANS_DIR_HEADER}={resp.headers.get(PLANS_DIR_HEADER)!r}"
    )

    parked = _row_for(body, PARKED_ID)
    assert parked is not None, f"the parked plan vanished from the list: {ids!r}"
    assert _data_source_of(parked).get("kind") == SOURCE_STATE_DB, (
        f"a row must carry the data source of the listing it came from; "
        f"got {parked.get('data_source')!r}"
    )
    assert parked.get("plans_dir") == str(vp_010_plans["plans_dir"]), (
        f"a row must carry the plans root of the listing it came from; "
        f"got {parked.get('plans_dir')!r}"
    )


def test_vp_010_empty_listing_still_names_the_tree_it_read(
    tmp_path: Path,
) -> None:
    """An EMPTY listing still says which tree it read.

    This is the case the provenance exists for: an empty array is what
    "no plans here" looks like, and it is also what "another instance's
    plans root" looks like.  Without the provenance the two cannot be
    told apart — and the empty listing is the one answer that carries
    no row to carry it.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    # A state database with no rows: the listing read the phases (there
    # were none) and the answer is empty for that reason alone.
    _seed_state_db(server._state_db_path(), ())

    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"the plans list must answer; got {resp.status_code}: {resp.text!r}"
    )
    assert resp.json() == [], (
        f"there are no plans in this tree; got {resp.json()!r}"
    )
    assert resp.headers.get(DATA_SOURCE_HEADER) == SOURCE_STATE_DB, (
        f"an empty listing must still say which data source produced it; "
        f"got {DATA_SOURCE_HEADER}={resp.headers.get(DATA_SOURCE_HEADER)!r}"
    )
    assert resp.headers.get(PLANS_DIR_HEADER) == str(plans_dir), (
        f"an empty listing must still say which plans root it read; got "
        f"{PLANS_DIR_HEADER}={resp.headers.get(PLANS_DIR_HEADER)!r}"
    )

    # -- and the cached answer says the same thing ----------------------
    # The second call is served from the plans cache, and an empty
    # cached payload has no row to carry the provenance — so the answer
    # must still name the tree, or the cache would turn a readable
    # answer into an unreadable one.
    again = client.get("/api/plans")
    assert again.status_code == 200, (
        f"the cached plans list must answer; got {again.status_code}: "
        f"{again.text!r}"
    )
    assert again.json() == [], (
        f"the cached plans list must still be empty; got {again.json()!r}"
    )
    assert again.headers.get(DATA_SOURCE_HEADER) == SOURCE_STATE_DB, (
        f"a cached listing must still say which data source produced it; "
        f"got {DATA_SOURCE_HEADER}={again.headers.get(DATA_SOURCE_HEADER)!r}"
    )
    assert again.headers.get(PLANS_DIR_HEADER) == str(plans_dir), (
        f"a cached listing must still say which plans root it read; got "
        f"{PLANS_DIR_HEADER}={again.headers.get(PLANS_DIR_HEADER)!r}"
    )


def test_vp_010_degraded_listing_names_the_directory_it_scanned(
    tmp_path: Path,
) -> None:
    """A degraded listing says it read a directory, not the state database.

    The degraded listing has no phases to filter on, so it answers with
    every plan it can see.  It must also say so: which data source it
    came from (``directory``), and which root it scanned.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for plan_id in (PARKED_ID, NORMAL_ID):
        _write_plan_dir(plans_dir, plan_id)

    # No state database: the listing degrades to the directory scan.
    assert not server._state_db_path().exists(), (
        "this test is about the degraded listing; the state database must "
        "not exist"
    )

    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"a degraded listing still answers; got {resp.status_code}: "
        f"{resp.text!r}"
    )
    body = resp.json()

    assert resp.headers.get(DATA_SOURCE_HEADER) == SOURCE_DIRECTORY, (
        f"a listing that fell back to the directory scan must say so; got "
        f"{DATA_SOURCE_HEADER}={resp.headers.get(DATA_SOURCE_HEADER)!r}"
    )
    assert resp.headers.get(PLANS_DIR_HEADER) == str(plans_dir), (
        f"a degraded listing must say which root it scanned; got "
        f"{PLANS_DIR_HEADER}={resp.headers.get(PLANS_DIR_HEADER)!r}"
    )
    for row in body:
        assert _data_source_of(row).get("kind") == SOURCE_DIRECTORY, (
            f"a row of a degraded listing must carry the degraded data "
            f"source; got {row!r}"
        )
        assert row.get("plans_dir") == str(plans_dir), (
            f"a row of a degraded listing must carry the scanned root; "
            f"got {row!r}"
        )
        assert row.get("degraded") is True, (
            f"a row of a degraded listing must carry the degradation "
            f"marker; got {row!r}"
        )


# ---------------------------------------------------------------------------
# The provenance the listing echoes
# ---------------------------------------------------------------------------
#
# The acceptance command of this task names
# ``test_list_echoes_data_source_and_plans_dir``: the listing must say
# WHICH data source produced it and WHICH plans root it read.  The
# behaviour is the one ``test_vp_010_plans_list_keeps_waiting_and_hides_failed``
# touches in passing while it checks the filtering; this test is about
# the provenance alone, so a broken provenance is caught even when the
# filtering is right — and, unlike that sibling, it also checks the
# CACHED answer, which is the path that can lose the provenance (a
# cached payload carries no fresh rendering to read it from).


def test_list_echoes_data_source_and_plans_dir(
    vp_010_plans: Dict[str, Any],
) -> None:
    """The listing names its data source and the plans root it read.

    A list without provenance is ambiguous in the one way that matters
    here: an empty answer from an instance pointed at another
    worktree's plans root is the same shape as "this instance really has
    no plans", and a full answer from the wrong root looks like a
    correct one.  So the answer says where it came from — on the
    response (the headers) and on every row (the fields), because a
    reader may read either.
    """
    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"the plans list must answer; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"the plans list must be a JSON array; got {type(body).__name__}"
    )
    assert body, (
        "this fixture has plans; an empty listing would say nothing about "
        "the rows' provenance"
    )

    plans_dir = str(vp_010_plans["plans_dir"])

    # -- the response says where it came from ----------------------------
    assert resp.headers.get(DATA_SOURCE_HEADER) == SOURCE_STATE_DB, (
        f"the listing must say which data source produced it; got "
        f"{DATA_SOURCE_HEADER}={resp.headers.get(DATA_SOURCE_HEADER)!r}"
    )
    assert resp.headers.get(PLANS_DIR_HEADER) == plans_dir, (
        f"the listing must say which plans root it read; got "
        f"{PLANS_DIR_HEADER}={resp.headers.get(PLANS_DIR_HEADER)!r}"
    )

    # -- and so does every row -------------------------------------------
    for row in body:
        assert _data_source_of(row).get("kind") == SOURCE_STATE_DB, (
            f"a row must carry the data source of the listing it came "
            f"from; got {row!r}"
        )
        assert row.get("plans_dir") == plans_dir, (
            f"a row must carry the plans root of the listing it came "
            f"from; got {row!r}"
        )

    # -- and the CACHED answer says it too -------------------------------
    # The second call is served from the plans cache.  A cached payload
    # carries no fresh rendering to read the provenance from, so this is
    # the path where the answer can silently lose it — and a listing
    # that names no tree cannot be told apart from one that read the
    # wrong tree.
    again = client.get("/api/plans")
    assert again.status_code == 200, (
        f"the cached plans list must answer; got {again.status_code}: "
        f"{again.text!r}"
    )
    cached_body = again.json()
    assert isinstance(cached_body, list), (
        f"the cached plans list must be a JSON array; got "
        f"{type(cached_body).__name__}"
    )
    assert again.headers.get(DATA_SOURCE_HEADER) == SOURCE_STATE_DB, (
        f"a cached listing must still say which data source produced it; "
        f"got {DATA_SOURCE_HEADER}={again.headers.get(DATA_SOURCE_HEADER)!r}"
    )
    assert again.headers.get(PLANS_DIR_HEADER) == plans_dir, (
        f"a cached listing must still say which plans root it read; got "
        f"{PLANS_DIR_HEADER}={again.headers.get(PLANS_DIR_HEADER)!r}"
    )
    for row in cached_body:
        assert _data_source_of(row).get("kind") == SOURCE_STATE_DB, (
            f"a row of a cached listing must carry the data source of the "
            f"listing it came from; got {row!r}"
        )
        assert row.get("plans_dir") == plans_dir, (
            f"a row of a cached listing must carry the plans root of the "
            f"listing it came from; got {row!r}"
        )


# ---------------------------------------------------------------------------
# The round's service outlives the process that started it
# ---------------------------------------------------------------------------
#
# The 2026-10-26 round could not answer its own criterion, and the reason
# it could not is worth writing down: the answer depended on WHICH
# instance answered.
#
# Two rounds in a row produced ``200`` with an EMPTY array from a URL that
# was supposed to hold the round's plans, and the round that judged the
# criterion found three plans at the same URL.  Nothing about the
# criterion changed between those two moments — what changed was the
# instance behind the port.  One of them was serving a data plane that
# does not describe the tree the round seeded, and an empty array is
# exactly what such an instance has to say.
#
# So an empty array is not an answer, it is a coincidence of shape:
# "this instance has no plans" and "this instance is serving somebody
# else's database" look identical on the wire.  The list therefore
#
#   * says which data plane produced it — the database it read (by path
#     AND by inode), the process that answered (pid + boot id) and how
#     many rows it found; and
#   * REFUSES (503) when the tree it serves holds plans that its data
#     plane knows nothing about, instead of answering 200 with an empty
#     array that reads as "there is nothing here".
#
# The service outliving its starter is the situation both facts have to
# survive: the starter wrote the rows and seeded the tree, then went
# away, and the process still serving has to answer from the durable
# rows — and to say that it did.


def test_the_plans_list_stays_answerable_after_the_round_service_outlives_its_starter(
    quota_plans: Dict[str, Any],
) -> None:
    """The list stays answerable — and stays the RIGHT list — after the starter is gone.

    The round's service outlived the process that started the round: the
    starter wrote the rows and seeded the tree, then went away.  The
    process still serving has to answer from what was written down, and
    the answer has to name the data plane it read — otherwise the round
    cannot tell this answer from another instance's answer about the
    same port.

    Three things, asserted on the same answer:

      * the PRD criterion still holds — a plan parked for quota and a
        normal plan are in the list, a plan that failed is not;
      * the answer is made of the DURABLE rows, not of the starter's
        memory: the stale view the starter left behind (a run it called
        "running", under a pid that is long gone) does not change what
        the list says about the phase;
      * the answer names its data plane — the database it read by path
        and by inode, the process that answered, and how many rows it
        found.
    """
    db_path = Path(quota_plans["db_path"])

    client = _client()

    # -- the starter is gone --------------------------------------------
    # What it left behind in memory is stale: the run it owned is
    # parked on quota, and the pid it recorded belongs to a process that
    # is long gone.  A live process still holds that view — it is what
    # the serving instance sees when the process that wrote the rows is
    # gone.
    server._execution_state[PARKED_ID] = {
        "status": "running",
        "pid": 424242,
        "project_dir": str(db_path.parent / "project" / PARKED_ID),
    }

    resp = client.get("/api/plans")

    assert resp.status_code == 200, (
        f"the plans list must stay answerable after the starter is gone; "
        f"got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, list), (
        f"the plans list must be a JSON array; got {type(body).__name__}"
    )

    ids = _plan_ids(body)

    # -- the criterion still holds --------------------------------------
    assert PARKED_ID in ids, (
        f"a plan parked for quota is not in a terminal state, so it must "
        f"stay in the list; got {ids!r}"
    )
    assert TOPUP_ID in ids, (
        f"a plan parked at the hard quota limit is not in a terminal "
        f"state either; got {ids!r}"
    )
    assert NORMAL_ID in ids, (
        f"a normal plan at the manual gate must stay in the list; got "
        f"{ids!r}"
    )
    assert CRASH_ID not in ids, (
        f"a plan that failed is in a terminal state and must be filtered "
        f"out of the default list; got {ids!r}"
    )

    # -- the answer is made of the rows, not of the starter's memory ---
    parked = _row_for(body, PARKED_ID)
    assert parked is not None, f"the parked plan vanished from the list: {ids!r}"
    assert parked.get("current_phase") == PARKED_PHASE, (
        f"the phase must come from the durable routing row, not from the "
        f"starter's stale in-memory view; got {parked.get('current_phase')!r}"
    )

    # -- the answer names the data plane it read ------------------------
    source = _data_source_of(parked)
    assert source.get("kind") == SOURCE_STATE_DB, (
        f"the phases were read from the state database; got {source!r}"
    )
    assert source.get("state_db_path") == str(db_path), (
        f"the answer must name the database it read; got "
        f"{source.get('state_db_path')!r}, expected {str(db_path)!r}"
    )
    assert source.get("state_db_inode") == db_path.stat().st_ino, (
        f"the answer must name the inode of the database it read — a "
        f"different file at the same path is a different data plane; got "
        f"{source.get('state_db_inode')!r}"
    )
    assert source.get("boot_id") == server._INSTANCE_BOOT_ID, (
        f"the answer must name the process that produced it; got "
        f"{source.get('boot_id')!r}"
    )
    assert source.get("pid") == os.getpid(), (
        f"the serving instance is this process; got {source.get('pid')!r}"
    )
    assert source.get("row_count") == 4, (
        f"the answer must say how many rows it found; got "
        f"{source.get('row_count')!r}"
    )

    # -- and the CACHED answer says it too ------------------------------
    # The second poll is served from the cache.  A cached payload
    # carries no fresh rendering to read the provenance from, so this is
    # the path where the answer can silently lose it — and an answer
    # that names no data plane cannot be told apart from another
    # instance's answer about the same port.
    again = client.get("/api/plans")
    assert again.status_code == 200, (
        f"the cached plans list must answer; got {again.status_code}: "
        f"{again.text!r}"
    )
    cached_parked = _row_for(again.json(), PARKED_ID)
    assert cached_parked is not None, (
        f"the cached listing lost the parked plan: "
        f"{_plan_ids(again.json())!r}"
    )
    cached_source = _data_source_of(cached_parked)
    assert cached_source.get("state_db_inode") == db_path.stat().st_ino, (
        f"the cached answer must still name the data plane it read; got "
        f"{cached_source!r}"
    )


def test_the_plans_list_refuses_instead_of_answering_an_empty_array_when_the_data_source_does_not_describe_the_tree(
    tmp_path: Path,
) -> None:
    """A data plane that does not describe the tree is REFUSED, not answered empty.

    This is the shape the round actually hit: the plans tree holds the
    round's plans, and the state database behind the port is another
    instance's — one that knows nothing about them.  The listing built
    from it is empty, and an empty array is what "this instance has no
    plans" looks like.

    So the answer is ``503`` with the data source named.  The refusal
    carries the same facts the successful answer carries (the database
    by path and by inode, the process that answered, how many rows it
    found), so a reader can see WHICH data plane was asked and go and
    look at it.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for plan_id in (PARKED_ID, NORMAL_ID):
        _write_plan_dir(plans_dir, plan_id)

    # The state database EXISTS — so the listing reads its phases — but
    # it holds no row for any of the plans on disk.  That is the other
    # instance's database.
    db_path = server._state_db_path()
    _seed_state_db(db_path, ())
    assert db_path.exists(), (
        "this case is about a state database that holds nothing; the file "
        "must exist"
    )

    client = _client()
    resp = client.get("/api/plans")

    assert resp.status_code == 503, (
        f"a listing whose data source does not describe the tree must be "
        f"refused; got {resp.status_code}: {resp.text!r}"
    )
    body = resp.json()
    assert isinstance(body, dict), (
        f"the refusal must be a JSON object; got {type(body).__name__}"
    )
    source = body.get("data_source")
    assert isinstance(source, dict), (
        f"the refusal must name the data source that could not answer; "
        f"got {source!r}"
    )
    assert source.get("state_db_path") == str(db_path), (
        f"the refusal must name the database that was asked; got {source!r}"
    )
    assert source.get("state_db_inode") == db_path.stat().st_ino, (
        f"the refusal must name the inode of the database that was asked; "
        f"got {source!r}"
    )
    assert source.get("row_count") == 0, (
        f"the refusal must say how many rows that database held; got "
        f"{source!r}"
    )
    assert body.get("plans_dir") == str(plans_dir), (
        f"the refusal must name the tree it could not describe; got {body!r}"
    )
