"""Every task-state writer must declare — and keep — its column blast radius.

Why this file exists (2026-09-23)
---------------------------------
``PlanTaskRepository.update_task`` was a **partial** update before the v4
migration and a **full-column overwrite** after it.  The legacy path
merged into a JSON object::

    new_entry = dict(existing_entry)
    for k, v in fields.items():
        new_entry[k] = v        # only the keys the payload names

The v4 rewrite replaced that dict with columns and assigned every
runtime column as ``excluded.*`` — so a key the payload omitted was
clamped to NULL instead of left alone.  No docstring changed; the
behaviour reversed underneath them.

That silently broke the two-step completion write in
``AutonomousAgent.process_task``::

    update_task_status(task.id, "completed")   # agent.py:4704
    ...                                        # 11 lines later
    _commit_task_changes(...)                  # agent.py:4715
                                               # → {"commit_sha": sha}

The second write erased the first.  A production plan
ran 12 tasks to completion while the progress card stayed at 0% —
the card counts ``status = 'completed'``, and every such row ended with
``status IS NULL``.  ``_load_tasks`` read NULL as "never started" and
re-dispatched them: 20 commits for 12 tasks.

Why the 3421 passing tests did not catch it: the clamp only shows up on
a row written **twice with disjoint payloads**.  Every existing test
wrote a row once (the INSERT branch, where NULL is correct for an
omitted column) or wrote the same column every time.  The shape was
structurally unreachable.

The three guards
----------------
* ``TestWriterColumnContract`` — for each production writer, the set of
  columns it actually changes must equal the set it declares.  The
  declaration lives in ``WRITERS`` below and is the thing under review.
* ``TestWriteCallSiteInventory`` — an AST sweep over ``backend/``; a new
  ``.update_task(...)`` call site fails the suite until it is
  acknowledged here.
* ``TestMergeModelProperty`` — random partial-write sequences are
  checked against the legacy merge model (``dict.update``), so any
  future divergence from partial-update semantics is caught whatever
  its cause.

Database isolation
------------------
Every test in this module runs against a throwaway database under
``tmp_path``, redirected via ``PDT_STATE_DB_PATH`` (the env var every
state-db lookup in ``TaskManager`` already honours).  The fixture
destroys it in teardown, and ``real_database_untouched`` asserts after
the module that no ``test-write-contract-*`` row reached the operator's
real ``state.db``.
"""

from __future__ import annotations

import ast
import json
import os
import random
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Set

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from task_manager import TaskManager  # noqa: E402

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


#: The operator's live database.  Opened read-only, and only to prove
#: that nothing here wrote to it.
REAL_STATE_DB = BACKEND_DIR.parent / "state.db"

#: Every plan id in this module starts with this.  It is distinctive
#: enough that a matching row in the real database is unambiguous
#: evidence of a leak — no real plan can collide with it.
PLAN_PREFIX = "test-write-contract-"

PLAN_ID = PLAN_PREFIX + "writers"

#: Mirrors ``ALLOWED_TASK_FIELDS`` in the repository.
RUNTIME_COLUMNS: tuple[str, ...] = (
    "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
    "failure_reason", "breakdown_count",
)


# ---------------------------------------------------------------------------
# Database isolation
# ---------------------------------------------------------------------------


def _leaked_plan_ids(db_path: Path = REAL_STATE_DB) -> Set[str]:
    """Plan ids in ``db_path`` that belong to this module.

    Read-only, and only ever *reads*. Returns an empty set when the
    database does not exist (nothing to protect, nothing leaked).
    """
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT plan_id FROM plan_tasks WHERE plan_id LIKE ?",
            (PLAN_PREFIX + "%",),
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def real_database_untouched() -> Iterator[None]:
    """After the module, the live ``state.db`` must hold none of our rows.

    A module-scoped fixture so it runs once, after every test here has
    had its chance to leak.  The check is a read of the real file — it
    never opens it for writing, and it tolerates the file being absent.
    """
    yield
    leaked = _leaked_plan_ids()
    assert not leaked, (
        f"these tests wrote into the operator's real state.db: "
        f"{sorted(leaked)}. Every lookup must go through the "
        f"PDT_STATE_DB_PATH override in ``isolated_state_db``."
    )


@pytest.fixture(autouse=True)
def isolated_state_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Point every state-db lookup at a throwaway file, then destroy it.

    ``TaskManager`` resolves its database as ``PDT_STATE_DB_PATH`` or,
    failing that, ``<repo>/state.db`` — so overriding the variable is
    the whole isolation story for the writer paths under test.  The
    fixture asserts the override took effect (a silently-ineffective
    override would send every write to the live database), and removes
    the file it created.
    """
    work = tmp_path / "state-db"
    work.mkdir()
    db_path = work / "state.db"

    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    assert Path(os.environ["PDT_STATE_DB_PATH"]) == db_path, (
        "the isolation override did not take effect"
    )
    assert db_path.resolve() != REAL_STATE_DB.resolve(), (
        "the isolation fixture must never point at the live database"
    )

    yield db_path

    # Destroy it — the database, its WAL/SHM sidecars, and the directory
    # that held them. Nothing this module created may outlive the test.
    shutil.rmtree(work, ignore_errors=True)
    assert not work.exists(), "the throwaway state.db outlived the test"


def test_the_isolation_fixture_points_away_from_the_real_database(
    isolated_state_db: Path,
) -> None:
    """A guard on the guard: if ``PDT_STATE_DB_PATH`` were ignored (or the
    fixture stopped overriding it), every other test here would be
    writing to production."""
    assert Path(os.environ["PDT_STATE_DB_PATH"]) == isolated_state_db
    assert isolated_state_db.resolve() != REAL_STATE_DB.resolve()


def test_the_leak_detector_actually_detects(tmp_path: Path) -> None:
    """A guard that cannot fail is not a guard. Feed the detector a
    database that *does* contain a leaked row and check it says so —
    done against a scratch file, never the real one."""
    scratch = tmp_path / "scratch-state.db"
    conn = open_db(scratch)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (PLAN_PREFIX + "leak", "1", "completed", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    assert _leaked_plan_ids(scratch) == {PLAN_PREFIX + "leak"}

    # A real schema with other plans' rows must read as clean.
    clean = tmp_path / "clean-state.db"
    conn = open_db(clean)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("a production plan", "1", "completed", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    assert _leaked_plan_ids(clean) == set()


# ---------------------------------------------------------------------------
# Fixtures shared with the writer-contract and property tests
# ---------------------------------------------------------------------------


def _task(tid: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": tid,
        "title": f"task {tid}",
        "description": "d",
        "test_command": "pytest -q",
        "files_to_modify": ["__NO_FILE_CHANGES__"],
        "depends_on": [],
        "model_type": "medium",
        "project_dir": None,
    }
    out.update(extra)
    return out


def _write_tasks(plan_dir: Path, tasks: List[Dict[str, Any]]) -> None:
    (plan_dir / "tasks.json").write_text(
        json.dumps({"requirement": "r", "tasks": tasks}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def plan_dir(tmp_path: Path) -> Iterator[Path]:
    # ``derive_plan_id_from_tasks_file`` takes the parent directory name
    # as the plan id, so the directory carries the name we assert on.
    d = tmp_path / PLAN_ID
    d.mkdir(parents=True, exist_ok=True)
    yield d


@pytest.fixture
def conn(isolated_state_db: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(isolated_state_db)
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> PlanTaskRepository:
    return PlanTaskRepository(conn)


def _manager(plan_dir: Path) -> TaskManager:
    tm = TaskManager(project_dir=plan_dir, tasks_file=plan_dir / "tasks.json")
    tm.load_tasks()
    return tm


#: A fully-populated row.  Every column carries a non-NULL sentinel so
#: "unchanged" cannot be confused with "NULL either way" — the old
#: clamp wrote NULL, and a NULL seed would have made the comparison
#: pass for the wrong reason.
SEEDED_ROW: Dict[str, Any] = {
    # Deliberately not ``in_progress``: every status writer under test
    # must be able to move it, or ``must_change`` would fail for a
    # reason that has nothing to do with the bug being guarded.
    "status": "pending",
    "end_ts": "2026-01-01T00:00:00Z",
    "schedule_ts": "2026-01-02T00:00:00Z",
    "attempt": 4,
    "commit_sha": "f" * 40,
    "failure_reason": "seeded",
    "breakdown_count": 2,
}


# ---------------------------------------------------------------------------
# 1 — the writer contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriterContract:
    """One production writer and the columns it is allowed to touch.

    ``must_change`` are the columns this writer exists to write;
    ``may_change`` are columns whose value legitimately depends on the
    prior state.  Everything else is forbidden — that is the assertion.
    """

    name: str
    must_change: FrozenSet[str]
    may_change: FrozenSet[str] = frozenset()

    @property
    def forbidden(self) -> FrozenSet[str]:
        return frozenset(RUNTIME_COLUMNS) - self.must_change - self.may_change


#: ``name → (contract, how to invoke it)``.  Adding a writer to the
#: production code without adding it here is caught by
#: ``TestWriteCallSiteInventory``.
WRITERS: Dict[str, tuple] = {
    "TaskManager.update_task_status(completed)": (
        WriterContract(
            name="TaskManager.update_task_status",
            must_change=frozenset({"status", "end_ts"}),
            # Cleared when it was set, left NULL when it was not.
            may_change=frozenset({"failure_reason"}),
        ),
        lambda tm: tm.update_task_status("1", "completed"),
    ),
    "TaskManager.update_task_status(in_progress)": (
        WriterContract(
            name="TaskManager.update_task_status",
            must_change=frozenset({"status", "end_ts"}),
            may_change=frozenset({"failure_reason"}),
        ),
        lambda tm: tm.update_task_status("1", "in_progress"),
    ),
    "TaskManager.record_task_failure": (
        WriterContract(
            name="TaskManager.record_task_failure",
            must_change=frozenset({"status", "failure_reason", "end_ts"}),
        ),
        lambda tm: tm.record_task_failure("1", "boom"),
    ),
    # The writer whose omission started all of this: it runs eleven
    # lines after ``update_task_status(..., "completed")`` and must not
    # touch anything but the column it is named for.
    "TaskManager.update_task_commit_sha": (
        WriterContract(
            name="TaskManager.update_task_commit_sha",
            must_change=frozenset({"commit_sha"}),
        ),
        lambda tm: tm.update_task_commit_sha("1", "a" * 40),
    ),
}


@pytest.mark.parametrize("writer_name", sorted(WRITERS))
def test_a_writer_changes_exactly_the_columns_it_declares(
    writer_name: str, plan_dir: Path, conn: sqlite3.Connection,
) -> None:
    """The contract assertion: some state changed,
    some state did not.

    Fails in both directions — a writer that under-delivers (a column it
    promised to write did not move) and one that over-reaches (a column
    it never named moved anyway).  The second is the 0923 failure.
    """
    contract, invoke = WRITERS[writer_name]

    _write_tasks(plan_dir, [_task("1")])
    tm = _manager(plan_dir)
    repo = PlanTaskRepository(conn)

    repo.update_task(PLAN_ID, "1", dict(SEEDED_ROW), expected_version=0)
    before = repo.get_task(PLAN_ID, "1")

    invoke(tm)

    after = repo.get_task(PLAN_ID, "1")
    changed = {c for c in RUNTIME_COLUMNS if after[c] != before[c]}

    assert not (contract.must_change - changed), (
        f"{writer_name} did not change {sorted(contract.must_change - changed)}, "
        f"which it declares as its purpose"
    )
    assert not (changed & contract.forbidden), (
        f"{writer_name} changed {sorted(changed & contract.forbidden)}, "
        f"which it does not declare. A task-state write must never touch a "
        f"column it did not name — that is how a completed task's status "
        f"gets erased by the commit write that follows it."
    )


def test_the_contract_list_is_not_empty() -> None:
    assert set(WRITERS) >= {
        "TaskManager.update_task_status(completed)",
        "TaskManager.record_task_failure",
        "TaskManager.update_task_commit_sha",
    }


def test_no_contract_is_vacuous() -> None:
    """A contract that declares every column would assert nothing."""
    for writer_name, (contract, _invoke) in WRITERS.items():
        assert contract.must_change, f"{writer_name} declares no required column"
        assert contract.forbidden, (
            f"{writer_name} forbids nothing — the assertion would be vacuous"
        )


def test_completed_then_commit_sha_leaves_the_row_terminal(
    plan_dir: Path, conn: sqlite3.Connection,
) -> None:
    """The end-to-end shape that broke: the two writes in the order
    ``AutonomousAgent.process_task`` performs them."""
    _write_tasks(plan_dir, [_task("1")])
    tm = _manager(plan_dir)
    repo = PlanTaskRepository(conn)

    tm.update_task_status("1", "completed")
    tm.update_task_commit_sha("1", "c" * 40)

    entry = repo.get_task(PLAN_ID, "1")
    assert entry["status"] == "completed"
    assert entry["commit_sha"] == "c" * 40
    assert entry["end_ts"], "the completion timestamp must survive the commit write"


# ---------------------------------------------------------------------------
# 2 — the call-site inventory
# ---------------------------------------------------------------------------


#: Every ``.update_task(...)`` call site in production code, keyed by
#: ``<path relative to backend/>::<enclosing function/class chain>``.
#:
#: This is deliberately an exact set, not a superset: a *new* site fails
#: the test (declare its blast radius) and a *stale* entry fails too
#: (the writer was renamed or deleted).
ACKNOWLEDGED_CALL_SITES: Dict[str, str] = {
    "agent.py::AutonomousAgent._persist_task_status":
        "fields 由 task 的运行期属性逐个 `is not None` 累积 —— 按构造即部分更新",
    "verification_loop.py::_mark_task_skipped":
        "fields={'status','failure_reason','end_ts'} —— 标记跳过",
    "state_machine/repositories/_task_progress_migration.py"
    "::migrate_tasks_json_to_progress":
        "旧 task_progress JSON 一次性迁移，runtime_entry 为整行运行态",
    "task_manager.py::TaskManager._persist_commit_sha_to_sqlite":
        "fields={'commit_sha'} —— 只写这一列（0923 回归现场）",
    "task_manager.py::TaskManager._persist_status_to_sqlite":
        "fields={'status','end_ts'}，非 failed 时显式 failure_reason=None",
    "task_plan_delta.py::apply_task_delta":
        "fields={'status','failure_reason'} —— superseded",
    "task_repository.py::TaskRepository.update_status":
        "legacy shim，原样转发调用方给的 fields",
}


def _production_update_task_call_sites() -> Set[str]:
    """``{<relpath>::<qualname>}`` for every ``.update_task(...)`` call in
    production ``backend/`` code (tests excluded)."""
    sites: Set[str] = set()
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        rel = path.relative_to(BACKEND_DIR).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        stack: List[str] = []

        class _Walker(ast.NodeVisitor):
            def _enter(self, node: ast.AST) -> None:
                stack.append(node.name)          # type: ignore[attr-defined]
                self.generic_visit(node)
                stack.pop()

            visit_FunctionDef = _enter
            visit_AsyncFunctionDef = _enter
            visit_ClassDef = _enter

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "update_task":
                    sites.add(f"{rel}::" + ".".join(stack))
                self.generic_visit(node)

        _Walker().visit(tree)
    return sites


def test_every_update_task_call_site_is_acknowledged() -> None:
    """A new task-state write path cannot appear silently.

    This is the guard against the *next* version of this bug: the last
    two rounds both came from a writer nobody had audited for its column
    blast radius, and both times the fix landed on one of two parallel
    paths.  Forcing every new call site through this list forces the
    question "which columns do I write, and which do I preserve?" to be
    answered at review time instead of discovered in a re-run.
    """
    found = _production_update_task_call_sites()
    acknowledged = set(ACKNOWLEDGED_CALL_SITES)

    new = sorted(found - acknowledged)
    stale = sorted(acknowledged - found)

    assert not new, (
        "new task-state write path(s):\n  "
        + "\n  ".join(new)
        + "\n\nDeclare each one in ACKNOWLEDGED_CALL_SITES and state the "
        "columns it may change. If it is a TaskManager-level writer, add a "
        "WRITERS entry too so its blast radius is enforced, not just noted."
    )
    assert not stale, (
        "ACKNOWLEDGED_CALL_SITES lists call site(s) that no longer exist:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove them (a renamed writer must be re-acknowledged)."
    )


# ---------------------------------------------------------------------------
# 3 — the merge model, as a property
# ---------------------------------------------------------------------------

_STATUSES = ("pending", "in_progress", "completed", "failed", "skipped", "superseded")


def _value_for(column: str, step: int) -> Any:
    """A distinct, schema-valid value per (column, step)."""
    if column == "status":
        return _STATUSES[step % len(_STATUSES)]
    if column in ("attempt", "breakdown_count"):
        return step + 1
    if column == "commit_sha":
        # ``_SAFE_COMMIT_SHA_RE`` is ``^[A-Za-z0-9_-]+$``.
        return f"sha{step:037d}"
    return f"2026-03-01T00:00:{step % 60:02d}Z"


@pytest.mark.parametrize("seed", [20260923, 1, 424242])
def test_random_partial_write_sequences_match_the_merge_model(
    repo: PlanTaskRepository, seed: int,
) -> None:
    """The oracle is the semantics the v4 migration dropped.

    The pre-v4 implementation merged the payload into an existing dict
    (``for k, v in fields.items(): entry[k] = v``).  A dict merge is a
    partial update by construction, so the old behaviour is a clean
    reference model: replay each payload against ``dict.update`` and
    against the repository, and the rows must agree.

    Random sequences rather than hand-picked pairs, because the failure
    needs a *second* write that omits a column the first one set — an
    ordering a hand-written case only covers where someone thought to
    look.  Seeds are fixed so a failure is reproducible.
    """
    rng = random.Random(seed)
    repo.update_task("p1", "t1", dict(SEEDED_ROW), expected_version=0)
    model: Dict[str, Any] = dict(SEEDED_ROW)

    for step in range(80):
        columns = rng.sample(RUNTIME_COLUMNS, rng.randint(1, 3))
        payload = {c: _value_for(c, step) for c in columns}

        repo.update_task("p1", "t1", payload, expected_version=0)
        model.update(payload)

        entry = repo.get_task("p1", "t1")
        actual = {c: entry[c] for c in RUNTIME_COLUMNS}
        expected = {c: model.get(c) for c in RUNTIME_COLUMNS}
        assert actual == expected, (
            f"seed={seed} step={step}: wrote {sorted(payload)}; the row "
            f"diverged from the merge model, so a column the payload did "
            f"not name did not survive. "
            f"differing columns: "
            f"{sorted(c for c in RUNTIME_COLUMNS if actual[c] != expected[c])}"
        )


def test_the_merge_model_oracle_is_not_vacuous(repo: PlanTaskRepository) -> None:
    """Sanity-check the oracle itself: a dict-merge model *does* retain a
    column written earlier and omitted later — otherwise every assertion
    above would hold trivially."""
    model: Dict[str, Any] = dict(SEEDED_ROW)
    model.update({"status": "completed"})
    model.update({"commit_sha": "b" * 40})
    assert model["status"] == "completed", (
        "the reference model must keep a column that a later payload omits"
    )
