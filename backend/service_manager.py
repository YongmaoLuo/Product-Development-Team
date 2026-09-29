"""Plan-service lifecycle owner (2026-09-18, C2 of the service-ownership work).

Why this module exists
-----------------------
C1 (:mod:`service_declaration`) gave the plan a single, authoritative
``services`` block and a way to reference those services by name. This
module is what actually *honours* that declaration: it brings each
declared service up **once** before any VP runs, and records who owns
the resulting process so the round can be unwound later (C3).

The behaviour it replaces is documented in
:mod:`service_freshness` — a regex-scraped port list, a freshness rule
that only asked "was the listener started after the last source edit?",
and a restart agent whose fast path re-exec'd whatever cmdline happened
to hold the port. On 2026-09-18 that combination blessed a ``next
start`` production build on the port a VP wanted for ``next dev``,
because "started 43s after the last edit" says nothing about *which
server flavour* is running.

Ownership rules (the part that matters)
---------------------------------------
A declared port can be held by three kinds of process, and the
response to each is deliberately different:

  1. **Ours, from this round** — recorded in the ledger (C3). Fresh →
     adopt. Stale → restart using the *declared* ``start_cmd`` (which
     is authoritative; the captured cmdline of an old process is not).
  2. **A foreign process whose cmdline matches the declaration** — a
     service the operator already had running. Adopt it if fresh;
     restart it if stale. Either way it is *not* killed unless its
     cmdline matches — see rule 3.
  3. **A foreign process whose cmdline does NOT match** — **relocate**.
     Nothing is killed (the same safety position
     :mod:`service_restart_agent` takes: "never kill a process you
     cannot positively identify", which is what stops this module from
     re-enacting the 2026-09-07 incident where a restart script murdered
     the backend on :8000) — but refusing outright would let someone
     else's process strand the whole round. So we start our own copy on a
     free port and point every VP at it.

     That last part is the 2026-09-18 decision: a process whose cmdline
     matches the declaration is one we started, so it may be killed;
     anything else is unknown — quite possibly started by someone else —
     and must not be touched. Rule 3 is how the round gets out of its
     way. The shape it exists for: an operator's
     ``next-server`` production build on :3000 while the plan wanted
     ``next dev`` — and the old behaviour blocked every dependent VP.

Matching is by *all* of the declaration's normalised command tokens, so
``npx next dev`` matches ``npm exec next dev`` but not ``next-server``.
See :func:`normalized_command_tokens` for why each normalisation is
there.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from service_declaration import SERVICE_HOST, ServiceDeclaration
from service_freshness import (
    PROTECTED_PORTS,
    PortProbe,
    newest_project_source_mtime,
    probe_port,
    wait_for_ready,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Where the round's "who did we start" record lives, relative to the
#: plan directory. C3 reads it on every workflow exit and on backend
#: startup (a crash leaves it behind on purpose — that is the whole
#: point of persisting it).
LEDGER_FILENAME = "managed_services.json"

#: Shell noise that carries no identity: operators, redirections,
#: separators. Dropped before matching so ``a && b`` and ``a; b`` and
#: ``a > log 2>&1`` all normalise the same way.
_SHELL_NOISE_RE = re.compile(r"^(&&|\|\||\||;|&|>|>>|<|2>&1|2>|&>)$")

#: Launcher wrappers that are interchangeable spellings of the same
#: thing. ``npx next dev``, ``npm exec next dev`` and ``pnpm exec next
#: dev`` are the same server; the 2026-09-18 leak was literally a
#: declaration saying ``npx`` and a live process saying ``npm exec``.
_WRAPPER_TOKENS = frozenset({
    "npx", "npm", "pnpm", "yarn", "bun", "exec", "run", "dlx", "bunx",
    "nohup", "sudo", "env", "time", "exec",
})

#: Tokens that must never be dropped even though they are common, and
#: tokens that must always be dropped. ``dev`` / ``start`` / ``serve``
#: are *kept* on purpose — they are exactly the words that distinguish
#: ``next dev`` from ``next start`` — the adoption that must fail.
_KEEP_TOKENS = frozenset({"dev", "start", "serve", "server", "watch"})

#: Flags whose value is the next token, so both must be dropped
#: together (``-p 3000``, ``--port 3000``, ``--host 0.0.0.0``).
_VALUE_FLAGS = frozenset({
    "-p", "--port", "-h", "--host", "-w", "--workers",
    "-c", "--config", "--hostname", "--dir", "-H",
})

_READY_POLL_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Command normalisation + matching
# ---------------------------------------------------------------------------


def normalized_command_tokens(start_cmd: str) -> List[str]:
    """The identity-bearing tokens of a start command.

    Drops, in order: shell noise, environment assignments
    (``FOO=bar``), flags and their values (``-p 3000``), ``cd`` targets,
    ``source``/activate prefixes, and launcher wrappers (``nohup``,
    ``npx``, ``npm exec``…). Keeps everything else, **including**
    ``dev``/``start``/``serve`` — those words are precisely what tells
    ``next dev`` apart from ``next start``.

    The result is used as an "all of these must appear in the listener's
    cmdline" set, so the function must over-keep rather than over-drop:
    one dropped identity token turns a mismatched process into an
    adopted one.
    """
    if not isinstance(start_cmd, str):
        return []
    # Normalise path-ish tokens by their basename later; here we only
    # need a token stream.
    tokens = start_cmd.replace("\n", " ").split()
    kept: List[str] = []
    index = 0
    skip_next = False
    while index < len(tokens):
        raw = tokens[index]
        index += 1
        if skip_next:
            skip_next = False
            continue
        if _SHELL_NOISE_RE.match(raw):
            continue
        if raw in ("source", "."):
            # ``source venv1/bin/activate`` — the whole construct is an
            # environment setup, not an identity.
            skip_next = True
            continue
        if raw == "cd":
            skip_next = True
            continue
        if "=" in raw and not raw.startswith("-"):
            head = raw.split("=", 1)[0]
            if head and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", head):
                continue  # FOO=bar env assignment
        if raw.startswith("-"):
            if raw in _VALUE_FLAGS:
                skip_next = True
            continue
        if raw.lower() in _WRAPPER_TOKENS and raw.lower() != "run":
            # ``npm run dev`` keeps ``dev``; ``npm exec`` keeps the rest.
            continue
        # Strip surrounding quotes and take the basename of a path so
        # ``venv1/bin/python`` and ``/usr/bin/python3`` both reduce to
        # an interpreter name that substring-matches either spelling.
        token = raw.strip("'\"")
        if "/" in token and not token.startswith("-"):
            token = token.rstrip("/").rsplit("/", 1)[-1]
        if not token:
            continue
        if raw.lower() in _WRAPPER_TOKENS:
            continue
        kept.append(token)
    # ``run``/``exec`` are wrappers only when they are not the whole
    # command; dropping them unconditionally is safe because a real
    # process name ``run`` does not exist in practice.
    return [t for t in kept if t.lower() != "run" or t in _KEEP_TOKENS]


def matches_declaration(decl: ServiceDeclaration, probe: PortProbe) -> bool:
    """True when the process holding ``probe``'s port looks like the
    service ``decl`` describes.

    Requires **every** identity token of the declared ``start_cmd`` to
    appear (case-insensitively, as a substring) in at least one of the
    listener's cmdlines. Substring rather than token equality so
    ``python`` matches ``/path/to/venv1/bin/python``.

    A declaration with no identity tokens at all (a bare binary name)
    matches nothing — better to block with a reason than to adopt a
    stranger.
    """
    tokens = normalized_command_tokens(decl.start_cmd)
    if not tokens:
        return False
    if not probe.listening:
        return False
    haystack = " ".join(probe.cmdlines).lower()
    if not haystack:
        return False
    return all(token.lower() in haystack for token in tokens)


# ---------------------------------------------------------------------------
# Runtime record
# ---------------------------------------------------------------------------


@dataclass
class ServiceRuntime:
    """What the round learned about one declared service."""

    name: str
    port: int
    #: The port the *plan* declared. Differs from ``port`` when the round
    #: had to relocate (see :func:`ensure_services` rule 3) — the ledger
    #: records both so an operator can tell "it moved" from "it failed".
    declared_port: Optional[int] = None
    pid: Optional[int] = None
    pgid: Optional[int] = None
    start_epoch: Optional[float] = None
    start_cmd: str = ""
    cwd: str = "."
    log_path: str = ""
    #: "spawned" (this round started it) | "adopted" (a matching
    #: process was already live and fresh) | "ready_foreign" (a
    #: matching foreign process was restarted) | "failed"
    origin: str = "failed"
    ready: bool = False
    reap_on_exit: bool = True
    detail: str = ""

    @property
    def spawned_by_ac(self) -> bool:
        return self.origin == "spawned"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "declared_port": (
                self.declared_port if self.declared_port is not None else self.port
            ),
            "pid": self.pid,
            "pgid": self.pgid,
            "start_epoch": self.start_epoch,
            "start_cmd": self.start_cmd,
            "cwd": self.cwd,
            "log_path": self.log_path,
            "origin": self.origin,
            "ready": self.ready,
            "reap_on_exit": self.reap_on_exit,
            "detail": self.detail,
        }


@dataclass
class ServiceRuntimeMap:
    """Result of :func:`ensure_services`."""

    runtimes: Dict[str, ServiceRuntime] = field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def ready_names(self) -> List[str]:
        return sorted(n for n, r in self.runtimes.items() if r.ready)

    @property
    def unready_ports(self) -> List[int]:
        return sorted(r.port for r in self.runtimes.values() if not r.ready)

    @property
    def blocked_ports(self) -> List[int]:
        """Ports whose VPs must be BLOCKED — every service that is not
        ready, plus every runtime that failed outright."""
        return self.unready_ports

    def enabled(self) -> bool:
        return bool(self.runtimes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "services": {n: r.to_dict() for n, r in self.runtimes.items()},
            "ready": self.ready_names,
            "unready_ports": self.unready_ports,
            "duration_seconds": round(self.duration_seconds, 2),
        }


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _process_group_of(pid: int) -> Optional[int]:
    out = _run(["ps", "-p", str(pid), "-o", "pgid="])
    if not out:
        return None
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return None


def _process_start_epoch(pid: int) -> Optional[float]:
    raw = _run(["ps", "-p", str(pid), "-o", "lstart="])
    if not raw:
        return None
    from service_freshness import _parse_ps_lstart  # local: private helper

    return _parse_ps_lstart(raw)


def _run(argv: Sequence[str], timeout: int = 10) -> Optional[str]:
    """Best-effort subprocess helper; never raises."""
    try:
        out = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001 - probes are best-effort
        logger.debug("[service_manager] %s failed: %s", argv, exc)
        return None


def stop_service(
    runtime: ServiceRuntime,
    decl: Optional[ServiceDeclaration] = None,
    *,
    grace_seconds: float = 10.0,
) -> Dict[str, Any]:
    """Stop a service this module (or a previous round) started.

    Signals the **process group** when one is known — an ``npx next
    dev`` chain is three processes (npm exec → node → next-server) and
    killing only the recorded PID leaves the wrapper alive to re-spawn
    or hold the port. Then waits for the port to be released and
    reports it.

    Refuses to touch protected ports. Never raises.
    """
    result: Dict[str, Any] = {
        "name": runtime.name, "port": runtime.port, "killed": [],
        "signalled": [], "port_free": False, "detail": "",
    }
    if runtime.port in PROTECTED_PORTS:
        result["detail"] = (
            f"refusing to stop port {runtime.port}: backend-runtime port"
        )
        return result

    targets: List[int] = []
    if runtime.pid:
        targets.append(runtime.pid)
    # Re-probe: the recorded PID may be gone while a sibling still
    # holds the port (that is exactly the wrapper-survives case).
    if decl is not None:
        probe = probe_port(runtime.port)
        for pid, cmdline in zip(probe.pids, probe.cmdlines or []):
            if pid not in targets and cmdline and matches_declaration(
                decl, PortProbe(
                    port=runtime.port, listening=True, pids=[pid],
                    cmdlines=[cmdline],
                ),
            ):
                targets.append(pid)

    if not targets:
        result["port_free"] = not probe_port(runtime.port).listening
        result["detail"] = "no process to stop"
        return result

    pgid = runtime.pgid or _process_group_of(targets[0])
    signalled_pg = False
    if pgid and pgid > 1:
        try:
            os.killpg(pgid, 15)
            result["signalled"].append(f"pgid {pgid} SIGTERM")
            signalled_pg = True
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug(
                "[service_manager] killpg %s failed: %s", pgid, exc,
            )
    if not signalled_pg:
        for pid in targets:
            try:
                os.kill(pid, 15)
                result["signalled"].append(f"pid {pid} SIGTERM")
            except (ProcessLookupError, PermissionError, OSError):
                continue

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not probe_port(runtime.port).listening:
            result["port_free"] = True
            result["detail"] = "stopped after SIGTERM"
            return result
        time.sleep(0.5)

    # Escalate.
    for pid in targets:
        try:
            os.kill(pid, 9)
            result["killed"].append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    if pgid and pgid > 1:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    time.sleep(1.0)
    result["port_free"] = not probe_port(runtime.port).listening
    result["detail"] = (
        "stopped after SIGKILL" if result["port_free"]
        else f"port {runtime.port} still held after SIGKILL"
    )
    return result


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------


#: How many different ports to try before giving up on a relocation.
#: A readiness failure after a relocation can mean two things, and both are
#: worth one more try: the port we picked was taken between the probe and
#: the service's bind (a TOCTOU race), or the service ignored our rewrite
#: and tried to bind the declared port (which is occupied).
_RELOCATION_ATTEMPTS = 3


def allocate_free_port() -> Optional[int]:
    """An ephemeral port that nothing currently holds on loopback.

    Binding port 0 and reading back what the kernel chose is the only
    race-free way to ask. The port is released immediately, so a
    relocation still has a TOCTOU window — which is why
    :func:`ensure_services` retries with a fresh port when readiness
    fails.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
    except OSError as exc:
        logger.warning("[service_manager] could not allocate a port: %s", exc)
        return None
    if port in PROTECTED_PORTS:
        return None
    return port


def rewrite_port(text: Any, old_port: int, new_port: int) -> Any:
    """Replace every standalone ``old_port`` literal in ``text``.

    Whole-number matching only (``(?<!\\d)3000(?!\\d)``), so ``8080`` is
    never rewritten inside ``180801`` or ``80801``. Used on a declared
    ``start_cmd`` / ``health_url`` when relocating a service.
    """
    if not isinstance(text, str) or not text:
        return text
    return re.sub(
        rf"(?<!\d){int(old_port)}(?!\d)", str(int(new_port)), text,
    )


def render_declaration(
    decl: ServiceDeclaration, port: int,
) -> ServiceDeclaration:
    """The declaration as it will actually be run, on ``port``.

    Only the port-bearing fields move: the command, the health URL, and
    the log filename (so two relocations of the same service do not
    interleave in one log).
    """
    if port == decl.port:
        return decl
    return ServiceDeclaration(
        name=decl.name,
        port=port,
        start_cmd=rewrite_port(decl.start_cmd, decl.port, port),
        cwd=decl.cwd,
        health_url=rewrite_port(decl.health_url, decl.port, port),
        ready_timeout_seconds=decl.ready_timeout_seconds,
        log_path=decl.log_path,
        reap_on_exit=decl.reap_on_exit,
    )


def service_env(decl: ServiceDeclaration) -> Dict[str, str]:
    """Environment variables a VP can read for this service.

    ``PDT_SVC_<NAME>_URL`` / ``_PORT`` / ``_HOST``. The name is
    upper-cased with non-alphanumerics folded to ``_``, so
    ``api-server`` becomes ``PDT_SVC_API_SERVER_PORT``.

    Injected into the service process *and* advertised in the VP prompt:
    a VP that reads them can never be pointing at a stale port, which is
    the whole reason relocation is safe to do at all.
    """
    key = re.sub(r"[^A-Za-z0-9]+", "_", decl.name).strip("_").upper()
    return {
        f"PDT_SVC_{key}_URL": decl.url,
        f"PDT_SVC_{key}_PORT": str(decl.port),
        f"PDT_SVC_{key}_HOST": SERVICE_HOST,
    }


def _log_path_for(decl: ServiceDeclaration, project_dir: Path) -> Path:
    if decl.log_path:
        return Path(project_dir) / decl.log_path
    return Path(project_dir) / "tmp" / f"pdt_service_{decl.name}.log"


def wait_until_ready(
    decl: ServiceDeclaration, timeout_seconds: Optional[int] = None,
) -> bool:
    """HTTP-probe ``health_url`` when declared, else TCP-accept.

    A declared ``health_url`` is the stronger signal and is what lets a
    service distinguish "listening" from "serving"; without one the
    module falls back to :func:`service_freshness.wait_for_ready`.
    """
    timeout = timeout_seconds or decl.ready_timeout_seconds
    if not decl.health_url:
        return wait_for_ready(decl.port, timeout_seconds=timeout)

    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(decl.health_url, timeout=3) as resp:
                if 200 <= resp.status < 400:
                    return True
        except Exception:  # noqa: BLE001 - any failure means "not yet"
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL_INTERVAL)


def _relocate_service(
    decl: ServiceDeclaration,
    project_dir: Path,
    *,
    foreign_cmdline: str = "",
) -> ServiceRuntime:
    """Start the service on a different, free port — without touching the
    process that currently holds the declared one.

    This is the third ownership rule from the module docstring: a port
    held by a process we cannot positively identify is **not ours to
    kill**, but it is also not a reason to strand the round. The shape it
    exists for: the operator had ``next-server`` (a production
    build) on :3000 while the plan wanted ``next dev`` — and the old
    behaviour was to refuse and block every VP that depended on it.

    The declared ``start_cmd`` / ``health_url`` are rewritten to the new
    port, and ``PDT_SVC_<NAME>_*`` is exported into the child so a VP (or
    the service itself) can discover where it landed.

    Readiness is retried on a **fresh** port, because a failure can mean
    either a TOCTOU race (someone took the port between :func:`allocate_free_port`
    and the service's bind) or a service that ignored our rewrite and
    tried to bind the occupied declared port. Both are worth another port.
    """
    last_detail = ""
    for attempt in range(1, _RELOCATION_ATTEMPTS + 1):
        port = allocate_free_port()
        if port is None or port == decl.port:
            last_detail = "could not allocate a free port"
            continue

        relocated = render_declaration(decl, port)
        runtime = start_service(relocated, project_dir)
        runtime.declared_port = decl.port
        if runtime.ready:
            runtime.origin = "spawned"
            runtime.detail = (
                f"port {decl.port} held by a process that does not look "
                f"like service {decl.name!r} ({foreign_cmdline}); left it "
                f"alone and started a copy on port {port} instead "
                f"(attempt {attempt}/{_RELOCATION_ATTEMPTS})"
            )
            return runtime

        # A failed attempt may still have left a half-started process
        # behind — clean it up before trying another port, or the ledger
        # accumulates orphans.
        if runtime.pid:
            stop_service(runtime, relocated, grace_seconds=5.0)
        last_detail = runtime.detail

    failed = ServiceRuntime(
        name=decl.name, port=decl.port, declared_port=decl.port,
        start_cmd=decl.start_cmd, cwd=decl.cwd,
        reap_on_exit=decl.reap_on_exit,
    )
    failed.origin = "failed"
    failed.detail = (
        f"port {decl.port} is held by a process that does not look like "
        f"service {decl.name!r} ({foreign_cmdline}), and relocating failed "
        f"after {_RELOCATION_ATTEMPTS} attempt(s): {last_detail}"
    )
    return failed


def start_service(
    decl: ServiceDeclaration, project_dir: Path,
    *, timeout_seconds: Optional[int] = None,
) -> ServiceRuntime:
    """Launch one declared service detached and wait for readiness.

    Detached (``start_new_session=True``) so it outlives this process —
    which is also why C3 exists: a detached process nobody reaps is an
    orphan.
    """
    runtime = ServiceRuntime(
        name=decl.name, port=decl.port, start_cmd=decl.start_cmd,
        cwd=decl.cwd, reap_on_exit=decl.reap_on_exit,
    )
    if decl.port in PROTECTED_PORTS:
        runtime.detail = (
            f"port {decl.port} is an backend-runtime port; not starting"
        )
        return runtime

    workdir = Path(project_dir) / decl.cwd
    if not workdir.is_dir():
        runtime.detail = f"cwd {decl.cwd!r} does not exist"
        return runtime

    log_file = _log_path_for(decl, project_dir)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        runtime.detail = f"cannot create log dir: {exc}"
        return runtime
    runtime.log_path = str(log_file)

    try:
        env = dict(os.environ)
        env.update(service_env(decl))
        with open(log_file, "ab") as log_fp:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", decl.start_cmd],
                cwd=str(workdir),
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:  # noqa: BLE001 - never abort the round
        runtime.detail = f"spawn failed: {type(exc).__name__}: {exc}"
        return runtime

    runtime.pid = proc.pid
    runtime.pgid = _process_group_of(proc.pid)
    runtime.start_epoch = _process_start_epoch(proc.pid)
    runtime.origin = "spawned"

    if wait_until_ready(decl, timeout_seconds):
        runtime.ready = True
        runtime.detail = (
            f"started (pid {proc.pid}); ready within "
            f"{decl.ready_timeout_seconds}s"
        )
        return runtime

    # Not ready: report what the log's tail says so the operator gets a
    # diagnosable reason instead of a bare timeout.
    tail = _tail(log_file, 15)
    runtime.detail = (
        f"started (pid {proc.pid}) but not ready after "
        f"{decl.ready_timeout_seconds}s; log tail: {tail or '(empty)'}"
    )
    return runtime


def _tail(path: Path, lines: int) -> str:
    try:
        content = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(content[-lines:])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def ensure_services(
    project_dir: Path,
    declarations: Dict[str, ServiceDeclaration],
    *,
    round_number: int = 0,
    ignore_freshness: bool = False,
) -> ServiceRuntimeMap:
    """Bring every declared service to a live, ready state.

    Order per service:

      1. Port free → start it with the declared ``start_cmd``.
      2. Port held by a process matching the declaration → adopt it,
         unless it predates the newest source edit (then restart it
         with the declared command, not the captured one).
      3. Port held by anything else → **refuse**: no kill, service
         reported unready with a reason, its VPs get BLOCKED.

    Never raises; a failure is a per-service ``ready=False``.
    """
    start = time.monotonic()
    result = ServiceRuntimeMap()
    project_dir = Path(project_dir)
    if not declarations:
        result.duration_seconds = time.monotonic() - start
        return result

    newest_source = newest_project_source_mtime(project_dir)

    for name in sorted(declarations):
        decl = declarations[name]
        runtime = ServiceRuntime(
            name=name, port=decl.port, start_cmd=decl.start_cmd,
            cwd=decl.cwd, reap_on_exit=decl.reap_on_exit,
        )
        if decl.port in PROTECTED_PORTS:
            # Belt-and-braces: the declaration parser already rejects
            # these (they are the backend runtime's own control plane, and
            # the 2026-09-07 incident killed the backend on :8000 this
            # way), but every other function in this area re-checks, so
            # a future caller that bypasses the parser cannot get past
            # here either.
            runtime.detail = (
                f"port {decl.port} is an backend-runtime port; refusing to "
                f"manage service {name!r}"
            )
            result.runtimes[name] = runtime
            continue
        try:
            probe = probe_port(decl.port)
        except Exception as exc:  # noqa: BLE001 - probe never raises
            runtime.detail = f"probe failed: {exc}"
            result.runtimes[name] = runtime
            continue

        if probe.listening:
            if not matches_declaration(decl, probe):
                # Rule 3: the port is held by something we cannot identify,
                # so we neither adopt it nor kill it — **we get out of the
                # way**. Start our own copy on a free port and point every
                # VP at that. (2026-09-18 decision: a process whose
                # cmdline matches the declaration is ours to kill; anything
                # else is unknown and must not be touched — the round
                # gets out of its way instead.)
                foreign = '; '.join(probe.cmdlines) or 'unknown'
                runtime = _relocate_service(
                    decl, project_dir, foreign_cmdline=foreign,
                )
                result.runtimes[name] = runtime
                continue

            stale = (
                not ignore_freshness
                and newest_source is not None
                and probe.process_start_epoch is not None
                and probe.process_start_epoch < newest_source
            )
            if not stale:
                runtime.origin = "adopted"
                runtime.ready = True
                runtime.pid = probe.pids[0] if probe.pids else None
                runtime.start_epoch = probe.process_start_epoch
                runtime.pgid = (
                    _process_group_of(runtime.pid) if runtime.pid else None
                )
                runtime.detail = "adopted a live, fresh, matching listener"
                result.runtimes[name] = runtime
                continue

            # Matching but stale: restart it. Its cmdline matched the
            # declaration, so killing it is the identified-process case
            # the safety model allows.
            runtime.origin = "ready_foreign"
            runtime.pid = probe.pids[0] if probe.pids else None
            runtime.pgid = (
                _process_group_of(runtime.pid) if runtime.pid else None
            )
            stop_info = stop_service(runtime, decl)
            if not stop_info["port_free"]:
                runtime.ready = False
                runtime.detail = (
                    f"stale matching listener could not be stopped: "
                    f"{stop_info['detail']}"
                )
                result.runtimes[name] = runtime
                continue
            restarted = start_service(decl, project_dir)
            restarted.origin = "ready_foreign" if restarted.ready else "failed"
            restarted.detail = (
                f"restarted stale listener (was pid {runtime.pid}); "
                f"{restarted.detail}"
            )
            result.runtimes[name] = restarted
            continue

        runtime = start_service(decl, project_dir)
        result.runtimes[name] = runtime

    result.duration_seconds = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Ledger (written here, acted upon by C3)
# ---------------------------------------------------------------------------


def ledger_path(plan_dir: Path) -> Path:
    return Path(plan_dir) / LEDGER_FILENAME


def record_runtimes(
    plan_dir: Path,
    plan_id: str,
    runtime_map: ServiceRuntimeMap,
    *,
    project_dir: Path,
    round_number: int = 0,
) -> Optional[Path]:
    """Persist who this round started, so it can be reaped later.

    Only services **this process spawned** are recorded. Adopted
    listeners are deliberately excluded: they were running before AC
    touched them, so the backend must not claim ownership of them and C3 must
    not kill them on exit.

    Never raises — a ledger write failure costs the exit-time reap of
    one round, which is strictly better than aborting the round.
    """
    spawned = [
        r for r in runtime_map.runtimes.values()
        if r.spawned_by_ac and r.pid
    ]
    if not spawned:
        return None
    payload = {
        "plan_id": plan_id,
        "project_dir": str(project_dir),
        "round": round_number,
        "updated_at": datetime.now().isoformat(),
        "services": [r.to_dict() for r in spawned],
    }
    path = ledger_path(plan_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path
    except OSError as exc:
        logger.warning(
            "[service_manager] could not write service ledger %s: %s",
            path, exc,
        )
        return None


def read_ledger(plan_dir: Path) -> Dict[str, Any]:
    """Read a plan's service ledger. Returns ``{}`` when absent or
    corrupt — a damaged ledger must not block a reap sweep."""
    path = ledger_path(plan_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Reaping (C3) — no backend-spawned process outlives the workflow
# ---------------------------------------------------------------------------


@dataclass
class ReapReport:
    """Outcome of one reaping pass over a plan's ledger."""

    plan_id: str = ""
    reason: str = ""
    reaped: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    #: Ports still held by a process we could NOT identify. Reported,
    #: never killed — see the module docstring's ownership rules.
    foreign_ports: List[int] = field(default_factory=list)
    ledger_cleared: bool = False

    @property
    def clean(self) -> bool:
        return not self.failed and not self.foreign_ports

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "reason": self.reason,
            "reaped": list(self.reaped),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "foreign_ports": list(self.foreign_ports),
            "ledger_cleared": self.ledger_cleared,
        }


def _declaration_from_ledger_entry(entry: Dict[str, Any]) -> Optional[ServiceDeclaration]:
    """Rebuild just enough of a declaration to identify the process.

    The ledger stores the fields a reap needs (port + start_cmd), not
    the whole declaration — the readiness timeout and health URL are
    meaningless once the service is running.
    """
    try:
        return ServiceDeclaration(
            name=str(entry.get("name") or ""),
            port=int(entry.get("port")),
            start_cmd=str(entry.get("start_cmd") or ""),
            cwd=str(entry.get("cwd") or "."),
            log_path=str(entry.get("log_path") or ""),
        )
    except (TypeError, ValueError):
        return None


def _pid_reused(entry: Dict[str, Any]) -> bool:
    """True when the recorded PID now belongs to a different process.

    PIDs are recycled; killing ``entry["pid"]`` without this check can
    murder an unrelated process that inherited the number. Compares the
    recorded ``start_epoch`` against the live one with a 2-second
    tolerance (``ps -o lstart=`` has 1-second granularity and the two
    readings can straddle a tick).
    """
    pid = entry.get("pid")
    recorded = entry.get("start_epoch")
    if not pid or recorded is None:
        return False
    live = _process_start_epoch(int(pid))
    if live is None:
        return False  # process gone, or ps unavailable — not evidence of reuse
    return abs(float(live) - float(recorded)) > 2.0


def reap_services(
    plan_dir: Path,
    plan_id: str = "",
    *,
    reason: str = "",
    grace_seconds: float = 10.0,
) -> ReapReport:
    """Stop every service this plan's ledger says the backend started.

    Safety, in order — each check can only *prevent* a kill:

      1. ``reap_on_exit: false`` in the ledger → skip (operator opt-out).
      2. Recorded PID has been recycled → skip. PID reuse is how an
         "orphan sweep" kills something it never started.
      3. The listener is neither the recorded PID, nor in the recorded
         process group, nor a signature match for the declared
         ``start_cmd`` → report as foreign, do not kill. The group is
         what catches ``next dev``, where the port is held by a
         grandchild whose rewritten process title matches nothing.
      4. After stopping, the port is re-probed. Still held, and the
         holder matches the declaration → kill that too (a leaked
         VP-spawned sibling). Still held and unidentified → foreign.

    The ledger is cleared only when nothing failed and no foreign
    holder remains; otherwise the un-reaped entries are written back so
    a later sweep retries them.
    """
    plan_dir = Path(plan_dir)
    report = ReapReport(plan_id=plan_id or plan_dir.name, reason=reason)
    ledger = read_ledger(plan_dir)
    entries = ledger.get("services")
    if not isinstance(entries, list) or not entries:
        return report

    survivors: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "?")
        port = entry.get("port")
        if not entry.get("reap_on_exit", True):
            report.skipped.append(
                {"name": name, "reason": "reap_on_exit=false"}
            )
            survivors.append(entry)
            continue
        if not isinstance(port, int):
            report.skipped.append(
                {"name": name, "reason": "ledger entry has no usable port"}
            )
            continue
        if port in PROTECTED_PORTS:
            report.skipped.append(
                {"name": name, "reason": f"port {port} is an backend-runtime port"}
            )
            continue
        if _pid_reused(entry):
            report.skipped.append(
                {"name": name, "reason": "recorded pid was recycled"}
            )
            continue

        decl = _declaration_from_ledger_entry(entry)
        try:
            probe = probe_port(port)
        except Exception as exc:  # noqa: BLE001 - probe never raises
            report.failed.append({"name": name, "reason": f"probe: {exc}"})
            survivors.append(entry)
            continue

        if not probe.listening:
            report.reaped.append(name)  # already gone; nothing to do
            continue

        recorded_pid = entry.get("pid")
        recorded_pgid = entry.get("pgid")
        ours = bool(recorded_pid) and recorded_pid in probe.pids
        if not ours and recorded_pgid:
            # 2026-09-23: the listener is often a DESCENDANT of the
            # process the ledger recorded, and then neither check above
            # can see it. ``npx next dev -p N`` spawns ``npm exec`` →
            # ``node .../.bin/next dev`` → ``next-server``; only
            # ``next-server`` binds the port, and it rewrites its own
            # process title, so ``matches_declaration`` has nothing to
            # match either. All three share the group npm created, and
            # the ledger has carried ``pgid`` all along —
            # ``stop_service`` is already group-aware and kills with
            # ``killpg``, it just never got the chance.
            #
            # Residual risk: a recycled pgid could in principle name a
            # stranger's group. It would also have to have bound this
            # exact port, and the ledger only ever records ports the backend
            # itself started. That is a far smaller exposure than the
            # leak it fixes — port 60129 outlived its plan by 3.4 days
            # and would have outlived it indefinitely.
            ours = any(
                _process_group_of(pid) == recorded_pgid
                for pid in probe.pids
            )
        if not ours and decl is not None:
            ours = matches_declaration(decl, probe)
        if not ours:
            report.foreign_ports.append(port)
            report.failed.append({
                "name": name,
                "reason": (
                    f"port {port} is held by an unidentified process "
                    f"({'; '.join(probe.cmdlines) or 'cmdline unknown'}); "
                    f"not killed"
                ),
            })
            survivors.append(entry)
            continue

        runtime = ServiceRuntime(
            name=name, port=port, pid=recorded_pid,
            pgid=entry.get("pgid"),
            start_cmd=str(entry.get("start_cmd") or ""),
        )
        stop_info = stop_service(runtime, decl, grace_seconds=grace_seconds)
        if stop_info.get("port_free"):
            report.reaped.append(name)
            continue

        # Still held. If the holder still matches the declaration it is
        # a leaked sibling of the same service (a VP that started its
        # own copy) — fair game. Anything else is foreign.
        after = probe_port(port)
        if decl is not None and after.listening and matches_declaration(
            decl, after,
        ):
            sibling = ServiceRuntime(
                name=name, port=port,
                pid=after.pids[0] if after.pids else None,
                pgid=_process_group_of(after.pids[0]) if after.pids else None,
                start_cmd=str(entry.get("start_cmd") or ""),
            )
            second = stop_service(
                sibling, decl, grace_seconds=grace_seconds,
            )
            if second.get("port_free"):
                report.reaped.append(name)
                continue
            report.failed.append({
                "name": name,
                "reason": f"still held after a second stop: {second.get('detail')}",
            })
        elif after.listening:
            report.foreign_ports.append(port)
            report.failed.append({
                "name": name,
                "reason": (
                    f"port {port} was taken over by an unidentified "
                    f"process; not killed"
                ),
            })
        else:
            report.failed.append({
                "name": name,
                "reason": str(stop_info.get("detail") or "stop failed"),
            })
        survivors.append(entry)

    report.ledger_cleared = _finalise_ledger(
        plan_dir, ledger, survivors, report,
    )
    return report


def _finalise_ledger(
    plan_dir: Path,
    ledger: Dict[str, Any],
    survivors: List[Dict[str, Any]],
    report: ReapReport,
) -> bool:
    """Delete the ledger when the reap was clean, else rewrite it with
    what is left. Returns True when the file is gone."""
    path = ledger_path(plan_dir)
    if not survivors:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning(
                "[service_manager] could not remove ledger %s: %s", path, exc,
            )
            return False
    try:
        payload = dict(ledger)
        payload["services"] = survivors
        payload["updated_at"] = datetime.now().isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning(
            "[service_manager] could not rewrite ledger %s: %s", path, exc,
        )
    return False


def reap_all_plans(
    plans_dir: Path, *, reason: str = "", grace_seconds: float = 10.0,
) -> List[ReapReport]:
    """Sweep every plan's ledger. Used on backend startup, so a crash
    that left orphans behind is cleaned up by the next boot.

    A plan whose ledger is missing or empty is skipped silently — the
    common case is that no plan ever started a service.
    """
    plans_dir = Path(plans_dir)
    reports: List[ReapReport] = []
    if not plans_dir.is_dir():
        return reports
    for ledger_file in sorted(plans_dir.glob(f"*/{LEDGER_FILENAME}")):
        plan_dir = ledger_file.parent
        if not read_ledger(plan_dir).get("services"):
            continue
        try:
            reports.append(
                reap_services(
                    plan_dir, reason=reason, grace_seconds=grace_seconds,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad plan must not
            # abort the sweep for the others
            logger.warning(
                "[service_manager] reap failed for %s: %s", plan_dir.name, exc,
            )
    return reports
