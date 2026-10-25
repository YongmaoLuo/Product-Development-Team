"""产物判定：plan 目录里"这次要产出哪类产物"的**唯一**判据。

这个模块解决什么
----------------
一个 plan 目录里可能躺着两类互斥的产物：

* ``tasks.json`` —— 开发任务清单。存在它 ⇒ 这条 plan 要先执行再验证。
* ``verification-plan.md`` —— 验证计划书。存在它 ⇒ 这条 plan 跳过初始
  执行，直接进入验证。

框架里"计划处于什么阶段 / 该走哪条路"的判断位置不止一处：阶段推断
(:meth:`backend.plan_state.PlanState._infer_phase`) 按文件存在性推断阶段，
任务生成入口有自己的前置检查，验证入口有自己的阶段 CAS 与计划书兜底，计划
列表与状态接口还会各自读一次产物。

如果每个调用点各自 ``stat`` 一次文件，判定必然漂移：某个入口同时看到两类
产物时自己挑了一个，另一个入口报错，第三个入口推断出错误阶段；冲突处理也
会各写一套。这不是"谁写得细"的问题 —— 是同一件事被复制了三遍。

所以判定收敛到这里：**纯函数、只读目录、每次重算**。调用点只消费结果，不
再自己看文件。

四种取值
--------
===============  ====================================================
``branch``       含义
===============  ====================================================
``dev``          只有 ``tasks.json`` —— 走执行再验证
``verification`` 只有 ``verification-plan.md`` —— 跳过初始执行直接验证
``conflict``     两类产物同时存在 —— **拒绝猜测意图**，由调用方报冲突
``empty``        两类产物都不存在 —— 旧计划的行为，保持原样
===============  ====================================================

为什么不持久化这个分支
----------------------
分支是**从磁盘算出来的事实**，不是用户选择的开关。一旦把它写进
``plan_state.json`` 或 ``state.db``，就多出第二个真源：产物被手工删掉 /
改名 / 补上之后，持久化的分支与磁盘事实会不一致，而且没人裁决谁对。分支
不进任何请求参数，也不进状态文件 —— 每次从磁盘重新计算。

冲突为什么必须显式报错
----------------------
两类产物同时存在时，"它到底想干什么"**只有人能回答**：是要执行开发任务，
还是要按计划书直接验证？框架替它选一个，等于替用户做决定；框架直接拒绝，
至少把两个路径都交回去，由操作者裁决。因此 ``conflict`` 保留**两个**路径，
不自动删除、不自动择一。

消费方式
--------
* :func:`detect` —— 判定入口，返回 :class:`ArtifactSet`。
* :func:`require_branch` —— 门禁谓词：实际分支与期望不符（或实际是冲突）
  时抛 :class:`ArtifactConflictError`，异常里带着相关产物路径。
* :func:`is_verification_branch` —— 只关心"是不是验证分支"的布尔映射。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Tuple

#: 开发分支的产物：执行任务清单。
TASKS_FILENAME = "tasks.json"

#: 验证分支的产物：验证计划书（人工审核对象，机器格式由它翻译而来）。
PLAN_DOC_FILENAME = "verification-plan.md"

#: 只有开发任务清单。
BRANCH_DEV = "dev"
#: 只有验证计划书。
BRANCH_VERIFICATION = "verification"
#: 两类产物同时存在 —— 冲突，调用方必须拒绝猜测。
BRANCH_CONFLICT = "conflict"
#: 两类产物都不存在。
BRANCH_EMPTY = "empty"

#: 分支取值的穷举集合。
Branch = Literal["dev", "verification", "conflict", "empty"]

#: 全部合法分支取值，供调用方做入参校验。
BRANCHES: Tuple[str, ...] = (
    BRANCH_DEV,
    BRANCH_VERIFICATION,
    BRANCH_CONFLICT,
    BRANCH_EMPTY,
)


@dataclass(frozen=True)
class ArtifactSet:
    """一次判定的结果：产物路径 + 分支取值。

    Attributes:
        tasks_path: ``tasks.json`` 的路径；不存在时为 ``None``。
        plan_doc_path: ``verification-plan.md`` 的路径；不存在时为 ``None``。
        branch: ``dev`` / ``verification`` / ``conflict`` / ``empty``。
    """

    tasks_path: Optional[Path]
    plan_doc_path: Optional[Path]
    branch: Branch


class ArtifactConflictError(RuntimeError):
    """产物冲突：两类产物同时存在，或实际分支与调用方期望的不符。

    Attributes:
        paths: 参与冲突的产物路径（冲突时是两个，不匹配时是实际存在的那
            份，产物一个都没有时为空元组）。路径原样交给调用方，调用方
            负责把它们变成 409 响应体里的可诊断信息。
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

    if has_tasks and has_plan_doc:
        branch: Branch = BRANCH_CONFLICT
    elif has_tasks:
        branch = BRANCH_DEV
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
        ArtifactConflictError: 实际分支是 ``conflict``（两类产物同时存在，
            永远不可被"要求"），或实际分支与 ``expected`` 不符（含产物一个
            都没有时的 ``empty``）。异常里带着相关产物路径。
    """
    artifacts = detect(plan_dir)
    found = tuple(
        p for p in (artifacts.tasks_path, artifacts.plan_doc_path) if p is not None
    )

    if artifacts.branch == BRANCH_CONFLICT:
        raise ArtifactConflictError(
            found,
            expected=expected,
            actual=artifacts.branch,
        )

    if artifacts.branch != expected:
        raise ArtifactConflictError(
            found,
            expected=expected,
            actual=artifacts.branch,
        )

    return artifacts


def is_verification_branch(plan_dir: Path) -> bool:
    """plan 目录是不是验证分支（只有验证计划书）。

    冲突**不算**验证分支 —— 冲突的处置是拒绝，不是当成验证分支跑。
    """
    return detect(plan_dir).branch == BRANCH_VERIFICATION