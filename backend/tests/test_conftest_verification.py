"""
Unit tests for ``conftest_verification.py`` fixtures.

The conftest provides reusable factory functions and a streaming
log parser so verification tests can be written data-driven without
duplicating VP / plan / log scaffolding.

These tests pin three contracts:

1. ``make_vp`` — default values are sensible (sleep=1.0, single
   ``;``-free clause, ``should_fail=False``); kwargs override them.
2. ``make_verification_plan`` — wraps VPs in the standard
   ``{"verification_points": [...]}`` envelope and respects a
   custom ``plan_id``.
3. ``parse_execution_log`` — returns a streaming object with
   ``.filter(event=...)`` and ``.group_by(event, key)`` query
   methods that operate on the JSON-lines log format the
   persistence layer writes.
"""

import json

import pytest


# Make the conftest importable when pytest is launched from either
# the project root or the `backend/` directory.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# The conftest lives under backend/tests/, but conftest.py itself is
# auto-loaded by pytest. We import its members explicitly to pin the
# public surface in one place — callers should reach for these names
# via the autouse fixtures, not by re-importing.
import importlib.util

_CONFTEST_PATH = Path(__file__).parent / "conftest_verification.py"
_spec = importlib.util.spec_from_file_location("conftest_verification", _CONFTEST_PATH)
assert _spec and _spec.loader, f"conftest_verification.py not found at {_CONFTEST_PATH}"
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)

make_vp = _conftest.make_vp
make_verification_plan = _conftest.make_verification_plan
parse_execution_log = _conftest.parse_execution_log


# -----------------------------------------------------------------------------
# make_vp defaults
# -----------------------------------------------------------------------------


class TestMakeVPDefaults:
    """``make_vp()`` must return a usable VP without explicit kwargs."""

    def test_conftest_make_vp_default_values(self):
        """Default VP: id=VP-001, single clause, sleep=1.0, should_fail=False.

        Pinned because the conftest factory is used by every other
        verification test — silently changing a default would
        cascade into dozens of test breakages with no obvious root
        cause.
        """
        vp = make_vp()

        assert vp["id"] == "VP-001"
        assert vp["verification_method"] == "automated_test"
        # Default expected_result is a single clause (no ';')
        # so the splitter won't decompose it on timeout.
        assert ";" not in vp["expected_result"]
        # The sleep/should_fail defaults are the values the fake
        # backend reads when callers don't override.
        assert vp.get("fake_sleep_seconds", 1.0) == 1.0
        assert vp.get("should_fail", False) is False

    def test_conftest_make_vp_kwargs_override(self):
        """Explicit kwargs override the defaults."""
        vp = make_vp(
            id="VP-XYZ",
            verification_method="ui_validation",
            expected_result="A; B",
            fake_sleep_seconds=2.5,
            should_fail=True,
        )

        assert vp["id"] == "VP-XYZ"
        assert vp["verification_method"] == "ui_validation"
        assert vp["expected_result"] == "A; B"
        assert vp["fake_sleep_seconds"] == 2.5
        assert vp["should_fail"] is True


# -----------------------------------------------------------------------------
# make_verification_plan
# -----------------------------------------------------------------------------


class TestMakeVerificationPlan:
    """``make_verification_plan`` wraps VPs in the standard envelope."""

    def test_conftest_make_verification_plan_envelope(self):
        """Returns a dict with ``verification_points`` and ``plan_id``."""
        plan = make_verification_plan(
            [make_vp(id="VP-1"), make_vp(id="VP-2")],
            plan_id="plan-abc",
        )

        assert plan["plan_id"] == "plan-abc"
        assert len(plan["verification_points"]) == 2
        assert [vp["id"] for vp in plan["verification_points"]] == ["VP-1", "VP-2"]


# -----------------------------------------------------------------------------
# parse_execution_log — streaming API
# -----------------------------------------------------------------------------


def _write_log(path, events):
    """Write a list of events as JSON-lines to ``path``."""
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


class TestParseExecutionLog:
    """``parse_execution_log`` returns a streaming query object."""

    def test_conftest_parse_execution_log_filter_event(self, tmp_path):
        """``filter(event='group_started')`` returns only matching rows."""
        log_path = tmp_path / "execution.log"
        _write_log(
            log_path,
            [
                {"event": "group_started", "group": "ui", "ts": "2026-06-05T10:00:00"},
                {"event": "vp_started", "vp_id": "VP-001", "ts": "2026-06-05T10:00:01"},
                {"event": "group_started", "group": "automated_test",
                 "ts": "2026-06-05T10:00:02"},
                {"event": "vp_completed", "vp_id": "VP-001", "ts": "2026-06-05T10:00:03"},
            ],
        )

        stream = parse_execution_log(log_path)
        group_starts = stream.filter(event="group_started")

        assert [row["group"] for row in group_starts] == ["ui", "automated_test"]

    def test_conftest_parse_execution_log_group_by(self, tmp_path):
        """``group_by('vp_started', 'vp_id')`` returns a dict keyed by vp_id."""
        log_path = tmp_path / "execution.log"
        _write_log(
            log_path,
            [
                {"event": "vp_started", "vp_id": "VP-001", "attempt": 1},
                {"event": "vp_started", "vp_id": "VP-002", "attempt": 1},
                {"event": "vp_completed", "vp_id": "VP-001"},
            ],
        )

        stream = parse_execution_log(log_path)
        grouped = stream.group_by("vp_started", "vp_id")

        assert set(grouped.keys()) == {"VP-001", "VP-002"}
        assert grouped["VP-001"]["attempt"] == 1
        assert grouped["VP-002"]["attempt"] == 1
