"""Plan-dir lookup helpers for the in-process notifier.

Why this exists
---------------
The notifier needs two things from a ``plan_id``:

1. The on-disk plan directory (``<repo_root>/plans/<plan_id>/``) —
   used to persist card state in
   ``<plan_dir>/notifications/feishu_card.json`` and to read
   ``tasks.json`` for the runtime overlay.

2. The aggregated data the bridge used to pull from
   ``/api/plan/{id}/summary`` / ``/api/execution/{id}/progress`` /
   ``/api/verification/{id}/progress``. Those three endpoints are
   the authoritative data source the notifier feeds into the card
   builders; they live in ``server.py`` alongside many other
   FastAPI handlers and would require a large refactor to import
   directly. We call them over HTTP from a background worker
   thread — the bridge did the same for years and the round-trip
   on localhost is sub-millisecond.

The notifier owns an ``httpx.Client`` (sync) and reuses it across
events; HTTP calls happen on the worker thread, so they do NOT
block the FastAPI request path.

Why we don't HTTP-call our own server in production: the bridge
worked fine for ~10 years this way. The "extract service-layer
functions" refactor is the long-term cleanup; we leave a
``TODO(perf)`` here so the next maintainer sees it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from config_paths import resolve_plans_dir, resolve_state_db_path
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------
#
# Both paths below are resolved through ``config_paths`` rather than
# mirrored from ``server.py``. Mirroring is what this section used to do
# — ``server.py`` computes ``Path(__file__).parent.parent / "plans"``, so
# a copy of that expression was kept here to keep the notifier's layout
# identical. Copies drift; the 2026-09-13 fixture-plan card pollution and
# the ``watchdog`` state.db drift are both instances of it. The module
# anchors below are still used to build display paths, but anything that
# *opens* a file goes through a resolver.

_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent


def _plans_dir() -> Path:
    """Return the plans root the notifier should read/write right now.

    2026-09-13: honours ``PDT_PLANS_DIR`` (via
    ``config_paths.resolve_plans_dir``) exactly like
    ``server.PLANS_DIR``. Before this, a test process that constructed
    the notifier wrote card snapshots into the operator's live
    ``<repo>/plans/<fixture-id>/notifications/`` tree, and the running
    server's startup sweep then kept refreshing cards for those junk
    fixture plans — the empty "执行中" Feishu cards the operator saw on
    2026-09-13.

    Resolved per call (not cached at import) so the env var is honoured
    even when it is set after this module has been imported.
    """
    return resolve_plans_dir()


def _state_db() -> Path:
    """Return the state-machine SQLite path the sweep filter should read.

    Delegates to :func:`config_paths.resolve_state_db_path`, so the sweep
    never falls through to the operator's live ``state.db`` from a test
    process — the property the previous hand-rolled copy of the same rule
    was there to provide, now provided once for the whole backend.
    """
    return resolve_state_db_path()


# ---------------------------------------------------------------------------
# Startup-sweep filter
# ---------------------------------------------------------------------------
#
# Only sweep plans whose ``current_phase`` is one of these two values.
# Other phases either have no card content yet (interview, prd_*) or
# are work the notifier should not amplify. See the docstring on
# ``list_active_plan_ids`` for the user-feedback context
# (2026-09-06: "过期卡片就不应该弹出来消息了").
_ACTIVE_PHASES: tuple[str, ...] = ("executing", "verification_running")

# Plans not touched in this many days are skipped — they're either
# stale interview prompts or plans that the operator has stopped
# caring about. Override via ``FEISHU_NOTIFY_STARTUP_SWEEP_MAX_AGE_DAYS``.
STARTUP_SWEEP_MAX_AGE_DAYS: float = float(
    os.environ.get("FEISHU_NOTIFY_STARTUP_SWEEP_MAX_AGE_DAYS", "7")
)

#: 测试夹具产生的 plan_id 形状。
#:
#: 2026-09-16: 统计出来的 9 张里,
#: **只有 2 张是真实计划**, 另外 7 张是 pytest 夹具漏写进真实 ``plans/``
#: 目录的产物（``vp-003-explicit`` / ``vp001-pid-in-payload`` …）。它们有
#: ``plan_state.json``、有 ``tasks.json``、还真的推成过飞书卡片 —— 从数据上
#: 看和真实计划长得一模一样, 唯一能区分的就是 id 形状。
#:
#: 不滤的后果: 每次重启都给这些死卡片推一条消息, 正是 2026-09-13 那次
#: 事故（``_plans_dir`` 的 docstring 记着它）的翻版。
#:
#: **刻意锚定前缀, 不用 scheduler 那种宽子串规则。** 宽子串规则
#: (``"test" in plan_id or "vp" in plan_id``) 会连 ``20260604-test`` 这种
#: 日期前缀的真实计划一起扫进去。通知器侧的误判代价是"真实卡片永远不刷新",
#: 方向比 scheduler 侧更危险, 所以窄一点。锚定后剩下的命中全是真夹具形状
#: (``vp-*`` / ``vp001-*`` / ``resume-test-*`` / ``project_vp016-*`` …)。
_FIXTURE_ID_RE = re.compile(
    r"^(?:vp[-_]|vp\d|test[-_]|test_|resume[-_]test|project_vp)",
    re.IGNORECASE,
)


def _looks_like_fixture_plan(plan_id: str) -> bool:
    """``plan_id`` 是测试夹具的产物吗（而非用户真实创建的计划）。

    只影响**自动 sweep** 的覆盖面 —— 真实计划永远可以走事件推送那条路,
    这里滤掉只是不让它在每次重启时被"复活"。
    """
    return bool(_FIXTURE_ID_RE.match(plan_id or ""))


def plan_dir_for(plan_id: str) -> Path:
    """Return the on-disk directory for ``plan_id``.

    Does NOT validate that the directory exists — callers (notifier
    startup sweep) use this to compute paths that may or may not be
    backed by a real plan yet (e.g. for plans that have been
    deleted but whose events still arrive during shutdown).
    """
    return _plans_dir() / plan_id


def card_state_path_for(plan_id: str) -> Path:
    """Where the notifier persists card state for ``plan_id``.

    Co-located with the plan so a plan deletion also wipes its card
    state — the notifier never has to know which plans have been
    retired because their card state is gone with them.
    """
    return plan_dir_for(plan_id) / "notifications" / "feishu_card.json"


def last_pushed_card_path_for(plan_id: str) -> Path:
    """Path to the latest snapshot of the rendered card body that
    was successfully sent to at least one transport.

    Co-located with :func:`card_state_path_for`. Operators (and the
    assistant) can read this file at any time to see exactly what
    was pushed to Feishu / Telegram without needing to call the
    Feishu ``GET /im/v1/messages/{id}`` API. Filled by
    :meth:`FeishuNotifier._write_card_snapshot`.

    Layout::

        {
          "plan_id": "...",
          "timestamp": "2026-09-08T...Z",
          "phase": "executing",
          "verification_status": "failed",
          "fingerprint": "sha256:...",
          "message_id": "om_xxx",
          "transports": ["feishu", "telegram"],
          "card": { ...rendered card JSON... },
          "summary": { .../api/plan/{id}/summary response... }
        }
    """
    return plan_dir_for(plan_id) / "notifications" / "last_pushed_card.json"


def pushed_card_history_path_for(plan_id: str) -> Path:
    """Append-only JSONL history of every successful card push.

    One line per push with the same schema as
    :func:`last_pushed_card_path_for` (without ``card.summary`` to
    keep the line small; full summary is in the latest snapshot
    file). Operators can grep / jq / diff to see the timeline of
    what was rendered when.
    """
    return plan_dir_for(plan_id) / "notifications" / "pushed_card_history.jsonl"


# ---------------------------------------------------------------------------
# Plan list for startup sweep
# ---------------------------------------------------------------------------


def list_active_plan_ids() -> list[str]:
    """Enumerate ACTIVE plan ids whose cards the startup sweep should refresh.

    Used at notifier startup to enqueue one ``plan_phase_changed``
    per active plan so the user sees a fresh card after every
    server restart. Plans whose state-machine row says "terminal"
    are filtered later by the notifier (a terminal plan will not
    be re-pushed because its last_pushed_fingerprint already
    matches).

    **Important** (2026-09-06): we only sweep plans
    that are CURRENTLY doing work — i.e. ``current_phase`` is one of
    the two phases that actually produce card content
    (``executing`` or ``verification_running``). Interview /
    prd_generation / arch_* / test_* / task_generation plans have
    not yet emitted any task or VP events, so refreshing their
    cards produces a blank progress card with no useful
    information — exactly the kind of "spammy historical
    notification" the user explicitly rejected ("过期卡片就不应该
    弹出来消息了").

    Additionally, plans not updated in the last
    ``STARTUP_SWEEP_MAX_AGE_DAYS`` days are skipped. This catches
    interview-phase plans that were started months ago and never
    progressed past the bootstrap prompt — they're "active" by
    phase but stale by recency, and a card push would only confuse
    the operator.

    Returns plan_id strings sorted alphabetically so the startup
    sweep is deterministic across restarts.
    """
    plans_dir = _plans_dir()
    state_db = _state_db()
    if not plans_dir.exists() or not state_db.exists():
        return []
    cutoff = time.time() - STARTUP_SWEEP_MAX_AGE_DAYS * 86400
    try:
        with sqlite3.connect(str(state_db)) as conn:
            rows = conn.execute(
                "SELECT plan_id, updated_at FROM plan_routing "
                "WHERE current_phase IN ({})".format(
                    ",".join("?" for _ in _ACTIVE_PHASES)
                ),
                tuple(_ACTIVE_PHASES),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        logger.warning("could not read state machine for sweep filter: %s", exc)
        return []

    out: list[str] = []
    for plan_id, updated_at in rows:
        if not plan_id:
            continue
        if _looks_like_fixture_plan(plan_id):
            continue
        try:
            ts = datetime.fromisoformat(
                updated_at.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            ts = 0.0
        if ts < cutoff:
            continue
        out.append(plan_id)
    return sorted(out)


#: 终态 phase。用于 :func:`list_stranded_card_plan_ids` —— 只有已经终了的
#: 计划才可能"卡片说在跑、其实早停了"。
#:
#: 2026-09-17 (schema v5): 这两个值以前是 routing 词汇的 ``terminal_done``
#: / ``terminal_failed``，它们各自是多对一投影 —— ``terminal_failed``
#: 覆盖 ``failed`` / ``stopped`` / ``verification_failed`` /
#: ``verification_loop_stopped`` 四个 phase。合并成 ``current_phase``
#: 一列之后必须逐个列出，否则漏掉的那几个 phase 会被这个扫描无视，
#: 卡片就会一直搁浅。
_TERMINAL_PHASES: tuple[str, ...] = (
    "completed",
    "verification_passed",
    "failed",
    "stopped",
    "verification_failed",
    "verification_loop_stopped",
)


def list_stranded_card_plan_ids() -> list[str]:
    """Enumerate plans whose pushed card is **stale relative to their state**.

    2026-09-16（搁浅在「执行中」的卡片）::

    现象：卡片里任务的最后更新时间停在上一次推送，标题却仍显示「执行中」，
    而实际执行早已结束。

    成因：修复执行被 server 重启打断 —— shutdown 顺手终止了它的子进程，DB
    正常写成 ``terminal_failed``，但**没有任何事件去推卡片**，于是卡片永远
    停在最后一次推送的那一版「执行中」。

    判据（**三条全中**才算搁浅，刻意收得很紧）：

      1. ``plan_routing.current_phase`` 已是终态；
      2. 卡片确实推成功过（``message_id`` 或 ``telegram_message_id`` 存在）；
      3. ``last_push_ts < updated_at`` —— 卡片**早于**计划最后一次状态变更。

    第 3 条是重点。**不能**只按"推过卡片"来扫：那样会在每次重启时命中两百
    多个历史计划，正是 2026-09-06 那条反馈（"过期卡片就不应该弹出来消息了"）
    要避免的。而正常终了的计划，它的终态转换会推一次卡片，于是
    ``last_push_ts`` 必然晚于 ``updated_at``，第 3 条自然把它排除掉 ——
    只有"被强行打断、没人推"的那些才落进来。
    """
    plans_dir = _plans_dir()
    state_db = _state_db()
    if not plans_dir.exists() or not state_db.exists():
        return []
    try:
        with sqlite3.connect(str(state_db)) as conn:
            rows = conn.execute(
                "SELECT plan_id, updated_at FROM plan_routing "
                "WHERE current_phase IN ({})".format(
                    ",".join("?" for _ in _TERMINAL_PHASES)
                ),
                tuple(_TERMINAL_PHASES),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        logger.warning("could not read state machine for stranded cards: %s", exc)
        return []

    out: list[str] = []
    for plan_id, updated_at in rows:
        if not plan_id:
            continue
        if _looks_like_fixture_plan(plan_id):
            continue
        try:
            state_ts = datetime.fromisoformat(
                updated_at.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            continue
        try:
            data = json.loads(
                card_state_path_for(plan_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if not (data.get("message_id") or data.get("telegram_message_id")):
            continue
        last_push = data.get("last_push_ts")
        if isinstance(last_push, (int, float)) and float(last_push) < state_ts:
            out.append(plan_id)
    return sorted(out)


# ---------------------------------------------------------------------------
# Data fetch (HTTP to localhost backend)
# ---------------------------------------------------------------------------
#
# TODO(perf): the long-term move is to extract the summary / progress /
# verification-progress builders from server.py into plain functions
# under ``backend.notifications.data_providers`` and call those
# directly, eliminating the HTTP hop. Until then, the worker thread
# uses urllib (already imported, zero new deps) to hit the same
# endpoints the bridge used to poll. Localhost round-trip is ~1ms.

DEFAULT_BACKEND_BASE = os.environ.get(
    "BACKEND_API_BASE_URL", "http://127.0.0.1:8000"
).rstrip("/")


def fetch_plan_status(
    plan_id: str, base_url: str = DEFAULT_BACKEND_BASE
) -> Optional[dict]:
    """Return the body of ``GET /api/plan/{id}/status`` or ``None``.

    2026-09-23: the notifier used to rebuild a card from **three** reads
    — ``/summary`` + ``/execution/progress`` + ``/verification/progress``
    — taken at three instants, and then re-derived a verdict from the
    mixture. That is how the header came to describe a plan that did not
    exist (``plan_status`` documents the incident). The status endpoint
    returns all of it plus the verdict, read once, server-side.

    The response carries the same sub-payload shapes the three endpoints
    return, under ``summary`` / ``execution`` / ``verification``, so the
    card body keeps working unchanged.
    """
    encoded = urllib.parse.quote(plan_id, safe="")
    return _get_json(f"{base_url}/api/plan/{encoded}/status")


def fetch_summary(plan_id: str, base_url: str = DEFAULT_BACKEND_BASE) -> Optional[dict]:
    """Return the body of ``GET /api/plan/{id}/summary`` or ``None``.

    Returns ``None`` on any error (404, network, timeout) — the
    notifier treats this as "skip this rebuild cycle" and the plan
    is retried on the next event. Never raises.

    Plan ids often contain CJK characters or other non-ASCII bytes;
    we percent-encode them per RFC 3986 (``safe=""`` so ``/`` and
    ``:`` are also encoded — ``plan_id`` may legitimately contain
    ``:`` for legacy plans truncated to ``(`` on macOS APFS, but
    never ``/``).
    """
    encoded = urllib.parse.quote(plan_id, safe="")
    return _get_json(f"{base_url}/api/plan/{encoded}/summary")


def fetch_execution_progress(
    plan_id: str, base_url: str = DEFAULT_BACKEND_BASE
) -> Optional[dict]:
    """Return the body of ``GET /api/execution/{id}/progress`` or ``None``."""
    encoded = urllib.parse.quote(plan_id, safe="")
    return _get_json(f"{base_url}/api/execution/{encoded}/progress")


def fetch_verification_progress(
    plan_id: str, base_url: str = DEFAULT_BACKEND_BASE
) -> Optional[dict]:
    """Return the body of ``GET /api/verification/{id}/progress`` or ``None``.

    A ``None`` here usually means the verification round hasn't
    started yet for this plan — the notifier falls back to the
    progress card (which still renders the execution state) rather
    than treating it as a hard error.
    """
    encoded = urllib.parse.quote(plan_id, safe="")
    return _get_json(f"{base_url}/api/verification/{encoded}/progress")



def _api_headers() -> Dict[str, str]:
    """Headers for a call back into this backend's own HTTP API.

    The notifier reads plan state over HTTP rather than importing the
    server, so it is a *client* of the guarded ``/api/*`` surface and has
    to satisfy the request guard like any other caller.

    It did not, until 2026-09-26: the guard answers 403 to a request with
    no ``X-PDT-Request`` header, ``_get_json`` turns an ``HTTPError`` into
    ``None``, and every caller reads ``None`` as "no state yet, retry next
    tick". The execution watch therefore never saw a fingerprint change
    and never refreshed a card — the whole push path was dead on a
    correctly-configured machine, and its only symptom was silence.
    """
    return {"Accept": "application/json", REQUEST_HEADER: REQUEST_HEADER_VALUE}

def _get_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers=_api_headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        # 404 is a normal "no progress yet" signal; warn at DEBUG
        # so a fresh-plan startup doesn't spam ERROR.
        logger.debug(
            "GET %s -> HTTP %s", url, exc.code,
        )
        return None
    except Exception as exc:
        logger.warning("GET %s failed: %s", url, exc)
        return None