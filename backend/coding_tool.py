"""
Coding Tool Interface
==================

Abstract base class for AI coding tools and implementations.
"""

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
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone

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
# Per-plan LLM-call attribution registry (2026-09-21). Pure logging side
# channel — record failures must never break a dispatch, so the registry
# swallows its own errors and we never let them propagate either.
from usage_registry import current_task_id
from usage_registry import record_llm_call as _record_usage_entry
from file_lock_protocol import socket_path

logger = logging.getLogger(__name__)


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


def _cooldown_seconds_for(exc: BaseException) -> float:
    """Park duration for ``exc``; env-overridable for operators/tests."""
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
    return default


def _park_provider_after_error(provider: str, exc: BaseException) -> float:
    """Park ``provider`` when ``exc`` is a capacity error; return seconds.

    Returns ``0.0`` when nothing was parked (not a capacity error, or
    the cooldown module is unavailable).
    """
    if not provider or not is_capacity_error(exc):
        return 0.0
    seconds = _cooldown_seconds_for(exc)
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
    # How long a scene-routed dispatch waits for a free provider slot
    # before giving up and falling back to the parent process config
    # (2026-09-17). Overridable per-process with PDT_PROVIDER_SLOT_WAIT_SEC.
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
        # 2026-09-14 (user insight): the LAST stdout line itself, so
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

        2026-09-21: asked for by the operator — "如果走 legacy walk，那就要
        去获取 cc-switch 当前的实际上路由的那个 provider". CC Switch owns the
        routing decision, so its current selection is the only honest
        answer for which provider a scene-less dispatch should use.

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
            total_timeout: 2026-09-06 — wall-clock cap on the entire
                call. Defaults to ``DEFAULT_TOTAL_TIMEOUT`` (15 min).
                Even if the LLM is actively streaming, this watcher
                thread forces a SIGTERM after the cap so a wedged
                provider cannot block the verification iteration loop
                indefinitely. The previous behaviour had no total cap,
                so a sync-blocked ``query_json`` could keep the auto-loop
                thread in Phase 2 forever.
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
                settings_data.setdefault("env", {})
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

        # Scene routing (2026-09-13): when a scene is active, resolve its
        # provider chain (scene → tier → regex over CC Switch names) on
        # EVERY call and prefer its candidates. When the scene chain is
        # empty or yields nothing available, the provider_priority walk
        # below runs (documented fallback).
        effective_scene = scene if scene is not None else self.scene
        scene_chain: List[str] = []
        # Skipped entirely in inherit mode: both branches below read CC
        # Switch display names, so with no database they can only return
        # an empty chain while logging "CC Switch database unavailable" —
        # a warning about a component that is not supposed to be there.
        if effective_scene and not self.inherit_providers:
            try:
                from dynamic_provider_concurrency import (
                    get_shared_cooldown,
                )
                from provider_routing import resolve_fallback_chains
                # 2026-09-22 — cross-tier widening, used ONLY when the
                # scene's own tier has no usable provider left.
                #
                # Before this the walk could only see the scene's tier,
                # and ``provider_routing.yaml`` declares
                # ``medium: ["^Vendor A"]`` — two CC Switch rows. So
                # "every provider in the tier failed" was two 429s away,
                # after which the only remaining move was the parent
                # process env (not a tier member at all), and when that
                # also 429'd the task died with it:
                #
                #   provider_fallback vendor-a-pro failed (429)
                #   provider_parent_fallback No provider in
                #            priority list available
                #   task_api_error [429] 已达到 Token Plan 用量上限
                #
                # Widening is the ESCAPE HATCH, not the default. A tier
                # whose providers are merely BUSY still queues on its own
                # local semaphore — downgrade is driven by local slot
                # exhaustion, not by reachability. Spilling to another
                # tier whenever a pool filled would quietly change which
                # semaphores govern the fleet. Only "every provider here
                # is excluded or parked" widens.
                _chains = resolve_fallback_chains(effective_scene)
                primary = _chains[0] if _chains else []
                _cooldown = get_shared_cooldown()
                usable_here = [
                    name for name in primary
                    if name not in excluded and not _cooldown.is_parked(name)
                ]
                if usable_here or len(_chains) <= 1:
                    scene_chain = list(primary)
                else:
                    scene_chain = []
                    for _chain in _chains:
                        for _name in _chain:
                            if _name not in scene_chain:
                                scene_chain.append(_name)
                    if self.logger:
                        self.logger.warning(
                            "provider_scene_chain_widened",
                            (
                                f"Every provider in scene "
                                f"{effective_scene!r}'s own tier is "
                                f"unusable ({primary}); widening to "
                                f"{len(_chains)} tiers ({len(scene_chain)} "
                                f"providers) instead of dropping to the "
                                f"parent config"
                            ),
                            data={
                                "scene": effective_scene,
                                "primary_chain": primary,
                                "excluded": list(excluded),
                                "parked": _cooldown.snapshot(),
                                "chains": _chains,
                                "flattened": scene_chain,
                            },
                        )
            except Exception as exc:
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
            # full, QUEUE (acquire_provider_with_dynamic_capacity polls)
            # instead of piling more work onto the first entry.
            scene_candidates: List[str] = []
            scene_configs: dict = {}
            from dynamic_provider_concurrency import get_shared_cooldown
            _cooldown = get_shared_cooldown()
            for display_name in scene_chain:
                if display_name in excluded:
                    continue
                # 2026-09-22 — a provider that recently answered
                # "quota exhausted" is reachable and correctly
                # configured, so every probe below says yes and the walk
                # picks it again. The 0921 run re-selected
                # Vendor A Pro twelve times that way, ~3 minutes of
                # stuck agent each time. Skip it until its park expires.
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

            if scene_candidates:
                from dynamic_provider_concurrency import (
                    acquire_provider_with_dynamic_capacity,
                )
                from provider_order import load_provider_5h_usage

                try:
                    usage_map = load_provider_5h_usage()
                except Exception:
                    # The dispatch path must never fail on a malformed
                    # quota file; a missing entry reads as 100%.
                    usage_map = {}

                wait_sec = self.provider_slot_wait_sec
                chosen = acquire_provider_with_dynamic_capacity(
                    scene_candidates,
                    self._concurrency_tracker,
                    usage_map,
                    timeout=wait_sec,
                )

                if chosen is None:
                    if self.logger:
                        self.logger.warning(
                            "provider_scene_all_pools_exhausted",
                            f"Every scene candidate for {effective_scene!r} is "
                            f"out of local capacity after waiting "
                            f"{wait_sec:.0f}s ({scene_candidates}); falling back "
                            f"to the parent process config",
                            data={
                                "scene": effective_scene,
                                "candidates": scene_candidates,
                                "wait_sec": wait_sec,
                                "tracker": self._concurrency_tracker.snapshot(),
                            },
                        )
                else:
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
                # walk). When every scene candidate was unavailable we
                # fall through to the parent fallback below, NOT to
                # out-of-tier providers.
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
            elif self.settings is not None:
                cmd += ["--settings", str(self.settings)]

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

    def _query_internal_m1(self, prompt, system_instruction, timeout,
                           *, start_time, prompt_len,
                           retry_count=0, excluded_providers=None,
                           scene=None):
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

        def _call(excluded=None, total_timeout=None):
            text, _sid = self._run_interactive_resilient(
                compacted_prompt, system_instruction,
                session_id=None, resume=False,
                excluded_providers=excluded,
                total_timeout=total_timeout,
                allowed_tools=None,
                scene=scene,
            )
            return text

        try:
            if timeout is None:
                result = _call(excluded=excluded_providers)
            else:
                # 2026-09-06 — BOTH layers of timeout cap are active:
                # 1. ``total_timeout=timeout`` → daemon watcher inside
                #    ``_run_claude_interactive`` closes the subprocess
                #    stdout / SIGTERM after `timeout` seconds, unblocking
                #    the sync ``for line in process.stdout:`` read loop.
                # 2. ``future.result(timeout=timeout)`` → ThreadPoolExecutor
                #    surface the result (now that the inner function
                #    returned thanks to layer 1).
                # Without layer 1, layer 2 raises FuturesTimeoutError but
                # the inner call keeps running forever holding the GIL.
                # Usage-registry attribution (2026-09-21): propagate the
                # caller's contextvars (plan/task binding) into the
                # worker thread — executor.submit does NOT copy context
                # by default, which would silently drop attribution for
                # every call made through this timeout wrapper.
                _ctx = contextvars.copy_context()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        _ctx.run, _call, excluded_providers, timeout
                    )
                    try:
                        result = future.result(timeout=timeout)
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
                    self.logger.warning(
                        "provider_timeout_fallback",
                        f"[{next_retry}/{self.MAX_TIMEOUT_RETRIES}] {self.current_call_provider} timed out (idle={self.DEFAULT_IDLE_TIMEOUT}s), retrying with next provider",
                        data={"from_provider": self.current_call_provider, "retry": next_retry,
                              "max_retries": self.MAX_TIMEOUT_RETRIES, "idle_sec": self.DEFAULT_IDLE_TIMEOUT},
                    )
                return self._query_internal_m1(
                    prompt, system_instruction, timeout,
                    start_time=start_time, prompt_len=prompt_len,
                    retry_count=next_retry, excluded_providers=new_excluded,
                    scene=scene,
                )
            providers_tried = list(excluded_providers or [])
            if self.current_call_provider and self.current_call_provider not in providers_tried:
                providers_tried.append(self.current_call_provider)
            raise TimeoutError(
                f"All providers failed after {retry_count} timeout retries (max={self.MAX_TIMEOUT_RETRIES}). "
                f"Idle timeout: {self.DEFAULT_IDLE_TIMEOUT}s. "
                f"Providers attempted: {providers_tried or 'unknown'}. "
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

    def _query_json_internal(self, prompt, system_instruction, timeout, retry_count=0, excluded_providers=None, allowed_tools=None, scene=None):
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
        json_prompt = compacted_prompt + "\n\nIMPORTANT: Return ONLY the JSON object requested, no markdown fencing."

        def _query_json_internal(excluded=None, total_timeout=None):
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
            session_id = str(uuid.uuid4())
            response_text, _ = self._run_interactive_resilient(
                json_prompt,
                system_instruction,
                session_id=session_id,
                resume=False,
                excluded_providers=excluded,
                total_timeout=total_timeout,
                allowed_tools=allowed_tools,
                scene=scene,
            )
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
                result = _query_json_internal(excluded=excluded_providers)
            else:
                # 2026-09-06 — same dual-timeout pattern as
                # ``_query_internal_m1``: layer 1 (total_timeout watcher
                # inside ``_run_claude_interactive``) closes the
                # subprocess so the read loop unblocks; layer 2
                # (``future.result``) is the public surface that
                # returns the now-completed inner call.
                # Usage-registry attribution: propagate contextvars into
                # the worker thread (see _query_internal_m1).
                _ctx = contextvars.copy_context()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        _ctx.run, _query_json_internal, excluded_providers, timeout
                    )
                    try:
                        result = future.result(timeout=timeout)
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
                        future.cancel()
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
                    self.logger.warning(
                        "provider_timeout_fallback_json",
                        f"[{next_retry}/{self.MAX_TIMEOUT_RETRIES}] {self.current_call_provider} timed out (idle={self.DEFAULT_IDLE_TIMEOUT}s), retrying with next provider",
                        data={"from_provider": self.current_call_provider, "retry": next_retry,
                              "max_retries": self.MAX_TIMEOUT_RETRIES, "idle_sec": self.DEFAULT_IDLE_TIMEOUT},
                    )
                return self._query_json_internal(
                    prompt, system_instruction, timeout, retry_count=next_retry,
                    excluded_providers=new_excluded,
                    scene=scene,
                )
            # Max retries exceeded — raise with detailed error
            providers_tried = list(excluded_providers or [])
            if self.current_call_provider and self.current_call_provider not in providers_tried:
                providers_tried.append(self.current_call_provider)
            raise TimeoutError(
                f"All providers failed after {retry_count} timeout retries (max={self.MAX_TIMEOUT_RETRIES}). "
                f"Idle timeout: {self.DEFAULT_IDLE_TIMEOUT}s. "
                f"Providers attempted: {providers_tried or 'unknown'}. "
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
                future.cancel()
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
                future.cancel()
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
                future.cancel()
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
                future.cancel()
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

