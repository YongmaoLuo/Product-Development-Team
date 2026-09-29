"""TDD tests for ``tests.integration.resume_test.api_client.ApiClient``.

These tests pin the 4 contracts documented in the resume test spec:

  1. ``test_init_defaults`` — constructor stores ``base_url`` and a
     default ``timeout`` of 10 seconds (overridable).
  2. ``test_get_returns_parsed_json`` — ``urlopen`` returning JSON is
     parsed into a dict.
  3. ``test_connection_error_propagates`` — ``URLError`` (e.g. ECONNREFUSED)
     surfaces as ``ConnectionError``.
  4. ``test_http_error_includes_body`` — a 5xx response from the server
     raises our ``HTTPError`` carrying both ``status_code`` and ``body``.

All tests use ``unittest.mock`` to patch ``urllib.request.urlopen`` so
they do NOT need a running the backend.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

# ``urllib.error.HTTPError`` is the natural sentinel for ``urlopen``
# raising on a 4xx / 5xx response.
import urllib.error

# The SUT module lives in the same package as this test module, so we
# add its parent directory (``backend/tests/integration``) to sys.path
# and import ``resume_test.api_client`` directly. This is robust to
# the rootdir / pythonpath quirks that arise when both project-root
# and backend-level pytest.ini files coexist.
_INTEGRATION_DIR = Path(__file__).resolve().parent.parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from resume_test.api_client import (
    ApiClient,
    ConnectionError,
    HTTPError,
)


def _mock_urlopen_returning(body: bytes, content_type: str = "application/json"):
    """Build a ``mock.patch`` target whose ``urlopen`` returns ``body``.

    The returned object is a context manager (``__enter__`` returns a
    file-like response with ``.read()`` returning ``body``).
    """

    class _Resp:
        def __init__(self, raw: bytes):
            self._raw = raw

        def read(self) -> bytes:
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    resp = _Resp(body)
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def test_init_defaults():
    """Default base_url + timeout are stored as instance attributes."""
    client = ApiClient("http://127.0.0.1:8000")
    assert client.base_url == "http://127.0.0.1:8000"
    # Spec: "默认超时 10 秒". We accept 10.0 exactly or as int — the
    # implementation stores it as float, so we compare as float.
    assert float(client.timeout) == 10.0

    # Trailing slash on base_url is normalised away so subsequent
    # joins never produce double-slash URLs.
    client2 = ApiClient("http://127.0.0.1:8000/")
    assert client2.base_url == "http://127.0.0.1:8000"

    # Explicit timeout is honoured.
    client3 = ApiClient("http://127.0.0.1:8000", timeout=3.5)
    assert float(client3.timeout) == 3.5


def test_get_returns_parsed_json():
    """Mock urlopen returning JSON → client.get returns a dict."""
    payload = {
        "plan_id": "plan-xyz",
        "state": {"current_phase": "executing"},
        "execution": {"status": "running"},
    }
    raw = json.dumps(payload).encode("utf-8")
    urlopen_cm = _mock_urlopen_returning(raw)

    client = ApiClient("http://127.0.0.1:8000")
    with mock.patch("urllib.request.urlopen", return_value=urlopen_cm) as m_urlopen:
        result = client.get_plan_summary("plan-xyz")

    # Returned value is the parsed JSON, not raw bytes or text.
    assert isinstance(result, dict)
    assert result == payload
    # urlopen was invoked exactly once with the expected URL.
    assert m_urlopen.call_count == 1
    call_args = m_urlopen.call_args
    # ``call_args.args[0]`` is the Request object; ``call_args.kwargs``
    # carries ``timeout``.
    assert "timeout" in call_args.kwargs
    assert float(call_args.kwargs["timeout"]) == 10.0
    request_obj = call_args.args[0]
    assert request_obj.full_url == "http://127.0.0.1:8000/api/plan/plan-xyz/summary"
    assert request_obj.get_method() == "GET"


def test_connection_error_propagates():
    """``URLError`` (e.g. connection refused) → ``ConnectionError``."""
    client = ApiClient("http://127.0.0.1:8000")

    # ``URLError`` is the umbrella exception; ``ConnectionRefusedError``
    # is a sibling under ``OSError``, but ``URLError`` itself is what
    # ``urlopen`` raises for socket-level failures (the OSError is
    # set as ``.reason``).
    url_err = urllib.error.URLError("[Errno 61] Connection refused")

    with mock.patch(
        "urllib.request.urlopen", side_effect=url_err
    ) as m_urlopen:
        raised: Exception
        try:
            client.get_plan_summary("any-plan")
        except ConnectionError as exc:
            raised = exc
        else:
            raise AssertionError(
                "expected ConnectionError, but no exception was raised"
            )

    # The exception class is exactly ``ConnectionError`` from our
    # module — not the builtin ``ConnectionError``.
    assert type(raised) is ConnectionError
    assert m_urlopen.call_count == 1
    # Original URLError is preserved via ``__cause__`` (from ``raise X from exc``).
    assert raised.__cause__ is url_err


def test_http_error_includes_body():
    """A 500 response → ``HTTPError`` with status_code + body."""
    client = ApiClient("http://127.0.0.1:8000")

    error_body_text = '{"error": "internal server error", "trace_id": "abc"}'
    # ``HTTPError`` doubles as a file-like object — ``.read()`` yields
    # the response body. We instantiate it with a real HTTP code.
    http_err = urllib.error.HTTPError(
        url="http://127.0.0.1:8000/api/plan/boom/summary",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(error_body_text.encode("utf-8")),
    )

    with mock.patch(
        "urllib.request.urlopen", side_effect=http_err
    ) as m_urlopen:
        raised: Exception
        try:
            client.get_plan_summary("boom")
        except HTTPError as exc:
            raised = exc
        else:
            raise AssertionError(
                "expected HTTPError, but no exception was raised"
            )

    assert type(raised) is HTTPError
    assert raised.status_code == 500
    assert raised.body == error_body_text
    assert m_urlopen.call_count == 1
    # Original ``urllib.error.HTTPError`` is preserved via ``__cause__``.
    assert raised.__cause__ is http_err