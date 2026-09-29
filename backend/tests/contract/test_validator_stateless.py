"""Contract tests for ``framework.task_output_validator``.

Architecture decision point 4 mandates that ``TaskOutputValidator`` is
**stateless** across calls. The counter / quota that bounds
``auto_fix`` invocations lives in the dispatcher / refiner modules,
NOT in the validator. These tests pin that contract.

Contracts (from the L1 task spec):
  1. ``validate(s1)`` run N=100 times in a row returns the same report
     every time (same input -> same output, deterministic).
  2. ``auto_fix(s1)`` run N=100 times in a row returns the same list
     every time.
  3. ``auto_fix(s1)`` does NOT mutate ``s1`` (``input_snapshot`` deep
     equality before vs after).
  4. ``validate`` / ``auto_fix`` have no disk side-effects: sha256
     of every file in the target directory is identical before vs
     after the call.
  5. ``validate`` -> ``auto_fix`` -> ``validate`` produces the same
     final state as ``auto_fix`` -> ``validate`` for the same input.
  6. The validator instance has NO residual mutable state fields
     (``_auto_fix_count``, ``_quota``, ``_last_report``, ``_cache``,
     ``_depends_on_fix_passes``, ``_fixed_tasks``).

The validator lives at ``framework/task_output_validator.py`` and
must import the ``SubTask`` model from the existing ``task`` module.
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Path / import setup
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from framework.task_output_validator import (  # noqa: E402
    TaskOutputValidator,
    ValidationReport,
    TaskValidationError,
)
from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL  # noqa: E402


# Re-exported for tests that build broken snapshots.
_LOCAL_UNKNOWN_SENTINEL = "__UNKNOWN_MODIFICATIONS__"


# ---------------------------------------------------------------------------
# Fixtures: an in-memory snapshot that is small enough to validate quickly
# ---------------------------------------------------------------------------

N_ITERATIONS = 100


def _make_snapshot(tmp_path: Path) -> list[SubTask]:
    """Build a snapshot whose ``test_command`` and ``files_to_modify``
    point at REAL files in ``tmp_path`` so ``validate`` passes (step 3
    + step 4) on a fresh snapshot.

    The snapshot has 3 tasks, all referencing real artifacts under
    ``tmp_path``. Descriptions avoid the literal substring
    ``"task-N"`` / ``"task N"`` so step 2 (depends_on consistency)
    passes — otherwise ``validate`` would flag every task that has
    a token in its description but not in its ``depends_on`` list.
    """
    # Create 3 placeholder test files referenced by the snapshot's
    # ``test_command``. The validator's step 4 AST-parses these.
    test_paths = []
    for i in range(3):
        p = tmp_path / f"test_task_{i}.py"
        p.write_text(
            f"def test_task_{i}():\n"
            f"    assert True\n",
            encoding="utf-8",
        )
        test_paths.append(p)

    tasks = []
    for i, p in enumerate(test_paths):
        # Description intentionally does NOT reference "task N" so
        # step 2's depends_on consistency check passes (no missing
        # edges to flag).
        t = SubTask(
            id=f"task-{i+1}",
            title=f"Task {i+1}",
            description=f"Implementation for the {i+1} feature",
            test_command=f"pytest {p.name}::test_task_{i}",
            files_to_modify=[p.name],
            depends_on=[],
        )
        tasks.append(t)
    return tasks


@pytest.fixture
def tmp_with_files(tmp_path: Path) -> Path:
    """Plain tmp_path; tests build their own snapshots against it."""
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_tree(directory: Path) -> dict[str, str]:
    """Return ``{relative_path: sha256_hex}`` for every regular file
    in ``directory``.

    Used by tests 4 to verify the validator has no disk side-effects.
    """
    out: dict[str, str] = {}
    for root, _dirs, files in os.walk(directory):
        for f in files:
            full = Path(root) / f
            rel = full.relative_to(directory).as_posix()
            h = hashlib.sha256()
            h.update(full.read_bytes())
            out[rel] = h.hexdigest()
    return out


def _report_to_dict(report: ValidationReport) -> dict:
    """Compare two ``ValidationReport`` values by content equality.

    The contract says "report可比较: 转 dict 后 ``==``".  Pydantic
    ``BaseModel`` already exposes ``.model_dump()`` which gives a
    plain dict; that's the canonical comparator.
    """
    return report.model_dump()


def _tasks_to_dicts(tasks: list[SubTask]) -> list[dict]:
    return [t.model_dump() for t in tasks]


# ---------------------------------------------------------------------------
# Contract 1: validate() is deterministic across N iterations
# ---------------------------------------------------------------------------


def test_validate_is_deterministic_across_n_iterations(tmp_with_files: Path) -> None:
    """``validate(s1)`` called 100 times returns the same report every time.

    This is the core stateless contract: the validator must hold no
    per-call counters or caches that change its output between
    invocations.
    """
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)

    reports = [validator.validate(s1) for _ in range(N_ITERATIONS)]
    dicts = [_report_to_dict(r) for r in reports]
    first = dicts[0]
    assert all(d == first for d in dicts), (
        "validate() output differs across N iterations:\n"
        f"first.status={first.get('status')!r} "
        f"last.status={dicts[-1].get('status')!r}"
    )


# ---------------------------------------------------------------------------
# Contract 2: auto_fix() is deterministic across N iterations
# ---------------------------------------------------------------------------


def test_auto_fix_is_deterministic_across_n_iterations(tmp_with_files: Path) -> None:
    """``auto_fix(s1)`` called 100 times returns the same list every time."""
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)

    results = [validator.auto_fix(s1) for _ in range(N_ITERATIONS)]
    dicts = [_tasks_to_dicts(rs) for rs in results]
    first = dicts[0]
    assert all(d == first for d in dicts), "auto_fix() output drifts across iterations"


# ---------------------------------------------------------------------------
# Contract 3: auto_fix() does NOT mutate input_snapshot
# ---------------------------------------------------------------------------


def test_auto_fix_does_not_mutate_input_snapshot(tmp_with_files: Path) -> None:
    """After ``auto_fix(s1)`` returns, ``s1`` is deep-equal to its
    pre-call state — no in-place mutation of the caller's list.
    """
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)

    before = _tasks_to_dicts(s1)
    _ = validator.auto_fix(s1)
    after = _tasks_to_dicts(s1)

    assert before == after, (
        "auto_fix() mutated the input snapshot — the validator must "
        "deepcopy its input before applying any fix"
    )


def test_validate_does_not_mutate_input_snapshot(tmp_with_files: Path) -> None:
    """``validate(s1)`` must also leave ``s1`` untouched."""
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)

    before = _tasks_to_dicts(s1)
    _ = validator.validate(s1)
    after = _tasks_to_dicts(s1)

    assert before == after, "validate() mutated the input snapshot"


# ---------------------------------------------------------------------------
# Contract 4: validate / auto_fix have no disk side-effects
# ---------------------------------------------------------------------------


def _assert_no_disk_side_effects(
    func_name: str,
    func,
    snapshot: list[SubTask],
    validator: TaskOutputValidator,
    target_dir: Path,
) -> None:
    """Run ``func(snapshot)`` and assert every file's sha256 is unchanged."""
    before = _hash_tree(target_dir)
    _ = func(snapshot)
    after = _hash_tree(target_dir)
    assert before == after, (
        f"{func_name}() mutated files on disk under {target_dir}: "
        f"added={sorted(set(after) - set(before))} "
        f"removed={sorted(set(before) - set(after))} "
        f"changed={[k for k in before if k in after and before[k] != after[k]]}"
    )


def test_validate_has_no_disk_side_effects(tmp_with_files: Path) -> None:
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    _assert_no_disk_side_effects(
        "validate", validator.validate, s1, validator, tmp_with_files,
    )


def test_auto_fix_has_no_disk_side_effects(tmp_with_files: Path) -> None:
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    _assert_no_disk_side_effects(
        "auto_fix", validator.auto_fix, s1, validator, tmp_with_files,
    )


# ---------------------------------------------------------------------------
# Contract 5: validate -> auto_fix -> validate == auto_fix -> validate
# (order-independence of the post-fix validation pass)
# ---------------------------------------------------------------------------


def test_validate_auto_fix_validate_equals_auto_fix_validate(
    tmp_with_files: Path,
) -> None:
    """Order-independence: the final ``validate`` after a single
    ``auto_fix`` pass is identical whether the caller ran an initial
    ``validate`` before ``auto_fix`` or not.
    """
    s1 = _make_snapshot(tmp_with_files)
    validator_a = TaskOutputValidator(project_dir=tmp_with_files)
    validator_b = TaskOutputValidator(project_dir=tmp_with_files)

    # Path A: validate -> auto_fix -> validate
    _ = validator_a.validate(s1)
    fixed_a = validator_a.auto_fix(s1)
    final_a = validator_a.validate(fixed_a)

    # Path B: auto_fix -> validate
    fixed_b = validator_b.auto_fix(s1)
    final_b = validator_b.validate(fixed_b)

    assert _report_to_dict(final_a) == _report_to_dict(final_b), (
        "validate→auto_fix→validate disagrees with auto_fix→validate — "
        "the validator carries hidden cross-call state"
    )


# ---------------------------------------------------------------------------
# Contract 6: no residual mutable state fields on the validator instance
# ---------------------------------------------------------------------------


RESIDUAL_FIELD_NAMES = [
    "_auto_fix_count",
    "_quota",
    "_last_report",
    "_cache",
    "_depends_on_fix_passes",
    "_fixed_tasks",
    "_last_snapshot",
    "_call_count",
]


@pytest.mark.parametrize("field_name", RESIDUAL_FIELD_NAMES)
def test_validator_has_no_residual_state_field(
    field_name: str, tmp_with_files: Path,
) -> None:
    """The validator must not expose any per-call counters / caches
    / last-report fields. This is the regression guard called out by
    the task spec ("反向断言: dir(validator) 中不存在 _auto_fix_count /
    _quota 之类残留字段 (防回归)").
    """
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    assert field_name not in dir(validator), (
        f"validator exposes residual state field {field_name!r}; "
        "quota / counter state must live in the caller (dispatcher / "
        "refiner) not in the validator"
    )


# ---------------------------------------------------------------------------
# Sanity: validate + auto_fix still work (the validator is functional)
# ---------------------------------------------------------------------------


def test_validate_returns_validation_report(tmp_with_files: Path) -> None:
    """Smoke test: on a clean snapshot, ``validate`` returns a
    ``ValidationReport`` with status ``"passed"``.
    """
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    report = validator.validate(s1)
    assert isinstance(report, ValidationReport)
    assert report.status == "passed"


def test_auto_fix_returns_list_of_tasks(tmp_with_files: Path) -> None:
    """Smoke test: ``auto_fix`` returns a list of ``SubTask``."""
    s1 = _make_snapshot(tmp_with_files)
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    fixed = validator.auto_fix(s1)
    assert isinstance(fixed, list)
    assert len(fixed) == len(s1)
    assert all(isinstance(t, SubTask) for t in fixed)


# ---------------------------------------------------------------------------
# Failure-path coverage: validate() / auto_fix() must also satisfy
# the statelessness contract on bad inputs (architecture decision
# point 4 + coverage target ≥ 85%).
# ---------------------------------------------------------------------------


def _make_broken_snapshot(tmp_path: Path) -> list[SubTask]:
    """Snapshot that fails steps 2 + 3 + 4 — used to drive the
    failure branches in ``validate`` so coverage reaches ≥ 85%."""
    # Step 3 (file existence) — references a path that does not exist.
    # Step 4 (test_command function) — references a real file but a
    # function name that does not exist in it.
    # Step 2 (depends_on consistency) — the description contains
    # ``"task 99"`` but ``depends_on`` is empty.
    broken_test = tmp_path / "test_broken.py"
    broken_test.write_text(
        "def test_something_else():\n    assert True\n",
        encoding="utf-8",
    )
    return [
        SubTask(
            id="broken-1",
            title="Broken task",
            description="depends on task 99 for downstream wiring",
            test_command=f"pytest {broken_test.name}::test_does_not_exist",
            files_to_modify=["definitely_missing_file_xyz.py"],
            depends_on=[],
        )
    ]


def test_validate_reports_failed_status_on_broken_snapshot(
    tmp_with_files: Path,
) -> None:
    """When validation fails, the report has status='failed' and
    surfaces the offending task id + reasons."""
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    report = validator.validate(_make_broken_snapshot(tmp_with_files))
    assert report.status == "failed"
    assert report.failed_task_ids == ("broken-1",)
    assert len(report.reasons) >= 1
    # The first failure is step 2 (depends_on consistency): the
    # description says "task 99" but depends_on is empty. We verify
    # the contract independently of step ordering — any step 1..4
    # is acceptable as long as one is recorded.
    assert any(s in (1, 2, 3, 4) for s in report.failed_steps)


def test_auto_fix_appends_missing_depends_on(tmp_with_files: Path) -> None:
    """``auto_fix`` appends description-referenced task ids that are
    missing from ``depends_on`` — provided the referenced id actually
    exists in the snapshot.

    2026-09 contract update: ``auto_fix`` refuses to inject phantom
    deps (description tokens with no matching task id in the plan) —
    that guard fixed a real executor freeze where a buried
    ``missing_upstream:task-1`` reference (plan ids were ``1-1``/``1-2``)
    blocked scheduling forever. The referenced id must therefore be a
    real task in the snapshot.
    """
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    snapshot = [
        SubTask(
            id="task-7",
            title="Fix wiring",
            description="depends on task 99",
            test_command="echo noop",
            files_to_modify=[_LOCAL_UNKNOWN_SENTINEL],
            depends_on=[],
        ),
        # Referenced id must exist in the snapshot or the phantom-dep
        # guard refuses the injection. ``auto_fix`` parses the raw id
        # ("99") out of the "task 99" phrasing.
        SubTask(
            id="99",
            title="Upstream wiring",
            description="upstream",
            test_command="echo noop",
            files_to_modify=[_LOCAL_UNKNOWN_SENTINEL],
            depends_on=[],
        ),
    ]
    fixed = validator.auto_fix(snapshot)
    assert "99" in fixed[0].depends_on
    # And the input was NOT mutated.
    assert snapshot[0].depends_on == []


def test_validate_does_not_mutate_broken_snapshot(tmp_with_files: Path) -> None:
    """Even on a broken snapshot, ``validate`` must not mutate the
    caller's list (the deep-copy guard fires before any check)."""
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    snapshot = _make_broken_snapshot(tmp_with_files)
    before = _tasks_to_dicts(snapshot)
    _ = validator.validate(snapshot)
    after = _tasks_to_dicts(snapshot)
    assert before == after


def test_validate_with_unknown_sentinel_files(tmp_with_files: Path) -> None:
    """A task whose ``files_to_modify`` is the UNKNOWN sentinel FAILS
    step 3 with the healable-reason.

    2026-09-09 two-constant scheme contract update: the UNKNOWN
    sentinel (``__UNKNOWN_MODIFICATIONS__``) is the dispatcher's
    signal that the subagent fill loop must determine the actual
    file list — it is NO longer a pass. Only the read-only sentinel
    (``__NO_FILE_CHANGES__``) passes step 3 directly.
    """
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    snapshot = [
        SubTask(
            id="sentinel-task",
            title="Sentinel",
            description="",
            test_command="echo noop",
            files_to_modify=[_LOCAL_UNKNOWN_SENTINEL],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "failed", (
        f"UNKNOWN sentinel files_to_modify must fail step 3 (healable "
        f"signal for the subagent fill loop); got {report.model_dump()}"
    )
    assert 3 in report.failed_steps
    # The reason text names the "unknown-modifications sentinel" (the
    # literal ``__UNKNOWN_MODIFICATIONS__`` token may be normalised to
    # lowercase in the message).
    assert any("unknown-modifications sentinel" in r for r in report.reasons), (
        f"step-3 reason must cite the UNKNOWN sentinel so operators "
        f"can distinguish it from the read-only sentinel; got "
        f"{report.reasons!r}"
    )


def test_validate_catches_step_3_missing_file(tmp_with_files: Path) -> None:
    """Step 3 surfaces as a failure when a path under files_to_modify
    does not exist on disk."""
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    snapshot = [
        SubTask(
            id="missing-file",
            title="Missing file",
            description="",
            test_command="echo noop",
            files_to_modify=["definitely_does_not_exist_xyz.py"],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "failed"
    assert 3 in report.failed_steps


def test_validate_catches_step_4_undefined_function(
    tmp_with_files: Path,
) -> None:
    """Step 4 surfaces when test_command references a function
    that does not exist in the referenced file."""
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    p = tmp_with_files / "step4_test.py"
    p.write_text("def test_other():\n    assert True\n", encoding="utf-8")
    snapshot = [
        SubTask(
            id="step4-task",
            title="Step 4",
            description="",
            test_command=f"pytest {p.name}::test_does_not_exist",
            files_to_modify=[p.name],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "failed"
    assert 4 in report.failed_steps


def test_validate_step_4_skips_non_pytest_commands(tmp_with_files: Path) -> None:
    """Commands that do not contain 'pytest' / 'py.test' skip step 4.

    Uses the read-only sentinel so step 3 passes and the report
    isolates the step-4 skip (the UNKNOWN sentinel would fail step 3
    under the 2026-09-09 two-constant scheme).
    """
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    snapshot = [
        SubTask(
            id="echo-task",
            title="Echo task",
            description="",
            test_command="echo hello world",
            files_to_modify=["__NO_FILE_CHANGES__"],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "passed"


def test_validate_reports_step_1_missing_field(tmp_with_files: Path) -> None:
    """Step 1 surfaces when a required field is missing.

    We bypass ``SubTask``'s pydantic validators by constructing the
    object via ``__init__`` with an explicitly empty title."""
    validator = TaskOutputValidator(project_dir=tmp_with_files)
    snapshot = [
        SubTask(
            id="bad-1",
            title="",  # empty -> step 1 fail
            description="",
            test_command="",
            files_to_modify=[_LOCAL_UNKNOWN_SENTINEL],
            depends_on=[],
        )
    ]
    report = validator.validate(snapshot)
    assert report.status == "failed"
    assert 1 in report.failed_steps
