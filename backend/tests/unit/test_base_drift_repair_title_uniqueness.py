"""A base-drift repair task is named after the base **commit**, not only the ref.

**The failure this file exists to prevent.**  Merging a drifting base is a
*repeatable* maintenance action: the same base ref drifts again and again,
and the number of conflicting files is often the same both times.  The
dispatcher decides "this repair was already applied and the problem came
back" by comparing **titles for equality**
(:meth:`TaskManager.find_completed_duplicate`), and a title match stops the
run with ``loop_detected`` before a single line of the merge is executed.

So a title of "the ref plus the file count" cannot tell *the same merge,
still broken* from *a new merge against a base that moved on* — the second
one is stopped while the conflict is still sitting in the working tree.

**What the title carries now.**  The base commit, abbreviated: the same
merge against the same commit keeps the same name, a later drift against a
later commit gets a different one.

The properties pinned here:

* two drifts of the same ref with the same conflict count get **different**
  titles (the drift is a different merge);
* a later drift is **not** a completed duplicate of an earlier one, so the
  dispatcher does not stop it;
* the *same* drift re-reported is still a completed duplicate — the loop
  detection the dispatcher relies on is not weakened;
* **negative control**: with the old title shape (ref + file count, no
  commit) the very same pair *is* taken as a duplicate.  Without this, the
  two properties above could pass for a reason that has nothing to do with
  the commit being in the title — e.g. a ``find_completed_duplicate`` that
  never matches anything.  The control fails the moment the title stops
  carrying the commit, so it is not vacuous.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from base_drift import (  # noqa: E402
    STATUS_CONFLICTS,
    BaseDriftReport,
    build_conflict_repair_task,
)
from task_manager import TaskManager  # noqa: E402

PLAN_ID = "plan-1"
PROJECT_DIR = Path("/repo")
BASE_REF = "origin/main"

# Two drifts of the same ref: the base moved on between them.
FIRST_SHA = "11111111aaaa"
SECOND_SHA = "22222222bbbb"

# The title shape that predates the commit in the name.
OLD_TITLE = "合并 origin/main：解决 1 个文件的冲突"


def _report(base_sha: str) -> BaseDriftReport:
    """A conflicting base-drift report against ``base_sha``."""
    return BaseDriftReport(
        status=STATUS_CONFLICTS,
        base_ref=BASE_REF,
        base_sha=base_sha,
        behind=1,
        conflict_files=[{"path": "app.py", "conflict_blocks": 1}],
        mode="repair",
    )


def _task(base_sha: str, existing_task_ids=None) -> dict:
    """Build one repair task the way the preflight builds it."""
    return build_conflict_repair_task(
        _report(base_sha),
        plan_id=PLAN_ID,
        project_dir=PROJECT_DIR,
        round_number=3,
        existing_task_ids=existing_task_ids,
    )


def _manager(tmp_path: Path, tasks: list, completed_ids=()) -> TaskManager:
    """A manager over a plan whose ``tasks.json`` holds ``tasks``.

    The completed status is supplied the way the production path supplies
    it: ``status`` is runtime state and lives in SQLite, so it arrives
    through ``runtime_overrides`` and is overlaid onto the static row
    read from disk (``task_manager.load_tasks``).
    """
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False),
        encoding="utf-8",
    )
    overrides = {task_id: {"status": "completed"} for task_id in completed_ids}
    return TaskManager(
        project_dir=tmp_path,
        tasks_file=tasks_file,
        runtime_overrides=overrides,
    )


def test_two_drifts_of_the_same_ref_get_different_titles():
    """Same ref, same conflict count, different base commit — different names.

    Without the commit in the title, the two drifts are the same string,
    and the second one is indistinguishable from the first one's
    leftovers.
    """
    first = _task(FIRST_SHA)
    second = _task(SECOND_SHA, existing_task_ids=[first["id"]])

    assert first["title"] != second["title"], (first["title"], second["title"])
    assert FIRST_SHA[:8] in first["title"], first["title"]
    assert SECOND_SHA[:8] in second["title"], second["title"]


def test_a_later_drift_is_not_a_completed_duplicate(tmp_path):
    """The dispatcher does not stop a *new* drift as a repeat of an old one.

    This is the accident: the first drift's repair was completed, the base
    drifted again with the same conflict count, and the new task was
    refused before it ran a line.
    """
    first = _task(FIRST_SHA)
    second = _task(SECOND_SHA, existing_task_ids=[first["id"]])

    manager = _manager(tmp_path, [first, second], completed_ids=[first["id"]])
    loaded = {t.id: t for t in manager.tasks}

    assert loaded[first["id"]].status == "completed"
    assert loaded[second["id"]].status == "pending"

    duplicate = manager.find_completed_duplicate(loaded[second["id"]])
    assert duplicate is None, (
        f"a later drift was taken as a repeat of {duplicate.title if duplicate else ''!r}"
    )


def test_the_same_drift_is_still_a_completed_duplicate(tmp_path):
    """The same merge against the same commit is still caught as a loop.

    The commit in the title must not turn the loop detector off: a merge
    that was already applied and is being asked again against the very same
    base is the case the detector exists for.
    """
    first = _task(FIRST_SHA)
    again = _task(FIRST_SHA, existing_task_ids=[first["id"]])

    manager = _manager(tmp_path, [first, again], completed_ids=[first["id"]])
    loaded = {t.id: t for t in manager.tasks}

    duplicate = manager.find_completed_duplicate(loaded[again["id"]])
    assert duplicate is not None, "the same drift must still be detected"
    assert duplicate.id == first["id"]


def test_the_old_title_shape_would_have_been_taken_as_a_duplicate(tmp_path):
    """Negative control: without the commit in the title, the drift is lost.

    Same two drifts, same statuses, same dispatcher — only the title is the
    old one.  The old shape is taken as a duplicate, which is exactly the
    accident.  If this control ever stops matching, the two properties
    above are passing for some other reason and no longer prove anything
    about the commit being in the title.
    """
    first = dict(_task(FIRST_SHA))
    second = dict(_task(SECOND_SHA, existing_task_ids=[first["id"]]))
    first["title"] = OLD_TITLE
    second["title"] = OLD_TITLE

    manager = _manager(tmp_path, [first, second], completed_ids=[first["id"]])
    loaded = {t.id: t for t in manager.tasks}

    duplicate = manager.find_completed_duplicate(loaded[second["id"]])
    assert duplicate is not None, (
        "the old title shape must still be caught as a duplicate — "
        "otherwise this file proves nothing"
    )
    assert duplicate.id == first["id"]
