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
import uuid
import atexit
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

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
from phase_migration import migrate_ready_to_queued
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
from utils.process import _signallable_pgid, kill_process_group
from sub_agent_registry import sub_agent_registry, SUB_AGENT_STALE_THRESHOLD_SEC
from provider_order import load_fallback_order, ProviderOrderError
from runtime_state import RuntimeState
from scheduling.guard import InFlightFileGuard
from dynamic_provider_concurrency import (
    ActiveConcurrencyTracker,
    set_shared_tracker,
)
from config_paths import (
    BACKEND_CONFIG_YAML,
    ENV_FILE,
    PROJECT_ROOT,
    SCHEDULER_FILE_ENV,
    SCHEDULER_FILE_NAME,
    STATE_DB,
    STATE_DB_ENV,
    resolve_config_dir,
    resolve_scheduler_file,
    resolve_server_host,
    resolve_state_db_path,
)

# ---------------------------------------------------------------------------
# hitl.service — the human-in-the-loop decision service
# ---------------------------------------------------------------------------
# Imported flat, like every other ``backend/*.py`` module: it resolves
# only while ``backend/`` is on ``sys.path``.  Only the expiry sweep is
# needed here — the HTTP surface asks, lists and answers through
# ``routes/hitl.py`` — and it is imported by name so that the sweep the
# heartbeat runs is a name the suite can replace.
from hitl.service import expire_due

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

#: The one function that signals a service.  Resolved at import time for
#: the same reason the reaps above are: the shutdown path runs from an
#: ``atexit`` hook, after the interpreter has begun tearing down, and an
#: import there would raise ``ModuleNotFoundError`` at the worst
#: possible moment.  ``None`` means the module is missing: the kill
#: becomes a no-op that says so, rather than a crash on the way out.
try:  # pragma: no cover - the repo always ships service_manager.py
    from service_manager import (
        ServiceRuntime as _ServiceRuntime,
        kill_service as _kill_service,
    )
except Exception:  # noqa: BLE001
    _ServiceRuntime = None
    _kill_service = None

try:  # pragma: no cover - the repo always ships service_manager.py
    from service_manager import (
        new_owner_token as _new_owner_token,
        register_service_owner as _register_owner,
        release_service_owner as _release_owner,
    )
except Exception:  # noqa: BLE001
    _new_owner_token = None
    _register_owner = None
    _release_owner = None

# ---------------------------------------------------------------------------
# shutdown_checkpoint (the shutdown checkpoint writer) — resolved once, at
# import time
# ---------------------------------------------------------------------------
# Same reasoning as the service_manager imports above: the shutdown path
# runs from an ``atexit`` hook, after the interpreter has begun tearing
# down — under pytest, after the ``pythonpath`` plugin has removed the
# entries it injected — so an import *there* raises ``ModuleNotFoundError``,
# and reporting that failure hits a logger whose stream is already closed
# ("I/O operation on closed file").  The only trace was an error block
# printed after an otherwise green run.
#
# The import is here; the *call* is not.  ``_shutdown_all_executions``
# re-reads ``shutdown_checkpoint.write_shutdown_checkpoints`` at call time
# (a function-level ``from ... import``), so a writer that has been
# swapped out is the one that runs.  What this import buys is that the
# module is already in ``sys.modules`` when the atexit hook fires, so
# that function-level import can no longer fail.
try:  # pragma: no cover - the repo always ships shutdown_checkpoint.py
    import shutdown_checkpoint  # noqa: F401
except Exception:  # noqa: BLE001 - a missing module must not break startup
    shutdown_checkpoint = None

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

# ---------------------------------------------------------------------------
# orphan_reaper (orphan-process reaping) — resolved once, at import time
# ---------------------------------------------------------------------------
# The same reasoning as the service_manager imports above: the module is
# resolved once so that the lifespan, the shutdown path and the tests all
# see one object, and a module that cannot be imported degrades to a
# no-op instead of taking the backend down with it.  ``None`` means the
# module is missing.
#
# What must NOT happen here is *running* anything.  The reaper's marker
# is written into ``os.environ`` in the lifespan, never at import time:
# a module that stamped the environment on import would mark every
# process the importer spawns — every pytest subprocess, every tool the
# suite runs — as a child of whoever imported it.
try:  # pragma: no cover - the repo always ships orphan_reaper.py
    import orphan_reaper
except Exception:  # noqa: BLE001 - a missing module must not break startup
    orphan_reaper = None

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
# declared in backend/config.yaml as project-relative without
# depending on the current working directory the server happens to
# launch from.
#
# 2026-10-28: this used to be spelled
# ``Path(__file__).parent.parent.resolve()`` here — a SECOND spelling
# of the repository root, next to the ``config_paths.PROJECT_ROOT``
# imported above.  Two spellings of one fact is how the ``.env`` file
# came to be read from two different places (see the note below).  It
# is now an alias of the single source of truth, so
# ``server._PROJECT_ROOT`` and ``config_paths.PROJECT_ROOT`` are the
# same object by construction rather than by coincidence.  The name
# stays: the extracted modules reach the app root through it
# (``_server._PROJECT_ROOT`` — see
# ``tests/static_gates/test_extracted_modules_use_app_root.py``).
_PROJECT_ROOT = PROJECT_ROOT
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
#
# The path comes from ``config_paths.ENV_FILE`` rather than being derived
# here. It used to be spelled ``_PROJECT_ROOT / ".env"`` at this site,
# ``backend/.env`` in ``config_paths``, and ``Path(__file__).parent /
# ".env"`` in ``env_config`` — three spellings, two different files, and
# this one the odd man out. See ``config_paths.ENV_FILE``.
try:
    from dotenv import load_dotenv

    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)
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
# first deploy: an operator-set threshold of 3600s in the dotenv met an
# absolute 2700s default, and every trip became terminal at once. The
# default is therefore DERIVED (threshold + 30 min of patience) rather
# than absolute; ``VERIFICATION_HARD_STALE_SECONDS`` can still override it.
#
# 2026-10-14: the hard ceiling is 1 HOUR, raised
# from the 45 min this default used to produce. Production, a
# 2026-10-12 plan, round 1: a healthy round
# was killed 7 min before it finished, purely because the gap in its
# round log had not closed yet.
#
# The 1 h is a FLOOR on the derived figure rather than a replacement for
# it, which is what keeps the invariant above true for every threshold
# rather than only the default one:
#
#   * ``max(threshold + 1800, 3600) > threshold`` holds for EVERY
#     threshold — if the threshold is below 3600 the 3600 floor wins and
#     is still above it; if it is at or above 3600 the derived term wins
#     and is above it by construction. An absolute 3600 could NOT say
#     that: raise the threshold to 3600 or above and the hold branch
#     dies again, which is the original bug.
#   * A lowered threshold (short threshold, impatient operator) can no
#     longer drag the ceiling below the hour the contract asks for.
#
# Note the *specific* 3600s-in-``.env`` figure quoted above is the
# first-deploy anecdote and is not asserted about any current file: the
# dotenv is per-deployment and untracked (``config_paths.ENV_FILE`` ->
# ``<repo>/.env``), so what it holds is a fact about a deployment and
# not something this comment can keep true. The invariant is the part
# that has to hold, and it is stated in terms of the threshold.
VERIFICATION_HARD_STALE_SECONDS = float(
    os.environ.get(
        "VERIFICATION_HARD_STALE_SECONDS",
        str(max(VERIFICATION_WATCHDOG_STALENESS_SECONDS + 1800.0, 3600.0)),
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
# Backend ancillary config
# ---------------------------------------------------------------------------
#
# The provider chain is not configured from here. It is asked of CC
# Switch at startup by :func:`load_providers`, which is where the
# inherit-mode check below also lives.


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

    DEPRECATED — no production caller remains. The verification round has
    no width to resolve: how many verification points run at once is
    decided per dispatch by the provider slot pool
    (``acquire_provider_with_dynamic_capacity``), which sums what every
    provider in the scene's own chain can serve right now rather than
    what the *first* provider's static cap says. Kept, with its tests,
    because it still answers a true question ("what does the primary
    provider declare?") and deleting it would be a wider change than
    retiring it from the width decision.

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

    The chain is asked of CC Switch via
    :func:`provider_order.load_fallback_order`, so every entry already
    is a CC Switch **display name** and resolution goes through
    :func:`cc_switch.get_provider`. The resolved configs are stored in
    :data:`STARTUP_PROVIDERS` for inspection.

    Boundary conditions:

    * **Chain unresolvable** — propagates
      :class:`provider_order.ProviderOrderError`, raised when CC Switch
      cannot be asked for its provider list over HTTP. Startup MUST
      fail explicitly; there is no silent YAML ``provider_priority``
      fallback and no second source to answer instead.
    * **DB unreadable** — propagates
      :class:`cc_switch.CCSwitchError`. Startup
      MUST fail explicitly rather than silently degrading to an empty
      provider set.
    * **Empty chain** — logs a warning and returns ``[]`` so the rest of
      the process can still come up (explicit degradation).
    * **Unknown display name in DB** — the entry is skipped and a
      warning is logged, so a chain/DB drift no longer crashes
      startup.

    Returns:
        The list of resolved :class:`~cc_switch.ProviderConfig` objects,
        in chain order.

    Raises:
        ProviderOrderError: when CC Switch could not be asked for its
            provider list over HTTP.
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
        # Already names the endpoint that was asked and the evidence
        # behind the failure. Re-wrapping would bury that under a
        # second message naming a step that no longer exists.
        raise
    except Exception as exc:
        raise ProviderOrderError(
            f"failed to resolve the provider chain from CC Switch: {exc}"
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

    On each tick the monitor also runs the **human-in-the-loop expiry
    sweep**: an ``auto`` decision nobody answered ends by itself when its
    deadline passes — the stored ``default_option`` becomes the answer and
    the decision ends ``defaulted`` (``hitl.service.expire_due``).  The
    sweep runs on every tick, including on an idle server: a plan waiting
    for a human is not running, so "nothing is in flight" is exactly when
    a missed deadline still has to be noticed.
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
        """The monitor's daemon loop: one tick per ``interval``.

        The tick is a method of its own so that a single iteration can
        be driven without a thread — the sweeps it runs have to be
        testable one at a time.

        The guard around the tick is the last line of defence: a
        daemon thread that died would stop every sweep for the rest of
        the process' life, and nothing would report it.
        """
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("heartbeat tick failed: %s", exc)
            if self._stop_event.wait(self.interval):
                break

    def _tick(self) -> None:
        """One tick of the monitor.

        Two sweeps, in this order: the liveness sweep (``_check_once``)
        and the human-in-the-loop expiry sweep.

        The expiry sweep runs **after** the liveness sweep, not inside
        it.  ``_check_once`` returns immediately when no plan is in
        flight — and a plan waiting for a human is not running, so an
        idle server is exactly the case where a missed deadline has to
        be noticed.  A sweep inside ``_check_once`` would resolve the
        overdue decisions only while some other plan happened to be
        running.

        Neither sweep can take the monitor down: a failure is logged
        and the next tick runs.
        """
        try:
            self._check_once()
        except Exception:
            pass
        self._expire_hitl_decisions()

    def _expire_hitl_decisions(self) -> None:
        """Resolve the human-in-the-loop decisions whose deadline passed.

        An ``auto`` decision nobody answered ends by itself when its
        deadline passes: the stored ``default_option`` becomes the
        answer and the decision ends ``defaulted``.  ``expire_due``
        knows *how*; the tick knows *when* — and this monitor is the
        thing that runs every 30 seconds, whether or not a plan is in
        flight.

        A failure here is logged and swallowed.  The sweep writes to a
        database another thread may be writing to, and a monitor that
        died on its first expiry would leave every later decision
        waiting forever — silently, because the loop swallows what
        the tick raises.
        """
        try:
            migrated = expire_due(now=datetime.now(timezone.utc))
        except Exception as exc:
            logger.warning("hitl expiry sweep failed: %s", exc)
            return
        if migrated:
            logger.info(
                "hitl expiry sweep resolved %d overdue decision(s)", migrated
            )

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


# ---------------------------------------------------------------------------
# The scheduler engine
# ---------------------------------------------------------------------------
#
# The PDT-native scheduler is one worker thread: a tick, a mailbox and a
# configuration snapshot (architecture decision point 1).  It is built
# here, in the lifespan, because the lifespan is the only place that
# knows when the process is really up — and because the engine must not
# tick before the migration and the recovery have finished, or its first
# tick would decide on a half-recovered world.
#
# The handle lives on ``app.state.scheduler_engine``; the read model in
# ``routes/scheduler.py`` reads it and nothing else.

#: How long the shutdown waits for the scheduler's worker to finish.
#: The engine's own ``stop()`` waits this long; the join that follows
#: it is the belt to its braces — a thread that was asked to stop but
#: is still running would tick into a world that is being taken apart.
_SCHEDULER_JOIN_TIMEOUT = 30.0


class _SchedulerRuntime(NamedTuple):
    """What the scheduler leaves behind on the app.

    Attributes:
        engine: the worker, or ``None`` when the scheduler is off.
        conn: the database connection the worker was built on.  It is
            given back once the worker has been stopped.
        owned: whether *this* call started the worker.  A lifespan that
            finds a worker already running does not own it, and must
            neither stop it nor take it off the app.
    """

    engine: Optional[Any]
    conn: Optional[sqlite3.Connection]
    owned: bool


def _scheduler_settings_candidates() -> List[Tuple[str, Path]]:
    """The chain the scheduler's settings are read from, in order.

    1. the file ``PDT_SCHEDULER_FILE`` names — the operator's escape
       hatch for a deployment that keeps its settings elsewhere;
    2. ``<config dir>/scheduler.yaml`` — the operator's own file, in the
       gitignored directory the provider tables live in;
    3. the backend's own ``config.yaml`` — the file that ships with the
       repository, and the one the deployments that know nothing about
       this setting rely on.

    The chain is tried in order and the first file that is there — and
    that can be read as settings — is the file.  When no file is there
    at all, the loader's own defaults apply.
    """
    from plan_scheduler.config import (
        SOURCE_BACKEND,
        SOURCE_CONFIG_DIR,
        SOURCE_ENV,
    )

    candidates: List[Tuple[str, Path]] = []
    if (os.environ.get(SCHEDULER_FILE_ENV) or "").strip():
        candidates.append((SOURCE_ENV, resolve_scheduler_file()))
    candidates.append(
        (SOURCE_CONFIG_DIR, resolve_config_dir() / SCHEDULER_FILE_NAME)
    )
    candidates.append((SOURCE_BACKEND, BACKEND_CONFIG_YAML))
    return candidates


def _scheduler_settings_source() -> Any:
    """Where the scheduler's settings come from: the file and its level.

    The level is what the log says, so the log says whether the
    settings came from the operator's own file, from the environment,
    from the backend's configuration, or from nowhere at all.
    """
    from plan_scheduler.config import resolve_scheduler_config_source

    return resolve_scheduler_config_source(_scheduler_settings_candidates())


def _scheduler_settings_path() -> Optional[Path]:
    """The YAML file the scheduler's settings are read from.

    The chain is in :func:`_scheduler_settings_candidates`, and the
    file it settles on is the one the loader watches.  ``None`` means
    there is no file at all: the loader then answers with its own
    defaults, and the scheduler runs with ``enabled: false``.
    """
    return _scheduler_settings_source().path


def _scheduler_sink(
    snapshot: Any,
    lifecycle: Any,
    facts: Any,
    store: Any = None,
    conn: Any = None,
) -> Any:
    """The exit the tick's decisions go through.

    A dry run writes the decisions down and touches nothing else.  A
    live run carries them out through the lifecycle — the one place
    that is allowed to move a plan's phase.

    The dry run's exit is the **writer**: it leaves the three
    artifacts the cutover gate is replayed against behind — the tick
    decisions, the windows and the report.  A dry run that only wrote
    a decision log would leave the gate with nothing to read, and the
    gate would exit 2 on every run.
    """
    from plan_lifecycle_service import StartPlanSpec
    from plan_scheduler.dryrun import (
        DEFAULT_DRYRUN_ROOT,
        DryRunWriter,
        ExecuteSink,
    )

    if snapshot.dry_run:
        return DryRunWriter(
            DEFAULT_DRYRUN_ROOT,
            store=store,
            conn=conn,
            reference=_dryrun_reference(),
            workspace_of=getattr(facts, "workspace_key", None),
        )

    def _execute(decision: Dict[str, Any]) -> Dict[str, Any]:
        """Carry one decision out."""
        action = str(decision.get("action") or "")
        plan_id = str(decision.get("plan_id") or "")
        if action == "admit":
            # A fresh execution has to be given the directory it works
            # in; the facts reader is the one that knows it.
            spec = facts.start_spec(plan_id)
            lifecycle.start_plan(
                plan_id,
                StartPlanSpec(project_dir=spec.get("project_dir")),
            )
        elif action == "stop":
            # The plan is over the capacity: it is stopped **and** put
            # back into the queue, so it keeps its place in the line.
            lifecycle.stop_plan(plan_id, requeue=True)
        return decision

    return ExecuteSink(_execute)


#: The environment variable that names the comparison side of a dry
#: run: the old engine's recorded windows.  It is read once, when the
#: scheduler is built, and it is only read — the file it points at is
#: evidence, and evidence the comparison writes is not evidence.
DRYRUN_REFERENCE_ENV = "PDT_DRYRUN_REFERENCE"


def _dryrun_reference() -> Any:
    """The old engine's recorded windows — the comparison side, read only.

    The dry run holds the new engine's side of the comparison.  The
    other side is what the old engine wrote down while it was running,
    and it comes from the file ``PDT_DRYRUN_REFERENCE`` names.

    Without it there is no comparison, and every window the dry run
    records says so — a run that compared nothing may not let the
    switch happen.  A file that cannot be read is the same thing: the
    dry run goes on, and the windows stay uncompared rather than
    pretending otherwise.
    """
    from plan_scheduler.dryrun import ReferenceWindows

    raw = (os.environ.get(DRYRUN_REFERENCE_ENV) or "").strip()
    if not raw:
        return None

    path = Path(raw).expanduser()
    try:
        return ReferenceWindows.from_file(path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "[scheduler] the dry run's comparison side (%s) could not be "
            "read: %s; every window stays uncompared",
            path,
            exc,
        )
        return None


def _release_scheduler_engine(engine: Any, conn: Any) -> None:
    """Give back what a half-built engine left behind.

    An engine that was constructed but never started has no thread to
    stop; a connection that was opened has to be closed either way —
    a descriptor that is never returned is a descriptor that is never
    given back.
    """
    if engine is not None:
        try:
            engine.stop()
        except Exception:  # noqa: BLE001 — the teardown must not raise
            logger.exception("[scheduler] the engine did not stop cleanly")
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — the teardown must not raise
            logger.exception(
                "[scheduler] the engine's connection could not be closed"
            )


def _build_scheduler_engine() -> _SchedulerRuntime:
    """Build the scheduler engine, start its worker, and hand it back.

    The worker starts **whatever the configuration says**.  ``enabled`` is
    read on every tick and not once at boot, so the scheduler can be
    switched on and off without a restart — and the switch is the same
    switch the rollback uses.

    The database is opened the first time the scheduler may run.  A
    scheduler that may not run must not touch the database: "may not
    run" has to mean "nothing happens", and opening the database is
    something that happens.

    An engine of ``None`` means the scheduler is not running at all:
    the engine could not be built — the failure is logged and
    everything that was created before it is given back.

    The worker is a daemon thread, so it is never the reason the
    process stays alive; the shutdown path stops it and joins it.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.config import SchedulerConfigLoader
    from plan_scheduler.engine import EngineParts, PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    path = _scheduler_settings_path()
    source = _scheduler_settings_source()
    logger.warning(
        "[scheduler] settings: %s (source: %s)",
        path if path is not None else "the built-in defaults",
        source.level,
    )

    config_loader = SchedulerConfigLoader(path)
    snapshot = config_loader.current()

    def _bind() -> EngineParts:
        """Open the database and build the scheduler's collaborators.

        This runs the first time the configuration says the scheduler may
        run, and not before.  A connection that was opened and never
        given back is a file descriptor that is never returned, so a
        failure here closes it again.
        """
        conn = None
        try:
            conn = open_db(_state_db_path())
            migrate(conn)
            current = config_loader.current()
            store = SqliteSchedulerStore()
            facts = PlanFactsReader(conn, PLANS_DIR)
            ledger = CapacityLedger(store, facts, conn=conn)
            lifecycle = PlanLifecycleService()
            commands = CommandExecutor(
                store,
                ledger,
                lifecycle,
                facts=facts,
                conn=conn,
                capacity=current.capacity,
                min_runtime_seconds=current.min_runtime_seconds,
                # The same loader the engine reads.  The tick and the
                # explicit start are the two triggers that may interrupt
                # somebody, and they have to read the same snapshot at
                # the same moment — otherwise they pick their victim out
                # of two different candidate sets.
                config_provider=config_loader,
            )
            return EngineParts(
                store=store,
                facts=facts,
                ledger=ledger,
                commands=commands,
                sink=_scheduler_sink(current, lifecycle, facts, store, conn),
                conn=conn,
            )
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 — the teardown must not raise
                    logger.exception(
                        "[scheduler] the scheduler's connection could not be closed"
                    )
            raise

    engine = None
    try:
        engine = PlanSchedulerEngine(
            config_loader,
            binder=_bind,
            # The engine reads one file out of the plans tree: the plan's
            # own artifact, which carries the priority the caller gave
            # when it asked for the plan.  The queue row is the
            # scheduler's copy of that number, and the copy is made by
            # the worker — so the number has to outlive the import, even
            # when the scheduler was switched off at the time.
            plans_dir=PLANS_DIR,
        )
        engine.start()
    except Exception:
        # A scheduler that cannot start must not take the API down with
        # it: the failure is logged and everything that was created
        # before it is given back.
        logger.exception(
            "[scheduler] the engine could not be built; continuing without it"
        )
        _release_scheduler_engine(engine, None)
        return _SchedulerRuntime(None, None, False)

    logger.warning(
        "[scheduler] started: capacity=%d dry_run=%s tick_seconds=%.1fs "
        "enabled=%s",
        snapshot.capacity, snapshot.dry_run, snapshot.tick_seconds,
        "yes" if snapshot.enabled else "no",
    )
    return _SchedulerRuntime(engine, None, True)


def _start_scheduler(app: Any) -> _SchedulerRuntime:
    """Start the scheduler engine and hang it on ``app.state``.

    A second call on an app that already carries an engine does **not**
    start a second worker: two workers are two schedulers, and "at any
    moment there is exactly one scheduler" is an invariant rather than
    a preference.
    """
    existing = getattr(app.state, "scheduler_engine", None)
    if existing is not None:
        logger.warning(
            "[scheduler] already running; no second worker is started"
        )
        return _SchedulerRuntime(existing, None, False)

    try:
        runtime = _build_scheduler_engine()
    except Exception:
        # The builder logs and cleans up after itself; this guard is
        # for the case where it could not even be called.
        logger.exception(
            "[scheduler] the engine could not be started; continuing "
            "without it"
        )
        return _SchedulerRuntime(None, None, False)

    app.state.scheduler_engine = runtime.engine
    return runtime


def _stop_scheduler_engine(runtime: _SchedulerRuntime) -> None:
    """Stop the worker, join it, and give the connection back.

    The order is the contract: stopping is a request and joining is the
    guarantee, and only a thread that has finished may be followed by
    the teardown of what it was using.
    """
    engine = runtime.engine
    if engine is None:
        return
    thread = getattr(engine, "thread", None)
    try:
        engine.stop(timeout=_SCHEDULER_JOIN_TIMEOUT)
    except Exception:  # noqa: BLE001 — the teardown must not raise
        logger.exception("[scheduler] the engine did not stop cleanly")
    if thread is not None:
        try:
            thread.join(_SCHEDULER_JOIN_TIMEOUT)
        except Exception:  # noqa: BLE001 — the teardown must not raise
            logger.exception("[scheduler] the engine thread could not be joined")
    if runtime.conn is not None:
        try:
            runtime.conn.close()
        except Exception:  # noqa: BLE001 — the teardown must not raise
            logger.exception(
                "[scheduler] the engine's connection could not be closed"
            )


def _orphan_reaper_may_act() -> bool:
    """Whether this process may let the reaper act on the machine.

    The reaper looks at every process on the box and signals the ones it
    judges to be orphans.  A **test** process must not do that: the suite
    boots the app — the lifespan tests enter ``_lifespan`` directly, and
    the state tests enter it through ``TestClient`` — and a boot that
    reaped the developer's own processes, or left a detached daemon
    behind, would be a test suite with side effects on the machine it
    runs on.

    ``PDT_ORPHAN_REAPER=0`` is the deployment's answer to the same
    question ("this machine is not the reaper's to touch").  This is the
    test suite's answer, and it is asked here — at the point where the
    lifespan decides to act — rather than in the environment, so that
    the decision is visible in the same file as the decision itself.

    The same signal guards the live state database in
    ``state_machine/db/connection.py``: outside pytest the backend and
    the executor open it legitimately.
    """
    return not os.environ.get("PYTEST_CURRENT_TEST")


def _install_orphan_reaper_marks() -> Optional[Dict[str, str]]:
    """Mark this process as the owner of whatever it spawns.

    The backend is the process that spawns everything else PDT runs —
    executors, sub-agents, test runs.  Writing the owner marker into
    this process' own ``os.environ`` means every one of those children
    inherits it, so a spawn point that was never taught about the reaper
    still leaves a traceable child behind.  A child that outlives the
    backend is then recognisable as an orphan instead of being
    indistinguishable from an unrelated process.

    Three rules make this safe to call from the lifespan:

    * **Never at import time.**  The marker is written here, in the
      lifespan, and not by the module body: a marker written on import
      would mark every process the importer spawns.
    * **Never fatally.**  A reaper that cannot be installed costs the
      backend its marker and nothing else — the boot continues.
    * **Idempotently.**  Calling it twice writes the same marks.

    A reaper that is switched off (``PDT_ORPHAN_REAPER=0``) writes
    nothing at all: a deployment that turned the reaper off must not
    get a marked environment anyway.
    """
    if orphan_reaper is None:
        logger.warning(
            "[orphan_reaper] unavailable; the owner marker was not installed"
        )
        return None

    try:
        config = orphan_reaper.config_from_env()
    except Exception:
        logger.warning(
            "[orphan_reaper] could not read its configuration; "
            "the owner marker was not installed",
            exc_info=True,
        )
        return None

    if not getattr(config, "enabled", True):
        return None

    try:
        return orphan_reaper.install_owner_marks()
    except Exception:
        logger.warning(
            "[orphan_reaper] could not install the owner marker; continuing",
            exc_info=True,
        )
        return None


def _start_orphan_reaper() -> Optional[int]:
    """Scan once for orphans, then make sure the resident reaper is running.

    Two halves, each with a different job:

    * the **scan** answers "what is on this machine right now" — the
      orphans a previous life left behind, which is exactly what a
      backend that boots onto a busy box should reclaim first;
    * the **daemon** answers "what will be on this machine in a minute".
      It is a separate, detached process because it has to outlive the
      backend: a daemon that died with the backend would leave the
      orphans behind again and the next backend would have to notice
      them from scratch.

    Neither half may cost the backend its boot.  A scan that raises
    costs the scan; the daemon is still started, because the daemon is
    the part that keeps working after this backend is gone.  A daemon
    that cannot be started costs the daemon and nothing else.

    A reaper that is switched off does nothing at all.
    """
    if orphan_reaper is None:
        logger.warning("[orphan_reaper] unavailable; the startup scan is skipped")
        return None

    try:
        config = orphan_reaper.config_from_env()
    except Exception:
        logger.warning(
            "[orphan_reaper] could not read its configuration; "
            "the startup scan is skipped",
            exc_info=True,
        )
        return None

    if not getattr(config, "enabled", True):
        return None

    try:
        orphan_reaper.run_once(config)
    except Exception:
        logger.warning(
            "[orphan_reaper] the startup scan failed; continuing",
            exc_info=True,
        )

    try:
        return orphan_reaper.ensure_daemon(config)
    except Exception:
        logger.warning(
            "[orphan_reaper] could not start the daemon; continuing",
            exc_info=True,
        )
        return None


def _run_boot_sweeps() -> None:
    """The two boot-time sweeps, as one unit of work.

    Both of them are housekeeping.  They clean up after a *previous*
    life of the backend — the services a plan declared but never reaped,
    and the payloads that still hold a live credential — and neither of
    them is on the readiness path.

    That is the whole reason they live in their own function (and, one
    level up, on their own thread): the credential sweep shells out to
    ``ps`` and waits for it, which took 18-21 s on a machine with many
    processes, and uvicorn binds the socket only after the lifespan
    returns — every one of those seconds was a second in which the API
    did not answer.

    The sweeps below are the ones that used to sit inline in the
    lifespan, unchanged: the log lines, their levels, and the "a failed
    sweep costs the sweep and nothing else" behaviour are all exactly
    what they were.
    """
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
            _swept = _reap_all_plans(
                PLANS_DIR, reason="startup_sweep",
                caller="server.startup_sweep",
            )
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


def _start_boot_sweeps() -> threading.Thread:
    """Start the boot sweeps on a daemon thread and return that thread.

    The thread is a daemon so that a sweep still running at shutdown
    cannot keep the interpreter alive, and it is named so that a thread
    dump says what it is.

    The thread is returned so that a caller *can* wait for it: the boot
    deliberately does not, but a test that entered the lifespan has to
    leave no sweep running behind it.
    """
    thread = threading.Thread(
        target=_run_boot_sweeps,
        name="boot-sweeps",
        daemon=True,
    )
    thread.start()
    return thread


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
    # 2026-10-27 (T8): the orphan reaper's owner marker — first thing
    # the boot does, before anything that could spawn a child.
    #
    # The marker lives in this process' own environment, so every child
    # the backend spawns inherits it.  It has to be in place before the
    # first spawn happens, or the children spawned before this line
    # would be the only ones the reaper cannot trace.
    #
    # It is deliberately *not* written at import time: a marker written
    # by the module body would mark every process the importer spawns.
    if _orphan_reaper_may_act():
        _install_orphan_reaper_marks()

    # The request guard's self-check, before this instance serves
    # anything.  uvicorn runs the lifespan BEFORE it binds the socket,
    # so a guard that does not refuse stops the boot right here: the
    # port is never opened, and no browser page can reach an API that
    # answers without the guard.  The second half of the check — the
    # same question asked of the running instance on its own address —
    # lives next to the middleware; see ``_start_request_guard_selfcheck``.
    #
    # The question is asked of **the app this module serves** — the one
    # uvicorn is given in ``__main__`` — and not of the handle the
    # lifespan was handed: that is the app whose stack an outside
    # request has to pass, and in every real deployment the two are the
    # same object.
    _guard_ok, _guard_detail = await _request_guard_selfcheck_async()
    if not _guard_ok:
        logger.error(
            "%s",
            _request_guard_failure_line(
                _SERVER_BOUND_PORT, REQUEST_GUARD_SELFCHECK_PATH,
                _guard_detail,
            ),
        )
        raise RuntimeError(
            "the request guard is not in force on this instance; refusing "
            f"to serve ({_guard_detail})"
        )

    # 2026-10-25 (R10): startup phase migration — ``ready`` → ``queued``.
    #
    # ``queued`` is the only waiting phase now (see ``phase_vocabulary``),
    # but the data on disk may still carry the legacy word.  A plan parked
    # on ``ready`` is a hole: the scheduler only picks up ``queued``, and
    # ``ready`` shows up in no "waiting on someone" mental model either —
    # it is neither running nor waiting.
    #
    # The migration hangs off the startup hook on purpose: it has to reach
    # the plans that already exist, not only the ones created from now on.
    # It runs *before* ``RuntimeState`` is built so that everything the
    # app does afterwards reads the converged data.
    #
    # A failed migration must NOT keep the backend from starting: this is a
    # data sweep, and a sweep that could not finish this round is retried
    # on the next boot (it converges on the values it finds, it does not
    # repeat what it already did).  So the exception is logged and the boot
    # continues.
    try:
        _phase_counts = migrate_ready_to_queued(
            _state_db_path(), PLANS_DIR, logger=logger
        )
        logger.warning(
            "[phase_migration] rows_migrated=%d state_files_migrated=%d",
            _phase_counts.get("rows_migrated", 0),
            _phase_counts.get("state_files_migrated", 0),
        )
    except Exception:
        logger.exception(
            "[phase_migration] startup migration failed; continuing boot"
        )

    # 2026-09-06: TC-006 dependency-injection refactor — the lifespan
    # is the canonical constructor for ``RuntimeState``. The three
    # orthogonal collaborators (``config``, ``in_flight_guard``,
    # ``dynamic_tracker``) are owned here and
    # injected onto ``app.state.runtime_state`` so request handlers
    # can read / mutate the handle without re-importing the loader
    # modules or threading through global singletons. A previous
    # revision exposed a module-level ``RUNTIME_STATE`` singleton;
    # that singleton was removed by the refactor — the only handle
    # on the live state is now ``app.state.runtime_state``.
    backend_config = _load_backend_config_yaml()
    runtime_state = RuntimeState(
        config=backend_config,
        in_flight_guard=InFlightFileGuard(),
        dynamic_tracker=ActiveConcurrencyTracker(),
    )
    app.state.runtime_state = runtime_state

    # 2026-10-29: the two boot sweeps — the ledger sweep and the
    # credential-residue sweep — run on their own thread now.
    #
    # They are housekeeping: they clean up after a previous life of the
    # backend, and the next boot finishes whatever this one did not get
    # to.  Nothing about serving an API depends on them, and the
    # credential sweep waits on ``ps``, which held the boot for 18-21 s
    # on a busy machine — uvicorn binds the socket only after the
    # lifespan returns, so every one of those seconds was a second in
    # which the API did not answer.
    _start_boot_sweeps()

    # 2026-10-27 (T8): the orphan reaper's own startup pass, and the
    # resident daemon that keeps it running.
    #
    # The sweeps are concurrent now, so the reaper and the ledger sweep
    # no longer have an order between them.  They do not need one: the
    # ledger layer stops the processes a plan declared and can still
    # identify, the reaper is the *bottom* layer underneath it and
    # reclaims the processes nobody declared any more, and each of them
    # judges by its own record — the ledger, or the owner marker.  Both
    # kill by process identity, and a kill is idempotent, so the two
    # layers running at the same time cannot make either of them kill
    # the same process twice.
    if _orphan_reaper_may_act():
        _start_orphan_reaper()

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
    # forward.  Rows at verification_running whose
    # plan_verification row is already terminal are invisible to
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

    # 2026-10-28: the PDT-native scheduler (architecture decision
    # point 1).  The engine is built **here**, after the phase migration
    # and the recovery of the plans that were running: its first tick
    # decides on the plan facts, and facts that have not been recovered
    # yet are facts the scheduler would decide against — a plan that was
    # running a moment ago and is not in the facts yet is a plan the
    # scheduler would start a second time.
    #
    # It is built last on purpose.  Everything above can abort the
    # boot, and an engine that was started before the abort would leave
    # a worker thread behind that nothing would ever stop.
    scheduler = _start_scheduler(app)

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
        # The scheduler stops **before** the executions are killed: a
        # worker that is still ticking would start a plan into a world
        # that is being torn down.  Stopping is a request and joining
        # is the guarantee, so the application state is only taken
        # apart once the thread has finished.  A lifespan that did not
        # start the worker does not stop it either — it is not its
        # worker to stop.
        if scheduler.owned:
            _stop_scheduler_engine(scheduler)
            app.state.scheduler_engine = None
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


# ---------------------------------------------------------------------------
# The guard's self-check — "the guard is in force", proved at runtime
# ---------------------------------------------------------------------------
#
# The middleware above is the reason a page from another origin cannot
# drive this API.  A test can prove that the middleware *refuses* a
# request; it cannot prove that the instance an operator actually
# reaches on a port refuses one — that instance is a separate process,
# and it is the one a browser can reach.  The two came apart in the
# round this code belongs to: the in-process check passed, and the
# instance that was serving did not carry the guard.
#
# So the instance asks itself, at the two moments where the answer is
# still worth something:
#
#   * **before it serves anything.**  The lifespan runs before uvicorn
#     binds the socket, so a check there that finds the guard missing
#     stops the boot: the port is never opened, and the instance never
#     serves an API that a browser page could drive.
#   * **on the address it listens on.**  Right after uvicorn has bound
#     the port, the same question is asked over real HTTP, to the real
#     port.  Nothing but the running instance can answer that one, so
#     it is the check that cannot be satisfied by code that merely
#     *looks* right.
#
# Both answers are recorded in :data:`_REQUEST_GUARD_SELFCHECK` and
# published on ``/health/data`` as ``request_guard``, so a reader can
# tell "this instance enforces the guard" from "somebody else answered
# on this port".

#: The path the self-check asks.  It is under the guarded prefix, it
#: always exists, and it touches nothing: a liveness answer, so a
#: failing check can only ever mean "the guard let a request through".
REQUEST_GUARD_SELFCHECK_PATH = "/api/health"

#: The path the self-check asks **outside** the guarded prefix.  The
#: guard has to refuse the API and leave the UI alone, so the check asks
#: both questions — a guard that swallowed the static assets would
#: answer this one with a 403 too, and the record of the check would say
#: so.
REQUEST_GUARD_UNGUARDED_PROBE_PATH = "/health"

#: What this process knows about its own guard.  ``status`` is one of:
#:
#:   * ``"enforced"`` — the self-check passed;
#:   * ``"failed"`` — the self-check ran and the guard did not refuse;
#:   * ``"unchecked"`` — no self-check has run in this process (an
#:     application that was imported rather than started).
#:
#: The rest of the record is what the check **found**: when it ran
#: (``checked_at``), what the guarded path answered without and with the
#: header (``status_without_header`` / ``status_with_header``), and the
#: per-path answers (``paths``).  A verdict on its own says "the guard is
#: in force"; the numbers say *what was asked and what came back*, which
#: is what a reader needs when the answer came from a port they did not
#: start themselves.
_REQUEST_GUARD_SELFCHECK: Dict[str, Any] = {
    "status": "unchecked",
    "detail": None,
    "checked_at": None,
    "status_without_header": None,
    "status_with_header": None,
    "paths": [],
}

#: The address this process serves on, when it is known.  ``__main__``
#: fills it in before uvicorn is started, so a refusal can name the
#: port the instance was about to serve on.
_SERVER_BOUND_HOST: Optional[str] = None
_SERVER_BOUND_PORT: Optional[int] = None


def _judge_guard_probe(
    bare_status: int, bare_body: str, headed_status: int,
) -> Tuple[bool, str]:
    """Decide whether a pair of probe answers proves the guard is in force.

    ``bare_*`` is the answer to a request that carries no guard header;
    ``headed_status`` is the answer to the same request that carries it.

    The two have to be read together.  A 403 on the bare request proves
    the request was refused — but a route that refuses *everything*
    would look exactly the same, and the route being alive is what
    says the refusal was the guard's doing.
    """
    problems: List[str] = []
    if bare_status != 403:
        problems.append(
            f"a request without {request_guard.REQUEST_HEADER} answered "
            f"{bare_status} instead of 403"
        )
    if "forbidden" not in (bare_body or "").lower():
        problems.append(
            f"the refusal body does not carry 'forbidden': "
            f"{(bare_body or '')[:200]!r}"
        )
    if headed_status == 403:
        problems.append(
            f"a request WITH {request_guard.REQUEST_HEADER} was refused too "
            f"(403) — the route under the guard is not being served"
        )
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"a request without {request_guard.REQUEST_HEADER} was refused with "
        f"403 and a 'forbidden' body, and the same request with the header "
        f"was served ({headed_status})"
    )


async def _asgi_probe(
    target_app, path: str, headers: Sequence[Tuple[bytes, bytes]],
) -> Tuple[int, str]:
    """Call *target_app* over ASGI and return ``(status, body)``.

    The application is called the way a server calls it — one ASGI
    request, through the whole middleware stack — so what comes back is
    the answer the middleware gives, not the answer the guard's
    predicate gives on its own.
    """
    scope: Dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        # The Host is loopback on purpose: the guard refuses for two
        # independent reasons, and this check is about the header one.
        "headers": [(b"host", b"127.0.0.1"), *headers],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
        "state": {},
    }
    status = 0
    body_parts: List[bytes] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b"") or b"")

    await target_app(scope, receive, send)
    return status, b"".join(body_parts).decode("utf-8", "replace")


async def _request_guard_selfcheck_async(
    target_app=None,
    path: str = REQUEST_GUARD_SELFCHECK_PATH,
) -> Tuple[bool, str]:
    """Ask this application, over its own stack, whether it refuses.

    ``target_app`` defaults to this module's application.  The check
    asks two questions of the same path: a request without the guard
    header, and the same request with it.
    """
    app_to_ask = target_app if target_app is not None else app
    bare_status, bare_body = await _asgi_probe(app_to_ask, path, ())
    headed_status, _headed_body = await _asgi_probe(
        app_to_ask,
        path,
        ((
            request_guard.REQUEST_HEADER.lower().encode("ascii"),
            request_guard.REQUEST_HEADER_VALUE.encode("ascii"),
        ),),
    )
    ok, detail = _judge_guard_probe(bare_status, bare_body, headed_status)
    paths = await _guard_probe_paths_async(app_to_ask, path)
    _record_request_guard_selfcheck(
        ok, detail,
        status_without_header=bare_status,
        status_with_header=headed_status,
        paths=paths,
    )
    return ok, detail


async def _guard_probe_paths_async(
    target_app, guarded_path: str,
) -> List[Dict[str, Any]]:
    """What each probed path answered, with and without the header.

    The record is what the check *found*, not what it concluded: a
    reader who was not there can see which paths were asked and what
    came back.  The guarded path comes first — it is the one the verdict
    is about — and the unguarded one after it, because a guard that
    swallowed the static assets would answer it with a 403 too.
    """
    records: List[Dict[str, Any]] = []
    for path in (guarded_path, REQUEST_GUARD_UNGUARDED_PROBE_PATH):
        if any(str(entry.get("path")) == path for entry in records):
            continue
        bare_status, _bare_body = await _asgi_probe(target_app, path, ())
        headed_status, _headed_body = await _asgi_probe(
            target_app,
            path,
            ((
                request_guard.REQUEST_HEADER.lower().encode("ascii"),
                request_guard.REQUEST_HEADER_VALUE.encode("ascii"),
            ),),
        )
        records.append({
            "path": path,
            "status_without_header": bare_status,
            "status_with_header": headed_status,
        })
    return records


def _request_guard_selfcheck_on_port(
    host: str,
    port: int,
    path: str = REQUEST_GUARD_SELFCHECK_PATH,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """Ask the instance listening on ``host:port`` whether it refuses.

    This is the check that cannot be satisfied by code that merely
    looks right: the answer comes over a real socket, from the process
    that is serving.
    """
    import http.client

    def _get(probe_path: str, headers: Dict[str, str]) -> Tuple[int, str]:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", probe_path, headers=headers)
            resp = conn.getresponse()
            return int(resp.status), resp.read().decode("utf-8", "replace")
        finally:
            conn.close()

    bare_status, bare_body = _get(path, {})
    headed_status, _headed_body = _get(path, {
        request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
    })
    ok, detail = _judge_guard_probe(bare_status, bare_body, headed_status)
    paths = _guard_probe_paths_on_port(host, port, path, timeout)
    _record_request_guard_selfcheck(
        ok, detail,
        status_without_header=bare_status,
        status_with_header=headed_status,
        paths=paths,
    )
    return ok, detail


def _guard_probe_paths_on_port(
    host: str, port: int, guarded_path: str, timeout: float,
) -> List[Dict[str, Any]]:
    """What each probed path answered on the wire, with and without the header."""
    import http.client

    def _get(probe_path: str, headers: Dict[str, str]) -> int:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", probe_path, headers=headers)
            resp = conn.getresponse()
            resp.read()
            return int(resp.status)
        finally:
            conn.close()

    records: List[Dict[str, Any]] = []
    for probe_path in (guarded_path, REQUEST_GUARD_UNGUARDED_PROBE_PATH):
        if any(str(entry.get("path")) == probe_path for entry in records):
            continue
        records.append({
            "path": probe_path,
            "status_without_header": _get(probe_path, {}),
            "status_with_header": _get(probe_path, {
                request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
            }),
        })
    return records


def _record_request_guard_selfcheck(
    ok: bool,
    detail: str,
    *,
    status_without_header: Optional[int] = None,
    status_with_header: Optional[int] = None,
    paths: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Remember what the self-check found, for ``/health/data``.

    The verdict (``status``) is what the round compares; the numbers are
    what lets it check the verdict instead of taking it on trust.
    """
    _REQUEST_GUARD_SELFCHECK["status"] = "enforced" if ok else "failed"
    _REQUEST_GUARD_SELFCHECK["detail"] = detail
    _REQUEST_GUARD_SELFCHECK["checked_at"] = datetime.now(
        timezone.utc,
    ).isoformat()
    _REQUEST_GUARD_SELFCHECK["status_without_header"] = status_without_header
    _REQUEST_GUARD_SELFCHECK["status_with_header"] = status_with_header
    _REQUEST_GUARD_SELFCHECK["paths"] = [
        dict(entry) for entry in (paths or []) if isinstance(entry, dict)
    ]


def _request_guard_detail() -> Dict[str, Any]:
    """The self-check's own record, published for whoever asks.

    This is the evidence the round could not get on its own: the
    instance checked **itself** before it began serving, and this is
    what it found.  A round that reaches a port whose instance has since
    died cannot ask the instance — but it can read this from the
    service-ready event, and it does not have to re-derive the guard's
    behaviour on an address that is already gone.
    """
    record = _REQUEST_GUARD_SELFCHECK
    paths = record.get("paths") or []
    return {
        "status": record.get("status"),
        "detail": record.get("detail"),
        "checked_at": record.get("checked_at"),
        "status_without_header": record.get("status_without_header"),
        "status_with_header": record.get("status_with_header"),
        "paths": [dict(entry) for entry in paths if isinstance(entry, dict)],
    }


def _request_guard_failure_line(
    port: Optional[int], path: str, detail: str,
) -> str:
    """The one line an operator reads when the guard is not in force."""
    where = f"port={port}" if port is not None else "port=unknown"
    return f"request_guard_selfcheck_failed{{{where}, path={path}}} {detail}"


def _connect_host(host: str) -> str:
    """The address to connect to for a server bound to *host*."""
    return host if host not in ("0.0.0.0", "::", "") else "127.0.0.1"


def _start_request_guard_selfcheck(
    instance,
    host: str,
    port: int,
    path: str = REQUEST_GUARD_SELFCHECK_PATH,
    timeout: float = 60.0,
) -> threading.Thread:
    """Ask the running instance, on its own address, whether it refuses.

    The question can only be answered by something outside the serving
    loop, so the check runs in its own thread: it waits for the socket
    to be listening, asks the instance over real HTTP, and — if the
    guard does not refuse — stops the instance instead of letting it
    serve an API a browser page could drive.
    """

    def _run() -> None:
        deadline = time.monotonic() + timeout
        while not instance.started and time.monotonic() < deadline:
            if instance.should_exit:
                # The server is going down for another reason; there is
                # nothing left to ask.
                return
            time.sleep(0.05)
        if not instance.started:
            detail = (
                f"the server never started listening on {host}:{port} "
                f"within {timeout:g}s"
            )
            _record_request_guard_selfcheck(False, detail)
            logger.error(
                "%s", _request_guard_failure_line(port, path, detail),
            )
            instance.should_exit = True
            return
        try:
            ok, detail = _request_guard_selfcheck_on_port(
                _connect_host(host), port, path,
            )
        except Exception as exc:  # noqa: BLE001 - a probe that cannot run is a failure
            ok, detail = False, f"the probe raised {type(exc).__name__}: {exc}"
            _record_request_guard_selfcheck(False, detail)
        if not ok:
            logger.error(
                "%s", _request_guard_failure_line(port, path, detail),
            )
            instance.should_exit = True

    thread = threading.Thread(
        target=_run, name="pdt-request-guard-selfcheck", daemon=True,
    )
    thread.start()
    return thread


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


# ---------------------------------------------------------------------------
# 2026-10-25 — the instance block: WHICH process answered
# ---------------------------------------------------------------------------
#
# ``/health/data`` used to answer with exactly one fact about the data
# plane: how many ``plan_routing`` rows it could read.  That is enough
# to tell a broken database from a working one, and NOT enough to tell
# whose answer it is.
#
# The 2026-10-24 round found that difference the hard way.  A probe
# reached a backend on a busy port; that backend answered 200 with
# ``status: ok`` and an EMPTY routing table; and the round could not
# say whether the answer came from its own instance or from the
# stranger that had taken the port.  "Some instance answered" and "the
# round's instance answered" were the same shape on the wire.
#
# So the probe answers the identity question too, in an ``instance``
# block — a fact sheet about the process that produced the answer:
#
#   * ``boot_id`` — minted once per process, never changes.  Two
#     answers carrying the same boot id came from the same process;
#     two answers carrying different ones did not.
#   * ``pid`` / ``pgid`` — the process and its process group.
#   * ``cwd`` — the directory the process was started from.
#   * ``started_at`` — when the process started (ISO-8601, UTC).
#   * ``project_dir`` — the repository root this process belongs to.
#     Not the plans tree: ``PDT_PLANS_DIR`` moves that one, and a field
#     that moved with it would name a configuration, not a workspace.
#   * ``plans_dir`` — the plans root this process is serving.
#   * ``state_db_path`` / ``state_db_inode`` — the database this
#     process reads, and the inode of that file.  The inode matters:
#     a DIFFERENT file at the same path is a different data plane.
#   * ``plan_routing_rows`` / ``fixture_plan_ids`` — what the process
#     found in that database: the row count, and the plan ids behind
#     it (the round's fixture, when the round seeded one).
#
# Comparing ``plans_dir`` and ``state_db_path`` against the ones the
# round configured is what answers "is this the round's instance?" —
# and the ids explain an empty data plane ("this instance serves
# another tree") instead of leaving it as a mystery count.
#
# Every field is a fact about THIS process; none of it is derived from
# the request, so two answers can be compared field by field.

#: The identity of THIS process, minted once when the module is
#: imported and never changed afterwards.
#:
#: It is deliberately NOT the ``pdt_server_boot_id`` counter the
#: startup path writes next to the state database: that counter is
#: shared by every process that uses the same database directory, so
#: two instances running side by side would report the same number.
_INSTANCE_BOOT_ID = uuid.uuid4().hex

#: When the module was imported — the fallback for ``started_at`` on a
#: host where the real process start time cannot be read.
_PROCESS_IMPORT_EPOCH = time.time()


def _process_started_at() -> str:
    """When this process started, as an ISO-8601 UTC stamp.

    The process start time is a fact about the OS, not about the
    request, and it is what a reader compares against another
    instance's claim.  ``psutil`` is a dev dependency only, so the
    import-time stamp is the fallback for a runtime without it.
    """
    started = None
    try:
        import psutil

        started = float(psutil.Process().create_time())
    except Exception:  # noqa: BLE001 - the fallback is good enough
        started = None
    if started is None:
        started = _PROCESS_IMPORT_EPOCH
    return datetime.fromtimestamp(started, tz=timezone.utc).isoformat()


#: The process facts, computed once at startup.
_PROCESS_STARTED_AT = _process_started_at()
_PROCESS_PID = os.getpid()
_PROCESS_PGID = os.getpgid(0) if hasattr(os, "getpgid") else None
_PROCESS_CWD = os.getcwd()

#: How many plan ids the instance block lists.  The round's fixture is
#: a handful of plans; a production-sized routing table is not, and
#: this probe runs before EVERY api_test VP — so the list is capped and
#: the row count beside it stays the complete number.
_INSTANCE_PLAN_IDS_LIMIT = 100


def _instance_identity(
    db_path: Optional[Path] = None,
    *,
    routing_rows: Optional[int] = None,
    fixture_plan_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The fact sheet about the process that is answering.

    ``db_path`` is the database the caller is reading; when it is not
    given the path is resolved here, and a resolution failure (the
    live-database guard under pytest) leaves it unknown rather than
    turning the probe into a 500.

    The paths are read when the answer is written, because they are the
    configuration this process is serving RIGHT NOW — a test, and an
    operator, can point a running process at another tree, and the
    answer has to name the tree in use rather than the one from
    startup.  The process facts beside them (boot id, pid, group, cwd,
    start time) are frozen at startup because they describe the
    process, not its configuration.

    2026-10-28: the block publishes ``project_dir`` beside
    ``plans_dir``.  ``plans_dir`` names the plans TREE this process
    reads and writes, and it is relocatable — ``PDT_PLANS_DIR`` moves
    it, and so does the per-test fixture.  It therefore cannot answer
    "which checkout is this?", which is the question two answers to
    ``/health/data`` are compared on.  ``project_dir`` answers it: the
    repository root this process belongs to, anchored on
    :data:`config_paths.PROJECT_ROOT`, which is resolved once at import
    time and is NOT affected by the plans-dir override.
    """
    if db_path is None:
        try:
            db_path = _state_db_path()
        except Exception:  # noqa: BLE001 - a probe must never 500
            db_path = None
    inode: Optional[int] = None
    if db_path is not None:
        try:
            inode = int(db_path.stat().st_ino)
        except OSError:
            inode = None
    return {
        "boot_id": _INSTANCE_BOOT_ID,
        "pid": _PROCESS_PID,
        "pgid": _PROCESS_PGID,
        "cwd": _PROCESS_CWD,
        "started_at": _PROCESS_STARTED_AT,
        "project_dir": str(Path(PROJECT_ROOT).resolve()),
        "plans_dir": str(Path(PLANS_DIR)),
        "state_db_path": str(db_path) if db_path is not None else None,
        "state_db_inode": inode,
        "plan_routing_rows": routing_rows,
        "fixture_plan_ids": fixture_plan_ids,
    }


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

    2026-10-25: the answer also carries an ``instance`` block — the
    fact sheet about the process that answered, so a reader can tell
    "this instance" from "somebody else's instance on the same port"
    (see :func:`_instance_identity`).  The block is purely additive:
    ``status``, ``plan_routing_rows`` and ``reason`` keep exactly the
    meaning they had.

    2026-10-26: the answer carries the guard's own verdict as
    ``request_guard`` — ``"enforced"`` when this process checked
    itself and found the guard refusing, ``"failed"`` when the check
    ran and the guard did not refuse, ``"unchecked"`` when no check
    has run here (an application that was imported rather than
    started).  It is the field a reader compares when the answer came
    from a port they did not start themselves: an instance that
    publishes ``"enforced"`` is one that proved it; one that does not
    publish the field at all is somebody else.

    The verdict is not the whole of the record, so the answer carries
    the record too: ``request_guard_detail`` says when the check ran
    (``checked_at``), which paths it asked (``paths``), and what each of
    them answered with and without the header
    (``status_without_header`` / ``status_with_header``).  This is the
    evidence the round could not get on its own — the instance checked
    **itself** before it began serving, and this is what it found.  A
    round that reaches a port whose instance has since died cannot ask
    the instance any more, so the record has to be published while the
    instance is still there.
    """
    db_path: Optional[Path] = None
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate as _migrate
        db_path = _state_db_path()
        if not db_path.exists():
            # Fresh install — no state.db yet is legitimate, not a fault.
            return {
                "status": "ok",
                "plan_routing_rows": 0,
                "request_guard": _REQUEST_GUARD_SELFCHECK["status"],
                "request_guard_detail": _request_guard_detail(),
                "instance": _instance_identity(
                    db_path, routing_rows=0, fixture_plan_ids=[],
                ),
            }
        conn = open_db(db_path)
        try:
            _migrate(conn)
            row = conn.execute("SELECT COUNT(*) FROM plan_routing").fetchone()
            plan_ids = [
                str(r[0])
                for r in conn.execute(
                    "SELECT plan_id FROM plan_routing ORDER BY plan_id LIMIT ?",
                    (_INSTANCE_PLAN_IDS_LIMIT,),
                ).fetchall()
            ]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a probe must never 500
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": f"{type(exc).__name__}: {exc}",
                # The data plane is broken, so the counts are unknown —
                # but WHO is broken is exactly what a reader needs now.
                "request_guard": _REQUEST_GUARD_SELFCHECK["status"],
                "request_guard_detail": _request_guard_detail(),
                "instance": _instance_identity(
                    db_path, routing_rows=None, fixture_plan_ids=None,
                ),
            },
        )
    routing_rows = row[0] if row else 0
    return {
        "status": "ok",
        "plan_routing_rows": routing_rows,
        "request_guard": _REQUEST_GUARD_SELFCHECK["status"],
        "request_guard_detail": _request_guard_detail(),
        "instance": _instance_identity(
            db_path, routing_rows=routing_rows, fixture_plan_ids=plan_ids,
        ),
    }


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
    #: DEPRECATED — accepted, ignored, and on its way out.
    #:
    #: This field used to set how many verification points a round ran
    #: at once. It no longer does: nothing does. The verification round
    #: has no framework-level width, and how many verification points
    #: run together is decided per dispatch by the provider slot pool
    #: (``acquire_provider_with_dynamic_capacity``), so there is nothing
    #: left for a request body to pin. A body that still sends it keeps
    #: being accepted rather than 422-ing, because a stored curl/script
    #: from before the change should not break a running plan — but the
    #: value is not read.
    #:
    #: No producer existed in this repo even when it *was* read (no
    #: frontend, script, or test sends it), so dropping it is a
    #: compatibility question about operators outside the repo, not
    #: about callers inside it.
    max_parallel: Optional[int] = 1


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
        clear_root_cause_chain: also delete the root-cause chain's
            ledger (``verification_root_cause_chain.json``) and the
            reports it names (``vps[*].report_path``).

            2026-10-30: the chain outlived every other per-round
            artefact. A reset clears the verdicts, the repair tasks and
            the round's disk outputs, but the ledger stayed — so the
            next round still saw the old ``BLOCKED_ON_USER`` entry and
            the point stayed occupied by a chain nobody had re-examined.
            The report is the other half: the ledger says the diagnosis
            was delivered, and the reader judges that by whether the
            report file is there.

            Off by default: clearing the chain is a **decision** (the
            chain is the plan's own history of where each point's root
            cause stood), not a side effect of a reset. When it is on,
            only the files the ledger names are deleted — the reports
            directory is never globbed.
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
    clear_root_cause_chain: bool = False
    supersede_repair_tasks: bool = True
    restart_at_round: int = Field(default=1, ge=1, le=1000)


class ResetRoundsRequest(BaseModel):
    """Operator payload for ``POST /api/verification/{plan_id}/reset_rounds``.

    2026-09-16 — the endpoint was renamed from
    ``reset_max_rounds`` because that name described the *original*
    behaviour, not the current one: a name that says "max rounds" tells
    the reader this rewrites the budget when it resets the counter.

    The rename follows the 2026-09-14 semantic inversion. The decision
    behind it: the cap is not what this endpoint is for. The budget is
    set when the plan is created and is not meant to move afterwards;
    the counter is the part that legitimately needs to be rewound, and
    only with the operator's authorization.

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
    # 2026-10-24: ``waiting_for_quota`` joined the vocabulary — a plan
    # parked because every provider is out of capacity. The two
    # execution-phase Literals (``status`` here and ``execution_status``
    # in ``ExecutionProgressResponse``) MUST grow together: this model
    # is the ``response_model`` of
    # ``GET /api/execution/{plan_id}/status``
    # (backend/routes/execution.py), so a value that is not in the
    # Literal makes the endpoint raise ``ResponseValidationError`` → 500
    # on every poll. Same class of bug as the 2026-09-14 ``stop_reason``
    # Literal incident.
    status: Literal[
        "running", "completed", "failed", "stopped", "not_started",
        "waiting_for_quota",
    ]
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
    # The pause half of the answer (2026-10-20). ``status`` alone
    # cannot tell a plan that finished from one that was interrupted
    # and is waiting to be resumed — both are ``stopped`` on disk, and
    # after a restart neither is in ``_execution_state`` at all. These
    # three fields are read from the plan's checkpoint file, which is
    # the only part of that record that survives the restart.
    #
    # Defaults, not required fields: every other construction site of
    # this model (notably ``POST /start``, which returns a four-key
    # dict) must keep working without a pause to report.
    paused: bool = False
    paused_at: Optional[str] = None
    interrupted_task_count: int = 0


class ExecutionProgressResponse(BaseModel):
    plan_id: str
    pid: Optional[int] = None
    project_dir: str
    execution_status: Literal[
        "running", "completed", "failed", "stopped", "not_started",
        "waiting_for_quota",
    ]
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
#:
#: ``voided`` (2026-10-16) joins the set because a voided task is exactly
#: as unschedulable as a completed one: its premise was revoked, so there
#: is nothing left to run. It must be here or the summary endpoint
#: hydrates a voided DB-only row back into the plan's task list as work.
#: It is NOT in ``_STATUS_TASK_KEYS`` above — voiding is not a phase the
#: plan passes through, so giving it a bucket would make the status object
#: claim the plan spent a phase it never spent.
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "skipped", "superseded", "voided"}
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
#: * ``queued`` — 2026-09-15: a plan the operator
#:   handed to the scheduler. Extracted to a module constant so the
#:   contract is testable without standing up the endpoint's subprocess.
#:   2026-10-25 (R10): ``queued`` is now the **only** waiting phase —
#:   ``ready`` used to mean "tasks are generated, waiting for a human"
#:   and the write path no longer produces it (see
#:   :mod:`phase_vocabulary`). It was removed from this whitelist
#:   because a whitelist entry the write path cannot produce is a door
#:   to rows nobody can create — and a start that CASes from ``ready``
#:   would move the row to ``executing`` while the rest of the system
#:   (the scheduler, the plan list) treats ``queued`` as the only
#:   waiting phase. Legacy rows still sitting at ``ready`` are
#:   migrated to ``queued`` at startup (:mod:`phase_migration`); a row
#:   that reaches the CAS at ``ready`` anyway is refused with 409.
#: * ``failed`` / ``stopped`` / ``verification_failed`` /
#:   ``verification_loop_stopped`` — re-entry after a round that did not
#:   pass. These four were a single routing value (``terminal_failed``)
#:   before schema v5 collapsed the two vocabularies; they are listed
#:   individually here because the phase vocabulary DOES distinguish
#:   them, and the pre-v5 gate accepted all four.
#: * ``completed`` / ``verification_passed`` — likewise the single old
#:   ``terminal_done``.
#: * ``waiting_for_quota`` — 2026-10-24: a plan parked because every
#:   provider was out of capacity. It still has work left, so once
#:   capacity returns the operator (or the scheduler) has to be able to
#:   start it again from here — including after a backend restart.
_EXECUTION_START_SOURCE_PHASES = (
    "queued",
    "failed",
    "stopped",
    "verification_failed",
    "verification_loop_stopped",
    "completed",
    "verification_passed",
    "waiting_for_quota",
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


# Grace window between SIGTERM and the SIGKILL escalation in
# ``_kill_process_tree``.  Hard-coded before, so a deployment that wanted
# a shorter stop could only edit source; the env var is the config entry
# point.  Read per call (not at import time) so tests and a restarted
# deployment see the same value the operator configured.
_STOP_GRACE_ENV = "PDT_STOP_GRACE_SECONDS"
_STOP_GRACE_DEFAULT_SECONDS = 10.0


def _resolve_stop_grace_seconds(grace_seconds: Optional[float]) -> float:
    """Resolve how long to wait after SIGTERM before escalating to SIGKILL.

    Priority: explicit argument > ``PDT_STOP_GRACE_SECONDS`` > 10s.
    A non-numeric or negative value at *either* level falls back to the
    default rather than to the next level down: an invalid explicit
    argument means the caller got its own value wrong, and silently
    inheriting the env var would hide that.  More importantly, a negative
    grace would mean "don't wait at all" — i.e. skipping SIGTERM's grace
    entirely, the opposite of the contract this function exists to
    enforce — so it must never reach ``proc.wait()``.
    """
    if grace_seconds is not None:
        try:
            explicit = float(grace_seconds)
        except (TypeError, ValueError):
            return _STOP_GRACE_DEFAULT_SECONDS
        # NaN fails the >= 0 comparison, so it falls back too.
        return explicit if explicit >= 0 else _STOP_GRACE_DEFAULT_SECONDS

    raw_env = os.environ.get(_STOP_GRACE_ENV)
    if raw_env is not None and raw_env.strip():
        try:
            from_env = float(raw_env)
        except ValueError:
            return _STOP_GRACE_DEFAULT_SECONDS
        return from_env if from_env >= 0 else _STOP_GRACE_DEFAULT_SECONDS

    return _STOP_GRACE_DEFAULT_SECONDS


def _execution_plan_dir(proc: object) -> Optional[Path]:
    """The plan directory of the execution that owns *proc*, if any.

    The server knows which plan every running execution belongs to —
    ``_execution_state`` is the map — but ``_kill_process_tree`` is
    handed a bare ``Popen``.  Looking the process up here is what lets
    the kill request land in the right plan's event stream without
    changing the signature every call site already uses.
    """
    for plan_id, state in list(_execution_state.items()):
        if not isinstance(state, dict):
            continue
        if state.get("process") is not proc:
            continue
        try:
            return _plan_dir(str(plan_id))
        except Exception:  # noqa: BLE001 - a bad plan id is not a verdict
            return None
    return None


def _kill_process_tree(
    proc: subprocess.Popen, grace_seconds: Optional[float] = None
) -> bool:
    """Kill an entire process tree by sending SIGTERM to the process group.

    Processes started with ``start_new_session=True`` get their own session
    (and thus process group). Killing the group ensures all descendant
    processes (shells, child commands, etc.) are terminated together.

    The signal goes out through ``service_manager.kill_service``
    (2026-10-26, repair-r1-49), like every other signal aimed at a
    process this backend owns.  That function writes the request down
    **before** the signal goes out — who asked, why, which round — so a
    process that dies under the backend leaves a record on the plan's
    own event stream instead of only a ``received SIGTERM`` on the
    dying process's side.  The 2026-10-26 round could not answer "who
    killed my service"; this is what makes the next one able to.

    ``fallback_to_pids=False``: this path has a process **group** it
    trusts, and a pid it cannot — the recorded pid may have been
    recycled by the time the signal goes out, and signalling a stranger
    is worse than signalling nobody.  The escalation below still reaps
    the child through ``proc.kill()``, which addresses it directly.

    ``grace_seconds`` is how long to wait after SIGTERM before escalating
    to SIGKILL (explicit argument > ``PDT_STOP_GRACE_SECONDS`` > 10s).
    Callers that ignore the return value keep the previous behaviour; the
    bool is for callers that need to distinguish a cooperative shutdown
    from one that had to be forced:

    * ``False`` — the process was already gone, or it exited inside the
      grace window.
    * ``True``  — the grace window expired and SIGKILL was issued. The
      post-kill reap failing does not make this ``False``: the tree *was*
      hard-killed, and a caller acting on this value must hear about it.
    """
    if proc.poll() is not None:
        return False
    grace = _resolve_stop_grace_seconds(grace_seconds)
    try:
        pgid = os.getpgid(proc.pid)
        if _signallable_pgid(pgid) is None:
            pgid = None
    except (ProcessLookupError, PermissionError, OSError):
        # The group is already gone, or we may not signal it. Either way
        # the child still gets the SIGTERM-shaped chance to exit below via
        # proc.wait(); we must not turn a signal failure into "give up".
        pgid = None
    if pgid is not None and _kill_service is not None:
        try:
            _kill_service(
                _ServiceRuntime(
                    name="execution", port=0, pid=proc.pid, pgid=pgid,
                ),
                "server._kill_process_tree",
                "the execution's process tree is being stopped",
                plan_dir=_execution_plan_dir(proc),
                sig=signal.SIGTERM,
                pgid=pgid,
                fallback_to_pids=False,
            )
        except Exception:  # noqa: BLE001 - a kill must never raise
            logger.exception(
                "[service_manager] kill request for pid=%s could not be "
                "recorded", getattr(proc, "pid", "<unknown>"),
            )
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    else:
        return False

    # Grace expired: the child ignored SIGTERM, so escalate.
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        logger.warning(
            "SIGKILL escalation for pid=%s did not reap cleanly; "
            "the process tree may still have descendants",
            getattr(proc, "pid", "<unknown>"),
        )
    return True


def _shutdown_all_executions() -> None:
    """Terminate all running execution and verification subprocesses.

    Called during server shutdown (lifespan finally block) and atexit
    to prevent orphaned processes.

    The checkpoint pass comes **first**, on purpose.  A checkpoint
    written after the kill would describe a plan whose process tree is
    already gone, and a resume reading it would restart work that had in
    fact been torn down mid-flight — so the resume point has to be on
    disk before the first SIGTERM.

    Nothing here may raise.  The shutdown path is the last code to run
    before the process goes away: a checkpoint that cannot be written
    costs one plan its resume point, while an exception would cost the
    shutdown every remaining plan its SIGTERM.  So the writer's own
    failures (it reports them in ``report['failed']``) and a writer that
    raises outright are both logged and stepped over.
    """
    try:
        from shutdown_checkpoint import write_shutdown_checkpoints

        report = write_shutdown_checkpoints(
            _execution_state, _verification_state, plan_dir_for=_plan_dir
        )
        if report["failed"]:
            logger.warning(
                "[shutdown_checkpoint] fell back to crash judgement: %s",
                report["failed"],
            )
    except Exception:
        logger.exception(
            "[shutdown_checkpoint] checkpoint pass failed; shutdown continues"
        )

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
    #
    # ``release_own_owners=True`` (2026-10-24): this process is going
    # away, so the rounds it registered are over and the services they
    # started must not outlive it. Without the release, the owner
    # tokens still name this (live) pid and the sweep would consider
    # those entries the property of a running round — and leave the
    # backend's own services holding their ports after it exited.
    if _reap_all_plans is not None:
        try:
            _reap_all_plans(
                PLANS_DIR, reason="server_shutdown", release_own_owners=True,
                caller="server.shutdown_sweep",
            )
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
    backend.server`` runs the body as ``__main__``, and the provider
    chain reader used to do a lazy ``from server import
    PROVIDER_ORDER_FILE`` *during lifespan startup* — a second,
    independent execution of the body under the name ``server``.

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




























def _new_service_owner_token(prefix: str = "run") -> str:
    """A token identifying one *round* of service ownership (2026-10-24).

    The ledger used to record "which processes the backend started for
    this plan", not "which round started them" — so an older round's
    exit-time reap read the newer round's entries out of the one ledger
    the plan directory has and stopped them. The token is what tells the
    two apart.
    """
    if _new_owner_token is None:
        return ""
    try:
        return _new_owner_token(prefix)
    except Exception:  # noqa: BLE001 - never raise on a cleanup path
        logger.exception("[service_manager] owner token creation failed")
        return ""


def _register_service_owner(
    plan_dir: Path, owner_token: str, *, current: bool = False,
) -> None:
    """Register ``owner_token`` as running for this plan.

    ``current=True`` is for the round that *starts* services: it is the
    round whose ledger entries carry this token. A caller that merely
    reaps must not claim it.
    """
    if _register_owner is None or not owner_token:
        return
    try:
        _register_owner(plan_dir, owner_token, current=current)
    except Exception:  # noqa: BLE001 - never raise on a cleanup path
        logger.exception(
            "[service_manager] owner registration failed for %s", owner_token,
        )


def _release_service_owner(plan_dir: Path, owner_token: str) -> None:
    """Deregister ``owner_token``: the round it names is over."""
    if _release_owner is None or not owner_token:
        return
    try:
        _release_owner(plan_dir, owner_token)
    except Exception:  # noqa: BLE001 - never raise on a cleanup path
        logger.exception(
            "[service_manager] owner release failed for %s", owner_token,
        )


def _reap_managed_services(
    plan_id: str, plan_dir: Path, reason: str, owner_token: Optional[str] = None,
    caller: str = "server._reap_managed_services",
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

    ``owner_token`` names the round doing the reaping (2026-10-24).
    Entries that belong to a *different, still-running* round are left
    alone: that round reaps its own services when it exits. Without the
    token, an older round's exit stopped the api server the newer round
    had just started — 47 seconds before that round's first test asked
    it for ``/health``.

    A caller that passes no token owns nothing, so its reap stops the
    entries whose owner is gone (the orphans a crashed round left) and
    leaves the live rounds' services to their owners. That is the right
    default for the route call sites — a stop or a reset is not the
    owner of a round that is still running. They still get a token of
    their own, so each exit is identifiable in the log.

    ``caller`` names who is doing the reaping. It is written to the
    plan's kill event stream before each signal goes out, so a service
    that dies during a reap says on the record that a reap stopped it
    (2026-10-26, repair-r1-49).
    """
    token = owner_token if owner_token else _new_service_owner_token(reason)
    try:
        if _reap_services is None:
            logger.warning(
                "[service_manager] reap unavailable for plan=%s: "
                "service_manager did not import", plan_id,
            )
            return
        report = _reap_services(
            plan_dir, plan_id, reason=reason, owner_token=token,
            caller=caller,
        )
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


# --- routes/fast_lane.py -----------------------------------------
# Mounted here for the same reason the routers above are: this block
# runs before the frontend catch-all at the bottom of the file, so
# ``POST /api/plan/import`` is reachable rather than swallowed. It is a
# POST and the catch-all is a GET, so the ordering is belt-and-braces
# rather than load-bearing today - it stops being that the moment
# someone adds the GET that reads an import back.
from routes import fast_lane as _routes_fast_lane  # noqa: E402
from routes.fast_lane import (  # noqa: E402,F401
    ImportPlanRequest,
    import_plan,
)

app.include_router(_routes_fast_lane.router)


# --- routes/plan_lifecycle.py -----------------------------------
# The plan-level lifecycle facade: ``POST /api/plan/{id}/lifecycle/stop``
# and ``POST /api/plan/{id}/lifecycle/start``.  It is the door that lets
# an out-of-process caller say "stop this plan" / "start this plan"
# without knowing which layer is currently running - the dispatch itself
# lives in ``plan_lifecycle_service`` and reads this process's live
# state.
#
# Mounted here for the same reason the routers above are: this block
# runs before the frontend catch-all at the bottom of the file.  Both
# routes are POSTs and the catch-all is a GET, so the ordering is
# belt-and-braces rather than load-bearing today - it stops being that
# the moment someone adds the GET that reads a lifecycle state back.
from routes import plan_lifecycle as _routes_plan_lifecycle  # noqa: E402
from routes.plan_lifecycle import (  # noqa: E402,F401
    StartPlanRequest,
    StopPlanRequest,
    start_plan,
    stop_plan,
)

app.include_router(_routes_plan_lifecycle.router)


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


# --- routes/hitl.py -----------------------------------------------
# The human-in-the-loop decision endpoints: ask a human, list what a
# plan is waiting for, answer.
#
# Mounted here for the same reason the routers above are: this block
# runs before the frontend catch-all at the bottom of the file.  For
# the other routers that ordering is belt-and-braces — they are POSTs
# and the catch-all is a GET — but the list endpoint is a GET, so the
# ordering is load-bearing: a route registered after the catch-all
# would never be reached, and the request would be answered with
# ``index.html`` instead of the decision list.
from routes import hitl as _routes_hitl  # noqa: E402
from routes.hitl import (  # noqa: E402,F401
    AnswerDecisionRequest,
    CreateDecisionRequest,
    answer_hitl_decision,
    create_hitl_decision,
    list_hitl_decisions,
)

app.include_router(_routes_hitl.router)


# --- routes/scheduler.py -----------------------------------------
# The scheduler's read model: ``GET /api/scheduler/state``.  It answers
# the one document the operator panel, the LUI and the API all read, so
# that they cannot disagree about what is running and what is waiting.
#
# Mounted here for the same reason the routers above are: this block
# runs before the frontend catch-all at the bottom of the file.  For
# the routers whose endpoints are POSTs that ordering is belt-and-braces
# — but this one is a GET, and the catch-all (``@app.get("/{path:path}")``)
# is a GET too, so the ordering is load-bearing.  Route matching is
# first-match-wins: registered after the catch-all, this route would
# never be reached, and the caller would get the catch-all's answer
# instead — a 404 with the body ``{"detail": "Not found"}``, which reads
# like a missing asset rather than a missing route.
#
# The order is pinned by a test, not by this comment:
# ``tests/api/plan_scheduler/test_scheduler_state_api.py`` asserts that
# this route is registered ahead of the catch-all, so moving this block
# below it turns the suite red rather than turning the endpoint quiet.
from routes import scheduler as _routes_scheduler  # noqa: E402
from routes.scheduler import (  # noqa: E402,F401
    get_scheduler_state,
)

app.include_router(_routes_scheduler.router)


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
    _verification_event_logger,
    _are_all_unfinished_blocked_by_failed_upstream,
    _mark_task_skipped,
    _mark_verification_round_running,
    _plan_next_round,
    _bind_verification_state_conn,
    _release_verification_state_conn,
    _run_repair_execution,
    _run_repair_execution_async,
    # 2026-10-22: the post-repair callback is a module-level factory
    # now, so the ``/start`` repair-resume path and the auto-loop share
    # ONE implementation of the post-repair chain.
    _make_repair_on_complete,
    _persist_verification_terminal,
    _count_unfinished_execution_tasks,
    _rollback_dead_end_to_queued,
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
    from uvicorn.main import STARTUP_FAILURE

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

    # The address this instance serves on, recorded before the server
    # starts so a refusal can name it.
    _SERVER_BOUND_HOST = host
    _SERVER_BOUND_PORT = bound_port

    print(f"[backend server] Starting uvicorn on {host}:{bound_port}")
    # The server is built by hand rather than through ``uvicorn.run`` so
    # that the request guard can be checked against the address this
    # instance actually serves on: the check needs the server object —
    # to know when it is listening, and to stop it if the guard is not
    # in force.  ``uvicorn.run`` does exactly this for the single-worker
    # case, plus its own "the process never came up" exit.
    config = uvicorn.Config(app, host=host, port=bound_port)
    instance = uvicorn.Server(config=config)
    _start_request_guard_selfcheck(instance, host, bound_port)
    try:
        instance.run()
    except KeyboardInterrupt:  # pragma: no cover - the signal path
        pass
    if not instance.started:
        # uvicorn's own verdict: the process never came up (a failed
        # lifespan, a port that could not be bound).
        sys.exit(STARTUP_FAILURE)
    if _REQUEST_GUARD_SELFCHECK["status"] == "failed":
        print(
            "[backend server] FATAL: "
            + _request_guard_failure_line(
                bound_port,
                REQUEST_GUARD_SELFCHECK_PATH,
                _REQUEST_GUARD_SELFCHECK["detail"] or "",
            )
        )
        sys.exit(1)