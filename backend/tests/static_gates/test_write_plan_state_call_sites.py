"""No ``write_plan_state`` call site passes ``verification=`` any more.

Why this gate exists
--------------------
``RoutingRepository.write_plan_state`` used to take a ``verification``
dictionary and serialise it into the ``plan_routing.verification``
column.  That column is the *mirror* of the verification state; the
authoritative home is the ``plan_verification`` table.  The mirror
writer was deleted (arch decision point 1), the column no longer carries
the verification state at all (arch decision point 3), and the
``verification`` parameter was removed from the signature.

A keyword argument that no longer exists is not a harmless leftover: at
runtime it is a ``TypeError``.  Every call site still passing it is a
broken test, and the break is silent until the suite runs.  This gate
turns that class of break into a static one — the call sites are found
by reading the tree, not by waiting for a red suite.

Scope: the whole ``backend/`` tree, not a hand-picked list of files.
The signature is a contract of the repository layer, and every caller of
that layer has to honour it — including the ones added after this gate
was written.  A list of files would need editing every time a new
caller appears, and the one nobody remembers to add is the one that
breaks.

Why the checks are AST-based
----------------------------
A text scan cannot tell a live call from prose.  Several of these files
*document* the removal — the docstrings mention ``verification=`` while
explaining that the parameter is gone — and a text scan would report
those as violations, inviting the gate to be "fixed" by deleting the
explanation.  The AST can tell a ``Call`` from a string, so it answers
the question actually being asked: *does any call still pass the
argument?*

The negative control below is there for the same reason: a gate that
finds nothing because it looked in the wrong place passes just as
happily as a gate that found nothing because there is nothing to find.
"""

from __future__ import annotations

import ast
import sys
from functools import lru_cache
from pathlib import Path

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


#: The files this change was written for.  They are called out
#: explicitly because they are the call sites the removal actually
#: broke; the scan below covers the whole tree, and these five are
#: additionally asserted to be *in* the scan, so a future narrowing of
#: the scan cannot quietly drop them.
TARGETS = (
    "state_machine/tests/unit/test_routing_repository.py",
    "state_machine/tests/unit/test_execution_repository.py",
    "tests/test_auto_verification_state.py",
    # Arrived by merge from ``origin/main`` (``ec446cb6``) after the
    # three call sites above were enumerated, so it was never in the
    # list — and it was the one that stayed red.
    "tests/unit/test_plan_listing_archived_phase.py",
    # The same story, one merge later: this file came in with a base
    # merge (``980249a5`` / ``316b360a``) after the enumeration above
    # was written.  The whole-tree scan already reported it as an
    # offender, but it was not pinned here — so the gate was half a
    # gate: it could see the violation, not nail it down.
    "tests/unit/test_plan_state_gate_20261020_regression.py",
)

#: Directory names that are never scanned.
_EXCLUDED_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".venv",
        ".pytest_cache",
        ".git",
        "node_modules",
    }
)


@lru_cache(maxsize=1)
def _python_files() -> tuple[Path, ...]:
    """Every ``.py`` file under ``backend/``, sorted for stable output."""
    files = []
    for path in BACKEND_DIR.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return tuple(sorted(files))


def _call_name(func: ast.expr) -> str | None:
    """The bare name a call refers to, for ``f(...)`` and ``a.b(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _offenders_in_tree(tree: ast.AST) -> list[int]:
    """Line numbers of the ``verification=`` keywords in the tree.

    The line reported is the *keyword's* line — the place a reader has
    to look — not the line the call expression starts on.
    """
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) != "write_plan_state":
            continue
        for keyword in node.keywords:
            if keyword.arg == "verification":
                lines.append(keyword.lineno)
    return lines


@lru_cache(maxsize=1)
def _scan() -> tuple[tuple[str, ...], int]:
    """``(offending "path:line" strings, total call sites seen)``."""
    offenders: list[str] = []
    total = 0
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken file is its own failure
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) == "write_plan_state"
            ):
                total += 1
        if not _offenders_in_tree(tree):
            continue
        rel = path.relative_to(BACKEND_DIR)
        for lineno in _offenders_in_tree(tree):
            offenders.append(f"{rel}:{lineno}")
    return tuple(offenders), total


def test_no_call_site_passes_verification() -> None:
    """No ``write_plan_state(...)`` call passes ``verification=``."""
    offenders, total = _scan()
    # ``_scan`` is cached and therefore hands back a tuple; compare against a
    # list so that "no offenders" really is ``[]`` and not a tuple that is
    # merely equal to nothing.
    offenders = list(offenders)

    assert offenders == [], (
        f"{len(offenders)} call site(s) still pass verification=: {offenders}. "
        "The parameter was removed from RoutingRepository.write_plan_state — "
        "passing it is a TypeError at runtime, not a style issue. The "
        "verification state belongs to the plan_verification table; the "
        "baseline belongs to RoutingRepository.write_verification_baseline."
    )

    # A scan that found nothing because it looked nowhere must not pass.
    assert total > 0, (
        "the scan found no write_plan_state call at all — it is looking in "
        "the wrong place, and 'no violations' means nothing here"
    )


def test_the_named_call_sites_are_inside_the_scan() -> None:
    """The five files this change was written for are actually scanned.

    The fourth — ``tests/unit/test_plan_listing_archived_phase.py`` —
    joined the list when the merge from ``origin/main`` brought in a
    call site the original enumeration had missed.  The fifth —
    ``tests/unit/test_plan_state_gate_20261020_regression.py`` — joined
    it the same way, one base merge later.
    """
    scanned = {str(path.relative_to(BACKEND_DIR)) for path in _python_files()}
    missing = [rel for rel in TARGETS if rel not in scanned]
    assert missing == [], (
        f"these call sites are not inside the scan: {missing}. A scan that "
        "does not cover them cannot vouch for them."
    )

    for rel in TARGETS:
        tree = ast.parse((BACKEND_DIR / rel).read_text(encoding="utf-8"))
        assert _offenders_in_tree(tree) == [], (
            f"{rel} still passes verification= to write_plan_state"
        )


def test_the_detector_finds_a_violation() -> None:
    """Negative control: the detector sees a violation when there is one.

    Without this, a detector that silently matches nothing would let the
    gate above pass forever.
    """
    source = (
        "repo.write_plan_state(\n"
        "    'p1',\n"
        "    completed_phases=[],\n"
        "    review_rounds={},\n"
        "    flags={},\n"
        "    verification={},\n"
        "    last_updated=None,\n"
        ")\n"
    )
    assert _offenders_in_tree(ast.parse(source)) == [6], (
        "the detector missed a verification= keyword it should have found"
    )

    clean = (
        "repo.write_plan_state(\n"
        "    'p1',\n"
        "    completed_phases=[],\n"
        "    review_rounds={},\n"
        "    flags={},\n"
        "    last_updated=None,\n"
        ")\n"
    )
    assert _offenders_in_tree(ast.parse(clean)) == [], (
        "the detector reported a violation in a call that passes none"
    )