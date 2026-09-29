"""
Verification Orchestrator — Manages Verification Cycles and State Transitions
============================================================================

Core responsibilities:
- Drive the verification loop via VerificationAgent
- Apply 3-layer loop control: round limits, same-failure detection, user intervention
- Coordinate RepairTaskGenerator when verification fails
- Persist all state transitions through PlanState
"""

import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from coding_tool import CodingTool, ClaudeCodingTool
from config_paths import resolve_state_db_path
from plan_state import PlanState
from verification_agent import VerificationAgent
from repair_generator import RepairTaskGenerator


def _state_db_path_for_orchestrator() -> Path:
    """Resolve the backend state-machine SQLite path.

    Kept as a named function rather than inlined because ``server.py``
    must not be imported here (circular dep: server → this module).
    :func:`config_paths.resolve_state_db_path` has no such constraint —
    it imports only the standard library — so this now delegates to it
    instead of re-deriving the path locally, which is what every other
    caller does too.

    **Why delegation, and not the previous local derivation.** This
    function used to resolve its default from its own ``__file__``, and
    for a while used ``.parent.parent``: that reached
    ``<repo>/backend/state.db`` — a stale 40 KB database from
    2026-08-27 with no ``plan_tasks`` table. Every ``add_task`` call
    therefore wrote to a DB no other process reads, silently losing the
    RP-* repair rows that the executor's Phase 2 reconcile needs. A
    third ``.parent`` made it correct *on that day*; delegating makes it
    correct on the next move as well, and that is the property worth
    having.
    """
    return resolve_state_db_path()


class VerificationOrchestrator:
    """Manages the verification loop and state transitions."""

    def __init__(
        self,
        plan_dir: Path,
        project_dir: Path,
        coding_tool: Optional[CodingTool] = None,
        max_parallel: int = 1,
        verif_repo: Optional[Any] = None,
    ):
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)
        self.plan_state = PlanState(self.plan_dir)
        self.coding_tool = coding_tool or ClaudeCodingTool()
        self.max_parallel = max_parallel
        self.verification_agent = VerificationAgent(
            self.plan_dir, self.project_dir, self.coding_tool,
            max_parallel=max_parallel,
            verif_repo=verif_repo,
        )
        self.repair_generator = RepairTaskGenerator(
            self.coding_tool, self.plan_dir, self.project_dir
        )
        # 2026-09-14: kept so the VP-split path can persist each parent's
        # SPLIT verdict into ``plan_verification.verdicts`` (the row a
        # resumed round reads to skip the superseded parent).
        self.verif_repo = verif_repo
        self._current_vp_splits: List[Dict[str, Any]] = []
        self._previous_failed_ids: Optional[set] = None
        self._consecutive_same_failure_rounds: int = 0
        # 2026-09-12: terminating the chain after 1
        # same-failure repeat (old behaviour) is too violent — but
        # giving the agent unlimited retries without feedback lets it
        # repeat the same failing approach. The right design:
        #
        # 1. Don't terminate on the first repeat. Bump counter and
        #    keep generating repair_tasks so the executor gets
        #    another shot.
        # 2. Inject previous-round failure evidence into the
        #    repair-task-generation prompt so the LLM agent sees
        #    "上次方案完全无效" and is steered away from repeating
        #    it. We accumulate feedback per-VP across rounds (full
        #    history, not just last round) so the agent can see
        #    what each previous attempt observed.
        # 3. Hard cap ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` as
        #    a safety net — enough chances for feedback to land, but
        #    not so many that a truly broken VP burns tokens forever.
        self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 1
        # 2026-09-19（**推翻** 2026-09-12 的"第一次重复不终止"）：两轮失败
        # 集合相同就停。
        #
        # 计数语义：`_consecutive_same_failure_rounds` 数的是**重复次数**，
        # 即"第 2 轮和第 1 轮相同"记 1、"第 3 轮又相同"记 2。所以阈值 1 =
        # **两轮失败集合相同就停**。
        #
        # 2026-09-12 当时的理由是"第一次重复就终止太暴力，真 bug 一轮修不
        # 完"；2026-09-19 实测推翻了它：某一轮的 round 2 与 round 3 的失败
        # 集合完全相同仍继续跑，白白多烧一整轮，而且真正的上限在服务端
        # auto-loop（`for round_num in range(start_round, max_rounds+1)`），
        # 所以阈值 3 在 `max_rounds=3` 下**数学上不可达**——这条停止条件从来
        # 没生效过，每次都以 `max_rounds_reached` 收场。
        #
        # 想再放宽的话，阈值必须同时满足 `<= max_rounds - 1`，否则又是死代码。
        # ``vp_id -> list of {round, actual_result, evidence}`` —
        # one entry per round where the VP failed. The list grows
        # monotonically and is cleared only when the VP passes.
        # 2026-09-14: the per-VP failure history
        # used to live ONLY in this instance's memory, but the
        # repair→execute→re-verify chain builds a FRESH orchestrator every
        # round (server.py ``_on_repair_complete``), so
        # ``previous_failure_feedback`` was always empty in production and
        # the "上次修复尝试未生效" steering never reached the repair
        # prompt. It is now loaded from
        # ``plans/{id}/verification_failure_history.json`` (see
        # ``verification.failure_history``), with a one-time seed from
        # state.db's accumulated verdicts so plans already mid-flight get
        # feedback without waiting a round.
        self._failure_history: Dict[str, List[Dict[str, Any]]] = (
            self._load_failure_history()
        )
        self._failure_history_seeded = False
        # 2026-09-19: ``_previous_failed_ids`` /
        # ``_consecutive_same_failure_rounds`` used to live ONLY in this
        # instance's memory — precisely the hole the paragraph above
        # describes for the failure history, and it was never closed for
        # these two. Because ``_on_repair_complete`` builds a FRESH
        # orchestrator for every post-repair round, both reset to
        # ``None`` / ``0`` at the start of each round, which puts
        # ``_previous_failed_ids`` at ``None`` on the comparison below —
        # the same-failure branch could never fire at all: consecutive
        # rounds fail the identical VP set and the loop still ends with
        # ``max_rounds_reached``.
        # Persisted next to the failure history so the stop decision
        # survives the rebuild.
        self._previous_failed_ids, self._consecutive_same_failure_rounds = (
            self._load_loop_tracking()
        )
        self._current_repair_tasks: List[Dict] = []
        self._waiting_for_user = False

    #: Loop-progress tracking that must outlive a single orchestrator (see
    #: the 2026-09-19 note in ``__init__``).
    _LOOP_TRACKING_FILENAME = "verification_loop_tracking.json"

    def _loop_tracking_path(self) -> Path:
        return Path(self.plan_dir) / self._LOOP_TRACKING_FILENAME

    def _load_loop_tracking(self) -> Tuple[Optional[set], int]:
        """Return ``(previous_failed_ids, consecutive_same_failure_rounds)``.

        Missing / unreadable / malformed file → ``(None, 0)``, i.e. the
        old in-memory behaviour. A file left over from a previous plan
        generation is not a correctness problem: the first comparison
        would simply be against a set of VP ids that no longer matches,
        which resets the counter instead of stopping the loop.

        ``verification/reset`` deletes this file, so an operator restart
        starts the counting from scratch rather than inheriting a
        verdict set from the run that was just discarded.
        """
        try:
            with self._loop_tracking_path().open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None, 0
        if not isinstance(payload, dict):
            return None, 0
        raw_ids = payload.get("previous_failed_ids")
        previous = (
            {str(i) for i in raw_ids} if isinstance(raw_ids, list) else None
        )
        raw_count = payload.get("consecutive_same_failure_rounds")
        count = (
            raw_count
            if isinstance(raw_count, int) and not isinstance(raw_count, bool)
            else 0
        )
        return previous, max(0, count)

    def _save_loop_tracking(self) -> None:
        """Persist the loop-progress tracking.

        Best-effort: a write failure must never abort a verification
        round — the worst case is that the next (fresh) orchestrator
        behaves as it did before 2026-09-19.
        """
        payload = {
            "previous_failed_ids": (
                sorted(self._previous_failed_ids)
                if self._previous_failed_ids is not None
                else None
            ),
            "consecutive_same_failure_rounds": (
                self._consecutive_same_failure_rounds
            ),
        }
        path = self._loop_tracking_path()
        try:
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            tmp.replace(path)
        except OSError:
            pass

    def _load_failure_history(self) -> Dict[str, List[Dict[str, Any]]]:
        """Per-VP failure history for this plan.

        Disk file (``verification_failure_history.json``) is
        authoritative once written — that is what makes the history
        survive the fresh orchestrator the auto-loop builds for every
        post-repair round. When the file is absent (a plan already
        mid-flight when this feature shipped), seed once from the
        accumulated ``plan_verification.verdicts`` list so the very next
        repair generation already knows how each VP failed before.
        """
        from verification import failure_history as _fh

        history = _fh.load(self.plan_dir)
        if history:
            return history
        verdicts = None
        try:
            if self.verif_repo is not None:
                summary = self.verif_repo.summary(self.plan_dir.name)
                if isinstance(summary, dict):
                    verdicts = summary.get("verdicts")
        except Exception:
            verdicts = None
        if not verdicts:
            return {}
        seeded = _fh.seed_from_verdicts(verdicts)
        if seeded:
            print(
                f"[Orchestrator] seeded failure history from verdicts for "
                f"{len(seeded)} VP(s)"
            )
        return seeded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_verification_cycle(self, round_number: int = 1, force: bool = False, resume: bool = False) -> dict:
        """
        Start a new verification cycle.

        Args:
            round_number: Current verification round number (default: 1)
            force: If True, always re-run verification even if a report exists.
            resume: If True, skip the verdict-cache clear so already-PASSED
                VPs (per the on-disk ``verification-executor sidecar``
                verdict map) are filtered out by
                :meth:`BaseExecutor.run`'s ``completed_set`` check. The
                default ``False`` keeps the historic "always start from
                a clean slate" behaviour that protects against stale
                verdicts from a previous ``verification_plan.json``.
        """
        # 2026-08-19 audit: the orchestrator's ``PlanState`` instance
        # is long-lived but its in-memory ``_state`` cache can drift
        # from the SQLite row whenever another writer (the ``/start``
        # handler, the auto-loop, ``begin_verification``) updates the
        # plan. Reload BEFORE every cycle so the cycle's
        # ``transition_to`` calls see the latest persisted phase and
        # do not raise ``Illegal transition`` (which the callers
        # silently swallow, leaving the plan stuck in
        # ``verification_running``).
        self.plan_state.reload()
        self._waiting_for_user = False
        current = self.plan_state.get_current_phase()
        if current in ("executing", "ready", "completed", "verification_repairing"):
            self.plan_state.transition_to("verification")
        # After a closed-out cycle (``verification_passed`` /
        # ``verification_failed`` / ``verification_loop_stopped`` /
        # the top-level ``failed`` / ``stopped`` / ``completed``
        # terminals) the orchestrator may want to re-run verification
        # from scratch — the user just confirmed a repair, hit
        # ``POST /api/verification/{id}/start`` after a previous run
        # crashed mid-round, or a manual force-verify is in flight.
        # ``transition_to`` refuses terminals by design (they are the
        # canonical end states), so we call ``force_set_phase`` to
        # drop back to ``verification`` and let the next line advance
        # into ``verification_running``.
        #
        # The security boundary is preserved at the plan_state layer
        # (no edge from ``verification_passed`` to ``completed``); the
        # orchestrator simply chooses to leave the terminal before
        # crossing that boundary. The dispatcher only treats a plan
        # as truly closed when the orchestrator writes a fresh
        # ``verification_passed`` afterwards; an intermediate
        # ``force_set_phase("verification")`` round-trip is fine.
        #
        # 2026-09-07 fix: previously only the three ``verification_*``
        # terminals were force_set back to ``verification``. A plan
        # whose previous run crashed mid-round and left
        # ``current_phase=failed`` (the general-purpose terminal, not
        # the verification-specific one) would then hit the direct
        # ``transition_to("verification_running")`` below and raise
        # ``Illegal transition from 'failed' to 'verification_running'``,
        # which the caller silently swallows and strands the plan.
        # Widening the catch-all to all terminal phases here is safe
        # because the routing CAS at the ``/start`` HTTP entry already
        # vetted that this call is a legitimate re-entry (source
        # stages include ``terminal_failed``, ``terminal_done``,
        # ``verification_idle``, ``verification_passed``,
        # ``verification_failed``, ``verification_loop_stopped``).
        if current in (
            "verification_passed",
            "verification_failed",
            "verification_loop_stopped",
            "completed",
            "failed",
            "stopped",
        ):
            self.plan_state.force_set_phase("verification")
        # verification_rerunning -> verification_running directly (skip verification phase)
        self.plan_state.transition_to("verification_running")

        # Short-circuit: if the plan has no actionable tasks, there is
        # nothing to verify. Returning a passed report immediately prevents
        # the verification pipeline from burning LLM tokens / hanging on VPs
        # that only make sense when real tasks exist. This keeps empty-task
        # boundary tests fast and deterministic.
        tasks_file = self.plan_dir / "tasks.json"
        if tasks_file.exists():
            try:
                tasks_data = json.loads(tasks_file.read_text(encoding="utf-8"))
                tasks = tasks_data.get("tasks") if isinstance(tasks_data, dict) else tasks_data
                if not tasks:
                    self.plan_state.verification_passed()
                    return {
                        "overall_status": "PASSED",
                        "verification_results": [],
                        "requirement_deviations": [],
                        "summary": "tasks.json is empty; no tasks to verify.",
                        "execution_profile": {"mode": "short_circuit_empty_tasks"},
                    }
            except Exception:
                # Corrupt tasks.json is not fatal; fall through to normal
                # verification so any issues are surfaced as verification failures.
                pass

        try:
            # NOTE: run_full_verification() doesn't accept a force kwarg in this version.
            # The "force" semantics is already handled at the API layer (manual-verification
            # loop always runs all 3 rounds when force=True). Drop the kwarg here.
            #
            # 2026-07-19: ``resume`` plumbs through to the agent which
            # then skips the "clear verdict cache" guard. That lets
            # :meth:`BaseExecutor.run` filter out VPs whose
            # ``_completed_items`` is already populated from the
            # verdict cache (see base_executor.py:344). The default
            # False preserves the historic "always re-run" safety
            # net for first-time starts; the orchestrator flips it to
            # True when resuming a mid-run cycle so we don't burn
            # LLM tokens re-running VPs whose verdicts are already
            # cached on disk.
            #
            # 2026-09-08: snapshot the previous round's
            # ``verification_results`` into ``rounds[N-1]`` and clear
            # the top-level ``verification_results`` so the Feishu
            # card doesn't show stale FAILED entries from a prior
            # round while round N is running. This is the single-line
            # fix for the VP-034 stale-data bug (round 1 showed
            # round 0's FAILED entry in the card even though round 1
            # was actively re-running VP-034).
            from verification.verification_report_reader import (
                snapshot_round_results,
                clear_round_results,
            )
            report_path = self.plan_dir / "verification_report.json"
            if report_path.exists() and not resume:
                try:
                    snapshot_round_results(report_path, round_number)
                    clear_round_results(report_path)
                except Exception as snapshot_exc:
                    # Best-effort — never crash round start over a
                    # snapshot/clear failure. The card may show stale
                    # entries but the round still runs.
                    if self.logger:
                        self.logger.warning(
                            "round_start_snapshot_failed",
                            f"could not snapshot/clear verification_results "
                            f"at round {round_number} start: {snapshot_exc}",
                        )

            report = self.verification_agent.run_full_verification(
                round_number, resume=resume
            )
            return report
        except Exception:
            self.plan_state.verification_failed()
            raise

    def check_cycle_conditions(self, report: dict, round_number: int = 1) -> dict:
        """
        Evaluate the verification report and decide the next step.

        Returns a dict with keys:
        - should_continue: bool
        - should_stop: bool
        - stop_reason: Optional[str]
        - repair_tasks: List[Dict]
        - waiting_for_user: bool
        - status: str   # passed | loop_stopped | verification_failed
        """
        overall_status = report.get("overall_status", "FAILED")

        # 2026-08-19 audit: reload BEFORE classifying the cycle so the
        # subsequent ``transition_to`` reflects the latest SQLite state.
        # Without this, a Phase 3 LLM verdict that takes >30s can race
        # with a parallel writer and the orchestrator's cached
        # ``current_phase`` is stale, causing ``transition_to`` to
        # raise ``Illegal transition`` that the caller silently
        # swallows — the plan then stays in ``verification_running``
        # even though the verdict was ``PASSED``.
        self.plan_state.reload()

        # Count actual failures (excluding SKIPPED/manual_check)
        failed_count = sum(
            1 for r in report.get("verification_results", [])
            if r.get("status") == "FAILED"
        )
        deviation_count = len(report.get("requirement_deviations", []))

        # PASSED or PARTIAL with no real failures → treat as passed
        if overall_status == "PASSED" or (overall_status == "PARTIAL" and failed_count == 0 and deviation_count == 0):
            self._previous_failed_ids = None
            self._consecutive_same_failure_rounds = 0
            self._save_loop_tracking()
            # All failures resolved — drop the per-VP feedback history
            # so the next plan (if this orchestrator is reused) starts
            # with a clean slate. Persist the empty state too: a later
            # post-repair round builds a fresh orchestrator that would
            # otherwise resurrect the pre-pass history from disk.
            self._failure_history.clear()
            try:
                from verification import failure_history as _fh_clear
                _fh_clear.save(self.plan_dir, {})
            except Exception:
                pass
            self.plan_state.verification_passed()
            return {
                "should_continue": False,
                "should_stop": False,
                "stop_reason": None,
                "repair_tasks": [],
                "waiting_for_user": False,
                "status": "passed",
            }

        # --- Failure path ---
        current_failed_ids = self._extract_failed_ids(report)

        # (2) Same-failure detection — 2026-09-12 fix: don't terminate
        # the chain on the first repeat. Real bugs (e.g. VP-006
        # needs an actual code change to remove VP-013 instrumentation;
        # VP-023 needs docker compose setup; VP-027 needs coverage work)
        # can't be fixed in one round. The user explicitly demanded:
        # "make the chain run — if VP fails, generate repair tasks,
        # route to executor, keep iterating".
        #
        # Old behaviour: same failures on two consecutive rounds →
        # return empty repair_tasks → auto-loop records
        # ``no_repair_tasks`` terminal → chain dies (this is the bug
        # we are fixing right now).
        #
        # New behaviour: bump ``_consecutive_same_failure_rounds``;
        # only terminate after ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS``
        # rounds with the same failure set AND no executor pass
        # intervened. Otherwise still generate repair_tasks so the
        # executor gets another shot.
        if (
            self._previous_failed_ids is not None
            and current_failed_ids == self._previous_failed_ids
            and len(current_failed_ids) > 0
        ):
            self._consecutive_same_failure_rounds += 1
            if (
                self._consecutive_same_failure_rounds
                >= self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS
            ):
                # Genuinely stuck — fall through to terminate.
                self._safe_phase_call("verification_failed", "verification_failed")
                self.plan_state.stop_verification_loop(
                    "same_failure_repeated_after_max_attempts"
                )
                return {
                    "should_continue": False,
                    "should_stop": True,
                    "stop_reason": "same_failure_repeated_after_max_attempts",
                    "repair_tasks": [],
                    "waiting_for_user": False,
                    "status": "loop_stopped",
                }
            # Not yet at the cap — log and continue, generating
            # repair tasks again so the executor can try once more.
            _logger = getattr(self, "logger", None)
            if _logger is not None:
                try:
                    _logger.warning(
                        "verification_same_failures_persisting",
                        f"Same failures persisted for "
                        f"{self._consecutive_same_failure_rounds}/"
                        f"{self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS} rounds — "
                        f"regenerating repair tasks",
                        data={
                            "plan_id": getattr(self.plan_dir, "name", "?"),
                            "failed_vps": sorted(current_failed_ids),
                            "consecutive_rounds": self._consecutive_same_failure_rounds,
                            "max_consecutive_rounds": self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS,
                        },
                    )
                except Exception:
                    pass
        else:
            # Different (or first) failure set — reset counter.
            self._consecutive_same_failure_rounds = 0

        # (1) Round limit — soft only. Verification must keep
        # iterating until all failures are fixed (or same-failure-repeated
        # termination above fires). max_rounds is now a *soft* limit used
        # only for logging/observability, not for stopping the loop.
        current_round = self.plan_state.get_verification_round()
        max_rounds = self.plan_state.get_verification_max_rounds()
        if current_round >= max_rounds:
            # Soft cap: log a warning but keep going. Only same-failure-repeated
            # above (and explicit user_stop_verification) actually terminate.
            print(
                f"[Verification] WARNING: reached max_rounds={max_rounds}, "
                f"but continuing — all failures must be fixed)."
            )

        # Can continue — enter repair phase and generate repair tasks
        # for the executor to attempt a fix.
        #
        # 2026-09-14 (live post-mortem, round 3): this call used to be
        # FATAL. The watchdog had already force-stamped the plan to
        # ``failed`` (a false ``verification_log_stale``), so
        # ``transition_to("verification_failed")`` raised
        # ``Illegal transition from 'failed' to 'verification_failed'`` —
        # the exception unwound the whole auto-loop, and a round whose
        # judgment had COMPLETED (report written, 5 failed VPs) produced
        # no repair tasks and no split decision at all. Phase bookkeeping
        # must never be able to destroy the round's actual work, so it is
        # now best-effort with a force-set fallback.
        self._safe_phase_call("verification_failed", "verification_failed")
        self._previous_failed_ids = current_failed_ids
        # 2026-09-19: record the round's failure set (and the repeat
        # counter that was bumped / reset just above) on disk. This is
        # the only write the comparison at the top of the next round
        # reads, and the next round runs in a FRESH orchestrator — see
        # the ``__init__`` note.
        self._save_loop_tracking()

        # 2026-09-12: track failure history per
        # VP across rounds. When the same VP fails again, the next
        # round's repair-task-generation prompt includes the previous
        # attempts' actual_result + evidence so the LLM agent knows
        # "上次方案完全无效" and is steered away from repeating it.
        # A hard "auto-cap at 3 rounds" was rejected in favour of this —
        # feedback gives the agent more information to learn from, while
        # still capping the chain so a truly stuck situation eventually
        # terminates.
        round_failed_vps = self._extract_failed_vps_with_evidence(report)
        from verification import failure_history as _failure_history_mod

        _failure_history_mod.merge_round(
            self._failure_history, round_failed_vps, round_number,
        )
        # Drop history entries for VPs that have now passed (so the
        # next round's prompt isn't polluted with stale failures), then
        # persist so a freshly-constructed orchestrator (the auto-loop
        # builds one per post-repair round) reads the same history back.
        _failure_history_mod.prune(self._failure_history, current_failed_ids)
        _failure_history_mod.save(self.plan_dir, self._failure_history)

        # Build feedback dict: vp_id -> previous attempts (excluding
        # the current round). Empty dict on first occurrence.
        previous_failure_feedback: Dict[str, List[Dict[str, Any]]] = {}
        for vp_id, history in self._failure_history.items():
            prior = [h for h in history if h.get("round") != round_number]
            if prior:
                previous_failure_feedback[vp_id] = prior

        # Phase bookkeeping only — see ``_safe_phase_call`` (2026-09-14:
        # a watchdog force-stamp must not be able to abort the repair /
        # split decision).
        self._safe_phase_call("start_verification_repair", "verification_repairing")
        # 2026-09-07: switch from the legacy three-step LLM chain
        # (``generate_verification_tasks`` →
        # ``understand_requirements`` + ``collect_failure_evidence`` +
        # ``_generate_repair_tasks``) to the single-call content flow
        # + :class:`RepairTaskAssembler` split. The legacy chain
        # produced zero tasks for any plan whose evidence didn't
        # perfectly align with PRD criteria, leaving the auto-loop
        # stuck in ``verification_failed`` with no path forward. The
        # new flow reads ``verification_report.json`` directly via
        # :func:`extract_failed_vps_with_paths` and stamps
        # ``id / priority / depends_on`` locally so the state machine
        # can recognise the tasks regardless of LLM behaviour.
        #
        # 2026-09-13: switched from ``extract_failed_vps_from_report`` to
        # the path-based sibling. The inlining reader truncated each VP's
        # ``actual_result`` / ``evidence`` to 1000 chars — for a VP whose
        # whole point is "which of the 245 failing tests", that dropped
        # exactly the actionable part. The path-based shape carries a
        # bounded summary plus the file paths the repair agent Reads on
        # demand, so the prompt stays bounded and nothing is lost.
        from verification.verification_report_reader import (
            extract_failed_vps_with_paths,
            extract_vp_test_commands,
        )
        from repair_generator import RepairGenerationError, RepairTaskAssembler

        failed_vps = extract_failed_vps_with_paths(
            self.plan_dir / "verification_report.json",
            self.plan_dir / "verification_plan.json",
        )
        # 2026-09-14 — repair vs split 判断.
        #
        # 修复任务生成器同时充当判断者：对一个失败的 VP，决定是生成
        # 新的修复任务，还是拆分它。有些失败并不是产品缺陷，只是当前
        # 的 VP 太大 —— 容易超时，拆开就好。
        #
        # 在生成修复内容之前先分流: 判 split 的失败 VP 不生成修复任务，
        # 而是拆成子 VP 写入 verification_plan.json + SPLIT verdict 写
        # 入 state.db，下一轮验证跑子 VP。判 repair 的走原有流程。
        # 任何异常都回退"全部走 repair" —— 拆分是优化路径，绝不能成为
        # 新的搁浅原因。
        repair_candidates = failed_vps
        vp_splits: List[Dict[str, Any]] = []
        # ``_apply_vp_split_decisions`` never raises — any failure inside
        # the judge/splitter degrades to "repair everything", so the
        # split path can't become a new stranding cause.
        repair_candidates, vp_splits = self._apply_vp_split_decisions(
            failed_vps, round_number,
        )

        # 2026-09-14 — a FAILED generation must not masquerade as "no
        # repair tasks needed". ``generate_repair_contents`` now raises
        # ``RepairGenerationError`` when the LLM call dies or returns
        # nothing usable while failures are pending; we catch it here
        # and surface it on the result payload so the auto-loop parks
        # the plan resumable instead of recording a terminal
        # ``no_repair_tasks`` verdict over real failures.
        repair_generation_error: Optional[str] = None
        repair_contents: List[Dict[str, Any]] = []
        # 2026-09-14: 上一轮为该 VP 生成的修复任务**执行后**是什么结果
        # (状态/命令/失败原因) 也要进 prompt —— 只看验证报告只能说
        # "这个 VP 又失败了", 说不了"上轮那个修复任务跑了什么、为什么
        # 没生效"。数据在 state.db 的 plan_tasks (修复任务不在
        # tasks.json 里)。
        repair_outcomes: Dict[str, Dict[str, Any]] = {}
        try:
            from verification.failure_history import load_repair_outcomes
            repair_outcomes = load_repair_outcomes(
                _state_db_path_for_orchestrator(), self.plan_dir.name,
            )
        except Exception as outcomes_exc:  # noqa: BLE001
            print(
                f"[Repair] round {round_number}: repair-outcome lookup "
                f"skipped ({outcomes_exc})"
            )
            repair_outcomes = {}
        if repair_candidates:
            try:
                repair_contents = self.repair_generator.generate_repair_contents(
                    repair_candidates, round_number,
                    previous_failure_feedback=previous_failure_feedback,
                    repair_outcomes=repair_outcomes,
                )
            except RepairGenerationError as gen_exc:
                repair_generation_error = str(gen_exc)
                print(
                    f"[Repair] round {round_number}: repair-task generation "
                    f"FAILED ({len(repair_candidates)} failed VP(s) pending) — "
                    f"{gen_exc}"
                )
        else:
            # Every failure was routed to a split — there is nothing for
            # the repair LLM to do. Skipping the call (instead of asking
            # for an empty list) keeps the "no repair tasks" outcome
            # unambiguous for the auto-loop's split branch.
            print(
                f"[Repair] round {round_number}: all {len(failed_vps)} "
                f"failed VP(s) routed to split — no repair tasks generated"
            )
        # 2026-09-14: the repair task's ``test_command`` must be the failed
        # VP's *raw* command from ``verification_plan.json``. The path-based
        # reader above deliberately returns only a bounded summary + a
        # JSON-pointer path, so the runnable string has to be read
        # separately and handed to the assembler. Without it every repair
        # task reached the executor with no command at all and the
        # dual-criterion completion rule silently fell back to the AI's
        # own claim (``test_cross_verify_unverified``).
        _plan_file = self.plan_dir / "verification_plan.json"
        # 2026-09-20 (post-mortem): repair ids must be unique for the
        # life of the plan, not merely within the batch. A counter reset
        # hands the plan a fresh round 1, so the next batch reproduced
        # ``repair-r1-01`` … verbatim and ``plan_tasks`` — keyed on
        # ``(plan_id, task_id)`` — overwrote the previous batch's rows
        # instead of adding to them. Three batches in that run all numbered
        # from ``repair-r1-01``; ``repair-r1-06``'s 2026-09-18 breakdown
        # children are still on disk pointing at the 2026-09-20 task that
        # took its id.
        #
        # So: read what has already been allocated for this (plan, round)
        # from both stores, start above it, and report it if the two
        # stores disagree about what an existing id means.
        seq_base = 1
        try:
            from repair_generator import (
                cross_store_task_conflicts,
                next_repair_seq_base,
            )
            from verification.failure_history import load_task_titles

            # ``getattr``, not ``self.logger``: the attribute is not set in
            # ``__init__``, and an AttributeError here would be swallowed by
            # the ``except`` below — silently reverting to seq_base=1, i.e.
            # to the bug this whole block exists to fix.
            logger = getattr(self, "logger", None)
            db_titles = load_task_titles(
                _state_db_path_for_orchestrator(), self.plan_dir.name,
            )
            disk_titles = self._load_disk_task_titles()
            conflicts = cross_store_task_conflicts(disk_titles, db_titles)
            if conflicts:
                sample = list(conflicts.items())[:3]
                print(
                    f"[Repair] round {round_number}: {len(conflicts)} task "
                    f"id(s) disagree between tasks.json and plan_tasks — "
                    f"{sample}"
                )
                if logger is not None:
                    logger.warning(
                        "repair_task_id_store_conflict",
                        f"{len(conflicts)} task id(s) describe different "
                        f"tasks in tasks.json vs plan_tasks "
                        f"(round {round_number})",
                        data={"conflicts": {k: list(v) for k, v in sample}},
                    )
            seq_base = next_repair_seq_base(
                set(disk_titles) | set(db_titles), round_number,
            )
            if seq_base > 1:
                print(
                    f"[Repair] round {round_number}: {len(db_titles)} prior "
                    f"task row(s) already allocated; starting at seq "
                    f"{seq_base} so no existing id is reused"
                )
        except Exception as id_exc:  # noqa: BLE001
            # Id allocation is an integrity improvement, never a
            # precondition for the repair round. Falling back to seq_base
            # = 1 reproduces the pre-2026-09-20 behaviour exactly.
            print(
                f"[Repair] round {round_number}: repair-id allocation "
                f"check skipped ({id_exc})"
            )
            seq_base = 1
        repair_tasks = RepairTaskAssembler(
            round_number=round_number,
            failed_vps=failed_vps,
            vp_test_commands=extract_vp_test_commands(_plan_file),
            plan_path=str(_plan_file),
            seq_base=seq_base,
        ).assemble(repair_contents)
        self._current_repair_tasks = repair_tasks
        # 2026-09-08: single-writer refactor (mirrors the refiner
        # cleanup in agent.py). Previously the auto-loop wrote a
        # ``verification_tasks_round_N.json`` snapshot and the executor
        # spawned by ``_run_repair_execution`` merged it into a sibling
        # ``tasks_with_repair_round_N.json`` via ``_merge_repair_tasks_into_plan``
        # — a second writer that violated ``tasks.json`` being the only
        # canonical seed. The 2026-09-08 single-writer path writes the
        # repair tasks straight to ``state.db`` via
        # :class:`PlanTaskRepository.add_task`; the executor's
        # :meth:`AutonomousAgent._load_tasks` Phase-2 reconcile step then
        # picks them up as state.db orphans and injects them into the
        # DAG without ever touching ``tasks.json``.
        #
        # This block is best-effort: a SQLite error here is logged and
        # the orchestrator still returns ``repair_tasks`` for any caller
        # that wants to inspect them in-memory. The
        # ``_run_auto_verification_loop`` server-side branch reads
        # ``self._current_repair_tasks`` so the legacy in-memory contract
        # is preserved.
        #
        # Note: ``RepairTaskAssembler.assemble`` stamps ``status='pending'``
        # on every output dict. ``add_task`` rejects ``status`` (it is a
        # runtime mirror field owned by ``update_task``), so we strip it
        # before calling — ``add_task`` defaults the new entry to
        # ``status='pending'`` internally, so the end state is the same
        # with a single source of truth.
        if repair_tasks:
            try:
                from state_machine.db.connection import open as _open_db
                from state_machine.db.schema import migrate as _migrate
                from state_machine.repositories.plan_task_repository import (
                    PlanTaskRepository,
                )
                plan_id = self.plan_dir.name
                conn = _open_db(_state_db_path_for_orchestrator())
                try:
                    _migrate(conn)
                    repo = PlanTaskRepository(conn)
                    written = 0
                    for rt in repair_tasks:
                        if not isinstance(rt, dict):
                            continue
                        if not rt.get("id"):
                            continue
                        # ``add_task`` filters keys through
                        # ``ALLOWED_STATIC_TASK_FIELDS``. The repair
                        # fields (task_group / execution_group /
                        # priority / acceptance_criteria / failed_vp_id
                        # / round) are all on the allow-list as of
                        # 2026-09-08, so they pass through verbatim.
                        #
                        # ``status`` is NOT on the allow-list — it
                        # belongs to ``update_task``'s runtime mirror.
                        # :class:`RepairTaskAssembler.assemble` stamps
                        # ``status='pending'`` on every output dict,
                        # which would cause ``add_task`` to raise
                        # ``TaskProgressValidationError``. Strip the
                        # key here so ``add_task`` can apply its own
                        # default (also ``status='pending'`` — same
                        # outcome, single source of truth).
                        static_payload = {
                            k: v for k, v in rt.items() if k != "status"
                        }
                        repo.add_task(plan_id, static_payload)
                        written += 1
                    _logger = getattr(self, "logger", None)
                    if _logger is not None:
                        try:
                            _logger.info(
                                "verification_repair_tasks_persisted",
                                f"Wrote {written} repair tasks to state.db "
                                f"for round {round_number}",
                                data={
                                    "plan_id": plan_id,
                                    "round": round_number,
                                    "written": written,
                                    "task_ids": [
                                        rt.get("id") for rt in repair_tasks
                                        if isinstance(rt, dict)
                                    ],
                                },
                            )
                        except Exception:
                            pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception as persist_exc:
                # Non-fatal: the in-memory ``repair_tasks`` list is
                # still returned for any caller that consumes it.
                # ``_run_repair_execution`` no longer reads
                # ``verification_tasks_round_N.json`` so a state.db
                # write failure leaves no disk artifact. Operators
                # can recover by re-triggering ``check_cycle_conditions``
                # (the next round will re-emit and re-attempt the write).
                _logger = getattr(self, "logger", None)
                if _logger is not None:
                    try:
                        _logger.warning(
                            "verification_repair_tasks_persist_failed",
                            f"Could not persist repair tasks to state.db "
                            f"for round {round_number}: {persist_exc}",
                            data={
                                "plan_id": self.plan_dir.name,
                                "round": round_number,
                                "task_count": len(repair_tasks),
                                "error": str(persist_exc)[:500],
                            },
                        )
                    except Exception:
                        pass
        # The auto-loop never reads ``waiting_for_user`` (verified
        # by grep — only tests assert on it). Keep the field
        # contract intact for backwards compat but default to False
        # so any future dashboard reader doesn't wait on a flag the
        # orchestrator's auto-confirm path will never raise.
        self._waiting_for_user = False

        return {
            "should_continue": False,
            "should_stop": False,
            "stop_reason": None,
            "repair_tasks": repair_tasks,
            # 2026-09-14 — non-None when repair-task generation itself
            # failed (LLM error / unusable reply) with failures pending.
            # Consumers must treat this as "the generator broke", NOT as
            # "the chain converged": ``repair_tasks`` is empty in both
            # cases, but only the latter justifies a terminal
            # ``no_repair_tasks`` verdict.
            "repair_generation_error": repair_generation_error,
            # 2026-09-14 — 本轮因"VP 过大"而被拆分的父 VP 记录。每条:
            # ``{vp_id, child_vp_ids, hint, reason, persisted}``。
            # 非空表示下一轮验证要跑子 VP（父 VP 已 superseded），
            # 且本轮可能因此没有修复任务可执行 —— auto-loop 依据它
            # 跳过执行器、直接进入下一轮验证。
            "vp_splits": vp_splits,
            "waiting_for_user": False,
            "status": "verification_failed",
        }

    def _load_disk_task_titles(self) -> Dict[str, str]:
        """``task_id -> title`` from this plan's ``tasks.json``.

        Half of the cross-store consistency check (the other half is
        ``plan_tasks`` via ``verification.failure_history.load_task_titles``).
        Repair ids get folded into ``tasks.json`` by the executor's
        load-time reconcile, so the same id can appear in both stores —
        and after an id collision it shows up describing two different
        tasks.

        Accepts both the ``{"tasks": [...]}`` envelope and a bare list,
        matching how the rest of the codebase reads this file. Returns
        ``{}`` when the file is missing or unreadable — a malformed
        ``tasks.json`` is the executor's problem to report, not a reason
        to abort repair generation.
        """
        path = self.plan_dir / "tasks.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(payload, dict):
            tasks = payload.get("tasks") or []
        else:
            tasks = payload
        if not isinstance(tasks, list):
            return {}
        titles: Dict[str, str] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            titles[task_id] = str(task.get("title") or "")
        return titles

    def _apply_vp_split_decisions(
        self, failed_vps: List[Dict[str, Any]], round_number: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split the failed VPs that the judge routes to ``split``.

        Never raises: an exception anywhere in the judge / splitter /
        persistence degrades to ``(failed_vps, [])`` — "repair
        everything", the pre-2026-09-14 behaviour. Splitting is an
        optimisation; it must not be able to strand a round.
        """
        try:
            return self._judge_and_split(failed_vps, round_number)
        except Exception as exc:  # noqa: BLE001 — conservative fallback
            print(
                f"[Repair] round {round_number}: vp-split judging failed, "
                f"falling back to repair for all {len(failed_vps)} VP(s): "
                f"{exc}"
            )
            return list(failed_vps), []

    def _safe_phase_call(self, method_name: str, force_phase: str = "") -> None:
        """Call a ``PlanState`` phase helper without letting a phase
        conflict abort the round's real work.

        2026-09-14 (live post-mortem, round 3): phase transitions are
        *bookkeeping* — the round's actual products are the report, the
        repair tasks and the split decisions. When the watchdog had
        force-stamped the plan to ``failed`` mid-round, the transition to
        ``verification_failed`` raised ``Illegal transition`` and the
        exception unwound the auto-loop, discarding a completed
        judgment. Any phase conflict here is now logged and
        force-corrected instead of propagated.
        """
        try:
            getattr(self.plan_state, method_name)()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[Verification] phase bookkeeping {method_name}() failed "
                f"({exc}) — forcing phase={force_phase or method_name} and "
                f"continuing"
            )
            if force_phase:
                try:
                    self.plan_state.force_set_phase(force_phase)
                except Exception:
                    pass

    def _judge_and_split(
        self, failed_vps: List[Dict[str, Any]], round_number: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split the failed VPs that the judge routes to ``split``.

        Returns ``(repair_candidates, vp_splits)``:

          * ``repair_candidates`` — the original ``failed_vps`` entries
            that still need a repair task (every entry, when nothing was
            split);
          * ``vp_splits`` — one record per SPLIT parent:
            ``{vp_id, child_vp_ids, hint, reason, persisted}``.

        The split itself is two writes: children + ``superseded_by`` in
        ``verification_plan.json`` (so the next round's VP universe
        contains the children and not the parent) and the parent's
        ``SPLIT`` verdict in state.db (so a resumed round skips the
        parent even when it re-reads a stale plan). A parent whose split
        is declined by the splitter (too few groups, unreadable plan)
        stays in the repair set — declining is always safe.
        """
        from vp_split import ACTION_SPLIT, VpSplitJudge, VpSplitter

        judge = VpSplitJudge(self.coding_tool, self.plan_dir)
        candidates: List[Dict[str, Any]] = []
        originals: Dict[str, Dict[str, Any]] = {}
        for vp in failed_vps:
            vp_id = str(vp.get("id", "") or "")
            if not vp_id:
                continue
            entry = judge.plan_entry(vp_id) or {}
            originals[vp_id] = vp
            candidates.append({
                **vp,
                # The report reader's failed-VP dicts carry bounded
                # summaries, not the raw method/command — both live in
                # the plan entry (the judge's gate reads them).
                "method": entry.get("verification_method")
                or vp.get("method")
                or vp.get("verification_method")
                or "",
                "test_command": entry.get("test_command") or "",
                # 2026-09-14: how many times this VP has failed BEFORE this
                # round, from the same persisted history the repair prompt
                # uses. A VP that keeps failing the same way is evidence
                # it is too big (or too entangled) to fix in one piece —
                # a signal the repair-vs-split judge scores.
                "prior_failure_rounds": len([
                    h for h in self._failure_history.get(vp_id, [])
                    if h.get("round") != round_number
                ]),
            })
        if not candidates:
            return list(failed_vps), []

        decisions = judge.decide(candidates)
        splitter = VpSplitter(self.plan_dir, self.project_dir)
        repair_candidates: List[Dict[str, Any]] = []
        vp_splits: List[Dict[str, Any]] = []
        for candidate in candidates:
            vp_id = str(candidate.get("id"))
            decision = decisions.get(vp_id) or {}
            if decision.get("action") != ACTION_SPLIT:
                repair_candidates.append(originals[vp_id])
                continue
            reason = str(decision.get("reason", "") or "")
            hint = str(decision.get("split_hint", "") or "by_directory")
            children = splitter.split(vp_id, hint, reason=reason)
            if not children:
                # Declined (no useful grouping / unreadable plan) — the
                # VP still needs a repair task.
                repair_candidates.append(originals[vp_id])
                continue
            child_ids = [c["id"] for c in children]
            persisted = VpSplitter.persist_split(
                self.verif_repo, self.plan_dir.name, vp_id, children,
                reason=reason,
            )
            print(
                f"[Repair] round {round_number}: split {vp_id} into "
                f"{len(child_ids)} sub-VP(s) ({', '.join(child_ids)}) — "
                f"hint={hint} db_persisted={persisted}"
            )
            vp_splits.append({
                "vp_id": vp_id,
                "child_vp_ids": child_ids,
                "hint": hint,
                "reason": reason,
                "persisted": persisted,
            })
        self._current_vp_splits = vp_splits
        return repair_candidates, vp_splits

    def confirm_repair_and_rerun(self):
        """User has confirmed repair tasks — advance to ``executing`` phase.

        2026-09-07 fix: ``_waiting_for_user=False`` is now stamped by
        :meth:`check_cycle_conditions` because the auto-loop never reads
        it (and historically the field was a no-op signal that confused
        manual dashboards). But ``confirm_repair_and_rerun`` historically
        gated the ``start_verification_rerun`` call on
        ``self._waiting_for_user`` being True — with that flag now
        False, this method would return early and the plan's phase
        would stay stuck in ``verification_repairing``. Without the
        ``verification_repairing → verification_rerunning`` transition
        the auto-loop's later call to
        ``ps.stop_verification_loop(\"repair_execution_failed\")`` would
        raise ``Illegal transition from 'verification_repairing' to
        'verification_loop_stopped'`` (or — depending on whether the
        executor subprocess flipped the phase to ``executing`` first —
        ``Illegal transition from 'executing' to
        'verification_loop_stopped'``). The auto-loop's blanket
        ``except Exception`` swallowed that error and the plan was
        stranded in ``verification_repairing`` until the watchdog
        noticed.

        The fix: treat this method as the unconditional advance
        trigger. ``check_cycle_conditions`` has already produced the
        repair tasks, so the next round can begin regardless of the
        manual-confirm flag's value.

        2026-09-17: the sentence that used to be here credited the
        manual ``/api/verification/{id}/confirm-repairs`` endpoint as a
        second caller of this path. That endpoint was removed — it never
        called this method, only flipped in-memory flags and left the
        round unstarted (so ``/start`` answered 409 "already running"
        forever after). The auto-loop is the only caller.

        2026-09-12 (state machine closed-loop fix): replace
        ``start_verification_rerun()`` (which routes
        ``verification_repairing → verification_rerunning`` and stays
        inside the verification sub-machine) with
        ``start_repair_execution()`` which routes
        ``verification_repairing → executing``. The executor
        subprocess spawned by ``_run_repair_execution_async`` IS
        genuinely running tasks — the state vocabulary should reflect
        that. The corresponding edge is already declared in
        ``VERIFICATION_PHASE_TRANSITIONS`` at plan_state.py:177, so
        this method is a pure re-routing of the existing legal edge.
        """
        self._waiting_for_user = False
        self.plan_state.start_repair_execution()

    def stop_verification(self):
        """Handle a manual stop request from the user."""
        self._waiting_for_user = False
        current = self.plan_state.get_current_phase()
        if current in ("executing", "ready"):
            self.plan_state.transition_to("verification")
        elif current == "verification_running":
            self.plan_state.verification_failed()
        self.plan_state.stop_verification_loop("user_stopped")

    def is_waiting_for_user(self) -> bool:
        return self._waiting_for_user

    def get_current_repair_tasks(self) -> List[Dict]:
        return list(self._current_repair_tasks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_failed_ids(self, report: dict) -> set:
        """Collect all failed verification-point IDs from the report.

        2026-09-08: include ``timeout`` and ``SPLIT`` statuses in
        the failed set so the auto-loop routes them to repair instead
        of silently dropping them. Previously only ``FAILED`` was
        collected, which meant hard-timeout-split VPs disappeared
        from the repair pipeline.

        2026-09-12: the previous code only read the top-level ``verification_results``
        array. After ``snapshot_round_results`` + ``clear_round_results``
        runs at round start, the top-level array is emptied and the data
        lives in ``rounds[N-1].results``. When the new round ends with
        FAILED VPs the orchestrator may have already snapshotted the
        previous round's results — without falling back to
        ``rounds[-1].results`` the failed-IDs set is empty and the
        same-failure-repeated branch never fires — a stuck terminal can
        show FAILED VPs in ``rounds[0]`` while reporting
        ``no_repair_tasks``.

        Fall back to ``rounds[-1].results`` when ``verification_results``
        is empty so the same-failure detection still works.
        """
        failed_ids: set = set()
        # Union of all result lists we should inspect: top-level + last
        # round snapshot. ``failed_ids`` is a set so dedupe is automatic
        # when the same VP appears in both (during the brief window
        # between generate_verification_report writing top-level and
        # clear_round_results running).
        results_lists: List[List[Dict[str, Any]]] = []
        top_level = report.get("verification_results") or []
        if top_level:
            results_lists.append(top_level)
        rounds_field = report.get("rounds") or []
        if isinstance(rounds_field, list) and rounds_field:
            last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
            last_results = last_round.get("results") or []
            if isinstance(last_results, list) and last_results:
                # Always include the last round snapshot — dedupe happens
                # via the failed_ids set below, so even when top-level
                # and rounds[-1].results share VPs the same-failure
                # detection still works.
                results_lists.append(last_results)

        for results_list in results_lists:
            for r in results_list:
                if not isinstance(r, dict):
                    continue
                # FAILED is the legacy failure; timeout / SPLIT are the
                # 2026-09-08 hard-timeout-auto-split additions.
                if r.get("status") in ("FAILED", "timeout", "SPLIT", "hard_timeout"):
                    vp_id = r.get("id", "")
                    if vp_id:
                        failed_ids.add(vp_id)
        for d in report.get("requirement_deviations", []):
            failed_ids.add(d.get("verification_point_id", ""))
        return {fid for fid in failed_ids if fid}

    def _extract_failed_vps_with_evidence(self, report: dict) -> Dict[str, Dict[str, Any]]:
        """Like :meth:`_extract_failed_ids` but returns the full
        ``actual_result`` / ``evidence`` text per VP.

        2026-09-12: giving the agent unlimited retries without feedback
        lets it repeat the same failing approach. We collect each round's
        actual evidence so the next round's repair-task-generation
        prompt can show "上次方案完全无效" with the prior round's
        actual_result / evidence verbatim.

        Returns ``{vp_id: {"actual_result": str, "evidence": str}}``.
        When a VP appears in multiple result lists (top-level +
        rounds[-1]), the FIRST occurrence wins so the freshest
        verdict is preferred (top-level is appended first by the
        verifier before snapshot+clear runs).
        """
        out: Dict[str, Dict[str, Any]] = {}
        results_lists: List[List[Dict[str, Any]]] = []
        top_level = report.get("verification_results") or []
        if top_level:
            results_lists.append(top_level)
        rounds_field = report.get("rounds") or []
        if isinstance(rounds_field, list) and rounds_field:
            last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
            last_results = last_round.get("results") or []
            if isinstance(last_results, list) and last_results:
                results_lists.append(last_results)
        for results_list in results_lists:
            for r in results_list:
                if not isinstance(r, dict):
                    continue
                if r.get("status") not in ("FAILED", "timeout", "SPLIT", "hard_timeout"):
                    continue
                vp_id = str(r.get("id", ""))
                if not vp_id:
                    continue
                # First occurrence wins (top-level is freshest)
                if vp_id in out:
                    continue
                out[vp_id] = {
                    "actual_result": str(r.get("actual_result", "") or ""),
                    "evidence": str(r.get("evidence", "") or ""),
                }
        return out
