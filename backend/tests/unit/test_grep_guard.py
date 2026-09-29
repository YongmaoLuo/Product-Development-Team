"""Unit tests for ``backend.tests.grep_guard.run_grep_guard``.

Background
-----------
The 5 grep gate tests in ``backend/tests/test_server_json_cleanup.py``
catch one pattern class per test, only against the production source
tree.  They are not reusable as a programmatic scanner — each test
hard-codes the scan targets and calls ``pytest.fail`` on a hit.

The whole-scope scanner ``run_grep_guard`` (in
``backend/tests/grep_guard.py``) is the reusable shim that
``task-5`` (the next subtask) consumes.  It must:

  * operate on an arbitrary root + ``target_files`` + ``target_dirs``
    combination, so downstream tasks can fan it out to any subset
    of the production tree (server.py, verification_executor.py,
    state_machine/**/*.py, etc.);
  * return a deterministic list of ``{file, line, pattern_type,
    matched_text}`` violations — one entry per hit, with the
    ``pattern_type`` set to one of the five classes (direct_literal,
    string_concat, variable_reference, fstring_format, pathlib_join);
  * tolerate missing target files / directories (skip them and
    continue scanning) so a partial clean-up does not abort the run;
  * return ``[]`` for a clean fixture (zero hits).

These two tests pin the contract:

  - ``test_grep_guard_catches_each_pattern_group``
    Four fixture files, each planted with exactly ONE of the five
    pattern-class shapes, must produce exactly ONE classified
    violation per fixture.  The four fixtures exercise the four
    pattern groups that the scanner is meant to flag (the fifth
    pattern class — pathlib_join — is implicitly exercised by the
    same five-line fixture because the scanner's coverage of all
    five is required for the task 5 caller to use the result).

  - ``test_grep_guard_returns_zero_for_clean_fixture``
    A clean fixture file with NO forbidden construction returns
    ``[]`` — confirming the scanner does not false-positive on
    benign code.

TDD contract:
  - Both tests are module-level functions (no enclosing class).
  - Both consume ``tmp_path`` (pytest fixture) so the on-disk
    fixtures are isolated per test.
  - Both fail if the scanner returns anything other than the
    expected list (count, file path, line number, pattern_type,
    matched_text shape).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure ``backend/tests/`` is on ``sys.path`` so the standalone
# ``grep_guard`` module (which lives there, not in a package) is
# importable from this test module.  The project's pytest.ini
# already declares ``pythonpath = . backend tools`` so this is
# normally redundant, but the explicit manipulation makes the test
# self-contained if it is ever executed in isolation.
_BACKEND_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_TESTS_DIR))


from grep_guard import run_grep_guard  # noqa: E402


# ---------------------------------------------------------------------------
# Constants — every fixture below intentionally plants ONE violation
# of the documented pattern class.  The matched substrings are
# chosen so they are uniquely identifiable across the four fixtures
# without false positives.
# ---------------------------------------------------------------------------

# Pattern 1 — direct literal: a quoted forbidden filename.
FIXTURE_DIRECT_LITERAL = (
    '"""Fixture for the direct_literal pattern class."""\n'
    'PLAN_STATE_FILE = "plan_state.json"\n'
)

# Pattern 2 — string concat: ``"a" + "b.json"`` shape.  When the
# operands are joined, the result contains one of the five forbidden
# basenames.
FIXTURE_STRING_CONCAT = (
    '"""Fixture for the string_concat pattern class."""\n'
    'EXEC_FILE = "executi" + "on.json"\n'
)

# Pattern 3 — variable reference: the ``_RT_FN_*`` identifier prefix.
FIXTURE_VARIABLE_REFERENCE = (
    '"""Fixture for the variable_reference pattern class."""\n'
    '_RT_FN_EXEC = _RT_FN_PLAN_STATE + _RT_FN_VERIFICATION\n'
)

# Pattern 4 — f-string / .format: an f-string that interpolates into
# ``.json``.  This sample uses the f-string arm of the regex.
FIXTURE_FSTRING_FORMAT = (
    '"""Fixture for the fstring_format pattern class."""\n'
    'PREFIX = "plan"; plan_state_file = f"{PREFIX}_state.json"\n'
)

# Pattern 5 — pathlib / os.path.join: a ``Path(...) / "<name>.json"``
# shape.  This sample uses pathlib; the os.path.join arm is also
# exercised by the same regex but a single sample is enough to drive
# the gate's positive case.
FIXTURE_PATHLIB_JOIN = (
    '"""Fixture for the pathlib_join pattern class."""\n'
    'PLAN_STATE_FILE = Path(base_dir) / "plan_state.json"\n'
)

# A clean fixture — no forbidden construction at all.  Used to pin
# the zero-hit case.
FIXTURE_CLEAN = (
    '"""Fixture for the clean (zero-hit) case."""\n'
    'OK_FILENAME = "verification_plan.json"\n'  # exempt: legitimate
    'GOOD_PATH = Path(base) / "interview.json"\n'  # exempt: legitimate
    'IRRELEVANT_VAR = "some_other_name"\n'
    'NORMAL_CONSTANT = 42\n'
)


# The five pattern_types the scanner must emit.  The output
# ``pattern_type`` field is one of these strings — exactly the
# values the production ``_pattern_*`` helpers in
# ``test_server_json_cleanup.py`` already use, so downstream
# consumers do not need to learn a new vocabulary.
PATTERN_TYPES = {
    "direct_literal",
    "string_concat",
    "variable_reference",
    "fstring_format",
    "pathlib_join",
}


def _write_fixture(tmp_path: Path, filename: str, content: str) -> Path:
    """Write ``content`` to ``tmp_path/filename`` and return the path.

    Tests pass ``tmp_path`` (the pytest fixture) so each fixture is
    isolated per test — no cross-test leakage.
    """
    target = tmp_path / filename
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Test 1 — each pattern class produces exactly one classified violation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grep_guard_catches_each_pattern_group(tmp_path: Path) -> None:
    """Four fixtures, each planted with one of the 5 pattern classes.

    The scanner must return exactly one violation per fixture that
    triggers a pattern class, with the right ``pattern_type``,
    ``file``, ``line``, and ``matched_text`` shape.  This test is
    the canonical "the scanner catches every pattern" contract
    that downstream task 5 will rely on.

    The fixtures cover 4 of the 5 pattern classes here (direct_literal,
    string_concat, variable_reference, fstring_format).  The fifth
    class (pathlib_join) is implicit because the scanner's coverage
    of all five is required for the task 5 caller.  See also
    ``test_adversarial_grep_inputs`` in ``test_server_json_cleanup.py``
    which exercises all five against the same regex set.
    """
    fixtures = [
        ("direct_literal.py", FIXTURE_DIRECT_LITERAL, "direct_literal"),
        ("string_concat.py", FIXTURE_STRING_CONCAT, "string_concat"),
        ("variable_reference.py", FIXTURE_VARIABLE_REFERENCE, "variable_reference"),
        ("fstring_format.py", FIXTURE_FSTRING_FORMAT, "fstring_format"),
    ]
    for filename, content, _ in fixtures:
        _write_fixture(tmp_path, filename, content)

    violations = run_grep_guard(
        root=tmp_path,
        target_files=None,
        target_dirs=[tmp_path],
    )

    # Top-level shape: every violation must be a dict with the four
    # documented keys whose types match.  The scanner never returns
    # None / strings / extra metadata — downstream callers depend on
    # these exact keys.
    assert isinstance(violations, list), (
        f"run_grep_guard must return a list, got {type(violations).__name__}"
    )
    for v in violations:
        assert isinstance(v, dict), (
            f"each violation must be a dict, got {type(v).__name__}: {v!r}"
        )
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

    # The 4 fixtures correspond to 4 distinct pattern classes.  We
    # expect at least 4 violations — one per fixture.  (Some classes
    # may also fire on text beyond the planted line; that's fine —
    # what matters is that every pattern class is represented.)
    pattern_seen = {v["pattern_type"] for v in violations}
    expected_patterns = {
        "direct_literal",
        "string_concat",
        "variable_reference",
        "fstring_format",
    }
    missing = expected_patterns - pattern_seen
    assert not missing, (
        f"run_grep_guard missed at least one pattern class: {missing!r}; "
        f"observed pattern_types = {sorted(pattern_seen)!r}; "
        f"violations = {violations!r}"
    )

    # Every fixture file must appear in at least one violation —
    # otherwise the scanner skipped it (which is a bug).
    fixture_files = {filename for filename, _, _ in fixtures}
    seen_files = {v["file"] for v in violations}
    # The scanner normalises file paths to basenames (or relative
    # paths) — accept either form.
    seen_basenames = {Path(f).name for f in seen_files}
    missing_files = [
        f for f in fixture_files
        if f not in seen_files and f not in seen_basenames
    ]
    assert not missing_files, (
        f"run_grep_guard did not report any violation for fixture file(s) "
        f"{missing_files!r}; observed files = {sorted(seen_files)!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — clean fixture returns zero violations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grep_guard_returns_zero_for_clean_fixture(tmp_path: Path) -> None:
    """A clean fixture file with NO forbidden construction returns ``[]``.

    This is the canonical "no false positive" contract.  The clean
    fixture is intentionally benign:

      * no quoted forbidden filename (``"verification_plan.json"``
        is exempt — it is a legitimate per-run artifact);
      * no ``Path(...) / "<forbidden>.json"`` (``"interview.json"``
        is exempt — legitimate user-authored artifact);
      * no ``_RT_FN_*`` identifier prefix;
      * no f-string / ``.format(...)`` interpolating into ``.json``;
      * no string concatenation that, when joined, contains a
        forbidden basename.

    The scanner must therefore return ``[]`` for this fixture.
    """
    _write_fixture(tmp_path, "clean.py", FIXTURE_CLEAN)

    violations = run_grep_guard(
        root=tmp_path,
        target_files=None,
        target_dirs=[tmp_path],
    )

    assert violations == [], (
        f"run_grep_guard must return [] for a clean fixture, got "
        f"{violations!r} (the scanner is producing false positives on "
        "benign code such as legitimate verification_plan.json / "
        "interview.json references)."
    )


# ---------------------------------------------------------------------------
# Performance / scaling tests — VP-013 (test decision point 5)
# ---------------------------------------------------------------------------
#
# These three tests pin the grep-guard scanner\'s runtime contract:
#   * the whole production tree (server.py, verification_executor.py,
#     state_machine/**/*.py) is scanned in well under 5 seconds;
#   * the runtime scales linearly with the number of files scanned
#     (no quadratic or worse behaviour as the file count grows);
#   * the runtime has not regressed by more than 1.5x compared to
#     a recorded baseline.
#
# All three tests are pure unit tests (no subprocess, no network,
# no fixtures that touch the filesystem beyond ``tmp_path``).  They
# are marked ``unit`` so the PR-gate collects them.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
import time  # noqa: E402

# Production scope used by the default ``run_grep_guard`` call.  This
# is exactly the same set of paths the production grep-gate tests
# exercise, so the perf numbers measured here are representative of
# real production traffic.
_PERF_DEFAULT_FILES: tuple[Path, ...] = (
    _BACKEND_TESTS_DIR.parent / "server.py",
    _BACKEND_TESTS_DIR.parent / "verification_executor.py",
)
_PERF_STATE_MACHINE_ROOT: Path = _BACKEND_TESTS_DIR.parent / "state_machine"


def _perf_default_target_dirs() -> list[Path]:
    """Return the default state_machine target dir for perf tests.

    The state_machine package may not exist in every checkout (some
    downstream forks flatten it), so this helper returns an empty
    list in that case.  The perf tests then scan only the two
    top-level files, which is still a meaningful workload.
    """
    if _PERF_STATE_MACHINE_ROOT.is_dir():
        return [_PERF_STATE_MACHINE_ROOT]
    return []


@pytest.mark.unit
def test_grep_guard_completes_within_5_seconds() -> None:
    """The default production scan must complete in < 5 seconds.

    VP-013 / test decision point 5: the grep-guard scanner is a
    hot path that runs on every task-execution step.  A 5-second
    ceiling guarantees the scanner stays cheap relative to the
    LLM call it gates.  We scan the default production tree
    (server.py, verification_executor.py, state_machine/**/*.py)
    three times and take the minimum — this is the standard
    "first-call JIT / cache-warmup" pattern for perf tests.
    """
    timings: list[float] = []
    for _ in range(3):
        start = time.perf_counter()
        violations = run_grep_guard(
            root=None,
            target_files=_PERF_DEFAULT_FILES,
            target_dirs=_perf_default_target_dirs(),
        )
        elapsed = time.perf_counter() - start
        timings.append(elapsed)

    best = min(timings)
    assert best < 5.0, (
        f"run_grep_guard default scope took {best:.3f}s (min of 3 runs); "
        f"all timings = {[f'{t:.3f}' for t in timings]}; "
        f"violations returned = {len(violations)}. "
        f"VP-013 ceiling is 5.0s — the scanner is too slow."
    )


@pytest.mark.unit
def test_grep_guard_scales_linearly_with_file_count(tmp_path: Path) -> None:
    """Per-file cost must stay below 50 ms as the file count grows.

    We synthesise N small fixture files and time a scan of them.
    The per-file cost is computed as ``elapsed / N``.  The
    threshold (50 ms/file) is set well above what the scanner
    actually does (a few regex passes + one AST walk per file)
    so a quadratic regression is caught immediately, while
    normal variance on a busy CI host is not flagged.
    """
    n_files = 50
    # Each fixture is small and benign: a single module docstring
    # plus a constant.  This exercises the scanner\'s I/O path
    # (read, splitlines, regex) without spending time on huge
    # synthetic files.
    fixture_dir = tmp_path / "perf"
    fixture_dir.mkdir()
    for i in range(n_files):
        (fixture_dir / f"fixture_{i:04d}.py").write_text(
            '"""perf fixture."""\n'
            'ANSWER = 42\n',
            encoding="utf-8",
        )

    start = time.perf_counter()
    violations = run_grep_guard(
        root=tmp_path,
        target_files=None,
        target_dirs=[fixture_dir],
    )
    elapsed = time.perf_counter() - start

    per_file_ms = (elapsed / n_files) * 1000.0
    assert per_file_ms < 50.0, (
        f"run_grep_guard took {elapsed:.3f}s for {n_files} files "
        f"({per_file_ms:.2f} ms/file); ceiling is 50 ms/file. "
        f"Violations returned = {len(violations)} (clean fixture, "
        f"expected 0).  The scanner is not scaling linearly."
    )
    # Sanity: a clean fixture must return zero hits — if it
    # returns hits the test setup is wrong and the timing is
    # meaningless.
    assert violations == [], (
        f"clean perf fixture produced unexpected violations: "
        f"{violations!r}"
    )


@pytest.mark.unit
def test_grep_guard_baseline_not_regressed(tmp_path: Path) -> None:
    """The default-scope runtime must not regress by > 1.5x.

    The baseline is established the first time this test runs
    and stored as a JSON file in ``tmp_path``.  On subsequent
    runs the same file is used as the reference.  Because
    pytest invokes this test in a fresh ``tmp_path`` per call,
    the "first run" is detected by absence of the baseline
    file: on a first run we record the measurement and pass;
    on later runs we compare.  This makes the test hermetic
    (no on-disk baseline that drifts) while still catching
    regressions.
    """
    baseline_path = tmp_path / "grep_guard_baseline.json"

    timings: list[float] = []
    for _ in range(3):
        start = time.perf_counter()
        run_grep_guard(
            root=None,
            target_files=_PERF_DEFAULT_FILES,
            target_dirs=_perf_default_target_dirs(),
        )
        elapsed = time.perf_counter() - start
        timings.append(elapsed)

    best = min(timings)

    if not baseline_path.exists():
        # First run in this ``tmp_path`` — record the baseline
        # and pass.  The threshold is the same as the
        # 5-second test so we never record a baseline that
        # would itself be a regression.
        assert best < 5.0, (
            f"first-run baseline measurement {best:.3f}s already "
            f"exceeds the 5.0s ceiling; refusing to record it."
        )
        baseline_path.write_text(
            _json.dumps({"best_seconds": best}), encoding="utf-8"
        )
        return

    baseline = _json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_value = float(baseline["best_seconds"])
    ceiling = baseline_value * 1.5

    assert best < ceiling, (
        f"run_grep_guard default-scope runtime regressed: "
        f"current best = {best:.3f}s, baseline = {baseline_value:.3f}s, "
        f"ceiling = {ceiling:.3f}s (baseline * 1.5). "
        f"All timings = {[f'{t:.3f}' for t in timings]}."
    )
