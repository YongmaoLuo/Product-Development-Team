"""Single source of truth for the backend's filesystem paths.

Every path the backend derives from ``__file__`` is declared here once,
so that:

* leaf utilities no longer re-derive ``Path(__file__).parent.parent /
  "plans"`` (and friends) module by module, each with its own drift
  risk; and
* a leaf utility such as :mod:`provider_order` can obtain
  ``provider-order.json``'s location **without importing**
  :mod:`server` — the upward ``leaf → FastAPI app`` dependency flagged
  as finding #2 of the 2026-09-04 architecture review.

Two rules keep this module usable from anywhere in the tree:

1. **No side effects at import.** The module *declares* paths; it never
   creates, touches, or deletes them. Callers that need a directory to
   exist call ``mkdir(parents=True, exist_ok=True)`` themselves.
2. **No backend imports.** Only the standard library is imported at
   module scope (PyYAML is imported lazily, inside the one function
   that needs it), so importing this module can never trigger app
   construction or a circular import.

All constants are absolute :class:`~pathlib.Path` objects anchored on
:data:`PROJECT_ROOT` / :data:`BACKEND_DIR` rather than on the current
working directory, so they stay correct no matter where a process is
launched from.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

#: ``<repo>/backend`` — the directory this file lives in.
BACKEND_DIR: Path = Path(__file__).resolve().parent

#: ``<repo>`` — the repository root, resolved once at import time.
PROJECT_ROOT: Path = BACKEND_DIR.parent

# ---------------------------------------------------------------------------
# Repo-level paths
# ---------------------------------------------------------------------------

#: Per-plan artifact root: ``<repo>/plans/{plan_id}/...``
#:
#: This is the *default*. Anything that reads or writes plan artifacts
#: should call :func:`resolve_plans_dir` instead, so an ``PDT_PLANS_DIR``
#: override is honoured — see that function for why.
PLANS_DIR: Path = PROJECT_ROOT / "plans"

#: Static frontend assets served by the FastAPI app.
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"

#: Root for operator-supplied auxiliary tooling.
#:
#: This repository ships no such tooling and nothing here requires the
#: directory to exist; it is only an anchor for deployments that keep
#: their own tools alongside the app.
TOOLS_DIR: Path = PROJECT_ROOT / "tools"

#: Environment variable that relocates :data:`STATE_DB`. Read through
#: :func:`resolve_state_db_path` — never ``os.environ`` directly, or the
#: copy that skips the helper is the one that silently opens the wrong
#: database under a test.
STATE_DB_ENV: str = "PDT_STATE_DB_PATH"

#: Directory holding this checkout's local runtime state: ``<repo>/.pdt``.
#:
#: Dot-prefixed so it is hidden and sorts out of the way, and named after
#: the runtime-state convention the project already uses (a workspace
#: keeps its file locks under ``.pdt/locks/``). Gitignored, in the same
#: category as :data:`CONFIG_DIR`: per-machine state a checkout *produces*
#: rather than ships.
#:
#: **Why the state store left the repository root (2026-09-28).** It used
#: to sit next to ``README.md`` as four separate entries — ``state.db``,
#: its two WAL siblings, a boot counter and a ``backups/`` tree — which
#: are unambiguously not source and yet are indistinguishable at a glance
#: from the files that are. They are one unit of runtime state, so they
#: get one directory.
STATE_DIR: Path = PROJECT_ROOT / ".pdt"

#: State-machine SQLite database.
#:
#: Its **parent** is load-bearing well beyond this one file. Two runtime
#: artifacts derive from it instead of declaring their own anchor, so
#: they follow this path wherever it points:
#:
#:   * ``<parent>/pdt_server_boot_id`` — the boot counter ``server.py``
#:     writes so a SIGKILL'd process can be correlated against its own
#:     ``state.db`` rows;
#:   * ``<parent>/backups/state-db/`` — ``startup_guard``'s shutdown
#:     backups of the live database.
#:
#: That is why relocating the store means relocating all three together,
#: and why :data:`STATE_DIR` exists rather than the filename being
#: repeated with a different prefix here.
STATE_DB: Path = STATE_DIR / "state.db"

# ---------------------------------------------------------------------------
# Backend-level paths
# ---------------------------------------------------------------------------

#: Layered agent/verification YAML configs (``_base.yaml`` + overlays).
#:
#: These are *product content* — the prompt templates and agent settings
#: the code ships with. They version with the code.
CONFIGS_DIR: Path = BACKEND_DIR / "configs"

#: Local configuration directory: ``<repo>/.config``.
#:
#: **Operator-owned, gitignored, and the only place deployment-specific
#: configuration is read from.** Which providers exist on your machine and
#: which tier each belongs to is not something this repository can know, so
#: those files live here rather than in the shipped tree.
#:
#: Templates ship in ``<repo>/example/`` — copy one in and rename it to the
#: real filename to activate it. See README.md.
CONFIG_DIR: Path = PROJECT_ROOT / ".config"

BASE_CONFIG_YAML: Path = CONFIGS_DIR / "_base.yaml"
VERIFICATION_CONFIG_YAML: Path = CONFIGS_DIR / "verification.yaml"
CODING_CONFIG_YAML: Path = CONFIGS_DIR / "coding.yaml"
ARCH_PRINCIPLES_YAML: Path = CONFIGS_DIR / "arch_principles.yaml"

#: Server-level config (supervisor fleet, ``provider_order_file``, ...).
BACKEND_CONFIG_YAML: Path = BACKEND_DIR / "config.yaml"

#: Optional dotenv file read by :mod:`env_config`.
ENV_FILE: Path = BACKEND_DIR / ".env"

# ---------------------------------------------------------------------------
# provider-order contract state
# ---------------------------------------------------------------------------

#: Fallback-chain file, used when neither the env var nor
#: ``backend/config.yaml`` overrides it.
#:
#: This is a runtime artifact of whichever producer an installation runs,
#: so it lives beside the other per-deployment configuration rather than
#: at a path this repository invents. ``.config/`` is already the
#: documented, gitignored home for exactly that — see :data:`CONFIG_DIR`.
#: Nothing is required to exist here; a missing file means no producer is
#: running, which ``load_fallback_order``'s caller reports rather than
#: treating as an error.
DEFAULT_PROVIDER_ORDER_FILE: Path = CONFIG_DIR / "provider-order.json"


def _load_backend_config_yaml() -> Dict[str, Any]:
    """Read :data:`BACKEND_CONFIG_YAML` and return it as a plain dict.

    Returns an empty dict when the file is missing or malformed — and
    also when PyYAML is unavailable — so callers can rely on ``.get()``
    without try/except noise. Path resolution must never be the reason
    a process fails to start; the hard-coded defaults remain usable.
    """
    yaml_path = BACKEND_CONFIG_YAML
    if not yaml_path.exists():
        return {}
    try:
        import yaml  # imported lazily: keeps module-scope stdlib-only

        with open(yaml_path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_plans_dir() -> Path:
    """Return the absolute per-plan artifact root to use right now.

    Precedence (first hit wins):

    1. ``PDT_PLANS_DIR`` environment variable — the test / harness
       escape hatch, mirroring ``PDT_STATE_DB_PATH`` for the SQLite
       state store.
    2. :data:`PLANS_DIR` — ``<repo>/plans``.

    **Why this exists (2026-09-13):** the plans tree is *live operator
    state*. A test that constructs a leaf helper (``ExecutionLogger``,
    ``PlanState``, the repair generator) without passing an explicit
    directory used to land in ``<repo>/plans/<fixture-id>/``, leaving
    junk directories behind. The notifier's startup sweep then found
    those rows/dirs, decided they were real in-flight plans, and pushed
    empty "执行中" Feishu cards with no task data — the operator-visible
    symptom of test pollution. ``server.PLANS_DIR`` already honoured
    ``PDT_PLANS_DIR``; this makes every other resolver agree.

    Resolution is re-run on every call (never cached) so a test that
    sets the env var *after* import still gets the redirected root.
    """
    env_value = os.environ.get("PDT_PLANS_DIR")
    if env_value:
        return Path(env_value).expanduser()
    return PLANS_DIR


def resolve_state_db_path() -> Path:
    """Return the state-machine SQLite file to use right now.

    Precedence (first hit wins):

    1. ``PDT_STATE_DB_PATH`` environment variable — operator override
       and the test/harness seam.
    2. :data:`STATE_DB` — ``<repo>/.pdt/state.db``.

    **Why this function exists (2026-09-28).** The same two-step
    resolution was re-implemented *nine* times across the backend —
    ``server._state_db_path``, ``plan_state._state_db_path``,
    ``task_repository``, ``task_manager`` (twice), ``agent``,
    ``watchdog``, ``verification.orchestrator`` and
    ``notifications.plan_dir_resolver`` — each deriving the default from
    its own ``__file__``. Two of them had already drifted:

    * ``verification/orchestrator`` used ``.parent.parent`` and reached
      ``backend/state.db`` instead of the repo root, writing RP-* repair
      rows into a stale 40 KB database no other process reads — the
      2026-09-12 bug its own comment still describes;
    * ``watchdog`` did not read ``PDT_STATE_DB_PATH`` at all, so a test
      that redirected the database still had the watchdog opening the
      operator's live one.

    Neither failed loudly. That is the property this module exists to
    remove: with one resolver, a wrong path is a single wrong line
    rather than a per-site drift nobody notices.

    Resolution is re-run on every call (never cached) so a test that
    sets the env var *after* import still gets the redirected file.
    """
    env_value = os.environ.get(STATE_DB_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return STATE_DB


def resolve_config_dir() -> Path:
    """Return the absolute local configuration directory.

    Precedence (first hit wins):

    1. ``PDT_CONFIG_DIR`` environment variable — absolute, or relative to
       :data:`PROJECT_ROOT`.
    2. :data:`CONFIG_DIR` — ``<repo>/.config``.

    Resolution is re-run on every call (never cached) so a test that sets
    the env var *after* import still gets the redirected directory.
    """
    env_value = os.environ.get("PDT_CONFIG_DIR")
    if env_value:
        candidate = Path(env_value).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate
    return CONFIG_DIR


def resolve_provider_routing_file() -> Path:
    """Return the absolute path to ``provider_routing.yaml``.

    Precedence (first hit wins):

    1. ``PDT_PROVIDER_ROUTING_FILE`` environment variable — the
       long-standing escape hatch, honoured first so existing overrides
       keep working.
    2. ``<config dir>/provider_routing.yaml``.

    There is deliberately **no fallback to a bundled copy**. An unrouted
    deployment should say so rather than silently run on a table nobody
    chose — the same principle that removed the built-in provider chain.
    """
    env_value = os.environ.get("PDT_PROVIDER_ROUTING_FILE")
    if env_value:
        candidate = Path(env_value).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate
    return resolve_config_dir() / "provider_routing.yaml"


def resolve_provider_capacity_file() -> Path:
    """Return the absolute path to ``provider_capacity.yaml``.

    Precedence (first hit wins):

    1. ``PDT_PROVIDER_CAPACITY_FILE`` environment variable.
    2. ``<config dir>/provider_capacity.yaml``.

    Same shape as :func:`resolve_provider_routing_file`, including the
    deliberate absence of a bundled fallback: a deployment that has not
    configured its per-provider concurrency should get a documented
    default (see ``provider_capacity``) rather than a table nobody chose.
    """
    env_value = os.environ.get("PDT_PROVIDER_CAPACITY_FILE")
    if env_value:
        candidate = Path(env_value).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate
    return resolve_config_dir() / "provider_capacity.yaml"


def resolve_sandbox_profile_file() -> Optional[Path]:
    """Return the Seatbelt profile sub-agents run under, or ``None``.

    Precedence (first hit wins):

    1. ``PDT_SANDBOX_PROFILE`` environment variable.
    2. ``<config dir>/sandbox_profile.sb``.

    ``None`` means "no sandbox configured", and the executor then spawns
    Claude unsandboxed exactly as it always has. A deployment that
    points this at a profile gets a **fail-closed** guarantee: see
    :mod:`claude_sandbox`, where a configured-but-unusable profile
    raises rather than silently falling back. That asymmetry is the
    whole point — a sandbox that quietly turns itself off is worse than
    no sandbox, because the operator's `/sandbox` status (or, here, the
    mere presence of the env var) says otherwise.

    macOS-only. On any other platform the profile cannot be applied and
    :mod:`claude_sandbox` raises rather than running unsandboxed, for
    the same reason.
    """
    env_value = os.environ.get("PDT_SANDBOX_PROFILE")
    if env_value:
        candidate = Path(env_value).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate
    bundled = resolve_config_dir() / "sandbox_profile.sb"
    return bundled if bundled.is_file() else None


def resolve_provider_order_file() -> Path:
    """Return the absolute path to ``provider-order.json``.

    Precedence (first hit wins):

    1. ``PROVIDER_ORDER_FILE`` environment variable — operator escape
       hatch for tests and one-off overrides. Relative values are
       resolved against :func:`Path.cwd`.
    2. ``provider_order_file`` in ``backend/config.yaml`` — relative
       values are resolved against :data:`PROJECT_ROOT`, so the path
       stays valid no matter where the process was launched from.
    3. ``<config dir>/provider-order.json`` — same shape as
       :func:`resolve_provider_routing_file` and
       :func:`resolve_provider_capacity_file`, including the deliberate
       absence of a bundled copy.

    Resolution is re-run on every call so a test (or an operator
    flipping the env var) sees the current value; import-time callers
    can use the :data:`PROVIDER_ORDER_FILE` constant below instead.
    """
    env_value = os.environ.get("PROVIDER_ORDER_FILE")
    if env_value:
        candidate = Path(env_value).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        return candidate

    cfg_value = _load_backend_config_yaml().get("provider_order_file")
    if cfg_value:
        candidate = Path(str(cfg_value)).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate

    return resolve_config_dir() / "provider-order.json"


#: Import-time snapshot, mirroring ``server.PROVIDER_ORDER_FILE``.
PROVIDER_ORDER_FILE: Path = resolve_provider_order_file()


#: Interface the HTTP server binds to when ``PDT_HOST`` is unset.
#: Loopback on purpose — see :func:`resolve_server_host`.
DEFAULT_SERVER_HOST: str = "127.0.0.1"


def resolve_server_host() -> str:
    """Return the interface the HTTP server binds to.

    Defaults to :data:`DEFAULT_SERVER_HOST` — **loopback only**. The web
    UI has no authentication, so a wildcard bind exposes every endpoint
    (including the ones that spawn subprocesses and write files) to
    anyone who can reach the host over the network. That must not be
    something an operator gets by accident.

    An operator who genuinely needs LAN access — a container, a VM, a
    deliberately shared box — opts in by setting ``PDT_HOST``. Making the
    exposure explicit is the whole point.

    Resolution is re-run on every call so a test that sets the env var
    after import still sees it.
    """
    return os.environ.get("PDT_HOST") or DEFAULT_SERVER_HOST
