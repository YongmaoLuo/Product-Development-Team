"""Validate the diagnosis JSON produced by 8-1-1.

This test file treats ``backend/tests/fixtures/diagnosis.json`` as the
artifact under test.  It verifies:

1. The JSON file exists and is valid JSON.
2. It contains exactly 10 failures.
3. Each failure carries the required fields.
4. ``bucket`` values belong to the allowed vocabulary.
5. ``error_type`` values belong to the allowed vocabulary.
6. ``line_no`` values match the line numbers reported by
   ``pytest --collect-only -q`` for the referenced test ids.
7. The referenced pytest source module exists.

The fixture target module is ``backend/tests/fixtures/diagnosis_target.py``;
its test functions are intentionally no-ops so the validation tests can focus
on the diagnosis JSON structure rather than on actual runtime failures.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Project root is four directories up from this file
# (backend/tests/unit/test_diagnosis_output.py -> project root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DIAGNOSIS_PATH = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "diagnosis.json"
TARGET_MODULE = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "diagnosis_target.py"

REQUIRED_FAILURE_FIELDS = {"test_id", "line_no", "bucket", "error_type", "message"}

ALLOWED_BUCKETS = {
    "assertion",
    "import",
    "timeout",
    "syntax",
    "missing_fixture",
    "runtime",
    "collection",
}

ALLOWED_ERROR_TYPES = {
    "AssertionError",
    "ImportError",
    "ModuleNotFoundError",
    "asyncio.TimeoutError",
    "pytest_timeout",
    "SyntaxError",
    "MissingFixture",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "KeyError",
    "AttributeError",
}


def _load_diagnosis() -> dict:
    """Load and return the diagnosis JSON document."""
    with open(DIAGNOSIS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _collect_line_numbers() -> dict[str, int]:
    """Run ``pytest --collect-only -q`` against the target module and map
    collected test ids to their source line numbers.

    ``pytest --collect-only -q`` emits node ids (e.g.
    ``backend/tests/fixtures/diagnosis_target.py::test_name``).  We resolve
    line numbers by importing the module and inspecting the function
    definitions, which keeps the test independent of pytest's internal
    reporting format and avoids caching the line numbers.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(TARGET_MODULE.relative_to(PROJECT_ROOT)),
            "--collect-only",
            "-q",
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pytest --collect-only -q failed: {proc.stdout}\n{proc.stderr}"
        )

    node_ids: list[str] = []
    current_module = TARGET_MODULE.relative_to(PROJECT_ROOT).as_posix()
    for raw_line in proc.stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        # Tree format from ``pytest --collect-only -q``:
        #   <Module diagnosis_target.py>
        #     <Function test_bucket_assertion_alpha>
        if stripped.startswith("<Module "):
            match = re.search(r"<Module\s+(.+?)>", stripped)
            if match:
                current_module = match.group(1)
                # If pytest reports just a basename, resolve it against the
                # target module's parent directory so node ids stay stable.
                if "/" not in current_module and "\\" not in current_module:
                    current_module = (
                        TARGET_MODULE.relative_to(PROJECT_ROOT).as_posix()
                    )
        elif stripped.startswith("<Function "):
            match = re.search(r"<Function\s+(.+?)>", stripped)
            if match:
                func_name = match.group(1)
                node_ids.append(f"{current_module}::{func_name}")

    # Resolve line numbers from the target module's source.
    import ast

    source = TARGET_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(TARGET_MODULE))
    func_lines = {
        node.name: node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }

    line_map: dict[str, int] = {}
    for node_id in node_ids:
        parts = node_id.split("::")
        if len(parts) < 2:
            continue
        func_name = parts[-1]
        if func_name in func_lines:
            line_map[node_id] = func_lines[func_name]

    return line_map


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_diagnosis_json_exists():
    """The diagnosis JSON file must exist and be parseable."""
    assert DIAGNOSIS_PATH.is_file(), f"diagnosis file not found: {DIAGNOSIS_PATH}"
    data = _load_diagnosis()
    assert isinstance(data, dict), "diagnosis root must be a JSON object"
    assert "failures" in data, "diagnosis must contain a 'failures' key"


def test_diagnosis_has_10_failures():
    """The diagnosis must summarize exactly 10 failures."""
    data = _load_diagnosis()
    failures = data.get("failures", [])
    assert isinstance(failures, list), "failures must be a list"
    assert len(failures) == 10, f"expected 10 failures, got {len(failures)}"
    assert data.get("total_failures") == 10, "total_failures must equal 10"


def test_diagnosis_each_failure_has_required_fields():
    """Every failure entry must contain the required schema fields."""
    data = _load_diagnosis()
    failures = data.get("failures", [])
    for i, failure in enumerate(failures):
        assert isinstance(failure, dict), f"failure[{i}] is not an object"
        missing = REQUIRED_FAILURE_FIELDS - set(failure.keys())
        assert not missing, f"failure[{i}] missing fields: {missing}"
        assert isinstance(failure["test_id"], str) and failure["test_id"]
        assert isinstance(failure["line_no"], int) and failure["line_no"] > 0
        assert isinstance(failure["bucket"], str) and failure["bucket"]
        assert isinstance(failure["error_type"], str) and failure["error_type"]
        assert isinstance(failure["message"], str)


def test_diagnosis_bucket_is_valid():
    """All bucket values must belong to the allowed vocabulary."""
    data = _load_diagnosis()
    failures = data.get("failures", [])
    for failure in failures:
        bucket = failure["bucket"]
        assert bucket in ALLOWED_BUCKETS, f"invalid bucket {bucket!r}"


def test_diagnosis_error_type_is_valid():
    """All error_type values must belong to the allowed vocabulary."""
    data = _load_diagnosis()
    failures = data.get("failures", [])
    for failure in failures:
        error_type = failure["error_type"]
        assert error_type in ALLOWED_ERROR_TYPES, f"invalid error_type {error_type!r}"


def test_diagnosis_line_numbers_match_pytest():
    """Each failure's line_no must match pytest's current collection view."""
    data = _load_diagnosis()
    failures = data.get("failures", [])
    pytest_lines = _collect_line_numbers()

    for failure in failures:
        test_id = failure["test_id"]
        expected_line = failure["line_no"]
        actual_line = pytest_lines.get(test_id)
        assert actual_line is not None, f"{test_id} not found by pytest collection"
        assert expected_line == actual_line, (
            f"line_no mismatch for {test_id}: "
            f"diagnosis={expected_line}, pytest={actual_line}"
        )


def test_diagnosis_real_pytest_source():
    """The referenced pytest source module must exist on disk."""
    data = _load_diagnosis()
    failures = data.get("failures", [])
    for failure in failures:
        test_id = failure["test_id"]
        # Node id format: path/to/file.py::FunctionName or path::Class::method
        file_part = test_id.split("::")[0]
        source_path = PROJECT_ROOT / file_part
        assert source_path.is_file(), f"source file not found for {test_id}: {source_path}"
