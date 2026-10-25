"""The verification branch's CLI: a thin wrapper over the API.

Why this suite exists
---------------------
The verification branch is entered through HTTP endpoints — the plan-doc
endpoints of tasks 17-21 and the ``/start`` of task 22 — and the CLI is
the E2E-1 entry point into the same branch. A CLI that is *equivalent* to
the API is equivalent only if it forwards the request faithfully and
reports the server's answer faithfully. This suite pins both halves:

* **The request is the endpoint's request.** The method, the path and the
  body are asserted against what the endpoint declares, so a CLI that
  posts to the wrong path, or sends ``status`` where the endpoint reads
  ``action``, fails here rather than on the first real run against a
  plan somebody spent a day on.
* **The server's answer is the CLI's answer.** The exit code is zero only
  for a 2xx, and the server's machine-readable error code reaches the
  operator's terminal. A CLI that prints a success line for a 404 is
  worse than no CLI at all: it makes a failed plan look reviewed.

Why unit
--------
Nothing here opens a socket. ``urlopen`` is replaced by a recorder that
answers from a table, so the assertions are about what the CLI *sent* and
what it did with what came back — the two things that make it a wrapper
rather than a second implementation of the branch.

The recorder replaces ``urlopen`` rather than the CLI's own request
helper on purpose: the URL, the method and the JSON body are part of
what is being tested, and a fake placed above the HTTP layer would let
all three drift without a single failure here.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error

import pytest

import cli


#: Where the recorder's server "lives". Every URL the CLI builds is
#: asserted against this prefix, so a change of base URL is a visible
#: change rather than a silent one.
API_BASE = "http://127.0.0.1:8000"


class _FakeResponse:
    """The minimal response object ``urlopen`` hands back."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Recorder:
    """Stands in for ``urlopen``: records the request, answers from a table.

    The answer table is keyed by ``"METHOD /path"`` — the same spelling
    the assertions use — so a test says what the server should answer
    and the recorder says it back without the test having to spell out a
    route table of its own.
    """

    def __init__(self, answers=None):
        self.requests = []
        self.answers = dict(answers or {})

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        method = request.get_method()
        path = request.full_url
        if path.startswith(API_BASE):
            path = path[len(API_BASE):]
        status, body = self.answers.get(f"{method} {path}", (200, {}))
        raw = json.dumps(body).encode("utf-8")
        if status >= 400:
            # A real 4xx/5xx arrives as an exception, not as a response
            # object; a recorder that returned it as a 200-shaped object
            # would test a code path the CLI never takes.
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {}, io.BytesIO(raw)
            )
        return _FakeResponse(status, raw)

    def body_of(self, request) -> dict:
        """Return the JSON body the CLI sent with ``request``."""
        assert request.data is not None, "this request carried no body"
        return json.loads(request.data.decode("utf-8"))


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    """Point the CLI at a recorder instead of a socket."""
    rec = _Recorder()
    monkeypatch.setenv("PDT_API_BASE_URL", API_BASE)
    monkeypatch.setattr(cli.urllib.request, "urlopen", rec)
    return rec


def run_cli(monkeypatch, capsys, argv):
    """Run the CLI with ``argv``; return ``(exit_code, stdout, stderr)``."""
    monkeypatch.setattr(sys, "argv", ["autonomous-coding"] + list(argv))
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# generate: the plan-doc entry point
# ---------------------------------------------------------------------------


def test_generate_subcommand_calls_endpoint(recorder, monkeypatch, capsys):
    """``verification-plan generate p1`` posts to the generate endpoint.

    The answer is printed as it came back, with the command named: a
    script driving several plans in a row has to be able to tell whose
    answer it is holding.
    """
    recorder.answers["POST /api/verification-plan/p1/generate"] = (200, {
        "plan_id": "p1",
        "document_path": "verification-plan.md",
        "current_phase": "verification_plan_review",
    })

    code, out, err = run_cli(
        monkeypatch, capsys, ["verification-plan", "generate", "p1"]
    )

    assert code == 0, err
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"{API_BASE}/api/verification-plan/p1/generate"

    printed = json.loads(out)
    assert printed["command"] == "verification-plan generate"
    assert printed["plan_id"] == "p1"
    # The phase is the endpoint's answer, forwarded untouched: the CLI
    # does not know what phases exist, and must not pretend to.
    assert printed["current_phase"] == "verification_plan_review"


# ---------------------------------------------------------------------------
# review: the per-item action
# ---------------------------------------------------------------------------


def test_review_subcommand_passes_action_and_note(recorder, monkeypatch, capsys):
    """``--status skip --note ...`` reaches the endpoint as action + note.

    The flag is ``--status`` and the body's field is ``action`` — the
    endpoint's own name for it. The CLI translates the flag, and does
    not re-validate it: the action vocabulary belongs to the server, and
    a second copy here is a second thing that can be right while the
    server is wrong.
    """
    recorder.answers["POST /api/verification-plan/p1/review/item/3"] = (200, {
        "items": [],
        "doc_sha256": "sha-3",
        "resolved": True,
    })

    code, out, err = run_cli(
        monkeypatch,
        capsys,
        [
            "verification-plan", "review", "p1",
            "--item", "3",
            "--status", "skip",
            "--note", "无服务",
        ],
    )

    assert code == 0, err
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"{API_BASE}/api/verification-plan/p1/review/item/3"

    body = recorder.body_of(request)
    assert body["action"] == "skip"
    assert body["note"] == "无服务"

    printed = json.loads(out)
    assert printed["command"] == "verification-plan review"
    assert printed["plan_id"] == "p1"


# ---------------------------------------------------------------------------
# verify: the start of the verification run
# ---------------------------------------------------------------------------


def test_verify_subcommand_starts_verification(recorder, monkeypatch, capsys):
    """``verification verify p1`` posts to the start endpoint.

    The body is empty on purpose: the endpoint owns its defaults (round
    budget, auto-fix), and a CLI that filled them in would be pinning
    values the operator never asked for.
    """
    recorder.answers["POST /api/verification/p1/start"] = (200, {
        "status": "started",
        "project_dir": "verification-workspaces/p1",
    })

    code, out, err = run_cli(monkeypatch, capsys, ["verification", "verify", "p1"])

    assert code == 0, err
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"{API_BASE}/api/verification/p1/start"

    printed = json.loads(out)
    assert printed["command"] == "verification verify"
    assert printed["plan_id"] == "p1"
    assert printed["status"] == "started"


# ---------------------------------------------------------------------------
# failures: an unknown plan, an unknown item
# ---------------------------------------------------------------------------


def test_invalid_plan_exits_nonzero(recorder, monkeypatch, capsys):
    """A 404 is a nonzero exit and a printed error — never a success line.

    The server's error code is printed, because that is the string the
    server's own documentation and the API's own responses use; an
    operator grepping a CI log for ``plan_not_found`` has to find it.
    """
    recorder.answers["POST /api/verification-plan/p1/generate"] = (404, {
        "detail": {
            "error": "plan_not_found",
            "detail": "Plan not found",
            "plan_id": "p1",
        },
    })

    code, out, err = run_cli(
        monkeypatch, capsys, ["verification-plan", "generate", "p1"]
    )

    assert code != 0
    assert "plan_not_found" in err
    assert out == ""


def test_invalid_item_exits_nonzero(recorder, monkeypatch, capsys):
    """An item that does not exist is a failure, and says which one."""
    recorder.answers["POST /api/verification-plan/p1/review/item/99"] = (404, {
        "detail": {
            "error": "item_not_found",
            "detail": "审核条目序号不存在：99",
            "plan_id": "p1",
            "item_index": 99,
        },
    })

    code, out, err = run_cli(
        monkeypatch,
        capsys,
        [
            "verification-plan", "review", "p1",
            "--item", "99",
            "--status", "accept",
        ],
    )

    assert code != 0
    assert "item_not_found" in err


def test_unreachable_server_exits_nonzero(monkeypatch, capsys):
    """A server that cannot be reached is a failure, not a stack trace."""
    def _refuse(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setenv("PDT_API_BASE_URL", API_BASE)
    monkeypatch.setattr(cli.urllib.request, "urlopen", _refuse)

    code, out, err = run_cli(
        monkeypatch, capsys, ["verification-plan", "get", "p1"]
    )

    assert code != 0
    assert "error" in err.lower()


# ---------------------------------------------------------------------------
# the rest of the vocabulary
# ---------------------------------------------------------------------------


def test_get_and_items_read_the_plan_and_its_review_items(
    recorder, monkeypatch, capsys
):
    """``get`` reads the document, ``items`` reads the review entries."""
    recorder.answers["GET /api/verification-plan/p1"] = (200, {
        "content": "# 计划书\n",
        "structure_errors": [],
        "baseline": {"repo_dir": "repo", "ref": "abc", "pr_number": 7},
    })
    recorder.answers["GET /api/verification-plan/p1/review/items"] = (200, {
        "items": [{"index": 0, "id": "PLAN-OBJECT", "status": "pending"}],
        "doc_sha256": "sha-1",
    })

    code, out, err = run_cli(monkeypatch, capsys, ["verification-plan", "get", "p1"])
    assert code == 0, err
    assert recorder.requests[0].get_method() == "GET"
    assert recorder.requests[0].full_url == f"{API_BASE}/api/verification-plan/p1"
    printed = json.loads(out)
    assert printed["command"] == "verification-plan get"
    assert printed["content"] == "# 计划书\n"

    code, out, err = run_cli(
        monkeypatch, capsys, ["verification-plan", "items", "p1"]
    )
    assert code == 0, err
    assert recorder.requests[1].get_method() == "GET"
    assert recorder.requests[1].full_url == (
        f"{API_BASE}/api/verification-plan/p1/review/items"
    )
    printed = json.loads(out)
    assert printed["command"] == "verification-plan items"
    assert printed["doc_sha256"] == "sha-1"


def test_refine_subcommand_passes_feedback(recorder, monkeypatch, capsys):
    """``refine`` sends the feedback and nothing else."""
    recorder.answers["POST /api/verification-plan/p1/refine"] = (200, {
        "document_path": "verification-plan.md",
        "doc_sha256": "sha-2",
        "items": [],
    })

    code, out, err = run_cli(
        monkeypatch,
        capsys,
        ["verification-plan", "refine", "p1", "--feedback", "把第三节写细"],
    )

    assert code == 0, err
    request = recorder.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"{API_BASE}/api/verification-plan/p1/refine"
    assert recorder.body_of(request) == {"feedback": "把第三节写细"}
    assert json.loads(out)["command"] == "verification-plan refine"


def test_unlisted_subcommand_exits_nonzero(monkeypatch, capsys):
    """A subcommand that is not in the vocabulary is refused.

    The vocabulary is fixed — ``verification-plan generate|get|items|
    review|refine`` and ``verification verify`` — so anything else has to
    fail loudly rather than fall through to the autonomous path and be
    read as a requirement.
    """
    code, out, err = run_cli(
        monkeypatch, capsys, ["verification-plan", "frobnicate", "p1"]
    )
    assert code != 0

    code, out, err = run_cli(monkeypatch, capsys, ["verification", "stop", "p1"])
    assert code != 0


def test_every_request_carries_the_servers_guard_header(
    recorder, monkeypatch, capsys
):
    """The CLI's requests are ones the server's guard lets through.

    Every ``/api/*`` call must carry the guard's custom header, or the
    server answers 403 — a CLI that omitted it would be refused by every
    call it ever made, and the operator would be reading that as "the
    API is down". The header name and value come from the guard module
    rather than from a second spelling here, so a rename cannot leave
    this CLI behind.
    """
    code, out, err = run_cli(
        monkeypatch, capsys, ["verification-plan", "get", "p1"]
    )
    assert code == 0, err
    request = recorder.requests[0]
    assert request.get_header("X-pdt-request") == "1"


def test_base_url_follows_the_environment(monkeypatch):
    """The server's address is the operator's to choose.

    ``PDT_API_BASE_URL`` wins outright; otherwise the port comes from
    ``PDT_PORT`` — the same variable the server binds with — and the host
    is loopback, which is where the server binds by default.
    """
    monkeypatch.setenv("PDT_API_BASE_URL", "http://example.test:9000/")
    assert cli._api_base_url() == "http://example.test:9000"

    monkeypatch.delenv("PDT_API_BASE_URL")
    monkeypatch.setenv("PDT_PORT", "18090")
    assert cli._api_base_url() == "http://127.0.0.1:18090"

    monkeypatch.delenv("PDT_PORT")
    assert cli._api_base_url() == "http://127.0.0.1:8000"
