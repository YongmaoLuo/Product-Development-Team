"""Static gate: the watcher hand-off must be gated on durable facts.

Background (2026-11-03)
-----------------------
``backend/routes/execution.py``'s executor watcher used to gate the whole
completion branch -- the auto-verification hand-off included -- on

    if state["status"] == "running":

``state["status"]`` is an *in-memory* field that any other writer may
flip while the executor subprocess is still alive (``stop_execution``
writes ``"stopped"``).  On 2026-11-03 the status drifted while the
executor was running, the subprocess then exited 0, and the hand-off was
silently skipped: no log line, no verification round, the plan just
stopped.

The fix re-based the hand-off on facts that survive a status drift --
``rc == 0`` AND ``pending_or_unstarted == 0`` AND no quota marker -- and
added a structured ``auto_verification_skipped`` log for the paths that
deliberately do *not* hand off.

Why this is a *static* gate
---------------------------
The behavioural suite (``tests/unit/test_auto_verification_handoff_*.py``)
proves the new behaviour works.  It cannot prove the *old* gate is gone:
the old condition and the new one can coexist in the source and the
behavioural suite stays green, because the new condition happens to be
true in every case the suite drives.  The regression this gate prevents
is a *source* regression -- somebody re-introducing
``state["status"] == "running"`` as the guard around the hand-off call.

So this gate reads the AST and asks three structural questions:

1. the auto-verification call still exists at all;
2. every call site is guarded by at least one ``ast.If`` (it is never a
   bare, unconditional call);
3. **no** enclosing ``ast.If`` tests the mutable-status literal
   ``state["status"] == "running"`` -- the exact condition that caused
   the 2026-11-03 incident.

And it checks that the ``auto_verification_skipped`` log key is still in
the source: the skip log is the only trace an operator gets for a plan
that stopped without a verification round, so deleting it is a silent
regression of the same incident's second half.

The gate follows the code, not the file path
---------------------------------------------
``tests.app_source.find_def`` locates the module that owns
``start_execution`` -- today ``backend/routes/execution.py`` -- so the
gate keeps working if the watcher is split out of ``routes/`` later.
The module is then scanned whole; every ``_run_auto_verification_loop``
call in it is collected, together with its ancestor chain.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from tests.app_source import find_def  # noqa: E402


#: The watcher whose completion branch owns the hand-off.
_WATCHER = "start_execution"

#: The hand-off call itself.
_LOOP = "_run_auto_verification_loop"

#: The mutable in-memory status literal that caused the 2026-11-03
#: incident.  Compared against ``ast.unparse(i.test)`` with the quotes
#: normalised, so ``'running'`` and ``"running"`` both match.
_BANNED_TEST = 'state["status"] == "running"'

#: The structured key that records a *deliberate* skip.
_SKIP_LOG_KEY = "auto_verification_skipped"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_loop_call(node: ast.AST) -> bool:
    """Is ``node`` a call of ``_run_auto_verification_loop``?

    Both spellings count: the attribute form the watcher uses
    (``_server._run_auto_verification_loop(...)``) and a bare name.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == _LOOP
    if isinstance(func, ast.Name):
        return func.id == _LOOP
    return False


def _collect_chains(node: ast.AST, ancestors: tuple, hits: list) -> None:
    """Collect ``(call, ancestors)`` for every ``_run_auto_verification_loop`` call.

    The walk is recursive and carries the ancestor chain down, so each hit
    knows the ``ast.If`` / ``ast.Try`` / ... nodes it is nested under --
    which is what the "is it guarded?" and "is it guarded by the mutable
    status?" questions need.
    """
    for child in ast.iter_child_nodes(node):
        if _is_loop_call(child):
            hits.append((child, ancestors))
        _collect_chains(child, ancestors + (child,), hits)


def _watcher_module():
    """The production module that owns the executor watcher."""
    found = find_def(_WATCHER)
    return found.path, found.tree


def _hits() -> list:
    """Every auto-verification call site in the watcher module."""
    _path, tree = _watcher_module()
    hits: list = []
    _collect_chains(tree, (), hits)
    return hits


def _if_ancestors(ancestors: tuple) -> list:
    """The ``ast.If`` nodes in an ancestor chain."""
    return [a for a in ancestors if isinstance(a, ast.If)]


def _normalised_test(node: ast.If) -> str:
    """``ast.unparse`` of an ``if`` test with the quotes normalised."""
    return ast.unparse(node.test).replace("'", '"')


# ---------------------------------------------------------------------------
# 1. The call still exists
# ---------------------------------------------------------------------------


def test_auto_verification_call_still_exists():
    """The watcher still calls ``_run_auto_verification_loop`` somewhere.

    A gate that scans a haystack that has gone empty passes vacuously --
    this is the assertion that keeps the other three from doing that.
    """
    hits = _hits()

    assert hits, "自动验证调用不见了"


# ---------------------------------------------------------------------------
# 2. Every call site is guarded by a condition
# ---------------------------------------------------------------------------


def test_every_auto_verification_call_is_guarded_by_a_condition():
    """No auto-verification call may be a bare, unconditional call.

    A call that is not under any ``if`` runs on *every* watcher exit,
    including the ones that must not hand off (a crashed executor, a
    plan with pending work, a quota-parked plan).
    """
    hits = _hits()

    assert hits, "自动验证调用不见了"
    unguarded = [
        call
        for call, ancestors in hits
        if not _if_ancestors(ancestors)
    ]
    assert not unguarded, (
        "an auto-verification call is not guarded by any condition: "
        f"{[ast.unparse(c) for c in unguarded]!r}"
    )


# ---------------------------------------------------------------------------
# 3. No call site is guarded by the mutable-status literal
# ---------------------------------------------------------------------------


def test_no_auto_verification_call_is_guarded_by_the_mutable_status_literal():
    """The guard must not be ``state["status"] == "running"`` any more.

    That literal is an in-memory field another writer may flip while the
    executor subprocess is still alive -- it is what made the 2026-11-03
    hand-off disappear without a trace.
    """
    hits = _hits()

    assert hits, "自动验证调用不见了"
    offenders = [
        (call, node)
        for call, ancestors in hits
        for node in _if_ancestors(ancestors)
        if _normalised_test(node) == _BANNED_TEST
    ]
    assert not offenders, (
        "an auto-verification call is still guarded by the mutable-status "
        f"literal {_BANNED_TEST!r}: "
        f"{[ast.unparse(c) for c, _ in offenders]!r}"
    )


# ---------------------------------------------------------------------------
# 4. The skip log is still there
# ---------------------------------------------------------------------------


def test_skip_log_key_is_still_in_the_source():
    """``auto_verification_skipped`` must stay in the watcher module.

    A skip that leaves no line in the log is indistinguishable from a
    plan that was never started -- the second half of the same incident.
    """
    path, _tree = _watcher_module()
    source = path.read_text(encoding="utf-8")

    assert _SKIP_LOG_KEY in source, (
        f"the {_SKIP_LOG_KEY!r} log key is gone from {path} -- a skipped "
        "hand-off must leave a structured trace"
    )
