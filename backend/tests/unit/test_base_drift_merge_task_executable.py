"""The base-drift conflict task has to be *executable*, not merely present.

A repair task that the round can write is not the same as a repair task
that the executor will run.  The executor reconciles rows the refiner
left behind (``plan_tasks`` rows whose id is not in the on-disk
``tasks.json``) and, for each such orphan, decides between **merging
its real content** and **substituting a content-free placeholder**.  The
predicate is ``orphan_rules.has_mergeable_content``, and it asks for
``title`` *and* ``description`` together — ``test_command`` is
deliberately not part of it.

A base-drift task that carried a description but no title was therefore
**always** downgraded to a placeholder: the merge never ran, and the
round reported a drained repair batch while the conflict was still
sitting in the working tree.

So this file pins the three things that make the task executable:

* a **title** that names the base and how many files are in conflict,
* a **test command** that actually probes "the merge is done" (the base
  commit became an ancestor of HEAD, and no path is left unmerged),
* a **description** that says what kind of task this is — a *base
  merge*, not a product-code fix — so the agent that picks it up does
  not go looking for a product bug that does not exist.

The **scene** constant lives here too: the merge runs under its own
provider-routing scene, and only the *name* belongs in code — which tier
that scene resolves to is a configuration decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
from base_drift import (  # noqa: E402
    STATUS_CONFLICTS,
    BaseDriftReport,
    build_conflict_repair_task,
)

PLAN_ID = "plan-1"
PROJECT_DIR = Path("/repo")
BASE_REF = "origin/main"
BASE_SHA = "abc123"

MERGE_PROBE = (
    'git merge-base --is-ancestor abc123 HEAD && test -z "$(git ls-files -u)"'
)


def _report(**over):
    """A conflicting base-drift report, with the given fields replaced."""
    payload = {
        "status": STATUS_CONFLICTS,
        "base_ref": BASE_REF,
        "base_sha": BASE_SHA,
        "behind": 2,
        "conflict_files": [
            {"path": "a.py", "conflict_blocks": 1},
            {"path": "b.py", "conflict_blocks": 2},
        ],
        "mode": "repair",
    }
    payload.update(over)
    return BaseDriftReport(**payload)


def _task(report=None, **over):
    """Build one repair task from ``report`` (a conflicting one by default)."""
    kwargs = {
        "plan_id": PLAN_ID,
        "project_dir": PROJECT_DIR,
        "round_number": 3,
        "seq": 1,
    }
    kwargs.update(over)
    return build_conflict_repair_task(
        report if report is not None else _report(), **kwargs
    )


def test_title_names_the_base_and_file_count():
    """The title says which base was merged and how much is in conflict.

    A title that only says "fix the conflict" leaves the person (or the
    agent) reading the queue with no way to tell a base merge from any
    other repair.  Naming the base and the number of conflicting files
    is what makes the queue readable — and it is what makes the task
    mergeable at all: a row without a title is replaced by a
    content-free placeholder instead of being executed.
    """
    task = _task()

    assert task["title"] == "合并 origin/main：解决 2 个文件的冲突"


def test_title_falls_back_to_sha_then_base():
    """Without a ref the title names the commit; without either, "base".

    The ref is the friendlier name, but a report does not always carry
    one — a conflict resolved against a detached commit has only a SHA.
    When neither is known the title still has to name *something*: an
    unnamed base is still a base, and a title of "" would be no title
    at all (the loader treats whitespace as missing).
    """
    with_sha = _task(report=_report(base_ref=""))
    assert with_sha["title"] == "合并 abc123：解决 2 个文件的冲突"

    without_either = _task(report=_report(base_ref="", base_sha=""))
    assert without_either["title"] == "合并 base：解决 2 个文件的冲突"


def test_test_command_probes_merge_completion():
    """The test command asks the question that matters: is the merge done?

    Two facts have to hold — the base commit is now an ancestor of HEAD
    (it really was merged, not just fetched) and no path is left
    unmerged (the conflicts were resolved, not merely staged away).
    The command is exactly that, so a reviewer can read it and know
    what "green" means for this task.
    """
    task = _task()

    assert task["test_command"] == MERGE_PROBE


def test_test_command_omitted_when_no_merge_target():
    """With nothing to merge there is nothing to probe, so no command.

    An empty target would make ``git merge-base --is-ancestor  HEAD``
    degenerate — the check would pass for a repository that never merged
    anything, which is a green light on a conflict that is still there.
    A missing command is honest; a vacuously-true one is a lie.
    """
    task = _task(report=_report(base_ref="", base_sha=""))

    assert "test_command" not in task


def test_description_marks_the_task_as_a_base_merge():
    """The description says: this is a base merge, not a product fix.

    The agent that picks the task up reads the description first.  If it
    does not know the task is a *merge*, it will look for a product bug
    that does not exist — or "fix" the conflicting files by deleting one
    side of the conflict.  The description is the only place that
    distinction can live: the task row has no column for it.
    """
    description = _task()["description"]

    assert "base 合并" in description
    assert "不是产品代码修复" in description


def test_scene_constant_is_the_config_key():
    """The scene name is a constant — the tier it maps to is not.

    The merge runs under its own provider-routing scene so it can be
    given its own tier.  Only the *name* may live in code: the tier
    belongs to ``provider_routing.yaml``, so changing where the merge
    runs is a configuration change and not a code change.
    """
    assert base_drift.BASE_DRIFT_MERGE_SCENE == "base_drift_merge"
