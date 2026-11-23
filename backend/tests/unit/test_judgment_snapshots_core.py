"""The first Tier B goldens: the three core modules, frozen into JSON.

Why this file exists
--------------------
§5.2 of the judgment-surface material describes a *behavioral golden*: a
fixed set of inputs, run through a judgment function, with the output
frozen into a JSON file.  The golden does not claim the behavior is
right — it claims the behavior has not **changed**.  That is what lets it
cover the long tail: nobody has to write a semantic expectation for
every branch, only the inputs.

This file pins the first three groups — ``evidence``, ``agent`` and
``buckets`` — the three core modules the material names:

* ``test_three_groups_match`` — the frozen file still says what the
  three modules say today.  A red here means a judgment moved, and the
  diff in the pull request is the record of that move;
* ``test_each_group_has_min_cases`` — no group is a handful of cases.
  A golden of two rows pins nothing worth pinning.

Where the inputs come from
--------------------------
The inputs are the fixed corpus of task 16
(``backend/tests/fixtures/judgment_corpus.py``): the corpus's group of
the same name holds that group's cases, so a case id in the golden can
be looked up in the corpus to see what it fed in.  The outputs are
computed by the modules under test — ``verification_evidence``,
``agent`` and ``verification_buckets`` — so this file states no verdict
of its own.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_judgment_snapshots_cli.py`` loads
it that way.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

#: The repository root — four parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
#: The script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "judgment_snapshots.py"
#: Where the snapshot file lives, relative to the repository root.
_SNAPSHOT_RELATIVE = Path("backend") / "tests" / "fixtures" / "judgment_snapshots.json"

#: The three core modules the first goldens pin.
CORE_GROUPS = ("evidence", "agent", "buckets")

#: The smallest number of cases a group may hold.  Fewer than this and
#: the group pins a handful of paths rather than a behavior.
MIN_CASES = 6

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs.  The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "judgment_snapshots_core_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
snapshots = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = snapshots
_spec.loader.exec_module(snapshots)


def test_three_groups_match() -> None:
    """The frozen goldens still say what the three core modules say today.

    Three things have to hold at once: the three groups are registered,
    the snapshot file holds exactly those three groups, and the file
    agrees with the behavior as it stands.  The last one is the golden's
    whole job — a difference means a judgment moved, and the report
    names the differing lines.
    """
    for name in CORE_GROUPS:
        assert name in snapshots.REGISTRY, (
            f"组 {name!r} 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
        )

    path = _REPO_ROOT / _SNAPSHOT_RELATIVE
    assert path.exists(), f"快照文件不在：{path}"
    stored = json.loads(path.read_text(encoding="utf-8"))
    missing = [name for name in CORE_GROUPS if name not in stored]
    assert not missing, (
        f"快照文件里没有这些组：{missing}；文件里的是 {sorted(stored)}"
    )

    assert snapshots.main(["--check", "--root", str(_REPO_ROOT)]) == 0


def test_each_group_has_min_cases() -> None:
    """Every core group holds at least six cases, and every id is its own.

    A group of one or two rows pins a single path through the function
    and says nothing about the others.  The ids have to be unique too:
    an id is how a reader finds the row, and how the corpus case it
    came from is looked up.
    """
    for name in CORE_GROUPS:
        assert name in snapshots.REGISTRY, (
            f"组 {name!r} 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
        )
        document = snapshots.render_group(name)
        cases = document["cases"]
        assert len(cases) >= MIN_CASES, (
            f"组 {name!r} 只有 {len(cases)} 个用例；金标至少要 {MIN_CASES} 个"
        )
        ids = [case["id"] for case in cases]
        assert len(set(ids)) == len(ids), f"组 {name!r} 的用例 id 重复：{ids}"