"""The reaper daemons a test run started are taken back at teardown.

Why this gate exists
--------------------
``backend/tests/reaper_daemon_guard.py`` knows **which** daemons belong
to a run — the ones that were not running when the run took its census,
and that serve a root pytest made — and it knows how to stop them.  What
it does not know is whether anything **calls** it.

A module nobody calls changes nothing, and the defect it was written for
is silent: every test that starts a server leaves a daemon behind, the
daemons accumulate for as long as the machine lives, and nothing in the
suite fails.  The only symptom is "the machine is slow again", which
nobody traces back to a test run.

So this gate pins the two call sites that turn the rule into behaviour:

  * ``backend/tests/conftest.py`` — a session-scoped, autouse fixture
    that takes the census when the session starts and reclaims at its
    end.  This is the backstop: it catches the daemons of the tests that
    did not clean up after themselves, and it is the reason the daemon
    count after a full run is not higher than the count before it.
  * ``backend/tests/integration/test_verification_progress_live.py`` —
    the live-server fixture stops the daemons of the root it served.
    This is the specific case: the fixture knows exactly which root is
    its own, so it does not have to wait for the census.

Both are checked against the **source**, not against a running system: a
gate that starts servers to prove that a teardown runs is a gate that
leaks the very thing it is about.

The rule itself — what counts as "this run's daemon" — belongs to the
guard module and is pinned by ``tests/unit/test_reaper_daemon_guard.py``.
What is pinned here is that the rule is *reached*.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: ``backend/`` — this file lives at ``backend/tests/static_gates/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent

#: The two call sites this gate pins.
_CONFTEST = _REPO_ROOT / "backend" / "tests" / "conftest.py"
_LIVE_TEST = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "integration"
    / "test_verification_progress_live.py"
)

#: The session fixture that has to reclaim what the run started.
_RECLAIM_FIXTURE = "_reclaim_reaper_daemons"

#: The live fixture that has to stop the daemons of the root it served.
_LIVE_FIXTURE = "live_server"

#: The class that has to remember which root it served.
_LIVE_SERVER_CLASS = "_LiveServer"


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _find_function(tree: ast.Module, name: str):
    """The function (or fixture) called *name*, or ``None``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _find_class(tree: ast.Module, name: str):
    """The class called *name*, or ``None``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _fixture_options(func) -> dict:
    """The keyword arguments given to ``pytest.fixture`` on *func*.

    An empty dict when the function is not a fixture at all — which is
    itself an answer, and the callers below act on it.
    """
    for decorator in func.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return {kw.arg: kw.value for kw in decorator.keywords}
    return {}


def _literal(node):
    """The value of a constant AST node, or ``None`` when it is not one."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _called_name(node: ast.Call):
    """The name a call reaches, whether it is ``f()`` or ``mod.f()``."""
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _calls(func, name: str) -> list:
    """Every call to *name* in *func*, wherever it sits."""
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and _called_name(node) == name
    ]


def _calls_in_finally(func, name: str) -> list:
    """Every call to *name* in the ``finally`` block of a ``try`` in *func*."""
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.finalbody:
            for sub in ast.walk(statement):
                if isinstance(sub, ast.Call) and _called_name(sub) == name:
                    found.append(sub)
    return found


def _guarded_calls(func, name: str) -> list:
    """Every call to *name* that sits inside the ``try`` of a ``try``."""
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.body:
            for sub in ast.walk(statement):
                if isinstance(sub, ast.Call) and _called_name(sub) == name:
                    found.append(sub)
    return found


def _saves_attribute(cls, attribute: str) -> bool:
    """Whether *cls* assigns ``self.<attribute>`` somewhere."""
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr != attribute:
                continue
            owner = target.value
            if isinstance(owner, ast.Name) and owner.id == "self":
                return True
    return False


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_conftest_reclaims_after_session() -> None:
    """The session takes a census when it starts and reclaims when it ends."""
    tree = _tree(_CONFTEST)
    func = _find_function(tree, _RECLAIM_FIXTURE)

    assert func is not None, (
        "backend/tests/conftest.py declares no {0}() fixture. The reaper "
        "daemons a test run starts are never taken back, so they accumulate "
        "for as long as the machine lives — and the only symptom is 'the "
        "machine is slow again', which nobody traces back to a test "
        "run.".format(_RECLAIM_FIXTURE)
    )

    options = _fixture_options(func)
    assert _literal(options.get("scope")) == "session", (
        "the reclaim fixture must be session-scoped: a per-test fixture "
        "would take a census after the daemons it is meant to catch are "
        "already running"
    )
    assert _literal(options.get("autouse")) is True, (
        "the reclaim fixture must be autouse: a fixture nothing asks for is "
        "a fixture that never runs"
    )

    assert _calls(func, "snapshot_daemons"), (
        "{0}() must take the census of the running daemons when the session "
        "starts — the daemons that were already running are somebody "
        "else's and must never be stopped".format(_RECLAIM_FIXTURE)
    )
    assert _calls(func, "reclaim_new_daemons"), (
        "{0}() must reclaim the daemons the session started when the "
        "session ends".format(_RECLAIM_FIXTURE)
    )
    assert _guarded_calls(func, "reclaim_new_daemons"), (
        "the reclaim must sit inside a ``try``: a cleanup that fails must "
        "not take the whole run down over litter"
    )


def test_live_fixture_stops_its_daemons() -> None:
    """The live server fixture stops the daemons of the root it served."""
    tree = _tree(_LIVE_TEST)
    func = _find_function(tree, _LIVE_FIXTURE)

    assert func is not None, (
        "backend/tests/integration/test_verification_progress_live.py "
        "declares no {0}() fixture".format(_LIVE_FIXTURE)
    )

    assert _calls_in_finally(func, "stop_daemons_for_plans_root"), (
        "the {0}() fixture must stop the reaper daemons of the root it "
        "served, in its ``finally`` block: a reaper daemon is written to "
        "outlive the process that started it, so terminating the server "
        "does not take the daemon with it".format(_LIVE_FIXTURE)
    )

    cls = _find_class(tree, _LIVE_SERVER_CLASS)
    assert cls is not None, (
        "backend/tests/integration/test_verification_progress_live.py "
        "declares no {0} class".format(_LIVE_SERVER_CLASS)
    )
    assert _saves_attribute(cls, "plans_root"), (
        "{0} must remember the plans root it served: the teardown needs to "
        "know which root's daemons are its own".format(_LIVE_SERVER_CLASS)
    )


def test_the_reclaim_rule_is_not_reimplemented_in_the_conftest() -> None:
    """The rule lives in the guard module; the conftest only calls it.

    The rule — "not running when the census was taken" **and** "serves a
    root pytest made" — is the part that is easy to get subtly wrong.
    A second copy of it in the conftest is a copy that will drift, and
    the drift is invisible: it either stops the developer's own daemon
    or leaves the suite's behind.
    """
    tree = _tree(_CONFTEST)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module != "tests.reaper_daemon_guard":
                continue
            imported.update(alias.name for alias in node.names)

    missing = {"snapshot_daemons", "reclaim_new_daemons"} - imported
    assert not missing, (
        "backend/tests/conftest.py must import {0} from "
        "tests.reaper_daemon_guard rather than writing its own census: the "
        "rule belongs to the guard module, and a second copy of it drifts "
        "silently".format(sorted(missing))
    )
