"""验证计划书的审核条目、独立审核存储与完备性判定。

这个模块解决什么
----------------
验证计划书是人工审核的**唯一**对象，它有自己的一份审核状态。这份状态不能复用
PRD 的 ``review.json``：PRD 的条目是 ``prd.json`` 的决策点，计划书的条目是计划书
自己的章节与验证点，两份文档的 id 空间、条目数量、完备性含义都不一样。塞进同一
个文件，判定就分不清「PRD 审完了」与「计划书审完了」——所以计划书的审核状态落在
自己的文件里：``verification-plan-review.json``。

审核状态与文档字节绑定
----------------------
只按条目状态判定完备性有个洞：审核通过之后文档被静默改了一个字节，条目状态还
是「全部 accepted」，判定却说「审过了」。本模块给审核状态带一个按**文件字节**
计算的 ``doc_sha256``：任何一个字节变了（包括基线行、行尾、章节顺序、空格），
全部条目就回到 ``pending``，审核不能覆盖一份已经变过的文档。

哈希按原始字节算，不做换行归一化：CRLF 与 LF 的差别是字节差别，理应让审核
失效。所以 ``is_resolved`` 收到的文档必须是「原始字节按 UTF-8 解码」的结果，
不能是经过通用换行转换读出来的文本。

条目是什么
----------
每个实质性陈述恰好属于一个条目：

* 5 个**计划级**条目：``PLAN-OBJECT``（被验证对象 + 验证范围，含 ``baseline:``
  行本身）、``PLAN-EXCLUDE``（排除范围）、``PLAN-METHOD``（验证方法）、
  ``PLAN-ENV``（环境与依赖）、``PLAN-RISK``（风险面）；
* N 个**验证点**条目：每个 ``VP-xxx`` 块一个，块内的通过判据、证据要求、预期
  结果随该条目一起审核。

「验证点清单」是容器章节，不单独成条目。计划级条目的 id 与顺序是**契约**：它们
不随文档内容变化，审核界面与报告按 id 归因。

skip 语义
---------
``resolved = status ∈ {accepted, skipped}``；``pending`` 阻断。``skipped`` 表示
「本次不验、不计失败」，但必须带非空原因——原因要进报告，没有原因的 skip 无法
归因，等于把条目悄悄扔掉。

skip 视为已解决的判定**只用于验证分支**：验证分支的最小 PRD 允许整条 skip，
而开发分支的 PRD 门禁仍只认 ``accepted``（开发分支行为不变）。判定依据是
:func:`plan_artifacts.is_verification_branch`：目录里只有验证计划书、没有
``tasks.json`` 才算验证分支；两类产物同时存在（冲突）不算，目录里两者都没有
（分支尚未落地）也保守地按开发分支处理。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from plan_artifacts import PLAN_DOC_FILENAME, is_verification_branch
from verification_plan_doc import ParsedPlan, parse_plan_doc

#: 计划书的审核状态文件名。与 PRD 的 ``review.json`` 物理分离。
REVIEW_FILENAME = "verification-plan-review.json"

#: PRD 的审核状态文件名（只读：计划书审核不写它）。
PRD_REVIEW_FILENAME = "review.json"

#: PRD 的决策点文件名（只读：用来对齐「哪些决策点必须被审过」）。
PRD_FILENAME = "prd.json"

#: 条目状态词汇。``pending`` 阻断，``accepted`` / ``skipped`` 算已解决。
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_SKIPPED = "skipped"

#: 全部合法状态。
STATUSES: Tuple[str, ...] = (STATUS_PENDING, STATUS_ACCEPTED, STATUS_SKIPPED)

#: 算「已解决」的状态集合。
RESOLVED_STATUSES: frozenset = frozenset({STATUS_ACCEPTED, STATUS_SKIPPED})

#: 审核动作 → 状态。``revise`` 不在这里：改文档是另一条链路（改文档会改哈希，
#: 审核随之失效），不在这里假装能改。
_ACTIONS: Dict[str, str] = {
    "accept": STATUS_ACCEPTED,
    "skip": STATUS_SKIPPED,
    "reset": STATUS_PENDING,
}

#: 5 个计划级审核条目：``(id, 标题)``。顺序即审核顺序，``index`` 由此定死。
#:
#: ``PLAN-OBJECT`` 覆盖「被验证对象」与「验证范围」两节——它们是同一件事的两面
#: （验什么、在哪儿验），拆成两个条目会让审核者对基线行 accept 之后又去改范围。
PLAN_LEVEL_ITEMS: Tuple[Tuple[str, str], ...] = (
    ("PLAN-OBJECT", "被验证对象"),
    ("PLAN-EXCLUDE", "排除范围"),
    ("PLAN-METHOD", "验证方法"),
    ("PLAN-ENV", "环境与依赖"),
    ("PLAN-RISK", "风险面"),
)


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass
class ReviewItem:
    """一个审核条目。

    Attributes:
        index: 条目序号（0 起）。计划级条目在前，验证点条目按文档顺序在后。
        id: 稳定 id：计划级是 ``PLAN-xxx``，验证点是文档里的 ``VP-xxx``。
        title: 给人看的标题。
        status: ``pending`` / ``accepted`` / ``skipped``。
        note: 原因。``skipped`` 时必须非空。
    """

    index: int
    id: str
    title: str
    status: str = STATUS_PENDING
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化成条目形状 ``{index, id, title, status, note}``。"""
        return {
            "index": self.index,
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewItem":
        """从落盘数据还原条目；缺字段与非法状态都退化成安全的默认值。"""
        try:
            index = int(data.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        status = str(data.get("status") or STATUS_PENDING)
        if status not in STATUSES:
            status = STATUS_PENDING
        return cls(
            index=index,
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            status=status,
            note=str(data.get("note") or ""),
        )

    def is_resolved(self) -> bool:
        """条目是否已解决（``accepted`` 或 ``skipped``）。"""
        return self.status in RESOLVED_STATUSES

    # -- 字典访问 ----------------------------------------------------------
    # 条目既要能直接 ``item.to_dict()`` 序列化给接口，也要能当字典用
    # （``item["id"]`` / ``dict(item)``）：两种用法都在下游出现过。

    def keys(self) -> List[str]:
        """条目形状的键名。"""
        return list(self.to_dict().keys())

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def get(self, key: str, default: Any = None) -> Any:
        """按键取值，缺键返回 ``default``。"""
        return self.to_dict().get(key, default)


@dataclass
class ReviewState:
    """一份审核状态。

    Attributes:
        items: 全部审核条目。
        doc_sha256: 审核所针对的文档字节的 sha256。文档变了它就对不上，
            对不上就意味着这份审核不再覆盖当前文档。
    """

    items: List[ReviewItem] = field(default_factory=list)
    doc_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化成落盘形状 ``{items: [...], doc_sha256: "..."}``。"""
        return {
            "items": [item.to_dict() for item in self.items],
            "doc_sha256": self.doc_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewState":
        """从落盘数据还原状态。"""
        raw_items = data.get("items") or []
        items: List[ReviewItem] = []
        if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
            for raw in raw_items:
                if isinstance(raw, Mapping):
                    items.append(ReviewItem.from_dict(raw))
        return cls(items=items, doc_sha256=str(data.get("doc_sha256") or ""))

    def item_by_index(self, index: int) -> Optional[ReviewItem]:
        """按序号取条目；没有则返回 ``None``。"""
        for item in self.items:
            if item.index == index:
                return item
        return None

    def reset_to_pending(self) -> "ReviewState":
        """全部条目回到 pending，原因一并清掉（回到未审的状态）。"""
        return ReviewState(
            items=[
                ReviewItem(
                    index=item.index,
                    id=item.id,
                    title=item.title,
                    status=STATUS_PENDING,
                    note="",
                )
                for item in self.items
            ],
            doc_sha256=self.doc_sha256,
        )


# ---------------------------------------------------------------------------
# 哈希与文件
# ---------------------------------------------------------------------------


def doc_sha256(doc_bytes: bytes) -> str:
    """按**原始字节**算文档哈希（不做任何换行或空白归一化）。"""
    return hashlib.sha256(doc_bytes).hexdigest()


def _text_sha256(doc_text: Any) -> str:
    """文档文本的哈希。接受 ``bytes``（直接按字节算）或 ``str``（按 UTF-8 编码算）。"""
    if isinstance(doc_text, bytes):
        return doc_sha256(doc_text)
    return doc_sha256(str(doc_text).encode("utf-8"))


def review_path(plan_dir: Path) -> Path:
    """计划书审核状态文件的路径。"""
    return Path(plan_dir) / REVIEW_FILENAME


def _current_doc_sha256(plan_dir: Path) -> str:
    """当前计划书文件的字节哈希；文件不在则返回空串。"""
    try:
        return doc_sha256((Path(plan_dir) / PLAN_DOC_FILENAME).read_bytes())
    except OSError:
        return ""


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """读 JSON 文件；不存在或损坏都返回 ``None``（读不动的东西按没有处理）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    """落盘 JSON。先写同目录临时文件再替换，避免半截文件。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 条目提取
# ---------------------------------------------------------------------------


def extract_items(parsed: ParsedPlan) -> List[ReviewItem]:
    """从解析产物里提取审核条目。

    5 个计划级条目在前（id 与顺序是契约，不随文档内容变化），随后是文档里的
    每个验证点条目（id 与标题取自文档）。

    Args:
        parsed: :func:`verification_plan_doc.parse_plan_doc` 的解析产物。

    Returns:
        审核条目列表，序号即审核顺序。
    """
    items: List[ReviewItem] = []
    for index, (item_id, title) in enumerate(PLAN_LEVEL_ITEMS):
        items.append(ReviewItem(index=index, id=item_id, title=title))
    for plan_item in parsed.items:
        items.append(
            ReviewItem(index=len(items), id=plan_item.id, title=plan_item.title)
        )
    return items


# ---------------------------------------------------------------------------
# 审核状态读写
# ---------------------------------------------------------------------------


def load(plan_dir: Path) -> Optional[ReviewState]:
    """读回审核状态。

    文档字节变了（``doc_sha256`` 对不上当前文件）时，全部条目回到 ``pending``：
    之前的审核针对的是另一份文档，不能算数。

    Args:
        plan_dir: plan 目录。

    Returns:
        审核状态；没有审核文件时返回 ``None``。
    """
    plan_dir = Path(plan_dir)
    data = _read_json(review_path(plan_dir))
    if data is None:
        return None
    state = ReviewState.from_dict(data)
    if state.doc_sha256 != _current_doc_sha256(plan_dir):
        return state.reset_to_pending()
    return state


def record_action(
    plan_dir: Path,
    item_index: int,
    action: str,
    note: Optional[str] = None,
) -> ReviewState:
    """记录一次审核动作并落盘。

    条目来自**当前文档**：审核状态是文档的派生视图，文档改了条目集合就跟着改。
    文档字节与上次审核不一致时，旧条目状态一律作废（全部回到 pending），只有
    本次动作的条目带着新状态落盘。

    Args:
        plan_dir: plan 目录。
        item_index: 条目序号（见 :func:`extract_items`）。
        action: ``accept`` / ``skip`` / ``reset``。
        note: 原因。``skip`` 必须给非空原因。

    Returns:
        落盘后的审核状态。

    Raises:
        FileNotFoundError: 计划书不存在（没有可审核的对象）。
        StructureError: 计划书结构非法（:mod:`verification_plan_doc` 抛出）。
        ValueError: 动作未知、条目序号不存在，或 ``skip`` 缺非空原因。
    """
    plan_dir = Path(plan_dir)
    doc_path = plan_dir / PLAN_DOC_FILENAME
    if not doc_path.exists():
        raise FileNotFoundError(f"验证计划书不存在：{doc_path}")

    doc_bytes = doc_path.read_bytes()
    current_sha = doc_sha256(doc_bytes)
    parsed = parse_plan_doc(doc_bytes.decode("utf-8"))
    items = extract_items(parsed)

    stored = _read_json(review_path(plan_dir))
    carried: Dict[int, Tuple[str, str]] = {}
    if stored is not None and str(stored.get("doc_sha256") or "") == current_sha:
        for raw in stored.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            carried[index] = (
                str(raw.get("status") or STATUS_PENDING),
                str(raw.get("note") or ""),
            )

    for item in items:
        status, item_note = carried.get(item.index, (STATUS_PENDING, ""))
        item.status = status if status in STATUSES else STATUS_PENDING
        item.note = item_note

    target = next((item for item in items if item.index == item_index), None)
    if target is None:
        raise ValueError(f"审核条目序号不存在：{item_index}")

    normalized = str(action or "").strip().lower()
    if normalized not in _ACTIONS:
        raise ValueError(f"未知审核动作：{action!r}（可用：{', '.join(sorted(_ACTIONS))}）")

    reason = str(note or "").strip()
    if normalized == "skip" and not reason:
        raise ValueError("skip 必须带非空原因（note）：没有原因的 skip 无法在报告里归因")

    target.status = _ACTIONS[normalized]
    target.note = "" if normalized == "reset" else reason

    state = ReviewState(items=items, doc_sha256=current_sha)
    _write_json(review_path(plan_dir), state.to_dict())
    return state


# ---------------------------------------------------------------------------
# 完备性判定
# ---------------------------------------------------------------------------


def is_resolved(state: Optional[ReviewState], doc_text: Any) -> bool:
    """计划书审核是否完备。

    完备 = 全部条目 ``accepted`` / ``skipped`` **且** 审核时的文档哈希与当前文档
    一致。两个条件缺一不可：只有状态不看哈希，审完之后改文档照样放行；只看哈希
    不看状态，一份没审过的文档也放行。

    Args:
        state: 审核状态（:func:`load` 的结果）。
        doc_text: 当前文档的原始字节（``bytes``）或按 UTF-8 解码后的原文
            （``str``）。不得做换行归一化，否则哈希对不上。

    Returns:
        完备为 ``True``。
    """
    if state is None:
        return False
    if not state.items:
        return False
    if _text_sha256(doc_text) != state.doc_sha256:
        return False
    return all(item.is_resolved() for item in state.items)


def prd_is_resolved(plan_dir: Path) -> bool:
    """PRD 审核是否已解决（skip 语义按分支区分）。

    * 验证分支（目录里只有验证计划书、没有 ``tasks.json``）：``accepted`` 与
      ``skipped`` 都算已解决——验证分支的最小 PRD 允许整条 skip；
    * 其余情况（开发分支、产物冲突、分支尚未落地）：只认 ``accepted``，开发
      分支的既有行为逐字不变。

    「哪些决策点必须被审过」以 ``prd.json`` 的决策点为准（与既有 PRD 门禁同
    一口径）；没有 ``prd.json`` 时以 ``review.json`` 里的条目为准。

    Args:
        plan_dir: plan 目录。

    Returns:
        已解决为 ``True``；没有审核文件、JSON 损坏或有 pending 条目为 ``False``。
    """
    plan_dir = Path(plan_dir)
    review_data = _read_json(plan_dir / PRD_REVIEW_FILENAME)
    if review_data is None:
        return False

    raw_items = review_data.get("items") or []
    statuses: Dict[Any, str] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        statuses[raw.get("index")] = str(raw.get("status") or STATUS_PENDING)

    required = _required_prd_indices(plan_dir, raw_items)
    if required is None:
        return False
    if not required:
        return True

    allowed = (
        RESOLVED_STATUSES if is_verification_branch(plan_dir) else {STATUS_ACCEPTED}
    )
    return all(statuses.get(index) in allowed for index in required)


def _required_prd_indices(
    plan_dir: Path, raw_items: Any
) -> Optional[Set[Any]]:
    """必须被审过的 PRD 决策点序号集合。

    ``prd.json`` 在 → 用它的决策点序号（与既有 PRD 门禁同一口径）；``prd.json``
    不在 → 用 ``review.json`` 里的条目序号。返回 ``None`` 表示「判不出来」
    （``prd.json`` 存在但损坏）——按未解决处理。
    """
    prd_path = Path(plan_dir) / PRD_FILENAME
    if prd_path.exists():
        prd_data = _read_json(prd_path)
        if prd_data is None:
            return None
        decision_points = prd_data.get("decision_points") or []
        indices = {
            dp.get("index")
            for dp in decision_points
            if isinstance(dp, Mapping)
        }
        return indices

    indices = set()
    if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
        for raw in raw_items:
            if isinstance(raw, Mapping):
                indices.add(raw.get("index"))
    return indices
