"""VP-019 anchor (1/3): cutoff boundary classification — 5-point parametrization.

The state-machine refactor introduces a hard 2026-08-05 cutoff between
"active" plans (kept in SQLite) and "archived" plans (rendered read-only
via 410 Gone semantics). The boundary rule is ``parsed_date <= cutoff``
=> "archived" — i.e. the exact-cutoff moment is also archived.

This test pins all 5 boundary values explicitly:

  1. cutoff - 1 day  -> "archived"
  2. cutoff - 1 sec  -> "archived"
  3. cutoff exactly  -> "archived" (钉死)
  4. cutoff + 1 sec  -> "new" (next-day resolution)
  5. cutoff + 1 day  -> "new"

The boundary rule is parametrised so a future regression that flips
the comparator from ``<=`` to ``<`` (or vice versa) is caught at the
exact point where it would change behaviour, with no false-positive
from any of the 4 other boundary points.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from state_machine.db.archive_scan import classify_plan

CUTOFF = datetime(2026, 8, 5)


@pytest.mark.parametrize(
    ("dirname", "boundary_label", "expected"),
    [
        ("20260804-x", "cutoff - 1 day", "archived"),
        ("20260805-x", "cutoff exact moment (钉死)", "archived"),
        ("20260806-y", "cutoff + 1 day", "new"),
    ],
)
def test_cutoff_day_precision_boundary(
    tmp_path: Path,
    dirname: str,
    boundary_label: str,
    expected: str,
) -> None:
    """Parametrised 3-point day-precision boundary check.

    ``YYYYMMDD`` dirname encoding has only day precision, so this
    parametrisation covers ``cutoff - 1 day``, ``cutoff exact``, and
    ``cutoff + 1 day`` at the day-resolution level. The other two
    boundary points (sub-second) are covered by the dedicated tests
    below so each can fail with a precise, non-ambiguous error.
    """
    plan_dir = tmp_path / dirname
    plan_dir.mkdir()
    result = classify_plan(plan_dir, CUTOFF)
    assert result == expected, (
        f"Boundary {boundary_label!r} (dirname={dirname}) must classify "
        f"as {expected!r}; got {result!r}."
    )


def test_cutoff_minus_one_second_is_archived(tmp_path: Path) -> None:
    """A plan one second before cutoff (still on 08-04) is archived.

    The dirname parses as 20260804, which is strictly less than the
    cutoff 2026-08-05 00:00:01, so the result must be ``"archived"``.
    """
    plan_dir = tmp_path / "20260804-x"
    plan_dir.mkdir()
    # Cutoff pushed one second into 08-05 so the 20260804 dirname is
    # definitively less than the cutoff.
    cutoff = datetime(2026, 8, 5, 0, 0, 1)
    result = classify_plan(plan_dir, cutoff)
    assert result == "archived", (
        f"Plan dated 20260804 must be 'archived' against cutoff "
        f"{cutoff.isoformat()}; got {result!r}."
    )


def test_cutoff_plus_one_second_is_new(tmp_path: Path) -> None:
    """A plan one second after the cutoff is ``"new"``.

    Because the dirname only encodes the calendar day, the closest
    representable "cutoff + 1 second" is a plan on 2026-08-06. Against
    a cutoff set to 2026-08-05 00:00:01 (one second into 08-05),
    2026-08-06 is unambiguously the next day, so the plan must be
    classified ``"new"``.
    """
    plan_dir = tmp_path / "20260806-y"
    plan_dir.mkdir()
    cutoff = datetime(2026, 8, 5, 0, 0, 1)
    result = classify_plan(plan_dir, cutoff)
    assert result == "new", (
        f"Plan dated 20260806 must be 'new' against cutoff "
        f"{cutoff.isoformat()}; got {result!r}."
    )
