"""跨轮 infra 重试账本 —— 给 infra 失败装一个次数上限（VP-006 arch 决策点 2）。

背景
====

``verification.failure_triage`` 的分诊表给 infra 类的处置是
``retry_not_counted``：环境失败不算在产物的失败账上，可以重试。但
"可以重试" 没有边界 —— 没有次数上限，也没有按 VP 记账的地方。一个
永远超时的 VP 可以被无限重试，而每一次重试看起来都"不算数"。

本文件钉住账本本身的行为（账本由 ``verification.infra_retry`` 提供）：

* 按 VP 记下每一轮的 provider / reason_code；
* 上限判据 ``len(attempts) >= 1 + max_retries``（一次首试 + 两次重试）；
* 跨实例读得到（落盘，不是进程内存）；
* 同一 ``(vp_id, round)`` 重复记账不重复计；
* 文件缺失 / 损坏 / 不是 dict → 空账本，不抛异常；
* 并发记账不丢条目。

本文件**不接线** —— 谁来记账、耗尽之后怎么办，是后续任务的事。
"""

from __future__ import annotations

import json
import threading

from verification.failure_triage import MAX_INFRA_CROSS_ROUND_RETRIES
from verification.infra_retry import LEDGER_FILENAME, InfraRetryLedger


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def test_max_infra_cross_round_retries_is_two():
    """上限常量必须是 2 —— 一次首试 + 两次跨轮重试。"""
    assert MAX_INFRA_CROSS_ROUND_RETRIES == 2


def test_default_max_retries_comes_from_the_constant(tmp_path):
    ledger = InfraRetryLedger(tmp_path)
    assert ledger.max_retries == MAX_INFRA_CROSS_ROUND_RETRIES


# ---------------------------------------------------------------------------
# 记账
# ---------------------------------------------------------------------------


def test_record_attempt_returns_the_entry(tmp_path):
    """记账返回刚记下的那一条。"""
    ledger = InfraRetryLedger(tmp_path)
    entry = ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro", reason_code="coding_tool_call_failed"
    )
    assert entry == {
        "round": 1,
        "provider": "vendor-a-pro",
        "reason_code": "coding_tool_call_failed",
    }


def test_record_attempt_then_read_back(tmp_path):
    """任务书给的输入 / 输出示例，一字不差。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro", reason_code="coding_tool_call_failed"
    )
    assert ledger.attempts("VP-006") == [
        {"round": 1, "provider": "vendor-a-pro", "reason_code": "coding_tool_call_failed"}
    ]
    assert ledger.is_exhausted("VP-006") is False
    assert ledger.excluded_providers("VP-006") == ["vendor-a-pro"]


def test_ledger_file_lives_in_the_plan_dir(tmp_path):
    """账本落在 plan 目录里，文件名固定。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    assert (tmp_path / LEDGER_FILENAME).exists()
    assert LEDGER_FILENAME == "verification_infra_retries.json"


def test_vps_are_accounted_separately(tmp_path):
    """账本是按 VP 记的 —— 一个 VP 的失败不连累另一个。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    ledger.record_attempt("VP-007", 1, provider="vendor-b-pro")
    assert ledger.excluded_providers("VP-006") == ["vendor-a-pro"]
    assert ledger.excluded_providers("VP-007") == ["vendor-b-pro"]


def test_record_creates_the_plan_dir_if_missing(tmp_path):
    """plan 目录还不存在时，记账把目录建出来（原子写的既有约定）。"""
    plan_dir = tmp_path / "plan-not-created-yet"
    ledger = InfraRetryLedger(plan_dir)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    assert (plan_dir / LEDGER_FILENAME).exists()


# ---------------------------------------------------------------------------
# 上限判据
# ---------------------------------------------------------------------------


def test_exhausts_after_two_retries(tmp_path):
    """记 3 轮（首试 + 2 次重试）→ 耗尽。"""
    ledger = InfraRetryLedger(tmp_path)
    for round_number in (1, 2, 3):
        ledger.record_attempt("VP-006", round_number, provider="vendor-a-pro")
    assert ledger.is_exhausted("VP-006") is True


def test_not_exhausted_at_two_rounds(tmp_path):
    """记 2 轮（首试 + 1 次重试）→ 还没到上限。"""
    ledger = InfraRetryLedger(tmp_path)
    for round_number in (1, 2):
        ledger.record_attempt("VP-006", round_number, provider="vendor-a-pro")
    assert ledger.is_exhausted("VP-006") is False


def test_exhaustion_threshold_is_one_plus_max_retries(tmp_path):
    """判据是 ``1 + max_retries``：上限改成 0 就一轮即耗尽。"""
    ledger = InfraRetryLedger(tmp_path, max_retries=0)
    assert ledger.is_exhausted("VP-006") is False
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    assert ledger.is_exhausted("VP-006") is True


def test_unknown_vp_is_never_exhausted(tmp_path):
    """没记过账的 VP 不是耗尽 —— 是"还没开始"，不是"试完了"。"""
    ledger = InfraRetryLedger(tmp_path)
    assert ledger.is_exhausted("VP-999") is False
    assert ledger.attempts("VP-999") == []
    assert ledger.excluded_providers("VP-999") == []


# ---------------------------------------------------------------------------
# provider 排除集
# ---------------------------------------------------------------------------


def test_excluded_providers_dedup(tmp_path):
    """两轮同一个 provider → 排除集里只出现一次。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    ledger.record_attempt("VP-006", 2, provider="vendor-a-pro")
    assert ledger.excluded_providers("VP-006") == ["vendor-a-pro"]


def test_excluded_providers_lists_distinct_providers_in_order(tmp_path):
    """不同 provider 各出现一次，按轮次顺序。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    ledger.record_attempt("VP-006", 2, provider="vendor-b-pro")
    ledger.record_attempt("VP-006", 3, provider="vendor-a-pro")
    assert ledger.excluded_providers("VP-006") == ["vendor-a-pro", "vendor-b-pro"]


def test_excluded_providers_skips_blank_providers(tmp_path):
    """没有 provider 的条目不进入排除集（空串不是 provider）。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="", reason_code="timeout")
    assert ledger.excluded_providers("VP-006") == []


# ---------------------------------------------------------------------------
# 跨实例 / 落盘
# ---------------------------------------------------------------------------


def test_persists_across_instances(tmp_path):
    """新实例读到同一份账本 —— 账本在磁盘上，不在实例内存里。"""
    first = InfraRetryLedger(tmp_path)
    first.record_attempt(
        "VP-006", 1, provider="vendor-a-pro", reason_code="coding_tool_call_failed"
    )
    second = InfraRetryLedger(tmp_path)
    assert second.attempts("VP-006") == [
        {"round": 1, "provider": "vendor-a-pro", "reason_code": "coding_tool_call_failed"}
    ]


def test_second_instance_appends_to_the_same_ledger(tmp_path):
    """第二个实例写的条目，第一个实例也读得到。"""
    first = InfraRetryLedger(tmp_path)
    first.record_attempt("VP-006", 1, provider="vendor-a-pro")
    InfraRetryLedger(tmp_path).record_attempt("VP-006", 2, provider="vendor-b-pro")
    assert len(first.attempts("VP-006")) == 2


# ---------------------------------------------------------------------------
# 边界：文件缺失 / 损坏 / 不是 dict
# ---------------------------------------------------------------------------


def test_missing_file_starts_empty(tmp_path):
    """账本文件不存在 → 空账本，不抛。"""
    ledger = InfraRetryLedger(tmp_path)
    assert ledger.attempts("VP-006") == []
    assert ledger.is_exhausted("VP-006") is False
    assert ledger.excluded_providers("VP-006") == []


def test_corrupt_file_starts_empty(tmp_path):
    """账本文件内容损坏 → 空账本，不抛。"""
    (tmp_path / LEDGER_FILENAME).write_text("{ not json", encoding="utf-8")
    ledger = InfraRetryLedger(tmp_path)
    assert ledger.attempts("VP-006") == []
    assert ledger.is_exhausted("VP-006") is False


def test_non_dict_file_starts_empty(tmp_path):
    """账本文件不是 dict → 空账本，不抛。"""
    (tmp_path / LEDGER_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    ledger = InfraRetryLedger(tmp_path)
    assert ledger.attempts("VP-006") == []


def test_corrupt_file_can_be_repaired_by_recording(tmp_path):
    """坏文件被一次记账覆盖成可用账本，而不是一直坏着。"""
    (tmp_path / LEDGER_FILENAME).write_text("{ not json", encoding="utf-8")
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    assert InfraRetryLedger(tmp_path).attempts("VP-006") == [
        {"round": 1, "provider": "vendor-a-pro", "reason_code": ""}
    ]


# ---------------------------------------------------------------------------
# 幂等与并发
# ---------------------------------------------------------------------------


def test_duplicate_round_is_idempotent(tmp_path):
    """同一 (vp_id, round) 重复记账 → 不重复计。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro", reason_code="coding_tool_call_failed"
    )
    ledger.record_attempt(
        "VP-006", 1, provider="vendor-a-pro", reason_code="coding_tool_call_failed"
    )
    attempts = ledger.attempts("VP-006")
    assert len(attempts) == 1
    assert attempts[0] == {
        "round": 1,
        "provider": "vendor-a-pro",
        "reason_code": "coding_tool_call_failed",
    }


def test_duplicate_round_does_not_advance_the_budget(tmp_path):
    """重复记账不消耗重试预算 —— 幂等是记账的账，不是轮次的账。"""
    ledger = InfraRetryLedger(tmp_path)
    for _ in range(5):
        ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    assert ledger.is_exhausted("VP-006") is False


def test_concurrent_records_are_not_lost(tmp_path):
    """并发记账不丢条目（读改写必须持锁）。"""
    ledger = InfraRetryLedger(tmp_path)
    workers = 8
    start = threading.Barrier(workers)

    def record(index: int) -> None:
        start.wait()
        ledger.record_attempt("VP-006", index, provider=f"vendor-{index}")

    threads = [threading.Thread(target=record, args=(i,)) for i in range(1, workers + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    attempts = ledger.attempts("VP-006")
    assert len(attempts) == workers
    assert sorted(entry["round"] for entry in attempts) == list(range(1, workers + 1))


def test_ledger_file_is_valid_json(tmp_path):
    """落盘的是合法 JSON，读得回来的账本。"""
    ledger = InfraRetryLedger(tmp_path)
    ledger.record_attempt("VP-006", 1, provider="vendor-a-pro")
    payload = json.loads((tmp_path / LEDGER_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
