"""TDD spec — backend/verification/failure_fingerprint.py 失败指纹纯函数。

Background
----------
A2 要回答的是「同一 VP 相邻两次失败,根因有没有演进」。现有的
``same_failure_counts`` 只按 ``VP-x|分类`` 计数,回答不了这个问题:两次失败
分类相同(都是 ``deliverable_defect``),但一次断在 JSON 路径、一次断在状态码 ——
计数说「同一种失败又发生了一次」,根因其实已经换了。

失败指纹就是那个能分辨根因的键:同一 VP 的两次失败,指纹相同 => 根因没变;
指纹不同 => 根因变了。

指纹必须是纯函数
----------------
不读文件、不发请求、不看时钟、不依赖 ``hash()`` 的随机种子。同样的输入永远
给同样的输出 —— 否则「根因是否演进」这个判断本身就不可复算,跨进程比对更是
无从谈起。

指纹对什么敏感,对什么不敏感
----------------------------
敏感(必须能分辨):``vp_id`` / ``failure_class`` / ``failed_assertions`` /
``command`` / ``first_error_line``。

不敏感(噪声,不能进指纹):

* **tmp 路径** —— pytest 每次给每个用例一个新的临时根,同一个失败在两次运行
  里路径都不一样。
* **十六进制地址** —— 内存地址每次都变。
* **大小写** —— 同一件事换个写法。
* **空白** —— 换行、缩进、多余空格。
* **断言顺序** —— 断言是一个集合,不是序列。

归一化复用 failure_classification
----------------------------------
掩码**不另造第二套**:``backend/failure_classification.py`` 已经在做同一件事
(``_normalize_path_token`` 归一路径, ``_mask_hex`` 把十六进制摘要掩成
``<hex>``),这里直接复用它的那几个原语,两处就不会漂。

TDD contract — 4 cases
-----------------------
1. ``test_golden_snapshot`` —— 固定输入 -> 固定 16 位 hex,与 golden 文件一致
   (在 ``test_failure_fingerprint_golden.py`` 里)。
2. ``test_noise_invariance`` —— tmp 路径 / 十六进制地址 / 大小写 / 空白变化
   -> 指纹相同。
3. ``test_assertion_order_invariance`` —— 断言顺序打乱 -> 指纹不变。
4. ``test_cross_process_reproducible`` —— 子进程换 ``PYTHONHASHSEED`` 复算一致。

边界条件(同一文件覆盖):

* 缺字段用固定哨兵 ``_MISSING``,归一化输入里**不能**出现 ``None`` 字面量。
* 指纹必须是 16 位小写 hex。
* 不同的失败必须给不同的指纹 —— 否则「根因已演进」永远判不出来。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.failure_fingerprint import (  # noqa: E402
    _MISSING,
    canonical_fingerprint_input,
    failure_fingerprint,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmp_path(*parts: str) -> str:
    """A path under this machine's temporary root."""
    return os.path.join(tempfile.gettempdir(), *parts)


def _fingerprint_in_subprocess(payload: dict, hash_seed: str) -> str:
    """Recompute the fingerprint in a fresh interpreter.

    The child gets its own ``PYTHONHASHSEED``: if the fingerprint ever
    depended on ``hash()`` (a set, a dict iteration order), the two seeds
    would disagree and this test would catch it.
    """
    code = (
        "import json\n"
        "from verification.failure_fingerprint import failure_fingerprint\n"
        "print(failure_fingerprint(**json.loads({payload!r})))\n"
    ).format(payload=json.dumps(payload))
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["PYTHONPATH"] = os.pathsep.join(
        [str(BACKEND_DIR), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"the child interpreter failed:\n{completed.stdout}\n{completed.stderr}"
    )
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# 2. noise invariance
# ---------------------------------------------------------------------------
def test_noise_invariance():
    """tmp 路径 / 十六进制地址 / 大小写 / 空白 —— 四种噪声都不能改指纹。

    这些是「同一个失败在两次运行里长得不一样」的四种典型原因。只要有一种
    漏进去,相邻两次的指纹就会假性不同,A2 会把「同一个 bug 又犯了一次」判成
    「根因已经演进」。
    """
    first_run = _tmp_path("pytest-of-root", "pytest-12", "test_alpha0", "thing.py")
    second_run = _tmp_path("pytest-of-root", "pytest-987", "test_alpha0", "thing.py")

    noisy = failure_fingerprint(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path", "assert status"],
        command=f"pytest {first_run}",
        first_error_line=f"AssertionError at {first_run}:12 object 0x7f3c9a21",
    )
    quiet = failure_fingerprint(
        vp_id="vp-3",
        failure_class="DELIVERABLE_DEFECT",
        failed_assertions=["assert json_path", "assert status"],
        command=f"pytest   {second_run}",
        first_error_line=(
            f"assertionerror   at   {second_run}:12   object   0x1a2b3c4d"
        ),
    )
    assert noisy == quiet, (
        f"the same failure told two ways got two fingerprints: "
        f"{noisy!r} vs {quiet!r}"
    )


def test_a_different_tmp_path_is_the_same_failure():
    """只改临时根这一项,其余完全不动 —— 指纹必须不变。

    这一条单独拆出来,是因为它是「同一失败两次运行」最常见的形态:pytest
    每次换一个临时根,别的什么都没变。
    """
    first = failure_fingerprint(
        vp_id="VP-7",
        failure_class="criterion_defect",
        failed_assertions=["assert status"],
        command="pytest tests/x.py",
        first_error_line=f"ValueError at {_tmp_path('run-1', 'a.py')}",
    )
    second = failure_fingerprint(
        vp_id="VP-7",
        failure_class="criterion_defect",
        failed_assertions=["assert status"],
        command="pytest tests/x.py",
        first_error_line=f"ValueError at {_tmp_path('run-2', 'a.py')}",
    )
    assert first == second, (
        f"one failure under two temporary roots is one failure: "
        f"{first!r} vs {second!r}"
    )


def test_a_hex_address_is_not_part_of_the_identity():
    """内存地址每次运行都变,它不是失败的属性。"""
    first = failure_fingerprint(
        vp_id="VP-7",
        failure_class="criterion_defect",
        failed_assertions=["assert status"],
        command="pytest tests/x.py",
        first_error_line="SegmentationFault at 0xdeadbeefcafe",
    )
    second = failure_fingerprint(
        vp_id="VP-7",
        failure_class="criterion_defect",
        failed_assertions=["assert status"],
        command="pytest tests/x.py",
        first_error_line="SegmentationFault at 0x0123456789ab",
    )
    assert first == second, (
        f"an address is not a failure: {first!r} vs {second!r}"
    )


# ---------------------------------------------------------------------------
# 3. assertion order invariance
# ---------------------------------------------------------------------------
def test_assertion_order_invariance():
    """断言是一个集合,不是序列 —— 顺序换了还是同一个失败。"""
    first = failure_fingerprint(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path", "assert status", "assert schema"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    second = failure_fingerprint(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert schema", "assert json_path", "assert status"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    assert first == second, (
        f"the same assertion set in a different order is one failure: "
        f"{first!r} vs {second!r}"
    )


def test_a_different_assertion_set_is_a_different_failure():
    """顺序不变,但**内容**变了必须是另一个失败 —— 否则「根因演进」判不出来。"""
    first = failure_fingerprint(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path", "assert status"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    second = failure_fingerprint(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path", "assert exit_code"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    assert first != second, (
        "a different assertion set is a different root cause and must not "
        "share a fingerprint"
    )


# ---------------------------------------------------------------------------
# 4. cross-process reproducibility
# ---------------------------------------------------------------------------
def test_cross_process_reproducible():
    """换一个 ``PYTHONHASHSEED`` 的子进程必须复算出同一个指纹。

    ``PYTHONHASHSEED`` 决定 ``hash()`` 的结果,也决定集合/字典的迭代顺序。
    指纹如果不小心经过了任何一个无序结构,两个种子就会给出不同的答案 ——
    而「根因是否演进」是一个要写进状态文件的判断,它必须跨进程可复算。
    """
    payload = {
        "vp_id": "VP-3",
        "failure_class": "deliverable_defect",
        "failed_assertions": ["assert json_path", "assert status", "assert schema"],
        "command": "pytest tests/x.py",
        "first_error_line": "AssertionError at backend/tests/x.py:12",
    }
    seeds = ("0", "1", "12345")
    fingerprints = {
        seed: _fingerprint_in_subprocess(payload, seed) for seed in seeds
    }
    assert len(set(fingerprints.values())) == 1, (
        f"the fingerprint changed with PYTHONHASHSEED: {fingerprints}"
    )
    assert fingerprints["0"] == failure_fingerprint(**payload), (
        "the child interpreter and this one disagree about the same input"
    )


# ---------------------------------------------------------------------------
# boundary conditions
# ---------------------------------------------------------------------------
def test_missing_fields_use_the_sentinel_and_never_the_none_literal():
    """缺字段填固定哨兵,归一化输入里不能出现 ``None`` 字面量。

    ``None`` 字面量会让「字段缺失」和「字段内容恰好是 None」撞成同一个指纹;
    而两者含义完全不同 —— 一个是「不知道」,一个是「知道,内容是 None」。
    """
    canonical = canonical_fingerprint_input(
        vp_id=None,
        failure_class=None,
        failed_assertions=None,
        command=None,
        first_error_line=None,
    )
    assert "None" not in canonical, (
        f"the None literal leaked into the hashed input: {canonical!r}"
    )
    assert canonical.count(_MISSING) == 5, (
        f"every missing field must carry the sentinel: {canonical!r}"
    )


def test_an_omitted_field_carries_the_sentinel_too():
    """「没传」和「传了 None」是同一件事:我们不知道这个字段。"""
    omitted = canonical_fingerprint_input(vp_id="VP-3")
    explicit = canonical_fingerprint_input(
        vp_id="VP-3",
        failure_class=None,
        failed_assertions=None,
        command=None,
        first_error_line=None,
    )
    assert omitted == explicit, (
        f"omitting a field and passing None must read the same: "
        f"{omitted!r} vs {explicit!r}"
    )
    assert omitted.count(_MISSING) == 4, omitted


def test_a_missing_field_is_not_the_same_as_an_empty_one():
    """「不知道」和「知道是空的」必须给不同的指纹。"""
    missing = failure_fingerprint(
        vp_id="VP-3",
        failure_class=None,
        failed_assertions=None,
        command="pytest tests/x.py",
        first_error_line="AssertionError",
    )
    empty = failure_fingerprint(
        vp_id="VP-3",
        failure_class="",
        failed_assertions=[],
        command="pytest tests/x.py",
        first_error_line="AssertionError",
    )
    assert missing != empty, (
        "an absent field and an empty field are different facts and must "
        "not share a fingerprint"
    )


def test_the_fingerprint_is_16_lowercase_hex():
    """形状是契约的一部分:16 位小写 hex。"""
    fingerprint = failure_fingerprint(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    assert len(fingerprint) == 16, fingerprint
    assert all(char in "0123456789abcdef" for char in fingerprint), fingerprint


def test_the_same_input_always_gives_the_same_fingerprint():
    """纯函数:同样的输入永远给同样的输出。"""
    kwargs = dict(
        vp_id="VP-3",
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path", "assert status"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    assert failure_fingerprint(**kwargs) == failure_fingerprint(**kwargs)


def test_different_vps_get_different_fingerprints():
    """指纹里带着 VP:不同 VP 的失败本来就是不同的失败。"""
    kwargs = dict(
        failure_class="deliverable_defect",
        failed_assertions=["assert json_path"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    assert failure_fingerprint(vp_id="VP-3", **kwargs) != failure_fingerprint(
        vp_id="VP-4", **kwargs
    )


def test_different_failure_classes_get_different_fingerprints():
    """分类不同,根因就不同 —— 这是 A2 最主要的一条判据。"""
    kwargs = dict(
        vp_id="VP-3",
        failed_assertions=["assert json_path"],
        command="pytest tests/x.py",
        first_error_line="AssertionError at backend/tests/x.py:12",
    )
    assert failure_fingerprint(
        failure_class="deliverable_defect", **kwargs
    ) != failure_fingerprint(failure_class="criterion_defect", **kwargs)
