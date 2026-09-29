"""Per-plan LLM-call attribution registry (Step 1 of plan usage stats).

Every LLM call the backend makes funnels through
``coding_tool.ClaudeCodingTool._run_claude_interactive``. This module
gives that single choke point a way to record *which plan (and task) a
Claude session belonged to* so a later aggregation pass can join the
sessions against CC Switch's own ledger (``proxy_request_logs`` in
``~/.cc-switch/cc-switch.db``) and answer "how much did plan X burn".

Design notes
------------
* **Two attribution channels.** In-process callers (PRD/ arch / test /
  task / verification / repair phases) set a :class:`~contextvars.ContextVar`
  via :func:`plan_usage_context` (server middleware + background-thread
  entries). The execution *subprocess* instead relies on the ``PDT_PLAN_ID``
  env var that ``server.py`` already sets when spawning the executor
  (see ``_spawn_execution_subprocess``); :func:`current_plan_id` falls
  back to it automatically.
* **Never break a dispatch.** :func:`record_llm_call` swallows all I/O
  errors and returns ``False`` when no plan can be resolved. Registry
  failures must never surface as workflow failures.
* **Both CC Switch data sources.** The registry only records session
  ids; the aggregator (Step 2) is expected to match them against CC
  Switch rows with *either* ``data_source`` — ``proxy`` rows for calls
  that went through the local proxy and ``session_log`` rows for
  direct (scene-routed) calls CC Switch learned from the session JSONL
  transcripts. A session in this registry with zero CC Switch rows is
  itself a signal (scanner lag, dead session, or a bug) and is kept,
  not dropped.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

#: ``{"plan_id": Optional[str], "task_id": Optional[str]}`` — per-thread /
#: per-async-task attribution context set by server middleware, background
#: thread entries, and per-task scopes in the executor.
_plan_ctx: ContextVar[Optional[Dict[str, Optional[str]]]] = ContextVar(
    "pdt_plan_usage_ctx", default=None
)

#: Serializes appends from concurrent threads (verification round + repair
#: generation + phase calls can record from several threads at once).
#: A single ``threading.Lock`` is enough: writes are one line each and
#: POSIX O_APPEND makes each write atomic w.r.t. interleaving anyway; the
#: lock guards the open/close pair so a crash mid-append cannot leave a
#: torn line that a later reader chokes on.
_registry_lock = threading.Lock()


def repo_plans_dir() -> Path:
    """Plans root, resolved the same way the rest of the backend does.

    Delegates to :func:`config_paths.resolve_plans_dir` so the
    ``PDT_PLANS_DIR`` escape hatch (used by the test suite and by
    operators running a redirected workspace) applies to the registry
    too — a registry that resolved independently would write into the
    live ``<repo>/plans`` tree during tests. Falls back to the
    file-relative path if the helper is unavailable (e.g. this module
    imported standalone).
    """
    try:
        from config_paths import resolve_plans_dir

        return resolve_plans_dir()
    except Exception:
        return Path(__file__).resolve().parent.parent / "plans"


def current_plan_id() -> Optional[str]:
    """Resolve the plan this call belongs to.

    ContextVar first (in-process phases), ``PDT_PLAN_ID`` env fallback
    (executor subprocess — set by ``server.py`` at spawn time).
    """
    ctx = _plan_ctx.get()
    if ctx and ctx.get("plan_id"):
        return ctx["plan_id"]
    return os.environ.get("PDT_PLAN_ID") or None


def current_task_id() -> Optional[str]:
    ctx = _plan_ctx.get()
    return (ctx or {}).get("task_id")


@contextmanager
def plan_usage_context(
    plan_id: Optional[str] = None, task_id: Optional[str] = None
) -> Iterator[None]:
    """Bind LLM calls in this scope to ``plan_id`` / ``task_id``.

    Passing ``plan_id=None`` (e.g. only task attribution is known) keeps
    the env ``PDT_PLAN_ID`` fallback alive for plan resolution.
    """
    token = _plan_ctx.set({"plan_id": plan_id, "task_id": task_id})
    try:
        yield
    finally:
        _plan_ctx.reset(token)


def record_llm_call(
    *,
    session_id: Optional[str],
    scene: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    ok: bool = True,
    usage: Optional[Dict[str, Any]] = None,
    cost_usd: Optional[Any] = None,
    num_turns: Optional[int] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> bool:
    """Append one JSON line to ``plans/<plan_id>/llm_sessions.jsonl``.

    Returns ``True`` when a line was written. ``False`` (silent no-op)
    when no plan can be resolved or the write fails — the registry must
    never break a dispatch.
    """
    plan_id = current_plan_id()
    if not plan_id or not session_id:
        return False
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "task_id": current_task_id(),
        "session_id": session_id,
        "scene": scene,
        "provider": provider,
        "model": model,
        "ok": ok,
        "usage": usage,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "error": error,
    }
    try:
        path = repo_plans_dir() / plan_id / "llm_sessions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _registry_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except OSError:
        return False


def read_llm_calls(plan_id: str) -> list:
    """Read back the registry for one plan ([] when absent/corrupt)."""
    path = repo_plans_dir() / plan_id / "llm_sessions.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a torn tail line from a crash
    return entries
