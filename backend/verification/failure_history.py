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
            {"round": 2, "actual_result": "...", "evidence": "...",
             "failure_class": "deliverable_defect",
             "failure_class_source": "defaulted"},
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

失败"性质"也在历史里
======================

历史原本只记录"又失败了"，不记录"怎么失败的"。分诊要的恰恰是后者：
同一个 VP 连着三轮失败，可能是第三轮才第一次变成 ``criterion_defect``
（判据本身有问题，repair 再修也没用），也可能一直是
``deliverable_defect``（产物确实错了，repair 值得再试）。这两种情况在
旧记录里长得一模一样，分诊因此读不出性质漂移。

所以每条记录带上 ``failure_class`` 与 ``failure_class_source`` 两个键，
取值来自 ``verification.failure_triage`` 的四类词汇表
（``infra`` / ``deliverable_defect`` / ``criterion_defect`` /
``out_of_scope``）。缺字段的历史文件读回来不崩，按
``deliverable_defect`` 解读 —— 那是计数的那一档，保守。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from verification.failure_triage import (
    ALL_FAILURE_CLASSES,
    CRITERION_DEFECT,
    DELIVERABLE_DEFECT,
    INFRA,
    SOURCE_DEFAULTED,
    SOURCE_DETERMINISTIC,
)

#: 与 ``verification_plan.json`` 同目录的特性产物（不是状态镜像 ——
#: 它记录的是历史事实，不参与状态机判定）。
HISTORY_FILENAME = "verification_failure_history.json"

#: 计划文件的名字 —— 它是"这个计划定义了哪些验证点"的唯一权威。
PLAN_FILENAME = "verification_plan.json"

#: 分派在飞行中被取消时，记录里留下的两句话。两句都要：单说
#: "verification cancelled" 可能是在评述产物，单说
#: "dispatch was in flight" 可能只是转述。两句同时在，才是"这次尝试
#: 被取消了，交付物从未被判过"。
DISPATCH_CANCELLED_MARKERS = (
    "verification cancelled",
    "dispatch was in flight",
)

#: 这两类失败会被**对账**掉：记录保留（审计不丢），但不再进入失败
#: 集合。``criterion_defect`` 是判据本身坏了（要人重新表达判据），
#: ``infra`` 是这次尝试压根没跑成（该重试的是验证本身）。
SUPERSEDED_FAILURE_CLASSES = frozenset({CRITERION_DEFECT, INFRA})

#: 被对账掉的记录上写着的话 —— 说的是**人要做的动作**，不是"再修一次"。
_SUPERSEDED_REASONS = {
    CRITERION_DEFECT: (
        "判据本身有缺陷（criterion_defect）：该验证点没有可核的判据，"
        "请作废它或由计划属主重新表达判据；执行器不得自行发明判据，"
        "因此不生成修复任务"
    ),
    INFRA: (
        "环境失败（infra）：这次尝试没跑成（分派在飞行中被取消），"
        "该重试的是验证本身而不是重做产物，因此不生成修复任务"
    ),
}

_SCHEMA_VERSION = 1

#: 失败记录缺分类时写下的那一档 —— 计数的那一档，也就是人该看到的那
#: 一档。取自 ``verification.failure_triage.DELIVERABLE_DEFECT``，不是
#: 另抄一份字面量：分诊读历史时用的就是那一张表，两边各写一份迟早
#: 漂移。``tests/unit/test_failure_history_class.py`` 把这个字面量钉住。
DEFAULT_FAILURE_CLASS = DELIVERABLE_DEFECT

#: 失败记录缺来源时写下的那一档 —— 分类是**假定**的，不是挣来的。
#: 取自 ``verification.failure_triage.SOURCE_DEFAULTED``。
DEFAULT_FAILURE_CLASS_SOURCE = SOURCE_DEFAULTED

#: 历史记录允许携带的分类。表外的东西按
#: :data:`DEFAULT_FAILURE_CLASS` 解读：分诊只认那四类，别的名字到了
#: 它那里没有处置动作。
KNOWN_FAILURE_CLASSES = frozenset(ALL_FAILURE_CLASSES)


#: 每个 VP 最多保留多少条历史。修复 prompt 会把它们全部渲染出来，
#: 无上限会让长跑计划的 prompt 线性膨胀。
MAX_ENTRIES_PER_VP = 5

#: 单条历史里 ``actual_result`` / ``evidence`` 的字符上限。
#: 2026-10-11: 原为 600 / 400。``actual_result`` 现在会带上子 agent
#: 的全部 verdict 判据（失败的那条排在最后），600 的切片恰好切掉
#: "哪条判据没达标"这一段，修复 agent 读到的是一份"全部判据都通过"
#: 的历史。8000 与 ``verification_agent`` / ``repair_generator`` 对齐。
MAX_HISTORY_TEXT_CHARS = 8000


def _clamp_history_text(text: str, label: str) -> str:
    """截断时**声明**截断，而不是静默丢弃。

    静默切片和"原文就这么多"在读者眼里没有区别：修复 agent 会把一段
    残缺当成完整来推理，然后照着残缺去修。丢掉尾巴可以接受，丢掉
    "尾巴被丢了"这个事实不行，所以声明里带上原长。
    """
    rendered = str(text or "")
    if len(rendered) <= MAX_HISTORY_TEXT_CHARS:
        return rendered
    notice = (
        f"…[{label} 已截断，原长 {len(rendered)} 字符，"
        f"此处保留前 {MAX_HISTORY_TEXT_CHARS}]"
    )
    return rendered[:MAX_HISTORY_TEXT_CHARS] + notice


def history_path(plan_dir: Path) -> Path:
    return Path(plan_dir) / HISTORY_FILENAME


def _normalize_failure_class(value) -> str:
    """The canonical class for ``value``.

    A missing or unrecognised class reads as
    :data:`DEFAULT_FAILURE_CLASS` — the class that counts. That is the
    only class whose verdict is safe to act on when nobody has said
    otherwise: the other three either do not count or demand a human,
    and reaching one of those by accident is the expensive mistake this
    default exists to prevent.
    """
    if value is None:
        return DEFAULT_FAILURE_CLASS
    key = re.sub(r"[\s\-]+", "_", str(value).strip().lower()).strip("_")
    if key in KNOWN_FAILURE_CLASSES:
        return key
    return DEFAULT_FAILURE_CLASS


def _normalize_failure_class_source(value) -> str:
    """The canonical source for ``value``.

    A missing or blank source reads as
    :data:`DEFAULT_FAILURE_CLASS_SOURCE` — the class was assumed rather
    than earned. A source that IS stated is kept (lower-cased): it says
    where the class came from, and a provenance tag the caller knows and
    this module has never heard of is still information. Discarding it
    would turn "we do not know" into "nobody looked", which is a
    different claim.
    """
    text = str(value or "").strip().lower()
    return text or DEFAULT_FAILURE_CLASS_SOURCE


def failure_class_of(
    entry: Any,
    *,
    defined_vp_ids: Optional[Iterable[str]] = None,
    vp_id: Optional[str] = None,
) -> str:
    """The class a history record carries.

    A record that **names** a class keeps it — the caller earned it. A
    record that names none is judged by the rules in
    :func:`rule_failure_class`, and only then falls back to
    :data:`DEFAULT_FAILURE_CLASS` (``deliverable_defect``).

    The rules are what stop a failure that was never judged from
    becoming a repair task (2026-10-25, VP-031):

    * the point has **no definition** in the plan → ``criterion_defect``:
      there is no criterion to check, so reworking the deliverable
      solves nothing;
    * the attempt was **cancelled in flight** → ``infra``: nothing was
      judged, so the remedy is another attempt.

    The default is still ``deliverable_defect`` — the class that counts,
    and the conservative reading of "nobody said otherwise". It is the
    *last* branch, not the first.

    Never raises — a corrupt record reads as the default.
    """
    return classify_failure(
        entry, defined_vp_ids=defined_vp_ids, vp_id=vp_id,
    )[0]


def failure_class_source_of(
    entry: Any,
    *,
    defined_vp_ids: Optional[Iterable[str]] = None,
    vp_id: Optional[str] = None,
) -> str:
    """The source a history record carries.

    ``defaulted`` when the record names none — see
    :func:`_normalize_failure_class_source`. A class the **rules**
    produced carries :data:`SOURCE_DETERMINISTIC` instead: it was
    earned by a rule, not assumed.

    Never raises.
    """
    return classify_failure(
        entry, defined_vp_ids=defined_vp_ids, vp_id=vp_id,
    )[1]


def _stated_failure_class(entry: Any) -> Optional[str]:
    """The class the record names, or ``None`` when it names none we know.

    "None we know" covers both a missing key and a spelling outside the
    vocabulary: a class nobody can act on is not a class. An unknown
    spelling is deliberately *not* folded into the default here — the
    rules get to look at the record first (see
    :func:`classify_failure`).
    """
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("failure_class")
    if raw is None:
        return None
    key = re.sub(r"[\s\-]+", "_", str(raw).strip().lower()).strip("_")
    return key if key in KNOWN_FAILURE_CLASSES else None


def _entry_vp_id(entry: Any, vp_id: Optional[str] = None) -> str:
    """The verification point a record is about.

    The id may be given by the caller (the history keys its records by
    id) or carried by the record itself (the report's verdict rows name
    their own ``id``).
    """
    if vp_id:
        return str(vp_id).strip()
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("id") or entry.get("vp_id") or "").strip()


def _entry_text(entry: Any) -> str:
    """Everything a record says, joined into one searchable blob.

    The fields are the ones a failure writes its story into. A record
    that says nothing carries no text — and therefore matches no rule.
    """
    if not isinstance(entry, Mapping):
        return ""
    parts: List[str] = []
    for key in (
        "actual_result",
        "actual_result_summary",
        "evidence",
        "evidence_summary",
        "reason",
        "reasons",
        "message",
        "output",
    ):
        value = entry.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def is_dispatch_cancelled(entry: Any) -> bool:
    """Whether the record says the attempt was cancelled while in flight.

    The wording is literal: the two phrases the cancellation path
    writes (:data:`DISPATCH_CANCELLED_MARKERS`), case-insensitively.
    Such an attempt never reached a verdict — nothing about the
    deliverable is known — so it is not a deliverable defect.
    """
    text = _entry_text(entry).lower()
    if not text:
        return False
    return all(marker in text for marker in DISPATCH_CANCELLED_MARKERS)


def rule_failure_class(
    entry: Any,
    *,
    defined_vp_ids: Optional[Iterable[str]] = None,
    vp_id: Optional[str] = None,
) -> Optional[str]:
    """The class the **rules** give a record, or ``None``.

    ``None`` means the rules have no opinion — the caller then keeps the
    default. The rules are, in order:

    1. the point has **no definition** in the plan → ``criterion_defect``;
    2. the attempt was **cancelled in flight** → ``infra``.

    Rule 1 comes first because it is the durable fact: a point with no
    definition stays undefined until the plan's owner writes one, while
    a cancellation is a property of one attempt. A record that is both
    undefined and cancelled is undefined — retrying it would run the
    same missing criterion again.

    ``defined_vp_ids`` is ``None`` when the plan could not be read: an
    unknown plan is not an empty plan, so rule 1 stays silent then.
    """
    point = _entry_vp_id(entry, vp_id)
    if point and defined_vp_ids is not None:
        # A set is used as-is (membership only) — copying it per call
        # would be O(plan size) for every record. Anything else is
        # materialised once, so a generator is not consumed by the
        # membership test.
        known = (
            defined_vp_ids
            if isinstance(defined_vp_ids, (set, frozenset))
            else set(defined_vp_ids)
        )
        if point not in known:
            return CRITERION_DEFECT
    if is_dispatch_cancelled(entry):
        return INFRA
    return None


def classify_failure(
    entry: Any,
    *,
    defined_vp_ids: Optional[Iterable[str]] = None,
    vp_id: Optional[str] = None,
) -> Tuple[str, str]:
    """``(failure_class, failure_class_source)`` for one record.

    A record that names a class keeps it — the caller earned that class,
    and this function does not second-guess it. A record that names none
    is judged by :func:`rule_failure_class`; only a record the rules
    have no opinion about reads as the default
    (``deliverable_defect`` / ``defaulted``).
    """
    stated = _stated_failure_class(entry)
    if stated is not None:
        return stated, _stated_class_source(entry)
    ruled = rule_failure_class(
        entry, defined_vp_ids=defined_vp_ids, vp_id=vp_id,
    )
    if ruled is not None:
        return ruled, SOURCE_DETERMINISTIC
    return DEFAULT_FAILURE_CLASS, DEFAULT_FAILURE_CLASS_SOURCE


def _stated_class_source(entry: Any) -> str:
    """The source a record that **names** its class carries.

    ``defaulted`` when the record names none — see
    :func:`_normalize_failure_class_source`. Never raises.
    """
    if not isinstance(entry, Mapping):
        return DEFAULT_FAILURE_CLASS_SOURCE
    return _normalize_failure_class_source(entry.get("failure_class_source"))


def _read_file(plan_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Every record in the history file, reconciled-away ones included.

    The raw view — the audit view. :func:`load` filters it.
    """
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


def load(
    plan_dir: Path,
    *,
    include_superseded: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Read ``{vp_id: [entry, ...]}``; ``{}`` when absent/corrupt.

    The default view is the **live** one: the records that were
    reconciled away (see :func:`reconcile_history`) are left out, so a
    failure nobody may repair stops counting as a failure. They are
    still in the file — ``include_superseded=True`` reads them back for
    the audit.
    """
    raw = _read_file(plan_dir)
    if include_superseded:
        return raw
    live: Dict[str, List[Dict[str, Any]]] = {}
    for vp_id, entries in raw.items():
        kept = [e for e in entries if not is_superseded(e)]
        if kept:
            live[vp_id] = kept
    return live


def is_superseded(entry: Any) -> bool:
    """Whether the record was reconciled away.

    A reconciled record is kept in the file — it happened, and it is
    what an operator asks for — but it is not a live failure any more.
    """
    if not isinstance(entry, Mapping):
        return False
    return bool(entry.get("superseded_by"))


def mark_superseded(entry: Dict[str, Any], failure_class: str) -> None:
    """Mark the record as reconciled away, in place.

    ``superseded_by`` says **what** superseded it (the class that was
    judged); ``superseded_reason`` says what a human has to do about
    it. Both are written on the record itself, so the file explains
    itself without anybody having to remember the rules.
    """
    entry["superseded_by"] = str(failure_class or "")
    if not entry.get("superseded_reason"):
        entry["superseded_reason"] = _SUPERSEDED_REASONS.get(
            str(failure_class or ""),
            "该失败不需要重做产物；由操作者处置",
        )


def _entry_round(entry: Any) -> Optional[int]:
    """The round the record belongs to, or ``None``."""
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("round")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _same_round(left: Any, right: Any) -> bool:
    """Whether two records describe the same round of the same point."""
    left_round = _entry_round(left)
    right_round = _entry_round(right)
    if left_round is None or right_round is None:
        return False
    return left_round == right_round


def _sorted_by_round(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows in round order; the round-less ones (seeded) come first.

    A stable sort, so two records of the same round keep their order.
    """
    return sorted(
        rows,
        key=lambda row: (
            _entry_round(row) if _entry_round(row) is not None else -1
        ),
    )


def _settle_record(
    row: Dict[str, Any],
    vp_id: str,
    defined_vp_ids: Optional[Iterable[str]],
) -> None:
    """Settle the class of one record, in place.

    Only an **assumed** class is re-judged: a record whose class was
    *earned* (the triage looked at this failure and said what it was)
    keeps it, and is never marked — a real finding is something the
    operator has to see, not something to reconcile away.

    A record whose class is the default — or missing, or spelled in a
    way the vocabulary does not carry — is judged by the rules. When
    the rules come out ``criterion_defect`` / ``infra`` the record is
    marked ``superseded_by``: it happened, it stays in the file for the
    audit, and it stops counting as a live failure.

    A record that names no class at all and that the rules have no
    opinion about is left exactly as it is — this function never adds a
    key to a record that does not have one.
    """
    stated = _stated_failure_class(row)
    source = _normalize_failure_class_source(row.get("failure_class_source"))
    assumed = stated is None or (
        stated == DEFAULT_FAILURE_CLASS and source == DEFAULT_FAILURE_CLASS_SOURCE
    )
    if not assumed:
        return
    ruled = rule_failure_class(
        row, defined_vp_ids=defined_vp_ids, vp_id=vp_id,
    )
    if ruled is None:
        # The rules have no opinion: the class stays the assumed default.
        if "failure_class" in row:
            row["failure_class"] = DEFAULT_FAILURE_CLASS
            row["failure_class_source"] = DEFAULT_FAILURE_CLASS_SOURCE
        return
    row["failure_class"] = ruled
    row["failure_class_source"] = SOURCE_DETERMINISTIC
    mark_superseded(row, ruled)


def save(plan_dir: Path, history: Mapping[str, Sequence[Dict[str, Any]]]) -> None:
    """Atomically persist the history (tmp + rename).

    Best-effort: a failed write must never break the repair chain — the
    prompt simply falls back to the in-memory round data, exactly as
    before this module existed.

    Two things happen here that did not happen before (2026-10-25):

    * the **class of a record is settled** — this is the one place that
      knows which points the plan defines, so a record whose class was
      merely *defaulted* is judged by the rules before it is written,
      and a record that comes out ``criterion_defect`` / ``infra`` is
      marked ``superseded_by``;
    * the records that were **already reconciled away are preserved** —
      they are the audit trail of a failure nobody may repair, and the
      caller (which reads the live view) never saw them.
    """
    plan_dir = Path(plan_dir)
    defined_vp_ids = plan_defined_vp_ids(plan_dir)

    rows_by_vp: Dict[str, List[Dict[str, Any]]] = {}
    for vp_id, entries in history.items():
        if not entries:
            continue
        rows: List[Dict[str, Any]] = []
        for entry in list(entries)[-MAX_ENTRIES_PER_VP:]:
            if not isinstance(entry, Mapping):
                continue
            if isinstance(entry, dict):
                # Settled in place, so the caller's view and the file
                # never disagree about what a record is.
                row = entry
            else:
                row = dict(entry)
            _settle_record(row, str(vp_id), defined_vp_ids)
            rows.append(row)
        if rows:
            rows_by_vp[str(vp_id)] = rows

    # The reconciled-away records stay in the file.
    for vp_id, entries in _read_file(plan_dir).items():
        rows = rows_by_vp.setdefault(str(vp_id), [])
        for entry in entries:
            if not is_superseded(entry):
                continue
            if any(_same_round(row, entry) for row in rows):
                # The caller's record for that round is the newer one and
                # replaces it; the round stays reconciled either way.
                continue
            rows.append(dict(entry))
        rows_by_vp[str(vp_id)] = _sorted_by_round(rows)[-MAX_ENTRIES_PER_VP:]

    payload = {
        "version": _SCHEMA_VERSION,
        "plan_id": plan_dir.name,
        "vp_history": {
            vp_id: rows for vp_id, rows in rows_by_vp.items() if rows
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


def _split_failure_item(item: Any) -> Tuple[str, Dict[str, Any]]:
    """``(vp_id, fields)`` from one record of the list form.

    The list form is the shape the verdict records arrive in: a flat
    record whose ``id`` names the verification point and whose other
    keys are the record's fields. ``vp_id`` is accepted as a synonym of
    ``id`` because the two spellings both occur in the backend, and
    guessing wrong would file the failure under a VP that does not
    exist.
    """
    if not isinstance(item, Mapping):
        return ("", {})
    vp_id = str(item.get("id") or item.get("vp_id") or "").strip()
    fields = {
        str(key): value
        for key, value in item.items()
        if key not in ("id", "vp_id")
    }
    return (vp_id, fields)


def _collect_round_failures(
    round_failed_vps: Any,
    failures: Optional[Sequence[Any]],
) -> Dict[str, Dict[str, Any]]:
    """The round's failure records, keyed by VP id.

    Two shapes arrive here. The dict form (``{vp_id: {...}}``) is what
    the orchestrator has always passed. The list form
    (``[{"id": "VP-007", ...}]``) is the shape the verdict records
    arrive in, and it is the shape that carries the class. Both are
    accepted; where both carry the same VP, the list form wins — it is
    the one that came from the verdict.
    """
    records: Dict[str, Dict[str, Any]] = {}
    if isinstance(round_failed_vps, Mapping):
        for vp_id, vp_data in round_failed_vps.items():
            records[str(vp_id)] = (
                dict(vp_data) if isinstance(vp_data, Mapping) else {}
            )
    for item in failures or []:
        vp_id, fields = _split_failure_item(item)
        if vp_id:
            records[vp_id] = fields
    return records


def merge_round(
    history: Dict[str, List[Dict[str, Any]]],
    round_failed_vps: Optional[Union[Mapping[str, Mapping[str, Any]], int]] = None,
    round_number: Optional[int] = None,
    *,
    failures: Optional[Sequence[Any]] = None,
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
    extra keys the caller supplies — and the two keys the triage reads:
    ``failure_class`` and ``failure_class_source``. A caller that names
    neither gets :data:`DEFAULT_FAILURE_CLASS` /
    :data:`DEFAULT_FAILURE_CLASS_SOURCE`: the class that counts, and a
    class nobody earned. That is the conservative pair, and it is the
    pair a legacy record (one written before the class existed) reads
    as.

    Args:
        history: The history to record into, mutated in place.
        round_failed_vps: The round's failures, keyed by VP id. This is
            the shape the orchestrator passes.
        round_number: The round the failures belong to.
        failures: The same failures in the flat list form the verdict
            records arrive in — each item names its VP under ``id`` (or
            ``vp_id``) and carries the record's fields. Accepted
            alongside ``round_failed_vps``; where both carry the same
            VP, the list form wins.

    Returns:
        ``history``, mutated in place.
    """
    if isinstance(round_failed_vps, int) and round_number is None:
        # ``merge_round(history, 4, failures=[...])`` — the round number
        # in the second position.
        round_failed_vps, round_number = None, int(round_failed_vps)

    if round_number is None:
        raise ValueError(
            "merge_round() needs the round number: "
            "merge_round(history, round_number=N, failures=[...])"
        )
    round_number = int(round_number)

    for vp_id, vp_data in _collect_round_failures(round_failed_vps, failures).items():
        entries = history.setdefault(str(vp_id), [])
        entry: Dict[str, Any] = {
            "id": str(vp_id),
            "round": round_number,
            "actual_result": str(vp_data.get("actual_result", "") or ""),
            "evidence": str(vp_data.get("evidence", "") or ""),
            "failure_class": failure_class_of(vp_data),
            "failure_class_source": failure_class_source_of(vp_data),
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


def plan_defined_vp_ids(plan_dir: Path) -> Optional[Set[str]]:
    """The verification points the plan defines, or ``None``.

    ``None`` means **unknown** — the plan file is missing or unreadable,
    or it does not define a single verification point. That is not the
    same as "this plan defines no point": an unknown plan cannot say
    that a point is undefined, so the rule that judges an undefined
    point stays silent until the plan can be read.

    A plan that parses but carries no ``verification_points`` (the key is
    absent, or the list is empty) is **unknown**, not "it defines none"
    (2026-10-25, VP-031 second pass). Reading it as "it defines none"
    would classify **every** failure in the plan as
    ``criterion_defect`` — a conclusion about the criterion that nothing
    in the plan supports, and one that turns a half-written plan into a
    batch of fake verdicts.

    The plan file is the only authority on what a point *is*: a point
    that is not in it was never written down, so there is no criterion
    to check and nothing a rework of the deliverable could satisfy.
    """
    path = Path(plan_dir) / PLAN_FILENAME
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    points = data.get("verification_points")
    if not isinstance(points, list):
        return None
    defined = {
        str(vp.get("id")).strip()
        for vp in points
        if isinstance(vp, Mapping) and str(vp.get("id") or "").strip()
    }
    if not defined:
        # A plan that names no point cannot say that a point is missing.
        return None
    return defined


def reconcile_history(
    plan_dir: Path,
    *,
    defined_vp_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Reconcile the plan's failure records against the plan itself.

    Every record the rules judge ``criterion_defect`` / ``infra`` is
    marked ``superseded_by`` and **kept** — the audit trail of a failure
    nobody may repair is exactly what an operator asks for — but it
    stops counting as a live failure: :func:`load` leaves it out of the
    active view, so the same id's old record does not come back every
    round.

    Idempotent: a record that is already marked is left alone, and
    running this twice reports the same thing.

    Args:
        plan_dir: the plan's directory — where the history and the plan
            file live.
        defined_vp_ids: the ids the plan defines. Read from the plan
            file when not given.

    Returns:
        A summary: the records it marked (``superseded``), how many
        stayed live (``live``), and whether the plan could be read
        (``plan_known``).
    """
    plan_dir = Path(plan_dir)
    history = load(plan_dir, include_superseded=True)
    if defined_vp_ids is None:
        defined_vp_ids = plan_defined_vp_ids(plan_dir)

    superseded: List[Dict[str, Any]] = []
    live = 0
    for vp_id, entries in history.items():
        for entry in entries:
            if is_superseded(entry):
                continue
            # Only the **rules** may reconcile a record away: a record
            # the triage judged (an earned class) is a real finding the
            # operator has to see, and it stays in the failed set. What
            # the rules judge is a record that was **never judged** — no
            # definition to check, or an attempt that was cancelled.
            ruled = rule_failure_class(
                entry, defined_vp_ids=defined_vp_ids, vp_id=vp_id,
            )
            if ruled is not None:
                entry["failure_class"] = ruled
                entry["failure_class_source"] = SOURCE_DETERMINISTIC
                mark_superseded(entry, ruled)
                superseded.append({
                    "id": str(vp_id),
                    "round": entry.get("round"),
                    "failure_class": ruled,
                })
            else:
                live += 1
    if superseded:
        save(plan_dir, history)
    return {
        "superseded": superseded,
        "live": live,
        "plan_known": defined_vp_ids is not None,
    }


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
            "repair_failure_reason": _clamp_history_text(
                str(task.get("failure_reason") or ""), "repair_failure_reason"
            ),
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
                "actual_result": _clamp_history_text(
                    failure["reason"], "actual_result"
                ),
                "evidence": _clamp_history_text(
                    failure["evidence"], "evidence"
                ),
            })
        history[vp_id] = entries
    return history
