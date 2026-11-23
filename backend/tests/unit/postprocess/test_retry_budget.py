"""The resume budget of the finishing stage (收尾阶段的续跑预算).

What this file pins
-------------------
The budget is the one thing standing between "a plan parked at
``postprocess_failed`` can be continued" and "a plan can be run
forever".  It is a **counter with a cap**, and the cap is two:

1. the first :func:`claim_retry` returns 1 and the file says
   ``retries_used == 1``;
2. the second returns 2;
3. the third raises :class:`RetryBudgetExhausted` — and the file still
   says 2, because a *refused* resume must not spend anything;
4. reading a plan that never spent a retry answers 0 **and writes
   nothing** — a read is not a resume, so it must not leave a trace;
5. a write leaves no temporary file behind next to the budget file
   (the write is atomic: a temporary file in the same directory, then
   ``os.replace``).

The budget is deliberately *not* a setting: it is a product decision,
so it lives in a constant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from postprocess.retry_budget import (
    BUDGET_FILENAME,
    MAX_RETRIES,
    RetryBudgetExhausted,
    claim_retry,
    read_budget,
)


def _budget_file(plan_dir: Path) -> Path:
    return plan_dir / BUDGET_FILENAME


def test_max_retries_is_two():
    """The cap is the product decision: two resumes after the first run."""
    assert MAX_RETRIES == 2


def test_first_claim_returns_one_and_is_recorded(tmp_path):
    """The first resume is number 1 and lands in the file."""
    assert claim_retry(tmp_path, reason="first") == 1

    document = json.loads(_budget_file(tmp_path).read_text(encoding="utf-8"))
    assert document["retries_used"] == 1
    assert document["plan_id"] == tmp_path.name
    assert len(document["history"]) == 1
    assert document["history"][0]["reason"] == "first"
    assert document["history"][0]["at"]


def test_second_claim_returns_two(tmp_path):
    """The second resume is number 2 and the file says so."""
    claim_retry(tmp_path, reason="first")
    assert claim_retry(tmp_path, reason="second") == 2

    document = json.loads(_budget_file(tmp_path).read_text(encoding="utf-8"))
    assert document["retries_used"] == 2
    assert len(document["history"]) == 2


def test_third_claim_is_refused_and_spends_nothing(tmp_path):
    """The third resume raises — and the counter does not move."""
    claim_retry(tmp_path, reason="first")
    claim_retry(tmp_path, reason="second")

    with pytest.raises(RetryBudgetExhausted) as caught:
        claim_retry(tmp_path, reason="third")

    assert caught.value.retries_used == 2
    assert caught.value.max_retries == MAX_RETRIES
    assert "2" in str(caught.value)

    document = json.loads(_budget_file(tmp_path).read_text(encoding="utf-8"))
    assert document["retries_used"] == 2
    assert len(document["history"]) == 2


def test_read_budget_of_a_fresh_plan_writes_nothing(tmp_path):
    """A read answers 0 and leaves the disk alone."""
    budget = read_budget(tmp_path)

    assert budget["retries_used"] == 0
    assert budget["history"] == []
    assert not _budget_file(tmp_path).exists()
    assert list(tmp_path.iterdir()) == []


def test_write_leaves_no_temporary_file_behind(tmp_path):
    """The write is atomic: the temporary file is gone afterwards."""
    claim_retry(tmp_path, reason="first")

    leftovers = [
        path.name
        for path in tmp_path.iterdir()
        if path.name != BUDGET_FILENAME
    ]
    assert leftovers == []


def test_read_budget_after_a_claim_reflects_the_file(tmp_path):
    """What was spent is what the next reader sees."""
    claim_retry(tmp_path, reason="first")

    budget = read_budget(tmp_path)
    assert budget["retries_used"] == 1
    assert len(budget["history"]) == 1
