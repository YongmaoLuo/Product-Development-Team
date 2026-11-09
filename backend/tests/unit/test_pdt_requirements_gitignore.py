"""``.gitignore`` 对 PDT 运行期文件的忽略契约。

被测对象
--------
仓库根 ``.gitignore`` 末尾的 PDT 规则块（``PDT.md`` 与 ``PDT.*.md``）。

为什么需要这个门禁
------------------
``PDT.md`` 家族是**部署级**的「操作者要求」：它写满本机部署策略（provider
选择、额度、目录约定）。这些内容对这台机器是对的，对公开仓是错的 ——
运行期文件必须被 ``.gitignore`` 挡住，一次 ``git add -A`` 也不能把它们带进
公开仓。

同时它**不能**误伤两样东西：

* ``example/`` 下的四份 ``.example`` 样例 —— 新部署者唯一的起点，必须随仓
  发行；
* ``docs/development/pdt-requirements.md`` —— 机制文档，公开仓需要它。

怎么验
------
``git check-ignore``：退出码 0 表示命中忽略规则，1 表示没命中。它只对规则
求值，不要求被检查的路径真的存在，所以门禁在「还没部署任何要求」的机器上
同样成立。

``--no-index`` 的用途
---------------------
默认情况下 ``git check-ignore`` 会**查索引**：已被跟踪的文件不再受忽略规则
约束，于是对一个已入库文件它一律返回 1。用它来断言「规则没有匹配」是空的
——样例是被跟踪的，无论规则写没写都返回 1。所以「不该被忽略」的两条断言
额外用 ``--no-index`` 走一遍纯规则路径：这条路径上返回 1 才真的说明规则没
匹配上。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: 仓库根：``backend/tests/unit/x.py`` → ``parents[3]``。
ROOT = Path(__file__).resolve().parents[3]

#: 运行期文件名（相对仓库根）。base 一份，每个 stage 一份。
RUNTIME_FILENAMES: tuple[str, ...] = (
    "PDT.md",
    "PDT.plan.md",
    "PDT.execution.md",
    "PDT.verification.md",
)

#: 样例文件名（相对仓库根）。去掉 ``.example`` 即上表的运行期文件名。
EXAMPLE_FILENAMES: tuple[str, ...] = (
    "example/PDT.md.example",
    "example/PDT.plan.md.example",
    "example/PDT.execution.md.example",
    "example/PDT.verification.md.example",
)

#: 机制文档路径（相对仓库根）。
DOC_PATH = "docs/development/pdt-requirements.md"


def _check_ignore(path: str, *, no_index: bool = False) -> subprocess.CompletedProcess[str]:
    """在仓库根跑一次 ``git check-ignore -q``。

    ``cwd`` 固定为仓库根：测试以 ``cd backend`` 运行，裸相对路径会读错地方，
    所以路径一律是相对仓库根的写法，由 ``cwd`` 补全。
    """
    args = ["git", "check-ignore", "-q"]
    if no_index:
        args.append("--no-index")
    args.append(path)
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)


def test_runtime_files_ignored() -> None:
    """四份运行期 PDT 要求文件全部被忽略规则命中。

    命中 = ``git check-ignore`` 退出码 0。它们各自写着本机部署策略，被
    ``git add -A`` 带进公开仓就是泄漏。
    """
    for name in RUNTIME_FILENAMES:
        result = _check_ignore(name)
        assert result.returncode == 0, (
            f"{name} 没有被 .gitignore 的 PDT 规则命中；"
            f"一次 `git add -A` 就会把本机部署策略提交进公开仓。"
            f" exit={result.returncode} stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )


def test_examples_not_ignored() -> None:
    """``example/`` 下四份样例都不被忽略规则命中。

    样例是新部署者唯一的起点，被忽略就等于没随仓发行 —— 一台新机器上没有
    ``PDT.md``，只有这四份模板。

    两条断言都要有：默认路径查索引，已跟踪文件一律返回 1（空断言）；加上
    ``--no-index`` 的纯规则路径，返回 1 才真的说明规则没有匹配上。
    """
    for name in EXAMPLE_FILENAMES:
        result = _check_ignore(name)
        assert result.returncode == 1, (
            f"{name} 被忽略规则命中了；样例必须随仓发行，"
            f"否则新部署者拿不到起点模板。 exit={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        pure = _check_ignore(name, no_index=True)
        assert pure.returncode == 1, (
            f"{name} 被忽略规则命中了（--no-index 纯规则路径）；"
            f"样例必须随仓发行，否则新部署者拿不到起点模板。"
            f" exit={pure.returncode} stdout={pure.stdout!r} "
            f"stderr={pure.stderr!r}"
        )


def test_doc_not_ignored() -> None:
    """机制文档 ``docs/development/pdt-requirements.md`` 不被忽略规则命中。

    文档描述的是产品行为，公开仓需要它；被忽略就等于只在本机存在。
    """
    result = _check_ignore(DOC_PATH)
    assert result.returncode == 1, (
        f"{DOC_PATH} 被忽略规则命中了；机制文档属于公开仓内容，"
        f"被忽略就等于只在本机存在。 exit={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    pure = _check_ignore(DOC_PATH, no_index=True)
    assert pure.returncode == 1, (
        f"{DOC_PATH} 被忽略规则命中了（--no-index 纯规则路径）；"
        f"机制文档属于公开仓内容，被忽略就等于只在本机存在。"
        f" exit={pure.returncode} stdout={pure.stdout!r} stderr={pure.stderr!r}"
    )