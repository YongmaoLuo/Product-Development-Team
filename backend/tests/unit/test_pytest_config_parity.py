"""Parity contract between the repo-root ``pytest.ini`` and ``backend/pytest.ini``.

Background
----------
This repository ships **two** pytest configs, and which one pytest resolves
depends on the invocation's rootdir:

  * ``pytest.ini`` at the repository root — used when pytest is invoked
    from the repo root (that is how the CI shards and ``scripts/run_tests.sh``
    reach the suites that live outside ``backend/``).
  * ``backend/pytest.ini`` — used when pytest is invoked with ``cwd=backend/``.

Both describe the same suite, so a contract enforced in only one of them is
a contract that half the invocations do not have.  The 2026-09 incident
(``backend/pytest.ini`` gained ``timeout = 300`` / ``timeout_method =
thread`` after a hung pytest grew its stderr capture file to 30+ GB and
pinned a SQLite WAL writer) is exactly that shape: the root config never
learned the cap, so a root-invoked run could still hang for hours.

The second half of this module pins the direction of travel.  Parallelising
the suite exposed wall-clock cases that only fail when the machine is
busy.  The tempting fix — skip them, ``xfail`` them, deselect them, or
narrow the ``-m`` expression — buys a green badge by *deleting the
evidence*.  This module therefore pins the baseline selection surface of
the root config: the narrowing options it carried before the parallelism
work, and not one more.  A wall-clock case that cannot pass under
parallel load is moved to the serial lane with the ``time_sensitive``
marker, which the root config already excludes from the parallel lanes and
``scripts/run_tests.sh`` runs alone with ``-m "time_sensitive or perf"
--timeout=1800``.

Contracts pinned here:

  1. ``test_root_and_backend_declare_the_same_timeout_contract`` — both
     configs declare ``timeout = 300`` and ``timeout_method = thread``.
  2. ``test_root_addopts_narrows_no_further_than_the_baseline`` — the root
     config's selection surface is the baseline and nothing more.
  3. ``test_root_marker_registry_keeps_the_lane_markers`` — the markers the
     lanes are built from (``time_sensitive`` / ``e2e`` / ``integration`` /
     ``perf``) are still registered in the root config.
  4. ``test_the_wall_clock_wake_case_is_marked_time_sensitive`` — the
     wall-clock case that parallelisation exposed is moved to the serial
     lane by a marker, not removed from the suite.
  5. ``test_root_and_backend_declare_the_same_marker_set`` — the two
     registries offer the suite the *same* markers: the backend registry
     declares nothing the root one lacks, the difference is pinned by
     name, and every marker in that difference that a case carries is
     still registered at runtime.

Why 5 exists (VP-022, round 4)
------------------------------
The root registry carried ``observational`` and ``needs_real_process``;
``backend/pytest.ini`` did not.  Both configs enable ``--strict-markers``,
and pytest checks a marker when it is *used*, so a case under
``backend/tests`` that used either marker aborted *collection* for the
whole module — pytest reports "not found in ``markers`` configuration
option" and collects nothing, which reads as a green run with zero
cases.  The gate resolves ``backend/pytest.ini`` (its target is
``backend/tests``), so the registry that actually governs the gate was
the one missing the markers.

The comment in the root config claimed the two blocks were identical.
They were not, and nothing checked.  This contract is that check.

The obvious repair — copying the root's marker block into
``backend/pytest.ini`` — is not available: that file's marker list is
frozen by
``backend/tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py``,
which demands it equal a written-down baseline exactly.  The two markers
the suite carries are therefore registered with the live pytest config by
the modules that carry them (the pattern
``backend/tests/integration/test_context_pack_reduces_exploration.py``
established for ``observational``), and this contract pins both the
difference and that bridge, so neither can change unnoticed.
"""
from __future__ import annotations

import ast
import configparser
import shlex
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
#
# The paths are derived from this file's own location rather than from an
# env var: a contract that depends on a developer's environment is one that
# quietly breaks the moment someone clones the repo into a different layout.

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/
_REPO_ROOT = _BACKEND_DIR.parent  # repository root

_ROOT_PYTEST_INI = _REPO_ROOT / "pytest.ini"
_BACKEND_PYTEST_INI = _BACKEND_DIR / "pytest.ini"

#: The wall-clock case that the parallel lanes exposed: it parks a budget
#: of 0.1 s for 0.3 s of real time, so on a loaded machine the wait is
#: charged and the verdict never comes back through the ordinary path.
_WAKE_TEST_MODULE = (
    _BACKEND_DIR / "tests" / "unit" / "test_provider_quota_wake.py"
)
_WAKE_TEST_CLASS = "TestTheAttemptAttachesIt"
_WAKE_TEST_CASE = (
    "test_a_wait_outlasting_the_whole_cap_still_reaches_a_verdict"
)


# ---------------------------------------------------------------------------
# The baseline selection surface of the root config
# ---------------------------------------------------------------------------
#
# These are the narrowing options the root ``pytest.ini`` carried before
# the parallelism work, and the complete list of what it is allowed to
# carry afterwards.  Everything else — a fourth ``--ignore``, a
# ``--deselect``, a narrowed ``-m`` expression, another ``-p no:`` — is a
# case that stopped being counted.

#: The three CLI-dependent modules that cannot run in this environment.
_BASELINE_IGNORE_PATHS = frozenset(
    {
        "backend/tests/test_agent.py",
        "backend/tests/test_coding_tool.py",
        "backend/tests/test_verification_agent.py",
    }
)

#: The single marker expression of the root config: the parallel lanes
#: exclude the wall-clock cases, which run alone in the serial lane.
_BASELINE_MARKER_EXPRESSION = "not time_sensitive"

#: The plugins the root config turns off.
_BASELINE_DISABLED_PLUGINS = frozenset({"cacheprovider"})

#: Markers the CI lanes are assembled from.  Dropping one of them from
#: the registry breaks the lane that names it (and, under
#: ``--strict-markers``, aborts collection outright).
_LANE_MARKERS = ("time_sensitive", "e2e", "integration", "perf")

#: The markers the repo-root config registers and ``backend/pytest.ini`` does
#: not.  The backend registry cannot simply grow: its marker list is frozen
#: by ``backend/tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py``,
#: which requires it to equal a written-down baseline exactly.  Every entry
#: here is therefore accounted for by name:
#:
#:   * ``observational`` and ``needs_real_process`` are carried by cases, so
#:     the module that carries them registers them with the live pytest
#:     config before it uses them (see :data:`_BRIDGED_MARKERS`);
#:   * ``vendor_a_pro_exhausted``, ``fallback_vendor_b`` and
#:     ``recovery_vendor_a_pro`` are provider-fallback selection keywords
#:     for verification points that no case carries today.
_ROOT_ONLY_MARKERS = frozenset(
    {
        "observational",
        "needs_real_process",
        "vendor_a_pro_exhausted",
        "fallback_vendor_b",
        "recovery_vendor_a_pro",
    }
)

#: The subset of :data:`_ROOT_ONLY_MARKERS` that a case in the tree carries.
#: A marker in this set is registered with the live pytest config at import
#: time by the module that carries it — the pattern
#: ``backend/tests/integration/test_context_pack_reduces_exploration.py``
#: established for ``observational`` — which is what keeps it usable under
#: the config the gate resolves.
_BRIDGED_MARKERS = frozenset({"observational", "needs_real_process"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_pytest_ini(path: Path) -> configparser.ConfigParser:
    """Return the parsed ini file at ``path``.

    ``ConfigParser`` is used rather than a hand-rolled regex because it is
    the canonical parser the upstream tools use, so a reformat of the file
    surfaces here instead of silently drifting away from what pytest reads.
    """
    assert path.exists(), (
        f"{path} is missing; the config-parity contract cannot be "
        f"evaluated against an absent config file"
    )
    parser = configparser.ConfigParser(interpolation=None)
    # ``configparser`` lower-cases keys by default; pytest treats
    # ``[pytest]`` literally, so preserve case to keep diffs readable.
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(path, encoding="utf-8")
    assert parser.has_section("pytest"), (
        f"{path} is missing the `[pytest]` section; pytest will refuse to "
        f"load it and the suite silently collects zero"
    )
    return parser


def _addopts_tokens(parser: configparser.ConfigParser) -> list[str]:
    """Return the root config's ``addopts`` as a list of argv tokens.

    ``shlex`` is used rather than ``str.split`` because the marker
    expression is a quoted argument (``-m "not time_sensitive"``) — a
    naive split would tear it into three tokens and the "is the expression
    still the one we expect" question would be unanswerable.
    """
    raw = parser.get("pytest", "addopts", fallback="")
    try:
        return shlex.split(raw, posix=True)
    except ValueError:  # pragma: no cover - defensive: unbalanced quotes
        pytest.fail(f"addopts is not parseable as an argv list: {raw!r}")


def _marker_expressions(tokens: list[str]) -> list[str]:
    """Return every ``-m`` expression in ``tokens``."""
    return [
        tokens[index + 1]
        for index, token in enumerate(tokens[:-1])
        if token == "-m"
    ]


def _ignore_paths(tokens: list[str]) -> set[str]:
    """Return the paths passed to ``--ignore`` in ``tokens``."""
    return {
        token.split("=", 1)[1]
        for token in tokens
        if token.startswith("--ignore=")
    }


def _disabled_plugins(tokens: list[str]) -> set[str]:
    """Return the plugins disabled with ``-p no:<plugin>`` in ``tokens``."""
    disabled: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token != "-p":
            continue
        value = tokens[index + 1]
        if value.startswith("no:"):
            disabled.add(value[len("no:") :])
    return disabled


def _registered_markers(parser: configparser.ConfigParser) -> set[str]:
    """Return the marker names declared under ``[pytest] markers =``.

    The block is a multi-line value with one marker per line, each in the
    shape ``<name>: <description>``; only the name is needed here.
    """
    raw = parser.get("pytest", "markers", fallback="")
    names: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip optional inline comments — pytest's parser ignores text
        # after ``#`` in a marker line.
        marker_part = stripped.split("#", 1)[0].strip()
        if ":" not in marker_part:
            continue
        names.add(marker_part.split(":", 1)[0].strip())
    return names


def _decorator_names(node: ast.AST) -> set[str]:
    """Return the dotted names of the decorators on ``node``.

    ``@pytest.mark.time_sensitive`` becomes ``{"pytest.mark.time_sensitive"}``
    so the check does not have to know how the decorator is spelled.
    """
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        parts: list[str] = []
        current: ast.AST = decorator
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        if parts:
            names.add(".".join(reversed(parts)))
    return names


def _is_registered_at_runtime(marker: str) -> bool:
    """True when some module registers ``marker`` with the live config.

    pytest knows a marker in exactly two ways: the ``markers`` ini key
    of the config that governs the run, or
    ``config.addinivalue_line("markers", ...)`` at runtime.  The second
    is what a module uses when the ini that governs its run cannot list
    the marker, so the check is that some module under ``backend/tests``
    both appends to the ``markers`` list and names the marker in the
    description it appends.

    The description is matched as ``"<marker>:`` — a quoted name
    followed by a colon — so the match cannot be satisfied by the
    marker merely being *used* in the same file.
    """
    needle = f'"{marker}:'
    for path in sorted((_BACKEND_DIR / "tests").rglob("*.py")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - an unreadable file is not a marker
            continue
        if "addinivalue_line" not in body or '"markers"' not in body:
            continue
        if needle in body:
            return True
    return False


# ---------------------------------------------------------------------------
# Contract 1 — both configs carry the same timeout contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ini_path",
    [_ROOT_PYTEST_INI, _BACKEND_PYTEST_INI],
    ids=["root", "backend"],
)
def test_root_and_backend_declare_the_same_timeout_contract(
    ini_path: Path,
) -> None:
    """Both configs declare ``timeout = 300`` and ``timeout_method = thread``.

    The cap turns a silent hang into a loud failure within five minutes,
    which is what lets the watchdog reap an orphaned pytest instead of
    leaving it to rot (and to keep growing its stderr capture file).  It
    only protects an invocation if the config that invocation resolves
    carries it — which is why the root config has to carry it too.

    ``timeout_method = thread`` is not interchangeable with ``signal``:
    the signal-based method is not usable on macOS, where Python disables
    SIGALRM under threading.
    """
    parser = _load_pytest_ini(ini_path)

    assert parser.has_option("pytest", "timeout"), (
        f"{ini_path} no longer declares `timeout = ...`; a hung pytest "
        f"will grow its stderr-capture file without bound and pin the "
        f"SQLite WAL writer (2026-09 incident: 30+ GB). "
        f"Restore `timeout = 300`."
    )
    timeout = parser.get("pytest", "timeout").strip()
    assert timeout == "300", (
        f"{ini_path} declares `timeout = {timeout}`; the contract "
        f"requires `timeout = 300` in BOTH configs, and the cap must not "
        f"be widened or dropped in one of them — a half-applied cap "
        f"protects only the invocations that happen to resolve that file"
    )

    assert parser.has_option("pytest", "timeout_method"), (
        f"{ini_path} no longer declares `timeout_method = ...`; "
        f"pytest-timeout would pick a method automatically and the choice "
        f"can drift between runners. Restore `timeout_method = thread`."
    )
    method = parser.get("pytest", "timeout_method").strip()
    assert method == "thread", (
        f"{ini_path} declares `timeout_method = {method}`; the contract "
        f"requires `thread` — the `signal` method needs SIGALRM, which "
        f"Python disables under threading on macOS"
    )


# ---------------------------------------------------------------------------
# Contract 2 — the root config narrows no further than the baseline
# ---------------------------------------------------------------------------


def test_root_addopts_narrows_no_further_than_the_baseline() -> None:
    """The root config's selection surface is the baseline, not less.

    Every option checked here shrinks the set of cases that run.  Shrinking
    it is how a suite goes green without having been fixed: a deselected
    case, an extra ``--ignore``, a narrowed ``-m`` expression or a second
    disabled plugin all report "green" while the defect they were covering
    is no longer measured.  The only legitimate way to retire a case that
    cannot pass under parallel load is to move it to the serial lane with
    the ``time_sensitive`` marker — which changes *when* it runs, not
    *whether* it runs.
    """
    parser = _load_pytest_ini(_ROOT_PYTEST_INI)
    tokens = _addopts_tokens(parser)

    deselects = [
        token for token in tokens if token.startswith("--deselect")
    ]
    assert not deselects, (
        f"root pytest.ini gained the deselect option(s) {deselects!r}; "
        f"deselecting a case removes it from the gate, which is the "
        f"opposite of what the timeout-parity work is for"
    )

    ignores = _ignore_paths(tokens)
    assert ignores == _BASELINE_IGNORE_PATHS, (
        f"root pytest.ini ignores {sorted(ignores)!r}; the baseline is "
        f"{sorted(_BASELINE_IGNORE_PATHS)!r}. Adding an --ignore hides a "
        f"case; the only allowed change is to move it to the serial lane"
    )

    expressions = _marker_expressions(tokens)
    assert len(expressions) == 1, (
        f"root pytest.ini carries {len(expressions)} marker expressions "
        f"{expressions!r}; the baseline is exactly one "
        f"({_BASELINE_MARKER_EXPRESSION!r}) — a second expression or a "
        f"narrower one would silently shrink the lanes"
    )
    assert expressions[0] == _BASELINE_MARKER_EXPRESSION, (
        f"root pytest.ini marker expression is {expressions[0]!r}; the "
        f"baseline is {_BASELINE_MARKER_EXPRESSION!r}. Widening it (e.g. "
        f"adding `and not unit`) removes cases from the parallel lanes"
    )

    disabled = _disabled_plugins(tokens)
    assert disabled == _BASELINE_DISABLED_PLUGINS, (
        f"root pytest.ini disables the plugin(s) {sorted(disabled)!r}; "
        f"the baseline is {sorted(_BASELINE_DISABLED_PLUGINS)!r}"
    )


# ---------------------------------------------------------------------------
# Contract 3 — the lane markers survive in the root registry
# ---------------------------------------------------------------------------


def test_root_marker_registry_keeps_the_lane_markers() -> None:
    """The root config still registers the markers the lanes are built from.

    The lanes are assembled by marker name (``-m "time_sensitive or perf"``
    and friends), so a marker that disappears from the registry does not
    just lose its lane — under ``--strict-markers`` it aborts collection
    for every module that references it, and the run reports zero cases
    instead of a failure.
    """
    parser = _load_pytest_ini(_ROOT_PYTEST_INI)
    registered = _registered_markers(parser)
    missing = [marker for marker in _LANE_MARKERS if marker not in registered]
    assert not missing, (
        f"root pytest.ini no longer registers the marker(s) {missing!r}; "
        f"the lanes are assembled by marker name and `--strict-markers` "
        f"aborts collection for any module that uses them"
    )


# ---------------------------------------------------------------------------
# Contract 4 — the wall-clock case is moved to the serial lane, not dropped
# ---------------------------------------------------------------------------


def test_the_wall_clock_wake_case_is_marked_time_sensitive() -> None:
    """The wall-clock wake case carries the ``time_sensitive`` marker.

    The case parks a 0.1 s budget for 0.3 s of real time and then asserts
    that the budget was never charged.  Under a parallel lane the machine
    is busy enough that the wait *is* charged and the assertion fails —
    the case is measuring the runner's load, not the code.  Marking it
    ``time_sensitive`` moves it into the serial lane
    (``-m "time_sensitive or perf" --timeout=1800``), where it runs alone
    and still asserts the same three things.

    The alternatives — skipping it, ``xfail``-ing it, deleting it, or
    deselecting it — all retire the coverage.  The marker keeps it.
    """
    assert _WAKE_TEST_MODULE.exists(), (
        f"{_WAKE_TEST_MODULE} is missing; the wall-clock case this "
        f"contract pins cannot be located"
    )
    tree = ast.parse(
        _WAKE_TEST_MODULE.read_text(encoding="utf-8"),
        filename=str(_WAKE_TEST_MODULE),
    )

    classes = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    assert _WAKE_TEST_CLASS in classes, (
        f"{_WAKE_TEST_MODULE} no longer defines the class "
        f"{_WAKE_TEST_CLASS!r}; the wall-clock case this contract pins "
        f"cannot be located"
    )

    functions = {
        node.name: node
        for node in classes[_WAKE_TEST_CLASS].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert _WAKE_TEST_CASE in functions, (
        f"{_WAKE_TEST_CLASS} no longer defines {_WAKE_TEST_CASE!r}; the "
        f"wall-clock case this contract pins cannot be located"
    )

    decorators = _decorator_names(functions[_WAKE_TEST_CASE])
    assert "pytest.mark.time_sensitive" in decorators, (
        f"{_WAKE_TEST_CLASS}::{_WAKE_TEST_CASE} carries the decorator(s) "
        f"{sorted(decorators)!r}; it needs `time_sensitive` so it runs in "
        f"the serial lane alone instead of failing under a parallel "
        f"lane's load. Do NOT skip, xfail or delete it — that retires "
        f"the coverage instead of rescheduling it"
    )


# ---------------------------------------------------------------------------
# Contract 5 — the two registries offer the suite the same markers
# ---------------------------------------------------------------------------


def test_root_and_backend_declare_the_same_marker_set() -> None:
    """The two configs must offer the suite the same markers.

    Both configs run with ``--strict-markers``, and pytest checks a
    marker at the moment it is *used* (``pytest.mark.<name>``), so a
    marker that the governing config does not declare aborts collection
    for the whole module that carries it — pytest reports "not found in
    ``markers`` configuration option", collects nothing from that
    module, and the run looks green over an empty room.  Which config
    governs depends on the invocation:

    * the gate (``scripts/run_tests.sh``) points pytest at
      ``backend/tests``, so it resolves ``backend/pytest.ini``;
    * the same case run from the repo root resolves the root config.

    So the defect is invisible from one side and fatal from the other.

    The two registries cannot simply be made identical by copying the
    root's marker block into ``backend/pytest.ini``: that file's marker
    list is frozen by
    ``backend/tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py``,
    which requires it to equal a written-down baseline exactly.  The
    difference is therefore pinned here by name, and every marker in it
    is accounted for:

    * the two the suite actually carries (``observational`` and
      ``needs_real_process``) are registered with the live pytest config
      by the module that carries them, before it uses them — the
      pattern ``backend/tests/integration/test_context_pack_reduces_exploration.py``
      established for ``observational``;
    * the three provider-fallback keywords are selection keywords for
      verification points that no case carries today.

    This contract fails if a marker is added to one registry and not the
    other, if a bridged marker stops being registered at runtime, or if
    the backend registry starts declaring a marker the root registry
    does not know.
    """
    root_markers = _registered_markers(_load_pytest_ini(_ROOT_PYTEST_INI))
    backend_markers = _registered_markers(_load_pytest_ini(_BACKEND_PYTEST_INI))

    missing_in_root = sorted(backend_markers - root_markers)
    assert not missing_in_root, (
        f"{_ROOT_PYTEST_INI} does not register the marker(s) "
        f"{missing_in_root!r} that {_BACKEND_PYTEST_INI} registers. A "
        f"marker registered in only one of the two configs is registered "
        f"for half the invocations; the root registry has to know every "
        f"marker the backend registry knows."
    )

    root_only = sorted(root_markers - backend_markers)
    assert root_only == sorted(_ROOT_ONLY_MARKERS), (
        f"the markers the root config registers and backend/pytest.ini does "
        f"not are {root_only!r}; the pinned set is "
        f"{sorted(_ROOT_ONLY_MARKERS)!r}. A new entry here is not a "
        f"cosmetic difference: the marker has to be usable under BOTH "
        f"configs — registered in backend/pytest.ini (whose list is frozen "
        f"by the keychain static gate), registered at runtime by the "
        f"module that carries it, or carried by no case at all. Name it "
        f"in _ROOT_ONLY_MARKERS and say which of the three it is."
    )

    for marker in sorted(_BRIDGED_MARKERS):
        assert _is_registered_at_runtime(marker), (
            f"no module registers the marker {marker!r} with the live "
            f"pytest config any more. backend/pytest.ini does not declare "
            f"it (its marker list is frozen by the keychain static gate), "
            f"so a case that carries it aborts collection under the "
            f"config the gate resolves — pytest reports 'not found in "
            f"`markers` configuration option', collects nothing from that "
            f"module, and the run looks green over an empty room."
        )
