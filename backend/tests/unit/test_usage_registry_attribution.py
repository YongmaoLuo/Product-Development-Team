"""Cost-attribution coordinates in the usage registry (2026-10-26).

The registry already answered three questions about every LLM call the
backend made: which plan, which task, which session. That was enough to
say *how much* a plan spent and *which phase* spent it, but not enough
to say *where inside the verification* the money went — a plan's
``by_scene`` split shows that verification eats a large share of the
budget, yet it cannot name the verification point (``vp_id``) or the
round that burned it. Roughly a third of every plan's tokens landed in
``(unattributed)``, and an unattributed bucket cannot be optimised:
you cannot tell which VP is expensive when you cannot tell which VPs
the tokens belonged to at all.

This pins the two coordinates that close that gap, and the two
properties that make them safe to rely on:

* ``plan_usage_context`` binds them, ``current_vp_id`` / ``current_round``
  read them, and a nested scope restores the outer value on exit —
  without that, a per-task scope inside a round would erase the round
  it runs in;
* ``record_llm_call`` writes them into the JSONL line, next to the
  pre-existing fields, which keep their names and their values;
* a registry sink that fails is swallowed — the dispatch being recorded
  must not die because the bookkeeping did — but the failure is
  counted, so a silently-blind registry is visible rather than
  invisible.

Every test redirects ``usage_registry.repo_plans_dir`` to ``tmp_path``:
the autouse ``isolated_plans_dir`` conftest fixture redirects
``PDT_PLANS_DIR`` but this module's resolver is patched explicitly, so
a test can never write into the real repo's ``plans/`` tree.
"""

from __future__ import annotations

import json

import pytest

import usage_registry


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """Point the registry's writer at a per-test plans root."""
    target = tmp_path / "plans"
    monkeypatch.setattr(usage_registry, "repo_plans_dir", lambda: target)
    return target


def test_context_binding_restores(plans_dir):
    """A nested binding gives the outer value back when it exits."""
    with usage_registry.plan_usage_context(
        plan_id="p1", vp_id="VP-1", round=1
    ):
        assert usage_registry.current_vp_id() == "VP-1"
        assert usage_registry.current_round() == 1

        with usage_registry.plan_usage_context(
            plan_id="p1", vp_id="VP-2", round=2
        ):
            assert usage_registry.current_vp_id() == "VP-2"
            assert usage_registry.current_round() == 2

        # The inner scope is gone: the OUTER value is back — not None,
        # and not the inner one.
        assert usage_registry.current_vp_id() == "VP-1"
        assert usage_registry.current_round() == 1

    # Outside every scope there is no attribution at all.
    assert usage_registry.current_vp_id() is None
    assert usage_registry.current_round() is None


def test_nested_scope_keeps_the_outer_attribution(plans_dir):
    """A scope that names no VP/round keeps the ones it runs inside.

    A verification round binds its coordinates once; the per-task
    scopes inside it say nothing about which round they belong to, so
    they must not erase the answer.
    """
    with usage_registry.plan_usage_context(
        plan_id="p1", vp_id="VP-1", round=1
    ):
        with usage_registry.plan_usage_context(plan_id="p1", task_id="1-2"):
            assert usage_registry.current_vp_id() == "VP-1"
            assert usage_registry.current_round() == 1
            assert usage_registry.current_task_id() == "1-2"
        assert usage_registry.current_vp_id() == "VP-1"
        assert usage_registry.current_round() == 1


def test_jsonl_entry_has_attribution_keys(plans_dir):
    """The written line carries vp_id / round next to the old fields."""
    with usage_registry.plan_usage_context(
        plan_id="p1", task_id="1-2", vp_id="VP-7", round=2
    ):
        ok = usage_registry.record_llm_call(
            session_id="sid-1",
            scene="verification",
            provider="vendor-a",
            model="Vendor A-M3",
            ok=True,
            usage={"input_tokens": 10, "output_tokens": 5},
            cost_usd="0.001",
            num_turns=1,
            duration_ms=100,
        )

    assert ok is True
    entries = usage_registry.read_llm_calls("p1")
    assert len(entries) == 1
    entry = entries[0]

    # the two new coordinates
    assert entry["vp_id"] == "VP-7"
    assert entry["round"] == 2

    # the pre-existing fields keep their names and their values
    assert entry["plan_id"] == "p1"
    assert entry["task_id"] == "1-2"
    assert entry["session_id"] == "sid-1"
    assert entry["scene"] == "verification"
    assert entry["provider"] == "vendor-a"
    assert entry["model"] == "Vendor A-M3"
    assert entry["ok"] is True
    assert entry["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert entry["cost_usd"] == "0.001"
    assert entry["num_turns"] == 1
    assert entry["duration_ms"] == 100
    assert entry["error"] is None
    assert entry["ts"]

    # the raw line on disk carries them too (not just the parsed dict)
    raw = (plans_dir / "p1" / "llm_sessions.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(raw) == 1
    line = json.loads(raw[0])
    assert line["vp_id"] == "VP-7"
    assert line["round"] == 2
    assert line["session_id"] == "sid-1"


def test_entry_without_attribution_is_null(plans_dir):
    """An unbound call writes null, not a missing key."""
    with usage_registry.plan_usage_context(plan_id="p1"):
        usage_registry.record_llm_call(session_id="sid-none")

    entry = usage_registry.read_llm_calls("p1")[0]
    assert entry["vp_id"] is None
    assert entry["round"] is None


def test_sink_failure_does_not_raise(plans_dir, monkeypatch):
    """A sink that raises must not break the caller — but it is counted."""
    before = usage_registry.sink_failure_count()

    def _boom(path, line):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(usage_registry, "_sink_append", _boom)

    with usage_registry.plan_usage_context(
        plan_id="p1", vp_id="VP-7", round=2
    ):
        # no exception escapes: the dispatch being recorded survives
        ok = usage_registry.record_llm_call(
            session_id="sid-boom", scene="verification"
        )

    assert ok is False
    assert usage_registry.sink_failure_count() == before + 1
    # nothing was written
    assert usage_registry.read_llm_calls("p1") == []
