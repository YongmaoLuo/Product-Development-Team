"""CI 适配脚本的统一判定（2026-11-05）。

这个模块解决什么
----------------
CI 验收的形态是多样的：有的项目在本地跑门禁，有的在 GitHub Actions 上跑，
有的要把代码推到一个容器环境里跑。**框架不实现任何一种执行方式，也不解析
脚本内容** —— 每个项目各写一份 CI 适配脚本，框架只认「脚本存在 + 退出码
契约」：

* 路径：``<project>/scripts/ci_gate.sh``，必须是可执行文件；
* 退出码：``0`` 绿 / ``1`` 红 / ``3`` 判不了（沿用既有 ``full_ci`` 契约）。

导入通道（``POST /api/plan/import``）与启动通道
（``POST /api/execution/{id}/start``）问的是同一个问题：「这个项目提供了
可执行的 CI 适配脚本吗？」。所以判定规则必须**只有一份定义** —— 就在这个
模块里，两处共用。

两份各写一遍的规则一定会漂移：一边把「存在但没有执行位」也算缺，另一边
还只查「文件在不在」，于是同一个项目在导入时通过、在启动时被拒（或者
反过来）。漂移出来的失败发生在最不该失败的地方，而且只有一条链路能给出
解释 —— 另一条只会说「启动失败」。规则在这里写死一次，两条链路引用同一个
函数，漂移就没有落脚点。

判定结果怎么用
--------------
:func:`missing_ci_adapter` 返回 ``None`` 表示「适配脚本齐备，没有缺口」，
否则返回一段**原因文本**，形如::

    ci_adapter_missing: 项目 <path> 缺少可执行的 scripts/ci_gate.sh（退出码 0 绿 / 1 红 / 3 判不了）

原因文本以 ``ci_adapter_missing`` 开头，调用方（导入的 422 响应体、启动的
400 响应体）按这个前缀识别它，两条链路的错误文案因此也是同一份。

**两种缺失共用同一段文案**，这是刻意的：「文件不存在」和「文件存在但没有
执行位」对调用方是同一件事 —— 这个项目没有可执行的 CI 适配脚本。分开写
两段文案只多给调用方一个分支，却多出一处将来会漂移的字符串。

为什么这个判定自己不抛异常
--------------------------
导入通道拿到的 ``project_dir`` 可能指向一个不存在的目录（用户手输路径、
工作区被删），启动通道也可能读到一份陈旧的计划。判定是**门禁**，门禁自己
崩掉会把一条可读的原因（「这个项目缺适配脚本」）变成一条栈回溯 —— 后者对
操作员没有任何意义。所以 :func:`missing_ci_adapter` 对任何输入都返回结果：
要么是原因文本，要么是 ``None``。

本模块只依赖标准库 —— 导入通道与启动通道都会 import 它，它不该拖慢任何
一条链路。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

__all__ = [
    "CI_ADAPTER_RELATIVE_PATH",
    "ci_adapter_path",
    "missing_ci_adapter",
]

#: 项目提供的 CI 适配脚本路径（相对项目根）。
CI_ADAPTER_RELATIVE_PATH = "scripts/ci_gate.sh"

#: 适配脚本的退出码契约。框架不解析脚本内容，这三个数字是它与项目之间
#: 唯一的沟通方式。
_EXIT_CODE_CONTRACT = "退出码 0 绿 / 1 红 / 3 判不了"


def ci_adapter_path(project_dir: Union[str, Path]) -> Path:
    """返回 ``project_dir`` 下 CI 适配脚本的路径。

    路径由 :data:`CI_ADAPTER_RELATIVE_PATH` 决定，导入与启动两条链路都
    从这里取，不各写一份字符串。
    """
    return Path(project_dir) / CI_ADAPTER_RELATIVE_PATH


def missing_ci_adapter(project_dir: Union[str, Path, None]) -> Optional[str]:
    """项目没有可执行的 CI 适配脚本时返回原因文本，有则返回 ``None``。

    判定条件（两条都要满足才算「有」）：文件存在、且有执行位。
    只有文件没有执行位同样算缺 —— 执行期拿一个 0644 的脚本去跑，得到的
    是「Permission denied」，而不是一条能指向原因的失败。

    对任何输入都不抛异常：``project_dir`` 指向不存在的目录、空目录、
    ``None``，都返回原因文本。门禁自己崩掉会把可读的原因变成栈回溯。
    """
    if _is_blank(project_dir):
        return _missing_reason(project_dir)

    adapter = ci_adapter_path(project_dir)
    if adapter.is_file() and os.access(adapter, os.X_OK):
        return None
    return _missing_reason(project_dir)


def _is_blank(project_dir) -> bool:
    """空项目目录一律按「缺适配脚本」处理（fail-close）。

    空的 ``project_dir`` 不指向任何项目，但 ``Path("")`` 会被解析成当前
    工作目录 —— 于是一个空路径有机会"找到"当前目录下的适配脚本而蒙混过关。
    这里在解析之前就把空值拦下来。
    """
    if project_dir is None:
        return True
    if isinstance(project_dir, str):
        return not project_dir.strip()
    return False


def _missing_reason(project_dir) -> str:
    """构造缺适配脚本的原因文本。"""
    return (
        f"ci_adapter_missing: 项目 {_display(project_dir)} 缺少可执行的 "
        f"{CI_ADAPTER_RELATIVE_PATH}（{_EXIT_CODE_CONTRACT}）"
    )


def _display(project_dir) -> str:
    """原因文本里展示的项目路径（规范化，去掉多余的尾部分隔符）。"""
    try:
        return str(Path(project_dir))
    except TypeError:
        return str(project_dir)