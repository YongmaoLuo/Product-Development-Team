"""
TDD spec — the executor's coding tool runs under the scene its process was
started with.

Background
----------
``cli.py --recover`` starts the executor as a CHILD PROCESS.  Whatever
scene the parent wanted the child's provider routing to use therefore has
to travel through the environment — the child shares no Python state with
its parent.  ``PDT_EXECUTOR_SCENE`` is that channel.

``agent.autonomous_coding()`` builds the ``ClaudeCodingTool`` the child
runs on, so it is where the variable has to be consumed: the tool is
constructed with ``scene=_executor_scene_from_env()``.

TDD contract — 4 test cases
---------------------------
1. ``test_unset_env_yields_none`` — an unset variable yields ``None``,
   so a parent that never set it behaves exactly as before.
2. ``test_blank_env_yields_none`` — a blank (or whitespace-only) value
   also yields ``None``: a blank scene name would be routed as if it
   were a scene.
3. ``test_env_value_is_returned_stripped`` — a value with surrounding
   whitespace is returned stripped.
4. ``test_claude_tool_is_built_with_the_scene`` — the
   ``ClaudeCodingTool(...)`` call inside ``autonomous_coding`` carries a
   ``scene`` keyword whose value is a call to
   ``_executor_scene_from_env()``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_AGENT_PY = _BACKEND_DIR / "agent.py"

_SCENE_ENV = "PDT_EXECUTOR_SCENE"


# ---------------------------------------------------------------------------
# Test 1 — an unset variable yields None
# ---------------------------------------------------------------------------


def test_unset_env_yields_none(monkeypatch):
    """No variable → ``None``.

    ``None`` is the "no scene" answer: ``ClaudeCodingTool`` treats it as
    "no scene routing" and falls back to the provider priority chain,
    which is what every caller had before this variable existed.
    """
    from agent import _executor_scene_from_env

    monkeypatch.delenv(_SCENE_ENV, raising=False)

    assert _executor_scene_from_env() is None


# ---------------------------------------------------------------------------
# Test 2 — a blank value yields None
# ---------------------------------------------------------------------------


def test_blank_env_yields_none(monkeypatch):
    """Empty / whitespace-only → ``None``.

    A blank scene is not a scene: routing it would resolve a chain for a
    scene nobody named, so it is treated as absent.
    """
    from agent import _executor_scene_from_env

    for blank in ("", "   ", "\t\n"):
        monkeypatch.setenv(_SCENE_ENV, blank)
        assert _executor_scene_from_env() is None, (
            f"a blank scene ({blank!r}) must read as None"
        )


# ---------------------------------------------------------------------------
# Test 3 — a value is returned stripped
# ---------------------------------------------------------------------------


def test_env_value_is_returned_stripped(monkeypatch):
    """The scene name is returned stripped of surrounding whitespace.

    The value crosses a process boundary, where a trailing newline is a
    routine accident of shell quoting; the scene name that reaches the
    provider routing table must be the bare one.
    """
    from agent import _executor_scene_from_env

    monkeypatch.setenv(_SCENE_ENV, "  base_drift_merge  ")

    assert _executor_scene_from_env() == "base_drift_merge"


# ---------------------------------------------------------------------------
# Test 4 — the tool is built with the scene
# ---------------------------------------------------------------------------


def test_claude_tool_is_built_with_the_scene():
    """``autonomous_coding`` builds the tool with ``scene=...``.

    Checked on the AST rather than by running the function: building the
    tool needs a provider chain and a settings file, and the contract
    under test is the wiring, not the dispatch.
    """
    tree = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))

    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "autonomous_coding"
        ),
        None,
    )
    assert function is not None, "autonomous_coding must exist in agent.py"

    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ClaudeCodingTool"
    ]
    assert calls, "autonomous_coding must build a ClaudeCodingTool"

    scenes = [
        keyword.value
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "scene"
    ]
    assert scenes, "the ClaudeCodingTool call must pass a scene keyword"

    assert any(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "_executor_scene_from_env"
        for value in scenes
    ), "the scene keyword must be _executor_scene_from_env()"
