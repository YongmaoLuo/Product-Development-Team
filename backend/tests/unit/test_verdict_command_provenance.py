"""verdict 的出处 —— 结论只对产生它的那条命令有效（2026-09-15，用户选 C）。

起因
----
a production plan 的 round 1 重跑里，30 个 VP **只有 VP-023 真的执行了**，其余 29
个的结论全部来自历史。铁证：VP-013 的失败理由写着
``cargo test --lib ... running 0 tests; 175 filtered out``，而计划早在
17:47 就把命令改成了 ``cargo test --test signal_classification``。
**那份结论比它要评判的命令还旧**，却被当作通过跳过。

根因：``VerificationExecutor`` 从 ``plan_verification.verdicts``（DB 里跨轮
累积的表）**无条件**水合 ``_verdicts``，据此把 PASSED 塞进 ``completed_set``
跳过。``/start`` 的 ``force`` / ``resume`` 都到不了这里 ——
``VerificationExecutor.__init__`` 连 ``resume`` 形参都没有。

为什么不选 B（force 就全量重验）
------------------------------------
撞到轮次上限并不等于结论失效 —— 那种情况下本来就应该重跑、继续迭代
验证。

重置轮次计数常常只是为了撞顶后继续迭代，那时把已挣到的结论全部作废是错的。
作废的判据应该是**命令变没变**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verification_executor import VerificationExecutor
from verification_verdict_provenance import (
    FINGERPRINT_FIELD,
    command_fingerprint,
    is_stale,
    stamp,
)


# ---------------------------------------------------------------------------
# 指纹本身
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_whitespace_churn() -> None:
    a = "cargo test --test x test_y"
    b = "  cargo   test --test x\ttest_y  "
    assert command_fingerprint(a) == command_fingerprint(b), (
        "纯粹重排空白不该让一份结论失效"
    )


def test_fingerprint_catches_the_real_change() -> None:
    """这正是 VP-013 的那次改动。"""
    old = "cargo test --lib test_no_cross_branch_fallback"
    new = "cargo test --test signal_classification test_no_cross_branch_fallback"
    assert command_fingerprint(old) != command_fingerprint(new)


@pytest.mark.parametrize("raw", [None, "", "   ", 42, [], {}])
def test_fingerprint_is_empty_for_non_commands(raw) -> None:
    assert command_fingerprint(raw) == ""


def test_stamp_returns_a_copy() -> None:
    original = {"status": "PASSED"}
    stamped = stamp(original, "pytest tests/a.py")
    assert FINGERPRINT_FIELD in stamped
    assert FINGERPRINT_FIELD not in original, "stamp 不得改动原对象"


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_no_fingerprint_is_stale() -> None:
    """旧结论无法证明自己对应哪条命令 —— 唯一诚实的处理是让它重新挣一次。

    反过来（当有效）等于把 bug 原样保留：VP-013 那条 ``--lib`` 结论恰恰
    就是"没有指纹"的。
    """
    assert is_stale({"status": "PASSED"}, "pytest tests/a.py") is True


def test_matching_fingerprint_is_not_stale() -> None:
    cmd = "pytest tests/a.py -v"
    assert is_stale(
        {"status": "PASSED", FINGERPRINT_FIELD: command_fingerprint(cmd)}, cmd,
    ) is False


def test_changed_command_is_stale() -> None:
    verdict = {"status": "PASSED", FINGERPRINT_FIELD: command_fingerprint("pytest tests/a.py")}
    assert is_stale(verdict, "pytest tests/b.py") is True


def test_split_verdicts_are_exempt() -> None:
    """SPLIT 是"这个 VP 被拆成子 VP 了"，真结论在子 VP 身上，父 VP 不该重跑。"""
    assert is_stale({"status": "SPLIT"}, "pytest tests/a.py") is False


def test_a_vp_missing_from_the_plan_is_not_invalidated() -> None:
    """``None`` = VP 已不在计划里 —— 无从比较，维持旧行为。"""
    assert is_stale({"status": "PASSED"}, None) is False


def test_non_dict_verdicts_are_tolerated() -> None:
    assert is_stale(None, "pytest tests/a.py") is False  # type: ignore[arg-type]
    assert is_stale("garbage", "pytest tests/a.py") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 执行器集成：过期结论必须掉出 completed_set（＝会被重跑）
# ---------------------------------------------------------------------------


def _executor(tmp_path: Path, commands: dict) -> VerificationExecutor:
    plan = {
        "verification_points": [
            {"id": vid, "title": vid, "test_command": cmd}
            for vid, cmd in commands.items()
        ],
    }
    return VerificationExecutor(
        verification_plan=plan,
        plan_id="p-provenance",
        plan_dir=tmp_path,
        sub_agent_runner=lambda *_a, **_k: None,
    )


OLD_CMD = "cargo test --lib test_no_cross_branch_fallback"
NEW_CMD = "cargo test --test signal_classification test_no_cross_branch_fallback"


def test_a_verdict_from_a_changed_command_is_rerun(tmp_path: Path) -> None:
    """VP-013 的复现：结论是旧命令挣的，计划里已经是新命令。"""
    ex = _executor(tmp_path, {"VP-013": NEW_CMD})
    ex._verdicts = {
        "VP-013": {
            "status": "PASSED",
            FINGERPRINT_FIELD: command_fingerprint(OLD_CMD),
        },
    }

    ex._backfill_index_lists_from_verdicts()

    assert ex._completed_vps == [], (
        "过期结论绝不能进 completed_set —— 进了就永远不会重跑"
    )
    assert ex._completed_items == [], (
        "BaseExecutor 的 completed_set 读的是 _completed_items"
    )
    assert "VP-013" not in ex._verdicts, "过期结论被摘掉，避免重跑前被读到"


def test_a_stale_verdict_is_not_mislabelled_as_failed(tmp_path: Path) -> None:
    """它没有失败，只是证据过期了 —— 报成 FAILED 会污染失败统计与修复任务。"""
    ex = _executor(tmp_path, {"VP-013": NEW_CMD})
    ex._verdicts = {"VP-013": {"status": "PASSED"}}

    ex._backfill_index_lists_from_verdicts()

    assert ex._failed_vps == []
    assert ex._skipped_vps == []


def test_a_verdict_matching_the_current_command_still_skips(tmp_path: Path) -> None:
    """C 不做全量重验：命令没变的 VP 照旧跳过，reset 计数后依然如此。

    这正是用户要的「撞顶后重置，继续迭代」。
    """
    ex = _executor(tmp_path, {"VP-001": NEW_CMD, "VP-002": "pytest tests/b.py"})
    ex._verdicts = {
        "VP-001": {"status": "PASSED", FINGERPRINT_FIELD: command_fingerprint(NEW_CMD)},
        "VP-002": {"status": "PASSED", FINGERPRINT_FIELD: command_fingerprint("pytest tests/b.py")},
    }

    ex._backfill_index_lists_from_verdicts()

    assert sorted(ex._completed_vps) == ["VP-001", "VP-002"]


def test_record_verdict_stamps_the_current_command(tmp_path: Path) -> None:
    ex = _executor(tmp_path, {"VP-001": NEW_CMD})

    ex.record_verdict("VP-001", {
        "status": "PASSED", "reasons": ["ok"], "evidence": {},
    })

    assert ex._verdicts["VP-001"][FINGERPRINT_FIELD] == command_fingerprint(NEW_CMD)


def test_the_stamp_makes_a_fresh_verdict_survive_the_next_backfill(tmp_path: Path) -> None:
    """端到端：record → backfill 之后它仍然是"已完成"，不会自己把自己作废。"""
    ex = _executor(tmp_path, {"VP-001": NEW_CMD})
    ex.record_verdict("VP-001", {
        "status": "PASSED", "reasons": ["ok"], "evidence": {},
    })

    ex._backfill_index_lists_from_verdicts()

    assert ex._completed_vps == ["VP-001"]


def test_persisted_verdicts_carry_the_fingerprint(tmp_path: Path) -> None:
    """指纹必须落库 —— 否则重启后从 DB 水合回来的结论又变成"没有指纹"，
    下一轮全被作废，C 就白做了。
    """
    seen: list = []

    class _Repo:
        def append_verdict(self, plan_id, payload):
            seen.append(payload)

        # ``record_verdict`` 之外的路径不参与本用例。
        def __getattr__(self, _name):
            raise AssertionError(f"unexpected repo call: {_name}")

    ex = _executor(tmp_path, {"VP-001": NEW_CMD})
    ex.verif_repo = _Repo()

    ex.record_verdict("VP-001", {
        "status": "PASSED", "reasons": ["ok"], "evidence": {},
    })

    assert seen and seen[0][FINGERPRINT_FIELD] == command_fingerprint(NEW_CMD)
