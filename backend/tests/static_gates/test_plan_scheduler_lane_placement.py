"""Static gate: the scheduler's tests live in an existing lane's subdirectory.

Why this gate exists
--------------------
The repository's CI decides what runs by *path*: the unit lane runs
``pytest tests/unit``, the integration lane runs ``pytest tests/integration``,
and so on.  Nothing consults a marker to decide which lane a file belongs
to -- the directory is the whole contract.  That is deliberate (this
repository has already paid for a marker/directory mismatch) and it has
one consequence: a test file in the wrong directory is a test file that
does not run.

There are three ways to be wrong, and the two most expensive ones are
silent:

1. **A parallel tree.**  ``backend/tests/scheduler/{unit,contract,...}``
   looks tidy and is invisible to every lane command, because no lane
   command mentions it.  Making it run means editing the CI commands,
   which turns "the directory decides" into "the directory plus a
   hand-written list decides" -- and a list that misses an entry misses
   silently.  A test that never runs is the cheapest defect in a
   repository and the most expensive one to notice.
2. **A case dropped straight into a lane.**  ``tests/unit/test_x.py`` is
   collected, so it runs in the *default* lane -- which is where an
   integration case with a database and a subprocess slows everybody
   down, or where a case meant for the nightly lane runs on every push.
3. **An empty lane directory.**  The directory exists, the plan says the
   lane was delivered, and the lane collects nothing.  Green, and empty.

What this gate checks
---------------------
The ownership table the test design fixed for the native scheduler (test
decision point 11) puts every scheduler case in
``backend/tests/<existing lane>/plan_scheduler/`` and puts the gate
files themselves at the top level of ``backend/tests/static_gates/``.
This gate reads the tree and asserts exactly that:

1. **No parallel tree.**  ``backend/tests/scheduler/`` does not exist.
2. **Everything is under an existing lane.**  Every scheduler test file
   -- one that lives inside a ``plan_scheduler`` directory, and one that
   announces itself in its own name -- sits either in a lane's
   ``plan_scheduler/`` subdirectory or at the top level of the
   static-gate directory.
3. **Every delivered lane is non-empty.**  Each of the seven lanes owns a
   ``plan_scheduler/`` subdirectory, and each holds at least one file
   pytest would collect.  A lane with nothing collectible in it has
   delivered nothing, whatever the directory tree says.

The paths in every assertion are relative to ``backend/tests``, so the
messages read the same on every machine -- this gate never depends on
where the repository happens to be checked out.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

#: ``backend/tests`` -- this file lives in ``backend/tests/static_gates/``.
TESTS_DIR: Path = Path(__file__).resolve().parents[1]

#: The lane directories that already existed before the native scheduler
#: started, in the order the test design's ownership table lists them.
#: ``contract`` is deliberately absent: the design keeps the scheduler's
#: contract surface in ``api`` rather than spreading it over two lanes.
LANE_DIRS: tuple[str, ...] = (
    "unit",
    "api",
    "integration",
    "e2e",
    "perf",
    "regression",
    "security",
)

#: The static-gate directory.  Its gate files sit at the top level, next
#: to the gates that are already there -- a gate is not a scheduler case
#: and does not belong under a lane.
STATIC_GATES_DIR: str = "static_gates"

#: The name of the parallel tree that must never appear.
FORBIDDEN_TREE: str = "scheduler"

#: The subdirectory each lane's scheduler cases live in.
SUBDIR: str = "plan_scheduler"

#: pytest's default ``python_files`` patterns.  The repository does not
#: override them, so a file matching neither of them is never collected
#: as a test -- it is either a helper or a case that will never run.
PYTEST_FILE_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")

#: Directories that hold no test source.
_SKIPPED_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", ".pytest_cache"})

#: Files that carry no test case.
_SKIPPED_FILE_NAMES: frozenset[str] = frozenset({"__init__.py"})


# ---------------------------------------------------------------------------
# Scanning the test tree
# ---------------------------------------------------------------------------


def relative(path: Path, tests_root: Path = TESTS_DIR) -> str:
    """Return *path* as a POSIX string relative to the test tree.

    Every message this gate prints goes through here, so a failure reads
    ``unit/plan_scheduler/test_x.py`` rather than a path that only means
    something on the machine that ran it.
    """
    return path.relative_to(tests_root).as_posix()


def is_test_file(path: Path) -> bool:
    """Whether pytest would collect *path* as a test module."""
    return any(
        fnmatch.fnmatchcase(path.name, pattern) for pattern in PYTEST_FILE_PATTERNS
    )


def plan_scheduler_dirs(tests_root: Path = TESTS_DIR) -> list[Path]:
    """Every directory named ``plan_scheduler`` under the test tree.

    The directory is the unit of placement: a directory the ownership
    table does not allow puts every file inside it in the wrong place,
    however the files themselves are named.
    """
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(tests_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIR_NAMES)
        if os.path.basename(dirpath) == SUBDIR:
            found.append(Path(dirpath))
    return sorted(found)


def collectible_cases(directory: Path) -> list[Path]:
    """The test files pytest would collect from *directory*.

    ``__init__.py`` and ``conftest.py`` carry no test case, so they do
    not count towards a lane's delivery; neither does anything under
    ``__pycache__``.
    """
    cases: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIR_NAMES)
        for name in sorted(filenames):
            if name in _SKIPPED_FILE_NAMES:
                continue
            path = Path(dirpath) / name
            if is_test_file(path):
                cases.append(path)
    return cases


def scheduler_test_files(tests_root: Path = TESTS_DIR) -> list[Path]:
    """Every scheduler test file, wherever it sits.

    Two ways a file can be a scheduler case: it lives inside a
    ``plan_scheduler`` directory, or its own name says it is one.  The
    second catches a case dropped straight into a lane -- it would still
    be collected, but in the wrong lane, at the wrong moment.
    """
    found: set[Path] = set()
    for directory in plan_scheduler_dirs(tests_root):
        found.update(collectible_cases(directory))
    for dirpath, dirnames, filenames in os.walk(tests_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIR_NAMES)
        for name in sorted(filenames):
            if name in _SKIPPED_FILE_NAMES or not name.endswith(".py"):
                continue
            if SUBDIR not in name:
                continue
            path = Path(dirpath) / name
            if is_test_file(path):
                found.add(path)
    return sorted(found)


# ---------------------------------------------------------------------------
# Where a file is allowed to live
# ---------------------------------------------------------------------------


def lane_of_dir(path: Path, tests_root: Path = TESTS_DIR) -> str | None:
    """The lane a ``plan_scheduler`` directory belongs to, or ``None``.

    ``None`` means the directory is somewhere the ownership table does
    not allow: a parallel tree, a lane the table does not list, or a
    nested tree one level deeper than the table puts it.
    """
    parent = path.parent
    if parent.parent.resolve() != tests_root.resolve():
        return None
    return parent.name if parent.name in LANE_DIRS else None


def lane_of_file(path: Path, tests_root: Path = TESTS_DIR) -> str | None:
    """The lane a test file belongs to, or ``None`` when it is misplaced.

    A file is legally placed when it sits in a lane's ``plan_scheduler/``
    subdirectory, or at the top level of the static-gate directory.
    """
    try:
        parts = path.relative_to(tests_root).parts
    except ValueError:
        return None
    if len(parts) == 2 and parts[0] == STATIC_GATES_DIR:
        return STATIC_GATES_DIR
    if len(parts) >= 3 and parts[0] in LANE_DIRS and parts[1] == SUBDIR:
        return parts[0]
    return None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_parallel_scheduler_tree() -> None:
    """``backend/tests/scheduler/`` must not exist.

    A parallel tree is not collected by any lane command, so its cases
    never run -- and the lanes that skip them stay green.  The tree only
    becomes visible to CI if every lane command is edited to mention it,
    and a lane command that misses the tree is a lane that silently
    stops running its cases.  The directory tree has to stay the whole
    contract, so the tree is refused outright.
    """
    parallel = TESTS_DIR / FORBIDDEN_TREE
    assert not parallel.exists(), (
        f"`backend/tests/{FORBIDDEN_TREE}/` exists. That parallel tree is "
        f"not on any lane command's path, so the cases inside it never run "
        f"while the lanes that skip them report green. Put the cases in "
        f"`backend/tests/<lane>/{SUBDIR}/` instead, where the existing "
        f"lane commands already collect them."
    )


def test_all_files_under_existing_lane() -> None:
    """Every scheduler test file sits in an existing lane's subdirectory.

    A file that lives outside the ownership table is either invisible to
    CI (a parallel tree) or collected by the wrong lane (a case dropped
    into the lane root, where it slows down every default-lane push).
    """
    directories = plan_scheduler_dirs()
    assert directories, (
        "no `plan_scheduler` directory was found under `backend/tests`. "
        "The scan is broken -- it reads nothing, so the assertion below "
        "would pass on an empty set and check nothing."
    )

    misplaced_dirs = [d for d in directories if lane_of_dir(d) is None]
    assert not misplaced_dirs, (
        "these `plan_scheduler` directories are not the subdirectory of an "
        "existing lane, so the lane commands do not collect them:\n  "
        + "\n  ".join(relative(d) for d in misplaced_dirs)
    )

    files = scheduler_test_files()
    assert files, (
        "no scheduler test file was found under `backend/tests`. The scan "
        "is broken -- it reads nothing, so the assertion below would pass "
        "on an empty set and check nothing."
    )

    misplaced = [f for f in files if lane_of_file(f) is None]
    assert not misplaced, (
        "these scheduler test files are not under an existing lane's "
        f"`{SUBDIR}/` subdirectory (nor at the top level of "
        f"`{STATIC_GATES_DIR}/`):\n  "
        + "\n  ".join(relative(f) for f in misplaced)
    )


def test_each_lane_dir_nonempty() -> None:
    """Every lane the ownership table names holds at least one case.

    A lane directory that exists but holds nothing collectible is a lane
    that delivers nothing: the tree looks right, the lane runs nothing,
    and the job reports green.
    """
    for lane in LANE_DIRS:
        directory = TESTS_DIR / lane / SUBDIR
        assert directory.is_dir(), (
            f"`backend/tests/{lane}/{SUBDIR}/` does not exist. The "
            f"ownership table puts the scheduler's `{lane}` cases there, "
            f"and the `{lane}` lane command (`pytest tests/{lane}`) "
            f"collects them from there."
        )
        cases = collectible_cases(directory)
        assert cases, (
            f"`backend/tests/{lane}/{SUBDIR}/` holds no file pytest would "
            f"collect as a test (pytest collects "
            f"{', '.join(PYTEST_FILE_PATTERNS)}). The lane exists, runs "
            f"nothing, and reports green."
        )


# ---------------------------------------------------------------------------
# Sensitivity -- the rules above have to be able to fail
# ---------------------------------------------------------------------------


def test_the_placement_rule_reads_the_ownership_table(tmp_path: Path) -> None:
    """The rule must accept the table's layout and refuse the rest.

    A rule that cannot fail is a rule nothing enforces.  The detector is
    handed one legal path per legal shape and three illegal ones -- a
    parallel tree, a lane the table does not list, and a gate that was
    put under a subdirectory instead of at the top level -- and has to
    tell them apart.
    """
    assert lane_of_dir(tmp_path / "unit" / SUBDIR, tmp_path) == "unit"
    assert lane_of_dir(tmp_path / FORBIDDEN_TREE / SUBDIR, tmp_path) is None
    assert lane_of_dir(tmp_path / STATIC_GATES_DIR / SUBDIR, tmp_path) is None
    assert lane_of_dir(tmp_path / "unit" / "nested" / SUBDIR, tmp_path) is None

    assert lane_of_file(tmp_path / "unit" / SUBDIR / "test_case.py", tmp_path) == "unit"
    assert (
        lane_of_file(tmp_path / STATIC_GATES_DIR / "test_gate.py", tmp_path)
        == STATIC_GATES_DIR
    )
    assert (
        lane_of_file(tmp_path / FORBIDDEN_TREE / "test_case.py", tmp_path) is None
    )
    assert lane_of_file(tmp_path / "smoke" / SUBDIR / "test_case.py", tmp_path) is None
    assert (
        lane_of_file(tmp_path / "unit" / "test_case.py", tmp_path) is None
    )
    assert (
        lane_of_file(
            tmp_path / STATIC_GATES_DIR / SUBDIR / "test_gate.py", tmp_path
        )
        is None
    )


def test_an_empty_lane_directory_delivers_nothing(tmp_path: Path) -> None:
    """A lane directory with nothing collectible in it is not a delivery.

    ``__init__.py`` and ``conftest.py`` are not test cases: pytest loads
    them, it never runs them.  A directory holding only those two has
    the shape of a delivery and none of its substance.
    """
    directory = tmp_path / "unit" / SUBDIR
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / "conftest.py").write_text("", encoding="utf-8")
    assert collectible_cases(directory) == []

    (directory / "test_case.py").write_text("", encoding="utf-8")
    assert [p.name for p in collectible_cases(directory)] == ["test_case.py"]