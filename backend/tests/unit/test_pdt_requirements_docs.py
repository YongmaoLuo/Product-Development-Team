"""机制文档的契约测试：文档里不可省略的内容不许悄悄消失。

被测对象
--------
``docs/development/pdt-requirements.md`` —— 部署级「操作者要求」机制
（``PDT.md`` 家族）的公开说明。

为什么文档也需要测试
--------------------
文档没有测试就只能靠人读：改代码的人不会顺手改文档，久了文档就成了
"看起来对、其实不是"的说明书 —— 而这份文档是**公开**的，后续维护者只能
靠它理解机制。这里把机制里**不可省略**的几件事钉住：

* 三层文件名：``PDT.md`` / ``PDT.<stage>.md`` / ``PDT.<scene>.md``；
* 8 KB 上限（8192）、红线不可覆盖、缓存按 mtime 判失效、没有要求文件时
  是 no-op；
* 样例在 ``example/``，用法是复制到仓库根并去掉 ``.example`` 后缀；
* 文档本身不含本机路径 —— 公开仓里的文档不能带任何人的机器信息。

另有一条对照测试：文档写下的上限与红线必须与**实现**一致（文档说 8192，
代码就得是 8192），否则文档会变成一份"看起来对"的二手转述。

路径一律由 ``__file__`` 反推仓库根：测试以 ``cd backend`` 运行，CWD 是
``backend/``，裸相对路径会读错地方。
"""

from __future__ import annotations

import re
from pathlib import Path

import pdt_requirements

#: 仓库根：``backend/tests/unit/x.py`` → ``parents[3]``。
ROOT = Path(__file__).resolve().parents[3]

#: 机制文档。
DOC_PATH = ROOT / "docs" / "development" / "pdt-requirements.md"

#: 本机路径形状（macOS / Linux / Windows 盘符形式都覆盖）。
LOCAL_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")


def read_doc() -> str:
    """读机制文档；不存在时给出可定位的失败信息。"""
    assert DOC_PATH.is_file(), f"机制文档不存在：{DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def test_doc_exists_and_names_three_layers() -> None:
    """文档写明三层的文件名，且文件名与实现里的推导规则一致。"""
    doc = read_doc()

    assert "PDT.md" in doc
    assert "PDT.<stage>.md" in doc
    assert "PDT.<scene>.md" in doc
    # base 层文件名不是写死的散文，而是实现里的常量。
    assert pdt_requirements.BASE_FILENAME in doc


def test_doc_states_limits_and_redlines() -> None:
    """文档写明 8 KB 上限、红线不可覆盖、缓存按 mtime、无文件时 no-op。"""
    doc = read_doc()

    assert "8192" in doc
    assert "红线" in doc
    assert "mtime" in doc
    assert "no-op" in doc
    # 上限的数值必须与实现一致，而不是文档自说自话。
    assert str(pdt_requirements.MAX_FILE_BYTES) in doc
    # 红线是代码写死的：文档里出现的红线措辞必须是代码里那一条的一部分。
    assert "不得伪造或篡改证据" in doc
    assert "不得伪造或篡改证据" in pdt_requirements.REDLINE


def test_doc_describes_example_location() -> None:
    """文档写明样例在 ``example/``，以及复制到仓库根、去掉 ``.example`` 后缀的用法。"""
    doc = read_doc()

    assert "example/" in doc
    assert "去掉 `.example` 后缀" in doc
    # 复制到仓库根这一步也必须写出来：只在 example/ 里放着是不生效的。
    assert "仓库根" in doc


def test_doc_has_no_local_paths() -> None:
    """公开文档里零本机路径。"""
    doc = read_doc()

    hits = LOCAL_PATH_RE.findall(doc)
    assert not hits, f"文档含本机路径：{hits}"
