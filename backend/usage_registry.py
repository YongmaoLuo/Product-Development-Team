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
  failures must never surface as workflow failures. A sink that fails
  is *counted* (:func:`sink_failure_count`), because a registry that
  silently stopped writing would look exactly like a plan that made
  fewer calls.
* **Two attribution coordinates** (2026-10-26): ``vp_id`` (which
  verification point) and ``round`` (which round of it). They answer
  the question the plan-level split cannot — a plan's ``by_scene``
  says verification is expensive, but only these say WHICH
  verification point and WHICH round. A scope that does not name them
  inherits them from the scope it runs inside, so a per-task scope
  inside a verification round cannot erase the round it belongs to.
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

#: ``{"plan_id": Optional[str], "task_id": Optional[str],
#: "task_read_only": Optional[bool], "task_allowed_files": Optional[list],
#: "task_report_path": Optional[str], "vp_id": Optional[str],
#: "round": Optional[int]}``
#: — per-thread / per-async-task attribution context set by server
#: middleware, background thread entries, and per-task scopes in the
#: executor.
#:
#: ``task_read_only`` (2026-10-17) rides here rather than in a module
#: global because it is a property of ONE task and tasks run concurrently
#: on separate threads. It shares the context with ``task_id`` for the same
#: reason it must: the two are read at the same moment, by the same call,
#: to decide the same task's sub-agent settings — and a global would have
#: the last task to start silently re-scoping every other running task.
#:
#: ``task_allowed_files`` (2026-10-19) is that task's *declared* write
#: set, for the same single-read-at-one-moment reason.
#:
#: ``task_report_path`` (2026-10-19) is that task's report deliverable,
#: already resolved to an absolute path by the caller. It rides here for
#: the same single-read-at-one-moment reason as the other two: it is read
#: at the moment the sub-agent's ``--settings`` payload is assembled, by
#: the same call that reads ``task_read_only``, and a global would let the
#: last report task to start re-scope every other running task.
#:
#: ``vp_id`` / ``round`` (2026-10-26) are the cost-attribution
#: coordinates — which verification point, which round of it. Unlike the
#: task family they are INHERITED by a nested scope that does not name
#: them: a round is a property of the enclosing verification round, and a
#: per-task scope inside it says nothing about which round it is in.
_plan_ctx: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "pdt_plan_usage_ctx", default=None
)

#: Serializes appends from concurrent threads (verification round + repair
#: generation + phase calls can record from several threads at once).
#: A single ``threading.Lock`` is enough: writes are one line each and
#: POSIX O_APPEND makes each write atomic w.r.t. interleaving anyway; the
#: lock guards the open/close pair so a crash mid-append cannot leave a
#: torn line that a later reader chokes on.
_registry_lock = threading.Lock()

#: How many registry writes were swallowed since the process started.
#: A registry that cannot write is invisible in the DATA (the plan's
#: report simply shows fewer calls), so the count is kept where it can
#: be seen: :func:`sink_failure_count`.
_sink_failures = 0
_sink_failures_lock = threading.Lock()


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


def current_task_is_readonly() -> bool:
    """Whether the task in scope declared no concrete files to modify.

    ``False`` outside any task scope. The read-only refusal is the one
    guard rule that fails OPEN by design (see ``coding_tool``'s
    ``PDT_READONLY_TASK`` stamp): an unbound context means "no task said
    anything", which is not the same as "a task said it changes nothing",
    and the sub-agents that run without a task scope (the refiner's own
    queries, the verification agents) must keep working.
    """
    ctx = _plan_ctx.get()
    return bool((ctx or {}).get("task_read_only"))


def current_task_allowed_files() -> Optional[list[str]]:
    """The task in scope's declared write set, or ``None`` for none.

    The second half of the declaration contract (2026-10-19). A
    read-only task has ``None`` here — it keeps failing through
    ``PDT_READONLY_TASK`` — and so does everything that runs outside a
    task scope.

    ``None`` deliberately fails OPEN at the consuming guard. The
    asymmetry is the same one the read-only stamp uses and for the same
    reason: a false refusal bricks a task that was going to succeed,
    while a missed refusal is the status quo this change is meant to
    remove one layer of. So the guard refuses only when a slip was
    actually issued and this path is genuinely absent from it.
    """
    ctx = _plan_ctx.get()
    if not ctx:
        return None
    return ctx.get("task_allowed_files")


def current_task_report_path() -> Optional[str]:
    """The task in scope's report deliverable, or ``None`` for none.

    The third leg of the declaration contract (2026-10-19): a task
    whose deliverable is a report file has to be able to write exactly
    that one file even though it declares no source files and is
    therefore stamped read-only. Without this, the report task's only
    way to produce its deliverable is the Write tool, and the guard
    refuses that write — so the executor retries it until the budget
    runs out and the task dies having produced nothing.

    ``None`` for a code task and for anything outside a task scope,
    which is what keeps the carve-out from widening: the consuming
    guard only lets a write through when a path was actually issued
    for THIS task.
    """
    ctx = _plan_ctx.get()
    if not ctx:
        return None
    rp = ctx.get("task_report_path")
    if not rp or not str(rp).strip():
        return None
    return str(rp)


def current_vp_id() -> Optional[str]:
    """The verification point this call belongs to, or ``None``.

    ``None`` outside any scope that names one — which is what makes an
    entry unattributable rather than misattributed: a call with no
    verification point behind it must not borrow somebody else's.
    """
    ctx = _plan_ctx.get()
    return (ctx or {}).get("vp_id")


def current_round() -> Optional[int]:
    """The round this call belongs to, or ``None``.

    A round is a NUMBER, so the value is normalised: an ``int`` is
    taken as it is, a numeric string is read as the number it spells,
    and anything else is "no round" rather than a round nobody could
    order or compare.
    """
    ctx = _plan_ctx.get()
    value = (ctx or {}).get("round")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def sink_failure_count() -> int:
    """How many registry writes have been swallowed since process start.

    The registry is a side channel: a sink that fails (a full disk, a
    read-only tree, a resolver that raises) must never break the
    dispatch it was recording. But a silently-blind registry is worse
    than a loud one — every token it failed to record looks exactly
    like a token nobody spent — so the failures are counted, and this
    counter is how they stay visible.
    """
    with _sink_failures_lock:
        return _sink_failures


def _count_sink_failure() -> None:
    global _sink_failures
    with _sink_failures_lock:
        _sink_failures += 1


def _sink_append(path: Path, line: str) -> None:
    """The registry's write sink: append one JSON line to ``path``.

    Split out of :func:`record_llm_call` so the failure path has ONE
    name (and so a test can make the sink fail without touching the
    filesystem). Everything it does is best-effort by contract: the
    caller swallows the failure and counts it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _registry_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


@contextmanager
def plan_usage_context(
    plan_id: Optional[str] = None,
    task_id: Optional[str] = None,
    task_read_only: Optional[bool] = None,
    task_allowed_files: Optional[list[str]] = None,
    task_report_path: Optional[str] = None,
    vp_id: Optional[str] = None,
    round: Optional[int] = None,
) -> Iterator[None]:
    """Bind LLM calls in this scope to ``plan_id`` / ``task_id``.

    Passing ``plan_id=None`` (e.g. only task attribution is known) keeps
    the env ``PDT_PLAN_ID`` fallback alive for plan resolution.

    ``task_read_only`` is the task's file-declaration verdict, computed by
    the caller from the task it is about to run (see
    ``agent._declared_modification_files``). Defaulting to ``None`` means
    "unknown", which :func:`current_task_is_readonly` reads as not
    read-only — so every pre-existing caller of this helper keeps its old
    behaviour without having to be edited.

    ``task_allowed_files`` is the same declaration when it IS a concrete
    list of paths, i.e. the task's permission slip as well as its claim
    (2026-10-19). ``None`` means "no slip issued" and
    :func:`current_task_allowed_files` propagates that, so every caller
    outside a task scope — the refiner's own queries, the verification
    agents — is unaffected.

    ``task_report_path`` (2026-10-19) is the ONE file a report-task is
    still permitted to write, for the same reason as the other two: it
    is a property of the task being dispatched, it is read at the same
    moment, and ``None`` (a code task, or no task at all) means "no
    carve-out issued", which :func:`current_task_report_path`
    propagates unchanged.

    ``vp_id`` / ``round`` (2026-10-26) are the cost-attribution
    coordinates: which verification point the call belongs to, and
    which round of it. They are INHERITED — a scope that names neither
    keeps the ones it runs inside, so the per-task scopes of a
    verification round cannot erase the round they belong to. Naming
    them in the inner scope overrides the outer value for the inner
    scope only; leaving the inner scope restores the outer value.
    """
    outer = _plan_ctx.get() or {}
    token = _plan_ctx.set({
        "plan_id": plan_id,
        "task_id": task_id,
        "task_read_only": task_read_only,
        "task_allowed_files": task_allowed_files,
        "task_report_path": task_report_path,
        "vp_id": vp_id if vp_id is not None else outer.get("vp_id"),
        "round": round if round is not None else outer.get("round"),
    })
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
    never break a dispatch. A failed write is counted
    (:func:`sink_failure_count`) rather than dropped: the dispatch
    survives, and the blind spot stays visible.

    The line carries the two attribution coordinates (``vp_id`` /
    ``round``) next to the fields that were already there; both are
    ``null`` when nothing named them.
    """
    plan_id = current_plan_id()
    if not plan_id or not session_id:
        return False
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "task_id": current_task_id(),
        "vp_id": current_vp_id(),
        "round": current_round(),
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
        line = json.dumps(entry, ensure_ascii=False, default=str)
        _sink_append(path, line)
        return True
    except Exception:
        # The registry is a side channel: a sink that fails (a full
        # disk, a read-only tree, a resolver that raises) must not
        # break the dispatch it was recording. Counted, not hidden.
        _count_sink_failure()
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
