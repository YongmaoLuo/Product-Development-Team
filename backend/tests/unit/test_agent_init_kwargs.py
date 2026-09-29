"""
TDD spec — AutonomousAgent.__init__ accepts guard + dispatcher + runtime_state.

Background
----------
The dispatcher/guard refactor split the task-execution loop's
responsibilities into three orthogonal collaborators:

  * ``guard``     — coarse-grained validator that runs before each
                    executor tick (cycle, same-id loop, file-modify
                    contract). Stand-in for the existing
                    ``self._check_*`` methods.
  * ``dispatcher`` — per-tick loop controller: picks the next eligible
                    task, invokes the executor, persists the result
                    via the runtime_state handle. Stand-in for
                    ``self._run_async`` and the in-line ``while True``
                    pattern.
  * ``runtime_state`` — typed wrapper around the
                    ``plan_verification.runtime_state`` SQLite JSON
                    column (see ``runtime_state.py``). Holds
                    in-flight bookkeeping (e.g. ``pending_vps``) that
                    both the dispatcher and the executor read/mutate.

The constructor must accept all three as optional kwargs and store
them on ``self`` so downstream methods can reach them without a
global. Default to ``None`` so existing callers (no kwargs) keep
working.

TDD contract — 3 test cases
---------------------------
1. ``test_init_accepts_guard_kwarg`` — passing ``guard=<obj>`` stores
   it as ``agent.guard``.
2. ``test_init_accepts_dispatcher_kwarg`` — passing
   ``dispatcher=<obj>`` stores it as ``agent.dispatcher``.
3. ``test_init_accepts_runtime_state_kwarg`` — passing a real
   ``RuntimeState`` instance stores it as ``agent.runtime_state``.
4. ``test_init_defaults_to_none`` — when no kwargs are passed, the
   three new attributes are all ``None`` (backward-compat).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _DummyCodingTool:
    """Minimal coding tool stub — only ``__init__`` signature is consumed
    by ``AutonomousAgent.__init__``; we never reach an LLM call.
    """

    def __init__(self, *args, **kwargs):
        pass


def _git_init(project_dir: Path) -> None:
    """Initialise a real git repo at ``project_dir`` so ``GitManager`` can
    bind to it (GitManager uses ``search_parent_directories=True`` and
    would otherwise walk up to a sibling checkout).
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def project_dir(tmp_path):
    """A real project_dir with a real git repo so GitManager can bind."""
    _git_init(tmp_path)
    return tmp_path


def _build_agent(project_dir: Path, **extra):
    """Build a minimal AutonomousAgent bound to ``project_dir``."""
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for guard/dispatcher/runtime_state kwargs",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
        **extra,
    )


# ---------------------------------------------------------------------------
# Test 1 — guard kwarg is accepted and stored
# ---------------------------------------------------------------------------


def test_init_accepts_guard_kwarg(project_dir):
    """``AutonomousAgent(guard=<obj>)`` must store the handle on
    ``self.guard`` so downstream methods (the dispatcher's per-tick
    guard call) can reach it without a global.

    The guard object is intentionally a sentinel ``object()`` — the
    constructor doesn't validate its type, only stores the reference.
    """
    sentinel_guard = object()
    agent = _build_agent(project_dir, guard=sentinel_guard)

    assert agent.guard is sentinel_guard, (
        "AutonomousAgent.__init__ must store the ``guard`` kwarg as "
        "``self.guard`` (got "
        f"{getattr(agent, 'guard', '<missing>')!r})"
    )


# ---------------------------------------------------------------------------
# Test 2 — dispatcher kwarg is accepted and stored
# ---------------------------------------------------------------------------


def test_init_accepts_dispatcher_kwarg(project_dir):
    """``AutonomousAgent(dispatcher=<obj>)`` must store the handle on
    ``self.dispatcher`` so the executor's loop can route per-tick
    callbacks (e.g. ``dispatcher.dispatch_next``) through the typed
    collaborator instead of an inline ``while True``.
    """
    sentinel_dispatcher = object()
    agent = _build_agent(project_dir, dispatcher=sentinel_dispatcher)

    assert agent.dispatcher is sentinel_dispatcher, (
        "AutonomousAgent.__init__ must store the ``dispatcher`` kwarg "
        f"as ``self.dispatcher`` (got "
        f"{getattr(agent, 'dispatcher', '<missing>')!r})"
    )


# ---------------------------------------------------------------------------
# Test 3 — runtime_state kwarg is accepted and stored
# ---------------------------------------------------------------------------


def test_init_accepts_runtime_state_kwarg(project_dir):
    """``AutonomousAgent(runtime_state=<RuntimeState>)`` must store the
    handle on ``self.runtime_state``.

    We use the real ``RuntimeState`` dataclass from
    ``runtime_state.py`` because it's the typed wrapper around the
    ``plan_verification.runtime_state`` SQLite column — passing it
    around is exactly what the dispatcher refactor needs.
    """
    from runtime_state import RuntimeState

    rs = RuntimeState(pending_vps=["vp-001", "vp-002"])
    agent = _build_agent(project_dir, runtime_state=rs)

    assert agent.runtime_state is rs, (
        "AutonomousAgent.__init__ must store the ``runtime_state`` "
        "kwarg as ``self.runtime_state`` (got "
        f"{getattr(agent, 'runtime_state', '<missing>')!r})"
    )
    # And the stored value must preserve its payload — the executor
    # reads ``agent.runtime_state.pending_vps`` directly.
    assert agent.runtime_state.pending_vps == ["vp-001", "vp-002"]


# ---------------------------------------------------------------------------
# Test 4 — defaults are None (backward-compat)
# ---------------------------------------------------------------------------


def test_init_defaults_to_none(project_dir):
    """Existing callers (no kwargs) must still work — the three new
    attributes default to ``None`` so the legacy JSON-file persistence
    path stays the active default.

    This is the regression guard against the same mistake that
    bit ``verif_repo``: when the new params were introduced they
    had to default to ``None`` so older tests / subprocess flows
    didn't break.
    """
    agent = _build_agent(project_dir)

    assert agent.guard is None, (
        f"default ``guard`` must be None, got {agent.guard!r}"
    )
    assert agent.dispatcher is None, (
        f"default ``dispatcher`` must be None, got {agent.dispatcher!r}"
    )
    assert agent.runtime_state is None, (
        f"default ``runtime_state`` must be None, got {agent.runtime_state!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — all three kwargs can be passed at once
# ---------------------------------------------------------------------------


def test_init_accepts_all_three_kwargs_at_once(project_dir):
    """The dispatcher refactor will instantiate the agent with all three
    collaborators wired in; the constructor must accept them
    simultaneously and store each on its own attribute (no overwriting).
    """
    from runtime_state import RuntimeState

    sentinel_guard = object()
    sentinel_dispatcher = object()
    rs = RuntimeState(pending_vps=["vp-A"])

    agent = _build_agent(
        project_dir,
        guard=sentinel_guard,
        dispatcher=sentinel_dispatcher,
        runtime_state=rs,
    )

    assert agent.guard is sentinel_guard
    assert agent.dispatcher is sentinel_dispatcher
    assert agent.runtime_state is rs
    assert agent.runtime_state.pending_vps == ["vp-A"]
