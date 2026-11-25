"""The approval wait names the pull request it is about — and where it came from.

What this file witnesses
------------------------
An approval request is a question about **one action**: usually a pull
request that is waiting to be merged.  The waiting row a human reads
must therefore say *which* pull request it is about — its number and
its link — and *where* it came from, so an approval request is told
apart from an ordinary question put to a human (architecture decision
points R5 and R6).

Four behaviours are pinned here:

1. ``gh pr merge 42 --repo owner/name`` yields the number **and** the
   link ``https://github.com/owner/name/pull/42``;
2. a command that names no pull request at all (``gh pr merge
   --continue``) yields nothing — the wait is still created, it simply
   carries no pull request;
3. the record of an approval wait carries the two pull-request fields
   and says that it is an approval (``postprocess_approval``);
4. a wait that is **not** an approval says so — its source is never
   ``postprocess_approval``, and an ordinary human wait carries no
   source at all.

Why the link is composed from ``--repo``
---------------------------------------
``gh`` prints the link itself, but a command that is only *about* to
run has printed nothing yet.  The link is therefore composed from the
two facts the command carries: the number and the repository.  A
command without ``--repo`` keeps its number and has no link — a
half-known pull request is better than a wrong one.

Test command::

    pytest backend/tests/unit/postprocess/test_approval_wait_pr_reference.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hitl.store import HitlStore
from postprocess.executor import NeedsApproval
from postprocess.pr_ref import extract_pr_ref
from postprocess.waits import POSTPROCESS_APPROVAL_SOURCE, WaitRegistry
from tests.fixtures.postprocess.builders import FakeClock, make_item


#: The moment every case starts from — a fixed point, so a deadline in
#: an assertion is written out rather than computed twice.
T0 = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)

#: The keys one wait record carries — the shared state model of
#: architecture decision point 5, plus the source and the pull request
#: the wait is about.
WAIT_RECORD_KEYS = {
    "item_id",
    "wait_type",
    "question",
    "deadline",
    "extensions",
    "status",
    "source",
    "pr_number",
    "pr_url",
}


@pytest.fixture
def plans_root(tmp_path, monkeypatch) -> Path:
    """A private plans tree, so no case writes into the live one."""
    root = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


@pytest.fixture
def clock() -> FakeClock:
    """A clock the case moves instead of waiting for."""
    return FakeClock(T0)


@pytest.fixture
def store(tmp_path):
    """A HITL store on a private database."""
    opened = HitlStore(tmp_path / "state.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def registry(plans_root, clock, store) -> WaitRegistry:
    """A registry bridged onto the private store."""
    return WaitRegistry("p1", clock=clock, store=store)


def human_item(item_id: str = "approve") -> dict:
    """A requirement item that waits for a human."""
    return make_item(id=item_id, wait={"type": "human"})


def external_item(item_id: str = "ci") -> dict:
    """A requirement item that waits for something outside."""
    return make_item(id=item_id, external=True, wait={"type": "external"})


# ---------------------------------------------------------------------------
# Reading the pull request out of a command
# ---------------------------------------------------------------------------


def test_extract_pr_number_and_url():
    """The command names the pull request — number and link."""
    ref = extract_pr_ref(["gh", "pr", "merge", "42", "--repo", "o/r", "--squash"])

    assert ref is not None, "a command that names a pull request names one"
    assert ref["pr_number"] == 42
    assert ref["pr_url"] == "https://github.com/o/r/pull/42"


def test_extract_returns_none_without_number():
    """A command that names no pull request names none."""
    assert extract_pr_ref(["gh", "pr", "merge", "--continue"]) is None


# ---------------------------------------------------------------------------
# The wait record carries the pull request and the source
# ---------------------------------------------------------------------------


def test_wait_record_carries_pr_fields(registry):
    """An approval wait says which pull request it is about, and that it is one."""
    approval = NeedsApproval(
        question="批准合入 PR #42？",
        command=["gh", "pr", "merge", "42", "--repo", "o/r", "--squash"],
    )

    record = registry.create_approval_request(
        human_item(),
        approval,
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )

    assert record["pr_number"] == 42, (
        "the waiting row names the pull request the approval is about"
    )
    assert record["pr_url"] == "https://github.com/o/r/pull/42"
    assert record["source"] == POSTPROCESS_APPROVAL_SOURCE, (
        "an approval request says that it is one — that is what tells it "
        "apart from an ordinary question put to a human"
    )

    stored = registry.get("approve")
    assert set(stored) == WAIT_RECORD_KEYS, (
        f"a wait record carries exactly {sorted(WAIT_RECORD_KEYS)}, got "
        f"{sorted(stored)}"
    )
    assert "answer" not in stored, (
        "the answer is the registry's own state — it never lands in the record"
    )


def test_non_approval_record_has_empty_source(registry):
    """A wait that is not an approval never claims to be one."""
    human = registry.create_human_wait(human_item(), question="批准合入？")
    external = registry.create_external_wait(external_item(), check=lambda: True)

    assert human["source"] == "", (
        "an ordinary human wait carries no source — nothing says it is an "
        "approval, and nothing has to be cleared to say so"
    )
    assert external["source"] != POSTPROCESS_APPROVAL_SOURCE
    assert external["source"] == "external", (
        "a wait for something outside says what it waits for"
    )
