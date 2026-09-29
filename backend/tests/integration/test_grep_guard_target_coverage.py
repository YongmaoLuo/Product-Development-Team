"""Integration test for grep-guard target coverage.

Background
----------
The grep-guard scanner (``backend/tests/grep_guard.py::run_grep_guard``)
is the reusable shim that downstream task consumers run against the
production source tree.  The 5 grep gate tests in
``backend/tests/test_server_json_cleanup.py`` already pin Defense 1 for
exactly three production locations (server.py, verification_executor.py,
state_machine/**/*.py), but they hard-code the scan targets and call
``pytest.fail`` on a hit.

This integration test pins the **coverage** contract: when the scanner
is pointed at all six production target paths the brief calls out
(server.py, verification_executor.py, plus every ``*.py`` under
``state_machine/repositories``, ``state_machine/db``, and
``state_machine/services``), it must walk every file and report each
planted violation with the correct ``pattern_type``.

Why an integration test (not just a unit test)
----------------------------------------------
The scanner's path enumeration, recursion, and exclusion logic
(``tests/`` subtrees skipped) are exercised against a realistic
``fixture_tree_with_violations`` shape.  A unit test that simply
points at a single tmp dir would not catch a regression where, e.g.,
a nested package directory is silently skipped, or where the scanner
returns an empty result against a fixture that contains a real
violation.

TDD contract
------------
- ``test_grep_guard_scans_all_production_targets``: build a fixture
  tree shaped like the production source layout (server.py,
  verification_executor.py, and a nested state_machine package with
  ``repositories/``, ``db/``, ``services/`` sub-packages, each
  containing ``*.py`` files); plant one violation per sub-tree using
  a DIFFERENT forbidden filename in each; invoke ``run_grep_guard``
  with the matching target_files + target_dirs; assert every planted
  violation is reported with the correct ``pattern_type`` and that
  no sub-tree was silently skipped.

Boundary conditions enforced:

  * nested ``.py`` files under a package directory MUST be scanned
    (e.g. ``state_machine/repositories/routing_repository.py``);
  * non-Python files (``.txt``, ``.md``) MUST be ignored — the
    scanner's rglob selector is ``*.py`` and we want to lock that in;
  * ``tests/`` subtrees under ``state_machine/`` MUST be skipped —
    plant one violation inside ``state_machine/tests/`` and assert
    it is NOT reported (regression in either direction would be a
    contract violation);
  * every violation's ``pattern_type`` is one of the five documented
    classes (direct_literal, string_concat, variable_reference,
    fstring_format, pathlib_join).

The 6 fixture paths correspond 1:1 to the production tree shape::

    fixture_tree/
        server.py
        verification_executor.py
        state_machine/
            repositories/
                routing_repository.py
                verification_repository.py
            db/
                connection.py
            services/
                scheduler_support.py
            tests/
                test_smoke.py    # excluded by the scanner

Each production-shaped fixture file is planted with ONE violation of
ONE pattern class using a DIFFERENT forbidden filename so the
integration test can assert "every fixture was scanned AND every
violation was reported" without ambiguity.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure ``backend/tests/`` is on sys.path so the standalone
# ``grep_guard`` module is importable.  ``pytest.ini`` declares
# ``pythonpath = . backend tools`` but the explicit manipulation makes
# the test self-contained.
_BACKEND_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_TESTS_DIR))


from grep_guard import run_grep_guard  # noqa: E402


# The 5 pattern classes the scanner must emit.  Mirrors
# ``PATTERN_TYPES`` in ``backend/tests/unit/test_grep_guard.py``.
PATTERN_TYPES = frozenset(
    {
        "direct_literal",
        "string_concat",
        "variable_reference",
        "fstring_format",
        "pathlib_join",
    }
)


def fixture_tree_with_violations(root: Path) -> dict[str, Path]:
    """Build a fixture tree shaped like the production source layout.

    Creates under ``root`` the following layout (1:1 with the
    production tree):

        ``root``/
            server.py                          # direct_literal
            verification_executor.py           # string_concat
            state_machine/
                repositories/
                    routing_repository.py       # variable_reference
                    verification_repository.py  # fstring_format
                db/
                    connection.py               # pathlib_join
                services/
                    scheduler_support.py        # direct_literal (2nd)
                tests/
                    test_smoke.py               # excluded by the scanner
            README.txt                          # ignored (non-.py)
            CHANGELOG.md                        # ignored (non-.py)

    Each production-shaped ``.py`` file is planted with ONE violation
    using a DIFFERENT forbidden filename so the assertion can verify
    "every fixture was scanned AND every violation was reported"
    without ambiguity.

    Returns a dict mapping ``"label" -> path`` so the test body can
    pin assertions on the labeled fixtures by reference rather than
    reconstructing paths.

    Why include ``tests/`` + ``*.txt`` / ``*.md``?

    * The ``tests/`` subtree under ``state_machine/`` MUST be
      excluded by the scanner (test code is allowed to mention
      forbidden filenames).  The test plants a violation inside
      ``state_machine/tests/test_smoke.py`` and asserts it is NOT
      reported — that proves the exclusion works against a realistic
      package shape.
    * The non-Python files (``README.txt``, ``CHANGELOG.md``) prove
      the scanner's ``rglob('*.py')`` selector filters them out even
      when they are siblings of a scanned file.
    """
    paths: dict[str, Path] = {}

    paths["server.py"] = root / "server.py"
    paths["server.py"].parent.mkdir(parents=True, exist_ok=True)
    paths["server.py"].write_text(
        '"""Fixture mirroring production backend/server.py shape."""\n'
        'PLAN_STATE_FILENAME = "plan_state.json"\n',
        encoding="utf-8",
    )

    paths["verification_executor.py"] = root / "verification_executor.py"
    paths["verification_executor.py"].write_text(
        '"""Fixture mirroring production backend/verification_executor.py shape."""\n'
        'EXEC_FILE = "executi" + "on.json"\n',
        encoding="utf-8",
    )

    # --- state_machine/ (nested package) ------------------------------
    state_machine = root / "state_machine"
    repositories = state_machine / "repositories"
    db_dir = state_machine / "db"
    services = state_machine / "services"

    paths["routing_repository.py"] = repositories / "routing_repository.py"
    paths["routing_repository.py"].parent.mkdir(parents=True, exist_ok=True)
    paths["routing_repository.py"].write_text(
        '"""Fixture mirroring state_machine/repositories/routing_repository.py."""\n'
        '_RT_FN_ROUTING = "routing_runtime_state"\n',
        encoding="utf-8",
    )

    paths["verification_repository.py"] = repositories / "verification_repository.py"
    paths["verification_repository.py"].write_text(
        '"""Fixture mirroring state_machine/repositories/verification_repository.py."""\n'
        'PREFIX = "verifier"; '
        'v_filename = f"{PREFIX}_executor_state.json"\n',
        encoding="utf-8",
    )

    paths["connection.py"] = db_dir / "connection.py"
    paths["connection.py"].parent.mkdir(parents=True, exist_ok=True)
    paths["connection.py"].write_text(
        '"""Fixture mirroring state_machine/db/connection.py."""\n'
        'PROGRESS_PATH = Path(base) / "verification_progress_state.json"\n',
        encoding="utf-8",
    )

    paths["scheduler_support.py"] = services / "scheduler_support.py"
    paths["scheduler_support.py"].parent.mkdir(parents=True, exist_ok=True)
    paths["scheduler_support.py"].write_text(
        '"""Fixture mirroring state_machine/services/scheduler_support.py."""\n'
        'RUNTIME_STATE_FILENAME = "verification_runtime_state.json"\n',
        encoding="utf-8",
    )

    # --- state_machine/tests/ (MUST be excluded by the scanner) --------
    sm_tests = state_machine / "tests"
    sm_tests.mkdir(parents=True, exist_ok=True)
    paths["state_machine_tests_test_smoke.py"] = sm_tests / "test_smoke.py"
    paths["state_machine_tests_test_smoke.py"].write_text(
        '"""Fixture under state_machine/tests/ — scanner MUST skip this."""\n'
        '_RT_FN_TEST_EXCLUDED = "plan_state.json"\n',
        encoding="utf-8",
    )

    # --- Non-Python siblings (MUST be ignored by the scanner) ---------
    paths["README.txt"] = root / "README.txt"
    paths["README.txt"].write_text(
        'plan_state.json\n',
        encoding="utf-8",
    )
    paths["CHANGELOG.md"] = root / "CHANGELOG.md"
    paths["CHANGELOG.md"].write_text(
        '# verification_runtime_state.json mentions here are not code\n',
        encoding="utf-8",
    )

    return paths


# The 6 production-shaped fixtures the scanner MUST scan, paired with
# the pattern_type the scanner is expected to attribute to each
# planted violation.  Each fixture plants a DIFFERENT forbidden
# filename so the test can disambiguate which fixture's violation
# surfaced (and which was skipped).
EXPECTED_COVERAGE: list[tuple[str, str, str]] = [
    # (fixture_label, expected_pattern_type, expected_matched_substring)
    ("server.py", "direct_literal", "plan_state.json"),
    ("verification_executor.py", "string_concat", "execution.json"),
    ("routing_repository.py", "variable_reference", "_RT_FN_ROUTING"),
    ("verification_repository.py", "fstring_format", "_executor_state.json"),
    ("connection.py", "pathlib_join", "verification_progress_state.json"),
    ("scheduler_support.py", "direct_literal", "verification_runtime_state.json"),
]


@pytest.mark.integration
def test_grep_guard_scans_all_production_targets(tmp_path: Path) -> None:
    """Six production-shaped targets with planted violations → all reported.

    Builds a fixture tree shaped exactly like the production source
    layout (server.py, verification_executor.py, plus
    state_machine/{repositories,db,services}/*.py) and plants one
    forbidden-construction violation in each production-shaped file
    using a DIFFERENT forbidden filename.  Then invokes
    ``run_grep_guard`` against the matching ``target_files`` and
    ``target_dirs`` and asserts:

      * every planted violation is reported;
      * the ``pattern_type`` for each match matches the expected
        class;
      * the ``state_machine/tests/test_smoke.py`` violation is NOT
        reported (the scanner MUST exclude ``tests/`` subtrees);
      * the non-Python files (``README.txt``, ``CHANGELOG.md``) are
        NOT reported (the scanner's rglob selector is ``*.py``);
      * the 6 expected pattern_types are all present in the output
        (i.e. {"direct_literal", "string_concat", "variable_reference",
        "fstring_format", "pathlib_join"} — note ``direct_literal``
        fires twice because two fixtures plant direct literals, but
        that still satisfies the "all 5 classes represented"
        contract).

    This is the canonical "the scanner covers the whole production
    tree" contract — a regression that silently drops a nested
    package directory or forgets to recurse would fail this test.
    """
    paths = fixture_tree_with_violations(tmp_path)

    # Build the target_files / target_dirs lists exactly the way a
    # real downstream caller (task 5) would: two top-level files +
    # the state_machine package directory (recursed into for *.py).
    target_files = [
        paths["server.py"],
        paths["verification_executor.py"],
    ]
    target_dirs = [tmp_path / "state_machine"]

    violations = run_grep_guard(
        root=tmp_path,
        target_files=target_files,
        target_dirs=target_dirs,
    )

    # ---- Shape: list of dicts with the 4 documented keys ------------
    assert isinstance(violations, list), (
        f"run_grep_guard must return a list, got {type(violations).__name__}"
    )
    for v in violations:
        assert isinstance(v, dict)
        assert set(v.keys()) == {"file", "line", "pattern_type", "matched_text"}, (
            f"violation keys must be exactly {{file, line, pattern_type, matched_text}}, "
            f"got {sorted(v.keys())!r}"
        )
        assert isinstance(v["file"], str)
        assert isinstance(v["line"], int) and v["line"] >= 1
        assert v["pattern_type"] in PATTERN_TYPES, (
            f"pattern_type must be one of {sorted(PATTERN_TYPES)}, "
            f"got {v['pattern_type']!r}"
        )
        assert isinstance(v["matched_text"], str) and v["matched_text"]

    # ---- Exclusions: tests/ subtree and non-.py files ---------------
    # The fixture under state_machine/tests/ MUST be excluded.
    excluded_files = {
        paths["state_machine_tests_test_smoke.py"].name,
    }
    seen_filenames = {Path(v["file"]).name for v in violations}
    leaked_excluded = seen_filenames & excluded_files
    assert not leaked_excluded, (
        f"run_grep_guard leaked {len(leaked_excluded)} violation(s) from "
        f"the tests/ subtree — these MUST be excluded. Observed files: "
        f"{sorted(seen_filenames)!r}"
    )

    # Non-Python files MUST be ignored.  The scanner's rglob selector
    # is ``*.py``; if a .txt or .md filename shows up in violations,
    # the selector regressed.
    non_py = {
        paths["README.txt"].name,
        paths["CHANGELOG.md"].name,
    }
    leaked_non_py = seen_filenames & non_py
    assert not leaked_non_py, (
        f"run_grep_guard scanned non-Python files {leaked_non_py!r} — "
        f"the rglob('*.py') selector regressed"
    )

    # ---- Per-fixture coverage: every production fixture was scanned --
    # Group violations by fixture basename so we can assert
    # "every fixture's planted violation surfaced".
    by_fixture: dict[str, list[dict]] = {}
    for v in violations:
        by_fixture.setdefault(Path(v["file"]).name, []).append(v)

    for fixture_label, expected_type, expected_substring in EXPECTED_COVERAGE:
        fixture_path = paths[fixture_label]
        fixture_basename = fixture_path.name
        fixture_violations = by_fixture.get(fixture_basename, [])
        assert fixture_violations, (
            f"run_grep_guard did NOT report any violation for production "
            f"fixture {fixture_label!r} ({fixture_path}) — the scanner "
            f"silently skipped this file. Observed fixture files: "
            f"{sorted(by_fixture.keys())!r}"
        )
        # At least one violation must carry the expected pattern_type
        # and matched_text substring.
        matched = [
            v for v in fixture_violations
            if v["pattern_type"] == expected_type
            and expected_substring in v["matched_text"]
        ]
        assert matched, (
            f"fixture {fixture_label!r} ({fixture_path}) did NOT produce "
            f"a {expected_type!r} violation matching {expected_substring!r}. "
            f"Observed violations for this fixture: {fixture_violations!r}"
        )

    # ---- 5 pattern classes are all represented ----------------------
    seen_pattern_types = {v["pattern_type"] for v in violations}
    expected_classes = {
        "direct_literal",
        "string_concat",
        "variable_reference",
        "fstring_format",
        "pathlib_join",
    }
    missing_classes = expected_classes - seen_pattern_types
    assert not missing_classes, (
        f"run_grep_guard coverage is incomplete — missing pattern "
        f"classes: {sorted(missing_classes)!r}. Observed pattern_types: "
        f"{sorted(seen_pattern_types)!r}"
    )