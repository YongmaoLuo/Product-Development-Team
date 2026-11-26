"""产物判定：plan 目录里"这次要产出哪类产物"的**唯一**判据。

这个模块解决什么
----------------
一个 plan 目录里可能躺着两类产物：

* ``tasks.json`` —— 开发任务清单。存在它 ⇒ 这条 plan 要先执行再验证。
* ``verification-plan.md`` —— 验证计划书。存在它 ⇒ 这条 plan 跳过初始
  执行，直接进入验证。

框架里"计划处于什么阶段 / 该走哪条路"的判断位置不止一处：阶段推断
(:meth:`backend.plan_state.PlanState._infer_phase`) 按文件存在性推断阶段，
任务生成入口有自己的前置检查，验证入口有自己的阶段 CAS 与计划书兜底，计划
列表与状态接口还会各自读一次产物。

如果每个调用点各自 ``stat`` 一次文件，判定必然漂移：某个入口同时看到两类
产物时自己挑了一个，另一个入口报错，第三个入口推断出错误阶段。这不是"谁写
得细"的问题 —— 是同一件事被复制了三遍。

所以判定收敛到这里：**纯函数、只读目录、每次重算**。调用点只消费结果，不
再自己看文件。

三种取值
--------
===============  ====================================================
``branch``       含义
===============  ====================================================
``dev``          有 ``tasks.json`` —— 走执行再验证
``verification`` 只有 ``verification-plan.md`` —— 跳过初始执行直接验证
``empty``        两类产物都不存在 —— 旧计划的行为，保持原样
===============  ====================================================

两类产物共存时：**主分支是 ``dev``**
------------------------------------
``tasks.json`` 与 ``verification-plan.md`` 同时存在**不是冲突**：按「任务
优先」的主分支语义，这条 plan 就是一条开发分支的计划，只不过额外带着一份
计划书（附加产物）。因此：

* :func:`detect` 返回 ``dev``，``plan_doc_path`` 非空 —— 附加产物由路径与
  :attr:`ArtifactSet.has_plan_doc` / :attr:`ArtifactSet.is_reviewed_plan`
  暴露，调用点不需要新增数据结构；
* :func:`is_verification_branch` 保持**狭窄**定义（只有计划书才算）：验证
  分支跳过初始执行，共存目录若被当成验证分支，开发任务就会被跳过。

``BRANCH_CONFLICT`` 常量保留（API 错误码契约：``artifact_conflict``），
但 :func:`detect` **不再产出**它 —— 输出域恰为 ``dev`` / ``verification``
/ ``empty`` 三个取值。

来源判定：验证轮消费哪一份计划
------------------------------
除了"走哪条分支"，验证轮还要回答"我该消费哪一份验证计划"。这是**另一个**
问题，由 :func:`resolve_plan_source` 判定，两个判定**正交**：

* :func:`detect` 看 ``tasks.json`` 与 ``verification-plan.md``；
* :func:`resolve_plan_source` 看 ``verification-plan.md`` 与机器计划
  ``verification_plan.json``。

来源**不能**从 :func:`detect` 派生 —— 有机器计划却没有计划书的目录，
:func:`detect` 判 ``empty`` / ``dev``，来源却是 ``MACHINE_ONLY``。

=================  ==========================================
来源               磁盘状态
=================  ==========================================
``REVIEWED``       计划书在（机器计划在不在都一样）
``MACHINE_ONLY``   计划书不在、机器计划在
``NONE``           两者都不在
=================  ==========================================

判定优先级固定，计划书优先：计划书是**真源**，机器计划是它的派生物。两者同时
在时消费的仍然是机器计划（不重新翻译），但它必须与**当前**计划书逐条等价。

为什么不持久化这个分支
----------------------
分支是**从磁盘算出来的事实**，不是用户选择的开关。一旦把它写进
``plan_state.json`` 或 ``state.db``，就多出第二个真源：产物被手工删掉 /
改名 / 补上之后，持久化的分支与磁盘事实会不一致，而且没人裁决谁对。分支
不进任何请求参数，也不进状态文件 —— 每次从磁盘重新计算。

门禁谓词仍按「不匹配即拒」
--------------------------
:func:`require_branch` 的职责没有随输出域变化：期望分支与实际不符时抛
:class:`ArtifactConflictError`，异常里带着相关产物路径。共存合法**不等于**
门禁失效 —— 要求 ``verification`` 的调用点仍然会被拦住。

消费方式
--------
* :func:`detect` —— 判定入口，返回 :class:`ArtifactSet`。
* :func:`require_branch` —— 门禁谓词：实际分支与期望不符时抛
  :class:`ArtifactConflictError`，异常里带着相关产物路径。
* :func:`is_verification_branch` —— 只关心"是不是验证分支"的布尔映射。
* :func:`resolve_plan_source` —— 来源判定入口，返回 :class:`PlanSource`。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Optional, Tuple

#: 开发分支的产物：执行任务清单。
TASKS_FILENAME = "tasks.json"

#: 验证分支的产物：验证计划书（人工审核对象，机器格式由它翻译而来）。
PLAN_DOC_FILENAME = "verification-plan.md"

#: 验证分支的机器计划：验证回路真正消费的那一份（由已审阅计划书翻译而来）。
#:
#: 它是**派生物**，不是真源 —— 真源是 :data:`PLAN_DOC_FILENAME`。两者同时
#: 在时，机器计划仍然要与计划书逐条等价（等价性门禁）。
MACHINE_PLAN_FILENAME = "verification_plan.json"

#: 只有开发任务清单。
BRANCH_DEV = "dev"
#: 只有验证计划书。
BRANCH_VERIFICATION = "verification"
#: 两类产物同时存在 —— **历史取值**。
#:
#: 保留是因为它是 API 错误码契约的一部分（``artifact_conflict``）：调用方
#: 仍会用这个字符串去识别"产物状态不符"这一类回答。但 :func:`detect` 已经
#: **不再产出**它 —— 共存按「任务优先」归入 :data:`BRANCH_DEV`。
BRANCH_CONFLICT = "conflict"
#: 两类产物都不存在。
BRANCH_EMPTY = "empty"

#: 分支取值的穷举集合 —— :func:`detect` 的输出域恰为这三个取值。
Branch = Literal["dev", "verification", "empty"]

#: 全部合法分支取值，供调用方做入参校验。
BRANCHES: Tuple[str, ...] = (
    BRANCH_DEV,
    BRANCH_VERIFICATION,
    BRANCH_EMPTY,
)


@dataclass(frozen=True)
class ArtifactSet:
    """一次判定的结果：产物路径 + 主分支取值。

    Attributes:
        tasks_path: ``tasks.json`` 的路径；不存在时为 ``None``。
        plan_doc_path: ``verification-plan.md`` 的路径；不存在时为 ``None``。
            共存时它**非空** —— 那是附加产物，不是冲突。
        branch: ``dev`` / ``verification`` / ``empty``。
    """

    tasks_path: Optional[Path]
    plan_doc_path: Optional[Path]
    branch: Branch

    @property
    def has_tasks(self) -> bool:
        """目录里有没有 ``tasks.json``。"""
        return self.tasks_path is not None

    @property
    def has_plan_doc(self) -> bool:
        """目录里有没有 ``verification-plan.md``（附加产物也算）。"""
        return self.plan_doc_path is not None

    @property
    def is_reviewed_plan(self) -> bool:
        """这份 plan 带不带一份**已审阅的验证计划书**。

        判定只看文件在不在：``verification-plan.md`` 就是人工审阅对象，机器
        翻译产物（``verification_plan.json``）不由这条判定看（那是
        :func:`resolve_plan_source` 的判定域），所以 ``is_reviewed_plan``
        等价于 ``has_plan_doc``。

        但调用点要问"有没有计划书"时应该用这个**语义名**，而不是拿
        :func:`is_verification_branch` 代理 —— 后者只认"目录里只有计划书"，
        共存目录不是验证分支。
        """
        return self.plan_doc_path is not None


class PlanSource(Enum):
    """验证计划的来源：验证轮该消费哪一份计划。

    与 :class:`ArtifactSet`（"这条 plan 走哪条分支"）回答的是**两个不同的
    问题**，取值也互相独立。

    Attributes:
        REVIEWED: 目录里有已审阅的验证计划书（``verification-plan.md``）。
            机器计划（``verification_plan.json``）在不在都不改变这个取值 ——
            计划书是**真源**，机器计划是它的派生物，两者必须过等价性门禁。
        MACHINE_ONLY: 只有机器计划、没有计划书。沿用既有的磁盘复用语义。
        NONE: 两者都没有。走既有的 LLM Phase-1 生成路径。
    """

    REVIEWED = "reviewed"
    MACHINE_ONLY = "machine_only"
    NONE = "none"


class ArtifactConflictError(RuntimeError):
    """产物状态与调用方的期望不符。

    Attributes:
        paths: 参与冲突的产物路径（不匹配时是实际存在的那份，产物一个都没
            有时为空元组）。路径原样交给调用方，调用方负责把它们变成 409
            响应体里的可诊断信息。
    """

    def __init__(
        self,
        paths: Tuple[Path, ...],
        *,
        expected: Optional[str] = None,
        actual: Optional[str] = None,
    ) -> None:
        self.paths = tuple(paths)
        self.expected = expected
        self.actual = actual
        listed = ", ".join(str(p) for p in self.paths) or "<no artifact found>"
        super().__init__(
            "artifact conflict: "
            f"expected={expected!r}, actual={actual!r}, paths=[{listed}]"
        )


def detect(plan_dir: Path) -> ArtifactSet:
    """判定 plan 目录当前属于哪条分支。

    这是**唯一**的判定入口：只读目录，不写任何东西，每次调用都从磁盘
    重新计算。

    两类产物共存时按「任务优先」归入 ``dev``：计划书是**附加产物**，由
    ``plan_doc_path`` 与 :attr:`ArtifactSet.has_plan_doc` 暴露。

    Args:
        plan_dir: plan 目录。目录本身不存在时返回 ``empty``，不抛错 ——
            判定是纯读，调用方决定"没有产物"意味着什么。

    Returns:
        :class:`ArtifactSet`：存在的产物路径（不存在的为 ``None``）+ 分支
        取值。
    """
    plan_dir = Path(plan_dir)
    tasks_path = plan_dir / TASKS_FILENAME
    plan_doc_path = plan_dir / PLAN_DOC_FILENAME

    has_tasks = tasks_path.exists()
    has_plan_doc = plan_doc_path.exists()

    if has_tasks:
        # 共存（``has_plan_doc`` 也为真）时主分支仍是 dev：计划书是附加
        # 产物，不是冲突。
        branch: Branch = BRANCH_DEV
    elif has_plan_doc:
        branch = BRANCH_VERIFICATION
    else:
        branch = BRANCH_EMPTY

    return ArtifactSet(
        tasks_path=tasks_path if has_tasks else None,
        plan_doc_path=plan_doc_path if has_plan_doc else None,
        branch=branch,
    )


def require_branch(plan_dir: Path, expected: str) -> ArtifactSet:
    """门禁谓词：实际分支必须等于 ``expected``。

    Args:
        plan_dir: plan 目录。
        expected: 期望的分支取值（``dev`` / ``verification`` / ``empty``）。

    Returns:
        匹配时返回 :class:`ArtifactSet`（与 :func:`detect` 的结果一致）。

    Raises:
        ArtifactConflictError: 实际分支与 ``expected`` 不符（含产物一个都
            没有时的 ``empty``）。异常里带着相关产物路径。
    """
    artifacts = detect(plan_dir)
    found = tuple(
        p for p in (artifacts.tasks_path, artifacts.plan_doc_path) if p is not None
    )

    if artifacts.branch != expected:
        raise ArtifactConflictError(
            found,
            expected=expected,
            actual=artifacts.branch,
        )

    return artifacts


def is_verification_branch(plan_dir: Path) -> bool:
    """plan 目录是不是验证分支（**只有**验证计划书）。

    共存（``tasks.json`` + ``verification-plan.md``）**不算**验证分支 ——
    验证分支跳过初始执行，共存目录的主分支是开发分支。
    """
    return detect(plan_dir).branch == BRANCH_VERIFICATION


def resolve_plan_source(plan_dir: Path) -> PlanSource:
    """判定验证计划的来源：验证轮该消费哪一份计划。

    与 :func:`detect` **正交** —— 这是本模块存在的理由，不是重复：

    * :func:`detect` 看 ``tasks.json`` 与 ``verification-plan.md``，回答"这条
      plan 走哪条分支"；
    * :func:`resolve_plan_source` 看 ``verification-plan.md`` 与机器计划
      ``verification_plan.json``，回答"验证轮消费哪一份计划"。

    所以来源**不能**从 :func:`detect` 派生：有机器计划却没有计划书的目录，
    :func:`detect` 判 ``empty`` / ``dev``，来源却是 ``MACHINE_ONLY``。

    判定优先级固定：

    * 计划书在（机器计划在不在都一样）→ :attr:`PlanSource.REVIEWED`：计划书
      是真源，机器计划由它翻译而来，且必须过等价性门禁；
    * 计划书不在、机器计划在 → :attr:`PlanSource.MACHINE_ONLY`：沿用既有的
      磁盘复用路径；
    * 两者都不在 → :attr:`PlanSource.NONE`：走既有的 LLM Phase-1 生成路径。

    与 :func:`detect` 一样：**纯读、每次重算、不写任何文件**。

    Args:
        plan_dir: plan 目录。目录本身不存在时返回 :attr:`PlanSource.NONE`，
            不抛错 —— 判定是纯读，调用方决定"没有产物"意味着什么。

    Returns:
        :class:`PlanSource`：来源取值。
    """
    plan_dir = Path(plan_dir)

    if (plan_dir / PLAN_DOC_FILENAME).exists():
        return PlanSource.REVIEWED
    if (plan_dir / MACHINE_PLAN_FILENAME).exists():
        return PlanSource.MACHINE_ONLY
    return PlanSource.NONE