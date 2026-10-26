"""Integration tests for the HITL delivery path: config → subprocess → cc-connect.

What is being pinned
--------------------
A human-in-the-loop decision is only useful if somebody is actually
asked, and the chain that asks one has three links: the plan's stored
hook (a *command*), the dispatcher that runs it, and the delivery CLI
that hands the message to ``cc-connect``.  Each link can break, and
each break has to show up in the report and in the audit mirror —
because "the message went out" and "the message could not go out" are
the difference between a person being asked and nobody being asked.

Four outcomes are pinned here, and they are the four the contract
names:

* **delivered** — the command ran, the transport ran, and both said
  so.  ``test_delivers_to_stub``
* **failed** — the command ran and the transport said no.  A non-zero
  exit code is a failure, never a delivery.
  ``test_nonzero_stub_is_failed``
* **skipped** — the command was never run, because the hook does not
  carry the field the transport needs.
  ``test_missing_command_is_skipped``
* **failed, not delivered** — the transport is not there at all.
  ``test_missing_binary_is_failed_not_delivered``

The last one is the negative control: a missing binary must never be
reported as a delivery.  A dispatcher that counted "the command ran"
as "the message arrived" would tell the caller that a human was asked
when nobody was asked anything — and the caller would then settle the
decision as if a person had replied.

Why a stub and not the real ``cc-connect``
------------------------------------------
The real one talks to a chat group, and a test that sends a decision
question to a real group is a test that spams a human.  The stub is a
``/bin/sh`` script that records the arguments and the message it was
given and then exits with a code the test chooses, so all four
outcomes are produced without a network and without a person.

The delivery CLI runs as a **real subprocess** — that is the point of
this file: the hook is a command, and a command that only works when
it is interpreted in-process is a command that does not work.  The
child inherits this test's working directory, which is not the project
root, so ``PYTHONPATH`` is pinned to the project root: without it
``python -m backend.hooks.session_send`` cannot import the package it
is supposed to run.

Test command::

    pytest backend/tests/integration/test_hitl_session_send_delivery.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The project root — the directory ``backend`` lives in.  It goes on
#: the child's ``PYTHONPATH``, because the child's working directory
#: is this test's, not the project's.
PROJECT_ROOT = BACKEND_DIR.parent

from hooks import HITL_CONFIG_FILENAME, write_for_plan  # noqa: E402
from hooks.dispatcher import AUDIT_FILENAME, dispatch  # noqa: E402

#: This is the integration lane: it spawns real subprocesses.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_stub(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """Write a stub ``cc-connect`` and return its path.

    The stub records the arguments it was given and the message it was
    given on stdin (both into ``$CAPTURE``), and then exits with
    ``exit_code`` — so a test chooses whether the transport accepted
    the delivery.

    It is a shell script rather than a Python one because the stub
    stands in for a binary: what the dispatcher must be able to run is
    a program, with no interpreter of its own to find.
    """
    stub = tmp_path / "cc-connect-stub"
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" >> \"$CAPTURE\"\n"
        "cat >> \"$CAPTURE\"\n"
        "exit {0}\n".format(exit_code),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _audit_entry(plan_dir: Path) -> dict:
    """The single audit line the dispatch appended, as a dict."""
    path = plan_dir / AUDIT_FILENAME
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(lines) == 1, "expected one audit line, got {0}".format(len(lines))
    record = json.loads(lines[0])
    assert len(record["entries"]) == 1, (
        "expected one audit entry, got {0}".format(len(record["entries"]))
    )
    return record["entries"][0]


def _plan_with_cmd_hook(plan_dir: Path) -> Path:
    """A plan whose origin produced a ``cmd`` hook, the way it really happens."""
    write_for_plan(
        plan_dir,
        {
            "source": "plan_creation",
            "session_id": "s-1",
            "destination": "grp-1",
        },
    )
    return plan_dir


# ---------------------------------------------------------------------------
# delivered
# ---------------------------------------------------------------------------


def test_delivers_to_stub(tmp_path, monkeypatch):
    """The whole chain runs, and the transport is what got the message.

    The plan's origin produced a command; the dispatcher ran it; the
    command ran the delivery CLI; the CLI ran the transport with the
    message on its stdin.  Every link reported success, so the report
    says ``delivered`` and the audit line says so too.
    """
    plan_dir = _plan_with_cmd_hook(tmp_path / "plan-delivered")
    stub = _write_stub(tmp_path)
    capture = tmp_path / "capture.txt"

    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    report = dispatch(
        "hitl_decision_created",
        {"decision_id": "d1", "question": "是否合入？"},
        plan_dir,
    )

    assert report.delivered == 1
    assert report.failed == 0
    assert report.skipped == 0

    entry = _audit_entry(plan_dir)
    assert entry["kind"] == "cmd"
    assert entry["outcome"] == "delivered"
    assert entry["exit_code"] == 0

    # The transport really was reached, and it was told where to send.
    recorded = capture.read_text(encoding="utf-8")
    assert recorded.splitlines()[:5] == ["send", "-p", "grp-1", "-s", "s-1"]
    assert "d1" in recorded


# ---------------------------------------------------------------------------
# failed
# ---------------------------------------------------------------------------


def test_nonzero_stub_is_failed(tmp_path, monkeypatch):
    """A transport that says no is a failure, not a delivery.

    The command ran and the transport refused; the exit code says so.
    Counting that as a delivery would tell the caller that a human was
    asked when the transport dropped the message.
    """
    plan_dir = _plan_with_cmd_hook(tmp_path / "plan-nonzero")
    stub = _write_stub(tmp_path, exit_code=3)
    capture = tmp_path / "capture.txt"

    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    report = dispatch(
        "hitl_decision_created",
        {"decision_id": "d1", "question": "是否合入？"},
        plan_dir,
    )

    assert report.delivered == 0
    assert report.failed == 1
    assert report.skipped == 1

    entry = _audit_entry(plan_dir)
    assert entry["kind"] == "cmd"
    assert entry["outcome"] == "failed"
    assert entry["exit_code"] == 3


# ---------------------------------------------------------------------------
# skipped
# ---------------------------------------------------------------------------


def test_missing_command_is_skipped(tmp_path, monkeypatch):
    """A ``cmd`` hook without a command is never attempted.

    The entry names the transport but not the thing the transport needs,
    so there is nothing to run: the destination is skipped, with the
    missing field named, and the transport is not touched at all.
    """
    plan_dir = tmp_path / "plan-missing-command"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / HITL_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 1800,
                "hooks": [{"kind": "cmd"}],
            }
        ),
        encoding="utf-8",
    )
    stub = _write_stub(tmp_path)
    capture = tmp_path / "capture.txt"

    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    report = dispatch(
        "hitl_decision_created",
        {"decision_id": "d1", "question": "是否合入？"},
        plan_dir,
    )

    assert report.delivered == 0
    assert report.failed == 0
    assert report.skipped == 1

    entry = _audit_entry(plan_dir)
    assert entry["kind"] == "cmd"
    assert entry["outcome"] == "skipped"
    assert entry["reason"] == "missing_field:command"

    # "Skipped" means "never attempted", not "attempted and did not land".
    assert not capture.exists()


# ---------------------------------------------------------------------------
# the negative control
# ---------------------------------------------------------------------------


def test_missing_binary_is_failed_not_delivered(tmp_path, monkeypatch):
    """A transport that is not installed is a failure — never a delivery.

    The command runs and the delivery CLI cannot find the transport it
    was told to use, so it exits non-zero.  The dispatcher must count
    that as a failure: the message did not go anywhere, and a report
    that said ``delivered`` would be a report that lied to the caller
    deciding whether a human was asked.
    """
    plan_dir = _plan_with_cmd_hook(tmp_path / "plan-missing-binary")
    capture = tmp_path / "capture.txt"

    monkeypatch.setenv("PDT_CC_CONNECT_BIN", "/nonexistent/cc-connect")
    monkeypatch.setenv("CAPTURE", str(capture))
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    report = dispatch(
        "hitl_decision_created",
        {"decision_id": "d1", "question": "是否合入？"},
        plan_dir,
    )

    assert report.delivered == 0
    assert report.failed == 1

    entry = _audit_entry(plan_dir)
    assert entry["kind"] == "cmd"
    assert entry["outcome"] == "failed"

    # Nothing was sent, because there was nothing to send it with.
    assert not capture.exists()
