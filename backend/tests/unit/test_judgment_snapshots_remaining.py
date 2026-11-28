"""The Tier B goldens for the remaining ``verification_*`` modules.

Why this file exists
--------------------
The judgment-surface inventory (``docs/judgment_points.json``) names ten
judgment modules.  The first goldens pinned five of them — ``evidence``,
``agent`` and ``buckets`` (the core three), then ``executor`` and
``ci_runner``.  This file pins the three remaining ``verification_*``
modules:

* ``loop`` — ``backend/verification_loop.py``: when a round's deadline
  has arrived, whether a snapshot belongs to a round the plan has left,
  what the round recorded as its stop reason, and whether the round's
  own thread is still running;
* ``subagent`` — ``backend/verification_subagent.py``: which checkpoints
  did not pass, what budget a point declared, and what the sub-agent is
  told when the framework rejected its artifact;
* ``verification_agent`` — ``backend/verification_agent.py``: how the
  split children roll up into one verdict, how many attempts the ledger
  holds, and what a result's evidence says once it has been read back.

The contract is the same as the earlier goldens: a golden does not claim
the behavior is *right*, it claims the behavior has not **changed**.  So
the assertions here are about *matching*, not about semantic
expectations.

Where the inputs come from
--------------------------
The inputs are the fixed corpus of task 16
(``backend/tests/fixtures/judgment_corpus.py``) — the corpus's group of
the same name holds that group's cases, so a case id in the golden can
be looked up in the corpus to see what it fed in.  The outputs are
computed by the modules under test, so this file states no verdict of
its own.

The boundary conditions
-----------------------
Only pure functions are driven, and a judgment that needs a stub to be
handed its input gets one — a stub that carries the *input* (a question
with a deadline, a thread that answers whether it is alive), never one
that replaces the judgment.  The two tests at the bottom of the file pin
that boundary: a golden that started a process or wrote a file would not
be a golden, it would be a test that runs the world.

What could not be driven
------------------------
A judgment that reads the disk, the runtime database or a live thread
has no fixed input, so it cannot be frozen.  Those are written down in
the script's ``COVERAGE_REPORT`` rather than quietly forgotten, and
``test_coverage_report_names_surfaces_that_exist`` checks that every
name in it really is a function of the module it is filed under.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_judgment_snapshots_core.py``
loads it that way.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

#: The repository root — four parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
#: The script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "judgment_snapshots.py"
#: Where the snapshot file lives, relative to the repository root.
_SNAPSHOT_RELATIVE = Path("backend") / "tests" / "fixtures" / "judgment_snapshots.json"
#: The inventory of judgment points — the list of the modules there are.
_MANIFEST_RELATIVE = Path("docs") / "judgment_points.json"

#: The smallest number of cases a group may hold.  Fewer than this and
#: the group pins a handful of paths rather than a behavior.
MIN_CASES = 4

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs.  The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "judgment_snapshots_remaining_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
snapshots = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = snapshots
_spec.loader.exec_module(snapshots)

#: The three groups this file pins — the script's own statement of them,
#: so the two cannot drift apart.
GROUPS = snapshots.REMAINING_GROUPS


def _stored() -> dict:
    """The frozen snapshot file, as it is on disk."""
    path = _REPO_ROOT / _SNAPSHOT_RELATIVE
    assert path.exists(), f"快照文件不在：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest() -> dict:
    """The judgment-point inventory, as it is on disk."""
    path = _REPO_ROOT / _MANIFEST_RELATIVE
    assert path.exists(), f"判定面清单不在：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_modules() -> list[str]:
    """Every judgment module the inventory names, in its own order."""
    return sorted({str(point["module"]) for point in _manifest()["points"]})


def _module_by_the_naming_rule(group: str) -> str | None:
    """The inventory module a group is named after, if there is one.

    A group is named after its module: the module's own name, or that
    name without the ``verification_`` prefix.  The full name wins when
    both could match — ``verification_agent`` is that module's, while
    the short ``agent`` belongs to ``agent.py``, which comes first in
    the inventory.
    """
    modules = _manifest_modules()
    for module in modules:
        if group == module[:-3]:
            return module
    for module in modules:
        if group == module[:-3].removeprefix("verification_"):
            return module
    return None


def _group_module(group: str) -> str | None:
    """The inventory module a group belongs to.

    A group **declared outside the inventory** claims no inventory
    module at all.  The judgment-point inventory names the modules
    that make judgments, and a golden group is named after the module
    it freezes — so a group the inventory does not describe is a hole
    in the inventory.  The framework-fix lane's judgments live in a
    **package** (``backend/framework_fix/``) and the census that
    generates the inventory surveys the flat ``backend/*.py`` modules
    only, so the inventory names no module for them; the script writes
    that hole down in ``GROUPS_OUTSIDE_THE_INVENTORY``, together with
    the files that hold the judgments the group freezes.  Such a group
    is validated by **those files**, and it must not take an inventory
    module away from another group.

    Every other group is named after its module — see
    :func:`_module_by_the_naming_rule`.
    """
    if group in snapshots.GROUPS_OUTSIDE_THE_INVENTORY:
        return None
    return _module_by_the_naming_rule(group)


def _surface_names(module: str) -> set[str]:
    """Every function a module defines — at module level or in a class.

    The names are read out of the module's **source**, not out of an
    imported module: the coverage report says what a module decides, and
    that is a fact about the source.  Reading the source also keeps this
    check independent of the import order the modules happen to need.
    """
    source = (_REPO_ROOT / "backend" / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


# ---------------------------------------------------------------------------
# The two tests the task names
# ---------------------------------------------------------------------------


def test_remaining_groups_match() -> None:
    """The frozen goldens still say what the three modules say today.

    Four things have to hold at once for every group: it is registered,
    the snapshot file holds it, the file agrees with the behavior as it
    stands, and the file's own check says so too.  The last one is the
    golden's whole job — a difference means a judgment moved, and the
    report names the differing lines.
    """
    stored = _stored()

    for name in GROUPS:
        assert name in snapshots.REGISTRY, (
            f"组 {name!r} 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
        )
        assert name in stored, (
            f"快照文件里没有组 {name!r}；文件里的是 {sorted(stored)}"
        )

        fresh = snapshots.render_group(name, root=_REPO_ROOT)
        assert fresh == stored[name], (
            f"冻结的 {name!r} 金标与当前行为不一致；"
            "差异说明判定面动过了，跑 scripts/judgment_snapshots.py --update 重新生成"
        )

        assert snapshots.check_group(name, _REPO_ROOT) == 0

    assert snapshots.main(["--check", "--root", str(_REPO_ROOT)]) == 0


def test_each_group_has_min_cases() -> None:
    """Every group holds at least four cases, and every id is its own.

    A group of one or two rows pins a single path through the function
    and says nothing about the others.  The ids have to be unique too:
    an id is how a reader finds the row, and how the corpus case it
    came from is looked up.

    The script's own floor is higher than this one — it refuses to
    render a group of fewer than :data:`snapshots.MIN_CASES` cases — so
    a group that reaches this floor has already cleared that one too.
    """
    for name in GROUPS:
        assert name in snapshots.REGISTRY, (
            f"组 {name!r} 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
        )
        document = snapshots.render_group(name, root=_REPO_ROOT)
        cases = document["cases"]
        assert len(cases) >= MIN_CASES, (
            f"组 {name!r} 只有 {len(cases)} 个用例；金标至少要 {MIN_CASES} 个"
        )
        assert len(cases) >= snapshots.MIN_CASES, (
            f"组 {name!r} 只有 {len(cases)} 个用例；"
            f"脚本自己的下限是 {snapshots.MIN_CASES} 个"
        )
        ids = [case["id"] for case in cases]
        assert len(set(ids)) == len(ids), f"组 {name!r} 的用例 id 重复：{ids}"


# ---------------------------------------------------------------------------
# The coverage the goldens claim
# ---------------------------------------------------------------------------


def test_every_verification_module_has_a_group() -> None:
    """Every ``verification_*`` module in the inventory has a group.

    This is the "补齐" the task asks for, stated as a fact about the
    tree: the inventory names the modules that make judgments, every
    registered group answers to exactly one of them — or is declared
    outside the inventory, in which case it is held to the files it
    declares — and none of the ``verification_*`` ones is left without
    a golden.
    """
    modules = _manifest_modules()
    claimed: dict[str, str] = {}
    for name in sorted(snapshots.REGISTRY):
        declared = snapshots.GROUPS_OUTSIDE_THE_INVENTORY.get(name)
        if declared is not None:
            # A group declared outside the inventory: it is a hole in
            # the inventory that the script writes down, so it is held
            # to the files that hold the judgments it freezes.  It
            # claims no inventory module — and it must not quietly
            # claim one by the naming rule either.
            assert declared, (
                f"组 {name!r} 登记成清单之外的组，却没有写明它钉住的文件"
            )
            for relative in declared:
                path = _REPO_ROOT / relative
                assert path.is_file(), (
                    f"清单之外的组 {name!r} 登记的文件不在：{relative}"
                )
            assert _module_by_the_naming_rule(name) is None, (
                f"组 {name!r} 既登记成清单之外的组，又按命名规则落在某个清单"
                "模块上；两者只能取一个"
            )
            continue
        module = _group_module(name)
        assert module is not None, (
            f"组 {name!r} 的名字不对应任何判定模块；"
            f"清单里的模块是 {modules}"
        )
        assert module not in claimed, (
            f"模块 {module!r} 被两个组认领：{claimed[module]!r} 与 {name!r}"
        )
        claimed[module] = name

    missing = [
        module
        for module in modules
        if module.startswith("verification_") and module not in claimed
    ]
    assert not missing, f"这些 verification_* 模块还没有金标组：{missing}"


def test_coverage_report_names_surfaces_that_exist() -> None:
    """Every name in the coverage report is a function of its module.

    The report is where a judgment that could not be frozen is written
    down, so a name in it that is not there would hide the gap twice:
    once in the report and once in the module.
    """
    modules = _manifest_modules()
    for module, entry in snapshots.COVERAGE_REPORT.items():
        assert module in modules, (
            f"覆盖报告里的模块 {module!r} 不在清单里；清单里的是 {modules}"
        )
        group = entry["group"]
        assert group in snapshots.REGISTRY, (
            f"覆盖报告说模块 {module!r} 的组是 {group!r}，但它没有注册"
        )
        assert _group_module(group) == module, (
            f"组 {group!r} 的名字与模块 {module!r} 对不上"
        )

        surfaces = _surface_names(module)
        for name in entry["pinned"]:
            assert name in surfaces, (
                f"模块 {module!r} 里没有函数 {name!r}，覆盖报告却把它算作已钉住"
            )
        for name in entry["undriven"]:
            assert name in surfaces, (
                f"模块 {module!r} 里没有函数 {name!r}，覆盖报告却把它算作无法驱动"
            )
        assert entry["pinned"], f"模块 {module!r} 的覆盖报告一个已钉住的函数都没写"


def test_undriven_surfaces_are_listed_with_a_reason() -> None:
    """A surface that could not be driven says why it could not be driven.

    "We could not freeze this one" is only useful when it says what the
    judgment reads — otherwise the next reader has to go and find out.
    """
    for module, entry in snapshots.COVERAGE_REPORT.items():
        for name, reason in entry["undriven"].items():
            assert isinstance(reason, str) and reason.strip(), (
                f"模块 {module!r} 的 {name!r} 没有写明为什么驱动不了"
            )


# ---------------------------------------------------------------------------
# The boundary conditions
# ---------------------------------------------------------------------------


def test_rendering_writes_nothing() -> None:
    """Rendering a group does not write a single byte.

    A golden that wrote a file would be a second source of truth, and
    the file it wrote would be a side effect of *reading* the behavior
    — which is how a snapshot ends up disagreeing with itself.
    """
    path = _REPO_ROOT / _SNAPSHOT_RELATIVE
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns

    for name in GROUPS:
        snapshots.render_group(name, root=_REPO_ROOT)

    assert path.read_bytes() == before, "渲染金标改了快照文件"
    assert path.stat().st_mtime_ns == stamp, "渲染金标碰了快照文件"


def test_rendering_starts_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendering a group starts no process.

    The groups are meant to drive judgment functions, not to *be* the
    thing under test — a run of the real gate inside the golden would
    make the golden depend on the machine it runs on.
    """

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("金标渲染不许起进程")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "check_output", _forbidden)
    monkeypatch.setattr(subprocess, "call", _forbidden)

    for name in GROUPS:
        snapshots.render_group(name, root=_REPO_ROOT)
