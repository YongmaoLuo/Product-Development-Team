"""Feishu notifier — subscribes to the in-process state-event bus.

Why this exists
---------------
Replaces the polling bridge (``tools/main.py``) that used to hit
``/api/plan/{id}/summary`` and friends every 30s. The bridge had
three structural problems:

1. **30-second worst-case latency.** A VP completing at T+0 would
   not show up in the chat until T+30 when the next poll ran.
2. **Telegram-coupled Feishu.** The bridge's ``_patch_card`` had
   an early-return at line 1767-1769 that silently blocked the
   Feishu branch whenever the Telegram ``send_or_edit_telegram``
   import failed (the 2026-09-06 incident).
3. **Local symlink dependency.** the transport module was
   a symlink to a non-existent sibling repo; a fresh import in the
   bridge process threw ModuleNotFoundError, the early-return
   fired, the Feishu branch was unreachable, and the user watched
   the cards stop updating for 2.5 hours with no signal in the
   server logs.

This notifier runs **inside the backend** so it has direct
access to the state changes that drive the cards, no HTTP round-
trips for state-change detection, and zero symlink dependencies.

Architecture
-----------

* Subscribes to ``STATE_EVENT_BUS`` via ``publish_safe``.
* Receives events on its own thread (``_on_event`` queues them
  into a bounded ``queue.Queue``).
* A single worker thread (``_worker_loop``) drains the queue,
  coalescing events per-plan over a short window so a burst of 40
  ``task_state_changed`` events collapses to one card rebuild.
* For each dirty plan: rebuilds the card via
  ``cards.build_progress_card`` or ``cards.build_verification_card``
  based on the dirty kinds, computes the fingerprint, skips the
  push if it matches the last push, otherwise sends/updates the
  Feishu card and persists the new state atomically.
* The terminal ``plan_closed`` event BYPASSES coalescing so the
  operator always sees the final card, even if it would otherwise
  be deduped.

Lifecycle
---------
* Construct in ``_lifespan`` AFTER repositories are ready.
* ``.start()`` spawns the worker thread and emits the startup
  sweep (one ``plan_phase_changed`` per active plan).
* ``.stop(timeout)`` drains pending events, sends final pushes,
  joins the worker thread. Called from the lifespan ``finally``
  block so the process never leaks a thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .cards import (
    VERIFICATION_PHASES,
    VERIFICATION_TERMINAL_STATUSES,
    build_progress_card,
    build_verification_card,
    drop_terminal_db_orphans,
)
from .feishu_client import FeishuClient, FeishuUnavailable
from .fingerprint import card_fingerprint
from .plan_dir_resolver import (
    DEFAULT_BACKEND_BASE,
    card_state_path_for,
    fetch_execution_progress,
    fetch_plan_status,
    fetch_summary,
    fetch_verification_progress,
    list_active_plan_ids,
    list_stranded_card_plan_ids,
)
from .state_events import (
    KIND_PLAN_CLOSED,
    KIND_PLAN_PHASE_CHANGED,
    KIND_STALE_REFRESH,
    KIND_TASK_STATE_CHANGED,
    KIND_VP_STATE_CHANGED,
    StateEvent,
)
from .telegram_client import (
    MAX_MESSAGE_CHARS,
    escape_markdown_v2,
    load_telegram_config,
    send_or_edit_telegram,
)

# Telegram mirrors the same card content through the transport in
# ``backend/notifications/telegram_client.py`` — a sibling of the Feishu
# client, configured the same way, from the environment.
#
# It used to borrow that transport from a module **outside this repo**
# (a module outside this repository, resolved by dotted
# string at call time). On a clean checkout ``tools/`` does not exist, so
# the import raised and the channel was dead everywhere except the machine
# that happened to own that directory — and it turned an untracked path
# into an execution slot inside the server process. See the docstring of
# ``telegram_client.py``; this is the same failure shape as item 3 in the
# module docstring above, which is why the Feishu side was localised.


logger = logging.getLogger(__name__)
# Debug-only override: ``FEISHU_NOTIFY_DEBUG_LOG=1`` flips the
# notifier's logger to WARNING so the worker-thread trail
# (``sent new card`` / ``patched card`` / ``update_message failed``)
# surfaces in the uvicorn access log without polluting production.
if os.environ.get("FEISHU_NOTIFY_DEBUG_LOG"):
    logger.setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Card-state persistence
# ---------------------------------------------------------------------------


@dataclass
class PlanCardState:
    """In-memory + on-disk state for one plan's Feishu card.

    Persisted as a single JSON file at
    ``<plan_dir>/notifications/feishu_card.json`` so the notifier
    can recover after a server restart without re-sending a new
    card.

    Fields:
        plan_id: Same as the keying dict key (kept here so the
            on-disk JSON is self-describing — useful when an
            operator inspects the file by hand).
        chat_id: Feishu chat id to push into (per-plan override
            ``FEISHU_NOTIFY_CHAT_ID`` falls back to
            ``FEISHU_DEFAULT_CHAT_ID``).
        message_id: Feishu message id returned by the most recent
            successful push. ``None`` means "we haven't sent
            yet" → the worker calls ``send_message`` instead of
            ``update_message``.
        telegram_message_id: Telegram message id returned by the
            most recent successful Telegram push. Mirrors
            ``message_id`` for the Telegram channel. ``None``
            means "we haven't sent yet" → the worker calls
            ``sendMessage`` instead of ``editMessageText``.
            Persisted since 2026-09-07; older card-state JSONs
            without this key are loaded with ``None`` so the
            migration is safe.
        last_fingerprint: SHA-256 of the meaningful content from
            the last push (volatile fields stripped). ``None``
            means "first push" → always send.
        last_push_ts: Epoch seconds of the most recent push.
            Used for the per-plan min-interval gate and the
            coalesce-window bookkeeping.
        permanently_disabled: Set to True when chat_id resolution
            fails (no env config). The worker treats this as
            "skip forever" and never re-tries — operators can
            clear the flag by editing the JSON file or by
            restarting after fixing the env.
    """

    plan_id: str
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    telegram_message_id: Optional[str] = None
    last_fingerprint: Optional[str] = None
    last_push_ts: Optional[float] = None
    permanently_disabled: bool = False


def _load_card_state(plan_id: str) -> PlanCardState:
    """Rehydrate ``PlanCardState`` from disk; missing → fresh state."""
    path = card_state_path_for(plan_id)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return PlanCardState(
                plan_id=data.get("plan_id", plan_id),
                chat_id=data.get("chat_id"),
                message_id=data.get("message_id"),
                telegram_message_id=data.get("telegram_message_id"),
                last_fingerprint=data.get("last_fingerprint"),
                last_push_ts=data.get("last_push_ts"),
                permanently_disabled=bool(data.get("permanently_disabled", False)),
            )
    except (OSError, ValueError) as exc:
        logger.warning(
            "feishu_card.json for plan %s unreadable (%s); starting fresh",
            plan_id, exc,
        )
    return PlanCardState(plan_id=plan_id)


def _save_card_state(state: PlanCardState) -> None:
    """Persist ``state`` atomically (write to .tmp + os.replace)."""
    path = card_state_path_for(state.plan_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "plan_id": state.plan_id,
                    "chat_id": state.chat_id,
                    "message_id": state.message_id,
                    # Persisted since 2026-09-07 — older readers
                    # ignore unknown keys, so adding this is safe.
                    "telegram_message_id": state.telegram_message_id,
                    "last_fingerprint": state.last_fingerprint,
                    "last_push_ts": state.last_push_ts,
                    "permanently_disabled": state.permanently_disabled,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        # Persistence failure must not break the push path — log and
        # carry on. The next push will try again.
        logger.exception(
            "failed to persist feishu_card.json for plan %s", state.plan_id,
        )


# ---------------------------------------------------------------------------
# Chat-id resolution
# ---------------------------------------------------------------------------


def _resolve_chat_id(plan_id: str) -> Optional[str]:
    """Pick the Feishu chat id for this plan.

    Per-plan ``FEISHU_NOTIFY_CHAT_ID_<plan_id>`` overrides the
    shared ``FEISHU_NOTIFY_CHAT_ID``, which overrides
    ``FEISHU_DEFAULT_CHAT_ID``. Returns ``None`` when none are
    configured — the notifier disables that plan permanently rather
    than silently dropping messages.

    Note: there is intentionally NO hardcoded fallback here. An
    earlier bridge (since deleted) defaulted to
    ``oc_<chat-id>`` when no env var was set,
    but that chat id is not valid for
    ``POST /open-apis/im/v1/messages`` (Feishu returns
    ``code=230001 invalid receive_id`` for it). The earlier bridge
    only ever PATCHed existing messages, so the invalid id was never
    exercised. Returning ``None`` here forces the operator to set
    ``FEISHU_NOTIFY_CHAT_ID`` rather than silently producing
    ``pushes_failed > 0`` on every fresh-card send.
    """
    return (
        os.environ.get(f"FEISHU_NOTIFY_CHAT_ID_{plan_id}")
        or os.environ.get("FEISHU_NOTIFY_CHAT_ID")
        or os.environ.get("FEISHU_DEFAULT_CHAT_ID")
    )


# ---------------------------------------------------------------------------
# Telegram chat-id resolution + render helpers
# ---------------------------------------------------------------------------


def _resolve_telegram_chat_id(plan_id: str) -> Optional[str]:
    """Pick the Telegram chat id for this plan.

    Symmetric with :func:`_resolve_chat_id`: per-plan override
    (``TELEGRAM_CHAT_ID_<plan_id>``) wins, then the shared
    ``TELEGRAM_CHAT_ID``. Returns ``None`` when neither is set —
    the notifier logs once and skips Telegram for this event,
    without affecting the Feishu channel.
    """
    return (
        os.environ.get(f"TELEGRAM_CHAT_ID_{plan_id}")
        or os.environ.get("TELEGRAM_CHAT_ID")
    )


def _telegram_channel_provisioned(plan_id: str) -> bool:
    """A bot token AND a chat id are resolvable for ``plan_id``.

    Fast-path guard so we don't call the transport only to have it
    short-circuit on ``enabled=False``.

    The chat id is resolved **per plan**: ``TELEGRAM_CHAT_ID_<plan_id>``
    alone is enough, which is the whole point of the per-plan override —
    requiring the shared ``TELEGRAM_CHAT_ID`` as well would make the more
    specific setting unusable on its own.
    """
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN")
        and _resolve_telegram_chat_id(plan_id)
    )


def _telegram_channel_provisioned_any() -> bool:
    """The plan-agnostic form of the check above, for the status report.

    True when the channel can reach at least one plan: a bot token plus
    *some* chat id — the shared ``TELEGRAM_CHAT_ID`` or any per-plan
    ``TELEGRAM_CHAT_ID_<plan_id>``. Reporting the shared variable alone
    would show "disabled" for an operator who configured only per-plan
    ids, which is a supported setup.
    """
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return False
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return True
    return any(k.startswith("TELEGRAM_CHAT_ID_") for k in os.environ)


def _render_card_as_markdown(card: dict, plan_id: str) -> str:
    """Flatten a Feishu card dict into a single Markdown text block.

    Telegram has no concept of an interactive card — its closest
    equivalent is a ``sendMessage`` body with MarkdownV2 formatting.
    We use ``card["header"]["title"]["content"]`` as the title line
    and join every ``elements[*].text.content`` of every section
    into the body. Sections with no text content are skipped.

    This is intentionally lossy (Telegram can't render Feishu
    status colours, clickable buttons, etc.); the goal is "the
    operator sees the same headline content in Telegram as in
    Feishu", not a pixel-perfect mirror. The full structured card
    remains available in Feishu; Telegram is a notification mirror.

    The returned string is a **valid MarkdownV2 body**: every piece of
    card content is escaped here, and the two emphasis markers this
    function adds itself are the only unescaped ``*``/``_`` in it. The
    transport sends the text verbatim (with ``parse_mode=MarkdownV2``)
    and cannot do this for us — it has no way to tell the emphasised
    title from a ``*`` that is content, and Telegram rejects the whole
    message with HTTP 400 on one unescaped reserved character.
    """
    lines: list[str] = []
    header = card.get("header") or {}
    title = (header.get("title") or {}).get("content")
    if title:
        lines.append(f"*{escape_markdown_v2(str(title))}*")
    plan_id_tag = card.get("plan_id_tag")
    if plan_id_tag and plan_id_tag != title:
        lines.append(f"_{escape_markdown_v2(str(plan_id_tag))}_")

    for section in card.get("sections") or []:
        text_node = (section or {}).get("text") or {}
        body = text_node.get("content")
        if body:
            lines.append(escape_markdown_v2(str(body)))

    if not lines:
        # Last-ditch: emit the plan_id so the operator sees
        # *something* in Telegram even if the card builder
        # produced a fully-empty dict (shouldn't happen, but
        # better than a silent empty message).
        lines.append(f"(empty card) plan_id={escape_markdown_v2(plan_id)}")

    rendered = "\n\n".join(lines)
    if len(rendered) > MAX_MESSAGE_CHARS:
        # Telegram rejects anything longer than 4096 characters, and a
        # rejected push is worse than a truncated one: the operator sees
        # nothing at all. Cut on the character boundary and say so.
        cut = rendered[: MAX_MESSAGE_CHARS - 1]
        # Never end on a dangling escape. The cut can land between a
        # backslash and the character it escapes, and a trailing escape
        # with nothing to escape is itself a parse error — which would
        # make this exact body fail on every retry, forever.
        trailing = len(cut) - len(cut.rstrip("\\"))
        if trailing % 2 == 1:
            cut = cut[:-1]
        rendered = cut + "…"
    return rendered


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


#: Minimum interval between two pushes **of the same card**.
#:
#: The gate is per-plan, never global: each ``PlanCardState`` carries its
#: own ``last_push_ts``, so plan A being throttled never delays plan B.
#: One push per card per minute is the intended cadence.
#:
#: The 1.0s floor that ``917ba66`` (event-driven push replaces polling
#: bridge, 2026-09-06) left behind was sized for "don't hammer the API
#: from the new event path", not for per-card throttling. 60.0 restores
#: the 1/minute cadence; use the ``min_interval_seconds`` kwarg to
#: tighten it in tests.
DEFAULT_MIN_INTERVAL_SECONDS: float = 60.0


#: How long a worker may run with the card unchanged before the notifier
#: forces a refresh anyway.
#:
#: The fingerprint dedup is what keeps the chat quiet, and it is also
#: what made the card lie by omission: any phase whose progress does not
#: alter a single rendered field produces byte-identical rebuilds, every
#: one of which is discarded. A round's judgment phase is the case that
#: matters — it walks the VPs one at a time over a period long enough
#: that an operator watching the card assumes it has hung.
#:
#: 5 minutes is chosen against the slowest useful signal: the operator
#: watching a card wants to know a long job is still alive well before
#: they start wondering whether it hung. It is a ceiling on staleness,
#: not a cadence — a plan whose card IS changing pushes at its own rate
#: and never consults this.
DEFAULT_STALE_REFRESH_SECONDS: float = 300.0


class FeishuNotifier:
    """Subscribes to ``STATE_EVENT_BUS`` and pushes cards to Feishu.

    Constructor does NOT touch the network — it only stores config
    and registers the bus subscriber. ``.start()`` is the side-
    effecting call (spawns the worker thread and emits the startup
    sweep).
    """

    def __init__(
        self,
        coalesce_seconds: float = 5.0,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        backend_base_url: str = DEFAULT_BACKEND_BASE,
        stale_refresh_seconds: float = DEFAULT_STALE_REFRESH_SECONDS,
    ) -> None:
        self._coalesce_seconds = coalesce_seconds
        self._min_interval_seconds = min_interval_seconds
        self._stale_refresh_seconds = stale_refresh_seconds
        self._backend_base = backend_base_url

        # Per-plan state. Lock guards mutations to this dict AND
        # to the contained PlanCardState objects (the worker is
        # the only writer; the bus handler is the only reader).
        self._states_lock = threading.RLock()
        self._states: Dict[str, PlanCardState] = {}

        # Bounded queue to absorb burst events without unbounded
        # memory growth. 1024 should fit any realistic burst
        # (40-task plans produce ~40 events in a tight loop; 1024
        # covers ~25 plans bursting at once).
        self._queue: "queue.Queue[StateEvent]" = queue.Queue(maxsize=1024)
        self._queue_dropped = 0

        # 2026-09-14 (execution-phase card frozen at "正在跑 tasks"
        # for 30+ min): the executor runs as a
        # SUBPROCESS, so its STATE_EVENT_BUS publishes land in the
        # subprocess's own in-process bus and never reach this
        # server's notifier. Watch the one file the executor MUST
        # rewrite on every task-status flip — ``tasks.json`` in its
        # project_dir — and synthesize a refresh event when its
        # mtime/size moves. Downstream gates (60 s min_interval +
        # fingerprint dedup) already suppress no-op pushes.
        # plan_id -> (st_mtime_ns, st_size) last observed.
        self._exec_watch: Dict[str, Tuple[int, int]] = {}
        # Counter: synthetic events enqueued by the watch (surfaced
        # in /api/debug/notifications).
        self._exec_watch_refreshes = 0
        self._stale_refreshes = 0

        # Worker thread lifecycle.
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

        # Feishu client. Created lazily in start() because the
        # constructor imports lark_oapi, which may not be installed
        # in every test environment.
        self._client: Optional[FeishuClient] = None
        self._disabled_reason: Optional[str] = None

        # Counters for /api/debug/notifications.
        self._deduped = 0
        self._pushes_ok = 0
        self._pushes_failed = 0
        self._self_heals = 0
        self._telegram_pushes_ok = 0
        self._telegram_pushes_failed = 0
        # 2026-09-14: number of rebuilds dropped because the plan had
        # nothing to render (see ``_has_operator_content``).
        self._empty_skipped = 0
        # 2026-09-07: log the "telegram disabled because env missing"
        # event at most once per process. Without this guard the
        # worker would emit the same line on every dirty plan tick,
        # turning the access log into a Telegram-channel wall of
        # noise for any operator who simply hasn't set the env.
        self._telegram_disabled_logged = False
        self._last_error: Optional[Dict[str, Any]] = None
        self._last_error_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker thread and emit the startup sweep.

        Safe to call exactly once. Re-calling is a no-op so the
        lifespan startup/restart handshake doesn't have to track
        state. If the Feishu client fails to construct (missing
        lark-oapi, missing env), the notifier logs ERROR and stays
        inert — ``_disabled_reason`` is set and ``stats()`` shows
        ``enabled=False``.
        """
        if self._worker is not None:
            return

        try:
            self._client = FeishuClient()
        except FeishuUnavailable as exc:
            self._disabled_reason = str(exc)
            logger.error(
                "[feishu_notifier] disabled at startup: %s", exc,
            )
            # Still register the bus subscriber so the worker
            # can keep consuming events (we'll just log them
            # and never push) — this surfaces "we received N
            # events but couldn't push them" in stats().
            self._register_subscriber()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="feishu-notifier",
                daemon=True,
            )
            self._worker.start()
            return
        except Exception:
            logger.exception("[feishu_notifier] unexpected error at start")
            return

        self._register_subscriber()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="feishu-notifier",
            daemon=True,
        )
        self._worker.start()

        # Startup sweep: enqueue one plan_phase_changed per active
        # plan so the operator sees a fresh card after every server
        # restart. The fingerprint dedup will short-circuit plans
        # whose content hasn't changed since the last push.
        #
        # 2026-09-16（搁浅在「执行中」的卡片）: 也扫**卡片相对
        # 自身状态已过期**的计划。一个执行被 server 重启打断的计划，会被
        # shutdown 终止子进程、DB 照常写成终态，但**没有事件去推卡片** —— 飞书
        # 就永远停在打断前那一版。sweep 只覆盖"正在干活儿"的计划，正好漏掉它。
        #
        # 判据收得很紧（终态 + 推过 + last_push_ts < updated_at），不是"弹历史
        # 卡片"（那条 2026-09-06 的反馈仍然成立）—— 见 list_stranded_card_plan_ids。
        sweep_ids = set(list_active_plan_ids())
        try:
            sweep_ids |= set(list_stranded_card_plan_ids())
        except Exception:
            logger.exception(
                "[feishu_notifier] startup sweep: "
                "list_stranded_card_plan_ids failed"
            )
        for plan_id in sorted(sweep_ids):
            self._enqueue_event(StateEvent(
                kind=KIND_PLAN_PHASE_CHANGED,
                plan_id=plan_id,
                payload={"sub_kind": "startup_sweep"},
            ))

        logger.info("[feishu_notifier] started (coalesce=%.1fs, min_interval=%.1fs)",
                    self._coalesce_seconds, self._min_interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to drain pending events and exit."""
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                logger.warning("[feishu_notifier] worker did not exit within %.1fs",
                               timeout)
        logger.info(
            "[feishu_notifier] stopped (pushes_ok=%d, push_failed=%d, deduped=%d)",
            self._pushes_ok, self._pushes_failed, self._deduped,
        )

    def _register_subscriber(self) -> None:
        from .state_events import STATE_EVENT_BUS
        STATE_EVENT_BUS.subscribe(self._on_event)

    # ------------------------------------------------------------------
    # Bus handler (called on FastAPI request / executor threads)
    # ------------------------------------------------------------------

    def _on_event(self, event: StateEvent) -> None:
        """Enqueue an incoming event. Never raises — ``publish_safe``
        already wraps the bus call, but we belt-and-suspenders."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._queue_dropped += 1
            logger.warning(
                "[feishu_notifier] queue full; dropped event kind=%s plan=%s",
                event.kind, event.plan_id,
            )

    def _enqueue_event(self, event: StateEvent) -> None:
        """Same as ``_on_event`` but used by startup sweep."""
        self._on_event(event)

    # ------------------------------------------------------------------
    # Worker loop (runs on its own thread)
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Drain events into per-plan dirty sets; rebuild + push.

        Algorithm (called every ``coalesce_seconds`` tick):

        1. Drain everything currently in the queue into a
           ``dict[plan_id, set[event_kind]]``. This is the
           "trailing-edge coalescing" — 40 events for the same
           plan collapse into one rebuild.
        2. For each dirty plan: try to load + rebuild its card.
        3. Skip if the plan is ``permanently_disabled`` (no chat_id).
        4. Skip if ``now - last_push_ts < min_interval_seconds`` —
           but mark the plan as ``pending`` so the NEXT tick
           retries (the worker's second drain will re-mark it).
        5. Else: rebuild, fingerprint, dedup, push.
        """
        while not self._stop_event.is_set():
            tick_start = time.time()
            try:
                self._watch_execution_progress()
                self._watch_stale_cards()
                self._drain_and_push()
            except Exception:
                logger.exception("[feishu_notifier] tick failed")

            # Sleep until next tick OR stop.
            elapsed = time.time() - tick_start
            remaining = self._coalesce_seconds - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

        # Drain on shutdown: process whatever's left in the queue.
        logger.info("[feishu_notifier] draining %d pending events on shutdown",
                    self._queue.qsize())
        try:
            self._drain_and_push(final_flush=True)
        except Exception:
            logger.exception("[feishu_notifier] final drain failed")

    def _watch_execution_progress(self) -> None:
        """Detect executor progress by polling each active plan's *state*.

        2026-09-14 (the card froze mid-execution):
        during the ``executing`` window the card went stale because the
        executor is a separate process — its bus events never reach this
        notifier. That is true of bus events; it is NOT true of state.
        Every status flip the executor makes lands in the shared SQLite
        store, and ``/api/execution/{id}/progress`` is the read model
        over it (disk DAG + runtime overlay). So this watch polls that
        state and enqueues a synthetic ``plan_phase_changed`` when it
        actually moved; the existing min-interval (60 s) and
        fingerprint-dedup gates keep unchanged cards from pushing.

        2026-09-17 — this used to stat ``{project_dir}/tasks.json``
        instead. Two things were wrong with that:

          * **Wrong file.** ``project_dir`` is the target repository,
            but the executor's ``--tasks-file`` is
            ``plans/<plan_id>/tasks.json``. On an earlier plan the
            probe watched the wrong ``tasks.json`` — one that had gone
            stale while the real one changed on every flip — so the card
            froze for the whole task and showed no in-progress task.
          * **A proxy, and a fragile one.** The file's *content* carries
            no runtime state at all (``save_tasks`` writes static fields
            only). Its mtime moved purely because ``update_task_status``
            happens to call ``save_tasks()`` unconditionally. Any future
            "content unchanged → skip the write" optimisation would kill
            the probe silently — the same failure mode, one layer down.

        Comparing the state itself removes both: no path resolution, no
        mtime, no dependence on a side effect. The fingerprint includes
        the in-progress task id, so a task *starting* is a change too
        (that is what makes the card's "current task" line catch up).

        First sight of a plan records the baseline WITHOUT enqueuing
        (the startup sweep already pushes one fresh card per active
        plan; a baseline enqueue would double it).
        """
        try:
            plan_ids = list_active_plan_ids()
        except Exception:
            logger.exception(
                "[feishu_notifier] exec watch: list_active_plan_ids failed"
            )
            return
        for plan_id in plan_ids:
            try:
                progress = fetch_execution_progress(
                    plan_id, base_url=self._backend_base,
                )
                sig = self._execution_state_fingerprint(progress)
                if sig is None:
                    # No readable state yet — retry next tick.
                    continue
            except Exception:
                # Unreachable backend / unreadable progress — next
                # tick retries. Never let the watch kill the tick.
                continue
            prev = self._exec_watch.get(plan_id)
            self._exec_watch[plan_id] = sig
            if prev is None or prev == sig:
                continue
            self._exec_watch_refreshes += 1
            logger.info(
                "[feishu_notifier] exec watch: state changed for "
                "plan=%s — scheduling card refresh", plan_id,
            )
            self._enqueue_event(StateEvent(
                kind=KIND_PLAN_PHASE_CHANGED,
                plan_id=plan_id,
                payload={"sub_kind": "exec_progress_watch"},
            ))

    def _watch_stale_cards(self) -> None:
        """Force a refresh for any plan whose card has gone quiet while
        a worker is still running.

        The fingerprint dedup suppresses pushes whose rendered content is
        unchanged. That is the right default — without it a busy plan
        re-posts an identical card every coalesce window — but it cannot
        distinguish "nothing is happening" from "something is happening
        that renders to no visible change". A round's judgment phase is
        the case that matters: it walks the VPs one at a time, emitting
        a heartbeat for each, every one of which rebuilds the card and
        every one of which hashes to the value already pushed. The card
        freezes for the whole phase while the watchdog in this same
        process reports that the thread is alive with no VP in flight.

        So: when a plan has a worker in flight and its last push is older
        than ``_stale_refresh_seconds``, ask for a rebuild and let it
        through the dedup. The push that comes out is usually identical
        to the last one — that is the point. The operator sees a live
        "🕐 刷新于" timestamp on a job they are watching instead of a
        card that stopped moving, and if the work really has stalled the
        card will show it.

        Scoped to plans with a worker in flight. A plan that is idle has
        nothing to report, and re-pushing it forever would be exactly
        the spam the dedup exists to prevent.
        """
        if self._stale_refresh_seconds <= 0:
            return
        now = time.time()
        due: List[str] = []
        with self._states_lock:
            for plan_id, state in self._states.items():
                if state.permanently_disabled:
                    continue
                last = state.last_push_ts
                if last is None or (now - last) < self._stale_refresh_seconds:
                    continue
                due.append(plan_id)
        if not due:
            return

        for plan_id in due:
            try:
                with self._states_lock:
                    state = self._states.get(plan_id)
                if state is None or state.permanently_disabled:
                    # Evicted between the scan above and here — the
                    # terminal event closed the plan out from under us.
                    continue
                last_push = state.last_push_ts
                payload = fetch_plan_status(
                    plan_id, base_url=self._backend_base,
                )
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if not (
                payload.get("execution_in_flight")
                or payload.get("verification_in_flight")
            ):
                # The worker finished between the snapshot and now. The
                # terminal event already pushed; nothing to refresh.
                continue
            self._stale_refreshes += 1
            logger.info(
                "[feishu_notifier] stale card for in-flight plan=%s — "
                "forcing a refresh (last push %.0fs ago, threshold %.0fs)",
                plan_id, now - (last_push or now),
                self._stale_refresh_seconds,
            )
            self._enqueue_event(StateEvent(
                kind=KIND_STALE_REFRESH,
                plan_id=plan_id,
                payload={"sub_kind": "stale_watchdog"},
            ))

    @staticmethod
    def _execution_state_fingerprint(
        progress: Optional[Dict[str, Any]],
    ) -> Optional[Tuple[Any, ...]]:
        """Reduce a progress payload to the part that changes on progress.

        Returns ``None`` when the payload carries no task state at all
        (nothing to compare), otherwise a comparable tuple of the
        per-task ``(id, status)`` pairs, the in-progress task id, the
        aggregate counts, and the executor's current activity.
        ``end_ts`` is deliberately excluded: a task that finishes already
        changes ``status``.

        ``current_activity`` is in the signature for the same reason the
        in-progress task id is: without it the watch is blind to every
        window where the executor is working on something that is not a
        task. The refiner is the expensive case — it rewrites the task
        list, so no ``plan_tasks`` row moves for as long as it runs and
        the fingerprint stays identical. That makes the watch emit
        nothing AND leaves the card rendering whatever the previous task
        had left behind: "正在跑 tasks" naming nothing. Keying on
        ``(kind, started_at)`` rather than the kind alone is deliberate —
        two consecutive refines of the same kind must still read as a
        change, or the second is invisible.
        """
        if not isinstance(progress, dict):
            return None
        tasks = progress.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return None
        status_pairs = tuple(sorted(
            (str(t.get("id")), str(t.get("status")))
            for t in tasks
            if isinstance(t, dict) and t.get("id")
        ))
        current = progress.get("current") or {}
        current_id = str(current.get("id")) if isinstance(current, dict) else ""
        counts = progress.get("counts") or {}
        counts_sig = tuple(sorted(
            (str(k), int(v)) for k, v in counts.items()
            if isinstance(v, int)
        )) if isinstance(counts, dict) else ()
        activity = progress.get("current_activity")
        activity_sig = (
            (str(activity.get("kind")), str(activity.get("started_at")))
            if isinstance(activity, dict) else ("", "")
        )
        return (status_pairs, current_id, counts_sig, activity_sig)


    def _drain_and_push(self, final_flush: bool = False) -> None:
        dirty: Dict[str, Set[str]] = {}
        while True:
            try:
                ev = self._queue.get_nowait()
            except queue.Empty:
                break
            kinds = dirty.setdefault(ev.plan_id, set())
            kinds.add(ev.kind)

        for plan_id, kinds in dirty.items():
            try:
                self._handle_plan(plan_id, kinds, final_flush=final_flush)
            except Exception:
                logger.exception(
                    "[feishu_notifier] handle_plan %s failed", plan_id,
                )

    def _handle_plan(
        self, plan_id: str, kinds: Set[str], final_flush: bool,
    ) -> None:
        with self._states_lock:
            state = self._states.get(plan_id)
            if state is None:
                state = _load_card_state(plan_id)
                if state.chat_id is None:
                    state.chat_id = _resolve_chat_id(plan_id)
                self._states[plan_id] = state

            if state.permanently_disabled:
                return

            # Min-interval gate. Skip unless plan_closed, final flush, or
            # the stale-refresh watchdog. The watchdog is a third bypass
            # because the thing it exists to catch is precisely a long
            # silence: gating it on "enough time passed" would defeat it.
            bypass = (
                KIND_PLAN_CLOSED in kinds
                or KIND_STALE_REFRESH in kinds
                or final_flush
            )
            if (
                not bypass
                and state.last_push_ts is not None
                and (time.time() - state.last_push_ts) < self._min_interval_seconds
            ):
                # Mark pending so the NEXT tick re-tries this plan
                # (the worker keeps draining events into dirty
                # regardless).
                return

        # Build card off-lock (network calls happen here).
        card, phase, verification_status, summary = self._rebuild_card(
            plan_id, kinds,
        )
        if card is None:
            return

        fp = card_fingerprint(card)

        with self._states_lock:
            if state.last_fingerprint == fp and not bypass:
                self._deduped += 1
                return
            chat_id = state.chat_id

        if chat_id is None:
            # No chat_id configured — disable permanently and warn
            # ONCE per plan so the operator sees it.
            with self._states_lock:
                state.permanently_disabled = True
            self._record_error(
                "no chat_id configured (set FEISHU_NOTIFY_CHAT_ID or FEISHU_DEFAULT_CHAT_ID)",
                plan_id=plan_id,
            )
            logger.error(
                "[feishu_notifier] no chat_id for plan=%s; permanently disabled "
                "(restart with FEISHU_NOTIFY_CHAT_ID set to re-enable)", plan_id,
            )
            return

        ok = self._push_to_feishu(chat_id, state, card)
        if not ok:
            with self._states_lock:
                # If update failed because the message expired, we
                # cleared state.message_id and sent a fresh one.
                # Re-load to capture the new message_id.
                pass

        # 2026-09-07: mirror the same card to Telegram on the same
        # tick. Each channel is independently try/except'd in the
        # transport method, so a Telegram transport failure does
        # NOT mark the Feishu push as failed and vice versa.
        # The fingerprint + min_interval gate above already
        # suppressed redundant pushes for both channels — they
        # share the same fingerprint because they share the same
        # card body.
        self._push_to_telegram(state, card)

        # 2026-09-08 (a dev checkout divergence plan audit): persist
        # the rendered card to disk so operators (and the assistant)
        # can see what Feishu / Telegram actually displayed without
        # needing to fetch from Feishu's API. Without this, the
        # "card content didn\'t change" report can only be
        # investigated by re-running the card builder locally and
        # guessing whether it matches what was sent — which is
        # exactly the gap that produced a dev checkout 22
        # pending tasks same_id_loop investigation.
        self._write_card_snapshot(
            plan_id=plan_id,
            card=card,
            fingerprint=fp,
            message_id=state.message_id,
            transports=["feishu", "telegram"],
            phase=phase,
            verification_status=verification_status,
            summary=summary,
        )

        # Persist updated message_id + fingerprint.
        with self._states_lock:
            state.last_fingerprint = fp
            state.last_push_ts = time.time()
        _save_card_state(state)

    # ------------------------------------------------------------------
    # Card rebuild
    # ------------------------------------------------------------------

    @staticmethod
    def _has_operator_content(
        summary: Dict[str, Any],
        progress: Optional[Dict[str, Any]],
    ) -> bool:
        """True when the card this plan would render carries real content.

        "Real content" means at least one of the sections the card renders
        will have at least one row:

        * **tasks** — ``summary.tasks.total > 0`` drives the
          "📋 任务执行状态" bar and the completed / in-flight / blocked /
          failed task lists.
        * **verification history** — a ``summary.state.verification``
          entry that is not the fresh-plan default (``status=pending``,
          ``round=0``) means the plan has run at least one verification
          round, so the "🔍 verification 状态" section renders a verdict.
        * **execution progress** — ``/api/execution/{id}/progress``
          returning any task rows (``progress["tasks"]``) or a current
          task, which happens when tasks exist in the DB even if
          ``summary.tasks`` lags a snapshot.

        Everything else (``pending`` + ``round 0``, zero tasks, no
        progress) renders a card whose entire body is the
        "📍 当前状态" line — see the caller for why we drop those.

        Deliberately does NOT treat "execution started" as content: a plan
        in ``executing`` with no tasks yet is exactly the blank-card case.

        2026-09-17: applied to the same normalised view the card renders
        from — superseded DB-only rows are dropped first, so a plan whose
        only remaining rows are dead ledger entries does not push a card
        that is empty by construction.
        """
        summary, progress = drop_terminal_db_orphans(summary, progress)

        tasks_info = summary.get("tasks") or {}
        if (tasks_info.get("total") or 0) > 0:
            return True

        verification = ((summary.get("state") or {}).get("verification")) or {}
        v_status = str(verification.get("status") or "").lower()
        v_round = verification.get("round") or 0
        if v_status not in ("", "pending", "not_started", "none") or v_round:
            return True

        if progress:
            if progress.get("tasks"):
                return True
            if progress.get("current"):
                return True

        return False

    def _rebuild_card(self, plan_id: str, kinds: Set[str]):
        """Build the appropriate card for this dirty plan.

        Routing: any ``vp_state_changed`` or verification phase in
        the plan summary → verification card; else progress card.
        Returns ``(None, None, None, None)`` when the data couldn't
        be fetched (no summary available yet, network down, etc.) —
        the worker drops this tick and the next event will retry.

        Returns ``(card, phase, verification_status, summary)`` on
        success so the caller can also persist the rendered card +
        source data snapshot (see
        :meth:`_write_card_snapshot` — operator visibility for what
        Feishu / Telegram actually displayed).
        """
        # 2026-09-23: ONE read. The card used to be rebuilt from three
        # endpoints fetched at three instants and then re-derived a
        # verdict from the mixture -- ``plan_status`` documents the
        # incident that produced. The three sub-payloads are still here,
        # but they now describe one moment, and the verdict comes from
        # the server instead of from whatever this process assembled.
        payload = fetch_plan_status(plan_id, base_url=self._backend_base)
        if payload is None:
            return None, None, None, None

        from plan_status import from_payload as _status_from_payload

        plan_status = _status_from_payload(payload)
        summary = payload.get("summary")
        if summary is None:
            return None, None, None, None

        state_dict = summary.get("state") or {}
        phase = plan_status.phase or state_dict.get("current_phase") or ""
        verification_status = (
            plan_status.verification_status
            or (state_dict.get("verification") or {}).get("status")
        )
        # 2026-09-11: a plan whose execution has already finished
        # (``current_phase="completed"``) but whose verification loop
        # reached a terminal verdict (passed / failed / loop_stopped)
        # is NOT in ``VERIFICATION_PHASES`` anymore, so it used to fall
        # through to the progress card — which renders no verification
        # section at all, hiding the very verdict the operator is
        # waiting for. The terminal verification status is itself
        # sufficient evidence that we want the verification card, and
        # ``/api/verification/{id}/progress`` keeps returning data for
        # terminal plans.
        wants_verification = (
            KIND_VP_STATE_CHANGED in kinds
            or phase in VERIFICATION_PHASES
            or (verification_status or "").lower()
            in VERIFICATION_TERMINAL_STATUSES
        )

        progress = payload.get("execution") or {}

        # 2026-09-09 (unified phase in cards): pull the
        # 2026-09-17 (schema v5): this block used to pull TWO values out
        # of ``summary.state`` — a routing ``stage`` and a plan-state
        # ``current_phase`` — so the unified "📍 当前状态" line could
        # describe both the top-level worker and the inner sub-state.
        # Those were two columns in ``plan_routing`` that could disagree,
        # and the card was the place the disagreement became visible.
        # There is one workflow-state column now, so the card reads one
        # value (`phase` above, from the same ``summary.state``).

        # 2026-09-14: a plan with no tasks, no verification history and no
        # execution progress renders NOTHING — the card degenerates to a
        # single "📍 当前状态" line plus an empty "🔍 verification 状态"
        # section header. A card whose body carries no information beyond
        # its own status line is pure noise on push.
        #
        # Such plans are almost always fixture ids that leaked into the live
        # state store (bounded now by the conftest ``PDT_STATE_DB_PATH`` /
        # ``PDT_PLANS_DIR`` redirects), but the guard is worth having on its
        # own: the 2026-09-06 sweep filter already encodes "never emit a
        # blank card" for the startup sweep, and this applies the same rule
        # to event-driven pushes.
        #
        # Suppress ONLY when every content source is empty. A plan with a
        # single task, or one verification round on record, renders real
        # rows and is pushed normally — and a plan that is merely early
        # (``executing`` before ``tasks.json`` lands) pushes as soon as its
        # first task appears, which is the moment the card becomes useful.
        if not self._has_operator_content(summary, progress):
            self._empty_skipped += 1
            logger.info(
                "[feishu_notifier] skipping contentless card plan=%s "
                "phase=%s (no tasks, no verification history, no progress)",
                plan_id, phase,
            )
            return None, None, None, None

        if wants_verification:
            verif = payload.get("verification")
            if verif is not None:
                tasks_info = summary.get("tasks", {})
                task_progress = progress.get("tasks", []) if progress else None
                try:
                    # 2026-09-09 (unified phase in cards): the
                    # verification card's unified header section
                    # needs both ``max_rounds`` and ``round``. The
                    # verification progress endpoint historically
                    # returned only ``verification_round``; fall
                    # back to ``summary.state.verification.max_rounds``
                    # when ``progress.max_rounds`` is missing.
                    max_rounds = verif.get("max_rounds")
                    if max_rounds is None:
                        max_rounds = (state_dict.get("verification") or {}).get(
                            "max_rounds"
                        )
                    # 2026-09-09: ``stop_reason`` is the only
                    # way the verification card can tell
                    # ``round==max_rounds`` apart from "we ran the
                    # last round successfully" — without it the card
                    # keeps rendering "🔄 验证中 round 3/3" even
                    # though the orchestrator has already flagged
                    # the plan ``max_rounds_reached`` and is waiting
                    # for the user to call ``/reset_rounds`` to
                    # keep iterating. The header needs to flip to
                    # "⏹ 循环停止" so the user knows the loop is
                    # suspended, not still running.
                    stop_reason = verif.get("stop_reason")
                    if stop_reason is None:
                        stop_reason = (state_dict.get("verification") or {}).get(
                            "stop_reason"
                        )
                    # 2026-09-08: pass ``current_phase`` so the
                    # card can distinguish verification_repairing /
                    # verification_rerunning / verify_first_pass
                    # from the generic "🔄 验证中" — the user
                    # explicitly asked for visible intermediate
                    # states (反思中 / 正在生成修复任务 / 重跑验证).
                    # 2026-09-09: ``max_rounds`` so the
                    # round/max line in the unified section has the
                    # denominator. It used to also pass
                    # ``routing_stage`` / ``execution_current_phase``;
                    # see the 2026-09-17 note above.
                    card = build_verification_card(
                        plan_id,
                        verif,
                        tasks_info=tasks_info,
                        task_progress=task_progress,
                        current_phase=phase,
                        max_rounds_override=max_rounds,
                        stop_reason_override=stop_reason,
                        plan_status=plan_status,
                    )
                    return card, phase, verification_status, summary
                except Exception:
                    logger.exception(
                        "[feishu_notifier] build_verification_card failed for %s",
                        plan_id,
                    )
                    # Fall through to progress card.

        try:
            # 2026-09-11: the legacy ``build_progress_card`` shim passes
            # ``verification_progress=None`` so the unified builder
            # would skip the "🔍 verification 状态" group entirely
            # (see cards.py line 1292). That hid the verification
            # round history from the operator right when they most
            # needed to see it — the moment the executor restarted
            # to run RP-* tasks. The right fix is: when we know the
            # plan has run verification at least once, ALWAYS render
            # the verification section as a historical record, even
            # during execution phases. We synthesise a minimal
            # ``verification_progress`` dict from ``summary.state.verification``
            # when the live ``/api/verification/{id}/progress``
            # endpoint is unavailable (e.g. verification is idle).
            verif_history: Optional[Dict[str, Any]] = None
            summary_verif = (state_dict.get("verification") or {}) if state_dict else {}
            if summary_verif:
                # Only synthesise when there's something to show —
                # a plan that never ran verification has an empty
                # ``summary.state.verification`` and stays quiet.
                verif_history = {
                    "verification_status": summary_verif.get("status") or "",
                    "verification_round": summary_verif.get("round") or 0,
                    "max_rounds": summary_verif.get("max_rounds") or 0,
                    "stop_reason": summary_verif.get("stop_reason"),
                }
                # Prefer the live endpoint (covers the in-flight round
                # the snapshot may not have caught up to yet).
                try:
                    _live_verif = payload.get("verification")
                    if _live_verif:
                        # Merge: live wins for status / round (the
                        # snapshot lags), history keeps max_rounds /
                        # stop_reason if live didn't set them.
                        verif_history = {
                            "verification_status": _live_verif.get(
                                "verification_status"
                            ) or verif_history["verification_status"],
                            "verification_round": _live_verif.get(
                                "verification_round"
                            ) or verif_history["verification_round"],
                            "max_rounds": _live_verif.get(
                                "max_rounds"
                            ) or verif_history["max_rounds"],
                            "stop_reason": _live_verif.get(
                                "stop_reason"
                            ) or verif_history["stop_reason"],
                            # 2026-09-12: the
                            # historical body section renders per-VP
                            # titles + actual_result quotes + counts ONLY
                            # when these live fields are forwarded
                            # through ``verif_history``. Without
                            # them, ``_verification_sections`` falls
                            # back to the bare ``body_only`` dict
                            # which has no failed_vps / vps / counts
                            # — operators see "❌ 失败 VP (3):" with
                            # blank titles. Pass them through.
                            "failed_vps": _live_verif.get("failed_vps") or [],
                            "completed_vps": _live_verif.get("completed_vps") or [],
                            "vps": _live_verif.get("vps") or [],
                            "counts": _live_verif.get("counts") or {},
                            "repair_tasks": _live_verif.get("repair_tasks") or [],
                            # ``current_vp`` was missing from this
                            # merge, which made the progress-card route
                            # structurally unable to name a running
                            # verification point: the unified line reads
                            # it from ``verification_progress``, and on
                            # this path the synthesised dict is passed as
                            # ``verification_body_only`` instead, so an
                            # omitted key is an omitted name. The card
                            # looked permanently "nameless" for every
                            # plan in an execution phase with
                            # verification history.
                            "current_vp": _live_verif.get("current_vp") or None,
                            # Round sub-step for the windows with no VP
                            # in flight (planning / judging /
                            # summarizing). See
                            # ``routes.verification._verification_round_activity``.
                            "activity": _live_verif.get("activity") or None,
                        }
                except Exception:
                    logger.debug(
                        "[feishu_notifier] live verification_progress "
                        "fetch failed for %s; using summary snapshot",
                        plan_id,
                    )

            card = build_progress_card(
                plan_id, summary,
                progress=progress or None,
                verification_history=verif_history,
                plan_status=plan_status,
            )
        except Exception:
            logger.exception(
                "[feishu_notifier] build_progress_card failed for %s", plan_id,
            )
            return None, None, None, None
        return card, phase, verification_status, summary

    # ------------------------------------------------------------------
    # Feishu transport
    # ------------------------------------------------------------------

    def _push_to_feishu(
        self,
        chat_id: str,
        state: PlanCardState,
        card: dict,
    ) -> bool:
        """Send a new card or patch the existing one.

        Returns True on success, False on transport failure. The
        worker treats False as a fatal-for-this-event error and
        moves on; the next event will retry.

        Self-heal: if the existing message expired (Feishu caps
        card edits at ~14 days, or the operator deleted the chat),
        ``update_message`` returns False and we clear
        ``state.message_id`` and send a fresh card.
        """
        if self._client is None:
            # Disabled at startup; never reach here normally.
            self._record_error("client is None", plan_id=state.plan_id)
            return False

        payload = json.dumps(card, ensure_ascii=False)

        if state.message_id is None:
            new_id = self._client.send_message(
                receive_id=chat_id,
                content=payload,
                msg_type="interactive",
            )
            if new_id is None:
                self._record_error("send_message returned None",
                                   plan_id=state.plan_id)
                self._pushes_failed += 1
                return False
            with self._states_lock:
                state.message_id = new_id
            self._pushes_ok += 1
            logger.warning(
                "[feishu_notifier] sent new card %s to %s for plan=%s "
                "(pushes_ok=%d)",
                new_id, chat_id, state.plan_id, self._pushes_ok,
            )
            return True

        updated = self._client.update_message(state.message_id, payload)
        if updated:
            self._pushes_ok += 1
            logger.warning(
                "[feishu_notifier] patched card %s for plan=%s (pushes_ok=%d)",
                state.message_id, state.plan_id, self._pushes_ok,
            )
            return True

        # Self-heal: try a fresh card.
        logger.warning(
            "[feishu_notifier] update_message failed for %s (plan=%s); "
            "sending a fresh card",
            state.message_id, state.plan_id,
        )
        new_id = self._client.send_message(
            receive_id=chat_id,
            content=payload,
            msg_type="interactive",
        )
        if new_id is None:
            self._record_error(
                "update_message and send_message both failed",
                plan_id=state.plan_id,
            )
            self._pushes_failed += 1
            return False

        with self._states_lock:
            state.message_id = new_id
        self._self_heals += 1
        self._pushes_ok += 1
        logger.info(
            "[feishu_notifier] self-healed: replaced dead card %s with %s (plan=%s)",
            state.message_id, new_id, state.plan_id,
        )
        return True

    # ------------------------------------------------------------------
    # Telegram transport
    # ------------------------------------------------------------------

    def _push_to_telegram(
        self,
        state: PlanCardState,
        card: dict,
    ) -> bool:
        """Mirror the same card content to Telegram.

        Uses :func:`backend.notifications.telegram_client.send_or_edit_telegram`
        — the in-package transport, so this path has no dependency on
        anything outside the repository. The channel is skipped entirely
        when the operator has not provisioned a bot token plus a chat id
        (see :func:`_telegram_channel_provisioned`).

        The body is built once by :func:`_render_card_as_markdown`, which
        returns MarkdownV2 with its content already escaped; the transport
        sends it verbatim, so an escaped body must not be escaped again.

        On transport failure the function records the error and
        increments ``telegram_pushes_failed``; the Feishu side of
        the same event is unaffected (each channel has its own
        try/except in :meth:`_handle_dirty`).

        Returns ``True`` on a successful send/edit, ``False`` on
        transport failure or when the channel is disabled by env.
        A disabled channel does NOT count as a failure — the
        operator simply hasn't asked for Telegram push.
        """
        if not _telegram_channel_provisioned(state.plan_id):
            # Already logged once per process via the
            # _telegram_disabled_logged guard; subsequent events
            # stay silent so the worker log isn't flooded.
            if not self._telegram_disabled_logged:
                logger.info(
                    "[feishu_notifier] telegram channel disabled: "
                    "TELEGRAM_BOT_TOKEN and a chat id (TELEGRAM_CHAT_ID or "
                    "TELEGRAM_CHAT_ID_<plan_id>) are not both set; skipping "
                    "telegram push for all plans until env is fixed",
                )
                self._telegram_disabled_logged = True
            return False

        chat_id = _resolve_telegram_chat_id(state.plan_id)
        rendered = _render_card_as_markdown(card, state.plan_id)
        config = load_telegram_config(chat_id)

        try:
            # One call covers both cases: it edits ``telegram_message_id``
            # when there is one, and falls back to a fresh send when the
            # edit fails (message deleted, content unchanged, ...). The
            # returned id is the message that is now live on the channel —
            # always persist *that*, not the id we asked it to edit.
            live_id = send_or_edit_telegram(
                rendered, config, state.telegram_message_id,
            )
            if live_id is None:
                # Nothing reached the channel: neither the edit nor the
                # fallback send succeeded.
                self._record_error(
                    "telegram send/edit delivered nothing",
                    plan_id=state.plan_id,
                )
                self._telegram_pushes_failed += 1
                return False

            with self._states_lock:
                state.telegram_message_id = str(live_id)
            self._telegram_pushes_ok += 1
            logger.warning(
                "[feishu_notifier] telegram message %s is live for plan=%s "
                "(telegram_pushes_ok=%d)",
                live_id, state.plan_id, self._telegram_pushes_ok,
            )
            return True
        except Exception:
            # Transport-level failure that escaped the client's own
            # never-raise contract (network, rate limit, malformed
            # response). Don't escalate: the next event retries the same
            # message id, and if Telegram permanently rejects the message
            # an operator can clear ``telegram_message_id`` from the
            # on-disk JSON to force a fresh send on the next event.
            logger.exception(
                "[feishu_notifier] telegram push failed for plan=%s",
                state.plan_id,
            )
            self._record_error(
                "telegram transport raised", plan_id=state.plan_id,
            )
            self._telegram_pushes_failed += 1
            return False

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def _record_error(self, msg: str, plan_id: str = "") -> None:
        with self._last_error_lock:
            self._last_error = {"msg": msg, "ts": time.time(), "plan_id": plan_id}

    def _write_card_snapshot(
        self,
        plan_id: str,
        card: dict,
        fingerprint: str,
        message_id: Optional[str],
        transports: List[str],
        phase: str,
        verification_status: Optional[str],
        summary: Optional[dict],
    ) -> None:
        """Persist the rendered card body + the source data used to
        build it so an operator (or this assistant) can inspect what
        Feishu / Telegram actually displayed at any later time
        without calling the Feishu ``GET /im/v1/messages/{id}`` API.

        Two files written:

        * ``plans/<plan_id>/notifications/last_pushed_card.json`` —
          atomic JSON write (tmp + rename). Holds the latest
          snapshot in full, including the ``summary`` blob that
          was used to build the card. Sized for human inspection
          (``jq``/editor), not high-frequency reads.

        * ``plans/<plan_id>/notifications/pushed_card_history.jsonl``
          — append-only, one line per push. Carries the same
          schema minus the heavy ``summary`` to keep lines small.
          Operators can grep / jq the timeline to see what was
          rendered when.

        Best-effort: never raises. Disk errors are logged at WARNING
          so a permission problem surfaces in the server log but
          doesn't break the live push.
        """
        from .plan_dir_resolver import (
            last_pushed_card_path_for,
            pushed_card_history_path_for,
        )

        timestamp = datetime.utcnow().isoformat() + "Z"
        latest_snapshot = {
            "plan_id": plan_id,
            "timestamp": timestamp,
            "phase": phase,
            "verification_status": verification_status,
            "fingerprint": fingerprint,
            "message_id": message_id,
            "transports": transports,
            "card": card,
            "summary": summary,
        }
        history_line = {
            "plan_id": plan_id,
            "timestamp": timestamp,
            "phase": phase,
            "verification_status": verification_status,
            "fingerprint": fingerprint,
            "message_id": message_id,
            "transports": transports,
            # Card body kept in history too — small enough and the
            # full content is what makes the history useful for
            # debugging "what did the card say at HH:MM:SS".
            "card": card,
        }

        snap_path = last_pushed_card_path_for(plan_id)
        hist_path = pushed_card_history_path_for(plan_id)

        try:
            snap_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = snap_path.with_suffix(".json.tmp")
            # 2026-09-08: dump to memory first so a non-serialisable
            # value raises BEFORE any bytes hit disk. ``json.dump``
            # writes incrementally, so a mid-stream TypeError would
            # otherwise leave a partial file that ``os.replace``
            # would happily promote into the live snapshot path.
            payload_str = json.dumps(
                latest_snapshot, ensure_ascii=False, indent=2,
            )
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload_str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, snap_path)
        except Exception as exc:
            logger.warning(
                "[feishu_notifier] failed to write card snapshot for %s: %s",
                plan_id, exc,
            )

        try:
            with open(hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(history_line, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(
                "[feishu_notifier] failed to append card history for %s: %s",
                plan_id, exc,
            )

    def stats(self) -> Dict[str, Any]:
        with self._last_error_lock:
            last_error = self._last_error
        with self._states_lock:
            return {
                "enabled": self._disabled_reason is None,
                "disabled_reason": self._disabled_reason,
                "queued": self._queue.qsize(),
                "dropped": self._queue_dropped,
                "deduped": self._deduped,
                "empty_skipped": self._empty_skipped,
                "exec_watch_refreshes": self._exec_watch_refreshes,
                "stale_refreshes": self._stale_refreshes,
                "pushes_ok": self._pushes_ok,
                "pushes_failed": self._pushes_failed,
                "self_heals": self._self_heals,
                "telegram_pushes_ok": self._telegram_pushes_ok,
                "telegram_pushes_failed": self._telegram_pushes_failed,
                "telegram_enabled": _telegram_channel_provisioned_any(),
                "plans_tracked": len(self._states),
                "last_error": last_error,
                # Read back the configured cadence so an operator can tell
                # "the card is quiet because nothing changed" apart from
                # "the card is quiet because the gate is throttling it".
                "coalesce_seconds": self._coalesce_seconds,
                "min_interval_seconds": self._min_interval_seconds,
            }