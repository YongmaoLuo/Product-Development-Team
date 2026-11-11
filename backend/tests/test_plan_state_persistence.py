import json
import os
import threading
from pathlib import Path

import pytest

from plan_state import PlanState


def read_plan_routing_state(plan_id, db_path):
    """Read the legacy ``PlanState`` shape for ``plan_id`` from the
    ``plan_routing`` SQLite row at ``db_path``. (Task #3.7: persistence
    moved off ``plan_state.json`` onto the ``plan_routing`` table.)
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_db(db_path)
    try:
        migrate(conn)
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    if row is None:
        return None
    return PlanState._sqlite_row_to_state(row)


def count_routing_rows(plan_id, db_path):
    """How many ``plan_routing`` rows exist for ``plan_id``.

    The raw ``COUNT(*)`` — not the repository accessor — because the
    question these tests ask is "does a row EXIST", not "what does the
    row say".  A read that has to go through the repository would
    already be the thing under test.
    """
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate

    conn = _open_db(str(db_path))
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


class TestAtomicWrite:
    def test_atomic_write_fields_complete(self, sample_plan_factory, state_db):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        ps.transition_to("executing")

        data = read_plan_routing_state(plan_dir.name, state_db)
        assert data["plan_id"] == plan_dir.name
        assert data["current_phase"] == "executing"
        assert "completed_phases" in data
        assert "review_rounds" in data
        assert "flags" in data
        assert "last_updated" in data

    def test_no_tmp_residue(self, sample_plan_factory, state_db):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        ps.transition_to("executing")

        tmp_files = list(plan_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_legacy_file_not_rewritten(self, sample_plan_factory, state_db):
        """After a transition, the legacy ``plan_state.json`` (if present)
        is NOT updated — SQLite is now the single source of truth.
        """
        plan_dir = sample_plan_factory(phase="ready")
        original_content = (plan_dir / "plan_state.json").read_text(encoding="utf-8")

        ps = PlanState(plan_dir)
        ps.transition_to("executing")

        # The on-disk legacy file is untouched (still says "ready")...
        assert (plan_dir / "plan_state.json").read_text(encoding="utf-8") == original_content
        # ...while SQLite carries the new phase.
        data = read_plan_routing_state(plan_dir.name, state_db)
        assert data["current_phase"] == "executing"

    def test_no_tmp_residue_after_write(self, sample_plan_factory, state_db):
        """No ``*.tmp`` residue is left behind by the SQLite write path."""
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)
        ps.transition_to("executing")
        assert list(plan_dir.glob("*.tmp")) == []


class TestConcurrency:
    def test_concurrent_same_plan(self, sample_plan_factory, state_db):
        plan_dir = sample_plan_factory(phase="interview")
        ps = PlanState(plan_dir)
        errors = []

        def worker():
            try:
                ps.transition_to("interview_complete")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        data = read_plan_routing_state(plan_dir.name, state_db)
        assert data["current_phase"] == "interview_complete"
        assert isinstance(data, dict)

    def test_lock_independence(self, sample_plan_factory, tmp_path, state_db):
        plan_dir_a = sample_plan_factory(plan_id="plan-a", phase="ready")
        plan_dir_b = sample_plan_factory(plan_id="plan-b", phase="ready")

        ps_a = PlanState(plan_dir_a)
        ps_b = PlanState(plan_dir_b)

        barrier = threading.Barrier(2)

        def worker_a():
            barrier.wait()
            for _ in range(20):
                ps_a.transition_to("executing")
                ps_a.transition_to("ready")

        def worker_b():
            barrier.wait()
            for _ in range(20):
                ps_b.transition_to("executing")
                ps_b.transition_to("ready")

        t_a = threading.Thread(target=worker_a)
        t_b = threading.Thread(target=worker_b)
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        data_a = read_plan_routing_state("plan-a", state_db)
        data_b = read_plan_routing_state("plan-b", state_db)
        # 2026-10-26 (R10): 断言的允许集合跟着相位词汇一起更新。
        #
        # worker 循环的最后一步是 ``transition_to("ready")``，而 R10 之后
        # ``transition_to`` 在合法性检查**之前**把 ``ready`` 归一化成
        # ``queued``（``phase_vocabulary.normalize_waiting_phase``）——
        # ``ready`` 只是输入别名，写路径不再产生它。于是每轮循环的落点是
        # ``queued``，断言里留着 ``ready`` 就是钉旧词汇，重跑永远不会转绿。
        #
        # 允许集合保留两个值：``executing`` 也是合法落点（并发下若最后一个
        # 动作被调度截断，停在 ``executing`` 同样成立）。用例要证的仍然是
        # 「两个计划的写入互不串扰」，所以两个 plan 各自的相位都要落在
        # 合法集合里。
        assert data_a["current_phase"] in ("queued", "executing")
        assert data_b["current_phase"] in ("queued", "executing")
        assert isinstance(data_a, dict)
        assert isinstance(data_b, dict)


class TestPhaseSurvivesMetadataWrites:
    """2026-09-15 — a CAS'd phase must not be clobbered by a
    metadata-only PlanState write.

    ``_save_state_to_sqlite`` re-derived the routing value from
    ``current_phase`` on every upsert. The verification layer CASes the
    phase independently (``RoutingRepository.try_mark_phase``), so any
    later ``set_verification_*`` write dragged the live value back to the
    stale one.

    Live symptom: ``POST /api/verification/{id}/reset_rounds`` ends
    with ``set_verification_max_rounds`` → the routing value snapped from
    ``verification`` back to ``verification_running`` → the very
    next ``/start`` failed with ``409 stage_mismatch``. Reproduced twice
    on a production plan before the cause was pinned.

    2026-09-17 (schema v5): the two columns are one, and the fix is
    structural rather than a special case — ``write_plan_state`` uses
    ``ON CONFLICT DO UPDATE`` over an explicit column list, so it
    cannot touch a column it does not name. The test is kept because
    the *behaviour* (a metadata write must not move the phase) is
    still the contract.
    """

    def _routing_row(self, plan_id, db_path):
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = _open_db(str(db_path))
        try:
            migrate(conn)
            return RoutingRepository(conn).current(plan_id)
        finally:
            conn.close()

    def _cas_phase(self, plan_id, db_path, from_phase, to_phase):
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = _open_db(str(db_path))
        try:
            migrate(conn)
            RoutingRepository(conn).try_mark_phase(
                plan_id, (from_phase,), to_phase,
            )
        finally:
            conn.close()

    def test_metadata_write_preserves_the_casd_phase(
        self, sample_plan_factory, state_db,
    ):
        plan_dir = sample_plan_factory(phase="verification_running")
        ps = PlanState(plan_dir)
        # 2026-11-11: 构造不再建行，所以这里必须**显式**写一次状态
        # 把 routing 行建出来（行由第一次真正的写创建）。用显式写而不是
        # 构造副作用，是这个用例的新前提。
        ps._save_state()
        # The verification layer parks the plan while an operator resets.
        self._cas_phase(
            plan_dir.name, state_db, "verification_running",
            "verification",
        )

        ps.set_verification_max_rounds(4)

        row = self._routing_row(plan_dir.name, state_db)
        assert row["current_phase"] == "verification", (
            "the CAS'd phase must survive a metadata-only PlanState write; "
            f"got {row['current_phase']!r}"
        )

    def test_a_real_phase_transition_still_moves_the_value(
        self, sample_plan_factory, state_db,
    ):
        plan_dir = sample_plan_factory(phase="ready")
        ps = PlanState(plan_dir)

        ps.transition_to("executing")

        row = self._routing_row(plan_dir.name, state_db)
        assert row["current_phase"] == "executing", (
            f"a real phase change must still drive the value, got "
            f"{row['current_phase']!r}"
        )


class TestResetToTrimsTheHistory:
    """``reset_to`` is the ONE write that may drop recorded history.

    Every other ``PlanState`` write MERGES ``completed_phases`` — a
    stale snapshot may only add the phases it knows about, never
    delete the ones somebody else recorded (the routing CAS maintains
    the very same ledger).

    Rewinding the plan is the deliberate exception: "reset to phase
    X" means "forget everything after X".  That is only possible if
    the write REPLACES the column — under the merge the trimmed
    entries would be merged straight back in and the plan would keep
    a history it just asked to discard.
    """

    def test_reset_to_trims_the_recorded_history(
        self, sample_plan_factory, state_db,
    ):
        plan_dir = sample_plan_factory(phase="prd_review")
        ps = PlanState(plan_dir)
        ps.mark_phase_complete("interview")
        ps.mark_phase_complete("prd_generation")

        before = read_plan_routing_state(plan_dir.name, state_db)
        assert before["completed_phases"] == ["interview", "prd_generation"], (
            f"precondition: the history must be on the row; got "
            f"{before['completed_phases']!r}"
        )

        ps.reset_to("prd_generation")

        after = read_plan_routing_state(plan_dir.name, state_db)
        assert after["completed_phases"] == ["interview"], (
            "reset_to must TRIM the history (replace), not merge the "
            "trimmed entries back in; got "
            f"{after['completed_phases']!r}"
        )
        assert after["current_phase"] == "prd_generation"


def _make_plan_dir(tmp_path, plan_id="some_plan", legacy_phase=None):
    """Materialise a plan directory, optionally with a legacy file.

    ``legacy_phase`` writes the pre-SQLite ``plan_state.json`` the
    read fallback is there for.
    """
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    if legacy_phase is not None:
        (plan_dir / "plan_state.json").write_text(
            json.dumps({
                "plan_id": plan_id,
                "current_phase": legacy_phase,
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {},
            }),
            encoding="utf-8",
        )
    return plan_dir


class TestConstructorDoesNotWrite:
    """构造 ``PlanState`` 不得写库。

    2026-11-11：``PlanState.__init__`` 以前会做一次「一次性迁移」，
    把 legacy/default 状态 INSERT 进 ``plan_routing``。于是**只读**
    用法（``get_current_phase()`` / ``get_state()``）也会留下一个
    计划行；``VerificationOrchestrator.__init__`` 构造一次
    ``PlanState``，所以每跑一个 probe 就登记一个 plan。

    现在：构造是纯读，行由**第一次真正的写状态**创建。
    """

    def _plan_dir(self, tmp_path, plan_id="some_plan", legacy_phase=None):
        return _make_plan_dir(
            tmp_path, plan_id=plan_id, legacy_phase=legacy_phase,
        )

    def test_constructing_plan_state_does_not_write_a_row(
        self, tmp_path, state_db,
    ):
        plan_dir = self._plan_dir(tmp_path, "some_plan")

        PlanState(plan_dir)

        assert count_routing_rows("some_plan", state_db) == 0, (
            "constructing a PlanState must not create a routing row"
        )

    def test_first_save_creates_the_row(self, tmp_path, state_db):
        """第一次真正的写状态 → 行出现，且相位正确。"""
        plan_dir = self._plan_dir(tmp_path, "some_plan")

        ps = PlanState(plan_dir)
        assert count_routing_rows("some_plan", state_db) == 0

        ps.set_verification_max_rounds(4)

        assert count_routing_rows("some_plan", state_db) == 1, (
            "the first real write must create the routing row"
        )
        row = read_plan_routing_state("some_plan", state_db)
        assert row["current_phase"] == "interview", (
            "a fresh plan's row must carry the phase we hold; got "
            f"{row['current_phase']!r}"
        )
        assert row["verification"]["max_rounds"] == 4

    def test_first_save_carries_the_phase_read_from_the_legacy_file(
        self, tmp_path, state_db,
    ):
        """行由第一次写创建时，相位必须是**读到**的那个相位。

        没有 routing 行、只有 ``plan_state.json`` 的老计划：
        构造函数读到 ``prd_review``；第一次写建行时，行必须也是
        ``prd_review``，不能掉回列的 DEFAULT（``'interview'``）——
        那会让 routing 行和状态自相矛盾。
        """
        plan_dir = self._plan_dir(
            tmp_path, "legacy_plan", legacy_phase="prd_review",
        )

        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "prd_review"

        ps.set_verification_max_rounds(5)

        row = read_plan_routing_state("legacy_plan", state_db)
        assert row is not None
        assert row["current_phase"] == "prd_review", (
            "the row created by the first write must carry the phase "
            f"read from the legacy file; got {row['current_phase']!r}"
        )

    def test_legacy_file_is_still_read_when_no_row_exists(
        self, tmp_path, state_db,
    ):
        """读侧回退保留：只有旧文件、没有行时，相位来自文件，且不建行。"""
        plan_dir = self._plan_dir(
            tmp_path, "legacy_plan", legacy_phase="prd_review",
        )

        ps = PlanState(plan_dir)

        assert ps.get_current_phase() == "prd_review"
        assert count_routing_rows("legacy_plan", state_db) == 0, (
            "reading the legacy file must not create a routing row"
        )

    def test_a_read_only_accessor_never_writes(self, tmp_path, state_db):
        """``get_state()`` / ``get_current_phase()`` 各调一次 → 行数不变。"""
        plan_dir = self._plan_dir(tmp_path, "some_plan")

        ps = PlanState(plan_dir)
        before = count_routing_rows("some_plan", state_db)

        ps.get_state()
        ps.get_current_phase()

        assert count_routing_rows("some_plan", state_db) == before == 0

    def test_a_read_only_accessor_does_not_touch_an_existing_row(
        self, tmp_path, state_db,
    ):
        """行已经存在时，只读访问也不该动它（连写都不该发生）。"""
        plan_dir = self._plan_dir(tmp_path, "some_plan")

        ps = PlanState(plan_dir)
        ps.set_verification_max_rounds(4)
        before = count_routing_rows("some_plan", state_db)
        assert before == 1

        ps2 = PlanState(plan_dir)
        ps2.get_state()
        ps2.get_current_phase()

        assert count_routing_rows("some_plan", state_db) == before == 1


class TestEnsureRowIsExplicit:
    """``ensure_row()`` 是**显式**的建行动作。

    构造不再写库，所以「现在就要这一行存在」的地方必须自己说出来。
    生产上真有两个这样的调用点（2026-11-11）：

    * ``backend/verification_loop.py`` —— 自动验证循环在动 routing
      相位之前，先让行存在（否则 CAS 撞上 ``PlanNotFoundError``，
      相位历史就没人维护）；
    * ``backend/routes/verification.py`` —— ``POST /start`` 的 CAS 同理。

    这里钉住的是这个方法的**契约**：建行、带正确的相位、不动已存在
    行的相位。
    """

    def test_ensure_row_creates_the_row_with_the_state_it_holds(
        self, tmp_path, state_db,
    ):
        plan_dir = _make_plan_dir(
            tmp_path, "some_plan", legacy_phase="prd_review",
        )

        ps = PlanState(plan_dir)
        assert count_routing_rows("some_plan", state_db) == 0

        ps.ensure_row()

        assert count_routing_rows("some_plan", state_db) == 1
        row = read_plan_routing_state("some_plan", state_db)
        assert row["current_phase"] == "prd_review"

    def test_ensure_row_does_not_move_an_existing_phase(
        self, tmp_path, state_db,
    ):
        """行已在、相位已被别人 CAS 动过 → ``ensure_row()`` 不能把它拖回去。"""
        plan_dir = _make_plan_dir(
            tmp_path, "some_plan", legacy_phase="prd_review",
        )

        ps = PlanState(plan_dir)
        ps.ensure_row()

        # 另一个写者把相位推到了 verifying。
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.routing_repository import (
            RoutingRepository,
        )

        conn = _open_db(str(state_db))
        try:
            migrate(conn)
            RoutingRepository(conn).try_mark_phase(
                "some_plan", ("prd_review",), "verifying",
            )
        finally:
            conn.close()

        ps.ensure_row()

        row = read_plan_routing_state("some_plan", state_db)
        assert row["current_phase"] == "verifying", (
            "ensure_row() must not restate a cached phase; got "
            f"{row['current_phase']!r}"
        )

