"""The base-drift merge must be dispatched under its own scene.

Background
----------
``base_drift.BASE_DRIFT_MERGE_SCENE`` names the provider-routing scene a
base merge runs under.  Naming it is not enough: the executor runs as a
**child process** (``cli.py --recover``), so it shares no Python state with
the parent that dispatched it.  The only channel that survives the process
boundary is the environment — ``PDT_EXECUTOR_SCENE`` — which
``agent._executor_scene_from_env()`` reads back when it builds the
``ClaudeCodingTool``.

So the dispatch has to *carry* the scene: ``_run_repair_execution_async``
has to accept a ``scene`` and hand it to ``_spawn_executor_subprocess`` as
``extra_env={"PDT_EXECUTOR_SCENE": scene}``.  A scene that stops at the
Python call site is a scene the executor never hears about.

Two dispatch sites hand the plan to the executor when a base-drift conflict
went through the repair channel:

* ``_make_repair_on_complete``'s inner callback — the chain that comes
  back from a repair round, and
* ``_run_auto_verification_loop_inner`` — the loop's own round.

Both of them dispatch a **base merge**, so both of them have to name the
scene.  A third dispatch site that repairs product code must NOT name one:
an unnamed scene means "no scene routing", and giving it one would silently
move ordinary repair work onto the merge's provider tier.

TDD contract — 4 test cases
---------------------------
1. ``test_scene_reaches_the_spawn_env`` — a dispatch that names a scene
   reaches the subprocess environment under ``PDT_EXECUTOR_SCENE``.
2. ``test_without_scene_no_executor_scene_env`` — a dispatch that names no
   scene adds no such variable (the negative control).
3. ``test_both_base_drift_branches_pass_the_scene`` — every
   ``_base_drift_round_action(...) == "repair"`` branch in the production
   tree dispatches with ``scene=base_drift.BASE_DRIFT_MERGE_SCENE``.
4. ``test_both_branches_hand_over_an_on_complete`` — both branches hand
   over a non-empty ``on_complete``.

Boundary conditions
-------------------
* ``scene=None`` (or blank) adds no environment variable at all — the
  ordinary repair dispatch is unchanged.
* The spawn-failure path and the ``on_complete(-1)`` semantics are
  untouched by the scene: the scene is a routing hint, not a lifecycle
  change.
* The *semantics* of the hand-over ("re-enter verification on success")
  are covered by the existing ``_make_repair_on_complete`` unit tests
  (``test_verification_preflight.py`` / ``test_execution_stop_holds.py``);
  this file does not re-implement them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ``backend/`` on sys.path so ``import server`` works no matter which
# entry point runs the suite.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import server  # noqa: E402

PLAN_ID = "p-scene"

_SCENE_ENV = "PDT_EXECUTOR_SCENE"

# The production tree: every ``.py`` under ``backend/`` that is not a test
# and not a hidden (``.venv``) directory.
_PRODUCTION_ROOT = _BACKEND_DIR


def _iter_production_modules():
    """Yield every production ``.py`` module under ``backend/``."""
    for path in sorted(_PRODUCTION_ROOT.rglob("*.py")):
        relative = path.relative_to(_PRODUCTION_ROOT)
        if relative.parts[0] == "tests":
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        yield path


def _is_base_drift_repair_test(node) -> bool:
    """Is this the test of ``_base_drift_round_action(...) == "repair"``?"""
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    if len(node.comparators) != 1:
        return False
    comparator = node.comparators[0]
    if not isinstance(comparator, ast.Constant) or comparator.value != "repair":
        return False
    left = node.left
    return (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "_base_drift_round_action"
    )


def _base_drift_repair_branches():
    """Every ``if _base_drift_round_action(...) == "repair"`` in production.

    Returns a list of ``(path, ast.If)`` pairs.  The whole production tree
    is scanned, not just the module that happens to hold them today: a
    third branch somewhere else would dispatch a merge without the scene
    just as silently as one here.
    """
    found = []
    for path in _iter_production_modules():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_base_drift_repair_test(node.test):
                found.append((path, node))
    return found


def _dispatch_calls(branch):
    """The ``_run_repair_execution_async(...)`` calls inside one branch."""
    return [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_run_repair_execution_async"
    ]


def _keyword(call, name):
    """The value of a keyword argument, or ``None`` when it is absent."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_base_drift_merge_scene(node) -> bool:
    """Is this the expression ``base_drift.BASE_DRIFT_MERGE_SCENE``?"""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "base_drift"
        and node.attr == "BASE_DRIFT_MERGE_SCENE"
    )


# ---------------------------------------------------------------------------
# Fixtures — a throwaway plan tree and a spawn that never spawns
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """Point ``server.PLANS_DIR`` at a throwaway tree for this test."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", root)
    return root


@pytest.fixture
def plan_dir(plans_dir):
    """The plan's own directory, with an (empty) ``tasks.json``."""
    directory = plans_dir / PLAN_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tasks.json").write_text("[]", encoding="utf-8")
    return directory


@pytest.fixture
def spawn_spy(monkeypatch):
    """Capture the spawn instead of running an executor."""
    spawn = MagicMock(name="_spawn_executor_subprocess")
    monkeypatch.setattr(server, "_spawn_executor_subprocess", spawn)

    monkeypatch.setattr(
        server,
        "PlanState",
        lambda *a, **kw: MagicMock(force_set_phase=MagicMock()),
    )
    return spawn


@pytest.fixture(autouse=True)
def _clean_execution_state():
    """Keep the process-global execution state from leaking across tests."""
    with server._execution_state_lock:
        server._execution_state.clear()
    yield
    with server._execution_state_lock:
        server._execution_state.clear()


def _dispatch(plan_dir, spawn_spy, tmp_path, **kwargs):
    """Run one repair dispatch against a captured spawn."""
    workdir = tmp_path / "worktree"
    workdir.mkdir(exist_ok=True)

    proc = MagicMock()
    proc.pid = 4343
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    spawn_spy.side_effect = lambda **spawn_kwargs: (proc, tmp_path / "log")

    out = server._run_repair_execution_async(
        PLAN_ID, workdir, on_complete=None, **kwargs,
    )
    return out, spawn_spy.call_args


# ---------------------------------------------------------------------------
# Test 1 — the scene reaches the subprocess environment
# ---------------------------------------------------------------------------


def test_scene_reaches_the_spawn_env(plan_dir, spawn_spy, tmp_path):
    """A named scene travels into the child's environment.

    The executor is a child process: the scene has to cross the process
    boundary as an environment variable, and the variable the child reads
    is ``PDT_EXECUTOR_SCENE``.  A scene that stops at the Python call site
    is a scene the executor never hears about.
    """
    out, call_args = _dispatch(
        plan_dir, spawn_spy, tmp_path,
        scene=base_drift.BASE_DRIFT_MERGE_SCENE,
    )

    assert out["status"] == "started", "the dispatch itself must still work"
    extra_env = call_args.kwargs.get("extra_env") or {}
    assert extra_env.get(_SCENE_ENV) == "base_drift_merge", (
        f"the scene must reach the subprocess environment; got {extra_env!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — no scene means no scene variable (the negative control)
# ---------------------------------------------------------------------------


def test_without_scene_no_executor_scene_env(plan_dir, spawn_spy, tmp_path):
    """A dispatch that names no scene adds no environment variable.

    This is the negative control for test 1: without it, an implementation
    that always sets ``PDT_EXECUTOR_SCENE`` (to ``None``, to an empty
    string, or to a hard-coded name) would still pass test 1.  An ordinary
    product-code repair must keep the provider chain it had before.
    """
    _out, call_args = _dispatch(plan_dir, spawn_spy, tmp_path)

    extra_env = call_args.kwargs.get("extra_env") or {}
    assert _SCENE_ENV not in extra_env, (
        f"an ordinary repair dispatch must not set a scene; got {extra_env!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — both base-drift branches name the scene
# ---------------------------------------------------------------------------


def test_both_base_drift_branches_pass_the_scene():
    """Every base-drift repair dispatch names the merge scene.

    Both branches dispatch a **base merge**, so both have to name the
    scene.  A branch that forgets it does not fail loudly: the merge runs,
    on the ordinary provider chain, with no sign of what went wrong.
    """
    branches = _base_drift_repair_branches()

    assert len(branches) == 2, (
        f"expected exactly 2 base-drift repair branches, found "
        f"{len(branches)}: {[str(p) for p, _ in branches]}"
    )

    for path, branch in branches:
        calls = _dispatch_calls(branch)
        assert len(calls) == 1, (
            f"{path}:{branch.lineno} must dispatch the executor exactly once, "
            f"found {len(calls)} calls"
        )
        scene = _keyword(calls[0], "scene")
        assert scene is not None, (
            f"{path}:{branch.lineno} dispatches a base merge without a scene"
        )
        assert _is_base_drift_merge_scene(scene), (
            f"{path}:{branch.lineno} must pass "
            f"scene=base_drift.BASE_DRIFT_MERGE_SCENE, got "
            f"{ast.dump(scene)}"
        )


# ---------------------------------------------------------------------------
# Test 4 — both branches hand over a callback
# ---------------------------------------------------------------------------


def test_both_branches_hand_over_an_on_complete():
    """Both branches hand over a non-empty ``on_complete``.

    The scene says *where* the merge runs; the callback says what happens
    after it.  A base-drift dispatch that names the scene but drops the
    callback leaves the chain waiting for a round that never comes — so
    the two travel together.

    What the callback *does* ("re-enter verification on success") is
    covered by the existing ``_make_repair_on_complete`` unit tests; this
    test only pins that the callback is handed over at all.
    """
    branches = _base_drift_repair_branches()

    assert len(branches) == 2, (
        f"expected exactly 2 base-drift repair branches, found "
        f"{len(branches)}: {[str(p) for p, _ in branches]}"
    )

    for path, branch in branches:
        calls = _dispatch_calls(branch)
        assert len(calls) == 1, (
            f"{path}:{branch.lineno} must dispatch the executor exactly once, "
            f"found {len(calls)} calls"
        )
        on_complete = _keyword(calls[0], "on_complete")
        assert on_complete is not None, (
            f"{path}:{branch.lineno} dispatches without an on_complete "
            f"callback — the chain would wait for a round that never comes"
        )
        assert not (
            isinstance(on_complete, ast.Constant) and on_complete.value is None
        ), (
            f"{path}:{branch.lineno} hands over a None on_complete callback"
        )
