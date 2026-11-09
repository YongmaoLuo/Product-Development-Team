"""``example/`` 下四份 PDT 样例的契约测试。

被测对象
--------
``example/PDT.md.example``、``example/PDT.plan.md.example``、
``example/PDT.execution.md.example``、``example/PDT.verification.md.example``
—— 部署级「操作者要求」机制（``PDT.md`` 家族）的**入库样例**。

为什么样例也需要测试
--------------------
样例是新部署者唯一的起点：他手上没有 ``PDT.md``，只有这四份文件。少一份、
名字写错、里面留着上一台机器的路径，这台部署就带着一份错的要求跑下去，而且
往往要等到下一次模型调用、甚至一次失败的验收才看得出来。

这里钉住几件事：

* 四份样例都在、都非空；
* 样例里零本机路径 —— 公开仓里的文件不能带任何人的机器信息；
* 样例名去掉 ``.example`` 之后**正好**等于运行期文件名。这条不是抄一份
  名单，而是从实现（``candidate_files()`` 的推导规则）现算出来的：base 层
  一份，每个 stage 一份，多一张少一张都不行；
* 每份样例不超过 2048 字节 —— 要求文件本身有 8192 字节的上限，但样例是
  要被人读完的起点，不是用来塞满配额的；
* 每份样例写明自己的运行期文件名，操作者复制过去时不必猜。

路径一律由 ``__file__`` 反推仓库根：测试以 ``cd backend`` 运行，CWD 是
``backend/``，裸相对路径会读错地方。
"""

from __future__ import annotations

import re
from pathlib import Path

import pdt_requirements

#: 仓库根：``backend/tests/unit/x.py`` → ``parents[3]``。
ROOT = Path(__file__).resolve().parents[3]

#: 样例文件名（带 ``.example`` 后缀）。顺序即 base → stage。
EXAMPLE_FILENAMES: tuple[str, ...] = (
    "PDT.md.example",
    "PDT.plan.md.example",
    "PDT.execution.md.example",
    "PDT.verification.md.example",
)

#: 去掉 ``.example`` 后缀后的运行期文件名 —— 加载层只认这些名字。
RUNTIME_FILENAMES: tuple[str, ...] = (
    "PDT.md",
    "PDT.plan.md",
    "PDT.execution.md",
    "PDT.verification.md",
)

#: 单份样例的字节上限。
MAX_EXAMPLE_BYTES = 2048

#: 本机路径形状（macOS 的 ``Users`` 形式与 Linux 的 ``home`` 形式都覆盖）。
LOCAL_PATH_RE = re.compile(r"/(?:Users|home)/")


def example_path(name: str) -> Path:
    """返回样例文件的绝对路径。"""
    return ROOT / "example" / name


def read_example(name: str) -> str:
    """读一份样例；不存在时给出可定位的失败信息。"""
    path = example_path(name)
    assert path.is_file(), f"样例不存在：{path}"
    return path.read_text(encoding="utf-8")


def test_four_examples_exist() -> None:
    """四份样例都在，且 strip 后非空。"""
    for name in EXAMPLE_FILENAMES:
        path = example_path(name)
        assert path.is_file(), f"样例不存在：{path}"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"样例内容为空：{path}"


def test_examples_have_no_local_paths() -> None:
    """样例里零本机路径。"""
    for name in EXAMPLE_FILENAMES:
        hits = LOCAL_PATH_RE.findall(read_example(name))
        assert not hits, f"样例含本机路径：{name} → {hits}"


def test_example_names_mirror_runtime_names() -> None:
    """样例名去掉 ``.example`` 后，与运行期文件名一一对应。

    运行期名字不是抄来的：base 层取实现里的常量，stage 层取实现里那张
    scene→stage 表的取值集合 —— 少一张 stage 样例，那一整个阶段就没有可
    复制的起点。
    """
    derived = {name.removesuffix(".example") for name in EXAMPLE_FILENAMES}
    assert derived == set(RUNTIME_FILENAMES), (
        f"样例名去掉 .example 后应等于运行期文件名 {sorted(RUNTIME_FILENAMES)}；"
        f"实得 {sorted(derived)}"
    )

    assert pdt_requirements.BASE_FILENAME == "PDT.md"
    stages = set(pdt_requirements.STAGE_BY_SCENE.values())
    assert stages == {"plan", "execution", "verification"}, (
        f"实现里的 stage 集合变了，样例要跟着变；实得 {sorted(stages)}"
    )


def test_example_names_come_from_candidate_files(monkeypatch) -> None:
    """运行期文件名由 ``candidate_files()`` 现算，而不是本测试自带的名单。

    加载层用哪个名字找文件，样例就得叫哪个名字 —— 两边一旦分叉，样例复制
    到仓库根也不会被加载，而这种分叉只有在真的跑起来之后才看得见。
    """
    monkeypatch.setattr(pdt_requirements, "_deployment_root", lambda: ROOT)

    derived: set[str] = set()
    for scene in pdt_requirements.STAGE_BY_SCENE:
        derived.update(path.name for path in pdt_requirements.candidate_files(scene))

    for runtime_name in RUNTIME_FILENAMES:
        assert runtime_name in derived, (
            f"运行期不会去找 {runtime_name}，对应样例就永远不会被加载；"
            f"candidate_files 推导出的名字是 {sorted(derived)}"
        )


def test_examples_stay_within_size_budget() -> None:
    """每份样例不超过 2048 字节。"""
    for name in EXAMPLE_FILENAMES:
        size = example_path(name).stat().st_size
        assert size <= MAX_EXAMPLE_BYTES, (
            f"样例过大：{name} 是 {size} 字节，上限 {MAX_EXAMPLE_BYTES}"
        )


def test_examples_name_their_runtime_target() -> None:
    """每份样例写明自己复制过去之后的运行期文件名。"""
    for name in EXAMPLE_FILENAMES:
        runtime_name = name.removesuffix(".example")
        assert runtime_name in read_example(name), (
            f"样例 {name} 没有写明复制后的运行期文件名 {runtime_name}"
        )