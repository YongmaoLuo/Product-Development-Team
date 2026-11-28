"""框架缺陷的机械指纹 —— 跨 plan 稳定的全局去重键。

这个模块解决什么
----------------
PRD 要求「同一失败签名在全系统只产生一个修复 plan」。这个签名必须**跨 plan
稳定**：同一个框架缺陷在不同的 plan 里表现为不同的失败用例、不同的 VP、
不同的轮次、不同的计划号，但根因是同一个 —— 那些量都是 plan 局部量。

键由三个跨 plan 稳定的字段组成（架构决策点 2）::

    framework_files     失败所在 / 修复将要触碰的框架文件（repo 相对路径，排序后）
    error_class         异常类型或错误类别
    first_error_line    归一化后的首错行

显式排除：仓库名、node id、VP id、阶段、轮次、plan id。除仓库名外都是 PRD
明确指出的 plan 局部量；仓库名不进入指纹，但仍是注册表的运营字段。

指纹必须是纯函数
----------------
不读文件、不发请求、不看时钟、不依赖 ``hash()`` 的随机种子。同一份材料在
任何进程、任何机器上给出同一个指纹 —— 否则「这是不是同一个缺陷」这个问题
本身就不可复算，跨进程比对更是无从谈起。

归一化口径复用 verification.failure_fingerprint
------------------------------------------------
仓库已有的 ``backend/verification/failure_fingerprint.py`` 已经在做同一件事
（路径归一、十六进制掩码、运行噪声掩码、空白折叠）。**掩码不另造第二套**：
两处各写一遍，两处就会漂。这里直接复用它的 ``_normalize_text``（首错行）与
``_normalize_identifier``（错误类别），本模块只在其上叠加「plan 局部量」的
掩码 —— 那是本模块特有的、verification 那边没有的语义。

包形态
------
``backend/framework_fix/`` **故意没有** ``__init__.py``：它是 PEP 420 的隐式
命名空间包，按 ``backend.framework_fix.<module>`` 导入。新增领域包不必先立
一个包标记文件，也不与既有 ``sys.path`` 布局里的同名目录打架。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Dict, List, Optional, Sequence

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from verification.failure_fingerprint import (
        _normalize_identifier,
        _normalize_text,
    )
except ImportError:  # pragma: no cover
    from backend.verification.failure_fingerprint import (  # type: ignore
        _normalize_identifier,
        _normalize_text,
    )

__all__ = [
    "FINGERPRINT_PREFIX",
    "canonical_key",
    "compute_fingerprint",
    "normalize_line",
    "resolve_alias",
]


#: 指纹的前缀。``sha256:`` 让值自带算法名：算法换了，旧值仍能被认出是另一
#: 种算法的值，而不是同一个键的另一种写法。
FINGERPRINT_PREFIX = "sha256:"


# ---------------------------------------------------------------------------
# plan 局部量 —— 同一缺陷在不同 plan 里的不同表现
# ---------------------------------------------------------------------------
#
# 架构决策点 2 明确排除 node id、VP id、阶段、轮次、plan id。它们描述的是
# 「这次失败发生在哪儿」，不是「哪里坏了」。留在键里，同一个缺陷就会按 plan
# 裂成 N 个签名，全局去重随即失效。
#
# 下面的掩码就是这条排除的实现 —— 每一类 plan 局部量对应一条正则，掩完的
# 形状逐字相同，而不是「都不含那个词就算过」。

#: pytest node id —— ``tests/unit/test_x.py::TestY::test_z``。整段剔除，连同
#: 它前面的路径一起：只去掉 ``::test_z`` 会把半截路径留在原地，而那半截
#: 路径同样带着 plan 局部量。
_NODE_ID_RE = re.compile(r"[\w./\\+-]*\.py::[\w.:]+")

#: VP id —— ``VP-3`` / ``vp_12``。
_VP_ID_RE = re.compile(r"\bvp[-_ ]?\d+\b", re.IGNORECASE)

#: 计划号。本仓的计划目录形如 ``<YYYYMMDD>-<slug>``，也有
#: ``plan_20260101_120000`` 这种写法。
_PLAN_ID_RE = re.compile(
    r"\b(?:"
    r"plan[-_ ]?id\b\s*[:=]?\s*[A-Za-z0-9][A-Za-z0-9_-]*"
    r"|plan[-_][A-Za-z0-9][A-Za-z0-9_-]*"
    r"|\d{8}-[A-Za-z0-9][A-Za-z0-9-]*"
    r")"
)

#: 轮次 —— ``round=2`` / ``round 2``。
_ROUND_RE = re.compile(r"\brounds?\b\s*[=:#]?\s*\d+\b", re.IGNORECASE)

#: 轮次的中文写法 —— ``第 2 轮``。
_ROUND_CN_RE = re.compile(r"第\s*\d+\s*轮")

# ---------------------------------------------------------------------------
# 阶段 —— 同样是 plan 局部量
# ---------------------------------------------------------------------------
#
# 阶段在失败输出里有好几种写法，每一种都要归一成同一个形状。判据是**掩完
# 之后逐字相同**，不是「都不含 phase 就算过」：掩剩半个横幅，两条横幅照样是
# 两个不同的指纹。
#
#   ``phase=verification`` / ``phase: verification`` / ``阶段=verification``
#       —— 关键字加取值。
#   ``[verification]``
#       —— 方括号里的阶段名。
#   ``verification phase`` / ``执行阶段``
#       —— 阶段名在关键字之前。
#   ``=== phase 2/3 (e2e + integration) ===``
#       —— 横幅：序号与括号里的标签都是「这一轮跑到哪儿了」，一起换掉。

#: 横幅 —— ``phase 2/3 (e2e + integration)``。只换掉序号会留下标签，掩完
#: 仍是两个不同的横幅，所以序号与紧随其后的括号标签整段换掉。
_PHASE_BANNER_RE = re.compile(
    r"\b(?:phase|阶段)\b\s*[:=：]?\s*\d+\s*/\s*\d+\s*(?:\([^)]*\))?"
)

#: ``phase=<值>`` / ``phase: <值>`` / ``阶段=<值>`` / ``阶段：<值>``。
#: 关键字前面可以带一个词（``verification phase: x``）—— 那是同一个阶段的
#: 另一种写法，一并换掉。
_PHASE_ASSIGN_RE = re.compile(
    r"(?:(?<![\w<])[A-Za-z][A-Za-z0-9_-]*\s+)?"
    r"\b(?:phase|阶段)\b\s*[:=：]\s*[^\s,;)\]}]+"
)

#: ``验证阶段=<值>`` —— 中文写法，阶段名在关键字之前。
_PHASE_CN_ASSIGN_RE = re.compile(
    r"[一-鿿]{2,4}阶段\s*[:=：]\s*[^\s,;)\]}]+"
)

#: ``verification phase`` / ``执行阶段`` —— 阶段名在关键字之前，且后面没有
#: 取值（取值归 :data:`_PHASE_ASSIGN_RE` 管）。
_PHASE_PREFIX_RE = re.compile(
    r"(?<![\w<])[A-Za-z][A-Za-z0-9_-]*\s+phase\b(?!\s*[:=：])"
    r"|[一-鿿]{2,4}阶段(?![=：:])"
)

#: ``[verification]`` —— 方括号里的阶段名。方括号里只放**一个词**时才换：
#: 带空格或标点的括号内容是失败消息的一部分（``[Errno 2]``），抹掉它等于
#: 抹掉缺陷的身份。
_PHASE_BRACKET_RE = re.compile(
    r"\[[A-Za-z_一-鿿][^\[\]\s]*\]"
)

#: 掩码的顺序。node id 在最前：它连着路径，先剔掉它，剩下的路径才有机会
#: 交给通用的路径归一。
#:
#: 阶段的几条按「从最有锚点到最没锚点」排：横幅带序号，取值带分隔符，前缀
#: 形带关键字，方括号形什么锚点都没有 —— 它排最后，免得抢掉别的形态。
_VOLATILE_MASKS = (
    (_NODE_ID_RE, "<nodeid>"),
    (_VP_ID_RE, "<vp>"),
    (_PLAN_ID_RE, "<plan>"),
    (_ROUND_RE, "round=<n>"),
    (_ROUND_CN_RE, "第<n>轮"),
    (_PHASE_BANNER_RE, "<phase>"),
    (_PHASE_ASSIGN_RE, "<phase>"),
    (_PHASE_CN_ASSIGN_RE, "<phase>"),
    (_PHASE_PREFIX_RE, "<phase>"),
    (_PHASE_BRACKET_RE, "[<phase>]"),
)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def normalize_line(text: str) -> str:
    """一行失败输出，跨 plan / 跨轮次拼写一致。

    两层归一化：

    1. **plan 局部量** —— node id、VP id、阶段、轮次、计划号被整段剔除。它们
       换了名字也还是同一个缺陷。
    2. **运行噪声** —— 交给 ``verification.failure_fingerprint`` 的
       ``_normalize_text``：临时路径归一、十六进制掩码、时间戳/进程号/计数
       掩码、空白折叠、大小写折叠。

    Args:
        text: 一行失败输出。``None`` 当作空行处理。

    Returns:
        归一化后的行。空行归一化后仍是空行 —— 首错行缺失是一个事实，
        不该被归一化成一个和「有一行内容」相同的形状。
    """
    if text is None:
        return ""
    raw = str(text)
    for pattern, replacement in _VOLATILE_MASKS:
        raw = pattern.sub(replacement, raw)
    return _normalize_text(raw)


def _normalize_files(framework_files: Optional[Sequence[str]]) -> List[str]:
    """框架文件列表，归一为排序去重后的集合。

    排序：文件是一个集合，不是序列 —— 同一批文件换个顺序列出，还是同一批。
    去重：同一个文件被列两次也还是同一批。

    路径本身保留大小写：文件名在大小写敏感的文件系统上是不同的文件，
    把 ``Foo.py`` 和 ``foo.py`` 合并成一个键会合并两个不同的缺陷。
    """
    if framework_files is None:
        return []
    if isinstance(framework_files, str):
        items: Sequence[object] = [framework_files]
    else:
        items = list(framework_files)
    normalized = set()
    for item in items:
        if item is None:
            continue
        text = str(item).strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if not text:
            continue
        normalized.add(text)
    return sorted(normalized)


def _normalize_error_class(error_class: Optional[str]) -> str:
    """错误类别，按标识符口径归一：折叠空白 + 小写。

    口径**复用**仓库既有的 ``verification.failure_fingerprint._normalize_identifier``：
    错误类别的大小写不是身份的一部分（``AssertionError`` 与 ``assertionerror``
    是同一个类别），而大小写一分叉就会让全局去重漏掉同一个缺陷。口径只有
    一处 —— 内联第二套，两处就会漂。

    ``None`` 是唯一的例外：共享原语把「没有值」归一成 ``'<missing>'``，这里
    要的是空串 —— ``canonical_key`` 靠空串抛 ``ValueError``（错误类别缺失是
    「没有材料」，不是「类别叫 missing」）。
    """
    if error_class is None:
        return ""
    return _normalize_identifier(error_class)


# ---------------------------------------------------------------------------
# 键与指纹
# ---------------------------------------------------------------------------
def canonical_key(
    *,
    framework_files: Sequence[str],
    error_class: str,
    first_error_line: str,
) -> Dict[str, object]:
    """指纹的规范键 —— 计算指纹所依据的那份材料。

    Args:
        framework_files: 失败所在 / 修复将要触碰的框架文件（repo 相对路径）。
        error_class: 异常类型或错误类别。
        first_error_line: 首错行；在这里归一化。

    Returns:
        一个可直接 JSON 序列化的字典，三个键：``framework_files``（排序去重
        后的列表）、``error_class``（归一后的类别）、``first_error_line``
        （归一后的首错行）。

    Raises:
        ValueError: ``framework_files`` 归一后为空，或 ``error_class`` 归一后
            为空。两者都空意味着「没有材料」——那样的键不是身份，是噪声。
    """
    files = _normalize_files(framework_files)
    if not files:
        raise ValueError(
            "framework_files must not be empty: the fingerprint key needs at "
            "least one framework file"
        )
    klass = _normalize_error_class(error_class)
    if not klass:
        raise ValueError(
            "error_class must not be empty: the fingerprint key needs the "
            "failure's error class"
        )
    return {
        "framework_files": files,
        "error_class": klass,
        "first_error_line": normalize_line(first_error_line),
    }


def compute_fingerprint(
    *,
    framework_files: Sequence[str],
    error_class: str,
    first_error_line: str,
) -> str:
    """框架缺陷指纹 —— 全局去重键。

    Args:
        framework_files: 失败所在 / 修复将要触碰的框架文件（repo 相对路径）。
        error_class: 异常类型或错误类别。
        first_error_line: 首错行；在这里归一化。

    Returns:
        ``sha256:<64 位小写 hex>``。同一份材料恒定给出同一个值。

    Raises:
        ValueError: 见 :func:`canonical_key`。
    """
    key = canonical_key(
        framework_files=framework_files,
        error_class=error_class,
        first_error_line=first_error_line,
    )
    payload = json.dumps(
        key, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return FINGERPRINT_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 别名
# ---------------------------------------------------------------------------
def resolve_alias(
    fingerprint: str,
    lookup: Callable[[str], Optional[str]],
) -> str:
    """沿别名链解析到规范指纹。

    机械指纹会漏掉「症状不同但语义相同」的命中；语义复核判为同一个缺陷时
    写入别名映射（``framework_fix_alias``），后续同形命中靠这条链回到同一个
    规范指纹。

    Args:
        fingerprint: 待解析的指纹。
        lookup: 查一张别名表：给一个指纹，返回它指向的规范指纹；没有别名
            时返回 ``None``。

    Returns:
        规范指纹。链尾没有别名时，返回链尾自己。

    Raises:
        ValueError: 别名成环（含自环），或 ``fingerprint`` 不是非空字符串。
            成环的别名表没有「链尾」，解析会永远转下去 —— 那是数据坏了，
            不是「没找到」。
    """
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError("fingerprint must be a non-empty string")
    current = fingerprint.strip()
    seen = {current}
    while True:
        target = lookup(current)
        if target is None:
            return current
        if not isinstance(target, str) or not target.strip():
            raise ValueError(
                f"alias for {current!r} must point at a non-empty fingerprint"
            )
        target = target.strip()
        if target in seen:
            raise ValueError(
                f"alias cycle detected at {target!r} (chain: {sorted(seen)})"
            )
        seen.add(target)
        current = target
