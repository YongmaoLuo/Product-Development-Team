"""框架缺陷的双条件阻塞判定 —— 把 PRD 的判定公式变成一个纯函数。

这个模块要解决什么
------------------
PRD 规定：一次失败只有在「**验收关键** 且（**框架已复现** 或 **改动越界**）」
时才算 blocking::

    blocking = acceptance_critical and (framework_reproduced or out_of_scope_change)

这条公式此前没有任何代码表达过 —— 判定散落在调用方的 if 里。口径一旦漂移，
「同一个缺陷在 A plan 阻塞、在 B plan 不阻塞」就会发生，而这种分歧没有任何
地方能看出来。本模块把公式收成**一个纯函数**：输入是这次命中的事实，输出
是一个决定。

判定必须是纯函数
----------------
不读文件、不发请求、不看时钟、不读数据库。同一份输入在任何进程、任何机器上
给出同一个决定 —— 否则「这个失败到底阻不阻塞」本身就不可复算，跨进程比对
更是无从谈起。

语义复核不是纯函数，但它的模型调用是**注入**的
----------------------------------------------
本模块的第二个出口 :func:`semantic_review` 要问模型「这次失败与候选签名是不是
同一个根因」。这一步绕不开模型，但模型是**调用方传进来的**（``model_call``）：
本模块自己不读文件、不发请求、不看时钟、不读数据库，也不知道模型长什么样。
这样做的理由是失败方向可控 —— 模型不可用、超时、返回的不是 JSON，这三种情况
一律按「不同缺陷」处理（详见 :func:`semantic_review` 的说明）。

优先级阶梯
----------
公式里有两个「或」项和两个「不」条款，它们的先后顺序本身就是判定的一部分。
本模块按下面四级依次判定，每一级都有明确的理由：

1. **非验收关键 -> 永不 blocking。** 「验收关键」是公式的第一个合取项，
   它为假时后两项怎么取值都不改变结论。先判它，省掉后面所有无谓的分支。
2. **已复现为框架缺陷（``framework_deterministic`` / ``framework_timeout``）
   -> blocking。** 这是公式的正向条件；越界与否不改变结论。
3. **本轮明确不是框架问题（``flake`` / ``not_reproducible``）-> 不阻塞。**
   这一级**优先于**「改动越界」：PRD 明确写了 flake / not_reproducible 优先
   于 out_of_scope_change —— 越界说明「改动写到了不该写的地方」，但如果这次
   失败本身是 flake 或只在本地复现，那它就不是一个框架缺陷，本轮不建修复
   plan（分别登记 flaky / 本地继续）。
4. **改动越界 -> blocking，且无需复现。** 越界是关于**改动文件集**的事实，
   不依赖复现结论；正因为它独立成立，「越界就阻塞」不需要先跑一次复现。

剩下的是「还没到能下结论」的两级：

* ``needs_repro`` —— 关键但既没复现也没越界：本轮还判不了，要先复现。
* ``unclassified`` —— 复现跑过了但归不了类（输出截断、命令不可执行、
  ``origin/main`` 不可读）：登记证据，下轮重试。
* ``exhausted`` —— 同一签名连续多轮仍归不了类：升级 HITL，不再复现。

处置是闭集
----------
:data:`DISPOSITIONS` 是五个值的闭集。调用方按 disposition 分派，不允许自造
字符串 —— 自造的字符串在这里是拼写错误，不是新语义。同理，复现结论也是一个
闭集：未知分类值抛 ``ValueError``，而不是被悄悄当成某一类。

语义复核 —— 机械指纹认不出的同因
--------------------------------
机械指纹按「框架文件 + 错误类别 + 归一化首错行」算键。它稳，但它**窄**：同
一个根因可以有两张脸 —— 首错行换个措辞、错误类别从一个异常变成另一个异常，
机械指纹就认不出这是同一个缺陷，于是同一个缺陷会长出第二个修复 plan。语义复
核是补这一刀：**一次模型调用**，问「这次失败与候选签名是不是同一个根因」，判
为同因就把新指纹写成候选指纹的别名，之后的同形命中直接机械命中。

失败方向是「不同缺陷」，不是「抛错」
----------------------------------
模型不可用、超时、返回的不是 JSON —— 三种情况一律按**不同缺陷**处理，然后照
常建新的修复 plan。理由：宁可多修一次（重复修复会被「涉及文件 + 失败证据摘
要」的重叠审计暴露，事后可合并），也不能因为模型抽风就把一个真实缺陷判成别人
的别名，从此再没人修它。所以 :func:`semantic_review` 对模型侧的失败**永不抛
错**，只在 ``reason`` 里记下发生了什么。

谁写别名
--------
**调用方**写。本模块只回答「是不是同因、该指向哪个指纹」；落库是注册表的职
责（``framework_fix_alias`` 表 + 审计事件），不在这里。理由：判定与落库是两件
事 —— 判定是纯逻辑，落库有事务、有审计链；混在一起就既没法单独测判定，也没
法在别名写失败时只重试判定。

包形态
------
``backend/framework_fix/`` **故意没有** ``__init__.py``：它是 PEP 420 的隐式
命名空间包，按 ``backend.framework_fix.<module>`` 导入。与
:mod:`backend.framework_fix.fingerprint` 同理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

__all__ = [
    "DISPOSITIONS",
    "EXHAUSTED_UNCLASSIFIED_ROUNDS",
    "REPRO_CLASSIFICATIONS",
    "FailureDecision",
    "FailureHit",
    "SemanticVerdict",
    "decide",
    "semantic_review",
]


#: 处置的闭集。五个值，按「结论有多确定」排：blocking（确定阻塞）、
#: needs_repro（还不能下结论）、unclassified（试过了仍不能下结论）、
#: exhausted（连续不能下结论，升级）、not_blocking（确定不阻塞）。
DISPOSITIONS: Tuple[str, ...] = (
    'blocking',
    'needs_repro',
    'unclassified',
    'exhausted',
    'not_blocking',
)

#: 复现结论的闭集。取值与 ``framework_fix.repro.classify`` 的分类表一致。
REPRO_CLASSIFICATIONS: Tuple[str, ...] = (
    'framework_deterministic',
    'framework_timeout',
    'flake',
    'not_reproducible',
    'unclassified',
)

#: 连续多少轮归不了类之后升级 HITL。架构决策点写的是「连续 3 轮才 HITL」，
#: 其中前两轮已经计入调用方传入的 ``unclassified_rounds``，本轮是第 3 轮 ——
#: 所以阈值是 2：``unclassified_rounds >= 2`` 时本轮升级。
EXHAUSTED_UNCLASSIFIED_ROUNDS = 2

#: 「框架已复现」—— 公式里 ``framework_reproduced`` 的取值集合。
_FRAMEWORK_REPRODUCED = frozenset({'framework_deterministic', 'framework_timeout'})

#: 「本轮明确不是框架问题」—— 优先于「改动越界」。
_NOT_FRAMEWORK = frozenset({'flake', 'not_reproducible'})


# ---------------------------------------------------------------------------
# 输入 / 输出
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FailureHit:
    """一次失败命中 —— 判定公式的全部输入事实。

    Attributes:
        vp_id: 命中所在的验证点 id；门禁类失败可能为 ``None``。
        plan_id: 命中所在的计划号。
        error_class: 异常类型或错误类别。
        first_error_line: 首错行（原文）。
        framework_files: 失败所在 / 修复将要触碰的框架文件（repo 相对路径）。
            本模块不读它 —— 它是**指纹的原料**，由注册表那层拿去算指纹；
            放在命中里是因为指纹与判定读的是同一份材料，分开存就得同步两份。
        acceptance_critical: 该命中是否落在验收关键的 VP / 任务上。
        out_of_scope_change: 修复文件集是否越出 ``files_to_modify`` /
            写作用域。
    """

    vp_id: Optional[str]
    plan_id: Optional[str]
    error_class: str
    first_error_line: str
    framework_files: Sequence[str]
    acceptance_critical: bool
    out_of_scope_change: bool


@dataclass(frozen=True)
class FailureDecision:
    """一次判定的结果。

    Attributes:
        disposition: 处置，取值来自 :data:`DISPOSITIONS`。
        blocking: 是否阻塞。恒等于 ``disposition == 'blocking'``。
        register: 本次命中是否应登记进框架缺陷注册表。已得出结论的处置
            （blocking / not_blocking / unclassified / exhausted）都登记 ——
            unclassified 登记的是「试过了但归不了类」这件事与它的证据；
            ``needs_repro`` 不登记，因为那时还不知道这是不是框架缺陷。
        reproduce: 是否还需要再复现一次。``needs_repro`` / ``unclassified``
            为真，其余处置不再复现。
        unclassified_rounds: 判定时已知的连续未分类轮数，原样带回，便于
            调用方记轮次。
    """

    disposition: str
    blocking: bool
    register: bool
    reproduce: bool
    unclassified_rounds: int


# ---------------------------------------------------------------------------
# 输入规整
# ---------------------------------------------------------------------------
def _normalize_classification(reproduced: Optional[str]) -> Optional[str]:
    """复现结论规整成闭集里的值。

    ``None`` 表示「还没复现」。字符串按闭集取值，大小写与首尾空白不参与
    判定 —— 分类表产出的值是小写，但调用方从 JSON / 命令行读回来的可能不是。

    Raises:
        ValueError: ``reproduced`` 不在闭集里。未知分类是数据坏了（分类表换了
            口径、或调用方拼错了），把它悄悄当成某一类会让判定结论变得
            不可复算 —— 响亮地失败比错误地阻塞好。
    """
    if reproduced is None:
        return None
    if not isinstance(reproduced, str):
        raise TypeError(
            f"reproduced must be a string or None, got {type(reproduced).__name__}"
        )
    candidate = reproduced.strip().lower()
    if not candidate:
        return None
    if candidate not in REPRO_CLASSIFICATIONS:
        raise ValueError(
            f"unknown reproduction classification {reproduced!r}; "
            f"expected one of {REPRO_CLASSIFICATIONS} or None"
        )
    return candidate


def _normalize_rounds(unclassified_rounds: int) -> int:
    """未分类轮数规整。

    Raises:
        TypeError: 不是整数。
        ValueError: 负数。轮次只会递增，负数是数据坏了。
    """
    if isinstance(unclassified_rounds, bool) or not isinstance(unclassified_rounds, int):
        raise TypeError(
            "unclassified_rounds must be an int, got "
            f"{type(unclassified_rounds).__name__}"
        )
    if unclassified_rounds < 0:
        raise ValueError(
            f"unclassified_rounds must not be negative, got {unclassified_rounds}"
        )
    return unclassified_rounds


def _fact(hit: FailureHit, name: str) -> bool:
    """取命中上的一个布尔事实。

    Raises:
        TypeError: 命中对象没有这个属性。调用方传错了对象 —— 判定静悄悄
            读到一个缺省值，结论就会变成另一个样子。
    """
    if not hasattr(hit, name):
        raise TypeError(
            f"hit must expose {name!r}; got {type(hit).__name__}"
        )
    return bool(getattr(hit, name))


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def decide(
    hit: FailureHit,
    *,
    reproduced: Optional[str] = None,
    unclassified_rounds: int = 0,
) -> FailureDecision:
    """双条件阻塞判定 —— PRD 判定公式的纯函数实现。

    Args:
        hit: 这次命中的事实（验收关键与否、改动是否越界等）。
        reproduced: 复现结论，取值来自 :data:`REPRO_CLASSIFICATIONS`；
            ``None`` 表示还没复现。
        unclassified_rounds: 该签名**此前**已连续未分类的轮数（不含本轮）。
            达到 :data:`EXHAUSTED_UNCLASSIFIED_ROUNDS` 时本轮升级 HITL。

    Returns:
        一个 :class:`FailureDecision`。``disposition`` 取值必在
        :data:`DISPOSITIONS` 内；``blocking`` 恒等于
        ``disposition == 'blocking'``。

    Raises:
        TypeError: ``hit`` 缺少必要属性，或 ``reproduced`` / ``unclassified_rounds``
            类型不对。
        ValueError: ``reproduced`` 不在闭集里，或 ``unclassified_rounds`` 为负。

    判定阶梯（顺序即优先级）::

        非验收关键                       -> not_blocking
        已复现为框架缺陷                   -> blocking
        flake / not_reproducible         -> not_blocking   （优先于越界）
        改动越界                         -> blocking       （无需复现）
        连续未分类轮数 >= 2               -> exhausted
        复现过但归不了类                   -> unclassified还没复现                          -> needs_repro
    """
    acceptance_critical = _fact(hit, 'acceptance_critical')
    out_of_scope_change = _fact(hit, 'out_of_scope_change')
    classification = _normalize_classification(reproduced)
    rounds = _normalize_rounds(unclassified_rounds)

    # 1. 非验收关键 —— 公式的第一个合取项为假，结论已经定了：永不阻塞。
    if not acceptance_critical:
        return FailureDecision(
            disposition='not_blocking',
            blocking=False,
            register=True,
            reproduce=False,
            unclassified_rounds=rounds,
        )

    # 2. 已复现为框架缺陷 —— 公式的正向条件成立。
    if classification in _FRAMEWORK_REPRODUCED:
        return FailureDecision(
            disposition='blocking',
            blocking=True,
            register=True,
            reproduce=False,
            unclassified_rounds=rounds,
        )

    # 3. 本轮明确不是框架问题 —— 优先于「改动越界」：这不是一个框架缺陷，
    #    本轮不建修复 plan（flake 登记 flaky，not_reproducible 本地继续）。
    if classification in _NOT_FRAMEWORK:
        return FailureDecision(
            disposition='not_blocking',
            blocking=False,
            register=True,
            reproduce=False,
            unclassified_rounds=rounds,
        )

    # 4. 改动越界 —— 关于改动文件集的事实，与复现结论无关；正因为独立成立，
    #    它不需要先跑一次复现。
    if out_of_scope_change:
        return FailureDecision(
            disposition='blocking',
            blocking=True,
            register=True,
            reproduce=False,
            unclassified_rounds=rounds,
        )

    # 5. 连续多轮归不了类 —— 升级 HITL，不再复现。
    if rounds >= EXHAUSTED_UNCLASSIFIED_ROUNDS:
        return FailureDecision(
            disposition='exhausted',
            blocking=False,
            register=True,
            reproduce=False,
            unclassified_rounds=rounds,
        )

    # 6. 复现跑过了但归不了类 —— 登记证据，下轮重试。
    if classification == 'unclassified':
        return FailureDecision(
            disposition='unclassified',
            blocking=False,
            register=True,
            reproduce=True,
            unclassified_rounds=rounds,
        )

    # 7. 还没复现 —— 本轮还判不了，先去复现。
    return FailureDecision(
        disposition='needs_repro',
        blocking=False,
        register=False,
        reproduce=True,
        unclassified_rounds=rounds,
    )


# ---------------------------------------------------------------------------
# 语义复核
# ---------------------------------------------------------------------------

#: 模型回答的键。模型只要给一个 JSON 对象，这三个键就是它的全部词汇表。
_MODEL_SAME_KEY = 'same'
_MODEL_MATCHED_KEY = 'matched_fingerprint'
_MODEL_REASON_KEY = 'reason'

#: 字符串形态的布尔值。模型把 ``true`` 写成 ``"true"`` 是常事；不认它，一份
#: 结论就被当成「没有结论」，白白丢掉一次同因判定。
_TRUTHY_TOKENS = frozenset({'true', 'yes', 'y', '1'})
_FALSY_TOKENS = frozenset({'false', 'no', 'n', '0'})

#: 给模型的问题。规则写在前面 —— 模型只需要回答一个问题，越少约束越少跑偏。
_SEMANTIC_REVIEW_QUESTION = (
    '你是框架缺陷的语义复核员。判断下面这次失败与候选签名是不是**同一个根因**：'
    '症状不同（首错行措辞不同、错误类别不同）但根因相同，也算同一个缺陷。\n'
    '只回答一个 JSON 对象，不要写别的文字：'
    '{"same": true 或 false, '
    '"matched_fingerprint": "与本次失败同因的那个候选的 fingerprint；'
    '不同因时填空字符串", '
    '"reason": "一句话理由"}'
)

#: 候选里取出来给人（模型）看的证据字段。取不到就跳过 —— 空字段比缺字段更
#: 容易误导模型。
_CANDIDATE_EVIDENCE_FIELDS = ('error_class', 'first_error_line', 'framework_files')

#: 判定「缺失属性」用的哨兵。
_MISSING = object()


def _hit_attr(hit: Any, name: str, default: Any = _MISSING) -> Any:
    """取命中上的一个属性。

    Args:
        hit: 这次命中。
        name: 属性名。
        default: 缺失时的返回值。不给就是「必须有」。

    Returns:
        属性值。

    Raises:
        TypeError: 属性缺失且没给 ``default``。语义复核的材料缺了就不是复核 ——
            静悄悄用一个空值去问模型，模型会给出一个看起来合理但毫无依据的
            结论。
    """
    if hasattr(hit, name):
        return getattr(hit, name)
    if default is _MISSING:
        raise TypeError(
            f"hit must expose {name!r}; got {type(hit).__name__}"
        )
    return default


def _usable_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """挑出能当候选用的那些。

    候选里没有 ``fingerprint`` 的条目直接丢掉：模型判同因时要指名一个候选，
    指不出指纹的条目没法被指名 —— 它不是候选，是噪声。

    Raises:
        TypeError: ``candidates`` 不是一个序列，或某条候选不是字典。
    """
    if candidates is None:
        return []
    if isinstance(candidates, (str, bytes, Mapping)):
        raise TypeError(
            "candidates must be a sequence of dicts; got "
            f"{type(candidates).__name__}"
        )
    usable: List[Mapping[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise TypeError(
                "each candidate must be a mapping; got "
                f"{type(candidate).__name__}"
            )
        fingerprint = candidate.get('fingerprint')
        if isinstance(fingerprint, str) and fingerprint.strip():
            usable.append(candidate)
    return usable


def _candidate_field(
    candidate: Mapping[str, Any], name: str
) -> Optional[Any]:
    """候选上的一个证据字段。

    候选有两种形状：注册表的一行（材料在 ``key`` 子字典里）和调用方自己拼的
    扁平字典（材料就在顶层）。两种都认 —— 认死一种就等于要求调用方按注册表
    的内部结构拼候选。
    """
    value = candidate.get(name)
    if value is None or value == '' or value == [] or value == {}:
        key = candidate.get('key')
        if isinstance(key, Mapping):
            value = key.get(name)
    return value


def _format_files(files: Any) -> str:
    """文件列表 → 一行文本。"""
    if isinstance(files, str):
        return files
    if isinstance(files, Sequence):
        return ', '.join(str(item) for item in files)
    return ''


def _build_prompt(
    hit: Any,
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    """把这次失败与候选拼成给模型的问题。

    比对材料里**不含** plan 局部量（plan id、VP id、轮次）：它们描述「这次失败
    发生在哪儿」，不是「哪里坏了」。把它们摆到模型面前，模型就会去比计划号而
    不是比根因 —— 而机械指纹（:mod:`backend.framework_fix.fingerprint`）正是
    把这些量排除在身份之外的。两处口径必须一致。
    """
    lines: List[str] = [_SEMANTIC_REVIEW_QUESTION, '', '【本次失败】']
    lines.append(f'error_class: {_hit_attr(hit, "error_class")}')
    lines.append(f'first_error_line: {_hit_attr(hit, "first_error_line")}')
    files = _hit_attr(hit, 'framework_files', ())
    lines.append(f'framework_files: {_format_files(files)}')

    lines.append('')
    lines.append('【候选签名】')
    for index, candidate in enumerate(candidates, start=1):
        lines.append(f'{index}. fingerprint={candidate["fingerprint"]}')
        for name in _CANDIDATE_EVIDENCE_FIELDS:
            value = _candidate_field(candidate, name)
            if value is None:
                continue
            if name == 'framework_files':
                lines.append(f'   framework_files: {_format_files(value)}')
            else:
                lines.append(f'   {name}: {value}')
    return '\n'.join(lines)


def _strip_code_fence(text: str) -> Optional[str]:
    """去掉模型答案外面的代码围栏。

    模型十有八九把 JSON 裹在 ```json 里。围着它报错等于把一份好答案当成坏
    答案 —— 而「坏答案」的下游后果是「判成不同缺陷」，也就是重复修一次。
    """
    if not text.startswith('```'):
        return None
    body = text[3:]
    newline = body.find('\n')
    if newline == -1:
        return None
    body = body[newline + 1:]
    end = body.rfind('```')
    if end == -1:
        return None
    return body[:end].strip()


def _json_candidates(text: str) -> List[str]:
    """模型文本里可能藏着的 JSON 片段，按可能性排序。"""
    candidates = [text]
    fenced = _strip_code_fence(text)
    if fenced:
        candidates.append(fenced)
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    return candidates


def _parse_model_payload(raw: Any) -> Optional[Dict[str, Any]]:
    """把模型的原文解析成一个 JSON 对象。

    Returns:
        解析出来的字典；解析不出来（不是文本、不是 JSON、不是对象）时是
        ``None``。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _coerce_bool(value: Any) -> Optional[bool]:
    """把模型给的「是 / 否」读成三态布尔。

    Returns:
        ``True`` / ``False`` / ``None``（``None`` = 没给出可判定的结论）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUTHY_TOKENS:
            return True
        if token in _FALSY_TOKENS:
            return False
        return None
    if isinstance(value, int):
        return bool(value)
    return None


@dataclass(frozen=True)
class SemanticVerdict:
    """语义复核的结论。

    Attributes:
        same: 这次失败与某个候选签名是不是同一个根因。
        matched_fingerprint: 判为同因的那个候选指纹；``same`` 为假时是
            ``None``。
        alias: 调用方**是否应该**把本次命中的新指纹写成
            ``matched_fingerprint`` 的别名。恒等于「同因且指名了候选」。
        reason: 一句话理由。模型给了就取模型的原话；模型没给出结论时，这里
            记的是**没能下结论的原因**（超时、输出不可解析等等）。
    """

    same: bool
    matched_fingerprint: Optional[str]
    alias: bool
    reason: str


def semantic_review(
    hit: FailureHit,
    candidates: Sequence[Mapping[str, Any]],
    model_call: Callable[[str], str],
) -> SemanticVerdict:
    """语义复核 —— 问模型「这次失败与候选是不是同一个根因」。

    Args:
        hit: 本次失败的事实。语义复核要读它的 ``error_class`` /
            ``first_error_line`` / ``framework_files``。
        candidates: 候选签名。每条至少要有 ``fingerprint``；注册表的一行
            （材料在 ``key`` 里）也认。
        model_call: 调模型的函数：收一段提示词，回一段文本。**注入**进来的
            —— 本模块不知道模型长什么样，也不知道它连不连得上。

    Returns:
        一个 :class:`SemanticVerdict`。

        判为同因时 ``same=True``、``matched_fingerprint`` 指向那个候选、
        ``alias=True``。**别名由调用方写**（``write_alias(新指纹, 命中指纹)``）
        —— 本模块不碰数据库。

        下面这些情况一律返回「不同缺陷」，并且**不抛错**：

        * 没有候选（``candidates`` 为空，或没有一条带指纹）—— 连模型都不调，
          没有可合并的对象；
        * 模型超时；
        * 模型不可用（调用本身抛错）；
        * 模型回的不是可解析的 JSON；
        * 模型没给出可判定的结论；
        * 模型给的指纹不在候选里（编出来的指纹不是别名该指向的目标）。

    Raises:
        TypeError: ``hit`` 缺少必要属性，或 ``candidates`` 的形状不对。这两
            种是调用方的 bug —— 响亮地失败比默默问出一个假问题好。
    """
    usable = _usable_candidates(candidates)
    if not usable:
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason='没有候选签名可比对：不写别名',
        )

    prompt = _build_prompt(hit, usable)

    try:
        raw = model_call(prompt)
    except TimeoutError:
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason='模型调用超时：按不同缺陷处理',
        )
    except Exception as exc:  # noqa: BLE001 - 模型侧的失败一律降级
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason=f'模型调用失败（{exc!r}）：按不同缺陷处理',
        )

    payload = _parse_model_payload(raw)
    if payload is None:
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason='模型输出不是可解析的 JSON：按不同缺陷处理',
        )

    model_reason = payload.get(_MODEL_REASON_KEY)
    reason = (
        model_reason.strip()
        if isinstance(model_reason, str) and model_reason.strip()
        else ''
    )

    same = _coerce_bool(payload.get(_MODEL_SAME_KEY))
    if same is None:
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason='模型没有给出可判定的结论：按不同缺陷处理',
        )

    if not same:
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason=reason or '模型判定为不同缺陷',
        )

    matched = payload.get(_MODEL_MATCHED_KEY)
    if not isinstance(matched, str) or not matched.strip():
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason='模型判定同因但没有指名候选：不写别名',
        )
    matched = matched.strip()
    known = {
        str(candidate['fingerprint']).strip() for candidate in usable
    }
    if matched not in known:
        return SemanticVerdict(
            same=False,
            matched_fingerprint=None,
            alias=False,
            reason=f'模型给出的指纹 {matched!r} 不在候选里：不写别名',
        )

    return SemanticVerdict(
        same=True,
        matched_fingerprint=matched,
        alias=True,
        reason=reason or '模型判定与候选同因',
    )