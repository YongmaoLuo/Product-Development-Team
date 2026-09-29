"""Deterministic API-acceptance runner (2026-09-18, VP judgment rework).

Why this module exists
-----------------------
An ``api_test`` verification point asks a black-box question: *does the
running service actually serve the contract the PRD describes?* Until
now the system answered it by spawning a full Claude Code sub-agent,
letting it issue the request, and trusting the ``pytest_exit_code`` /
``tests_run`` numbers it reported about itself
(``verification_subagent.parse_verdict``). Three things were wrong with
that:

  * the sub-agent was the **only** source of the "objective" signal, so
    the same artifact could be graded PASSED in one round and FAILED in
    the next (a 2026-09-18 post-mortem: VP-001 flipped on an
    unchanged ``echo`` command);
  * a VP whose planner could not invent a shell command got
    ``echo 'Basic functionality check'`` filled in by
    ``verification_agent._annotate_verification_points`` — a command
    that exits 0 while proving nothing, which the zero-tests gate then
    turned into a permanent FAILED;
  * the judgment did not depend on the request at all: whatever the
    sub-agent *said* about the response was the verdict.

This module replaces that with the thing an API acceptance test should
always have been: a **declared request** and **declared assertions**,
executed and evaluated by the framework. No LLM is involved, so the
verdict is a pure function of (request, response, assertions) and is
therefore reproducible across rounds.

Schema (in ``plans/<id>/verification_plan.json``)
-------------------------------------------------
::

    {
      "id": "VP-014",
      "verification_method": "api_test",
      "request": {
        "method": "GET",
        "url": "{{svc.api.url}}/api/items?limit=10",
        "headers": {"Accept": "application/json"},
        "body": null,
        "timeout_seconds": 30
      },
      "assertions": [
        {"name": "状态码", "status": 200},
        {"name": "资源 id 存在", "json_path": "$.id", "exists": true},
        {"name": "至少返回一条记录", "json_path": "$.items", "length_gte": 1}
      ]
    }

``{{svc.*}}`` placeholders are expanded (in memory) by the caller before
this module runs — see ``verification_agent._resolve_service_placeholders``.

Assertion vocabulary
--------------------
The canonical list lives in the module-level constants
``SUBJECT_KEYS`` / ``COMPARATOR_KEYS`` / ``SELF_CONTAINED_SUBJECTS``
(declared once). Anything that needs to teach the vocabulary to an LLM
(``prompts._api_test_assertion_table``) or validate it elsewhere should
import those tuples rather than hand-duplicate them. The summary below
mirrors them for human readers; if it ever disagrees with the constants,
the constants decide.

Exactly one *subject* per assertion, plus (for most subjects) one
*comparator*:

  ``status``            ``{"status": 200}`` — self-contained: the value
                        IS the expected status. A list means "any of".
  ``body_contains``     ``{"body_contains": "..."}`` — self-contained:
                        the body must contain this substring.
  ``body_not_contains`` ``{"body_not_contains": "..."}`` — self-contained:
                        the body must NOT contain this substring. The
                        negation of the line above; a secret-leak or
                        path-traversal VP needs both halves, and before
                        2026-09-27 only the positive one existed.
  ``json_path``         needs a comparator:
                        ``{"json_path": "$.a.b", "equals": "x"}``
  ``header``            value is the header *name*, needs a comparator:
                        ``{"header": "content-type", "contains": "json"}``

``body_contains`` / ``body_not_contains`` are the only subjects that can
speak about the *raw* body. ``json_path`` + ``not_contains`` is not a
substitute: it needs the body to be JSON and to resolve one path.

  comparators: ``equals`` | ``not_equals`` | ``contains`` |
               ``not_contains`` | ``exists`` | ``matches`` | ``in`` |
               ``not_in`` | ``gt`` | ``gte`` | ``lt`` | ``lte`` |
               ``length_gte`` | ``length_lte`` | ``length_equals``

An assertion that needs a comparator but names none is a schema error,
not a silent pass — same principle as everywhere else in this rework: a
typo must fail loudly rather than verify nothing.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Verdict-compatible statuses this runner can emit.
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300

#: How much of a response body is kept in the evidence / artifact.
MAX_BODY_CHARS = 4000

SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

#: Canonical list of api_test assertion *subjects* — every key an
#: assertion is allowed to carry, i.e. every part of the response the
#: runner knows how to look at. Public so ``prompts`` (which renders the
#: plan-generation spec table straight from this tuple) and any other
#: consumer read the SAME source instead of hand-copying a second
#: spelling that then drifts.
#:
#: ``body_not_contains`` was added on 2026-09-27. Its absence was a real
#: dead end, not a missing convenience: the vocabulary was asymmetric
#: (``json_path`` / ``header`` both accept the ``not_contains``
#: comparator, the raw body accepted only ``contains``), so "the response
#: must not leak /etc/passwd" — the second half of every path-traversal
#: criterion — was **inexpressible**. A plan author who reached for the
#: symmetric spelling got the whole VP rejected at schema time, valid
#: assertions included, and the VP never sent a request at all.
SUBJECT_KEYS = ("status", "header", "json_path", "body_contains", "body_not_contains")

#: Canonical list of api_test assertion *comparators*. Subjects in
#: :data:`SELF_CONTAINED_SUBJECTS` carry their own expectation; the rest
#: must name exactly one of these.
COMPARATOR_KEYS = (
    "equals", "not_equals", "contains", "not_contains", "exists",
    "matches", "in", "not_in", "gt", "gte", "lt", "lte",
    "length_gte", "length_lte", "length_equals",
)

#: Subjects whose value IS the expectation — ``{"status": 200}`` needs no
#: separate comparator. The others describe *where* to look and must say
#: what they expect to find there.
SELF_CONTAINED_SUBJECTS = frozenset({"status", "body_contains", "body_not_contains"})

_INDEX_RE = re.compile(r"\[(\d+)\]")


# ---------------------------------------------------------------------------
# Schema validation (pure — used by the plan-generation guard too)
# ---------------------------------------------------------------------------


@dataclass
class SchemaIssue:
    """One reason an ``api_test`` VP cannot be executed as declared."""

    vp_id: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"vp_id": self.vp_id, "detail": self.detail}


def validate_vp(vp: Any) -> List[SchemaIssue]:
    """Return every schema problem that makes this VP unrunnable.

    Called at plan-generation time (so a plan with a malformed
    ``api_test`` is regenerated rather than run) **and** again by
    :func:`run_api_verification` (so a hand-edited plan fails loudly at
    execution time instead of verifying nothing).
    """
    issues: List[SchemaIssue] = []
    if not isinstance(vp, dict):
        return [SchemaIssue("unknown", f"VP is {type(vp).__name__}, expected an object")]
    vp_id = str(vp.get("id", "unknown"))

    request = vp.get("request")
    if not isinstance(request, dict):
        issues.append(SchemaIssue(
            vp_id,
            "api_test requires a 'request' object with 'method' and 'url'",
        ))
    else:
        method = str(request.get("method") or "").strip().upper()
        if method not in SUPPORTED_METHODS:
            issues.append(SchemaIssue(
                vp_id,
                f"request.method must be one of {sorted(SUPPORTED_METHODS)}, "
                f"got {request.get('method')!r}",
            ))
        if not str(request.get("url") or "").strip():
            issues.append(SchemaIssue(vp_id, "request.url is required"))

    assertions = vp.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        issues.append(SchemaIssue(
            vp_id,
            "api_test requires a non-empty 'assertions' list — an API "
            "verification with no assertion proves nothing",
        ))
    else:
        for index, assertion in enumerate(assertions):
            issues.extend(_validate_assertion(vp_id, index, assertion))

    return issues


def _validate_assertion(vp_id: str, index: int, assertion: Any) -> List[SchemaIssue]:
    prefix = f"assertions[{index}]"
    if not isinstance(assertion, dict):
        return [SchemaIssue(vp_id, f"{prefix} is {type(assertion).__name__}, expected an object")]

    subjects = [k for k in SUBJECT_KEYS if k in assertion]
    if not subjects:
        return [SchemaIssue(
            vp_id,
            f"{prefix} has no subject — name one of {list(SUBJECT_KEYS)}",
        )]
    if len(subjects) > 1:
        return [SchemaIssue(
            vp_id, f"{prefix} names several subjects {subjects}; use exactly one"
        )]

    subject = subjects[0]
    comparators = [k for k in COMPARATOR_KEYS if k in assertion]

    if subject in SELF_CONTAINED_SUBJECTS:
        # ``{"status": 200}`` / ``{"body_contains": "..."}`` /
        # ``{"body_not_contains": "..."}`` carry their own expectation.
        # Adding a comparator on top would be ambiguous about which one
        # decides.
        if comparators:
            return [SchemaIssue(
                vp_id,
                f"{prefix} uses the self-contained subject {subject!r} "
                f"together with comparator(s) {comparators}; drop the "
                f"comparator — {subject!r} carries its own expectation",
            )]
        if subject == "status":
            expected = assertion.get("status")
            if isinstance(expected, list):
                if not expected or not all(
                    isinstance(v, int) and not isinstance(v, bool)
                    for v in expected
                ):
                    return [SchemaIssue(
                        vp_id,
                        f"{prefix}.status list must be non-empty integers",
                    )]
            elif not isinstance(expected, int) or isinstance(expected, bool):
                return [SchemaIssue(
                    vp_id,
                    f"{prefix}.status must be an int or a list of ints, "
                    f"got {expected!r}",
                )]
        elif not isinstance(assertion.get(subject), str):
            return [SchemaIssue(
                vp_id, f"{prefix}.{subject} must be a string"
            )]
        elif not assertion[subject]:
            # The empty substring is the one value of these two subjects
            # that carries no information: ``"" in body`` is always true
            # and ``"" not in body`` is always false, so the assertion
            # either verifies nothing or is doomed to fail. Same rule as
            # "no comparator" below — a typo must fail loudly here rather
            # than grade a VP on a tautology.
            return [SchemaIssue(
                vp_id,
                f"{prefix}.{subject} is empty — an empty substring is "
                f"always {'present' if subject == 'body_contains' else 'absent'}, "
                f"so this assertion decides nothing",
            )]
        return []

    if not comparators:
        return [SchemaIssue(
            vp_id,
            f"{prefix} names subject {subject!r} but no comparator — name "
            f"one of {list(COMPARATOR_KEYS)}. An assertion without a "
            f"comparator would always pass.",
        )]
    if len(comparators) > 1:
        return [SchemaIssue(
            vp_id, f"{prefix} names several comparators {comparators}; use exactly one"
        )]

    if subject == "header" and not str(assertion.get("header") or "").strip():
        return [SchemaIssue(vp_id, f"{prefix} uses 'header' but it is empty")]

    return []


# ---------------------------------------------------------------------------
# json_path lookup (deterministic subset: $, dotted keys, [n] indices)
# ---------------------------------------------------------------------------


def lookup_json_path(document: Any, path: str) -> Tuple[bool, Any]:
    """Resolve ``$.a.b[0].c`` against ``document``.

    Returns ``(found, value)``. The syntax is deliberately a *subset* of
    JSONPath (no wildcards, no filters, no recursive descent): an
    acceptance assertion must resolve to exactly one value, and a
    wildcard would make "equals" ambiguous.
    """
    if not isinstance(path, str) or not path.startswith("$"):
        return False, None

    tokens: List[Any] = []
    for segment in path[1:].split("."):
        if not segment:
            continue
        # ``signals[0]`` → key ``signals`` then index 0
        match = re.match(r"^([^.\[\]]*)((?:\[\d+\])*)$", segment)
        if not match:
            return False, None
        key, indices = match.group(1), match.group(2)
        if key:
            tokens.append(key)
        for index_str in _INDEX_RE.findall(indices):
            tokens.append(int(index_str))

    current = document
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return False, None
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return False, None
            current = current[token]
    return True, current


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------


def _subject_value(
    assertion: Dict[str, Any], status: int, headers: Dict[str, str], body: str,
) -> Tuple[bool, Any, str]:
    """Resolve the assertion's subject against the response.

    Returns ``(found, value, label)``. ``label`` is what the failure
    message names, so an operator reading the report knows which part of
    the response the assertion was about.
    """
    if "status" in assertion:
        return True, status, "status"

    if "header" in assertion:
        name = str(assertion.get("header") or "").strip().lower()
        return (name in headers), headers.get(name), f"header {name!r}"

    if "json_path" in assertion:
        path = str(assertion.get("json_path") or "")
        try:
            document = json.loads(body) if body.strip() else None
        except json.JSONDecodeError:
            return False, None, f"json_path {path} (response body is not JSON)"
        found, value = lookup_json_path(document, path)
        return found, value, f"json_path {path}"

    if "body_contains" in assertion or "body_not_contains" in assertion:
        return True, body, "body"

    return False, None, "(no subject)"


def _comparator_and_expected(assertion: Dict[str, Any]) -> Tuple[str, Any]:
    """The ``(comparator, expected)`` a validated assertion resolves to.

    Self-contained subjects fold their value into the comparator:
    ``{"status": [200, 201]}`` becomes ``("in", [200, 201])`` and
    ``{"body_contains": "x"}`` becomes ``("contains", "x")``. This is
    why :func:`validate_vp` must run first — it is what guarantees the
    keys this reads are present and well-typed.

    ``body_not_contains`` folds into the comparator that already existed
    for ``json_path`` / ``header``; the subject adds a spelling, not a
    new semantics.
    """
    if "status" in assertion:
        expected = assertion["status"]
        return ("in", expected) if isinstance(expected, list) else ("equals", expected)
    if "body_contains" in assertion:
        return ("contains", assertion["body_contains"])
    if "body_not_contains" in assertion:
        return ("not_contains", assertion["body_not_contains"])
    comparator = next(k for k in COMPARATOR_KEYS if k in assertion)
    return comparator, assertion.get(comparator)


def _evaluate_comparator(
    comparator: str, expected: Any, found: bool, actual: Any,
) -> Tuple[bool, str]:
    """Return ``(passed, explanation)`` for one comparator."""
    def _mismatch() -> str:
        return f"expected {comparator} {expected!r}, got {actual!r}"

    if comparator == "exists":
        want = bool(expected)
        ok = found if want else (not found)
        return ok, (
            f"expected the subject to {'exist' if want else 'not exist'}; "
            f"it {'does' if found else 'does not'}"
        )

    if not found:
        return False, f"subject not found (expected {comparator} {expected!r})"

    if comparator == "equals":
        return actual == expected, _mismatch()
    if comparator == "not_equals":
        return actual != expected, f"expected {actual!r} != {expected!r}"
    if comparator == "contains":
        ok = isinstance(actual, str) and str(expected) in actual
        return ok, (f"expected to contain {expected!r}" if not ok else "contains")
    if comparator == "not_contains":
        ok = not (isinstance(actual, str) and str(expected) in actual)
        return ok, (f"expected NOT to contain {expected!r}" if not ok else "does not contain")
    if comparator == "matches":
        try:
            ok = isinstance(actual, str) and re.search(str(expected), actual) is not None
        except re.error as exc:
            return False, f"invalid regex {expected!r}: {exc}"
        return ok, (f"expected to match /{expected}/" if not ok else "matches")
    if comparator == "in":
        ok = isinstance(expected, list) and actual in expected
        return ok, (f"expected one of {expected!r}" if not ok else "in set")
    if comparator == "not_in":
        ok = isinstance(expected, list) and actual not in expected
        return ok, (f"expected NOT one of {expected!r}" if not ok else "not in set")

    # Numeric / length comparators.
    if comparator.startswith("length_"):
        if not isinstance(actual, (list, dict, str)):
            return False, f"subject has no length ({type(actual).__name__})"
        left, label = len(actual), f"length {len(actual)}"
        comparator = comparator[len("length_"):]
    else:
        left, label = actual, repr(actual)

    if not isinstance(left, (int, float)) or isinstance(left, bool):
        return False, f"subject is not numeric ({label})"
    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        return False, f"comparator value {expected!r} is not numeric"

    operations = {
        "gt": (left > expected, ">"),
        "gte": (left >= expected, ">="),
        "lt": (left < expected, "<"),
        "lte": (left <= expected, "<="),
        "equals": (left == expected, "=="),
    }
    if comparator not in operations:
        return False, f"unknown comparator {comparator!r}"
    ok, symbol = operations[comparator]
    return ok, (f"expected {label} {symbol} {expected}" if not ok else f"{label} {symbol} {expected}")


# ---------------------------------------------------------------------------
# Request execution
# ---------------------------------------------------------------------------


@dataclass
class ApiRunResult:
    """Everything one ``api_test`` produced — verdict plus the raw basis."""

    status: str = STATUS_FAILED
    reasons: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_verdict_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


def _request_timeout(vp: Dict[str, Any]) -> int:
    raw = (vp.get("request") or {}).get("timeout_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(1, min(value, MAX_TIMEOUT_SECONDS))


def run_api_verification(
    vp: Dict[str, Any], *, artifact_dir: Optional[Path] = None,
) -> ApiRunResult:
    """Execute one ``api_test`` VP and grade it against its assertions.

    Deterministic end to end: same request + same response + same
    assertions ⇒ same verdict, every round. Never raises — a broken
    request is a FAILED verdict carrying the reason, not an exception
    the round has to survive.
    """
    vp_id = str(vp.get("id", "unknown"))
    result = ApiRunResult()

    issues = validate_vp(vp)
    if issues:
        result.reasons = [f"schema: {i.detail}" for i in issues]
        result.evidence = {"vp_id": vp_id, "schema_issues": [i.to_dict() for i in issues]}
        return result

    request = vp["request"]
    method = str(request["method"]).strip().upper()
    url = str(request["url"]).strip()
    headers = {
        str(k): str(v)
        for k, v in (request.get("headers") or {}).items()
        if isinstance(k, str)
    }
    body = request.get("body")
    timeout = _request_timeout(vp)

    payload: Optional[bytes] = None
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        else:
            payload = str(body).encode("utf-8")

    request_record = {
        "method": method, "url": url, "headers": headers,
        "body": body, "timeout_seconds": timeout,
    }

    try:
        req = urllib.request.Request(
            url, data=payload, headers=headers, method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = int(resp.status)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a *response*, not an execution failure — the
        # assertions decide, and ``{"status": 404}`` is a legitimate
        # acceptance criterion.
        status_code = int(exc.code)
        resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        try:
            raw_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw_body = ""
    except Exception as exc:  # noqa: BLE001 - network/timeout/DNS/…
        result.reasons = [
            f"请求未能完成：{type(exc).__name__}: {exc}",
            f"request: {method} {url}",
        ]
        result.evidence = {
            "vp_id": vp_id,
            "request": request_record,
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_artifact(artifact_dir, vp_id, {
            "vp_id": vp_id, "request": request_record,
            "error": f"{type(exc).__name__}: {exc}",
            "assertions": vp.get("assertions"),
        })
        return result

    failures: List[str] = []
    passes: List[str] = []
    for index, assertion in enumerate(vp["assertions"]):
        name = str(assertion.get("name") or f"assertions[{index}]")
        found, actual, label = _subject_value(
            assertion, status_code, resp_headers, raw_body,
        )
        comparator, expected = _comparator_and_expected(assertion)
        ok, explanation = _evaluate_comparator(
            comparator, expected, found, actual,
        )
        line = f"[{name}] {label}: {explanation}"
        (passes if ok else failures).append(line)

    body_excerpt = raw_body[:MAX_BODY_CHARS]
    result.status = STATUS_FAILED if failures else STATUS_PASSED
    if failures:
        result.reasons = failures
    else:
        result.reasons = [f"全部断言通过（{len(passes)} 条）：" + "；".join(passes)]
    result.evidence = {
        "vp_id": vp_id,
        "request": request_record,
        "status_code": status_code,
        "assertions_passed": passes,
        "assertions_failed": failures,
        "response_body": body_excerpt,
        "response_body_truncated": len(raw_body) > MAX_BODY_CHARS,
    }
    _write_artifact(artifact_dir, vp_id, {
        "vp_id": vp_id,
        "request": request_record,
        "status_code": status_code,
        "response_headers": resp_headers,
        "response_body": body_excerpt,
        "response_body_truncated": len(raw_body) > MAX_BODY_CHARS,
        "assertions": vp.get("assertions"),
        "assertions_passed": passes,
        "assertions_failed": failures,
    })
    return result


def _write_artifact(artifact_dir: Optional[Path], vp_id: str, payload: Dict[str, Any]) -> None:
    """Persist the raw basis of this verdict for post-hoc investigation.

    A failed write is logged and ignored: losing the audit copy must not
    change the verdict, which has already been computed from the live
    response.

    ``mkstemp`` allocates the ``.tmp`` file *before* the write, so a
    raise between there and ``os.replace`` (unserialisable payload,
    mid-write disk full) leaves a half-written tempfile on disk that has
    to be unlinked explicitly. Without that, ``.tmp`` files accumulate
    under ``plans/<id>/vp_artifacts/<vp>/`` across rounds — invisible to
    ``find_orphans``, which only reports non-``tmp`` files — so the
    residue silently grows until the next operator sweep.
    """
    if artifact_dir is None:
        return
    tmp_name: Optional[str] = None
    try:
        import os
        import tempfile

        target_dir = Path(artifact_dir) / vp_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "api_response.json"
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, target)
        tmp_name = None  # ownership transferred; do not unlink on success
    except Exception as exc:  # noqa: BLE001 - audit copy is best-effort
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning(
            "[api_runner] could not write artifact for %s: %s", vp_id, exc,
        )


# ---------------------------------------------------------------------------
# CLI — replay one api_test VP's assertions
# ---------------------------------------------------------------------------


def _load_vp_from_plan(plan_dir: Path, vp_id: str) -> Optional[Dict[str, Any]]:
    """Load ``vp_id`` from ``verification_plan.json``, placeholders resolved.

    The on-disk plan carries ``{{svc.<name>.<field>}}`` placeholders by
    design (C1: ports are declared once and re-resolved every round), so
    a replay has to expand them the same way a round does.
    """
    try:
        plan = json.loads(
            (Path(plan_dir) / "verification_plan.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read verification_plan.json: {exc}", file=sys.stderr)
        return None

    vps = plan.get("verification_points") or []
    vp = next(
        (v for v in vps if isinstance(v, dict) and str(v.get("id")) == vp_id),
        None,
    )
    if vp is None:
        return None

    try:
        from service_declaration import apply_resolution, parse_declarations

        parsed = parse_declarations(plan, Path(plan_dir).parent)
        resolved = apply_resolution(plan, parsed.by_name())
        vps = resolved.get("verification_points") or []
        vp = next(
            (v for v in vps if isinstance(v, dict) and str(v.get("id")) == vp_id),
            vp,
        )
    except Exception as exc:  # noqa: BLE001 - replay is best-effort
        print(
            f"warning: could not resolve service placeholders ({exc}); "
            f"running with the raw plan",
            file=sys.stderr,
        )
    return vp


def main(argv: Optional[List[str]] = None) -> int:
    """Replay one ``api_test`` VP. Exit 0 iff every assertion holds.

    This is what a repair task gets as its ``test_command`` (2026-09-18):
    the acceptance criterion the repair must satisfy, executed by the
    framework against the running service. It replaces the old "edit the
    VP's ``test_command`` in verification_plan.json" repair family — that
    one had the repair agent rewriting the very criterion it was being
    graded against, and it could never produce a project-side diff, so
    it was doomed to fail and split.

    Usage::

        python -m verification_api_runner --plan <plan_dir> --vp VP-014

    Placeholders (``{{svc.*}}``) are expanded against the plan's own
    ``services`` block, so the replay targets the same endpoint a round
    would.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Replay one api_test VP's assertions against the live service.",
    )
    parser.add_argument("--plan", required=True, help="plan directory")
    parser.add_argument("--vp", required=True, help="verification point id")
    parser.add_argument(
        "--artifact-dir", default=None,
        help="where to write the raw response (default: <plan>/vp_artifacts)",
    )
    args = parser.parse_args(argv)

    plan_dir = Path(args.plan)
    vp = _load_vp_from_plan(plan_dir, args.vp)
    if vp is None:
        print(
            f"verification point {args.vp!r} not found in "
            f"{plan_dir / 'verification_plan.json'}",
            file=sys.stderr,
        )
        return 2

    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else (
        plan_dir / "vp_artifacts"
    )
    result = run_api_verification(vp, artifact_dir=artifact_dir)

    print(f"VP {args.vp}: {result.status}")
    for reason in result.reasons:
        print(f"  - {reason}")
    request = result.evidence.get("request") or {}
    if request:
        print(f"  request: {request.get('method')} {request.get('url')}")
    if result.evidence.get("status_code") is not None:
        print(f"  status_code: {result.evidence['status_code']}")
    return 0 if result.status == STATUS_PASSED else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    import sys

    raise SystemExit(main())
