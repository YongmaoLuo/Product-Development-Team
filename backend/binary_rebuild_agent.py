"""Generic binary-rebuild sub-agent (2026-09-07 plan).

Why this module exists
-----------------------
The binary-freshness pre-check (``binary_freshness.check_binary_freshness``)
flags a VP as stale when the compiled artefact is older than its sources.
The *intended* recovery is "detect → auto-rebuild → re-check → PASSED", and
``verification_agent`` already calls ``rebuild_binary`` for exactly that.

But ``rebuild_binary`` is **hardcoded** to ``maturin develop --release`` /
``cargo build --release`` / ``python -m compileall``. On a real machine that
breaks in at least four ways (see the 2026-09-07 audit):

  * the build tool is not on the *server's* PATH (``maturin`` lives in the
    project's own ``venv1/bin``, not the backend venv) → ``OSError``
  * ``maturin develop`` installs into whichever venv is *active* for the
    backend process, not the project's venv
  * the workspace has two ``Cargo.toml`` files and the inferred package root
    drifts depending on which one ``rglob`` hits first
  * any non-Rust/Python build system (cmake / go / gradle / npm-native …)
    is simply not handled at all

Rather than keep stacking ``if kind == ...`` branches, this module delegates
the rebuild to a **tool-restricted LLM sub-agent** that *reads* the project
(build manifest, venv layout, available toolchains) and picks the right
command. That is what makes the mechanism generic across projects.

Safety model (A + C, user-approved):
  * **A — hard tool restriction.** The spawned ``claude`` CLI is invoked with
    ``--allowedTools Bash,Read,Grep,Glob`` so the agent *cannot* Edit/Write.
  * **C — prompt restriction + audit.** The system prompt explicitly forbids
    source modification and every command the agent runs is captured in
    ``commands_run``.

Trust model: the agent's self-report is **never** trusted. After the agent
returns, :func:`attempt_intelligent_rebuild` independently re-runs
``check_binary_freshness`` and only that mtime comparison decides success.
If the agent claims success but the artefact is still stale, the result is
forced to ``success=False`` and the diagnostics are annotated with
``AGENT SELF-REPORT MISMATCH``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from binary_freshness import (
    FreshnessReport,
    check_binary_freshness,
    rebuild_binary,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass
class RebuildResult:
    """Structured outcome of a rebuild attempt (fast-path or agent).

    ``success`` is only ever True when the orchestrator's independent
    :func:`check_binary_freshness` re-check passes — never on the agent's
    word alone. ``agent_used`` distinguishes the cheap deterministic path
    from the LLM path for cost/audit reporting.
    """

    success: bool
    rebuilt_binary_path: Optional[str] = None
    commands_run: List[str] = field(default_factory=list)
    diagnostics: str = ""
    post_check_passed: bool = False
    duration_seconds: float = 0.0
    agent_used: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "rebuilt_binary_path": self.rebuilt_binary_path,
            "commands_run": list(self.commands_run),
            "diagnostics": self.diagnostics,
            "post_check_passed": self.post_check_passed,
            "duration_seconds": round(self.duration_seconds, 2),
            "agent_used": self.agent_used,
        }


# ---------------------------------------------------------------------------
# The sub-agent
# ---------------------------------------------------------------------------


class BinaryRebuildAgent:
    """Tool-restricted LLM sub-agent that rebuilds a stale binary.

    The agent is spawned through ``coding_tool.query_json`` with
    ``allowed_tools=["Bash","Read","Grep","Glob"]`` so it can inspect the
    project and run build commands but **cannot** Edit/Write any file.
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

        # Risk guard: --allowedTools is only honoured by the Claude-CLI
        # provider (``coding_tool.py:1610``). The other ``query_json``
        # definitions (non-Claude providers) do not spawn the CLI and would
        # silently drop the restriction, leaving only the prompt-level rule.
        provider_name = type(coding_tool).__name__ if coding_tool is not None else "None"
        if "Claude" not in provider_name:
            logger.warning(
                "[binary_rebuild] coding_tool provider %r is not the Claude "
                "CLI provider — --allowedTools hard restriction will be a "
                "silent no-op; source-modification safety falls back to the "
                "system prompt only.",
                provider_name,
            )

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Constant system prompt: role + hard forbiddens + output contract."""
        return (
            "You are a build-system specialist sub-agent spawned by an "
            "automated verification pipeline. Your ONLY job is to rebuild a "
            "stale compiled artefact so that it becomes newer than its "
            "sources.\n\n"
            "You are running with tools restricted to Bash, Read, Grep and "
            "Glob. You do NOT have Edit or Write.\n\n"
            "HARD RULES — violating any of these is a failure:\n"
            "1. DO NOT modify, create, or delete any source code file "
            "(.rs, .py, .cpp, .cc, .go, .ts, .java, .swift, etc.).\n"
            "2. DO NOT edit build configuration/manifest files "
            "(Cargo.toml, pyproject.toml, CMakeLists.txt, go.mod, "
            "package.json, Makefile, build.gradle, setup.py, etc.).\n"
            "3. DO NOT use `sed -i`, `echo ... >`, `printf ... >`, `tee`, "
            "or any shell redirection to overwrite a project file.\n"
            "4. DO NOT install global system packages (no `brew install`, "
            "`apt`, `sudo`, or `pip install` outside the project venv).\n"
            "5. DO NOT access the network except what the project's own "
            "build command performs (e.g. cargo fetching crates).\n\n"
            "You MAY: read any file, run the project's build command, "
            "locate the project-local virtualenv, and re-run the build "
            "with the correct working directory and environment.\n\n"
            "TIME BUDGET & PROGRESS REPORTING:\n"
            "The verification watchdog force-terminates any sub-agent "
            "whose log has no fresh writes for >~15 min. Build commands "
            "(`cargo build --release`, `maturin develop --release`, "
            "`cmake --build`, `go build`, etc.) easily exceed 5 min on "
            "cold caches and must therefore emit observable progress "
            "while they run:\n"
            "  - Always run the build with a streaming tee to a stable "
            "    file, e.g.\n"
            "      cargo build --release 2>&1 | tee /tmp/rebuild_<id>.log\n"
            "  - Run it in the background; in a separate `while kill -0 "
            "$PID` loop, read NEW lines from the tee'd file every 60-120 "
            "s. A working pattern:\n"
            "      cargo build --release 2>&1 | tee /tmp/rebuild.log &\n"
            "      PID=$!\n"
            "      while kill -0 $PID 2>/dev/null; do\n"
            "        tail -n 1 /tmp/rebuild.log\n"
            "        sleep 90\n"
            "      done\n"
            "  - For `maturin develop --release`, also use `--verbose` "
            "and tee.\n"
            "Anti-cheat: every progress event must carry real data "
            "from the tee'd log (current crate being compiled, last "
            "cargo line, byte count, etc.). NEVER pad the log with "
            "empty heartbeats — that is CHEATING and will be detected.\n"
            "If the build appears genuinely stalled, REPORT FAILURE "
            "honestly instead of padding the log to keep the watchdog "
            "alive.\n\n"
            "OUTPUT CONTRACT: your final message must be a SINGLE JSON "
            "object and nothing else (no markdown fencing, no prose):\n"
            '{"success": bool, "binary_path": string|null, '
            '"commands_run": [string, ...], "diagnostics": string}\n'
            "Set success=true ONLY if you actually ran a build and the "
            "artefact now exists and is newer than the newest source file. "
            "List every shell command you ran in commands_run."
        )

    def build_user_prompt(
        self, project_dir: Path, freshness: FreshnessReport
    ) -> str:
        """Per-call context: what is stale and how to discover the build."""
        prev_cmd = freshness.rebuild_command or "(no previous guess)"
        binary_path = freshness.binary_path or "(no artefact on disk yet)"
        return (
            f"Project directory: {project_dir}\n"
            f"Detected project kind: {freshness.kind}\n"
            f"Why it is stale: {freshness.detail}\n"
            f"Stale artefact path: {binary_path}\n"
            f"Evidence: {freshness.evidence}\n"
            f"A previous hardcoded guess at the rebuild command was: "
            f"`{prev_cmd}` — do NOT assume it is correct; it likely failed "
            f"because the build tool was not on PATH or the wrong venv was "
            f"targeted.\n\n"
            "Discovery checklist (work through it):\n"
            "  1. Glob for the build manifest: Cargo.toml, pyproject.toml, "
            "     CMakeLists.txt, go.mod, package.json, Makefile, "
            "     build.gradle, etc. Read it to learn the build system and "
            "     the package/crate name.\n"
            "  2. Find the project-local virtualenv (venv1/, .venv/, env/, "
            "     venv/). Its bin/ dir holds tools like maturin.\n"
            "  3. Check which build tools exist: `which maturin cargo "
            "     cmake go npm gradle make` and also look inside the venv "
            "     bin/ dir (the tool may be there but not on PATH — if so, "
            "     invoke it by absolute path).\n"
            "  4. Choose the correct build command and run it with the "
            "     correct cwd and the project venv's bin/ on PATH (or "
            "     activate the venv first).\n"
            "  5. After building, confirm the artefact exists and its mtime "
            "     is newer than the newest source file (e.g. `ls -la` the "
            "     artefact and `find . -name '*.rs' -newer <artefact>`).\n\n"
            "Remember: rebuild only. Do not fix or refactor source code, "
            "even if the build fails because of a source error — in that "
            "case report success=false with the compiler error in "
            "diagnostics."
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, project_dir: Path, freshness: FreshnessReport) -> RebuildResult:
        """Spawn the agent once. Never raises; failure → success=False."""
        start = time.monotonic()
        settings_path: Optional[str] = None
        handle = None

        # Lazy imports so this module stays importable without pulling the
        # heavier sub-agent modules (and so tests can patch freely).
        try:
            from verification_subagent import SettingsJsonBuilder
        except Exception:  # pragma: no cover - defensive
            SettingsJsonBuilder = None

        try:
            # Build the temp settings.json carrying the 8 security hooks.
            # The vp_id gets a "-rebuild" suffix so its temp file does not
            # collide with a concurrently-running VP sub-agent that shares
            # the same bare vp_id (SettingsJsonBuilder keys on (vp_id,
            # plan_id)).
            if SettingsJsonBuilder is not None:
                builder = SettingsJsonBuilder(
                    vp_id=f"{self.vp_id}-rebuild", plan_id=self.plan_id
                )
                settings_path = builder.build()
                # Point the coding tool at the settings file so the CLI
                # picks up the security hooks.
                try:
                    self.coding_tool.settings = settings_path
                except Exception:
                    pass

            # Register with the heartbeat watchdog so a stalled rebuild is
            # detectable. attempt=0 — rebuild is a single shot.
            if self.registry is not None:
                try:
                    handle = self.registry.register_sub_agent(
                        self.plan_id,
                        f"{self.vp_id}-rebuild",
                        0,
                        scoped_tool=self.coding_tool,
                        timeout_seconds=self.TIMEOUT_SECONDS,
                        max_retries=0,
                    )
                except Exception as exc:
                    logger.warning(
                        "[binary_rebuild] registry registration failed "
                        "(continuing without watchdog): %s", exc,
                    )

            raw = self.coding_tool.query_json(
                self.build_user_prompt(project_dir, freshness),
                system_instruction=self.build_system_prompt(),
                timeout=self.TIMEOUT_SECONDS,
                allowed_tools=list(self.ALLOWED_TOOLS),
            )
            result = self._parse_agent_output(raw, start)
            return result
        except Exception as exc:
            return RebuildResult(
                success=False,
                diagnostics=f"agent invocation failed: {type(exc).__name__}: {exc}",
                duration_seconds=time.monotonic() - start,
                agent_used=True,
            )
        finally:
            if SettingsJsonBuilder is not None and settings_path:
                try:
                    SettingsJsonBuilder(
                        vp_id=f"{self.vp_id}-rebuild", plan_id=self.plan_id
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

    def _parse_agent_output(self, raw: Any, start: float) -> RebuildResult:
        """Normalise the agent's JSON into a RebuildResult."""
        duration = time.monotonic() - start
        if not isinstance(raw, dict):
            return RebuildResult(
                success=False,
                diagnostics=f"agent returned non-dict payload: {type(raw).__name__}",
                duration_seconds=duration,
                agent_used=True,
            )

        success = bool(raw.get("success", False))
        binary_path = raw.get("binary_path") or None
        commands = raw.get("commands_run") or []
        if not isinstance(commands, list):
            commands = [str(commands)]
        commands = [str(c) for c in commands]
        diagnostics = str(raw.get("diagnostics", ""))

        # Guard: agent claiming success without producing an artefact path
        # is not credible — downgrade.
        if success and not binary_path:
            success = False
            diagnostics = (
                diagnostics
                + " | agent reported success but gave no binary_path; "
                "downgraded to failure"
            ).strip()

        if success and not commands:
            logger.warning(
                "[binary_rebuild] agent reported success with empty "
                "commands_run — suspicious; post-check will adjudicate."
            )

        return RebuildResult(
            success=success,
            rebuilt_binary_path=binary_path,
            commands_run=commands,
            diagnostics=diagnostics,
            duration_seconds=duration,
            agent_used=True,
        )


# ---------------------------------------------------------------------------
# Orchestrator: dumb fast-path → agent slow-path → independent re-check
# ---------------------------------------------------------------------------


def attempt_intelligent_rebuild(
    project_dir: Path,
    freshness: FreshnessReport,
    *,
    coding_tool: Any,
    plan_id: str,
    vp_id: str,
    registry: Optional[Any] = None,
    fast_path_timeout: int = 5,
) -> RebuildResult:
    """Try to clear a stale-binary state. Never raises.

    Order:
      1. **Fast path** — the existing deterministic ``rebuild_binary``
         (cheap, no LLM). If it succeeds, return immediately.
      2. **Slow path** — spawn :class:`BinaryRebuildAgent`.
      3. **Independent verification** — re-run ``check_binary_freshness``.
         Only this decides ``success``; the agent's self-report is advisory.
    """
    project_dir = Path(project_dir)

    # 1. Fast path.
    try:
        if rebuild_binary(project_dir, freshness.kind, timeout=fast_path_timeout):
            post = check_binary_freshness(project_dir)
            return RebuildResult(
                success=post.status == "PASSED",
                rebuilt_binary_path=post.binary_path,
                commands_run=[freshness.rebuild_command or "rebuild_binary(fast-path)"],
                diagnostics="fast-path rebuild_binary succeeded",
                post_check_passed=post.status == "PASSED",
                agent_used=False,
            )
    except Exception as exc:
        logger.warning(
            "[binary_rebuild] fast-path rebuild_binary raised (%s); "
            "falling through to agent", exc,
        )

    # 2. Slow path — the LLM agent.
    agent = BinaryRebuildAgent(coding_tool, plan_id, vp_id, registry=registry)
    result = agent.run(project_dir, freshness)

    # 3. Independent verification — the only trustworthy success signal.
    try:
        post = check_binary_freshness(project_dir)
        result.post_check_passed = post.status == "PASSED"
        if post.binary_path:
            result.rebuilt_binary_path = post.binary_path
    except Exception as exc:
        result.post_check_passed = False
        result.diagnostics += f" | post-check errored: {exc}"

    if result.success and not result.post_check_passed:
        result.diagnostics += (
            " | AGENT SELF-REPORT MISMATCH: agent claimed success but the "
            "artefact is still stale after rebuild"
        )
    result.success = result.success and result.post_check_passed
    return result
