"""Unit tests: when was this plan created?

Background
----------
The scheduler interrupts a *running* plan when a slot has to be freed,
and to pick the right one it has to know how old each plan is.  The
slot's own timestamp cannot answer that — it is rewritten whenever the
ledger is reconciled — so the age has to come from the plan itself.

The plan's own directory is the record: the directory was created when
the plan was created, so its creation time (``st_birthtime``) is the
answer.  On the filesystems that keep no creation time at all, the
change time (``st_ctime``) stands in for it.

A plan with no directory has no directory to ask, so the plan id is the
fallback: an id that begins with eight digits names its date, and that
date at local midnight is the closest thing to a creation time that is
still left.  An id that names no day names no time either, and the
answer is ``None`` — a plan with no creation time is a plan the
scheduler cannot age, not a plan it may guess about.

What this file holds down
-------------------------
1. **The directory wins.**  The answer is the directory's own
   timestamp — the creation time when there is one, the change time
   when there is not.
2. **The id is the fallback.**  No directory → the date in the id, at
   local midnight.
3. **An id that names no day names no time.**  No date prefix, or a
   date that does not exist → ``None``.
4. **The directory is asked once.**  One reader, one plan, one stat:
   a directory's creation time does not change once the directory
   exists, so there is nothing to re-read.

The tests assert **the number the policy is given** — the epoch
seconds — and not how the reader got there.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from plan_scheduler.facts import PlanFactsReader

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _UnstattableDirectory:
    """A plan directory that cannot be stat'ed."""

    def stat(self) -> Any:
        raise OSError("the directory is gone")


def _directory_timestamp(path: Path) -> float:
    """The timestamp the reader is expected to read off a directory.

    The creation time when the filesystem keeps one, and the change
    time when it does not — the same rule the reader follows, so the
    case holds on every platform rather than only on the one that
    happens to record a birth time.
    """
    stat = path.stat()
    birthtime = float(getattr(stat, "st_birthtime", 0.0) or 0.0)
    return birthtime if birthtime else float(stat.st_ctime)


def _midnight(year: int, month: int, day: int) -> float:
    """Local midnight of a day, in epoch seconds."""
    return datetime(year, month, day).timestamp()


@pytest.fixture()
def reader(tmp_path: Path):
    """A facts reader over a plans tree that holds nothing yet."""
    conn = sqlite3.connect(":memory:")
    try:
        yield PlanFactsReader(conn, tmp_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The directory wins
# ---------------------------------------------------------------------------


def test_plan_created_at_reads_directory_timestamp(
    reader: PlanFactsReader, tmp_path: Path
) -> None:
    """A plan with a directory is as old as that directory is.

    The directory is the plan's own record of when it came into
    existence, and its timestamp is the answer — the creation time
    where the filesystem keeps one, and the change time where it does
    not.
    """
    plan_dir = tmp_path / "20261029-with-directory"
    plan_dir.mkdir()

    assert reader.plan_created_at("20261029-with-directory") == (
        _directory_timestamp(plan_dir)
    )


# ---------------------------------------------------------------------------
# 2. The id is the fallback
# ---------------------------------------------------------------------------


def test_plan_created_at_falls_back_to_id_date(reader: PlanFactsReader) -> None:
    """A plan with no directory is as old as the date its id names.

    Eight digits at the front of the id are the day the plan was made,
    and that day at local midnight is the closest thing to a creation
    time that is left.
    """
    assert reader.plan_created_at("20261029-x") == _midnight(2026, 10, 29)


# ---------------------------------------------------------------------------
# 3. An id that names no day names no time
# ---------------------------------------------------------------------------


def test_plan_created_at_invalid_id_returns_none(reader: PlanFactsReader) -> None:
    """An id without a date says nothing about when the plan was made."""
    assert reader.plan_created_at("vp-003") is None


@pytest.mark.parametrize(
    "plan_id",
    ["20261301-x", "20260230-x", "00000000-x"],
    ids=["month-13", "february-30", "year-0"],
)
def test_plan_created_at_rejects_a_date_that_does_not_exist(
    reader: PlanFactsReader, plan_id: str
) -> None:
    """Eight digits that are not a date name no day, and no creation time.

    The id looks like it names a day, so the fallback has to *parse* it
    rather than count its digits — otherwise a plan would be dated to
    a day that never happened.
    """
    assert reader.plan_created_at(plan_id) is None


# ---------------------------------------------------------------------------
# 4. The directory is asked once
# ---------------------------------------------------------------------------


def test_plan_created_at_is_stat_once(
    reader: PlanFactsReader, tmp_path: Path
) -> None:
    """One reader, one plan, one stat.

    The directory's creation time cannot change while the directory is
    there, so the reader asks for it once and answers from what it
    read.  A directory that is deleted afterwards does not turn the
    answer into the id's date — that would be a second, different
    answer to the same question.
    """
    plan_dir = tmp_path / "20261029-cached"
    plan_dir.mkdir()

    first = reader.plan_created_at("20261029-cached")
    assert first == _directory_timestamp(plan_dir)
    assert first != _midnight(2026, 10, 29)

    shutil.rmtree(plan_dir)

    assert reader.plan_created_at("20261029-cached") == first


def test_plan_created_at_survives_a_failing_stat(
    reader: PlanFactsReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that cannot be stat'ed falls back to the id's date.

    The stat may fail — the directory may have gone between the check
    and the read — and a plan that cannot be stat'ed is still a plan
    whose id names a day.
    """
    monkeypatch.setattr(
        PlanFactsReader,
        "_plan_dir",
        lambda self, plan_id: _UnstattableDirectory(),
    )

    assert reader.plan_created_at("20261029-x") == _midnight(2026, 10, 29)
