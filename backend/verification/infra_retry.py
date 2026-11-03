"""跨轮 infra 重试账本 —— 给 "可以重试" 装一个边界。

为什么有这个模块
================

``verification.failure_triage`` 的分诊表给 infra 类的处置是
``retry_not_counted``：环境失败（限流、超时、传输错误、watchdog 杀进程）
不算在产物的失败账上，可以重试，而且重试**不计入**同一个失败原因的计数。

但 "可以重试" 没有边界，是这个设计里唯一没有上锁的地方：

* 没有次数上限 —— 一个每次都超时的 VP 可以被重试到天荒地老；
* 没有按 VP 记账 —— "这个 VP 已经因为 infra 失败过两次" 这件事
  没有地方可查，于是每一轮都从零开始，谁也说不清它到底试过几次。

``MAX_INFRA_CROSS_ROUND_RETRIES``（定义在 ``failure_triage``，紧挨着
分诊表）给出了次数上限；本模块给出记账的地方。两者合起来就是
arch 决策点 2 的"跨轮有界重试"。

账本长什么样
============

``plans/{plan_id}/verification_infra_retries.json``::

    {
      "version": 1,
      "vp_attempts": {
        "VP-006": [
          {"round": 1, "provider": "vendor-a-pro",
           "reason_code": "coding_tool_call_failed"},
          {"round": 2, "provider": "vendor-b-pro",
           "reason_code": "timeout"}
        ]
      }
    }

每一轮记一条：轮次、当时用的 provider、失败原因码。provider 记下来
是为了下一轮**换一家**重选（:meth:`InfraRetryLedger.excluded_providers`
给出"这个 VP 已经用过的 provider"），而不是每次都死在同一家身上。

它落在磁盘上而不是实例内存里，理由和 ``verification.failure_history``
一样：自动循环的每一轮都会新建 orchestrator 实例，存在内存里的账本
每轮都是空的。

边界
====

* 账本文件不存在 / 内容损坏 / 不是 dict → **空账本，不抛异常**。
  坏账本不应该把一整轮验证带下水；宁可当没有记录，也不能崩。
* 同一 ``(vp_id, round)`` 重复记账 → 幂等，不重复计。记账是"这一轮
  发生了这件事"，不是"这一轮被尝试了几次"；调用方重试一次记账，
  不该把重试预算吃掉。
* 并发记账 → 持锁做读改写。账本文件是所有实例共享的，所以锁是
  **模块级**的：每实例一把锁只能防住自己那次调用，两把实例级的锁
  防不住"两个实例同时改同一个文件"。
* 写盘走 ``utils.atomic_io.atomic_write_json(..., reraise=False)``：
  临时文件 + ``os.replace``，读到的永远是完整的一份；写失败不抛，
  只记日志。

跨进程的并发（多个 worker 进程写同一个 plan 目录）不在本模块的保证
范围内 —— 那需要文件锁，而本模块的调用方目前都在同一个进程里。

本模块**不接线**
================

谁来记账、谁读账本、耗尽之后怎么办（终止该 VP、转终态、还是上报），
都不在这里。这里只保证账本本身是对的。接线是后续任务的事。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import atomic_io
from verification.failure_triage import MAX_INFRA_CROSS_ROUND_RETRIES


#: 账本文件名。落在 plan 目录里，与其它 verification 产物同目录。
LEDGER_FILENAME = "verification_infra_retries.json"

#: 账本文件格式版本。
LEDGER_VERSION = 1

#: 账本里一条尝试的键。
_ROUND = "round"
_PROVIDER = "provider"
_REASON_CODE = "reason_code"

#: 模块级锁。
#:
#: 账本文件是**所有实例共享**的，所以锁必须是模块级的：每实例一把
#: 锁只能防住"这个实例的这次调用"，而两个实例各拿一把锁同时改同一
#: 个文件时，丢的是后写的那一份。
#:
#: 刻意用 ``Lock`` 而不是 ``RLock``：读方法与写方法共用这一把锁，
#: 所以任何一个持锁的方法都**不能**再去调用另一个会加锁的方法
#: （否则自死锁）。下面的实现都走不带锁的内部读取。
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 条目规范化
# ---------------------------------------------------------------------------


def _normalize_round(value: Any) -> Any:
    """The round number, as an ``int`` whenever it can be one.

    Rounds are ordinals, so ``"3"`` and ``3`` are the same round — and
    a ledger read back from disk must not treat them as two different
    ones (that would let a duplicate round slip past the idempotency
    check and eat the retry budget). A value that is not a number at
    all stays a string rather than being coerced into a wrong number.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value)


def _normalize_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """One attempt as the canonical three-key dict, or ``None``.

    The shape is fixed — ``round`` / ``provider`` / ``reason_code`` —
    so a ledger read back from disk (possibly hand-edited) reads back
    in the same shape it was written. A malformed entry is dropped
    rather than propagated: one bad row must not take the ledger with
    it.
    """
    if not isinstance(entry, dict):
        return None
    return {
        _ROUND: _normalize_round(entry.get(_ROUND)),
        _PROVIDER: str(entry.get(_PROVIDER) or ""),
        _REASON_CODE: str(entry.get(_REASON_CODE) or ""),
    }


def _same_round(left: Any, right: Any) -> bool:
    """Whether two round numbers name the same round."""
    return _normalize_round(left) == _normalize_round(right)


def _round_sort_key(entry: Dict[str, Any]) -> Tuple[int, int, str]:
    """Sort key for one attempt: by round, integers before oddities."""
    value = entry.get(_ROUND)
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, value, "")
    return (1, 0, str(value))


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


class InfraRetryLedger:
    """按 VP 记账的跨轮 infra 重试账本。

    Args:
        plan_dir: The plan's directory — the ledger lives next to the
            plan's other verification artefacts.
        max_retries: How many retries a point may make after its first
            attempt. Defaults to
            :data:`verification.failure_triage.MAX_INFRA_CROSS_ROUND_RETRIES`.
            The bound in force is the one this instance was built with;
            it is not read back from the file.
    """

    def __init__(
        self,
        plan_dir: Path,
        max_retries: int = MAX_INFRA_CROSS_ROUND_RETRIES,
    ) -> None:
        self._plan_dir = Path(plan_dir)
        self._max_retries = int(max_retries)

    # -- properties ------------------------------------------------------

    @property
    def path(self) -> Path:
        """The ledger file."""
        return self._plan_dir / LEDGER_FILENAME

    @property
    def plan_dir(self) -> Path:
        """The plan directory the ledger lives in."""
        return self._plan_dir

    @property
    def max_retries(self) -> int:
        """How many retries a point may make after its first attempt."""
        return self._max_retries

    # -- reading ---------------------------------------------------------

    def attempts(self, vp_id: str) -> List[Dict[str, Any]]:
        """Every attempt recorded for ``vp_id``, oldest round first.

        An unknown VP reads as an empty list — "never tried" and "no
        record" are the same thing to every caller here.
        """
        with _LOCK:
            payload = self._read_payload()
        entries = payload.get("vp_attempts", {}).get(str(vp_id), [])
        return [dict(entry) for entry in sorted(entries, key=_round_sort_key)]

    def is_exhausted(self, vp_id: str) -> bool:
        """Whether ``vp_id`` has used up its retry budget.

        The threshold is ``1 + max_retries``: the first attempt plus
        the retries that follow it. Two rounds recorded against the
        default bound of two retries is *not* exhausted — one retry is
        still allowed.
        """
        return len(self.attempts(vp_id)) >= 1 + self._max_retries

    def excluded_providers(self, vp_id: str) -> List[str]:
        """The providers ``vp_id`` has already been tried with.

        Deduplicated, in round order, blank providers dropped: an empty
        string is not a provider, and excluding "" from the pool would
        be meaningless.
        """
        excluded: List[str] = []
        seen = set()
        for entry in self.attempts(vp_id):
            provider = entry.get(_PROVIDER) or ""
            if not provider or provider in seen:
                continue
            seen.add(provider)
            excluded.append(provider)
        return excluded

    # -- writing ---------------------------------------------------------

    def record_attempt(
        self,
        vp_id: str,
        round_number: Any,
        provider: str = "",
        reason_code: str = "",
    ) -> Dict[str, Any]:
        """Record one infra attempt for ``vp_id`` and return the entry.

        Idempotent on ``(vp_id, round)``: recording the same round twice
        keeps one entry, so a caller that retries its bookkeeping does
        not eat the retry budget. A second record of the same round may
        fill in a blank field, but a blank field never erases a
        recorded one — "I don't know the provider" is not "the provider
        was none".
        """
        vp_key = str(vp_id)
        entry = {
            _ROUND: _normalize_round(round_number),
            _PROVIDER: str(provider or ""),
            _REASON_CODE: str(reason_code or ""),
        }

        with _LOCK:
            payload = self._read_payload()
            entries = payload["vp_attempts"].setdefault(vp_key, [])
            existing = next(
                (row for row in entries if _same_round(row.get(_ROUND), entry[_ROUND])),
                None,
            )
            if existing is None:
                entries.append(entry)
            else:
                if entry[_PROVIDER]:
                    existing[_PROVIDER] = entry[_PROVIDER]
                if entry[_REASON_CODE]:
                    existing[_REASON_CODE] = entry[_REASON_CODE]
                entry = dict(existing)
            entries.sort(key=_round_sort_key)
            atomic_io.atomic_write_json(self.path, payload, reraise=False)

        return dict(entry)

    # -- internals -------------------------------------------------------

    def _read_payload(self) -> Dict[str, Any]:
        """The ledger's content, or an empty ledger.

        A missing file, a corrupt file, a file that is not a dict — all
        of them read as an empty ledger rather than raising. A broken
        ledger must not take the verification round down with it.

        Callers hold ``_LOCK`` (or are the constructor); this method
        does no locking of its own.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return self._empty_payload()

        if not isinstance(data, dict):
            return self._empty_payload()
        raw = data.get("vp_attempts")
        if not isinstance(raw, dict):
            return self._empty_payload()

        cleaned: Dict[str, List[Dict[str, Any]]] = {}
        for vp_id, rows in raw.items():
            if not isinstance(rows, list):
                continue
            entries = [entry for entry in (_normalize_entry(row) for row in rows) if entry]
            if entries:
                cleaned[str(vp_id)] = entries
        return {"version": LEDGER_VERSION, "vp_attempts": cleaned}

    @staticmethod
    def _empty_payload() -> Dict[str, Any]:
        """A fresh, empty ledger payload."""
        return {"version": LEDGER_VERSION, "vp_attempts": {}}
