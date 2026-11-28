"""TDD spec — backend/framework_fix/classifier.py 双条件阻塞判定。

这个模块要解决什么
------------------
PRD 要求：失败点只有在「**验收关键** 且（**框架已复现** 或 **改动越界**）」时
才算 blocking::

    blocking = acceptance_critical and (framework_reproduced or out_of_scope_change)

这条公式此前没有任何代码表达过 —— 分类散落在调用方的 if 里，口径一旦漂移，
「同一个缺陷在 A plan 阻塞、在 B plan 不阻塞」就会发生。

判定必须是纯函数
----------------
不读文件、不发请求、不看时钟、不读数据库。输入是「这次命中的事实」（是否
验收关键、是否越界、复现结论、已连续未分类的轮数），输出是一个决定。同一份
输入在任何进程里给出同一个决定 —— 否则「这个失败到底阻不阻塞」本身就不可
复算。

处置是闭集
----------
:data:`DISPOSITIONS` 是五个值的闭集。调用方按 disposition 分派，不允许自造
字符串 —— 自造的字符串在这里是拼写错误，不是新语义。

TDD contract — 5 named cases
-----------------------------
1. ``test_both_conditions`` —— 验收关键 + 框架已复现 -> blocking。
2. ``test_non_critical`` —— 非验收关键 -> not_blocking（永不 blocking）。
3. ``test_out_of_scope`` —— 改动越界 -> blocking，且无需复现。
4. ``test_needs_repro`` —— 尚未复现 -> needs_repro。
5. ``test_exhausted`` —— 连续两轮未分类 -> exhausted。

同一文件覆盖的边界条件：

* ``flake`` / ``not_reproducible`` 优先于「改动越界」：即使越界，本轮也不创建
  修复 plan（分别登记 flaky / 本地继续）。
* ``acceptance_critical=False`` 压过一切：永不 blocking。
* 未知分类值 -> ``ValueError``（闭集，不静默当成某一类）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.framework_fix.classifier import (  # noqa: E402
    DISPOSITIONS,
    FailureDecision,
    FailureHit,
    decide,
)


def _hit(
    *,
    acceptance_critical: bool = True,
    out_of_scope_change: bool = False,
) -> FailureHit:
    """一份最小命中材料：一次「断言失败，发生在某个框架脚本上」。"""
    return FailureHit(
        vp_id='VP-3',
        plan_id='p1',
        error_class='AssertionError',
        first_error_line='assert 1 == 2',
        framework_files=['scripts/g.sh'],
        acceptance_critical=acceptance_critical,
        out_of_scope_change=out_of_scope_change,
    )


# ---------------------------------------------------------------------------
# 1. 双条件都满足 -> blocking
# ---------------------------------------------------------------------------
def test_both_conditions():
    """验收关键 + 框架已复现 -> blocking，登记签名，不再复现。"""
    decision = decide(_hit(), reproduced='framework_deterministic')

    assert decision.disposition == 'blocking'
    assert decision.blocking is True
    assert decision.register is True
    assert decision.reproduce is False
    assert decision.unclassified_rounds == 0


# ---------------------------------------------------------------------------
# 2. 非验收关键 -> 永不 blocking
# ---------------------------------------------------------------------------
def test_non_critical():
    """非验收关键 -> not_blocking；即使已复现为框架缺陷也不阻塞。"""
    decision = decide(
        _hit(acceptance_critical=False), reproduced='framework_deterministic'
    )

    assert decision.disposition == 'not_blocking'
    assert decision.blocking is False


def test_non_critical_never_blocks_even_when_out_of_scope():
    """越界也救不了非验收关键的失败：「永不 blocking」压过一切。"""
    decision = decide(
        _hit(acceptance_critical=False, out_of_scope_change=True),
        reproduced='framework_deterministic',
    )

    assert decision.disposition == 'not_blocking'
    assert decision.blocking is False


# ---------------------------------------------------------------------------
# 3. 改动越界 -> blocking，无需复现
# ---------------------------------------------------------------------------
def test_out_of_scope():
    """改动越界本身就是阻塞理由：不需要再复现一次。"""
    decision = decide(_hit(out_of_scope_change=True), reproduced=None)

    assert decision.disposition == 'blocking'
    assert decision.blocking is True
    assert decision.reproduce is False


# ---------------------------------------------------------------------------
# 4. 尚未复现 -> needs_repro
# ---------------------------------------------------------------------------
def test_needs_repro():
    """关键但既没复现也没越界 -> needs_repro：本轮还不能下结论。"""
    decision = decide(_hit(), reproduced=None)

    assert decision.disposition == 'needs_repro'
    assert decision.blocking is False
    assert decision.reproduce is True


# ---------------------------------------------------------------------------
# 5. 连续未分类 -> exhausted
# ---------------------------------------------------------------------------
def test_exhausted():
    """同一签名连续两轮未分类 -> exhausted（升级 HITL，不再复现）。"""
    decision = decide(_hit(), reproduced='unclassified', unclassified_rounds=2)

    assert decision.disposition == 'exhausted'
    assert decision.blocking is False
    assert decision.reproduce is False
    assert decision.unclassified_rounds == 2


def test_exhausted_also_reachable_without_reproduction_result():
    """轮次计数本身就是判据：上一轮就未分类，本轮不必再跑一次复现。"""
    decision = decide(_hit(), reproduced=None, unclassified_rounds=2)

    assert decision.disposition == 'exhausted'
    assert decision.blocking is False


# ---------------------------------------------------------------------------
# 边界：flake / not_reproducible 优先于越界
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reproduced",
    ["flake", "not_reproducible"],
)
def test_flake_and_not_reproducible_never_block(reproduced):
    """本轮不复现为框架缺陷 -> 不阻塞；即使同时越界也不阻塞。"""
    decision = decide(
        _hit(out_of_scope_change=True), reproduced=reproduced
    )

    assert decision.disposition == 'not_blocking'
    assert decision.blocking is False
    assert decision.register is True
    assert decision.reproduce is False


def test_framework_timeout_counts_as_reproduced():
    """超时与确定性失败同属「框架已复现」。"""
    decision = decide(_hit(), reproduced='framework_timeout')

    assert decision.disposition == 'blocking'
    assert decision.blocking is True


def test_unclassified_not_yet_exhausted():
    """复现跑过但归不了类 -> unclassified：登记并下轮重试。"""
    decision = decide(_hit(), reproduced='unclassified', unclassified_rounds=1)

    assert decision.disposition == 'unclassified'
    assert decision.blocking is False
    assert decision.register is True
    assert decision.reproduce is True


# ---------------------------------------------------------------------------
# 真值表：acceptance_critical × reproduced × out_of_scope_change
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "acceptance_critical,reproduced,out_of_scope,expected_disposition,expected_blocking",
    [
        # 验收关键 + 已复现（两种框架结论）-> 阻塞。
        (True, "framework_deterministic", False, "blocking", True),
        (True, "framework_deterministic", True, "blocking", True),
        (True, "framework_timeout", False, "blocking", True),
        (True, "framework_timeout", True, "blocking", True),
        # 验收关键 + 已复现但不是框架问题 -> 不阻塞（越界也不例外）。
        (True, "flake", False, "not_blocking", False),
        (True, "flake", True, "not_blocking", False),
        (True, "not_reproducible", False, "not_blocking", False),
        (True, "not_reproducible", True, "not_blocking", False),
        # 验收关键 + 归不了类 -> 越界给出确定结论，否则继续等。
        (True, "unclassified", False, "unclassified", False),
        (True, "unclassified", True, "blocking", True),
        # 验收关键 + 还没复现 -> 越界直接阻塞，否则 needs_repro。
        (True, None, False, "needs_repro", False),
        (True, None, True, "blocking", True),
        # 非验收关键 -> 永不阻塞。
        (False, "framework_deterministic", False, "not_blocking", False),
        (False, "framework_deterministic", True, "not_blocking", False),
        (False, "flake", False, "not_blocking", False),
        (False, "not_reproducible", True, "not_blocking", False),
        (False, "unclassified", False, "not_blocking", False),
        (False, None, True, "not_blocking", False),
    ],
)
def test_truth_table(
    acceptance_critical,
    reproduced,
    out_of_scope,
    expected_disposition,
    expected_blocking,
):
    """全部真值组合穷举：判定口径不靠调用方的 if 拼装。"""
    decision = decide(
        _hit(
            acceptance_critical=acceptance_critical,
            out_of_scope_change=out_of_scope,
        ),
        reproduced=reproduced,
    )

    assert decision.disposition == expected_disposition
    assert decision.blocking == expected_blocking
    assert decision.disposition in DISPOSITIONS


# ---------------------------------------------------------------------------
# 契约面
# ---------------------------------------------------------------------------
def test_dispositions_is_a_closed_set():
    """处置是闭集：调用方按它分派，不允许自造字符串。"""
    assert DISPOSITIONS == (
        'blocking',
        'needs_repro',
        'unclassified',
        'exhausted',
        'not_blocking',
    )


def test_decision_fields():
    """决定的形状固定：五个字段，顺序与契约一致。"""
    decision = decide(_hit(), reproduced='framework_deterministic')

    assert isinstance(decision, FailureDecision)
    assert (
        decision.disposition,
        decision.blocking,
        decision.register,
        decision.reproduce,
        decision.unclassified_rounds,
    ) == ('blocking', True, True, False, 0)


def test_unknown_classification_raises():
    """未知分类值是数据坏了，不是「某一类」：响亮地失败。"""
    with pytest.raises(ValueError):
        decide(_hit(), reproduced='framework_reproduced')


def test_decide_is_pure():
    """纯函数：同一份输入给出同一个决定，且不改命中材料。"""
    hit = _hit()
    first = decide(hit, reproduced='framework_deterministic')
    second = decide(hit, reproduced='framework_deterministic')

    assert first == second
    assert hit.framework_files == ['scripts/g.sh']
    assert hit.acceptance_critical is True
    assert hit.out_of_scope_change is False