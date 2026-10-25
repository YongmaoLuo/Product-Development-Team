"""Tests for ``hooks.dispatcher`` — the plan's event fan-out.

What is being pinned
--------------------
A plan's ``hooks`` array is a fan-out: every destination is a
first-class target, and the ones that are broken must not take the
others down with them.  Four properties carry the weight, and each of
them is a way the "one bad destination degrades the whole plan" rule
can be broken by accident:

* **The destinations are independent.**  A destination that hangs has
  its own timeout and holds up none of the others.  ``test_fanout_independent``
  pins this without a wall-clock assertion: the first destination
  waits for a marker that only the second one writes, so "both were
  delivered" is only possible when the fan-out really is parallel.
* **The payload is data, never a command.**  ``test_cmd_payload_via_stdin``
  puts shell metacharacters in the payload and asserts that the
  sentinel they would have created does not exist, while the bytes the
  command received on stdin are the payload verbatim.
* **A destination that cannot be delivered to is skipped, and the
  others still are.**  ``test_invalid_entries_skipped`` covers the
  unknown transport, the missing field and the transport that is not
  implemented yet.
* **Nothing is retried.**  ``test_no_retry`` counts the attempts a
  timed-out destination made: exactly one.

The report and the audit line are pinned alongside them, because a
caller decides "unroutable" from ``delivered`` and an operator reads
the audit line when the caller says something went wrong.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.config import HITL_CONFIG_FILENAME  # noqa: E402
from hooks.dispatcher import (  # noqa: E402
    AUDIT_FILENAME,
    DispatchReport,
    dispatch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_hooks(plan_dir: Path, hooks) -> Path:
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


def _script(path: Path, source: str) -> Path:
    """Write a small helper script the ``cmd`` transport can run."""
    path.write_text(source, encoding="utf-8")
    return path


def _cmd(*parts: str) -> str:
    """A command string with every part quoted."""
    return " ".join(f'"{part}"' for part in parts)


def _url(server) -> str:
    """The URL of the local recording receiver."""
    return f"http://127.0.0.1:{server.server_address[1]}/hook"


class _RecordingHandler(BaseHTTPRequestHandler):
    """A receiver that records what it was sent."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - the name is BaseHTTPRequestHandler's
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.received.append({"path": self.path, "body": body})
        payload = b"ok"
        self.send_response(int(getattr(self.server, "status", 200)))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # noqa: D102 - silence the access log
        return


@pytest.fixture
def http_receiver():
    """A local HTTP receiver that records the bodies it is sent."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.received = []
    server.status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The destinations are independent
# ---------------------------------------------------------------------------


def test_fanout_independent(tmp_path):
    """A destination that is still busy must not hold the others up.

    The first destination waits for a marker that only the second one
    writes.  Were the destinations dispatched one after another, the
    first would wait out its whole window, fail, and only then would
    the second run — so "both were delivered" is only possible when
    the fan-out is parallel.  The assertion needs no wall clock: the
    marker is either written in time or it is not.
    """
    plan_dir = tmp_path / "plan"
    marker = tmp_path / "fast.done"

    waiter = _script(
        tmp_path / "wait_for.py",
        "import os, sys, time\n"
        "deadline = time.time() + float(sys.argv[2])\n"
        "while time.time() < deadline:\n"
        "    if os.path.exists(sys.argv[1]):\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.02)\n"
        "sys.exit(1)\n",
    )
    writer = _script(
        tmp_path / "write_marker.py",
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('fast', encoding='utf-8')\n",
    )

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _cmd(sys.executable, waiter, marker, "10"),
                "timeout_sec": 30,
            },
            {
                "kind": "cmd",
                "command": _cmd(sys.executable, writer, marker),
                "timeout_sec": 30,
            },
        ],
    )

    report = dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    assert report.delivered == 2
    assert report.timed_out == 0
    assert report.skipped == 0
    assert marker.exists()


def test_timed_out_destination_does_not_stop_the_others(tmp_path):
    """A destination that hangs is abandoned; the others still land."""
    plan_dir = tmp_path / "plan"
    marker = tmp_path / "fast.done"

    writer = _script(
        tmp_path / "write_marker.py",
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('fast', encoding='utf-8')\n",
    )

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _cmd(sys.executable, "-c", "import time; time.sleep(30)"),
                "timeout_sec": 1,
            },
            {
                "kind": "cmd",
                "command": _cmd(sys.executable, writer, marker),
                "timeout_sec": 30,
            },
        ],
    )

    report = dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    assert report.timed_out == 1
    assert report.delivered == 1
    assert marker.exists()


# ---------------------------------------------------------------------------
# The payload is data, never a command
# ---------------------------------------------------------------------------


def test_cmd_payload_via_stdin(tmp_path):
    """The payload reaches the command on stdin, and only on stdin.

    The payload carries the metacharacters a command injection needs.
    The command is a fixed script that writes its stdin to a file, so
    the two assertions together distinguish "the payload was passed
    safely" from "the command never ran": the sentinel the payload
    would have created must not exist, and the file the command wrote
    must hold the payload verbatim.
    """
    plan_dir = tmp_path / "plan"
    received = tmp_path / "received.json"
    sentinel = tmp_path / "sentinel"

    receiver = _script(
        tmp_path / "receive.py",
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n",
    )

    payload = {
        "decision_id": "d1",
        "question": f"; touch {sentinel}",
        "options": [
            f"$(touch {sentinel})",
            f"`touch {sentinel}`",
            f"| touch {sentinel}",
            f"\ntouch {sentinel}",
        ],
    }

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _cmd(sys.executable, receiver, received),
                "timeout_sec": 30,
            }
        ],
    )

    report = dispatch("hitl_decision_created", payload, plan_dir)

    assert report.delivered == 1
    assert not sentinel.exists(), "the payload reached the command line"
    expected = json.dumps({"event": "hitl_decision_created", **payload})
    assert received.read_text(encoding="utf-8") == expected


# ---------------------------------------------------------------------------
# A destination that cannot be delivered to
# ---------------------------------------------------------------------------


def test_invalid_entries_skipped(tmp_path, http_receiver):
    """An undeliverable destination is skipped; the others still are."""
    plan_dir = tmp_path / "plan"

    _write_hooks(
        plan_dir,
        [
            {"kind": "sms", "destination": "x"},  # not a transport at all
            {"kind": "http"},  # no url to post to
            {"kind": "cmd"},  # no command to run
            {"kind": "telegram", "destination": "grp-1"},  # not implemented yet
            {"kind": "http", "url": _url(http_receiver)},
        ],
    )

    report = dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    assert report.delivered == 1
    assert report.timed_out == 0
    assert report.skipped == 4
    assert len(http_receiver.received) == 1
    body = json.loads(http_receiver.received[0]["body"].decode("utf-8"))
    assert body["event"] == "hitl_decision_created"
    assert body["decision_id"] == "d1"


# ---------------------------------------------------------------------------
# Nothing is retried
# ---------------------------------------------------------------------------


def test_no_retry(tmp_path):
    """A destination is attempted exactly once, even when it hangs."""
    plan_dir = tmp_path / "plan"
    attempts = tmp_path / "attempts.log"

    sleeper = _script(
        tmp_path / "sleep_once.py",
        "import sys, time\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
        "    handle.write('attempt\\n')\n"
        "time.sleep(float(sys.argv[2]))\n",
    )

    _write_hooks(
        plan_dir,
        [
            {
                "kind": "cmd",
                "command": _cmd(sys.executable, sleeper, attempts, "30"),
                "timeout_sec": 1,
            }
        ],
    )

    report = dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    assert report.timed_out == 1
    assert report.delivered == 0
    assert attempts.read_text(encoding="utf-8").splitlines() == ["attempt"]


# ---------------------------------------------------------------------------
# The report and the audit mirror
# ---------------------------------------------------------------------------


def test_no_hooks_delivers_nothing(tmp_path):
    """A plan with no destinations has nothing to deliver, and says so."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    report = dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    assert report == DispatchReport(delivered=0, timed_out=0, skipped=0)
    # The event still happened, so it is still recorded: "this was
    # dispatched to nobody" is the fact an operator needs.
    audit = (plan_dir / AUDIT_FILENAME).read_text(encoding="utf-8")
    assert len(audit.strip().splitlines()) == 1
    assert json.loads(audit)["delivered"] == 0


def test_audit_line_is_appended_per_event(tmp_path, http_receiver):
    """One line per event, in the order the events were dispatched."""
    plan_dir = tmp_path / "plan"
    _write_hooks(plan_dir, [{"kind": "http", "url": _url(http_receiver)}])

    dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)
    dispatch("plan_failed", {"reason": "boom"}, plan_dir)

    lines = (plan_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["event"] == "hitl_decision_created"
    assert first["delivered"] == 1
    assert second["event"] == "plan_failed"
    assert second["delivered"] == 1
    assert len(http_receiver.received) == 2


def test_audit_line_records_the_target_without_its_credential(tmp_path):
    """A token in a webhook URL must not become a token in the plan dir."""
    plan_dir = tmp_path / "plan"
    secret = "supersecret-1"
    _write_hooks(
        plan_dir,
        [{"kind": "http", "url": f"http://127.0.0.1:9/hook?token={secret}"}],
    )

    dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    audit = (plan_dir / AUDIT_FILENAME).read_text(encoding="utf-8")
    assert secret not in audit
    assert "<redacted>" in audit


def test_http_non_2xx_is_a_failure(tmp_path, http_receiver):
    """A destination that answers with an error has not been delivered to."""
    plan_dir = tmp_path / "plan"
    http_receiver.status = 500
    _write_hooks(plan_dir, [{"kind": "http", "url": _url(http_receiver)}])

    report = dispatch("hitl_decision_created", {"decision_id": "d1"}, plan_dir)

    assert report.delivered == 0
    assert report.timed_out == 0
    assert report.skipped == 1
    assert report.failed == 1
    # One attempt, not two: the failure is not retried either.
    assert len(http_receiver.received) == 1
