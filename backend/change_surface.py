"""改动面解析：把 ``tasks.json`` 的 ``files_to_modify`` 并集变成结构化改动面。

这个模块解决什么
----------------
VP（验证点）生成器要决定"这次改动该生成哪类 E2E"，前提是它知道**这次改动
碰了哪些文件**。这个事实只存在于 plan 目录的 ``tasks.json`` 里 ——
``tasks[].files_to_modify`` 是每个任务声明的"我打算改哪些文件"。

生成器拿不到这个信息，后果不是"分类错了"，而是**根本没得分类**：
纯后端改动照样生成 ``ui_validation``（要起浏览器、要占机器），
本地 CLI 改动被当成服务型改动（要起服务）。所以这里把文件清单解析成
一个明确的数据结构，交给生成器消费。

产物
----
:class:`ChangeSurface` —— 三件东西：

``files``
    全部任务声明的文件**并集**，去重且**保序**（首次出现的顺序）。
    保序是因为提示词里"本次改动面"要读起来像人写的清单；去重是因为
    多个任务常常声明同一个文件（例如公共测试文件），重复出现会让读者
    以为改了两遍。

``kinds``
    命中的分类集合：``frontend`` / ``framework`` / ``cli`` / ``backend`` /
    ``other``。**一个文件命中多类时全部计入** —— 例如 ``scripts/build.js``
    既是脚本（``scripts/`` 前缀）又是前端资源（``.js`` 扩展名）。

``has_ui``
    只由 ``frontend`` 类决定。``backend/routes/`` 下的 HTTP 路由**不算**
    界面 —— 纯后端改动不该生成 ``ui_validation``。

分类规则
--------
规则**按顺序**求值，每条规则先命中先算：

=============  =========================================================
``frontend``   前缀 ``frontend/`` / ``templates/`` / ``static/``，
               或扩展名 ``.html`` / ``.css`` / ``.js`` / ``.tsx`` / ``.vue``
``framework``  前缀 ``backend/framework/``，或文件名 ``verification_*.py``
               / ``coding_tool.py``
``cli``        前缀 ``scripts/``，或文件名 ``cli.py`` / ``__main__.py``，
               或路径含 ``bin/`` 目录
``backend``    **其余** ``backend/**/*.py``
``other``      **其余**路径
=============  =========================================================

"其余"两字是承重的：``backend/verification_agent.py`` 既是框架层文件也是
后端 ``.py``，但它先命中 ``framework``，于是**不再**计入 ``backend`` ——
一次改动不该因为落在 ``backend/`` 目录下就被当成普通后端代码。

``has_ui`` 因此只反映"有没有碰到界面资源"，而不是"有没有碰到带
``backend/`` 前缀的文件"。

为什么读不到 tasks.json 时不抛异常
----------------------------------
改动面是**辅助信息**，不是门禁：读不到、解析失败、字段缺失时，正确行为是
"这次改动面未知"（空改动面），让下游按"无改动面"继续走，而不是让整个
生成链路崩在一个可选输入上。所以 :func:`load_change_surface` 在任何读盘 /
解析失败时都返回空改动面。

这个模块只依赖标准库 —— 它会被 ``verification_agent`` 之类的重型模块
导入，不该反过来拖慢或污染它们。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Iterable, List, Optional, Set, Tuple

#: plan 目录里的任务清单文件名。
TASKS_FILENAME = "tasks.json"

# --- 分类取值 ---------------------------------------------------------------

#: 前端：界面资源（模板、样式、脚本、前端组件）。
KIND_FRONTEND = "frontend"
#: 框架层：验证 / 编码工具这类"框架自己的代码"。
KIND_FRAMEWORK = "framework"
#: 命令行入口：脚本、可执行入口。
KIND_CLI = "cli"
#: 普通后端代码。
KIND_BACKEND = "backend"
#: 其它（文档、配置、示例……）。
KIND_OTHER = "other"

#: 分类的固定展示顺序。``kinds`` 是无序集合，但写进提示词时必须有稳定
#: 顺序，否则同一份改动面每次生成的提示词都不一样。
KIND_ORDER: Tuple[str, ...] = (
    KIND_FRONTEND,
    KIND_FRAMEWORK,
    KIND_CLI,
    KIND_BACKEND,
    KIND_OTHER,
)

# --- 路径形态 ---------------------------------------------------------------

#: 前端目录前缀。
_FRONTEND_PREFIXES: Tuple[str, ...] = ("frontend/", "templates/", "static/")

#: 前端文件扩展名。
_FRONTEND_SUFFIXES: Tuple[str, ...] = (".html", ".css", ".js", ".tsx", ".vue")

#: 框架层目录前缀。
_FRAMEWORK_PREFIX = "backend/framework/"

#: 框架层文件名（``verification_*.py`` 由前缀匹配覆盖）。
_FRAMEWORK_FILENAMES: Tuple[str, ...] = ("coding_tool.py",)

#: 框架层文件名前缀（``verification_agent.py`` / ``verification_ci_runner.py``）。
_FRAMEWORK_FILENAME_PREFIXES: Tuple[str, ...] = ("verification_",)

#: 命令行入口的目录前缀。
_CLI_PREFIXES: Tuple[str, ...] = ("scripts/",)

#: 命令行入口的文件名。
_CLI_FILENAMES: Tuple[str, ...] = ("cli.py", "__main__.py")

#: 命令行入口的目录名（``bin/`` 下的东西都是入口）。
_CLI_DIR_NAME = "bin/"

#: 后端代码的目录前缀与扩展名。
_BACKEND_PREFIX = "backend/"
_BACKEND_SUFFIX = ".py"

# --- 哨兵 -------------------------------------------------------------------

#: "作者没能列出要改哪些文件"的哨兵（见 ``backend/task.py`` 的同名常量）。
#: 它不是文件路径，出现在 ``files_to_modify`` 里时该任务被跳过。
UNKNOWN_MODIFICATIONS_SENTINEL = "__UNKNOWN_MODIFICATIONS__"

#: "这个任务不改任何文件"的哨兵（见 ``backend/task.py`` 的同名常量）。
NO_FILE_CHANGES_SENTINEL = "__NO_FILE_CHANGES__"

#: 所有"不是文件路径"的哨兵值。
_SENTINELS: Tuple[str, ...] = (
    UNKNOWN_MODIFICATIONS_SENTINEL,
    NO_FILE_CHANGES_SENTINEL,
)


@dataclass(frozen=True)
class ChangeSurface:
    """一次改动的结构化改动面。

    Attributes:
        files: 全部任务声明的文件并集，去重且保序。
        kinds: 命中的分类集合（``frontend`` / ``framework`` / ``cli`` /
            ``backend`` / ``other``）。空改动面时是空集合。
        has_ui: 这次改动是否触及界面资源。只由 ``frontend`` 类决定。
    """

    files: Tuple[str, ...] = ()
    kinds: FrozenSet[str] = frozenset()
    has_ui: bool = False

    @property
    def ordered_kinds(self) -> Tuple[str, ...]:
        """按 :data:`KIND_ORDER` 排好序的分类（写进提示词时用）。"""
        return tuple(kind for kind in KIND_ORDER if kind in self.kinds)


def _normalize(path: str) -> str:
    """把路径归一到"仓库相对、POSIX 分隔"的形式（只用于分类）。"""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _basename(path: str) -> str:
    """取路径的文件名部分。"""
    return path.rsplit("/", 1)[-1]


def _is_frontend(path: str) -> bool:
    """是不是界面资源（前端目录 / 前端扩展名）。"""
    if path.startswith(_FRONTEND_PREFIXES):
        return True
    return path.endswith(_FRONTEND_SUFFIXES)


def _is_framework(path: str) -> bool:
    """是不是框架层代码（``backend/framework/`` 或框架自己的模块）。"""
    if path.startswith(_FRAMEWORK_PREFIX):
        return True
    name = _basename(path)
    if name in _FRAMEWORK_FILENAMES:
        return True
    return name.startswith(_FRAMEWORK_FILENAME_PREFIXES) and name.endswith(
        _BACKEND_SUFFIX
    )


def _is_cli(path: str) -> bool:
    """是不是命令行入口（脚本目录 / 入口文件名 / ``bin/`` 目录）。"""
    if path.startswith(_CLI_PREFIXES):
        return True
    if _basename(path) in _CLI_FILENAMES:
        return True
    # ``bin/`` 目录下的东西都是入口：路径中间出现 ``bin/``，或者它就是
    # 顶层目录（``bin/tool.py``）。
    return f"/{_CLI_DIR_NAME}" in path or path.startswith(_CLI_DIR_NAME)


def _is_backend(path: str) -> bool:
    """是不是普通后端代码（``backend/`` 下的 ``.py``）。"""
    return path.startswith(_BACKEND_PREFIX) and path.endswith(_BACKEND_SUFFIX)


def _kinds_for(path: str) -> Set[str]:
    """一个文件命中的分类集合。

    具体规则（``frontend`` / ``framework`` / ``cli``）可以同时命中 ——
    全部计入。兜底规则（``backend`` / ``other``）只对**没被前面规则认领**
    的文件生效，这就是分类表里"其余"二字的含义。
    """
    normalized = _normalize(path)
    kinds: Set[str] = set()
    if _is_frontend(normalized):
        kinds.add(KIND_FRONTEND)
    if _is_framework(normalized):
        kinds.add(KIND_FRAMEWORK)
    if _is_cli(normalized):
        kinds.add(KIND_CLI)
    if not kinds and _is_backend(normalized):
        kinds.add(KIND_BACKEND)
    if not kinds:
        kinds.add(KIND_OTHER)
    return kinds


def classify(paths: Iterable[str]) -> ChangeSurface:
    """把一组文件路径分类成改动面。

    Args:
        paths: 仓库相对的文件路径（通常是各任务 ``files_to_modify``
            的并集）。空值 / 非字符串 / 重复项都会被安全地跳过或去重。

    Returns:
        :class:`ChangeSurface`：去重保序的文件列表 + 命中的分类集合 +
        是否触及界面。
    """
    files: List[str] = []
    seen: Set[str] = set()
    kinds: Set[str] = set()

    for raw in paths or ():
        if not isinstance(raw, str):
            continue
        path = raw.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        files.append(path)
        kinds.update(_kinds_for(path))

    return ChangeSurface(
        files=tuple(files),
        kinds=frozenset(kinds),
        has_ui=KIND_FRONTEND in kinds,
    )


def _extract_task_rows(payload: object) -> Optional[List[object]]:
    """从 tasks.json 的载荷里取出任务数组。

    接受两种形态：规范信封 ``{"tasks": [...]}`` 与裸数组 ``[...]``。
    其它形态返回 ``None``（调用方据此返回空改动面）。
    """
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        rows = payload.get("tasks")
        if isinstance(rows, list):
            return list(rows)
    return None


def _declared_paths(rows: Iterable[object]) -> List[str]:
    """收集全部任务声明的真实文件路径（跳过哨兵与非字符串项）。"""
    paths: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        declared = row.get("files_to_modify")
        if not isinstance(declared, list):
            # 没有声明文件的任务（老计划 / 纯分析任务）不参与改动面。
            continue
        for entry in declared:
            if not isinstance(entry, str):
                continue
            if entry.strip() in _SENTINELS:
                # 哨兵不是文件路径：``__UNKNOWN_MODIFICATIONS__`` 表示
                # "作者没列出改哪些文件"，``__NO_FILE_CHANGES__`` 表示
                # "这个任务不改文件"。两者都让该任务不贡献改动面。
                continue
            paths.append(entry)
    return paths


def load_change_surface(plan_dir: Path) -> ChangeSurface:
    """读 plan 目录的 ``tasks.json``，解析出改动面。

    Args:
        plan_dir: plan 目录（里面应该有 ``tasks.json``）。

    Returns:
        :class:`ChangeSurface`：全部任务 ``files_to_modify`` 的并集分类。
        **读不到、解析失败、格式不认识时返回空改动面，不抛异常** ——
        改动面是辅助信息，不是门禁。
    """
    plan_dir = Path(plan_dir)
    tasks_path = plan_dir / TASKS_FILENAME

    try:
        raw = tasks_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ChangeSurface()

    try:
        payload = json.loads(raw)
    except ValueError:
        return ChangeSurface()

    rows = _extract_task_rows(payload)
    if rows is None:
        return ChangeSurface()

    return classify(_declared_paths(rows))
