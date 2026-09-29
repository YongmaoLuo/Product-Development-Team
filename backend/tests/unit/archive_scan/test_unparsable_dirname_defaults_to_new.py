"""An unparsable dirname defaults to "new" — the corrected VP-019 rule.

The original archive-scan spec classified any plan directory whose name
does NOT match ``YYYYMMDD-*`` as ``"archived"`` ("safe side"). That rule
was wrong, and commit ``d76ebf6`` corrected it:

    A pre-existing bug in ``classify_plan`` downgraded any plan whose
    dirname does not match the canonical YYYYMMDD-* format to
    "archived" — conflating "we don't know when this was created" with
    "this was created before 2026-08-05".

Defaulting to ``"archived"`` on *absence* of evidence silently hides
legitimate plans: test fixtures (``vp001-...``), ad-hoc working dirs
(``migration-plan-...``), and hand-renamed directories would all be
rendered read-only and excluded from the live SQLite state machine,
with no way for the operator to notice. The archive boundary needs
*some* positive evidence of an old creation date, and the only accepted
evidence is a parseable ``YYYYMMDD-*`` prefix.

So this module now pins the corrected contract:

  * unparsable dirname  -> ``"new"`` (not archived)
  * parseable + ``<= cutoff`` -> ``"archived"``
  * parseable + ``>  cutoff`` -> ``"new"``

The dated cases live in ``test_cutoff_boundary_classification.py``;
this file covers the unparsable branch of
:func:`state_machine.db.archive_scan.classify_plan`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from state_machine.db.archive_scan import classify_plan

CUTOFF = datetime(2026, 8, 5)


@pytest.mark.parametrize(
    "dirname",
    [
        "no-date-prefix",
        "2026-only-year",
        "2026080-incomplete",
        "20261301-invalid-month",
        "not-a-date-at-all",
        "x-y-z",
    ],
)
def test_unparsable_dirname_defaults_to_new(
    tmp_path: Path,
    dirname: str,
) -> None:
    """Any dirname that does not parse as ``YYYYMMDD-*`` is "new".

    Parametrised across 6 representative failure modes so a future
    refactor that only handles some of them (e.g. only the
    "no-date-prefix" case) is caught with a precise, named failure.
    """
    plan_dir = tmp_path / dirname
    plan_dir.mkdir()
    result = classify_plan(plan_dir, CUTOFF)
    assert result == "new", (
        f"Unparsable dirname {dirname!r} must default to 'new'; got "
        f"{result!r}. Archiving on an unparsable name would silently hide "
        f"legitimate non-conforming plans (fixtures, ad-hoc dirs) — the "
        f"2026-08-05 boundary requires positive evidence of an old "
        f"creation date, i.e. a parseable date prefix."
    )


def test_unparsable_dirname_with_future_mtime_still_new(tmp_path: Path) -> None:
    """mtime is never consulted, in either direction.

    The contract is dirname-only: a hand-edited mtime must not resurrect
    an archived plan, and — symmetrically — an unparsable dirname is not
    archived just because its filesystem metadata looks old or new.
    """
    import os

    plan_dir = tmp_path / "no-date-prefix"
    plan_dir.mkdir()

    # Set mtime well into the future.
    future_mtime_ts = datetime(2026, 12, 31, 12, 0, 0).timestamp()
    os.utime(plan_dir, (future_mtime_ts, future_mtime_ts))

    result = classify_plan(plan_dir, CUTOFF)
    assert result == "new", (
        f"Unparsable dirname must be 'new' regardless of mtime; got "
        f"{result!r}."
    )


def test_single_character_dirname_defaults_to_new(tmp_path: Path) -> None:
    """Degenerate short names are unparsable, so they are "new".

    A single-character name cannot match ``^(\\d{8})(-.*)?$`` — the
    regex rejects it and the safe-downgrade branch must NOT fire.
    """
    plan_dir = tmp_path / "a"
    plan_dir.mkdir()
    result = classify_plan(plan_dir, CUTOFF)
    assert result == "new", (
        f"Single-character dirname must default to 'new'; got {result!r}."
    )


# ---------------------------------------------------------------------------
# The dated branch still archives — guards against "fixing" the
# unparsable case by weakening the cutoff rule itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dirname",
    [
        "20260805-cutoff-exact",
        "20260804-day-before",
        "20260101-far-past",
    ],
)
def test_parseable_dirname_at_or_before_cutoff_is_still_archived(
    tmp_path: Path,
    dirname: str,
) -> None:
    """A parseable date ``<= cutoff`` is archived, exact cutoff included."""
    plan_dir = tmp_path / dirname
    plan_dir.mkdir()
    assert classify_plan(plan_dir, CUTOFF) == "archived"


@pytest.mark.parametrize(
    "dirname",
    [
        "20260806-day-after",
        "20261231-far-future",
    ],
)
def test_parseable_dirname_after_cutoff_is_new(tmp_path: Path, dirname: str) -> None:
    """A parseable date ``> cutoff`` is "new"."""
    plan_dir = tmp_path / dirname
    plan_dir.mkdir()
    assert classify_plan(plan_dir, CUTOFF) == "new"
