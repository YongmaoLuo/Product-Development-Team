"""The notifier must satisfy the request guard when it calls its own API.

Why this exists
---------------
The notifier reads plan state over HTTP instead of importing the server,
so it is a *client* of the guarded ``/api/*`` surface. ``_get_json`` sent
only ``Accept: application/json``, and the guard answers **403** to a
request without ``X-PDT-Request``. ``_get_json`` maps ``HTTPError`` to
``None``, and every caller reads ``None`` as "no state yet, retry next
tick" — so the execution watch never observed a fingerprint change and
never refreshed a card.

The failure was silent by construction: a correctly-configured machine
with working credentials simply never pushed anything, and the only
evidence was a 403 in the access log.

These tests stub the API with a server that enforces the same rule, so
the contract is checked without depending on the real guard's internals.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from notifications.plan_dir_resolver import (  # noqa: E402
    fetch_execution_progress,
    fetch_plan_status,
)
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE  # noqa: E402


class _GuardedHandler(BaseHTTPRequestHandler):
    """Stands in for the guarded API: 403 unless the header is present."""

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        if self.headers.get(REQUEST_HEADER) != REQUEST_HEADER_VALUE:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"detail":"missing header"}')
            return
        body = json.dumps({"plan_id": "p", "tasks": [], "counts": {}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):  # silence the stub's access log
        pass


class _StubServer:
    def __enter__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _GuardedHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def test_execution_progress_reaches_a_guarded_api():
    """A 403 here means the notifier cannot see execution progress at all."""
    with _StubServer() as stub:
        result = fetch_execution_progress("p", base_url=stub.base)

    assert result is not None, (
        "fetch_execution_progress returned None against a guarded stub — "
        "the request is missing the guard header and is being answered 403"
    )
    assert result.get("plan_id") == "p"


def test_plan_status_reaches_a_guarded_api():
    with _StubServer() as stub:
        result = fetch_plan_status("p", base_url=stub.base)

    assert result is not None, (
        "fetch_plan_status returned None against a guarded stub; the card "
        "rebuild would treat the plan as unreadable"
    )


def test_the_stub_really_enforces_the_guard():
    """Guard the guard: if the stub stopped enforcing, the tests above
    would pass vacuously and prove nothing."""
    import urllib.error
    import urllib.request

    with _StubServer() as stub:
        req = urllib.request.Request(f"{stub.base}/api/execution/p/progress")
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as exc:
            raised = exc.code == 403
        assert raised, "the stub must answer 403 without the guard header"
