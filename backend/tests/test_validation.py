"""Input validation boundary tests.

VP-013: Input validation boundary test
- test_input_validation: Validates that invalid inputs (empty, over-length, special chars)
  return 4xx status codes and don't trigger system exceptions (500).
"""

import json

import pytest
from fastapi.testclient import TestClient

from server import app


client = TestClient(app)


def call_api(path: str, payload: dict, timeout: float = 5.0):
    """Make a POST request and return (status_code, response_body)."""
    response = client.post(
        path,
        json=payload,
    )
    return response.status_code, response.text


def test_input_validation():
    """
    VP-013: test_input_validation

    Tests the backend's input validation for string fields.
    Expected: Invalid inputs (empty, over-length, special chars) return 4xx
    status codes and don't trigger 5xx system exceptions.
    """

    # Test cases targeting POST /api/interview/start (accepts requirement: str)
    test_cases = [
        {
            "name": "empty_string",
            "payload": {"requirement": ""},
            "expected_status_range": (400, 499),
        },
        {
            "name": "whitespace_only",
            "payload": {"requirement": "   \t\n   "},
            "expected_status_range": (400, 499),
        },
        {
            "name": "overlong_requirement",
            "payload": {"requirement": "a" * 10001},
            "expected_status_range": (400, 499),
        },
        {
            "name": "sql_injection",
            "payload": {"requirement": "'; DROP TABLE users; --"},
            "expected_status_range": (400, 499),
        },
        {
            "name": "nosql_injection",
            "payload": {"requirement": {"$ne": None}},
            "expected_status_range": (400, 499),
        },
        {
            "name": "xss_injection",
            "payload": {"requirement": "<script>alert(1)</script>"},
            "expected_status_range": (400, 499),
        },
        {
            "name": "unicode_injection",
            "payload": {"requirement": "\x00\x01\x02"},
            "expected_status_range": (400, 499),
        },
        {
            "name": "null_requirement",
            "payload": {"requirement": None},
            "expected_status_range": (400, 499),
        },
        {
            "name": "missing_requirement",
            "payload": {},
            "expected_status_range": (400, 499),
        },
    ]

    passed = 0
    failed = 0
    errors = []

    for test_case in test_cases:
        case_name = test_case["name"]
        payload = test_case["payload"]
        expected_range = test_case["expected_status_range"]

        status_code, body = call_api("/api/interview/start", payload)

        # Check: must return a 4xx status, not 5xx (system exception)
        if status_code == 500:
            failed += 1
            errors.append(
                f"[{case_name}] API returned 500 Internal Server Error — "
                f"invalid input triggered system exception: {body[:200]}"
            )
            continue

        if not (expected_range[0] <= status_code <= expected_range[1]):
            failed += 1
            errors.append(
                f"[{case_name}] Expected 4xx status, got {status_code}: {body[:200]}"
            )
            continue

        # Verify response body is valid JSON with error detail
        try:
            body_json = json.loads(body)
            assert "detail" in body_json, (
                f"[{case_name}] Response missing 'detail' field: {body[:200]}"
            )
        except (json.JSONDecodeError, AssertionError) as e:
            failed += 1
            errors.append(f"[{case_name}] {e}: {body[:200]}")
            continue

        passed += 1

    # Summary assertions
    assert failed == 0, (
        f"VP-013 input validation tests failed ({failed}/{len(test_cases)}):\n"
        + "\n".join(errors)
    )
    assert passed == len(test_cases), (
        f"Expected {len(test_cases)} passed, got {passed}"
    )

    print(f"\nVP-013 test_input_validation: {passed}/{len(test_cases)} passed — API correctly rejects invalid inputs with 4xx")
