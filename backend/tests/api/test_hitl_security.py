"""Security regression suite for the HITL HTTP surface and its hook fan-out.

What is pinned here
-------------------
The human-in-the-loop surface is the first one in the backend that
takes **attacker-shaped text** on purpose: a question is a piece of
free text a caller sends over HTTP, and a plan's ``hooks`` array is a
set of destinations a plan's own configuration names.  Four risks
follow from that, and this module pins one test per risk.

1. **The request guard covers all three doors** — ``test_guard_matrix``.
   The guard is middleware over ``/api/``, so a HITL route added
   outside its reach would answer a browser page that must not reach
   it.  All three endpoints are checked against all three ways a
   request can fail the guard: no ``X-PDT-Request`` header, a foreign
   ``Origin``, and a foreign ``Host``.  Nine cases, nine 403s.

2. **A ``plan_id`` cannot walk out of the plans root** —
   ``test_path_traversal_zero_write``.  The plan id is a path
   parameter that becomes a filesystem leaf (``plans/{plan_id}``), so
   it is the classic traversal carrier.  The test does not only check
   the status code: it snapshots the tree **outside** the plans root
   before and after a battery of traversal ids and asserts the two
   snapshots are identical — zero difference, not "nothing important
   changed".

3. **The payload never becomes a command** — ``test_cmd_injection_sentinel``.
   A decision's text is whatever a caller wrote, and the ``cmd``
   transport runs a command from the plan's configuration.  The only
   defence that holds is structural: the payload is handed to the
   process on **stdin**, never interpolated into the command string.
   The test asserts both halves — the sentinel the injected text would
   have created does not exist, and the bytes the command received are
   the payload verbatim.

4. **The audit trail does not carry a credential** — ``test_redaction``.
   A webhook URL routinely carries a token in its query string, and
   the plan directory is not the place for that token.  The dispatch audit
   records the target in redacted form; the decision audit mirror
   records a fixed whitelist of fields and never the question text.
   Neither may contain the credential in the clear.

The routing contract a broken hook must not break
--------------------------------------------------
A plan's hooks are a fan-out: one broken destination does not take the
others down with it.  ``test_one_bad_hook_does_not_take_the_question_down``
pins that a question somebody *did* receive stays open, and
``test_all_bad_hooks_leave_the_question_unanswered`` pins that a
routing failure is never dressed up as an answer — nobody was asked,
so nothing may claim that a human replied.

Why ``TestClient`` and not raw ``httpx``
----------------------------------------
``request_guard`` is the production posture: every ``/api/*`` request
must carry ``X-PDT-Request: 1`` and a loopback ``Host``.  The suite's
conftest injects the header into every ``TestClient``, so a client
that must *not* send it has to have the header popped explicitly —
which is exactly what the "missing header" row of the guard matrix
does.  Driving the real app (rather than calling the handlers) is what
makes the guard, the router and the store part of the same test.

Isolation
---------
The autouse ``isolated_plans_dir`` fixture redirects the plans tree and
the state database at ``tmp_path``; this file never reads a live plan
or a live database.  The traversal case additionally snapshots
``tmp_path.parent`` — the directory *above* the test's own root — so a
plan id that climbed out of the plans root would be caught even when it
landed one level above the test's sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from hitl.models import DecisionStatus  # noqa: E402
from hooks.config import HITL_CONFIG_FILENAME  # noqa: E402
from hooks.dispatcher import AUDIT_FILENAME, dispatch  # noqa: E402
from request_guard import REQUEST_HEADER  # noqa: E402


#: The mounted application, driven the way a caller drives it.
client = TestClient(server.app)

#: A plan id that is valid under the shared validator (``[A-Za-z0-9._-]``)
#: and that no other test in the suite uses.
PLAN_ID = "sec-hitl"

#: The event a plan's outlet is asked to deliver when a decision is
#: asked.
EVENT = "hitl_decision_created"


# ---------------------------------------------------------------------------
# The three doors, and the three ways to fail the guard
# ---------------------------------------------------------------------------

#: A question that can become a decision.
CREATE_BODY: Dict[str, Any] = {
    "question": "Ship the change as written?",
    "options": ["accept", "revise"],
    "default_option": "accept",
}

#: An answer to a decision that does not exist yet — the guard has to
#: refuse the request *before* the decision is looked up.
ANSWER_BODY: Dict[str, Any] = {
    "option": "accept",
    "answered_by": "alice",
    "note": "",
}

#: The three HITL endpoints: the name, the method, the path template and
#: the body the request carries.
ENDPOINTS: Tuple[Tuple[str, str, str, Dict[str, Any]], ...] = (
    ("create", "POST", "/api/hitl/{plan_id}/decisions", {"json": CREATE_BODY}),
    ("list", "GET", "/api/hitl/{plan_id}/decisions", {}),
    (
        "answer",
        "POST",
        "/api/hitl/{plan_id}/decisions/{decision_id}/answer",
        {"json": ANSWER_BODY},
    ),
)

#: The three ways a request can fail the guard.  The header mapping is
#: what the request carries *in addition to* the guard header;
#: ``None`` means the guard header itself is missing, which the client
#: wrapper has to be told explicitly (it injects one by default).
GUARD_BREAKS: Tuple[Tuple[str, Optional[Dict[str, str]]], ...] = (
    ("missing_header", None),
    ("foreign_origin", {"origin": "http://evil.example"}),
    ("foreign_host", {"host": "evil.example"}),
)


def _url(path: str, plan_id: str = PLAN_ID, decision_id: str = "d-guard") -> str:
    """The URL of one of the three doors."""
    return path.format(plan_id=plan_id, decision_id=decision_id)


def _bare_client() -> TestClient:
    """A client that sends no ``X-PDT-Request`` header.

    The suite's conftest injects the guard header into every
    ``TestClient`` — that is what keeps the rest of the suite testing
    routes rather than plumbing.  This client is the one place where
    the plumbing itself is under test, so the header is popped.
    """
    bare = TestClient(server.app)
    bare.headers.pop(REQUEST_HEADER, None)
    return bare


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _tree_snapshot(root: Path, skip_names: Tuple[str, ...] = ()) -> Dict[str, str]:
    """A content digest of every file and directory under ``root``.

    The digest is the sha256 of the file's bytes, so a file that is
    rewritten with the same content is not a difference and a file that
    is rewritten with different content is.  ``skip_names`` drops files
    whose *name* starts with one of the given prefixes — the test uses
    it for the SQLite database, whose ``-wal`` / ``-shm`` sidecars come
    and go with the connections the app opens; the database's logical
    content is asserted separately and exactly.
    """
    entries: Dict[str, str] = {}
    if not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        if skip_names and path.name.startswith(skip_names):
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[rel] = "symlink:" + os.readlink(path)
        elif path.is_dir():
            entries[rel + "/"] = "dir"
        else:
            entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def _outside_snapshot(tmp_path: Path) -> Dict[str, str]:
    """The tree *around* the plans root, as one digest mapping.

    The plans root lives at ``tmp_path/plans``; this snapshot is taken
    over ``tmp_path.parent`` (one level higher, so a plan id that
    climbed two levels out of the plans root is still inside it) with
    the plans subtree removed — what is left is exactly "everything
    outside the plans root".
    """
    plans_root = resolve_plans_dir()
    plans_prefix = f"{tmp_path.name}/{plans_root.name}/"
    snapshot = _tree_snapshot(tmp_path.parent, skip_names=("state.db",))
    return {
        path: digest
        for path, digest in snapshot.items()
        if not path.startswith(plans_prefix)
    }


def _decision_plan_ids() -> List[str]:
    """Every plan id the decision table holds, whatever its spelling."""
    db_path = resolve_state_db_path()
    if not db_path.exists():
        return []
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute("SELECT plan_id FROM plan_decisions").fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def _write_hooks(plan_dir: Path, hooks: List[Dict[str, Any]]) -> Path:
    """Write a plan's hook configuration, the way a plan creation would."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / HITL_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 1800,
                "hooks": list(hooks),
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def _receiver_script(tmp_path: Path) -> Path:
    """A helper script the ``cmd`` transport can run.

    It writes its stdin to the file named by its first argument and
    ignores everything else — so a hook command may carry extra
    arguments of its own without changing what the payload looks like
    on the wire.
    """
    script = tmp_path / "receive.py"
    script.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return script


def _quoted(*parts: str) -> str:
    """A command string with every part quoted."""
    return " ".join(f'"{part}"' for part in parts)


# ---------------------------------------------------------------------------
# 1 — the guard covers all three doors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint", ENDPOINTS, ids=[name for name, _, _, _ in ENDPOINTS]
)
@pytest.mark.parametrize(
    "guard", GUARD_BREAKS, ids=[name for name, _ in GUARD_BREAKS]
)
def test_guard_matrix(endpoint, guard):
    """Every door refuses every way of failing the guard — with a 403.

    The guard is middleware over ``/api/``, so a route registered
    outside its reach would answer 200 to the one caller that must not
    reach it.  A *new* route is exactly where such a bypass would be
    added, so all three endpoints are checked — one of them answering
    200 would mean the guard does not cover this path.

    The three ways to fail the guard are the three the guard actually
    implements: the custom header (a browser page cannot set it
    without a preflight this server never grants), the ``Origin``
    (a page served from somewhere else) and the ``Host`` (a rebound
    hostname that resolves to loopback).

    "Nothing happened behind the refusal" is asserted as well: a 403
    that still created a decision would be a 403 with a side effect.
    """
    name, method, path, kwargs = endpoint
    guard_name, extra_headers = guard

    if guard_name == "missing_header":
        caller = _bare_client()
        headers: Optional[Dict[str, str]] = None
    else:
        caller = client
        headers = dict(extra_headers or {})

    response = caller.request(
        method, _url(path), headers=headers, **kwargs
    )

    assert response.status_code == 403, (
        f"the {name!r} endpoint answered {response.status_code} to a "
        f"request that fails the guard ({guard_name}); the guard does "
        f"not cover this path"
    )
    body = response.json()
    assert body["error"] == "forbidden", (
        "the 403 body is flat — ``error`` names the rule that refused "
        "the request"
    )
    assert body["detail"], "a 403 that says nothing is a 403 nobody can debug"

    # Nothing was created behind the refusal.
    assert client.get(_url("/api/hitl/{plan_id}/decisions")).json() == []


# ---------------------------------------------------------------------------
# 2 — a plan id cannot walk out of the plans root
# ---------------------------------------------------------------------------

#: The plan ids that try to leave the plans root.  Each spelling is a
#: different way of saying "..": in the clear, percent-encoded, with an
#: encoded separator, with a backslash, doubled, and as an absolute
#: path.
TRAVERSAL_PLAN_IDS: Tuple[str, ...] = (
    "..",
    "%2e%2e",
    "%2e%2e%2f%2e%2e",
    "..%2f..%2ftmp",
    "..%2f..%2fescape",
    "%2e%2e%5c%2e%2e",
    "....%2f....%2fescape",
    "%2fetc%2fpasswd",
)


def test_path_traversal_zero_write(tmp_path):
    """A traversal plan id writes nothing — inside the plans root or out.

    The plan id becomes a filesystem leaf, so it is the one piece of
    the request that could name a directory the plan does not own.  The
    status code alone is not the property worth pinning: a 400 that
    still created ``plans/../escape`` is a 400 with a side effect.  So
    the test snapshots the tree **outside** the plans root before and
    after and asserts the two snapshots are identical — zero
    difference, not "nothing important changed".

    The snapshot covers ``tmp_path.parent`` (one level above the test's
    own root) so a plan id that climbed two levels out of the plans
    root is inside the snapshot rather than next to it.
    """
    plans_root = resolve_plans_dir()
    inside_before = _tree_snapshot(plans_root)
    outside_before = _outside_snapshot(tmp_path)

    for plan_id in TRAVERSAL_PLAN_IDS:
        for name, method, path, kwargs in ENDPOINTS:
            response = client.request(
                method, _url(path, plan_id=plan_id), **kwargs
            )
            assert response.status_code < 500, (
                f"the {name!r} endpoint answered {response.status_code} for "
                f"the traversal plan id {plan_id!r}; a malformed plan id "
                f"must be refused, not crash the server"
            )
            assert response.status_code != 200, (
                f"the {name!r} endpoint answered 200 for the traversal plan "
                f"id {plan_id!r}; the validator was bypassed or its "
                f"contract drifted"
            )
            assert 400 <= response.status_code < 500, (
                f"the {name!r} endpoint answered {response.status_code} for "
                f"the traversal plan id {plan_id!r}; expected a 4xx"
            )
            # A refusal says what was wrong with the request; it does
            # not narrate where the server's files live.
            assert "Traceback" not in response.text, (
                f"the {name!r} endpoint answered with a traceback for the "
                f"traversal plan id {plan_id!r}"
            )
            assert str(plans_root) not in response.text, (
                f"the {name!r} endpoint leaked the plans root path in its "
                f"refusal of the traversal plan id {plan_id!r}"
            )

    # The decision id is a path parameter too.  It is a column value
    # rather than a filesystem leaf, so it cannot walk out of the plans
    # root — but a caller must not be able to crash the endpoint with
    # it either.
    for decision_id in TRAVERSAL_PLAN_IDS:
        response = client.post(
            _url(
                "/api/hitl/{plan_id}/decisions/{decision_id}/answer",
                decision_id=decision_id,
            ),
            json=ANSWER_BODY,
        )
        assert 400 <= response.status_code < 500, (
            f"the answer endpoint answered {response.status_code} for the "
            f"traversal decision id {decision_id!r}; expected a 4xx"
        )

    inside_after = _tree_snapshot(plans_root)
    outside_after = _outside_snapshot(tmp_path)

    assert inside_after == inside_before, (
        "a traversal plan id changed the plans tree: "
        f"{_difference(inside_before, inside_after)}"
    )
    assert outside_after == outside_before, (
        "a traversal plan id wrote outside the plans root: "
        f"{_difference(outside_before, outside_after)}"
    )

    # The decision table is the other half of "nothing was written": a
    # row that was inserted under a traversal id would not show up
    # in the plans tree at all.
    stored = _decision_plan_ids()
    assert stored == [], (
        f"the decision table holds rows for {stored!r}; a traversal plan "
        f"id created a decision"
    )


def _difference(before: Dict[str, str], after: Dict[str, str]) -> str:
    """The entries that differ between two snapshots, as one line."""
    changed = [
        f"{path}: {before.get(path, '<absent>')} -> {after.get(path, '<absent>')}"
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]
    return "; ".join(changed) or "(none)"


# ---------------------------------------------------------------------------
# 3 — the payload never becomes a command
# ---------------------------------------------------------------------------


def test_cmd_injection_sentinel(tmp_path, isolated_plans_dir):
    """A question full of shell metacharacters stays data.

    The decision's text is whatever a caller wrote — it arrives over
    HTTP and is stored verbatim — and the ``cmd`` transport runs a
    command from the plan's configuration.  The only defence that
    holds against ``; rm -rf /`` in a question is structural: the
    payload is handed to the process on **stdin** and never
    interpolated into the command string.

    Both halves are asserted, because either alone is ambiguous: the
    sentinel the injected text would have created must not exist (the
    payload did not reach a command line), and the bytes the command
    received must be the payload verbatim (the command really ran, and
    really got the question — a command that never ran would satisfy
    the first assertion alone).
    """
    plan_dir = isolated_plans_dir / PLAN_ID
    received = tmp_path / "received.json"
    sentinel = tmp_path / "pdt-hitl-sentinel"
    script = _receiver_script(tmp_path)

    # The question carries everything a command injection needs.  The
    # sentinel lives in this test's own sandbox, so an injection that
    # *did* work would be visible without touching anything outside
    # it.
    question = (
        f"; touch {sentinel}\n"
        f"$(touch {sentinel})\n"
        f"`touch {sentinel}`\n"
        f"| touch {sentinel}\n"
        f"&& touch {sentinel}"
    )

    created = client.post(
        _url("/api/hitl/{plan_id}/decisions"),
        json={
            "question": question,
            "options": [f"accept {sentinel}", "revise"],
            "default_option": f"accept {sentinel}",
        },
    )
    assert created.status_code == 200, created.text
    decision = created.json()
    assert decision["question"] == question, (
        "the question is stored as it was written — that is the whole "
        "point of the case"
    )

    # The outlet is configured *after* the question was asked, so the
    # only delivery that runs is the one this case drives.  A plan
    # that already named a destination would have the question
    # delivered on the background thread as well, and the two
    # deliveries would write the same file — the case would then be
    # reading whichever of them happened to land last.
    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _quoted(sys.executable, str(script), str(received)),
                "timeout_sec": 30,
            }
        ],
    )

    # The plan's outlet is asked to deliver the question.
    payload: Dict[str, Any] = {
        "decision_id": decision["decision_id"],
        "plan_id": decision["plan_id"],
        "question": decision["question"],
        "options": decision["options"],
    }
    report = dispatch(EVENT, payload, plan_dir)

    assert report.delivered == 1, (
        f"the command hook was not delivered to: {report}"
    )
    assert not sentinel.exists(), (
        "the payload reached the command line; the question text was "
        "interpolated into the command instead of being written to stdin"
    )

    expected = json.dumps({"event": EVENT, **payload})
    assert received.read_text(encoding="utf-8") == expected, (
        "the command did not receive the payload verbatim on stdin"
    )


# ---------------------------------------------------------------------------
# 4 — the audit trail does not carry a credential
# ---------------------------------------------------------------------------


def test_redaction(tmp_path, isolated_plans_dir):
    """A token in a hook's target never reaches the plan directory.

    Three shapes of credential are covered, because three different
    parts of the URL carry one: a query parameter, the userinfo part
    of the URL, and a command line that names its own token.  All
    three end up in the dispatch audit line as the *target* of the
    destination — and the audit line is a file in the plan directory,
    which is not the place for a credential.

    The decision's own audit mirror is checked too, for the same
    reason from the other side: the mirror records a fixed whitelist
    of fields, and the question text — which can carry source
    excerpts, and here carries a token as well — is not one of them.
    """
    plan_dir = isolated_plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    query_token = "QUERY-TOKEN-2f8a4c"
    userinfo_token = "USERINFO-TOKEN-91c3e7"
    command_token = "COMMAND-TOKEN-4b7ed2"

    received = tmp_path / "received.json"
    script = _receiver_script(tmp_path)

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "http",
                "url": f"http://127.0.0.1:9/hook?token={query_token}",
            },
            {
                "kind": "http",
                "url": f"https://user:{userinfo_token}@127.0.0.1:9/hook",
            },
            {
                "kind": "cmd",
                "command": (
                    _quoted(sys.executable, str(script), str(received))
                    + f" --token={command_token}"
                ),
                "timeout_sec": 30,
            },
        ],
    )

    dispatch(EVENT, {"decision_id": "d-redact"}, plan_dir)

    audit = (plan_dir / AUDIT_FILENAME).read_text(encoding="utf-8")
    for token in (query_token, userinfo_token, command_token):
        assert token not in audit, (
            f"the token {token!r} is in the dispatch audit in the clear"
        )
    assert "<redacted>" in audit, (
        "the target was not redacted at all — the credential is gone, but "
        "so is the information the audit line exists to carry"
    )

    # The decision's own mirror: the question text is not one of the
    # fields it records, so the token it carries never reaches it.
    created = client.post(
        _url("/api/hitl/{plan_id}/decisions"),
        json={
            "question": f"is token={query_token} safe to ship?",
            "options": ["accept", "revise"],
            "default_option": "accept",
        },
    )
    assert created.status_code == 200, created.text

    mirror = (plan_dir / "hitl_decisions.jsonl").read_text(encoding="utf-8")
    assert query_token not in mirror, (
        "the token is in the decision audit mirror in the clear"
    )
    assert "safe to ship" not in mirror, (
        "the question text is in the decision audit mirror; the mirror's "
        "field whitelist exists so a question can carry source excerpts "
        "without writing them down"
    )


# ---------------------------------------------------------------------------
# The routing contract a broken hook must not break
# ---------------------------------------------------------------------------


def test_one_bad_hook_does_not_take_the_question_down(tmp_path, isolated_plans_dir):
    """One broken destination does not take the question down with it.

    A plan's hooks are a fan-out: every destination is a first-class
    target, and the ones that are broken must not take the others down
    with them.  The question is still asked, and it is still open —
    somebody *was* reached, so a human can still answer it.
    """
    plan_dir = isolated_plans_dir / PLAN_ID
    received = tmp_path / "received.json"
    script = _receiver_script(tmp_path)

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _quoted(sys.executable, "-c", "import sys; sys.exit(3)"),
                "timeout_sec": 30,
            },
            {
                "kind": "cmd",
                "command": _quoted(sys.executable, str(script), str(received)),
                "timeout_sec": 30,
            },
        ],
    )

    created = client.post(
        _url("/api/hitl/{plan_id}/decisions"),
        json={
            "question": "Ship the change as written?",
            "options": ["accept", "revise"],
            "default_option": "accept",
        },
    )
    assert created.status_code == 200, created.text
    decision = created.json()

    report = dispatch(
        EVENT,
        {"decision_id": decision["decision_id"], "plan_id": decision["plan_id"]},
        plan_dir,
    )

    assert report.delivered == 1, (
        "the working destination was not reached; one broken destination "
        f"took the whole fan-out down: {report}"
    )
    assert report.failed == 1, (
        "the broken destination was not reported as failed: "
        f"{report}"
    )

    # The question is still open: a human was reached, so it can still
    # be answered.
    listed = client.get(_url("/api/hitl/{plan_id}/decisions")).json()
    assert [d["status"] for d in listed] == [
        DecisionStatus.PENDING.value
    ], "a routing failure settled a question somebody could still answer"

    answered = client.post(
        _url(
            "/api/hitl/{plan_id}/decisions/{decision_id}/answer",
            decision_id=decision["decision_id"],
        ),
        json={"option": "accept", "answered_by": "alice", "note": ""},
    )
    assert answered.status_code == 200, answered.text


def test_all_bad_hooks_leave_the_question_unanswered(tmp_path, isolated_plans_dir):
    """Nobody was asked — so nothing may claim that a human replied.

    When every destination fails, the delivery report is the only
    honest signal the caller has: ``delivered`` is zero, so nobody
    was reached.  The decision must not be dressed up as answered or
    defaulted — both of those say "the question was settled", and it
    was not.

    The state that means "nobody was asked" is ``unroutable``.  No
    production path writes that transition yet (the router that would
    consume the report does not exist), so the decision is still
    ``pending`` today; the invariant pinned here is the one that holds
    either way — a routing failure never becomes an answer.
    """
    plan_dir = isolated_plans_dir / PLAN_ID

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _quoted(sys.executable, "-c", "import sys; sys.exit(3)"),
                "timeout_sec": 30,
            },
            {
                "kind": "cmd",
                "command": _quoted(sys.executable, "-c", "import sys; sys.exit(4)"),
                "timeout_sec": 30,
            },
        ],
    )

    created = client.post(
        _url("/api/hitl/{plan_id}/decisions"),
        json={
            "question": "Ship the change as written?",
            "options": ["accept", "revise"],
            "default_option": "accept",
        },
    )
    assert created.status_code == 200, created.text
    decision = created.json()

    report = dispatch(
        EVENT,
        {"decision_id": decision["decision_id"], "plan_id": decision["plan_id"]},
        plan_dir,
    )

    assert report.delivered == 0, (
        f"a destination that failed was reported as delivered: {report}"
    )
    assert report.failed == 2, (
        f"the two broken destinations were not reported as failed: {report}"
    )

    listed = client.get(_url("/api/hitl/{plan_id}/decisions")).json()
    assert len(listed) == 1
    stored = listed[0]
    assert stored["status"] in {
        DecisionStatus.PENDING.value,
        DecisionStatus.UNROUTABLE.value,
    }, (
        "a decision nobody was asked about must not be reported as "
        f"answered or defaulted; it is {stored['status']!r}"
    )
    assert stored["answer"] is None, (
        "a routing failure stored an answer; nobody answered"
    )
    assert stored["answered_by"] is None, (
        "a routing failure named an answerer; nobody answered"
    )