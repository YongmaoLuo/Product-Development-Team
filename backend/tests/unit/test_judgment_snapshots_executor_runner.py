"""The Tier B goldens for the executor and the CI runner, frozen into JSON.

Why this file exists
--------------------
The first three goldens (``evidence``, ``agent``, ``buckets``) pin the
judgment modules the material calls *core*.  This file pins the two
modules that decide **whether the work ran and whether it passed**:

* ``executor`` — the executor (``backend/verification_executor.py``) and
  the layer planner it shares with the task executor
  (``backend/base_executor.py``): what counts as a conclusion this
  round, what still has to be dispatched, and how the deferred layers
  are ordered;
* ``ci_runner`` — the CI runner (``backend/verification_ci_runner.py``):
  which sub-class an ``e2e`` point is, which lane an entry asks for,
  what a failure's shape is, and which failures are **blocking**.

The contract is the same as the first three goldens: a golden does not
claim the behavior is *right*, it claims the behavior has not
**changed**.  So the assertions here are about *matching*, not about
semantic expectations.

Where the inputs come from
--------------------------
The inputs are the fixed corpus of task 16
(``backend/tests/fixtures/judgment_corpus.py``).  The corpus's group of
the same name holds that group's cases, so a case id in the golden can
be looked up in the corpus to see what it fed in.  The outputs are
computed by the modules under test, so this file states no verdict of
its own.

The boundary conditions
-----------------------
Both groups only drive judgment functions that are **pure** or that a
minimal stub can drive: no process is started and nothing is written to
disk.  The two tests at the bottom of the file pin that boundary — a
golden that started a process or wrote a file would not be a golden,
it would be a test that runs the world.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_judgment_snapshots_core.py``
loads it that way.
"""

from __future__ import annotations

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

#: The smallest number of cases a group may hold.  Fewer than this and
#: the group pins a handful of paths rather than a behavior.
MIN_CASES = 6

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  It is registered in ``sys.modules`` *before* it is
#: executed, because ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs.  The registered name is
#: unique to this file, so nothing else can collide with it.
_MODULE_NAME = "judgment_snapshots_executor_runner_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
snapshots = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = snapshots
_spec.loader.exec_module(snapshots)

#: The two groups this file pins — the script's own statement of them,
#: so the two cannot drift apart.
GROUPS = snapshots.RUNNER_GROUPS


def _stored() -> dict:
    """The frozen snapshot file, as it is on disk."""
    path = _REPO_ROOT / _SNAPSHOT_RELATIVE
    assert path.exists(), f"快照文件不在：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_executor_group_matches() -> None:
    """The frozen executor golden still says what the executor says today.

    Three things have to hold at once: the group is registered, the
    snapshot file holds it, and the file agrees with the behavior as it
    stands.  The last one is the golden's whole job — a difference means
    a judgment moved, and the report names the differing lines.
    """
    assert "executor" in snapshots.REGISTRY, (
        f"组 'executor' 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
    )

    stored = _stored()
    assert "executor" in stored, (
        f"快照文件里没有组 'executor'；文件里的是 {sorted(stored)}"
    )

    fresh = snapshots.render_group("executor", root=_REPO_ROOT)
    assert fresh == stored["executor"], (
        "冻结的执行器金标与当前行为不一致；"
        "差异说明判定面动过了，跑 scripts/judgment_snapshots.py --update 重新生成"
    )

    assert snapshots.check_group("executor", _REPO_ROOT) == 0
    assert snapshots.main(["--check", "--root", str(_REPO_ROOT)]) == 0


def test_ci_runner_group_matches() -> None:
    """The frozen CI runner golden still says what the runner says today.

    The same three things as the executor golden: registered, stored,
    and matching.  The runner's judgments are the ones that decide
    whether a CI run is a blocking failure or an inconclusive one, so a
    silent change there is exactly what a golden is for.
    """
    assert "ci_runner" in snapshots.REGISTRY, (
        f"组 'ci_runner' 没有注册；已注册的组：{sorted(snapshots.REGISTRY)}"
    )

    stored = _stored()
    assert "ci_runner" in stored, (
        f"快照文件里没有组 'ci_runner'；文件里的是 {sorted(stored)}"
    )

    fresh = snapshots.render_group("ci_runner", root=_REPO_ROOT)
    assert fresh == stored["ci_runner"], (
        "冻结的运行器金标与当前行为不一致；"
        "差异说明判定面动过了，跑 scripts/judgment_snapshots.py --update 重新生成"
    )

    assert snapshots.check_group("ci_runner", _REPO_ROOT) == 0
    assert snapshots.main(["--check", "--root", str(_REPO_ROOT)]) == 0


def test_each_group_has_min_cases() -> None:
    """Every group holds at least six cases, and every id is its own.

    A group of one or two rows pins a single path through the function
    and says nothing about the others.  The ids have to be unique too:
    an id is how a reader finds the row.
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
        ids = [case["id"] for case in cases]
        assert len(set(ids)) == len(ids), f"组 {name!r} 的用例 id 重复：{ids}"


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
    thing under test — a run of the CI gate inside the golden would
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
