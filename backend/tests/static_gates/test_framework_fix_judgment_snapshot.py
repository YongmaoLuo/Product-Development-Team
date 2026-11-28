"""The framework-fix judgment surface has a golden group of its own.

Why this file exists
--------------------
Task 5 (``backend/framework_fix/classifier.py``) and task 7
(``backend/framework_fix/read_model.py``) put the framework-fix lane's
judgments into code: whether one failure **blocks** the plan
(:func:`decide`) and what a plan is **displayed as** right now
(:func:`project_display_state`).

The behavioural goldens
(``backend/tests/fixtures/judgment_snapshots.json``) are the mechanism
that pins a judgment's *behavior*: a fixed input is run through a
judgment function and the output is frozen byte for byte, so a later
change to the judgment shows up as a changed golden instead of as a
quietly different verdict on somebody else's plan.

Until this group existed, no golden covered either function.  The
framework-fix lane could change what it decides — and what it shows —
and nothing in this repository would have said so.

What it pins
------------
1. ``test_group_registered`` — the group exists: it is registered in the
   checker's registry, the snapshot file holds it, and the corpus holds
   the fixed inputs it is rendered from.  The groups that were already
   there are all still there too: a new group that displaces an old one
   is a silent loss of coverage, not a new one.
2. ``test_check_passes`` — ``--check`` exits ``0``.  That is the golden's
   whole job, and it is checked over **every** group, so the old
   goldens and the new one have to agree at the same time.
3. ``test_surface_gate_green`` — the one entry point that runs the three
   judgment-surface checks (``scripts/ci_judgment_surface.sh``) still
   exits ``0`` on this tree.
4. ``test_every_group_is_in_the_inventory_or_declared_as_a_gap`` — the
   group is accounted for: either the inventory names the module it
   freezes, or the checker declares the gap.

Two more cases are the supporting checks.  One is a **negative control**:
a tree whose snapshot file has not been re-pinned yet must make
``--check`` go red *and* print the command that fixes it — a checker that
says "different" without saying how to regenerate leaves the reader to
guess.  The other pins the promise the golden makes: it freezes behavior
and does not assert correctness, so the corpus declares inputs only and
never states the answer.

A third one pins the **gap** this group exposes.  The judgment-point
inventory (``docs/judgment_points.json``) names the modules that make
judgments, and the framework-fix lane's judgments live in a package that
the census does not survey — so the inventory names no module for them,
and a group named after a module the inventory does not have cannot be
resolved by the naming rule the inventory and the groups share.  The
checker writes that gap down (``GROUPS_OUTSIDE_THE_INVENTORY``), and
this case holds it to that: every group is either in the inventory or
declared, and a declaration that outlived its gap is a declaration
nobody removed.

Scope, stated honestly: this gate reads the checker, the snapshot file,
the corpus and the inventory, and it runs the checker and the surface
script.  It does not judge whether the framework-fix decisions are
*right* — the golden never claims that, and neither does this file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: The repository root — three parents up from ``backend/tests/static_gates/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The behavioural-golden checker.
SNAPSHOTS = REPO_ROOT / "scripts" / "judgment_snapshots.py"

#: The one entry point that runs the three judgment-surface checks.
SURFACE = REPO_ROOT / "scripts" / "ci_judgment_surface.sh"

#: The golden file.
SNAPSHOT = REPO_ROOT / "backend" / "tests" / "fixtures" / "judgment_snapshots.json"

#: The inventory of judgment points — the modules that make judgments.
MANIFEST = REPO_ROOT / "docs" / "judgment_points.json"

#: The group this gate is about.
GROUP = "framework_fix"

#: The judgment functions the framework-fix golden freezes, and the file
#: each of them lives in.  The script's own declaration of the gap has to
#: name those files, so the declaration cannot drift away from the
#: judgments it is about.
FROZEN_JUDGMENTS = {
    "decide": "backend/framework_fix/classifier.py",
    "project_display_state": "backend/framework_fix/read_model.py",
}

#: The groups that were registered before this one.  A new group must not
#: take one of their names: a golden that displaces another one is a
#: silent loss of coverage, not a new one.
EARLIER_GROUPS = (
    "evidence",
    "agent",
    "buckets",
    "executor",
    "ci_runner",
    "loop",
    "subagent",
    "verification_agent",
)

#: The command that re-pins the golden file.  The checker prints it
#: whenever the snapshot is missing or differs; the gate holds it to
#: that promise.
UPDATE_COMMAND = "scripts/judgment_snapshots.py --update"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs.  The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "judgment_snapshots_framework_fix_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, SNAPSHOTS)
assert _spec and _spec.loader
snapshots = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = snapshots
_spec.loader.exec_module(snapshots)


# ---------------------------------------------------------------------------
# Running things
# ---------------------------------------------------------------------------


def _run(command: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    """Run ``command`` in this repository, and hand back what it said."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHON": sys.executable},
    )


def _transcript(completed: subprocess.CompletedProcess) -> str:
    """What a finished command said, both streams."""
    return f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"


#: The names under ``backend/tests`` that are never read by the golden
#: checker.  It reads exactly two files from there — the corpus and the
#: snapshot — and the test tree beside them is two orders of magnitude
#: bigger.
_UNREAD_TEST_NAMES = frozenset({"fixtures", "__init__.py"})


def _copy_backend(destination: Path) -> Path:
    """A copy of the backend tree, minus what the golden checker never reads.

    The checker reads the modules *and* the snapshot file out of one
    root, and it refuses a module that did not come from that root — so
    the modules have to be real files under the root, not links to
    another checkout's.
    """
    tests_dir = REPO_ROOT / "backend" / "tests"

    def _ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {
            name
            for name in names
            if name
            in {
                ".venv",
                "__pycache__",
                ".pytest_cache",
                ".ruff_cache",
                "logs",
                "e2e-verification-state",
            }
        }
        if Path(directory) == tests_dir:
            skipped |= {name for name in names if name not in _UNREAD_TEST_NAMES}
        return skipped

    shutil.copytree(REPO_ROOT / "backend", destination, ignore=_ignore)
    return destination


def _inventory_modules() -> list[str]:
    """Every judgment module the inventory names, in its own order."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return sorted({str(point["module"]) for point in document["points"]})


def _resolves_to_an_inventory_module(group: str, modules: list[str]) -> bool:
    """Whether the naming rule resolves ``group`` to an inventory module.

    The rule is the one the inventory and the groups share: a group is
    named after its module — the module's own name, or that name without
    the ``verification_`` prefix.
    """
    for module in modules:
        if group == module[:-3]:
            return True
    for module in modules:
        if group == module[:-3].removeprefix("verification_"):
            return True
    return False


# ---------------------------------------------------------------------------
# The three cases the task names
# ---------------------------------------------------------------------------


def test_group_registered() -> None:
    """The framework-fix group is registered, and it displaced no other one.

    A group that is registered but missing from the snapshot file is a
    group nobody can compare — the checker would report every other group
    as consistent and stay green.  A group in the snapshot file but not in
    the registry is a golden nothing ever re-renders, so it goes stale
    silently.  Both have to be true at once, and neither may cost an
    older group its place.
    """
    assert GROUP in snapshots.REGISTRY, (
        f"金标组 {GROUP!r} 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
    )

    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert GROUP in document, (
        f"快照文件里没有组 {GROUP!r}；文件里的是 {sorted(document)}。"
        f"重新生成快照的命令：{UPDATE_COMMAND}"
    )

    missing = [name for name in EARLIER_GROUPS if name not in document]
    assert not missing, (
        f"新增金标组之后这些组不见了：{missing}；文件里的是 {sorted(document)}。"
        "组名不能与已有的组重复 —— 覆盖掉一个旧组不是新增覆盖，是少了一份覆盖。"
    )

    cases = document[GROUP]["cases"]
    assert cases, f"金标组 {GROUP!r} 一个用例都没有：它什么也没钉住"
    ids = [case["id"] for case in cases]
    assert len(set(ids)) == len(ids), f"金标组 {GROUP!r} 的用例 id 重复：{ids}"
    for case in cases:
        assert "out" in case, (
            f"金标组 {GROUP!r} 的用例 {case['id']!r} 没有 out："
            "没有 out 的用例什么都没有钉住"
        )

    corpus = snapshots._load_corpus(REPO_ROOT)
    assert GROUP in corpus, (
        f"固定语料里没有组 {GROUP!r}；语料里的是 {sorted(corpus)}"
    )
    corpus_ids = [case["id"] for case in corpus[GROUP]]
    assert corpus_ids == ids, (
        f"金标组 {GROUP!r} 的用例与语料里的用例对不上："
        f"金标 {ids}，语料 {corpus_ids}"
    )


def test_check_passes() -> None:
    """``--check`` agrees with every group — the old ones and the new one.

    The check runs over the whole registry, so this says two things at
    once: the new group agrees with what the code produces today, and
    adding it did not disturb any of the goldens that were already
    there.  A golden that no longer matches the code is the one failure
    a reader cannot notice by reading the diff.
    """
    completed = _run(
        [sys.executable, str(SNAPSHOTS), "--check", "--root", str(REPO_ROOT)]
    )
    assert completed.returncode == 0, (
        "金标与当前行为对不上 —— 判定面动了，而金标没有跟上：\n"
        + _transcript(completed)
    )
    for name in EARLIER_GROUPS + (GROUP,):
        assert f"一致：{name}" in completed.stdout, (
            f"检查器没有把组 {name!r} 报成一致。每一个注册的组都要被比对，"
            f"结论也要在日志里。\n{_transcript(completed)}"
        )


def test_surface_gate_green() -> None:
    """The one entry point that runs the judgment-surface checks is green.

    ``scripts/ci_judgment_surface.sh`` is what CI runs and what a
    developer runs at a terminal.  A new golden group is only really
    wired in when that entry point still passes with the group in place —
    the entry point is the thing that runs the census, the three static
    gates and the goldens together.
    """
    completed = _run(["bash", str(SURFACE), "--root", str(REPO_ROOT)])
    assert completed.returncode == 0, (
        f"判定面一致性检查退出了 {completed.returncode}，应该是 0：\n"
        + _transcript(completed)
    )


# ---------------------------------------------------------------------------
# The supporting checks
# ---------------------------------------------------------------------------


def test_every_group_is_in_the_inventory_or_declared_as_a_gap() -> None:
    """Every registered group is either in the inventory or declared as a gap.

    The judgment-point inventory (``docs/judgment_points.json``) names the
    modules that make judgments, and a golden group is named after the
    module it freezes.  A group the inventory does not describe is
    therefore a hole in the inventory — and a hole nobody wrote down is a
    hole nobody closes.

    The framework-fix lane's judgments live in a **package**
    (``backend/framework_fix/``), and the census that generates the
    inventory surveys the flat ``backend/*.py`` modules, so the inventory
    names no module for them.  The script says so, in
    ``GROUPS_OUTSIDE_THE_INVENTORY``, and this case holds it to that:
    every group is either resolvable to an inventory module or declared,
    and every declaration names the files that hold the judgments the
    group freezes.

    The declaration is a **debt**, not a permission.  It has to shrink to
    empty when the census learns to survey packages, so the case also
    checks that the inventory does not describe those files yet: a
    declaration that outlived the gap it names would be a way to stop
    looking.
    """
    modules = _inventory_modules()
    declared = dict(snapshots.GROUPS_OUTSIDE_THE_INVENTORY)

    for name in sorted(snapshots.REGISTRY):
        if _resolves_to_an_inventory_module(name, modules):
            continue
        assert name in declared, (
            f"金标组 {name!r} 在判定面清单里找不到对应的模块，"
            f"脚本也没有把它登记成清单之外的组；清单里的模块是 {modules}。"
            "清单之外的组要在脚本的 GROUPS_OUTSIDE_THE_INVENTORY 里登记"
        )

    for name, files in sorted(declared.items()):
        assert name in snapshots.REGISTRY, (
            f"脚本把 {name!r} 登记成清单之外的组，但它没有注册；"
            f"已注册的组：{sorted(snapshots.REGISTRY)}"
        )
        for relative in files:
            path = REPO_ROOT / relative
            assert path.is_file(), (
                f"清单之外的组 {name!r} 登记的文件不在：{relative}"
            )
            module = str(path.relative_to(REPO_ROOT / "backend"))
            assert module not in modules, (
                f"判定面清单已经描述了 {module!r} —— 清单不再是缺口了，"
                f"把 {name!r} 从脚本的 GROUPS_OUTSIDE_THE_INVENTORY 里删掉"
            )

    for function, relative in sorted(FROZEN_JUDGMENTS.items()):
        assert relative in declared.get(GROUP, ()), (
            f"脚本登记的清单之外的组 {GROUP!r} 没有写明 {function!r} 所在的文件 "
            f"{relative!r}；它登记的是 {declared.get(GROUP, ())}"
        )
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert f"def {function}(" in source, (
            f"{relative!r} 里没有函数 {function!r}，"
            "但金标组登记的却是它"
        )


def test_a_missing_group_fails_the_check_and_says_how_to_make_one(
    tmp_path: Path,
) -> None:
    """A snapshot without the group is a failure, and the failure says what to do.

    The negative control for ``test_check_passes``: a tree whose snapshot
    file has not been re-pinned yet must make ``--check`` go red, and the
    red has to name the group that is missing and the command that
    regenerates it.  A checker that reported "different" without saying
    how to regenerate would leave the reader to guess — and a checker
    that stayed green would leave the group uncovered without a word.
    """
    tree = tmp_path / "tree"
    _copy_backend(tree / "backend")

    snapshot = tree / "backend" / "tests" / "fixtures" / "judgment_snapshots.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document.pop(GROUP, None)
    snapshot.write_text(
        json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    completed = _run(
        [sys.executable, str(SNAPSHOTS), "--check", "--root", str(tree)]
    )
    assert completed.returncode == 1, (
        f"快照里没有组 {GROUP!r} 时检查退出了 {completed.returncode}，应该是 1：\n"
        + _transcript(completed)
    )
    assert "快照缺失" in completed.stdout, (
        f"检查没有说快照缺了哪个组：\n{_transcript(completed)}"
    )
    assert GROUP in completed.stdout, (
        f"检查没有点名缺的是哪个组：\n{_transcript(completed)}"
    )
    assert "--update" in completed.stdout, (
        f"检查没有告诉读者怎么重新生成快照：\n{_transcript(completed)}"
    )


def test_the_golden_freezes_behavior_without_asserting_correctness() -> None:
    """The corpus declares inputs; it never states the answer.

    The golden's promise is narrow on purpose: it says the behavior has
    not *changed*, never that the behavior is *right*.  That promise is
    only keepable if the corpus holds inputs — a corpus that spelled out
    the expected disposition would turn the golden into a hand-written
    expectation table, and the long tail it exists for would be written
    by hand after all.

    So the corpus's framework-fix cases carry the facts the judgments
    read and nothing else, and the frozen ``out`` is whatever the
    judgments returned when the golden was written.
    """
    corpus = snapshots._load_corpus(REPO_ROOT)
    cases = corpus.get(GROUP) or []
    assert cases, f"固定语料里没有组 {GROUP!r} 的用例"

    forbidden = {"disposition", "blocking", "expected", "expected_result_of"}
    for case in cases:
        evidence = (case.get("verdict") or {}).get("evidence") or {}
        stated = sorted(forbidden & set(evidence))
        assert not stated, (
            f"语料组 {GROUP!r} 的用例 {case['id']!r} 写了判定结果 {stated}："
            "金标冻结行为，不断言正确性 —— 语料只固定拿什么去判"
        )
        assert "hit" in evidence, (
            f"语料组 {GROUP!r} 的用例 {case['id']!r} 没有命中事实："
            "判定函数没有输入可读"
        )

    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for case in document[GROUP]["cases"]:
        out = case["out"]
        assert isinstance(out["disposition"], str) and out["disposition"], (
            f"金标组 {GROUP!r} 的用例 {case['id']!r} 没有记下判定结论：{out!r}"
        )
        assert isinstance(out["blocking"], bool)
        assert isinstance(out["display_state"], (str, type(None)))
