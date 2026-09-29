"""Tests for the per-plan LLM-call usage registry (2026-09-21).

Two layers are covered:

* ``usage_registry`` itself — append/read semantics, plan/task
  attribution via contextvar + ``PDT_PLAN_ID`` env fallback, concurrent
  append safety, and torn-line tolerance.
* ``coding_tool`` plumbing — ``_run_interactive_resilient`` records one
  registry entry per subprocess attempt (success, empty-response retry,
  and generic failure) with the usage metadata captured from the
  stream-json ``result`` event.

Every test redirects ``usage_registry.repo_plans_dir`` to ``tmp_path``:
the autouse ``isolated_plans_dir`` conftest fixture redirects
``server.PLANS_DIR`` but does not know about this module's resolver, and
an unpatched test would otherwise write into the real repo's ``plans/``.
"""

from __future__ import annotations

import json
import threading

import pytest

import usage_registry
from coding_tool import (
    ApiError,
    ClaudeCodingTool,
    EmptyResponseError,
)


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    target = tmp_path / "plans"
    monkeypatch.setattr(usage_registry, "repo_plans_dir", lambda: target)
    return target


# ---------------------------------------------------------------------------
# usage_registry semantics
# ---------------------------------------------------------------------------


def test_record_appends_jsonl_with_all_fields(plans_dir):
    with usage_registry.plan_usage_context(plan_id="plan-1", task_id="1-2"):
        ok = usage_registry.record_llm_call(
            session_id="sid-1",
            scene="execution",
            provider="vendor-a",
            model="Vendor A-M3",
            ok=True,
            usage={"input_tokens": 10, "output_tokens": 5},
            cost_usd="0.001",
            num_turns=1,
            duration_ms=100,
        )
    assert ok is True
    entries = usage_registry.read_llm_calls("plan-1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["plan_id"] == "plan-1"
    assert entry["task_id"] == "1-2"
    assert entry["session_id"] == "sid-1"
    assert entry["scene"] == "execution"
    assert entry["provider"] == "vendor-a"
    assert entry["model"] == "Vendor A-M3"
    assert entry["ok"] is True
    assert entry["usage"]["input_tokens"] == 10
    assert entry["cost_usd"] == "0.001"
    assert entry["num_turns"] == 1
    assert entry["duration_ms"] == 100
    assert entry["error"] is None


def test_record_without_plan_is_silent_noop(plans_dir, monkeypatch):
    monkeypatch.delenv("PDT_PLAN_ID", raising=False)
    ok = usage_registry.record_llm_call(session_id="sid-x", ok=True)
    assert ok is False
    assert not (plans_dir / "sid-x").exists()
    assert list(plans_dir.rglob("*")) == []


def test_record_env_fallback_when_no_context(plans_dir, monkeypatch):
    monkeypatch.setenv("PDT_PLAN_ID", "plan-from-env")
    ok = usage_registry.record_llm_call(session_id="sid-env", ok=True)
    assert ok is True
    entries = usage_registry.read_llm_calls("plan-from-env")
    assert [e["session_id"] for e in entries] == ["sid-env"]
    assert entries[0]["task_id"] is None


def test_context_overrides_env(plans_dir, monkeypatch):
    monkeypatch.setenv("PDT_PLAN_ID", "plan-from-env")
    with usage_registry.plan_usage_context(plan_id="plan-from-ctx"):
        usage_registry.record_llm_call(session_id="sid-c", ok=True)
    assert usage_registry.read_llm_calls("plan-from-env") == []
    entries = usage_registry.read_llm_calls("plan-from-ctx")
    assert [e["session_id"] for e in entries] == ["sid-c"]


def test_context_with_only_task_id_keeps_env_plan(plans_dir, monkeypatch):
    # The executor-subprocess pattern: agent.py binds task_id per task
    # while the plan id resolves through PDT_PLAN_ID.
    monkeypatch.setenv("PDT_PLAN_ID", "plan-env")
    with usage_registry.plan_usage_context(task_id="3-1"):
        usage_registry.record_llm_call(session_id="sid-t", ok=True)
    entries = usage_registry.read_llm_calls("plan-env")
    assert len(entries) == 1
    assert entries[0]["task_id"] == "3-1"


def test_record_without_session_id_is_noop(plans_dir):
    with usage_registry.plan_usage_context(plan_id="plan-1"):
        assert usage_registry.record_llm_call(session_id=None, ok=True) is False
    assert usage_registry.read_llm_calls("plan-1") == []


def test_concurrent_appends_never_tear_lines(plans_dir):
    # Mirrors production: raw threads do NOT inherit contextvars, so
    # each worker binds the plan context inside its own thread (the
    # same way agent.py / server.py thread entries do).
    def _worker(n: int) -> None:
        with usage_registry.plan_usage_context(plan_id="plan-race"):
            for i in range(10):
                usage_registry.record_llm_call(
                    session_id=f"sid-{n}-{i}", ok=True
                )

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    path = plans_dir / "plan-race" / "llm_sessions.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    for line in lines:  # every line parses — none interleaved/torn
        json.loads(line)
    entries = usage_registry.read_llm_calls("plan-race")
    assert len(entries) == 100


def test_read_tolerates_torn_tail_line(plans_dir):
    with usage_registry.plan_usage_context(plan_id="plan-torn"):
        usage_registry.record_llm_call(session_id="sid-ok", ok=True)
    path = plans_dir / "plan-torn" / "llm_sessions.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-21T', )  # torn write from a crash
    entries = usage_registry.read_llm_calls("plan-torn")
    assert [e["session_id"] for e in entries] == ["sid-ok"]


def test_plan_usage_context_resets_after_scope(plans_dir, monkeypatch):
    monkeypatch.delenv("PDT_PLAN_ID", raising=False)
    with usage_registry.plan_usage_context(plan_id="plan-a"):
        pass
    assert usage_registry.current_plan_id() is None


# ---------------------------------------------------------------------------
# coding_tool plumbing — one registry entry per subprocess attempt
# ---------------------------------------------------------------------------


def _make_tool(monkeypatch, stub):
    tool = ClaudeCodingTool()
    monkeypatch.setattr(tool, "_run_claude_interactive", stub)
    return tool


_SUCCESS_META = {
    "usage": {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 0,
    },
    "total_cost_usd": "0.0123",
    "num_turns": 2,
    "duration_ms": 8000,
    "duration_api_ms": 7000,
    "is_error": False,
}


def test_resilient_records_success_with_usage_meta(plans_dir, monkeypatch):
    def _stub(*args, **kwargs):
        return ('{"ok": true}', "sid-success", dict(_SUCCESS_META))

    tool = _make_tool(monkeypatch, _stub)
    with usage_registry.plan_usage_context(plan_id="plan-s", task_id="1-1"):
        text, sid = tool._run_interactive_resilient(
            "prompt", None, session_id="sid-success", resume=False,
            excluded_providers=None, total_timeout=None, allowed_tools=None,
            scene="execution",
        )
    assert text == '{"ok": true}'
    assert sid == "sid-success"
    entries = usage_registry.read_llm_calls("plan-s")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["ok"] is True
    assert entry["session_id"] == "sid-success"
    assert entry["task_id"] == "1-1"
    assert entry["scene"] == "execution"
    assert entry["usage"]["input_tokens"] == 1200
    assert entry["usage"]["cache_read_input_tokens"] == 4000
    assert entry["cost_usd"] == "0.0123"
    assert entry["num_turns"] == 2


def test_resilient_records_empty_response_then_reraises(
    plans_dir, monkeypatch
):
    def _stub(*args, **kwargs):
        raise EmptyResponseError(
            "no assistant text",
            provider="vendor-a",
            returncode=0,
            stderr="",
            elapsed_sec=1.0,
        )

    monkeypatch.setattr(ClaudeCodingTool, "MAX_EMPTY_RESPONSE_RETRIES", 0)
    tool = _make_tool(monkeypatch, _stub)
    with usage_registry.plan_usage_context(plan_id="plan-e"):
        with pytest.raises(EmptyResponseError):
            tool._run_interactive_resilient(
                "p", None, session_id="sid-dead", resume=False,
                excluded_providers=None, total_timeout=None,
                allowed_tools=None, scene="prd",
            )
    entries = usage_registry.read_llm_calls("plan-e")
    assert len(entries) == 1
    assert entries[0]["ok"] is False
    assert entries[0]["error"] == "empty_response"
    assert entries[0]["session_id"] == "sid-dead"


def test_resilient_records_generic_error_and_propagates(plans_dir, monkeypatch):
    def _stub(*args, **kwargs):
        raise ApiError(message="provider 500", status=500, retry_after=None)

    tool = _make_tool(monkeypatch, _stub)
    with usage_registry.plan_usage_context(plan_id="plan-err"):
        with pytest.raises(ApiError):
            tool._run_interactive_resilient(
                "p", None, session_id="sid-err", resume=False,
                excluded_providers=None, total_timeout=None,
                allowed_tools=None, scene="verification",
            )
    entries = usage_registry.read_llm_calls("plan-err")
    assert len(entries) == 1
    assert entries[0]["ok"] is False
    assert entries[0]["error"] == "ApiError"


def test_resilient_records_nothing_without_plan(plans_dir, monkeypatch):
    monkeypatch.delenv("PDT_PLAN_ID", raising=False)

    def _stub(*args, **kwargs):
        return ("{}", "sid-orphan", dict(_SUCCESS_META))

    tool = _make_tool(monkeypatch, _stub)
    text, _ = tool._run_interactive_resilient(
        "p", None, session_id="sid-orphan", resume=False,
        excluded_providers=None, total_timeout=None, allowed_tools=None,
        scene="execution",
    )
    assert text == "{}"
    assert list(plans_dir.rglob("*")) == []


def test_resilient_falls_back_to_env_plan(plans_dir, monkeypatch):
    """Executor-subprocess pattern: no context, PDT_PLAN_ID env set."""
    monkeypatch.setenv("PDT_PLAN_ID", "plan-subproc")

    def _stub(*args, **kwargs):
        return ("{}", "sid-sub", dict(_SUCCESS_META))

    tool = _make_tool(monkeypatch, _stub)
    tool._run_interactive_resilient(
        "p", None, session_id="sid-sub", resume=False,
        excluded_providers=None, total_timeout=None, allowed_tools=None,
        scene="execution",
    )
    entries = usage_registry.read_llm_calls("plan-subproc")
    assert [e["session_id"] for e in entries] == ["sid-sub"]
    assert entries[0]["task_id"] is None
