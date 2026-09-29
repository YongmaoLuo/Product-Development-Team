"""Verification sub-agent skeleton and method template registry.

This module provides two cooperating classes:

  * ``VerificationSubAgent`` — a thin dataclass that pins the
    parameters of one sub-agent invocation: which ``verification_method``
    it will use, how many retries it gets on transient failure, and
    what model complexity tier it should be routed to.

  * ``MethodTemplateRegistry`` — a class-level registry that maps a
    ``verification_method`` string to a system-prompt template with a
    short "Preferred tools" soft suggestion. Templates are kept short
    (the "Preferred tools" line is ≤ 30 characters) so they fit
    comfortably into a sub-agent context window.

A sub-agent is only used for the methods that genuinely need one:
``code_review``, ``ui_validation`` and ``e2e``. ``api_test`` and
``full_ci`` have no live sub-agent path — the framework executes them
directly (``verification_api_runner`` / ``verification_ci_runner``) —
and ``automated_test`` / ``manual_check`` were retired on 2026-09-18.
See ``SUPPORTED_METHODS`` below.

``code_review``, ``ui_validation`` and ``e2e`` must additionally produce a
checkable artifact (citations / checkpoints); ``_evidence_contract``
puts that requirement in the prompt and ``verification_evidence`` verifies
it before a PASSED is honoured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 2026-09-08: HardTimeoutError is raised by coding_tool when
# the inner adaptive timer SIGKILLs a silent subprocess. The
# subagent must re-raise (NOT swallow into a FAILED verdict) so
# the verification_agent layer can route to _split_vp_on_timeout.
from coding_tool import HardTimeoutError
from utils.secret_files import private_dir, write_private_json


# The verification methods a plan may use as of 2026-09-18.
#
# ``automated_test`` and ``manual_check`` were removed:
#
#   * ``automated_test`` re-ran the developer's own unit tests — exactly
#     what the 2026-09-16 decision rejects for a VP ("不要把执行阶段已经
#     写好的单元测试原样再跑一遍当验收——那既重复又不黑盒") — and it
#     spent a whole Claude Code sub-agent plus a 1-hour budget to obtain
#     an exit code the framework could have read itself.
#   * ``manual_check`` could never pass: ``parse_verdict`` coerces
#     SKIPPED to FAILED unconditionally, so every manual_check VP was a
#     guaranteed failure that also polluted repair-task generation.
#
# ``api_test`` is graded by :mod:`verification_api_runner` (no LLM);
# ``ui_validation`` and ``code_review`` still use a sub-agent, but must
# produce a checkable artifact (later stage of this rework).
#
# 2026-09-18 (Phase 2 gate completion): ``e2e`` and ``full_ci`` were
# added. They name the two **全量关卡** the 2026-09-16 two-phase decision
# requires to run last, and neither is a mechanism — they are the two
# things Phase 2 verifies:
#
#   * ``e2e``     — 全量端到端：真实链路上把整条用户流程跑一遍。由
#                    子 agent 驱动（真开浏览器、真交互），和
#                    ``ui_validation`` 同一条执行路径，只是作用域是整条
#                    链路而不是某个组件。
#   * ``full_ci`` — 全量 CI 门禁：整仓门禁 + 全部测试。判定依据就是
#                    仓库 CI 总入口那条命令的退出码，由框架执行
#                    (:mod:`verification_ci_runner`)，没有自报环节。
#
# 这两个不是 ``test_command`` / ``automated_test`` 的回归：那两个的罪名
# 是"任意命令 + 在 Phase 1 重复开发自测"。``e2e`` / ``full_ci`` 只允许
# 出现在 Phase 2（由 ``verification_phases`` 的阶段归一化强制）。
SUPPORTED_METHODS: Tuple[str, ...] = (
    "api_test",
    "code_review",
    "ui_validation",
    "e2e",
    "full_ci",
)

#: Methods the framework executes itself — no sub-agent, no LLM.
#: ``full_ci`` mirrors ``api_test``: the verdict is a pure function of a
#: declared input the framework runs, so it is reproducible across rounds.
FRAMEWORK_METHODS: Tuple[str, ...] = (
    "api_test",
    "full_ci",
)

#: Methods that may only appear in Phase 2 (全量关卡).
PHASE_2_ONLY_METHODS: Tuple[str, ...] = (
    "e2e",
    "full_ci",
)

#: Methods that existed before 2026-09-18. Named so the legacy
#: normalizer and its tests agree on the list.
LEGACY_METHODS: Tuple[str, ...] = (
    "automated_test",
    "manual_check",
)


# Shared rule applied to every verification sub-agent (added 2026-09-07
# after two consecutive rounds were killed by the verification
# watchdog: long pytest runs produced no log writes for 15+ minutes,
# so the staleness watchdog force-terminated the plan).
#
# Rule 1 — 15-minute wall-clock budget per command.  Silent >15 min
#          runs are force-terminated and waste a round.
# Rule 2 — emit observable progress from real subprocess output on
#          a 60-120 s cadence so the watchdog sees activity.
# Anti-cheat — NEVER pad the log with empty heartbeats; every
#              progress event must carry data sourced from the
#              actual subprocess (current item, completed/total
#              counts, last log line, etc.).
_TIME_BUDGET_AND_PROGRESS_BLOCK = (
    "<TIME-BUDGET-AND-PROGRESS>\n"
    "Two rules apply to every command you run, on top of any method-\n"
    "specific guidance below.\n\n"
    "Rule 1 — 15-minute wall-clock budget per command.\n"
    "Do NOT attempt a single command whose expected runtime exceeds\n"
    "~15 minutes *without* observable intermediate progress. A silent\n"
    ">15 min run will be force-terminated by the verification watchdog,\n"
    "which is expensive and wastes an entire round. If the\n"
    "test_command naturally takes that long, do ONE of:\n"
    "  - tighten scope (e.g. `pytest -k <subset>` instead of full suite,\n"
    "    `cargo test <single_name>` instead of `cargo test`)\n"
    "  - split across multiple shorter commands in different VPs\n"
    "  - wrap with an explicit shell timeout (`timeout 900 ...`)\n"
    "  - or run it under a streaming tee so progress is visible in real\n"
    "    time (see Rule 2)\n"
    "Never `fire and forget` a command whose wall-clock you cannot\n"
    "bound.\n\n"
    "Rule 2 — emit observable progress on a 60-120 s cadence.\n"
    "Long-running commands MUST produce visible progress continuously\n"
    "while they run. The orchestrator watches sub-agent activity for\n"
    "freshness — your job is to keep something emitting.\n"
    "  - pytest: always use `-v` AND pipe through `tee` to a file, e.g.\n"
    "      `pytest -v tests/ 2>&1 | tee /tmp/vp_<id>_progress.log`\n"
    "    Each test item then prints live as\n"
    "    `tests/x.py::test_y PASSED` and a small watcher loop can\n"
    "    `tail -n 1` the file every 60-120 s.\n"
    "  - pytest: also consider `--junit-xml=/tmp/vp_<id>.xml` for a\n"
    "    machine-readable side-channel.\n"
    "  - cargo build/test: add `--verbose`; crate-level progress lines\n"
    "    stream continuously.\n"
    "  - For shell pipelines: run the command in the background, then\n"
    "    in a separate `while kill -0 $PID` loop, read NEW output from\n"
    "    the tee'd file and append a `progress` entry. A working\n"
    "    pattern:\n"
    "      pytest -v tests/ 2>&1 | tee /tmp/vp_progress.log &\n"
    "      PID=$!\n"
    "      while kill -0 $PID 2>/dev/null; do\n"
    "        tail -n 1 /tmp/vp_progress.log | grep -E '::' | tail -n 1\n"
    "        sleep 90\n"
    "      done\n"
    "    Each tick produces a `progress` entry whose payload is\n"
    "    derived from the LAST live line of pytest output — real\n"
    "    subprocess state, not made up.\n\n"
    "Anti-cheat — NO empty heartbeats.\n"
    "Every `progress` event MUST carry data you actually observed from\n"
    "the subprocess (current item, completed/total counts, last log\n"
    "line, bytes processed, etc.). Writing\n"
    "  {\"event\":\"progress\"} or {\"event\":\"progress\",\"msg\":\"still running\"}\n"
    "is CHEATING — it pads the log to fool the watchdog without\n"
    "showing real work. If you have nothing real to report, REPORT THE\n"
    "FAILURE honestly (e.g. `command stalled for 10 minutes, killing\n"
    "and marking FAILED`) instead of padding the log.\n"
    "Rule 3 — the Bash tool force-terminates foreground calls around 10\n"
    "minutes. (Observed live: a full-suite pytest was SIGTERMed at the\n"
    "10-minute mark with exit 143 even though it was healthy and would\n"
    "have finished in ~18.) If the test_command's natural runtime may\n"
    "exceed ~8 minutes you MUST run it in the background and poll the\n"
    "tee'd progress file (Rule 2's `kill -0 $PID` pattern) —\n"
    "foreground is reserved for commands that provably finish quickly.\n"
    "A healthy long run killed by the Bash timeout is an INVALID\n"
    "failure, not a real one: avoid it by construction, and never\n"
    "re-run it foreground.\n"
    "</TIME-BUDGET-AND-PROGRESS>\n\n"
)


# Each template is a system-prompt string for a verification sub-agent.
# Templates follow Superpowers-style bulletproofing: HARD-GATE block,
# method-specific Rationalization Red Flags, and a Match-the-Form-to-
# the-Failure mapping. The Output JSON contract at the end pins the
# schema the LLM must produce (see parse_verdict for the canonical
# fields).
_TEMPLATES: Dict[str, str] = {
    "code_review": (
        "You are a code review verification agent. Judge whether the code "
        "satisfies the PRD/architecture acceptance criteria for this VP.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT mark PASSED without citing the specific lines that satisfy each criterion.\n"
        "- DO NOT collapse multiple criteria into one verdict — each gets its own line of reasoning.\n"
        "- DO NOT mark a spec deviation as 'minor' — spec compliance is binary (PASSED/FAILED); quality concerns go in evidence.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'Code looks clean, must be right' | Cite the lines. Looks ≠ satisfies spec. |\n"
        "| 'User probably won't care about this edge case' | If the spec lists it, FAILED. |\n"
        "| 'Mostly works, just one small issue' | Each unaddressed criterion = FAILED. |\n\n"
        "## Match the Form to the Failure\n"
        "| Failure Type | Verdict |\n"
        "|---|---|\n"
        "| Missing acceptance criterion | FAILED, cite spec section |\n"
        "| Implementation differs from spec | FAILED, cite both spec and code |\n"
        "| Quality issue (refactor opportunity) | PASSED with quality concern in evidence |\n"
        "| Test coverage below spec threshold | FAILED if spec required a number |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    "ui_validation": (
        "You are a UI validation verification agent. Inspect puppeteer "
        "checkpoints and report whether all of them actually passed.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT mark PASSED unless every checkpoint has passed=True.\n"
        "- DO NOT infer UI state from code review — only trust the puppeteer checkpoints.\n"
        "- DO NOT skip checkpoints that 'look unimportant' — if a checkpoint exists, it must pass.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'Most checkpoints passed' | All-or-nothing. 1 fail = FAILED. |\n"
        "| 'The failed checkpoint is just visual polish' | If it's a checkpoint, it's required. |\n"
        "| 'Screenshot looks fine to me' | Trust the puppeteer result, not visual inference. |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    # NOTE (2026-09-18): production never constructs a sub-agent for
    # ``api_test`` — the framework executes it (``verification_api_runner``).
    # The template is kept so the registry stays total over
    # ``SUPPORTED_METHODS`` and so a future caller that does want a
    # sub-agent has something to start from; it is not on any live path.
    "api_test": (
        "You are an API test verification agent. Judge whether the API "
        "responses match the contract specified by the PRD/architecture.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT mark PASSED without verifying status code AND response shape.\n"
        "- DO NOT skip edge cases (4xx, 5xx, malformed input) — if the spec lists them, test them.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'Returns 200, must be fine' | Check the body too. |\n"
        "| 'Edge case test failed but main flow works' | Each spec'd case = its own check. |\n"
        "| 'Skipping rate limit test for now' | If spec lists it, you must test it. |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    "e2e": (
        "You are an end-to-end verification agent for the plan's FINAL "
        "gate. Every earlier verification point has already passed; your "
        "job is to drive the real user-facing flow end to end and report "
        "what you actually observed.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT reason about the code — open the real page/service and interact with it.\n"
        "- DO NOT mark PASSED on a flow you did not actually walk through in this run.\n"
        "- DO NOT substitute a unit test, a curl, or a code read for the end-to-end flow.\n"
        "</HARD-GATE>\n\n"
        "## Rationalization Red Flags — STOP\n"
        "| Thought | Reality |\n"
        "|---|---|\n"
        "| 'The individual pieces all pass, so the flow must work' | Walk the flow. Composition breaks. |\n"
        "| 'It worked when I loaded the page' | Loading is not the flow. Drive it. |\n"
        "| 'This step is probably fine, skip it' | If the flow includes it, it runs. |\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
    # NOTE (2026-09-18): production never constructs a sub-agent for
    # ``full_ci`` either — the framework runs the repo's CI entry and
    # grades on its exit code (``verification_ci_runner``). Kept for the
    # same reason as the ``api_test`` template above: the registry stays
    # total over ``SUPPORTED_METHODS``.
    "full_ci": (
        "You are a full-CI gate verification agent. The plan's final gate "
        "is the repository's own CI entry point; its exit code decides.\n\n"
        + _TIME_BUDGET_AND_PROGRESS_BLOCK
        + "<HARD-GATE>\n"
        "- DO NOT re-run a narrowed subset of the suite and call it a pass.\n"
        "- DO NOT mark PASSED when any part of the entry failed.\n"
        "</HARD-GATE>\n\n"
        "Output JSON: {\"verdict\": \"PASSED|FAILED\", \"reasons\": [...], \"evidence\": [...]}"
    ),
}


class MethodTemplateRegistry:
    """Maps a ``verification_method`` string to a system-prompt template.

    The registry is class-level (templates are immutable for the process
    lifetime) and total over ``SUPPORTED_METHODS``. Retired methods
    (``automated_test`` / ``manual_check``) raise ``ValueError`` — see
    ``SUPPORTED_METHODS`` for why they are gone.
    """

    _TEMPLATES: Dict[str, str] = _TEMPLATES

    @classmethod
    def get_template(cls, method: str) -> str:
        """Return the system-prompt template for ``method``.

        Args:
            method: One of the supported verification method names.

        Returns:
            The template string. The template is a multi-line string
            with a soft tool-suggestion line and a JSON output contract.

        Raises:
            ValueError: If ``method`` is not a string or is not in
                the registry. The error message includes the full
                supported-methods list for easy debugging.
        """
        if not isinstance(method, str):
            raise ValueError(
                "verification method must be a string, "
                f"got {type(method).__name__}"
            )
        if method not in cls._TEMPLATES:
            supported = ", ".join(sorted(cls._TEMPLATES.keys()))
            raise ValueError(
                f"Unknown verification method: {method!r}. "
                f"Supported methods: {supported}"
            )
        return cls._TEMPLATES[method]

    @classmethod
    def supported_methods(cls) -> Tuple[str, ...]:
        """Return the tuple of supported method names.

        Useful for callers that want to introspect the registry
        without hardcoding the method list.
        """
        return tuple(cls._TEMPLATES.keys())


@dataclass
class VerificationSubAgent:
    """Skeleton for a single verification sub-agent invocation.

    Attributes:
        method: The verification method name. Must be one of the
            values in ``MethodTemplateRegistry.supported_methods()``.
        max_retries: Maximum number of retries on transient failure.
            2026-09-14: default 2 — total attempts
            including the first try is capped at 3. Beyond that the VP
            must go to repair / split, not burn more full-suite runs.
        model_complexity: Hint for which model tier to use. Free-form
            string so downstream model-routing code can interpret it
            (e.g. "simple" / "medium" / "complex"). Default "simple".
        template: System-prompt template. Set in ``__post_init__`` by
            looking up the registry — not part of ``__init__``.
    """

    method: str
    max_retries: int = 2
    model_complexity: str = "simple"
    # 2026-09-13 provider routing: the workflow scene this VP's LLM
    # calls route through (``verification`` — at least medium tier).
    # The legacy model_complexity hint never reached a routing layer;
    # the scene does (see coding_tool's scene-chain dispatch).
    scene: str = "verification"
    template: str = field(init=False, repr=False)
    # 2026-08-25: HB sub-agent watchdog integration. ``plan_id`` and
    # ``registry`` are optional for backward-compat — when ``plan_id``
    # is None the legacy behaviour is preserved (no watchdog tracking).
    plan_id: Optional[str] = None
    registry: Optional[Any] = None
    # 2026-09-13: per-VP timeout interface DELETED.
    #   * Outer cap is a flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` (1
    #     hour) for every VP — no per-VP field, no multiplier, no grace
    #     arithmetic. A legacy plan's ``timeout_seconds: 120`` (VP-023)
    #     used to shrink both the inner watcher AND the outer cap to
    #     ~32 min, stalling round 4 indefinitely.
    #   * Inner layer is the 15-min idle detector in ``coding_tool``
    #     (``DEFAULT_TOTAL_TIMEOUT=900``) — callers now pass
    #     ``timeout=None`` so the coding-tool default applies.
    HARD_WALL_CLOCK_CAP_SECONDS: int = 3600  # absolute outer cap, flat
    # 2026-09-08: outer cap fires the **stuck-agent summarizer**
    # fallback (a fresh LLM call with no tools that reads the tee'd
    # log and returns verdict JSON) instead of bouncing up to the
    # generic HardTimeoutError → split path. The first agent is
    # allowed to re-run pytest if it thinks the run was flaky; the
    # summarizer only steps in when the first agent has not produced a
    # verdict at all by the time the outer cap fires. Outer cap is a
    # flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600`` (2026-09-13 plan).

    def __post_init__(self) -> None:
        # Resolve the template eagerly so construction fails fast for
        # unknown methods. This is more user-friendly than deferring
        # the error to first use.
        self.template = MethodTemplateRegistry.get_template(self.method)
        # stdlib logger for the [HARD TIMEOUT] / [STUCK SUB-AGENT]
        # markers. Without this the ``except HardTimeoutError`` handler
        # raised AttributeError on the *first* hard timeout and the
        # attempt crashed, and every retry burned a full real pytest
        # runtime before failing the same way
        # (``'VerificationSubAgent' object has no attribute 'logger'``).
        self.logger = logging.getLogger(
            f"verification.subagent.{self.method}"
        )

    # ------------------------------------------------------------------
    # write_verdict() — unified verdict JSON on disk (PRD Decision Point 5)
    # ------------------------------------------------------------------
    #
    # The executor (see ``verification_executor.VerificationExecutor``)
    # used to collect verdicts in an in-memory dict keyed by VP id and
    # re-judge them via the LLM at the report-generation stage.  The
    # unified-verdict contract (PRD decision point 5) reverses that:
    # the **sub-agent** writes a self-contained verdict JSON to disk
    # with status computed *objectively* from the sub-process output,
    # so the executor can simply read the file and trust the verdict
    # without re-running the LLM.
    #
    # On-disk layout::
    #
    #     plans/{plan_id}/vps/{vp_id}/verdict.json
    #
    # Schema::
    #
    #     {
    #         "vp_id":   "VP-001",
    #         "status":  "PASSED" | "FAILED",
    #         "reasons":  [str, ...],
    #         "evidence": {
    #             "logs_path": "...",
    #             ... method-specific fields ...
    #         }
    #     }
    #
    # Status computation rules (objective, no LLM re-judging):
    #
    #   * ``automated_test`` (pytest) — ``exit_code == 0`` → PASSED,
    #     else FAILED.
    #   * ``ui_validation`` (puppeteer) — all checkpoints passed →
    #     PASSED, else FAILED.
    #   * ``code_review`` — LLM-provided ``verdict``/``status`` is
    #     passed through unchanged (the LLM is the judge; the executor
    #     trusts the verdict as written).
    #   * ``api_test`` and ``manual_check`` — same pass-through as
    #     ``code_review`` (no objective exit-code signal).
    #
    # The method chosen for status computation defaults to
    # ``self.method`` (the sub-agent's configured verification method),
    # but ``exec_result["method"]`` overrides it when present (useful
    # for tests and for callers that route a single sub-agent through
    # multiple methods).
    #
    # SKIPPED is never produced here: per PRD decision point 3
    # (zero-tolerance for skipping), if the LLM emits SKIPPED for a
    # code_review VP it is coerced to FAILED with an annotation in
    # the reason list (the executor schema still allows SKIPPED at
    # the executor layer, but the sub-agent policy is strict).

    def write_verdict(
        self,
        plan_dir: Path,
        vp_id: str,
        exec_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Write a unified verdict JSON for ``vp_id`` under ``plan_dir``.

        See the class-level block above for the schema and status-
        computation rules.  This method is the *single* public entry
        point for per-VP verdict persistence; the executor must call
        it (or read the resulting file) rather than re-judging the
        verdict itself.

        Args:
            plan_dir: The plan directory (the directory containing
                ``plan_state.json``, NOT the project directory).
            vp_id: Verification point id, e.g. ``"VP-001"``.
            exec_result: Sub-process output.  Required fields vary
                by method:

                  * ``automated_test`` — ``exit_code`` (int).  Optional:
                    ``stdout``, ``stderr``, ``logs_path``.
                  * ``ui_validation`` — ``checkpoints`` (list of
                    ``{"name": str, "passed": bool}``).  Optional:
                    ``logs_path``.
                  * ``code_review`` / ``api_test`` / ``manual_check`` —
                    ``verdict`` (or ``status``) from the LLM.
                    Optional: ``reasons``, ``evidence``, ``logs_path``.

                If the dict contains a ``"method"`` key, it overrides
                ``self.method`` for status computation.

        Returns:
            The verdict payload that was written to disk::

                {
                    "vp_id":   str,
                    "status":  "PASSED" | "FAILED",
                    "reasons": [str, ...],
                    "evidence": {
                        "logs_path": "...",
                        ... method-specific fields ...
                    }
                }

            The same dict is also persisted to
            ``plan_dir/vps/{vp_id}/verdict.json`` (atomic write).
        """
        method = str(exec_result.get("method") or self.method)
        status, reasons, evidence = self._compute_verdict(method, exec_result)

        payload: Dict[str, Any] = {
            "vp_id": str(vp_id),
            "status": status,
            "reasons": list(reasons),
            "evidence": dict(evidence),
        }

        self._atomic_write_verdict(plan_dir, vp_id, payload)
        return payload

    def _compute_verdict(
        self,
        method: str,
        exec_result: Dict[str, Any],
    ) -> tuple:
        """Dispatch to the per-method objective status computer.

        Returns a 3-tuple ``(status, reasons, evidence)``.
        """
        if method in ("ui_validation", "e2e"):
            # ``e2e`` shares the puppeteer path: both are "drive the real
            # UI and report what you observed". The difference is scope
            # (whole flow vs. one component), which the VP node carries —
            # not something the verdict computation needs to know.
            return self._compute_puppeteer_verdict(exec_result)
        # code_review — LLM-judged, pass-through. (api_test never reaches
        # this class: it is graded deterministically by
        # :mod:`verification_api_runner`, which the executor dispatches to
        # before a sub-agent is ever spawned.)
        return self._compute_llm_verdict(exec_result)

    @staticmethod
    def _compute_puppeteer_verdict(
        exec_result: Dict[str, Any],
    ) -> tuple:
        """Compute verdict for puppeteer (ui_validation) runs.

        All checkpoints must be ``passed=True`` for PASSED; any failure
        or a missing/empty checkpoint list forces FAILED.  The reason
        list always states the passed/total count so the on-disk
        artifact is human-readable without re-running the test.
        """
        checkpoints = exec_result.get("checkpoints") or []
        if not isinstance(checkpoints, list):
            checkpoints = []
        total = len(checkpoints)
        passed_count = sum(
            1 for c in checkpoints if isinstance(c, dict) and bool(c.get("passed"))
        )
        all_passed = total > 0 and passed_count == total
        status = "PASSED" if all_passed else "FAILED"
        reasons: List[str] = [
            f"puppeteer checkpoints: {passed_count}/{total} passed"
        ]
        failed_count = total - passed_count
        if failed_count > 0:
            reasons.append(f"{failed_count} checkpoint(s) failed")
        elif total == 0:
            reasons.append("no checkpoints recorded")
        evidence: Dict[str, Any] = {
            "logs_path": str(exec_result.get("logs_path") or ""),
            "checkpoints": checkpoints,
            "passed_count": passed_count,
            "failed_count": failed_count,
        }
        return status, reasons, evidence

    @staticmethod
    def _compute_llm_verdict(
        exec_result: Dict[str, Any],
    ) -> tuple:
        """Compute verdict for LLM-judged methods (code_review etc.).

        The LLM is the source of truth for these methods — we do NOT
        re-judge.  Both ``status`` (the new unified-schema field name)
        and ``verdict`` (the existing template's field name) are
        accepted so callers that produce either form round-trip
        cleanly.

        SKIPPED is coerced to FAILED (PRD decision point 3, zero-
        tolerance for skipping); the coercion is auditable via the
        appended reason.
        """
        raw_status = (
            exec_result.get("status")
            or exec_result.get("verdict")
            or "FAILED"
        )
        if raw_status not in ("PASSED", "FAILED", "SKIPPED"):
            raw_status = "FAILED"
        coerced = raw_status == "SKIPPED"
        status = "FAILED" if coerced else raw_status

        reasons_raw = exec_result.get("reasons") or []
        reasons: List[str] = list(reasons_raw) if isinstance(reasons_raw, list) else []
        if coerced:
            reasons.append("verdict=SKIPPED coerced to FAILED")
        if not reasons:
            reasons.append(f"code_review LLM verdict: {raw_status}")

        raw_evidence = exec_result.get("evidence")
        if isinstance(raw_evidence, dict):
            evidence: Dict[str, Any] = dict(raw_evidence)
        else:
            evidence = {}
        logs_path = exec_result.get("logs_path")
        if logs_path and "logs_path" not in evidence:
            evidence["logs_path"] = str(logs_path)
        return status, reasons, evidence

    @staticmethod
    def _atomic_write_verdict(
        plan_dir: Path,
        vp_id: str,
        payload: Dict[str, Any],
    ) -> Path:
        """Atomic-write ``payload`` to ``vps/{vp_id}/verdict.json``.

        Creates the ``vps/`` and ``vps/{vp_id}/`` directories on
        demand (parents=True).  Writes to a sibling ``.tmp`` file
        first, fsyncs the data, then ``os.replace``-s into place —
        the POSIX guarantee on ``os.replace`` means a crash mid-write
        never leaves a partial JSON on disk, and the executor can
        safely re-read the file as soon as this method returns.
        """
        plan_dir = Path(plan_dir)
        verdict_dir = plan_dir / "vps" / str(vp_id)
        verdict_dir.mkdir(parents=True, exist_ok=True)
        verdict_path = verdict_dir / "verdict.json"
        tmp_path = verdict_path.with_suffix(verdict_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, verdict_path)
        return verdict_path

    # ------------------------------------------------------------------
    # run() / self-heal loop — PRD Decision Point 2
    # ------------------------------------------------------------------
    #
    # The run() method implements the self-heal retry loop pinned by
    # PRD Decision Point 2, AMENDED 2026-09-15:
    #
    #   1. Build a temp settings.json with 8 PreToolUse security hooks
    #      (delegated to SettingsJsonBuilder).
    #   2. Invoke the coding tool to attempt the verification. The
    #      ``max_retries + 1`` total attempts cover the initial try
    #      plus the retry budget — the budget exists for ATTEMPT
    #      MALFUNCTIONS (verdict-parse garble, watchdog kill, transport
    #      error), not for verdicts.
    #   3. On a clean FAILED verdict (the sub-agent produced a
    #      well-formed verdict with real evidence), RETURN IT — no
    #      retry, no self-heal. A verification agent is an observer,
    #      not a repairer: "113 tests failed" is a finding, and
    #      re-running pytest does not change the code under test.
    #      Retrying a clean FAILED burns the whole suite again per VP per
    #      round, and a re-run cannot change the verdict — only the code
    #      under test can. The FAILED verdict is consumed
    #      downstream by the repair/split judge — that is the fix path.
    #   4. On a BLOCKED verdict (security net intercepts an
    #      Edit/Write or Bash tool call), return immediately — the
    #      security net is intentional, not a transient failure to
    #      retry away.
    #   5. When the retry budget is exhausted by malfunctions,
    #      synthesize a FAILED verdict from the last error (never
    #      SKIPPED — PRD decision point 3 zero-tolerance).
    #   6. The ``finally`` clause always cleans up the temp
    #      settings.json so /tmp does not accumulate stale files.

    async def run(
        self,
        vp_node: Dict[str, Any],
        coding_tool: Optional[Any] = None,
        log_dir: Optional[Path] = None,
        project_dir: Optional[Path] = None,
        plan_id: str = "default",
    ) -> "Verdict":
        """Run a verification sub-agent invocation with self-heal retry.

        Args:
            vp_node: Verification point dict (id, title,
                verification_method, test_command, expected_result).
            coding_tool: Optional coding tool (defaults to
                ClaudeCodingTool). Exposed as a parameter so tests
                can inject mocks / fake coding tools.
            log_dir: Directory where retry attempt logs are written
                (default: ``Path("logs")`` so the file lives at
                ``logs/vp_attempt_*.log`` — note the ``vp_attempt_``
                prefix avoids collision with the round-level
                ``verification_{round}_*.log`` files).
            project_dir: Project directory (passed for context; not
                used in the default no-op self-heal).
            plan_id: Plan ID, used to name the temp settings.json and
                tag the log entries.

        Returns:
            A :class:`Verdict` with ``verdict`` ∈ ``{"PASSED",
            "FAILED", "BLOCKED"}``. ``BLOCKED`` is set only by the
            security net (the LLM never emits it).
        """
        vp_id = str(vp_node.get("id", "unknown"))
        builder = SettingsJsonBuilder(vp_id=vp_id, plan_id=plan_id or "default")
        settings_path: Optional[str] = None
        log_path: Optional[Path] = None
        last_blocked: Optional[BlockedPathError] = None

        try:
            # Step 1 — build temp settings.json with the 8 security hooks
            settings_path = builder.build()

            # Per-VP retry-attempt logs use the ``vp_attempt_`` prefix so
            # they do NOT collide with the round-level
            # ``verification_{round}_*.log`` files used by
            # verification_persistence.start_round. The VP-006 test
            # contract globs ``logs/*.log`` (no prefix filter) and expects
            # the first result to start with ``event=round_start``; on
            # filesystems where ``glob`` order is undefined (e.g. macOS
            # APFS, where Python's ``glob.glob`` returns inode order
            # instead of lexicographic), co-locating both prefixes in
            # ``logs/`` causes ``vp_attempt_*.log`` to land at index 0
            # and trip the assertion. Shelve attempt logs into
            # ``logs/vp_attempts/`` so the unsorted glob at the parent
            # level only sees round logs.
            if log_dir is None:
                log_dir = Path("logs") / "vp_attempts"
            else:
                log_dir = Path(log_dir) / "vp_attempts"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"vp_attempt_{vp_id}_{int(time.time() * 1000)}.log"

            # Step 3 — run the self-heal loop
            return await self._self_heal_loop(
                vp_node=vp_node,
                coding_tool=coding_tool,
                settings_path=settings_path,
                log_path=log_path,
                project_dir=project_dir,
            )
        except BlockedPathError as e:
            # Security net intercepted an action; surface as a BLOCKED
            # verdict (per spec input example: {verdict: BLOCKED, ...}).
            last_blocked = e
            return Verdict(
                verdict="BLOCKED",
                reasons=[f"Security net blocked: {e.reason}"],
                evidence=[e.evidence] if e.evidence else [],
                provider="",
                chosen_model="",
                model_complexity=self.model_complexity,
            )
        finally:
            # Step 4 — always clean up the temp settings.json. This
            # block runs even if an unexpected exception escapes the
            # try / except chain (and even on KeyboardInterrupt), so the
            # private temp dir never accumulates stale verif_settings_*
            # files from a crashed / retried run.
            try:
                builder.cleanup()
            except Exception:
                # Cleanup failures must never mask the run() result.
                pass
            # If we hit a BLOCKED path, ensure the log captures it
            # (forensic trail even on the early-return path).
            if last_blocked is not None and log_path is not None:
                self._write_log(log_path, "blocked", {
                    "vp_id": vp_id,
                    "reason": last_blocked.reason,
                    "evidence": last_blocked.evidence,
                })

    async def _self_heal_loop(
        self,
        vp_node: Dict[str, Any],
        coding_tool: Optional[Any],
        settings_path: str,
        log_path: Path,
        project_dir: Optional[Path] = None,
    ) -> "Verdict":
        """Run the self-heal retry loop, returning the final Verdict.

        Total attempts = ``max_retries + 1`` (one initial try + N
        retries), and the budget exists ONLY for attempt malfunctions
        (verdict-parse garble, watchdog kill, transport error). A clean
        FAILED verdict is returned immediately — no retry, no
        self-heal (2026-09-15 the verifier is an
        observer; a finding is not a malfunction to retry away). The
        loop short-circuits on BLOCKED (no retry) and, when the budget
        is exhausted by malfunctions, synthesizes a FAILED verdict
        from the last error.
        """
        last_error: Optional[str] = None
        # 2026-09-14: the watchdog-kill bonus slot is
        # REMOVED — total attempts is now a hard ``max_retries + 1``
        # (initial try + 2 retries = 3). The old ``+1`` reserved slot
        # let a VP burn up to 5 full-suite runs (observed live: VP-023
        # burned ~100 min of pytest per round). Watchdog kills now
        # consume the normal retry budget like any other failure; a VP
        # that still fails goes to repair / split instead of retrying.
        total_attempts = self.max_retries + 1

        for attempt in range(total_attempts):
            self._write_log(log_path, "attempt_started", {
                "vp_id": vp_node.get("id", "unknown"),
                "attempt": attempt,
                "max_attempts": total_attempts,
            })

            try:
                verdict = await self._execute_attempt(
                    vp_node=vp_node,
                    coding_tool=coding_tool,
                    settings_path=settings_path,
                    log_path=log_path,
                    attempt=attempt,
                    project_dir=project_dir,
                )
            except BlockedPathError:
                # Propagate to the outer handler in run() so the
                # structured BLOCKED verdict is constructed there.
                raise
            except VerdictParseError as e:
                # L2 (json-parse retry): the sub-agent's response
                # could not be parsed as the Verdict JSON contract.
                # That's usually a transient protocol error (truncated
                # stream, missing bracket, etc.) — retry up to
                # ``max_retries`` so a momentary JSON garble doesn't
                # fail the VP. We record the raw output so the
                # failure is auditable, but we do NOT feed the parse
                # error into _self_heal (that path is for code
                # defects, not for protocol garbles).
                last_error = f"VerdictParseError: {e}"
                self._write_log(log_path, "verdict_parse_error", {
                    "attempt": attempt,
                    "error": last_error,
                    "raw_output_head": (str(e) or "")[:400],
                })
                if attempt < self.max_retries:
                    # Bounded exponential backoff: 1s, 2s, 4s, ...
                    # so we don't hammer the LLM while still
                    # giving the provider time to recover.
                    backoff = 2 ** attempt
                    self._write_log(log_path, "verdict_parse_retry", {
                        "attempt": attempt,
                        "backoff_sec": backoff,
                    })
                    await asyncio.sleep(backoff)
                continue
            except WatchdogKilledError as e:
                # 2026-09-07: the watchdog killed our subprocess.
                # Mark this run as having already consumed its single
                # watchdog-retry slot, then continue the loop — the
                # next iteration builds a fresh ``scoped_tool`` and
                # registers a fresh watchdog handle.
                #
                # 2026-09-14 bugfix: the old reset block referenced a
                # ``watchdog_handle`` local that does not exist in this
                # scope (it lives inside ``_execute_attempt``), so every
                # real kill raised NameError here instead of retrying.
                # The reset is also unnecessary: ``_execute_attempt``'s
                # ``finally`` ALWAYS unregisters the handle before the
                # exception bubbles up (verification_subagent.py:1228),
                # and ``register_sub_agent`` creates a fresh handle with
                # ``watchdog_kill_count=0`` on the next attempt. The
                # only state that must survive is the retry-used flag.
                self._watchdog_retry_used = True
                self._write_log(log_path, "sub_agent_kill_retry", {
                    "vp_id": vp_node.get("id", "unknown"),
                    "attempt": attempt,
                    "kill_count": e.kill_count,
                })
                continue
            except StuckAgentError as e:
                # The original sub-agent ran the test_command (perhaps
                # re-running it; that's allowed) but never returned
                # verdict JSON by the time the outer cap fired. Route
                # to a fresh-LLM-call summarizer that has no tools and
                # just returns verdict JSON based on the tee'd log.
                # This avoids the previous behaviour where
                # the entire round budget was burned by a stuck LLM.
                #
                # Note: the watchdog handle was already unregistered
                # in ``_execute_attempt``'s ``finally`` clause before
                # the exception bubbled up here — we don't need to
                # touch it again.
                self._write_log(log_path, "stuck_sub_agent_routed_to_summarizer", {
                    "vp_id": e.vp_id,
                    "attempt": attempt,
                    "elapsed": e.elapsed,
                    "threshold": e.threshold,
                    "output_file": e.output_file,
                })
                summarizer_verdict = await self._summarize_vp_result(
                    vp_node=vp_node,
                    output_file=e.output_file,
                    coding_tool=coding_tool,
                    log_path=log_path,
                )
                # Whatever the summarizer returns is treated as the
                # final verdict — the stuck agent's retry would just
                # loop again. Don't burn more rounds on this VP.
                return summarizer_verdict
            except Exception as e:
                # Coding tool call itself raised (not a verdict-parse
                # failure, but a transport / API error). Record the
                # failure and continue to the next attempt, unless
                # we're out of budget.
                last_error = str(e)
                self._write_log(log_path, "attempt_exception", {
                    "attempt": attempt,
                    "error": last_error,
                })
                if attempt < self.max_retries:
                    await self._self_heal(
                        vp_node=vp_node,
                        last_verdict=None,
                        last_error=last_error,
                        log_path=log_path,
                        attempt=attempt,
                    )
                continue

            self._write_log(log_path, "attempt_completed", {
                "attempt": attempt,
                "verdict": verdict.verdict,
                "reasons": verdict.reasons,
            })

            if verdict.verdict == "PASSED":
                return verdict
            if verdict.verdict == "BLOCKED":
                # Security net is intentional — no retry, no heal.
                return verdict

            # 2026-09-15: a clean FAILED verdict is a
            # FINDING, not a malfunction. The observer reported real
            # evidence; re-running the same checks cannot change the
            # code under test — every retry here burned a full suite
            # run (VP-023: 3 × 21-minute pytest to re-confirm the same
            # 113 failures). Return the verdict as-is so the round can
            # close and the repair/split judge can act on it. No
            # self-heal either: a verification agent has nothing to
            # "heal" — fixing belongs to the repair phase, and letting
            # the verifier touch code would corrupt verification
            # independence.
            return verdict

        # Budget exhausted by ATTEMPT MALFUNCTIONS only (a clean FAILED
        # verdict never reaches this point — it returns above). Per PRD
        # decision point 3 (zero-tolerance for skipping), we never
        # coerce this to SKIPPED; it's a hard FAILED.
        return Verdict(
            verdict="FAILED",
            reasons=[f"max retries exhausted: {last_error or 'unknown error'}"],
            evidence=[],
            model_complexity=self.model_complexity,
        )

    async def _execute_attempt(
        self,
        vp_node: Dict[str, Any],
        coding_tool: Optional[Any],
        settings_path: str,
        log_path: Path,
        attempt: int,
        project_dir: Optional[Path] = None,
    ) -> "Verdict":
        """Execute one verification attempt.

        Builds the prompt from the VP, invokes the coding tool, and
        parses the response into a :class:`Verdict`. Raises
        :class:`BlockedPathError` if the security net blocks an
        action; the caller (``_self_heal_loop``) will propagate it
        to :meth:`run` for structured verdict construction.
        """
        if coding_tool is None:
            # Lazy import so the module remains importable in
            # environments where coding_tool is unavailable (e.g.,
            # the unit test sandbox when no real LLM is wired up).
            try:
                from coding_tool import ClaudeCodingTool
                coding_tool = ClaudeCodingTool(
                    cwd=str(project_dir) if project_dir else None,
                    scene=self.scene,
                )
            except Exception as e:
                return Verdict(
                    verdict="FAILED",
                    reasons=[f"no coding tool available: {e}"],
                    evidence=[],
                    model_complexity=self.model_complexity,
                )

        prompt = self._build_prompt(vp_node, attempt=attempt)

        # Build a scoped coding_tool clone that carries the verification
        # sub-agent settings (security hooks + inherited LLM config) so
        # Phase 2 uses the same isolation as task-execution sub-agents.
        from coding_tool import ClaudeCodingTool
        from pathlib import Path as _Path

        scoped_tool = coding_tool
        if (
            settings_path
            and isinstance(coding_tool, ClaudeCodingTool)
            and coding_tool.settings != _Path(settings_path)
        ):
            # Note: we deliberately do NOT forward the parent's
            # ``base_url`` / ``auth_token`` here. The scoped tool
            # must walk the same ``provider_priority`` fallback
            # chain as the parent does — that chain is
            # ``_check_provider_availability`` (CC Switch SQLite DB
            # with a shell-env fallback), and any change in priority
            # order (e.g. user swapping [vendor-a-pro, vendor-b] to
            # [vendor-b, vendor-a-pro]) must be reflected in sub-agents
            # too. Inheriting the parent's resolved creds would
            # bypass the chain and freeze sub-agents on whichever
            # provider the parent happened to pick.
            #
            # See bug 3 fix on coding_tool.py
            # (``_load_provider_from_db`` now reads the CC Switch DB
            # so a missing DB no longer breaks the chain).
            scoped_tool = ClaudeCodingTool(
                model=coding_tool.model,
                model_type=getattr(coding_tool, "model_type", None),
                model_map=getattr(coding_tool, "model_map", None),
                provider_priority=getattr(coding_tool, "provider_priority", None),
                mcp_config=getattr(coding_tool, "mcp_config", None),
                logger=getattr(coding_tool, "logger", None),
                settings=_Path(settings_path),
                cwd=str(project_dir) if project_dir else None,
                scene=self.scene,
            )

        # 2026-08-25: register scoped_tool with the HB watchdog so a
        # hung ``query_json`` call can be killed (otherwise the sync
        # blocking I/O inside the asyncio loop hides indefinitely and
        # HeartbeatMonitor cannot detect the stall via thread.is_alive).
        watchdog_handle = None
        if self.registry is not None and self.plan_id is not None:
            try:
                watchdog_handle = self.registry.register_sub_agent(
                    plan_id=self.plan_id,
                    vp_id=str(vp_node.get("id", "unknown")),
                    attempt=attempt,
                    scoped_tool=scoped_tool,
                    timeout_seconds=self.HARD_WALL_CLOCK_CAP_SECONDS,
                    max_retries=self.max_retries,
                )
            except Exception:
                # Never let watchdog registration failures break a VP.
                watchdog_handle = None

        # 2026-08-25: master try/finally guarantees watchdog handle is
        # unregistered on every exit path — return success, return FAILED,
        # or reraise (BlockedPathError → caller). Without this the
        # HeartbeatMonitor would keep a stale handle until the next
        # registry sweep and risk killing a Popen that already exited.
        try:
            try:
                # query_json enforces a JSON object response; we re-serialise
                # to a string so parse_verdict applies the canonical contract
                # (reasons list copy, evidence list copy, SKIPPED coercion).
                if watchdog_handle is not None:
                    self.registry.mark_progress(
                        self.plan_id, watchdog_handle, stage="llm_query_start"
                    )
                # 2026-09-07 hang-fix: ``query_json`` is a *synchronous*
                # blocking call (spawns the claude CLI and blocks on its
                # stdout read loop). Awaiting it directly on the event
                # loop froze the whole asyncio loop — every other VP's
                # ``asyncio.wait_for`` timeout, the executor's parallel
                # scheduling and the watchdog's kill path were all dead
                # while one VP's LLM call ran: the frozen VP's own timeout
                # never fired and the remaining VPs starved behind it.
                #
                # Fix: run the blocking call in a worker thread via
                # ``asyncio.to_thread`` so the loop stays live.
                # If the outer timeout wins first, the worker thread is
                # abandoned but the loop is unblocked and the watchdog
                # can still SIGKILL the subprocess via the registry
                # handle.
                #
                # 2026-09-13: per-VP timeout
                # interface DELETED. The timeout contract is now:
                #   * Outer cap: flat ``HARD_WALL_CLOCK_CAP_SECONDS``
                #     (3600s) — fires the stuck-agent summarizer.
                #   * Inner layer: the 15-min idle detector inside
                #     ``coding_tool`` (``DEFAULT_TOTAL_TIMEOUT=900``).
                #     We pass ``timeout=None`` so that default applies;
                #     a legacy plan's per-VP ``timeout_seconds: 120``
                #     (VP-023) can no longer shrink either layer.
                outer_timeout = self.HARD_WALL_CLOCK_CAP_SECONDS
                # 2026-09-14 (user insight — "对于一个活着的pytest，即使它
                # 的执行时长很长…它其实也会有一些stdout，比如说它的进度
                # 条"): a healthy long-running tool call streams stdout
                # continuously, but that liveness only reached
                # ``scoped_tool._last_output_ts`` — never the plan-dir log
                # that the verification watchdog measures by mtime. A
                # 22-minute pytest therefore looked "silent" and got
                # stamped ``verification_log_stale`` (live: the plan
                # round-3 false positive). Pump the streaming activity
                # into the attempt log so health is visible where the
                # watchdog looks — and so operators see real progress.
                _pump_stop = threading.Event()
                _pump_thread = threading.Thread(
                    target=self._pump_tool_output,
                    args=(str(vp_node.get("id", "") or ""), scoped_tool,
                          log_path, watchdog_handle, _pump_stop),
                    daemon=True,
                    name=f"vp-output-pump-{vp_node.get('id', 'vp')}",
                )
                try:
                    _pump_thread.start()
                except Exception:
                    # A thread that cannot start must never block the VP —
                    # the watchdog's second-chance checks still cover it.
                    _pump_thread = None
                try:
                    response_dict = await asyncio.wait_for(
                        asyncio.to_thread(
                            scoped_tool.query_json,
                            prompt=prompt,
                            system_instruction=self.template,
                            timeout=None,
                        ),
                        timeout=outer_timeout,
                    )
                finally:
                    _pump_stop.set()
            except BlockedPathError:
                # Let the security-net signal bubble up to run().
                raise
            except HardTimeoutError as hte:
                # 2026-09-08: the inner coding_tool SIGKILL fired
                # (subprocess silent for ``total_sec`` seconds). This is
                # a SIGNAL to auto-split, not a verdict to surface. Re-raise
                # so the verification_agent layer routes to
                # ``_split_vp_on_timeout``. Do NOT swallow into FAILED.
                vp_id_log = vp_node.get("id", "unknown")
                self.logger.warning(
                    "[HARD TIMEOUT] vp_id=%s total_sec=%s elapsed=%.1fs "
                    "last_line=%r — propagating to auto-split",
                    vp_id_log, hte.total_sec, hte.elapsed, hte.last_line[:80],
                )
                raise
            except asyncio.TimeoutError:
                # 2026-09-08: the OUTER asyncio.wait_for fired
                # — the flat 1-hour cap elapsed without a verdict.
                # Route to the **stuck-agent
                # summarizer** (a fresh LLM call with no tools that
                # reads /tmp/vp_<id>_progress.log and returns
                # verdict JSON) instead of bouncing up to the
                # generic HardTimeoutError → split path. Splitting a
                # test_command that's already COMPLETE in its log
                # file is wasteful; summarization reads the existing
                # result. The first agent was allowed to re-run
                # pytest if it thought the run was flaky — we only
                # step in if it didn't return a verdict at all.
                vp_id_log = vp_node.get("id", "unknown")
                # Conventional tee path the user-prompt instructs the
                # sub-agent to use. Empty string means pytest never
                # ran — summarizer degrades to FAILED with that note.
                output_file = f"/tmp/vp_{vp_id_log}_progress.log"
                self.logger.warning(
                    "[STUCK SUB-AGENT] vp_id=%s outer_cap=%ds "
                    "(HARD_WALL_CLOCK_CAP_SECONDS=%d) "
                    "— routing to summarizer (will Read %s)",
                    vp_id_log, outer_timeout,
                    self.HARD_WALL_CLOCK_CAP_SECONDS, output_file,
                )
                raise StuckAgentError(
                    elapsed=float(outer_timeout),
                    threshold=float(outer_timeout),
                    vp_id=vp_id_log,
                    output_file=output_file,
                )
            except Exception as e:
                # 2026-09-07: distinguish watchdog kills from
                # genuine coding-tool failures. Read the per-handle
                # kill counter under the registry lock so we don't
                # race with the watchdog thread mutating it from the
                # heartbeat loop.
                kc = 0
                if watchdog_handle is not None:
                    with self.registry._lock:
                        kc = getattr(
                            watchdog_handle, "watchdog_kill_count", 0,
                        )

                retry_already_used = getattr(
                    self, "_watchdog_retry_used", False,
                )
                if kc >= 2 or (kc >= 1 and retry_already_used):
                    # Second kill on the same VP (or first kill on the
                    # retry attempt after _watchdog_retry_used was
                    # set) — give up and let the executor route this
                    # into ``_skipped_vps`` via the existing SKIPPED
                    # branch in ``_record_result``
                    # (verification_executor.py:1254-1256).
                    self._write_log(log_path, "sub_agent_skipped_after_kill", {
                        "vp_id": vp_node.get("id", "unknown"),
                        "attempt": attempt,
                        "kill_count": kc,
                        "original_error": str(e)[:200],
                    })
                    return Verdict(
                        verdict="SKIPPED",
                        reasons=[
                            "watchdog_killed_twice",
                            f"coding tool call failed: {e}",
                        ],
                        evidence=[],
                        model_complexity=self.model_complexity,
                    )
                if kc >= 1 and not retry_already_used:
                    # First kill on the initial attempt — raise so
                    # ``_self_heal_loop``'s new
                    # ``except WatchdogKilledError`` branch retries
                    # the VP with a fresh ``scoped_tool``. The retry
                    # attempt is what consumes the +1 slot from the
                    # budget extension (Step H).
                    raise WatchdogKilledError(
                        kill_count=kc, original_exc=e,
                    )
                # No watchdog kill involved — genuine coding-tool
                # failure, same as the pre-fix behavior.
                return Verdict(
                    verdict="FAILED",
                    reasons=[f"coding tool call failed: {e}"],
                    evidence=[],
                    model_complexity=self.model_complexity,
                )

            raw = json.dumps(response_dict) if not isinstance(
                response_dict, str
            ) else response_dict

            try:
                # 2026-09-18: no cross-verify here any more. The layer that
                # let the sub-agent's self-reported exit code override its
                # own verdict is gone — see ``parse_verdict``. The verdict
                # is validated for shape, and the *method's* own evidence
                # check (``verification_evidence`` for code_review /
                # ui_validation, ``verification_api_runner`` for api_test)
                # is what decides whether a PASSED is honoured.
                return parse_verdict_to_dataclass(raw)
            except VerdictParseError as e:
                return Verdict(
                    verdict="FAILED",
                    reasons=[f"verdict parse error: {e}"],
                    evidence=[raw[:500]],
                    model_complexity=self.model_complexity,
                )
        finally:
            if watchdog_handle is not None and self.registry is not None:
                try:
                    self.registry.unregister(self.plan_id, watchdog_handle)
                except Exception:
                    # Unregister failure must never bleed out of VP code.
                    pass

    async def _summarize_vp_result(
        self,
        vp_node: Dict[str, Any],
        output_file: Optional[str],
        coding_tool: Optional[Any],
        log_path: Path,
    ) -> "Verdict":
        """Fresh-LLM-call summarizer for a stuck sub-agent (2026-09-08 plan).

        Contract:
          * Builds a fresh coding-tool call with **NO tools allowed**
            (``allowed_tools=[]``). The summarizer can ONLY return
            JSON; it cannot run Bash, Read, Grep, or any tool.
          * The prompt contains: VP id, title, ``expected_result``,
            ``test_command``, and the **tail** of the tee'd log
            (``output_file``) so the LLM can read the actual pytest
            summary line + last failing test names. The summarizer is
            NOT given the full conversation history of the stuck
            agent — that would re-introduce the loop. Instead it's
            given the relevant fact: "pytest exited with what?
            Did it satisfy expected_result?".
          * Returns a parsed :class:`Verdict`. On parse failure
            falls back to ``FAILED`` with a clear reason so the
            downstream split path can take over.

        Why a fresh agent instead of re-running the original:
        ``_self_heal_loop`` could in principle retry the original
        sub-agent, but the original is structured to RUN pytest and
        judge it inline — a re-run is exactly what triggered the
        stuck loop. A second agent with no tools cannot re-run;
        it can only summarize the existing log.

        Why no tools at all (``allowed_tools=[]``):
        a summarizer with even Read access will sometimes try to
        Read other files to "understand context" and get sidetracked.
        Stripping the tool belt is the simplest way to ensure the
        summarizer produces one JSON object and exits.
        """
        vp_id = str(vp_node.get("id", "unknown"))
        if coding_tool is None:
            from coding_tool import ClaudeCodingTool
            coding_tool = ClaudeCodingTool(scene="summarizer")

        # Read the tee'd log tail (last ~200 lines is enough for the
        # pytest summary line + the last few FAILED entries).
        log_tail = ""
        log_size = 0
        if output_file:
            try:
                p = Path(output_file)
                if p.exists():
                    log_size = p.stat().st_size
                    with p.open("r", errors="replace") as f:
                        # Slurp the tail efficiently — file may be
                        # hundreds of MB for a full pytest run.
                        # ``deque(maxlen=200)`` keeps memory bounded.
                        from collections import deque
                        tail_lines: deque = deque(maxlen=200)
                        for line in f:
                            tail_lines.append(line)
                        log_tail = "".join(tail_lines)
            except OSError as e:
                self._write_log(log_path, "summarizer_log_unreadable", {
                    "vp_id": vp_id, "output_file": output_file,
                    "error": str(e)[:200],
                })

        if not log_tail:
            # No log → can't summarize → return a clear FAILED so
            # the parent split path can take over instead.
            self._write_log(log_path, "summarizer_no_output", {
                "vp_id": vp_id, "output_file": output_file,
            })
            return Verdict(
                verdict="FAILED",
                reasons=[
                    "stuck sub-agent: no test output captured at "
                    f"{output_file!r}; cannot summarize"
                ],
                evidence=[],
                model_complexity=self.model_complexity,
            )

        self._write_log(log_path, "summarizer_started", {
            "vp_id": vp_id,
            "output_file": output_file,
            "log_size_bytes": log_size,
            "tail_lines": log_tail.count("\n"),
        })

        prompt = (
            f"You are a VERDICT SUMMARIZER for a verification point that "
            f"hit a stuck sub-agent. The previous verification agent ran "
            f"the test_command but never returned a verdict JSON. Your "
            f"ONLY job is to read the pytest output below and produce a "
            f"structured verdict.\n\n"
            f"VP id: {vp_id}\n"
            f"Title: {vp_node.get('title', '')}\n"
            f"Expected result: {vp_node.get('expected_result', '')}\n"
            f"Test command: {vp_node.get('test_command', '')}\n\n"
            f"Test output (tail, last 200 lines of {output_file}):\n"
            f"```\n{log_tail}\n```\n\n"
            f"Decide PASSED or FAILED based on the pytest summary line "
            f"and the FAILED entries above. If you see \"= X failed, Y "
            f"passed\" then pytest exited non-zero → FAILED. If \"= Y "
            f"passed\" only (no failures), pytest exited 0 → PASSED.\n\n"
            f"Return ONLY this JSON object, no markdown, no prose:\n"
            f'{{"verdict": "PASSED|FAILED", "reasons": [<one-line '
            f'explanation citing the pytest summary line>], "evidence": '
            f'[<last 3-5 FAILED test names if FAILED, else empty list>]}}'
        )
        system_instruction = (
            "You are a verdict summarizer. Read the pytest output in the "
            "user prompt and return ONLY the JSON object requested. Do "
            "NOT call any tools. Do NOT re-run any tests. Do NOT add "
            "prose. Output is the JSON object and nothing else."
        )

        try:
            response_dict = coding_tool.query_json(
                prompt=prompt,
                system_instruction=system_instruction,
                timeout=self.HARD_WALL_CLOCK_CAP_SECONDS,
                allowed_tools=[],  # summarizer has no tools
            )
        except Exception as e:
            self._write_log(log_path, "summarizer_query_failed", {
                "vp_id": vp_id, "error": str(e)[:300],
            })
            return Verdict(
                verdict="FAILED",
                reasons=[
                    f"stuck sub-agent: summarizer query failed: "
                    f"{type(e).__name__}: {str(e)[:200]}"
                ],
                evidence=[],
                model_complexity=self.model_complexity,
            )

        raw = json.dumps(response_dict) if not isinstance(
            response_dict, str
        ) else response_dict
        try:
            verdict = parse_verdict_to_dataclass(raw)
        except VerdictParseError as e:
            self._write_log(log_path, "summarizer_verdict_parse_error", {
                "vp_id": vp_id,
                "error": str(e)[:300],
                "raw_head": raw[:400],
            })
            return Verdict(
                verdict="FAILED",
                reasons=[
                    f"stuck sub-agent: summarizer returned unparseable "
                    f"verdict ({type(e).__name__}: {str(e)[:200]})"
                ],
                evidence=[raw[:500]],
                model_complexity=self.model_complexity,
            )

        self._write_log(log_path, "summarizer_returned_verdict", {
            "vp_id": vp_id,
            "verdict": verdict.verdict,
            "reasons_count": len(verdict.reasons or []),
            "evidence_count": len(verdict.evidence or []),
        })
        return verdict

    def _build_prompt(self, vp_node: Dict[str, Any], attempt: int) -> str:
        """Build the LLM prompt from a verification point node.

        The prompt is short and structured: VP id, title, method,
        expected result, and — since 2026-09-18 — the ``target_url`` a
        ``ui_validation`` point is required to declare. On retries
        (attempt > 0) a self-heal hint is appended so the LLM knows the
        previous try failed, and ``_evidence_contract`` appends the
        artifact requirement for the LLM-judged methods.

        The prompt explicitly forbids project-wide test runs and
        coverage analysis. Without that constraint the LLM treats the
        verification as a free-form code review and reports a synthetic
        verdict based on the project's overall test state — observed
        once as 21 unrelated test failures plus a 59% coverage number
        reported against a point that verified three specific functions.
        """
        parts = [
            f"Verification point: {vp_node.get('id', 'unknown')}",
            f"Title: {vp_node.get('title', '')}",
            f"Method: {vp_node.get('verification_method', self.method)}",
            f"Priority: {vp_node.get('priority', 'medium')}",
            f"Expected result: {vp_node.get('expected_result', '')}",
        ]
        # 2026-09-18: a ui_validation VP is *required* to declare a
        # target_url, but it never reached this prompt — the sub-agent had
        # to infer the page from prose. Emit it up front.
        target_url = str(vp_node.get("target_url") or "").strip()
        if target_url:
            parts.append(f"Target URL: {target_url}")
        evidence_command = str(vp_node.get("evidence_command") or "").strip()
        if evidence_command:
            parts.append(
                "Supporting evidence command (optional; it does NOT decide "
                f"the verdict): {evidence_command}"
            )
        service_env = vp_node.get("service_env")
        if isinstance(service_env, dict) and service_env:
            parts.append("")
            parts.append(
                "## Managed services — the framework already started these. "
                "Use these addresses; do NOT start your own copy."
            )
            for key in sorted(service_env):
                parts.append(f"  {key}={service_env[key]}")
        parts.extend([
            "",
            "STRICT SCOPE: Your verdict MUST rest on evidence you actually "
            "gathered for THIS verification point. Do NOT run project-wide "
            "test suites, do NOT compute project-wide coverage, and do NOT "
            "pull in evidence from files this point does not exercise. "
            "Without that constraint the verdict drifts into a synthetic "
            "code review — observed once as 21 unrelated test failures and "
            "a 59% coverage number reported against a point that verified "
            "three specific functions.",
            "",
            "EVIDENCE: Whatever your method, you must be able to show the "
            "basis of your conclusion. For code_review and ui_validation "
            "the framework checks a specific artifact (see the EVIDENCE "
            "CONTRACT below) and will refuse a PASSED it cannot verify.",
            "",
            "PROGRESS REPORTING (if the work is long-running): The "
            "orchestrator's staleness watchdog force-terminates plans whose "
            "verification log has no fresh writes for >~15 min. If what you "
            "are about to do would exceed ~5 min of silent wall-clock time, "
            "structure it so observable progress is emitted continuously — "
            "pipe through `tee` to a stable path (e.g. `/tmp/"
            "<vp_id>_progress.log`) and, where the framework exposes it, "
            "append `progress` events to the sub-agent log via "
            "`_write_log(log_path, 'progress', {...})`. Each progress event "
            "MUST carry real observed data (current item, completed/total "
            "counts, last log line) — never an empty heartbeat. See the "
            "system prompt's TIME-BUDGET-AND-PROGRESS block for the full "
            "anti-cheat contract.",
        ])

        if attempt > 0:
            parts.append("")
            parts.append(
                f"Retry attempt {attempt + 1}/{self.max_retries + 1}: "
                "the previous attempt failed. Analyse the failure, "
                "fix the underlying issue (edit code, install a "
                "missing dependency, or tweak config), then re-run "
                "the test_command and report the verdict."
            )
        parts.extend(self._evidence_contract(vp_node))
        return "\n".join(parts)

    def _evidence_contract(self, vp_node: Dict[str, Any]) -> List[str]:
        """The "prove your conclusion" block for the LLM-judged methods.

        2026-09-18: ``code_review`` and ``ui_validation`` rest on an LLM's
        word, and the old cross-method overlay could not check that word
        against anything — it only compared a *self-reported* exit code,
        which is how the same artifact graded PASSED in one round and
        FAILED in the next. The framework now checks the artifact each
        method was supposed to produce; a PASSED whose artifact is
        missing or does not hold up is downgraded to FAILED.

        The block is generated rather than baked into the template
        constants because the artifact directory is per-VP.
        """
        method = str(
            vp_node.get("verification_method") or self.method or ""
        ).strip()
        artifact_dir = str(vp_node.get("evidence_artifact_dir") or "").strip()
        if not artifact_dir:
            return []

        if method == "code_review":
            return [
                "",
                "## EVIDENCE CONTRACT — write citations.json (MANDATORY)",
                "Every conclusion you report must rest on specific lines "
                "you actually read. Write exactly this file (create the "
                "directory if needed):",
                f"    {artifact_dir}/citations.json",
                '{"citations": [',
                '  {"file": "<path relative to the project root>",',
                '   "line": <1-based line number>,',
                '   "snippet": "<the exact text you saw on that line>",',
                '   "supports": "<which part of the assertion this proves>"}',
                "]}",
                "Rules:",
                "- At least one citation. Cite every file/line your verdict "
                "depends on.",
                "- `snippet` must be text that is actually on (or within a "
                "couple of lines of) `line`. The framework reads the file "
                "and checks this; a citation it cannot resolve is not "
                "evidence.",
                "- `snippet` must be the verbatim line — do NOT abbreviate "
                "with `...` or other ellipsis markers. The framework's "
                "whitespace-normalised, single-line lookup cannot resolve a "
                "snippet whose middle is hidden; an ellipsis always reads "
                "as \"no real citation here\" and the citation is downgraded "
                "to unverifiable.",
                "- `file` must be a path relative to the project root "
                "(e.g. `backend/foo.py`, `SECURITY_AUDIT.md`). Absolute "
                "paths, paths under `/tmp/`, `/private/tmp/`, or any other "
                "directory outside this repository are NOT project evidence "
                "— the framework rejects them as `outside_project` and the "
                "citation is treated as unverifiable.",
                "- A verdict of PASSED with no citations, or with citations "
                "that do not resolve, is NOT honoured — it is downgraded to "
                "FAILED.",
            ]

        if method in ("ui_validation", "e2e"):
            observed = (
                "Every end-to-end observation you report must be one you "
                "actually made in this run."
                if method == "e2e" else
                "Every UI observation you report must be one you actually "
                "made."
            )
            return [
                "",
                "## EVIDENCE CONTRACT — write checkpoints.json (MANDATORY)",
                observed + " Write exactly this file (create the directory if "
                "needed):",
                f"    {artifact_dir}/checkpoints.json",
                '{"checkpoints": [',
                '  {"selector": "<the selector you looked at>",',
                '   "expected": "<what the requirement says should be there>",',
                '   "actual": "<what you actually observed>",',
                '   "passed": true|false}',
                "]}",
                "Rules:",
                "- At least one checkpoint. No checkpoint may omit "
                "`selector` or `actual` — \"it looked fine\" is not a "
                "checkpoint.",
                "- `passed` must be a boolean, and your verdict must agree "
                "with it: PASSED requires every checkpoint to have "
                "passed=true.",
                "- A PASSED whose checkpoints.json is missing or malformed "
                "is NOT honoured — it is downgraded to FAILED.",
            ]

        return []

    async def _self_heal(
        self,
        vp_node: Dict[str, Any],
        last_verdict: Optional[Verdict],
        last_error: Optional[str],
        log_path: Path,
        attempt: int,
    ) -> str:
        """Apply a self-heal action based on the last failure.

        The default implementation is a no-op stub: it returns a
        human-readable description of the would-be heal action so
        callers (and tests) have something concrete to assert on,
        but it does NOT actually mutate code or install packages.
        Production deployments can subclass :class:`VerificationSubAgent`
        and override this method to wire in real fix-up logic (e.g.,
        an LLM that analyses the test output and emits a patch).

        Returns:
            A short description of the heal action taken.
        """
        if last_error is not None:
            return f"attempted heal after error: {last_error[:120]}"
        if last_verdict is not None and last_verdict.reasons:
            return (
                f"attempted heal based on reasons: "
                f"{last_verdict.reasons[0][:120]}"
            )
        return "attempted heal (no specific reason recorded)"

    #: How often the tool-output pump writes a liveness line while a
    #: sub-agent's tool call is streaming. Must be comfortably under the
    #: verification watchdog's staleness threshold (3600s in
    #: ``.env``); 60s gives operators a dense trail without flooding the
    #: attempt log.
    OUTPUT_HEARTBEAT_INTERVAL_SEC: int = int(
        os.environ.get("VP_OUTPUT_HEARTBEAT_INTERVAL_SEC", "60")
    )

    def _pump_tool_output(
        self,
        vp_id: str,
        scoped_tool: Any,
        log_path: Optional[Path],
        watchdog_handle: Any,
        stop_event: "threading.Event",
    ) -> None:
        """Mirror a streaming tool call's liveness into the attempt log.

        2026-09-14 (user insight): a live pytest prints progress lines
        even when it runs far longer than any idle threshold — that is
        exactly the case that was being killed as "silent". The coding
        tool already stamps ``_last_output_ts`` per stdout line
        (``coding_tool`` read loop); this thread samples it and writes a
        ``tool_output_heartbeat`` event whenever it advanced, which:
          * refreshes the attempt-log mtime the watchdog's staleness
            check measures (``logs/vp_attempts/*.log``);
          * refreshes the registry handle so ``find_stale`` does not
            kill the subprocess;
          * leaves an operator-visible trail ("still printing: <line>").

        Runs on a daemon thread and never raises — a missing attribute or
        a closed log must not disturb the VP.
        """
        started = time.monotonic()
        last_ts: Optional[float] = None
        while not stop_event.wait(self.OUTPUT_HEARTBEAT_INTERVAL_SEC):
            try:
                ts = getattr(scoped_tool, "_last_output_ts", None)
                if ts is None or ts == last_ts:
                    # No new stdout since the last sample — staying silent
                    # is the point (a genuinely idle tool produces no
                    # heartbeat, so the watchdog can still catch it).
                    continue
                last_ts = ts
                last_line = str(
                    getattr(scoped_tool, "_last_output_line", "") or ""
                )
                self._write_log(log_path, "tool_output_heartbeat", {
                    "vp_id": vp_id,
                    "elapsed_sec": round(time.monotonic() - started, 1),
                    "last_line": last_line[:200],
                })
                if watchdog_handle is not None and self.registry is not None:
                    try:
                        self.registry.mark_progress(
                            self.plan_id, watchdog_handle,
                            stage="tool_streaming",
                        )
                    except Exception:
                        pass
            except Exception:
                continue

    @staticmethod
    def _write_log(log_path: Path, event: str, data: Dict[str, Any]) -> None:
        """Append a structured JSON-line entry to the run's log file.

        Failures in the logger must never break the run() result, so
        every operation is wrapped in a broad try/except. The file
        format is JSON-lines (one object per line), matching the
        convention used by ``plans/{plan_id}/execution.log``.
        """
        if log_path is None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "event": event,
                "data": data,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Never let logging failures propagate.
            pass


class WatchdogKilledError(Exception):
    """Raised by ``_execute_attempt`` when the sub-agent watchdog killed
    the subprocess (because ``last_progress_ts`` went stale) and the
    per-handle kill count is exactly 1 — i.e. the executor should retry
    once with a fresh subprocess via the existing ``_self_heal_loop``.

    When the kill count is already 2 (a second kill on the same VP),
    ``_execute_attempt`` returns ``Verdict(verdict="SKIPPED", ...)``
    directly without raising — the round should advance to the next
    VP rather than burning the entire retry budget.

    Attributes:
        kill_count: The value of ``SubAgentHandle.watchdog_kill_count``
            at the moment the exception was raised. ``1`` for the
            retry path; ``>= 2`` for the SKIPPED path.
        original_exc: The underlying ``Exception`` raised by
            ``scoped_tool.query_json`` after the SIGKILL broke the
            stdout pipe.
    """

    def __init__(self, kill_count: int, original_exc: BaseException):
        self.kill_count = kill_count
        self.original_exc = original_exc
        super().__init__(
            f"sub-agent subprocess killed by watchdog (kill_count="
            f"{kill_count}): {original_exc}"
        )


class StuckAgentError(Exception):
    """Raised by ``_execute_attempt`` when the outer wall-clock cap
    fires — i.e. the original sub-agent ran the test_command (the
    tee'd log has its summary line) but never returned verdict JSON
    before the outer cap elapsed.

    Per the 2026-09-08: the first sub-agent's context (the
    tee'd log file at the conventional ``/tmp/vp_<id>_progress.log``
    path) is handed to a fresh summarizer LLM call with **no tools**
    that reads the log and returns a structured verdict. The first
    agent is allowed to re-run pytest if it suspects flakiness; the
    summarizer only steps in when the first agent never produced a
    verdict at all. This avoids the previous behaviour where the
    orchestrator killed the round budget on a stuck LLM and the
    parent had to fall through to ``status="timeout"`` with no
    verdict.

    Attributes:
        elapsed: Wall-clock seconds from ``_execute_attempt`` start
            to the outer cap firing.
        threshold: The configured outer-cap threshold that fired.
        vp_id: The VP id (for logging).
        output_file: The tee'd log path the summarizer should Read.
            ``None`` if pytest never produced one (in which case the
            summarizer degrades to ``FAILED, reasons=["no test
            output captured"]``).
    """

    def __init__(
        self,
        elapsed: float,
        threshold: float,
        vp_id: str,
        output_file: Optional[str] = None,
    ):
        self.elapsed = elapsed
        self.threshold = threshold
        self.vp_id = vp_id
        self.output_file = output_file
        super().__init__(
            f"stuck sub-agent for vp_id={vp_id}: query_json ran for "
            f"{elapsed:.1f}s (threshold={threshold:.1f}s); routing to "
            f"summarizer with output_file={output_file!r}"
        )


class BlockedPathError(Exception):
    """Raised when a security net hook blocks a sub-agent action.

    The security net lives in the temp settings.json built by
    :class:`SettingsJsonBuilder`. When the sub-agent tries to
    Edit/Write outside its own workspace or run a blacklisted Bash
    command (e.g., ``rm -rf plans/``), the hook returns exit code 2,
    which the SDK reports as a blocked tool call. The sub-agent
    catches that signal and raises this exception so
    :meth:`VerificationSubAgent.run` can return a structured
    :class:`Verdict` with ``verdict="BLOCKED"`` (rather than
    retrying, which would just keep hitting the same blacklist).

    Attributes:
        reason: Human-readable explanation of why the action was
            blocked (e.g. "Edit/Write outside the workspace blocked").
        evidence: Raw signal from the hook (e.g. "hook exit=2 at call 0").
    """

    def __init__(self, reason: str = "", evidence: str = "") -> None:
        self.reason = reason
        self.evidence = evidence
        message = reason or "security net blocked"
        super().__init__(message)


# ---------------------------------------------------------------------------
# SettingsJsonBuilder — temporary settings.json with 8 hard PreToolUse hooks
# ---------------------------------------------------------------------------
#
# PRD Decision Point 2 pins 8 hard PreToolUse hooks for the verification
# sub-agent. The hooks are split into two groups:
#
#   * Basic 5 (Bash): rm -rf /, fork bomb, mkfs, dd if=/dev/zero, force
#     push to main. These are system-level safety nets that protect
#     against irreversible damage from any sub-agent.
#
#   * Extended 3 (Bash ×2, Edit|Write ×1): plans/ deletion interception,
#     server-restart interception, and Edit/Write outside the
#     sub-agent's own workspace. These guard the project a sub-agent
#     was pointed at against accidental destruction.
#
# The order of the 8 rules is stable: basic 5 in front, extended 3 last.
# Tests pin the order so the SDLC audit trail is reproducible.
#
# Each rule's command is a one-liner that reads a JSON payload from
# stdin (the Claude Code SDK supplies ``tool_input`` keyed by tool
# name) and exits 2 to block or 0 to allow. The descriptive name is
# kept ≤ 30 Chinese characters per the PRD's "≤30字" rule.

_BASIC_RM_ROOT_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'rm\\s+-(?=[a-zA-Z]*[rR])(?=[a-zA-Z]*[fF])[a-zA-Z]+',c) "
    "and re.search(r'(/\\s|/$|/ )',c) else 0)\""
)

_BASIC_FORK_BOMB_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r':\\s*\\(\\s*\\)\\s*\\{.*:\\s*\\|.*\\}\\s*;\\s*:',c) else 0)\""
)

_BASIC_MKFS_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bmkfs(\\.|\\b)',c) else 0)\""
)

_BASIC_DD_ZERO_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bdd\\b.*if=/dev/(zero|urandom)',c) else 0)\""
)

_BASIC_FORCE_PUSH_MAIN_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\bgit\\b.*\\bpush\\b.*-f',c) "
    "and re.search(r'\\bmain\\b',c) else 0)\""
)

_EXT_PLANS_DELETE_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'\\brm\\b.*plans/',c) else 0)\""
)

_EXT_SERVER_RESTART_BLOCK = (
    "python3 -c "
    "\"import json,sys,re;d=json.load(sys.stdin);"
    "c=d.get('tool_input',{}).get('command','');"
    "sys.exit(2 if re.search(r'pkill\\s+.*server\\.py',c) "
    "or re.search(r'\\bkill\\b.*server\\.py',c) else 0)\""
)

_EXT_EXEC_PATH_BLOCK = (
    "python3 -c "
    "\"import json,sys,os,pathlib,tempfile;"
    "d=json.load(sys.stdin);"
    "p=d.get('tool_input',{}).get('file_path','') or '';"
    "R=lambda x: str(pathlib.Path(x).resolve()) if x else '';"
    "inside=lambda x,r: bool(r) and (x==r or x.startswith(r+os.sep));"
    "rp=R(p);pdr=R(os.environ.get('PDT_PROJECT_DIR',''));"
    "pr=R(os.environ.get('PDT_PLANS_DIR',''));tmp=R(tempfile.gettempdir());"
    "scratch=rp.startswith('/tmp/') or rp.startswith('/private/tmp/') "
    "or inside(rp,tmp);"
    "blocked=bool(rp) and bool(pdr) and not "
    "(inside(rp,pdr) or inside(rp,pr) or scratch);"
    "sys.exit(2 if blocked else 0)\""
)


_HOOK_RULES: Tuple[Dict[str, str], ...] = (
    # Basic 5 — system-level safety nets
    {
        "matcher": "Bash",
        "name": "rm_root_block",
        "command": _BASIC_RM_ROOT_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "fork_bomb_block",
        "command": _BASIC_FORK_BOMB_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "mkfs_block",
        "command": _BASIC_MKFS_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "dd_zero_block",
        "command": _BASIC_DD_ZERO_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "force_push_main_block",
        "command": _BASIC_FORCE_PUSH_MAIN_BLOCK,
    },
    # Extended 3 — this project's own guards
    {
        "matcher": "Bash",
        "name": "plans_delete_block",
        "command": _EXT_PLANS_DELETE_BLOCK,
    },
    {
        "matcher": "Bash",
        "name": "pdt_server_restart_block",
        "command": _EXT_SERVER_RESTART_BLOCK,
    },
    {
        "matcher": "Edit|Write",
        "name": "exec_path_block",
        "command": _EXT_EXEC_PATH_BLOCK,
    },
)


@dataclass
class SettingsJsonBuilder:
    """Build a temporary ``settings.json`` with 8 PreToolUse security hooks.

    The 8 hooks are pinned by PRD Decision Point 2 of the verification
    sub-agent work-package. Each rule is identified by a short ``name``
    (≤ 30 Chinese characters) and the ``command`` is a one-liner shell
    that blocks (exit 2) or allows (exit 0) a given tool invocation.

    Attributes:
        vp_id: Verification-point ID, e.g. ``"VP-001"``. Used in the
            temp file path.
        plan_id: Plan ID, e.g. ``"20260101-example"``. Also
            used in the temp file path.
    """

    vp_id: str
    plan_id: str

    #: Memoised temp path. ``build()`` writes it and ``cleanup()`` removes
    #: it, so the two must agree — see :meth:`_temp_path`.
    _temp_file_path: Optional[str] = field(
        default=None, repr=False, compare=False,
    )

    def build(self) -> str:
        """Build and write the settings.json into a private temp dir.

        The temp file path follows the pattern
        ``<private tmpdir>/verif_settings_{vp_id}_{plan_id}.json``.

        Returns:
            Absolute path to the temp settings file.
        """
        path = self._temp_path()
        settings = {
            "hooks": {
                "PreToolUse": [self._rule_to_hook(r) for r in _HOOK_RULES],
            }
        }
        write_private_json(Path(path), settings)
        return path

    def cleanup(self) -> None:
        """Remove the temp settings file and its private directory.

        Idempotent: a missing file is silently ignored. Calling this on
        an instance that was never built does nothing at all — it must
        not materialise a directory just to remove it.

        The class does not register an ``atexit`` hook — the caller is
        responsible for invoking ``cleanup()`` (typically in a
        ``finally`` block).
        """
        if not self._temp_file_path:
            return
        path = Path(self._temp_file_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
        # The 0700 directory was created for this one file; leaving it
        # behind would accumulate one empty dir per VP per round.
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def hooks(self) -> List[Dict]:
        """Return the in-memory list of PreToolUse hook entries.

        Each entry has the canonical shape::

            {
                "matcher": "Bash" | "Edit|Write",
                "hooks": [{"type": "command", "command": "..."}]
            }

        Useful for callers that want to inspect the rules without
        re-reading the temp file.
        """
        return [self._rule_to_hook(r) for r in _HOOK_RULES]

    @staticmethod
    def _rule_to_hook(rule: Dict[str, str]) -> Dict:
        """Convert an internal rule dict to the canonical hook entry."""
        return {
            "matcher": rule["matcher"],
            "hooks": [
                {
                    "type": "command",
                    "command": rule["command"],
                }
            ],
        }

    def _temp_path(self) -> str:
        """Absolute path of the temp settings file, created on first call.

        Memoised because ``build()`` and ``cleanup()`` both call this and
        must agree on the path — a fresh :func:`private_dir` per call
        would leave ``cleanup()`` hunting for a file ``build()`` never
        wrote.

        A private directory rather than a flat ``/tmp`` name: this
        payload carries hook commands rather than credentials, but the
        flat name handed the plan id and the VP id to every local
        account, and the file can outlive a crashed run.
        """
        if self._temp_file_path is None:
            self._temp_file_path = str(
                private_dir()
                / f"verif_settings_{self.vp_id}_{self.plan_id}.json"
            )
        return self._temp_file_path


# ---------------------------------------------------------------------------
# Verdict + parse_verdict — structured JSON contract for sub-agent output
# ---------------------------------------------------------------------------
#
# PRD Decision Point 4 (zero-tolerance): any malformed JSON, wrong
# top-level type, or missing required field is rejected with
# ``VerdictParseError``. The caller is expected to retry / log / report
# rather than silently absorb a bad payload.
#
# PRD Decision Point 3 (zero-tolerance for skipping): ``verdict="SKIPPED"``
# is coerced to ``"FAILED"`` at parse time. The downstream judgment
# pipeline never sees a SKIPPED value.


class VerdictParseError(ValueError):
    """Raised when an LLM output cannot be parsed into a ``Verdict``.

    Per PRD decision point 4 (zero-tolerance), any malformed JSON
    payload is rejected — the parse layer never silently coerces
    or drops fields. Per PRD decision point 3 (zero-tolerance for
    skipping), ``verdict="SKIPPED"`` is coerced to ``"FAILED"`` at
    parse time; downstream judgment therefore never has to handle
    a SKIPPED value.
    """


@dataclass
class Verdict:
    """Structured result from a single verification sub-agent invocation.

    Attributes:
        verdict: One of ``"PASSED"`` or ``"FAILED"``. ``"SKIPPED"``
            is never present — :func:`parse_verdict` coerces it to
            ``"FAILED"`` per PRD decision point 3.
        reasons: Free-form explanations supporting the verdict
            (e.g. "all assertions passed", "missing field X").
        evidence: Concrete observations the LLM cited (e.g. command
            output excerpts, file paths, line numbers).
        provider: Provider key (e.g. ``"vendor-a-pro"``,
            ``"vendor-b"``). Empty string if the LLM did not declare it.
        chosen_model: Concrete model id chosen for this VP
            (e.g. ``"Vendor A-M3"``, ``"B-PRO-5.1"``).
        model_complexity: Tier hint that produced the routing decision
            (e.g. ``"simple"`` / ``"medium"`` / ``"complex"``).
    """

    verdict: str
    reasons: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    provider: str = ""
    chosen_model: str = ""
    model_complexity: str = ""


def parse_verdict(subagent_output: Dict[str, Any]) -> str:
    """Validate a sub-agent's self-reported verdict and return it.

    2026-09-18 (VP judgment rework): this used to be a **cross-verify**
    layer. It took the ``pytest_exit_code`` and ``tests_run`` the
    sub-agent reported *about itself* and let them override its verdict.
    That is precisely what made the same artifact grade PASSED in one
    round and FAILED in the next — one run's VP-001 reported ``tests_run=0``
    in round 1 (→ FAILED) and "Exit code 0 confirms successful
    execution" in round 3 (→ PASSED), on an unchanged command. The
    failed-VP set therefore differed every round,
    ``same_failure_repeated`` never fired, and the loop always ran out
    to ``max_rounds``.

    A signal that one agent both produces and interprets is not a second
    source of truth. Each method now carries its own checkable basis
    instead:

      * ``api_test`` — executed and graded by
        :mod:`verification_api_runner`, no LLM involved;
      * ``code_review`` / ``ui_validation`` — must produce an artifact
        (citations / checkpoints) that :mod:`verification_evidence`
        verifies before a PASSED is honoured.

    So this function is back to doing one thing: validate the shape.
    It no longer takes exit-code arguments, and there is no
    ``cross_verify_status`` to report because there is no second
    signal to report *about*.

    ``SKIPPED`` is coerced to ``FAILED`` per PRD decision point 3
    (zero-tolerance for skipping).

    Raises:
        VerdictParseError: If ``subagent_output`` is not a dict, is
            missing the ``verdict`` field, or has a ``verdict`` value
            outside ``{"PASSED", "FAILED", "SKIPPED"}``.
    """
    if not isinstance(subagent_output, dict):
        raise VerdictParseError(
            f"subagent_output must be a dict, got "
            f"{type(subagent_output).__name__}"
        )

    if "verdict" not in subagent_output:
        raise VerdictParseError("missing required field: 'verdict'")

    verdict = subagent_output["verdict"]
    if not isinstance(verdict, str):
        raise VerdictParseError(
            f"'verdict' must be a str, got {type(verdict).__name__}"
        )

    if verdict == "SKIPPED":
        return "FAILED"
    if verdict not in ("PASSED", "FAILED"):
        raise VerdictParseError(
            "verdict must be 'PASSED' or 'FAILED' (or 'SKIPPED' to be "
            f"coerced to FAILED), got {verdict!r}"
        )
    return verdict


def parse_verdict_to_dataclass(raw_output: str) -> "Verdict":
    """JSON-string entry point returning a :class:`Verdict` dataclass.

    Kept as a distinct function from :func:`parse_verdict` because the
    two have different jobs: ``parse_verdict`` validates the status
    field, while this wrapper additionally enforces the richer contract
    the dataclass consumers expect (``evidence`` must be present, per
    PRD decision point 4).

    Internally: JSON-decode ``raw_output``, validate the status via
    :func:`parse_verdict`, then carry ``reasons`` / ``evidence`` /
    ``provider`` / ``chosen_model`` / ``model_complexity`` through.

    Raises:
        VerdictParseError: For malformed JSON, missing required
            fields, or invalid verdict values (see
            :func:`parse_verdict`).
    """
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError as e:
        raise VerdictParseError(f"Invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise VerdictParseError(
            f"top-level JSON must be an object, got {type(data).__name__}"
        )

    verdict_str = parse_verdict(data)

    raw_reasons = data.get("reasons", [])
    if not isinstance(raw_reasons, list):
        raise VerdictParseError(
            f"'reasons' must be a list, got {type(raw_reasons).__name__}"
        )

    if "evidence" not in data:
        raise VerdictParseError("missing required field: 'evidence'")
    evidence = data["evidence"]
    if not isinstance(evidence, list):
        raise VerdictParseError(
            f"'evidence' must be a list, got {type(evidence).__name__}"
        )

    return Verdict(
        verdict=verdict_str,
        reasons=list(raw_reasons),
        evidence=list(evidence),
        provider=str(data.get("provider", "")),
        chosen_model=str(data.get("chosen_model", "")),
        model_complexity=str(data.get("model_complexity", "")),
    )
