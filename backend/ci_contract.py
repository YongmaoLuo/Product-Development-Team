"""CI 契约：每个 Plan 自己声明"我的门禁是什么意思"。

这个模块解决什么
----------------
整仓门禁（``full_ci``）把"哪些退出码表示门禁没能判定"写死在框架里
（``verification_ci_runner`` 里的 ``GATE_INCONCLUSIVE_EXIT_CODE = 3``）。
换一个门禁脚本的项目，机器跑不动会被记成代码坏了 —— 而项目自己最清楚
它的门禁用哪个退出码表示"没判定"。

所以判定口径从框架常量变成**每个 Plan 的一份契约文件**：
``<plan_dir>/ci_contract.json``。本模块只负责**读它、校验它、缺省回落**，
不做任何判定 —— 判定接入在 :mod:`verification_ci_runner` 那一侧。

四个字段
--------
==================  ==============  =========================================
字段                 缺省             含义
==================  ==============  =========================================
``inconclusive_    ``(3,)``        哪些退出码表示"门禁没能判定"（不是失败）。
``exit_codes``                       缺省沿用框架自己门禁的 3。
``budget_source``  ``"gate_script"`` 预算从哪来：``"gate_script"`` = 读门禁
                                    脚本自己声明的预算；``"contract"`` =
                                    用契约里写的 ``budget_seconds``。
``budget_seconds`` ``None``        契约声明的预算秒数。**只在**
                                    ``budget_source == "contract"`` 时生效，
                                    且必须是正整数。
``summary_format``  ``"gate_v1"``   摘要格式：``"gate_v1"`` = 按框架自己的
                                    门禁摘要格式解析；``"none"`` = 不解析摘要。
==================  ==============  =========================================

两套缺省，为什么不一样
----------------------
* **完全没有契约**（没有 ``plan_dir``，或目录里没有 ``ci_contract.json``）
  → 框架默认：``(3,)`` / ``"gate_script"`` / ``None`` / ``"gate_v1"``。
  这一条是**零回归**保证：不写契约的项目，判定与引入契约之前逐字一致。
* **契约存在但没写某个字段** → 该字段回落框架默认，**除了**
  ``summary_format``：契约没声明格式，就**不解析摘要**（``"none"``）。
  契约存在本身就是一句话："这个门禁的输出不一定长得像框架自己的"。
  契约没写摘要格式时按 ``"gate_v1"`` 解析，等于替契约作者猜了一个他没
  答应过的格式。

契约坏了不影响判定
------------------
契约是**输入**，不是判据。读不了（文件读不了，**连 stat 都做不了** ——
计划目录的权限不对）/ 不是合法 JSON / 顶层不是对象 / 字段
类型不对 —— 任何一种坏法，都只做两件事：回落框架默认 + ``logger.warning``
说明原因。**本模块不抛异常**：契约坏了不能把门禁带崩，更不能把"机器跑
不动"改写成"代码坏了"。

字段级回落
----------
坏字段是**逐个**回落的，不是整份契约作废：一份声明了退出码但预算写错的
契约，退出码照常生效，预算回落默认。回落的原因记在 :attr:`CiContract.error`
里，与文件级失败共用同一个字段（文件级失败时 ``applied`` 为 ``False``，
字段级回落时 ``applied`` 仍为 ``True`` —— 契约本身是被用上了的，只是有
字段被拒了）。

显式 ``null``
-------------
JSON 里的 ``null`` 按"没声明"处理（回落默认），与不写这个键同义 —— 这是
JSON 里表达"这个字段不适用"的常规写法，不当成类型错误。

消费方式
--------
* :func:`load_ci_contract` —— 唯一入口，只读文件，绝不抛异常。
* :class:`CiContract` —— 结果；:meth:`CiContract.parses_summary` 回答
  "要不要解析摘要"，:meth:`CiContract.to_evidence` 给出可 JSON 序列化的
  证据（落在审计产物里的那份）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 契约文件名（放在 plan 目录里）。
CONTRACT_FILENAME = "ci_contract.json"

#: 框架自己门禁的"没能判定"退出码。契约没声明时的缺省。
FRAMEWORK_INCONCLUSIVE_EXIT_CODES: Tuple[int, ...] = (3,)

#: 预算来源：读门禁脚本自己声明的预算。
BUDGET_SOURCE_GATE_SCRIPT = "gate_script"
#: 预算来源：用契约里写的秒数。
BUDGET_SOURCE_CONTRACT = "contract"
#: 预算来源的合法取值。
BUDGET_SOURCES: Tuple[str, ...] = (BUDGET_SOURCE_GATE_SCRIPT, BUDGET_SOURCE_CONTRACT)

#: 摘要格式：框架自己的门禁摘要格式。
SUMMARY_FORMAT_GATE_V1 = "gate_v1"
#: 摘要格式：不解析摘要。
SUMMARY_FORMAT_NONE = "none"
#: 摘要格式的合法取值。
SUMMARY_FORMATS: Tuple[str, ...] = (SUMMARY_FORMAT_GATE_V1, SUMMARY_FORMAT_NONE)

#: 没有契约时的缺省预算来源。
DEFAULT_BUDGET_SOURCE = BUDGET_SOURCE_GATE_SCRIPT
#: 没有契约时的缺省摘要格式（框架自己的格式，零回归）。
DEFAULT_SUMMARY_FORMAT = SUMMARY_FORMAT_GATE_V1
#: 契约存在、但没声明摘要格式时的取值：不解析摘要。
CONTRACT_ABSENT_SUMMARY_FORMAT = SUMMARY_FORMAT_NONE


@dataclass(frozen=True)
class CiContract:
    """一份契约读出来的结果。

    Attributes:
        inconclusive_exit_codes: 哪些退出码表示"门禁没能判定"。缺省
            ``FRAMEWORK_INCONCLUSIVE_EXIT_CODES``。
        budget_source: ``"gate_script"`` / ``"contract"``。
        budget_seconds: 契约声明的预算秒数；只在 ``budget_source`` 是
            ``"contract"`` 时有意义，否则是 ``None``。
        summary_format: ``"gate_v1"`` / ``"none"``。
        path: 契约文件的路径；没有契约文件（或 ``plan_dir`` 是 ``None``）
            时是 ``None``。
        applied: 契约是否被用上了。文件级失败（读不了 / 非法 JSON /
            顶层不是对象）是 ``False``；字段级回落不影响它，仍是
            ``True``。
        error: 契约为什么没被完全采信；没有问题时是 ``None``。
    """

    inconclusive_exit_codes: Tuple[int, ...] = FRAMEWORK_INCONCLUSIVE_EXIT_CODES
    budget_source: str = DEFAULT_BUDGET_SOURCE
    budget_seconds: Optional[int] = None
    summary_format: str = DEFAULT_SUMMARY_FORMAT
    path: Optional[Path] = None
    applied: bool = False
    error: Optional[str] = None

    def parses_summary(self) -> bool:
        """要不要按门禁摘要格式解析输出。

        只有 ``"gate_v1"`` 解析；``"none"`` 表示这份契约没描述门禁的
        输出格式，解析就是猜。
        """
        return self.summary_format == SUMMARY_FORMAT_GATE_V1

    def to_evidence(self) -> Dict[str, Any]:
        """给出可 JSON 序列化的证据（落在审计产物里的那份）。

        退出码在这里是**列表**：这份字典要直接 ``json.dump``，列表是
        JSON 里的数组形态。
        """
        return {
            "path": str(self.path) if self.path is not None else None,
            "applied": self.applied,
            "inconclusive_exit_codes": list(self.inconclusive_exit_codes),
            "budget_source": self.budget_source,
            "budget_seconds": self.budget_seconds,
            "summary_format": self.summary_format,
            "parses_summary": self.parses_summary(),
            "error": self.error,
        }


def load_ci_contract(plan_dir: Optional[Path]) -> CiContract:
    """读一个 plan 的 CI 契约。

    这是**唯一**的入口：只读文件，绝不抛异常。

    Args:
        plan_dir: plan 目录；``None`` 表示没有 plan 目录可查。

    Returns:
        :class:`CiContract`：没有契约 / 契约坏了 / 契约完好，三种情况
        各自对应的结果。契约坏了时是框架默认 + ``error`` 说明原因 +
        一条 warning 日志。
    """
    if plan_dir is None:
        return CiContract()

    try:
        path = Path(plan_dir) / CONTRACT_FILENAME
    except TypeError as exc:  # 目录给的不是路径 —— 当成"没有契约"
        logger.warning(
            "ci_contract: unusable plan_dir %r (%s) — using framework defaults",
            plan_dir,
            exc,
        )
        return CiContract(error=f"unusable plan_dir: {exc}")

    try:
        contract_exists = path.exists()
    except OSError as exc:
        # 计划目录读不了（EACCES / EPERM）：``Path.exists()`` 只吞掉少数
        # 几个 errno（ENOENT、ENOTDIR、EBADF、ENAMETOOLONG），其余的会
        # 重新抛出来 —— 那个异常会一路穿出这个入口，把门禁带崩。而"读不
        # 了契约"恰恰是最该回落框架默认的一种坏法。
        return _broken(path, f"cannot read the contract file: {exc}")
    except Exception as exc:
        # 意外异常同样不能带崩门禁 —— 这个模块的承诺是"绝不抛"。
        return _broken(path, f"cannot read the contract file: {exc}")

    if not contract_exists:
        # 没有契约不是错误 —— 这是绝大多数 plan 的样子。
        return CiContract()

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _broken(path, f"cannot read the contract file: {exc}")

    try:
        data = json.loads(raw)
    except ValueError as exc:
        return _broken(path, f"the contract file is not valid JSON: {exc}")
    except RecursionError as exc:
        # 嵌套太深的载荷：解码器报的不是 ValueError，得自己接住。
        return _broken(path, f"the contract file is nested too deeply to parse: {exc}")

    if not isinstance(data, dict):
        return _broken(
            path,
            f"the contract must be a JSON object, got {type(data).__name__}",
        )

    return _apply(path, data)


def _broken(path: Path, reason: str) -> CiContract:
    """契约整个不可用：框架默认 + 记原因 + 一条 warning。"""
    logger.warning("ci_contract: %s — %s; using framework defaults", path, reason)
    return CiContract(path=path, applied=False, error=reason)


def _apply(path: Path, data: Dict[str, Any]) -> CiContract:
    """把一份能读懂的对象变成契约；坏字段逐个回落。"""
    problems: List[str] = []

    codes = _read_exit_codes(data, problems)
    budget_source = _read_budget_source(data, problems)
    budget_seconds = _read_budget_seconds(data, budget_source, problems)
    summary_format = _read_summary_format(data, problems)

    if problems:
        logger.warning(
            "ci_contract: %s — %s", path, "; ".join(problems)
        )

    return CiContract(
        inconclusive_exit_codes=codes,
        budget_source=budget_source,
        budget_seconds=budget_seconds,
        summary_format=summary_format,
        path=path,
        applied=True,
        error="; ".join(problems) if problems else None,
    )


def _read_exit_codes(data: Dict[str, Any], problems: List[str]) -> Tuple[int, ...]:
    """读 ``inconclusive_exit_codes``；类型不对就回落框架默认。"""
    raw = data.get("inconclusive_exit_codes")
    if raw is None:
        return FRAMEWORK_INCONCLUSIVE_EXIT_CODES

    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        problems.append(
            f"inconclusive_exit_codes must be a list of ints, "
            f"got {type(raw).__name__}"
        )
        return FRAMEWORK_INCONCLUSIVE_EXIT_CODES

    codes: List[int] = []
    for item in raw:
        # ``True`` 是 ``int`` 的子类，但它不是一个退出码。
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            problems.append(
                f"inconclusive_exit_codes entry {item!r} is not a "
                f"non-negative int"
            )
            return FRAMEWORK_INCONCLUSIVE_EXIT_CODES
        codes.append(item)

    if not codes:
        # 空列表会让框架自己的 3 号退出码静默失效 —— 那正是这个特性要
        # 修的"机器跑不动被记成代码坏了"。当作坏字段，回落默认。
        problems.append("inconclusive_exit_codes is empty")
        return FRAMEWORK_INCONCLUSIVE_EXIT_CODES

    return tuple(codes)


def _read_budget_source(data: Dict[str, Any], problems: List[str]) -> str:
    """读 ``budget_source``；不是合法取值就回落 ``"gate_script"``。"""
    raw = data.get("budget_source")
    if raw is None:
        return DEFAULT_BUDGET_SOURCE

    if not isinstance(raw, str) or raw not in BUDGET_SOURCES:
        problems.append(
            f"budget_source must be one of {list(BUDGET_SOURCES)}, got {raw!r}"
        )
        return DEFAULT_BUDGET_SOURCE

    return raw


def _read_budget_seconds(
    data: Dict[str, Any],
    budget_source: str,
    problems: List[str],
) -> Optional[int]:
    """读 ``budget_seconds``；必须是正整数，且只在预算来自契约时生效。

    类型**总是**校验：写错类型的字段要回落并留日志，不管它这一轮有没有
    被用上 —— 没被用上的坏字段同样说明契约作者写错了什么。
    """
    raw = data.get("budget_seconds")
    if raw is None:
        return None

    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        problems.append(f"budget_seconds must be a positive int, got {raw!r}")
        return None

    if budget_source != BUDGET_SOURCE_CONTRACT:
        # 预算不来自契约，契约里写多少都不算数。
        return None

    return raw


def _read_summary_format(data: Dict[str, Any], problems: List[str]) -> str:
    """读 ``summary_format``；只接受 ``"gate_v1"`` / ``"none"``。

    没声明时是 ``"none"``：契约存在就说明这个门禁的输出不一定长得像
    框架自己的，没声明格式就不解析摘要。
    """
    raw = data.get("summary_format")
    if raw is None:
        return CONTRACT_ABSENT_SUMMARY_FORMAT

    if not isinstance(raw, str) or raw not in SUMMARY_FORMATS:
        problems.append(
            f"summary_format must be one of {list(SUMMARY_FORMATS)}, got {raw!r}"
        )
        return SUMMARY_FORMAT_NONE

    return raw