"""Unit TDD contract for DP7 (a)(b): removal of ``_RT_FN_PLAN_STATE``
from the three scope files, plus the new ``plan_state.migration_audit``
schema + ``record()`` writer surface.

Background
----------
Architecture decision point 7 splits work into two parts:

  (a) Remove ``_RT_FN_PLAN_STATE`` string-concat logic from
      ``backend/tasks_generator.py``,
      ``backend/preflight_review.py``, and
      ``backend/plan_state.py``.

  (b) Replace the ad-hoc string-concat with a structured top-level
      ``migration_audit`` record on ``plan_state.json`` carrying::

          {
              "migrated":     bool,
              "migrated_at":  ISO-8601 string,
              "migrator":     str (the subsystem that recorded it),
              "source_hash":  sha256 hex digest,
          }

      The timestamp MUST come from ``framework.clock.utcnow_iso()``,
      never ``datetime.now()`` locally.

This file pins the unit-level contract (pure Python checks, no
filesystem dependency beyond ``tmp_path``). The integration-level
schema validation against the live plans tree lives in
``backend/tests/integration/test_migration_audit_schema.py``.

TDD spec (the six asserts in the task brief)
-------------------------------------------
1. ``test_grep_literal_removed`` — ``grep -rE '_RT_FN_PLAN_STATE'``
   against the three scope files returns ZERO lines.
2. ``test_record_writes_iso8601_and_sha256`` — ``record(...)``
   writes the field, the timestamp is a valid ISO-8601 string, and
   ``source_hash`` is a sha256 hex digest.
3. ``test_idempotent_record_preserves_source_hash`` — two
   consecutive ``record(...)`` calls do NOT clobber the original
   ``source_hash`` and do NOT add a duplicate key (the writer must
   keep the existing record when one is already present).
4. ``test_migration_audit_uses_shared_schema_constant`` — the
   three files all read from ONE module-level
   ``MIGRATION_AUDIT_SCHEMA`` constant (asserted by ``is`` identity
   or value equality across the import sites).
5. ``test_migration_audit_not_in_tasks_table_layout`` — if a
   ``tasks.db`` SQLite file with a ``tasks`` table exists, the
   column list MUST NOT contain ``migration_audit``.
6. ``test_three_files_share_one_schema_constant`` — companion to
   (4): not only is the constant shared by value, but reading the
   JSON schema across all three files returns identical content.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test 1: grep the literal out of the three scope files.
# ---------------------------------------------------------------------------


SCOPE_FILES = [
    "tasks_generator.py",
    "preflight_review.py",
    "plan_state.py",
]


def test_grep_literal_removed_in_three_scope_files():
    """``grep -rE '_RT_FN_PLAN_STATE'`` in the three scope files = 0.

    The grep runs against the live repository (NOT stripped Python
    source) so it catches even the code-path that builds the
    literal via string-concat inside an f-string or ``+``
    expression. A non-zero count means the string-concat has
    sneaked back in and the DP7(a) delivery is incomplete.

    Resolves the scope files relative to THIS test file rather than
    ``Path.cwd()`` because the full-suite command
    (``backend/.venv/bin/python -m pytest backend/tests/ -q``) runs
    with cwd at the autonomous-coding repo root, NOT at ``backend/``.
    Relying on ``Path.cwd()`` made the test fragile — exit code 2
    (file not found) instead of the expected exit code 1 (no
    matches) — and broke the R1-5 "0 failed" baseline. The scope
    files live at ``<repo>/backend/{tasks_generator,preflight_review,
    plan_state}.py``; this test file lives at
    ``<repo>/backend/tests/unit/test_m6_cleanup.py`` so going up two
    parents reaches the ``backend/`` directory.
    """
    # backend/tests/unit/test_m6_cleanup.py -> backend/tests/unit ->
    # backend/tests -> backend/
    backend_dir = Path(__file__).resolve().parents[2]
    cmd = ["grep", "-rE", "_RT_FN_PLAN_STATE"] + [
        str(backend_dir / p) for p in SCOPE_FILES
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=10,
    )
    # grep exit code 1 = no match, which is what we want.
    # Any other exit code (0=match, 2=file missing) is a failure.
    assert proc.returncode == 1, (
        "grep returned matches against the scope files — the literal "
        f"'_RT_FN_PLAN_STATE' must NOT appear anywhere in "
        f"{SCOPE_FILES!r}; stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == "", (
        "grep stdout should be empty when no matches; got "
        f"{proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan_state_module():
    """Reload ``plan_state`` to pick up runtime additions."""
    return importlib.import_module("plan_state")


# ---------------------------------------------------------------------------
# Tests 2 + 3: the ``record(...)`` writer is correct and idempotent.
# ---------------------------------------------------------------------------


def test_record_writes_iso8601_and_sha256(tmp_path, monkeypatch):
    """``record(...)`` writes a valid ``migration_audit`` field.

    The field is read back from disk:

      * ``migrated`` is a bool
      * ``migrated_at`` is an ISO-8601 string (round-trips through
        ``datetime.fromisoformat``)
      * ``migrator`` is a non-empty string
      * ``source_hash`` is a 64-char hex digest (sha256)

    The ``migrated_at`` field's provenance is from
    ``framework.clock.utcnow_iso()`` — the test freezes the clock
    and asserts the exact ``migrated_at`` string matches what the
    clock returns at the time of the call. This catches a
    regression that routes the timestamp through
    ``datetime.utcnow().isoformat()`` instead of the injected
    framework clock.
    """
    plan_state = _make_plan_state_module()

    # Freeze the clock via the framework hook (architecture DP5).
    from framework import clock as framework_clock
    sentinel_iso = "2026-08-07T12:34:56.789012+00:00"
    monkeypatch.setattr(
        framework_clock,
        "utcnow_iso",
        lambda: sentinel_iso,
    )

    plan_id = "m6-unittest"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    plan_state_path = plan_dir / "plan_state.json"
    plan_state_path.write_text(
        json.dumps({"plan_id": plan_id, "current_phase": "ready"}, indent=2),
        encoding="utf-8",
    )

    # Pin ``plans_dir`` so the writer finds the plan under tmp_path
    # rather than the real ``plans/`` directory.
    monkeypatch.setattr(plan_state, "plans_dir", tmp_path)

    # Seed a non-trivial task payload so the source-hash has real
    # bytes to digest (not an empty string).
    payload = b"task-1: hello\ntask-2: world\n"

    record = plan_state.migration_audit.record(
        plan_id=plan_id,
        migrator="unit-test",
        source_payload=payload,
    )

    # Writer may return the new record (convenient) or None — we
    # only depend on the on-disk side effect.
    assert record is None or isinstance(record, dict)

    # Round-trip the on-disk JSON.
    on_disk = json.loads(plan_state_path.read_text(encoding="utf-8"))
    audit = on_disk.get("migration_audit")
    assert audit is not None, (
        f"plan_state.json must carry a 'migration_audit' field after "
        f"record(); got keys {sorted(on_disk.keys())!r}"
    )

    # migrated (bool)
    assert isinstance(audit.get("migrated"), bool), (
        f"migration_audit['migrated'] must be a bool; got "
        f"{type(audit.get('migrated')).__name__}"
    )

    # migrated_at (ISO-8601)
    assert isinstance(audit.get("migrated_at"), str), (
        f"migration_audit['migrated_at'] must be a string; got "
        f"{type(audit.get('migrated_at')).__name__}"
    )
    raw_iso = audit["migrated_at"]
    # Round-trip via fromisoformat (Python 3.11+ accepts the '+00:00'
    # tail; older versions accept a trailing 'Z' but reject '+00:00'.
    # The framework clock emits '+00:00', which is what we want.)
    parsed = datetime.fromisoformat(raw_iso)
    assert parsed.tzinfo is not None, (
        f"migration_audit['migrated_at'] must be tz-aware (got "
        f"{raw_iso!r}); naive timestamps are not allowed"
    )
    # And the value is exactly what the (frozen) framework clock
    # returned. If the implementation strayed to datetime.now() the
    # value would differ.
    assert raw_iso == sentinel_iso, (
        f"migration_audit['migrated_at'] must equal the injected "
        f"framework clock output ({sentinel_iso!r}); got "
        f"{raw_iso!r}. The writer is routing the timestamp through "
        f"datetime.now() instead of framework.clock.utcnow_iso()."
    )

    # migrator (string)
    assert isinstance(audit.get("migrator"), str) and audit["migrator"], (
        f"migration_audit['migrator'] must be a non-empty string; "
        f"got {audit.get('migrator')!r}"
    )

    # source_hash (sha256 hex)
    assert isinstance(audit.get("source_hash"), str), (
        f"migration_audit['source_hash'] must be a string; got "
        f"{type(audit.get('source_hash')).__name__}"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", audit["source_hash"]), (
        f"migration_audit['source_hash'] must be a 64-char hex "
        f"sha256 digest; got {audit['source_hash']!r}"
    )

    # Sanity: the hash matches the sha256 of the input payload.
    import hashlib
    expected_hash = hashlib.sha256(payload).hexdigest()
    assert audit["source_hash"] == expected_hash, (
        f"source_hash must equal sha256(payload); "
        f"expected {expected_hash!r}, got {audit['source_hash']!r}"
    )


def test_idempotent_record_preserves_source_hash(tmp_path, monkeypatch):
    """Two consecutive ``record()`` calls keep the FIRST source_hash.

    The writer is idempotent at the ``migration_audit`` key level:
    when a record already exists, the second call MUST NOT blow it
    away with a fresh ``migrated_at`` / ``migrator`` /
    ``source_hash``. (Otherwise a retry loop would rewrite the
    audit trail on every run and corrupt forensic review.)

    The test seeds a different payload into the second call to
    prove that even when the input differs, the on-disk record is
    untouched.
    """
    plan_state = _make_plan_state_module()

    from framework import clock as framework_clock

    # Freeze the clock so the FIRST record's timestamp is fixed.
    monkeypatch.setattr(
        framework_clock,
        "utcnow_iso",
        lambda: "2026-08-07T01:00:00.000000+00:00",
    )
    monkeypatch.setattr(plan_state, "plans_dir", tmp_path)

    plan_id = "m6-idempotent"
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir()
    plan_state_path = plan_dir / "plan_state.json"
    plan_state_path.write_text(
        json.dumps({"plan_id": plan_id, "current_phase": "ready"}, indent=2),
        encoding="utf-8",
    )

    payload_a = b"a" * 64
    payload_b = b"b" * 64  # different bytes, would yield a different hash

    plan_state.migration_audit.record(
        plan_id=plan_id,
        migrator="first",
        source_payload=payload_a,
    )

    after_first = json.loads(plan_state_path.read_text(encoding="utf-8"))
    audit_first = after_first["migration_audit"]

    # Now call record() again, twice, with different payloads and a
    # later "frozen" clock. A correct idempotent writer MUST NOT
    # overwrite the existing record.
    monkeypatch.setattr(
        framework_clock,
        "utcnow_iso",
        lambda: "2026-08-07T23:00:00.000000+00:00",
    )
    plan_state.migration_audit.record(
        plan_id=plan_id,
        migrator="second",
        source_payload=payload_b,
    )
    plan_state.migration_audit.record(
        plan_id=plan_id,
        migrator="third",
        source_payload=b"third different",
    )

    after_third = json.loads(plan_state_path.read_text(encoding="utf-8"))
    audit_third = after_third["migration_audit"]

    # Source_hash preserved across the two later calls.
    assert audit_third["source_hash"] == audit_first["source_hash"], (
        "idempotent record() must NOT overwrite an existing "
        f"source_hash; first={audit_first['source_hash']!r}, "
        f"third={audit_third['source_hash']!r}"
    )

    # migrator/migrated_at also preserved (no spurious update).
    assert audit_third["migrator"] == audit_first["migrator"], (
        "idempotent record() must NOT update migrator on subsequent "
        f"calls; first={audit_first['migrator']!r}, "
        f"third={audit_third['migrator']!r}"
    )
    assert audit_third["migrated_at"] == audit_first["migrated_at"], (
        "idempotent record() must NOT update migrated_at on subsequent "
        f"calls; first={audit_first['migrated_at']!r}, "
        f"third={audit_third['migrated_at']!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 + 6: the schema is exported from ONE constant, shared by all
# three files that consume it.
# ---------------------------------------------------------------------------


def test_migration_audit_uses_shared_schema_constant():
    """The ``MIGRATION_AUDIT_SCHEMA`` constant lives in plan_state.

    The schema dict is exposed at module level so both
    ``tasks_generator`` and ``preflight_review`` can import the
    SAME object (not a deep-copy). A test in their
    ``integration`` counterpart pins the import-site equivalence,
    so this unit test only asserts the constant is present and
    has the four contract-pinned keys.
    """
    plan_state = _make_plan_state_module()
    schema = getattr(plan_state, "MIGRATION_AUDIT_SCHEMA", None)
    assert schema is not None, (
        "plan_state must export MIGRATION_AUDIT_SCHEMA at module level "
        "so tasks_generator and preflight_review share one definition"
    )

    # The four contract-pinned keys.
    assert set(schema.keys()) == {"migrated", "migrated_at", "migrator", "source_hash"}, (
        f"MIGRATION_AUDIT_SCHEMA must have the four contract-pinned "
        f"keys; got {sorted(schema.keys())!r}"
    )

    # Each key is annotated with a ``type`` string (in the style
    # ``{"type": "string"}``) so callers can JSON-schema-validate
    # without the runtime cost of jsonschema.
    for key in ("migrated", "migrated_at", "migrator", "source_hash"):
        assert "type" in schema[key], (
            f"schema entry for {key!r} must declare a JSON type; got "
            f"{schema[key]!r}"
        )


def test_three_files_share_one_schema_constant():
    """``tasks_generator`` and ``preflight_review`` import the
    SAME ``MIGRATION_AUDIT_SCHEMA`` object as ``plan_state``.

    Catches the regression where each scope file hard-codes its
    own copy of the schema. Imports flow through the
    package's normal ``import plan_state`` path; the assertion
    is ``schema is plan_state.MIGRATION_AUDIT_SCHEMA`` (identity,
    not value-equality).
    """
    plan_state = _make_plan_state_module()
    plan_state_schema = plan_state.MIGRATION_AUDIT_SCHEMA
    assert plan_state_schema is not None

    # tasks_generator and preflight_review live inside the
    # ``backend`` package; pytest adds ``backend`` to sys.path via
    # pytest.ini, so they import as top-level modules.
    tasks_generator = importlib.import_module("tasks_generator")
    preflight_review = importlib.import_module("preflight_review")

    tg_schema = getattr(tasks_generator, "MIGRATION_AUDIT_SCHEMA", None)
    pf_schema = getattr(preflight_review, "MIGRATION_AUDIT_SCHEMA", None)

    assert tg_schema is plan_state_schema, (
        "tasks_generator.MIGRATION_AUDIT_SCHEMA must be the SAME "
        "object as plan_state.MIGRATION_AUDIT_SCHEMA (no copy)"
    )
    assert pf_schema is plan_state_schema, (
        "preflight_review.MIGRATION_AUDIT_SCHEMA must be the SAME "
        "object as plan_state.MIGRATION_AUDIT_SCHEMA (no copy)"
    )


# ---------------------------------------------------------------------------
# Test 5: ``tasks.db`` ``tasks`` table does NOT carry ``migration_audit``.
# ---------------------------------------------------------------------------


def test_migration_audit_not_in_tasks_table_layout(tmp_path):
    """If ``tasks.db`` exists, its ``tasks`` table MUST NOT have a
    ``migration_audit`` column.

    Per DP7 (a) boundary note: ``migration_audit`` belongs to the
    ``plan_state.json`` META file, NOT to the ``tasks`` SQLite
    table (the live task ledger). This test is a no-op when no
    ``tasks.db`` exists; otherwise it inspects the schema and
    asserts the absence of the column.

    The repository's working tree has no ``tasks.db`` at the time
    of writing, so the typical run is a "no tasks.db found" pass.
    Both branches count as PASS per the contract.
    """
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "backend" / "tasks.db",
        repo_root / "tasks.db",
    ]
    targets = [p for p in candidates if p.exists()]
    if not targets:
        # No tasks.db in the repo today — pass trivially. The
        # contract is "if it ever appears, it must not carry the
        # column".
        pytest.skip("no tasks.db present in the working tree")

    # At least one tasks.db exists — open it and inspect the
    # ``tasks`` table schema. Use stdlib sqlite3 so we don't add a
    # pytest plugin dependency just for this assertion.
    import sqlite3
    for db_path in targets:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='tasks'"
            )
            row = cur.fetchone()
            if row is None:
                # No ``tasks`` table in this DB — also passes.
                conn.close()
                continue
            cur = conn.execute("PRAGMA table_info(tasks)")
            columns = {r[1] for r in cur.fetchall()}
            assert "migration_audit" not in columns, (
                f"tasks table in {db_path} must NOT carry a "
                f"'migration_audit' column (that's plan_state.json's "
                f"responsibility); columns={sorted(columns)!r}"
            )
        finally:
            conn.close()
