"""Split-parent lifecycle: ``superseded`` follows children, ``voided`` cascades.

2026-10-16. Two statuses that used to be one label:

  * ``superseded`` — "this row stopped running because its work was handed
    to breakdown children". ``is_dependency_ready`` FOLLOWS that pointer
    and judges the children instead.
  * ``voided`` — "the premise was revoked". ``void_task`` cascades it
    down the whole subtree.

Why these tests drive the real agent
------------------------------------
The bug being fixed is not a wrong label in isolation — it is a deadlock.
A parent is split, the parent stays in the plan as ``failed``, and every
task that named it in ``depends_on`` blocks forever. Asserting on
``TaskManager.record_task_superseded`` alone would pass while the deadlock
still existed, so the headline test (:func:`test_downstream_runs_after_parent_split`)
takes the whole trip: it drives ``_execute_task_with_retry`` (the
production entry for the failure tail) with a refiner that splits the
task, then feeds the resulting plan into the real dispatcher gate.

Every test here reaches production code from a production entry. Where a
helper is asserted directly it is because the helper IS the contract
(``void_task``'s completed-descendant rule), and in each case a sibling
test covers the same rule through the agent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH")
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main"],
        cwd=str(project_dir), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir), check=True, capture_output=True, text=True,
    )
    (project_dir / "README.md").write_text("# baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=str(project_dir), check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=str(project_dir),
        check=True, capture_output=True,
    )


@pytest.fixture
def project_dir(tmp_path):
    # Named like a plan id on purpose: ``TaskManager`` derives its plan_id
    # from ``tasks_file.parent.name`` and refuses reserved / too-short
    # names ("project" is reserved), which would skip the SQLite mirror.
    pd = tmp_path / "plan-splitlife"
    pd.mkdir()
    _git_init(pd)
    return pd


class _FailingCodingTool:
    """A coder that always reports a failed test run.

    ``_execute_task_with_retry`` treats a ``TEST_RESULT: FAILED`` response
    as a retryable failure, so with ``max_retries=2`` this drives the tail
    to ``_refine_after_failure`` exactly as production does.
    """

    def __init__(self):
        self.calls: list = []

    def query(self, prompt, system_instruction=None, timeout=None, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return (
            "I tried but could not make the test pass.\n\n"
            "FILE: src/example.py\n"
            "```python\n"
            "def hello():\n    return 'still broken'\n```\n\n"
            "TEST_RESULT: FAILED\n"
            "REASON: assertion failed\n"
        )

    def query_json(self, prompt, system_instruction=None):
        raise AssertionError("query_json must not be reached on this path")


class _SplittingRefiner:
    """Refiner stub that splits the failed task into two children.

    The parent's own entry is ECHOED BACK alongside the children, and that
    detail is load-bearing rather than incidental.

    A refiner has two ways to express a split and they have opposite
    consequences:

    * **Echo the parent** — ``[1, 1-1, 1-2, 2]``. ``plan_refiner_structure``
      keeps the parent (it is not in ``removed_ids``), so the plan ends up
      holding the parent AND its children side by side. This is the shape
      ``refiner.py:430-438`` goes out of its way to support ("a full-table
      echo that also splits X omits X from its own reply -- that is what
      'replace X with X-1/X-2' looks like when the whole list comes
      back"), and it is an ordinary thing for an LLM to return. **This is
      the world the reported bug lives in**: the parent is still in the
      DAG, so the failure tail writes a terminal status onto it, and before
      2026-10-16 that status was always ``failed`` -- a red card entry for
      a task whose work had demonstrably been handed to its children.

    * **Drop the parent** — ``[1-1, 1-2, 2]``. The parent leaves both
      stores, and any downstream task still naming it now has a dangling
      dependency, which ``set_tasks`` rejects as
      ``DanglingTaskDependency`` — rejecting the WHOLE refinement, children
      included. Not modelled here because there is no split left to record;
      see the uncertainty note in the report.
    """

    def __init__(self, children=("1-1", "1-2")):
        self.children = list(children)
        self.calls = 0

    def refine(self, requirement=None, tasks=None, last_coder_response=None,
               last_result=None, file_context=None, exit_code=None,
               last_task_id=None, **kwargs):
        self.calls += 1
        out = []
        for t in tasks or []:
            if str(t.get("id")) == str(last_task_id):
                for cid in self.children:
                    out.append({
                        "id": cid, "title": f"child {cid}",
                        "description": "split out", "status": "pending",
                        "test_command": "pytest -q", "files_to_modify": [],
                        "depends_on": [], "model_type": None,
                    })
                # The parent's own row comes back too -- see the class
                # docstring. This is the shape that leaves the parent in
                # the DAG beside its children.
                out.append(dict(t))
                continue
            out.append(t)
        return out


def _build_agent(project_dir: Path, coding_tool, refiner=None, tasks=None):
    """Build the agent and seed ITS OWN task manager.

    The tasks must go through ``agent.task_manager``: the refine path ends
    in ``_apply_refiner_structure`` -> write tasks.json -> ``_load_tasks``
    re-read into the agent's ``TaskManager``. A second, separately-built
    one is a stale snapshot that never sees the children, which is how a
    test can look like it proved the split when it proved nothing.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent(
        requirement="split lifecycle fixture",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=None,
    )
    agent._session_task_completed_counts = {}
    if refiner is not None:
        agent.refiner = refiner
    if tasks is not None:
        agent.task_manager.set_tasks(tasks)
    return agent


def _make_manager(project_dir: Path, task_dicts: list):
    """A standalone TaskManager — for the pure ``void_task`` contract
    tests, which do not go through the agent."""
    from task_manager import TaskManager

    manager = TaskManager(project_dir)
    manager.set_tasks(task_dicts)
    return manager


def _t(tid, status="pending", depends_on=None, **kw):
    d = {
        "id": tid, "title": f"task {tid}", "description": "d",
        "test_command": "pytest -q", "status": status,
        "depends_on": depends_on or [], "files_to_modify": [],
    }
    d.update(kw)
    return d


# ===========================================================================
# (1)  THE HEADLINE: a split parent does not block its downstream.
# ===========================================================================


def test_downstream_runs_after_parent_split(project_dir):
    """Acceptance 1 (the load-bearing one): downstream whose
    ``depends_on`` STILL NAMES THE PARENT must be schedulable after the
    parent is split.

    This is the version that proves the RULE rather than the label, and
    the rule has to be proven on the shape that actually keeps pointing at
    the parent.

    Why non-numeric child ids
    ------------------------
    ``Refiner._rewrite_split_depends_on`` normally heals a downstream that
    still names a split parent: it rewrites ``depends_on`` onto the
    children. But it builds its parent map only from children whose id
    ends in ``-<digit>`` (``refiner.py``: ``if not suffix.isdigit() ...
    continue``). So with numeric children (``1-1``/``1-2``) the self-heal
    fires and the dep is rewritten, and the parent-pointing case never
    reaches the resolver at all. With a non-numeric child (``1-a``) the
    self-heal does NOT fire, the downstream keeps ``depends_on=["1"]``,
    and that reference reaches ``is_dependency_ready`` with the parent
    now terminal. On ``pdt/final`` that returns
    ``(False, 'upstream_not_ready:1')`` and the plan deadlocks for good:
    the parent is terminal, so no dispatch can ever change the answer.

    This test therefore drives that exact shape end to end — real failure
    tail, real refiner application, real ``_load_tasks``, real resolver.
    """
    from agent import is_dependency_ready

    coding_tool = _FailingCodingTool()
    refiner = _SplittingRefiner(children=("1-a", "1-b"))
    agent = _build_agent(
        project_dir, coding_tool, refiner,
        tasks=[
            _t("1"),                    # the task that will fail and split
            _t("2", depends_on=["1"]),  # downstream -- names the PARENT
        ],
    )
    manager = agent.task_manager

    # Drive the real failure tail. max_retries=2 is the configured value.
    task = next(t for t in manager.tasks if t.id == "1")
    ok = agent._execute_task_with_retry(task, max_retries=2)
    assert ok is False, "the task must have failed -- that is the premise"

    # The split really happened.
    assert refiner.calls == 1, "the failure tail must have delegated to the refiner"
    ids = {t.id for t in manager.tasks}
    assert {"1-a", "1-b"} <= ids, f"refiner must have split task 1, got {sorted(ids)}"

    # The parent is NOT failed any more.
    parent = next(t for t in manager.tasks if t.id == "1")
    assert parent.status == "superseded", (
        f"a split parent must be superseded, not failed -- got {parent.status!r}"
    )

    # And the split children carry a depth, which is what lets the
    # 5-level budget count them at all (the stamp used to require a
    # numeric suffix, so these would have read as depth 0).
    child = next(t for t in manager.tasks if t.id == "1-a")
    assert child.breakdown_count == 1, (
        f"a breakdown child must inherit depth 1, got {child.breakdown_count!r}"
    )

    # THE PRECONDITION of the whole acceptance: the downstream still names
    # the parent. If the self-heal had rewritten it this test would be
    # proving nothing, so it is asserted rather than assumed.
    downstream = next(t for t in manager.tasks if t.id == "2")
    assert list(downstream.depends_on) == ["1"], (
        f"precondition: the downstream must still name the parent, got "
        f"{list(downstream.depends_on)!r}"
    )

    # While the children are unfinished, it blocks -- naming the child.
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready is False and reason == "upstream_pending:1-a", (
        f"an unfinished child must block, naming it; got {reason!r}"
    )

    # THE ACCEPTANCE: once the children are done, the downstream runs --
    # resolved THROUGH the superseded parent it still names.
    for cid in ("1-a", "1-b"):
        next(t for t in manager.tasks if t.id == cid).status = "completed"
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready, (
        f"downstream of a split parent must be schedulable once its "
        f"children are done -- got blocked by {reason!r}. This is the "
        f"deadlock: the parent is terminal and never re-runs, so a "
        f"resolver that cannot follow the superseded pointer stalls the "
        f"plan forever."
    )


def test_downstream_runs_after_a_numeric_split(project_dir):
    """The everyday shape: numeric children, so the load-time self-heal
    rewrites the downstream's dep onto them.

    Kept as its own test because it exercises a different writer
    (``_rewrite_split_depends_on``) than the headline case, and because a
    regression there would be invisible to the headline test -- which uses
    non-numeric ids precisely to bypass it. Together they cover both ways
    a split leaves a downstream referencing the parent.
    """
    from agent import is_dependency_ready

    agent = _build_agent(
        project_dir, _FailingCodingTool(),
        _SplittingRefiner(children=("1-1", "1-2")),
        tasks=[_t("1"), _t("2", depends_on=["1"])],
    )
    manager = agent.task_manager
    task = next(t for t in manager.tasks if t.id == "1")
    agent._execute_task_with_retry(task, max_retries=2)

    parent = next(t for t in manager.tasks if t.id == "1")
    assert parent.status == "superseded"

    downstream = next(t for t in manager.tasks if t.id == "2")
    # The self-heal did its job: the dep now names the children.
    assert set(downstream.depends_on) == {"1-1", "1-2"}, (
        f"the numeric-children self-heal should have rewired the dep, got "
        f"{list(downstream.depends_on)!r}"
    )
    for cid in ("1-1", "1-2"):
        next(t for t in manager.tasks if t.id == cid).status = "completed"
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready, f"blocked by {reason!r}"


def test_downstream_still_blocked_while_a_child_is_pending(project_dir):
    """Negative control for 1: following the pointer must not mean
    "ignore the children". A parent whose children have not all finished
    still blocks, with the child's own token."""
    from agent import is_dependency_ready

    manager = _make_manager(project_dir, [
        _t("1", status="superseded"),
        _t("1-1", status="completed"),
        _t("1-2", status="pending"),
        _t("2", depends_on=["1"]),
    ])
    downstream = next(t for t in manager.tasks if t.id == "2")
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready is False
    assert reason == "upstream_pending:1-2", (
        f"the block must name the child that is not ready, got {reason!r}"
    )


def test_superseded_parent_expands_through_a_split_child(project_dir):
    """A child that was itself split must expand too, or the deadlock
    just moves one level down."""
    from agent import is_dependency_ready

    manager = _make_manager(project_dir, [
        _t("1", status="superseded"),
        _t("1-1", status="superseded"),   # 1-1 was itself split
        _t("1-1-1", status="completed"),
        _t("1-2", status="completed"),
        _t("2", depends_on=["1"]),
    ])
    downstream = next(t for t in manager.tasks if t.id == "2")
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready, f"nested split must resolve to the leaves, blocked by {reason!r}"


def test_superseded_with_no_children_does_not_deadlock(project_dir):
    """A ``superseded`` row with nothing to point at must not fall back
    to the old "not ready" bucket -- that is the deadlock this replaces.

    Its explicit semantic: inert. It blocks nothing.
    """
    from agent import is_dependency_ready

    manager = _make_manager(project_dir, [
        _t("1", status="superseded"),     # no children at all
        _t("2", depends_on=["1"]),
    ])
    downstream = next(t for t in manager.tasks if t.id == "2")
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready is True, (
        f"an inert superseded row must not block -- got {reason!r}"
    )


def test_no_generic_upstream_not_ready_fallback_remains(project_dir):
    """The catch-all is gone. An unknown status fails closed with a token
    that NAMES the status, so a defect cannot masquerade as progress."""
    import agent as agent_mod
    from agent import is_dependency_ready

    manager = _make_manager(project_dir, [
        _t("1", status="some_future_status"),
        _t("2", depends_on=["1"]),
    ])
    downstream = next(t for t in manager.tasks if t.id == "2")
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready is False
    assert reason == "upstream_unknown_status:1:some_future_status", (
        f"an unknown status must name itself, got {reason!r}"
    )
    # And the old generic token is never RETURNED anywhere in the
    # function. The docstring names it (to say it is gone), so this
    # asserts on the return expressions rather than the raw text.
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    body = src.split("def is_dependency_ready", 1)[1].split("\ndef ", 1)[0]
    returns = [
        ln.strip() for ln in body.splitlines()
        if ln.strip().startswith("return False, f")
    ]
    assert returns, "expected the resolver to still return reason tokens"
    for line in returns:
        assert "upstream_not_ready" not in line, (
            f"the generic catch-all must not survive as a reason token: {line}"
        )


# ===========================================================================
# (2)  A split parent is not a failed task.
# ===========================================================================


def test_split_parent_is_absent_from_the_failed_list(project_dir):
    """Acceptance 2: after a split, nothing named the parent is reported
    as failed -- through the plan's own failure detection, not a literal
    scan of the task list."""
    from agent import _is_plan_failed

    agent = _build_agent(
        project_dir, _FailingCodingTool(), _SplittingRefiner(),
        tasks=[_t("1"), _t("2", depends_on=["1"])],
    )
    manager = agent.task_manager

    task = next(t for t in manager.tasks if t.id == "1")
    agent._execute_task_with_retry(task, max_retries=2)

    failed = [t.id for t in manager.tasks if t.status == "failed"]
    assert "1" not in failed, (
        f"the split parent must not read as failed; failed={failed}"
    )
    assert not _is_plan_failed(manager.tasks), (
        "a plan whose only failure was a split parent must not report "
        "plan-level failure"
    )


def test_task_manager_refuses_superseded_without_children(project_dir):
    """The writer's guard: ``superseded`` means "go look at the children",
    so it is only written when children exist. Otherwise it would be a
    pointer row with nothing behind it."""
    manager = _make_manager(project_dir, [_t("1"), _t("2", depends_on=["1"])])
    assert manager.record_task_superseded("1", "boom") is False
    parent = next(t for t in manager.tasks if t.id == "1")
    assert parent.status == "pending", (
        "with no children present the row must keep its status"
    )


def test_no_split_leaves_the_task_failed(project_dir):
    """Negative control for 2: when the refiner changes nothing, there are
    no children, so the task really did fail and must say so. This is what
    keeps ``superseded`` from becoming a way to hide failures."""
    class _NoOpRefiner:
        def refine(self, **kwargs):
            return kwargs["tasks"]

    agent = _build_agent(
        project_dir, _FailingCodingTool(), _NoOpRefiner(),
        tasks=[_t("1"), _t("2", depends_on=["1"])],
    )
    manager = agent.task_manager
    task = next(t for t in manager.tasks if t.id == "1")
    agent._execute_task_with_retry(task, max_retries=2)

    parent = next(t for t in manager.tasks if t.id == "1")
    assert parent.status == "failed", (
        f"a task with no split really failed and must stay failed -- "
        f"got {parent.status!r}"
    )


# ===========================================================================
# (3)  voided cascades; completed is never rewritten.
# ===========================================================================


def test_void_task_cascades_down_the_whole_subtree(project_dir):
    """Acceptance 3: voiding a root voids every task that depends on it,
    transitively."""
    manager = _make_manager(project_dir, [
        _t("A"),
        _t("B", depends_on=["A"]),
        _t("C", depends_on=["B"]),
        _t("D", depends_on=["C"]),
        _t("independent"),
    ])
    summary = manager.void_task("A", "[reset] verdicts discarded")

    assert set(summary["voided"]) == {"A", "B", "C", "D"}, (
        f"the whole subtree must be voided, got {summary['voided']}"
    )
    before = next(t for t in manager.tasks if t.id == "independent")
    assert before.status == "pending", (
        "a task that does not depend on A must be untouched"
    )


def test_voided_blocks_rather_than_being_followed(project_dir):
    """``voided`` is not a pointer. A task still naming a voided upstream
    blocks, with its own token."""
    from agent import is_dependency_ready

    manager = _make_manager(project_dir, [
        _t("A", status="voided"),
        _t("B", depends_on=["A"]),
    ])
    downstream = next(t for t in manager.tasks if t.id == "B")
    ready, reason = is_dependency_ready(downstream, manager.tasks)
    assert ready is False
    assert reason == "upstream_voided:A", (
        f"a revoked premise has its own token, got {reason!r}"
    )


def test_void_does_not_rewrite_a_completed_descendant(project_dir, capsys):
    """The audit-chain rule, decided — and now a REFUSAL, not a spare.

    ``task_plan_delta.py``'s module docstring refuses to rewrite a task
    because that "makes the ``completed`` verdict it earned, its commit
    checkpoint and its failure reason stop matching, and breaks the audit
    chain". ``completed`` is the one status carrying an EARNED verdict, so
    it is the one status the cascade never overwrites.

    2026-10-16 (later the same day): the *rest* of the behaviour changed
    with it. A downstream task can only be ``completed`` if this task's
    premise held; voiding it is what revokes the premise. So "completed
    downstream of a voided premise" is a scheduler bug, not a data shape
    to step over — the cascade now stops, logs an error, and writes
    NOTHING. All four halves of that are pinned here, because each of them
    can rot independently: the error log, the failure verdict, the
    untouched ``completed`` row, and the absence of a partial void.
    """
    manager = _make_manager(project_dir, [
        _t("A"),
        _t("B", depends_on=["A"], status="completed"),
        _t("C", depends_on=["B"]),
    ])
    summary = manager.void_task("A", "[reset] premise gone")

    # (1) the error actually reaches a channel an operator reads
    stderr = capsys.readouterr().err
    assert "void_task" in stderr and "REFUSED" in stderr, (
        "hitting a completed descendant must log an error to stderr; "
        f"got {stderr!r}"
    )

    # (2) the voiding is judged a FAILURE, with its own flag -- not left
    #     to be inferred from an empty "voided" list, which is also what
    #     "nothing depended on A" returns.
    assert summary["ok"] is False, (
        f"a refused voiding must report ok=False, got {summary!r}"
    )
    assert summary["error"], (
        "the refusal must carry a reason the caller can surface; "
        f"got {summary['error']!r}"
    )

    # (3) the earned verdict is untouched
    b = next(t for t in manager.tasks if t.id == "B")
    assert b.status == "completed", (
        f"an earned completed verdict must survive the cascade, got {b.status!r}"
    )
    assert "B" in summary["completed_kept"], (
        "a kept completed descendant must be reported, not silently spared"
    )

    # (4) NO partial void. This is the half that is easy to get wrong:
    #     A is stageable, B is not, and "void A, keep B" would leave the
    #     precise shape this method exists to prevent.
    a = next(t for t in manager.tasks if t.id == "A")
    assert a.status == "pending", (
        "the cascade must abort WHOLE -- A was never voided, so the plan "
        f"is not left with a voided premise in front of an earned "
        f"completed row; got A.status={a.status!r}"
    )
    c = next(t for t in manager.tasks if t.id == "C")
    assert c.status == "pending"
    assert summary["voided"] == [], (
        f"nothing may be written when the cascade is refused, "
        f"got {summary['voided']!r}"
    )


def test_void_is_idempotent(project_dir):
    """Voiding twice must not change anything the second time."""
    manager = _make_manager(project_dir, [_t("A"), _t("B", depends_on=["A"])])
    first = manager.void_task("A", "r")
    second = manager.void_task("A", "r")
    assert first["voided"] == ["A", "B"]
    assert second["voided"] == [], (
        "a second void of an already-voided subtree must be a no-op"
    )


def test_voided_task_is_not_counted_as_work():
    """A voided task is a decision, not a phase: it must not inflate the
    plan's ``total``, and must not be counted as unfinished work (which
    would stall the plan forever)."""
    from routes.plans import _task_counts

    rows = {
        "A": {"status": "completed"},
        "B": {"status": "voided"},
        "C": {"status": "superseded"},
        "D": {"status": "pending"},
    }
    counts = _task_counts(rows, disk_ids={"A", "B", "C", "D"})
    assert counts["total"] == 2, (
        f"total must count only real work (completed + pending), got {counts}"
    )


def test_count_unfinished_tasks_does_not_stall_on_a_split_parent(
    project_dir, monkeypatch, tmp_path,
):
    """A superseded parent stays in tasks.json now, so this counter would
    otherwise report permanent unfinished work for a row the dispatcher
    will never re-run -- and the plan would never reach verification.

    Built against a REAL SQLite overlay rather than a hand-written
    tasks.json, because that is the only shape production produces:
    ``TaskManager.save_tasks`` strips runtime fields, so a split parent's
    ``status="superseded"`` lives in ``plan_tasks`` and nowhere else. A
    fixture that put the status in tasks.json would test a file layout
    the system does not produce and would pass for the wrong reason.
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from verification_loop import _count_unfinished_tasks

    plan_id = "plan-splitlife"
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )

    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps({
        "requirement": "r",
        "tasks": [_t("1"), _t("1-1"), _t("1-2"), _t("2", depends_on=["1"])],
    }), encoding="utf-8")

    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        def _add(task_id, status):
            repo.add_task(plan_id=plan_id, task_dict={
                "id": task_id, "title": f"task {task_id}",
                "description": "d", "test_command": "pytest -q",
                "files_to_modify": [], "depends_on": [],
            })
            # ``add_task`` deliberately never writes runtime state, so the
            # status goes in through the runtime writer -- which is the
            # same path ``record_task_superseded`` uses.
            repo.update_task(
                plan_id=plan_id, task_id=task_id,
                fields={"status": status}, expected_version=0,
            )

        _add("1", "superseded")
        for tid in ("1-1", "1-2", "2"):
            _add(tid, "completed")
    finally:
        conn.close()

    out = _count_unfinished_tasks(plan_id, tasks_file)
    assert out["pending_or_unstarted"] == 0, (
        f"a plan whose split parent is superseded has no unfinished work; "
        f"got {out} -- this would stall the plan forever"
    )
    assert out["failed"] == 0
    assert out["total"] == 4


# ===========================================================================
# (4)  Depth: 5 levels, 4 splits.
# ===========================================================================


def test_stamp_uses_nearest_existing_ancestor_not_hyphen_count():
    """The depth criterion.

    Counting hyphens decides nothing: ``repair-r1-01`` has two and is a
    root, ``12-6-4`` has two and is level 3. What decides is the nearest
    ancestor that is a REAL task row.
    """
    from agent import _stamp_breakdown_depths

    current = [
        {"id": "12", "breakdown_count": 0},
        {"id": "12-6", "breakdown_count": 1},
        # ``repair-r1`` is deliberately absent: there is no such task.
        {"id": "repair-r1-01", "breakdown_count": 0},
    ]
    effective = [
        {"id": "12-6-4"},          # nearest ancestor 12-6 (depth 1) -> 2
        {"id": "12-7"},            # nearest ancestor 12  (depth 0) -> 1
        {"id": "repair-r1-01-2"},  # nearest ancestor repair-r1-01 (0) -> 1
        {"id": "brand-new"},       # no ancestor at all -> unstamped
    ]
    stamped = _stamp_breakdown_depths(effective, current)
    assert stamped == {
        "12-6-4": 2, "12-7": 1, "repair-r1-01-2": 1,
    }, f"got {stamped}"


def test_stamp_no_longer_requires_a_numeric_suffix():
    """The stamp used to require ``suffix.isdigit()`` while the split
    writers (``refiner.py`` / ``refiner_structure.py``) accepted any
    ``<parent>-`` prefix. A child the LLM named ``12-a`` was therefore
    dropped from the DAG as a split parent yet given NO depth -- so the
    budget could not bind the very rows the split logic recognised."""
    from agent import _stamp_breakdown_depths

    current = [{"id": "12", "breakdown_count": 0}]
    effective = [{"id": "12-a"}]
    stamped = _stamp_breakdown_depths(effective, current)
    assert stamped == {"12-a": 1}, (
        f"a non-numeric child is still a breakdown child by the repo's own "
        f"definition; got {stamped}"
    )


def test_level_five_failure_is_not_split_again(project_dir):
    """Acceptance 4 at the boundary: level 5 does not split, and the task
    is recorded as a real failure."""
    from agent import EXECUTOR_BREAKDOWN_MAX

    assert EXECUTOR_BREAKDOWN_MAX == 5
    refiner = _SplittingRefiner()
    agent = _build_agent(
        project_dir, _FailingCodingTool(), refiner,
        tasks=[
            _t("1", breakdown_count=EXECUTOR_BREAKDOWN_MAX - 1),
            _t("2", depends_on=["1"]),
        ],
    )
    manager = agent.task_manager

    task = next(t for t in manager.tasks if t.id == "1")
    agent._execute_task_with_retry(task, max_retries=2)

    assert refiner.calls == 0, (
        "a level-5 failure must not be split again"
    )
    parent = next(t for t in manager.tasks if t.id == "1")
    assert parent.status == "failed", (
        f"the real fail must say failed, got {parent.status!r}"
    )
    assert {t.id for t in manager.tasks} == {"1", "2"}, (
        "no children may have been minted"
    )


# ===========================================================================
# (5)  Session reuse across attempts.
# ===========================================================================


def test_second_attempt_resumes_the_first_attempts_session(project_dir):
    """Acceptance 5 with observable evidence, not code reading.

    Two attempts against one task must use the SAME ``--resume`` session.
    The evidence is the argv the real ``coding_tool`` builds -- asserted
    by reproducing the exact decision at ``coding_tool.py:1987-1991`` off
    the real bound handle, so this cannot pass by "the handle is plumbed
    somewhere".
    """
    seen: list = []

    class _SessionSpy:
        """Stands in for ClaudeCodingTool and records the resume decision.

        ``_query_internal_m1`` decides resume-vs-fresh from the bound
        ``JsonSession`` handle. Reproducing that decision against the real
        handle is enough to observe reuse without a subprocess.
        """

        def __init__(self):
            self.calls = 0

        def query(self, prompt, system_instruction=None, timeout=None,
                  model_type=None, scene=None, **kwargs):
            from coding_tool import _JSON_SESSION

            handle = _JSON_SESSION.get()
            sid = handle.session_id if handle is not None else None
            self.calls += 1
            # This is coding_tool.py:1987-1991 verbatim.
            if sid:
                argv_tail = ["--resume", sid]
            else:
                argv_tail = ["--session-id", "new-uuid"]
            seen.append({"argv_tail": argv_tail, "had_handle": handle is not None})
            if sid is None:
                # First dispatch mints and returns a session id, exactly
                # like the real tool's system-init handler does.
                if handle is not None:
                    handle.session_id = "sess-abc-123"
            return (
                "attempt\n\nFILE: src/example.py\n```python\nx = 1\n```\n\n"
                "TEST_RESULT: FAILED\nREASON: nope\n"
            )

        def query_json(self, *a, **k):
            raise AssertionError("not reached")

    spy = _SessionSpy()
    agent = _build_agent(
        project_dir, spy, _SplittingRefiner(), tasks=[_t("1")],
    )
    task = next(t for t in agent.task_manager.tasks if t.id == "1")

    agent._execute_task_with_retry(task, max_retries=2)

    assert spy.calls >= 2, (
        f"both attempts must have dispatched, saw {spy.calls}"
    )
    first, second = seen[0], seen[1]
    assert first["had_handle"] is True, (
        "attempt 1 must still run inside the per-task session handle"
    )
    assert first["argv_tail"] == ["--session-id", "new-uuid"], (
        f"attempt 1 must open a fresh session, got {first['argv_tail']}"
    )
    assert second["argv_tail"] == ["--resume", "sess-abc-123"], (
        f"attempt 2 must RESUME the same session -- observable proof of "
        f"session reuse. Got {second['argv_tail']}"
    )


def test_session_handle_is_per_task_not_shared():
    """Two different tasks must not share a conversation."""
    from coding_tool import JsonSession, query_in_session

    handles = []

    class _Tool:
        def query(self, prompt, **kwargs):
            from coding_tool import _JSON_SESSION
            h = _JSON_SESSION.get()
            handles.append(h.session_id if h else None)
            return "ok"

    tool = _Tool()
    for _ in range(2):
        h = JsonSession()
        query_in_session(tool, h, "p")
        # Simulate the first dispatch binding the id.
        h.session_id = f"sid-{id(h)}"

    assert handles == [None, None], (
        "a fresh handle starts fresh -- the id is only bound by a real "
        "dispatch, so nothing leaks between tasks"
    )


def test_empty_first_attempt_leaves_the_handle_unbound():
    """The boundary the retry design has to respect: an attempt that
    produced no assistant text never created a session, so resuming it
    would fail in ~0.4s with "Invalid session ID".

    ``_remember`` only binds a real id, so the handle stays empty and the
    next attempt opens fresh. Asserted at the mechanism, not by hoping.
    """
    from coding_tool import ClaudeCodingTool, JsonSession, query_in_session
    import inspect

    class _EmptyTool:
        """Binds nothing -- models a dispatch that never returned a sid."""

        def query(self, prompt, **kwargs):
            return ""

    handle = JsonSession()
    query_in_session(_EmptyTool(), handle, "p")
    assert handle.session_id is None, (
        "no dispatch bound an id, so there is nothing to resume"
    )
    # And the real tool's own guard is still in place.
    src = inspect.getsource(ClaudeCodingTool._run_interactive_resilient)
    assert "fresh_session_on_retry" in src, (
        "the empty-response retry path must still be the fresh-session one"
    )


# ===========================================================================
# Invariants
# ===========================================================================


def test_voided_task_is_terminal_for_the_dispatcher():
    """A voided row must never be scheduled -- that is what
    ``_TERMINAL_TASK_STATUSES`` is for."""
    from agent import _TERMINAL_TASK_STATUSES as agent_terminal
    from server import _TERMINAL_TASK_STATUSES as server_terminal

    assert "voided" in agent_terminal
    assert "voided" in server_terminal, (
        "server.py must agree with agent.py, or the summary endpoint "
        "hydrates voided DB-only rows back in as work"
    )


def test_no_task_is_left_pointing_at_a_voided_task(project_dir):
    """The invariant the cascade exists to maintain.

    After voiding, nothing may still depend on a voided row. If it does,
    that task is blocked forever and no dispatch can clear it -- the same
    failure mode as the superseded deadlock, one status over.
    """
    from agent import is_dependency_ready

    manager = _make_manager(project_dir, [
        _t("A"), _t("B", depends_on=["A"]),
        _t("C", depends_on=["B"]), _t("D", depends_on=["A"]),
    ])
    manager.void_task("A", "[reset] premise gone")

    voided = {t.id for t in manager.tasks if t.status == "voided"}
    assert voided == {"A", "B", "C", "D"}

    # Nothing pending is left naming a voided upstream.
    for t in manager.tasks:
        if t.status in ("pending", "in_progress"):
            ready, reason = is_dependency_ready(t, manager.tasks)
            assert not (not ready and reason.startswith("upstream_voided")), (
                f"task {t.id} is left blocked behind a voided upstream: "
                f"{reason}"
            )


def test_max_retries_is_still_two():
    """The configured value must not drift. This change adds session reuse
    across attempts, and a silent bump would double the wall-clock of
    every failing task."""
    cfg = _BACKEND_DIR / "configs" / "_base.yaml"
    text = cfg.read_text(encoding="utf-8")
    assert "max_retries: 2" in text, f"max_retries must stay 2 in {cfg}"


def test_voiding_a_completed_root_is_refused_loudly(project_dir, capsys):
    """The root is subject to the same rule — refused, logged, nothing written.

    Found while producing the acceptance evidence: ``void_task`` on an
    already-``completed`` row returned ``{"voided": [],
    "completed_kept": []}`` — byte-identical to "nothing depended on
    it". An operator who voided a row and got an empty summary would
    read that as success, which is the exact failure mode the
    ``completed`` rule exists to avoid on the other side.

    What is pinned here is the REFUSAL being visible, not any new
    authority to rewrite a completed verdict: the row keeps its status,
    and the summary names it.
    """
    manager = _make_manager(project_dir, [
        _t("A", status="completed"),
        _t("B", depends_on=["A"], status="completed"),
        _t("C", depends_on=["B"]),
    ])
    summary = manager.void_task("A", "[reset] premise gone")

    assert [t.status for t in manager.tasks if t.id == "A"] == ["completed"], (
        "an earned verdict must not be rewritten, root or not"
    )
    assert summary["voided"] == [], (
        f"nothing may be voided behind a completed root, got {summary['voided']!r}"
    )
    # The refusal names every offender it found. The walk CONTINUES past
    # A so one pass reports B alongside A, rather than the operator
    # fixing A and coming back for B on the next attempt — and "names
    # them" is also what keeps the empty "voided" list from reading as
    # success. (An earlier draft of this test expected ["A"] from a walk
    # that stopped at the first completed row; "report them all, void
    # none of them" is the deliberate rule now.)
    assert summary["completed_kept"] == ["A", "B"], (
        "the walk must keep going so every offender is named in one pass, "
        f"and the refusal must name the rows it could not void; "
        f"got {summary['completed_kept']!r}"
    )
    assert summary["ok"] is False, (
        f"voiding a completed root must be judged a failure, got {summary!r}"
    )
    stderr = capsys.readouterr().err
    assert "void_task" in stderr and "REFUSED" in stderr, (
        f"the refusal must log an error to stderr; got {stderr!r}"
    )
    # Nothing is written anywhere in the subtree -- not A, not C.
    assert [t.status for t in manager.tasks if t.id == "C"] == ["pending"], (
        "a refused cascade writes nothing at all"
    )


def test_void_without_completed_is_not_penalised(project_dir, capsys):
    """The POSITIVE CONTROL.

    Every rule above is "when you see ``completed``, stop". A change that
    logs an error, or flips ``ok`` to False, on an ORDINARY cascade would
    turn the common path into a wall of false alarms — and because the
    whole point of the error is to say "go look at the scheduler", a
    false alarm actively trains the reader to ignore it.

    So this pins the other direction explicitly, on a plain
    three-deep cascade with no ``completed`` anywhere in it.
    """
    manager = _make_manager(project_dir, [
        _t("A"),
        _t("B", depends_on=["A"]),
        _t("C", depends_on=["B"]),
    ])
    summary = manager.void_task("A", "[reset] verdicts discarded")

    assert summary["ok"] is True, (
        f"an ordinary cascade must succeed; got {summary!r}"
    )
    assert summary["error"] is None
    assert summary["completed_kept"] == []
    assert set(summary["voided"]) == {"A", "B", "C"}
    assert {t.status for t in manager.tasks} == {"voided"}

    stderr = capsys.readouterr().err
    assert "void_task" not in stderr, (
        "a cascade with no completed row must not emit the refusal error; "
        f"got {stderr!r}"
    )


def test_void_refusal_names_every_offender_in_one_pass(project_dir, capsys):
    """Two ``completed`` rows, two sibling branches, one refusal.

    The walk queues by ``depends_on``, so offenders are discovered in
    breadth-first order rather than one-per-operator-attempt. Worth its
    own test because the "keep walking" half and the "void nothing" half
    are independent: an implementation that logs every offender but
    commits the staged rows it collected before the first hit would pass
    a single-offender fixture and fail here.
    """
    manager = _make_manager(project_dir, [
        _t("A"),
        _t("B1", depends_on=["A"]),
        _t("B2", depends_on=["A"], status="completed"),
        _t("C", depends_on=["B1"], status="completed"),
        _t("D", depends_on=["B2"]),
    ])
    summary = manager.void_task("A", "[reset] premise gone")

    assert summary["completed_kept"] == ["B2", "C"], (
        "both offenders must be named in the one refusal; "
        f"got {summary['completed_kept']!r}"
    )
    assert {t.status for t in manager.tasks} == {
        "pending", "completed",
    }, (
        "a refusal writes nothing: the only statuses left are the "
        "original pending rows and the earned completed rows"
    )
    stderr = capsys.readouterr().err
    assert "2 already-completed row(s)" in stderr, (
        f"the error must count the offenders so one log line describes the "
        f"whole refusal; got {stderr!r}"
    )
