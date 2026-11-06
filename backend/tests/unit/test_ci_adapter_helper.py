"""CI 适配脚本的统一判定（2026-11-05）。

契约来源：需求文档的「约定」节 —— CI 验收的形态是
多样的，**框架不实现任何一种执行方式，也不解析脚本内容**；每个项目各写
一份 CI 适配脚本，框架只认「脚本存在 + 退出码契约」：

* 路径：``<project>/scripts/ci_gate.sh``，必须是可执行文件；
* 退出码：``0`` 绿 / ``1`` 红 / ``3`` 判不了（沿用既有 ``full_ci`` 契约）。

导入通道（``POST /api/plan/import``）与启动通道
（``POST /api/execution/{id}/start``）问的是同一个问题：「这个项目提供了
可执行的 CI 适配脚本吗？」。所以判定规则必须**只有一份定义** —— 落在
:mod:`ci_adapter` 里，两处共用。

两份各写一遍的规则一定会漂移：一边把「存在但没有执行位」也算缺，另一边
还只查「文件在不在」，于是同一个项目在导入时通过、在启动时被拒（或者
反过来）。失败发生在最不该失败的地方，而且只有一条链路能给出解释。

本文件钉住四件事：

* 文件不存在 → 返回原因文本；
* 文件存在但没有执行位 → 同样返回原因文本；
* 文件存在且可执行 → 返回 None（没有缺口）；
* ``project_dir`` 本身不存在 → 返回原因文本，**不抛异常**（导入通道拿到 一个不存在的项目目录是常态，判定本身崩掉会把「缺适配脚本」这条可读的
  原因变成一条栈回溯）。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from ci_adapter import (  # noqa: E402
    CI_ADAPTER_RELATIVE_PATH,
    ci_adapter_path,
    missing_ci_adapter,
)


def _install_adapter(project_dir: Path, mode: int = 0o755) -> Path:
    """在 ``project_dir`` 下装一份 CI 适配脚本，并显式设置权限位。"""
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    adapter = scripts_dir / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(mode)
    return adapter


def test_missing_when_file_absent(tmp_path):
    """项目里没有适配脚本 → 返回原因文本，且文本点名缺了什么。"""
    reason = missing_ci_adapter(tmp_path)

    assert reason is not None
    assert "ci_adapter_missing" in reason
    assert CI_ADAPTER_RELATIVE_PATH in reason


def test_missing_when_not_executable(tmp_path):
    """适配脚本存在但没有执行位 → 同样拒绝（存在 ≠ 可执行）。"""
    adapter = _install_adapter(tmp_path, mode=0o644)
    assert not os.access(adapter, os.X_OK)

    reason = missing_ci_adapter(tmp_path)

    assert reason is not None
    assert "ci_adapter_missing" in reason


def test_none_when_present_and_executable(tmp_path):
    """适配脚本存在且可执行 → 没有缺口（返回 None）。"""
    adapter = _install_adapter(tmp_path, mode=0o755)
    assert os.access(adapter, os.X_OK)

    assert missing_ci_adapter(tmp_path) is None


def test_missing_when_project_dir_absent(tmp_path):
    """项目目录本身不存在 → 返回原因文本，不抛异常。"""
    absent = tmp_path / "no-such-project"

    reason = missing_ci_adapter(absent)

    assert reason is not None
    assert "ci_adapter_missing" in reason


def test_reason_text_has_the_agreed_format(tmp_path):
    """原因文本的格式是约定好的：调用方按 ``ci_adapter_missing`` 前缀识别它。"""
    reason = missing_ci_adapter(tmp_path)

    assert reason == (
        f"ci_adapter_missing: 项目 {tmp_path} 缺少可执行的 "
        f"{CI_ADAPTER_RELATIVE_PATH}（退出码 0 绿 / 1 红 / 3 判不了）"
    )


def test_ci_adapter_path_points_at_the_agreed_script(tmp_path):
    """适配脚本的路径由常量决定，两条链路都从这里取，不各写一份字符串。"""
    assert CI_ADAPTER_RELATIVE_PATH == "scripts/ci_gate.sh"
    assert ci_adapter_path(tmp_path) == tmp_path / "scripts" / "ci_gate.sh"


def test_accepts_string_project_dir(tmp_path):
    """``project_dir`` 传字符串与传 Path 判定结果一致。"""
    _install_adapter(tmp_path, mode=0o755)

    assert missing_ci_adapter(str(tmp_path)) is None

    other = tmp_path / "other"
    other.mkdir()
    assert missing_ci_adapter(str(other)) is not None


def test_directory_in_place_of_script_is_missing(tmp_path):
    """同名目录不是适配脚本 → 判定为缺（否则执行期才炸）。"""
    (tmp_path / "scripts" / "ci_gate.sh").mkdir(parents=True)

    reason = missing_ci_adapter(tmp_path)

    assert reason is not None
    assert "ci_adapter_missing" in reason


def test_executable_bit_is_what_matters_not_the_mode_number(tmp_path):
    """执行位存在即可：0700 / 0750 / 0755 都被接受，0644 / 0600 被拒绝。"""
    for mode in (0o700, 0o750, 0o755):
        project = tmp_path / f"p{mode:o}"
        project.mkdir()
        _install_adapter(project, mode=mode)
        assert stat.S_IMODE(os.stat(project / "scripts" / "ci_gate.sh").st_mode) == mode
        assert missing_ci_adapter(project) is None, mode

    for mode in (0o600, 0o644):
        project = tmp_path / f"p{mode:o}"
        project.mkdir()
        _install_adapter(project, mode=mode)
        assert missing_ci_adapter(project) is not None, mode


@pytest.mark.parametrize("empty", ["", None])
def test_empty_project_dir_is_missing(tmp_path, empty):
    """空项目目录不许被当成「当前工作目录」而蒙混过关（fail-close）。"""
    reason = missing_ci_adapter(empty)

    assert reason is not None
    assert "ci_adapter_missing" in reason