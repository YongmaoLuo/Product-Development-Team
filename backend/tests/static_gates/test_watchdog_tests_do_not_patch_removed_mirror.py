"""Static gate — the watchdog tests carry no reference to the removed mirror writer.

Why this gate exists
--------------------
Task 2 deleted the helper that wrote the ``plan_routing.verification``
mirror column (``_update_plan_state_to_terminal``) and left the
authoritative ``plan_verification`` row as the only answer to "what is
this plan's verification verdict?".  The sibling gate
``test_verification_mirror_write_removed.py`` already pins the helper's
absence from the *production* tree.

This gate covers the other half of the same removal: the *test* tree.
The watchdog test modules still reached for the deleted symbol — one
whole class plus four ``patch.object(server_mod, ...)`` stubs — and
every one of them fails with ``AttributeError`` the moment the module is
imported against a server that no longer carries the attribute.  A
removed symbol that the tests still name is not a green suite; it is a
suite that fails for a reason nobody is looking at any more.

So the gate pins the modules as name-free:

1. ``backend/tests/test_verification_watchdog_fix.py`` — the
   ``TestPlanStateMirror`` class exercised the deleted behaviour itself
   (that the helper wrote the mirror column while leaving ``stage``
   alone), so it was deleted rather than rewritten: there is no longer a
   mirror write to assert on.
2. ``backend/tests/unit/test_watchdog_quota_hold_w.py``,
   ``backend/tests/unit/test_verification_watchdog_triage.py``,
   ``backend/tests/unit/test_verification_death_stop_reason.py`` and
   ``backend/tests/unit/test_watchdog_progress_heartbeat_grace.py`` —
   each stubbed the helper out of the watchdog's terminal path so the
   test could observe a different call.  The stub is gone; the
   observation the test makes is unchanged.
3. ``backend/tests/unit/test_verification_quota_park_not_death.py`` —
   came in with the quota-resume base merge (``dd1a8b62``) and stubbed
   the same helper out of the death latch, so merging it in made all
   three of its cases ERROR at fixture setup.  The stub is gone; the
   observations the three cases make are unchanged.

Why a text scan
---------------
The question this gate answers is "does the *name* appear in these
files?", and a name is a name — the answer is in the text.  Unlike the
production-tree sibling, there is no docstring in these modules that
legitimately mentions the helper: the references were live code
(an import, a call, a ``patch.object`` target), and every one of them
was removed.  A prose mention would have been a false positive, and
none survived.

Scope, stated honestly
----------------------
The modules are named explicitly rather than globbed — and the
enumeration is now checked from the other side as well:
``test_the_enumeration_covers_every_live_reference`` below sweeps
``backend/tests`` for every module that stubs the removed helper and
fails if one of them is not named here.  That gap was not hypothetical:
the progress-heartbeat module was missing from the first four-module
list, so four of its cases died with ``AttributeError`` while this gate
stayed green.  It is named here now, and the list is the record of every
module found to reach for the removed helper.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: ``backend/`` — this file lives at
#: ``backend/tests/static_gates/test_watchdog_tests_do_not_patch_removed_mirror.py``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


#: The helper task 2 removed from the production tree.
REMOVED_MIRROR_WRITER = "_update_plan_state_to_terminal"

#: The watchdog test modules that referenced it.  Paths are relative
#: to ``backend/``.
WATCHDOG_TEST_FILES = (
    "tests/test_verification_watchdog_fix.py",
    "tests/unit/test_watchdog_quota_hold_w.py",
    "tests/unit/test_verification_watchdog_triage.py",
    "tests/unit/test_verification_death_stop_reason.py",
    "tests/unit/test_watchdog_progress_heartbeat_grace.py",
    # Arrived by merge from ``origin/main`` (``dd1a8b62``, the
    # quota-resume feature) after the enumeration above was written.  The
    # upstream module stubbed the very helper this branch removed
    # (``monkeypatch.setattr(server_mod, "_update_plan_state_to_terminal",
    # ...)``), so merging it in made all three of its cases ERROR at
    # fixture setup.  The stub is gone now; the observations the three
    # cases make are unchanged.  It is named here because the list is the
    # record of every module found to reach for the removed helper.
    "tests/unit/test_verification_quota_park_not_death.py",
)


def test_no_reference_in_watchdog_tests():
    """None of the enumerated watchdog test modules names the removed helper."""
    offenders = [
        rel
        for rel in WATCHDOG_TEST_FILES
        if REMOVED_MIRROR_WRITER
        in (BACKEND_DIR / rel).read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{REMOVED_MIRROR_WRITER!r} still appears in {offenders}. The "
        "helper is gone from the production tree; a test that names it "
        "fails with AttributeError instead of testing anything. Remove the "
        "reference (the mirror write it stubbed no longer exists)."
    )


def _call_target_name(func: ast.expr) -> str | None:
    """The bare name a call refers to, for ``f(...)`` and ``a.b(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_stub_call(node: ast.Call) -> bool:
    """Is this call one that can *install* the removed helper?

    ``setattr`` and ``patch`` (and ``patch.object``, ``mock.patch``) are
    the shapes a test uses to put a name onto a module — the quota-park
    module arrived as ``monkeypatch.setattr(server_mod,
    "_update_plan_state_to_terminal", ...)``.  ``patch.object`` is the
    one spelling that does not end in the bare name, so it is matched
    through the value's last level plus the ``object`` attribute.
    """
    func = node.func
    if _call_target_name(func) in {"setattr", "patch"}:
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "object"
        and _call_target_name(func.value) == "patch"
    )


def _names_the_removed_helper(node: ast.Call) -> bool:
    """Does one of the call's arguments spell the removed helper's name?"""
    arguments = list(node.args) + [keyword.value for keyword in node.keywords]
    return any(
        isinstance(argument, ast.Constant)
        and argument.value == REMOVED_MIRROR_WRITER
        for argument in arguments
    )


def _scan_for_stubbed_helper() -> tuple[tuple[str, ...], int]:
    """``(modules that stub the removed helper, number of modules read)``.

    Every ``*.py`` under ``backend/tests`` is read, except the gates
    themselves: they are this scan's subject, not the residue it is
    looking for.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted((BACKEND_DIR / "tests").rglob("*.py")):
        if "static_gates" in path.parts:
            continue
        scanned += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken file is its own failure
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _is_stub_call(node)
                and _names_the_removed_helper(node)
            ):
                offenders.append(str(path.relative_to(BACKEND_DIR)))
                break
    return tuple(sorted(offenders)), scanned


def test_the_enumeration_covers_every_live_reference():
    """The enumeration names every module that still reaches for the helper.

    The list above is a hand-written record, and a hand-written record
    only stays true if something checks it.  The way it went stale before
    is written down above: a module arrived with a merge, reached for the
    removed helper, and the gate stayed green — because the gate only
    looked at the modules it already knew about.

    So this check reads the other direction.  It finds every module under
    ``backend/tests`` that *stubs* the removed helper (the shape the
    quota-park module arrived in) and asks whether the enumeration names
    it.

    The scan is AST-based on purpose.  A text scan cannot tell a live
    stub from prose: the sibling gate
    ``test_verification_mirror_write_removed.py`` *names* the helper in
    its own module-level constant, and every gate's docstring talks
    about it.  The AST only ever looks at ``ast.Call`` nodes, so a
    comment, a docstring, or a plain assignment is not a hit.

    The gates under ``static_gates/`` are skipped — they are the scan's
    subject, not part of the residue this gate is about.
    """
    quota_park = "tests/unit/test_verification_quota_park_not_death.py"
    assert quota_park in WATCHDOG_TEST_FILES, (
        f"{quota_park} is not in WATCHDOG_TEST_FILES. It arrived with the "
        "quota-resume base merge (dd1a8b62) and stubbed the helper this "
        "branch removed; a module left out of the enumeration is a module "
        "the gate does not police."
    )
    assert len(WATCHDOG_TEST_FILES) == 6, (
        f"WATCHDOG_TEST_FILES holds {len(WATCHDOG_TEST_FILES)} entries, not 6. "
        "The enumeration is the record of every module that reached for the "
        "removed helper — a module missing from it is a module the gate "
        "silently ignores."
    )

    offenders, scanned = _scan_for_stubbed_helper()
    unlisted = [rel for rel in offenders if rel not in WATCHDOG_TEST_FILES]
    assert unlisted == [], (
        f"these modules stub {REMOVED_MIRROR_WRITER!r} but are not named in "
        f"WATCHDOG_TEST_FILES: {unlisted}. Name them in the enumeration (with "
        "a note saying where they came from) — the helper is gone from the "
        "production tree, and a test that stubs it fails with AttributeError."
    )

    assert scanned > 0, (
        "the scan read no test module at all — it is looking in the wrong "
        "place, and 'nothing found' means nothing here"
    )