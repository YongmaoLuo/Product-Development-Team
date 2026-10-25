"""The human-in-the-loop feature's cross-module contract, as one snapshot.

Why this file exists
--------------------
The feature was delivered in pieces, and every piece came with its own
tests: the verdict projection, the loop-tracking file, the decision
table, the three HTTP endpoints, the triage conclusion, the hook
payload, the ``vp_complete`` event, the per-plan hook configuration.
Each of those tests is happy as long as *its own* subject behaves.

What none of them can see is the moment a later piece changes a shape
an earlier piece agreed on — a field added to ``Verdict`` that the
event payload does not carry, a column dropped from ``plan_decisions``
that the store still writes, a route renamed under a UI that already
calls it.  Nothing fails; something just quietly stops arriving.

A contract snapshot is the cheapest detector for that class of drift:
one file that records the **outward** shape of every boundary next to
a literal, so a change on either side of a boundary turns it red and
the PR that did it has to say so in its description.

What is in the snapshot, and what is not
----------------------------------------
Only cross-module boundaries — a shape one module hands to another:

* ``Verdict`` and ``TriageConclusion``, the dataclasses other modules
  read field by field;
* the loop-tracking file, read by the *next* round's fresh orchestrator;
* the ``plan_decisions`` table, written by the store and read by
  everything else;
* the three HTTP endpoints — method, path, success code, conflict code;
* the hook payload, delivered to a destination as it is;
* the ``vp_complete`` payload, read by the loop and the progress
  endpoints;
* ``hitl_config.json``, written by plan creation and read by the
  dispatcher.

What is deliberately **not** in it: internal implementation detail.
How the orchestrator counts, how the store serialises its writes, which
helper a route happens to call — none of that crosses a boundary, and
pinning it would make this file a change-detector for code nobody
outside the module can see.

How the shapes are observed
---------------------------
Every shape below is *derived from the running code* and then compared
with a literal, never transcribed into the literal and asserted against
itself:

* the dataclass field sets come from ``__dataclass_fields__``;
* the tracking file is written by the orchestrator and read back;
* the column set comes from a migrated database's ``PRAGMA``;
* the endpoints are the routes the application actually registered, and
  the codes are the codes real requests came back with;
* the hook payload is what a real destination received on stdin;
* the ``vp_complete`` payload is what the orchestrator's own payload
  builder produced;
* the hook configuration is what the real writer wrote and the real
  reader read back.

A field added or removed anywhere in that list turns this file red.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

# Make the backend modules importable when this file is run from the
# project root (not from ``backend/``).
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_plans_dir  # noqa: E402
from hooks import config as hooks_config  # noqa: E402
from hooks.dispatcher import dispatch  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification.triage_agent import TriageConclusion  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_subagent import Verdict  # noqa: E402


client = TestClient(server.app)

#: The plan the HTTP snapshot asks its questions to.  It has to be a
#: valid plan id (``[A-Za-z0-9._-]``) and one no other test uses.
PLAN_ID = "contract-plan"

#: The question the HTTP snapshot asks.  The text does not matter —
#: what is under test is the shape of the exchange, not the prose.
QUESTION = {
    "question": "Ship the contract snapshot as written?",
    "options": ["accept", "revise"],
    "default_option": "accept",
}

#: The context a question is asked with: the verification point it is
#: about, the class of the failure that raised it, and the round of
#: the verification loop it was raised in.  These are the fields that
#: make a decision findable by the failure it is about.
CONTEXT_FIELDS = {
    "vp_id": "VP-018",
    "failure_class": "criterion_defect",
    "round": 2,
}

#: The event name the plan's outlet is asked to deliver when a decision
#: is asked.
EVENT = "hitl_decision_created"


# ---------------------------------------------------------------------------
# The shapes, observed
# ---------------------------------------------------------------------------


def _observed_endpoints():
    """The HITL routes the application actually registered.

    ``method`` and ``path`` come from the route table, so a route that
    was renamed, moved or given another method stops matching the
    snapshot.  ``HEAD`` and ``OPTIONS`` are left out: Starlette adds
    them for free and they are not part of anybody's contract.
    """
    registered: dict[str, set] = {}
    for route in server.iter_app_routes():
        path = route.path
        if not path.startswith("/api/hitl/"):
            continue
        methods = {
            method
            for method in (route.methods or set())
            if method not in ("HEAD", "OPTIONS")
        }
        registered.setdefault(path, set()).update(methods)
    return {path: sorted(methods) for path, methods in registered.items()}


def _give_the_plan_an_outlet(plan_id: str = PLAN_ID):
    """Give the plan a destination, so its questions are asked of somebody.

    A question nobody can be asked is ``unroutable`` from the start, and
    an ``unroutable`` decision is settled: answering it is a 409, not
    the 200 the contract promises.  The ``cmd`` destination here always
    succeeds — the delivery runs on its own thread, so it never holds
    the route.
    """
    plan_dir = resolve_plans_dir() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        hooks_config.config_path(plan_dir),
        {
            "version": 1,
            "mode": "auto",
            "timeout_sec": 1800,
            "hooks": [{"kind": "cmd", "command": "true"}],
        },
        reraise=True,
    )


def _observed_endpoint_codes(plan_id: str = PLAN_ID):
    """The status codes the three endpoints really answer with.

    Asked over HTTP, through the mounted application: a route that was
    never registered answers 404, and a route that answers 201 where
    the contract says 200 is a different contract.
    """
    created = client.post(f"/api/hitl/{plan_id}/decisions", json=QUESTION)
    decision_id = ""
    if created.status_code == 200:
        decision_id = created.json().get("decision_id", "")

    listed = client.get(f"/api/hitl/{plan_id}/decisions")

    answered = client.post(
        f"/api/hitl/{plan_id}/decisions/{decision_id}/answer",
        json={"option": "accept", "answered_by": "contract", "note": ""},
    )
    # The first answer wins; a second one is a conflict, never an
    # overwrite.  That 409 is the code the UI branches on.
    conflict = client.post(
        f"/api/hitl/{plan_id}/decisions/{decision_id}/answer",
        json={"option": "accept", "answered_by": "contract", "note": ""},
    )

    return {
        "create": created.status_code,
        "list": listed.status_code,
        "answer": answered.status_code,
        "answer_conflict": conflict.status_code,
    }


def _observed_tracking_payload(tmp_path):
    """The loop-tracking file, as the orchestrator writes it.

    The file is the contract between two orchestrator *instances*: the
    post-repair round builds a fresh one and continues the count from
    the file.  So the shape that matters is the shape on disk, and it
    is observed by letting the orchestrator write it.
    """
    plan_dir = tmp_path / "plans" / "contract-tracking"
    project_dir = tmp_path / "projects" / "contract-tracking"
    plan_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)

    # Only the tracking file matters here; the verification agent and
    # the repair generator would otherwise reach for a real toolchain.
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orchestrator = VerificationOrchestrator(plan_dir, project_dir, Mock())

    orchestrator._previous_failed_keys = {("VP-007", "criterion_defect")}
    orchestrator._same_failure_counts = {"VP-007|criterion_defect": 2}
    orchestrator._last_round = 4
    orchestrator._save_loop_tracking()

    path = orchestrator._loop_tracking_path()
    return json.loads(path.read_text(encoding="utf-8"))


def _observed_plan_decision_columns(tmp_path):
    """The columns of ``plan_decisions``, as the schema makes them.

    Read out of a migrated database with ``PRAGMA`` — the table the
    store writes and every reader opens.
    """
    conn = open_db(tmp_path / "contract-state.db")
    try:
        migrate(conn)
        rows = conn.execute("PRAGMA table_info(plan_decisions)").fetchall()
    finally:
        conn.close()
    return [row[1] for row in rows]


def _observed_hook_body(tmp_path, event: str, payload: dict):
    """The body a hook destination really receives.

    Delivered for real: the plan's configuration names a ``cmd``
    destination, the dispatcher runs it and writes the body to its
    stdin, and the body is read back off the file.  A destination that
    learned the event's name from anywhere else would be reading a
    different contract, which is exactly what this catches.
    """
    plan_dir = tmp_path / "plans" / "contract-hooks"
    plan_dir.mkdir(parents=True)
    received = tmp_path / "hook-body.json"

    receiver = tmp_path / "receive.py"
    receiver.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text("
        "sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )

    command = " ".join(
        shlex.quote(part)
        for part in (sys.executable, str(receiver), str(received))
    )
    hooks_config.config_path(plan_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 1800,
                "hooks": [
                    {"kind": "cmd", "command": command, "timeout_sec": 30}
                ],
            }
        ),
        encoding="utf-8",
    )

    report = dispatch(event, payload, plan_dir)
    assert report.delivered == 1, (
        f"the destination was not delivered to ({report!r}); the payload "
        "was not observed"
    )
    return json.loads(received.read_text(encoding="utf-8"))


def _observed_vp_complete_payload():
    """The ``vp_complete`` payload, from the one builder that makes it.

    The event log is what the loop, the progress endpoints and the
    notifier read, so the payload has exactly one definition — and this
    is that definition, asked for a verdict that failed.
    """
    verdict = Verdict(
        verdict="FAILED",
        reasons=["the acceptance test did not run"],
        evidence=["pytest: no tests ran"],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="medium",
        failure_class="deliverable_defect",
        failure_class_source="defaulted",
        evidence_quote="no tests ran",
    )
    return Orchestrator._vp_complete_payload("VP-007", 2, verdict)


def _observed_hitl_config(tmp_path):
    """The hook configuration, written by the writer and read by the reader.

    Two key sets, because there are two shapes: the document that lands
    on disk (which carries the provenance) and the effective
    configuration the reader hands out (which does not).
    """
    plan_dir = tmp_path / "plans" / "contract-config"
    plan_dir.mkdir(parents=True)

    document = hooks_config.write_for_plan(
        plan_dir,
        {
            "source": "plan_creation",
            "session_id": "session-1",
            "destination": "grp-1",
        },
    )
    effective = hooks_config.load_for_plan(plan_dir)
    return {
        "document_keys": set(document),
        "effective_keys": set(effective),
    }


# ---------------------------------------------------------------------------
# 1 — the verdict-shaped dataclasses
# ---------------------------------------------------------------------------


def test_verdict_contract():
    """The field sets of ``Verdict`` and ``TriageConclusion``.

    ``Verdict`` is read field by field by the event payload, the
    result projection and the loop; ``TriageConclusion`` is read by the
    caller that decides whether a failure is actionable.  A field added
    to either is a shape the other side has not agreed to carry, and a
    field removed from either is a field somebody is still reading.
    """
    snapshot = {
        "verdict_fields": set(Verdict.__dataclass_fields__),
        "triage_conclusion_fields": set(TriageConclusion.__dataclass_fields__),
    }

    assert snapshot["verdict_fields"] == {
        "verdict",
        "reasons",
        "evidence",
        "provider",
        "chosen_model",
        "model_complexity",
        "failure_class",
        "failure_class_source",
        "evidence_quote",
    }, (
        "the Verdict field set changed; the verdict projection, the "
        "vp_complete payload and the loop all read it field by field"
    )

    assert snapshot["triage_conclusion_fields"] == {
        "vp_id",
        "failure_class",
        "conclusive",
        "evidence_quote",
        "rationale",
        "replacement",
        # 2026-10-23 (VP-019): the distinction between a triage that
        # *ran* and could not characterise the failure and a triage that
        # *malfunctioned* is carried by one field. The caller reads it
        # field by field, so it belongs in the snapshot.
        "available",
    }, (
        "the TriageConclusion field set changed; the triage caller reads "
        "it field by field"
    )


# ---------------------------------------------------------------------------
# 2 — the loop-tracking file and the decision table
# ---------------------------------------------------------------------------


def test_tracking_and_db_contract(tmp_path):
    """The v2 tracking file's key set and the ``plan_decisions`` columns.

    The tracking file is read by the *next* round's fresh orchestrator,
    and the table is written by the store and read by every other
    consumer.  Both are boundaries between two instances of the
    feature, so both are pinned by what is on disk.
    """
    tracking = _observed_tracking_payload(tmp_path)
    snapshot = {
        "tracking_version": tracking.get("version"),
        "tracking_keys": set(tracking),
        "plan_decisions_columns": _observed_plan_decision_columns(tmp_path),
    }

    assert snapshot["tracking_version"] == 2, (
        "the loop-tracking file changed its version; v2 counts failures "
        "per (point, failure class) and v1 did not"
    )

    assert snapshot["tracking_keys"] == {
        "version",
        "previous_failed_keys",
        "same_failure_counts",
        "last_round",
    }, (
        "the loop-tracking file changed its key set; the next round's "
        "orchestrator reads exactly these keys"
    )

    assert snapshot["plan_decisions_columns"] == [
        "decision_id",
        "plan_id",
        "source",
        "task_id",
        "question",
        "options",
        "default_option",
        "mode",
        "timeout_sec",
        "status",
        "answer",
        "answered_by",
        "answered_at",
        "deadline_at",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "audit_seq",
        # The four context columns, appended: what the question is about
        # (the verification point, the failure class, the round) and the
        # note that travels with the decision.  They are appended rather
        # than interleaved so that a table created by the DDL and one
        # upgraded in place carry the same column order.
        "vp_id",
        "failure_class",
        "round",
        "answer_note",
    ], (
        "the plan_decisions column set changed; the store writes every "
        "one of them by name"
    )


# ---------------------------------------------------------------------------
# 3 — the HTTP endpoints and the payload shapes
# ---------------------------------------------------------------------------


def test_http_and_payload_contract(tmp_path):
    """The three endpoints, the hook payload, the event payload, the config.

    Four boundaries, one file: what a client may call, what a hook
    destination receives, what the event log records, and what the plan
    configuration holds.
    """
    _give_the_plan_an_outlet()
    codes = _observed_endpoint_codes()
    snapshot = {
        "routes": _observed_endpoints(),
        "endpoints": {
            "create": {
                "method": "POST",
                "path": "/api/hitl/{plan_id}/decisions",
                "success": codes["create"],
                "conflict": None,
            },
            "list": {
                "method": "GET",
                "path": "/api/hitl/{plan_id}/decisions",
                "success": codes["list"],
                "conflict": None,
            },
            "answer": {
                "method": "POST",
                "path": "/api/hitl/{plan_id}/decisions/{decision_id}/answer",
                "success": codes["answer"],
                "conflict": codes["answer_conflict"],
            },
        },
        "hook_payload": _observed_hook_body(
            tmp_path,
            EVENT,
            {"plan_id": PLAN_ID, "decision_id": "d-1"},
        ),
        "vp_complete": _observed_vp_complete_payload(),
        "hitl_config": _observed_hitl_config(tmp_path),
    }

    assert snapshot["routes"] == {
        "/api/hitl/{plan_id}/decisions": ["GET", "POST"],
        "/api/hitl/{plan_id}/decisions/{decision_id}/answer": ["POST"],
    }, (
        "the HITL routes changed their method or their path; the UI and "
        "every other client call them by name"
    )

    assert snapshot["endpoints"] == {
        "create": {
            "method": "POST",
            "path": "/api/hitl/{plan_id}/decisions",
            "success": 200,
            "conflict": None,
        },
        "list": {
            "method": "GET",
            "path": "/api/hitl/{plan_id}/decisions",
            "success": 200,
            "conflict": None,
        },
        "answer": {
            "method": "POST",
            "path": "/api/hitl/{plan_id}/decisions/{decision_id}/answer",
            "success": 200,
            "conflict": 409,
        },
    }, (
        "the endpoints changed their success or conflict code; a second "
        "answer is a 409 conflict, never an overwrite"
    )

    assert set(snapshot["hook_payload"]) == {
        "event",
        "plan_id",
        "decision_id",
    }, (
        "the hook payload changed its key set; the destination receives "
        "the event name at the top level next to the payload's own fields"
    )
    assert snapshot["hook_payload"]["event"] == EVENT, (
        "the hook payload lost its event name; a destination that learned "
        "the event from anywhere else would be reading another contract"
    )

    assert set(snapshot["vp_complete"]) == {
        "vp_id",
        "round",
        "status",
        "failure_class",
        "failure_class_source",
        "evidence_quote",
        "actual_result",
    }, (
        "the vp_complete payload changed its key set; the loop, the "
        "progress endpoints and the notifier read it by name"
    )

    assert snapshot["hitl_config"]["document_keys"] == {
        "version",
        "mode",
        "timeout_sec",
        "captured_at",
        "source",
        "hooks",
    }, (
        "the hitl_config document changed its top-level keys; the writer "
        "and the reader agree on them"
    )
    assert snapshot["hitl_config"]["effective_keys"] == {
        "version",
        "mode",
        "timeout_sec",
        "hooks",
    }, (
        "the effective hitl configuration changed its top-level keys; "
        "provenance is written but is not part of the answer"
    )


# ---------------------------------------------------------------------------
# 4 — the context a question is asked with
# ---------------------------------------------------------------------------


def _observed_context_fields(plan_id: str = PLAN_ID):
    """The context fields, as they travel from the request to the row.

    Asked over HTTP and read back from the list endpoint, so the whole
    way is observed: the request body names the verification point, the
    failure class and the round, and the decision that comes back
    carries the same three.  A layer that dropped one of them would
    leave a decision nobody can find by the failure it is about.
    """
    payload = dict(QUESTION)
    payload.update(CONTEXT_FIELDS)
    created = client.post(f"/api/hitl/{plan_id}/decisions", json=payload)
    assert created.status_code == 200, (
        f"the create endpoint answered {created.status_code}; the context "
        "fields cannot be observed through a request that was refused"
    )
    body = created.json()
    decision_id = body.get("decision_id", "")

    listed = client.get(f"/api/hitl/{plan_id}/decisions").json()
    stored = next(
        (item for item in listed if item.get("decision_id") == decision_id),
        {},
    )
    return {
        "created": {name: body.get(name) for name in CONTEXT_FIELDS},
        "listed": {name: stored.get(name) for name in CONTEXT_FIELDS},
    }


def test_the_context_fields_reach_the_decision():
    """The verification point, the failure class and the round survive.

    The three name *what the question is about*.  They are handed to
    the create endpoint and have to come back out of it — the row is
    what an audit reader searches, and a decision that lost its
    context on the way there is a decision about nothing in
    particular.
    """
    snapshot = _observed_context_fields()

    assert snapshot["created"] == CONTEXT_FIELDS, (
        "the create endpoint did not hand back the context it was given; "
        f"got {snapshot['created']!r}"
    )
    assert snapshot["listed"] == CONTEXT_FIELDS, (
        "the stored decision did not keep the context it was asked with; "
        f"got {snapshot['listed']!r}"
    )
