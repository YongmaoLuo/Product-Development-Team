"""The legacy mirror stop-reason migration is operator-triggered, not a read fallback.

Background
----------
Before the verification writes moved to ``plan_verification`` (arch decision
point 1), the only home of a verification loop's stop reason was the
``plan_routing.verification`` mirror column — the baseline commit
``aabce578``'s ``backend/verification_loop.py`` wrote it there with a bare
``UPDATE plan_routing SET verification = …``.  A plan that stopped back then
has no authoritative row, so when the reads stopped consulting the column
(VP-015 (c)) its reason would be lost — and the "first reason wins" rule
(the reason of the first stop IS the cause) would start overwriting it.

Carrying the reason over as a **read-path fallback** is what went wrong: the
read wrote a row, so the answer depended on the order the readers ran in, and
the mirror column was a state source again.  The carry-over is therefore an
explicit, operator-triggered one-time migration.

Contract pinned here
--------------------
1. The CLI is **dry-run by default** — a run without ``--apply`` reports what
   it would do and writes nothing.
2. ``--apply`` carries **only the stop reason** into ``plan_verification``.
   The mirror's ``status`` / ``round`` / ``max_rounds`` are NOT carried: the
   row gets the documented defaults — ``status="not_started"``, round ``0``
   and the documented default budget (arch decision point 3).  The plan's
   phase is not consulted either: the routing phase would be a second
   source of the verification status.
3. The migration is **idempotent** — a second run migrates nothing and leaves
   the authoritative row byte-identical.  A plan that already has an
   authoritative row is never touched.
4. The migration lands in ``plan_verification`` — the authoritative table —
   and in no other table.
5. The CLI's ``--apply`` path actually writes, and it leaves a **pre-write**
   ``.bak`` snapshot behind — the backup is the operator's only way back.
6. A plan whose write fails is **reported** under ``failed`` and the run
   continues with the next plan; the failed plan leaves no half-written row.

``scripts.migrate_legacy_verification_stop_reason`` is imported by module name
(not ``backend.scripts…``) because ``backend/pytest.ini`` puts ``backend/``
on ``pythonpath`` — the same import style the sibling migration test uses
(``backend/tests/unit/test_migration_audit_security.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scripts.migrate_legacy_verification_stop_reason import (  # noqa: E402
    classify_legacy_stop_reasons,
    main,
    migrate_legacy_verification_stop_reasons,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """A throwaway state database for the migration to run against."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    try:
        migrate(conn)
    finally:
        conn.close()
    return db


def _seed_plan(
    db: Path,
    plan_id: str,
    *,
    phase: str = "verification",
    mirror: Optional[dict[str, Any]] = None,
    authoritative: Optional[dict[str, Any]] = None,
) -> None:
    """Seed a plan: a routing row, an optional mirror payload, an optional row."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        RoutingRepository(conn).write_plan_state(
            plan_id,
            phase=phase,
            completed_phases=[],
            review_rounds={},
            flags={},
            last_updated=None,
        )
        if mirror is not None:
            conn.execute(
                "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
                (json.dumps(mirror), plan_id),
            )
            conn.commit()
        if authoritative is not None:
            VerificationRepository(conn).insert(
                plan_id,
                authoritative.get("status", "not_started"),
                round=authoritative.get("round", 0),
                max_rounds=authoritative.get("max_rounds", 3),
                verification_stop_reason=authoritative.get("stop_reason"),
            )
    finally:
        conn.close()


def _authoritative_row(db: Path, plan_id: str) -> Optional[dict[str, Any]]:
    """Read the ``plan_verification`` row for ``plan_id``."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db)
    try:
        migrate(conn)
        return VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TDD spec 1: the CLI is dry-run by default
# ---------------------------------------------------------------------------


def test_the_cli_is_dry_run_by_default(
    state_db: Path, capsys: pytest.CaptureFixture,
):
    """没有 ``--apply`` → 只报告，不写库。"""
    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "legacy_reason",
        },
    )

    exit_code = main(["--db", str(state_db)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "legacy-stop" in out, f"dry run 必须报告这个计划；got {out!r}"
    assert _authoritative_row(state_db, "legacy-stop") is None, (
        "dry run 不得写库"
    )


# ---------------------------------------------------------------------------
# TDD spec 2: --apply carries ONLY the stop reason
# ---------------------------------------------------------------------------


def test_the_migration_carries_only_the_stop_reason(state_db: Path):
    """``--apply`` 把停止理由搬进权威行，其余三键不搬。"""
    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            # The mirror's status is deliberately NOT the one the phase
            # implies, so the assertion below can tell where the status
            # came from: the mirror column, the plan's phase, or the
            # documented default.
            "status": "passed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "legacy_reason",
        },
    )

    report = migrate_legacy_verification_stop_reasons(state_db, apply=True)

    assert [entry["plan_id"] for entry in report["migrated"]] == ["legacy-stop"]
    row = _authoritative_row(state_db, "legacy-stop")
    assert row is not None, "迁移必须建出权威行"
    assert row["verification_stop_reason"] == "legacy_reason", (
        "停止理由必须搬过来"
    )
    # The mirror's status / round / max_rounds are NOT a source (arch
    # decision point 3): the row gets the documented defaults instead.
    # The phase is not consulted either — the routing phase would be a
    # second source of the verification status.
    assert row["verification_status"] == "not_started", (
        "状态取文档化默认值（not_started），不是镜像列的 passed，"
        "也不是相位蕴含的状态"
    )
    assert row["round"] == 0, "镜像的 round 不得被搬过来"
    assert row["max_rounds"] == 4, "镜像的 max_rounds 不得被搬过来"


def test_the_migration_does_not_derive_the_status_from_the_phase(
    state_db: Path,
):
    """相位蕴含验证状态时，状态仍然取文档化默认值。

    这里镜像列的 status 与相位蕴含的状态**相同**（都是 ``failed``），
    所以「不是镜像列的值」这条断言证明不了什么 —— 只有「不是相位的值」
    才排除得掉第二个来源。

    相位是路由数据；拿它推验证状态，等于让路由相位变成验证状态的第二个源
    —— 那正是架构决策点 3 要退役的东西。迁移只搬理由。
    """
    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 9,
            "max_rounds": 9,
            "stop_reason": "legacy_reason",
        },
    )

    migrate_legacy_verification_stop_reasons(state_db, apply=True)

    row = _authoritative_row(state_db, "legacy-stop")
    assert row is not None, "迁移必须建出权威行"
    assert row["verification_status"] == "not_started", (
        "相位蕴含 failed 也不行 —— 状态取文档化默认值（not_started）"
    )
    assert row["verification_stop_reason"] == "legacy_reason", (
        "停止理由必须搬过来"
    )


def test_the_migration_lands_in_the_authoritative_table(state_db: Path):
    """迁移只写 ``plan_verification`` —— 路由行逐字节不变。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "legacy_reason",
        },
    )
    conn = open_db(state_db)
    try:
        migrate(conn)
        before = RoutingRepository(conn).current("legacy-stop")
    finally:
        conn.close()

    migrate_legacy_verification_stop_reasons(state_db, apply=True)

    conn = open_db(state_db)
    try:
        after = RoutingRepository(conn).current("legacy-stop")
    finally:
        conn.close()
    assert after == before, "迁移不得改写路由行"


# ---------------------------------------------------------------------------
# TDD spec 3: idempotent, and plans that must not be touched
# ---------------------------------------------------------------------------


def test_the_migration_is_idempotent(state_db: Path):
    """跑两遍：第二遍什么也不做，权威行逐字段不变。"""
    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "legacy_reason",
        },
    )

    first = migrate_legacy_verification_stop_reasons(state_db, apply=True)
    row_after_first = _authoritative_row(state_db, "legacy-stop")
    second = migrate_legacy_verification_stop_reasons(state_db, apply=True)
    row_after_second = _authoritative_row(state_db, "legacy-stop")

    assert len(first["migrated"]) == 1
    assert second["migrated"] == [], (
        "第二遍不得再迁移一次；got "
        f"{second['migrated']!r}"
    )
    assert row_after_second == row_after_first, (
        "第二遍不得改写权威行"
    )


def test_the_migration_does_not_overwrite_an_existing_row(state_db: Path):
    """已有权威行的计划不动 —— 那是更新的记录。"""
    _seed_plan(
        state_db,
        "has-authoritative-row",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "mirror_reason",
        },
        authoritative={
            "status": "passed",
            "round": 2,
            "max_rounds": 3,
            "stop_reason": "authoritative_reason",
        },
    )

    report = migrate_legacy_verification_stop_reasons(state_db, apply=True)

    assert report["migrated"] == []
    assert "has-authoritative-row" in [
        entry["plan_id"] for entry in report["skipped_existing_row"]
    ]
    row = _authoritative_row(state_db, "has-authoritative-row")
    assert row is not None
    assert row["verification_stop_reason"] == "authoritative_reason", (
        "已有权威行的计划不得被镜像的理由覆盖"
    )
    assert row["verification_status"] == "passed"
    assert row["round"] == 2


def test_a_plan_without_a_mirror_stop_reason_is_left_alone(state_db: Path):
    """镜像列里没有停止理由 → 没有可搬的东西 → 不建行。"""
    _seed_plan(
        state_db,
        "no-reason",
        phase="verification",
        mirror={"status": "pending", "round": 0, "max_rounds": 3},
    )

    report = migrate_legacy_verification_stop_reasons(state_db, apply=True)

    assert report["migrated"] == []
    assert _authoritative_row(state_db, "no-reason") is None, (
        "没有停止理由就没有理由可搬，不得凭空建行"
    )


# ---------------------------------------------------------------------------
# TDD spec 5: the CLI's --apply path writes and leaves a pre-write backup
# ---------------------------------------------------------------------------


def test_the_cli_apply_writes_the_row_and_leaves_a_pre_write_backup(
    state_db: Path, capsys: pytest.CaptureFixture,
):
    """``--apply`` 真的写库，并且在写之前留一份 ``.bak``。

    备份是操作员唯一的退路，所以它必须是**写之前**的快照：备份里还
    没有迁移出来的行，原始库里才有。
    """
    from state_machine.db.connection import open as open_db

    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "legacy_reason",
        },
    )

    exit_code = main(["--db", str(state_db), "--apply"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "migrated: plan=legacy-stop" in out, f"CLI 必须报告搬了什么；got {out!r}"

    row = _authoritative_row(state_db, "legacy-stop")
    assert row is not None, "--apply 必须建出权威行"
    assert row["verification_stop_reason"] == "legacy_reason", "停止理由必须搬过来"

    backup = state_db.with_suffix(state_db.suffix + ".bak")
    assert backup.exists(), f"写之前必须留一份备份；缺 {backup}"
    conn = open_db(backup)
    try:
        rows_in_backup = conn.execute(
            "SELECT COUNT(*) FROM plan_verification"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows_in_backup == 0, "备份必须是写之前的快照，不能是迁移之后的样子"


# ---------------------------------------------------------------------------
# TDD spec 6: a failing plan is reported and the run continues
# ---------------------------------------------------------------------------


def test_a_failing_plan_is_reported_and_the_run_continues(
    state_db: Path, monkeypatch: pytest.MonkeyPatch,
):
    """一个计划写失败 → 记进 failed → 后面的计划照样搬。

    这是 docstring 里「a failure on one plan is reported and the run
    continues with the next one」那条契约的钉子：失败的计划不得留下半行，
    也不得让整轮迁移中止 —— 一轮迁移里有一个坏计划就整轮白跑，是操作员
    最容易踩的坑。
    """
    import sqlite3

    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    _seed_plan(
        state_db,
        "plan-a",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "reason_a",
        },
    )
    _seed_plan(
        state_db,
        "plan-b",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "reason_b",
        },
    )

    original_insert = VerificationRepository.insert

    def flaky_insert(self, plan_id, verification_status, **fields):
        if plan_id == "plan-a":
            raise sqlite3.OperationalError("simulated write failure")
        return original_insert(self, plan_id, verification_status, **fields)

    monkeypatch.setattr(VerificationRepository, "insert", flaky_insert)

    report = migrate_legacy_verification_stop_reasons(state_db, apply=True)

    assert [entry["plan_id"] for entry in report["failed"]] == ["plan-a"], (
        "写失败的计划必须被记进 failed；got "
        f"{report['failed']!r}"
    )
    assert [entry["plan_id"] for entry in report["migrated"]] == ["plan-b"], (
        "一个计划写失败不得让整轮迁移中止；got "
        f"{report['migrated']!r}"
    )
    assert _authoritative_row(state_db, "plan-a") is None, (
        "写失败的计划不得留下半行"
    )
    row = _authoritative_row(state_db, "plan-b")
    assert row is not None, "后面的计划必须照样搬完"
    assert row["verification_stop_reason"] == "reason_b"


# ---------------------------------------------------------------------------
# the classification is read-only
# ---------------------------------------------------------------------------


def test_the_classification_reads_nothing_and_writes_nothing(state_db: Path):
    """分类这一步是纯读：调用前后权威表一模一样。"""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    _seed_plan(
        state_db,
        "legacy-stop",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "legacy_reason",
        },
    )
    conn = open_db(state_db)
    try:
        migrate(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM plan_verification"
        ).fetchone()[0]
        plan = classify_legacy_stop_reasons(conn)
        after = conn.execute(
            "SELECT COUNT(*) FROM plan_verification"
        ).fetchone()[0]
    finally:
        conn.close()

    assert before == 0
    assert after == 0, "分类不得建行"
    assert [entry["plan_id"] for entry in plan["migrate"]] == ["legacy-stop"]
    assert plan["migrate"][0]["stop_reason"] == "legacy_reason"


# ---------------------------------------------------------------------------
# TDD spec 7: the guard lives in the migration itself, not only in the caller
# ---------------------------------------------------------------------------
#
# 脚本的分类（``classify_legacy_stop_reasons``）把已有权威行的计划归进
# ``skipped_existing_row``，所以脚本这一条路不会覆盖既有行。但守卫放在
# **调用方**而不是**被调方**时，任何第二个调用者（测试、未来的迁移脚本）
# 都能绕过它 —— 迁移助手自己必须守这条线。
#
# 存在性判据是裸 SQL ``SELECT 1``：``plan_verification`` 的 JSON 列逐列
# ``json.loads``，一个坏 blob 就让整行读不出来 —— 「行存在但读不出来」
# 仍然是「行存在」。


def _mirror_routing_row(plan_id: str, stop_reason: str) -> dict:
    """一条带着停止理由的路由行（镜像列的内容）。"""
    return {
        "plan_id": plan_id,
        "current_phase": "verification_failed",
        "verification": json.dumps({"stop_reason": stop_reason}),
    }


def _raw_lifecycle(db: Path, plan_id: str) -> Optional[tuple]:
    """读权威行的四个生命周期列 —— 不解码任何 JSON 列。"""
    from state_machine.db.connection import open as open_db

    conn = open_db(db)
    try:
        return conn.execute(
            "SELECT verification_status, round, max_rounds, "
            "verification_stop_reason FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()


def test_the_migration_helper_does_not_overwrite_an_existing_row(state_db: Path):
    """直接调用迁移助手时，既有权威行也不许被覆盖。"""
    from state_machine.db.connection import open as open_db
    from plan_state import migrate_mirror_stop_reason

    _seed_plan(
        state_db,
        "has-authoritative-row",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "mirror_reason",
        },
        authoritative={
            "status": "passed",
            "round": 2,
            "max_rounds": 3,
            "stop_reason": "authoritative_reason",
        },
    )

    conn = open_db(state_db)
    try:
        row = migrate_mirror_stop_reason(
            "has-authoritative-row",
            _mirror_routing_row("has-authoritative-row", "mirror_reason"),
            conn=conn,
        )
    finally:
        conn.close()

    assert row is not None, "既有权威行必须原样返回"
    assert row["verification_status"] == "passed"
    assert row["verification_stop_reason"] == "authoritative_reason"
    assert _raw_lifecycle(state_db, "has-authoritative-row") == (
        "passed",
        2,
        3,
        "authoritative_reason",
    ), "迁移助手不得覆盖既有权威行"


def test_the_migration_helper_does_not_overwrite_an_unreadable_row(
    state_db: Path,
):
    """既有权威行解不开时，迁移助手也不许写 —— 存在性判据不许被解码带偏。"""
    from state_machine.db.connection import open as open_db
    from plan_state import migrate_mirror_stop_reason

    _seed_plan(
        state_db,
        "unreadable-authoritative-row",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "mirror_reason",
        },
        authoritative={
            "status": "passed",
            "round": 2,
            "max_rounds": 3,
            "stop_reason": "authoritative_reason",
        },
    )

    conn = open_db(state_db)
    try:
        conn.execute(
            "UPDATE plan_verification SET results = 'not-json' WHERE plan_id = ?",
            ("unreadable-authoritative-row",),
        )
        conn.commit()
        migrate_mirror_stop_reason(
            "unreadable-authoritative-row",
            _mirror_routing_row(
                "unreadable-authoritative-row", "mirror_reason"
            ),
            conn=conn,
        )
    finally:
        conn.close()

    assert _raw_lifecycle(state_db, "unreadable-authoritative-row") == (
        "passed",
        2,
        3,
        "authoritative_reason",
    ), "既有权威行（哪怕解不开）不得被覆盖"


def test_the_migration_helper_still_seeds_a_plan_without_a_row(state_db: Path):
    """守卫不得把补种也挡掉 —— 没有权威行的计划照旧被补种。

    守卫的另一半：它只拦「已有行」，不拦「补种」。否则迁移会变成永远
    什么都不做的空转。
    """
    from state_machine.db.connection import open as open_db
    from plan_state import migrate_mirror_stop_reason

    _seed_plan(
        state_db,
        "no-authoritative-row",
        phase="verification_failed",
        mirror={
            "status": "failed",
            "round": 4,
            "max_rounds": 4,
            "stop_reason": "mirror_reason",
        },
    )

    conn = open_db(state_db)
    try:
        row = migrate_mirror_stop_reason(
            "no-authoritative-row",
            _mirror_routing_row("no-authoritative-row", "mirror_reason"),
            conn=conn,
        )
    finally:
        conn.close()

    assert row is not None, "没有权威行的计划必须被补种"
    assert _raw_lifecycle(state_db, "no-authoritative-row") == (
        "not_started",
        0,
        4,
        "mirror_reason",
    ), "补种必须写入文档化默认值 + 镜像的停止理由"
