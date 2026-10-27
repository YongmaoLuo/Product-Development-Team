"""VP 与计划主题的相关性判定内核（两级：确定性 → LLM 批量）。

这个模块解决什么
----------------
A1′ 要求「每次进入 verification 时判定活动 VP 是否仍在计划主题内」。当前
没有这个能力：一条跑题的 VP 会被当成正常 VP 跑完，跑完再由人从报告里看出来。

但**逐个 VP 调 LLM 是不可接受的**。一次 verification 可能有几十条 VP，逐条
调就是几十次模型往返 —— 成本和延迟都是数量级的浪费，而且把判定这件事从
「确定性检查」降级成了「模型的自由裁量」。

所以判定分两级：

1. **确定性层（零 I/O）**：VP 声明的 ``related_prd_criteria`` 里直接写了判据
   编号（``AC-2``），或者写了判据正文（归一化后互相包含），即判「相关」，
   根本不碰模型。绝大多数正常 VP 停在这一层。
2. **LLM 层（一次批量）**：第一层没判掉的 VP **打包成一次**调用交给模型。
   模型按 ``vp_id`` 回答每条是否相关、命中了哪些判据。

判定内核是**纯函数**：不读文件、不发请求、不打日志、不改全局状态。唯一的
外部依赖是调用方注入的 ``llm_batch`` 可调用对象。这样做有两个好处：单元测试
不需要任何 mock（每个用例塞一个普通函数进去，数它被调了几次 —— 「批量只调
一次」这条契约因此可以被直接断言），以及判定逻辑本身没有 I/O 副作用可以出错。

三态契约
--------
``RelevanceDecision`` 只有三个取值，判定结果永远落在这三个之一：

``deterministic_match``
    确定性层判定的相关。不需要模型。
``llm_relevant``
    相关。要么是模型说相关，要么是**兜底**（fail-open）时按相关处理。
``llm_irrelevant``
    无关。**只有**模型明确这么判、且这条 VP 自己声明过判据时才会出现。

三层（tier）说明判定是怎么来的：

``deterministic``
    第一层判的，没有调用模型。
``llm``
    第二层判的，模型给了可解析的回答。
``fail_open``
    模型不可用（没注入 / 抛错 / 返回不可解析 / 漏了这条 vp_id），或者判定
    缺少依据（没有判据可比、VP 没有声明任何判据）。**一律按相关处理。**

为什么「不可用时按相关处理」
----------------------------
判定的方向不对称：把一条跑题的 VP 留在计划里，代价是多跑一轮验证；把一条
正常的 VP 判死，代价是计划被误伤 —— 后者更糟。所以模型这一层只允许**收紧**
范围（判无关），不允许在模型失灵时**扩大**收紧范围。

兜底的具体边界
--------------
* 没有判据可比（``criteria`` 为空）→ 全部兜底，且**不**调模型（问了也是白问）。
* VP 没有声明任何判据（``related_prd_criteria`` 为空/缺失）→ 兜底按相关处理。
  模型凭标题措辞说它"看着跑题了"不足以判死一条 VP —— 没有声明，就没有一条
  可以被推翻的主张。反过来，VP **声明了**判据却对不上计划里的任何判据，那是
  一条具体的矛盾，模型可以据此判它无关。
* 模型返回的 ``vp_id`` 不在本次 VP 集合里 → 一律忽略（模型可能顺手把
  上下文里的其它 id 也写进来）。

归一化
------
判定用的文本比较走 :func:`normalize_criterion_text`：NFKC 折叠全角 → casefold
→ 只保留字母数字。中文标点（``、。；：（）「」【】``）、空白、符号全部删掉，
所以 ``导出， CSV`` / ``导出CSV`` / ``导出  ＣＳＶ`` 归一化后是同一个串。

LLM 返回格式
-------------
``llm_batch`` 应当返回 JSON 文本（或已经解析好的对象），形如::

    {"VP-1": {"relevant": true, "matched_criterion_ids": ["AC-2"]},
     "VP-2": {"relevant": false, "reason": "与计划主题无关"}}

也接受这些等价写法：``{"results": [{"vp_id": "VP-1", "relevant": true}]}``、
``{"VP-1": ["AC-2"]}``（非空即相关）、``{"VP-1": true}``。
"""

from __future__ import annotations

import inspect
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "RelevanceDecision",
    "RelevanceResult",
    "judge_topic_relevance",
    "normalize_criterion_text",
]


# ---------------------------------------------------------------------------
# 判定层级
# ---------------------------------------------------------------------------

#: 第一层判定（零 I/O）命中。
TIER_DETERMINISTIC = "deterministic"

#: 第二层判定（模型给了可解析的回答）。
TIER_LLM = "llm"

#: 兜底 —— 模型不可用或判定缺少依据，按相关处理。
TIER_FAIL_OPEN = "fail_open"


class RelevanceDecision(str, Enum):
    """相关性判定的三态结果。

    ``str`` 混入 + ``__str__`` 覆写是为了让成员既能用 ``== "llm_relevant"``
    比较，又能直接序列化成那个字符串（``json.dumps`` / f-string）。
    """

    #: 确定性层判定的相关 —— 没有调用模型。
    DETERMINISTIC_MATCH = "deterministic_match"

    #: 相关 —— 模型说相关，或者兜底按相关处理。
    LLM_RELEVANT = "llm_relevant"

    #: 无关 —— 模型明确判的，且这条 VP 自己声明过判据。
    LLM_IRRELEVANT = "llm_irrelevant"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RelevanceResult:
    """一条 VP 的相关性判定结果。"""

    #: VP 的 id。
    vp_id: str

    #: 三态判定结果。
    decision: RelevanceDecision

    #: 判定来自哪一层（``deterministic`` / ``llm`` / ``fail_open``）。
    tier: str

    #: 人可读的判定依据。
    reason: str

    #: 命中的判据编号。
    matched_criterion_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # frozen dataclass 仍然可以在 __post_init__ 里用 object.__setattr__
        # 归一化字段：decision 收成枚举，matched_criterion_ids 收成 tuple。
        object.__setattr__(self, "decision", RelevanceDecision(self.decision))
        object.__setattr__(
            self, "matched_criterion_ids", tuple(self.matched_criterion_ids)
        )

    def to_dict(self) -> Dict[str, Any]:
        """返回可 JSON 序列化的形式。"""
        return {
            "vp_id": self.vp_id,
            "decision": str(self.decision),
            "tier": self.tier,
            "reason": self.reason,
            "matched_criterion_ids": list(self.matched_criterion_ids),
        }


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

#: 切成"词"的分隔符：空白 + 中英文标点。判据编号（``AC-2``）里不含这些
#: 字符，所以 ``AC-2：导出`` 切出来的 ``AC-2`` 仍然是完整的一个词。
_TOKEN_SPLIT_RE = re.compile(
    r"[\s,;:、，。；：！？！（）()\[\]【】「」《》〈〉…·/\\|]+"
)

#: 文本包含判定要求较短一侧至少这么长（归一化之后）。
#:
#: 1 个字的中文在任何一段正文里都会被命中（"无" ⊂ "无关"），那种命中是噪声
#: 而不是证据。2 个字是本仓实测下漏得最少的一个门槛 —— 与
#: :mod:`verification_ac_coverage` 的 ``_MIN_LABEL_LEN`` 同一个取舍。
_MIN_CONTAINMENT_LEN = 2


def normalize_criterion_text(text: Any) -> str:
    """把判据正文 / VP 声明归一化成可比较的形式。

    归一化做三件事：

    1. **NFKC** —— 全角 ASCII（``Ａ`` ``１`` ``，``）折成半角。
    2. **casefold** —— 大小写不敏感（``CSV`` 与 ``csv`` 同形）。
    3. **只保留字母数字**（含 CJK）—— 中文标点（``、。；：（）「」【】``）、
       空白、符号一律删除。连续空白因此也被归一化掉：``导出  CSV``、
       ``导出 CSV``、``导出CSV`` 归一化后是同一个串。

    非字符串输入返回空串（不抛异常）—— 判定内核面对的是 LLM 填的字段，
    字段类型不可靠是常态，不是异常。
    """
    if not isinstance(text, str):
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in folded if char.isalnum())


def _tokens(text: str) -> Set[str]:
    """把一段自由文本切成词（空白与标点都是分隔符）。"""
    return {token for token in _TOKEN_SPLIT_RE.split(text) if token}


def _id_key(text: str) -> str:
    """判据编号的比较键。

    编号是标识符，不能像正文那样把标点全删掉（``AC-2`` 删掉连字符就成了
    ``AC2``，那是另一个编号）。但全角与大小写是噪声，所以折叠这两项。
    """
    return unicodedata.normalize("NFKC", text).casefold()


# ---------------------------------------------------------------------------
# 输入归一化
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Criterion:
    """一条验收标准（id + 正文）。"""

    id: str
    text: str


def _first_text(entry: Mapping[str, Any], keys: Sequence[str]) -> str:
    """取第一个非空的字符串字段。"""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _as_sequence(value: Any) -> List[Any]:
    """把入参读成列表。

    字符串 / 字典 / bytes 不是合法形状（字符串会被当成一串字符，字典会被
    当成一串键），一律当成空；其它可迭代（生成器等）读成列表。
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return []


def _normalize_criteria(criteria: Any) -> List[_Criterion]:
    """把判据列表归一化成 :class:`_Criterion` 列表。

    没有 id 的判据无法被引用，直接丢掉 —— 留着一个空 id 只会让匹配结果里
    出现一个谁也引用不了的编号。
    """
    if not isinstance(criteria, (list, tuple)):
        criteria = _as_sequence(criteria)
    normalized: List[_Criterion] = []
    for entry in criteria:
        if not isinstance(entry, Mapping):
            continue
        criterion_id = _first_text(entry, ("id", "criterion_id", "code"))
        if not criterion_id:
            continue
        text = _first_text(entry, ("text", "description", "content", "criterion"))
        normalized.append(_Criterion(id=criterion_id, text=text))
    return normalized


def _vp_id(vp: Mapping[str, Any]) -> str:
    """取 VP 的 id。"""
    return _first_text(vp, ("id", "vp_id"))


def _vp_candidates(vp: Mapping[str, Any]) -> List[str]:
    """取 VP 声明的判据文本片段。

    ``related_prd_criteria`` 在计划里有两种形状：编号列表（``["AC-2"]``）
    和自由文本（``"C1: 状态码；C2: …"``）。两种都收 —— 编号靠切词识别，
    自由文本靠归一化后互相包含识别。
    """
    raw = vp.get("related_prd_criteria")
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        return [stripped] if stripped else []
    if isinstance(raw, (list, tuple)):
        candidates = []
        for item in raw:
            if isinstance(item, str):
                text = item.strip()
            elif item is None:
                continue
            else:
                text = str(item).strip()
            if text:
                candidates.append(text)
        return candidates
    text = str(raw).strip()
    return [text] if text else []


# ---------------------------------------------------------------------------
# 第一层：确定性匹配
# ---------------------------------------------------------------------------


def _match_by_id(candidates: Sequence[str], criteria: Sequence[_Criterion]) -> Tuple[str, ...]:
    """按判据编号精确命中。

    VP 声明里出现了某个判据的编号就算命中 —— 这层是零 I/O 的主力：正常的
    VP 都会写清楚自己验的是哪条判据。
    """
    wanted: Set[str] = set()
    for candidate in candidates:
        for token in _tokens(candidate):
            wanted.add(_id_key(token))
    if not wanted:
        return ()
    hits = []
    for criterion in criteria:
        if _id_key(criterion.id) in wanted:
            hits.append(criterion.id)
    return tuple(hits)


def _match_by_text(
    candidates: Sequence[str], criteria: Sequence[_Criterion]
) -> Tuple[str, ...]:
    """按正文互相包含命中（双向）。

    归一化之后两个方向都要试：VP 声明可能比判据正文更短
    （VP 写"导出 CSV"，判据写"导出 CSV 报表并落盘"），也可能更长。
    """
    hits = []
    for criterion in criteria:
        criterion_text = normalize_criterion_text(criterion.text)
        if len(criterion_text) < _MIN_CONTAINMENT_LEN:
            continue
        for candidate in candidates:
            candidate_text = normalize_criterion_text(candidate)
            if len(candidate_text) < _MIN_CONTAINMENT_LEN:
                continue
            if candidate_text in criterion_text or criterion_text in candidate_text:
                hits.append(criterion.id)
                break
    return tuple(hits)


# ---------------------------------------------------------------------------
# 第二层：模型批量判定
# ---------------------------------------------------------------------------

#: 模型可能返回的"逐条结果"列表的键名。
_RESULT_LIST_KEYS = ("results", "verdicts", "judgments", "items", "vps")

#: 模型回答里表示"相关"的词。
_RELEVANT_WORDS = frozenset(
    {"true", "yes", "y", "1", "relevant", "related", "match", "matched", "是", "相关"}
)

#: 模型回答里表示"无关"的词。
_IRRELEVANT_WORDS = frozenset(
    {"false", "no", "n", "0", "irrelevant", "unrelated", "否", "无关", "跑题"}
)

#: 模型可能用来表达"相关/无关"的字段名。
_RELEVANT_KEYS = ("relevant", "is_relevant", "matches", "match", "in_scope", "within_topic")

#: 模型可能用来表达"无关"的字段名（取反读）。
_IRRELEVANT_KEYS = ("irrelevant", "out_of_scope")

#: 模型可能用来表达判定的字段名（读字符串词）。
_VERDICT_KEYS = ("verdict", "decision", "label", "status", "result")

#: 模型可能用来表达命中判据的字段名。
_MATCHED_ID_KEYS = ("matched_criterion_ids", "criterion_ids", "matched_criteria", "matched")

#: 模型可能用来表达理由的字段名。
_REASON_KEYS = ("reason", "explanation", "why", "rationale")


def _llm_batch_argument(name: str, vps: Sequence[Any], criteria: Sequence[Any]) -> Any:
    """按参数名决定一个 ``llm_batch`` 参数该收到什么。

    名字认得出来就按名字给（``vps`` / ``criteria``），认不出来就给整个
    载荷 dict —— 真实客户端常写成 ``llm_batch(payload)``，它要的是
    ``{"vps": ..., "criteria": ...}``。
    """
    lowered = name.lower()
    if lowered in ("vps", "vp_list", "batch"):
        return vps
    if lowered in ("criteria", "criterion_list"):
        return criteria
    return {"vps": list(vps), "criteria": list(criteria)}


def _invoke_llm_batch(
    llm_batch: Callable[..., Any], vps: Sequence[Mapping[str, Any]], criteria: Sequence[_Criterion]
) -> Any:
    """调用注入的批量判定器。

    契约是 ``llm_batch(vps, criteria)``。为了兼容真实客户端的各种写法，
    这里按签名自适应：

    * 两个及以上位置参数 → 传 ``(vps, criteria)``；
    * 只有一个位置参数 → 按参数名给 ``vps`` / ``criteria``，认不出来就
      给整个载荷 dict ``{"vps": ..., "criteria": ...}``；
    * 带 keyword-only 参数（``def llm_batch(*, vps, criteria)``）→
      **按关键字传**。

    最后一条是必须的，不是锦上添花：keyword-only 参数不能按位置传，硬按
    位置传会抛 TypeError，而 TypeError 被判定内核吞掉之后整批 VP 兜底 ——
    结果看着对（按相关处理），tier 却变成 ``fail_open``、原因是一条
    ``llm_error``。模型其实一次都没被问过，却看上去像是问了、问崩了。
    """
    try:
        signature = inspect.signature(llm_batch)
    except (TypeError, ValueError):
        signature = None

    if signature is not None:
        parameters = list(signature.parameters.values())
        if any(p.kind is p.VAR_POSITIONAL for p in parameters):
            return llm_batch(vps, criteria)

        positional = [
            p
            for p in parameters
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        keyword_only = [p for p in parameters if p.kind is p.KEYWORD_ONLY]

        if keyword_only:
            # 有关键字参数：位置参数按位置传，关键字参数按关键字传。
            args = [_llm_batch_argument(p.name, vps, criteria) for p in positional]
            kwargs = {
                p.name: _llm_batch_argument(p.name, vps, criteria)
                for p in keyword_only
            }
            return llm_batch(*args, **kwargs)

        if len(positional) >= 2:
            return llm_batch(vps, criteria)
        if len(positional) == 1:
            return llm_batch(_llm_batch_argument(positional[0].name, vps, criteria))
        return llm_batch()

    # 签名读不出来（内置 callable 等）：先按两参试，再按单参试。
    try:
        return llm_batch(vps, criteria)
    except TypeError:
        return llm_batch({"vps": list(vps), "criteria": list(criteria)})


#: 模型有时会把 JSON 包在 ```json … ``` 里。
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL)


def _parse_llm_response(raw: Any) -> Optional[Dict[str, Any]]:
    """把模型返回解析成 ``{vp_id: judgment}``。

    解析不出来（不是 JSON、不是对象/列表）时返回 ``None`` —— 调用方据此
    走兜底。**不可解析绝不抛异常**：判定内核面对的是模型的输出，那东西
    什么形状都可能出现。
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        text = raw.strip()
        fenced = _JSON_FENCE_RE.match(text)
        if fenced:
            text = fenced.group("body").strip()
        if not text:
            return None
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (list, tuple)):
        collected: Dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            entry_id = _first_text(entry, ("vp_id", "id"))
            if entry_id:
                collected.setdefault(entry_id, entry)
        return collected
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
    """把模型给的值读成布尔；读不出来返回 ``None``。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _RELEVANT_WORDS:
            return True
        if text in _IRRELEVANT_WORDS:
            return False
    return None


def _coerce_ids(value: Any) -> Tuple[str, ...]:
    """把模型给的命中判据读成编号元组。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    ids = []
    for item in value:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
        elif isinstance(item, Mapping):
            nested = _first_text(item, ("id", "criterion_id"))
            if nested:
                ids.append(nested)
    return tuple(ids)


def _normalize_judgment(value: Any) -> Optional[Tuple[Optional[bool], Tuple[str, ...], str]]:
    """把一条模型判定读成 ``(是否相关, 命中判据, 理由)``。

    读不出"是否相关"时返回 ``(None, …)`` —— 调用方据此兜底，而不是猜一个
    方向。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return (value, (), "")
    if isinstance(value, Mapping):
        relevant: Optional[bool] = None
        for key in _RELEVANT_KEYS:
            if key in value:
                relevant = _coerce_bool(value[key])
                if relevant is not None:
                    break
        if relevant is None:
            for key in _IRRELEVANT_KEYS:
                if key in value:
                    inverted = _coerce_bool(value[key])
                    if inverted is not None:
                        relevant = not inverted
                        break
        if relevant is None:
            for key in _VERDICT_KEYS:
                if key in value:
                    relevant = _coerce_bool(value[key])
                    if relevant is not None:
                        break
        matched: Tuple[str, ...] = ()
        for key in _MATCHED_ID_KEYS:
            if key in value:
                matched = _coerce_ids(value[key])
                if matched:
                    break
        reason = ""
        for key in _REASON_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                reason = candidate.strip()
                break
        return (relevant, matched, reason)
    if isinstance(value, (list, tuple)):
        matched = _coerce_ids(value)
        return (bool(matched), matched, "")
    if isinstance(value, str):
        return (_coerce_bool(value), (), "")
    return None


def _extract_judgment(payload: Mapping[str, Any], vp_id: str) -> Optional[
    Tuple[Optional[bool], Tuple[str, ...], str]
]:
    """从模型返回里取出一条 VP 的判定。

    取不到返回 ``None`` —— 调用方据此兜底。模型返回里 vps 集合外的 id
    一律忽略（这里根本不会去遍历它们）。
    """
    if vp_id in payload:
        return _normalize_judgment(payload[vp_id])
    for key in _RESULT_LIST_KEYS:
        entries = payload.get(key)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if _first_text(entry, ("vp_id", "id")) == vp_id:
                return _normalize_judgment(entry)
    return None


# ---------------------------------------------------------------------------
# 判定入口
# ---------------------------------------------------------------------------


def _fail_open(vp_id: str, reason: str) -> RelevanceResult:
    """兜底结果 —— 按相关处理，但把"这是兜底"记在 tier 和 reason 里。"""
    return RelevanceResult(
        vp_id=vp_id,
        decision=RelevanceDecision.LLM_RELEVANT,
        tier=TIER_FAIL_OPEN,
        reason=reason,
        matched_criterion_ids=(),
    )


def _fail_open_all(
    unresolved: Sequence[Tuple[str, Mapping[str, Any], List[str]]], reason: str
) -> Dict[str, RelevanceResult]:
    """给一批未判定的 VP 全部兜底。"""
    return {vp_id: _fail_open(vp_id, reason) for vp_id, _vp, _candidates in unresolved}


def judge_topic_relevance(
    *,
    vps: Sequence[Mapping[str, Any]],
    criteria: Sequence[Mapping[str, Any]],
    llm_batch: Optional[Callable[..., Any]] = None,
) -> Dict[str, RelevanceResult]:
    """判定一批 VP 是否仍在计划主题内。

    Args:
        vps: 待判定的验证点。每条至少有 ``id``；``related_prd_criteria``
            是它声明的判据（编号列表或自由文本）。
        criteria: 计划的验收标准。每条至少有 ``id``，``text`` 是正文。
        llm_batch: 批量判定器。签名 ``llm_batch(vps, criteria)``，返回
            JSON 文本或已解析的对象。``None`` 表示这一层不可用 → 全部兜底。

    Returns:
        ``{vp_id: RelevanceResult}``，顺序与 ``vps`` 一致。同一个 vp_id
        重复出现时保留第一条。

    Note:
        本函数不读文件、不发请求、不打日志、不改全局状态 —— 唯一的外部
        依赖是 ``llm_batch``。
    """
    criterion_list = _normalize_criteria(criteria)
    vp_list = _as_sequence(vps)

    # 第一层：确定性匹配。命中的不进入模型批次。
    verdicts: Dict[str, RelevanceResult] = {}
    unresolved: List[Tuple[str, Mapping[str, Any], List[str]]] = []
    order: List[str] = []
    seen: Set[str] = set()

    for vp in vp_list:
        if not isinstance(vp, Mapping):
            continue
        vp_id = _vp_id(vp)
        if not vp_id or vp_id in seen:
            continue
        seen.add(vp_id)
        order.append(vp_id)

        candidates = _vp_candidates(vp)
        id_hits = _match_by_id(candidates, criterion_list)
        if id_hits:
            verdicts[vp_id] = RelevanceResult(
                vp_id=vp_id,
                decision=RelevanceDecision.DETERMINISTIC_MATCH,
                tier=TIER_DETERMINISTIC,
                reason="id:" + ",".join(id_hits),
                matched_criterion_ids=id_hits,
            )
            continue
        text_hits = _match_by_text(candidates, criterion_list)
        if text_hits:
            verdicts[vp_id] = RelevanceResult(
                vp_id=vp_id,
                decision=RelevanceDecision.DETERMINISTIC_MATCH,
                tier=TIER_DETERMINISTIC,
                reason="text:" + ",".join(text_hits),
                matched_criterion_ids=text_hits,
            )
            continue
        unresolved.append((vp_id, vp, candidates))

    if not unresolved:
        return {vp_id: verdicts[vp_id] for vp_id in order}

    # 第二层：没有判据可比 → 问了也白问，直接兜底。
    if not criterion_list:
        verdicts.update(_fail_open_all(unresolved, "fail_open:no_criteria"))
        return {vp_id: verdicts[vp_id] for vp_id in order}

    if llm_batch is None:
        verdicts.update(_fail_open_all(unresolved, "fail_open:no_llm"))
        return {vp_id: verdicts[vp_id] for vp_id in order}

    # 一次批量调用 —— 不是每条 VP 一次。
    # 载荷是纯 dict：真实模型客户端要把它 json.dumps 出去。
    try:
        raw = _invoke_llm_batch(
            llm_batch,
            [dict(vp) for _vp_id, vp, _candidates in unresolved],
            [{"id": criterion.id, "text": criterion.text} for criterion in criterion_list],
        )
    except Exception:  # noqa: BLE001 - 模型侧的任何异常都不该打断判定
        verdicts.update(_fail_open_all(unresolved, "fail_open:llm_error"))
        return {vp_id: verdicts[vp_id] for vp_id in order}

    payload = _parse_llm_response(raw)
    if payload is None:
        verdicts.update(_fail_open_all(unresolved, "fail_open:unparseable_response"))
        return {vp_id: verdicts[vp_id] for vp_id in order}

    known_ids = {criterion.id for criterion in criterion_list}
    for vp_id, _vp, candidates in unresolved:
        judgment = _extract_judgment(payload, vp_id)
        if judgment is None:
            verdicts[vp_id] = _fail_open(vp_id, "fail_open:missing_vp_id")
            continue
        relevant, matched_ids, reason = judgment
        if relevant is None:
            verdicts[vp_id] = _fail_open(vp_id, "fail_open:unreadable_judgment")
            continue
        matched_ids = tuple(matched for matched in matched_ids if matched in known_ids)
        if relevant:
            verdicts[vp_id] = RelevanceResult(
                vp_id=vp_id,
                decision=RelevanceDecision.LLM_RELEVANT,
                tier=TIER_LLM,
                reason=reason or "llm:relevant",
                matched_criterion_ids=matched_ids,
            )
            continue
        # 模型说无关。只有这条 VP 自己声明过判据时才成立 —— 声明过的判据
        # 对不上计划，是一条具体的矛盾；什么都没声明的 VP 没有可以被推翻的
        # 主张，模型凭标题说它跑题不足以判死它，按兜底处理。
        if not candidates:
            verdicts[vp_id] = _fail_open(vp_id, "fail_open:no_declaration")
            continue
        verdicts[vp_id] = RelevanceResult(
            vp_id=vp_id,
            decision=RelevanceDecision.LLM_IRRELEVANT,
            tier=TIER_LLM,
            reason=reason or "llm:irrelevant",
            matched_criterion_ids=(),
        )

    # 输出顺序跟输入顺序一致（verdicts 是按判定顺序写入的，重排一次）。
    return {vp_id: verdicts[vp_id] for vp_id in order if vp_id in verdicts}