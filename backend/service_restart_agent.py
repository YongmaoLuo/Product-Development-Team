"""Generic service-restart sub-agent (2026-09-16 "VP0" service freshness).

Why this module exists
-----------------------
:mod:`service_freshness` decides *which* ports need a restart (no
listener / listener predates the newest source). It deliberately does
NOT perform the restart: discovering how a project starts its service
(uvicorn module path? venv? npm run serve? docker compose?) is not
something a static heuristic gets right across projects, and a wrong
guess that half-starts a service is worse than no guess.

This module mirrors :mod:`binary_rebuild_agent` (2026-09-07) exactly:

  * **Fast path** — when a listener already exists, its own cmdline
    (captured by the prober) is the most reliable known-good start
    command. Kill the old PID, re-exec the identical command under
    ``nohup``, wait for readiness. No LLM, no discovery risk.
  * **Slow path** — a tool-restricted LLM sub-agent
    (:class:`ServiceRestartAgent`, medium tier via the
    ``service_restart`` scene) discovers the start command from the
    repo (grep for the port, uvicorn/gunicorn/flask/serve entries,
    scripts/, e2e harnesses) when there is nothing alive to copy from.
  * **Independent post-check** — the orchestrator re-probes every port
    itself after either path. The agent's self-report is never trusted;
    a claimed success with no live listener is forced to failure.

Safety model (A + C, same as the rebuild agent, plus the kill guards
learned from the 2026-09-07 incident where a restart script murdered
the backend on :8000):

  * **A — hard tool restriction.** Spawned ``claude`` CLI runs with
    ``--allowedTools Bash,Read,Grep,Glob`` — no Edit/Write.
  * **C — prompt restriction + audit.** The system prompt hard-forbids
    touching protected ports (8000/8002/8003 = the backend runtime), forbids
    killing any process whose cmdline does not clearly belong to the
    target service, forbids source modification; every command is
    captured in ``commands_run``.
  * The deterministic fast path additionally refuses to re-exec
    cmdlines that look like port-forward/tunnel processes (ssh/socat/
    kubectl) — restarting a tunnel under nohup would silently change
    its semantics.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from service_freshness import (
    PROTECTED_PORTS,
    PortProbe,
    probe_port,
    wait_for_ready,
)

logger = logging.getLogger(__name__)


# Cmdline prefixes that must never be re-exec'd by the fast path: they
# are tunnels/forwards or orchestrators whose "restart" has semantics a
# blind nohup re-launch cannot preserve. Such ports fall through to the
# LLM discovery path (or stay blocked).
_FAST_PATH_DENY_PREFIXES = ("ssh ", "socat ", "kubectl ", "nc ", "ncat ")


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass
class RestartResult:
    """Structured outcome of a service-restart attempt.

    ``success`` is only ever True when the orchestrator's independent
    re-probe shows every requested port listening and ready — never on
    the agent's word alone. ``agent_used`` distinguishes the cheap
    deterministic re-exec path from the LLM path for cost/audit.
    """

    success: bool
    ports_restarted: List[int] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    diagnostics: str = ""
    post_check_passed: bool = False
    duration_seconds: float = 0.0
    agent_used: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "ports_restarted": list(self.ports_restarted),
            "commands_run": list(self.commands_run),
            "diagnostics": self.diagnostics,
            "post_check_passed": self.post_check_passed,
            "duration_seconds": round(self.duration_seconds, 2),
            "agent_used": self.agent_used,
        }


# ---------------------------------------------------------------------------
# The sub-agent
# ---------------------------------------------------------------------------


class ServiceRestartAgent:
    """Tool-restricted LLM sub-agent that restarts a project's service.

    Spawned through ``coding_tool.query_json`` with
    ``allowed_tools=["Bash","Read","Grep","Glob"]`` and the per-call
    ``scene="service_restart"`` override (→ ``medium`` tier in
    ``provider_routing.yaml``; nothing in this job needs high-tier
    reasoning — it is discovery + a kill/relaunch, and it runs once
    per verification round).
    """

    ALLOWED_TOOLS = ["Bash", "Read", "Grep", "Glob"]
    TIMEOUT_SECONDS = 300

    def __init__(
        self,
        coding_tool: Any,
        plan_id: str,
        vp_id: str,
        registry: Optional[Any] = None,
    ) -> None:
        self.coding_tool = coding_tool
        self.plan_id = plan_id or "default"
        self.vp_id = vp_id or "unknown"
        self.registry = registry

        provider_name = type(coding_tool).__name__ if coding_tool is not None else "None"
        if "Claude" not in provider_name:
            logger.warning(
                "[service_restart] coding_tool provider %r is not the "
                "Claude CLI provider — --allowedTools hard restriction "
                "will be a silent no-op; safety falls back to the system "
                "prompt only.",
                provider_name,
            )

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Constant system prompt: role + hard forbiddens + output contract."""
        protected = ", ".join(str(p) for p in sorted(PROTECTED_PORTS))
        return (
            "You are a service-lifecycle specialist sub-agent spawned by "
            "an automated verification pipeline. Your ONLY job is to "
            "restart the project's own service process(es) so they run "
            "the current code, and confirm the port accepts connections "
            "afterwards.\n\n"
            "You are running with tools restricted to Bash, Read, Grep "
            "and Glob. You do NOT have Edit or Write.\n\n"
            "HARD RULES — violating any of these is a failure:\n"
            f"1. NEVER kill, signal, or otherwise touch any process "
            f"listening on ports {protected} — those belong to the "
            "orchestrator that spawned you. Do not even probe them.\n"
            "2. NEVER kill a process whose command line you cannot "
            "positively identify as belonging to the target service. "
            "If an unknown process holds the target port, REPORT "
            "FAILURE — do not kill it blindly.\n"
            "3. Prefer graceful shutdown: `kill <pid>` (SIGTERM), wait "
            "up to 10s, and only escalate to `kill -9` if the process "
            "survives.\n"
            "4. DO NOT modify, create, or delete any source or config "
            "file (no sed -i, no shell redirection into project files, "
            "no edits to fix a failing start).\n"
            "5. DO NOT install packages (no brew/pip/npm install, no "
            "sudo).\n"
            "6. Start the service detached so it outlives you: "
            "`nohup <start command> > <log file> 2>&1 &` with an "
            "absolute log path under the project's tmp/ dir (create it "
            "if needed).\n"
            "7. After launching, ACTIVELY WAIT for readiness: poll "
            "`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:"
            "<port>/` (or a bare TCP check via python socket) once per "
            "second for up to 30 seconds. Do not declare success before "
            "a poll succeeds, and do not pad the wait with sleeps "
            "beyond that budget.\n\n"
            "You MAY: read any file, grep the repo for how the service "
            "is started, inspect running processes (ps/lsof) EXCEPT on "
            "the protected ports, and run the start command.\n\n"
            "OUTPUT CONTRACT: your final message must be a SINGLE JSON "
            "object and nothing else (no markdown fencing, no prose):\n"
            '{"success": bool, "commands_run": [string, ...], '
            '"diagnostics": string, "health_check": string}\n'
            "Set success=true ONLY if every target port now accepts "
            "connections. Put the readiness poll evidence (e.g. "
            '\"HTTP 200 after 6s\") in health_check. List every shell '
            "command you ran in commands_run."
        )

    def build_user_prompt(
        self,
        project_dir: Path,
        ports: List[int],
        probes: List[PortProbe],
    ) -> str:
        """Per-call context: which ports, what the prober saw, how to
        discover the start command."""
        probe_lines = []
        for probe in probes:
            if probe.port not in ports:
                continue
            if probe.listening:
                cmds = "; ".join(probe.cmdlines) or "(cmdline unknown)"
                probe_lines.append(
                    f"  - port {probe.port}: LISTENING, pids={probe.pids}, "
                    f"cmdline: {cmds}"
                )
            else:
                probe_lines.append(
                    f"  - port {probe.port}: NO LISTENER "
                    f"({probe.error or 'nothing bound'})"
                )
        probe_block = "\n".join(probe_lines) or "  (no probe data)"
        return (
            f"Project directory: {project_dir}\n"
            f"Port(s) that must be live and fresh: {ports}\n"
            f"Prober evidence:\n{probe_block}\n\n"
            "Discovery checklist (work through it):\n"
            "  1. Grep the repo for the port number itself — start "
            "     scripts and config almost always name it "
            "     (`grep -rn \"8080\" --include='*.sh' --include='*.py' "
            "     --include='*.md' .`).\n"
            "  2. Look for the conventional entry points: "
            "     scripts/*.sh, Makefile targets, pyproject [scripts], "
            "     package.json \"start\"/\"serve\", docker-compose.yml, "
            "     README run sections, and e2e test harnesses under "
            "     tests/e2e/ (they usually contain a working "
            "     pkill+nohup+wait-ready recipe you can adapt).\n"
            "  3. Identify the correct interpreter/environment — a "
            "     project-local venv (venv1/, .venv/) whose bin/ holds "
            "     uvicorn/gunicorn beats whatever is on PATH.\n"
            "  4. Restart per the HARD RULES (graceful kill, nohup "
            "     relaunch, 30s readiness poll), then confirm every "
            "     target port accepts connections.\n\n"
            "If the service genuinely cannot be started (e.g. the "
            "project has no server entry point at all), report "
            "success=false with the discovery evidence in diagnostics."
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        project_dir: Path,
        ports: List[int],
        probes: List[PortProbe],
    ) -> RestartResult:
        """Spawn the agent once. Never raises; failure → success=False."""
        start = time.monotonic()
        settings_path: Optional[str] = None
        handle = None

        try:
            from verification_subagent import SettingsJsonBuilder
        except Exception:  # pragma: no cover - defensive
            SettingsJsonBuilder = None

        try:
            # Temp settings.json carrying the 8 security hooks; the
            # "-svcrestart" suffix avoids temp-file collisions with a
            # concurrently-running VP sub-agent (SettingsJsonBuilder
            # keys on (vp_id, plan_id)).
            if SettingsJsonBuilder is not None:
                builder = SettingsJsonBuilder(
                    vp_id=f"{self.vp_id}-svcrestart", plan_id=self.plan_id
                )
                settings_path = builder.build()
                try:
                    self.coding_tool.settings = settings_path
                except Exception:
                    pass

            if self.registry is not None:
                try:
                    handle = self.registry.register_sub_agent(
                        self.plan_id,
                        f"{self.vp_id}-svcrestart",
                        0,
                        scoped_tool=self.coding_tool,
                        timeout_seconds=self.TIMEOUT_SECONDS,
                        max_retries=0,
                    )
                except Exception as exc:
                    logger.warning(
                        "[service_restart] registry registration failed "
                        "(continuing without watchdog): %s", exc,
                    )

            raw = self.coding_tool.query_json(
                self.build_user_prompt(project_dir, ports, probes),
                system_instruction=self.build_system_prompt(),
                timeout=self.TIMEOUT_SECONDS,
                allowed_tools=list(self.ALLOWED_TOOLS),
                scene="service_restart",
            )
            return self._parse_agent_output(raw, start)
        except Exception as exc:
            return RestartResult(
                success=False,
                diagnostics=(
                    f"agent invocation failed: {type(exc).__name__}: {exc}"
                ),
                duration_seconds=time.monotonic() - start,
                agent_used=True,
            )
        finally:
            if SettingsJsonBuilder is not None and settings_path:
                try:
                    SettingsJsonBuilder(
                        vp_id=f"{self.vp_id}-svcrestart", plan_id=self.plan_id
                    ).cleanup()
                except Exception:
                    pass
            if self.registry is not None and handle is not None:
                try:
                    self.registry.unregister(self.plan_id, handle)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    def _parse_agent_output(self, raw: Any, start: float) -> RestartResult:
        """Normalise the agent's JSON into a RestartResult."""
        duration = time.monotonic() - start
        if not isinstance(raw, dict):
            return RestartResult(
                success=False,
                diagnostics=(
                    f"agent returned non-dict payload: {type(raw).__name__}"
                ),
                duration_seconds=duration,
                agent_used=True,
            )

        success = bool(raw.get("success", False))
        commands = raw.get("commands_run") or []
        if not isinstance(commands, list):
            commands = [str(commands)]
        commands = [str(c) for c in commands]
        diagnostics = str(raw.get("diagnostics", ""))
        health_check = str(raw.get("health_check", ""))

        if success and not health_check:
            success = False
            diagnostics = (
                diagnostics
                + " | agent reported success but gave no health_check "
                "evidence; downgraded to failure"
            ).strip()

        return RestartResult(
            success=success,
            commands_run=commands,
            diagnostics=diagnostics,
            duration_seconds=duration,
            agent_used=True,
        )


# ---------------------------------------------------------------------------
# Deterministic fast path: re-exec the captured cmdline
# ---------------------------------------------------------------------------


def _fast_path_restart_port(
    project_dir: Path,
    probe: PortProbe,
    readiness_timeout: int = 30,
) -> Optional[List[str]]:
    """Restart one port by re-exec'ing its captured cmdline.

    Returns the list of shell commands run on success, None when the
    fast path does not apply (no cmdline captured, deny-listed prefix,
    protected port) — the caller then falls through to the LLM agent.
    Never raises.
    """
    if probe.port in PROTECTED_PORTS:
        return None
    if not probe.listening or not probe.pids or not probe.cmdlines:
        return None  # nothing alive to copy the start command from
    cmdline = probe.cmdlines[0].strip()
    if any(cmdline.startswith(p) for p in _FAST_PATH_DENY_PREFIXES):
        logger.info(
            "[service_restart] port %s cmdline %r looks like a "
            "tunnel/forward — skipping fast path",
            probe.port, cmdline,
        )
        return None

    commands: List[str] = []
    try:
        for pid in probe.pids:
            commands.append(f"kill {pid}")
            subprocess.run(
                ["kill", str(pid)], capture_output=True, timeout=5,
            )
        # Give SIGTERM a moment before declaring the port free; the
        # readiness poll below is the real gate anyway.
        time.sleep(1)
        log_dir = Path(project_dir) / "tmp"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"service_freshness_port{probe.port}.log"
        # Re-exec under nohup so the service outlives this process.
        relaunch = f"nohup {cmdline} > {log_file} 2>&1 &"
        commands.append(relaunch)
        with open(log_file, "ab") as log_fp:
            subprocess.Popen(
                ["nohup"] + cmdline.split(),
                cwd=str(project_dir),
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        if wait_for_ready(probe.port, timeout_seconds=readiness_timeout):
            return commands
        logger.warning(
            "[service_restart] fast-path relaunch on port %s did not "
            "become ready in %ss", probe.port, readiness_timeout,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - fast path is best-effort
        logger.warning(
            "[service_restart] fast path raised for port %s: %s",
            probe.port, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Orchestrator: fast-path re-exec → agent discovery → independent re-probe
# ---------------------------------------------------------------------------


def attempt_service_restart(
    project_dir: Path,
    ports_to_restart: List[int],
    probes: List[PortProbe],
    *,
    coding_tool: Any,
    plan_id: str,
    vp_id: str = "service-freshness",
    registry: Optional[Any] = None,
) -> RestartResult:
    """Bring every port in ``ports_to_restart`` to a live, ready state.

    Order per port:
      1. **Fast path** — re-exec the captured listener cmdline (no LLM).
      2. **Slow path** — :class:`ServiceRestartAgent` discovers the
         start command (mainly for ports with no live listener).
    Then ONE independent post-check re-probes every requested port;
    only that decides ``success``.
    """
    start = time.monotonic()
    project_dir = Path(project_dir)
    result = RestartResult(success=False)
    probe_by_port = {p.port: p for p in probes}

    refused: List[int] = []
    remaining: List[int] = []
    for port in ports_to_restart:
        if port in PROTECTED_PORTS:
            # Loud refusal: an explicit request to touch an backend-runtime
            # port is a caller bug, and the result must NOT read as
            # success — silence here is how the 2026-09-07 incident
            # class of bugs hides.
            refused.append(port)
            logger.warning(
                "[service_restart] refusing to restart protected port %s",
                port,
            )
            continue
        probe = probe_by_port.get(port, PortProbe(port=port))
        commands = _fast_path_restart_port(project_dir, probe)
        if commands is not None:
            result.commands_run.extend(commands)
            result.ports_restarted.append(port)
        else:
            remaining.append(port)

    if remaining:
        result.agent_used = True
        agent = ServiceRestartAgent(coding_tool, plan_id, vp_id, registry=registry)
        agent_result = agent.run(project_dir, remaining, probes)
        result.commands_run.extend(agent_result.commands_run)
        if agent_result.success:
            result.ports_restarted.extend(remaining)
        else:
            result.diagnostics = agent_result.diagnostics

    # Independent post-check — the only trustworthy success signal.
    unready = [
        port for port in ports_to_restart
        if port not in PROTECTED_PORTS
        and not (
            probe_port(port).listening
            and wait_for_ready(port, timeout_seconds=5)
        )
    ]
    result.post_check_passed = not unready
    result.success = result.post_check_passed and not refused
    notes: List[str] = []
    if result.diagnostics:
        notes.append(result.diagnostics)
    if unready:
        notes.append(
            f"POST-CHECK FAILED: port(s) {unready} still not ready "
            "after restart attempt"
        )
    if refused:
        notes.append(
            f"REFUSED protected port(s) {refused} — backend-runtime ports "
            "are never restarted"
        )
    result.diagnostics = " | ".join(notes)
    result.duration_seconds = time.monotonic() - start
    return result
