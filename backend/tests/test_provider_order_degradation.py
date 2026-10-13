"""Failure classification for the provider chain, asked of CC Switch.

The bulk of the test matrix lives in ``backend/tests/test_provider_order.py``;
this file re-exports it so the verification ``test_command`` keeps
resolving, and pins the one property that file does not: **every way CC
Switch can fail to answer is classified and named**.

The operator-facing requirement is unchanged by the chain having one
source: each way of not getting an answer carries a distinct
``reason=<sentinel>``, and the raise carries the evidence behind it. A
resolver that answered "provider chain unavailable" for all of them
would leave an operator to re-run the request by hand to find out
whether the port is wrong, the service is down, or CC Switch is
refusing.

Nothing here degrades quietly. The one answer that stays soft is a 200
with a valid but empty ``providers`` list — CC Switch saying it has no
providers, which is an answer rather than a failure to answer.
"""

import json
import logging
import urllib.error

import pytest

from provider_order import (
    REASON_API_UNREACHABLE,
    REASON_BAD_PAYLOAD,
    REASON_HTTP_STATUS,
    REASON_NO_USABLE_NAME,
    ProviderOrderError,
    cache_clear,
    load_fallback_order,
)

# Re-export the bulk of the matrix from the canonical test module.
from backend.tests.test_provider_order import *  # noqa: F401,F403,E402


#: An authority nothing can be listening on, so the resolver fails the
#: way it does when CC Switch is closed.
API_DOWN = "http://127.0.0.1:1"


class _Response:
    """Stand-in for what ``urlopen`` hands back, status line included."""

    status = 200

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _errors(caplog):
    return [
        rec for rec in caplog.records
        if rec.name == "provider_order" and rec.levelno >= logging.ERROR
    ]


def _reasons(caplog):
    return {getattr(rec, "reason", None) for rec in _errors(caplog)}


def _stub_api(monkeypatch, *, body=None, exc=None):
    import provider_order

    def _urlopen(*args, **kwargs):
        if exc is not None:
            raise exc
        return _Response(body if body is not None else b"")

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)


# ---------------------------------------------------------------------------
# Each failure class gets its own sentinel
# ---------------------------------------------------------------------------


def test_connection_refused_is_reported_as_api_unreachable(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """Nothing listening: the service is not running.

    Distinct from a status or a bad body, both of which mean something
    *did* answer. An operator reading this reason knows to start CC
    Switch, not to go looking for a bad payload.
    """
    cache_clear()
    caplog.set_level(logging.ERROR, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(
        monkeypatch,
        exc=urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    )

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    assert excinfo.value.reason == REASON_API_UNREACHABLE
    assert REASON_API_UNREACHABLE in _reasons(caplog), (
        f"expected reason={REASON_API_UNREACHABLE!r}, got {_reasons(caplog)!r}"
    )


def test_timeout_is_reported_as_api_unreachable(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """A CC Switch that accepts the connection and never answers.

    Classified with the unreachable reason rather than a status: there
    is no status line to report, and "the port is not usable" is the
    fact an operator needs. The exception type is carried so a timeout
    stays distinguishable from a refused connection.
    """
    cache_clear()
    caplog.set_level(logging.ERROR, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(monkeypatch, exc=TimeoutError("timed out"))

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    assert excinfo.value.reason == REASON_API_UNREACHABLE
    assert "TimeoutError" in excinfo.value.detail, (
        f"the exception type is not in the detail: {excinfo.value.detail!r}"
    )


def test_not_found_is_reported_as_http_status(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """Something answered, and it was not the provider list.

    The most actionable status there is: the configured endpoint is
    wrong, or whatever is listening on that port is not CC Switch.
    """
    cache_clear()
    caplog.set_level(logging.ERROR, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(
        monkeypatch,
        exc=urllib.error.HTTPError("u", 404, "Not Found", {}, None),
    )

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    assert excinfo.value.reason == REASON_HTTP_STATUS
    assert "HTTP 404" in str(excinfo.value)
    assert REASON_API_UNREACHABLE not in _reasons(caplog), (
        "a status line means the service answered; labelling that "
        "'unreachable' sends the operator to the wrong place"
    )


def test_server_error_is_reported_as_http_status(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """Kept separate from "unreachable" because the operator's next move
    differs: the service is up and answering, so this is a version or
    routing mismatch rather than a CC Switch that is not running.
    """
    cache_clear()
    caplog.set_level(logging.ERROR, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(
        monkeypatch,
        exc=urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None),
    )

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    assert excinfo.value.reason == REASON_HTTP_STATUS
    assert REASON_HTTP_STATUS in _reasons(caplog), (
        f"expected reason={REASON_HTTP_STATUS!r}, got {_reasons(caplog)!r}"
    )


def test_unparseable_body_is_reported_as_bad_payload(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """A 200 whose body is not a provider list is its own class.

    The commonest shape of this is an unrelated service on the
    configured port, so the sentinel has to distinguish "the response
    was not what we asked for" from "the response was an error".
    """
    cache_clear()
    caplog.set_level(logging.ERROR, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(monkeypatch, body=b"<html>a proxy's idea of /api</html>")

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    assert excinfo.value.reason == REASON_BAD_PAYLOAD
    assert REASON_BAD_PAYLOAD in _reasons(caplog), (
        f"expected reason={REASON_BAD_PAYLOAD!r}, got {_reasons(caplog)!r}"
    )


def test_rows_without_usable_names_are_reported_as_no_usable_name(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """Rows arrived; none of them was one this reader can dispatch on.

    Distinct from an empty list, which is CC Switch saying it has no
    providers. Here something answered with entries, so the response
    shape has moved and that is a fault to report rather than a fleet
    of zero.
    """
    cache_clear()
    caplog.set_level(logging.ERROR, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(
        monkeypatch, body=json.dumps({"providers": [{"id": "x"}]}).encode()
    )

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    assert excinfo.value.reason == REASON_NO_USABLE_NAME
    assert REASON_NO_USABLE_NAME in _reasons(caplog), (
        f"expected reason={REASON_NO_USABLE_NAME!r}, got {_reasons(caplog)!r}"
    )


def test_an_empty_provider_list_is_not_a_failure(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """The one soft answer, and it stays soft.

    ``[]`` reads downstream as "this deployment configured no
    providers", which is a supported state that CC Switch reported
    successfully. Producing it from a hollow response would wipe the
    chain for the rest of the process's life, and raising would make a
    supported configuration an outage.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(monkeypatch, body=json.dumps({"providers": []}).encode())

    assert load_fallback_order() == []
    assert not _errors(caplog), (
        "an empty provider list is a successful answer; it must not be "
        f"logged as an error: {[r.getMessage() for r in _errors(caplog)]!r}"
    )


# ---------------------------------------------------------------------------
# The raise carries the evidence, and the sentinel set is closed
# ---------------------------------------------------------------------------


def test_the_error_names_the_url_that_was_asked(
    tmp_path, monkeypatch, real_cc_switch
):
    """An operator handed this can find the port to check.

    The whole value of failing loudly instead of answering from
    somewhere else is that the failure points at the thing to fix, so
    the URL the resolver actually asked for is in the report.
    """
    cache_clear()
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    _stub_api(monkeypatch, exc=urllib.error.HTTPError("u", 404, "Not Found", {}, None))

    with pytest.raises(ProviderOrderError) as excinfo:
        load_fallback_order()

    logs_message = excinfo.value.args[0]
    assert REASON_HTTP_STATUS in logs_message
    assert "HTTP 404" in logs_message
    assert API_DOWN in logs_message, (
        f"the message does not name the endpoint that was tried, which "
        f"is the fact an operator needs first: {logs_message!r}"
    )
    assert excinfo.value.url == f"{API_DOWN}/api/providers"


def test_the_failure_sentinel_set_is_closed():
    """No failure can be reported without a sentinel.

    Every raise carries a ``reason`` from this module. A hand-rolled one
    with a free-text reason would reach an operator as an un-greppable
    message, so the set is pinned here and the raise path is required to
    use only what is in it.
    """
    import inspect

    import provider_order

    known = {
        provider_order.REASON_HTTP_STATUS,
        provider_order.REASON_API_UNREACHABLE,
        provider_order.REASON_BAD_PAYLOAD,
        provider_order.REASON_NO_USABLE_NAME,
    }
    assert all(isinstance(r, str) and r for r in known)

    source = inspect.getsource(provider_order._chain_from_api)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("ProviderOrderError("):
            argument = stripped.split("(", 1)[1].split(",", 1)[0].strip()
            assert argument.startswith("REASON_"), (
                f"a failure raised without a named sentinel: {stripped!r}"
            )


def test_the_reason_sentinels_are_the_only_chain_reasons():
    """No orphan sentinel left behind by a removed failure class.

    A constant that no raise uses is a class of failure that can no
    longer happen but can still be named in a log line, which is how a
    report claims a distinction it no longer makes.
    """
    import inspect
    import re

    import provider_order

    source = inspect.getsource(provider_order)
    declared = set(re.findall(r"^REASON_(\w+) = ", source, re.MULTILINE))

    # Sentinels for the quota payload's own file read, which is a
    # different reader with a different failure surface.
    quota = {
        "FILE_MISSING", "PERMISSION_DENIED", "JSON_INVALID", "NOT_A_DICT",
    }
    chain = {name for name in declared if name not in quota}

    used = set(re.findall(r"\bREASON_(\w+)\b", source))
    assert chain <= used, (
        "chain sentinels nothing raises any more: "
        f"{sorted(chain - used)!r}"
    )
    assert used <= (chain | quota), (
        "a raise names a sentinel that is not declared: "
        f"{sorted(used - chain - quota)!r}"
    )


# ---------------------------------------------------------------------------
# The lru_cache contract
# ---------------------------------------------------------------------------


def test_lru_cache(tmp_path, monkeypatch, real_cc_switch):
    """Ten same-process calls produce exactly one question.

    The chain is cached per process so a dispatch storm does not become
    a request storm against the desktop app — and, symmetrically, so a
    reordering the operator makes is picked up by restarting the
    backend rather than never.
    """
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(request.full_url)
        body = json.dumps(
            {"providers": [{"id": "1", "name": "vendor-a-pro"}]}
        ).encode()
        return _Response(body)

    import provider_order

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)
    cache_clear()

    for _ in range(10):
        assert load_fallback_order() == ["vendor-a-pro"]

    assert len(calls) == 1, (
        f"expected exactly one question, got {len(calls)}"
    )
