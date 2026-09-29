"""跨轮失败历史 —— 修复 prompt 的"上次方案无效"反馈来源。

2026-09-14：执行任务失败后，把上一次失败的原因放回 prompt 再重试，这条
链路原本是不通的。

确实是断的。原有机制（``orchestrator._failure_history`` →
``previous_failure_feedback`` → ``repair_generator`` 的
"⚠️ 上次修复尝试未生效" 区块）只活在 **orchestrator 实例的内存**里，
而自动循环的修复→执行→再验证这条链每轮都会
``VerificationOrchestrator(...)`` 新建实例（``server.py`` 的
``_on_repair_complete`` 回调），所以那份内存历史每次都是空的 ——
那段"不要重复老方案"的提示在生产路径上从未真正出现。

本模块把这份历史落到磁盘，让谁新建 orchestrator 都能读回来:

  ``plans/{plan_id}/verification_failure_history.json``::

      {
        "version": 1,
        "plan_id": "...",
        "vp_history": {
          "VP-023": [
            {"round": 2, "actual_result": "...", "evidence": "..."},
            {"round": 3, "actual_result": "...", "evidence": "...",
             "repair_task_id": "repair-r3-01",
             "repair_test_command": "pytest tests/ -v",
             "repair_status": "failed",
             "repair_failure_reason": "..."}
          ]
        }
      }

除了逐轮回写，``seed_from_verdicts`` 可以从 state.db 的
``plan_verification.verdicts``（跨轮累积的追加列表）为**既有计划**
一次性播种，这样长跑中的计划不用等一轮才有反馈。种子条目的
``round`` 为 ``None``、改带 ``label``（"既往失败 #3"）—— verdicts
没有轮次字段，用序号表达才诚实。

写入是"合并 + 幂等"的: 同一 ``(vp_id, round)`` 只保留一条；已通过的
VP 的历史在 ``prune`` 时丢弃，避免 prompt 被陈旧失败污染。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: 与 ``verification_plan.json`` 同目录的特性产物（不是状态镜像 ——
#: 它记录的是历史事实，不参与状态机判定）。
HISTORY_FILENAME = "verification_failure_history.json"

_SCHEMA_VERSION = 1

#: 每个 VP 最多保留多少条历史。修复 prompt 会把它们全部渲染出来，
#: 无上限会让长跑计划的 prompt 线性膨胀。
MAX_ENTRIES_PER_VP = 5


def history_path(plan_dir: Path) -> Path:
    return Path(plan_dir) / HISTORY_FILENAME


def load(plan_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Read ``{vp_id: [entry, ...]}``; ``{}`` when absent/corrupt."""
    path = history_path(plan_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    vp_history = data.get("vp_history") if isinstance(data, dict) else None
    if not isinstance(vp_history, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for vp_id, entries in vp_history.items():
        if not isinstance(entries, list):
            continue
        cleaned = [e for e in entries if isinstance(e, dict)]
        if cleaned:
            out[str(vp_id)] = cleaned
    return out


def save(plan_dir: Path, history: Mapping[str, Sequence[Dict[str, Any]]]) -> None:
    """Atomically persist the history (tmp + rename).

    Best-effort: a failed write must never break the repair chain — the
    prompt simply falls back to the in-memory round data, exactly as
    before this module existed.
    """
    plan_dir = Path(plan_dir)
    payload = {
        "version": _SCHEMA_VERSION,
        "plan_id": plan_dir.name,
        "vp_history": {
            str(vp): list(entries)[-MAX_ENTRIES_PER_VP:]
            for vp, entries in history.items()
            if entries
        },
    }
    try:
        plan_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(plan_dir), prefix=".failure_history.", suffix=".tmp"
        )
        os.close(fd)
        try:
            Path(tmp).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            Path(tmp).replace(history_path(plan_dir))
        except Exception:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 — best-effort persistence
        print(f"[FailureHistory] save failed for {plan_dir.name}: {exc}")


def merge_round(
    history: Dict[str, List[Dict[str, Any]]],
    round_failed_vps: Mapping[str, Mapping[str, Any]],
    round_number: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """Record this round's failures into ``history`` (in place).

    Idempotent on ``(vp_id, round)`` — one entry per pair, never two.
    Re-running the same round's ``check_cycle_conditions`` (retry,
    resume, duplicate callback) therefore does not duplicate anything.

    **Last write for a round wins.** Before 2026-09-19 a second write for
    the same ``(vp_id, round)`` was *skipped*, which turned "re-run round
    N" into "keep round N's oldest verdict forever". That is what a
    counter reset produces: the reset hands the plan a fresh batch
    starting at round 1, so the new round 1 collides with the previous
    batch's round 1 and its failure reason was silently discarded while
    the stale one stayed in the repair prompt. Overwriting keeps the
    idempotency guarantee the docstring always claimed and makes the
    freshest verdict the one that survives.

    Entries keep only the fields the repair prompt renders, plus any
    extra keys the caller supplies.
    """
    for vp_id, vp_data in round_failed_vps.items():
        entries = history.setdefault(str(vp_id), [])
        entry: Dict[str, Any] = {
            "round": int(round_number),
            "actual_result": str(vp_data.get("actual_result", "") or ""),
            "evidence": str(vp_data.get("evidence", "") or ""),
        }
        for key in ("repair_task_id", "repair_test_command",
                    "repair_status", "repair_failure_reason"):
            if key in vp_data and vp_data[key]:
                entry[key] = vp_data[key]
        for index, existing in enumerate(entries):
            if existing.get("round") == round_number:
                entries[index] = entry
                break
        else:
            entries.append(entry)
    return history


def prune(
    history: Dict[str, List[Dict[str, Any]]], current_failed_ids: Iterable[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Drop VPs that are no longer failing (in place)."""
    keep = {str(v) for v in current_failed_ids}
    for vp_id in list(history.keys()):
        if vp_id not in keep:
            history.pop(vp_id, None)
    return history


def _load_plan_tasks(db_path: Path, plan_id: str) -> Dict[str, Dict[str, Any]]:
    """``task_id -> row dict`` from ``plan_tasks``; ``{}`` on any failure."""
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
    except Exception:
        return {}
    try:
        conn = open_db(Path(db_path))
    except Exception as exc:  # noqa: BLE001
        print(f"[FailureHistory] cannot open state.db for outcomes: {exc}")
        return {}
    try:
        # ``load_all`` decodes the JSON-encoded columns for us (``_decode_field``);
        # raw SQL on this connection returns plain tuples.
        tasks = PlanTaskRepository(conn).load_all(plan_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[FailureHistory] load_all failed for {plan_id}: {exc}")
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not isinstance(tasks, dict):
        return {}
    return {
        str(task_id): task
        for task_id, task in tasks.items()
        if isinstance(task, dict)
    }


def load_task_titles(db_path: Path, plan_id: str) -> Dict[str, str]:
    """``task_id -> title`` for every ``plan_tasks`` row of ``plan_id``.

    2026-09-20. Two callers, one read:

    * ``repair_generator.next_repair_seq_base`` needs the ids already
      allocated for the round it is about to emit, so a second batch
      cannot overwrite the first batch's rows.
    * ``repair_generator.cross_store_task_conflicts`` needs the titles
      to notice when ``tasks.json`` and ``plan_tasks`` disagree about
      what an id means.

    Empty dict on any failure — callers degrade to the pre-existing
    behaviour rather than stranding the repair round.
    """
    return {
        task_id: str(task.get("title") or "")
        for task_id, task in _load_plan_tasks(db_path, plan_id).items()
    }


def load_repair_outcomes(
    db_path: Path, plan_id: str,
) -> Dict[str, Dict[str, Any]]:
    """``vp_id -> repair-task execution outcome`` from state.db.

    2026-09-14:
    the second half of the feedback loop is what happened to the repair
    task the previous round GENERATED. Repair tasks live in
    ``plan_tasks`` (the executor reconciles them into the DAG from there —
    they are not in ``tasks.json``), so this reads that table: for every
    row carrying a ``failed_vp_id``, the newest round wins.

    Returns ``{}`` on any failure — the prompt simply omits the block.
    """
    outcomes: Dict[str, Dict[str, Any]] = {}
    for task_id, task in _load_plan_tasks(db_path, plan_id).items():
        vp_id = str(task.get("failed_vp_id") or "")
        if not vp_id:
            continue
        try:
            row_round = int(task.get("round") or 0)
        except (TypeError, ValueError):
            row_round = 0
        previous = outcomes.get(vp_id)
        if previous is not None and previous.get("_round", 0) >= row_round:
            continue
        outcomes[vp_id] = {
            "_round": row_round,
            "repair_task_id": task_id,
            "repair_task_title": str(task.get("title") or "")[:200],
            "repair_test_command": str(task.get("test_command") or "")[:300],
            "repair_status": str(task.get("status") or ""),
            "repair_failure_reason": str(task.get("failure_reason") or "")[:400],
            "repair_attempt": task.get("attempt"),
        }
    for outcome in outcomes.values():
        outcome.pop("_round", None)
    return outcomes


def seed_from_verdicts(
    verdicts: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Build a history from state.db's accumulated verdict list.

    ``plan_verification.verdicts`` is append-only across rounds, so its
    order is the attempt order. Verdicts carry no round number, so each
    entry is labelled by its ordinal per VP ("既往失败 #2") rather than
    fabricating a round. Only non-success verdicts become history —
    PASSED entries are noise for a "don't repeat the failed approach"
    prompt.

    Keeps the last :data:`MAX_ENTRIES_PER_VP` failures per VP.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(verdicts, (list, tuple)):
        return grouped
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            continue
        status = str(verdict.get("status", "") or "").upper()
        if status not in ("FAILED", "BLOCKED", "SKIPPED"):
            continue
        vp_id = str(verdict.get("vp_id", "") or "")
        if not vp_id:
            continue
        grouped.setdefault(vp_id, []).append({
            "reason": str(verdict.get("reasons") or ""),
            "evidence": str(verdict.get("evidence") or ""),
        })
    history: Dict[str, List[Dict[str, Any]]] = {}
    for vp_id, failures in grouped.items():
        entries: List[Dict[str, Any]] = []
        for index, failure in enumerate(failures[-MAX_ENTRIES_PER_VP:], start=1):
            entries.append({
                "round": None,
                "label": f"既往失败 #{index}",
                "actual_result": failure["reason"][:600],
                "evidence": failure["evidence"][:400],
            })
        history[vp_id] = entries
    return history
