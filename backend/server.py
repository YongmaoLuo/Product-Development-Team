"""
FastAPI Web Server for Spec-Driven Development System
"""

import json
import logging
import os
import os as _os  # module-level alias: _signal_handler (line ~3549) uses
                  # _os.getpid(); the previous function-local import left
                  # the handler with a NameError that crashed the whole
                  # server on every SIGTERM.
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import atexit
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Sequence, Tuple, Any, Union

import anyio
import yaml

logger = logging.getLogger(__name__)

# 2026-09-07: configure root logger so logger.info/warning/error calls
# actually reach server.log. The previous code only relied on
# ``print()`` for operational logs (which uvicorn captures because
# it tees stdout) — but ``logger.info(...)` writes to whatever handlers
# the root logger has, and no basicConfig had ever been issued, so
# calls like ``logger.warning(...)` silently no-op'd against the
# stderr-less uvicorn worker. Set the level from env so operators
# can flip DEBUG on for the verification watchdog without a code
# change, and reuse uvicorn's loggers (uvicorn.error etc.) so server.log
# line format matches what uvicorn itself emits.
_LOG_LEVEL = os.environ.get("PDT_LOG_LEVEL", "INFO").upper()
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
# Idempotent: uvicorn imports ``server`` twice under some reload
# configurations (once via main loader, once via worker) and each
# import would otherwise append a duplicate handler producing
# double-logged lines. Track handlers we've added by attaching a
# sentinel attribute so we don't double-install.
_root = logging.getLogger()
if not getattr(_root, "_ac_handler_installed", False):
    _root.addHandler(_handler)
    _root._ac_handler_installed = True
_root.setLevel(_LOG_LEVEL)
logger.setLevel(_LOG_LEVEL)

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from coding_tool import ClaudeCodingTool, create_coding_tool
from usage_registry import plan_usage_context
import plan_usage
import request_guard
import re

from framework.ids import InvalidPlanIdError, derive_plan_id, validate_plan_id

from cc_switch import (
    get_provider,
    list_provider_names,
    CCSwitchError,
    ProviderConfig,
)
from interviewer import Interviewer
from prd_generator import PRDGenerator
from prd_review import PRDReviewer
from prd_refiner import PRDRefiner
from plan_state import PlanState
from arch_generator import ArchGenerator, ArchHardGateError
from self_review import SelfReviewUnavailableError
from arch_reviewer import ArchReviewer
from arch_refiner import ArchRefiner
from test_design_generator import TestDesignGenerator
from test_design_reviewer import TestDesignReviewer
from test_design_refiner import TestDesignRefiner
from decision_point_adder import (
    ArchDecisionPointAdder,
    PRDDecisionPointAdder,
    TestDecisionPointAdder,
)
from tasks_generator import TasksGenerator, TasksGenerationError
from preflight_review import PreFlightReviewer
from verification import VerificationOrchestrator
from execution_logger import ExecutionLogger
from utils.atomic_io import atomic_write_json
from utils.process import kill_process_group
from sub_agent_registry import sub_agent_registry, SUB_AGENT_STALE_THRESHOLD_SEC
from provider_order import load_fallback_order, ProviderOrderError
from runtime_state import RuntimeState
from scheduling.guard import InFlightFileGuard
from dynamic_provider_concurrency import (
    ActiveConcurrencyTracker,
    set_shared_tracker,
)
from config_paths import (
    STATE_DB,
    STATE_DB_ENV,
    resolve_provider_order_file,
    resolve_server_host,
    resolve_state_db_path,
)

# ---------------------------------------------------------------------------
# service_manager (orphan-process reaping) — resolved once, at import time
# ---------------------------------------------------------------------------
# ``service_manager`` sits beside this file and is imported flat, which is the
# convention for ``backend/*.py``: it resolves only while ``backend/`` is on
# ``sys.path``. Importing it lazily *inside* the startup/shutdown paths looked
# harmless and was not: the shutdown reap runs from an ``atexit`` hook, i.e.
# after the interpreter has begun tearing down — under pytest, after the
# ``pythonpath`` plugin has removed the entries it injected — so the import
# raised ``ModuleNotFoundError``, and reporting that failure then hit a
# logger whose stream was already closed ("I/O operation on closed file").
# The reap silently did not happen, and the only trace was two error blocks
# printed after an otherwise green run.
#
# Resolving it here instead makes the reference survive any later teardown.
# ``None`` means the module is missing: the sweeps become no-ops that say so
# once at startup, rather than failing at the worst possible moment.
try:  # pragma: no cover - the repo always ships service_manager.py
    from service_manager import reap_all_plans as _reap_all_plans
except Exception:  # noqa: BLE001 - a missing module must not break startup
    _reap_all_plans = None

try:  # pragma: no cover - the repo always ships service_manager.py
    from service_manager import reap_services as _reap_services
except Exception:  # noqa: BLE001
    _reap_services = None

# ---------------------------------------------------------------------------
# secret_sweep (credential-residue reaping) — resolved once, at import time
# ---------------------------------------------------------------------------
# Same reasoning as the service_manager imports above, one level down: a
# dispatch's ``--settings`` payload carries the routed provider's
# ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``, and it is redacted as
# soon as the child is reaped (``coding_tool``, both shutdown points). A
# backend that is killed, crashes, or is restarted mid-dispatch never
# reaches that cleanup, so the file keeps a live credential for as long as
# it sits in the temp root. This is the sweep that collects it.
try:  # pragma: no cover - the repo always ships utils/secret_sweep.py
    from utils.secret_sweep import sweep_default_roots as _sweep_credential_residue
except Exception:  # noqa: BLE001 - a missing module must not break startup
    _sweep_credential_residue = None

# 2026-09-13: optional env override (PDT_PLANS_DIR) so test / perf
# harnesses can point the server at an empty scratch plans root.
# Without this, the startup-perf comparison measures the working
# tree's real plans/ directory (hundreds of plan dirs to recover)
# against an empty git-archive baseline — an apples-to-oranges gap
# that has nothing to do with code regressions.
_PLANS_DIR_OVERRIDE = _os.environ.get("PDT_PLANS_DIR")
PLANS_DIR = (
    Path(_PLANS_DIR_OVERRIDE)
    if _PLANS_DIR_OVERRIDE
    else Path(__file__).parent.parent / "plans"
)
PLANS_DIR.mkdir(parents=True, exist_ok=True)


def _validated_plan_id(plan_id: str) -> str:
    """Return ``plan_id`` unchanged, or raise if it is not a safe leaf.

    The check on its own, for handlers that take a ``plan_id`` but never
    build a filesystem path from it (``/api/execution/{id}/status``
    answers from the in-memory execution map;
    ``/api/plan/{id}/state-from-db`` queries by it as a column value).

    They are not traversal risks today. They are here so the contract is
    uniform: a malformed id is rejected by every route that accepts one,
    rather than by 21 of 23 of them — an API where two endpoints answer
    "sure, here you go" for a value the other twenty-one call invalid is
    one where the next person adding a file read to one of those two has
    no reason to think a check is missing.

    Raises:
        HTTPException: 400 when the id is empty, absolute, or contains a
            path separator, a traversal component, a control byte, or
            any byte outside ``[A-Za-z0-9._-]``.
    """
    try:
        return validate_plan_id(plan_id)
    except InvalidPlanIdError as exc:
        raise HTTPException(
            400,
            {"error": "Invalid plan id", "detail": str(exc)},
        )


def _plan_dir(plan_id: str) -> Path:
    """Resolve a request-supplied ``plan_id`` to its directory.

    **Every** ``plans/{plan_id}`` access funnels through here. It used to
    be a bare ``PLANS_DIR / plan_id`` at 73 call sites, which meant the
    route's path parameter was joined onto the plans root unchecked.

    ``{plan_id}`` cannot smuggle a literal ``/`` (the router splits on
    it and the route simply does not match), but it *can* be ``..``: the
    segment reaches the handler intact, and ``PLANS_DIR / ".."`` is the
    repository root. ``GET /api/plan/../status`` and
    ``/api/plan/../summary`` are both answered 200 with
    ``plan_id: ".."``. The read routes reported the checkout as if it
    were a plan; the write routes would have dropped ``prd.json`` /
    ``plan_state.json`` beside the source tree, outside the one
    directory the design says plans live in.

    :func:`framework.ids.validate_plan_id` already existed for exactly
    this, and its docstring already claimed that *"every place that
    touches ``plans/{plan_id}`` MUST funnel through it"* — but no HTTP
    handler called it. The guard was wired into the watchdog and the
    signal writer and never into the API. This function is that missing
    wire.

    A malformed id is reported as 400 rather than 404: it is a bad
    request, not a missing plan, and telling the two apart is what lets
    a caller fix their URL instead of hunting for a plan that never
    existed.
    """
    return PLANS_DIR / _validated_plan_id(plan_id)


def _is_renderable_plan_id(plan_id: object) -> bool:
    """True when a *stored* plan id passes the same validator the routes use.

    ``plan_routing`` can hold ids that predate the traversal guard — one
    was written while ``..`` was still a live plan id (the id validates
    on no write path at all; ``RoutingRepository.insert`` takes the row
    as-is) and such a row shipped in a real ``state.db``. Consumers that
    iterate every stored row (the plans listing below, the notifier's
    plan-status sweep) must skip those rows instead of raising: one
    legacy row must not take down every consumer of the table.

    URL handlers that take a single id keep rejecting it with 400 via
    :func:`_plan_dir` / :func:`_validated_plan_id` — that contract is
    unchanged; this helper exists only for full-table renderings.
    """
    try:
        validate_plan_id(plan_id)
        return True
    except InvalidPlanIdError:
        return False

# Project root, resolved once at import time. Used to anchor paths
# declared in backend/config.yaml as project-relative (e.g. the
# ``provider_order_file`` field) without depending on the current
# working directory the server happens to launch from.
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
#: ``backend/`` itself — the directory this application's code lives in.
#: Defined here, and only here: a module extracted out of this file cannot
#: derive it from its own ``__file__`` any more (``routes/execution.py``
#: would compute ``backend/routes/``), which is exactly the class of bug
#: ``tests/static_gates/test_extracted_modules_use_app_root.py`` pins.
_BACKEND_DIR = Path(__file__).parent.resolve()

# Load project-root .env into os.environ at server-startup time.
#
# 2026-09-06 fix: backend/notifications/feishu_notifier.py is event-driven
# (subscribes to KIND_PLAN_PHASE_CHANGED / KIND_VP_STATE_CHANGED /
# KIND_PLAN_CLOSED via the in-process state-event bus) and replaces the
# legacy polling bridge (tools/main.py). It enables itself only when
# FEISHU_APP_ID and FEISHU_APP_SECRET are present in the environment.
# Previously the server relied on its parent shell having those exports,
# which silently failed when launched via the external process supervisor (parent
# launched by launchd with no FEISHU_APP_*), causing every event-driven
# push to be skipped and the legacy polling bridge to do the work alone.
#
# Loading the dotenv here mirrors what tools/main.py already does on its
# own startup (``load_dotenv()`` at tools/main.py:1891), so both processes
# get consistent credentials from the single source of truth
# (project-root .env).
try:
    from dotenv import load_dotenv

    _DOTENV_PATH = _PROJECT_ROOT / ".env"
    if _DOTENV_PATH.exists():
        load_dotenv(_DOTENV_PATH, override=False)
except ImportError:  # dotenv not installed — fall back to caller-provided env
    pass

HEARTBEAT_INTERVAL = 30.0

# 2026-09-06:
# If a verification log/results file has not been touched for this many
# seconds, the lazy watchdog treats the orchestrator as stuck (its
# background thread may still report ``is_alive()`` but be deadlocked
# waiting on a subprocess, lock, or network call) and forces the plan
# out of ``verification_running`` into ``failed``. Default 900s
# (15min) — comfortably longer than a typical verification round's
# longest single VP run, so a slower-than-usual round is not mistaken
# for a stall, yet short enough that a 12-hour-stuck plan is
# auto-recovered within
# ~15 min of backend startup. Override via env for testing.
VERIFICATION_WATCHDOG_STALENESS_SECONDS = float(
    os.environ.get("VERIFICATION_WATCHDOG_STALENESS_SECONDS", "900")
)

# 2026-09-14 分诊式看门狗: hard ceiling for declaring a
# verification VP TRULY dead. When the newest round log has a running VP
# (vp_start without vp_complete), a stale round log is NOT enough to
# conclude death — a full-suite automated_test VP legitimately runs
# 18+ min while its tee'd /tmp progress file keeps growing (VP-023
# was falsely flagged ``verification_log_stale`` while its pytest was
# healthy). Triage:
#
#   * fresh liveness signal (a live /tmp-matching process, a fresh tee
#     artifact, a registry handle with progress) → suppress the flag;
#   * no fresh signal but staleness age < this hard cap → hold off
#     (the VP may simply not follow the tee naming convention);
#   * no fresh signal AND age >= hard cap → truly dead: kill the
#     registered sub-agent processes AND the orphaned background
#     pytest/bash (disowned PPID-1 children the registry cannot see),
#     then flag failed as before.
#
# A verification with NO running VP in the newest round log keeps the
# original staleness semantics — there is nothing in flight to be
# patient for.
#
# IMPORTANT — this cap must EXCEED ``VERIFICATION_WATCHDOG_STALENESS_SECONDS``,
# otherwise the "hold" branch is dead code and the first stale trip
# becomes an immediate kill+cleanup. That is exactly what happened on
# first deploy: ``.env`` sets the staleness threshold to 3600s while an
# absolute 2700s default made every trip terminal at once. The default
# is therefore DERIVED (threshold + 30 min of patience) rather than
# absolute; ``VERIFICATION_HARD_STALE_SECONDS`` can still override it.
VERIFICATION_HARD_STALE_SECONDS = float(
    os.environ.get(
        "VERIFICATION_HARD_STALE_SECONDS",
        str(VERIFICATION_WATCHDOG_STALENESS_SECONDS + 1800.0),
    )
)

# Stop reasons emitted by ``_lazy_check_verification`` (the verification
# watchdog). Every one of them means "the verification chain is over" —
# the orchestrator thread died, or it stopped writing progress for longer
# than ``VERIFICATION_WATCHDOG_STALENESS_SECONDS``. They belong in
# ``VERIFICATION_TERMINAL_STOP_REASONS`` below so
# ``_persist_verification_terminal`` CASes ``plan_routing.stage`` out of
# ``verification_*`` and mirrors ``current_phase``.
#
# 2026-09-13: these three were missing from the whitelist,
# so a watchdog-terminated plan kept ``stage='verification_running'`` and
# ``current_phase='verification_running'`` while
# ``plan_verification.verification_status='failed'`` — the state-machine
# split-brain the operator saw as "飞书卡片说验证失败，但本地状态还在跑".
VERIFICATION_WATCHDOG_STOP_REASONS = frozenset(
    {
        "verification_thread_died_unexpectedly",
        "verification_log_stale",
        "verification_results_stale",
    }
)

# ``stop_reason`` values that mean "the verification loop converged on a
# failure set it cannot escape" — the orchestrator's same-failure detector
# gave up. These verdicts are terminal in a stronger sense than the rest
# of the vocabulary: they do NOT mean "go run whatever execution work is
# left", they mean the loop is over.
#
# 2026-09-20 (post-mortem): this set exists because two spellings of
# one verdict drifted apart. ``check_cycle_conditions`` emits
# ``same_failure_repeated_after_max_attempts`` since the
# consecutive-rounds counter landed on 2026-09-12, but the auto-loop's
# guard still compared against the bare ``same_failure_repeated`` it used
# to emit. The guard therefore never fired: the round fell through to the
# empty-repair-queue exit, which routes to ``executing`` — an edge that
# does not exist from ``verification_loop_stopped`` — so
# ``confirm_repair_and_rerun`` raised, the blanket ``except`` swallowed it,
# and another full round started. Net effect: the convergence detector
# could not stop the chain at all (only the round budget could), and the
# recorded ``same_failure_repeated_after_max_attempts`` was overwritten by
# ``max_rounds_reached``.
#
# The auto-loop now keys its guard off the orchestrator's
# ``status == "loop_stopped"`` instead, which cannot drift from the reason
# because both come from one return literal. This set remains for callers
# that only ever receive a ``stop_reason`` string.
VERIFICATION_CONVERGENCE_STOP_REASONS = frozenset(
    {
        "same_failure_repeated",
        "same_failure_repeated_after_max_attempts",
    }
)

# ``stop_reason`` values for which a terminal verification write really
# ends the chain (as opposed to an interim ``_record_terminal`` call made
# while the executor subprocess is still mid-flight). See the guard in
# ``_persist_verification_terminal``.
#
# 2026-09-13 audit: this used to be an inline literal inside step 2, which
# is exactly why the watchdog reasons could be forgotten. Hoisted to a
# module constant so the watchdog set is unioned in by construction and so
# tests can pin the membership. The convergence set is unioned in for the
# same reason — the 2026-09-20 post-mortem above is what its absence cost.
VERIFICATION_TERMINAL_STOP_REASONS = frozenset(
    {
        "max_rounds_reached",
        "user_stopped",
        "user_force_terminal",
        "repair_execution_failed",
        "exception",
    }
) | VERIFICATION_WATCHDOG_STOP_REASONS | VERIFICATION_CONVERGENCE_STOP_REASONS


# ---------------------------------------------------------------------------
# /api/plans response cache
# ---------------------------------------------------------------------------
#
# ``GET /api/plans`` reads the plans directory, the state-machine SQLite
# DB, and every per-plan interview.json / tasks.json / etc. file. When
# the UI polls the sidebar every few seconds (and several ai-agent
# callers do the same in lock-step) the per-call fan-out becomes the
# dominant cost of the route. The cache below keeps a TTL-bounded copy
# of the rendered list keyed on the ``include_terminal`` query
# parameter so the second (and third, and fourth) poll within the TTL
# returns the same payload without re-reading anything from disk.
#
# Invalidation model
# ------------------
# Two complementary mechanisms keep the cache from going stale:
#
# 1. TTL — the cache entry expires after ``_PLANS_CACHE_TTL_SECONDS``
#    even if no explicit invalidation fires, so a runaway stale read
#    self-heals within a small bounded delay.
# 2. Explicit invalidation — :func:`invalidate_plans_cache` is the
#    single point mutation endpoints call to drop the cached snapshot
#    the moment they know the listing is stale (e.g. execution start /
#    stop, verification start / stop, interview answer, PRD / arch /
#    test generation, tasks generation).
#
# The cache lives at module scope (not inside the route function) so the
# lookup is a single dict read and so the invalidation function can be
# called from anywhere — including tests — without threading the cache
# object through the call stack.
#
# Escape hatch
# ------------
# ``PLANS_DIR`` is a module global that tests and the perf harness swap
# per test (``PDT_PLANS_DIR`` / ``monkeypatch.setattr("server.PLANS_DIR",
# …)``). The cache is keyed on the *resolved root as well as* the query
# parameter, so a caller that moves the root gets a miss instead of a
# snapshot of the previous root's listing. Without the root in the key,
# the first test in a module could cache an empty list and every later
# test in the same 2-second window would be served it — the 2026-09-14
# ``plans_list x archived_plan_read`` failure, where ``/api/plans``
# answered ``[]`` for a plan that existed on disk.

_PLANS_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_PLANS_CACHE_TTL_SECONDS: float = 2.0


def _plans_cache_key(include_terminal: bool) -> str:
    """Cache key: the query parameter *and* the plans root in effect."""
    return f"{bool(include_terminal)}::{PLANS_DIR}"


def _read_plans_cache(include_terminal: bool) -> Optional[List[Dict[str, Any]]]:
    """Return the cached ``/api/plans`` payload if it is fresh, else ``None``."""
    key = _plans_cache_key(include_terminal)
    entry = _PLANS_CACHE.get(key)
    if entry is None:
        return None
    ts, result = entry
    if (time.monotonic() - ts) > _PLANS_CACHE_TTL_SECONDS:
        return None
    return result


def _write_plans_cache(
    include_terminal: bool, result: List[Dict[str, Any]]
) -> None:
    """Store the rendered ``/api/plans`` payload under the query + root key."""
    _PLANS_CACHE[_plans_cache_key(include_terminal)] = (time.monotonic(), result)


def invalidate_plans_cache() -> None:
    """Clear every cached ``/api/plans`` payload.

    Mutation endpoints (``start_execution`` / ``stop_execution`` /
    ``start_verification`` / ``stop_verification`` /
    ``interview/answer`` / ``prd/generate`` / ``tasks/generate`` / …)
    call this the moment they know the listing is stale, so the next
    ``GET /api/plans`` request re-reads from disk instead of returning
    a snapshot from before the mutation.
    """
    _PLANS_CACHE.clear()


# ---------------------------------------------------------------------------
# /api/execution/{plan_id}/files response cache
# ---------------------------------------------------------------------------
#
# ``GET /api/execution/{plan_id}/files`` walks the whole project tree
# with ``Path.rglob('*')`` and ``stat()``s every file it keeps. On a real
# project that walk — not the JSON rendering — is the dominant cost of
# the route, and the same UI / ai-agent pollers that hammer
# ``/api/plans`` poll this route too. The cache below keeps a
# TTL-bounded copy of the rendered payload so the second (and third, and
# fourth) poll within the TTL is a single dict read instead of another
# full-tree scan.
#
# The cache key is the resolved ``project_dir``, not the plan id: the
# payload is a pure function of the directory contents, so two plans
# pointing at the same directory can share one entry, and a plan whose
# project_dir is re-pointed lands on a different key instead of reading
# the previous directory's listing.
#
# Invalidation model — TTL-first. Unlike the plans listing, no endpoint
# knows when the files change: the executor subprocess writes into the
# tree out-of-band, so there is nothing to hang an explicit invalidation
# off. The TTL is therefore the primary staleness bound and is kept
# short. :func:`invalidate_files_cache` is exposed for tests and for any
# future caller that *does* know the listing is stale.

_FILES_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_FILES_CACHE_TTL_SECONDS: float = 5.0

# Hot-path optimization B-02: hoisted to module scope so the set is
# allocated once at import time instead of every cache-miss walk.
# Frozen so callers can't accidentally mutate it during a rglob walk.
_FILES_EXCLUDE_DIRS: frozenset = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "venv1",
    "build", "dist", ".next", "target", "logs", "reports", ".tox",
    "egg-info",
})


def _read_files_cache(project_dir: Path) -> Optional[Dict[str, Any]]:
    """Return the cached files payload for ``project_dir`` if fresh, else ``None``."""
    entry = _FILES_CACHE.get(str(project_dir))
    if entry is None:
        return None
    ts, result = entry
    if (time.monotonic() - ts) > _FILES_CACHE_TTL_SECONDS:
        return None
    return result


def _write_files_cache(project_dir: Path, result: Dict[str, Any]) -> None:
    """Store the rendered files payload under the ``project_dir`` key."""
    _FILES_CACHE[str(project_dir)] = (time.monotonic(), result)


def invalidate_files_cache() -> None:
    """Clear every cached ``/api/execution/{plan_id}/files`` payload.

    The next request for any plan re-walks its project directory instead
    of returning a snapshot from before the mutation.
    """
    _FILES_CACHE.clear()


# ---------------------------------------------------------------------------
# provider_order_file resolution
# ---------------------------------------------------------------------------
#
# An external provider-order producer writes a contract file that the
# backend reads when starting a sub-agent to determine the fallback
# chain. The path is declared in ``backend/config.yaml`` under
# ``provider_order_file`` and may be overridden at runtime by setting
# ``PROVIDER_ORDER_FILE``. With neither set the chain is simply empty —
# nothing has to exist for the server to run.
#
# The precedence lives in :func:`config_paths.resolve_provider_order_file`;
# this module only takes the import-time snapshot below.


def _load_backend_config_yaml() -> dict:
    """Read ``backend/config.yaml`` and return it as a plain dict.

    Returns an empty dict when the file is missing or malformed so
    that callers can rely on ``.get(...)`` without try/except noise.
    Backend startup must not fail just because this ancillary config
    file is absent — the hard-coded default still keeps the server
    runnable.
    """
    yaml_path = Path(__file__).parent / "config.yaml"
    if not yaml_path.exists():
        return {}
    try:
        with open(yaml_path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_provider_order_file() -> Path:
    """Return the absolute path to ``provider-order.json``.

    Delegates to :func:`config_paths.resolve_provider_order_file`, which
    owns the precedence: ``PROVIDER_ORDER_FILE`` → ``provider_order_file``
    in ``backend/config.yaml`` → ``<config dir>/provider-order.json``.

    This module used to carry a second, hand-copied resolution of that
    same precedence. The copies drifted, and the one here named a
    directory the repository does not ship — so a default that no clone
    could satisfy. Kept as a named function because the suite and the
    lifespan both refer to it; the logic is not duplicated any more.
    """
    return resolve_provider_order_file()


PROVIDER_ORDER_FILE: Path = _resolve_provider_order_file()


# Provider startup: drive both the chain and per-provider config from the
# consumer layer (``provider_order`` + ``cc_switch``). There
# are no hard-coded provider lists in this module — every lookup keys on
# the provider name as CC Switch spells it.
#
# ``_resolve_max_parallel`` consults the live ``PROVIDER_LIMITS`` map,
# which is itself populated from the consumer layer at module import time
# (see ``_get_provider_limits`` further down).
DEFAULT_VP_CONCURRENCY_CAP = 5

#: Default verification-round budget for a plan that did not pass one
#: explicitly (``POST /api/verification/{id}/start`` with no body).
#:
#: 2026-09-19: 3 → 4. The same-failure convergence
#: detector now stops a loop that repeats an identical failure set two
#: rounds running (``VerificationOrchestrator`` :: same-failure
#: threshold — two identical failure sets stop the loop), so the cap no longer has to double as
#: the primary convergence mechanism. Raising it to 4 gives a plan
#: whose failure set is *shrinking* one more repair round to close
#: out, while a plan going in circles is still cut at round 2.
#:
#: This is only the *default*; an individual plan's budget is frozen
#: in ``plan_verification.max_rounds`` at start time and is not
#: retroactively changed by editing this constant.
DEFAULT_MAX_VERIFICATION_ROUNDS = 4


def _resolve_max_parallel(coding_tool) -> int:
    """Return the VP concurrency cap based on the plan's primary provider.

    Resolution order:
      1. ``coding_tool.provider_priority[0]`` — the first provider in
         the priority list is treated as the primary provider for the
         plan.
      2. Look up that provider's cap in ``PROVIDER_LIMITS`` (the live
         per-provider cap table populated by the consumer layer).
      3. Fall back to :data:`DEFAULT_VP_CONCURRENCY_CAP` (5) when the
         list is empty, the primary has no cap, or the consumer layer
         returned no usable limits.

    The provider name is used verbatim as the lookup key. It used to be
    lower-cased, which was correct only while keys were kebab-case ids:
    ``"Vendor A Pro"`` lower-cases to ``"vendor-a-pro"`` and could
    never match, so every cap lookup silently missed.
    """
    priority = getattr(coding_tool, "provider_priority", None)
    if not (isinstance(priority, list) and priority):
        return DEFAULT_VP_CONCURRENCY_CAP
    primary = str(priority[0]).strip()
    if not primary:
        return DEFAULT_VP_CONCURRENCY_CAP
    cap = PROVIDER_LIMITS.get(primary)
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return cap
    return DEFAULT_VP_CONCURRENCY_CAP


# Module-level cache of the startup provider chain, populated by
# :func:`load_providers`. Tests and helpers can read ``STARTUP_PROVIDERS``
# after lifespan startup to observe the resolved chain.
STARTUP_PROVIDERS: List[ProviderConfig] = []


def load_providers() -> List[ProviderConfig]:
    """Initialise the provider chain at startup via the consumer layer.

    The chain comes from the optimizer contract file via
    :func:`provider_order.load_fallback_order`. Each entry in the chain
    is a CC Switch **display name** (post-5561e2c contract — the
    upstream ``provider-order.json`` carries display names verbatim),
    so resolution goes through
    :func:`cc_switch.get_provider`. The
    resolved configs are stored in :data:`STARTUP_PROVIDERS` for
    inspection.

    Boundary conditions:

    * **Optimizer file missing/invalid** — propagates
      :class:`provider_order.ProviderOrderError`. Startup MUST fail
      explicitly; there is no silent YAML ``provider_priority`` fallback.
    * **DB unreadable** — propagates
      :class:`cc_switch.CCSwitchError`. Startup
      MUST fail explicitly rather than silently degrading to an empty
      provider set.
    * **Empty chain** — logs a warning and returns ``[]`` so the rest of
      the process can still come up (explicit degradation).
    * **Unknown display name in DB** — the entry is skipped and a
      warning is logged, so an optimiser/DB drift no longer crashes
      startup.

    Returns:
        The list of resolved :class:`~cc_switch.ProviderConfig` objects,
        in chain order.

    Raises:
        ProviderOrderError: when the optimizer contract file is missing
            or fails schema validation.
        CCSwitchError: when the CC Switch database is missing or
            unreadable.
    """
    global STARTUP_PROVIDERS

    # Inherit mode: CC Switch is not installed, so there is no fleet to
    # resolve and nothing to fall back between. Logged at INFO rather
    # than WARNING because this is a *supported configuration*, not a
    # misconfiguration — the operator has nothing to fix, and a warning
    # here would send them looking for a problem that does not exist.
    # Sub-agents resolve provider configuration from Claude Code's own
    # settings; see ``ClaudeCodingTool._detect_inherit_mode``.
    if ClaudeCodingTool._detect_inherit_mode():
        logger.info(
            "Provider inherit mode: no CC Switch database found. Sub-agents "
            "will use Claude Code's own configuration "
            "(~/.claude/settings.json and the ambient env); no provider "
            "routing, ordering or failover is active."
        )
        STARTUP_PROVIDERS = []
        return STARTUP_PROVIDERS

    try:
        chain = load_fallback_order()
    except ProviderOrderError:
        # A fresh checkout has no optimizer, so the contract file is
        # simply absent — that is a supported state, not a
        # misconfiguration. A file that EXISTS but fails to load (bad
        # schema, unreadable, wrong shape) is a real problem and must
        # still stop startup, so the two are separated here rather
        # than both being swallowed.
        contract = resolve_provider_order_file()
        if contract.exists():
            raise
        logger.warning(
            "No provider-order contract file at %s — starting with no "
            "provider fallback chain (single-provider mode). Point "
            "PROVIDER_ORDER_FILE, or backend/config.yaml:"
            "provider_order_file, at a producer to enable failover.",
            contract,
        )
        STARTUP_PROVIDERS = []
        return STARTUP_PROVIDERS
    except Exception as exc:
        raise ProviderOrderError(
            f"failed to load provider fallback order: {exc}"
        ) from exc

    if not chain:
        logger.warning(
            "Provider fallback chain is empty; server starting with no providers"
        )
        STARTUP_PROVIDERS = []
        return STARTUP_PROVIDERS

    resolved: List[ProviderConfig] = []
    for name in chain:
        try:
            cfg = get_provider(name)
        except CCSwitchError:
            # Explicit failure: the DB is unreadable, so we cannot trust
            # any provider decision. Let startup fail.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Skipping provider %s: lookup error: %s", name, exc)
            continue
        if cfg is None:
            logger.warning("Skipping unknown provider name: %s", name)
            continue
        resolved.append(cfg)

    STARTUP_PROVIDERS = resolved
    logger.info(
        "Loaded %d provider(s) from consumer layer: %s",
        len(resolved),
        [cfg.name for cfg in resolved],
    )
    return STARTUP_PROVIDERS


class HeartbeatMonitor:
    """Daemon-thread monitor that periodically reconciles in-memory state
    with actual OS / thread liveness.

    On each tick, scans every plan whose status is 'running' and probes:

    - For **execution** plans: the recorded subprocess PID via ``os.kill(pid, 0)``.
      Dead processes are transitioned to 'failed' with stop_reason
      'process_died_unexpectedly' (and plan_state synced unless already terminal).

    - For **verification** plans: the background ``threading.Thread`` (via
      ``thread.is_alive()``). A dead verification thread but a still-"running"
      in-memory status means the daemon thread exited without writing its
      terminal status — mark as 'failed' with stop_reason
      'verification_thread_died_unexpectedly' so subsequent
      ``/api/verification/{id}/status`` calls return the truth.

    Both checks are idempotent: if status is not 'running' the check is a
    no-op, and only the in-memory state is touched (plan_state transition
    is best-effort and skipped if already terminal).
    """

    def __init__(self, interval: float = HEARTBEAT_INTERVAL):
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="HeartbeatMonitor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_once()
            except Exception:
                pass
            if self._stop_event.wait(self.interval):
                break

    def _check_once(self) -> None:
        # 2026-09-06: stamp the watchdog sweep before any early-exit so
        # ``/api/debug/verification_watchdog`` shows a recent
        # ``last_sweep_at`` even on an idle server (proves the
        # heartbeat thread is alive).
        #
        # 2026-09-18: ``last_sweep_count`` counts plans that are
        # actually *in flight*, not ``len()`` of the three registries.
        # The registries also hold resting records (a finished
        # execution keeps its ``_execution_state`` entry forever), so
        # the old ``len()`` sum read 3 on a server with nothing
        # running — and the field exists precisely so an operator can
        # tell an idle server from a hung one.
        _WATCHDOG_STATS["last_sweep_at"] = time.time()
        _WATCHDOG_STATS["last_sweep_count"] = (
            sum(1 for st in _execution_state.values() if _is_execution_in_flight(st))
            + sum(1 for st in _verification_state.values() if _is_verification_in_flight(st))
            + len(sub_agent_registry._by_plan)
        )

        # 2026-09-05: Early-exit on a quiet server. When no plan is in
        # flight — i.e. _execution_state, _verification_state, and the
        # sub-agent registry are all empty — skip the per-tick lock
        # acquisitions and dict iterations entirely. Without this guard
        # a 30s tick on an idle server still acquires three registry
        # locks (one per lazy check) and walks three empty dicts, which
        # adds up across millions of ticks.
        #
        # We deliberately read ``sub_agent_registry._by_plan`` without
        # taking the registry lock: a stale "empty" snapshot is fine —
        # the worst case is one extra slow-path tick where the registry
        # becomes empty mid-call, and ``find_stale`` is already cheap
        # when there are no handles.
        if (
            not _execution_state
            and not _verification_state
            and not sub_agent_registry._by_plan
        ):
            return
        for plan_id in list(_execution_state.keys()):
            _lazy_check_execution(plan_id)
        for plan_id in list(_verification_state.keys()):
            _lazy_check_verification(plan_id)
        # 2026-08-25: third pass — sub-agent watchdog. Inspects the
        # registry of live ``VerificationSubAgent``s and kills any
        # whose ``last_progress_ts`` is older than
        # ``SUB_AGENT_STALE_THRESHOLD_SEC`` (default 1200s / 20 min).
        # The previous two passes only look at the main verification
        # thread; a sync-blocked ``ClaudeCodingTool.query_json`` call
        # keeps the main thread alive forever, so without this pass
        # a hung LLM provider hangs the whole round.
        _lazy_check_sub_agents()


heartbeat_monitor = HeartbeatMonitor(interval=HEARTBEAT_INTERVAL)




@asynccontextmanager
async def _lifespan(app):
    """FastAPI lifespan handler — replaces deprecated @app.on_event decorators.

    On startup: construct the :class:`backend.runtime_state.RuntimeState`
    dependency-injection handle (TC-006 / AC-009 / VP-024), recover
    persisted execution state, and start the heartbeat monitor.
    Subsidiary processes (operator-supplied helpers,
    etc.) are supervised by the standalone tool at
    ``the external process supervisor`` — they are intentionally NOT started here so
    a misconfigured path or crashed subsidiary cannot take down the
    API. Start the supervisor separately via
    ``python <supervisor> start``.

    On shutdown: stop the heartbeat monitor and kill any in-flight
    execution subprocesses.
    """
    # 2026-09-06: TC-006 dependency-injection refactor — the lifespan
    # is the canonical constructor for ``RuntimeState``. The four
    # orthogonal collaborators (``config``, ``provider_order_file``,
    # ``in_flight_guard``, ``dynamic_tracker``) are owned here and
    # injected onto ``app.state.runtime_state`` so request handlers
    # can read / mutate the handle without re-importing the loader
    # modules or threading through global singletons. A previous
    # revision exposed a module-level ``RUNTIME_STATE`` singleton;
    # that singleton was removed by the refactor — the only handle
    # on the live state is now ``app.state.runtime_state``.
    resolved_provider_order_file = resolve_provider_order_file()
    backend_config = _load_backend_config_yaml()
    runtime_state = RuntimeState(
        config=backend_config,
        provider_order_file=resolved_provider_order_file,
        in_flight_guard=InFlightFileGuard(),
        dynamic_tracker=ActiveConcurrencyTracker(),
    )
    app.state.runtime_state = runtime_state

    # 2026-09-18 C3: orphan sweep. A verification round that started a
    # declared service records it in the plan's ``managed_services.json``;
    # every workflow exit reaps it. A backend that was killed mid-round
    # never got that far, so the ledger survives on disk and the orphan
    # keeps holding its port. Booting is the one moment when nothing of
    # ours can legitimately be running, which makes it the safe place to
    # clean up after a previous life. Only ledger-recorded, still
    # identifiable processes are stopped — see ``service_manager``.
    if _reap_all_plans is None:
        logger.warning(
            "[service_manager] unavailable; startup orphan sweep skipped"
        )
    else:
        try:
            _swept = _reap_all_plans(PLANS_DIR, reason="startup_sweep")
            _swept_reaped = [r for r in _swept if r.reaped or not r.clean]
            if _swept_reaped:
                logger.warning(
                    "[service_manager] startup sweep: %s",
                    [r.to_dict() for r in _swept_reaped],
                )
        except Exception:
            logger.exception(
                "[service_manager] startup orphan sweep failed; continuing"
        )

    # 2026-09-28 C4: credential-residue sweep. Same boot-time argument as
    # the orphan sweep above — nothing of ours can legitimately be running
    # yet, so any settings payload still holding a live credential belongs
    # to a previous life that died before its own cleanup. The sweep
    # refuses files a running process names after ``--settings``, so a
    # second backend on this machine is not a hazard to it.
    if _sweep_credential_residue is None:
        logger.warning(
            "[secret_sweep] unavailable; startup credential sweep skipped"
        )
    else:
        try:
            _sweep_findings, _sweep_failed, _sweep_pruned = (
                _sweep_credential_residue(apply=True)
            )
            _sweep_redacted = [f for f in _sweep_findings if f.status == "redacted"]
            if _sweep_redacted:
                logger.warning(
                    "[secret_sweep] startup sweep redacted %d credential-"
                    "bearing file(s) left by a previous life",
                    len(_sweep_redacted),
                )
            if _sweep_failed:
                logger.warning(
                    "[secret_sweep] startup sweep could not redact %d file(s): %s",
                    _sweep_failed,
                    [str(f.path) for f in _sweep_findings if f.status == "failed"],
                )
            # The other half of keeping the population bounded: every
            # dispatch mints a directory, and a dispatch that failed
            # before writing leaves an empty one behind while a dispatch
            # that succeeded leaves a non-empty one (the payload is
            # redacted and kept for post-mortem). Neither is reachable by
            # the other rule, so ``prune_residue`` runs both — the
            # settle-window empty prune and the 30-day age prune.
            if _sweep_pruned:
                logger.warning(
                    "[secret_sweep] startup sweep removed %d sweep-owned "
                    "director%s (empty past the settle window, or aged past "
                    "the retention gate)",
                    len(_sweep_pruned),
                    "y" if len(_sweep_pruned) == 1 else "ies",
                )
        except Exception:
            logger.exception(
                "[secret_sweep] startup credential sweep failed; continuing"
            )

    # 2026-09-17 — publish the same tracker to the scene-routing dispatch
    # path. ``AutonomousAgent`` receives it by DI, but ``ClaudeCodingTool``
    # is constructed in ~25 places (most without app state in scope) and
    # selects its provider inside ``_run_claude_interactive``. Without
    # this registration those tools would each get their own private
    # tracker, count only their own work, and the per-provider caps would
    # not bind at all — the exact failure that let every verification VP
    # pile onto Vendor A Pro while the other two Vendor A rows idled.
    set_shared_tracker(runtime_state.dynamic_tracker)

    # 2026-09-07: log startup-side state so a post-mortem on a
    # SIGKILL'd backend can correlate against state.db by boot_id
    # (incremented on every restart) and verify whether the
    # watchdog was running when the kill hit.
    # (_os is imported at module level — the previous function-local
    # import here left _signal_handler with a NameError on SIGTERM.)
    boot_id_path = Path(_state_db_path()).parent / "pdt_server_boot_id"
    try:
        _boot_id = int(boot_id_path.read_text().strip()) + 1 if boot_id_path.exists() else 1
        boot_id_path.write_text(str(_boot_id))
    except Exception:
        _boot_id = 0
    logger.warning(
        "[server_lifecycle] startup boot_id=%d pid=%d cwd=%s "
        "verification_state_count=%d execution_state_count=%d "
        "verifying_plans=%s",
        _boot_id, _os.getpid(), _os.getcwd(),
        len(_verification_state), len(_execution_state),
        [k for k, v in _verification_state.items()
         if v.get("verification_status") == "running"],
    )

    # 2026-09-23: state-database startup guard.  On 2026-09-23 the server
    # booted onto a deeply corrupt state.db and served 500s from every
    # data endpoint for hours — reads worked (WAL overlay masked b-tree
    # damage) so nothing detected it.  The guard runs ONE rolled-back
    # write probe at boot; a database whose write path is broken is
    # quarantined (with its -wal/-shm siblings, for forensics) and the
    # newest good shutdown backup restored in its place.  Cheap on the
    # good path; never raises.
    try:
        from state_machine.db.startup_guard import startup_guard
        startup_guard(_state_db_path())
    except Exception:
        logger.exception("[startup_guard] failed; continuing boot")

    _recover_execution_states(PLANS_DIR)
    # 2026-07-19: mirror recovery for verification state — same pattern
    # as execution so cross-restart in-progress runs aren't silently
    # reset to round=0.
    _recover_verification_states(PLANS_DIR)
    # 2026-09-13: after recovery, roll stranded verification stages
    # forward.  Rows at verification_running / verification_rerunning
    # whose plan_verification row is already terminal are invisible to
    # the in-memory watchdog: the card sticks at
    # "failed" while the routing stage still says "running".
    _reconcile_orphaned_verification_stages()

    heartbeat_monitor.start()
    # Initialise the provider chain via the consumer layer. Both
    # ``provider_order.load_fallback_order`` and
    # ``cc_switch.get_provider_config`` failures must
    # abort startup rather than silently degrade to a server with no
    # provider knowledge — see :func:`load_providers`.
    try:
        load_providers()
    except (ProviderOrderError, CCSwitchError) as exc:
        logger.error("Failed to initialise providers at startup: %s", exc)
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error initialising providers at startup: %s", exc)
        raise

    # 2026-09-06: event-driven Feishu notifier. Replaces the polling
    # bridge daemon (tools/main.py) that used to hit
    # /api/plan/{id}/summary every 30 seconds. The notifier
    # subscribes to in-process state-event hooks on every
    # repository write method, so plan phase transitions, task
    # lifecycle events, and VP progress changes all push a Feishu
    # card within seconds — no polling, no symlink dependency.
    # Construction is wrapped in try/except so a broken Feishu
    # configuration (missing creds, missing lark-oapi) can NEVER
    # prevent the server from accepting requests.
    feishu_notifier = None
    try:
        from notifications.feishu_notifier import FeishuNotifier
        feishu_notifier = FeishuNotifier()
        feishu_notifier.start()
        app.state.feishu_notifier = feishu_notifier
        # 2026-09-11 plan v4: explicit warning so operators can see
        # the per-plan rate limit (60s / card, independent across
        # plans — different plans do NOT throttle each other). The
        # FeishuNotifier .start() already logs "started
        # (coalesce=..., min_interval=...)" — this line repeats the
        # value with the v4 semantics for quick eyeball verification.
        logger.warning(
            "[feishu_notifier] configured: min_interval=%.1fs "
            "(per-plan, independent across plans; "
            "1 push per card per N seconds)",
            feishu_notifier._min_interval_seconds,
        )
    except Exception:
        logger.exception(
            "[feishu_notifier] failed to start; continuing without push"
        )
        app.state.feishu_notifier = None

    try:
        yield
    finally:
        # 2026-09-07: log shutdown path *before* the work, so even
        # a SIGKILL that bypasses atexit/lifespan still leaves the
        # heartbeat monitor tick to write its own exit line.
        logger.warning(
            "[server_lifecycle] shutdown begin pid=%d "
            "verifying_plans=%s in_flight_executions=%s",
            _os.getpid(),
            # 2026-09-18: both lists are filtered by the in-flight
            # predicates. They used to be a raw ``list(_execution_state)``
            # key dump, which reported every plan the record cache had
            # ever held — terminal ones included — under a label that
            # says "in flight".
            [k for k, v in _verification_state.items()
             if _is_verification_in_flight(v)],
            [k for k, v in _execution_state.items()
             if _is_execution_in_flight(v)],
        )
        heartbeat_monitor.stop()
        if feishu_notifier is not None:
            try:
                feishu_notifier.stop(timeout=5.0)
            except Exception:
                logger.exception("[feishu_notifier] error on shutdown")
        _shutdown_all_executions()
        # 2026-09-23: leave a restorable copy behind on every clean
        # shutdown.  First checkpoint the WAL so the backup (and the
        # file at rest) is self-contained, then copy it into the backup
        # dir that startup_guard restores from.  Never raises — a
        # failed backup must never abort shutdown.
        try:
            _db_path = _state_db_path()
            if _db_path.exists():
                from state_machine.db.connection import open as _open_db
                _ck = _open_db(_db_path)
                try:
                    _ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    _ck.close()
                from state_machine.db.startup_guard import backup_now
                _backup = backup_now(_db_path)
                if _backup is not None:
                    logger.warning(
                        "[server_lifecycle] state.db shutdown backup -> %s",
                        _backup,
                    )
        except Exception:
            logger.exception("[startup_guard] shutdown backup failed")
        logger.warning(
            "[server_lifecycle] shutdown complete pid=%d", _os.getpid(),
        )


app = FastAPI(title="Spec-Driven Dev System", lifespan=_lifespan)

# Per-plan LLM-call attribution (2026-09-21). HTTP entry points that
# carry a plan_id in the path bind it into the usage-registry context
# so every in-handler LLM call (PRD / arch / test / tasks generation,
# review & refine endpoints) is attributed without touching any
# handler body. Background threads (executor watcher, repair async,
# verification worker) set the context at their own entry; the
# executor *subprocess* uses the PDT_PLAN_ID env var instead.
_PLAN_PATH_RE = re.compile(
    r"^/api/(?:interview|prd|arch|test|review|tasks|plan|execution|verification)/(?P<pid>[^/?#]+)"
)


@app.middleware("http")
async def _plan_usage_context_middleware(request, call_next):
    match = _PLAN_PATH_RE.match(request.url.path)
    if match:
        with plan_usage_context(plan_id=match.group("pid")):
            return await call_next(request)
    return await call_next(request)


# Refuse cross-origin and rebound-host calls to the API. Loopback is not
# a boundary a browser respects — see ``request_guard`` for the threat
# model. Registered *after* the middleware above on purpose: Starlette
# inserts each ``@app.middleware`` at the head of the user stack, so the
# one defined last runs first and this rejection short-circuits before
# any handler-side context is bound.
@app.middleware("http")
async def _request_guard_middleware(request, call_next):
    reason = request_guard.rejection_reason(request)
    if reason is not None:
        return JSONResponse(
            status_code=403,
            content={"error": "forbidden", "detail": reason},
        )
    return await call_next(request)


def _run_in_plan_ctx(plan_id: str, fn) -> None:
    """Thread entry wrapper: bind the usage-registry plan context."""
    with plan_usage_context(plan_id=plan_id):
        fn()


def _usage_report_path(plan_id: str) -> Path:
    # Resolved by plan_usage so the reader and the writer can never
    # disagree about where the report lives.
    return plan_usage.usage_report_path(plan_id)


def _read_cached_usage_report(plan_id: str) -> Optional[dict]:
    """Read the persisted usage report, or ``None`` when absent/corrupt."""
    try:
        with open(_usage_report_path(plan_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _usage_summary_block(plan_id: str) -> Optional[dict]:
    """Compact usage view for ``/api/plan/{id}/summary`` (cached only).

    Polling ``/summary`` must stay cheap, so this never aggregates —
    it forwards whatever the last refresh persisted, or ``None``.
    """
    report = _read_cached_usage_report(plan_id)
    if not report:
        return None
    cross_check = report.get("cross_check") or {}
    return {
        "generated_at": report.get("generated_at"),
        "totals": report.get("totals"),
        "sessions": report.get("sessions"),
        "cross_check": {
            "verdict": cross_check.get("verdict"),
            "notes": cross_check.get("notes") or [],
        },
    }


def _refresh_usage_report(plan_id: str, *, block: bool = False) -> None:
    """Re-aggregate this plan's LLM usage into ``usage_report.json``.

    Runs on a short-lived daemon thread by default: the aggregation
    snapshots CC Switch's SQLite store (~90 MB) and must never stall
    the executor watcher / verification thread that triggers it. Any
    failure is logged and swallowed — accounting must not break the
    workflow (same contract as the registry itself).
    """

    def _work() -> None:
        try:
            plan_usage.write_usage_report(plan_id)
        except Exception:
            logger.warning(
                "usage_report_refresh_failed plan=%s", plan_id, exc_info=True
            )

    if block:
        _work()
    else:
        threading.Thread(target=_work, daemon=True).start()


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    """Return structured error responses when detail is a dict with error/detail keys."""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Catch-all handler for unhandled exceptions.
    Logs the full traceback and returns a structured error response
    so the client sees *why* the request failed instead of a bare 500.
    """
    import traceback
    tb = traceback.format_exc()
    logger.error(
        "Unhandled exception in %s %s: %s\n%s",
        request.method, request.url.path, exc, tb,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc),
            "type": type(exc).__name__,
        },
    )


# Serve frontend static files
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"




@app.get("/health")
def health_check():
    """Liveness probe used by startup latency tests and load balancers.

    2026-09-23: this answers ONLY "is the process serving HTTP".  During
    the SQLite-connection outage the process was perfectly healthy by
    this probe while every data endpoint returned 500, which is how the
    supervisor kept reporting it "running" and the scheduler's card said
    "未运行" for 40 minutes.  Whether the *data plane* works is a separate
    question — see :func:`health_data_check`.
    """
    return {"status": "ok"}


@app.get("/health/data")
def health_data_check():
    """Data-plane probe: can the server actually read its state database?

    Unlike :func:`health_check`, this exercises the SQLite path a real
    request depends on (open + version read).  It exists because a
    server can be process-healthy and data-plane-dead at the same time —
    the 2026-09-23 outage had ``/health`` returning 200 while
    ``/api/plans`` returned 500 ``database disk image is malformed`` for
    the rest of the server's life.

    Deliberately read-only and cheap: it never takes the v5 migration
    write lock (the fast path in ``migrate`` is a plain SELECT on a
    current-schema DB).
    """
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate as _migrate
        db_path = _state_db_path()
        if not db_path.exists():
            # Fresh install — no state.db yet is legitimate, not a fault.
            return {"status": "ok", "plan_routing_rows": 0}
        conn = open_db(db_path)
        try:
            _migrate(conn)
            row = conn.execute("SELECT COUNT(*) FROM plan_routing").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a probe must never 500
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
    return {"status": "ok", "plan_routing_rows": row[0] if row else 0}


@app.get("/api/health")
def api_health_check():
    """API-namespaced liveness probe (alias of /health)."""
    return {"status": "ok"}


@app.post("/start")
def start_run(request: Request):
    """Generic start endpoint that returns a fresh run identifier."""
    import uuid
    run_id = str(uuid.uuid4())
    return {"status": "ok", "run_id": run_id}


# --- Request/Response Models ---

class StartInterviewRequest(BaseModel):
    requirement: str
    plan_id: Optional[str] = None


class ContinueInterviewRequest(BaseModel):
    reply: str


class AnswerInterviewRequest(BaseModel):
    dimension: str
    answer: str


class ReviewActionRequest(BaseModel):
    action: str  # accept | skip | revise | reset
    note: str = ""
    question: str = ""


class RefineRequest(BaseModel):
    feedback: Optional[str] = None


class AddDecisionPointRequest(BaseModel):
    requirement: str
    count: int = 1


class PlanStateUpdateRequest(BaseModel):
    arch_enabled: Optional[bool] = None
    test_enabled: Optional[bool] = None
    current_phase: Optional[str] = None


class PlanPhaseTransitionRequest(BaseModel):
    target_phase: Optional[str] = None


class GeneratePRDRequest(BaseModel):
    project_dir: Optional[str] = None


class StartExecutionRequest(BaseModel):
    project_dir: Optional[str] = None
    tool: Optional[str] = None  # "claude" (default), "opencode", or "vendor-c"
    # Per-PRD-DP-1 + arch-DP-7: sink routing for downstream notifiers.
    # ``None`` falls back to the Telegram default (Feishu is owned by
    # the caller and not re-pushed here). ``[]`` is a valid "no sinks"
    # value — the plan runs but pushes are routed nowhere. Unknown
    # values are rejected by pydantic (422) so typos surface up-front
    # instead of silently dropping the message.
    sync_targets: Optional[List[Literal["feishu", "telegram"]]] = None


class StartVerificationRequest(BaseModel):
    #: Round budget for this run.
    #:
    #: ``None`` (the default when the body omits it) means "leave the
    #: budget alone" — the endpoint keeps ``plan_verification.max_rounds``
    #: as the plan was set up with, and only a plan that has never run
    #: gets ``DEFAULT_MAX_VERIFICATION_ROUNDS``. An explicit integer is
    #: how a task gets its own budget — ``/start`` may pass one per run, so
    #: different plans can carry different iteration budgets).
    max_rounds: Optional[int] = None
    auto_fix: Optional[bool] = True
    tool: Optional[str] = None
    force: Optional[bool] = False  # If True, re-run verification even if report exists
    max_parallel: Optional[int] = 1  # VP-level concurrency (1=serial, 0=unlimited)


class TaskDeltaRequest(BaseModel):
    """Operator payload for ``POST /api/execution/{plan_id}/task_delta``.

    Two actions, matching the verification side's
    ``verification_plan_delta`` contract (see ``backend/task_plan_delta.py``):

      * ``add``     — new task; must carry ``title``, ``reason`` and
                       ``test_command``.
      * ``obsolete`` — mark an existing task void (``id`` + ``reason``).
                       Marked, never deleted, and the task's own fields
                       are not touched.

    Attempting to *modify* an existing task (reusing its id in ``add``)
    is rejected and recorded in ``rejected_modifications`` rather than
    applied.
    """

    add: List[Dict[str, Any]] = []
    obsolete: List[Dict[str, Any]] = []
    round: int = 0
    actor: str = "operator"


class ResetVerificationRequest(BaseModel):
    """Operator payload for ``POST /api/verification/{plan_id}/reset``.

    2026-09-17: a manual restart must set the verification state
    directly rather than walk the state machine from one point to the
    next. Routing it through the transition table is what lets the
    machine drift into states nobody asked for, and it makes every
    restart depend on the transitions the current stage happens to
    allow. A reset is a fixed procedure — "clear what should be
    cleared" — so writing the post-reset state outright is both safer
    and simpler.

    This makes it a HARD RESET, in contrast with
    :class:`ResetRoundsRequest`, which is explicitly a *budget* reset —
    "verdicts / runtime state survive".

    Attributes:
        force: proceed even when a verification thread is live. The
            hard reset is not safe against a running round (that thread
            holds the orchestrator and keeps writing), so it refuses by
            default; ``force`` first signals the round to cancel, then
            resets. Without it a live round answers 409.
        clear_artifacts: delete the per-round disk outputs
            (``verification_report.json``,
            ``verification_execution_results.json``,
            ``verification_repair_tasks.json``,
            ``verification_loop_tracking.json``,
            ``logs/verification_*``, ``screenshots/verification_*``).

            ``verification_plan.json`` is deliberately **not** in this
            list any more — see ``clear_verification_plan``.
        clear_verification_plan: also delete ``verification_plan.json``,
            so the next round re-runs Phase-1 and regenerates the VP list
            from scratch. Off by default.

            2026-09-19: a reset clears the round counter without
            touching the VP list.

            The VP list is the only thing in this reset that the *next*
            generation of the plan is keyed against, and its ids are
            positional (``VP-0NN``). Regenerating recycles those ids onto
            different verification points, so every cross-batch artefact
            keyed by VP id — ``verification_failure_history.json`` above
            all, which is what the repair prompt renders as "上次修复尝试
            未生效" — silently re-attributes the previous batch's verdicts
            to the current batch's same-numbered VPs — a VP list that
            grows from one batch to the next keeps its ``VP-0NN`` ids
            while their meaning changes underneath.

            The reason the file used to be deleted — "leaving a stale
            plan behind is how a reset silently keeps the old verdict
            set" — no longer holds: the verdict set lives in
            ``plan_verification`` (``results`` / ``verdicts`` /
            ``runtime_state`` / ``executor_state`` / ``progress_state``
            / ``execution_results``) and this same reset clears those,
            plus the executor sidecars go on the next round's
            ``_clear_verification_state_files``. Nothing in
            ``verification_plan.json`` is per-round state — its top level
            is ``services`` + ``verification_points``, and every VP field
            is definitional.

            Turn it on when the plan itself was regenerated (PRD revised,
            tasks re-cut) and a fresh VP list is actually what you want.
        supersede_repair_tasks: mark pending ``RP-*`` tasks
            ``superseded``. They were generated from the verdicts this
            reset is discarding, so leaving them pending would have the
            executor run repairs for failures that no longer exist.
            Nothing is ever deleted — see the task-data rule.
        restart_at_round: the 1-based round the next ``/start`` begins
            at (same numbering the card shows). Default 1 = the full
            batch. May not exceed the plan's immutable ``max_rounds``.
    """

    force: bool = False
    clear_artifacts: bool = True
    clear_verification_plan: bool = False
    supersede_repair_tasks: bool = True
    restart_at_round: int = Field(default=1, ge=1, le=1000)


class ResetRoundsRequest(BaseModel):
    """Operator payload for ``POST /api/verification/{plan_id}/reset_rounds``.

    2026-09-16 — the endpoint was renamed from
    ``reset_max_rounds`` because that name described the *original*
    behaviour, not the current one::

        "这个 reset max rounds 跟实际的作用不符，把这个端点名称改名为
         reset current verification rounds。"

    The rename follows the 2026-09-14 semantic inversion. The user's
    words then::

        "本来是要把 MaxRounds 上限修改，现在不去改上限，改的是当前的
         计数。因为上限本来就没什么好改的，不允许被修改，但是当前的
         计数的话，在用户授权的情况下是可以被修改的。"

    So: ``max_rounds`` is the plan's budget and is immutable (raising it
    makes rounds unbounded — the 2026-08-25 incident; lowering it
    silently rewrites the plan's setup contract). What the operator
    actually needs is another *batch*: reset the round counter and the
    auto-loop may iterate up to ``max_rounds`` again. "reset_rounds"
    says exactly that.
    """

    #: Deprecated: accepted only when it equals the plan's current cap, so
    #: older callers that echo the cap back keep working.
    new_max_rounds: Optional[int] = Field(default=None, ge=1, le=1000)
    #: 2026-09-15: the value is
    #: **the round number the next iteration starts at**, in the same
    #: 1-based numbering the operator sees on the card ("重置回 1" →
    #: rounds 1, 2, 3 run). ``None``/``1`` → a full fresh batch of
    #: ``max_rounds`` rounds. Internally stored as completed-rounds =
    #: ``value - 1`` so ``/start``'s ``next_round = counter + 1`` lands
    #: on exactly this round. Must not exceed the immutable cap.
    reset_round_to: Optional[int] = Field(default=None, ge=1, le=1000)


class ExecutionStatusResponse(BaseModel):
    plan_id: str
    status: Literal["running", "completed", "failed", "stopped", "not_started"]
    logs: list[str]
    project_dir: str
    pid: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    # 2026-09-14: free-form str, NOT a Literal — the live state.db
    # carries historical values outside the original enum (e.g.
    # ``executor_killed_infinite_loop``, ``"pre-restart cleanup: stale
    # running row, no live pid"``) and the Literal made this endpoint
    # 500 on every poll for such plans. The sibling
    # ``ExecutionProgressResponse.stop_reason`` was already ``str``.
    stop_reason: Optional[str] = None
    sync_targets: Optional[List[str]] = None


class ExecutionProgressResponse(BaseModel):
    plan_id: str
    pid: Optional[int] = None
    project_dir: str
    execution_status: Literal["running", "completed", "failed", "stopped", "not_started"]
    tasks: list
    current: Optional[dict] = None
    next: Optional[dict] = None
    counts: dict
    stop_reason: Optional[str] = None
    stop_detail: Optional[str] = None
    api_error: Optional[dict] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    # Free-form str — see ExecutionStatusResponse.stop_reason (2026-09-14).
    execution_stop_reason: Optional[str] = None


# In-memory execution state keyed by plan_id
_execution_state: dict = {}

# Per-plan locks for atomic execution.json writes
_execution_locks: dict[str, threading.Lock] = {}

# 2026-09-11 plan v14 (repair→execution→verification auto-chain):
# Serializes concurrent writes to ``_execution_state[plan_id]`` across
# callers. ``start_execution`` (REST) and ``_run_repair_execution_async``
# (background callback from verification auto-loop) can both touch the
# same plan_id's state dict in close succession. Without the lock,
# ``setdefault`` races the field writes and the watchdog sees an
# inconsistent view (e.g. ``status='running'`` but ``pid=None``).
_execution_state_lock = threading.Lock()

# In-memory verification state keyed by plan_id
_verification_state: dict = {}

# Per-plan locks for atomic verification state writes
_verification_locks: dict[str, threading.Lock] = {}

# Global lock for atomic verification start (prevents concurrent starts for the same plan)
_verification_lock = threading.Lock()


def _is_execution_in_flight(state: Optional[dict]) -> bool:
    """True iff this ``_execution_state`` record is currently being worked on.

    ``_execution_state`` is a per-plan **record cache** (status / pid /
    logs / timestamps), NOT an in-flight set. Entries are never evicted,
    a plan that finishes in-process keeps its entry with
    ``status="completed"``, and every boot re-seeds the dict from
    ``plan_execution`` for *any* status via
    :func:`_recover_execution_states`. So ``plan_id in _execution_state``
    answers "does a record exist" — emphatically not "is something
    running". A raw key dump labeled ``in_flight_executions`` once
    reported three plans while every filtered consumer correctly
    reported zero (2026-09-18); this predicate is the single source of
    truth that keeps those two answers from diverging again.

    Two shapes count as in flight:

    * the executor subprocess handle is live (``proc.poll() is None``), or
    * no handle is retained but the record says ``running`` with no
      ``ended_at`` — verification / repair rounds run on a background
      *thread*, so there is no ``proc`` to poll.

    Liveness here means "has not exited", NOT "is making progress": a
    hung-but-alive executor still reads as in flight. Flipping such a
    plan to ``failed`` is the watchdog's job (``_lazy_check_execution``
    → ``_mark_failed_dead``), deliberately not this predicate's — this
    one is a cheap, IO-free read used on hot paths (the supervisor polls
    ``/api/system/active``; the heartbeat calls it every 30s).
    """
    if not state:
        return False
    proc = state.get("process")
    if proc is not None and proc.poll() is None:
        return True
    return state.get("status") == "running" and state.get("ended_at") is None


def _plan_execution_in_flight(plan_id: str) -> bool:
    """:func:`_is_execution_in_flight` for ``plan_id``'s record.

    Reads the same ``_execution_state`` entry the watchdog, the
    ``/api/system/active`` endpoint and the card renderer consult, so
    "is a background execution outstanding for this plan" has exactly
    one answer in the process. Used by the auto-verification loop to
    tell a terminal exit from a hand-off — see
    :class:`_VerificationLoopOutcome`.

    The repair path sets ``status="running"`` / ``ended_at=None`` when
    it seeds the record and clears them from its watcher thread once
    ``process.wait()`` returns (``_run_repair_execution_async``), so a
    repair round that has finished no longer reads as in flight.
    """
    return _is_execution_in_flight(_execution_state.get(plan_id))


#: Task-status buckets ``PlanStatus.tasks`` carries.
_STATUS_TASK_KEYS = ("completed", "failed", "in_progress", "pending", "skipped")

#: Statuses that end a task's life. Used to decide whether a ``plan_tasks``
#: row with no counterpart in ``tasks.json`` is a *terminal* DB-only orphan
#: — the rows the card drops (``drop_terminal_db_orphans``).
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "skipped", "superseded"}
)


















#: ``_verification_state`` ``verification_status`` values that mean the
#: verification workflow is still driving the plan: an ordinary round, a
#: repair execution spawned by the auto-loop, or a re-run after repair.
#: Anything else (``passed`` / ``failed`` / ``loop_stopped`` /
#: ``not_started``) is a resting state, even though the record stays in
#: the dict.
_VERIFICATION_IN_FLIGHT_STATUSES = ("running", "repairing", "rerunning")


def _is_verification_in_flight(state: Optional[dict]) -> bool:
    """True iff this ``_verification_state`` record is actively cycling.

    Sibling of :func:`_is_execution_in_flight` — same record-vs-set
    distinction, same single-source-of-truth role.
    """
    if not state:
        return False
    return state.get("verification_status") in _VERIFICATION_IN_FLIGHT_STATUSES


# 2026-09-06:
# Watchdog stats surfaced via ``GET /api/debug/verification_watchdog``.
# - ``last_sweep_at`` / ``last_sweep_count``: every ``HeartbeatMonitor``
#   tick increments the count and stamps the timestamp.
# - ``actions``: bounded deque of recent terminal-transition events.
#   Each entry is a dict ``{plan_id, stop_reason, ts}`` so operators can
#   see what the watchdog actually did without grepping logs.
_WATCHDOG_STATS: dict = {
    "last_sweep_at": None,
    "last_sweep_count": 0,
    "actions": deque(maxlen=200),
    # Per-plan dedup map so a stuck plan can't fire multiple
    # KIND_PLAN_CLOSED events while the lazy check runs every 30s. The
    # value is the epoch timestamp of the last successful
    # ``_persist_verification_terminal`` call for that plan.
    "per_plan_last_action_ts": {},
    # 2026-09-07: counters for the new kill+retry+SKIP path.
    # The sub-agent watchdog no longer aborts the verification round
    # on kill; it signals the executor to retry (kill_count == 1) or
    # give up + mark SKIPPED (kill_count >= 2). These counters let the
    # debug endpoint surface how often that path fires without
    # tailing server.log.
    "sub_agent_killed_total": 0,
    "sub_agent_skipped_total": 0,
}


def _latest_mtime(
    plan_dir: Path, glob_pattern: Union[str, Sequence[str]]
) -> Optional[float]:
    """Return the most-recent mtime (epoch seconds) for files in
    ``plan_dir`` matching ``glob_pattern``, or ``None`` if no files match.

    ``glob_pattern`` may be a single glob or a sequence of globs; with a
    sequence the newest mtime across *all* matching files is returned.

    Used by the verification watchdog to detect progress files that have
    stopped being written to (an orchestrator that's stuck waiting on
    something will not append to them, so their mtime goes stale). 2026-09-13: the log-staleness check now passes a *set*
    of globs, because a single verification round writes its progress to
    more than one file — ``logs/verification_*.log`` carries the
    group/VP lifecycle lines, while ``logs/vp_attempts/*.log`` carries
    the per-attempt subagent log for one VP. A long single-VP retry
    (e.g. VP-034's full-suite pytest, ~15 min per attempt) only touches
    the latter, so judging staleness on the orchestrator log alone made a
    healthy retry look identical to a hung orchestrator.
    """
    if isinstance(glob_pattern, str):
        patterns: Sequence[str] = (glob_pattern,)
    else:
        patterns = glob_pattern

    latest: Optional[float] = None
    for pattern in patterns:
        for path in plan_dir.glob(pattern):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                # A file can vanish between glob() and stat() (e.g. a
                # rotated vp_attempt log). Not progress evidence — skip it
                # rather than aborting the whole staleness check.
                continue
            if latest is None or mtime > latest:
                latest = mtime
    return latest


def _record_watchdog_action(plan_id: str, stop_reason: str) -> None:
    """Append an entry to ``_WATCHDOG_STATS["actions"]`` and stamp the
    per-plan dedup timestamp. Called from ``_lazy_check_verification``
    immediately after a successful ``_persist_verification_terminal``
    so a stuck plan can only fire one ``KIND_PLAN_CLOSED`` per episode.
    """
    now = time.time()
    _WATCHDOG_STATS["actions"].append(
        {"plan_id": plan_id, "stop_reason": stop_reason, "ts": now}
    )
    _WATCHDOG_STATS["per_plan_last_action_ts"][plan_id] = now

# Terminal plan_state phases that should not be transitioned away from
_TERMINAL_PHASES = ("completed", "failed", "stopped")

#: Routing phases ``POST /api/execution/{id}/start`` may CAS FROM.
#:
#: * ``ready`` — the manual gate (operator approves, then starts).
#: * ``queued`` — 2026-09-15: a plan the operator
#:   handed to the scheduler. Extracted to a module constant so the
#:   contract is testable without standing up the endpoint's subprocess.
#: * ``failed`` / ``stopped`` / ``verification_failed`` /
#:   ``verification_loop_stopped`` — re-entry after a round that did not
#:   pass. These four were a single routing value (``terminal_failed``)
#:   before schema v5 collapsed the two vocabularies; they are listed
#:   individually here because the phase vocabulary DOES distinguish
#:   them, and the pre-v5 gate accepted all four.
#: * ``completed`` / ``verification_passed`` — likewise the single old
#:   ``terminal_done``.
_EXECUTION_START_SOURCE_PHASES = (
    "ready",
    "queued",
    "failed",
    "stopped",
    "verification_failed",
    "verification_loop_stopped",
    "completed",
    "verification_passed",
)

# Minimum seconds between ``POST /api/verification/{id}/start`` and the
# point at which ``_lazy_check_verification`` is allowed to flip a
# "running" verification to "failed" based on a non-alive background
# thread. The grace period is short enough that real silent crashes are
# still detected within seconds-to-minutes of their occurrence, and long
# enough to cover the typical "POST /start → GET /status" round-trip in
# a test harness or in a UI flow that needs verification_status to read
# as "running" immediately after a successful start.
_VERIFICATION_STARTUP_GRACE_SECONDS = 60.0


# --- API Routes ---













# --- Execution ---


def _state_db_path(request: Optional["Request"] = None) -> Path:
    """Return the path of the state-machine SQLite file.

    Resolution precedence (first hit wins):

      1. ``PDT_STATE_DB_PATH`` env var — operator override (twelve-
         factor config).
      2. :data:`config_paths.STATE_DB` — ``<repo>/state.db``.

    Both steps live in :func:`config_paths.resolve_state_db_path`; this
    wrapper only adds the live-database guard below, so there is exactly
    one place that knows how the path is derived.

    A test can monkeypatch ``server._state_db_path`` to point at a
    per-test tmp SQLite file; that is the seam used by
    ``state_machine/tests/integration/test_plan_routes.py``.
    """
    env_override = os.environ.get(STATE_DB_ENV)
    if env_override:
        # An explicit override is by definition not the live database,
        # so it skips the guard below rather than being compared to it.
        return resolve_state_db_path()
    default = STATE_DB
    # 2026-09-23: the default below IS the operator's live database, so
    # it is never the right answer for a test. This resolver feeds the
    # bare ``sqlite3.connect`` call sites in this module, which do not
    # pass through ``state_machine.db.connection.open`` and would
    # otherwise slip past its guard. Same incident, same fix — see
    # ``_refuse_real_state_db_under_pytest`` for the 2026-09-13 write
    # of 200 fixture rows into the live database.
    try:
        from state_machine.db.connection import (
            _refuse_real_state_db_under_pytest,
        )
    except ImportError:  # state_machine unavailable — nothing to guard
        return default
    _refuse_real_state_db_under_pytest(default)
    return default


def _open_state_machine(
    request: Optional["Request"] = None,
) -> Optional[Tuple[Any, Any, Any, Any, Any]]:
    """Return ``(conn, routing, execution, verification, artifact)``.

    Returns ``None`` when the state-machine SQLite file does not yet
    exist on disk — the legacy on-disk JSON files then continue to
    serve the request so a fresh install (no plan has ever been
    routed through the state-machine) doesn't 500 the GET.

    The connection is opened in autocommit mode and ``migrate()`` is
    applied on every call so the helper is self-healing across
    schema-version bumps.

    Architecture decision point 5 forbids an in-memory aggregation
    facade: this helper just constructs the four repositories; each
    consumer composes them directly.

    **The caller MUST close the connection** — ``_close_state_machine``
    in a ``finally`` block::

        sm = _open_state_machine()
        try:
            ...
        finally:
            _close_state_machine(sm)

    2026-09-23: the connection used to be dropped on the floor here.
    The helper returned only the repositories, so no caller *could*
    close it, and the process accumulated one orphaned SQLite handle
    (two descriptors: the database and its WAL) per call — for the
    lifetime of the server. With eight call sites, one of them inside
    a per-plan loop in :func:`_list_plans_impl`, that is how a single
    process came to hold 388 descriptors. ``conn`` is now the FIRST
    tuple element so any un-updated ``a, b, c, d = ...`` unpacking
    fails loudly instead of silently resuming the leak.
    """
    db_path = _state_db_path(request)
    if not db_path.exists():
        return None
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )
        from state_machine.repositories.execution_repository import (
            ExecutionRepository,
        )
        from state_machine.repositories.artifact_repository import (
            ArtifactRepository,
        )
    except ImportError:
        # State-machine layer not on PYTHONPATH for some reason —
        # degrade to legacy JSON-only path.
        return None

    try:
        conn = open_db(db_path)
    except (sqlite3.OperationalError, PermissionError, OSError):
        return None
    try:
        migrate(conn)
    except sqlite3.OperationalError:
        conn.close()
        return None

    # VerificationRepository is task 2-7 + the contract from the
    # spec but the module may not be present in every checkout.
    # Import lazily so a missing module doesn't 500 list_plans.
    verification_repo: Any = None
    try:
        from state_machine.repositories.verification_repository import (
            VerificationRepository,
        )
        verification_repo = VerificationRepository(conn)
    except ImportError:
        verification_repo = None

    return (
        conn,
        RoutingRepository(conn),
        ExecutionRepository(conn),
        verification_repo,
        ArtifactRepository(conn),
    )


def _close_state_machine(
    sm: Optional[Tuple[Any, ...]],
) -> None:
    """Release the connection returned by :func:`_open_state_machine`.

    Safe to call with ``None`` (the not-yet-migrated path) and safe to
    call twice. Never raises: a failure to close must not turn a
    successful request into a 500, and the descriptor is the OS's
    problem once the handle is dropped.
    """
    if not sm:
        return
    try:
        sm[0].close()
    except Exception:  # noqa: BLE001 - closing must never fail a request
        logger.warning("failed to close state-machine connection", exc_info=True)








def _get_project_dir(plan_id: str) -> Optional[Path]:
    """Resolve the project_dir associated with a plan, if execution has been configured.

    The state-machine SQLite row (``plan_execution.project_dir``) is
    the canonical source for the project_dir — it is populated by
    :class:`ExecutionRepository` and survives server restarts.  The
    legacy ``execution.json`` direct read was removed because writes
    to that path bypass the SQLite CAS layer and silently desync
    from the rest of the state-machine row.
    already flow through the repository layer.
    """
    s = _execution_state.get(plan_id)
    if s and s.get("project_dir"):
        return Path(s["project_dir"])
    sm = _open_state_machine()
    try:
        if sm is not None:
            _, _routing, execution_repo, _verification, _artifact = sm
            try:
                row = execution_repo.summary(plan_id)
            except Exception:
                row = None
            if isinstance(row, dict):
                project_dir = row.get("project_dir")
                if project_dir:
                    return Path(project_dir)
    finally:
        _close_state_machine(sm)
    return None


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill an entire process tree by sending SIGTERM to the process group.

    Processes started with ``start_new_session=True`` get their own session
    (and thus process group). Killing the group ensures all descendant
    processes (shells, child commands, etc.) are terminated together.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def _shutdown_all_executions() -> None:
    """Terminate all running execution and verification subprocesses.

    Called during server shutdown (lifespan finally block) and atexit
    to prevent orphaned processes.
    """
    for plan_id, state in list(_execution_state.items()):
        proc = state.get("process")
        if proc and proc.poll() is None:
            logger.info("Shutting down execution subprocess for plan %s (pid=%s)", plan_id, proc.pid)
            _kill_process_tree(proc)
    # Clear background manager references held by any in-memory agents
    # (agents are ephemeral per-thread, but the BackgroundManager's
    # cleanup_all is a safety net for any leaked handles).
    for plan_id, state in list(_verification_state.items()):
        orch = state.get("orchestrator")
        if orch and hasattr(orch, "cleanup"):
            try:
                orch.cleanup()
            except Exception:
                pass
    # 2026-09-18 C3: the verification workflow's exit hooks reap each
    # plan's *declared* services as the workflow ends, but a backend
    # that dies (or is killed) never reaches them. Sweeping every
    # ledger here — atexit, lifespan shutdown, and again on the next
    # startup — is what makes "no orphan" hold across a crash, not just
    # across a clean exit.
    #
    # ``_reap_all_plans`` is the import-time reference (see the note where
    # it is bound, at the top of this module). This runs from ``atexit``,
    # i.e. after teardown has begun, so it must not resolve an import here
    # — that is exactly the failure this reference exists to avoid.
    if _reap_all_plans is not None:
        try:
            _reap_all_plans(PLANS_DIR, reason="server_shutdown")
        except Exception:  # pragma: no cover - shutdown must never raise
            logger.exception("[service_manager] shutdown service reap failed")

# Ensure subprocess cleanup even on abrupt process exit (SIGKILL bypasses
# this, but SIGTERM / unhandled exception / sys.exit all trigger atexit).
atexit.register(_shutdown_all_executions)


def _signal_handler(signum, frame):
    """2026-09-07: SIGTERM/SIGINT handler — write a shutdown-side log
    line before the Python interpreter tears the process down. Without
    this, a SIGTERM (vs graceful uvicorn reload) leaves no trace of the
    shutdown signal in server.log, which makes post-mortem correlation
    against the boot_id tag we wrote on startup impossible.

    This handler must be *transparent*: it exists to leave a trace, not
    to take over shutdown. Two rules follow from that, and both were
    violated by the 2026-09-07..09-14 revision (see
    :func:`_install_signal_handlers` for the observed incident):

    1. It must never displace a handler that was installed *after* it.
       uvicorn installs ``Server.handle_exit`` via
       ``Server.capture_signals`` — if we overwrite that, uvicorn never
       learns about the signal and the graceful shutdown (lifespan
       ``finally`` → heartbeat monitor stop → feishu notifier stop →
       ``_shutdown_all_executions``) is skipped entirely.
    2. It must actually let the process die. ``signal.signal(sig,
       SIG_DFL)`` only changes the disposition for the *next* delivery
       of ``sig`` — it does NOT re-arm the signal already in flight, so
       the old body logged the line and returned with the process still
       running and still serving.

    So: log, hand control to the handler we displaced if there is one,
    otherwise restore the default disposition and re-deliver the signal
    to ourselves.
    """
    sig_name = {
        signal.SIGTERM: "SIGTERM",
        signal.SIGINT: "SIGINT",
        signal.SIGHUP: "SIGHUP",
    }.get(signum, f"signal-{signum}")
    logger.warning(
        "[server_lifecycle] received %s pid=%d verifying_plans=%s "
        "in_flight_executions=%s",
        sig_name, _os.getpid(),
        # 2026-09-18: filtered by the in-flight predicates, same as the
        # lifespan shutdown line — see the comment there.
        [k for k, v in _verification_state.items()
         if _is_verification_in_flight(v)],
        [k for k, v in _execution_state.items()
         if _is_execution_in_flight(v)],
    )

    displaced = _DISPLACED_SIGNAL_HANDLERS.get(signum)
    if callable(displaced) and displaced is not _signal_handler:
        # Another handler owns shutdown (uvicorn's ``handle_exit`` when
        # ``_install_signal_handlers`` ran before ``uvicorn.run``).
        # Defer to it — it will set ``should_exit`` and the lifespan
        # teardown runs as designed.
        displaced(signum, frame)
        return

    # Nothing to defer to: the process must die from this signal. Unlike
    # the disposition change above, re-delivery is not optional —
    # without it the signal is swallowed and the server keeps serving.
    signal.signal(signum, signal.SIG_DFL)
    _os.kill(_os.getpid(), signum)


# Handlers that ``_install_signal_handlers`` displaced, keyed by signal.
# ``_signal_handler`` hands control back to them so that installing this
# tracing handler can never *disable* a shutdown path.
_DISPLACED_SIGNAL_HANDLERS: Dict[int, Any] = {}


def _install_signal_handlers() -> None:
    """Install :func:`_signal_handler` for SIGTERM/SIGINT/SIGHUP, once
    per process.

    Why "once per process" is load-bearing: ``server.py`` can be
    executed more than once inside a single process. ``python -m
    backend.server`` runs the body as ``__main__``, and
    ``provider_order._default_order_file()`` used to do a lazy ``from
    server import PROVIDER_ORDER_FILE`` *during lifespan startup* — a
    second, independent execution of the body under the name
    ``server``.

    That second execution can land *after* ``uvicorn.run()`` has
    installed ``Server.handle_exit``, at which point the module-level
    ``signal.signal(...)`` below overwrites uvicorn's handler. A SIGTERM
    then produces only the ``received SIGTERM`` log line — no ``shutdown
    begin``, no ``shutdown complete``, no exit: the event loop keeps
    serving until a *second* SIGTERM kills the process through the
    restored ``SIG_DFL``. Every ``kill``/``pkill``-based stop of the
    backend server, including the supervisor-style restarts, silently
    turns into a no-op.

    The sentinel lives on ``sys`` (a genuine process-wide singleton)
    because two module objects are involved — a module-global would be
    per-copy and would not dedupe. Same pattern as the
    ``_root._ac_handler_installed`` guard above, which exists for the
    identical double-execution reason.
    """
    if getattr(sys, "_ac_signal_handlers_installed", False):
        return
    sys._ac_signal_handlers_installed = True

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            _DISPLACED_SIGNAL_HANDLERS[_sig] = signal.getsignal(_sig)
            signal.signal(_sig, _signal_handler)
        except (ValueError, OSError):
            # ``signal.signal`` raises if not called from the main thread
            # — pytest fixtures / sub-process workers hit this. Skip
            # silently so import doesn't fail in test contexts.
            pass


_install_signal_handlers()


# ---------------------------------------------------------------------------
# Verification runtime state persistence
# ---------------------------------------------------------------------------
# 2026-07-19 audit: ``_verification_state[plan_id]`` was in-memory only,
# so any backend server restart lost the entire verification run state (round,
# current_vp, completed_vps, failed_vps, started_at, ...). The legacy
# sidecar JSON file captured VP events but NOT the round/orchestrator/
# status fields the /status endpoint reads from ``_verification_state``.
# After the state-machine refactor (task 11) the canonical store is the
# ``plan_verification.runtime_state`` SQLite column managed by
# :class:`state_machine.repositories.verification_repository.VerificationRepository`.
# This helper now reads the runtime state from that repository and
# reconstructs the legacy dict shape that callers expect.




















# Tee/junit artifact root — patched by unit tests so they don't litter
# (or depend on) the real shared /tmp directory.
_VP_TMP_ROOT = Path("/tmp")
































# 2026-09-11 plan v14 (repair→execution→verification auto-chain):
# ``_run_repair_execution`` above is synchronous — it blocks the
# verification auto-loop thread on ``process.wait()`` for the entire
# duration of the executor subprocess (often 5–30 minutes). The watchdog
# (``_lazy_check_verification``) then thinks the verification thread is
# alive but idle and flips the plan to ``failed`` on
# ``verification_log_stale``. Worse, the auto-loop never re-enters
# verification after the subprocess exits because its ``for round_num``
# range cap is already met.
#
# ``_run_repair_execution_async`` is the asynchronous variant: spawn
# the executor subprocess, seed ``_execution_state[plan_id]`` with the
# new pid / log_path / started_at / status='running', and return
# immediately. A daemon thread ``process.wait()``s for completion and
# fires ``on_complete(returncode)`` so the verification loop can
# chain a fresh ``start_verification_cycle`` round after the repair
# actually finishes.
#
# Why ``_execution_state`` (not ``_verification_state``):
# The repair subprocess IS an executor subprocess (same spawn helper,
# same log file shape, same heartbeat semantics). Routing the watchdog
# through the existing ``_lazy_check_execution`` path means we get
# crash detection for free without writing a parallel watchdog for the
# verification-side repair path.




























def _reap_managed_services(
    plan_id: str, plan_dir: Path, reason: str,
) -> None:
    """Stop every service this plan's ledger says the backend started (2026-09-18 C3).

    Called from every exit point of the verification workflow — round
    loop return, manual stop, reset, execution stop, server shutdown —
    plus a startup sweep for orphans a crash left behind. The point is
    that an backend-spawned process must not outlive the workflow that
    spawned it.

    Deliberately narrow about what it kills: only ledger-recorded
    processes that are still identifiable (see
    ``service_manager.reap_services``). An unidentified port holder is
    reported, never signalled. Never raises — exit-time cleanup must
    not be the thing that turns a clean shutdown into a crash.
    """
    try:
        if _reap_services is None:
            logger.warning(
                "[service_manager] reap unavailable for plan=%s: "
                "service_manager did not import", plan_id,
            )
            return
        report = _reap_services(plan_dir, plan_id, reason=reason)
    except Exception:  # pragma: no cover - reap never raises by design
        logger.exception(
            "[service_manager] reap failed for plan=%s (reason=%s)",
            plan_id, reason,
        )
        return
    if report.reaped or not report.clean:
        logger.info(
            "[service_manager] reap plan=%s reason=%s %s",
            plan_id, reason, report.to_dict(),
        )






















# --- Verification ---



# Hot-path optimization B-04: hoisted to module scope so the mapping is
# allocated once at import time instead of every status read. /api/
# verification/{plan_id}/status is polled by the verification UI on a
# 3-second tick; previously every poll rebuilt a 2-key dict just to
# look up 0-1 keys in it.
_VERIFICATION_STATUS_NORMALIZATION: Dict[str, str] = {
    "verification_passed": "passed",
    "verification_failed": "failed",
}




























































# --- Execution Log & Diagnostics Endpoints ---





# --- Static file serving (frontend) ---

#: Root the static route is allowed to serve from, fully resolved once.
#:
#: ``FRONTEND_DIR.resolve()`` rather than ``FRONTEND_DIR`` because the
#: containment test below compares resolved paths: a checkout reached
#: through a symlink (``/tmp/x`` → the real repo) would otherwise reject
#: every legitimate asset.
_FRONTEND_ROOT = FRONTEND_DIR.resolve()


def _resolve_frontend_path(path: str) -> Path:
    """Map a request path to a file inside :data:`_FRONTEND_ROOT`.

    Raises :class:`HTTPException` 404 for anything that does not land
    inside the frontend directory — which is the *only* thing this route
    is for.

    Why the containment test is not optional: the route is registered as
    ``{path:path}``, whose converter matches ``/``, and Starlette decodes
    percent escapes *before* matching — so ``%2e%2e%2f`` arrives as
    ``../`` already inside ``path``. A bare ``FRONTEND_DIR / path`` is
    therefore not confined to ``FRONTEND_DIR`` by construction; it is a
    general-purpose file server for the whole filesystem, reachable by
    any client that can open a socket to the port. Resolving and then
    requiring containment is what makes the route's actual capability
    equal the one thing it was written to do.

    404 (not 403) is returned deliberately: distinguishing "exists but
    forbidden" from "does not exist" tells an unauthenticated caller
    what is on the disk.
    """
    try:
        candidate = (FRONTEND_DIR / path).resolve()
    except (OSError, RuntimeError):
        # RuntimeError: symlink loop. OSError: unreadable component.
        raise HTTPException(404, "Not found")
    # ``resolve()`` follows symlinks, so a link inside the bundle that
    # points out of it is caught here too.
    if not candidate.is_relative_to(_FRONTEND_ROOT):
        raise HTTPException(404, "Not found")
    if not candidate.is_file():
        raise HTTPException(404, "Not found")
    return candidate


@app.get("/")
def serve_index():
    return FileResponse(
        _FRONTEND_ROOT / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# Extracted routers (see backend/routes/)
#
# Placement matters twice over:
#   * it must come AFTER the request models are defined — FastAPI evaluates
#     each handler's annotations at registration time, and these handlers
#     annotate with models that still live here;
#   * it must come BEFORE the frontend catch-all below — route matching is
#     first-match-wins and that route is a GET ``/{path:path}``, so any GET
#     route registered later (e.g. /api/plan/{id}/state-from-db) would be
#     swallowed by it and answer 404.
# ---------------------------------------------------------------------------
# ``routes.*`` late-bind back into this module (``_server.<name>``) so that
# monkeypatching ``server.PLANS_DIR`` / ``server._state_db_path`` / … keeps
# working. The import therefore has to happen HERE — after every name those
# modules reference exists.
#
# ``sys.modules`` is seeded first so that ``python server.py`` (where this
# file runs as ``__main__``) does not make ``import server`` in those modules
# execute a second, independent copy of the application.
sys.modules.setdefault("server", sys.modules[__name__])

from routes.phases import router as _phases_router  # noqa: E402
# Re-exported: the rest of this module (and the tests) still reach for them
# here — ``_reject_if_placeholder_prd`` is called by the executor-side code
# that stayed, and the other two are imported/patched by name from this
# module by the suite.
from routes.phases import (  # noqa: E402,F401
    _is_placeholder_prd,
    _reject_if_placeholder_prd,
    answer_interview,
    review_arch_item,
    review_test_item,
)

app.include_router(_phases_router)


# --- routes/plans.py ---------------------------------------------
from routes import plans as _routes_plans  # noqa: E402
from routes.plans import (  # noqa: E402,F401
    _read_task_rows,
    _read_disk_task_ids,
    _task_counts,
    _log_divergences,
    _build_plan_status,
    get_plan_status,
    _plan_status_payload,
    _plan_summary_section,
    get_active_tasks,
    list_plans,
    _list_plans_impl,
    _list_plans_body,
    _seed_plan_routing_phase,
    get_plan_summary,
    _get_plan_summary_body,
    get_plan_usage,
    get_plan_tasks,
)

app.include_router(_routes_plans.router)


# --- routes/execution.py -----------------------------------------
from routes import execution as _routes_execution  # noqa: E402
from routes.execution import (  # noqa: E402,F401
    _recover_execution_states,
    _adopt_run_project_dir,
    _spawn_executor_subprocess,
    start_execution,
    get_execution_status,
    apply_execution_task_delta,
    stop_execution,
    get_execution_progress,
    get_execution_files,
    get_execution_logs,
    diagnose_execution,
)

app.include_router(_routes_execution.router)


# --- routes/verification.py --------------------------------------
from routes import verification as _routes_verification  # noqa: E402
from routes.verification import (  # noqa: E402,F401
    ArchivedPlanError,
    _normalize_verification_status,
    _open_verification_state,
    _archived_plan_response,
    _ensure_verification_plan,
    _conflict_reason,
    _verification_status_from_db,
    _status_updated_at,
    _get_verification_status,
    start_verification,
    reset_verification,
    reset_rounds,
    get_verification_status,
    _read_latest_vp_start,
    _read_all_vp_starts,
    _read_latest_round_vp_activity,
    _running_vps_from_activity,
    _build_verification_progress,
    _verification_progress_body,
    _load_vp_splits_for_progress,
    _load_repair_tasks_for_progress,
    get_verification_progress,
    get_verification_repair_tasks,
    stop_verification,
    force_verification_terminal,
)

app.include_router(_routes_verification.router)


# --- verification_loop.py ----------------------------------------
from verification_loop import (  # noqa: E402,F401
    _load_verification_runtime_state,
    _init_verification_state,
    _recover_verification_states,
    _reconcile_orphaned_verification_stages,
    _mark_failed_dead,
    _update_plan_state_to_terminal,
    _mark_verification_failed_dead,
    _lazy_check_execution,
    _normalized_vp_token,
    _vp_artifact_processes,
    _verification_liveness_probe,
    _kill_orphaned_vp_processes,
    _cleanup_dead_verification_processes,
    _lazy_check_verification,
    _lazy_check_sub_agents,
    _kill_sub_agent_process,
    _handle_stuck_sub_agent,
    _count_unfinished_tasks,
    _are_all_unfinished_blocked_by_failed_upstream,
    _mark_task_skipped,
    _mark_verification_round_running,
    _plan_next_round,
    _bind_verification_state_conn,
    _release_verification_state_conn,
    _run_repair_execution,
    _run_repair_execution_async,
    _persist_verification_terminal,
    _count_unfinished_execution_tasks,
    _rollback_dead_end_to_ready,
    _round_is_pure_split,
    _dead_end_terminal,
    _repair_generation_failed,
    _get_pending_repair_tasks,
    _handle_passed_round,
    _record_verification_terminal,
    _VerificationLoopOutcome,
    _run_auto_verification_loop,
    _settle_phase_after_verification_loop,
    _run_auto_verification_loop_inner,
)


# --- routes/debug.py ---------------------------------------------
#
# The debug endpoints inject verification state and fire synthetic
# events straight into the shared bus — they are operator tooling for
# debugging *this* instance, not part of the product surface. They were
# registered unconditionally, which put five extra mutators in every
# install's route table for a capability only a maintainer ever uses.
#
# So they ship **off**. ``PDT_DEBUG_ROUTES=1`` opts in; the bundled test
# suite sets it in ``tests/conftest.py``, because that suite is what
# exercises them.
#
# The names are imported unconditionally either way: the suite
# monkeypatches them as ``server.<name>``, and the static contracts
# enumerate routes by identity.
from routes import debug as _routes_debug  # noqa: E402
from routes.debug import (  # noqa: E402,F401
    debug_inject_verification_state,
    debug_fire_event,
    debug_notifications,
    debug_repush_plan_card,
    debug_verification_watchdog,
)

#: Set to a truthy value to register ``routes/debug.py``.
ENV_DEBUG_ROUTES: str = "PDT_DEBUG_ROUTES"

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def debug_routes_enabled() -> bool:
    """True when this process opted into the debug routes."""
    return os.environ.get(ENV_DEBUG_ROUTES, "").strip().lower() in _TRUTHY_ENV_VALUES


if debug_routes_enabled():
    app.include_router(_routes_debug.router)


def iter_app_routes() -> list:
    """Every ``APIRoute`` this app serves, included routers flattened.

    ``app.routes`` alone is no longer enough: since FastAPI 0.141 an
    ``include_router`` call lands as a single ``_IncludedRouter`` entry whose
    own routes live under ``include_context.included_router``. The static
    contracts that enumerate routes — plan-id containment, scene annotations —
    need the flat list, and they needed it to keep working the moment the
    first router moved out of this file. Every future extraction is covered
    by going through here rather than by touching those tests again.
    """
    from fastapi.routing import APIRoute

    flat: list = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                flat.append(route)
                continue
            inner = getattr(route, "routes", None)
            if inner:
                walk(inner)
                continue
            included = getattr(
                getattr(route, "include_context", None), "included_router", None
            )
            if included is not None:
                walk(included.routes)

    walk(app.routes)
    return flat


@app.get("/{path:path}")
def serve_static(path: str):
    """Serve one file from the frontend bundle — and nothing else.

    The route exists to serve the UI's static assets (``index.html``,
    ``app.js``, ``style.css`` today). It is deliberately a catch-all
    rather than one route per file so that adding an asset does not mean
    editing the server — which is exactly why it needs an explicit
    containment check rather than relying on the URL shape.
    """
    file_path = _resolve_frontend_path(path)
    headers = (
        {"Cache-Control": "no-cache, no-store, must-revalidate"}
        if file_path.suffix in (".js", ".css", ".html")
        else {}
    )
    return FileResponse(file_path, headers=headers)


# ---------------------------------------------------------------------------
# Per-provider max concurrency limits
# ---------------------------------------------------------------------------
#
# ``PROVIDER_LIMITS`` maps a provider's CC Switch name to its maximum
# concurrent slot count. Keys are the names CC Switch uses, verbatim —
# the same strings ``provider_routing.yaml`` matches its regexes against
# and ``cc_switch`` looks up. There is no kebab-case alias
# in between.

#: Caps used when the consumer layer yields nothing.
#:
#: Empty on purpose. A per-provider cap describes *your* CC Switch rows
#: (a row may carry ``max_concurrency`` alongside its ``ANTHROPIC_*``
#: block), not this project — a table of names here would compile one
#: operator's provider set into every install. Callers fall back to
#: :data:`DEFAULT_VP_CONCURRENCY_CAP` instead.
DEFAULT_PROVIDER_LIMITS: Dict[str, int] = {}


def _read_dynamic_provider_limits() -> Dict[str, int]:
    """Read per-provider max-concurrency caps from the CC Switch DB.

    Walks every provider the consumer layer can see and harvests the
    optional ``max_concurrency`` field stored alongside its
    ``ANTHROPIC_*`` block. Returns an empty dict when the consumer layer
    is unavailable (DB missing, unreadable, or no provider carries the
    field) so callers fall back to :data:`DEFAULT_PROVIDER_LIMITS`.

    Only non-empty names with positive integer values are kept; anything
    else is silently dropped.
    """
    try:
        names = list_provider_names()
    except CCSwitchError:
        return {}
    except Exception:  # pragma: no cover - defensive
        return {}

    result: Dict[str, int] = {}
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            cfg = get_provider(name)
        except (CCSwitchError, ValueError):
            continue
        except Exception:  # pragma: no cover - defensive
            continue
        if cfg is None:
            continue
        mc = cfg.env.get("max_concurrency")
        if isinstance(mc, int) and not isinstance(mc, bool) and mc > 0:
            result[name] = mc
    return result


def _get_provider_limits() -> Dict[str, int]:
    """Return the active provider → limit map.

    Prefers dynamic values from the consumer layer; falls back to
    :data:`DEFAULT_PROVIDER_LIMITS` when the consumer layer is unavailable
    or returns no usable limits. Both branches key on CC Switch provider
    names and only ever carry positive integer values.
    """
    dynamic = _read_dynamic_provider_limits()
    if dynamic:
        return dynamic
    return dict(DEFAULT_PROVIDER_LIMITS)


PROVIDER_LIMITS: Dict[str, int] = _get_provider_limits()


def _port_is_already_serving(host: str, port: int) -> bool:
    """True when something already answers on ``host:port``.

    A successful ``bind`` is *not* a free-port test. ``SO_REUSEADDR`` —
    set below so that a socket left in TIME_WAIT by a restart does not
    force a pointless port bump — also lets a socket bind a **specific**
    address while another process still holds the wildcard: with one
    process on ``*:8000``, a second can successfully bind
    ``127.0.0.1:8000``, so two servers share the port and requests are
    routed by whichever listener happens to win. That is worse than a
    port bump.

    Connecting asks the question that actually matters — "is something
    already serving here?" — and gets the same answer on every platform.
    """
    probe_host = host if host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        try:
            probe.connect((probe_host, port))
        except OSError:
            return False
        return True




if __name__ == "__main__":

    import socket
    import uvicorn

    # Port backoff: try PDT_PORT (or default 8000); on AddressInUse, increment until free.
    # Two independent backend servers may coexist (e.g. two independent projects); refuse to silently
    # override another service. Use PDT_PORT=NN env var to pin a specific port.
    #
    # Host: loopback unless PDT_HOST says otherwise. See
    # ``config_paths.resolve_server_host`` — the UI is unauthenticated,
    # so a wildcard bind is something an operator must ask for, never a
    # default. This also means the probe below binds the same interface
    # the server will, so "port is free" is decided honestly: a port held
    # on *another* interface no longer looks free.
    host = resolve_server_host()
    preferred_port = int(os.environ.get("PDT_PORT", "8000"))
    max_attempts = 50
    bound_port = None
    last_error = None
    for offset in range(max_attempts):
        candidate = preferred_port + offset
        # Two complementary checks, because neither alone is sufficient:
        # the connect test catches an overlapping listener that
        # SO_REUSEADDR would let us bind alongside, and the bind test
        # catches a port that is taken but not currently answering.
        if _port_is_already_serving(host, candidate):
            last_error = f"port {candidate} already has a listener"
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, candidate))
            bound_port = candidate
            if offset > 0:
                print(f"[backend server] Port {preferred_port} occupied; using fallback port {bound_port}")
            break
        except OSError as e:
            last_error = e
            continue
    if bound_port is None:
        print(f"[backend server] FATAL: no free port in range {preferred_port}..{preferred_port + max_attempts - 1} ({last_error})")
        sys.exit(1)
    print(f"[backend server] Starting uvicorn on {host}:{bound_port}")
    uvicorn.run(app, host=host, port=bound_port)