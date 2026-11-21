"""The ``plan_routing.verification`` mirror writer is gone (2026-11-04).

Why this gate exists
--------------------
``backend/verification_loop.py`` used to carry a helper named
``_update_plan_state_to_terminal``.  It wrote the ``verification`` JSON
column of the ``plan_routing`` table with a bare ``UPDATE`` statement —
a *mirror* of the verdict, written in parallel with the authoritative
write to the ``plan_verification`` table.

Two stores, two writers, two answers.  ``/api/plan/{id}/summary`` read
the mirror, ``/api/verification/{id}/status`` read the authoritative
row, and the two disagreed on live plans — each endpoint reporting a
different verdict for the same round.  The authoritative row wins now:
the summary endpoint has read ``plan_verification`` since 2026-11-04.

This gate pins the mirror writer's *absence*, so it cannot quietly come
back:

1. the helper is not defined anywhere in the production tree;
2. nothing calls it — neither bare (``_update_plan_state_to_terminal(...)``)
   nor through the module handle (``_server._update_plan_state_to_terminal(...)``);
3. nothing imports it, so ``server._update_plan_state_to_terminal`` is
   gone as well (an ``import`` alone keeps the symbol alive in the
   namespace the callers reached it through);
4. the three modules this change edited carry no bare
   ``UPDATE plan_routing SET verification`` statement;
5. and — same scope, different question — none of them assigns the
   routing table's ``verification`` column at all, in any spelling: a
   mirror write can ride along inside an unrelated ``UPDATE`` as
   ``verification = NULL``, and a whole-column clear takes the frozen
   baseline with it.

Why the checks are AST-based
----------------------------
A text scan cannot tell a live reference from prose.  ``plan_state.py``
documented what replaced the helper, and a text scan would report that
docstring as a call site — a false positive that invites the gate to be
"fixed" by deleting the explanation.  The AST can tell a ``FunctionDef``
from a docstring, and a ``Call`` from an ``ImportFrom``, so it answers the
question that is actually being asked: *is the symbol still wired up?*

Scope, stated honestly: the bare-SQL check covers the three modules this
change edited, not the whole tree.  ``verification_repository.py`` writes
the same column through the repository API and is a separate change; the
"exactly one statement left in the tree" claim belongs to that one.
"""

from __future__ import annotations

import ast
import re
import sys
from functools import lru_cache
from pathlib import Path

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import app_source as app_source_module  # noqa: E402


#: The helper that used to write the mirror column.
MIRROR_WRITER = "_update_plan_state_to_terminal"

#: The bare statement that helper issued.
MIRROR_SQL = "UPDATE plan_routing SET verification"

#: The table the mirror column lives in.
ROUTING_TABLE = "plan_routing"

#: The column the mirror lived in.
MIRROR_COLUMN = "verification"

#: ``UPDATE <table> SET <assignments> [WHERE …]`` against the routing table.
_UPDATE_ROUTING = re.compile(
    r"UPDATE\s+(?:OR\s+\w+\s+)?(?:\w+\s*\.\s*)?"
    + ROUTING_TABLE
    + r"\s+SET\s+(?P<assignments>.*?)(?:\bWHERE\b|$)",
    re.IGNORECASE | re.DOTALL,
)

#: One assignment target inside a ``SET`` clause: at the start of the
#: clause or right after a comma, optionally table-qualified.
_ASSIGNMENT_TARGET = re.compile(
    r"(?:^|,)\s*(?:[A-Za-z_]\w*\s*\.\s*)?\"?([A-Za-z_]\w*)\"?\s*=(?!=)",
)

#: ``INSERT INTO <table> ( <columns> )`` against the routing table.
_INSERT_ROUTING = re.compile(
    r"INSERT\s+INTO\s+(?:\w+\s*\.\s*)?"
    + ROUTING_TABLE
    + r"\s*\((?P<columns>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


def _writes_mirror_column(sql: str) -> bool:
    """Does this statement write the routing table's ``verification`` column?

    Both spellings count: the column as the first assignment
    (``SET verification = ?``) and the column as a neighbour of other
    assignments (``SET current_phase = ?, verification = NULL, …``).  The
    second one is the shape a mirror write takes when it rides along with
    an unrelated statement, so a check that only recognised the first
    would miss exactly the case that matters.
    """
    match = _UPDATE_ROUTING.search(sql)
    if match:
        targets = {
            found.group(1).lower()
            for found in _ASSIGNMENT_TARGET.finditer(
                match.group("assignments")
            )
        }
        if MIRROR_COLUMN in targets:
            return True
    match = _INSERT_ROUTING.search(sql)
    if match:
        columns = {
            column.strip().strip('"').lower()
            for column in match.group("columns").split(",")
        }
        if MIRROR_COLUMN in columns:
            return True
    return False


def _statement_constants(tree: ast.AST) -> list[ast.Constant]:
    """Every string literal in the tree that is not a docstring.

    A docstring is prose that *talks about* the column; a statement is
    code that *writes* it.  Only the second one is a violation.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]

#: The modules whose bare mirror statement this change removed.  Kept
#: explicit rather than globbed: a module added to the tree later must
#: be a deliberate decision, not a silent widening of the scan.
EDITED_MODULES = (
    "verification_loop.py",
    "routes/verification.py",
    "server.py",
)


@lru_cache(maxsize=1)
def _app_trees() -> tuple[tuple[str, ast.Module], ...]:
    """``(path relative to backend/, parsed tree)`` for every production module.

    The production tree is read through :mod:`tests.app_source` rather
    than through a file list, so the scan follows the code when a module
    moves and never sees the test tree (a gate that scans the gates
    reports every other gate).
    """
    trees = []
    for path, source in app_source_module.app_sources():
        trees.append((str(path.relative_to(BACKEND_DIR)), ast.parse(source)))
    return tuple(trees)


def test_helper_definition_is_gone():
    """No production module defines the mirror writer any more."""
    defined = sorted(
        {
            node.name
            for _path, tree in _app_trees()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == MIRROR_WRITER
        }
    )
    assert defined == [], (
        f"{MIRROR_WRITER!r} is still defined in the production tree: {defined}. "
        "The mirror writer is the second answer to 'what is this plan's "
        "verification verdict?' — the authoritative one is plan_verification."
    )


def test_no_call_sites():
    """Nothing calls the mirror writer — bare, or through the module handle."""
    calls: list[str] = []
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == MIRROR_WRITER:
                calls.append(f"{path}:{node.lineno} (bare call)")
            elif isinstance(func, ast.Attribute) and func.attr == MIRROR_WRITER:
                calls.append(f"{path}:{node.lineno} (attribute call)")
    assert calls == [], (
        f"{MIRROR_WRITER!r} is still called: {calls}. Every terminal path "
        "already persists the authoritative plan_verification row; the "
        "mirror write is the redundant half."
    )


def test_helper_is_not_imported():
    """Nothing imports the mirror writer, so the symbol is gone from the app."""
    imported: list[str] = []
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == MIRROR_WRITER:
                        imported.append(f"{path}:{node.lineno} (from-import)")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == MIRROR_WRITER or alias.name.endswith(
                        f".{MIRROR_WRITER}"
                    ):
                        imported.append(f"{path}:{node.lineno} (import)")
    assert imported == [], (
        f"{MIRROR_WRITER!r} is still imported: {imported}. An import keeps "
        "the symbol alive in the namespace the callers reached it through."
    )


def test_no_raw_mirror_sql_in_edited_modules():
    """The edited modules carry no bare ``UPDATE plan_routing SET verification``."""
    offenders = [
        rel
        for rel in EDITED_MODULES
        if MIRROR_SQL in (BACKEND_DIR / rel).read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{MIRROR_SQL!r} still appears in {offenders}. The mirror column is "
        "written through the repository API; a bare UPDATE is what put two "
        "stores out of step."
    )


def test_no_routing_verification_column_write_in_edited_modules():
    """No statement in the edited modules writes the routing mirror column.

    Same scope as the check above — the three modules this change edited —
    but it asks a different question: not "is the helper's statement
    still here" but "does **any** statement here assign the column".

    The difference matters because a mirror write does not have to look
    like the helper's statement.  It can ride along with an unrelated
    write:

        UPDATE plan_routing SET current_phase = ?, substage = NULL,
        verification = NULL, version = version + 1, updated_at = ?
        WHERE plan_id = ?

    — the column name is not the first assignment, so a literal scan for
    ``UPDATE plan_routing SET verification`` walks straight past it.  And
    a whole-column clear takes the frozen baseline with it, which is the
    one thing the column is still allowed to hold.

    The table name has to be ``plan_routing``: a statement against
    ``plan_verification`` (the authoritative table) is the point of the
    whole change, not a violation.
    """
    offenders: list[str] = []
    for rel in EDITED_MODULES:
        tree = ast.parse((BACKEND_DIR / rel).read_text(encoding="utf-8"))
        for node in _statement_constants(tree):
            if _writes_mirror_column(node.value):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        f"{ROUTING_TABLE}.{MIRROR_COLUMN} is still written from "
        f"{offenders}. The column holds the frozen baseline and nothing "
        "else — its only writer is "
        "RoutingRepository.write_verification_baseline."
    )


def test_the_column_scanner_finds_a_violation():
    """Negative control: the scanner sees a mirror write when there is one.

    Without this, a detector that silently matched nothing would let the
    gate above pass forever.
    """
    violation = (
        "UPDATE plan_routing SET current_phase = ?, substage = NULL, "
        "verification = NULL, version = version + 1, updated_at = ? "
        "WHERE plan_id = ?"
    )
    assert _writes_mirror_column(violation), (
        "the scanner missed a mirror write it should have found"
    )

    clean = (
        "UPDATE plan_routing SET current_phase = ?, substage = NULL, "
        "version = version + 1, updated_at = ? WHERE plan_id = ?"
    )
    assert not _writes_mirror_column(clean), (
        "the scanner reported a mirror write in a statement that writes none"
    )

    other_table = (
        "UPDATE plan_verification SET verification_status = ?, "
        "round = ? WHERE plan_id = ?"
    )
    assert not _writes_mirror_column(other_table), (
        "the scanner confused the authoritative table with the mirror"
    )
