"""TDD spec — backend/framework_fix/repro.py 复现结果的机械分类（五分类）。

这个模块要解决什么
------------------
PRD 要求「在**干净 main** 上复现失败，才允许把失败判成框架问题」。复现协议把
同一条失败命令在临时 detached worktree 上跑两遍，留下两份 attempt；把这两份
attempt 归进哪一个类别，就是「这是不是框架问题」的唯一判定。

判定必须是纯函数
----------------
不读文件、不发请求、不看时钟、不问模型。同一份材料在任何进程、任何机器上给出
同一个分类 —— 否则「这是不是框架缺陷」这个问题本身就不可复算，跨 plan 的全局
去重也无从谈起（架构决策点 3）。

分类表（架构决策点 3）
----------------------

============================================  =====================
两次 attempt 的结果                             classification
============================================  ======================
2/2 以相同归一化首错行失败                      framework_deterministic
2/2 以同一超时签名超时                          framework_timeout
1/2 失败、1/2 通过                             flake
2/2 通过                                       not_reproducible
其它（输出截断 / 命令不可执行 / 证据不全）      unclassified
============================================  ======================

首错行先归一化再比较
--------------------
两次失败的首错行逐字不同，并不代表是两个缺陷：临时路径、node id、VP id、轮次、
计划号、时间戳都是 plan 局部量或运行噪声。归一化口径复用任务 2 的
``framework_fix.fingerprint.normalize_line`` —— 掩码不另造第二套，两处各写一遍
就会漂。

超时签名同样先归一化再比较
--------------------------
「两次都超时」不是「同一个超时」：一次挂在克隆、一次挂在测试，都是超时，但
不是同一个缺陷。所以超时也要比签名 —— 签名取该 attempt 的首错行（runner 从
``TimeoutExpired`` 的残余输出里取到），归一化口径与首错行**同一套**。

只有**两次都超时**且**超时签名全同**才是 ``framework_timeout``。超时与通过 /
失败混合（1/2 超时）走 ``flake`` —— 一次挂死不足以说明框架里挂着东西。

TDD contract — 5 cases
-----------------------
1. ``test_deterministic`` —— 两次同错 -> ``framework_deterministic``。
2. ``test_both_timeout_same_signature`` —— 两次都超时且签名相同 ->
   ``framework_timeout``。
3. ``test_flake`` —— 一过一败 / 首错行不同 -> ``flake``。
4. ``test_not_reproducible`` —— 两次通过 -> ``not_reproducible``。
5. ``test_empty`` —— 空列表 -> ``unclassified``。

外加四条边界：

* 归一化之后再比较首错行（两次只有 plan 局部量不同 -> 仍是确定性失败）；
* ``test_mixed_timeout_is_flake`` —— 1/2 超时（另一跑通过或失败）-> ``flake``，
  且下游 ``decide`` 判 ``blocking=False`` / ``reproduce=False``（不建修复
  plan，只登记）；
* ``test_two_timeouts_different_signatures`` —— 两次都超时但签名不同 ->
  ``flake``；``test_timeout_plus_unrunnable`` —— 超时 + 没跑起来 ->
  ``unclassified``；
* **永不抛错** —— 任何输入都返回五个词之一，分类不是让调度器崩掉的地方。
"""

from __future__ import annotations

import pytest

from framework_fix.classifier import FailureHit, decide
from framework_fix.repro import (
    CLASSIFICATIONS,
    FLAKE,
    FRAMEWORK_DETERMINISTIC,
    FRAMEWORK_TIMEOUT,
    NOT_REPRODUCIBLE,
    UNCLASSIFIED,
    ReproAttempt,
    ReproductionResult,
    classify,
)


def _hit(
    *,
    acceptance_critical: bool = True,
    out_of_scope_change: bool = False,
) -> FailureHit:
    """一份最小命中材料 —— 用来问下游「这个结论阻不阻塞」。"""
    return FailureHit(
        vp_id='VP-3',
        plan_id='p1',
        error_class='TimeoutError',
        first_error_line='assert 1 == 2',
        framework_files=['scripts/g.sh'],
        acceptance_critical=acceptance_critical,
        out_of_scope_change=out_of_scope_change,
    )


# ---------------------------------------------------------------------------
# 1. 2/2 相同首错行 -> 确定性失败
# ---------------------------------------------------------------------------
def test_deterministic():
    """两次以同一条首错行失败 -> framework_deterministic。

    这是最该被升级成修复 plan 的一类：干净 main 上稳定复现同一个错误。
    """
    attempts = [
        ReproAttempt(1, 1, False, "assert 1 == 2", 3.1),
        ReproAttempt(2, 1, False, "assert 1 == 2", 2.9),
    ]

    assert classify(attempts) == FRAMEWORK_DETERMINISTIC


def test_first_error_line_compared_after_normalization():
    """两次的首错行只有 plan 局部量不同 -> 仍是 framework_deterministic。

    临时路径、pytest 计数目录、node id 都换了名字，但那是同一个缺陷 —— 比较必须
    发生在归一化之后。
    """
    attempts = [
        ReproAttempt(
            1,
            1,
            False,
            "FAILED /tmp/pytest-of-root/pytest-12/test_x.py::test_alpha"
            " - AssertionError",
            1.0,
        ),
        ReproAttempt(
            2,
            1,
            False,
            "FAILED /tmp/pytest-of-root/pytest-987/test_x.py::test_beta"
            " - AssertionError",
            1.0,
        ),
    ]

    assert classify(attempts) == FRAMEWORK_DETERMINISTIC


# ---------------------------------------------------------------------------
# 2. 超时
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param(
            [
                ReproAttempt(1, None, True, "", 30.0),
                ReproAttempt(2, None, True, "", 30.0),
            ],
            id="identical-signature",
        ),
        pytest.param(
            [
                ReproAttempt(
                    1,
                    None,
                    True,
                    "TimeoutError: hung in /tmp/pytest-of-root/pytest-12/test_x.py",
                    30.0,
                ),
                ReproAttempt(
                    2,
                    None,
                    True,
                    "TimeoutError: hung in /tmp/pytest-of-root/pytest-987/test_x.py",
                    30.0,
                ),
            ],
            id="same-signature-after-normalization",
        ),
    ],
)
def test_both_timeout_same_signature(attempts):
    """两次都超时、且超时签名相同 -> framework_timeout。

    PRD 的口径是「2/2 次同一超时按超时签名归为框架问题」—— 两次都挂死**且**
    挂死的是同一个地方，才是框架里挂着东西。

    第二个参数化用例是「同一签名」的第二种形状：两次的首错行逐字不同（临时
    路径换了名字），归一化之后是同一条 —— 与失败分支同一套口径。
    """
    assert classify(attempts) == FRAMEWORK_TIMEOUT


def test_two_timeouts_different_signatures():
    """两次都超时、但超时签名不同 -> flake。

    「都超时」不等于「同一个超时」：一次挂在克隆、一次挂在测试，是两个不同
    的挂法，不能拿其中任何一个去建修复 plan。
    """
    attempts = [
        ReproAttempt(1, None, True, "TimeoutError: hung in git clone", 30.0),
        ReproAttempt(2, None, True, "TimeoutError: hung in pytest", 30.0),
    ]

    assert classify(attempts) == FLAKE


# ---------------------------------------------------------------------------
# 3. flake
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param(
            [
                ReproAttempt(1, 1, False, "assert 1 == 2", 1.0),
                ReproAttempt(2, 0, False, "", 1.0),
            ],
            id="one-passed-one-failed",
        ),
        pytest.param(
            [
                ReproAttempt(1, 1, False, "assert 1 == 2", 1.0),
                ReproAttempt(2, 1, False, "assert 2 == 3", 1.0),
            ],
            id="different-first-error-line",
        ),
    ],
)
def test_flake(attempts):
    """一过一败、或两次首错行不同 -> flake。

    flake 不进修复 plan：它登记为 flaky 后本地继续，不是一次框架缺陷的证据。
    """
    assert classify(attempts) == FLAKE


@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param(
            [
                ReproAttempt(1, None, True, "", 30.0),
                ReproAttempt(2, 0, False, "", 12.0),
            ],
            id="timeout-and-pass",
        ),
        pytest.param(
            [
                ReproAttempt(1, None, True, "", 30.0),
                ReproAttempt(2, 1, False, "assert 1 == 2", 2.0),
            ],
            id="timeout-and-failure",
        ),
    ],
)
def test_mixed_timeout_is_flake(attempts):
    """1/2 超时（另一跑通过或失败）-> flake，不是 framework_timeout。

    一次挂死不足以说明框架里挂着东西：另一半的观察说的是另一件事。把它当成
    框架缺陷，就会拿一个「只挂死过一次的现场」去建修复 plan。

    下游也要一起断言：``flake`` 在双条件阻塞判定里**不阻塞、不再复现** ——
    登记为 flaky 就地继续，不建修复 plan。
    """
    classification = classify(attempts)
    assert classification == FLAKE

    decision = decide(_hit(), reproduced=classification)
    assert decision.blocking is False
    assert decision.reproduce is False
    assert decision.register is True


# ---------------------------------------------------------------------------
# 4. 两次都通过
# ---------------------------------------------------------------------------
def test_not_reproducible():
    """两次都通过 -> not_reproducible。

    干净 main 上跑不出这个失败，问题在 plan 分支或已经消失 —— 本地继续，不建
    修复 plan。
    """
    attempts = [
        ReproAttempt(1, 0, False, "", 12.5),
        ReproAttempt(2, 0, False, "", 11.5),
    ]

    assert classify(attempts) == NOT_REPRODUCIBLE


# ---------------------------------------------------------------------------
# 5. 空
# ---------------------------------------------------------------------------
def test_empty():
    """没有 attempt -> unclassified。

    没有观察就没有结论。没有结论不是「通过」，也不是「框架缺陷」。
    """
    assert classify([]) == UNCLASSIFIED


# ---------------------------------------------------------------------------
# 边界：未知组合与永不抛错
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param(
            [
                ReproAttempt(1, None, False, "", 0.0),
                ReproAttempt(2, None, False, "", 0.0),
            ],
            id="command-not-executable",
        ),
        pytest.param(
            [
                ReproAttempt(1, 1, False, "", 1.0),
                ReproAttempt(2, 1, False, "", 1.0),
            ],
            id="truncated-output",
        ),
        pytest.param(
            [ReproAttempt(1, 1, False, "assert 1 == 2", 1.0)],
            id="single-attempt",
        ),
    ],
)
def test_unknown_combinations_are_unclassified(attempts):
    """命令不可执行、输出被截断、只有一次观察 -> unclassified。

    这三种都缺一份能比较的材料：没有退出码可比，或没有可比的首错行，或根本没
    跑够两次。2/2 的判据凑不齐，就不判。
    """
    assert classify(attempts) == UNCLASSIFIED


def test_timeout_plus_unrunnable():
    """超时 + 没跑起来 -> unclassified。

    没跑起来的那一跑连「挂没挂死」都无从说起：证据不全，2/2 的判据凑不齐，
    就不判 —— 更不能因为另一跑挂死了就把它算成框架缺陷。
    """
    attempts = [
        ReproAttempt(1, None, True, "", 30.0),
        ReproAttempt(2, None, False, "", 0.0),
    ]

    assert classify(attempts) == UNCLASSIFIED


@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param(None, id="none"),
        pytest.param("not-a-list", id="string"),
        pytest.param(42, id="int"),
        pytest.param([object()], id="foreign-object"),
    ],
)
def test_never_raises(attempts):
    """任何输入都返回五个词之一 —— 分类不是让调度器崩掉的地方。"""
    assert classify(attempts) in CLASSIFICATIONS


def test_classification_is_a_closed_set():
    """分类的取值落在闭集里 —— 五个词，没有第六个。"""
    samples = [
        [],
        [ReproAttempt(1, 1, False, "assert 1 == 2", 3.1)],
        [ReproAttempt(1, 1, False, "assert 1 == 2", 3.1),
         ReproAttempt(2, 1, False, "assert 1 == 2", 2.9)],
        [ReproAttempt(1, 1, False, "assert 1 == 2", 3.1),
         ReproAttempt(2, 0, False, "", 2.9)],
        [ReproAttempt(1, None, True, "", 30.0),
         ReproAttempt(2, None, True, "", 30.0)],
        [ReproAttempt(1, 0, False, "", 12.5),
         ReproAttempt(2, 0, False, "", 11.5)],
    ]

    assert CLASSIFICATIONS == {
        "framework_deterministic",
        "framework_timeout",
        "flake",
        "not_reproducible",
        "unclassified",
    }
    for attempts in samples:
        assert classify(attempts) in CLASSIFICATIONS


def test_classify_is_pure():
    """同一份材料给出同一个分类 —— 纯函数，不看时钟、不留状态。"""
    attempts = [
        ReproAttempt(1, 1, False, "assert 1 == 2", 3.1),
        ReproAttempt(2, 1, False, "assert 1 == 2", 2.9),
    ]

    assert classify(attempts) == classify(attempts) == FRAMEWORK_DETERMINISTIC


def test_accepts_any_sequence_of_attempts():
    """入参是序列，不只是 list —— 元组一样判。"""
    attempts = (
        ReproAttempt(1, 1, False, "assert 1 == 2", 3.1),
        ReproAttempt(2, 1, False, "assert 1 == 2", 2.9),
    )

    assert classify(attempts) == FRAMEWORK_DETERMINISTIC


def test_bookkeeping_fields_do_not_change_the_verdict():
    """``run`` 与 ``duration_s`` 是记账字段，不参与判定。

    跑第几遍、跑了多久，都不是失败的身份：同样的两次失败、同样的首错行，判定
    不该因为计时器快慢而变。
    """
    attempts = [
        ReproAttempt(7, 1, False, "assert 1 == 2", 0.01),
        ReproAttempt(7, 1, False, "assert 1 == 2", 99.0),
    ]

    assert classify(attempts) == FRAMEWORK_DETERMINISTIC


# ---------------------------------------------------------------------------
# 边界：结论的证据字段
# ---------------------------------------------------------------------------
def test_result_evidence_round_trips():
    """证据随结论一起往返 —— 写进去的读得回来。

    「基准读不出来」这类结论的价值全在证据里：请求的 base_ref、仓库路径、git
    的原始输出。证据存不进去，结论就只剩一个词。
    """
    result = ReproductionResult(
        classification=UNCLASSIFIED,
        attempts=(),
        clean_commit="",
        tree_fingerprint="",
        evidence={"base_ref": "origin/main", "unclassified_round": 2},
    )

    payload = result.to_dict()
    assert payload["evidence"] == {
        "base_ref": "origin/main",
        "unclassified_round": 2,
    }
    assert ReproductionResult.from_dict(payload) == result


def test_old_rows_without_evidence_read_back_clean():
    """老行没有 ``evidence`` 键 -> 读回来是空字典，不报错。

    落库的行是历史：加字段不能把历史读崩。
    """
    payload = {
        "classification": FRAMEWORK_DETERMINISTIC,
        "attempts": [
            {
                "run": 1,
                "exit_code": 1,
                "timed_out": False,
                "first_error_line": "assert 1 == 2",
                "duration_s": 1.0,
            },
            {
                "run": 2,
                "exit_code": 1,
                "timed_out": False,
                "first_error_line": "assert 1 == 2",
                "duration_s": 1.0,
            },
        ],
        "clean_commit": "a" * 40,
        "tree_fingerprint": "sha256:" + "b" * 64,
    }

    result = ReproductionResult.from_dict(payload)

    assert result.classification == FRAMEWORK_DETERMINISTIC
    assert result.evidence == {}
    assert result.to_dict()["evidence"] == {}
