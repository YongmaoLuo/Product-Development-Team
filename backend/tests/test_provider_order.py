"""``provider_order.load_fallback_order`` asks CC Switch for the chain.

What this module pins is the shape of that question. There is one
producer (CC Switch), one question, and one place the answer can come
from, so the cases are the two halves of that: what a good answer looks
like, and what happens to every way of not getting one.

The cases, in the order they matter:

1. **CC Switch answers** — the chain is what it served, in the order it
   served it, unfiltered.
2. **Every failure raises** — 4xx, 5xx, refused, DNS, TLS, timeout,
   non-JSON, non-UTF-8, a root that is not an object, ``providers`` that
   is not a list, and a list whose entries carry no usable name. Each
   one raises with a ``reason`` naming it and a ``detail`` quoting the
   evidence. There is nothing to retreat to, so nothing retreats.
3. **One answer stays soft** — 200 with a valid but empty ``providers``
   list. That is CC Switch saying it has no providers, which is an
   answer rather than a failure to answer one, so it returns ``[]``.
4. **Nothing else answers** — the deleted database reader, asserted
   absent from both the source and ``cc_switch``, so the quiet second
   road cannot come back unnoticed.

Stubbing convention: ``real_cc_switch`` puts the session's stub aside so
the genuine resolver runs; ``_stub_transport`` then replaces only the
network, which leaves URL building, status handling and body parsing
exercised for real.

No test here touches ``~/.cc-switch``. The chain does not read it, and
``cc_switch`` opens that store ``mode=ro`` — this suite must not be the
thing that reaches into a developer's own install.
"""

from __future__ import annotations

import inspect
import json
import logging
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

import pytest

import cc_switch
import provider_order
from provider_order import (
    REASON_API_UNREACHABLE,
    REASON_BAD_PAYLOAD,
    REASON_HTTP_STATUS,
    REASON_NO_USABLE_NAME,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_chain_cache():
    """The chain is cached once per process; every test starts clean."""
    provider_order.cache_clear()
    yield
    provider_order.cache_clear()


class _Response:
    """Minimal stand-in for what ``urlopen`` hands back."""

    def __init__(self, payload=None, status=200, body=None):
        self.status = status
        if body is not None:
            self._body = body
        else:
            self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def getcode(self):  # pragma: no cover - the .status branch wins
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _api_payload(names):
    return {"providers": [{"id": f"id-{i}", "name": n} for i, n in enumerate(names)]}


def _stub_transport(monkeypatch, response=None, exc=None):
    """Replace the network, leaving both real legs in place.

    Pair with the ``real_cc_switch`` fixture. Everything under the HTTP
    leg — URL building, status handling, body parsing — then runs for
    real.
    """
    def _urlopen(*args, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)


def _install_api(monkeypatch, names):
    """Make the resolver report ``names``, whatever the network says."""
    monkeypatch.setattr(provider_order, "_chain_from_api", lambda: list(names))
    provider_order.cache_clear()


# ---------------------------------------------------------------------------
# 1. CC Switch answers — the chain is what it served
# ---------------------------------------------------------------------------


def test_chain_is_the_served_order_verbatim(monkeypatch, real_cc_switch):
    """The array order CC Switch sends IS the chain.

    Re-sorting it here could only disagree with the application the
    operator is looking at, so this asserts the payload order survives
    untouched — including a chain whose order a sort would have changed.
    """
    order = ["Zeta", "Alpha", "Mid", "Beta"]
    _stub_transport(monkeypatch, _Response(_api_payload(order)))

    assert provider_order.load_fallback_order() == order


def test_chain_is_not_filtered_against_the_local_roster(monkeypatch, real_cc_switch):
    """The served answer stands on its own.

    A name CC Switch served is in the chain even when this machine has
    no CC Switch row for it. Dropping it here would make the chain a
    function of the local store as well as of the service — and would
    drop precisely the newest provider, the one the application has and
    the local database has not seen yet. Resolving a name to an endpoint
    is the consumer's job; ``server.load_providers`` already reports and
    skips the ones that do not resolve.
    """
    _stub_transport(monkeypatch, _Response(_api_payload(["Only-On-The-Wire"])))

    assert provider_order.load_fallback_order() == ["Only-On-The-Wire"]


def test_names_are_deduplicated_keeping_first_occurrence(monkeypatch, real_cc_switch):
    """A repeated name is one provider, and its first slot wins."""
    payload = {
        "providers": [
            {"name": "A"},
            {"name": "B"},
            {"name": "A"},
        ]
    }
    _stub_transport(monkeypatch, _Response(payload))

    assert provider_order.load_fallback_order() == ["A", "B"]


@pytest.mark.parametrize(
    "row",
    [
        pytest.param("a bare string", id="not-an-object"),
        pytest.param({"id": "x"}, id="no-name"),
        pytest.param({"name": ""}, id="empty-name"),
        pytest.param({"name": 7}, id="non-string-name"),
    ],
)
def test_unusable_entries_are_skipped_not_fatal(monkeypatch, real_cc_switch, row):
    """A malformed row is skipped; a chain with one good row still lands."""
    _stub_transport(
        monkeypatch, _Response({"providers": [row, {"name": "Good"}]})
    )

    assert provider_order.load_fallback_order() == ["Good"]


def test_the_request_is_a_read_only_get(monkeypatch, real_cc_switch):
    """One GET, no body, and no endpoint but ``/api/providers``.

    Reordering lives on the same service; a reader that can reach it
    would be a reader that can silently rewrite the operator's chain.
    """
    seen = {}

    def _urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        seen["data"] = request.data
        seen["timeout"] = timeout
        return _Response(_api_payload(["A"]))

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)

    provider_order.load_fallback_order()

    assert seen["method"] == "GET"
    assert seen["url"] == "http://127.0.0.1:4000/api/providers"
    assert seen["data"] is None
    assert seen["timeout"] == provider_order._API_TIMEOUT_SEC
    assert "/api/reorder" not in seen["url"]


def test_timeout_is_bounded(monkeypatch, real_cc_switch):
    """A CC Switch that stopped answering cannot stall a dispatch."""
    seen = {}

    def _urlopen(request, timeout=None):
        seen["timeout"] = timeout
        return _Response(_api_payload(["A"]))

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)
    provider_order.load_fallback_order()

    assert seen["timeout"] is not None
    assert seen["timeout"] > 0


# ---------------------------------------------------------------------------
# 2. Every failure raises — there is no second road
# ---------------------------------------------------------------------------


def _http_error(status):
    return urllib.error.HTTPError("u", status, "boom", {}, None)


#: Every way the chain can fail to arrive, as
#: ``(transport stub, raises?, reason, detail must quote)``.
#:
#: Each row pins the ``reason`` sentinel *and* the evidence the message
#: has to carry, so a resolver that collapsed every failure into one
#: generic sentence could not pass this matrix. The one row whose
#: ``raises`` is ``False`` is the one answer that is soft.
_FAILURE_CASES = [
    # 4xx — the sharpest case. Something answered, which means the
    # configured port or endpoint is wrong, or whatever is listening
    # there is not CC Switch.
    pytest.param(
        {"exc": _http_error(404)}, True, REASON_HTTP_STATUS, "HTTP 404",
        id="4xx-404",
    ),
    pytest.param(
        {"exc": _http_error(403)}, True, REASON_HTTP_STATUS, "HTTP 403",
        id="4xx-403",
    ),
    pytest.param(
        {"exc": _http_error(401)}, True, REASON_HTTP_STATUS, "HTTP 401",
        id="4xx-401",
    ),
    # 5xx — CC Switch is up and refusing.
    pytest.param(
        {"exc": _http_error(500)}, True, REASON_HTTP_STATUS, "HTTP 500",
        id="5xx-500",
    ),
    pytest.param(
        {"exc": _http_error(503)}, True, REASON_HTTP_STATUS, "HTTP 503",
        id="5xx-503",
    ),
    pytest.param(
        {"exc": _http_error(502)}, True, REASON_HTTP_STATUS, "HTTP 502",
        id="5xx-502",
    ),
    # Nothing on the port at all.
    pytest.param(
        {"exc": urllib.error.URLError(
            ConnectionRefusedError(111, "Connection refused"))},
        True, REASON_API_UNREACHABLE, "URLError", id="connection-refused",
    ),
    pytest.param(
        {"exc": urllib.error.URLError(
            socket.gaierror(-2, "Name or service not known"))},
        True, REASON_API_UNREACHABLE, "URLError", id="dns",
    ),
    pytest.param(
        {"exc": urllib.error.URLError(
            ssl.SSLError("certificate verify failed"))},
        True, REASON_API_UNREACHABLE, "certificate verify failed",
        id="tls",
    ),
    pytest.param(
        {"exc": TimeoutError("timed out")}, True, REASON_API_UNREACHABLE,
        "TimeoutError", id="timeout",
    ),
    pytest.param(
        {"exc": OSError("connection reset by peer")}, True,
        REASON_API_UNREACHABLE, "OSError", id="os-error",
    ),
    # A 200 whose body is not the provider list. An unrelated service on
    # the configured port is the commonest shape of this.
    pytest.param(
        {"response": _Response(body=b"<html>not json</html>")}, True,
        REASON_BAD_PAYLOAD, "JSONDecodeError", id="not-json",
    ),
    pytest.param(
        {"response": _Response(body=b"\xff\xfe\x00")}, True,
        REASON_BAD_PAYLOAD, "UnicodeDecodeError", id="not-utf8",
    ),
    pytest.param(
        {"response": _Response([1, 2, 3])}, True, REASON_BAD_PAYLOAD,
        "root is list", id="root-is-a-list",
    ),
    pytest.param(
        {"response": _Response("a bare string")}, True, REASON_BAD_PAYLOAD,
        "root is str", id="root-is-a-string",
    ),
    pytest.param(
        {"response": _Response({"providers": "nope"})}, True,
        REASON_BAD_PAYLOAD, "'providers' is str", id="providers-not-a-list",
    ),
    pytest.param(
        {"response": _Response({"nothing": "here"})}, True,
        REASON_BAD_PAYLOAD, "'providers' is NoneType",
        id="no-providers-key",
    ),
    # A non-empty list whose entries carry no name this reader can
    # dispatch on: a response shape that has moved, not a fleet of zero.
    pytest.param(
        {"response": _Response({"providers": [{"id": "x"}]})}, True,
        REASON_NO_USABLE_NAME, "none carried a usable name",
        id="no-usable-name",
    ),
    pytest.param(
        {"response": _Response({"providers": [{"id": "x"}, "bare"]})}, True,
        REASON_NO_USABLE_NAME, "2 provider row(s)", id="no-usable-name-2",
    ),
    # The one soft answer: CC Switch is serving and says it has no
    # providers. A successful answer carrying no chain.
    pytest.param(
        {"response": _Response({"providers": []})}, False, None, None,
        id="providers-empty-is-an-answer",
    ),
]


@pytest.mark.parametrize(
    "stub, raises_, reason, detail", _FAILURE_CASES
)
def test_every_way_of_failing_the_chain_is_loud(
    monkeypatch, real_cc_switch, stub, raises_, reason, detail
):
    """Nothing but the empty answer is answered quietly.

    There is one source now, so a way of not getting a chain is an
    operator's problem to see rather than one to route around. The row
    that does not raise is the deliberate exception and is named as
    such in its id.
    """
    _stub_transport(monkeypatch, **stub)

    if not raises_:
        assert provider_order.load_fallback_order() == [], (
            "a valid but empty providers list is CC Switch saying it has "
            "no providers; that is an answer, not a failure to answer"
        )
        return

    with pytest.raises(provider_order.ProviderOrderError) as excinfo:
        result = provider_order.load_fallback_order()
        pytest.fail(f"returned {result!r} instead of raising")

    error = excinfo.value
    assert error.reason == reason, (
        f"wrong reason: got {error.reason!r}, expected {reason!r}"
    )
    assert detail in error.detail, (
        f"the evidence is missing from the detail: {error.detail!r} does "
        f"not contain {detail!r}"
    )
    # The reason has to survive into the message too, because this
    # exception usually travels as one.
    message = str(error)
    assert reason in message, f"reason not in the message: {message!r}"
    assert detail in message, f"evidence not in the message: {message!r}"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 418, 429])
def test_every_4xx_raises_rather_than_answering_from_elsewhere(
    monkeypatch, real_cc_switch, status
):
    """No 4xx is soft, and 404 least of all.

    A 4xx means something answered this path, which is exactly the case
    where "the configured port is wrong" or "that is not CC Switch" has
    to reach the operator. Answering from somewhere else here would hide
    the one fact the response carries.
    """
    _stub_transport(monkeypatch, exc=_http_error(status))

    with pytest.raises(provider_order.ProviderOrderError) as excinfo:
        provider_order.load_fallback_order()

    assert excinfo.value.reason == REASON_HTTP_STATUS
    assert f"HTTP {status}" in str(excinfo.value)


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504])
def test_every_5xx_raises(monkeypatch, real_cc_switch, status):
    """A serving CC Switch that refuses is a refusal, not a chain."""
    _stub_transport(monkeypatch, exc=_http_error(status))

    with pytest.raises(provider_order.ProviderOrderError) as excinfo:
        provider_order.load_fallback_order()

    assert excinfo.value.reason == REASON_HTTP_STATUS
    assert f"HTTP {status}" in str(excinfo.value)


def test_a_non_200_status_without_an_http_error_still_raises(
    monkeypatch, real_cc_switch
):
    """The status check does not depend on the error path.

    ``urlopen`` raises for a 4xx/5xx, but the response object is still
    checked afterwards: a transport that hands back a response object
    carrying a non-200 must not be able to slip past on the assumption
    that the exception was the only way a status could be bad.
    """
    _stub_transport(monkeypatch, _Response(_api_payload(["A"]), status=204))

    with pytest.raises(provider_order.ProviderOrderError) as excinfo:
        provider_order.load_fallback_order()

    assert excinfo.value.reason == REASON_HTTP_STATUS
    assert "HTTP 204" in str(excinfo.value)


def test_failure_is_logged_with_its_reason_before_it_propagates(
    monkeypatch, real_cc_switch, caplog
):
    """The raise is not the only report.

    ``server.load_providers`` propagates this to startup, and anything
    catching it may log the message alone or not at all, so the
    greppable ``reason`` goes out on the ``provider_order`` logger too.
    """
    _stub_transport(monkeypatch, exc=_http_error(404))

    with caplog.at_level("ERROR", logger="provider_order"):
        with pytest.raises(provider_order.ProviderOrderError):
            provider_order.load_fallback_order()

    records = [
        r for r in caplog.records
        if getattr(r, "reason", None) == REASON_HTTP_STATUS
    ]
    assert records, (
        "no ERROR record carried reason= http_status; a failure that only "
        "exists as an exception message cannot be found by an operator "
        "grepping the log"
    )
    assert any("HTTP 404" in r.getMessage() for r in records), (
        f"the status code is not in the log line: "
        f"{[r.getMessage() for r in records]!r}"
    )


def test_the_reasons_are_distinguishable_from_one_another():
    """Different failures must not share a reason.

    The whole point of a sentinel is that the log says *which* way it
    went wrong; if two classes collapsed onto one token an operator
    would be left re-running the request by hand to tell them apart.
    """
    assert len({
        REASON_HTTP_STATUS,
        REASON_API_UNREACHABLE,
        REASON_BAD_PAYLOAD,
        REASON_NO_USABLE_NAME,
    }) == 4


def test_the_empty_answer_is_not_reported_as_a_failure(
    monkeypatch, real_cc_switch, caplog
):
    """The soft case logs nothing at ERROR.

    Returning ``[]`` for an empty list is a successful read, so treating
    it as a failure would train an operator to ignore this logger.
    """
    _stub_transport(monkeypatch, _Response({"providers": []}))

    with caplog.at_level("WARNING", logger="provider_order"):
        assert provider_order.load_fallback_order() == []

    assert not [
        r for r in caplog.records
        if r.name == "provider_order" and r.levelno >= logging.WARNING
    ], (
        "an empty provider list is CC Switch answering, and was logged as "
        f"a warning: {[r.getMessage() for r in caplog.records]!r}"
    )


def test_a_failure_never_returns_an_empty_list(monkeypatch, real_cc_switch):
    """An empty chain and an unresolvable one are different answers.

    An empty list reads downstream as "no providers configured", which
    is a supported state; returning it on failure would turn a broken
    CC Switch into a silently provider-less backend.
    """
    _stub_transport(monkeypatch, exc=TimeoutError("timed out"))

    with pytest.raises(provider_order.ProviderOrderError):
        result = provider_order.load_fallback_order()
        pytest.fail(f"returned {result!r} instead of raising")


def test_no_hard_coded_chain_exists_in_the_module():
    """There is nothing to fall back *to*.

    A list written down in this module is the answer that cannot be
    corrected by operating CC Switch, which is the whole thing being
    removed; this pins that no such list crept back in.
    """
    import inspect

    source = inspect.getsource(provider_order)
    assert "HARDCODED_CHAIN" not in source
    assert "HARDCODED" not in source


def test_the_request_is_a_read_only_get_unaffected_by_a_bad_url(
    monkeypatch, real_cc_switch
):
    """A misconfigured override fails loudly too.

    The URL is built from an operator-supplied base; a base that cannot
    be requested is a configuration fault and surfaces as one, rather
    than being quietly swapped for a default that happens to work.
    """
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", "not a url at all")
    seen = {}

    def _urlopen(request, timeout=None):
        seen["url"] = request.full_url
        return _Response(_api_payload(["A"]))

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)

    assert provider_order.load_fallback_order() == ["A"]
    assert seen["url"] == "http://not a url at all/api/providers"


# ---------------------------------------------------------------------------
# 3. One answer only — nothing else answers, and nothing else may
# ---------------------------------------------------------------------------


def test_no_database_leg_exists():
    """The quiet second road is gone from the resolver.

    Asserted against the module's own attributes and source rather than
    against this test's own idea of what to call: a leg that came back
    under a new name, or reached through ``cc_switch`` instead of a
    local function, would satisfy a caller-focused test while still
    being the second answer the change removed.
    """
    for name in ("_chain_from_database", "_resolve_chain", "REASON_DB_UNAVAILABLE"):
        assert not hasattr(provider_order, name), (
            f"provider_order.{name} exists again; the chain has one source "
            "and a second one would answer when CC Switch cannot"
        )

    source = inspect.getsource(provider_order)
    for token in (
        "list_providers_in_order",
        "_chain_from_database",
        "_ORDERED_CLAUDE_PROVIDERS_SQL",
        "REASON_DB_UNAVAILABLE",
        "CLAUDE_APP_TYPE",
    ):
        assert token not in source, (
            f"{token!r} is back in provider_order.py; the chain is asked "
            "of CC Switch's HTTP provider list and nowhere else"
        )


def test_cc_switch_no_longer_offers_an_ordered_provider_reader():
    """The database side of the deleted leg is gone too.

    ``cc_switch`` stays — it is still the reader of record for
    endpoints, credentials and current selection — but it no longer
    answers "what order does CC Switch show providers in", because that
    is a question only the HTTP surface is asked now.
    """
    for name in (
        "list_providers_in_order",
        "_ORDERED_CLAUDE_PROVIDERS_SQL",
        "CLAUDE_APP_TYPE",
    ):
        assert not hasattr(cc_switch, name), (
            f"cc_switch.{name} still exists; the ordered reader was the "
            "database half of a chain source that has one half now"
        )

    assert hasattr(cc_switch, "get_provider"), (
        "cc_switch.get_provider is how server.load_providers resolves a "
        "name from the chain; deleting the module would break startup"
    )
    assert hasattr(cc_switch, "current_provider")
    assert hasattr(cc_switch, "snapshot")


def test_provider_order_never_opens_a_database():
    """The chain reader touches no SQL and opens no connection.

    Checked on the source text, so it catches an import of ``sqlite3``,
    a ``mode=ro`` URI or a ``cc_switch`` connection helper that a
    passing call-path test would never reach.
    """
    source = inspect.getsource(provider_order)

    for token in ("sqlite3", "mode=ro", "_connect_db", "resolve_db_path"):
        assert token not in source, (
            f"provider_order.py references {token!r}; the chain is asked "
            "over HTTP and must not open CC Switch's store"
        )


def test_no_module_reaches_the_ordered_reader_by_any_name():
    """No other caller keeps the deleted reader alive.

    Importing the name would have failed already; this catches the
    softer form — a module holding its own copy of the query.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "COALESCE(sort_index" in text and path.name != Path(__file__).name:
            offenders.append(str(path.relative_to(root)))

    assert not offenders, (
        "a copy of CC Switch's provider ordering survived its reader: "
        f"{offenders!r}. The order is the array CC Switch serves; "
        "reproducing its SQL pins this project to a private schema."
    )


# ---------------------------------------------------------------------------
# 5. Endpoint resolution
# ---------------------------------------------------------------------------


def test_default_endpoint(monkeypatch):
    for name in provider_order._API_BASE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert provider_order._api_providers_url() == (
        "http://127.0.0.1:4000/api/providers"
    )


@pytest.mark.parametrize("name", list(provider_order._API_BASE_ENV_VARS))
def test_every_accepted_env_var_redirects(monkeypatch, name):
    monkeypatch.setenv(name, "http://127.0.0.1:9999")
    assert provider_order._api_providers_url() == (
        "http://127.0.0.1:9999/api/providers"
    )


def test_env_var_precedence_is_first_hit_wins(monkeypatch):
    monkeypatch.setenv(
        provider_order._API_BASE_ENV_VARS[0], "http://127.0.0.1:1111"
    )
    monkeypatch.setenv(
        provider_order._API_BASE_ENV_VARS[1], "http://127.0.0.1:2222"
    )
    assert provider_order._api_providers_url() == (
        "http://127.0.0.1:1111/api/providers"
    )


def test_override_carrying_a_path_still_asks_for_the_provider_list(monkeypatch, caplog):
    """The variables name a *base*, and the inherited ones carry paths.

    ``CC_SWITCH_API_URL`` and ``PROVIDER_USAGE_CC_SWITCH_API_URL`` are
    the optimizer's, and its second one points at ``/api/usage``. Taking
    such a value verbatim would request a path this reader cannot parse,
    so only the authority is kept.
    """
    monkeypatch.setenv("CC_SWITCH_API_URL", "http://127.0.0.1:4000/api/usage")

    with caplog.at_level("WARNING", logger="provider_order"):
        url = provider_order._api_providers_url()

    assert url == "http://127.0.0.1:4000/api/providers"
    assert any("ignoring the path" in r.getMessage() for r in caplog.records), (
        "dropping an operator's path silently is how a redirect ends up "
        "pointing at nothing"
    )


def test_override_without_a_scheme_is_treated_as_http(monkeypatch):
    """``host:port`` parses as a scheme plus a path without this."""
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", "127.0.0.1:4000")
    assert provider_order._api_providers_url() == (
        "http://127.0.0.1:4000/api/providers"
    )


def test_load_fallback_order_takes_no_arguments():
    """The chain has no seam for a path any more.

    A parameter here would be an invitation to reintroduce a second
    source, which is what this change removed.
    """
    import inspect

    assert inspect.signature(provider_order.load_fallback_order).parameters == {}


def test_the_chain_read_opens_no_file(tmp_path, monkeypatch):
    """The chain reader's only I/O is to CC Switch.

    A file-based chain would have to exist somewhere for this to pass,
    and with none present the reader would raise rather than fall
    through to the database.
    """
    monkeypatch.chdir(tmp_path)
    _install_api(monkeypatch, ["A"])
    assert provider_order.load_fallback_order() == ["A"]


# ---------------------------------------------------------------------------
# 6. Caching
# ---------------------------------------------------------------------------


def test_the_chain_is_read_once_per_process(monkeypatch, real_cc_switch):
    """A dispatch storm must not become a request storm."""
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _Response(_api_payload(["A"]))

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)

    for _ in range(5):
        provider_order.load_fallback_order()

    assert len(calls) == 1


def test_cache_clear_makes_the_next_call_ask_again(monkeypatch):
    """The escape hatch now means "re-ask CC Switch".

    The leg is swapped by hand rather than through ``_install_api``,
    which clears the cache as part of installing — exactly the thing
    this test is measuring.
    """
    monkeypatch.setattr(provider_order, "_chain_from_api", lambda: ["Before"])
    assert provider_order.load_fallback_order() == ["Before"]

    monkeypatch.setattr(provider_order, "_chain_from_api", lambda: ["After"])
    assert provider_order.load_fallback_order() == ["Before"], (
        "expected the cached chain before cache_clear()"
    )

    provider_order.cache_clear()
    assert provider_order.load_fallback_order() == ["After"]


def test_cache_clear_is_idempotent():
    provider_order.cache_clear()
    provider_order.cache_clear()


def test_the_cached_reader_is_the_lru_cached_one():
    """The caching contract is the operator-visible escape hatch.

    ``cache_clear`` only reaches what the wrapper holds, so a chain
    cached anywhere else would outlive it.
    """
    assert hasattr(provider_order._load_fallback_order_cached, "cache_info")
    info = provider_order._load_fallback_order_cached.cache_info()
    assert info.maxsize == 1


# ---------------------------------------------------------------------------
# 7. What was removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "SCHEMA_VERSION",
        "_validate_schema",
        "_is_iso8601",
        "_staleness_minutes",
        "_filter_order_to_live_providers",
        "_load_fallback_order_uncached",
        "_ChainUnavailable",
        "REASON_DB_UNAVAILABLE",
        "REASON_EMPTY_CHAIN",
    ],
)
def test_file_schema_reader_is_gone(name):
    """The reader that validated a contract file no longer exists.

    Each of these existed to answer "is this JSON a well-formed
    provider-order contract?" — a question with no referent once the
    chain comes from CC Switch. ``_ChainUnavailable``,
    ``REASON_DB_UNAVAILABLE`` and ``REASON_EMPTY_CHAIN`` came later and
    are here for the same reason: the first existed only to describe "an
    attempt failed, another might answer", the second named a source
    that is gone, and the third named a case that is a successful
    answer now rather than a failure.
    """
    assert not hasattr(provider_order, name), (
        f"provider_order.{name} still exists; the file-schema reader is "
        "supposed to be gone now that the chain is asked of CC Switch"
    )


@pytest.mark.parametrize(
    "name",
    [
        "REASON_VERSION_MISMATCH",
        "REASON_ORDER_MISSING",
        "REASON_ORDER_EMPTY",
        "REASON_UPDATED_AT_INVALID",
        "REASON_STALE",
        "REASON_INVALID_ORDER_TYPE",
        "REASON_NOT_A_DICT",
    ],
)
def test_contract_file_reasons_are_gone(name):
    """No log line can report "the contract file is malformed".

    ``REASON_NOT_A_DICT`` survives for the 5-hour quota payload, which
    is a different reader with its own file — so it is checked by that
    reader's test instead of being asserted gone here.
    """
    if name == "REASON_NOT_A_DICT":
        pytest.skip("belongs to the quota payload reader")
    assert not hasattr(provider_order, name), (
        f"provider_order.{name} still exists, so a log line can still be "
        "about validating the provider-order contract file"
    )


def test_config_paths_no_longer_resolves_a_chain_file():
    """The seam that pointed at a chain file is gone.

    ``resolve_provider_order_file`` existed so a reader could be told
    where to find the chain. There is no chain file to find, so the
    resolver and its ``PROVIDER_ORDER_FILE`` variable went with it.
    """
    import config_paths

    assert not hasattr(config_paths, "resolve_provider_order_file")
    assert not hasattr(config_paths, "PROVIDER_ORDER_FILE")


def test_server_exposes_no_chain_file_constant():
    """``server.PROVIDER_ORDER_FILE`` was the import-time snapshot.

    ``provider_order`` used to prefer it over the live environment, so a
    seam that survived in ``server`` would have kept the old precedence
    alive for anything that reached for it.
    """
    import server

    assert not hasattr(server, "PROVIDER_ORDER_FILE")
    assert not hasattr(server, "_resolve_provider_order_file")


def test_config_yaml_no_longer_declares_provider_order_file():
    """The commented-out declaration is gone from ``config.yaml``.

    A commented line is not documentation — it is a setting waiting for
    the next reader to uncomment it, which would reintroduce exactly the
    second source this change removed.
    """
    import config_paths

    text = Path(config_paths.BACKEND_CONFIG_YAML).read_text(encoding="utf-8")
    assert "provider_order_file" not in text
    assert "PROVIDER_ORDER_FILE" not in text
