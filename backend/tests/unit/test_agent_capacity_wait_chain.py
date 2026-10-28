"""The capacity wait must be able to see the WHOLE provider chain.

2026-10-27 — "I tried one provider" is not "I tried the chain"
---------------------------------------------------------------
``_capacity_wait_providers()`` answers a narrower question than the
one the wait actually needs to answer. It reports the provider the
LAST refused call went to — one name, the one that happened to be
tried last. But the question a reader of the exhaustion marker asks
is "which providers could have served this call?", and that answer is
the whole chain the scene routes to.

The difference is not cosmetic. A scene whose chain is four providers
long, where only the last one refused, has not run out of providers —
it has run out of *one* provider. Reporting the chain is what lets a
caller tell those two apart.

So the chain has to be resolvable from the scene, and the scene has to
reach the wait: ``_query_with_capacity_wait()`` takes a ``scene`` and
resolves the chain from it.

What these tests pin:

  * the chain comes from the scene that was asked for;
  * a missing scene falls back to the coding tool's own scene;
  * a resolver that blows up yields an empty chain, not an exception —
    a routing answer we could not get is not a chain we know, and it
    must never turn a provider's refusal into a crash;
  * the chain is deduplicated and keeps the resolver's order;
  * the scene reaches the wait, and the wait's own behaviour (what it
    raises, and which providers it reports) is unchanged by this.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_routing  # noqa: E402
from capacity_wait import QuotaWaitExhausted  # noqa: E402
from coding_tool import ApiError  # noqa: E402


#: A chain that is long enough that "the last provider" and "the
#: chain" cannot be confused for one another.
CHAIN = ["MiniMax Huawei", "MiniMax Token Plan Huawei", "Apple", "mother"]


class _Tool:
    """The part of the coding tool the chain resolution reads."""

    def __init__(self, scene=None, provider="vendor-a"):
        self.scene = scene
        self.current_call_provider = provider


def _make_agent(tool):
    """An agent wired with only what the chain resolution touches."""
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.coding_tool = tool
    agent.logger = None
    return agent


def _stub_resolver(monkeypatch, result, seen=None):
    """Patch the routing resolver and record the scenes it was asked for."""
    import provider_routing as routing

    def _fake(scene, *args, **kwargs):
        if seen is not None:
            seen.append(scene)
        if isinstance(result, BaseException):
            raise result
        return list(result)

    monkeypatch.setattr(routing, "resolve_provider_chain", _fake)
    return _fake


# ---------------------------------------------------------------------------
# The chain comes from the scene
# ---------------------------------------------------------------------------


def test_chain_comes_from_the_scene(monkeypatch):
    """The chain is the scene's chain, all of it, not the last provider."""
    seen = []
    _stub_resolver(monkeypatch, CHAIN, seen)
    agent = _make_agent(_Tool(scene="execution"))

    assert agent._capacity_wait_chain("execution") == CHAIN
    assert seen == ["execution"]


def test_chain_falls_back_to_tool_scene(monkeypatch):
    """No scene given → the coding tool's own scene is the one to resolve."""
    seen = []
    _stub_resolver(monkeypatch, CHAIN, seen)
    agent = _make_agent(_Tool(scene="execution"))

    assert agent._capacity_wait_chain() == CHAIN
    assert agent._capacity_wait_chain(None) == CHAIN
    assert seen == ["execution", "execution"]


def test_resolver_failure_returns_empty(monkeypatch):
    """A resolver that raises yields an empty chain, not a traceback."""
    _stub_resolver(monkeypatch, RuntimeError("routing table is unreadable"))
    agent = _make_agent(_Tool(scene="execution"))

    assert agent._capacity_wait_chain("execution") == []


def test_empty_chain_is_empty(monkeypatch):
    """A scene with no candidates is an empty chain, not an error."""
    _stub_resolver(monkeypatch, [])
    agent = _make_agent(_Tool(scene="execution"))

    assert agent._capacity_wait_chain("execution") == []


def test_chain_is_deduplicated_and_ordered(monkeypatch):
    """The chain keeps the resolver's order and says each name once."""
    _stub_resolver(
        monkeypatch,
        ["A", "B", "A", "C", "B"],
    )
    agent = _make_agent(_Tool(scene="execution"))

    assert agent._capacity_wait_chain("execution") == ["A", "B", "C"]


def test_no_scene_anywhere_returns_empty(monkeypatch):
    """Neither a scene nor a tool scene → nothing to resolve, so empty."""
    seen = []
    _stub_resolver(monkeypatch, CHAIN, seen)
    agent = _make_agent(_Tool(scene=None))

    assert agent._capacity_wait_chain() == []
    assert seen == []


# ---------------------------------------------------------------------------
# The scene reaches the wait
# ---------------------------------------------------------------------------


def test_wait_uses_the_scene_it_was_given(monkeypatch):
    """``_query_with_capacity_wait`` resolves the chain from its scene.

    The wait's own behaviour is unchanged: a capacity refusal with no
    budget left raises ``QuotaWaitExhausted`` reporting the provider
    the call went to — the chain is resolved so a caller can ask for
    it, not so the exception changes shape.
    """
    seen = []
    _stub_resolver(monkeypatch, CHAIN, seen)
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")

    agent = _make_agent(_Tool(scene="execution", provider="vendor-a"))

    def _call():
        raise ApiError("quota exceeded", status="429")

    with pytest.raises(QuotaWaitExhausted) as excinfo:
        agent._query_with_capacity_wait(
            _call, _task(), (1, 2), scene="execution",
        )

    assert seen == ["execution"]
    # The reported providers are still the ones the call went to.
    assert excinfo.value.providers == ["vendor-a"]


def _task():
    """A task object good enough for the wait's log line."""
    from task import SubTask

    return SubTask(
        id="14-2",
        title="implement the thing",
        description="implement the thing",
        test_command="exit 1",
        status="pending",
        project_dir=".",
        files_to_modify=["changed.py"],
        model_type="medium",
    )
