"""Unit tests for the deterministic api_test runner (2026-09-18 VP judgment rework).

Covers:
  * ``validate_vp`` — every schema rule, including the "an assertion with
    no comparator would always pass" case that must be a schema error.
  * ``lookup_json_path`` — the deterministic JSONPath subset.
  * comparator evaluation — all of them, pass and fail.
  * ``run_api_verification`` — end to end against a real local HTTP
    server: passing assertions, failing assertions, a 4xx that an
    assertion *expects*, an unresolvable host, and the audit artifact.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_api_runner import (  # noqa: E402
    COMPARATOR_KEYS,
    SELF_CONTAINED_SUBJECTS,
    STATUS_FAILED,
    STATUS_PASSED,
    SUBJECT_KEYS,
    lookup_json_path,
    run_api_verification,
    validate_vp,
)


# ---------------------------------------------------------------------------
# A tiny real HTTP server so the runner is exercised against a socket,
# not a mock.
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test output
        pass

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/ok"):
            body = json.dumps({
                "signal_type": "trend",
                "signals": [{"t": 1}, {"t": 2}, {"t": 3}],
                "empty": [],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-VP", "alpha")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/created"):
            body = b'{"id": 7}'
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/missing"):
            body = b'{"error": "not found"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/text"):
            body = b"plain text payload"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = _respond


@pytest.fixture(scope="module")
def server_url():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _vp(url: str, assertions: list, method: str = "GET", **over) -> dict:
    vp = {
        "id": "VP-001",
        "verification_method": "api_test",
        "request": {"method": method, "url": url},
        "assertions": assertions,
    }
    vp.update(over)
    return vp


# ---------------------------------------------------------------------------
# validate_vp
# ---------------------------------------------------------------------------


def test_valid_vp_has_no_issues():
    vp = _vp("http://x/y", [{"name": "s", "status": 200}])

    assert validate_vp(vp) == []


@pytest.mark.parametrize(
    "vp, fragment",
    [
        ({"id": "V"}, "requires a 'request' object"),
        (_vp("http://x", [{"status": 200}]) | {"request": {"url": "http://x"}},
         "request.method must be"),
        (_vp("http://x", [{"status": 200}]) | {"request": {"method": "GET"}},
         "request.url is required"),
        (_vp("http://x", []), "non-empty 'assertions' list"),
        (_vp("http://x", None), "non-empty 'assertions' list"),
        (_vp("http://x", ["not-an-object"]), "expected an object"),
        (_vp("http://x", [{"equals": 1}]), "has no subject"),
        (_vp("http://x", [{"status": 200, "json_path": "$.a", "equals": 1}]),
         "names several subjects"),
        (_vp("http://x", [{"json_path": "$.a", "equals": 1, "gt": 2}]),
         "names several comparators"),
        (_vp("http://x", [{"json_path": "$.a"}]),
         "no comparator"),
        (_vp("http://x", [{"status": 200, "matches": "x"}]),
         "self-contained subject"),
        (_vp("http://x", [{"status": "200"}]),
         "must be an int"),
        (_vp("http://x", [{"body_contains": 7}]),
         "body_contains must be a string"),
        (_vp("http://x", [{"body_not_contains": 7}]),
         "body_not_contains must be a string"),
        (_vp("http://x", [{"body_contains": ""}]),
         "body_contains is empty"),
        (_vp("http://x", [{"body_not_contains": ""}]),
         "body_not_contains is empty"),
        (_vp("http://x", [{"body_not_contains": "x", "equals": "x"}]),
         "self-contained subject"),
        (_vp("http://x", [{"header": "", "exists": True}]),
         "'header' but it is empty"),
    ],
)
def test_schema_issues(vp, fragment):
    issues = validate_vp(vp)

    assert issues, f"expected an issue for {vp!r}"
    assert any(fragment in i.detail for i in issues), [i.detail for i in issues]


def test_assertion_without_a_comparator_is_a_schema_error():
    """The whole point of this rework: an assertion that cannot fail is
    not an assertion. ``json_path`` says where to look; without a
    comparator it never says what it expects to find."""
    issues = validate_vp(_vp("http://x", [{"name": "looks fine", "json_path": "$.a"}]))

    assert len(issues) == 1
    assert "would always pass" in issues[0].detail


def test_status_list_means_any_of(server_url: str):
    result = run_api_verification(
        _vp(f"{server_url}/created", [{"name": "created", "status": [200, 201]}])
    )

    assert result.status == STATUS_PASSED, result.reasons

    missing = run_api_verification(
        _vp(f"{server_url}/created", [{"name": "created", "status": [200, 204]}])
    )
    assert missing.status == STATUS_FAILED


@pytest.mark.parametrize("method", ["GET", "post", "Delete"])
def test_method_is_case_insensitive(method):
    assert validate_vp(_vp("http://x", [{"status": 200}], method=method)) == []


# ---------------------------------------------------------------------------
# lookup_json_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc, path, expected",
    [
        ({"a": 1}, "$.a", (True, 1)),
        ({"a": {"b": {"c": "x"}}}, "$.a.b.c", (True, "x")),
        ({"a": [10, 20]}, "$.a[1]", (True, 20)),
        ({"a": [{"b": 5}]}, "$.a[0].b", (True, 5)),
        ({"a": 1}, "$", (True, {"a": 1})),
        ({"a": 1}, "$.missing", (False, None)),
        ({"a": [1]}, "$.a[5]", (False, None)),
        ({"a": 1}, "$.a.b", (False, None)),
        ({"a": 1}, "a", (False, None)),
        ({"a": 1}, "$.a[0]", (False, None)),
    ],
)
def test_lookup_json_path(doc, path, expected):
    assert lookup_json_path(doc, path) == expected


# ---------------------------------------------------------------------------
# end to end against the real server
# ---------------------------------------------------------------------------


def test_passing_assertions(server_url: str):
    vp = _vp(f"{server_url}/ok", [
        {"name": "状态码", "status": 200},
        {"name": "趋势类型", "json_path": "$.signal_type", "equals": "trend"},
        {"name": "信号数", "json_path": "$.signals", "length_gte": 3},
        {"name": "自定义头", "header": "x-vp", "equals": "alpha"},
    ])

    result = run_api_verification(vp)

    assert result.status == STATUS_PASSED, result.reasons
    assert result.evidence["status_code"] == 200
    assert len(result.evidence["assertions_passed"]) == 4
    assert result.evidence["assertions_failed"] == []


def test_failing_assertion_names_the_expected_and_actual(server_url: str):
    vp = _vp(f"{server_url}/ok", [
        {"name": "趋势类型", "json_path": "$.signal_type", "equals": "consolidation"},
    ])

    result = run_api_verification(vp)

    assert result.status == STATUS_FAILED
    [reason] = result.reasons
    assert "趋势类型" in reason
    assert "'consolidation'" in reason and "'trend'" in reason


def test_one_failed_assertion_fails_the_vp(server_url: str):
    vp = _vp(f"{server_url}/ok", [
        {"name": "ok", "status": 200},
        {"name": "bad", "json_path": "$.signal_type", "equals": "nope"},
    ])

    result = run_api_verification(vp)

    assert result.status == STATUS_FAILED
    assert len(result.evidence["assertions_passed"]) == 1
    assert len(result.evidence["assertions_failed"]) == 1


def test_an_expected_4xx_is_a_pass(server_url: str):
    """A 404 the spec asks for is a PASSED verdict, not an execution
    error — that is why an expected error can be an acceptance
    criterion at all."""
    vp = _vp(f"{server_url}/missing", [
        {"name": "not found", "status": 404},
        {"name": "error field", "json_path": "$.error", "equals": "not found"},
    ])

    result = run_api_verification(vp)

    assert result.status == STATUS_PASSED, result.reasons


def test_unexpected_5xx_is_a_failure_not_a_crash(server_url: str):
    vp = _vp(f"{server_url}/boom", [{"name": "ok", "status": 200}])

    result = run_api_verification(vp)

    assert result.status == STATUS_FAILED
    assert "500" in result.reasons[0]


def test_non_json_body_fails_a_json_path_assertion_cleanly(server_url: str):
    vp = _vp(f"{server_url}/text", [
        {"name": "field", "json_path": "$.a", "equals": 1},
    ])

    result = run_api_verification(vp)

    assert result.status == STATUS_FAILED
    assert "not JSON" in result.reasons[0]


def test_body_contains_and_regex(server_url: str):
    ok = run_api_verification(_vp(f"{server_url}/text", [
        {"name": "substring", "body_contains": "plain text"},
    ]))
    bad = run_api_verification(_vp(f"{server_url}/text", [
        {"name": "substring", "body_contains": "absent"},
    ]))

    assert ok.status == STATUS_PASSED, ok.reasons
    assert bad.status == STATUS_FAILED


# ---------------------------------------------------------------------------
# body_not_contains — the negation of body_contains (2026-09-27)
#
# The vocabulary was asymmetric: ``json_path`` / ``header`` both accept the
# ``not_contains`` comparator, but the raw body accepted only ``contains``.
# "The response must not leak /etc/passwd" — the second half of every
# path-traversal criterion — was therefore *inexpressible*. On the
# 2026-09-26 plan the planner reached for the symmetric spelling, and the
# whole VP was rejected at schema time: ``assertions[0]`` (a perfectly
# valid ``{"status": 404}``) never ran either, and the VP sent no request
# in four consecutive rounds.
# ---------------------------------------------------------------------------


def test_the_traversal_vp_shape_is_expressible():
    """The exact VP-007 assertion pair must validate clean.

    This is the regression pin for the incident. Before the fix the
    second assertion produced "assertions[1] has no subject", which
    voids the first one too — so the assertion under test here is not
    "does the second one pass" but "does the *pair* survive validation".
    """
    vp = _vp(
        "http://x/api/plan/..%2F..%2Fetc%2Fpasswd/status",
        [
            {"name": "穿越序列被拒为 404", "status": 404},
            {"name": "响应体不是系统文件内容", "body_not_contains": "root:x:0:0"},
        ],
    )

    assert validate_vp(vp) == [], validate_vp(vp)


def test_body_not_contains_passes_when_the_substring_is_absent(server_url: str):
    ok = run_api_verification(_vp(f"{server_url}/missing", [
        {"name": "no leak", "body_not_contains": "root:x:0:0"},
    ]))

    assert ok.status == STATUS_PASSED, ok.reasons


def test_body_not_contains_fails_when_the_substring_is_present(server_url: str):
    """Anti-vacuity control: the negation must actually be evaluated.

    Without this the subject could be "supported" by a code path that
    always returns found=True with a comparator that always passes.
    ``/missing`` answers ``{"error": "not found"}``, so the substring is
    genuinely present and the assertion must FAIL.
    """
    bad = run_api_verification(_vp(f"{server_url}/missing", [
        {"name": "leak", "body_not_contains": "not found"},
    ]))

    assert bad.status == STATUS_FAILED
    assert any("NOT to contain" in r for r in bad.reasons), bad.reasons


def test_body_not_contains_does_not_require_a_json_body(server_url: str):
    """The reason ``json_path`` + ``not_contains`` is not a substitute.

    ``/text`` answers ``plain text payload``, which is not JSON. A
    ``json_path`` assertion cannot speak about it at all; the raw-body
    subject can, which is what a leak check needs — the leak happens in
    the raw body, not at some resolvable path.
    """
    raw = run_api_verification(_vp(f"{server_url}/text", [
        {"name": "no secret", "body_not_contains": "AKIA"},
    ]))
    via_path = run_api_verification(_vp(f"{server_url}/text", [
        {"name": "no secret", "json_path": "$.detail", "not_contains": "AKIA"},
    ]))

    assert raw.status == STATUS_PASSED, raw.reasons
    assert via_path.status == STATUS_FAILED
    assert any("not JSON" in r for r in via_path.reasons), via_path.reasons


def test_regex_comparator_on_a_json_field(server_url: str):
    ok = run_api_verification(_vp(f"{server_url}/ok", [
        {"name": "trend-ish", "json_path": "$.signal_type", "matches": "^tr"},
    ]))
    bad = run_api_verification(_vp(f"{server_url}/ok", [
        {"name": "trend-ish", "json_path": "$.signal_type", "matches": "^zz"},
    ]))

    assert ok.status == STATUS_PASSED, ok.reasons
    assert bad.status == STATUS_FAILED


def test_exists_comparator(server_url: str):
    ok = run_api_verification(_vp(f"{server_url}/ok", [
        {"name": "present", "json_path": "$.empty", "exists": True},
        {"name": "absent", "json_path": "$.nope", "exists": False},
    ]))

    assert ok.status == STATUS_PASSED, ok.reasons


def test_unreachable_host_is_a_failed_verdict_not_an_exception():
    vp = _vp("http://127.0.0.1:1/nothing", [{"name": "ok", "status": 200}])

    result = run_api_verification(vp)

    assert result.status == STATUS_FAILED
    assert "请求未能完成" in result.reasons[0]
    assert result.evidence["request"]["url"].endswith("/nothing")


def test_schema_error_is_a_failed_verdict(server_url: str):
    vp = {"id": "VP-009", "verification_method": "api_test"}

    result = run_api_verification(vp)

    assert result.status == STATUS_FAILED
    assert result.evidence["schema_issues"]


def test_post_with_a_json_body(server_url: str):
    vp = _vp(
        f"{server_url}/ok", [{"name": "ok", "status": 200}],
        method="POST", request={
            "method": "POST", "url": f"{server_url}/ok",
            "body": {"symbol": "EXAMPLE"},
        },
    )

    result = run_api_verification(vp)

    assert result.status == STATUS_PASSED, result.reasons


def test_artifact_is_written_for_post_hoc_investigation(server_url: str, tmp_path: Path):
    vp = _vp(f"{server_url}/ok", [
        {"name": "趋势类型", "json_path": "$.signal_type", "equals": "trend"},
    ])

    run_api_verification(vp, artifact_dir=tmp_path)

    artifact = tmp_path / "VP-001" / "api_response.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text())
    assert payload["status_code"] == 200
    assert "'trend'" in json.dumps(payload["response_body"]) or "trend" in payload["response_body"]
    assert payload["assertions"]


def test_artifact_write_failure_does_not_change_the_verdict(server_url: str, tmp_path: Path):
    """Losing the audit copy must never turn a pass into a fail."""
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")

    result = run_api_verification(
        _vp(f"{server_url}/ok", [{"name": "ok", "status": 200}]),
        artifact_dir=blocked,
    )

    assert result.status == STATUS_PASSED


def test_timeout_is_clamped_and_defaulted():
    vp = _vp("http://127.0.0.1:1/x", [{"name": "ok", "status": 200}])
    vp["request"]["timeout_seconds"] = 99999

    result = run_api_verification(vp)

    assert result.evidence["request"]["timeout_seconds"] == 300

    vp["request"].pop("timeout_seconds")
    result = run_api_verification(vp)
    assert result.evidence["request"]["timeout_seconds"] == 30


# ---------------------------------------------------------------------------
# The vocabulary is a public, closed set (2026-09-27)
#
# Three properties that together make "the planner writes a key the runner
# does not know" structurally impossible rather than merely unlikely: the
# constants are importable, the docstring mirrors them, and an unknown key
# is a schema error rather than a silent pass.
# ---------------------------------------------------------------------------


def test_vocabulary_is_exported_as_public_constants():
    """``SUBJECT_KEYS`` / ``COMPARATOR_KEYS`` / ``SELF_CONTAINED_SUBJECTS``
    must be reachable as public module attributes.

    ``prompts`` builds the plan-generation spec table by importing these
    tuples directly, so the table cannot teach a spelling the runner does
    not accept. That import is the contract; a private ``_SUBJECT_KEYS``
    would put the single source of truth behind an underscore and invite
    the next consumer to re-declare its own copy.
    """
    assert tuple(SUBJECT_KEYS) == SUBJECT_KEYS
    assert tuple(COMPARATOR_KEYS) == COMPARATOR_KEYS
    assert frozenset(SELF_CONTAINED_SUBJECTS) == SELF_CONTAINED_SUBJECTS

    # The self-contained set must be a *subset* of the subjects — a
    # subject that carries its own expectation is one of the subjects,
    # not a parallel axis.
    assert SELF_CONTAINED_SUBJECTS.issubset(set(SUBJECT_KEYS)), (
        f"SELF_CONTAINED_SUBJECTS={set(SELF_CONTAINED_SUBJECTS)} "
        f"contains entries that aren't in SUBJECT_KEYS={set(SUBJECT_KEYS)}"
    )


def test_docstring_assertion_vocabulary_lists_every_subject():
    """The module docstring's "Assertion vocabulary" section must list
    every key in ``SUBJECT_KEYS``.

    The docstring is the part a human reads before touching the
    vocabulary; a subject silently added to the tuple and not to the
    summary is how the next reader concludes the vocabulary is smaller
    than it is. (The machine-readable half — the prompt table — is
    generated, so it cannot drift; this gate covers the prose.)
    """
    module = __import__("verification_api_runner")
    docstring = module.__doc__ or ""

    assert "Assertion vocabulary" in docstring, (
        "verification_api_runner module docstring is missing an "
        "'Assertion vocabulary' section"
    )

    for subject in SUBJECT_KEYS:
        assert subject in docstring, (
            f"subject {subject!r} is in SUBJECT_KEYS={list(SUBJECT_KEYS)} "
            f"but is missing from the module docstring's 'Assertion "
            f"vocabulary' section. The summary and the constants have "
            f"diverged — fix one to match the other."
        )


def test_validate_vp_rejects_unknown_subject():
    """A typo / unknown subject must come back as a schema issue, not as a
    silent pass.

    This is the contract that makes the vocabulary a closed set: any
    assertion key outside ``SUBJECT_KEYS`` cannot pass ``validate_vp``.
    The key is generated from the vocabulary rather than hard-coded, so
    this test keeps testing "an unknown key is rejected" even as the
    vocabulary grows — which is the property that matters, not any one
    historical spelling.
    """
    unknown = next(
        (k for k in ("body_doesnt_contain", "response_json", "totally_made_up")
         if k not in set(SUBJECT_KEYS)),
        "totally_made_up",
    )
    vp = _vp("http://x/y", [
        {"name": "bad", unknown: "hello"},
    ])

    issues = validate_vp(vp)

    assert issues, (
        f"validate_vp({vp!r}) returned no issues but the assertion uses "
        f"an unknown subject {unknown!r}; the vocabulary is supposed to "
        f"be closed, not open-ended"
    )
    assert any("has no subject" in i.detail for i in issues), [
        i.detail for i in issues
    ]
