"""TDD spec — backend/framework_fix/fingerprint.py 框架缺陷机械指纹。

这个模块要解决什么
------------------
PRD 要求「同一失败签名在全系统只产生一个修复 plan」。这个签名必须**跨 plan
稳定**：同一个框架缺陷在不同的 plan 里表现为不同的失败用例、不同的 VP、
不同的轮次、不同的计划号 —— 但根因是同一个。那些量都是 plan 局部量，
进了键，同一个缺陷就会按 plan 裂成 N 个签名，全局去重随即失效。

键由三个跨 plan 稳定的字段组成（架构决策点 2）::

    framework_files     失败所在 / 修复将要触碰的框架文件（repo 相对路径，排序后）
    error_class         异常类型或错误类别
    first_error_line    归一化后的首错行

显式排除：仓库名、node id、VP id、阶段、轮次、plan id。

指纹必须是纯函数
----------------
不读文件、不发请求、不看时钟、不依赖 ``hash()`` 的随机种子。同一份材料在
任何进程、任何机器上给出同一个指纹 —— 否则「这是不是同一个缺陷」这个问题
本身就不可复算。

TDD contract — 6 cases
-----------------------
1. ``test_same_key_same_fp`` —— 同一份材料 -> 同一个指纹；不同缺陷 -> 不同指纹。
2. ``test_volatile_fields_stripped`` —— 临时路径 / node id / VP id / 阶段 /
   轮次 / 计划号 / 时间戳变化 -> 指纹不变。
3. ``test_files_order_insensitive`` —— ``framework_files`` 顺序颠倒 -> 指纹不变。
4. ``test_empty_input_raises`` —— 空 ``framework_files`` / 空 ``error_class``
   -> ``ValueError``。
5. ``test_alias_chain`` —— 别名链 ``a -> b -> c`` -> 解析为 ``c``；成环 ->
   ``ValueError``。
6. ``test_namespace_package_import`` —— 包内没有 ``__init__.py``，
   ``import backend.framework_fix.fingerprint`` 仍然可用（PEP 420）。

边界条件（同一文件覆盖）：

* ``framework_files`` 是集合语义：排序 + 去重。
* ``error_class`` 按标识符口径归一（折叠空白 + 小写），与仓库既有的
  ``verification.failure_fingerprint._normalize_identifier`` 同一口径。
* 指纹形如 ``sha256:<64 位小写 hex>``。
* 归一化口径复用 ``verification.failure_fingerprint`` —— 掩码不另造第二套。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.framework_fix.fingerprint import (  # noqa: E402
    canonical_key,
    compute_fingerprint,
    normalize_line,
    resolve_alias,
)


#: 一份最小材料：一次「断言失败，发生在某个 CI 脚本」。
_BASE = {
    "framework_files": ["scripts/ci_x.sh"],
    "error_class": "AssertionError",
    "first_error_line": "assert 1 == 2",
}

#: 在一个只认识仓库根的全新解释器里复算同一个指纹。证明这个模块不依赖
#: 调用方的 ``sys.path`` 布局，也证明指纹跨进程可复算。
_SUBPROCESS_PROBE = (
    "import backend.framework_fix.fingerprint as m;"
    "print(m.compute_fingerprint("
    "framework_files=['scripts/ci_x.sh'],"
    "error_class='AssertionError',"
    "first_error_line='assert 1 == 2'))"
)


# ---------------------------------------------------------------------------
# 1. 同一份材料 -> 同一个指纹
# ---------------------------------------------------------------------------
def test_same_key_same_fp():
    """同一份材料给出同一个指纹；不同的缺陷不能共用一个指纹。"""
    first = compute_fingerprint(**_BASE)
    second = compute_fingerprint(**_BASE)

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64
    assert first[len("sha256:"):] == first[len("sha256:"):].lower()

    different_line = compute_fingerprint(
        **{**_BASE, "first_error_line": "assert 1 == 3"}
    )
    different_class = compute_fingerprint(**{**_BASE, "error_class": "ValueError"})
    different_files = compute_fingerprint(
        **{**_BASE, "framework_files": ["scripts/ci_y.sh"]}
    )

    assert different_line != first
    assert different_class != first
    assert different_files != first


# ---------------------------------------------------------------------------
# 2. plan 局部量不进指纹
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "left,right",
    [
        # 临时路径：pytest 每次给用例一个新的临时根。
        (
            "FAILED /tmp/pytest-of-root/pytest-12/test_x.py - AssertionError",
            "FAILED /tmp/pytest-of-root/pytest-987/test_x.py - AssertionError",
        ),
        # node id：同一个缺陷落在不同用例上。
        (
            "FAILED backend/tests/unit/test_x.py::test_alpha - AssertionError",
            "FAILED backend/tests/unit/test_x.py::test_beta - AssertionError",
        ),
        # VP id：plan 局部量。
        ("VP-3 failed", "VP-17 failed"),
        # 轮次：同一签名的第几轮不是身份的一部分。
        ("round=1 failed", "round=4 failed"),
        # 阶段：同一个缺陷落在不同阶段上。六种写法都要归一成同一行。
        ("phase=verification failed", "phase=execution failed"),
        ("phase: verification failed", "phase: execution failed"),
        ("阶段=verification failed", "阶段=execution failed"),
        ("[verification] failed", "[execution] failed"),
        ("verification phase failed", "execution phase failed"),
        (
            "=== phase 2/3 (e2e + integration) ===",
            "=== phase 1/3 (parallel) ===",
        ),
        # 计划号：本仓的计划目录形如 20260101-sample-plan。
        (
            "plans/20260101-sample-plan/logs/run.log failed",
            "plans/20261201-other-lane/logs/run.log failed",
        ),
        # 时间戳：这一轮跑的时间，不是缺陷的身份。
        (
            "2026-11-23T01:46:45Z failed",
            "2026-11-24T09:00:00Z failed",
        ),
    ],
)
def test_volatile_fields_stripped(left, right):
    """临时路径 / node id / VP id / 轮次 / 计划号 / 时间戳变化 -> 指纹不变。"""
    left_fp = compute_fingerprint(**{**_BASE, "first_error_line": left})
    right_fp = compute_fingerprint(**{**_BASE, "first_error_line": right})

    assert left_fp == right_fp


def test_volatile_fields_stripped_in_normalized_line():
    """归一化后的首错行里不再留着那些 plan 局部量。"""
    normalized = normalize_line(
        "FAILED /tmp/pytest-of-root/pytest-12/test_x.py::test_alpha - "
        "VP-3 round=1 plans/20260101-sample-plan failed"
    )

    for volatile in (
        "/tmp",
        "pytest-12",
        "test_alpha",
        "VP-3",
        "20261123",
    ):
        assert volatile not in normalized


#: 六种阶段写法，同一对之间只差阶段名。
_PHASE_PAIRS = (
    ("phase=verification failed", "phase=execution failed"),
    ("phase: verification failed", "phase: execution failed"),
    ("阶段=verification failed", "阶段=execution failed"),
    ("[verification] failed", "[execution] failed"),
    ("verification phase failed", "execution phase failed"),
    (
        "=== phase 2/3 (e2e + integration) ===",
        "=== phase 1/3 (parallel) ===",
    ),
)


@pytest.mark.parametrize("left,right", _PHASE_PAIRS)
def test_phase_is_excluded_from_normalized_line(left, right):
    """阶段是 plan 局部量：掩完后的两行逐字相同，阶段名不残留。"""
    left_normalized = normalize_line(left)
    right_normalized = normalize_line(right)

    assert left_normalized == right_normalized, (
        f"the phase leaked into the normalized line: {left!r} -> "
        f"{left_normalized!r} vs {right!r} -> {right_normalized!r}"
    )
    for phase_name in ("verification", "execution", "阶段"):
        assert phase_name not in left_normalized


def test_error_class_reuses_shared_identifier_primitive(monkeypatch):
    """错误类别归一复用 verification.failure_fingerprint._normalize_identifier。"""
    from backend.framework_fix import fingerprint as fingerprint_module

    calls = []

    def spy(value):
        calls.append(value)
        return "spied"

    monkeypatch.setattr(fingerprint_module, "_normalize_identifier", spy)

    assert fingerprint_module._normalize_error_class("AssertionError") == "spied"
    assert calls == ["AssertionError"]


def test_error_class_none_normalizes_to_empty_string():
    """``error_class=None`` 归一成空串 —— 不是共享原语的 ``'<missing>'``。

    ``canonical_key`` 靠空串抛 ``ValueError``：错误类别缺失是「没有材料」，
    不是「类别叫 missing」。
    """
    from backend.framework_fix import fingerprint as fingerprint_module

    assert fingerprint_module._normalize_error_class(None) == ""


# ---------------------------------------------------------------------------
# 3. framework_files 是集合
# ---------------------------------------------------------------------------
def test_files_order_insensitive():
    """文件顺序颠倒 -> 同一个指纹。"""
    left = compute_fingerprint(
        **{
            **_BASE,
            "framework_files": ["scripts/ci_x.sh", "backend/framework_fix/x.py"],
        }
    )
    right = compute_fingerprint(
        **{
            **_BASE,
            "framework_files": ["backend/framework_fix/x.py", "scripts/ci_x.sh"],
        }
    )

    assert left == right
    assert canonical_key(
        framework_files=["b.py", "a.py"], error_class="E", first_error_line="x"
    )["framework_files"] == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# 4. 空输入
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"framework_files": [], "error_class": "AssertionError"},
        {"framework_files": ["   "], "error_class": "AssertionError"},
        {"framework_files": ["scripts/ci_x.sh"], "error_class": ""},
        {"framework_files": ["scripts/ci_x.sh"], "error_class": "   "},
        {"framework_files": ["scripts/ci_x.sh"], "error_class": None},
    ],
)
def test_empty_input_raises(kwargs):
    """空的 framework_files 或空的 error_class -> ValueError。"""
    with pytest.raises(ValueError):
        compute_fingerprint(first_error_line="assert 1 == 2", **kwargs)


# ---------------------------------------------------------------------------
# 5. 别名解析
# ---------------------------------------------------------------------------
def test_alias_chain():
    """别名链 a -> b -> c 解析到 c；没有别名的指纹原样返回。"""
    table = {"aaa": "bbb", "bbb": "ccc"}

    assert resolve_alias("aaa", table.get) == "ccc"
    assert resolve_alias("bbb", table.get) == "ccc"
    assert resolve_alias("ccc", table.get) == "ccc"
    assert resolve_alias("zzz", table.get) == "zzz"


def test_alias_cycle_raises():
    """别名成环 -> ValueError（自环同样是环）。"""
    with pytest.raises(ValueError):
        resolve_alias("aaa", {"aaa": "bbb", "bbb": "aaa"}.get)
    with pytest.raises(ValueError):
        resolve_alias("aaa", lambda fingerprint: fingerprint)


# ---------------------------------------------------------------------------
# 6. PEP 420 隐式命名空间包
# ---------------------------------------------------------------------------
def test_namespace_package_import():
    """包内没有 __init__.py，子模块仍能以 backend.framework_fix.<module> 导入。"""
    package_dir = BACKEND_DIR / "framework_fix"
    assert package_dir.is_dir(), "backend/framework_fix/ must exist"
    assert not (package_dir / "__init__.py").exists(), (
        "backend/framework_fix/ must stay a PEP 420 namespace package: "
        "it must not carry an __init__.py"
    )

    import backend.framework_fix
    import backend.framework_fix.fingerprint as fingerprint_module

    # 命名空间包没有自己的 __file__，它的 __path__ 是一段路径列表。
    assert getattr(backend.framework_fix, "__file__", None) is None
    assert list(backend.framework_fix.__path__)
    assert fingerprint_module.compute_fingerprint is compute_fingerprint

    # 一个只认识仓库根的全新解释器里也能导入，并复算出同一个指纹。
    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_PROBE],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == compute_fingerprint(**_BASE)
