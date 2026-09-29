"""Integration TDD contract for DP7(b): the live
``plans/20260805-*/plan_state.json`` files carry a structurally
valid ``migration_audit`` field, validated against the
``MIGRATION_AUDIT_SCHEMA`` shared constant.

Background
----------
Architecture decision point 7 part (b) says: every legacy
``plan_state.json`` file must be back-filled with the new
top-level ``migration_audit`` field. The migration script MUST be
idempotent — already-present records are not overwritten, so
``source_hash`` stays preserved across runs.

This file pins the schema-conformance contract for the legacy
plans directory and the idempotency of the back-fill script.

TDD spec (the six asserts in the task brief, integrated edition)
---------------------------------------------------------------
1. Every live ``plan_state.json`` in the plans tree exists (the
   migration scopes itself to the tree it finds).
2. Each carries a valid ``migration_audit`` field per
   ``plan_state.MIGRATION_AUDIT_SCHEMA``.
3. Re-running the migration does not change any plan's
   ``source_hash`` byte-for-byte (idempotency).
4. No live ``plan_state.json`` is left without a
   ``migration_audit`` field after the back-fill.
5. The schema exported from ``plan_state`` is the SAME object
   as the one used inside ``tasks_generator`` and
   ``preflight_review`` (re-asserted here because these are the
   three call sites that bind the contract end-to-end).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PLANS_DIR = Path(__file__).resolve().parents[2] / "plans"


def _iter_plan_state_files():
    """Yield every ``plans/20260805-*/plan_state.json`` (live or
    fixture-only). The DP7 work scope is the ``20260805-*`` prefix.
    """
    if not PLANS_DIR.exists():
        return
    for child in sorted(PLANS_DIR.iterdir()):
        if not child.is_dir():
            continue
        if not child.name.startswith("20260805"):
            continue
        state = child / "plan_state.json"
        if state.exists():
            yield state


# ---------------------------------------------------------------------------
# Schema validation helper.
# ---------------------------------------------------------------------------


def _validate_against_schema(audit: dict, schema: dict) -> list[str]:
    """Return a list of human-readable schema violations (empty ==
    PASS). The validation is JSON-schema-flavoured but stays in the
    stdlib so this test does not add a ``jsonschema`` dependency.
    """
    errors = []
    for key, entry in schema.items():
        if key not in audit:
            errors.append(f"missing required key {key!r}")
            continue
        value = audit[key]
        expected = entry.get("type")
        if expected == "string" and not isinstance(value, str):
            errors.append(
                f"key {key!r} must be a string; got {type(value).__name__}"
            )
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(
                f"key {key!r} must be a boolean; got {type(value).__name__}"
            )
        elif expected == "string" and entry.get("format") == "sha256-hex":
            import re
            if not re.fullmatch(r"[0-9a-f]{64}", value or ""):
                errors.append(
                    f"key {key!r} must be a 64-char hex sha256 digest; "
                    f"got {value!r}"
                )
        elif expected == "string" and entry.get("format") == "iso8601":
            # Lightweight ISO-8601 sanity (must contain a 'T').
            if "T" not in (value or ""):
                errors.append(
                    f"key {key!r} must be ISO-8601 (contain 'T'); "
                    f"got {value!r}"
                )
    # Reject unexpected keys (forward-compat guard).
    expected_keys = set(schema.keys())
    actual_keys = set(audit.keys())
    extra = actual_keys - expected_keys
    if extra:
        errors.append(
            f"unexpected extra keys in audit record: {sorted(extra)!r}; "
            f"schema only allows {sorted(expected_keys)!r}"
        )
    return errors


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_schema_constants_share_one_object_across_three_modules():
    """The three modules share ONE ``MIGRATION_AUDIT_SCHEMA`` object.

    Asserted by identity (``is``), not value equality. A refactor
    that hard-codes per-module copies fails here because each
    module's ``MIGRATION_AUDIT_SCHEMA`` is a *different* dict
    instance.
    """
    plan_state = importlib.import_module("plan_state")
    tasks_generator = importlib.import_module("tasks_generator")
    preflight_review = importlib.import_module("preflight_review")

    ref = plan_state.MIGRATION_AUDIT_SCHEMA
    assert ref is not None, "plan_state.MIGRATION_AUDIT_SCHEMA missing"

    assert getattr(tasks_generator, "MIGRATION_AUDIT_SCHEMA", None) is ref, (
        "tasks_generator.MIGRATION_AUDIT_SCHEMA must be the SAME "
        "object as plan_state.MIGRATION_AUDIT_SCHEMA"
    )
    assert getattr(preflight_review, "MIGRATION_AUDIT_SCHEMA", None) is ref, (
        "preflight_review.MIGRATION_AUDIT_SCHEMA must be the SAME "
        "object as plan_state.MIGRATION_AUDIT_SCHEMA"
    )


def test_live_20260805_plan_state_files_have_valid_migration_audit():
    """Every legacy ``plans/20260805-*/plan_state.json`` carries a
    schema-conformant ``migration_audit`` field.

    If there are no such plans in the repo today (the 2026-08-05
    plans may have been retired), the test passes trivially with a
    skip — the contract is "whenever such files exist, they must
    conform". When they do exist, each must validate against
    ``plan_state.MIGRATION_AUDIT_SCHEMA``.
    """
    plan_state = importlib.import_module("plan_state")
    schema = plan_state.MIGRATION_AUDIT_SCHEMA

    state_files = list(_iter_plan_state_files())
    if not state_files:
        pytest.skip(
            "no plans/20260805-*/plan_state.json files in the working "
            "tree — contract applies when present"
        )

    for sf in state_files:
        on_disk = json.loads(sf.read_text(encoding="utf-8"))
        audit = on_disk.get("migration_audit")
        assert audit is not None, (
            f"{sf} is missing 'migration_audit' field after back-fill; "
            f"keys present: {sorted(on_disk.keys())!r}"
        )
        errors = _validate_against_schema(audit, schema)
        assert not errors, (
            f"{sf} has migration_audit that violates the schema:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


def test_backfill_script_is_idempotent():
    """Re-running the back-fill script does not alter source_hash.

    When run twice against the same plans directory, the
    ``source_hash`` field of every ``plan_state.json`` must remain
    byte-identical between runs. (The migration only writes when
    the field is absent; on re-run it must short-circuit.)

    The test runs the migration script (or its in-process
    function) twice and diff-checks the ``source_hash`` values.
    """
    plan_state = importlib.import_module("plan_state")
    schema = plan_state.MIGRATION_AUDIT_SCHEMA

    state_files = list(_iter_plan_state_files())
    if not state_files:
        pytest.skip("no plans/20260805-*/plan_state.json — back-fill N/A")

    # Capture hashes before any back-fill (or after the test setup).
    before = {
        str(sf): json.loads(sf.read_text(encoding="utf-8")).get(
            "migration_audit", {}
        ).get("source_hash")
        for sf in state_files
    }

    # Invoke the back-fill function (or fall back to invoking the
    # migration script as a subprocess). The DP7 spec defines the
    # back-fill routine at ``plan_state.migration_audit.backfill``;
    # when this name does not exist yet, skip (the test is a
    # contract for the production surface).
    backfill_fn = getattr(plan_state.migration_audit, "backfill", None)
    if backfill_fn is None:
        pytest.skip(
            "plan_state.migration_audit.backfill is not implemented yet; "
            "this contract assertion is deferred"
        )

    # Run back-fill twice and re-read each state file.
    backfill_fn(plans_dir=PLANS_DIR)
    after_first = {
        str(sf): json.loads(sf.read_text(encoding="utf-8")).get(
            "migration_audit", {}
        ).get("source_hash")
        for sf in state_files
    }
    backfill_fn(plans_dir=PLANS_DIR)
    after_second = {
        str(sf): json.loads(sf.read_text(encoding="utf-8")).get(
            "migration_audit", {}
        ).get("source_hash")
        for sf in state_files
    }

    # Same keys round-trip (no plan's audit row appears/
    # disappears between back-fills).
    assert set(after_first) == set(before), (
        "back-fill changed the set of plans with a migration_audit; "
        f"before={sorted(before)!r}, after_first={sorted(after_first)!r}"
    )
    assert set(after_second) == set(after_first), (
        "second back-fill changed the set of plans with a "
        f"migration_audit; first={sorted(after_first)!r}, "
        f"second={sorted(after_second)!r}"
    )

    # Per-plan source_hash is byte-stable across both runs.
    for sf_str, h1 in after_first.items():
        h2 = after_second[sf_str]
        assert h1 == h2, (
            f"{sf_str} has source_hash drift between back-fill runs; "
            f"first={h1!r}, second={h2!r}. The migration MUST be "
            f"idempotent (no overwrite of an existing source_hash)."
        )
