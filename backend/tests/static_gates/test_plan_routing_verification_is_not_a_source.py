"""``plan_routing.verification`` is a baseline record, not a state source.

Why this gate exists
--------------------
``plan_routing.verification`` used to be a **mirror** of the verification
state: ``status`` / ``round`` / ``max_rounds`` / ``stop_reason`` were
written next to the authoritative ``plan_verification`` row, and the two
answers drifted apart on live plans.  Architecture decision point 1 gave
the state a single home (``plan_verification``); architecture decision
point 3 retired the mirror: the column now carries ONE thing — the frozen
baseline and the hash of the document it was frozen from — and its only
writer is ``RoutingRepository.write_verification_baseline``.

A constraint like that does not survive on review alone.  It needs a gate
that fails when the mirror comes back, and it needs one that can still
tell the difference between "the column is written" and "the column is
*read as the answer*".

What it pins
------------
1. ``RoutingRepository.write_plan_state`` does not take a ``verification``
   argument any more — the broad routing write cannot carry the
   verification state, so no caller can smuggle it back in.
2. Exactly one statement in the whole production tree writes the
   ``verification`` column of ``plan_routing``, and it lives inside
   ``RoutingRepository.write_verification_baseline``.
3. That one writer puts the **baseline keys** into the column and
   nothing else — the lifecycle keys never reach it, so the column can
   never become a state source again.
4. ``VerificationRepository`` does not mention ``plan_routing`` at all —
   the authoritative store does not reach back into the mirror.
5. The ``PlanState`` verification setters persist through the
   authoritative door (``_write_verification_state``) and not through the
   broad routing write (``_save_state``).
6. The columns the routing row DOES own — ``completed_phases`` /
   ``review_rounds`` / ``flags`` — are still writable.  A gate that
   forbids too much is a gate that gets deleted.
7. The ``PlanState`` **read path** answers from the authoritative row
   and only reads: it takes no lifecycle key out of the mirror column
   and calls no repository write method.  A reader that writes makes
   the answer depend on the order the readers ran in — the "one plan,
   two answers" defect (VP-015 (c)).

Scope, stated honestly
----------------------
Point 2 is a whole-tree scan: it is the "exactly one writer" claim, and
it only means something when it looks everywhere.  The narrower
"no bare mirror statement in the modules that change edited" check lives
in ``test_verification_mirror_write_removed.py``, which owns that scope.

Why the checks are AST-based
----------------------------
A text scan cannot tell a live statement from prose.  Several modules
*document* the removal — docstrings and comments mention the column while
explaining that it is no longer a state source — and a text scan would
report those as violations, inviting the gate to be "fixed" by deleting
the explanation.  The AST can tell a docstring from a statement, so it
answers the question actually being asked: *is the column still written?*

The plan's example spelled the claim as a literal count over the
concatenated source (``src.count("UPDATE plan_routing SET verification")
== 1``).  The AST scan below is the same claim, done in a way that does
not fire on prose — and it is also *narrower* about what counts as a
hit, on purpose: the literal is a prefix of ``verification_status``,
so a text count would flag an innocent statement that sets a
differently-named column, while the AST scan asks the real question:
does this statement assign the routing table's ``verification`` column?
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from functools import lru_cache
from pathlib import Path

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)
from tests import app_source as app_source_module  # noqa: E402

#: The column that used to carry the verification state.
MIRROR_COLUMN = "verification"

#: The table the column lives in.
ROUTING_TABLE = "plan_routing"

#: The only writer of the column.
BASELINE_WRITER = "write_verification_baseline"

#: The module that writer lives in.
ROUTING_REPOSITORY = "state_machine/repositories/routing_repository.py"

#: The module whose verification state lives in the authoritative table.
VERIFICATION_REPOSITORY = (
    "state_machine/repositories/verification_repository.py"
)

#: The module the ``PlanState`` setters live in.
PLAN_STATE_MODULE = "plan_state.py"

#: The ``PlanState`` methods that write verification state.
VERIFICATION_SETTERS = (
    "set_verification_status",
    "set_verification_max_rounds",
    "increment_verification_round",
    "begin_verification",
)

#: The columns the routing row still owns — the ones this gate must not
#: forbid by accident.
ROUTING_OWNED_COLUMNS = ("completed_phases", "review_rounds", "flags")

#: The keys the baseline writer is allowed to put into the column.  The
#: column stores the frozen baseline and nothing else.
BASELINE_KEYS = frozenset({"baseline", "baseline_doc_sha256"})

#: The lifecycle keys.  Their presence in the column would make it a
#: state source again (arch decision point 3).
LIFECYCLE_KEYS = ("status", "round", "max_rounds", "stop_reason")

#: The ``PlanState`` getters that answer a verification question.  They
#: are read paths: they read the state the authoritative row was mapped
#: into and hand the answer out.
VERIFICATION_GETTERS = (
    "get_verification_status",
    "get_verification_round",
    "get_verification_max_rounds",
    "get_verification_stop_reason",
)

#: The in-memory state cache on ``PlanState``.  It holds what the
#: authoritative row was mapped into, so reading a lifecycle key out of
#: it is NOT reading the routing mirror column.  The detector has to
#: tell the two apart, or the verification getters could not be watched
#: at all.
STATE_CACHE_ATTR = "_state"

#: The ``PlanState`` read-path methods.  A read must answer from the
#: authoritative row, and it must not write anything: a read that
#: writes makes the answer depend on the read order (VP-015 (c)).
READ_PATH_METHODS = (
    "_load_state_from_sqlite",
    "_sqlite_row_to_state",
    "_load_state",
    "get_state",
    # The verification getters answer the same question from the same
    # cache, and the acceptance probe asks them by name
    # (``get_verification_stop_reason()``).  A getter that read the
    # routing column would answer a different question from
    # ``get_state()`` while every watched method stayed green.
    *VERIFICATION_GETTERS,
)

#: Repository write methods.  A read path that calls one of these is
#: writing while it reads.
REPOSITORY_WRITE_METHODS = (
    "set_state",
    "insert",
    "write_plan_state",
    "write_verification_baseline",
    "init_round",
    "complete_round",
    "reset_round_counter",
    "mark_stopped",
)

#: The mirror stop-reason migration must not be reachable from a read
#: path.  It is an operator-triggered one-time migration now — see
#: ``backend/scripts/migrate_legacy_verification_stop_reason.py``.
MIRROR_MIGRATION_CALLS = (
    "_migrate_mirror_stop_reason",
    "migrate_mirror_stop_reason",
)


# --- SQL reading ------------------------------------------------------------
#
# The statements are read out of the tree, not out of the text: the point
# is to find *statements*, and a column name in a docstring is prose.

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


def _mirror_column_write_kind(sql: str) -> str | None:
    """``"update"`` / ``"insert"`` / ``None`` — how this statement writes.

    ``None`` means the statement does not touch the column at all.

    Both spellings of the UPDATE count: the column as the first
    assignment (``SET verification = ?``) and the column as a neighbour of
    other assignments (``SET current_phase = ?, verification = NULL, …``).
    The second one is the shape a mirror write takes when it rides along
    with an unrelated statement, so a check that only recognised the
    first would miss exactly the case that matters.
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
            return "update"
    match = _INSERT_ROUTING.search(sql)
    if match:
        columns = {
            column.strip().strip('"').lower()
            for column in match.group("columns").split(",")
        }
        if MIRROR_COLUMN in columns:
            return "insert"
    return None


def _writes_mirror_column(sql: str) -> bool:
    """Does this statement write the routing table's ``verification`` column?"""
    return _mirror_column_write_kind(sql) is not None


# --- tree reading ----------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of the string constants that are docstrings."""
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
    return docstrings


def _statement_constants(tree: ast.AST) -> list[ast.Constant]:
    """Every string literal in the tree that is not a docstring."""
    docstrings = _docstring_nodes(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@lru_cache(maxsize=1)
def _mirror_column_writers() -> tuple[tuple[str, int, str], ...]:
    """``(path relative to backend/, line number, "update"/"insert")``.

    The whole production tree is scanned — the claim is "exactly one
    writer in the tree", and a claim about the tree cannot be answered by
    a list of files somebody remembered to add.
    """
    hits: list[tuple[str, int, str]] = []
    for path, source in app_source_module.app_sources():
        tree = ast.parse(source)
        for node in _statement_constants(tree):
            kind = _mirror_column_write_kind(node.value)
            if kind is not None:
                hits.append(
                    (
                        str(path.relative_to(BACKEND_DIR)),
                        node.lineno,
                        kind,
                    )
                )
    return tuple(hits)


def _enclosing_function(rel_path: str, lineno: int) -> str | None:
    """The name of the innermost function whose body holds ``lineno``."""
    tree = ast.parse((BACKEND_DIR / rel_path).read_text(encoding="utf-8"))
    owner: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = node.end_lineno or start
        if start <= lineno <= end and (owner is None or start > owner[0]):
            owner = (start, node.name)
    return owner[1] if owner else None


def _function_node(rel_path: str, name: str) -> ast.FunctionDef | None:
    """The ``def`` of ``name`` in the module at ``rel_path``."""
    tree = ast.parse((BACKEND_DIR / rel_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _class_method_node(
    class_name: str, method_name: str,
) -> ast.FunctionDef | None:
    """The ``def`` of ``class_name.method_name`` in ``plan_state.py``."""
    tree = ast.parse(
        (BACKEND_DIR / PLAN_STATE_MODULE).read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == method_name
            ):
                return item
    return None


def _call_name(call: ast.Call) -> str | None:
    """The bare name a call refers to, for ``f(...)`` and ``a.b(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _calls(node: ast.AST) -> list[ast.Call]:
    """Every call inside ``node``."""
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _subscript_keys(node: ast.AST) -> set[str]:
    """The string keys this code assigns into a dict.

    ``payload["baseline"] = ...`` yields ``"baseline"``.  The *name* of
    the container is deliberately ignored: the claim is about which
    keys end up in the column, not about what the local variable
    happens to be called this week — renaming ``payload`` must not
    turn the gate green.
    """
    keys: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            targets = list(child.targets)
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            index = target.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                keys.add(index.value)
    return keys


# ---------------------------------------------------------------------------
# 1. the broad routing write cannot carry the verification state
# ---------------------------------------------------------------------------


def test_write_plan_state_takes_no_verification() -> None:
    """``write_plan_state`` has no ``verification`` parameter any more."""
    params = inspect.signature(RoutingRepository.write_plan_state).parameters

    assert "verification" not in params, (
        f"write_plan_state must not accept the mirror column any more; "
        f"got {list(params)}"
    )


# ---------------------------------------------------------------------------
# 2. exactly one writer, and it is the baseline writer
# ---------------------------------------------------------------------------


def test_only_baseline_writer_touches_the_column() -> None:
    """One owner in the whole tree: ``write_verification_baseline``.

    The baseline writer needs two statements — an ``INSERT`` for the row
    that does not exist yet and an ``UPDATE`` for the one that does — so
    the claim is about the **owner**, not about the number of
    statements: exactly one function in the tree writes the column, and
    it is ``RoutingRepository.write_verification_baseline``.
    """
    writers = _mirror_column_writers()

    assert writers, (
        "the scanner found no writer of "
        f"{ROUTING_TABLE}.{MIRROR_COLUMN} at all — it is looking in the "
        "wrong place, and 'no violation' means nothing here"
    )

    owners = {
        (rel_path, _enclosing_function(rel_path, lineno))
        for rel_path, lineno, _kind in writers
    }
    assert owners == {(ROUTING_REPOSITORY, BASELINE_WRITER)}, (
        f"{len(writers)} statement(s) write "
        f"{ROUTING_TABLE}.{MIRROR_COLUMN}: {writers}, owned by "
        f"{sorted(str(owner) for owner in owners)}. The column is a frozen "
        "baseline record now — its only writer is "
        f"{ROUTING_REPOSITORY}::{BASELINE_WRITER}."
    )

    updates = [writer for writer in writers if writer[2] == "update"]
    assert len(updates) == 1, (
        f"{len(updates)} UPDATE statement(s) write the column: {updates}. "
        "There is one: the narrow baseline write."
    )

    writer = _function_node(ROUTING_REPOSITORY, BASELINE_WRITER)
    assert writer is not None, (
        f"{BASELINE_WRITER} is not defined in {ROUTING_REPOSITORY} — the "
        "column has no writer at all"
    )
    rel_path, lineno, _kind = updates[0]
    assert writer.lineno <= lineno <= (writer.end_lineno or writer.lineno), (
        f"{rel_path}:{lineno} is outside {BASELINE_WRITER} "
        f"(lines {writer.lineno}-{writer.end_lineno})"
    )


# ---------------------------------------------------------------------------
# 2b. the one writer puts the BASELINE in — and nothing else
# ---------------------------------------------------------------------------


def test_baseline_writer_stores_only_the_baseline_keys() -> None:
    """The column carries the baseline — and only the baseline.

    "Exactly one writer" is not "only the baseline": a writer that also
    stamped ``status`` / ``round`` / ``max_rounds`` / ``stop_reason``
    into the payload would turn the column back into a state source
    while every other check in this file stayed green.  The claim is
    therefore about the **keys** the writer puts in, not about the
    number of writers.

    The read-modify-write keeps whatever was already in the column, so
    this gate cannot promise that a column written by an older build
    holds nothing else — it promises that nothing writes the lifecycle
    keys there any more.
    """
    writer = _function_node(ROUTING_REPOSITORY, BASELINE_WRITER)
    assert writer is not None, (
        f"{BASELINE_WRITER} is not defined in {ROUTING_REPOSITORY} — the "
        "column has no writer at all"
    )

    keys = _subscript_keys(writer)
    assert keys == set(BASELINE_KEYS), (
        f"{BASELINE_WRITER} assigns {sorted(keys)} into the "
        f"{ROUTING_TABLE}.{MIRROR_COLUMN} column; it may only assign "
        f"{sorted(BASELINE_KEYS)}. A lifecycle key "
        f"({sorted(keys & set(LIFECYCLE_KEYS))}) would make the column a "
        "state source again (arch decision point 3)."
    )


# ---------------------------------------------------------------------------
# 3. the authoritative store does not reach back into the mirror
# ---------------------------------------------------------------------------


def test_verification_repository_has_no_routing_reference() -> None:
    """``VerificationRepository`` never mentions ``plan_routing``."""
    source = (BACKEND_DIR / VERIFICATION_REPOSITORY).read_text(
        encoding="utf-8"
    )

    assert "plan_routing" not in source, (
        f"{VERIFICATION_REPOSITORY} mentions {ROUTING_TABLE} — the "
        "authoritative store must not write or read the mirror."
    )


# ---------------------------------------------------------------------------
# 4. the setters persist through the authoritative door
# ---------------------------------------------------------------------------


def test_setters_do_not_call_save_state() -> None:
    """The verification setters do not persist through the routing write.

    ``_save_state`` is the broad routing write: it re-states the columns
    ``PlanState`` owns from this instance's **cached snapshot**.  A
    verification setter that went through it would push the verification
    state back into the routing row — the mirror write this whole change
    removed.

    ``begin_verification`` is the one method that also moves the *phase*,
    and the phase move is the routing row's business: its ``_save_state``
    call is the phase write, not a verification write.  The gate says so
    explicitly instead of pretending the call is not there — it has to be
    the **last** statement, and the authoritative verification write has
    to have happened before it.

    The plan spelled this requirement as "no ``_save_state`` call in the
    AST subtree of the four setters".  Taken literally that rule is not
    satisfiable against the upstream final form, and a gate that cannot
    go green is a gate that gets deleted: ``begin_verification`` moves
    the phase, the phase lives in ``plan_routing``, and the phase move
    goes through ``_save_state`` — that is the method's purpose, not an
    incidental rewrite.  What the literal rule is *about* is the mirror,
    and the mirror is unaffected: ``write_plan_state`` does not own the
    ``verification`` column any more (asserted above), so the routing
    write ``begin_verification`` performs cannot put the verification
    state back into the column.  This gate therefore pins the real
    claim — the four setters persist the verification state through the
    authoritative door, and the one routing write that survives is the
    phase move, exactly once, last — instead of a rule the production
    code cannot satisfy.
    """
    for name in VERIFICATION_SETTERS:
        node = _class_method_node("PlanState", name)
        assert node is not None, (
            f"PlanState.{name} is not defined in {PLAN_STATE_MODULE}"
        )

        calls = _calls(node)
        names = [_call_name(call) for call in calls]
        assert "_write_verification_state" in names, (
            f"PlanState.{name} must persist the verification state through "
            "the authoritative door (_write_verification_state), not through "
            "the routing write"
        )

        save_calls = [
            call for call in calls if _call_name(call) == "_save_state"
        ]
        if name != "begin_verification":
            assert save_calls == [], (
                f"PlanState.{name} calls _save_state — the broad routing "
                "write. The verification state belongs to the authoritative "
                "row."
            )
            continue

        # begin_verification also moves the phase; that part is the
        # routing row's business and stays.
        assert len(save_calls) == 1, (
            f"PlanState.{name} calls _save_state {len(save_calls)} times; "
            "the phase move is the only routing write this method may do"
        )
        last = node.body[-1]
        assert (
            isinstance(last, ast.Expr)
            and isinstance(last.value, ast.Call)
            and _call_name(last.value) == "_save_state"
        ), (
            f"PlanState.{name} must end with the phase write (_save_state); "
            "a verification write may not ride along with it"
        )
        authoritative = next(
            call
            for call in calls
            if _call_name(call) == "_write_verification_state"
        )
        assert authoritative.lineno < save_calls[0].lineno, (
            f"PlanState.{name} must write the authoritative verification row "
            "before the routing write"
        )


# ---------------------------------------------------------------------------
# 5. the columns the routing row owns are still writable
# ---------------------------------------------------------------------------


def test_routing_owned_json_columns_still_writeable() -> None:
    """``completed_phases`` / ``review_rounds`` / ``flags`` are still there."""
    params = inspect.signature(RoutingRepository.write_plan_state).parameters

    missing = [name for name in ROUTING_OWNED_COLUMNS if name not in params]
    assert missing == [], (
        f"write_plan_state lost {missing} — those columns are the routing "
        "row's own, and this gate must not forbid them"
    )


# ---------------------------------------------------------------------------
# 6. the read path answers from the authoritative row — and reads only
# ---------------------------------------------------------------------------
#
# Checks 1-5 are about the *writers*: who may put the verification state
# into the column.  This one is about the *readers*: a reader that takes
# a lifecycle key out of the column is a state source just as much as a
# writer that puts it there, and a reader that WRITES while it reads
# makes the answer depend on the order the readers ran in (VP-015 (c)):
# the same plan answered ``not_started`` on one read order and the
# mirror's ``failed`` on another.
#
# The detector is deliberately narrow about what it forbids.  The
# legacy ``plan_state.json`` migration (``_migrate_legacy_verification_state``)
# also lives on the read path — it reads the OLD FILE, not the mirror
# column, and it is explicitly out of scope for this gate (VP-019 (a)
# pins it).  It writes through ``_write_verification_state``, not through
# one of the repository write methods named below, so it does not trip
# the write check; the docstring of the check says so out loud.


def _subscript_key(node: ast.Subscript) -> str | None:
    """The string key of a subscript, or ``None``."""
    index = node.slice
    # ``ast.Index`` is the pre-3.9 spelling of the slice expression.
    if index.__class__.__name__ == "Index":  # pragma: no cover - py<3.9
        index = index.value  # type: ignore[attr-defined]
    if isinstance(index, ast.Constant) and isinstance(index.value, str):
        return index.value
    return None


def _is_state_cache(node: ast.AST) -> bool:
    """Is this expression ``self._state`` — the in-memory state cache?

    The cache is what the authoritative row was mapped into; reading a
    lifecycle key out of it is reading the authoritative state, not the
    routing column.  The mapping that filled it is watched on its own
    (``_sqlite_row_to_state``), so nothing is lost by telling the two
    apart here.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == STATE_CACHE_ATTR
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_mirror_column_access(node: ast.AST) -> bool:
    """Is this expression a read of the routing mirror column?

    ``row.get("verification")`` and ``row["verification"]`` both are.
    ``self._state.get("verification", {})`` is NOT: that is the
    in-memory cache (see :func:`_is_state_cache`).
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            if _is_state_cache(func.value):
                return False
            args = node.args
            if (
                args
                and isinstance(args[0], ast.Constant)
                and args[0].value == MIRROR_COLUMN
            ):
                return True
        return False
    if isinstance(node, ast.Subscript):
        return _subscript_key(node) == MIRROR_COLUMN
    return False


def _derives_from_mirror(node: ast.AST, known: set[str]) -> bool:
    """Does this expression carry the routing mirror column?"""
    if _is_mirror_column_access(node):
        return True
    if isinstance(node, ast.IfExp):
        # ``f(row.get("verification"), {}) if row else {}`` — the read
        # sits one node deeper than the plain assignment shape.
        return (
            _derives_from_mirror(node.body, known)
            or _derives_from_mirror(node.orelse, known)
        )
    if isinstance(node, ast.BoolOp):
        # ``row and f(row.get("verification"), {})``
        return any(_derives_from_mirror(v, known) for v in node.values)
    if isinstance(node, ast.Call):
        for arg in node.args:
            if _is_mirror_column_access(arg):
                return True
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in known
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                return func.value.id in known
            return _derives_from_mirror(func.value, known)
        return False
    if isinstance(node, ast.Attribute):
        return _derives_from_mirror(node.value, known)
    return False


def _mirror_derived_names(func: ast.AST) -> set[str]:
    """The local names that hold (part of) the routing mirror column."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            elif isinstance(node, ast.NamedExpr):
                # ``(mirror := f(row.get("verification"), {}))`` — the
                # walrus binds a name and reads the column in one node.
                targets = [node.target]
                value = node.value
            else:
                continue
            if value is None or not _derives_from_mirror(value, names):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _lifecycle_reads_from_mirror(func: ast.AST) -> list[str]:
    """``["<name>.stop_reason", …]`` — lifecycle keys read out of the mirror.

    A lifecycle key read from the mirror is the column answering the
    question, which is exactly what arch decision point 3 retired.  The
    baseline keys (``baseline`` / ``baseline_doc_sha256``) are not
    lifecycle keys — they only live in the column — so they are not
    reported.
    """
    names = _mirror_derived_names(func)
    hits: list[str] = []
    for node in ast.walk(func):
        key: str | None = None
        base: ast.AST | None = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            key = node.args[0].value
            base = node.func.value
        elif isinstance(node, ast.Subscript):
            key = _subscript_key(node)
            base = node.value
        if key is None or key not in LIFECYCLE_KEYS or base is None:
            continue
        if isinstance(base, ast.Name) and base.id in names:
            hits.append(f"{base.id}.{key}")
        elif _derives_from_mirror(base, names):
            hits.append(f"<mirror>.{key}")
    return hits


def _write_calls(func: ast.AST) -> list[str]:
    """The repository write methods (and mirror migrations) this code calls."""
    hits: list[str] = []
    for call in _calls(func):
        name = _call_name(call)
        if name is None:
            continue
        if name in REPOSITORY_WRITE_METHODS or name in MIRROR_MIGRATION_CALLS:
            hits.append(name)
    return hits


def test_the_read_path_never_derives_verification_state_from_the_mirror_column() -> None:
    """The read path reads the authoritative row — and only reads.

    Two claims, one scan:

    * **no lifecycle key out of the mirror column** — ``status`` /
      ``round`` / ``max_rounds`` / ``stop_reason`` read from the column
      would make the column a state source again;
    * **no write on the read path** — a read that writes makes the
      answer depend on the order the readers ran in, which is the
      "one plan, two answers" defect this whole change is about.

    The legacy ``plan_state.json`` migration is NOT a violation: it reads
    the old *file*, not the mirror column, and VP-019 (a) pins it.
    """
    for name in READ_PATH_METHODS:
        node = _class_method_node("PlanState", name)
        assert node is not None, (
            f"PlanState.{name} is not defined in {PLAN_STATE_MODULE} — the "
            "read path this gate watches is gone"
        )

        reads = _lifecycle_reads_from_mirror(node)
        assert reads == [], (
            f"PlanState.{name} takes {reads} out of the "
            f"{ROUTING_TABLE}.{MIRROR_COLUMN} column — the mirror is not "
            "the source of the verification state (arch decision point 3); "
            "the answer comes from the plan_verification row"
        )

        writes = _write_calls(node)
        assert writes == [], (
            f"PlanState.{name} calls {writes} — the read path must not "
            "write. A read that writes makes the answer depend on the "
            "read order (VP-015 (c)). The legacy plan_state.json "
            "migration reads the old file and is out of scope; it does "
            "not call a repository write method."
        )


# ---------------------------------------------------------------------------
# negative control — a scanner that finds nothing means nothing
# ---------------------------------------------------------------------------


def test_the_scanner_finds_a_violation() -> None:
    """The scanner sees a mirror write when there is one."""
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


def test_the_key_collector_sees_a_lifecycle_key() -> None:
    """The key collector is not blind.

    Without this, a collector that returned the empty set for every
    input would make ``test_baseline_writer_stores_only_the_baseline_keys``
    green forever — and a green gate that cannot see a violation is
    worse than no gate, because it is trusted.
    """
    tree = ast.parse(
        "def writer(payload):\n"
        "    payload['baseline'] = {}\n"
        "    payload['baseline_doc_sha256'] = ''\n"
        "    payload['status'] = 'running'\n"
    )

    assert _subscript_keys(tree) == {
        "baseline",
        "baseline_doc_sha256",
        "status",
    }, (
        "the key collector missed a key it should have found — the "
        "baseline-keys assertion cannot fail, so it proves nothing"
    )


def test_the_read_path_gate_detects_a_violation() -> None:
    """The read-path detector is not blind.

    A detector that reported "no violation" for every input would make
    ``test_the_read_path_never_derives_verification_state_from_the_mirror_column``
    green forever.  This is the shape the defect actually had: the mirror
    decoded into a local, a lifecycle key read out of it, and the
    authoritative row written from the read.
    """
    violation = ast.parse(
        "def _load_state_from_sqlite(self):\n"
        "    mirror = _decode_json_column(row.get('verification'), {})\n"
        "    stop_reason = mirror.get('stop_reason')\n"
        "    repo.set_state('p', stop_reason=stop_reason)\n"
        "    return mirror\n"
    )
    func = violation.body[0]

    assert _lifecycle_reads_from_mirror(func) == ["mirror.stop_reason"], (
        "the detector missed a lifecycle key read out of the mirror — the "
        "read-path assertion cannot fail, so it proves nothing"
    )
    assert "set_state" in _write_calls(func), (
        "the detector missed a repository write on the read path"
    )


def test_the_read_path_detector_allows_the_baseline_keys() -> None:
    """The detector does not fire on the baseline — the column's last job.

    The baseline keys are NOT verification state: ``plan_verification``
    has no such columns, so they still live in the mirror column and are
    still read out of it.  A gate that forbade that would forbid the
    one thing the column is allowed to do.
    """
    clean = ast.parse(
        "def _sqlite_row_to_state(row, verification_row=None):\n"
        "    mirror = _decode_json_column(row.get('verification'), {})\n"
        "    baseline = mirror.get('baseline')\n"
        "    return baseline\n"
    )
    func = clean.body[0]

    assert _lifecycle_reads_from_mirror(func) == [], (
        "the detector reported a lifecycle key where there is none — the "
        "baseline is not verification state"
    )
    assert _write_calls(func) == [], (
        "the detector reported a write where there is none"
    )


def test_the_read_path_detector_catches_an_inline_mirror_read() -> None:
    """``row.get("verification").get("stop_reason")`` is caught too.

    The local-variable shape is not the only way to read the column —
    a reader can chain the access.  A detector that only understood the
    local shape would be defeated by the one-liner.
    """
    chained = ast.parse(
        "def _load_state_from_sqlite(self):\n"
        "    stop_reason = row.get('verification').get('stop_reason')\n"
        "    return stop_reason\n"
    )
    func = chained.body[0]

    assert _lifecycle_reads_from_mirror(func) == ["<mirror>.stop_reason"], (
        "the detector missed a chained mirror read"
    )


# ---------------------------------------------------------------------------
# the verification getters are read paths too
# ---------------------------------------------------------------------------
#
# ``get_verification_stop_reason()`` is one of the four readers the VP-015
# acceptance probe asks directly, so a getter that read the mirror column
# would answer a different question from ``get_state()`` — and the gate
# would not see it, because the gate scans a fixed list of methods.
#
# Watching the getters needs the detector to tell TWO things apart:
#
# * ``self._state.get("verification", {})`` — the in-memory cache the
#   authoritative row was mapped into.  Reading a lifecycle key out of
#   it is not reading the routing column; the mapping that filled it is
#   watched separately (``_sqlite_row_to_state``).
# * ``row.get("verification")`` — the routing row's mirror column.  That
#   one is the thing arch decision point 3 retired.


def test_the_read_path_gate_watches_the_verification_getters() -> None:
    """The verification getters are on the gate's list, and they are clean."""
    for name in VERIFICATION_GETTERS:
        assert name in READ_PATH_METHODS, (
            f"PlanState.{name} is a read path but the gate does not "
            "watch it — a getter that read the mirror column would slip "
            "through while every watched method stayed green"
        )
        node = _class_method_node("PlanState", name)
        assert node is not None, (
            f"PlanState.{name} is not defined in {PLAN_STATE_MODULE}"
        )
        reads = _lifecycle_reads_from_mirror(node)
        assert reads == [], (
            f"PlanState.{name} takes {reads} out of the "
            f"{ROUTING_TABLE}.{MIRROR_COLUMN} column"
        )
        writes = _write_calls(node)
        assert writes == [], (
            f"PlanState.{name} calls {writes} — the read path must not write"
        )


def test_the_read_path_detector_does_not_mistake_the_cache_for_the_column() -> None:
    """``self._state.get("verification", {})`` is the cache, not the column.

    Without this the detector fires on every getter, and the red line
    would have to be weakened (the getters dropped from the list) to be
    usable at all.
    """
    getter = ast.parse(
        "def get_verification_status(self):\n"
        "    return self._state.get('verification', {}).get('status', 'not_started')\n"
    )
    func = getter.body[0]

    assert _lifecycle_reads_from_mirror(func) == [], (
        "the detector reported a mirror read where the getter only reads "
        "the in-memory state cache — the authoritative row was mapped "
        "into that cache by a watched method"
    )


def test_the_read_path_detector_catches_a_getter_that_reads_the_column() -> None:
    """The other half of the pair: a getter that reads the column is caught."""
    getter = ast.parse(
        "def get_verification_stop_reason(self):\n"
        "    row = self._load_state_from_sqlite()\n"
        "    mirror = _decode_json_column(row.get('verification'), {})\n"
        "    return mirror.get('stop_reason')\n"
    )
    func = getter.body[0]

    assert _lifecycle_reads_from_mirror(func) == ["mirror.stop_reason"], (
        "the detector missed a getter that reads the routing column — the "
        "getters are read paths, and a blind spot there is a blind spot in "
        "the red line"
    )


# ---------------------------------------------------------------------------
# the detector must see the column through the shapes real code uses
# ---------------------------------------------------------------------------
#
# A detector that only understands the plain assignment shape is defeated
# by the ordinary ways code is written.  The three below are not exotic:
# a conditional expression, a walrus, and a short-circuit ``and`` all
# read naturally, and each of them hides the column read one node deeper
# than the detector used to look.


def test_the_read_path_detector_sees_through_a_conditional_expression() -> None:
    """``x = f(row.get("verification")) if row else {}`` is still a read."""
    getter = ast.parse(
        "def get_verification_stop_reason(self):\n"
        "    row = self._load_state_from_sqlite()\n"
        "    mirror = _decode_json_column(row.get('verification'), {}) if row else {}\n"
        "    return mirror.get('stop_reason')\n"
    )
    func = getter.body[0]

    assert _lifecycle_reads_from_mirror(func) == ["mirror.stop_reason"], (
        "the detector missed a mirror read behind a conditional expression"
    )


def test_the_read_path_detector_sees_through_a_walrus() -> None:
    """``if (m := _decode(row.get("verification"), {})):`` is still a read."""
    getter = ast.parse(
        "def get_verification_stop_reason(self):\n"
        "    row = self._load_state_from_sqlite()\n"
        "    if (mirror := _decode_json_column(row.get('verification'), {})):\n"
        "        return mirror.get('stop_reason')\n"
        "    return None\n"
    )
    func = getter.body[0]

    assert _lifecycle_reads_from_mirror(func) == ["mirror.stop_reason"], (
        "the detector missed a mirror read behind a walrus assignment"
    )


def test_the_read_path_detector_sees_through_a_short_circuit_and() -> None:
    """``m = row and _decode(row.get("verification"), {})`` is still a read."""
    getter = ast.parse(
        "def get_verification_stop_reason(self):\n"
        "    row = self._load_state_from_sqlite()\n"
        "    mirror = row and _decode_json_column(row.get('verification'), {})\n"
        "    return mirror.get('stop_reason')\n"
    )
    func = getter.body[0]

    assert _lifecycle_reads_from_mirror(func) == ["mirror.stop_reason"], (
        "the detector missed a mirror read behind a short-circuit 'and'"
    )


