"""
Coding Tool Interface
==================

Abstract base class for AI coding tools and implementations.
"""

import copy
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import contextvars
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from contextlib import contextmanager

from utils.process import kill_process_group
from utils.secret_files import private_dir, redact_all, write_private_json
# Sub-agent sandbox wrapping (2026-10-06). Dependency-free (stdlib only)
# and it imports nothing from this module, so a top-level import is safe.
import claude_sandbox
# Capacity gating for scene-routed dispatch (2026-09-17). This module is
# dependency-free (stdlib only), so a top-level import carries no cycle
# risk — unlike ``provider_routing`` / ``provider_order``, which pull in
# the SQLite consumer layer and stay lazily imported at the call site.
from dynamic_provider_concurrency import get_shared_tracker
from quota_watchdog import get_shared_quota_watchdog as _shared_quota_watchdog
# Per-plan LLM-call attribution registry (2026-09-21). Pure logging side
# channel — record failures must never break a dispatch, so the registry
# swallows its own errors and we never let them propagate either.
from usage_registry import current_task_id
from usage_registry import record_llm_call as _record_usage_entry
from file_lock_protocol import socket_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reusable sessions for iteration loops
# ---------------------------------------------------------------------------
# An iteration loop — "that answer was unusable, ask again", "here is the
# validator's objection, fix the list" — is one question asked more than
# once. Answered in a fresh session each time, the sub-agent re-reads
# every file the prompt names, and these prompts name files by path
# rather than inlining their bodies, so that work is paid for again on
# every round. Re-opening the previous session keeps it in the
# transcript instead.
#
# The opt-in rides on a context variable rather than on a new keyword
# argument. Every ``query_json`` implementation, and every call site that
# does not want a reused session, stays byte-identical; a caller opts in
# only by routing its loop through :func:`query_json_in_session`. A tool
# with no sessions to re-open — a vendor CLI without a transcript, or a
# test double — simply never reads the variable, so it keeps behaving
# exactly as it does today.
#
# The handle is also what solves the chicken-and-egg a bare session id
# cannot: on the first round the caller has no id to pass, and the id the
# CLI actually used is picked inside the dispatch. The tool writes that
# id back into the handle and the next round reads it out, so a caller
# never has to guess which id is live.


class JsonSession:
    """A resumable JSON-call session, owned by the caller's loop.

    One per loop rather than one per call: the loop's second question is
    the first question again, and this is what makes it so.
    """

    __slots__ = ("session_id",)

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JsonSession(session_id={self.session_id!r})"


#: The session the call in flight should re-open, when its caller asked
#: for one. ``None`` means "fresh session", which is what every existing
#: caller gets and what stays the default.
_JSON_SESSION: "contextvars.ContextVar[Optional[JsonSession]]" = (
    contextvars.ContextVar("coding_json_session", default=None)
)


def query_json_in_session(tool, session: Optional[JsonSession], /, *args, **kwargs) -> dict:
    """``tool.query_json(*args, **kwargs)`` with ``session`` bound for that call.

    Entering with an empty handle opens a fresh session under an id the
    tool pins. Leaving, the handle carries whichever session actually
    produced the reply — including one a provider fallback had to switch
    to, and including a reply that failed to parse, since that session
    exists and re-asking inside it is the point of the loop.

    Arguments are forwarded exactly as written, positionally or by
    keyword, so ``tool`` cannot tell this from a direct call — and a
    ``query_json`` that takes neither ``scene`` nor ``allowed_tools``
    keeps working.
    """
    token = _JSON_SESSION.set(session)
    try:
        return tool.query_json(*args, **kwargs)
    finally:
        _JSON_SESSION.reset(token)


class ApiError(Exception):
    """Raised when the LLM API returns an error (e.g., rate limit, auth failure)."""

    def __init__(self, message: str, status: str = "unknown", retry_after: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        return {
            "type": "api_error",
            "status": self.status,
            "message": str(self),
            "retry_after": self.retry_after,
        }

    def __str__(self) -> str:
        return f"[API_ERROR:{self.status}] {super().__str__()}"


class EmptyResponseError(RuntimeError):
    """The subprocess exited without ever emitting an assistant message.

    Why this is its own type (2026-09-17)
    -------------------------------------
    A clean exit with empty stdout used to be returned as ``""``. The
    JSON callers handed that to ``parse_llm_json``, which raised
    ``JSONDecodeError: no JSON object / array boundaries found`` — a
    *symptom* wearing the costume of a *cause*. A repair generation died
    that way, and the reported error said the model had produced
    unparseable text when in fact nothing had been produced at all.

    Worse, the caller could not tell the two apart, so it always picked
    the same retry: ``--resume <session_id>``. But a session that never
    replied was never created, and resuming a non-existent session fails
    in ~0.4s with ``Invalid session ID`` — the retry was structurally
    guaranteed to fail. Raising a distinct type lets the retry layer
    choose *fresh session* instead, and carries the facts needed to
    actually diagnose the case: exit code, stderr, wall-clock.

    The stderr attribute is the important one — this used to be read
    into ``stderr_data`` and then thrown away on the empty path.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        returncode: Optional[int] = None,
        stderr: str = "",
        elapsed_sec: float = 0.0,
        stdout_len: int = 0,
    ):
        super().__init__(message)
        self.provider = provider
        self.returncode = returncode
        self.stderr = stderr
        self.elapsed_sec = elapsed_sec
        self.stdout_len = stdout_len

    def to_dict(self) -> dict:
        return {
            "type": "empty_response",
            "provider": self.provider,
            "returncode": self.returncode,
            "stderr": self.stderr[:500],
            "elapsed_sec": round(self.elapsed_sec, 1),
            "stdout_len": self.stdout_len,
            "message": str(self),
        }


# The user turn sent when a provider switch re-opens a session the previous
# attempt left unfinished.
#
# ``--resume`` restores the *conversation*; this string is the only new user
# turn that comes with it. Re-sending the original prompt would put the same
# instructions into the session twice — paying for every token of a long task
# prompt a second time and leaving the model to work out which copy is
# current — so the prompt is deliberately absent here and replaced by a
# continuation cue.
#
# The session is normally killed *mid-flight*, so "where you stopped" is a
# real question rather than a courtesy: a tool call may have been issued and
# its effect (a file written, a test run started, a process spawned) is
# invisible to the model. "Check before redoing" is what keeps a resumed agent
# from duplicating side effects it cannot see.
RESUME_CONTINUATION_PROMPT = (
    "Your previous turn in this session was cut off before you finished — "
    "the process running you was terminated mid-task, so the last action you "
    "started may or may not have completed.\n"
    "Continue the task from where you stopped. Do not restart it and do not "
    "redo steps you already completed: for any step whose result is not "
    "visible in this conversation, check its actual current state before "
    "deciding whether it needs to be done again. Then finish the task and "
    "report the outcome."
)

#: Prepended to a re-ask that runs in the session the same caller just
#: used. It is deliberately not the cue above: that one describes a turn
#: cut off mid-task and asks the model to pick the task back up, which is
#: what a provider switch is. A re-ask is a *new* ask about the same
#: subject, carrying whatever changed since — a validator's objection,
#: the reason the last answer was rejected — and that has to travel with
#: the turn or the model re-answers the question it was just asked.
#:
#: The caller's prompt is re-sent here on purpose. What the restored
#: transcript saves is the reading, the greps and the test runs behind
#: that prompt, not the question itself, and those are the expensive part.
RESUME_REASK_PROMPT = (
    "This is a follow-up in the session you have just answered. The files "
    "you read, the commands you ran and their output are all still in this "
    "conversation — build on them rather than starting over.\n\n"
    "Here is the new request:\n\n"
)

# Appended to every JSON-path prompt so a reply stays machine-parseable.
# A resumed turn needs it as much as a first turn does: the restored
# context ends on the previous attempt's own output, so the format
# instruction is not redundant just because the task prompt is.
JSON_ONLY_SUFFIX = (
    "\n\nIMPORTANT: Return ONLY the JSON object requested, no markdown fencing."
)


class SceneProviderUnavailable(RuntimeError):
    """A scene-routed call had no provider the scene's tier table approves.

    Why this exists
    ---------------
    A scene (e.g. ``execution``) names a tier, a tier names regexes over
    CC Switch provider display names, and the tier table is an
    *allowlist*: a provider that no tier names is not approved to serve
    that scene. The scene chain is therefore the exclusive candidate set
    while a scene is in effect.

    The hole this closes: when the chain resolved to nothing usable the
    dispatch used to continue anyway — into ``provider_priority`` (which
    is the whole optimizer chain with no tier filtering), then into the
    constructor's run-level ``base_url`` / ``auth_token``, then into the
    parent process env. Every one of those is a provider the scene never
    approved, and the outcome was invisible: ``current_call_provider``
    was stamped ``"parent"``, so the usage ledger recorded a call whose
    endpoint no ledger column described. A tier table that does not cover
    a provider is a configuration fact, and silently serving the call
    from outside the table turns that fact into an invisible one.

    So the call fails instead, and says why. The attributes exist so a
    caller can distinguish the three ways a chain comes up empty without
    parsing prose:

      * ``reason`` — ``"chain_unresolved"`` (resolution raised),
        ``"no_tier_match"`` (no CC Switch name matches any pattern in the
        scene's tier), ``"all_candidates_skipped"`` (every name was
        excluded, parked in quota cooldown, or unavailable),
        ``"no_capacity"`` (every reachable candidate's local pool has no
        width left at all — shut, parked, or scaled to zero by its
        remaining budget). Note what ``"no_capacity"`` does **not** mean:
        a pool that is merely FULL is queued for rather than failed on,
        because a slot is a lease that comes back on the holder's own
        timeline, so the word covers "nobody here can serve at all" and
        not "everybody here is busy".
      * ``tier`` / ``chains`` — the tiers the scene resolves to.
      * ``candidates`` — what the chain offered, before filtering.

    It is a distinct type rather than a reused ``ApiError`` because no
    HTTP request was ever made: ``ApiError`` means "a provider answered
    and refused", which drives the provider-fallback retry that excludes
    the failed provider and walks to the next one. There is no next
    provider here — the whole candidate set was empty — so that retry
    policy would be answering a question that was never asked. Reusing it
    would also let the dispatch record the failure as a provider error,
    which is precisely the misattribution being removed.
    """

    def __init__(
        self,
        message: str,
        *,
        scene: str,
        reason: str,
        tier: Optional[str] = None,
        chains: Optional[List[List[str]]] = None,
        candidates: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.scene = scene
        self.reason = reason
        self.tier = tier
        self.chains = chains or []
        self.candidates = candidates or []

    def to_dict(self) -> dict:
        return {
            "type": "scene_provider_unavailable",
            "scene": self.scene,
            "reason": self.reason,
            "tier": self.tier,
            "chains": self.chains,
            "candidates": self.candidates,
            "message": str(self),
        }


def _scene_chain_unavailable(
    scene: str,
    diag: dict,
    event_logger: Optional[object] = None,
) -> SceneProviderUnavailable:
    """Build (and log) the fail-closed error for a scene that approved nobody.

    Returns the exception rather than raising it, so the ``raise`` stays
    visible at the call site — the reader of the dispatch path sees where
    the call dies, not just a helper called three blocks earlier.

    Logging goes to both sinks on purpose. The module logger is always
    present, and the structured ``event_logger`` is what the plan's
    execution log and the operator dashboard read; a failure that only
    reaches one of them is invisible to whoever has to act on it. Both
    records carry the same scene / reason / candidates so the two can be
    joined.
    """
    reason = diag.get("reason") or "chain_unresolved"
    tier = diag.get("tier")
    chains = diag.get("chains") or []
    chain = diag.get("chain") or []
    candidates = diag.get("candidates") or []
    tier_txt = f" (tier {tier!r})" if tier else ""

    if reason == "no_tier_match":
        detail = (
            f"no CC Switch provider name matches any pattern in the scene's "
            f"own tier{tier_txt} — the chain is empty by configuration, so "
            f"every provider on this machine is unapproved for this scene"
        )
    elif reason == "all_candidates_skipped":
        detail = (
            f"the scene's chain offered {chain or '[]'} but every candidate "
            f"was excluded, parked in quota cooldown, or unavailable"
        )
    elif reason == "no_capacity":
        detail = (
            f"every reachable candidate {candidates} has no local width "
            f"left at all — shut, parked, or scaled to zero by its "
            f"remaining budget "
            f"({diag.get('waited_sec')}s). A pool that is merely full is "
            f"not this: that is queued for, because a slot comes back "
            f"when the agent holding it finishes"
        )
    elif reason == "chain_unresolved":
        detail = (
            f"resolving the chain raised: {diag.get('error')!r}"
        )
    else:
        detail = f"chain resolution ended as {reason!r}"

    message = (
        f"Scene {scene!r} has no dispatchable provider{tier_txt}: {detail}. "
        f"Not dispatching: with a scene in effect the provider must be one "
        f"the scene's tier table approves, and neither the priority chain, "
        f"nor the run-level provider, nor the parent process environment is "
        f"on that list. Fix the scene/tier mapping in provider_routing.yaml, "
        f"or run this phase without a scene."
    )
    exc = SceneProviderUnavailable(
        message,
        scene=scene,
        reason=reason,
        tier=tier,
        chains=chains,
        candidates=candidates or chain,
    )
    payload = exc.to_dict()
    logger.error("[SCENE PROVIDER UNAVAILABLE] %s", message)
    if event_logger is not None:
        event_logger.error("provider_scene_chain_empty", message, data=payload)
    return exc


class ProviderSlotWaitExpired(RuntimeError):
    """A saturated pool was queued for longer than the slot wait, and the wait ran out.

    What this is NOT
    ----------------
    It is not a failed dispatch, and no caller may record one from it. The
    call never reached a provider: nothing was sent, nothing was refused,
    and no evidence exists that anything is wrong with the work. What
    happened is that every provider this scene approves was at its local
    concurrency cap for longer than ``PROVIDER_SLOT_WAIT_SEC``, and the
    caller would rather have its slot back than keep holding whatever it
    was holding while it sleeps.

    Why a distinct type
    -------------------
    Because the layers above have to treat it as a *signal* rather than a
    verdict, and the only way to guarantee that is for the signal to be
    something they cannot catch by accident. Every generic
    ``except Exception`` between here and the verification gate would
    otherwise turn this into a ``FAILED`` verdict — the one thing this
    ticket exists to prevent. It is a sibling of :class:`RuntimeError`, not
    of :class:`ApiError`, so the provider-fallback machinery does not
    exclude a provider and walk to the next one: no provider failed, and
    excluding the whole tier would make the next attempt no better off.

    Who catches it
    --------------
    The verification fan-out. It releases both capacity gates (the
    ``finally`` already there does it) and puts the verification point back
    on the queue, so a point that could not get a slot is re-queued rather
    than failed. A caller with no such concept sees the exception like any
    other dispatch error, which is strictly better than the unbounded wait
    it replaces.
    """

    def __init__(
        self,
        message: str,
        *,
        scene: str,
        waited_sec: float,
        rounds: int,
        bound_sec: float,
        candidates: Optional[List[str]] = None,
        requested_sec: Optional[float] = None,
    ):
        super().__init__(message)
        self.scene = scene
        #: How long the point was actually asleep holding its gates — the
        #: elapsed time the bound is measured on.
        self.waited_sec = float(waited_sec)
        self.rounds = int(rounds)
        self.bound_sec = float(bound_sec)
        self.candidates = list(candidates or [])
        #: How long the queue asked to wait, which is a different number and
        #: is not what expired. Kept because it is the one that explains the
        #: round count, and because the two diverge whenever a step was cut
        #: short by a wake.
        self.requested_sec = (
            float(requested_sec) if requested_sec is not None else None
        )

    def to_dict(self) -> dict:
        return {
            "type": "provider_slot_wait_expired",
            "scene": self.scene,
            "waited_sec": round(self.waited_sec, 1),
            "requested_sec": (
                round(self.requested_sec, 1)
                if self.requested_sec is not None else None
            ),
            "rounds": self.rounds,
            "bound_sec": self.bound_sec,
            "candidates": self.candidates,
            "message": str(self),
        }


class SettingsTemplateMissing(RuntimeError):
    """The ``--settings`` template is gone AND this instance never read one.

    What happened (observed once)
    ------------------------------
    ``SubagentConfig.write_tmp_settings`` puts the template in a fresh
    ``private_dir()`` (``~/.pdt-scratch/pdt-subagent-*/``). Anything
    that sweeps that root — a boot-time cleaner, another run's TTL, an
    operator's ``rm`` — takes the template with it. The dispatch then
    forwarded the now-dangling path verbatim::

        claude ... --settings /Users/…/pdt-subagent-vw_xtnmf/subagent_settings_<uuid>.json

    The CLI validates ``--settings`` *before* any model call, so it
    died in 0.2s with ``Error: Settings file not found`` on stderr and
    no assistant text — which the read loop could only report as
    ``Claude sub-process exited without assistant text``. That message
    points at the provider and at the model; the fault is a local file
    that was swept. One task died, and four downstream tasks stalled
    behind it.

    Why the raise, rather than "just run without ``--settings``"
    -----------------------------------------------------------------
    Because the template carries the sub-agent's guardrails: the
    ``PreToolUse``/``PostToolUse`` hooks and the ``PDT_*`` env block
    that ``file_lock_broker`` and the Edit/Write containment guard read.
    Starting without it is a sub-agent with no hook and no lock broker,
    running normally — the exact failure this repo already names as the
    worse outcome in ``claude_sandbox.py`` ("a sandbox that quietly
    disables itself is the worse outcome"). A loud error beats an
    invisible, permanent hole in the guardrail, so the missing template
    is reported as an error that names the path it could not find.

    Why ``RuntimeError``, and not ``ApiError`` (this is load-bearing)
    -----------------------------------------------------------------
    The provider-fallback handler in ``_query_internal_m1`` is an
    ``except ApiError``, and on a hit it does three things: excludes the
    provider, calls ``_park_provider_after_error`` to sideline it for a
    cooldown, and re-dispatches with ``_call(excluded=...)``. For this
    fault that is wrong three times over:

      1. The next provider dies identically. The template is gone for
         the whole process, not per provider, so the fallback converts
         one 0.2s corpse into one per provider in the scene chain.
      2. It parks a *healthy* provider. A swept local file is evidence
         of nothing about the endpoint, so the cooldown is a lie that
         outlives the run and pushes later work off a working provider.
      3. It hides the cause. Each hop re-reads as "provider N failed",
         and the one true line — which path was missing — never reaches
         the log the operator is reading.

    So it is a sibling of :class:`RuntimeError`, the same deliberate
    choice as :class:`ProviderSlotWaitExpired`: it must fall through to
    the ``except Exception`` arm that logs and re-raises, with the
    missing path in the message, instead of being converted into a
    provider rotation. ``_run_interactive_resilient`` retries only
    :class:`EmptyResponseError`, so this propagates on the first raise
    rather than being retried.

    Who catches it
    --------------
    The task layer, as a named failure that says which file to look at.
    When the template *was* readable at least once on this instance the
    cached copy in ``self._settings_template_cache`` heals the dispatch
    and this never fires; it is the no-cache fallback for an instance
    whose very first read came up empty.
    """

    def __init__(self, message: str, *, path: str):
        super().__init__(message)
        #: The dangling template path, verbatim, so the caller can act on
        #: it without parsing the message.
        self.path = path


def _slot_wait_expired(
    scene: str,
    waited_sec: float,
    rounds: int,
    bound_sec: float,
    candidates: Optional[List[str]] = None,
    event_logger: Optional[object] = None,
    requested_sec: Optional[float] = None,
) -> ProviderSlotWaitExpired:
    """Build (and log) the give-up signal for a queue that outlived its bound.

    Returns the exception rather than raising it, so the ``raise`` stays
    visible at the call site inside the queue loop — the reader of that loop
    sees exactly where a VP gives its slot wait up, which is the one place
    the difference between "waiting" and "working" is unambiguous.

    Logged to both sinks for the same reason as
    :func:`_scene_chain_unavailable`: the module logger is always present,
    and the structured ``event_logger`` is what the plan's execution log and
    the operator dashboard read. Before the verification call sites started
    passing a logger, the second one did not exist on this path at all.
    """
    listed = list(candidates or [])
    # ``:g`` rather than ``:.0f``: the figure is configurable, so a bound of
    # 0.2s must not read as "0s" in the one message that exists to say which
    # bound fired.
    message = (
        f"Scene {scene!r} held its slot wait for {waited_sec:g}s over "
        f"{rounds} rounds for "
        f"a free provider slot and none of {listed} handed one back; the "
        f"bound is {bound_sec:g}s (PROVIDER_SLOT_WAIT_SEC). Handing the "
        f"slot wait back rather than holding a capacity slot while asleep — "
        f"this is not a failed dispatch and must not be recorded as one."
    )
    exc = ProviderSlotWaitExpired(
        message,
        scene=scene,
        waited_sec=waited_sec,
        rounds=rounds,
        bound_sec=bound_sec,
        candidates=listed,
        requested_sec=requested_sec,
    )
    payload = exc.to_dict()
    logger.warning("[PROVIDER SLOT WAIT EXPIRED] %s", message)
    if event_logger is not None:
        event_logger.warning(
            "provider_slot_wait_expired", message, data=payload,
        )
    return exc


# Project-specific guardrails: a sub-agent may only Edit/Write inside its own
# project_dir (plus the shared plans/ directory and the system temp dir).
# This is a *containment* rule, not a formal-repo-specific one: verification
# self-heal sub-agents used to edit whatever ``project_dir`` pointed at — when
# that was the production repo the sub-agent rewrote production code
# (2026-08-26 VP-023 incident: 17 files modified in the formal checkout).
# With containment, pointing project_dir at a dev checkout (a dev checkout)
# guarantees the sub-agent physically cannot touch the formal repo.
# Paths may be overridden via environment variables for testing or
# non-standard layouts.  When the env var is unset we resolve the
# default against THIS file's location (``backend/coding_tool.py``)
# so the hook works in any clone (formal repo, exec repo, CI
# checkout, container).  The previous hardcoded
# ``<repo-root>`` default
# would silently mis-direct the hook on any other machine.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_FORMAL_REPO_PATH = Path(os.environ.get(
    "PDT_FORMAL_REPO_PATH", str(_REPO_ROOT)
)).resolve()
_PLANS_DIR = Path(os.environ.get(
    "PDT_PLANS_DIR", str(_FORMAL_REPO_PATH / "plans")
)).resolve()

# PreToolUse hook that enforces "edit inside your own project_dir only",
# and then asks the file-lock broker for the file before allowing it.
#
# Stage 1 — containment. Allows Edit/Write when the target path is inside
# any of:
#   * PDT_PROJECT_DIR  — the sub-agent's own checkout (e.g. a dev checkout)
#   * PDT_PLANS_DIR    — shared plan state files
#   * temp dirs       — /tmp, /private/tmp, $TMPDIR (scratch files)
# When PDT_PROJECT_DIR is empty (no cwd known) the hook falls back to the
# legacy behaviour: block writes to the checkout the backend runs from only.
#
# Stage 2 — the lock. Containment answers "may this edit ever happen"; the
# broker answers "may it happen *now*". Before this stage existed, the
# executor only locked the files a task had **declared**; a sub-agent that
# decided mid-task to touch an undeclared file wrote it holding nothing.
# The hook is the only place that sees the real target at the moment of
# the write, which is why the lock lives here.
#
# Only the project dir is brokered. plans/ and scratch space are shared
# state files with their own writers and their own contracts; pulling them
# into a project-file lock would serialise unrelated machinery against
# sub-agents for no gain.
#
# Degradation is deliberate and asymmetric. A *busy* file blocks the edit
# (exit 2, message on stderr) because waiting is exactly what the caller
# should do. An *unreachable* broker allows the edit with a warning: the
# executor having died is not the sub-agent's fault and must not brick it.
# The honest limit of this stage is that a hook killed while queued (by a
# harness-level hook timeout shorter than the wait) degrades to
# fail-open — which is why the declared-file locks are taken inside the
# executor, where no hook timeout applies.
#
# The hook script is written to a per-process tmpfile once and re-execed
# via ``python3 <file>``. This avoids the multi-line-inside-``python3 -c``
# quoting hazards that bit us in 2026-08-26 (newline-collapsed nested-if
# blew up under ``subprocess.run(..., shell=True)``).
_EDIT_WRITE_FORMAL_REPO_GUARD_SRC = '''\
import json,sys,os,pathlib,tempfile,subprocess
d=json.load(sys.stdin)
p=d.get("tool_input",{}).get("file_path","")
pd=os.environ.get("PDT_PROJECT_DIR","")
fr=os.environ.get("PDT_FORMAL_REPO_PATH","")
pr=os.environ.get("PDT_PLANS_DIR","")
def R(x):
    return str(pathlib.Path(x).resolve()) if x else ""
rp=R(p); pdr=R(pd); fr_r=R(fr); pr_r=R(pr)
tmp=R(tempfile.gettempdir())
def inside(x, root):
    return bool(root) and (x == root or x.startswith(root + os.sep))
def in_tmp_scratch(x):
    return x.startswith("/tmp/") or x.startswith("/private/tmp/") or inside(x, tmp)
blocked = False
if rp:
    if pdr:
        # Containment rule: edits only inside the sub-agent's own checkout
        # (project_dir), the shared plans/ dir, or scratch space.
        allowed = inside(rp, pdr) or inside(rp, pr_r) or in_tmp_scratch(rp)
        blocked = not allowed
    elif fr_r:
        # Legacy fallback (no project_dir known): block writes to the
        # the checkout the backend runs from only. Anything else is allowed.
        blocked = inside(rp, fr_r)
if blocked:
    print("[pdt] refusing to edit outside the project dir: " + rp, file=sys.stderr)
    sys.exit(2)

cli=os.environ.get("PDT_LOCK_CLI","")
sock=os.environ.get("PDT_LOCK_BROKER","")
task=os.environ.get("PDT_LOCK_TASK_ID","")
if not (cli and sock and task and rp and pdr):
    sys.exit(0)
if not inside(rp, pdr):
    sys.exit(0)
if not os.path.exists(sock):
    # No broker for this workspace: the executor is not running a task
    # through ``Agent.run``, or it has already finished and unlinked the
    # socket. Silent, because this is the normal state for a standalone
    # ``coding_tool`` call and warning on every edit there would train
    # readers to ignore the warning that matters.
    sys.exit(0)
if not os.path.exists(cli):
    print("[pdt-lock] PDT_LOCK_CLI set but missing (" + cli + "); allowing the edit", file=sys.stderr)
    sys.exit(0)
try:
    wait=float(os.environ.get("PDT_LOCK_TIMEOUT","300") or 300)
except ValueError:
    wait=300.0
try:
    proc=subprocess.run(
        [sys.executable, cli, "acquire", "--task", task, "--path", rp,
         "--timeout", str(wait)],
        capture_output=True, text=True, timeout=wait+60)
except Exception as exc:
    print("[pdt-lock] broker call failed (" + type(exc).__name__
          + "); allowing the edit", file=sys.stderr)
    sys.exit(0)
if proc.returncode == 0:
    sys.exit(0)
if proc.returncode == 3:
    print("[pdt] another task is editing this file right now: " + rp
          + " -- wait for it to finish and retry this edit.", file=sys.stderr)
    sys.exit(2)
print("[pdt-lock] broker unavailable (exit " + str(proc.returncode)
      + "); allowing the edit", file=sys.stderr)
sys.exit(0)
'''


def _ensure_guard_hook_path() -> Path:
    """Write the containment guard script to a stable tmpfile and return
    its path. Created lazily on first call; the script is tiny and
    read-only at runtime so a single shared file is fine."""
    global _GUARD_HOOK_PATH
    if _GUARD_HOOK_PATH is not None:
        return _GUARD_HOOK_PATH
    p = Path(tempfile.gettempdir()) / "pdt_edit_write_containment_guard.py"
    p.write_text(_EDIT_WRITE_FORMAL_REPO_GUARD_SRC, encoding="utf-8")
    _GUARD_HOOK_PATH = p
    return p


_GUARD_HOOK_PATH: Optional[Path] = None  # initialised lazily above


def _build_guard_hook() -> Dict[str, str]:
    """Return the PreToolUse matcher dict for the Edit|Write guard."""
    return {
        "type": "command",
        "command": f"python3 {_ensure_guard_hook_path()}",
    }


# Kept as a string for backwards-compat callers (tests import
# ``_EDIT_WRITE_FORMAL_REPO_GUARD``). New code should use
# ``_build_guard_hook()``.
_EDIT_WRITE_FORMAL_REPO_GUARD = _build_guard_hook()["command"]


# 2026-09-24: ``ProviderSpec`` / ``PROVIDER_REGISTRY`` REMOVED.
#
# They existed to map a local kebab-case *provider id* onto the row label
# the provider has in CC Switch (``cc_switch_name``). That indirection
# bought nothing: scene routing already matches providers by REGEX OVER
# THEIR NAMES (``provider_routing.yaml``), so the name *is* the identity.
# Carrying a second, locally-invented key alongside it meant every name
# had to be declared twice — once in ``_base.yaml`` as an id, once in the
# DB — and the two could drift while both looked authoritative.
#
# There is now exactly one way to name a provider: the name it has in CC
# Switch. Nothing in this repository invents provider ids.


class CodingTool(ABC):
    """Abstract base class for AI coding tools."""

    # 2026-09-15 — ONE unified budget for every LLM call.
    #
    # ``query(..., timeout=N)`` REPLACES these windows rather than nesting
    # inside them: N becomes both the adaptive silence cap and the
    # wall-clock guard for that call. That is how healthy-but-slow calls
    # died early — e.g. ``timeout=60`` on the VP supplement review turned
    # a working Vendor A response into 17/30 silent degradations on the
    # a production plan (CC Switch logged 100% provider success throughout: we
    # were aborting our own requests). Callers should therefore OMIT
    # ``timeout`` and inherit the defaults below; only add an explicit
    # value when an operation genuinely needs a SHORTER bound, and prefer
    # a named, documented constant over a magic number.
    DEFAULT_TOTAL_TIMEOUT = 900   # 15 min of stdout silence → watcher fires
    DEFAULT_IDLE_TIMEOUT = 1800   # 30 min without any stdout line → close pipe

    @abstractmethod
    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None) -> str:
        """
        Query the AI coding tool.

        Args:
            prompt: The prompt to send to the AI
            system_instruction: Optional system instruction
            retries: Number of retry attempts
            timeout: Optional timeout in seconds

        Returns:
            AI response as string
        """
        pass

    @abstractmethod
    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                 retries: int = 3, timeout: Optional[int] = None) -> dict:
        """
        Query the AI coding tool and expect JSON response.

        Args:
            prompt: The prompt to send to the AI
            system_instruction: Optional system instruction
            retries: Number of retry attempts
            timeout: Optional timeout in seconds

        Returns:
            AI response as dict
        """
        pass


#: HTTP-ish statuses that mean "this provider cannot serve the call right
#: now, try another one" rather than "the request is wrong". 429 is the
#: rate-limit / quota case; 402 is a billing rejection; 503/529 are the
#: upstream-overloaded answers Anthropic-compatible gateways return.
_CAPACITY_ERROR_STATUSES = frozenset({"429", "402", "503", "529"})


def is_capacity_error(exc: BaseException) -> bool:
    """True when ``exc`` says the PROVIDER is unavailable, not the request.

    A caller that sees this must rotate providers (or retry later) —
    retrying the same provider with the same prompt cannot help.
    """
    status = str(getattr(exc, "status", "") or "").strip()
    if status in _CAPACITY_ERROR_STATUSES:
        return True
    try:
        from dynamic_provider_concurrency import looks_like_quota_exhaustion
    except Exception:  # pragma: no cover - import cycle guard
        return False
    return looks_like_quota_exhaustion(str(exc))


def _quota_reset_seconds_for(provider: str, fallback: float) -> float:
    """Seconds until the window that emptied ``provider`` comes back.

    A quota cooldown is only ever worth as long as the window it is
    waiting on, and ``resets_at`` is the one field in the reading that
    says when that is. So the parked length stops being a constant and
    becomes a countdown: a window twelve minutes out is parked for
    twelve minutes, rather than for the half hour the fixed number has
    to be because it has to cover the worst window — and a provider is
    almost never parked at the worst moment.

    Every way of failing to answer falls back to ``fallback`` rather
    than raising or returning a small number:

      * no reading for this provider, or no reading at all (CC Switch
        unreachable, provider absent from the map);
      * the reading shows no budget but names no tier — the refusal was
        real and the meter did not corroborate it;
      * ``resets_at`` absent, unparseable, or **already in the past**.

    That last one is the boundary worth naming. A ``resets_at`` in the
    past is also what the reader treats as "this window has already
    rolled", so the tier's percentage is discarded as stale — which
    means a past stamp cannot be paired with a conviction from the same
    tier. When it happens anyway, returning the remaining time would
    return a non-positive number, and :meth:`ProviderCooldown.park` is
    documented to ignore those: the provider would be neither parked
    nor dispatched around, and the next dispatch would re-select the
    provider that just refused. So a past stamp falls back instead.
    """
    if not provider:
        return fallback
    try:
        from provider_order import load_provider_quota_map
    except Exception:  # pragma: no cover - import cycle guard
        return fallback
    try:
        quota = load_provider_quota_map().get(provider)
    except Exception:
        return fallback
    if quota is None or not getattr(quota, "exhausted", False):
        # The reading does not corroborate a refusal. Park for the
        # fallback rather than for a window nobody named.
        return fallback
    resets_at = getattr(quota, "exhausted_resets_at", None)
    if resets_at is None:
        return fallback
    now = datetime.now(resets_at.tzinfo) if resets_at.tzinfo else datetime.now()
    seconds = (resets_at - now).total_seconds()
    if seconds <= 0:
        return fallback
    return seconds


def _quota_wake_probe_sec() -> float:
    """Seconds between two probes of a closed quota window.

    One figure, and it is deliberately *not* a countdown to the
    ``resets_at`` the reading carries. A countdown is the right answer
    only for a window that reopens on a schedule, and the schedule is
    exactly what a plan upgrade does not follow: buying capacity makes
    the budget come back at a moment no ``resets_at`` was ever going to
    name, so a wake that had parked itself on that stamp would keep
    waiting through a window that had already opened. The one thing
    that covers it is looking again on a fixed beat.

    So the probe does not consult the reading at all. Five minutes is
    the existing short cooldown — reused rather than redeclared so the
    probe cadence and the park length cannot drift apart — and it buys
    the same thing either way: at most one missed probe of delay after
    a window reopens, against a wake that is still watching a week
    later.

    The price is real and it is paid off the wire. Each probe is one
    forced read of the quota payload, and that payload is fetched from
    an endpoint that fans out to every provider at once rather than
    asking one. The figure below is therefore a network cost paid per
    five minutes, per process (the payload is behind a single in-flight
    barrier), for as long as the window stays shut.
    """
    try:
        from dynamic_provider_concurrency import DEFAULT_COOLDOWN_SEC
    except Exception:  # pragma: no cover - import cycle guard
        return 300.0
    return float(DEFAULT_COOLDOWN_SEC)


class WorkClock:
    """A budget that stops spending while its holder is parked.

    A dispatch that is waiting for a provider's quota window to reopen
    is not working, but it is also not free — it holds a thread and a
    coroutine while it waits. If the budget that bounds the work is
    kept running across that wait, the wait spends it: a caller that
    waits out a week-long window would have blown an hour-long cap long
    before the window did, and the time-out would be answered by a
    second, tool-less caller asked to guess the outcome from the logs.

    The budget is therefore split in two — work time and parked time —
    and only the first is ever charged to it. Parking stops the
    countdown; resuming starts it again from where it stopped rather
    than from where it would have been. :meth:`remaining` is the only
    question the bounded side asks, and it is answered in the same
    units either way.

    Thread-safe, because the pause and the resume are raised from the
    worker thread that is doing the waiting while the budget is read
    from the event loop that owns the deadline. Both are idempotent:
    a second park before a resume does not bank the segment twice.
    """
    def __init__(
        self,
        budget_seconds: float,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self._budget = float(budget_seconds)
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._spent = 0.0
        self._segment_start = self._monotonic()
        self._parked_at: Optional[float] = None
        self._on_resume: Optional[Callable[[], None]] = None

    def park(self) -> None:
        """Stop spending the budget until :meth:`resume`."""
        with self._lock:
            if self._parked_at is not None:
                return
            now = self._monotonic()
            self._spent += now - self._segment_start
            self._parked_at = now

    def resume(self) -> None:
        """Start spending again from the amount already banked."""
        with self._lock:
            if self._parked_at is None:
                return
            self._parked_at = None
            self._segment_start = self._monotonic()
        callback = self._on_resume
        if callback is not None:
            # Raised on a thread that is not the one waiting on the
            # budget, so whoever bound this must be prepared for that.
            callback()

    @property
    def is_parked(self) -> bool:
        """True while the holder is asleep rather than working.

        This is the only question anything outside this class needs to
        ask about a parked budget, and it is asked by something that has
        to tell "waiting for a provider's window" apart from "hung": a
        dispatch parked here is not spending the budget and has no
        subprocess to kill, so a staleness sweep that treated it like a
        hung call would discard the handle that protects the *real* stall
        — the one that is genuinely stuck once the window reopens. The
        answer is a plain bool rather than duck-typed so a stand-in tool
        whose ``_work_clock`` is a test double cannot answer it by
        accident.
        """
        with self._lock:
            return self._parked_at is not None

    def bind_resume(self, callback: Optional[Callable[[], None]]) -> None:
        """Register what to do when the budget starts running again.

        The bounded side is asleep on a timer until the work finishes
        or the budget runs out, and a resume that happened in between
        would otherwise go unnoticed until that timer expired. This is
        how it gets a prompt answer instead of the whole wait.
        """
        self._on_resume = callback

    def remaining(self) -> float:
        """Seconds of work still available; never negative."""
        with self._lock:
            if self._parked_at is not None:
                spent = self._spent
            else:
                spent = self._spent + (self._monotonic() - self._segment_start)
        return max(0.0, self._budget - spent)


#: Longest a bounded side sits in one wait before it looks at its clock
#: again. One second: short enough that a budget's deadline lands within
#: a second of the truth even when the dispatch was asleep for part of
#: the slice, long enough that an hours-long wait costs one wakeup a
#: second rather than one per millisecond.
LAYER2_SLICE_SEC: float = 1.0


def _result_under_work_clock(future, clock) -> Any:
    """``future.result(timeout=…)`` bounded by the clock's *work* time.

    The synchronous twin of :func:`verification_subagent._await_under_work_clock`,
    and it exists for the same reason. ``Future.result(timeout=N)`` spends
    N on wall time, which is the wrong unit here: the dispatch on the other
    side can park itself on a provider's closed quota window and stay there
    for as long as that window says, and the whole wait was being charged
    to the caller's own budget. That is what the parking in
    :meth:`_run_claude_interactive` stops, but it only stops it for the
    readers that ask the clock — and ``future.result`` is a number, so it
    asked nothing. The dispatch came off the wait at 11:22, did the work,
    and the timeout that had expired at 10:47 took the answer with it.

    The number is therefore replaced by a question asked again each pass,
    and the question is not "has this slice ended" but "has the *budget*
    ended". The two come apart whenever the dispatch parks in the middle
    of a slice: the slice is wall time, the budget is not, and a wait
    that stopped halfway through the second one left most of the first
    one unspent. Asking the budget is what makes a parked wait free
    instead of merely slow — and asking it again is what stops the
    opposite error, where the same fixed slice gets mistaken for the
    deadline and kills a dispatch a tenth of a second after its window
    reopened.

    Slicing rather than one long wait is forced by the API: a blocking
    ``Future.result`` cannot be woken on two things at once the way
    :func:`asyncio.wait` can, so the only way to notice a resume is to
    come back and look. A dispatch that never parks behaves exactly as
    it did before — same budget, same deadline, same ``TimeoutError`` —
    and pays one wakeup per second for the privilege of being able to
    tell those two cases apart.

    The wait is deliberately unbounded: a parked clock reports its
    frozen remainder, which is never zero on entry, so the loop keeps
    going for as long as the window stays shut. Only
    ``FuturesTimeoutError`` is handled here. ``HardTimeoutError`` is a
    subclass of it and must reach the caller untouched for auto-split
    routing; it arrives from ``future.result`` already wrapped, which is
    the same thing ``future.result(timeout=…)`` always delivered.
    """
    while True:
        remaining = clock.remaining()
        if remaining <= 0.0:
            # Nothing left to spend. A parked clock is frozen, so a zero
            # that arrives while parked is permanent — there is no slice
            # left to wait on, and looping on zero would spin.
            raise FuturesTimeoutError("work budget exhausted")
        try:
            return future.result(timeout=min(remaining, LAYER2_SLICE_SEC))
        except FuturesTimeoutError:
            left = clock.remaining()
            if left <= 0.0:
                # The budget, not the slice, is what ended this. That is
                # the timeout the caller has always been told about.
                raise
            if future.done():
                # Finished inside the slice and the expiry won the race
                # for the lock. The answer is there; take it rather than
                # reporting a timeout for work that completed.
                return future.result()
            # The slice ended with budget in hand. Either the dispatch is
            # parked — and the frozen remainder means this wait is free
            # and has no ceiling — or it resumed part-way through the
            # slice and this slice's tail was parked. Both say the same
            # thing: wait again on what is left.
            continue


@contextmanager
def _dispatch_work_clock(tool, budget_seconds: float):
    """The clock this dispatch's own budget is spent on.

    ``_run_claude_interactive`` parks ``self._work_clock`` and nothing
    else, so for :func:`_result_under_work_clock` to see a park there has
    to BE a clock on the tool. One usually is: the verification sub-agent
    hands its sub-agents a ``WorkClock`` covering the hard cap, and that
    is the budget the wait is correctly parked against. Everything else
    — the rebuild agent, every plain ``query(..., timeout=N)`` — arrives
    with nothing, which is why a ``timeout=300`` dispatch could be killed
    at 300s by a clock that had been stopped for most of it.

    An absent clock is therefore adopted for the length of the call, on
    the caller's own figure, and put back afterwards. Adoption is
    one-way: an existing clock wins, because whoever handed it over
    declared the budget for this dispatch and their declaration is the
    one that should be spent.
    """
    existing = getattr(tool, "_work_clock", None)
    if existing is not None:
        yield existing
        return
    clock = WorkClock(budget_seconds)
    tool._work_clock = clock
    try:
        yield clock
    finally:
        # Exactly what was there before, which is ``None``. Left set,
        # this clock would go on answering ``is_parked`` for a dispatch
        # that has finished — and the heartbeat sweep asks that question
        # of every handle it holds.
        tool._work_clock = None


def _quota_wake_sleep(seconds: float) -> None:
    """Wait before re-probing a quota-blocked dispatch.

    Its own function so the wait can be replaced rather than served.
    The numbers this path deals in are minutes, so a test that let them
    through would test nothing but the patience of the suite; a test
    that wants the decision instead patches this name and reads back
    what was asked for.

    Every wait on the closed-window path goes through here, the probe
    interval included, so a test drives the cadence of a multi-round
    wait without serving a single second of it. The park wait and the
    saturated-pool queue share the seam for the same reason — all three
    waits are uninterruptible and all three run for minutes at a time, so
    all three are substitutable.

    The production body is the ordinary blocking sleep and there is no
    flag, mode or keyword that makes it behave differently, so the
    substitution cannot leak into a run: the seam is the module-level
    name, resolved at call time, and replacing it is something a test
    does on purpose.
    """
    if seconds > 0:
        time.sleep(seconds)


#: Shortest and longest single step of the "wait for this tier to come
#: back" loop.
#:
#: The floor is what keeps the wait from turning into a spin: a park can
#: have a couple of seconds left, or none at all by the time the figure is
#: read, and a step of zero would re-derive the same empty answer as fast
#: as the clock allows. The ceiling is what keeps it from turning into one
#: uninterruptible sleep: a quota refusal parks for half an hour, and a
#: thread parked for half an hour cannot notice that another dispatch
#: already re-parked the provider, that the caller gave up, or that the
#: chain on disk changed underneath it.
PARK_WAKE_MIN_STEP_SEC: float = 1.0
PARK_WAKE_MAX_STEP_SEC: float = 30.0


def _park_wake_step_sec(cooldown, providers) -> float:
    """Seconds to wait before re-deriving a scene's candidates.

    The SOONEST park in the tier, because that is the soonest moment the
    pool can serve again; waiting out the longest would hold a dispatch
    behind a row that came back minutes ago. Bounded by the floor and the
    ceiling above.

    The quota reading is not consulted, and that is the whole difference
    between this and the closed-window wake. A park is a deadline this
    process wrote itself, on the timescale of that deadline, so the
    deadline is the answer. A closed window belongs to somebody else's
    account and reopens on no schedule the dispatcher was told about,
    which is why that path probes on a fixed beat instead. Two waits,
    two clocks, one per kind of thing being waited on.
    """
    soonest = 0.0
    for provider in providers:
        remaining = cooldown.remaining(provider)
        if remaining > 0.0 and (soonest == 0.0 or remaining < soonest):
            soonest = remaining
    return max(PARK_WAKE_MIN_STEP_SEC, min(soonest, PARK_WAKE_MAX_STEP_SEC))


#: Shortest and longest single step of the "wait for this pool to hand a
#: slot back" loop.
#:
#: The floor is the same shape of argument as the park wake's floor and is
#: worth as much: a slot can be free the instant the check that said it was
#: full ran, so a step of zero would re-ask the tracker as fast as the
#: clock allows and never move a single agent. The ceiling is what keeps a
#: saturated pool from becoming one uninterruptible sleep — a thread asleep
#: for half a minute cannot notice that the holder finished, that the chain
#: on disk changed, or that the caller underneath it gave up.
SLOT_WAKE_MIN_STEP_SEC: float = 1.0
SLOT_WAKE_MAX_STEP_SEC: float = 30.0


def _slot_wake_step_sec(round_index: int) -> float:
    """Seconds to wait before asking a saturated pool again for a slot.

    A third clock, and the only one of the three that reads *nothing*.

    The park wake asks the cooldown how long its longest remaining park is,
    because that deadline is the answer and the process wrote it itself. The
    closed-window wake probes on a fixed beat, because that window belongs
    to somebody else's account and reopens on no schedule anybody here was
    told about. A slot is neither: it is a lease held inside this process by
    an agent that finishes when it finishes, so there is no deadline to read
    and no stamp to count down to — the only honest input is how long the
    wait has already been running, and what a longer one implies.

    So the step doubles per round and is then held flat: a pool that has
    been saturated for four rounds is held by agents that are still running,
    and the odds of a slot appearing within the next second are worse than
    they were at round zero. Doubling buys the check back the moment it is
    cheap — a slot freed while the loop slept is noticed within one step — and
    the ceiling stops the tail from becoming a thirty-second poll against a
    table that is obviously not moving.
    """
    rounds = max(0, int(round_index))
    # The exponent is clamped so the shift itself cannot grow without
    # bound on a caller that hands in a large round number; the ceiling
    # already fixes the answer long before that would matter.
    scaled = SLOT_WAKE_MIN_STEP_SEC * (2 ** min(rounds, 16))
    return max(SLOT_WAKE_MIN_STEP_SEC, min(scaled, SLOT_WAKE_MAX_STEP_SEC))


def _cooldown_seconds_for(exc: BaseException, provider: str = "") -> float:
    """Park duration for ``exc``; env-overridable for operators/tests.

    A rate-limit refusal keeps its fixed short park — a transient limit
    is expected to clear in minutes and nothing about the exception says
    otherwise. A quota refusal is a statement about a *window*, so it is
    parked until that window returns
    (:func:`_quota_reset_seconds_for`), with the half-hour default as
    the answer whenever the reading cannot name the moment.
    """
    try:
        from dynamic_provider_concurrency import (
            DEFAULT_COOLDOWN_SEC,
            DEFAULT_QUOTA_COOLDOWN_SEC,
            looks_like_quota_exhaustion,
        )
    except Exception:  # pragma: no cover - import cycle guard
        return 0.0
    if looks_like_quota_exhaustion(str(exc)):
        default = DEFAULT_QUOTA_COOLDOWN_SEC
        env_key = "PDT_PROVIDER_QUOTA_COOLDOWN_SEC"
    else:
        default = DEFAULT_COOLDOWN_SEC
        env_key = "PDT_PROVIDER_COOLDOWN_SEC"
    raw = os.environ.get(env_key)
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    if default is not DEFAULT_QUOTA_COOLDOWN_SEC:
        return default
    return _quota_reset_seconds_for(provider, default)


def _park_provider_after_error(provider: str, exc: BaseException) -> float:
    """Park ``provider`` when ``exc`` is a capacity error; return seconds.

    Returns ``0.0`` when nothing was parked (not a capacity error, or
    the cooldown module is unavailable).
    """
    if not provider or not is_capacity_error(exc):
        return 0.0
    seconds = _cooldown_seconds_for(exc, provider)
    if seconds <= 0:
        return 0.0
    try:
        from dynamic_provider_concurrency import get_shared_cooldown
    except Exception:  # pragma: no cover - import cycle guard
        return 0.0
    try:
        get_shared_cooldown().park(
            provider, seconds=seconds, reason=str(exc)[:300],
        )
    except Exception:
        return 0.0
    return seconds


class ClaudeCodingTool(CodingTool):
    """Claude Code CLI tool using stdin/stdout stream-json protocol.

    Implements three-layer hang protection:
    1. Event idle timeout — if no stream-json line arrives within N seconds,
       close stdout pipe to unblock the read loop.
    2. Graceful shutdown — stdin EOF → wait → SIGTERM → SIGKILL.
    3. Stderr capture — on process failure, stderr is included in the error.
    4. Cross-thread process kill — when ThreadPoolExecutor timeout fires,
       the underlying claude sub-process is explicitly killed to prevent leaks.
    """

    # NOTE: DEFAULT_IDLE_TIMEOUT / DEFAULT_TOTAL_TIMEOUT are inherited from
    # ``CodingTool`` (2026-09-15) so every tool class shares one budget —
    # do not re-declare them here.
    MAX_TIMEOUT_RETRIES = 2      # max provider fallback retries on timeout
    # 2026-09-17 — an empty reply (subprocess exited, no assistant text)
    # is retried on a FRESH session, not with ``--resume``: the session
    # id we asked for was never created, so resuming it can only fail
    # again (~0.4s, "Invalid session ID" — measured). Two retries with a
    # 2s/4s backoff rides out the transient cases (provider 5xx burst,
    # momentary connection failure) without accumulating a long delay.
    MAX_EMPTY_RESPONSE_RETRIES = 2
    EMPTY_RESPONSE_RETRY_BACKOFF_SEC = 2.0
    # How long ONE attempt at taking a provider slot polls for it before
    # the dispatch re-decides. Overridable per-process with
    # PDT_PROVIDER_SLOT_WAIT_SEC.
    #
    # It is no longer a bound on the queue: a saturated tier is waited out
    # for as long as it takes (see ``_slot_wake_step_sec``), because a
    # slot is a lease inside this process that the agent holding it will
    # hand back when it finishes. What this bounds is a single attempt's
    # own polling, so a dispatch hands a long queue over to its parked
    # step-by-step wait quickly enough to be responsive and slowly enough
    # not to re-check a free slot in a loop. Setting it to 0 makes every
    # attempt a single look.
    PROVIDER_SLOT_WAIT_SEC = 300.0
    GRACEFUL_STOP_TIMEOUT = 120  # seconds to wait after stdin close

    # 2026-09-08: HARD cap constant. Subclasses (verification_subagent)
    # enforce an absolute 1-hour wall-clock ceiling via this constant; the
    # inner adaptive timer (DEFAULT_TOTAL_TIMEOUT=900s) is the primary
    # kill signal but can be skipped on truly pathological cases.
    HARD_WALL_CLOCK_TIMEOUT_SECONDS: int = 3600

    # --- Prompt compaction (mirrors Claude Code context compression) ---
    MAX_PROMPT_TOKENS = 180_000   # target ceiling; leaves 20K headroom below 200K limit
    MAX_TOKENS_PER_FILE = 15_000  # per-file truncation cap (mirrors POST_COMPACT_MAX_TOKENS_PER_FILE)
    COMPACT_BUFFER_TOKENS = 5_000 # safety buffer after compaction

    # Auto-compact window handed to the CLI for every sub-agent, in tokens.
    # Distinct from MAX_PROMPT_TOKENS above: that one caps the *initial*
    # prompt PDT sends, this one caps the *session* the CLI accumulates
    # across turns. CLAUDE_CODE_AUTO_COMPACT_WINDOW takes a plain integer in
    # [100000, 1000000] -- a suffixed "384k" reads as 384 and clamps to the
    # floor. The effective window is still min(this, model's context window).
    SUBAGENT_AUTO_COMPACT_WINDOW = "384000"

    TRUNCATION_MARKER = (
        "\n\n[... content truncated for context limit; "
        "use Read on the file path if you need the full text]"
    )
    FILE_BLOCK_RE = r'FILE:\s*(?P<path>[^\n]+)\n```[\w]*\n(?P<content>[\s\S]*?)\n```'

    @staticmethod
    def _normalize_model_type(value: Optional[str]) -> str:
        if value is None:
            return "medium"
        v = str(value).strip().lower()
        if v in ("complex", "high", "hard", "advanced"):
            return "complex"
        return "medium"

    @staticmethod
    def _get_live_provider_priority() -> Optional[List[str]]:
        """Read the provider priority chain at call time (hot-reload friendly).

        The ONLY source is the ``PDT_PROVIDER_PRIORITY`` env var — a
        comma-separated list of provider names as they appear in CC
        Switch (e.g. ``"Vendor A Pro,Vendor D API"``). Reading it per
        call means an operator can reorder the chain without restarting
        the backend.

        Two older sources are deliberately gone:

        * ``_base.yaml``'s ``provider_priority`` block — the file is
          product content shipped to every user, while a priority chain
          is one operator's deployment. It also contradicted the
          provider optimizer, whose whole job is to own that order: a
          list compiled into a shipped file would silently outrank the
          runtime decision.
        * ``~/.pdt/config.yaml`` — never written by anything, and not
          under the documented config directory. Deployment-specific
          configuration lives in ``.config/`` (see README.md).

        Returns ``None`` — not an empty list — when nothing is set, so
        the caller can tell "unconfigured" apart from "configured, empty"
        (both end up meaning "no chain", but only one is a mistake).
        """
        env_pri = os.environ.get("PDT_PROVIDER_PRIORITY")
        if env_pri:
            result = [p.strip() for p in env_pri.split(",") if p.strip()]
            if result:
                return result
        return None

    # 2026-09-13: _resolve_model REMOVED. Model management is delegated
    # entirely to CC Switch — the selected provider row's own
    # ANTHROPIC_* model env block is forwarded verbatim by the dispatch
    # walk, and scene → provider routing (provider_routing.py) decides
    # which provider candidates a call may use. The complex/medium
    # model_map tiering had degenerated to identical values anyway.

    def __init__(self, model: Optional[str] = None, cwd: Optional[str] = None, logger=None, api_key: Optional[str] = None,
                 model_type: Optional[str] = None, model_map: Optional[Dict] = None,
                 provider_priority: Optional[list] = None,
                 mcp_config: Optional[str] = None,
                 settings: Optional["Path"] = None,
                 hook_stdin: Optional[Dict] = None,
                 base_url: Optional[str] = None,
                 auth_token: Optional[str] = None,
                 scene: Optional[str] = None,
                 concurrency_tracker=None):
        # Default: inherit from parent process env (no override).
        # Only when explicitly passed do we override the child process.
        self.model = model
        self.api_key = api_key
        self.cwd = cwd
        self.logger = logger
        # base_url / auth_token: explicit SubagentConfig-driven env
        # overrides (decision 2/3 plumbing). When passed, the
        # subprocess ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN env
        # vars come from this pair and bypass provider selection
        # (so the SubagentConfig's provider is the single
        # source of truth).
        self.base_url = base_url
        self.auth_token = auth_token
        # mcp_config: path to a JSON file (or inline JSON string) of MCP
        # server definitions, passed to `claude -p` via --mcp-config.
        # The Claude subprocess does NOT auto-read ~/.claude.json's
        # mcpServers block — it must be injected explicitly. Used by
        # VerificationAgent._execute_ui_validation to load puppeteer.
        self.mcp_config = mcp_config
        # settings: path to a SubagentConfig-written tmpfile (decision 2/3).
        # When set, the file is passed to the Claude subprocess via
        # `--settings <path>` so the SDK can resolve ANTHROPIC_* env
        # overrides and PostToolUse hook scripts without polluting
        # ~/.claude/settings.json.
        self.settings = settings
        # _settings_template_cache: the template payload exactly as read
        # off disk, before this module stamps PDT_* keys or appends the
        # guard hook onto it.
        #
        # Why the tool keeps a copy of a file it does not own: the
        # template lives in a ``private_dir()`` that anything sweeping
        # ``~/.pdt-scratch`` can take away mid-run. Once gone it cannot
        # be rebuilt — the hooks and the ``defer_provider_config``
        # payload only exist in that file, and ``self.settings`` is a
        # path string, not the content. Caching the pristine payload at
        # read time is the only copy that survives the sweep, and it is
        # what lets a later dispatch on THIS instance self-heal instead
        # of handing the CLI a dangling path.
        #
        # Instance-level on purpose: the empty-output retry loop calls
        # ``_run_claude_interactive`` again on the same ``self``, so a
        # template that vanished between two attempts of one task is
        # still recoverable.
        self._settings_template_cache: Optional[dict] = None
        # hook_stdin: pass-through payload (decision 4) forwarded to
        # PostToolUse hooks (e.g. task_type, task_summary). Stored on
        # the instance so the subprocess invoker can append it to the
        # hook's stdin stream.
        self.hook_stdin = hook_stdin or {}
        # scene: workflow scene for provider routing (2026-09-13).
        # ``None`` means no scene routing, so the dispatch falls back to
        # ``provider_priority``; when set, every dispatch re-resolves the
        # scene chain via ``provider_routing.resolve_provider_chain``
        # (hot-reload friendly) and uses its candidates exclusively.
        self.scene = scene
        # current_call_model: the ANTHROPIC_MODEL env value stamped on
        # the child process for the in-flight call (usage-registry
        # metadata). Set per dispatch inside _run_claude_interactive.
        self.current_call_model: Optional[str] = None
        # model_type: retained as inert task metadata (planner's
        # complex/medium annotation). 2026-09-13: it no longer routes
        # model selection — model management is delegated to CC Switch
        # (the provider row's own env block) and scene → provider
        # routing selects the tier.
        self.model_type = self._normalize_model_type(model_type)
        self.model_map = model_map or {}
        # provider_priority resolution (re-read on every
        # _run_claude_interactive, never cached):
        #   1. an explicit provider_priority=... argument (tests / overrides)
        #   2. the PDT_PROVIDER_PRIORITY env var
        # There is no third source. Entries are provider NAMES as CC
        # Switch spells them — see the note above ``class CodingTool``.
        # An unconfigured chain stays empty, which the dispatch reads as
        # "no chain to walk" and hands the call to the parent process
        # config (with no CC Switch, that is the user's own Claude Code
        # settings).
        if provider_priority:
            self.provider_priority = [str(p) for p in provider_priority]
        else:
            self.provider_priority = [
                str(p) for p in (self._get_live_provider_priority() or [])
            ]
        # Inherit mode (2026-09-24): CC Switch is a *plugin*, not a
        # prerequisite. When it is absent there are no provider rows to
        # read, no chain to order and nothing to fail over between — the
        # user's own Claude Code configuration is already the answer, and
        # the only correct behaviour is to not interfere with it.
        # Resolved once per tool instance: the answer cannot change while
        # a run is in flight, and probing per dispatch would put a sqlite
        # open in front of every LLM call.
        self.inherit_providers: bool = self._detect_inherit_mode()
        self._current_process: Optional[subprocess.Popen] = None
        self._process_lock = threading.Lock()
        # Concurrency slot bookkeeping for capacity-gated scene routing
        # (2026-09-17). Defaults to the process-wide tracker the FastAPI
        # lifespan publishes (``RuntimeState.dynamic_tracker``); a
        # per-call private tracker would count only this tool's own work
        # and the per-provider caps would not bind.
        self._concurrency_tracker = concurrency_tracker or get_shared_tracker()
        # How long a dispatch waits for a free pool slot before giving up
        # and falling back to the parent config. Bounded well inside the
        # 900s default total timeout / 1800s executor task timeout so
        # queueing alone can never trip a timeout.
        try:
            self.provider_slot_wait_sec = float(
                os.environ.get("PDT_PROVIDER_SLOT_WAIT_SEC", "")
                or self.PROVIDER_SLOT_WAIT_SEC
            )
        except (TypeError, ValueError):
            self.provider_slot_wait_sec = float(self.PROVIDER_SLOT_WAIT_SEC)
        # Watchdog-visible signal: monotonic timestamp of the most recent
        # stdout line read from the current subprocess. The sub-agent
        # registry watchdog uses this to distinguish "LLM hang, stdout
        # silent" (truly stuck) from "long pytest run, stdout streaming"
        # (still making progress, do not kill). None means no subprocess
        # has produced output yet this call.
        self._last_output_ts: Optional[float] = None
        # The budget bounding this call, when something upstream owns one
        # and wants parked time excluded from it. None is the ordinary
        # case — a caller with no budget of its own — and every use
        # below is guarded, so an unset clock simply means nothing here
        # is accounted to anyone. See WorkClock.
        self._work_clock: Optional["WorkClock"] = None
        # 2026-09-14: the LAST stdout line itself, so
        # callers can surface real progress (pytest's progress bar, tool
        # names) instead of only knowing "something arrived". The
        # verification sub-agent pumps this into its attempt log, which
        # is what the staleness watchdog measures — a live pytest that
        # keeps printing progress lines can no longer look stalled.
        self._last_output_line: str = ""
        # Per-call state: which provider was just attempted in the current
        # _run_claude_interactive call. NOT a sticky "permanent skip" flag — every
        # call re-evaluates Vendor B/Vendor A availability fresh (see
        # _run_claude_interactive below). A previous failure must never permanently
        # blacklist a provider.

    @staticmethod
    def _load_api_key_from_env() -> Optional[str]:
        """Load ANTHROPIC_API_KEY from environment or the CWD's .env file.

        This is a utility for callers to discover the key explicitly;
        the CodingTool itself does NOT auto-inject it — only explicit
        api_key=... triggers injection.

        We deliberately only check the immediate CWD's ``.env`` and
        do NOT walk up the parent chain. Walking up risks leaking a
        key from a parent project's ``.env`` when the test (or the
        tool) runs in a subdirectory — e.g. CI running the backend
        suite from ``backend/`` would otherwise pick up the project
        root's ``.env`` and surface a key in test runs that
        ``monkeypatch.delenv`` already cleared from the environment.
        """
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key

        dotenv = Path.cwd() / ".env"
        if dotenv.exists():
            try:
                with open(dotenv, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ANTHROPIC_API_KEY="):
                            return line[len("ANTHROPIC_API_KEY="):].strip().strip('"').strip("'")
            except Exception:
                pass
        return None

    @staticmethod
    def _detect_inherit_mode() -> bool:
        """True when providers should be inherited from Claude Code itself.

        CC Switch is a *plugin*. It is how one operator manages a fleet
        of providers; it is not something this project may assume exists.
        On a machine without it there are no provider rows, no chain to
        order and nothing to fail over between — but there is still a
        working Claude Code, which is the only reason any of this runs at
        all. So the answer is not to degrade gracefully through a
        provider walk that can only fail; it is to step out of the way.

        ``PDT_PROVIDER_MODE`` pins the mode (``inherit`` or ``cc-switch``),
        so an operator can force it and a test can set it without having
        to fabricate a database or its absence.
        """
        override = os.environ.get("PDT_PROVIDER_MODE", "").strip().lower()
        if override == "inherit":
            return True
        if override == "cc-switch":
            return False
        try:
            from cc_switch import probe

            return not probe().available
        except Exception:  # noqa: BLE001 - a probe must never take the fleet down
            # The probe contract is "never raises"; if that is ever
            # broken, assume CC Switch is present. That preserves the
            # behaviour of every deployment that has one, and the ones
            # that do not are a subset of the ones that do.
            return False

    @staticmethod
    def _load_provider_from_db(provider_name: Optional[str] = None) -> dict:
        """Resolve a provider's connection config.

        Two distinct questions, two distinct answer sources:

        * **With a name** — "what are the credentials for the provider
          CC Switch calls ``X``?" Reads the cc-switch SQLite DB row
          named exactly ``X`` and nothing else. A named provider with no
          row is *unavailable*, and that is the honest answer: the one
          thing we must not do is answer a named lookup from the ambient
          environment, because the environment describes *an* endpoint
          and never says which provider it belongs to (see
          :meth:`_load_provider_from_env`).

        * **With no name** — "which provider is this process already
          configured for?" There is nothing to look up, so the ambient
          environment (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``)
          IS the configuration. This is the no-CC-Switch path.

        Returns:
            ``{"base_url": ..., "api_key": ..., "models": {...}}`` on
            success, or an empty dict when the relevant source has no
            usable entry.
        """
        if provider_name:
            cfg = ClaudeCodingTool._load_provider_from_cc_switch_db(provider_name)
            if cfg.get("base_url") and cfg.get("api_key"):
                return cfg
            return {}

        cfg = ClaudeCodingTool._load_provider_from_env()
        if cfg.get("base_url") and cfg.get("api_key"):
            return cfg
        return {}

    @staticmethod
    def _load_cc_switch_current_provider() -> Optional["ProviderConfig"]:
        """The provider CC Switch is *currently routing* Claude traffic to.

        2026-09-21: the legacy walk still answers "which provider?" by
        asking CC Switch for the one it is actually routing to. CC
        Switch owns the routing decision, so its current selection is
        the only honest answer for which provider a scene-less dispatch
        should use.

        Resolution: ``~/.cc-switch/settings.json::currentProviderClaude``
        (the provider id) → the matching ``providers`` row for
        ``app_type='claude'`` → its ``settings_config.env`` block.
        Falls back to the row flagged ``is_current``, then to ``None``.

        Returns a dispatchable :class:`cc_switch.ProviderConfig`, or
        ``None`` when CC Switch is not reachable / has no current
        provider. Never raises — callers treat a falsy result as "ask
        someone else".
        """
        from cc_switch import current_provider

        return current_provider()

    @staticmethod
    def _load_provider_from_cc_switch_db(provider_name: str) -> dict:
        """Source 1: the CC Switch row named ``provider_name``.

        The name is used verbatim — it IS the provider's identity (see
        the note above ``class CodingTool``). Finding the row and parsing
        it belong to :mod:`cc_switch`, which hands back a normalized
        :class:`cc_switch.ProviderConfig`; what stays here is the
        projection into the flat shape the dispatch walks,
        plus the rule that a row is only usable when it carries *both*
        an endpoint and a credential.

        Returns ``{"base_url", "api_key", "models"}``, or an empty dict
        when there is no such row or it cannot drive a sub-agent.
        """
        from cc_switch import CCSwitchError, get_provider

        try:
            cfg = get_provider(provider_name)
        except CCSwitchError:
            # This sits on the dispatch path, where "there is no usable
            # CC Switch row for this provider" is an ordinary answer that
            # the caller already handles by falling through. A missing or
            # unreadable database is the same answer — not an error, and
            # certainly not one that should abort a dispatch.
            return {}
        if cfg is None or not cfg.is_dispatchable():
            return {}
        return {
            "base_url": cfg.base_url,
            "api_key": cfg.api_key,
            "models": cfg.models,
        }

    @staticmethod
    def _load_provider_from_env() -> dict:
        """The provider the *process environment itself* describes.

        Reads ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` and
        rejects the PROXY MANAGED sentinel (both the spaced and
        underscored spellings) — it would route the sub-agent through
        cc-switch's currently-active provider instead of the one this
        walk selected, defeating any explicit ordering.

        This source is deliberately **nameless**: the environment
        describes *an* endpoint, never which provider it belongs to.
        That is why :meth:`_load_provider_from_db` only consults it when
        no provider name was requested. Answering a named lookup from
        here would hand back one provider's endpoint under another
        provider's name — a call logged as
        ``vendor-a-pro`` with a hard-coded ``m2.7`` model was in fact
        billed by CC Switch to vendor-d-lite.
        """
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        # Reject the PROXY MANAGED sentinel explicitly — it would
        # route through cc-switch's currently-active provider,
        # defeating the explicit fallback ordering. The caller can
        # still get PROXY MANAGED by setting ANTHROPIC_BASE_URL /
        # ANTHROPIC_AUTH_TOKEN directly (no env-rejection happens
        # then).
        #
        # Both spellings are rejected:
        #   * "PROXY MANAGED" (with space) — cc-switch legacy format
        #   * "PROXY_MANAGED" (with underscore) — cc-switch writes
        #     this form when the proxy is active and the app's
        #     environment-checker warns about it (see
        #     feedback-claude-proxy-managed-sentinel.md)
        if not base_url or not auth_token:
            return {}
        normalized = auth_token.strip().upper().replace("_", " ")
        if normalized == "PROXY MANAGED":
            return {}
        return {
            "base_url": base_url,
            "api_key": auth_token,
            "models": {},  # no model mapping available from env
        }

    @staticmethod
    def _check_provider_availability(provider_name: str) -> Tuple[bool, dict]:
        """Can this provider serve a call right now?

        ``provider_name`` is the provider's CC Switch name — the scene
        chain and the priority chain both yield names verbatim, so there
        is nothing to translate. Availability is satisfied by a usable
        ``base_url`` + ``api_key`` in the DB row.

        The ``/v1/models`` network probe that used to live here is gone.
        It fired once per candidate per dispatch with a 10s timeout, so a
        chain of unavailable providers could add tens of seconds of
        latency to a call that was going to fall back anyway — and it
        answered a question the answer to which is already known to be
        unreliable (CC Switch's own logs showed 100% provider success
        while the probe reported failures). A provider that has a row but
        is actually unreachable now fails at the request, where the
        dispatch's error handling can see the real status code.

        Returns ``(is_available, config_dict)`` with
        ``{"base_url", "api_key", "models"}`` from the row, or
        ``(False, {})`` when there is no usable row.
        """
        cfg = ClaudeCodingTool._load_provider_from_db(provider_name)
        if cfg.get("base_url") and cfg.get("api_key"):
            return True, cfg
        return False, {}

    # 2026-09-24: the per-provider availability wrappers REMOVED.
    #
    # ``_check_vendor-b_availability`` / ``_check_vendor_a_pro_availability``
    # / ``_check_vendor-b_glm_availability`` each hard-coded one operator's
    # provider name, and the dispatch resolved them by *deriving a method
    # name from the provider key* (``vendor-b-pro`` → ``getattr(tool,
    # "_check_vendor-b_glm_availability")``). That made a provider's
    # existence depend on a Python method existing — the opposite of what
    # the config file is for. One checker, keyed by name, replaces all
    # three.
    #
    # The peak-hour policy these wrappers once carried now lives ONLY in
    # the producer's rule engine, which expresses it as a
    # priority demotion. The demotion reaches this layer through the
    # chain ORDER — the walk simply takes the first available provider,
    # so a demoted provider is picked last without this module knowing
    # anything about peak hours.

    # ================================================================
    # Interactive mode (M1 migration)
    # ================================================================
    #
    # Background: tasks_generator / prd_generator etc. invoke the LLM
    # via ``query_json`` which delegates to ``_run_claude_interactive``. The previous
    # design used ``--print`` mode (single-shot, no conversation state,
    # no Read/Bash/Edit tool access). Smoke v11+ surfaced the failure
    # mode: the LLM could not call Read to fetch the upstream PRD/
    # arch/test content and instead returned ``{"error": "..."}`` or
    # ``{"tasks": []}`` ~33% of the time.
    #
    # The fix (M1): invoke Claude Code in **interactive** mode (the
    # default; no ``--print``). Interactive mode exposes the full Read/
    # Bash/Edit tool set, lets the LLM multi-turn Read the upstream
    # docs, and supports ``--resume <session_id>`` for follow-up calls.
    #
    # Stream-json OUTPUT is still used (--output-format stream-json) so
    # the parent process can read assistant events as a JSON event
    # stream. INPUT goes through plain text stdin — interactive mode's
    # default. The LLM naturally terminates when stdin closes (it sends
    # a ``result`` event with ``subtype: success``).
    #
    # Return shape: ``(text, session_id)`` — text is the final assistant
    # reply (the body of the ``result`` event); session_id is the Claude
    # Code session UUID extracted from the ``system init`` event. Callers
    # that don't care about session_id can ignore it (``[0]``); callers
    # that want to resume can pass ``session_id`` to the next call.
    def _run_claude_interactive(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        session_id: Optional[str] = None,
        resume: bool = False,
        idle_timeout: Optional[int] = None,
        total_timeout: Optional[int] = None,
        excluded_providers: Optional[list] = None,
        allowed_tools: Optional[list] = None,
        scene: Optional[str] = None,
    ) -> tuple:
        """Run Claude Code in interactive mode with stream-json output.

        Args:
            prompt: The user message to send (plain text, fed via stdin).
            system_instruction: Optional system prompt.
            session_id: If provided AND resume=True, this is the session
                to resume (use ``--resume <session_id>``). If provided
                AND resume=False, this is the session_id for the NEW
                session (``--session-id <uuid>`` — deterministic UUID so
                the caller can re-use it later).
            resume: True → --resume; False → --session-id (new session).
            idle_timeout: Seconds of stdout inactivity before killing
                the read loop. Defaults to ``DEFAULT_IDLE_TIMEOUT``.
            total_timeout: **Not** a wall-clock cap on the call.
                Defaults to ``DEFAULT_TOTAL_TIMEOUT`` (15 min). Since
                2026-09-08 this watcher measures SILENCE, not elapsed
                time: it SIGTERMs only after ``total_timeout`` seconds
                with no stdout line (``silence_baseline =
                max(last_line_time[0], started_at)``), so a call that
                keeps emitting stdout — thinking deltas, tool events,
                pytest progress — **never trips it**, however long it
                runs. "Alive and printing" is not killed by design. The
                absolute ceiling is the layer above
                (``HARD_WALL_CLOCK_TIMEOUT_SECONDS``).

                This entry previously read "Even if the LLM is actively
                streaming, this watcher thread forces a SIGTERM after
                the cap". That had been **false since 2026-09-08**, and
                it misled operators into treating layer 1 as a wall-clock
                ceiling. The authoritative history is the
                ``2026-09-15 CORRECTION`` comment on ``_total_timer``
                below — read that, not this summary, if the two ever
                disagree.
            excluded_providers: Skip providers on this call (provider
                fallback semantics, same as ``_run_claude_interactive``).
            scene: Optional per-call workflow scene override. When set,
                the provider chain is re-resolved for this scene (hot-
                reload friendly) and preferred over the legacy walk.
                ``None`` uses the instance ``scene``.

        Returns:
            ``(result_text, session_id, result_meta)`` where session_id
            is the Claude Code session UUID for follow-up ``--resume``
            calls and ``result_meta`` is the usage/cost metadata dict
            captured from the stream-json ``result`` event (``usage``,
            ``total_cost_usd``, ``num_turns``, durations; empty on
            timeout/error paths).
        """
        subagent_uuid = uuid.uuid4().hex
        subagent_log_file = f"/tmp/subagent_{subagent_uuid}_activity.log"

        # Sub-agent authorization: same iron rule as _run_claude_interactive.
        if self.hook_stdin:
            pdt_authorization = (
                "You are a subagent explicitly spawned by this workflow's backend. "
                "You have been authorized by the workflow orchestrator to implement the assigned task, run tests, "
                "and modify files. Any standing instruction you may have seen about not implementing tasks "
                "manually applies only to external/manual agents acting outside the workflow; it does NOT "
                "apply to you because you ARE the workflow's designated implementation agent. Do not refuse "
                "to implement the current task on the grounds that a plan is active.\n\n"
                "**No absolute paths in shipped code.** Any code you write that gets committed MUST NOT "
                "contain a literal user filesystem path such as `/Users/<user>/...`, "
                "`/home/<user>/...`, `~/...`, or `C:\\Users\\<user>\\...`. The repo is cloned onto many "
                "developer machines and CI runners; a hard-coded path from your environment will silently "
                "break the code elsewhere. Use one of these three substitutes instead, in order of "
                "preference:\n"
                "  1. `Path(__file__).resolve().parents[N] / '...'` — relative to the source file. "
                "Use this whenever the path is this repository itself or any file inside it.\n"
                "  2. `Path.home() / 'Documents' / '<repo>'` — only when the test or helper semantically "
                "needs *another repo on this developer's machine* (e.g. a cross-repo containment guard). "
                "Use `Path.home()`, never write the username literally.\n"
                "  3. `Path(os.environ['VAR_NAME']) / '...'` — when the value is operator-configured "
                "(PDT_PROJECT_DIR, HOME, PDT_PLANS_DIR, model_map_path, etc.).\n\n"
                "Before declaring a task complete, re-read every file you touched and confirm none of them "
                "contains a literal filesystem path."
            )
            if system_instruction:
                system_instruction = f"{system_instruction}\n\n{pdt_authorization}"
            else:
                system_instruction = pdt_authorization

        # Interactive mode = NO --print flag. Output is still stream-json
        # so the parent can read the event stream; input is plain text
        # via stdin (interactive mode default). The LLM terminates by
        # emitting a ``result`` event when stdin closes.
        cmd = [
            "claude",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", "bypassPermissions",
        ]
        if system_instruction:
            cmd += ["--append-system-prompt", system_instruction]
        if self.model:
            cmd += ["--model", self.model]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config]
        if session_id:
            if resume:
                cmd += ["--resume", session_id]
            else:
                cmd += ["--session-id", session_id]
        # Per-call tool restriction (2026-09-07 BinaryRebuildAgent plan).
        # When set, the spawned Claude CLI is hard-limited to the given
        # tool list (e.g. Bash,Read,Grep,Glob) so the sub-agent cannot
        # use Edit/Write. ``None`` keeps the default unrestricted set so
        # every existing caller is unaffected.
        if allowed_tools:
            cmd += ["--allowedTools", ",".join(allowed_tools)]

        # Env setup: identical to _run_claude_interactive (provider selection,
        # settings file, hook, etc). Kept inline here as the single source
        # of truth for both query() and query_json() execution paths.
        env = os.environ.copy()
        env["PDT_SUBAGENT_UUID"] = subagent_uuid
        env["PDT_SUBAGENT_LOG_FILE"] = subagent_log_file

        effective_settings_path: Optional[Path] = None
        # The ``--settings`` payload is only *assembled* here. The
        # ANTHROPIC_* keys are copied from ``env`` and the file is
        # written further down, AFTER every provider-selection / scene-
        # routing / ``self.*`` override has mutated ``env`` — see the
        # write block just before the ``--settings`` flag is appended.
        settings_data: Optional[dict] = None
        if self.settings is not None:
            original_settings = Path(self.settings)
            if original_settings.exists():
                with open(original_settings, "r", encoding="utf-8") as f:
                    settings_data = json.load(f)
                # Cache the PRISTINE template: taken after ``json.load``
                # but before any mutation below, so what is stored is what
                # SubagentConfig wrote and nothing this module adds. The
                # ``deepcopy`` matters because every line below writes into
                # this same dict — a shallow copy would hand the cache an
                # ``env`` sub-dict that the next dispatch overwrites, and
                # the cache would silently accumulate per-subagent keys.
                self._settings_template_cache = copy.deepcopy(settings_data)
            elif self._settings_template_cache is not None:
                # SELF-HEAL. The template was readable earlier on this
                # instance and is gone now (a sweep of the private root
                # took it), so fall back to the cached payload and let the
                # shared stamping below produce a normal settings file.
                # The file this dispatch ultimately writes lands in a
                # brand-new ``private_dir()``, so unlike the swept
                # template this one is always writable.
                settings_data = copy.deepcopy(self._settings_template_cache)
            else:
                # Nothing to fall back on: this instance's FIRST read came
                # up empty. There is no template on disk and no cached copy,
                # so any ``--settings`` value we could pass is either
                # dangling (0.2s CLI death, reported as "exited without
                # assistant text" — the failure this replaces) or absent
                # (no hooks, no lock broker — the worse outcome). Say which
                # path is missing instead. See SettingsTemplateMissing for
                # why this must not be an ApiError.
                raise SettingsTemplateMissing(
                    f"settings template missing and no cached copy: "
                    f"{original_settings}",
                    path=str(original_settings),
                )
            if settings_data is None:
                # ``json.load`` succeeded but produced no object — a file
                # whose whole content is ``null``. Treated as "not a
                # template" rather than as "no settings": it carries
                # neither hooks nor env, so passing it on would launch a
                # sub-agent with its guardrails silently off, which is the
                # outcome this branch exists to prevent. Enforced HERE, at
                # the read site, because it is the one place where the
                # invariant "settings set ⟹ settings_data non-None" is
                # established; the ``--settings`` fallback that depends on
                # it sits inside a ``try`` whose ``finally`` would mask any
                # error raised there.
                raise SettingsTemplateMissing(
                    f"settings template is not a JSON object "
                    f"(and no cached copy): {original_settings}",
                    path=str(original_settings),
                )

        if settings_data is not None:
            settings_data.setdefault("env", {})
            # Must ride in the ``--settings`` payload (flagSettings), NOT in the
            # child process env. At startup the CLI folds its settings scopes
            # into ``process.env`` in the order userSettings, flagSettings,
            # policySettings, each via an unconditional ``Object.assign`` -- so
            # a value passed through ``env=`` is clobbered by whatever this
            # machine's ``~/.claude/settings.json`` says, and only this file is
            # late enough to win. See SUBAGENT_AUTO_COMPACT_WINDOW.
            settings_data["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = (
                self.SUBAGENT_AUTO_COMPACT_WINDOW
            )
            settings_data["env"]["PDT_SUBAGENT_UUID"] = subagent_uuid
            settings_data["env"]["PDT_SUBAGENT_LOG_FILE"] = subagent_log_file
            if self.cwd:
                settings_data["env"]["PDT_PROJECT_DIR"] = str(Path(self.cwd).resolve())
                # File-lock broker (see ``file_lock_broker``): the socket
                # path is derived from the project dir, so the executor
                # and every sub-agent compute the same one. The CLI path
                # is absolute so the hook can run it under whatever
                # ``python3`` it has — ``file_lock_cli`` is stdlib-only
                # for exactly that reason.
                settings_data["env"]["PDT_LOCK_BROKER"] = str(
                    socket_path(self.cwd)
                )
                settings_data["env"]["PDT_LOCK_CLI"] = str(
                    _THIS_DIR / "file_lock_cli.py"
                )
                lock_task_id = current_task_id()
                if lock_task_id:
                    # Lock ownership is keyed by task id, so the executor
                    # can release everything the task acquired — including
                    # files it only decided to touch mid-task. Without it
                    # the hook has no identity to acquire under, and the
                    # ambient sub-agent uuid would not match the key the
                    # executor releases with.
                    settings_data["env"]["PDT_LOCK_TASK_ID"] = str(lock_task_id)
            settings_data["env"]["PDT_FORMAL_REPO_PATH"] = str(_FORMAL_REPO_PATH)
            settings_data["env"]["PDT_PLANS_DIR"] = str(_PLANS_DIR)
            settings_data.setdefault("hooks", {})
            settings_data["hooks"].setdefault("PreToolUse", [])
            guard_hook = _build_guard_hook()
            for entry in settings_data["hooks"]["PreToolUse"]:
                if entry.get("matcher") == "Edit|Write":
                    entry.setdefault("hooks", []).append(guard_hook)
                    break
            else:
                settings_data["hooks"]["PreToolUse"].append({
                    "matcher": "Edit|Write",
                    "hooks": [guard_hook],
                })

        # Provider selection — single source of truth (only _run_claude_interactive
        # remains in this codebase). Kept inline rather than extracted because
        # it depends on local env mutation.
        provider_selected = False
        self.current_call_provider: Optional[str] = None
        excluded = set(excluded_providers or [])

        # Inherit mode resolves the whole question here, once.
        #
        # Setting ``provider_selected`` is not a shortcut around the walk
        # — it is the correct answer *to* the walk. Every entry point
        # below is gated on it (the CC Switch "current provider" lookup,
        # the ``provider_priority`` loop, and the ``self.*`` parent
        # fallback), so marking it selected means: no provider lookup, no
        # scene routing, no failover, and — the part that matters —
        # nothing written into ``env``. Claude Code then resolves
        # ``ANTHROPIC_*`` from its own settings, which is exactly what
        # "inherit" means.
        #
        # ``current_call_provider`` is a label, not a provider: it shows
        # up in logs and usage rows, and "inherit" is the honest value.
        if self.inherit_providers:
            provider_selected = True
            self.current_call_provider = "inherit"

        # Capacity slot bookkeeping for this call (2026-09-17).
        #
        # ``slot_provider`` is a LOCAL, not instance state, on purpose:
        # the verification path shares ONE ``ClaudeCodingTool`` across
        # every concurrent VP (``verification_agent`` passes
        # ``self.coding_tool`` into each sub-agent), so an attribute here
        # would be clobbered by whichever VP dispatched last and the
        # wrong pool would be decremented — or the same one twice.
        #
        # ``_release_scene_slot`` is idempotent because two teardown arms
        # can reach it (the setup-region handler and the read loop's
        # ``finally``); releasing twice would under-count a pool and let
        # it over-subscribe.
        slot_provider: Optional[str] = None
        _slot_released = False

        def _release_scene_slot() -> None:
            nonlocal _slot_released, slot_provider
            if _slot_released or slot_provider is None:
                return
            _slot_released = True
            self._concurrency_tracker.release(slot_provider)

        # Scene routing: when a scene is active, resolve its provider chain
        # (scene → tier → regex over CC Switch names) on EVERY call and use
        # its candidates EXCLUSIVELY. The tier table is an allowlist, so a
        # chain that selects nothing does not hand the call to the
        # provider_priority walk below: the call fails, via
        # ``_scene_chain_unavailable``. With no scene there is no allowlist
        # to violate, and the walk (and the parent fallback after it) runs
        # as it always has.
        effective_scene = scene if scene is not None else self.scene
        scene_chain: List[str] = []
        # Skipped entirely in inherit mode: both branches below read CC
        # Switch display names, so with no database they can only return
        # an empty chain while logging "CC Switch database unavailable" —
        # a warning about a component that is not supposed to be there.
        #
        # ``_scene_diag`` records WHY the chain ended up empty (or which
        # candidates it offered) so the fail-closed raise below can say
        # so in a message, rather than only as a log line nobody reads
        # when a task dies. Defaults describe "resolution never ran",
        # which only happens for the no-scene / inherit paths that do
        # not reach the gate at all.
        _scene_diag: dict = {
            "reason": "chain_unresolved",
            "tier": None,
            "chains": [],
            "chain": [],
            "candidates": [],
            "waited_sec": None,
        }
        if effective_scene and not self.inherit_providers:
            try:
                from provider_routing import resolve_provider_chain
                # The scene's OWN tier, and the whole of it.
                #
                # A tier is a pool: while this pool has room the walk
                # moves between its own providers, which is the one
                # degradation the table allows. Reaching the end of it
                # ends the candidate search — the answer is then either
                # "wait for this pool" (see the park wake below) or
                # "this scene has nothing approved", never "serve it
                # from the next tier along".
                #
                # The escape hatch that used to sit here widened into
                # other tiers once every provider here was unusable, and
                # it is gone because the failure it answered now has
                # another way out: the park below waits out a refusal
                # instead of crossing a border nobody approved. A tier
                # whose providers are all out of budget is a statement
                # about that tier's accounts, and borrowing a pricier
                # one to cover it spends money the table did not budget
                # while the pool itself recovers on its own schedule.
                primary = resolve_provider_chain(effective_scene)
                # Tier label for the failure message. Pure config read
                # (mtime-cached, no DB), and independently guarded: the
                # label is diagnostic sugar, so a config read that fails
                # here must not change what the chain resolves to.
                try:
                    from provider_routing import resolve_scene_tier
                    _scene_diag["tier"] = resolve_scene_tier(effective_scene)
                except Exception:
                    _scene_diag["tier"] = None
                # One tier answers with one chain, so this list holds
                # exactly one element. The key stays because it is part
                # of the failure payload's shape.
                _scene_diag["chains"] = [list(primary)]
                if not primary:
                    _scene_diag["reason"] = "no_tier_match"
                scene_chain = list(primary)
            except Exception as exc:
                _scene_diag["reason"] = "chain_unresolved"
                _scene_diag["error"] = str(exc)
                if self.logger:
                    self.logger.warning(
                        "provider_scene_chain_error",
                        f"scene chain resolution failed for {effective_scene!r}: {exc}",
                        data={"scene": effective_scene, "error": str(exc)},
                    )
                scene_chain = []

        if scene_chain:
            # 2026-09-17 — capacity-gated selection. Before this the walk
            # answered exactly one question per candidate ("does it have
            # a base_url + token?") and took the first yes, so 100% of
            # verification traffic landed on the first row while its
            # sibling CC Switch rows sat idle.
            #
            # Downgrade is driven by *local slot exhaustion*, not by
            # reachability: the provider answers fine, but piling every
            # concurrent agent onto one row drains that row's quota. So
            # build the reachable candidate set first, then take the
            # first one with a free local slot — and when every pool is
            # full, QUEUE for one of them to come back rather than
            # piling more work onto the first entry.
            #
            # The queue is for held slots, and nothing else waits inside
            # it. A provider whose window is empty, or that a refusal
            # earlier in this run parked, is dropped before the queue is
            # entered — so neither of them is ever handed the work to be
            # refused again. What happens to the scene once they are all
            # gone is decided further down, by which of the three things
            # the tier is short of: a park expires on its own and the
            # scene waits for it, a closed window has to be looked up and
            # is looked up, a full pool has to be waited for and is, and
            # anything left over is a scene that has nothing approved and
            # says so.
            scene_candidates: List[str] = []
            scene_configs: dict = {}
            from dynamic_provider_concurrency import get_shared_cooldown
            _cooldown = get_shared_cooldown()

            def _collect_scene_candidates() -> None:
                """Rebuild the candidate set from the chain, from scratch.

                Rebuilt rather than adjusted because the park wait calls
                this again, and the second answer has to come out of the
                same rules as the first — same chain, same exclusion
                set, same park state read fresh. Patching a candidate
                back into the list that came up empty would let the wait
                decide the answer the filters are there to give.
                """
                nonlocal scene_candidates, scene_configs
                scene_candidates = []
                scene_configs = {}
                for display_name in scene_chain:
                    if display_name in excluded:
                        continue
                    # A provider that recently answered "quota
                    # exhausted" is reachable and correctly configured,
                    # so every probe below says yes and the walk picks
                    # it again. Skip it until its park expires.
                    if _cooldown.is_parked(display_name):
                        if self.logger:
                            self.logger.info(
                                "provider_scene_candidate_parked",
                                f"Scene chain candidate {display_name!r} is in "
                                f"quota cooldown for another "
                                f"{_cooldown.remaining(display_name):.0f}s; "
                                f"trying next",
                                data={
                                    "scene": effective_scene,
                                    "provider": display_name,
                                    "cooldown_sec": round(
                                        _cooldown.remaining(display_name), 1,
                                    ),
                                    "park_reason": _cooldown.reason(display_name),
                                },
                            )
                        continue
                    available, provider_config = (
                        ClaudeCodingTool._check_provider_availability(display_name)
                    )
                    if not (available and provider_config):
                        if self.logger:
                            self.logger.info(
                                "provider_scene_candidate_unavailable",
                                f"Scene chain candidate {display_name!r} unavailable; trying next",
                                data={"scene": effective_scene, "provider": display_name},
                            )
                        continue
                    scene_candidates.append(display_name)
                    scene_configs[display_name] = provider_config

            def _tier_awaiting_park() -> List[str]:
                """Chain members this call must wait for, else ``[]``.

                Non-empty only when *every* provider the call was willing
                to consider is sitting in a park. A chain member that is
                merely missing its configuration is not coming back on a
                timer, and a member the caller excluded has been told
                not to be used — so neither is a reason to wait, and both
                still fail closed below.
                """
                eligible = [n for n in scene_chain if n not in excluded]
                if not eligible:
                    return []
                parked = [n for n in eligible if _cooldown.is_parked(n)]
                return parked if len(parked) == len(eligible) else []

            _collect_scene_candidates()

            # What the chain offered, before the per-candidate filters
            # above dropped any of it. The fail-closed message needs this
            # to separate "the tier named nothing" from "the tier named
            # rows and every one of them was filtered out".
            _scene_diag["chain"] = list(scene_chain)
            _scene_diag["candidates"] = list(scene_candidates)
            if not scene_candidates:
                _scene_diag["reason"] = "all_candidates_skipped"

            # ---- An empty pool waits for the pool; it does not go
            # shopping.
            #
            # The filter above drops a parked provider, so a tier that
            # has just been refused by every one of its rows arrives here
            # with nothing and used to fail closed straight away. That is
            # the case this block exists for: the tier named rows, the
            # rows answered "no budget", and the answer is on a clock
            # (a park expires by itself) rather than a permanent one.
            # Failing closed here answered a question nobody asked —
            # "should this call never run?" — about a condition that
            # resolves itself in minutes.
            #
            # So the wait is entered, and it waits for THIS pool to come
            # back. Not for another tier's: the pools are independent and
            # a provider outside this scene's table is not an answer to
            # anything this block asks.
            #
            # It is the same shape as the closed-window wake further
            # down and deliberately not the same clock. That one watches
            # somebody else's meter on a fixed beat, because a plan
            # upgrade brings budget back at a moment no stamp names. This
            # one waits for a deadline this process wrote itself, and
            # reads the deadline rather than guessing at a cadence.
            # Merging them would put the two on one clock and lose the
            # difference that makes each correct.
            _park_rounds = 0
            _park_waited = 0.0
            _park_parked = False
            _park_work_clock = getattr(self, "_work_clock", None)
            try:
                while not scene_candidates:
                    _waiting_on = _tier_awaiting_park()
                    if not _waiting_on:
                        break
                    # Waiting is not working, exactly as on the
                    # closed-window path: the budget that bounds this
                    # call must not be spent on the wait.
                    if not _park_parked:
                        _park_parked = True
                        if _park_work_clock is not None:
                            _park_work_clock.park()
                    delay = _park_wake_step_sec(_cooldown, _waiting_on)
                    _park_rounds += 1
                    if self.logger:
                        self.logger.info(
                            "provider_scene_park_wait",
                            f"Every provider in scene {effective_scene!r}'s "
                            f"tier is in quota cooldown; waiting up to "
                            f"{delay:.0f}s for the tier to come back",
                            data={
                                "scene": effective_scene,
                                "parked": list(_waiting_on),
                                "wait_sec": delay,
                                "park_rounds": _park_rounds,
                            },
                        )
                    _quota_wake_sleep(delay)
                    _park_waited += delay
                    _collect_scene_candidates()
                    _scene_diag["candidates"] = list(scene_candidates)
                    _scene_diag["park_wait_rounds"] = _park_rounds
                    _scene_diag["park_waited_sec"] = round(_park_waited, 1)
            finally:
                if _park_parked and _park_work_clock is not None:
                    _park_work_clock.resume()

            if scene_candidates:
                from dynamic_provider_concurrency import (
                    acquire_provider_with_dynamic_capacity,
                    fleet_capacity,
                )
                from provider_order import (
                    load_provider_5h_usage,
                    load_provider_quota_map,
                )

                # The live reading decides whether these candidates may
                # be dispatched to at all; the legacy percentage map is
                # still read for the ones the live reading does not
                # cover, and a failure of either leaves every provider
                # selectable rather than failing the dispatch.
                try:
                    quota_map = load_provider_quota_map()
                except Exception:
                    quota_map = {}
                try:
                    usage_map = load_provider_5h_usage()
                except Exception:
                    # The dispatch path must never fail on a malformed
                    # quota file; a missing entry reads as 100%.
                    usage_map = {}

                wait_sec = self.provider_slot_wait_sec

                # The wake layer lives under the "nothing to dispatch
                # to" answer and nowhere else. It covers the two reasons
                # a scene can be unable to dispatch, which differ by
                # three orders of magnitude in how long they last — a
                # held slot is released in seconds, a closed window
                # reopens on the provider's own schedule — so each keeps
                # its own clock instead of being unified into one wait.
                _wake_rounds = 0
                _slot_rounds = 0
                _slot_waited = 0.0
                # The wall clock the queue's bound is measured on, and how
                # much of it the queue has spent inside its own steps.
                #
                # ``_slot_waited`` accumulates the durations the queue
                # *asked* for: the right number for a diagnostic, the wrong
                # number for a bound, because a step the queue was woken out
                # of early is not waiting that happened. ``_slot_held`` is
                # the elapsed time inside those steps and is what the bound
                # compares.
                #
                # Only the queue's own steps, and not the poll the selection
                # does between them: that poll is already bounded, by the
                # same figure, once per attempt — and a bound that counted
                # it as well would be met by a single poll and would take
                # the queue's second step away from it, which is the whole
                # thing the queue is for. What repeats without end is this
                # loop, so this loop is what the bound is on.
                #
                # Injectable for the same reason :class:`WorkClock` takes
                # one, so a test can drive a five-minute bound in a
                # millisecond.
                _slot_clock = getattr(
                    self, "_slot_wait_clock", None) or time.monotonic
                _slot_held = 0.0
                _parked = False
                _quota_waiting = False
                _work_clock = getattr(self, "_work_clock", None)
                # Parked across the whole selection-and-wait region, and
                # before the FIRST attempt rather than after it, because
                # the first attempt is itself a wait: with the pool full
                # the poller inside it blocks for ``wait_sec``. Parking
                # only once the loop had decided to wait would leave the
                # single longest stretch of queueing still charged to the
                # caller's budget, which is the defect the budget exists
                # to catch.
                #
                # On a pool with room the cost of that is the rotation
                # walk — a handful of tracker lookups, measured in
                # microseconds — so the clock is very slightly
                # under-charged rather than the wait being charged. That
                # is the direction this can err in: under-charging a
                # budget loses nothing, and over-charging it is what
                # turns a queue into a wall-clock timeout.
                if _work_clock is not None:
                    _work_clock.park()
                    _parked = True
                # ONE watchdog for the whole process, not one per waiting
                # dispatch. Quota is a global fact — usable when it is
                # usable, unusable when it is not — so every caller here
                # is asking the same question about the same door, and
                # the payload answers it by fanning out to every provider
                # at once. Letting each of them probe on its own cadence
                # spends one request per waiting dispatch per interval to
                # learn one thing; the reader's in-flight barrier cannot
                # fix that, because it serializes the requests without
                # sharing anything and ``force=True`` skips its second
                # cache check. The merge has to be in front of the reader,
                # merging results rather than requests.
                _quota_watchdog = _shared_quota_watchdog()
                try:
                    chosen = acquire_provider_with_dynamic_capacity(
                        scene_candidates,
                        self._concurrency_tracker,
                        usage_map,
                        timeout=wait_sec,
                        quota_map=quota_map,
                    )
                    while chosen is None:
                        # Nothing to dispatch to, right now. Which of the
                        # two reasons it was is in the diagnostic, because
                        # the two are waited on differently: a window that
                        # is empty comes back on the provider's own
                        # schedule and has to be looked up, while a held
                        # slot comes back when the agent holding it
                        # finishes and has to be waited for.
                        _quota_blocked = sorted(
                            name for name in scene_candidates
                            if name in quota_map and quota_map[name].exhausted
                        )
                        _scene_diag["reason"] = (
                            "quota_exhausted" if _quota_blocked
                            else "no_capacity"
                        )
                        _scene_diag["quota_exhausted"] = _quota_blocked
                        _scene_diag["waited_sec"] = round(
                            _slot_waited, 1,
                        )
                        if self.logger:
                            self.logger.warning(
                                "provider_scene_all_pools_exhausted",
                                f"Every scene candidate for {effective_scene!r} is "
                                f"unavailable: "
                                f"{_quota_blocked or 'no free slot'} "
                                f"({scene_candidates})",
                                data={
                                    "scene": effective_scene,
                                    "candidates": scene_candidates,
                                    "quota_exhausted": _quota_blocked,
                                    "wait_sec": wait_sec,
                                    "slot_wait_rounds": _slot_rounds,
                                    "slot_waited_sec": round(_slot_waited, 1),
                                    "tracker": self._concurrency_tracker.snapshot(),
                                },
                            )

                        # Which of the two waits this is, asked with the
                        # same arithmetic the poller applied. A pool with
                        # any width left is full rather than shut and is
                        # queued for below; a pool with no width left at
                        # all is shut — and *shut is a window*, not a
                        # verdict, so it is not queued for and is not
                        # failed on either.
                        #
                        # Nothing else can produce that second case.
                        # Parked rows and rows without a configuration
                        # were filtered out before the candidates were
                        # collected, and a reading that called a provider
                        # shut would have made ``_quota_blocked``
                        # non-empty and carried the decision to the window
                        # path already. What is left are caps that a
                        # remaining budget scaled to zero, and that budget
                        # is a meter on somebody else's clock: it comes
                        # back when the provider's window rolls, whether
                        # or not a plan upgrade is what brought it back.
                        #
                        # So a cap of zero is a window measured in hours,
                        # and the wait for it belongs to the probe path
                        # below rather than to the queue — a lease wait
                        # pointed at that clock would never outlast it.
                        # Failing here, which is what this branch used to
                        # do, turned a pause into a failed task for a
                        # budget that was on its way back, and it did it
                        # on the premise that no width at all means no
                        # width ever: the one reading a rolling window
                        # never supports.
                        _capacity = fleet_capacity(
                            scene_candidates,
                            usage_map,
                            quota_map,
                            cooldown=_cooldown,
                        )
                        # Who the window wait below is waiting on.
                        # Normally the providers the reading calls shut;
                        # when the reading calls none of them shut and it
                        # is the caps that ran out, the rows with no width
                        # left are the same fact under its other name, and
                        # a wait that names nobody is a wait an operator
                        # cannot read.
                        _window_blocked = _quota_blocked or list(
                            _capacity.blocked
                        )

                        if not _quota_blocked and _capacity.total > 0:
                            # ---- A saturated pool: queue for this pool.
                            #
                            # The queue has a bound, and the bound is
                            # ``wait_sec`` — the same figure the poll window
                            # above already reads, overridable through the
                            # same ``PDT_PROVIDER_SLOT_WAIT_SEC``. A second
                            # knob for "how long may I queue" would be a
                            # second answer to that question, and an operator
                            # would have no way to tell which one was live.
                            #
                            # What the bound buys is a *holder* going back.
                            # On the verification path a queue is not a
                            # thread on its own: the verification point that
                            # asked for the slot is holding a capacity gate on
                            # both sides of that wait, and a gate held by a VP
                            # that is asleep is a gate no working VP can take.
                            # With every VP of a round queued at once the
                            # whole fleet reads as busy and nothing is
                            # running — the round produces nothing for as
                            # long as the saturation lasts, which is a window
                            # with no far end in it. So the wait ends, and
                            # what it ends with is a slot wait *handed back*
                            # rather than a call marked failed.
                            #
                            # It is deliberately measured on the queue alone.
                            # Everything below this line is either waiting or
                            # about to dispatch; the dispatch itself runs
                            # outside the loop, so a call that waited and was
                            # then admitted is never near this branch again.
                            # That is what keeps the bound off a working call:
                            # a long Claude run is not a queue.
                            #
                            # And it is measured on elapsed time, not on the
                            # sum of the steps the queue asked for, and only
                            # on the queue's own steps. The sum overstates: a
                            # step the queue was woken out of early is not
                            # waiting that happened. Elapsed is also the
                            # figure the rest of this change is about — how
                            # long a gate has been held by something asleep
                            # in it — and the per-attempt poll between the
                            # steps has its own window already, so counting
                            # it here would spend the bound before the queue
                            # had taken a second step.
                            #
                            # ``wait_sec`` of zero (or below) means the queue
                            # is unbounded — the same figure that turns the
                            # per-attempt poll window off turns this off, so
                            # the two can never disagree about being set.
                            if wait_sec > 0.0 and _slot_held >= wait_sec:
                                raise _slot_wait_expired(
                                    effective_scene,
                                    _slot_held,
                                    _slot_rounds,
                                    wait_sec,
                                    candidates=scene_candidates,
                                    event_logger=self.logger,
                                    requested_sec=_slot_waited,
                                )
                            #
                            # Every candidate is reachable, inside its
                            # window, and merely out of local slots. This
                            # used to break straight out to the fail-closed
                            # answer on the argument that a slot comes back
                            # in seconds and the poller had already spent
                            # the whole of ``wait_sec`` on it.
                            #
                            # That argument was wrong about what a slot is.
                            # A quota window is somebody else's meter on
                            # their clock and a park is a deadline this
                            # process wrote itself; a slot is neither. It
                            # is a counter in this process, held by an
                            # agent that is running right now and released
                            # when that agent finishes. So it has its own
                            # timeline, one measured in the length of a
                            # task, and waiting on it is waiting for
                            # something that is genuinely on its way —
                            # unlike a window with no reopening time, which
                            # is exactly the case worth failing on.
                            #
                            # So the queue is entered, and it waits for THIS
                            # pool. Not for another tier's, for the same
                            # reason the park wait above does not shop
                            # around: the pools are independent and a
                            # provider outside this scene's table is not an
                            # answer to anything this block asks. And the
                            # step is bounded and growing, because the slot
                            # this dispatch wants is very likely the one
                            # another VP is about to hand back.
                            #
                            # The quota reading is deliberately not
                            # re-read here. A slot wait is seconds long
                            # and a window is hours long, so the stale
                            # verdict cannot be the thing that is wrong
                            # during it — and the closed-window wake
                            # re-reads on its own account, on the only
                            # clock that can see a reopen.
                            #
                            # There is no round budget, for the same reason
                            # the window wake has none: the limit on a busy
                            # pool belongs to the agents in it and not to
                            # the dispatcher, and a caller that gives up on
                            # its own schedule does not get a slot back — it
                            # reports a failure for a queue it declared
                            # over on a figure it made up. The budget is
                            # parked alongside the wait rather than spent
                            # on it.
                            delay = _slot_wake_step_sec(_slot_rounds)
                            _slot_rounds += 1
                            _held_from = _slot_clock()
                            if self.logger:
                                self.logger.info(
                                    "provider_scene_slot_wait",
                                    f"Every provider in scene "
                                    f"{effective_scene!r}'s tier is at its "
                                    f"local concurrency cap; queued for up "
                                    f"to {delay:.0f}s for one to be "
                                    f"released",
                                    data={
                                        "scene": effective_scene,
                                        "candidates": scene_candidates,
                                        "wait_sec": delay,
                                        "slot_rounds": _slot_rounds,
                                        "slot_waited_sec": round(
                                            _slot_waited, 1,
                                        ),
                                    },
                                )
                            _quota_wake_sleep(delay)
                            _slot_waited += delay
                            _slot_held += _slot_clock() - _held_from
                            _scene_diag["slot_wait_rounds"] = _slot_rounds
                            _scene_diag["slot_waited_sec"] = round(
                                _slot_waited, 1,
                            )
                            chosen = acquire_provider_with_dynamic_capacity(
                                scene_candidates,
                                self._concurrency_tracker,
                                usage_map,
                                timeout=wait_sec,
                                quota_map=quota_map,
                            )
                            continue

                        # ---- A closed window: probe until it may reopen, then
                        # decide again from a fresh reading.
                        #
                        # Two things arrive here, and they are the same
                        # wait: a reading that calls the window shut, and
                        # caps that a remaining budget scaled to zero.
                        # Neither comes back on a lease, so neither is
                        # queued for — both are watched, on this clock.
                        #
                        # The refresh that reopens a window happens inside
                        # CC Switch, and nothing on this side notices it: the
                        # quota payload is only re-read when something asks
                        # for it. So "stop dispatching" meant "stay stopped
                        # until an unrelated dispatch happened to arrive" —
                        # and for a scene with nothing to dispatch to, that
                        # is no arrival at all. The fleet sits idle for the
                        # rest of the window even though the budget behind it
                        # came back an hour into the wait.
                        #
                        # There is no round budget here, because the limit
                        # on a closed window belongs to the provider and not
                        # to the dispatcher: how long a tier stays shut is a
                        # fact about someone else's account, and a caller
                        # that gives up on its own schedule does not get the
                        # window back — it just reports a failure for a wait
                        # it declared over on a figure it made up. So this
                        # probes until the reading changes. What used to
                        # bound it (three rounds) only ever bought a failing
                        # answer, and bought it *late*: the seconds spent
                        # waiting were seconds the caller's own budget was
                        # being charged for, so a wait long enough to matter
                        # was already over the wall-clock cap by the time it
                        # ended. The budget is now parked alongside the wait
                        # rather than spent on it.
                        #
                        # The cost of an unbounded wait is a forced read of
                        # the quota payload every probe interval, for as long
                        # as the window stays shut, and that read goes to an
                        # endpoint which fans out to every provider at once.
                        # It is the price of being right about the case no
                        # timestamp can describe — a plan upgrade, which
                        # makes the budget come back at a moment no
                        # ``resets_at`` was ever going to name. What bounds
                        # that price is the single watchdog every waiting
                        # dispatch goes through: one read per interval for
                        # the process, not one per waiter, however many
                        # verification points are asleep on the same window.
                        #
                        # Entering the wait is itself an event worth a
                        # record. Nothing about a parked dispatch is visible
                        # from outside — it is asleep, it writes nothing, and
                        # its budget is deliberately not running down — so a
                        # wait that emits no start line is
                        # indistinguishable from a fleet that went quiet for
                        # a reason nobody can name. The line carries the
                        # providers that are shut and the soonest moment any
                        # of their windows comes back, which is the two
                        # facts an operator needs to tell "quota" from
                        # "broken".
                        # The budget is already parked — it is parked for
                        # the whole selection-and-wait region, before the
                        # first attempt, so that the attempt itself
                        # cannot charge it either. There is deliberately no
                        # second park here to fall out of step with that
                        # one: two places that park the same clock is how
                        # an exception path ends up leaving it parked, or
                        # resumed twice, and the window wake is reached
                        # from a loop whose sibling branch already holds
                        # it.
                        if not _quota_waiting:
                            _quota_waiting = True
                            _quota_watchdog.wait_entered(
                                scene=effective_scene,
                                blocked=_window_blocked,
                                quota_map=quota_map,
                                logger=self.logger,
                            )

                        delay = _quota_wake_probe_sec()
                        _wake_rounds = _quota_watchdog.wait_round()
                        if self.logger:
                            self.logger.info(
                                "provider_quota_window_wait",
                                f"Probing {delay:.0f}s for a quota window to "
                                f"reopen before re-deciding {effective_scene!r}",
                                data={
                                    "scene": effective_scene,
                                    "quota_exhausted": _window_blocked,
                                    "wait_sec": delay,
                                    "wake_rounds": _wake_rounds,
                                    "waiting_dispatches": (
                                        _quota_watchdog.waiting_dispatches
                                    ),
                                },
                            )
                        _quota_wake_sleep(delay)

                        # Re-read, then re-decide from scratch — same
                        # candidates, same eligibility rules, same answer
                        # shape. It is deliberately not a shortcut that
                        # bypasses the selection: the reading this wakes on
                        # is the only thing that has changed, and trusting
                        # it is the whole point.
                        #
                        # The read is **forced**. A cache entry is stamped
                        # when it was fetched, so a wake that slept less
                        # than the quota TTL would otherwise be handed the
                        # verdict it already had — read before the sleep, and
                        # therefore incapable of reporting a window that
                        # reopened during it. For the common case of a window
                        # about to close that is not a staleness question but
                        # the whole feature failing: the wake would wake,
                        # see the same "full", and stop having learned
                        # nothing. The TTL exists to keep a dispatch from
                        # asking CC Switch once per dispatch, and a wake is
                        # not a dispatch — it asks precisely because what it
                        # needs to know is what happened while it was not
                        # looking.
                        #
                        # It is asked of the shared watchdog rather than of
                        # the reader directly, which is what turns N wakes on
                        # one tick into one request: a wake that arrives while
                        # the payload is being fetched waits for that fetch
                        # and takes its answer instead of starting another.
                        #
                        # Even a fresh read may hand back the same verdict: a
                        # provider's meters do not update in the same instant
                        # its window rolls. That is an ordinary outcome and
                        # not a terminal one — the round counts, the next
                        # stamp is read, and the next round tries again. It is
                        # also the case the cadence is sized for: a window
                        # that opens at some moment no probe can predict is
                        # still a window that the next probe finds open.
                        quota_map = _quota_watchdog.probe(
                            blocked=_window_blocked,
                            scene=effective_scene,
                            logger=self.logger,
                        )
                        _scene_diag["wake_rounds"] = _wake_rounds
                        chosen = acquire_provider_with_dynamic_capacity(
                            scene_candidates,
                            self._concurrency_tracker,
                            usage_map,
                            timeout=wait_sec,
                            quota_map=quota_map,
                        )

                finally:
                    # The wait's end is logged with its start, because a
                    # window that was shut when this dispatch gave up and
                    # open by the time the next one looks is a different
                    # story from one that was shut the whole way through,
                    # and only the pair says which happened.
                    if _quota_waiting:
                        _quota_watchdog.wait_left(
                            scene=effective_scene,
                            rounds=_wake_rounds,
                            logger=self.logger,
                        )
                    # The budget starts running again the moment the
                    # wait ends, whether it ended because a slot came
                    # back, because a window opened, or because the
                    # selection raised — in the last case the call is
                    # over, but leaving the budget parked would be a lie
                    # told to whoever is still reading it. That is why
                    # the resume lives here and not at the bottom of the
                    # loop: an exception on any of the three waits leaves
                    # the same debt, and a clock nobody resumes is a clock
                    # that stops being read while the thing it bounds is
                    # still running.
                    if _parked and _work_clock is not None:
                        _work_clock.resume()

                _scene_diag["wake_rounds"] = _wake_rounds
                _scene_diag["slot_wait_rounds"] = _slot_rounds
                _scene_diag["slot_waited_sec"] = round(_slot_waited, 1)

                if chosen is not None:
                    provider_config = scene_configs[chosen]
                    provider_selected = True
                    self.current_call_provider = chosen
                    # Record the slot so the teardown paths can return it.
                    slot_provider = chosen
                    env["ANTHROPIC_BASE_URL"] = provider_config["base_url"]
                    env["ANTHROPIC_API_KEY"] = provider_config["api_key"]
                    env["ANTHROPIC_AUTH_TOKEN"] = provider_config["api_key"]
                    # Model management is delegated to CC Switch: forward the
                    # row's own ANTHROPIC_* model vars verbatim when present.
                    provider_models = provider_config.get("models") or {}
                    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = provider_models.get("sonnet", "")
                    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = provider_models.get("haiku", "")
                    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = provider_models.get("opus", "")
                    env["ANTHROPIC_MODEL"] = provider_models.get("default", "")
                    if self.logger:
                        self.logger.info(
                            "provider_selected",
                            f"Using {chosen} provider via scene routing",
                            data={"provider": chosen, "scene": effective_scene,
                                  "mode": "interactive",
                                  "in_flight": self._concurrency_tracker.current(chosen),
                                  "fleet_in_flight": self._concurrency_tracker.total()},
                        )

        # ---- Fail closed: a scene that approved nothing serves nothing.
        #
        # ``provider_routing.yaml`` is an ALLOWLIST — a tier declares which
        # CC Switch provider names the scene may use, and a name no tier
        # names is not approved for that scene. So while a scene is in
        # effect its chain is the whole candidate set, and "the chain
        # produced nothing" is a fact about configuration, not an
        # invitation to serve the call from elsewhere.
        #
        # Everything reachable from here on is outside the table: the
        # ``provider_priority`` walk is the optimizer chain (built by
        # ``load_fallback_order`` over the whole CC Switch list, with no
        # tier filtering), the constructor's ``base_url`` / ``auth_token``
        # are the run-level provider resolved once for the whole run, and
        # the parent process env is whatever the shell exported. Any of
        # them can answer, so a tier table that does not cover a provider
        # degrades into "that provider serves every scene anyway" — and
        # invisibly, because the ledger column that would have said so
        # (``current_call_provider``) was stamped ``"parent"`` two blocks
        # below, naming a source rather than the endpoint actually used.
        #
        # Raised HERE, before the CC Switch shortcut and the priority walk
        # below, because their own contract already says the scene chain is
        # exclusive: "Scene routing active: the chain is the exclusive
        # candidate set (no cross-tier fallback into the priority walk)".
        # The empty chain was the one case that broke it.
        #
        # The gate is deliberately narrow: it needs a scene AND an
        # unselected provider. A call with no scene has no tier table to
        # violate, so the CC Switch shortcut, the priority walk and the
        # parent fallback below all keep their current behaviour —
        # including when they leave nothing in ``env`` and Claude Code
        # resolves its own configuration. Inherit mode never reaches
        # here: it marks ``provider_selected`` at the top of this function,
        # which is the correct answer to the walk.
        #
        # ``current_call_provider`` is left unset (reset at the top of
        # this call) rather than labelled. The usage registry reads that
        # attribute when it records the attempt, so the ledger row for a
        # failed call says "no provider was selected" instead of claiming
        # the parent environment served it.
        if effective_scene and not provider_selected:
            raise _scene_chain_unavailable(
                effective_scene, _scene_diag, self.logger
            )

        # CC Switch's ACTUAL current provider — asked first (2026-09-21).
        #
        # The walk below historically *guessed*: it iterated the priority
        # list, and a name that did not resolve in the DB silently fell
        # back to the shell env, producing a hybrid — provider name said
        # one thing, the endpoint was CC Switch's proxy, and the model was
        # a hard-coded registry default. Neither the name nor the model
        # described what actually served the request (measured: registry
        # said ``vendor-a-pro`` / ``m2.7`` while CC Switch billed
        # vendor-d-lite for it).
        #
        # CC Switch owns the routing decision, so ask it which provider is
        # current and use that row's own env block (real name, real
        # endpoint, real model ids). Explicit caller intent still wins: an
        # ``PDT_PROVIDER_PRIORITY`` override, an exclusion set (provider
        # fallback), or a ``base_url``/``auth_token`` stamped by the caller
        # caller (``SubagentConfig`` — deliberately excludes the proxy
        # endpoint) all mean somebody already answered this question.
        if (
            not provider_selected
            and not scene_chain
            and not excluded
            and not self.base_url
            and not os.environ.get("PDT_PROVIDER_PRIORITY")
        ):
            cc_provider = ClaudeCodingTool._load_cc_switch_current_provider()
            if cc_provider:
                provider_selected = True
                self.current_call_provider = cc_provider.name
                env["ANTHROPIC_BASE_URL"] = cc_provider.base_url
                env["ANTHROPIC_API_KEY"] = cc_provider.api_key
                env["ANTHROPIC_AUTH_TOKEN"] = cc_provider.api_key
                cc_models = cc_provider.models or {}
                for _var, _key in (
                    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet"),
                    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku"),
                    ("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus"),
                    ("ANTHROPIC_MODEL", "default"),
                ):
                    # Only stamp keys the provider actually declares — an
                    # empty write would clobber the parent's mapping.
                    if cc_models.get(_key):
                        env[_var] = cc_models[_key]
                if self.logger:
                    self.logger.info(
                        "provider_selected",
                        f"Using CC Switch's current provider '{cc_provider.name}'"
                        " (legacy walk shortcut)",
                        data={
                            "provider": cc_provider.name,
                            "model": env.get("ANTHROPIC_MODEL"),
                            "mode": "interactive",
                            "source": "cc_switch_current_provider",
                        },
                    )

        for prov_name in self.provider_priority:
            if provider_selected or scene_chain:
                # Scene routing active: the chain is the exclusive
                # candidate set (no cross-tier fallback into the priority
                # walk). If it selected nothing the call has already
                # failed above — a scene-active call never reaches the
                # parent fallback below, because neither this walk nor
                # the parent environment is on the scene's allowlist.
                # So this break only ends the loop for a healthy scene
                # call that already has its provider.
                break
            if prov_name in excluded:
                continue
            available, provider_config = (
                ClaudeCodingTool._check_provider_availability(prov_name)
            )
            if not (available and provider_config):
                continue
            provider_selected = True
            self.current_call_provider = prov_name
            env["ANTHROPIC_BASE_URL"] = provider_config["base_url"]
            env["ANTHROPIC_API_KEY"] = provider_config["api_key"]
            env["ANTHROPIC_AUTH_TOKEN"] = provider_config["api_key"]
            # Model management is delegated to CC Switch: forward the
            # row's own ANTHROPIC_* model vars verbatim, exactly as the
            # scene path does. An empty value is written as-is rather
            # than substituted — the old code fell back to a hard-coded
            # "m2.7" here, which is how a call could run on one
            # provider's endpoint while ANTHROPIC_MODEL named another
            # provider's model.
            provider_models = provider_config.get("models") or {}
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = provider_models.get("sonnet", "")
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = provider_models.get("haiku", "")
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = provider_models.get("opus", "")
            env["ANTHROPIC_MODEL"] = provider_models.get("default", "")
            if self.logger:
                self.logger.info(
                    "provider_selected",
                    f"Using {prov_name} provider (model_type={self.model_type})",
                    data={"provider": prov_name, "model_type": self.model_type,
                          "model": env["ANTHROPIC_MODEL"], "mode": "interactive"},
                )
            break

        if not provider_selected:
            # Reachable only when NO scene is in effect: a scene-active
            # call that selected nothing has already raised. So this is
            # the answer to "no tier table applies to this call", not a
            # degradation of one.
            #
            # NOTE: an exhausted chain is NOT an exception. There used to
            # be a ``raise NoProviderAvailable("vendor-b-pro is currently
            # degraded due to peak-hour load …")`` branch here; it was
            # unreachable (its predicate was gated on a
            # ``.vendor-b_reset_tracker`` file nothing ever wrote) and the
            # class was deleted 2026-09-24. The parent process config is a
            # valid answer — without CC Switch it is the user's own Claude
            # Code settings — so the run must not die because a provider
            # list went stale. What must not be silent is the degradation:
            # the warning below names the provider that should have served
            # the call. The peak-hour policy itself lives in the
            # optimizer's rule engine, and a rule-avoided provider is
            # simply skipped during the walk above.
            #
            # What this branch must never do is answer for a scene. The
            # scene-active equivalent is the fail-closed raise above,
            # because here the ledger column recording which endpoint
            # served the call would say "parent" — a description of where
            # the configuration came from, not of who answered — while the
            # process env behind it is whatever the shell exported.
            self.current_call_provider = "parent"
            if self.logger:
                # 2026-09-13: include the intended provider and the full
                # priority chain in the structured payload — the B1
                # retrospective fix requires operators to SEE which
                # provider sub-agents were supposed to use when they
                # silently consume parent quota.
                #
                # ``provider_priority[0]`` is the *first preference*,
                # not a fallback: the walk above starts there and only
                # reaches "parent" when every entry in the chain was
                # skipped (excluded / unregistered / unavailable). So
                # the head of the list is exactly "who should have
                # served this call".
                _intended = (
                    self.provider_priority[0] if self.provider_priority else None
                )
                self.logger.warning(
                    "provider_parent_fallback",
                    "No provider in priority list available; using parent process env "
                    f"(intended provider: {_intended})",
                    data={
                        "provider": "parent",
                        "intended_provider": _intended,
                        "provider_priority": list(self.provider_priority),
                    },
                )

        # The provider mapping resolved above (scene → tier → chain) is
        # the SINGLE source of truth for endpoint + credentials. The
        # constructor's model / base_url / auth_token / api_key are a
        # last-resort fallback for calls that resolved no provider at
        # all — hence the ``not provider_selected`` gate.
        #
        # Applying them unconditionally is a second, competing source:
        # ``self.base_url`` / ``self.auth_token`` are stamped at
        # construction from the *run-level* provider, so they silently
        # overwrote the provider chosen here — while ``self.api_key``
        # (not passed by the caller) was empty, leaving the freshly
        # written ``ANTHROPIC_API_KEY`` untouched. The subprocess then
        # got a cross-provider triple (Vendor C endpoint + Vendor C bearer +
        # Vendor A key) and failed with ``API Error: 401`` — blamed on
        # the wrong provider by the ``provider_fallback`` log, because
        # that reports ``current_call_provider``, not the endpoint
        # actually used. (2026-09-14, the 2026-09-04 plan.)
        #
        # Which calls reach here: only the ones with no scene. A
        # scene-active call that resolved no provider has already raised,
        # so the run-level provider — which is resolved once per run from
        # the unfiltered optimizer chain and is therefore on no scene's
        # allowlist — can no longer answer a scene-routed call. For a
        # call with no scene this block is the documented behaviour: the
        # run-level provider is exactly the answer the caller configured.
        if not provider_selected:
            if self.model:
                cmd += ["--model", self.model]
                env["ANTHROPIC_MODEL"] = self.model
            if self.base_url:
                env["ANTHROPIC_BASE_URL"] = self.base_url
            if self.auth_token:
                env["ANTHROPIC_AUTH_TOKEN"] = self.auth_token
            if self.api_key:
                env["ANTHROPIC_API_KEY"] = self.api_key
            elif self.base_url or self.auth_token:
                # ``self.*`` is deliberately re-pointing the endpoint at
                # one specific provider; a key inherited from the parent
                # env (or an earlier provider) would contradict it. Drop
                # it rather than ship a cross-provider triple.
                env.pop("ANTHROPIC_API_KEY", None)

        # Settings-file write. MUST be the last thing that touches
        # ``env`` — ``claude --settings`` env outranks the process env
        # (verified: a request served entirely from the settings file
        # succeeds even when the process env points at an unreachable
        # host). Writing the file before provider selection pins the
        # pre-routing ``ANTHROPIC_BASE_URL`` — in this fleet that is the
        # CC Switch proxy inherited from the parent process env — so
        # every subagent call silently bypasses the scene-routed
        # provider and is served by whatever CC Switch currently has
        # selected. Symptom: server logs report a Vendor A dispatch while CC
        # Switch's usage table shows zero Vendor A requests (2026-09-13
        # investigation).
        # 2026-09-17 — everything from the settings write through the watcher startup sits
        # inside a try that returns the pool slot on ANY exit. The read loop below has
        # its own finally (which also releases); this arm covers the narrow window
        # before it — a failed settings write or a Popen that raises would otherwise
        # leak the slot permanently, and a leaked slot never comes back: the provider
        # would report itself full for the life of the process.
        #
        # The same arm is also where the credential has to be dropped: the
        # settings file is written below, so an exception anywhere between
        # that write and the read loop's own ``finally`` leaves a live key
        # on disk that nothing else will ever revisit. ``process`` starts
        # as ``None`` because the ``Popen`` below may be exactly what
        # raised.
        process: Optional[subprocess.Popen] = None
        try:
            if settings_data is not None:
                # Copy every ANTHROPIC_* key the routing layer / ``self.*``
                # overrides put into ``env``. The list is deliberately an
                # explicit allowlist rather than an ``ANTHROPIC_*`` prefix
                # sweep: the parent process env carries unrelated vendor
                # keys (``ANTHROPIC_BASE_URL_VENDOR_B`` …) that must not be
                # duplicated into a per-subagent tmpfile. ``ANTHROPIC_MODEL``
                # is included because the scene router writes it
                # (``env["ANTHROPIC_MODEL"] = provider_models.get("default")``)
                # and ``--settings`` outranks the process env.
                # Inherit mode: copy nothing.
                #
                # The subtle part is that *copying* is itself an
                # override. ``env`` is ``os.environ.copy()``, so on a
                # machine where cc-switch has exported
                # ``ANTHROPIC_BASE_URL`` (pointing at its own proxy)
                # into the shell, this loop would promote that value into
                # ``--settings`` — and ``--settings`` outranks
                # ``~/.claude/settings.json``. The user's own file would
                # lose to an environment variable they set for a
                # different tool, and nothing in the dispatch would look
                # wrong.
                if not self.inherit_providers:
                    for _key in (
                        "ANTHROPIC_BASE_URL",
                        "ANTHROPIC_API_KEY",
                        "ANTHROPIC_AUTH_TOKEN",
                        "ANTHROPIC_MODEL",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                        "ANTHROPIC_DEFAULT_OPUS_MODEL",
                        "ANTHROPIC_DEFAULT_SUMMARIZE_MODEL",
                    ):
                        if _key in env:
                            settings_data["env"][_key] = env[_key]
                # Private 0700 directory + 0600 file: ``settings_data``
                # carries the routed provider's ANTHROPIC_API_KEY /
                # ANTHROPIC_AUTH_TOKEN, and the previous flat
                # ``/tmp/subagent_settings_<uuid>.json`` was written with
                # the default mode (world-readable). See
                # ``utils.secret_files``.
                effective_settings_path = (
                    private_dir() / f"subagent_settings_{subagent_uuid}.json"
                )
                write_private_json(effective_settings_path, settings_data)

            if effective_settings_path is not None:
                cmd += ["--settings", str(effective_settings_path)]
            # No ``elif self.settings is not None`` fallback, deliberately.
            # It used to forward the raw template path to the CLI, which
            # handed a path that may no longer exist: the CLI validates
            # ``--settings`` before any model call, so the subprocess died
            # in ~0.2s and the read loop could only report "exited without
            # assistant text" — a provider-shaped message for a swept local
            # file. That arm is now unreachable: ``settings_data`` is
            # non-None whenever ``self.settings`` is set (enforced at the
            # template read, and a template that yields no object at all is
            # rejected there too), the write block above runs whenever
            # ``settings_data`` is non-None, and ``effective_settings_path``
            # is assigned unconditionally inside it — so reaching the ``elif``
            # would require ``self.settings is None``, a contradiction.
            #
            # The invariant is asserted at the read site rather than here
            # because a raise from inside the ``try`` above would be masked:
            # its ``finally`` calls ``_graceful_shutdown(process)`` with
            # ``process`` still ``None``, and the resulting ``AttributeError``
            # would replace the real message with a shutdown failure.

            # 2026-10-06: confine the sub-agent when the operator
            # configured a sandbox. `bypassPermissions` removes the
            # confirmation prompts, so without this the agent has
            # unrestricted filesystem access with no gate anywhere in
            # the path. Fails loudly rather than falling back — see
            # backend/claude_sandbox.py for why a sandbox that quietly
            # disables itself is the worse outcome.
            _sandbox = claude_sandbox.wrap_command(cmd, env)
            cmd = _sandbox.command
            if self.logger:
                if _sandbox.reason == "applied":
                    self.logger.info(
                        "subagent_sandboxed",
                        f"Sub-agent running under {_sandbox.profile}",
                        data={"profile": str(_sandbox.profile)},
                    )
                else:
                    self.logger.info(
                        "subagent_sandbox_absent",
                        "No sandbox configured; sub-agent runs unsandboxed",
                        data={"profile": None},
                    )

            # Launch subprocess. Interactive mode: stdin is plain text,
            # write the prompt then close stdin (which signals the LLM
            # to terminate with a result event).
            self.current_call_model = env.get("ANTHROPIC_MODEL")
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=env,
                start_new_session=True,
            )

            with self._process_lock:
                self._current_process = process

            # Feed the user prompt via stdin, then close.
            try:
                process.stdin.write(prompt)
                if not prompt.endswith("\n"):
                    process.stdin.write("\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    pass

            # Idle timer — Layer-1 protection (close stdout pipe on inactivity).
            idle_sec = idle_timeout if idle_timeout is not None else self.DEFAULT_IDLE_TIMEOUT
            idle_fired = threading.Event()
            last_line_time = [time.monotonic()]

            def _idle_timer():
                # 2026-09-14 — stamp this watcher's own start and clamp the
                # silence baseline to it. ``last_line_time`` is shared with
                # the total timer and is stamped ~16 statements earlier, so
                # any delay in between (a process freeze, a slow
                # ``Thread.start()``, a clock discontinuity) used to make the
                # idle watcher see more "silence" than had actually occurred.
                # With ``idle_sec=1800`` that read as "idle for the whole
                # window" and closed stdout immediately, which the read loop
                # then reported as an idle timeout.
                started_at = time.monotonic()
                while not idle_fired.is_set():
                    baseline = max(last_line_time[0], started_at)
                    elapsed = time.monotonic() - baseline
                    remaining = idle_sec - elapsed
                    if remaining <= 0:
                        break
                    idle_fired.wait(timeout=max(0.1, remaining))
                if not idle_fired.is_set():
                    try:
                        process.stdout.close()
                    except Exception:
                        pass

            if idle_sec > 0:
                timer_thread = threading.Thread(target=_idle_timer, daemon=True)
                timer_thread.start()

            # 2026-09-06 — Layer-1b total-timeout watcher. Idle timer only fires
            # when stdout goes silent; a wedged provider can keep emitting
            # "thinking" deltas forever, holding the GIL on the read loop and
            # blocking the verification iteration loop's Phase 2. The watcher
            # force-closes stdout + SIGTERMs the process so the read loop's
            # BlockingError bubbles up to query() as a normal TimeoutError →
            # provider fallback re-routes instead of hanging the auto-loop.
            #
            # 2026-09-15 CORRECTION — the original wording here claimed this was
            # a "hard wall-clock cap: even if the LLM is actively streaming".
            # That has NOT been true since the 2026-09-08 change: the timer now
            # measures SILENCE against ``last_line_time`` (clamped to its own
            # start), so a call that keeps emitting stdout — pytest progress,
            # thinking deltas, tool events — never trips it. That is the
            # intended behaviour: "alive and printing" must not be killed. The
            # absolute ceiling is the layer above (VerificationSubAgent's
            # 1-hour cap; see HARD_WALL_CLOCK_TIMEOUT_SECONDS).
            total_sec = total_timeout if total_timeout is not None else self.DEFAULT_TOTAL_TIMEOUT
            total_fired = threading.Event()
            total_started_at = [time.monotonic()]
            # Active marker for the read loop — signals "we already SIGTERM'd
            # due to wall-clock cap, don't try to read past EOF, just surface
            # the timeout error."
            total_triggered = [False]

            def _total_timer():
                """Adaptive total-timeout watcher.

                2026-09-08 plan — the legacy implementation waited a fixed
                ``total_sec`` regardless of subprocess activity. That hard
                wall-clock cap was the right call for "wedged provider
                emits thinking-deltas forever", but it broke legitimate
                long-running subprocesses (e.g. ``pytest backend/tests/``
                with 1,482 tests — runs 30+ min, well past the 15-min
                default cap).

                New behaviour: the timer fires only when there's been NO
                stdout for ``total_sec`` seconds. Every line the read loop
                pulls from ``process.stdout`` updates ``last_line_time``,
                which the timer consults via the closure-captured list.
                Effect:
                  * Active subprocess (pytest emitting progress): the
                    idle window never elapses → timer never fires.
                  * Hung subprocess (no output for 15 min): timer fires
                    exactly like the legacy hard cap.
                  * Wedged provider that emits only thinking-deltas
                    (text events without tool calls): each text event
                    also resets the timer. The previous "wedged
                    provider" concern is now mitigated by the *idle*
                    timer on line 1075 (separate from this one), which
                    closes stdout on inactivity — once stdout closes,
                    the read loop sees EOF, the query returns, and
                    ``_total_timer`` exits via ``total_fired.set()``.

                The cap value itself (``total_sec``) is unchanged —
                15 min by default. It is a hard bound, not a per-task
                tuning knob.
                """
                # 2026-09-14 (bogus-HardTimeout fix) — stamp this watcher's
                # own start as the first statement of the thread body, and
                # clamp the silence baseline to it.
                #
                # ``last_line_time`` is stamped ~30 statements *before*
                # ``total_started_at`` (it is shared with the idle timer,
                # which needs it earlier), and the read loop only re-stamps
                # it once the subprocess emits its first line.  Any delay
                # between the two stamps — a process freeze, a slow
                # ``Thread.start()``, a clock discontinuity — therefore
                # showed up as *silence that predates the timer*, and the
                # watcher fired immediately with an internally impossible
                # ``elapsed`` (``total_sec`` non-zero, ``elapsed`` near
                # zero), killing the report-generation and
                # repair-generation LLM calls in flight.
                #
                # ``max(...)`` states the real invariant: the watcher may
                # only account for silence that began while it was already
                # running.  In the healthy case the stamps are microseconds
                # apart, so behaviour is unchanged.
                started_at = time.monotonic()
                try:
                    while not total_fired.is_set():
                        silence_baseline = max(last_line_time[0], started_at)
                        time_since_progress = time.monotonic() - silence_baseline
                        if time_since_progress > total_sec:
                            # No progress for the full cap → fire.
                            break
                        # Otherwise wait until the cap would elapse
                        # *from the last progress point*, then re-check.
                        remaining = total_sec - time_since_progress
                        # Cap each wake-up at 1s so we react quickly to
                        # ``total_fired.set()`` from the read loop on
                        # clean EOF.
                        total_fired.wait(timeout=min(remaining, 1.0))
                except Exception:
                    return
                # 2026-09-14 — the loop also exits when ``total_fired`` is
                # set by the read loop (clean EOF, a ``result`` event, the
                # idle watcher closing stdout, an escaped ``ApiError``, …).
                # Without this guard the watcher flagged every such exit as
                # "the cap fired", and a subprocess that died before
                # producing any text was reported as a bogus
                # ``HardTimeoutError``.  The contract is the one the
                # in-tree replica always had — the production body had
                # simply diverged from it.
                if total_fired.is_set():
                    return
                # Wall-clock cap exceeded. Two-step unblock:
                # 1. Force-kill the entire process group so the subprocess
                #    stops writing to the pipe. Without this, an actively
                #    streaming LLM would keep filling the kernel pipe buffer
                #    and the read loop would block on read() of buffered data
                #    for the full buffer-drain window (~seconds).
                # 2. Close stdout from the parent's side so the read loop's
                #    ``for line in process.stdout:`` raises ValueError
                #    immediately after draining whatever's left in the buffer.
                # We deliberately do NOT use ``_graceful_shutdown`` here
                # because it calls ``process.wait(timeout=120)`` which would
                # block the watcher thread for up to 120s. The watcher is
                # only useful if it returns quickly so the main thread's
                # read loop can detect the closed pipe and raise TimeoutError.
                total_triggered[0] = True
                try:
                    kill_process_group(process, sig=signal.SIGKILL)
                except Exception:
                    # Fallback to direct kill if group kill fails
                    try:
                        process.kill()
                    except Exception:
                        pass
                try:
                    process.stdout.close()
                except Exception:
                    pass

            if total_sec > 0:
                total_thread = threading.Thread(target=_total_timer, daemon=True)
                total_thread.start()
            else:
                total_thread = None

            # Read the stream-json event stream. Interactive mode emits:
            #   system init       — session_id, available tools, model, etc
            #   system thinking_tokens — incremental thinking metrics
            #   assistant         — assistant turn content (text + tool_use)
            #   user              — synthetic tool_result messages
            #   result            — final turn summary, success or error
            # We capture session_id from system init, accumulate assistant
            # text, and break on result.
        except BaseException:
            _release_scene_slot()
            # The settings file may already be on disk (the write above
            # precedes the ``Popen``), and whatever raised here is about
            # to leave this function, so nothing further down will get a
            # chance to revisit it. Reap a child if one exists — a
            # successful ``Popen`` followed by a raised watcher setup
            # reaches this arm too — and drop the credential either way.
            # ``redact_all`` never raises, so it cannot displace the
            # exception being propagated.
            try:
                if process is not None:
                    self._graceful_shutdown(process)
            finally:
                redact_all(
                    [effective_settings_path],
                    logger=logger,
                )
            raise
        result_text = ""
        # Usage metadata captured from the stream-json ``result`` event
        # (usage / total_cost_usd / num_turns / durations). Surfaced to
        # the caller as the third return tuple element and recorded in
        # the per-plan usage registry (2026-09-21).
        result_meta: Dict[str, Any] = {}
        resolved_session_id: Optional[str] = session_id
        read_error = None
        try:
            for line in process.stdout:
                last_line_time[0] = time.monotonic()
                # Mirror to self._last_output_ts so the sub-agent watchdog
                # (server.py::_handle_stuck_sub_agent) can tell an active
                # sub-agent (still streaming tool calls / text) apart from
                # a wedged one (no stdout output for the threshold window).
                # Without this mirror, the watchdog only saw
                # SubAgentHandle.last_progress_ts — which the sub-agent
                # only touches on stage transitions, so a 12-minute pytest
                # re-run inside one attempt looked "stale" and got killed.
                self._last_output_ts = last_line_time[0]
                self._last_output_line = line[:400]
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    event_type = event.get("type", "")

                    if event_type == "system":
                        # The init subtype carries the session_id and
                        # the tools list. Subsequent system events
                        # are thinking_tokens / permission_denied.
                        if event.get("subtype") == "init":
                            resolved_session_id = event.get("session_id") or resolved_session_id
                        continue

                    if event_type == "assistant":
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                result_text += block.get("text", "")
                            elif block.get("type") == "tool_use":
                                # 2026-09-08: forward tool_use to the
                                # main log so the operator can see what
                                # subprocess the Claude sub-agent is
                                # launching. Without this, a 30-min
                                # ``pytest backend/tests/`` run is
                                # invisible until query() returns.
                                tool_name = block.get("name", "?")
                                tool_input = block.get("input", {}) or {}
                                # Compact form: tool name + first 200
                                # chars of input JSON. We deliberately
                                # omit full input to avoid logging
                                # sensitive data (the input can
                                # contain arbitrary user-provided
                                # content in some tools).
                                try:
                                    input_summary = json.dumps(
                                        tool_input, ensure_ascii=False,
                                    )[:200]
                                except Exception:
                                    input_summary = "<unserialisable>"
                                logger.info(
                                    "[CLAUDE STREAM] tool_use name=%s "
                                    "input=%s",
                                    tool_name,
                                    input_summary,
                                )
                        continue

                    if event_type == "user":
                        # Synthetic tool_result messages: Claude just
                        # got the output of a tool it called (e.g.
                        # ``pytest backend/tests/`` stdout/stderr).
                        # 2026-09-08: forward the tool_result
                        # content to the main log so the operator
                        # sees pytest progress in real-time — without
                        # this, a 30-min ``pytest`` run inside Claude
                        # is invisible until ``query()`` returns.
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_result":
                                tool_use_id = block.get("tool_use_id", "?")
                                result_body = block.get("content", "")
                                # ``content`` is either a string or a
                                # list of content blocks. Normalise.
                                if isinstance(result_body, list):
                                    result_body = "".join(
                                        b.get("text", "") if isinstance(b, dict) else str(b)
                                        for b in result_body
                                    )
                                # Truncate to 300 chars — pytest emits
                                # thousands of ``PASSED`` lines and we
                                # don't want each one in the main log.
                                summary = str(result_body)[:300]
                                logger.info(
                                    "[CLAUDE STREAM] tool_result "
                                    "tool_use_id=%s content=%r",
                                    tool_use_id,
                                    summary,
                                )
                                # Track non-empty results so we
                                # don't double-log the same content.
                                # (No-op for now — placeholder for
                                # future dedup if needed.)
                                _ = bool(summary.strip())
                        continue

                    if event_type == "result":
                        # Capture usage metadata BEFORE the is_error
                        # branch — an error result can still carry the
                        # usage/cost fields, and the registry records
                        # both outcomes for the cross-check against CC
                        # Switch's own ledger.
                        result_meta = {
                            "usage": event.get("usage"),
                            "total_cost_usd": event.get("total_cost_usd"),
                            "num_turns": event.get("num_turns"),
                            "duration_ms": event.get("duration_ms"),
                            "duration_api_ms": event.get("duration_api_ms"),
                            "is_error": bool(event.get("is_error")),
                        }
                        if event.get("is_error"):
                            api_status = event.get("api_error_status", "unknown")
                            error_text = event.get("result", "")
                            idle_fired.set()
                            self._graceful_shutdown(process)
                            raise ApiError(
                                message=error_text or f"Claude API returned error status: {api_status}",
                                status=api_status,
                                retry_after=None,
                            )
                        # Use result field as final fallback if no
                        # assistant text accumulated (rare — LLM
                        # responded via tool only).
                        if not result_text:
                            result_text = event.get("result", "")
                        break

                except json.JSONDecodeError:
                    pass
        except ValueError as e:
            read_error = e
        finally:
            # 2026-09-14 — cancel BOTH watchers no matter how the read
            # loop exited. Previously the two ``set()`` calls sat after
            # the ``except ValueError`` handler only, so an exception
            # other than ``ValueError`` (notably the ``ApiError`` raised
            # further down the loop body) skipped them: the watcher
            # thread then stayed alive for the life of the process,
            # holding a reference to a dead ``Popen`` and eventually
            # SIGKILLing its (possibly recycled) pid.
            idle_fired.set()
            total_fired.set()  # cancel total timer on clean exit
            # 2026-09-17 — hand the provider slot back. This runs on the
            # clean path, on the idle/total-timeout paths, and on the
            # ``ApiError`` raised from inside the loop, which is exactly
            # the set of ways a dispatch can end. Idempotent, so the
            # setup-region handler above may also have released it.
            _release_scene_slot()
            # Reap the child, then drop the credential. Both belong in
            # this ``finally`` for the same reason the three statements
            # above do: ``ApiError`` is not a ``ValueError``, so it
            # escapes the ``except`` clause entirely and carries control
            # past anything written after the statement. That is the
            # ordinary provider-error exit — the raise site reaps the
            # child itself, but the redaction used to sit after the
            # whole statement, where that exit never reached it.
            #
            # The order is fixed by ``utils.secret_files``:
            # ``coding_tool_hooks/pre_tool_use.sh`` reads
            # ``$CLAUDE_SETTINGS_PATH`` on every tool call, so the
            # credential may only be replaced once the child is gone.
            # Reaping twice on the raise path is harmless —
            # ``_graceful_shutdown`` returns as soon as ``wait`` reports
            # the child gone. The ``finally`` on the shutdown is what
            # keeps the credential from surviving a shutdown that itself
            # raises.
            #
            # ONLY ``effective_settings_path`` — the file written above
            # for this one child. ``self.settings`` is deliberately
            # excluded: it is a caller-owned *input* that a later
            # ``query()`` re-reads (``agent.py`` reassigns it once per
            # task, but a task can make more than one call). Redacting
            # it would hand the next dispatch ``<redacted>`` as its API
            # key and silently break routing — caught by
            # ``test_coding_tool_settings_write_order``.
            try:
                self._graceful_shutdown(process)
            finally:
                redact_all(
                    [effective_settings_path],
                    logger=logger,
                )

        # Layer 3: stderr capture — do this BEFORE the total-timeout check
        # so we can include stderr context in the timeout error message.
        try:
            stderr_data = process.stderr.read(4096)
        except Exception:
            stderr_data = ""
        finally:
            with self._process_lock:
                self._current_process = None

        # 2026-09-06 — If the read loop exited because the total-timeout
        # watcher SIGTERM'd the subprocess (rather than a clean ``result``
        # event), surface a distinct error so the caller can distinguish
        # "LLM is slow" from "wedged provider hung the verification loop".
        #
        # 2026-09-08 plan — emit ``HardTimeoutError`` (subclass of
        # ``TimeoutError``) so the verification / task-execution layers
        # can route this through auto-split / auto-refine. Generic
        # ``asyncio.TimeoutError`` from the outer 1-hour cap still
        # surfaces as a normal ``TimeoutError``.
        if total_triggered[0] and not result_text:
            elapsed = time.monotonic() - total_started_at[0]
            # Last line the subprocess emitted before silence (for diagnostics).
            try:
                last_line = (result_text.rsplit("\n", 2)[-2]
                             if "\n" in result_text else result_text[-200:])
            except Exception:
                last_line = ""
            if elapsed < total_sec:
                # 2026-09-14 — unreachable by construction once the
                # watcher clamps its baseline to ``total_started_at``
                # (see ``_total_timer``), kept as a hard guard so a
                # future regression cannot resurrect the bogus
                # ``HardTimeoutError``: a subprocess aborted by our own
                # watcher while less than ``total_sec`` has elapsed
                # since the watcher started has NOT been idle for the
                # cap, so routing it through the auto-split path would
                # be a lie. Surface a plain ``TimeoutError`` instead.
                logger.error(
                    "[HARD TIMEOUT GUARD] total_sec=%d elapsed=%.1fs — "
                    "watcher fired before the cap had elapsed; refusing to "
                    "raise HardTimeoutError. last_line=%r",
                    total_sec, elapsed, last_line[:200],
                )
                raise TimeoutError(
                    f"Claude sub-process killed by the total-timeout watcher "
                    f"after only {elapsed:.1f}s (cap {total_sec}s); the "
                    f"silence window was not real — see [HARD TIMEOUT GUARD]"
                )
            logger.warning(
                "[HARD TIMEOUT total_sec=%d elapsed=%.1fs last_line=%r] "
                "subprocess silent — raising HardTimeoutError for auto-split routing",
                total_sec, elapsed, last_line[:200],
            )
            raise HardTimeoutError(
                total_sec=total_sec,
                elapsed=elapsed,
                last_line=last_line,
            )

        if read_error is not None and not result_text:
            raise TimeoutError(
                f"Claude sub-process idle timeout ({idle_sec}s) in interactive mode: "
                f"no stdout activity for too long. Stderr: {stderr_data[:500]}"
            )

        # 2026-09-17 — a clean exit with no assistant text is a FAILURE,
        # not an empty success. Returning ``""`` here is what made every
        # downstream JSON caller report "no JSON object / array
        # boundaries found" (a parse error) for what was really "the CLI
        # produced nothing" — a misdiagnosis that cost a whole round of
        # an earlier plan. The stderr this function had already
        # collected was discarded on this path; it is now the payload.
        #
        # ``logger`` here is the MODULE logger, not ``self.logger``:
        # ``create_coding_tool(..., scene=...)`` does not pass a logger
        # on the verification path, so a ``self.logger``-only warning
        # would be a no-op exactly where it is needed most.
        if not (result_text or "").strip():
            try:
                returncode = process.returncode
            except Exception:
                returncode = None
            stderr_excerpt = (stderr_data or "").strip()
            elapsed = time.monotonic() - total_started_at[0]
            logger.warning(
                "[EMPTY RESPONSE] provider=%s returncode=%s elapsed=%.1fs "
                "stdout_len=%d scene=%s stderr=%r",
                self.current_call_provider, returncode, elapsed,
                len(result_text or ""), scene or self.scene,
                stderr_excerpt[:300],
            )
            if self.logger:
                self.logger.warning(
                    "coding_empty_response",
                    f"Claude sub-process exited without assistant text "
                    f"(provider={self.current_call_provider}, "
                    f"returncode={returncode}, elapsed={elapsed:.1f}s)",
                    data={
                        "provider": self.current_call_provider,
                        "returncode": returncode,
                        "elapsed_sec": round(elapsed, 1),
                        "stdout_len": len(result_text or ""),
                        "stderr": stderr_excerpt[:500],
                        "scene": scene or self.scene,
                    },
                )
            raise EmptyResponseError(
                f"Claude sub-process exited without assistant text "
                f"(provider={self.current_call_provider}, returncode={returncode}, "
                f"elapsed={elapsed:.1f}s). Stderr: {stderr_excerpt[:500] or '<empty>'}",
                provider=self.current_call_provider,
                returncode=returncode,
                stderr=stderr_excerpt,
                elapsed_sec=elapsed,
                stdout_len=len(result_text or ""),
            )

        return result_text, resolved_session_id, result_meta

    @staticmethod
    def _estimate_tokens(text: Optional[str]) -> int:
        """Roughly estimate token count from text length.

        Uses a blended heuristic: English ~4 chars/token, CJK ~1.5 chars/token.
        Falls back to len/3 for mixed content. This is intentionally fast
        (no tiktoken dependency) and good enough for log diagnostics.
        """
        if not text:
            return 0
        # Fast heuristic: average ~3 chars per token for mixed content
        return max(1, len(text) // 3)

    def _compact_prompt(self, prompt: str, system_instruction: Optional[str] = None) -> str:
        """Compress prompt to stay under MAX_PROMPT_TOKENS.

        Three-layer strategy (mirrors Claude Code context compression):
        1. Truncate individual file content blocks
        2. Drop oldest file content blocks
        3. Generate structured summary as last resort
        """
        import re

        sys_tokens = self._estimate_tokens(system_instruction)
        total_tokens = self._estimate_tokens(prompt) + sys_tokens
        if total_tokens <= self.MAX_PROMPT_TOKENS:
            return prompt

        if self.logger:
            self.logger.info(
                "coding_prompt_compact",
                f"Prompt compact triggered: {total_tokens} tokens > {self.MAX_PROMPT_TOKENS} limit",
                data={"before_tokens": total_tokens, "limit": self.MAX_PROMPT_TOKENS},
            )

        # --- Layer 1: Truncate individual file content blocks ---
        file_blocks = list(re.finditer(self.FILE_BLOCK_RE, prompt))
        compacted = prompt
        files_truncated = 0
        tokens_saved = 0

        for match in reversed(file_blocks):  # process largest files first
            content = match.group("content")
            content_tokens = self._estimate_tokens(content)
            if content_tokens <= self.MAX_TOKENS_PER_FILE:
                continue

            # Truncate to max tokens, keeping the head (setup/imports are usually at top)
            char_budget = self.MAX_TOKENS_PER_FILE * 3 - len(self.TRUNCATION_MARKER)
            truncated = content[:char_budget] + self.TRUNCATION_MARKER
            compacted = compacted[:match.start("content")] + truncated + compacted[match.end("content"):]
            tokens_saved += content_tokens - self._estimate_tokens(truncated)
            files_truncated += 1

        total_tokens = self._estimate_tokens(compacted) + sys_tokens
        if total_tokens <= self.MAX_PROMPT_TOKENS:
            if self.logger:
                self.logger.info(
                    "coding_prompt_compact_done",
                    f"Layer 1 truncation: {files_truncated} file(s) truncated, "
                    f"saved ~{tokens_saved} tokens, final={total_tokens}",
                    data={"files_truncated": files_truncated, "tokens_saved": tokens_saved, "after_tokens": total_tokens},
                )
            return compacted

        # --- Layer 2: Drop oldest file content blocks ---
        file_blocks = list(re.finditer(self.FILE_BLOCK_RE, compacted))
        files_dropped = 0
        tokens_saved_layer2 = 0

        # Drop from oldest (earliest in prompt) to newest
        for match in file_blocks:
            full_block = match.group(0)
            block_tokens = self._estimate_tokens(full_block)
            path = match.group("path").strip()
            placeholder = f"[File {path} omitted for context limit — use Read if needed]\n"
            compacted = compacted[:match.start()] + placeholder + compacted[match.end():]
            tokens_saved_layer2 += block_tokens - self._estimate_tokens(placeholder)
            files_dropped += 1

            total_tokens = self._estimate_tokens(compacted) + sys_tokens
            if total_tokens <= self.MAX_PROMPT_TOKENS - self.COMPACT_BUFFER_TOKENS:
                break

        if self.logger:
            self.logger.info(
                "coding_prompt_compact_done",
                f"Layer 2 drop: {files_dropped} file(s) dropped, "
                f"Layer 1 truncated {files_truncated}, final={total_tokens}",
                data={
                    "files_truncated": files_truncated,
                    "files_dropped": files_dropped,
                    "tokens_saved_l1": tokens_saved,
                    "tokens_saved_l2": tokens_saved_layer2,
                    "after_tokens": total_tokens,
                },
            )

        total_tokens = self._estimate_tokens(compacted) + sys_tokens
        if total_tokens <= self.MAX_PROMPT_TOKENS:
            return compacted

        # --- Layer 3: Last-resort hard truncation of the entire prompt ---
        # Keep head (instructions are usually at the top) and tail (most recent context)
        char_budget = self.MAX_PROMPT_TOKENS * 3
        head = compacted[:char_budget // 2]
        tail = compacted[-char_budget // 2:] if len(compacted) > char_budget else ""
        summary = (
            "[Prompt truncated for context limit. "
            "Key instructions preserved at top; recent context at bottom.]\n\n"
        )
        compacted = summary + head + "\n\n[... middle content omitted ...]\n\n" + tail

        total_tokens = self._estimate_tokens(compacted) + sys_tokens
        if self.logger:
            self.logger.warning(
                "coding_prompt_compact_hard",
                f"Layer 3 hard truncation applied, final={total_tokens}",
                data={"after_tokens": total_tokens, "files_truncated": files_truncated, "files_dropped": files_dropped},
            )
        return compacted

    def _log_query_context(self, prompt: str, system_instruction: Optional[str],
                          phase: str = "query") -> None:
        """Log prompt + system context size in tokens for diagnostics."""
        if not self.logger:
            return
        prompt_tokens = self._estimate_tokens(prompt)
        sys_tokens = self._estimate_tokens(system_instruction)
        total_tokens = prompt_tokens + sys_tokens
        self.logger.info(
            f"coding_{phase}_context",
            f"Context size: prompt={prompt_tokens} sys={sys_tokens} total={total_tokens} tokens (~{len(prompt)} chars)",
            data={
                "phase": phase,
                "prompt_tokens_est": prompt_tokens,
                "system_tokens_est": sys_tokens,
                "total_tokens_est": total_tokens,
                "prompt_chars": len(prompt),
                "system_chars": len(system_instruction) if system_instruction else 0,
            },
        )

    def _graceful_shutdown(self, process: subprocess.Popen) -> None:
        """Three-phase shutdown: stdin EOF → wait → SIGTERM group → SIGKILL."""
        # Phase 1: close stdin to signal EOF
        try:
            process.stdin.close()
        except Exception:
            pass

        # Wait for graceful exit
        try:
            process.wait(timeout=self.GRACEFUL_STOP_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            pass

        # Phase 2: SIGTERM the entire process group
        if kill_process_group(process, sig=signal.SIGTERM) is not None:
            return

        # Phase 3: SIGKILL (last resort)
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass

    def cleanup(self) -> None:
        """Kill any in-flight subprocess and clean up resources.

        Safe to call multiple times. Used by agent finally blocks and
        server shutdown handlers to prevent orphaned claude processes.
        """
        with self._process_lock:
            proc = self._current_process
            self._current_process = None
        if proc is not None and proc.poll() is None:
            self._graceful_shutdown(proc)

    # ================================================================
    # M1 path for query(): identical retry / fallback semantics to
    # ``_query_json_internal`` (lines 2023-2108), but driven directly
    # from ``_run_claude_interactive`` instead of the legacy
    # (the legacy ``_run_claude`` --print path was deleted; the ``_run_claude_interactive``
    #
    # One-shot per call: ``session_id=None`` (no resume), so retries
    # always start a fresh conversation. Caller is responsible for any
    # multi-turn resume semantics (currently nothing in this codebase
    # needs them on the ``query()`` path).
    # ================================================================
    def _record_llm_call(
        self,
        *,
        session_id: Optional[str],
        scene: Optional[str],
        ok: bool,
        meta: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record one LLM call attempt in the per-plan usage registry.

        Never raises: the registry is a logging side channel and must
        not break a dispatch (see ``usage_registry.record_llm_call``).
        """
        meta = meta or {}
        try:
            _record_usage_entry(
                session_id=session_id,
                scene=scene or self.scene,
                provider=getattr(self, "current_call_provider", None),
                model=getattr(self, "current_call_model", None),
                ok=ok,
                usage=meta.get("usage"),
                cost_usd=meta.get("total_cost_usd"),
                num_turns=meta.get("num_turns"),
                duration_ms=meta.get("duration_ms"),
                error=error,
            )
        except Exception:
            pass

    def _run_interactive_resilient(
        self,
        prompt: str,
        system_instruction: Optional[str],
        *,
        session_id: Optional[str],
        resume: bool,
        excluded_providers: Optional[list],
        total_timeout: Optional[int],
        allowed_tools: Optional[list],
        scene: Optional[str],
        fresh_session_on_retry: bool = True,
    ) -> tuple:
        """``_run_claude_interactive`` with a fresh-session retry on empty output.

        Only :class:`EmptyResponseError` is retried here, and the retry
        intentionally uses a **new** session id with ``resume=False``.
        That is the whole point of separating this error from the parse
        failures: an empty reply means the session never produced
        anything, so ``--resume``-ing it is guaranteed to fail (measured
        at ~0.4s, ``Invalid session ID``). The 2026-09-17 round-2
        repair-generation failure burned its one built-in retry exactly
        that way.

        Everything else (timeout, API error, hard timeout) propagates
        untouched so the existing provider-fallback handlers keep
        owning those cases.

        Callers that need a *format* retry (parse failed on non-empty
        prose) still do their own ``--resume`` follow-up; this helper is
        orthogonal to that and only guards the empty case.
        """
        attempt = 0
        while True:
            try:
                result_text, resolved_session_id, result_meta = self._run_claude_interactive(
                    prompt,
                    system_instruction,
                    session_id=session_id,
                    resume=resume,
                    excluded_providers=excluded_providers,
                    total_timeout=total_timeout,
                    allowed_tools=allowed_tools,
                    scene=scene,
                )
                self._record_llm_call(
                    session_id=resolved_session_id,
                    scene=scene,
                    ok=True,
                    meta=result_meta,
                )
                return result_text, resolved_session_id
            except EmptyResponseError as exc:
                # Session never produced anything — CC Switch will have
                # no rows for it either; record it so the cross-check
                # can distinguish "dead session" from "scanner lag".
                self._record_llm_call(
                    session_id=session_id,
                    scene=scene,
                    ok=False,
                    error="empty_response",
                )
                if attempt >= self.MAX_EMPTY_RESPONSE_RETRIES:
                    raise
                attempt += 1
                backoff = self.EMPTY_RESPONSE_RETRY_BACKOFF_SEC * attempt
                logger.warning(
                    "[EMPTY RESPONSE RETRY %d/%d] provider=%s returncode=%s "
                    "elapsed=%.1fs — retrying on a %s session in %.0fs "
                    "(resume would target a session that never existed). stderr=%r",
                    attempt, self.MAX_EMPTY_RESPONSE_RETRIES,
                    exc.provider, exc.returncode, exc.elapsed_sec,
                    "FRESH" if fresh_session_on_retry else "resumed",
                    backoff, exc.stderr[:200],
                )
                if self.logger:
                    self.logger.warning(
                        "coding_empty_response_retry",
                        f"[{attempt}/{self.MAX_EMPTY_RESPONSE_RETRIES}] empty reply "
                        f"from {exc.provider} (returncode={exc.returncode}); "
                        f"retrying with a "
                        f"{'fresh' if fresh_session_on_retry else 'resumed'} "
                        f"session in {backoff:.0f}s",
                        data={
                            "attempt": attempt,
                            "max_retries": self.MAX_EMPTY_RESPONSE_RETRIES,
                            "provider": exc.provider,
                            "returncode": exc.returncode,
                            "backoff_sec": backoff,
                            "fresh_session": fresh_session_on_retry,
                        },
                    )
                time.sleep(backoff)
                if fresh_session_on_retry:
                    # The session we asked for never existed, so a new
                    # uuid (and never a resume) is the only retry that
                    # can succeed.
                    session_id = str(uuid.uuid4())
                    resume = False
                # else: the session DOES exist (attempt 1 replied with
                # unparseable prose), so keep resuming it — a fresh
                # session would throw away the conversational context
                # the follow-up hint depends on.
            except Exception as exc:
                # Timeouts, ApiError, HardTimeoutError, … — record the
                # failed attempt (the session may still exist and CC
                # Switch may have partial rows for it) and re-raise so
                # the existing provider-fallback handlers keep owning
                # the retry policy.
                self._record_llm_call(
                    session_id=session_id,
                    scene=scene,
                    ok=False,
                    error=type(exc).__name__,
                )
                raise

    def _resume_or_fresh_session(
        self,
        *,
        prev_session_id: str,
        resume_prompt: str,
        fresh_prompt: str,
        system_instruction: Optional[str],
        excluded_providers: Optional[list],
        total_timeout: Optional[int],
        allowed_tools: Optional[list],
        scene: Optional[str],
    ) -> tuple:
        """Re-open ``prev_session_id`` where the previous attempt left off.

        This is the provider-switch path: the last attempt died on one
        provider and the retry runs on another. The conversation is
        worth keeping — the sub-agent may be minutes into reading
        files, running a test suite or editing files, and restarting
        the task from nothing pays for all of that work twice — so the
        retry resumes rather than re-sends.

        ``--resume`` is best-effort and has no guarantee: the session
        may never have been created (a provider that stalls before its
        first token writes no transcript at all), may have been pruned,
        or may be malformed. The CLI separates those cheaply — a
        session that produced output re-opens and the model picks the
        task back up, while a session that produced none fails at once
        — but neither outcome is allowed to take the dispatch down. A
        resume that cannot be honoured degrades to the fresh-session
        call it replaces, which is what would have happened anyway, and
        says so in the log so the degradation is visible rather than
        silent.

        Returns the same ``(text, session_id)`` pair as
        ``_run_interactive_resilient``. On the fallback path the id is
        the NEW session, so a caller that goes on to retry keeps
        pointing at a session that actually exists.
        """
        try:
            return self._run_interactive_resilient(
                resume_prompt,
                system_instruction,
                session_id=prev_session_id,
                resume=True,
                excluded_providers=excluded_providers,
                total_timeout=total_timeout,
                allowed_tools=allowed_tools,
                scene=scene,
            )
        except HardTimeoutError:
            # The resumed session is fine; the provider is slow. That is
            # the caller's existing timeout routing (auto-split, next
            # provider), not a resume failure — do not mask it as one.
            raise
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    "coding_resume_fallback",
                    f"could not resume session {prev_session_id} on "
                    f"{self.current_call_provider} ({type(exc).__name__}); "
                    f"retrying the same work in a fresh session",
                    data={
                        "session_id": prev_session_id,
                        "resume": True,
                        "error": type(exc).__name__,
                        "error_detail": str(exc)[:500],
                        "provider": getattr(self, "current_call_provider", None),
                        "excluded_providers": list(excluded_providers or []),
                        "scene": scene,
                        "fallback": "fresh_session",
                    },
                )
            logger.warning(
                "[RESUME FALLBACK] session=%s provider=%s error=%s: %s — "
                "continuing in a fresh session",
                prev_session_id, getattr(self, "current_call_provider", None),
                type(exc).__name__, str(exc)[:200],
            )
            return self._run_interactive_resilient(
                fresh_prompt,
                system_instruction,
                session_id=str(uuid.uuid4()),
                resume=False,
                excluded_providers=excluded_providers,
                total_timeout=total_timeout,
                allowed_tools=allowed_tools,
                scene=scene,
            )

    def _query_internal_m1(self, prompt, system_instruction, timeout,
                           *, start_time, prompt_len,
                           retry_count=0, excluded_providers=None,
                           scene=None, resume_session=None):
        compacted_prompt = self._compact_prompt(prompt, system_instruction)
        if compacted_prompt != prompt and self.logger:
            self.logger.info(
                "coding_prompt_compacted",
                f"Prompt compacted from {prompt_len} to {len(compacted_prompt)} chars",
                data={"original_chars": prompt_len, "compacted_chars": len(compacted_prompt)},
            )

        if self.logger:
            self.logger.debug("coding_query_started",
                              f"Claude query started (prompt={prompt_len} chars, timeout={timeout}, mode=interactive)",
                              data={"tool": "claude", "prompt_len": prompt_len, "timeout": timeout,
                                    "model": self.model, "model_type": self.model_type, "mode": "interactive"})

        def _call(excluded=None, total_timeout=None, resume=False):
            """One dispatch of this prompt; returns the assistant text.

            ``resume=False`` opens a fresh session under a pinned id;
            ``resume=True`` re-opens the session the previous attempt
            was using. Either way ``pinned_session_id`` names the
            session this dispatch ran in, because the timeout handler
            below can only resume a session whose id it has.
            """
            nonlocal pinned_session_id
            if resume:
                text, used_sid = self._resume_or_fresh_session(
                    prev_session_id=pinned_session_id,
                    resume_prompt=RESUME_CONTINUATION_PROMPT,
                    fresh_prompt=compacted_prompt,
                    system_instruction=system_instruction,
                    excluded_providers=excluded,
                    total_timeout=total_timeout,
                    allowed_tools=None,
                    scene=scene,
                )
                if used_sid:
                    # A resume that fell back to a fresh session
                    # reports the new id — keep pointing at the
                    # session that exists rather than the dead one.
                    pinned_session_id = used_sid
                return text
            # ``str(uuid4())``: ``--session-id`` rejects the 32-char
            # undashed ``uuid4().hex`` form with "Invalid session ID".
            pinned_session_id = str(uuid.uuid4())
            text, _sid = self._run_interactive_resilient(
                compacted_prompt, system_instruction,
                session_id=pinned_session_id, resume=False,
                excluded_providers=excluded,
                total_timeout=total_timeout,
                allowed_tools=None,
                scene=scene,
            )
            return text

        # The session this call will dispatch into. Pinning the id here
        # is what makes a provider switch resumable at all: without it
        # the CLI picks the id, the id is never returned on the failure
        # path, and the retry has nothing to resume.
        pinned_session_id = resume_session or str(uuid.uuid4())

        try:
            if timeout is None:
                result = _call(excluded=excluded_providers,
                               resume=resume_session is not None)
            else:
                # 2026-09-06 — BOTH layers of timeout cap are active:
                # 1. ``total_timeout=timeout`` → daemon watcher inside
                #    ``_run_claude_interactive`` closes the subprocess
                #    stdout / SIGTERM after `timeout` seconds, unblocking
                #    the sync ``for line in process.stdout:`` read loop.
                # 2. ``_result_under_work_clock`` → ThreadPoolExecutor
                #    surface the result (now that the inner function
                #    returned thanks to layer 1). 2026-10-14: this was
                #    ``future.result(timeout=timeout)`` — a wall-clock
                #    figure, which also charged the quota window this
                #    dispatch may be parked on.
                # Without layer 1, layer 2 raises FuturesTimeoutError but
                # the inner call keeps running forever holding the GIL.
                # Usage-registry attribution (2026-09-21): propagate the
                # caller's contextvars (plan/task binding) into the
                # worker thread — executor.submit does NOT copy context
                # by default, which would silently drop attribution for
                # every call made through this timeout wrapper.
                _ctx = contextvars.copy_context()
                with (
                    _dispatch_work_clock(self, timeout) as _budget,
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    future = executor.submit(
                        _ctx.run, _call, excluded_providers, timeout,
                        resume_session is not None,
                    )
                    try:
                        # 2026-10-14 — same clock as ``_query_json_internal``:
                        # layer 2 spends work time, so a quota window this
                        # dispatch is parked on cannot eat the caller's own
                        # budget. See ``_result_under_work_clock``.
                        result = _result_under_work_clock(future, _budget)
                    except HardTimeoutError:
                        # 2026-09-08: HardTimeoutError (subclass of
                        # TimeoutError) must propagate for auto-split
                        # routing. ``except FuturesTimeoutError`` below
                        # would otherwise catch it (FuturesTimeoutError
                        # IS builtins.TimeoutError) and re-wrap it.
                        raise
                    except FuturesTimeoutError:
                        # The inner watcher should have already SIGTERM'd
                        # the subprocess; this is belt-and-braces in case
                        # the inner call missed the signal somehow.
                        with self._process_lock:
                            proc = self._current_process
                            self._current_process = None
                        if proc is not None:
                            kill_process_group(proc)
                        duration = int(time.monotonic() - start_time)
                        if self.logger:
                            self.logger.warning("coding_query_timeout",
                                                f"Claude query timed out after {timeout}s",
                                                data={"timeout": timeout, "duration_sec": duration})
                        raise TimeoutError(f"Query timed out after {timeout} seconds")

            duration = int(time.monotonic() - start_time)
            if self.logger:
                self.logger.debug("coding_query_completed",
                                  f"Claude query completed in {duration}s ({len(result)} chars, interactive)",
                                  data={"tool": "claude", "response_len": len(result),
                                        "duration_sec": duration, "mode": "interactive"})
            return result
        except HardTimeoutError:
            # 2026-09-08: HardTimeoutError must propagate to the
            # caller for auto-split routing. Intercept BEFORE the
            # except-TimeoutError handler matches (subclass relationship).
            raise
        except TimeoutError as exc:
            if retry_count < self.MAX_TIMEOUT_RETRIES and self.current_call_provider:
                next_retry = retry_count + 1
                new_excluded = list(excluded_providers or [])
                if self.current_call_provider and self.current_call_provider not in new_excluded:
                    new_excluded.append(self.current_call_provider)
                with self._process_lock:
                    proc = self._current_process
                    self._current_process = None
                if proc is not None:
                    kill_process_group(proc)
                if self.logger:
                    # 2026-10-14 — this message used to hard-code
                    # ``idle={DEFAULT_IDLE_TIMEOUT}`` (1800s) in both the
                    # text and the payload. That is a guess, not the
                    # triggering timeout, and it names the wrong culprit:
                    # in the rebuild incident the layer-2 budget was what
                    # actually expired, and the log said "idle=1800"
                    # twice. This arm is a generic ``except TimeoutError``
                    # — it can be reached from the layer-2 budget, from
                    # the read loop's idle timeout, or from the
                    # total-timeout watcher's refusal path — so the text
                    # cannot name one of them. Report the exception that
                    # actually arrived instead, and carry the layer-2
                    # budget separately, labelled as a budget rather than
                    # as the cause. Mirrors ``provider_timeout_fallback_json``.
                    self.logger.warning(
                        "provider_timeout_fallback",
                        f"[{next_retry}/{self.MAX_TIMEOUT_RETRIES}] {self.current_call_provider} timed out, retrying with next provider",
                        data={"from_provider": self.current_call_provider, "retry": next_retry,
                              "max_retries": self.MAX_TIMEOUT_RETRIES,
                              "timeout_error": str(exc),
                              "layer2_budget_sec": timeout,
                              "resume_session": pinned_session_id},
                    )
                # Hand the retry the session that just timed out. It is
                # resumed on the next provider instead of restarted:
                # the work done so far stays done.
                return self._query_internal_m1(
                    prompt, system_instruction, timeout,
                    start_time=start_time, prompt_len=prompt_len,
                    retry_count=next_retry, excluded_providers=new_excluded,
                    scene=scene,
                    resume_session=pinned_session_id,
                )
            providers_tried = list(excluded_providers or [])
            if self.current_call_provider and self.current_call_provider not in providers_tried:
                providers_tried.append(self.current_call_provider)
            raise TimeoutError(
                f"All providers failed after {retry_count} timeout retries (max={self.MAX_TIMEOUT_RETRIES}). "
                f"Providers attempted: {providers_tried or 'unknown'}. "
                # 2026-10-14 — the ``Idle timeout: 1800s`` clause that used
                # to sit here was a constant, not the cause: this arm is a
                # generic ``except TimeoutError`` and can be reached from the
                # layer-2 budget, the read loop's idle timeout, or the
                # total-timeout watcher. ``exc`` on the next line is the
                # timeout that actually arrived, so it is the only thing
                # this message should have named.
                f"Original error: {exc}"
            )
        except ApiError as exc:
            failed_provider = self.current_call_provider
            if failed_provider and failed_provider != "parent":
                with self._process_lock:
                    proc = self._current_process
                    self._current_process = None
                if proc is not None:
                    kill_process_group(proc)
                new_excluded = list(excluded_providers or [])
                if failed_provider not in new_excluded:
                    new_excluded.append(failed_provider)
                # 2026-09-22 — remember that this provider has no budget
                # so the NEXT dispatch does not re-select it. Without
                # this, a quota-exhausted provider is re-picked on every
                # call: it is reachable, so every availability probe
                # says yes, and the run pays a failed round-trip each
                # time (observed 12 times in 0921).
                parked_sec = _park_provider_after_error(
                    failed_provider, exc,
                )
                if self.logger:
                    self.logger.warning(
                        "provider_fallback",
                        f"{failed_provider} failed ({exc.status}), retrying with next provider",
                        data={
                            "from_provider": failed_provider,
                            "error_status": exc.status,
                            "parked_sec": parked_sec,
                            # 2026-09-23: this used to compute
                            # ``remaining_after_exclusion`` from
                            # ``scene_chain`` — a local of
                            # ``_run_claude_interactive``, NOT of this
                            # function. The NameError fired *inside* the
                            # ApiError handler, so the fallback never ran:
                            # a recoverable provider error (observed:
                            # Vendor A 402 insufficient balance) became a
                            # hard task failure reading "name
                            # 'scene_chain' is not defined" — tasks 9 and
                            # 14-3 of the 0921 plan died this way.
                            #
                            # The scene chain is not visible in this
                            # scope; report what this scope does know —
                            # the exclusion set the retry carries.
                            "excluded_after_failure": new_excluded,
                        },
                    )
                return _call(excluded=new_excluded)
            raise
        except Exception as e:
            duration = int(time.monotonic() - start_time)
            if self.logger:
                self.logger.error("coding_query_failed",
                                  f"Claude query failed: {e}",
                                  data={"error": str(e)[:500], "duration_sec": duration})
            raise

    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None,
              model_type: Optional[str] = None,
              scene: Optional[str] = None) -> str:
        """Run a subtask through Claude Code CLI.

        Args:
            prompt: The prompt to send
            system_instruction: Optional system instruction
            retries: Retry attempts (not used, kept for interface compat)
            timeout: Total timeout in seconds for the entire query.
                     If None, only the idle timeout (default 600s) applies.
            model_type: Optional per-call override ("complex" | "medium").
                        If set, the model name is resolved from
                        ``self.model_map[model_type]``; if not set, the
                        instance's ``self.model`` is used as-is.
            scene: Optional per-call workflow scene override for provider
                   routing (e.g. ``"refiner"`` → high-tier chain).
        """
        start_time = time.monotonic()
        prompt_len = len(prompt) if prompt else 0
        self._log_query_context(prompt, system_instruction, phase="query")

        # 2026-09-13: per-call model_type no longer swaps the model —
        # model management is delegated to CC Switch. The scene kwarg
        # (forwarded below) is the per-call routing lever.
        return self._query_internal_m1(
            prompt, system_instruction, timeout,
            start_time=start_time, prompt_len=prompt_len,
            scene=scene,
        )

    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                   retries: int = 3, timeout: Optional[int] = None,
                   model_type: Optional[str] = None,
                   allowed_tools: Optional[list] = None,
                   scene: Optional[str] = None) -> dict:
        """Run a subtask through Claude Code CLI and parse the JSON response.

        model_type: per-call override ("complex" | "medium"). If set AND
        model_map is configured, the model name is resolved from
        model_map[model_type]. If not set, the instance's self.model is
        used as-is.

        allowed_tools: per-call tool restriction (e.g. ``["Bash",
        "Read", "Grep", "Glob"]``). When set, the spawned ``claude``
        CLI is hard-limited to these tools via ``--allowedTools`` so a
        sub-agent cannot Edit/Write. ``None`` (default) leaves the tool
        set unrestricted — existing callers are unaffected.

        scene: per-call workflow scene override for provider routing.
        """
        # 2026-09-13: per-call model_type no longer swaps the model —
        # model management is delegated to CC Switch. The scene kwarg
        # (forwarded below) is the per-call routing lever.
        return self._query_json_internal(prompt, system_instruction, timeout, retry_count=0, allowed_tools=allowed_tools, scene=scene)

    def _query_json_internal(self, prompt, system_instruction, timeout, retry_count=0, excluded_providers=None, allowed_tools=None, scene=None, resume_session=None):
        # Resolve a caller's reusable-session opt-in here, once, at the
        # entry. An explicit ``resume_session`` is the provider-fallback
        # recursion below, and it wins outright: that path is switching
        # providers mid-answer and must re-open the session that timed
        # out, not whatever session the caller's loop is holding. The
        # handle stays bound on that path too, so the loop is handed the
        # session that actually produced the answer rather than the one
        # that died.
        session_handle = _JSON_SESSION.get()
        entry_resume = resume_session
        from_handle = entry_resume is None and session_handle is not None
        if entry_resume is None and session_handle is not None:
            entry_resume = session_handle.session_id

        def _remember(sid: Optional[str]) -> None:
            """Hand the live session id back to the caller's loop.

            Written at every dispatch rather than once at the end, so a
            loop whose answer failed to parse still has an id worth
            re-asking in — that session exists, and the loop exists
            precisely because the answer was no good.
            """
            if session_handle is not None and sid:
                session_handle.session_id = sid

        start_time = time.monotonic()
        self._log_query_context(prompt, system_instruction, phase="query_json")

        # Compact prompt before sending to prevent context overflow
        compacted_prompt = self._compact_prompt(prompt, system_instruction)
        if compacted_prompt != prompt and self.logger:
            self.logger.info(
                "coding_prompt_compacted",
                f"Prompt compacted from {len(prompt)} to {len(compacted_prompt)} chars",
                data={"original_chars": len(prompt), "compacted_chars": len(compacted_prompt)},
            )

        if self.logger:
            self.logger.debug("coding_query_json_started",
                              f"Claude query_json started (prompt={len(compacted_prompt)} chars, interactive mode)",
                              data={"tool": "claude", "prompt_len": len(compacted_prompt), "mode": "interactive"})
        json_prompt = compacted_prompt + JSON_ONLY_SUFFIX

        # The session id of the dispatch currently in flight. The
        # timeout handler below lives outside the closure that owns the
        # id, so it reads it back from here to resume the same session
        # on the next provider.
        pinned: List[Optional[str]] = [None]

        def _query_json_internal(excluded=None, total_timeout=None,
                                 resume_session=None):
            # Try the first call. If the reply is unparseable (LLM
            # returned empty / plain prose / truncated JSON), retry
            # once with a follow-up hint that surfaces the actual
            # error. This is the same pattern self_review uses
            # (commit 96247f3) — the first-pass generation should
            # be just as resilient as the audit step.
            #
            # M1 migration (2026-08-11): both attempts now use
            # ``_run_claude_interactive`` so the LLM can Read the
            # upstream docs and resume the conversation. The two
            # calls share a session_id so the second attempt's
            # follow-up hint is in the LLM's conversational
            # context (instead of starting from scratch like the
            # old --print-mode retry path did).
            from utils.json_repair import parse_llm_json
            last_err: Optional[Exception] = None

            # First attempt: standard prompt, fresh session.
            # Note: --session-id requires a valid UUID format
            # (8-4-4-4-12 hex with dashes). ``uuid.uuid4().hex`` is
            # 32 hex chars WITHOUT dashes and is rejected with
            # ``Error: Invalid session ID``. str(uuid.uuid4()) is
            # the canonical dashed form.
            session_id = resume_session or str(uuid.uuid4())
            pinned[0] = session_id
            _remember(session_id)
            if resume_session:
                if from_handle:
                    # Re-ask: the previous round of a caller-owned loop
                    # ran here and the prompt has moved on since (the
                    # validator's objection, the reason the last answer
                    # was rejected). Send the new ask alongside the
                    # restored conversation; the cut-off cue would both
                    # misdescribe the turn and drop what changed.
                    resume_prompt = RESUME_REASK_PROMPT + json_prompt
                else:
                    # Provider switch: the previous attempt died partway
                    # through producing this JSON, so resume its session
                    # rather than paying for the whole prompt again. The
                    # continuation cue carries the JSON-only instruction
                    # with it because the restored context ends on the
                    # previous attempt's own turn, not on the original ask.
                    resume_prompt = RESUME_CONTINUATION_PROMPT + JSON_ONLY_SUFFIX
                response_text, used_sid = self._resume_or_fresh_session(
                    prev_session_id=session_id,
                    resume_prompt=resume_prompt,
                    fresh_prompt=json_prompt,
                    system_instruction=system_instruction,
                    excluded_providers=excluded,
                    total_timeout=total_timeout,
                    allowed_tools=allowed_tools,
                    scene=scene,
                )
                if used_sid:
                    session_id = used_sid
                    pinned[0] = used_sid
                _remember(used_sid)
            else:
                response_text, resolved_sid = self._run_interactive_resilient(
                    json_prompt,
                    system_instruction,
                    session_id=session_id,
                    resume=False,
                    excluded_providers=excluded,
                    total_timeout=total_timeout,
                    allowed_tools=allowed_tools,
                    scene=scene,
                )
                # The CLI may report an id other than the one pinned
                # above. Only the reusable-session handle is told —
                # ``pinned`` and ``session_id`` keep their existing
                # meaning for the provider-fallback and format-retry
                # paths, which are shared with callers that never opted
                # into reuse.
                _remember(resolved_sid)
            try:
                return parse_llm_json(response_text)
            except json.JSONDecodeError as exc:
                last_err = exc

            # Second attempt: resume the SAME session and surface
            # the parse error. The LLM sees its previous reply +
            # the error + a follow-up hint, in conversational
            # context.
            err_msg = (
                last_err.__class__.__name__ + ": " + str(last_err)
                if last_err is not None
                else "previous reply was not parseable JSON"
            )
            follow_up = (
                "你上一轮的回复无法解析（" + err_msg + "）。"
                "请重新输出，**只**输出一个 JSON 对象，"
                "第一个字符必须是 `{`，最后一个必须是 `}`，"
                "中间不要夹带任何 prose、Markdown 围栏、"
                "或第二个 JSON 对象。"
            )
            hint_text, _ = self._run_interactive_resilient(
                follow_up,
                system_instruction,
                session_id=session_id,
                resume=True,
                excluded_providers=excluded,
                total_timeout=total_timeout,
                allowed_tools=allowed_tools,
                # 2026-09-17 — this call previously omitted ``scene``, so
                # the format retry silently fell back to the capacity-blind
                # registry walk and could land on a completely different
                # provider than attempt 1 (whose session it was resuming).
                scene=scene,
                # The session exists (attempt 1 replied), so an empty
                # reply here is retried as a resume — a fresh session
                # would discard the context the hint depends on.
                fresh_session_on_retry=False,
            )
            try:
                return parse_llm_json(hint_text)
            except json.JSONDecodeError as exc:
                last_err = exc

            # Both attempts failed — surface the last error.
            raise last_err or json.JSONDecodeError(
                "no JSON object / array boundaries found", "", 0
            )

        try:
            if timeout is None:
                result = _query_json_internal(excluded=excluded_providers,
                                              resume_session=entry_resume)
            else:
                # 2026-09-06 — same dual-timeout pattern as
                # ``_query_internal_m1``: layer 1 (total_timeout watcher
                # inside ``_run_claude_interactive``) closes the
                # subprocess so the read loop unblocks; layer 2
                # (``_result_under_work_clock``) is the public surface that
                # returns the now-completed inner call. 2026-10-14: layer 2
                # spends the clock's *work* time, so a closed quota window
                # this dispatch is parked on is not charged to the budget.
                # Usage-registry attribution: propagate contextvars into
                # the worker thread (see _query_internal_m1).
                _ctx = contextvars.copy_context()
                with (
                    _dispatch_work_clock(self, timeout) as _budget,
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    future = executor.submit(
                        _ctx.run, _query_json_internal, excluded_providers,
                        timeout, entry_resume,
                    )
                    try:
                        # 2026-10-14 — spent on the clock's work time, not
                        # on wall time. The wait this wraps can be parked on
                        # a closed quota window for hours, and the park is
                        # already honoured everywhere else; charging it here
                        # is what threw away the dispatch that the window
                        # eventually let through.
                        result = _result_under_work_clock(future, _budget)
                    except HardTimeoutError:
                        # 2026-09-08: HardTimeoutError must propagate
                        # for auto-split routing. Note that
                        # ``concurrent.futures.TimeoutError`` is the same
                        # class as ``builtins.TimeoutError``, so the
                        # generic ``except FuturesTimeoutError`` below
                        # would also catch HardTimeoutError (it's a
                        # TimeoutError subclass) and incorrectly wrap it
                        # into a plain TimeoutError. Re-raise before
                        # the generic arm.
                        raise
                    except FuturesTimeoutError:
                        # 2026-10-14 — deliberately NO ``future.cancel()``
                        # here (there was one; it was dead code). A
                        # ``Future`` only cancels while PENDING; once the
                        # worker is running it, ``cancel()`` returns False
                        # and does nothing. By this point the future is
                        # necessarily RUNNING — ``result(timeout)`` only
                        # raises once it has waited out the full budget.
                        # Calling it only suggested that a timeout could
                        # stop the worker, which it cannot. The two lines
                        # below are the real remedy: drop the handle and
                        # SIGKILL the process group. Note the ordering
                        # matters — the ``raise`` leaves this ``with
                        # ThreadPoolExecutor`` block, whose ``__exit__``
                        # runs ``shutdown(wait=True)``, so without the
                        # kill the exception would not surface until the
                        # LLM call finished on its own.
                        with self._process_lock:
                            proc = self._current_process
                            self._current_process = None
                        if proc is not None:
                            kill_process_group(proc)
                        raise TimeoutError(f"Query timed out after {timeout} seconds")

            duration = int(time.monotonic() - start_time)
            if self.logger:
                self.logger.debug("coding_query_json_completed",
                                  f"Claude query_json completed in {duration}s",
                                  data={"tool": "claude", "duration_sec": duration})
            return result
        except HardTimeoutError:
            # 2026-09-08: HardTimeoutError must propagate to the
            # caller for auto-split routing. The generic
            # ``except TimeoutError`` below would otherwise fall through
            # to provider fallback — which is wrong here: retrying with
            # a different provider doesn't make a too-big task smaller.
            # Re-raise BEFORE the except-TimeoutError handler can match
            # (HardTimeoutError IS a TimeoutError subclass, so we must
            # intercept it first).
            raise
        except TimeoutError as exc:
            # Provider fallback on timeout: retry with next provider in priority list
            if retry_count < self.MAX_TIMEOUT_RETRIES and self.current_call_provider:
                next_retry = retry_count + 1
                # Exclude the timed-out provider so retry picks the next one
                new_excluded = list(excluded_providers or [])
                if self.current_call_provider and self.current_call_provider not in new_excluded:
                    new_excluded.append(self.current_call_provider)
                # Kill residual child process before retry to prevent hang
                with self._process_lock:
                    proc = self._current_process
                if proc is not None:
                    kill_process_group(proc)
                    with self._process_lock:
                        self._current_process = None
                if self.logger:
                    # 2026-10-14 — this message used to hard-code
                    # ``idle={DEFAULT_IDLE_TIMEOUT}`` (1800s). That is a
                    # guess, not the triggering timeout, and it named the
                    # wrong culprit during the incident that prompted the
                    # change: layer 2's 300s ``future.result`` budget was
                    # what actually expired (403s / 2120s late), yet the
                    # log twice said "idle=1800". This arm is a generic
                    # ``except TimeoutError`` — it can be reached from the
                    # layer-2 budget, from the read loop's idle timeout,
                    # or from the total-timeout watcher's refusal path —
                    # so the text cannot name one of them. Report the
                    # exception that actually arrived instead, and carry
                    # the layer-2 budget separately, labelled as a budget
                    # rather than as the cause.
                    self.logger.warning(
                        "provider_timeout_fallback_json",
                        f"[{next_retry}/{self.MAX_TIMEOUT_RETRIES}] {self.current_call_provider} timed out, retrying with next provider",
                        data={"from_provider": self.current_call_provider, "retry": next_retry,
                              "max_retries": self.MAX_TIMEOUT_RETRIES,
                              "timeout_error": str(exc),
                              "layer2_budget_sec": timeout,
                              "resume_session": pinned[0]},
                    )
                # Same contract as ``_query_internal_m1``: the retry
                # resumes the session that timed out instead of paying
                # for the whole prompt again from the top.
                return self._query_json_internal(
                    prompt, system_instruction, timeout, retry_count=next_retry,
                    excluded_providers=new_excluded,
                    scene=scene,
                    resume_session=pinned[0],
                )
            # Max retries exceeded — raise with detailed error
            providers_tried = list(excluded_providers or [])
            if self.current_call_provider and self.current_call_provider not in providers_tried:
                providers_tried.append(self.current_call_provider)
            raise TimeoutError(
                f"All providers failed after {retry_count} timeout retries (max={self.MAX_TIMEOUT_RETRIES}). "
                f"Providers attempted: {providers_tried or 'unknown'}. "
                # 2026-10-14 — the ``Idle timeout: 1800s`` clause that used
                # to sit here was the same constant quoted in
                # ``_query_internal_m1``, and the same lie: this arm is a
                # generic ``except TimeoutError``, so the clause could name
                # a timeout that never fired. ``exc`` below is the one that
                # did.
                f"Original error: {exc}"
            )
        except ApiError as exc:
            # Per-call fallback: exclude the provider that failed and retry
            # with the next one in the priority list.
            failed_provider = self.current_call_provider
            # Skip retry when no registered provider was selected
            # ("parent" fallback) — see _query_with_retry for rationale.
            if failed_provider and failed_provider != "parent":
                # Kill residual child process before retry to prevent hang
                with self._process_lock:
                    proc = self._current_process
                if proc is not None:
                    kill_process_group(proc)
                    with self._process_lock:
                        self._current_process = None
                new_excluded = list(excluded_providers or [])
                if failed_provider not in new_excluded:
                    new_excluded.append(failed_provider)
                # Same quota-cooldown contract as the interactive path —
                # see ``_park_provider_after_error`` for the 0921 story.
                parked_sec = _park_provider_after_error(
                    failed_provider, exc,
                )
                if self.logger:
                    self.logger.warning(
                        "provider_fallback",
                        f"{failed_provider} failed ({exc.status}), retrying with next provider",
                        data={
                            "from_provider": failed_provider,
                            "error_status": exc.status,
                            "parked_sec": parked_sec,
                        },
                    )
                return _query_json_internal(excluded=new_excluded)
            raise
        except Exception as e:
            duration = int(time.monotonic() - start_time)
            if self.logger:
                self.logger.error("coding_query_json_failed",
                                  f"Claude query_json failed: {e}",
                                  data={"error": str(e)[:500], "duration_sec": duration})
            raise


class VendorCCodingTool(CodingTool):
    """Vendor C Code CLI tool — runs `vendor-c -y -p <prompt>`."""  # kebab-case test_provider marker

    def __init__(self, model: Optional[str] = None):
        self.model = model

    def _run_vendor_c(self, prompt: str, system_instruction: Optional[str] = None) -> str:  # kebab-case test_provider marker
        import re
        full_prompt = prompt
        if system_instruction:
            full_prompt = f"{system_instruction}\n\nTask:\n{prompt}"

        cmd = ["vendor-c", "-y", "--no-thinking", "--print", "-p", full_prompt]  # kebab-case test_provider marker
        if self.model:
            cmd += ["-m", self.model]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Vendor C CLI failed: {result.stderr or result.stdout}")

        # Extract text from TextPart entries in structured --print output
        text_parts = re.findall(r"TextPart\(type='text', text='(.*?)'\)", result.stdout, re.DOTALL)
        if text_parts:
            # Unescape newlines in captured text
            return '\n'.join(t.replace('\\n', '\n') for t in text_parts)
        # Fallback: return raw stdout if no TextPart found
        return result.stdout

    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None) -> str:
        def _query_internal():
            return self._run_vendor_c(prompt, system_instruction)  # kebab-case test_provider marker

        # 2026-09-15: callers omit ``timeout`` now, so
        # inherit the unified budget. Unlike the interactive Claude path
        # there is no internal silence/idle watcher in this plain
        # subprocess/HTTP implementation — an unbounded call would block
        # this thread forever — so fall back to DEFAULT_TOTAL_TIMEOUT.
        if timeout is None:
            timeout = self.DEFAULT_TOTAL_TIMEOUT

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_query_internal)
            try:
                return future.result(timeout=timeout)
            except HardTimeoutError:
                # 2026-09-08: see ClaudeCodingTool — re-raise before
                # the generic TimeoutError arm can wrap it.
                raise
            except FuturesTimeoutError:
                # 2026-10-14 — the ``future.cancel()`` that used to sit
                # here was dead code, and its return value is the wrong
                # thing to reason about: a ``Future`` only cancels while
                # PENDING, and there it returns True, so "cancel() is
                # always False" would be false and would be knocked over
                # by the first counter-example someone constructs. The
                # criterion is the state, not the return: by the time
                # ``result(timeout)`` raises, it has waited out the whole
                # budget, and the worker picked the work item up before
                # it started waiting — so the future is necessarily
                # RUNNING or FINISHED, and ``cancel()`` on either returns
                # False and does nothing.
                #
                # PENDING is reachable on this path only before the
                # submit, never after it. There is nothing to stop here:
                # no kill follows either, only the ``raise`` — and the
                # ``with`` block's ``__exit__`` runs
                # ``shutdown(wait=True)``, so the timeout surfaces as
                # soon as the worker returns regardless of what the
                # timeout did or did not do to it. The four sites in
                # this file (VendorCCodingTool ×2, OpenCodeCodingTool ×2)
                # are all this same shape; the Claude sites are the
                # argument spelled out at :4779.
                raise TimeoutError(f"Query timed out after {timeout} seconds")

    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                   retries: int = 3, timeout: Optional[int] = None) -> dict:
        json_prompt = prompt + "\n\nIMPORTANT: Return ONLY the JSON object requested, no markdown fencing."

        def _query_json_internal():
            response_text = self._run_vendor_c(json_prompt, system_instruction)  # kebab-case test_provider marker
            from utils.json_repair import parse_llm_json
            return parse_llm_json(response_text)

        # 2026-09-15: callers omit ``timeout`` now, so
        # inherit the unified budget. Unlike the interactive Claude path
        # there is no internal silence/idle watcher in this plain
        # subprocess/HTTP implementation — an unbounded call would block
        # this thread forever — so fall back to DEFAULT_TOTAL_TIMEOUT.
        if timeout is None:
            timeout = self.DEFAULT_TOTAL_TIMEOUT

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_query_json_internal)
            try:
                return future.result(timeout=timeout)
            except HardTimeoutError:
                # 2026-09-08: re-raise before the generic arm.
                raise
            except FuturesTimeoutError:
                # 2026-10-14 — dead ``future.cancel()`` removed; the future
                # is necessarily RUNNING by this point. Criterion and the
                # PENDING boundary are argued at the ``query`` arm above.
                raise TimeoutError(f"Query timed out after {timeout} seconds")


class OpenCodeCodingTool(CodingTool):
    """OpenCode AI coding tool using opencode CLI."""

    def __init__(self):
        pass

    def _run_opencode(self, prompt: str) -> str:
        """Run opencode CLI."""
        result = subprocess.run(
            ["opencode", "run"],
            input=prompt,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise Exception(f"OpenCode failed: {result.stderr or result.stdout}")
        return result.stdout

    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None) -> str:
        """Query OpenCode AI."""
        def _query_internal():
            full_prompt = prompt
            if system_instruction:
                full_prompt = f"{system_instruction}\n\nTask:\n{prompt}"

            return self._run_opencode(full_prompt)

        # 2026-09-15: callers omit ``timeout`` now, so
        # inherit the unified budget. Unlike the interactive Claude path
        # there is no internal silence/idle watcher in this plain
        # subprocess/HTTP implementation — an unbounded call would block
        # this thread forever — so fall back to DEFAULT_TOTAL_TIMEOUT.
        if timeout is None:
            timeout = self.DEFAULT_TOTAL_TIMEOUT

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_query_internal)
            try:
                return future.result(timeout=timeout)
            except HardTimeoutError:
                # 2026-09-08: see ClaudeCodingTool.
                raise
            except FuturesTimeoutError:
                # 2026-10-14 — dead ``future.cancel()`` removed; the future
                # is necessarily RUNNING by this point. Criterion and the
                # PENDING boundary are argued at ``VendorCCodingTool.query``.
                raise TimeoutError(f"Query timed out after {timeout} seconds")

    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                 retries: int = 3, timeout: Optional[int] = None) -> dict:
        """Query OpenCode AI and expect JSON response."""

        def _query_json_internal():
            full_prompt = prompt
            if system_instruction:
                full_prompt = f"{system_instruction}\n\nTask:\n{prompt}"

            full_prompt += "\n\nIMPORTANT: Return ONLY the JSON object requested."

            response_text = self._run_opencode(full_prompt)
            from utils.json_repair import parse_llm_json
            return parse_llm_json(response_text)

        # 2026-09-15: callers omit ``timeout`` now, so
        # inherit the unified budget. Unlike the interactive Claude path
        # there is no internal silence/idle watcher in this plain
        # subprocess/HTTP implementation — an unbounded call would block
        # this thread forever — so fall back to DEFAULT_TOTAL_TIMEOUT.
        if timeout is None:
            timeout = self.DEFAULT_TOTAL_TIMEOUT

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_query_json_internal)
            try:
                return future.result(timeout=timeout)
            except HardTimeoutError:
                # 2026-09-08: re-raise before the generic arm.
                raise
            except FuturesTimeoutError:
                # 2026-10-14 — dead ``future.cancel()`` removed; the future
                # is necessarily RUNNING by this point. Criterion and the
                # PENDING boundary are argued at ``VendorCCodingTool.query``.
                raise TimeoutError(f"Query timed out after {timeout} seconds")


def create_coding_tool(tool_type: Optional[str] = None, cwd: Optional[str] = None, logger=None,
                       scene: Optional[str] = None) -> CodingTool:
    """Factory function to create the appropriate coding tool.

    Args:
        tool_type: Tool type — 'claude', 'vendor-c', 'opencode', or None (auto from env).  # kebab-case test_provider marker
        cwd: Working directory for the tool.
        logger: Optional logger for structured logging.
        scene: Workflow scene for provider routing (2026-09-13). Forwarded
            to ``ClaudeCodingTool``; ignored by the vendor-c/opencode branches.

    Returns:
        CodingTool instance.
    """
    if tool_type is None:
        tool_type = os.environ.get("PDT_DEFAULT_CODING_TOOL", "claude").lower()

    if tool_type == "vendor-c":  # kebab-case test_provider marker
        return VendorCCodingTool()
    elif tool_type == "opencode":
        return OpenCodeCodingTool()
    else:
        return ClaudeCodingTool(cwd=cwd, logger=logger, scene=scene)


class HardTimeoutError(TimeoutError):
    """Raised when the inner adaptive timer fires after ``total_sec``
    seconds of subprocess stdout silence (the "wall-clock cap" signal).

    Subclass of ``TimeoutError`` for backward compatibility with callers
    that already catch ``TimeoutError``, but distinct enough that the
    verification / task-execution layers can route it through the
    auto-split / auto-refine path instead of the generic failure path.

    A normal ``TimeoutError`` from ``asyncio.wait_for`` (e.g. the outer
    1-hour wall-clock cap in :class:`verification_subagent.VerificationSubAgent`)
    is NOT this exception — those still surface as ``TimeoutError`` to
    the caller, which is the existing behavior.

    Why a separate class:
      * Auto-split on hard timeout (per 2026-09-08 plan) requires
        distinguishing "subprocess silent for 15 min → split it" from
        "outer asyncio cap hit → keep moving". The latter can fire on
        a healthy long-running pytest that was just unlucky with the
        cap; the former genuinely means the task is too big.
      * Log marker ``[HARD TIMEOUT]`` is emitted at the same site
        (``_total_timer`` body) where this exception is raised, so
        operator-visible signal and exception type are 1:1.

    Note: ``TimeoutError`` inherits from ``OSError`` which has a
    restricted ``__init__`` (errno/strerror/filename only). We bypass
    that by setting attributes directly on ``self`` instead of via
    ``super().__init__``.
    """
    def __init__(self, total_sec: int, elapsed: float, last_line: str = ""):
        # Skip super().__init__ because TimeoutError(=OSError) only
        # accepts errno/strerror/filename. Set attributes directly.
        self.total_sec = total_sec
        self.elapsed = elapsed
        self.last_line = last_line
        # Build a useful message and store on args[0] so str(exc)
        # works as expected. Bypass OSError's special-cased __init__
        # by NOT calling super().__init__ — we set args ourselves.
        message = (
            f"Hard timeout after {total_sec}s of stdout silence "
            f"(elapsed={elapsed:.1f}s, last_line={last_line[:80]!r})"
        )
        # Exception.__init__ accepts a single message arg safely; this
        # avoids OSError's errno/strerror/filename constraint entirely.
        Exception.__init__(self, message)

