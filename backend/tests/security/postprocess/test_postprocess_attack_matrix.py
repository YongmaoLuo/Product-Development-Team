"""The finishing stage's security attack matrix.

What is pinned here
-------------------
The finishing stage (收尾) added four endpoints that **change** a plan:
push a waiting item's deadline out, give up on a wait, answer a
question a human was asked, and move a deadline.  Each of them is a
way in, so each of them is a row of the matrix below, and the matrix is
table-driven on purpose: the four endpoints and the four ways a request
can be malformed are crossed, and every cell has to answer the same
way.

The six boundaries
------------------
* **The request guard.**  A loopback address is not a browser security
  boundary, so every ``/api/`` request has to carry the guard header,
  come from its own origin and name its own host.  A request that does
  not is refused with a 403 **before** the handler runs — so a refused
  request changed nothing at all.
* **Names are names.**  A ``plan_id`` and an ``item_id`` name a row of
  the plan's own tree; neither may carry a separator or a traversal.
  A request that tries is refused (400 for a name that is not a name,
  404 for a row that is not there) and the sentinel file outside the
  plans root is what proves the refusal happened **before** the write.
* **No shell.**  A requirement book is text.  Its argv is a list, the
  list is handed to ``subprocess`` with ``shell=False``, and a payload
  that would expand under a shell stays one single argument.
* **No credentials on the wire.**  The four credential shapes the
  security review named are stopped where the text is **produced**: the
  verdict's evidence excerpt never holds the raw value, and the report
  — the file, the panel's terminal summary read out of it, and the
  failure notification — carries the redacted form instead.  The audit
  line the notification leaves behind is private (0600).
* **Approval, not execution.**  A merge into ``main``, a destructive
  command and a command that costs money become a *pending* question and
  nothing else: the executor does not spawn, and nobody answers on the
  human's behalf.
* **One plan's question is one plan's question.**  A decision asked by
  one plan cannot be answered through another plan's route, and a
  question that was given up on cannot be answered at all.

Why the requests are real HTTP requests
---------------------------------------
The cells drive the **mounted app**, not the route functions: the answer
an operator gets is the answer the HTTP layer gives, status code and
body together.  A cell that called the function directly would prove the
body and not the status, and the status is half of what this contract
is.

Why the waits are made by the production registry
-------------------------------------------------
A wait is created by :class:`postprocess.waits.WaitRegistry`, which
writes it into the plan's own progress file — which is where the
endpoints look for it.  The cells create their waits the same way, so
what an endpoint reads is a record the production code wrote, not a
hand-made file that happens to look like one.

Where the redaction boundary is
-------------------------------
The report is the finishing stage's source of truth — its evidence
excerpt is a copy of the command's output on purpose
(``postprocess/judge.py`` says so: the excerpt is what a reader
compares against the command that produced it).  A copy of a command's
output is exactly where a credential the command printed ends up, so
the boundary is drawn where that text is **produced**: the excerpt
that leaves the judge has already been through the redactor, and the
report writer redacts once more on the way to the file.  Redacting
twice is idempotent, so the report, the panel's terminal summary read
out of it and the failure notification all carry the same redacted
form — and none of them carries the raw value.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import request_guard  # noqa: E402
import server  # noqa: E402
from config_paths import resolve_state_db_path  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from plan_scheduler.snapshot import postprocess_summary  # noqa: E402
from postprocess.executor import CommandRunner, NeedsApproval  # noqa: E402
from postprocess.judge import judge  # noqa: E402
from postprocess.notify import (  # noqa: E402
    notification_log_path,
    notify_failure,
    redact,
)
from postprocess.progress import load_progress  # noqa: E402
from postprocess.report import report_path, write_report  # noqa: E402
from postprocess.spec import SPEC_FILENAME, freeze, load_spec  # noqa: E402
from postprocess.waits import WaitRegistry  # noqa: E402
from request_guard import REQUEST_HEADER  # noqa: E402
from routes.scheduler import plan_waiting_rows  # noqa: E402
from tests.fixtures.postprocess.builders import make_item, make_spec  # noqa: E402


#: Every case in this module is an integration case: it drives the
#: mounted app and the production writers.
pytestmark = pytest.mark.integration

#: The waiting item the cells act on.
ITEM_ID = "approve"

#: The item the answer endpoint's own question belongs to.
ANSWER_ITEM_ID = "approve-answer"

#: The four credential shapes the security review named.
CREDENTIALS: Tuple[str, ...] = (
    "ghp_AAAABBBBCCCCDDDDEEEE",
    "sk-AAAABBBBCCCCDDDD",
    "xoxb-AAAABBBBCCCCDDD",
    "Bearer AAAABBBBCCCCDDDD",
)


# ---------------------------------------------------------------------------
# The fixtures the cells act through
# ---------------------------------------------------------------------------


def plan_dir(plan_id: str) -> Path:
    """The plan's own directory, as the server sees it."""
    return Path(server.PLANS_DIR) / plan_id


def spec_path(plan_id: str) -> Path:
    """The requirement book's own path."""
    return plan_dir(plan_id) / SPEC_FILENAME


def digest(path: Path) -> str:
    """The sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_digests(plan_id: str) -> Dict[str, str]:
    """Every file of the plan's own directory, by name and by digest."""
    directory = plan_dir(plan_id)
    if not directory.is_dir():
        return {}
    return {
        path.name: digest(path)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def a_book(action: str = "整理发布说明") -> Dict[str, Any]:
    """A legal requirement book."""
    return make_spec([make_item(id="release-notes", action=action)])


def an_external_item(
    item_id: str,
    action: str,
    argv: List[str],
    target: str = "origin/main",
) -> Dict[str, Any]:
    """A requirement item that runs one command against something outside.

    The command runner's whitelist is built out of the book's argv, so
    an item that runs a command has to be an outward-facing one and has
    to say what it acts on.
    """
    item = make_item(id=item_id, action=action, external=True)
    item["target"] = target
    item["command"] = list(argv)
    return item


def write_book(plan_id: str, book: Mapping[str, Any]) -> Path:
    """Put a requirement book on disk, as the plan's own file."""
    path = spec_path(plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def make_wait(plan_id: str, item_id: str = ITEM_ID) -> None:
    """Give one plan one waiting item, through the production registry."""
    WaitRegistry(plan_id).create_wait(
        {"id": item_id, "wait": {"type": "human"}},
        question="批准合入？",
    )


def wait_record(plan_id: str, item_id: str = ITEM_ID) -> Dict[str, Any]:
    """One wait's durable record, as it sits in the progress file."""
    return load_progress(plan_id)["items"][item_id]["wait"]


def extend_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The extend endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/extend"


def terminate_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The terminate endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/terminate"


def deadline_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The deadline endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/deadline"


def spec_url(plan_id: str) -> str:
    """The requirement book endpoint of one plan."""
    return f"/api/scheduler/{plan_id}/postprocess-spec"


def answer_url(plan_id: str, decision_id: str) -> str:
    """The answer endpoint of one plan's own decision."""
    return f"/api/hitl/{plan_id}/decisions/{decision_id}/answer"


def pending_decision(plan_id: str, item_id: str = ANSWER_ITEM_ID) -> str:
    """One plan's own pending question, stored the way the stage stores one.

    The question is an **approval request** — the kind the finishing
    stage creates when the executor refuses to run a command on its
    own — and it goes into the HITL table through the wait registry.
    So the row the answer endpoint answers is a row production code
    wrote, not a hand-made one that happens to look like it.
    """
    store = HitlStore(resolve_state_db_path())
    try:
        registry = WaitRegistry(plan_id, store=store)
        record = registry.create_approval_request(
            {"id": item_id, "wait": {"type": "human"}},
            NeedsApproval(
                question="批准合入 main？",
                command=["gh", "pr", "merge", "42"],
            ),
        )
    finally:
        store.close()
    return str(record["decision_id"])


def mutation_requests(
    plan_id: str,
) -> List[Tuple[str, str, str, Optional[Dict[str, Any]]]]:
    """The four mutation endpoints, as ``(name, method, url, body)``.

    The four of them are the finishing stage's whole attack surface: a
    wait can be pushed out, a wait can be given up on, a question can be
    answered and a deadline can be moved.  A cell that only exercised
    one of them would leave the other three untested — and the answer
    endpoint is the one that used to be missing here, so the only place
    it appeared at all was the cross-plan cell, where the guard is not
    what decides the answer.
    """
    make_wait(plan_id)
    decision_id = pending_decision(plan_id)
    return [
        ("extend", "POST", extend_url(plan_id), {"seconds": 60}),
        ("terminate", "POST", terminate_url(plan_id), None),
        (
            "answer",
            "POST",
            answer_url(plan_id, decision_id),
            {"option": "approve"},
        ),
        ("deadline", "PATCH", deadline_url(plan_id), {"remaining_seconds": 60}),
    ]


def send(
    client: TestClient,
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
) -> Any:
    """One request, with a body only when the endpoint takes one."""
    if body is None:
        return client.request(method, url)
    return client.request(method, url, json=body)


def unguarded_client() -> TestClient:
    """A client whose requests carry no guard header.

    ``conftest`` wraps ``TestClient.__init__`` to inject the guard
    header; the wrapper adds it back on construction, so a client that
    must not send it has to drop it afterwards.
    """
    client = TestClient(server.app)
    client.headers.pop(REQUEST_HEADER, None)
    return client


class Recorder:
    """A notification channel that keeps what it was handed."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def notify(self, plan_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        self.calls.append((plan_id, kind, dict(payload)))


def decision_row(decision_id: str) -> Any:
    """One stored decision, read back through the production store."""
    store = HitlStore(resolve_state_db_path())
    try:
        return store.get(decision_id)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 1 — the request guard
# ---------------------------------------------------------------------------


def test_each_mutation_endpoint_rejects_missing_guard_header() -> None:
    """A request without the guard header never reaches the endpoint.

    The guard is middleware over every ``/api/`` path, so the refusal
    happens before the endpoint is reached — and the endpoint's own
    answer to "which item?" never gets to matter.  A request without
    the header is the shape a browser page from another origin produces,
    and loopback is not a boundary against one.
    """
    plan_id = "sec-guard-missing"
    requests = mutation_requests(plan_id)
    before = plan_digests(plan_id)

    for name, method, url, body in requests:
        response = send(unguarded_client(), method, url, body)

        assert response.status_code == 403, (
            f"{name}: a request without the guard header is refused, got "
            f"{response.status_code}: {response.text}"
        )

    assert plan_digests(plan_id) == before, (
        "the refused requests changed nothing in the plan's own directory"
    )
    assert wait_record(plan_id)["extensions"] == [], (
        "the refused extend changed no deadline"
    )
    assert not spec_path(plan_id).exists(), (
        "the refused write left the plan's own book unwritten"
    )


def test_cross_origin_and_host_rejected() -> None:
    """A foreign origin and a forged host are both refused.

    The origin check catches a page that is not this UI; the host check
    catches DNS rebinding, where the attacker's name re-resolves to
    loopback and arrives in ``Host``.  Both are refused before the
    handler runs.
    """
    plan_id = "sec-guard-origin-host"
    requests = mutation_requests(plan_id)
    before = plan_digests(plan_id)

    for name, headers in (
        ("foreign origin", {"Origin": "http://evil.example.com"}),
        ("forged host", {"Host": "evil.example.com"}),
    ):
        client = TestClient(server.app, headers=headers)
        for endpoint, method, url, body in requests:
            response = send(client, method, url, body)

            assert response.status_code == 403, (
                f"{name}: {endpoint} is refused, got "
                f"{response.status_code}: {response.text}"
            )

    assert plan_digests(plan_id) == before, (
        "the refused requests changed nothing in the plan's own directory"
    )
    assert wait_record(plan_id)["extensions"] == [], (
        "the refused extend changed no deadline"
    )
    assert not spec_path(plan_id).exists(), (
        "the refused write left the plan's own book unwritten"
    )


def test_the_answer_endpoint_answers_its_own_plan() -> None:
    """The control: the endpoint works when the request is legal.

    The guard cells prove that a forged request is refused.  A refusal
    on its own proves nothing about *what* was refused, so this cell
    asks the same question the legal way: the plan's own pending
    question, answered through the plan's own route, is answered — 200.
    Without this cell, a 403 could equally well mean "the guard refused
    it" and "the endpoint would have refused it anyway".
    """
    plan_id = "sec-answer-legal"
    decision_id = pending_decision(plan_id)

    response = send(
        TestClient(server.app),
        "POST",
        answer_url(plan_id, decision_id),
        {"option": "approve"},
    )

    assert response.status_code == 200, (
        f"the plan's own question is answered, got {response.status_code}: "
        f"{response.text}"
    )


def test_answer_endpoint_rejects_three_guard_violations() -> None:
    """The answer endpoint is behind the guard, like every other way in.

    The answer endpoint records what a human decided, so a forged
    request that reached it would put somebody else's answer into the
    plan.  It is guarded by the same shared middleware as the rest of
    ``/api/``, and this cell asks it the three ways a browser page
    from another origin asks: without the guard header, with a foreign
    origin, and with a forged host.  Each of the three is refused, and
    the question is still open afterwards — so the refusals were the
    guard's doing and not the endpoint's.

    The control is the same question asked the legal way: the plan's
    own pending question, answered through the plan's own route, is
    answered.
    """
    plan_id = "sec-answer-guard"
    decision_id = pending_decision(plan_id)
    url = answer_url(plan_id, decision_id)
    body = {"option": "approve"}

    for name, client in (
        ("missing header", unguarded_client()),
        (
            "foreign origin",
            TestClient(
                server.app, headers={"Origin": "http://evil.example.com"}
            ),
        ),
        (
            "forged host",
            TestClient(server.app, headers={"Host": "evil.example.com"}),
        ),
    ):
        response = send(client, "POST", url, body)

        assert response.status_code == 403, (
            f"{name}: the answer endpoint is refused, got "
            f"{response.status_code}: {response.text}"
        )

    answered = send(TestClient(server.app), "POST", url, body)
    assert answered.status_code == 200, (
        "the question is still open after the three refusals, so they were "
        f"the guard's doing — got {answered.status_code}: {answered.text}"
    )


def test_guard_prefix_mutation_detects_missing_answer_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer row's 403 is the guard's doing — take the guard away.

    A 403 that would still be a 403 with the guard's coverage of
    ``/api/`` taken away proves nothing: the endpoint might have
    refused the request on its own.  So this cell removes the guard's
    coverage and asks again — and the answer has to change, or the
    matrix's answer row is not pinning the guard at all.

    The mutation narrows the **prefix** the guard applies, which is the
    shipped way to take the coverage away: the guard reads the prefix
    when it decides, so narrowing it to a path this endpoint does not
    have leaves the endpoint unguarded.  Nothing else about the guard
    changes, and the cell restores the prefix when it is done.
    """
    plan_id = "sec-guard-mutation"
    decision_id = pending_decision(plan_id)
    url = answer_url(plan_id, decision_id)

    refused = send(unguarded_client(), "POST", url, {"option": "approve"})
    assert refused.status_code == 403, (
        f"a request without the guard header is refused, got "
        f"{refused.status_code}: {refused.text}"
    )

    monkeypatch.setattr(request_guard, "GUARDED_PREFIX", "/api/scheduler/")
    allowed = send(unguarded_client(), "POST", url, {"option": "approve"})

    assert allowed.status_code != 403, (
        "narrowing the guarded prefix still refuses the answer request, so "
        "the matrix's 403 cell does not depend on the guard — the answer "
        "row would not have caught a guard that stopped covering /api/"
    )
    assert allowed.status_code == 200, (
        "without the guard the answer request reaches the endpoint and "
        f"answers the question, got {allowed.status_code}: {allowed.text}"
    )


# ---------------------------------------------------------------------------
# 2 — names are names
# ---------------------------------------------------------------------------


def test_path_traversal_plan_and_item_ids_rejected(tmp_path) -> None:
    """A plan id or an item id that is a path is refused, and nothing is
    written outside the plans root.

    The sentinel is the proof: it lives **outside** the plans root, and
    a traversal that got as far as the filesystem would either write
    through it or create a directory next to it.  Both are checked, and
    the sentinel's own bytes are checked too — a refusal that rewrote
    the sentinel would be no refusal at all.
    """
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    plan_id = "sec-traversal-plan"
    make_wait(plan_id)
    write_book(plan_id, a_book())
    before = plan_digests(plan_id)

    #: Plan ids that are paths, not names.
    plan_id_traversals = (
        "%2e%2e%2fescape",
        "%2e%2e",
        "%2fetc%2fpasswd",
        "%00",
        "%5c..%5c",
    )
    #: Item ids that are paths, not names.
    item_id_traversals = (
        "a%2fb",
        "%2e%2e",
        "..%2f..%2fpostprocess.json",
    )

    for traversal in plan_id_traversals:
        response = send(
            TestClient(server.app),
            "PUT",
            f"/api/scheduler/{traversal}/postprocess-spec",
            a_book("越界"),
        )

        assert response.status_code == 400, (
            f"a plan id of {traversal!r} is a bad request, got "
            f"{response.status_code}: {response.text}"
        )

    #: The wait endpoints take the plan id as one segment, so only the
    #: traversals that keep the path's shape reach them at all — the
    #: others do not name a wait route any more, which is a refusal of
    #: its own kind.
    for traversal in ("%2e%2e", "%00", "%5c..%5c"):
        for method, url, body in (
            ("POST", extend_url(traversal), {"seconds": 60}),
            ("POST", terminate_url(traversal), None),
            ("PATCH", deadline_url(traversal), {"remaining_seconds": 60}),
        ):
            response = send(TestClient(server.app), method, url, body)

            assert response.status_code == 400, (
                f"a plan id of {traversal!r} is a bad request, got "
                f"{response.status_code}: {response.text}"
            )

    for traversal in item_id_traversals:
        for method, url, body in (
            ("POST", extend_url(plan_id, traversal), {"seconds": 60}),
            ("POST", terminate_url(plan_id, traversal), None),
            ("PATCH", deadline_url(plan_id, traversal), {"remaining_seconds": 60}),
        ):
            response = send(TestClient(server.app), method, url, body)

            assert response.status_code == 400, (
                f"an item id of {traversal!r} is a bad request, got "
                f"{response.status_code}: {response.text}"
            )

    assert sentinel.read_text(encoding="utf-8") == "untouched", (
        "the refused requests touched the sentinel outside the plans root"
    )
    assert not (tmp_path / "escape").exists(), (
        "the refused requests created a directory outside the plans root"
    )
    assert plan_digests(plan_id) == before, (
        "the refused requests changed nothing in the plan's own directory"
    )


# ---------------------------------------------------------------------------
# 3 — no shell
# ---------------------------------------------------------------------------


def test_shell_injection_payloads_not_expanded(tmp_path) -> None:
    """A payload that would expand under a shell stays one argument.

    The requirement book is text a plan writes, so its argv is
    untrusted input.  The runner hands the argv to ``subprocess`` as a
    list with ``shell=False``, which is what makes ``$(...)``, backticks
    and ``;`` ordinary characters instead of syntax.

    The payloads are run **for real** — against the real ``git``, in a
    directory that is not a repository — so "no shell" is proved by the
    sentinel file not existing, not by reading the runner's source.
    """
    sentinel = tmp_path / "injection-sentinel.txt"

    payloads = (
        "; touch {sentinel}",
        "&& touch {sentinel}",
        "| touch {sentinel}",
        "$(touch {sentinel})",
        "`touch {sentinel}`",
        "\ntouch {sentinel}",
    )

    plan_id = "sec-injection"
    for template in payloads:
        payload = template.format(sentinel=sentinel)
        argv = ["git", "status", payload]
        book = make_spec(
            [an_external_item("run-check", "跑一次检查", argv)]
        )
        write_book(plan_id, book)
        frozen = freeze(load_spec(plan_dir(plan_id)))
        item = frozen.spec.items[0]

        calls: List[Tuple[Any, Dict[str, Any]]] = []

        def recording_run(args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "", "")

        runner = CommandRunner(frozen, workspace=tmp_path, run=recording_run)
        runner.run_item(item)

        assert len(calls) == 1, (
            f"the runner spawned exactly once for {payload!r}, got {len(calls)}"
        )
        args, kwargs = calls[0]
        assert isinstance(args, list), (
            f"the command was handed a list, not a string, for {payload!r}"
        )
        assert args == argv, (
            f"the argv reached the process unchanged for {payload!r}"
        )
        assert kwargs.get("shell") is False, (
            f"the process was spawned without a shell for {payload!r}"
        )

        # The real thing: the same argv, against the real git.
        real = CommandRunner(frozen, workspace=tmp_path)
        real.run_item(item)

        assert not sentinel.exists(), (
            f"the payload {payload!r} was expanded by something"
        )

    assert not sentinel.exists(), (
        "no payload was expanded into the sentinel file"
    )

    # And the API that takes the text in the first place stores it as
    # text: a book whose action carries a payload is written, not run.
    action = "; touch {sentinel}".format(sentinel=sentinel)
    response = send(
        TestClient(server.app),
        "PUT",
        spec_url(plan_id),
        a_book(action),
    )
    assert response.status_code == 200, (
        f"a book is accepted as text, got {response.status_code}: "
        f"{response.text}"
    )
    assert not sentinel.exists(), (
        "writing the book did not run what the book said"
    )


def test_postprocess_package_never_uses_a_shell() -> None:
    """The finishing stage's own package holds no shell escape hatch.

    A behavioural cell proves that *this* argv was not expanded; this
    one proves that no code path in the package reaches for a shell at
    all — ``shell=True`` and ``os.system`` are the two ways that
    happens, and neither is anywhere in ``backend/postprocess/``.
    """
    package = _BACKEND_DIR / "postprocess"
    sources = sorted(package.glob("*.py"))

    assert sources, "the finishing stage's package is where the code is"

    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "shell=True" not in text, (
            f"{source.name} spawns a process through a shell"
        )
        assert "os.system" not in text, (
            f"{source.name} runs a command through os.system"
        )


# ---------------------------------------------------------------------------
# 4 — no credentials on the wire
# ---------------------------------------------------------------------------


def test_secrets_redacted_in_report_and_waiting() -> None:
    """The four credential shapes never reach a file anybody reads.

    The finishing stage hands text to the outside in two ways: the
    failure notification and the panel's waiting rows.  Both carry the
    redacted form, and the audit line the notification leaves behind is
    private (0600) besides.

    A credential is **replaced**, never dropped: the reader has to be
    able to see that something was there.
    """
    for index, secret in enumerate(CREDENTIALS):
        # 1 — the redactor stops the shape.
        redacted = redact(f"token {secret} end")
        assert secret not in redacted, (
            f"the redactor left {secret!r} in the text"
        )
        assert "<redacted>" in redacted, (
            f"the redactor did not say what it did to {secret!r}"
        )

        # 2 — the notification carries the redacted form, and only the
        #     redacted form reaches the channel.
        notify_plan = f"sec-notify-{index}"
        recorder = Recorder()
        payload = notify_failure(
            notify_plan,
            [secret],
            f"plans/{notify_plan}/{secret}/postprocess_progress.json",
            sink=recorder,
        )

        assert secret not in json.dumps(payload, ensure_ascii=False), (
            "the notification payload carries the credential"
        )
        assert recorder.calls, "the notification was delivered to nobody"
        assert secret not in json.dumps(recorder.calls, ensure_ascii=False), (
            "the delivered message carries the credential"
        )

        # 3 — the audit line of that delivery carries it neither, and
        #     the file it lives in is private.
        log = notification_log_path(notify_plan)
        on_disk = log.read_text(encoding="utf-8")
        assert secret not in on_disk, (
            "the delivery's audit line carries the credential"
        )
        assert "<redacted>" in on_disk, (
            "the audit line does not record that a credential was redacted"
        )
        assert stat.S_IMODE(log.stat().st_mode) == 0o600, (
            "the delivery's audit line is not private"
        )

        # 4 — the panel's waiting row carries no credential, even when
        #     the command waiting for approval names one.
        wait_plan = f"sec-wait-{index}"
        store = HitlStore(resolve_state_db_path())
        try:
            registry = WaitRegistry(wait_plan, store=store)
            registry.create_approval_request(
                {"id": ITEM_ID, "wait": {"type": "human"}},
                NeedsApproval(
                    question="批准合入 main？",
                    command=["gh", "pr", "merge", secret],
                ),
            )
        finally:
            store.close()

        rows = plan_waiting_rows(wait_plan)
        assert rows, "the approval request is a waiting row"
        assert secret not in json.dumps(rows, ensure_ascii=False), (
            "the panel's waiting row carries the credential"
        )


def test_credential_in_stdout_is_redacted_end_to_end() -> None:
    """A credential a command printed stops where the evidence is made.

    A credential has to stop where the text is **produced**, not only
    where it goes on the wire: the verdict's evidence excerpt is a copy
    of what the command printed, and a command that printed a token
    would otherwise put it straight into the report.  So the excerpt
    that leaves the judge is already redacted, the report redacts once
    more on the way to the file, and the panel's terminal summary — read
    straight out of that file — carries the same redacted form.

    The report's audit triple is text too, so it is redacted as well:
    the description of what was done, the command that was run and the
    target it acted on.
    """
    for index, secret in enumerate(CREDENTIALS):
        # 5 — the evidence itself carries none: the command printed a
        #     credential, and the excerpt that leaves the judge says
        #     "<redacted>" instead.
        evidence_plan = f"sec-evidence-{index}"
        write_book(
            evidence_plan,
            make_spec(
                [
                    an_external_item(
                        "run-check",
                        "跑一次检查",
                        ["git", "status", "--porcelain"],
                    )
                ]
            ),
        )
        frozen = freeze(load_spec(plan_dir(evidence_plan)))
        item = frozen.spec.items[0]

        def printing_run(args: Any, **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(args, 0, f"ok {secret}\n", "")

        runner = CommandRunner(
            frozen, workspace=plan_dir(evidence_plan), run=printing_run
        )
        evidence, audit = runner.run_item(item)
        verdict = judge(item, [evidence])

        assert secret not in verdict.evidence_excerpt, (
            "the verdict's evidence excerpt carries the credential"
        )
        assert "<redacted>" in verdict.evidence_excerpt, (
            "the verdict's evidence excerpt does not say what it did to the "
            "credential"
        )

        write_report(
            evidence_plan,
            frozen_spec=frozen,
            item_verdicts=[
                {
                    "id": verdict.item_id,
                    "verdict": "PASS" if verdict.passed else "FAIL",
                    "evidence_excerpt": verdict.evidence_excerpt,
                    "audit": {
                        "action": audit.action,
                        "command": list(audit.command),
                        "target": audit.target,
                    },
                }
            ],
            terminal_status="completed",
        )

        report_file = report_path(evidence_plan)
        report_text = report_file.read_text(encoding="utf-8")
        assert secret not in report_text, (
            "the report carries the credential"
        )
        assert "<redacted>" in report_text, (
            "the report does not say what it did to the credential"
        )
        assert stat.S_IMODE(report_file.stat().st_mode) == 0o600, (
            "the report is not private"
        )

        # 6 — and the panel's terminal summary is read straight out of
        #     that very file, so it carries the redacted form as well.
        summary = postprocess_summary(evidence_plan)
        assert summary is not None, "the plan has a terminal summary"
        summary_text = json.dumps(summary, ensure_ascii=False)
        assert secret not in summary_text, (
            "the panel's terminal summary carries the credential"
        )
        assert "<redacted>" in summary_text, (
            "the panel's terminal summary does not say what it did to the "
            "credential"
        )

        # 7 — the report writer redacts the audit triple as well: the
        #     description, the command and the target are text too.
        audit_plan = f"sec-audit-{index}"
        write_book(
            audit_plan,
            make_spec(
                [
                    an_external_item(
                        "push-notes",
                        "推送发布说明",
                        ["git", "status", "--porcelain"],
                    )
                ]
            ),
        )
        audit_frozen = freeze(load_spec(plan_dir(audit_plan)))
        write_report(
            audit_plan,
            frozen_spec=audit_frozen,
            item_verdicts=[
                {
                    "id": "push-notes",
                    "verdict": "PASS",
                    "evidence_excerpt": f"ok {secret}",
                    "audit": {
                        "action": f"用 {secret} 推送到远端",
                        "command": [
                            "git",
                            "push",
                            f"https://x:{secret}@example.com/o/r.git",
                        ],
                        "target": f"https://x:{secret}@example.com/o/r.git",
                    },
                }
            ],
            terminal_status="completed",
        )

        audit_text = report_path(audit_plan).read_text(encoding="utf-8")
        assert secret not in audit_text, (
            "the report's audit triple carries the credential"
        )
        assert audit_text.count("<redacted>") >= 4, (
            "the report does not redact every part that carried the "
            "credential"
        )


def test_redaction_does_not_touch_identifiers() -> None:
    """The redactor takes the credential, not the identifier.

    A redaction that is too eager is its own kind of damage: an item
    called ``task-1`` contains the letters ``sk-``, and a reader who is
    asked to act on that id needs it intact.  So the boundary is pinned
    from both sides — the credential is replaced, and the identifier
    that merely looks like one is left exactly as it was.
    """
    assert redact("task-1") == "task-1", (
        "the redactor touched an identifier that only looks like a "
        "credential"
    )
    assert redact("task-1 sk-AAAABBBBCCCCDDDD") == "task-1 <redacted>", (
        "the redactor did not take the credential out of a line that also "
        "carries an identifier"
    )
    assert redact("<redacted>") == "<redacted>", (
        "the redactor is not idempotent"
    )
    assert redact("ok ghp_AAAABBBBCCCCDDDDEEEE") == "ok <redacted>", (
        "the redactor did not replace the credential"
    )


def test_report_artifact_is_0600() -> None:
    """The report file is private — 0600, read by its owner only.

    The report is an audit artefact and it lands in the plans tree,
    which is the operator's own home directory, so a mode that leaves
    it readable by everything running as that operator hands the audit
    trail to every other process on the machine.  The mode is a
    **contract of the write** — set on the open descriptor of the
    temporary file before the first byte is written — so it does not
    depend on the process's umask.
    """
    plan_id = "sec-report-mode"
    write_book(plan_id, a_book())
    frozen = freeze(load_spec(plan_dir(plan_id)))

    write_report(
        plan_id,
        frozen_spec=frozen,
        item_verdicts=[
            {
                "id": "release-notes",
                "verdict": "PASS",
                "evidence_excerpt": "ok",
                "audit": {
                    "action": "整理发布说明",
                    "command": [],
                    "target": "release-notes",
                },
            }
        ],
        terminal_status="completed",
    )

    report_file = report_path(plan_id)
    assert report_file.exists(), "the report was not written"
    mode = stat.S_IMODE(report_file.stat().st_mode)
    assert mode == 0o600, (
        f"the report is not private: mode {oct(mode)}, expected 0o600"
    )


# ---------------------------------------------------------------------------
# 5 — one plan's question is one plan's question
# ---------------------------------------------------------------------------


def test_cross_plan_decision_cannot_be_answered() -> None:
    """A decision asked by one plan cannot be answered through another.

    The HITL table is keyed by the plan that asked, so a decision that
    belongs to one plan is not found under another plan's id — the
    answer is a 404, not a 403 and not a silent success.  A question
    that was given up on is settled, so a late answer is a 409: the
    question was asked and nobody answered it, and a late answer must
    not land as if it were still open.
    """
    plan_a = "sec-plan-a"
    plan_b = "sec-plan-b"

    store = HitlStore(resolve_state_db_path())
    registry = WaitRegistry(plan_a, store=store)
    first = registry.create_approval_request(
        {"id": "approve-first", "wait": {"type": "human"}},
        NeedsApproval(
            question="批准合入 main？", command=["gh", "pr", "merge", "1"]
        ),
    )
    second = registry.create_approval_request(
        {"id": "approve-second", "wait": {"type": "human"}},
        NeedsApproval(
            question="批准执行破坏性操作？",
            command=["git", "reset", "--hard", "HEAD~1"],
        ),
    )

    first_id = first["decision_id"]
    second_id = second["decision_id"]

    # Another plan's route cannot answer this plan's question.
    response = send(
        TestClient(server.app),
        "POST",
        f"/api/hitl/{plan_b}/decisions/{first_id}/answer",
        {"option": "approve"},
    )
    assert response.status_code == 404, (
        f"another plan's route does not find this plan's question, got "
        f"{response.status_code}: {response.text}"
    )

    # The control: the question is still open, and its own plan's route
    # can answer it — so the 404 above was about the plan, not about
    # the question being closed.
    response = send(
        TestClient(server.app),
        "POST",
        f"/api/hitl/{plan_a}/decisions/{first_id}/answer",
        {"option": "approve"},
    )
    assert response.status_code == 200, (
        f"the question's own plan can answer it, got "
        f"{response.status_code}: {response.text}"
    )

    # Another plan's route cannot reach this plan's waiting item either.
    response = send(
        TestClient(server.app),
        "POST",
        extend_url(plan_b, "approve-first"),
        {"seconds": 60},
    )
    assert response.status_code == 404, (
        f"another plan's waiting item is not found, got "
        f"{response.status_code}: {response.text}"
    )

    # And a question that was given up on cannot be answered at all.
    registry.terminate("approve-second", operator="operator")

    response = send(
        TestClient(server.app),
        "POST",
        f"/api/hitl/{plan_a}/decisions/{second_id}/answer",
        {"option": "approve"},
    )
    assert response.status_code == 409, (
        f"a question that was given up on cannot be answered, got "
        f"{response.status_code}: {response.text}"
    )
    store.close()


# ---------------------------------------------------------------------------
# 6 — approval, not execution
# ---------------------------------------------------------------------------


def test_main_merge_and_destructive_actions_require_approval() -> None:
    """Three kinds of action become a pending question and nothing else.

    A merge into ``main``, a destructive command and a command that
    costs money are legal commands that need a human to say yes.  So
    they become a *pending row*: the executor does not spawn, the row
    carries no default answer, and the sweep that answers questions on
    the human's behalf leaves this one alone.
    """
    cases = (
        ("merge", ["gh", "pr", "merge", "42"], "批准合入 main？"),
        (
            "protected push",
            ["git", "push", "--force", "origin", "main"],
            "批准合入 main？",
        ),
        ("destructive", ["git", "reset", "--hard", "HEAD~1"], "批准执行破坏性操作？"),
        (
            "remote delete",
            ["gh", "api", "-X", "DELETE", "repos/o/r/releases/1"],
            "批准执行破坏性操作？",
        ),
        ("money", ["gh", "api", "/user/billing"], "批准执行付费操作？"),
    )

    for name, argv, question in cases:
        plan_id = f"sec-approval-{name.replace(' ', '-')}"
        book = make_spec([an_external_item("act", "执行一个动作", argv)])
        write_book(plan_id, book)
        frozen = freeze(load_spec(plan_dir(plan_id)))
        item = frozen.spec.items[0]

        spawned: List[Any] = []

        def recording_run(args: Any, **kwargs: Any) -> Any:
            spawned.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        runner = CommandRunner(frozen, workspace=plan_dir(plan_id), run=recording_run)
        outcome = runner.run_item(item)

        assert isinstance(outcome, NeedsApproval), (
            f"{name}: the executor asked instead of running, got "
            f"{type(outcome).__name__}"
        )
        assert outcome.question == question, (
            f"{name}: the question names what is being asked, got "
            f"{outcome.question!r}"
        )
        assert outcome.command == argv, (
            f"{name}: the question carries the argv verbatim"
        )
        assert not spawned, (
            f"{name}: the command ran before anybody approved it"
        )

        # The question is stored — pending, with no default answer, so
        # the sweep that answers questions for a human cannot answer
        # this one.
        store = HitlStore(resolve_state_db_path())
        try:
            registry = WaitRegistry(plan_id, store=store)
            record = registry.create_approval_request(
                {"id": "act", "wait": {"type": "human"}},
                outcome,
            )
        finally:
            store.close()

        decision_id = record["decision_id"]
        row = decision_row(decision_id)

        assert row is not None, f"{name}: the question was not stored"
        assert row.status == "pending", (
            f"{name}: the question is pending, got {row.status!r}"
        )
        assert row.answer is None, (
            f"{name}: nobody answered the question, got {row.answer!r}"
        )
        assert row.default_option is None, (
            f"{name}: the question carries no default answer, got "
            f"{row.default_option!r}"
        )
        assert row.mode == "strict", (
            f"{name}: the question is a strict one, got {row.mode!r}"
        )
        assert list(row.options) == ["approve", "reject"], (
            f"{name}: the question offers the two answers, got {row.options!r}"
        )

        # And the panel shows it as waiting — not as answered.
        rows = plan_waiting_rows(plan_id)
        assert [row["item_id"] for row in rows] == ["act"], (
            f"{name}: the question is a waiting row, got {rows!r}"
        )