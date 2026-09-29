"""
TDD tests for the archive-scan module.

Background
----------
The state-machine refactor introduces a hard 2026-08-05 cutoff
between "active" plans (kept in SQLite) and "archived" plans
(rendered read-only via 410 Gone semantics). The two new modules
pin the boundary-value judgement:

  * :func:`classify_plan` — pure function, dirname priority over
    mtime, safe downgrade to "archived" on parse failure.

  * :func:`scan_archived_plans` — directory scan that returns only
    directory metadata (plan_id, requirement_first_line,
    created_at, archived=True). Deliberately NOT stage/current_phase
    — archived plans do not enter the SQLite state machine.

  * :func:`scan_artifacts` — directory scan that derives artifact
    status purely from file existence (no JSON parsing, no schema
    awareness). Archived plans surface their artifacts via this
    list instead of ``plan_artifacts`` table writes.

The 8 TDD tests below pin each contract independently so a future
refactor that drops one (e.g. removes the dirname-priority rule,
or adds a ``stage`` field to ``scan_archived_plans``) is caught by
the corresponding test failing RED.

Edge cases (boundary conditions):
  - ``cutoff`` is the pinned 2026-08-05 00:00 local datetime; the
    ``<= cutoff`` boundary is **钉死 (nailed down)** to "archived".
  - Unparsable dirname -> "archived" (safe downgrade).
  - Missing data_dir -> scan returns [] (no exception).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Provide a scratch data directory for archive-scan tests.

    Each test starts from an empty tmp_path so tests cannot leak
    directories into each other.
    """
    return tmp_path


# ---------------------------------------------------------------------------
# Tests for classify_plan boundary values
# ---------------------------------------------------------------------------


def test_classify_cutoff_minus_one_day_is_archived(data_dir: Path) -> None:
    """Plan dated one day before the cutoff is archived."""
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "20260804-x"
    plan_dir.mkdir(parents=True)
    result = classify_plan(plan_dir, datetime(2026, 8, 5))
    assert result == "archived"


def test_classify_cutoff_minus_one_second_is_archived(data_dir: Path) -> None:
    """Plan dated one second before the cutoff is archived."""
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "20260804-x"
    plan_dir.mkdir(parents=True)
    cutoff = datetime(2026, 8, 5, 0, 0, 1)
    result = classify_plan(plan_dir, cutoff)
    assert result == "archived"


def test_classify_cutoff_exact_moment_is_archived(data_dir: Path) -> None:
    """Plan dated EXACTLY at the cutoff moment is archived (boundary pinned).

    The spec says: ``<= cutoff`` -> "archived". The cutoff moment
    itself is therefore archived; this test is the explicit pinning
    of that boundary value. Future regressions (e.g. switching to
    ``< cutoff``) must break this test.
    """
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "20260805-x"
    plan_dir.mkdir(parents=True)
    cutoff = datetime(2026, 8, 5)
    result = classify_plan(plan_dir, cutoff)
    assert result == "archived"


def test_classify_cutoff_plus_one_second_is_new(data_dir: Path) -> None:
    """Plan dated one day after the cutoff is new (not archived).

    The ``YYYYMMDD`` dirname encoding has only day precision, so
    "cutoff + 1 second" cannot be expressed in the dirname. We
    therefore pin the contract to "next day after cutoff == new":
    a plan whose dirname resolves to the calendar day immediately
    following the cutoff must be classified "new".
    """
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "20260806-y"
    plan_dir.mkdir(parents=True)
    # Cutoff is 2026-08-05 00:00:01 — well into 08-05.
    cutoff = datetime(2026, 8, 5, 0, 0, 1)
    result = classify_plan(plan_dir, cutoff)
    assert result == "new"


def test_classify_cutoff_plus_one_day_is_new(data_dir: Path) -> None:
    """Plan dated one day after the cutoff is new."""
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "20260806-y"
    plan_dir.mkdir(parents=True)
    cutoff = datetime(2026, 8, 5)
    result = classify_plan(plan_dir, cutoff)
    assert result == "new"


# ---------------------------------------------------------------------------
# Tests for the dirname-vs-mtime precedence + safe-downgrade rule
# ---------------------------------------------------------------------------


def test_dirname_takes_precedence_over_mtime(data_dir: Path) -> None:
    """When dirname says 0801 (archived) but mtime is 0810 (new),
    the dirname wins — the plan is archived.

    Rationale: the dirname is the user-asserted creation date, and
    is the single source of truth for archive decisions. The mtime
    only flips the verdict when the dirname cannot be parsed.
    """
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "20260801-older"
    plan_dir.mkdir(parents=True)

    # Set mtime to 0810 12:00 — well AFTER the 0805 cutoff.
    new_mtime_ts = datetime(2026, 8, 10, 12, 0, 0).timestamp()
    import os
    os.utime(plan_dir, (new_mtime_ts, new_mtime_ts))

    cutoff = datetime(2026, 8, 5)
    result = classify_plan(plan_dir, cutoff)
    assert result == "archived", (
        f"dirname=20260801 must dominate mtime=20260810; got {result!r}"
    )


def test_unparsable_dirname_defaults_to_new(data_dir: Path) -> None:
    """A plan directory whose name is NOT in YYYYMMDD-* form is ``new``.

    2026-09-19: this test used to assert the opposite
    (``"archived"`` on a safe-side-downgrade argument). That rule was
    deliberately reversed — see :func:`classify_plan`'s docstring: it
    conflated "we don't know when this was created" with "this was
    created before 2026-08-05". The latter needs *evidence* of an old
    creation date, and an unparsable name supplies none. Non-conforming
    directories (test fixtures, ad-hoc working dirs, plans not yet
    promoted to the canonical naming convention) must not be silently
    archived just because their name does not parse.
    """
    from state_machine.db.archive_scan import classify_plan

    plan_dir = data_dir / "plans" / "no-date-prefix"
    plan_dir.mkdir(parents=True)
    cutoff = datetime(2026, 8, 5)
    result = classify_plan(plan_dir, cutoff)
    assert result == "new", (
        "an unparsable dirname carries no creation-date evidence, so it "
        "must downgrade to 'new' — archiving it would be inferring an "
        "old date from the absence of a parseable one"
    )


# ---------------------------------------------------------------------------
# Tests for scan_archived_plans metadata shape
# ---------------------------------------------------------------------------


def test_scan_archived_plans_returns_directory_metadata_only(
    data_dir: Path,
) -> None:
    """``scan_archived_plans`` returns ONLY directory-derived fields.

    Required keys per row: ``plan_id``, ``requirement_first_line``,
    ``created_at``, ``archived`` (=True).

    Explicitly disallowed: ``stage``, ``current_phase``, or any
    other field that would leak SQLite state-machine semantics
    into the archive-scan result. Archived plans do NOT enter
    SQLite — their existence is captured purely by the directory
    tree and the requirement text.
    """
    from state_machine.db.archive_scan import scan_archived_plans

    plans_root = data_dir / "plans"
    plans_root.mkdir()

    # Create one archived plan (dirname before cutoff) with an
    # interview.json whose first line is the requirement.
    archived = plans_root / "20260801-old-task"
    archived.mkdir()
    (archived / "interview.json").write_text(
        '"\\nold requirement text"\n',
        encoding="utf-8",
    )

    # Create one new plan (dirname after cutoff) — must NOT appear
    # in the archived list.
    new = plans_root / "20260806-new-task"
    new.mkdir()
    (new / "interview.json").write_text(
        '"\\nnew requirement text"\n',
        encoding="utf-8",
    )

    result = scan_archived_plans(plans_root, datetime(2026, 8, 5))

    # Exactly one archived plan, the old one.
    assert len(result) == 1
    row = result[0]
    assert set(row.keys()) == {
        "plan_id",
        "requirement_first_line",
        "created_at",
        "archived",
    }
    assert row["archived"] is True
    assert row["plan_id"] == "20260801-old-task"
    # ``created_at`` mirrors the parsed dirname date — 2026-08-01.
    assert row["created_at"] == "2026-08-01"


def test_scan_archived_plans_returns_empty_when_data_dir_missing(
    tmp_path: Path,
) -> None:
    """``scan_archived_plans`` returns ``[]`` when the data directory
    does not exist — it must NOT raise.
    """
    from state_machine.db.archive_scan import scan_archived_plans

    nonexistent = tmp_path / "plans_does_not_exist"
    assert not nonexistent.exists()
    result = scan_archived_plans(nonexistent, datetime(2026, 8, 5))
    assert result == []


# ---------------------------------------------------------------------------
# Tests for the CUTOFF_2026_08_05 constant
# ---------------------------------------------------------------------------


def test_cutoff_constant_is_module_level_and_patchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CUTOFF_2026_08_05`` is exposed at module level so tests can
    monkeypatch it (e.g. to test forward-compatibility at a later
    cutoff). The constant must be a ``datetime`` equal to
    2026-08-05 00:00:00.
    """
    from state_machine.db import archive_scan

    monkeypatch.setattr(archive_scan, "CUTOFF_2026_08_05", datetime(2026, 8, 5))
    assert isinstance(archive_scan.CUTOFF_2026_08_05, datetime)
    assert archive_scan.CUTOFF_2026_08_05 == datetime(2026, 8, 5)


# ---------------------------------------------------------------------------
# Tests for scan_artifacts (file-existence derived)
# ---------------------------------------------------------------------------


def test_scan_artifacts_returns_one_row_per_known_artifact(data_dir: Path) -> None:
    """``scan_artifacts`` returns one row per known artifact type,
    with ``status = "present"`` if the file exists and ``"missing"``
    otherwise. No JSON parsing, no schema awareness — purely a
    directory listing.
    """
    from state_machine.db.artifact_scan import scan_artifacts

    plan_dir = data_dir / "20260801-old"
    plan_dir.mkdir()

    # Create interview.json and prd.json, but NOT review.json or
    # tasks.json — those should show up as "missing".
    (plan_dir / "interview.json").write_text("{}", encoding="utf-8")
    (plan_dir / "prd.json").write_text("{}", encoding="utf-8")

    rows = scan_artifacts(plan_dir)
    by_type = {row["artifact_type"]: row for row in rows}

    # All four canonical artifact types must appear (interview, prd,
    # review, tasks) with deterministic file_path keys.
    assert set(by_type) == {"interview", "prd", "review", "tasks"}
    assert by_type["interview"]["status"] == "present"
    assert by_type["prd"]["status"] == "present"
    assert by_type["review"]["status"] == "missing"
    assert by_type["tasks"]["status"] == "missing"

    # file_path is the relative path inside the plan dir (just the
    # filename — archived artifacts live next to interview.json).
    for row in rows:
        assert row["file_path"] in {
            "interview.json",
            "prd.json",
            "review.json",
            "tasks.json",
        }
