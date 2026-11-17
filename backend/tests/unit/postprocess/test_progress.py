"""The finishing stage's persisted progress — read, write, and "what failed".

What this file witnesses
------------------------
The finishing stage (收尾阶段) may run twice: once now, and once more
after a human has dealt with whatever failed.  Which items the second
run repeats must come from something that outlived the first run — an
in-memory list dies with the process, and an agent's recollection of
what it already tried is not a record at all.  So the state lives in
one file, ``<plan>/postprocess_progress.json``, and this file pins the
four things the rest of the stage relies on:

1. the file is created with the frozen fingerprint and the marked item;
2. a second mark overwrites the status and accumulates the attempts;
3. the retry round sees **only** the failed items — a waiting or
   running item is not a failed one;
4. a write that dies half-way leaves the previous file complete and
   parseable (the temporary file plus ``os.replace``);
5. a status outside the closed vocabulary is refused.

Why the retry scope is a *query* and not a flag
----------------------------------------------
"The retry round runs the failed items" is a rule that has to be
decided by **reading the persisted record**, not by remembering what
happened.  A boolean on the orchestrator (``had_failures = True``) is
exactly the in-memory state the architecture forbids: it is lost on a
crash, and it cannot say *which* items failed.  So the scope is
computed from the file, by :func:`failed_item_ids`, and this file
pins that the computation returns the failed ids and nothing else.

Why the fingerprint matters
---------------------------
The progress file records which version of the requirement book the
stage is judging against.  A stage that judged against version A and
resumed against version B is not resuming — it is running a different
book under the same name, and the report would claim a verdict the
second round never reached.  So the fingerprint is written on the
first mark and never moves afterwards.

Where the file lives
--------------------
``<plans root>/<plan_id>/postprocess_progress.json``.  The plans root
is resolved through the repository's own resolver, so a test that
redirects ``PDT_PLANS_DIR`` gets its own tree — no test ever writes
into the operator's live plans directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from postprocess.progress import (
    PROGRESS_STATUSES,
    ProgressError,
    failed_item_ids,
    load_progress,
    mark,
    progress_path,
)
from tests.fixtures.postprocess.builders import make_progress


#: The fingerprint of an empty requirement book — the sha256 of no
#: bytes at all.  It is a real digest, so a reader that only checks
#: "does this look like a sha256" is not fooled by an empty string.
EMPTY_BOOK_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


@pytest.fixture
def plans_root(tmp_path, monkeypatch) -> Path:
    """A private plans tree, so no case writes into the live one."""
    root = tmp_path / "plans"
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    return root


def read_progress(plan_id: str) -> dict:
    """The progress document as it sits on disk."""
    return json.loads(progress_path(plan_id).read_text(encoding="utf-8"))


def test_mark_creates_file_with_spec_and_items(plans_root):
    """The first mark writes the file: fingerprint, start time, item."""
    mark("p1", "push", "passed", attempts=1)

    path = progress_path("p1")
    assert path.exists(), f"the progress file must be created at {path}"
    assert path.parent.name == "p1", (
        "the progress file lives inside the plan's own directory"
    )
    assert path.name == "postprocess_progress.json"

    document = read_progress("p1")
    assert set(document) == {"spec_sha256", "started_at", "items"}
    assert len(document["spec_sha256"]) == 64, (
        "the fingerprint must look like a sha256 digest, not an empty "
        f"string: {document['spec_sha256']!r}"
    )
    assert document["started_at"], "the start time must be recorded"
    assert document["items"]["push"]["status"] == "passed"
    assert document["items"]["push"]["attempts"] == 1


def test_mark_updates_status_and_attempts(plans_root):
    """A second mark overwrites the status and accumulates the attempts."""
    mark("p1", "push", "running", attempts=1)

    mark("p1", "push", "passed", attempts=1)

    record = read_progress("p1")["items"]["push"]
    assert record["status"] == "passed", (
        "the newer status must replace the older one"
    )
    assert record["attempts"] == 2, (
        "the attempts accumulate: the item was attempted twice"
    )


def test_failed_item_ids_returns_only_failed(plans_root):
    """The retry scope is the failed items — and only the failed ones."""
    mark("p1", "push", "passed", attempts=1)
    mark("p1", "pr", "failed", attempts=1, last_evidence_ref="ev.json")
    mark("p1", "merge", "waiting", attempts=1)
    mark("p1", "tag", "running", attempts=1)

    assert failed_item_ids(load_progress("p1")) == ["pr"]


def test_atomic_write_keeps_previous_file_on_error(plans_root, monkeypatch):
    """A write that dies at the rename leaves the old file intact."""
    mark("p1", "push", "passed", attempts=1)
    before = progress_path("p1").read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("the disk said no")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        mark("p1", "pr", "failed", attempts=1)

    after = progress_path("p1").read_text(encoding="utf-8")
    assert after == before, "the previous file must survive a failed write"
    assert json.loads(after)["items"] == {
        "push": {
            "status": "passed",
            "attempts": 1,
            "last_evidence_ref": None,
        }
    }, "the previous file must still parse"


def test_unknown_status_rejected(plans_root):
    """A status outside the closed vocabulary is refused."""
    with pytest.raises(ValueError, match="Invalid status"):
        mark("p1", "push", "bogus", attempts=1)

    assert not progress_path("p1").exists(), (
        "a refused mark must not leave a file behind"
    )


def test_the_status_vocabulary_is_closed(plans_root):
    """The vocabulary is exactly the five statuses the architecture names."""
    assert set(PROGRESS_STATUSES) == {
        "pending",
        "running",
        "passed",
        "failed",
        "waiting",
    }


def test_load_progress_returns_an_empty_skeleton_when_absent(plans_root):
    """A plan that never ran has no file — and that is not an error."""
    progress = load_progress("p1")

    assert set(progress) == {"spec_sha256", "started_at", "items"}
    assert progress["items"] == {}
    assert progress["spec_sha256"] == ""
    assert progress["started_at"] == ""


def test_corrupt_progress_raises_progress_error(plans_root):
    """A damaged file is reported, never read as "nothing to retry"."""
    path = progress_path("p1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")

    with pytest.raises(ProgressError):
        load_progress("p1")


def test_a_corrupt_file_is_not_overwritten_by_a_mark(plans_root):
    """A mark refuses to write over a file it could not read."""
    path = progress_path("p1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")

    with pytest.raises(ProgressError):
        mark("p1", "push", "passed", attempts=1)

    assert path.read_text(encoding="utf-8") == "{not json at all", (
        "the damaged file must still be there for a human to look at"
    )


def test_progress_error_is_a_value_error(plans_root):
    """A caller that catches ``ValueError`` catches the damage too."""
    assert issubclass(ProgressError, ValueError)


def test_mark_creates_the_plan_directory(plans_root):
    """The plan's directory is created on demand."""
    assert not (plans_root / "p1").exists()

    mark("p1", "push", "passed", attempts=1)

    assert (plans_root / "p1" / "postprocess_progress.json").is_file()


def test_a_plan_without_a_requirement_book_still_gets_a_digest(plans_root):
    """No book yet means the digest of an empty book — not an empty string."""
    mark("p1", "push", "passed", attempts=1)

    assert read_progress("p1")["spec_sha256"] == EMPTY_BOOK_SHA256


def test_the_fingerprint_is_the_requirement_book_digest(plans_root):
    """The fingerprint identifies the requirement book the stage judges."""
    book = plans_root / "p1" / "postprocess.json"
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text('{"items": []}', encoding="utf-8")

    mark("p1", "push", "passed", attempts=1)

    expected = hashlib.sha256(book.read_bytes()).hexdigest()
    assert read_progress("p1")["spec_sha256"] == expected


def test_the_fingerprint_never_moves_after_the_first_mark(plans_root):
    """A frozen stage keeps its fingerprint, even if the book changes."""
    book = plans_root / "p1" / "postprocess.json"
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text('{"items": []}', encoding="utf-8")

    mark("p1", "push", "passed", attempts=1)
    frozen = read_progress("p1")["spec_sha256"]

    book.write_text('{"items": [{"id": "pr"}]}', encoding="utf-8")
    mark("p1", "pr", "failed", attempts=1)

    assert read_progress("p1")["spec_sha256"] == frozen, (
        "the frozen fingerprint must not follow an edit of the book"
    )


def test_the_start_time_is_kept_across_marks(plans_root):
    """The stage's start time is the first mark's, not the last one's."""
    mark("p1", "push", "running", attempts=1)
    started_at = read_progress("p1")["started_at"]

    mark("p1", "push", "passed", attempts=1)

    assert read_progress("p1")["started_at"] == started_at


def test_the_last_evidence_ref_is_kept_when_not_stated(plans_root):
    """The evidence of the last try survives a mark that names none."""
    mark("p1", "pr", "failed", attempts=1, last_evidence_ref="ev.json")

    mark("p1", "pr", "running", attempts=1)

    record = read_progress("p1")["items"]["pr"]
    assert record["last_evidence_ref"] == "ev.json"


def test_a_wait_record_is_stored_only_when_given(plans_root):
    """``wait`` is optional: an item without a wait does not carry one."""
    mark("p1", "push", "passed", attempts=1)

    assert set(read_progress("p1")["items"]["push"]) == {
        "status",
        "attempts",
        "last_evidence_ref",
    }

    mark("p1", "merge", "waiting", attempts=1, wait={"type": "human"})

    assert read_progress("p1")["items"]["merge"]["wait"] == {"type": "human"}


def test_the_wait_record_is_not_aliased_into_the_progress(plans_root):
    """The caller's wait record is copied, not captured by reference."""
    wait = {"type": "human", "deadline": "2026-11-21T00:00:00+00:00"}

    progress = mark("p1", "merge", "waiting", attempts=1, wait=wait)

    wait["type"] = "external"
    assert progress["items"]["merge"]["wait"]["type"] == "human", (
        "a later mutation of the caller's dict must not reach into the "
        "progress document"
    )


def test_failed_item_ids_accepts_a_built_progress():
    """The query is a pure function of the document, wherever it came from."""
    progress = make_progress(
        {
            "push": {"status": "passed"},
            "pr": {"status": "failed"},
            "merge": {"status": "waiting"},
        }
    )

    assert failed_item_ids(progress) == ["pr"]


def test_failed_item_ids_is_empty_for_a_progress_without_failures():
    """No failures means an empty retry scope — not a None."""
    progress = make_progress({"push": {"status": "passed"}})

    assert failed_item_ids(progress) == []


def test_a_plan_id_that_escapes_the_plans_root_is_refused(plans_root):
    """The plan id is a filesystem leaf, never a path."""
    with pytest.raises(ValueError):
        progress_path("../escape")

    with pytest.raises(ValueError):
        mark("..", "push", "passed", attempts=1)