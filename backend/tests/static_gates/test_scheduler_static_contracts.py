"""Static gate: the scheduler's four static contracts.

Why this gate exists
--------------------
Four properties of the PDT-native scheduler cannot be observed by
calling it -- they are properties of the *source*, and each of them has
a failure mode that looks like something else:

1. **The router is registered before the catch-all.**  ``server.py``
   ends with a frontend catch-all (``GET /{path:path}``) that answers
   every unmatched path with a file -- or with a 404 that reads like a
   missing asset.  Starlette matches routes first-match-wins, so a
   router registered *after* the catch-all is unreachable.  The failure
   is **half-visible**: the catch-all is a GET, so the scheduler's POST
   commands keep working while its GET read model does not.  "The
   operator can promote a plan but cannot see why it is not running" is
   a state nobody suspects is a routing accident.

2. **The schema version moves forward, and only once.**  The version
   is what tells an existing database that it needs the v7 tables.  A
   version that goes backwards -- or a second definition of it -- is a
   database that is told it is newer than it is: the migration is
   skipped and the tables never appear.

3. **The v7 DDL is idempotent.**  ``migrate`` runs on every request
   that opens a connection, so its DDL is executed over and over.  A
   ``CREATE TABLE`` without ``IF NOT EXISTS`` raises on the second run
   -- on a request, not on a deploy.

4. **The scheduler package never calls itself over HTTP.**  The
   scheduler's facts live in one process and one database; a component
   that reaches the API over a socket queues behind the work it is
   trying to observe, and the decision ends up existing twice -- once
   in the process, once in the request.

Why the source is read through ``app_source``
---------------------------------------------
The application's source is split across ``backend/server.py``,
``backend/routes/*.py`` and the packages below them, and it keeps
moving.  A gate that opens one hand-picked path fails two ways: it
reports "the code is gone" when the code merely moved (which invites
deleting the gate), and -- worse -- a scan that matches nothing still
passes, because most of the contracts above are *absences* ("no second
registration point", "no HTTP client").  So every scan here goes
through :mod:`tests.app_source`, which walks the production tree: the
haystack is the whole application, and a move is followed rather than
mistaken for a deletion.

Sensitivity
-----------
A scanner that cannot fail is worse than no scanner: it reports the
code as clean.  The last tests in this file hand each detector a
planted violation -- a scheduler router registered after the catch-all,
a ``CREATE TABLE`` without its guard, an ``import requests`` -- and
require the detector to report it.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

#: ``backend/`` -- this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The repository root, three levels up.  Used to label findings with
#: repository-relative paths instead of absolute ones.
REPO_ROOT = Path(__file__).resolve().parents[3]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import app_source as app_source_module  # noqa: E402


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------

#: The frontend catch-all's path template: a single path parameter with
#: the ``:path`` converter, so it matches every one-segment path.
CATCH_ALL_TEMPLATE = re.compile(r"^/\{[^}/]+:path\}$")

#: The name of the version constant.  Spelled once so the gate and its
#: messages cannot drift apart.
VERSION_NAME = "CURRENT_SCHEMA_VERSION"

#: The schema version the v7 scheduler migration produces.
EXPECTED_SCHEMA_VERSION = 7

#: The schema versions this repository has already shipped.  The current
#: version must be strictly greater than every one of them: a version
#: that does not move forward is a version that went backwards.
RETIRED_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)

#: The three tables the v7 migration owns -- the scheduler's own facts.
SCHEDULER_TABLES: tuple[str, ...] = (
    "scheduler_queue",
    "scheduler_slot",
    "scheduler_command",
)

#: The indexes that belong to those tables.  A table without its index
#: is a table whose reader degrades into a full scan, and an index
#: created without the guard clause makes the second ``migrate`` fail.
SCHEDULER_INDEXES: tuple[str, ...] = (
    "idx_scheduler_queue_seq",
    "idx_scheduler_command_plan",
)

#: The name of the package the scheduler's own code lives in.
SCHEDULER_PACKAGE = "plan_scheduler"

#: HTTP clients the scheduler package may not import.  ``urllib.request``
#: is spelled separately because it is the standard library's way of
#: making the same mistake.
FORBIDDEN_HTTP_MODULES: frozenset[str] = frozenset({"httpx", "requests"})
FORBIDDEN_HTTP_SUBMODULES: frozenset[str] = frozenset({"urllib.request"})

#: A ``CREATE TABLE`` statement.  The guard clause is optional here on
#: purpose -- the point of the scan is to *notice* when it is missing.
CREATE_TABLE_PATTERN = re.compile(
    r"(?:^|;)[ \t]*CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TABLE\s+"
    r"(?P<guard>IF\s+NOT\s+EXISTS\s+)?"
    r"[\"'`\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)

#: A ``CREATE INDEX`` statement, same shape.
CREATE_INDEX_PATTERN = re.compile(
    r"(?:^|;)[ \t]*CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+"
    r"(?P<guard>IF\s+NOT\s+EXISTS\s+)?"
    r"[\"'`\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)

#: A statement that writes the recorded schema version.
SCHEMA_VERSION_WRITE_PATTERN = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE)\s+schema_version\b"
    r"[^\n]*\bversion\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One place in the source where something is registered."""

    #: The file, labelled repository-relative when it is inside the repo.
    path: str
    #: 1-based line number.
    lineno: int
    #: The source text of the statement, for the message.
    text: str = ""


@dataclass(frozen=True)
class SchemaObject:
    """One ``CREATE`` statement found in the schema module."""

    #: 1-based line number in the file the statement came from.
    lineno: int
    #: The object's name -- the table or index name.
    name: str
    #: Whether the statement carried ``IF NOT EXISTS``.
    idempotent: bool
    #: The statement itself, for the message.
    text: str = ""


@dataclass(frozen=True)
class VersionWrite:
    """One statement that writes the recorded schema version."""

    path: str
    lineno: int
    sql: str
    params: Optional[ast.AST]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _label(path: object) -> str:
    """Return ``path`` as a repository-relative label when it is one."""
    text = str(path)
    try:
        return str(Path(text).resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return text


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _docstring_ids(tree: ast.Module) -> set[int]:
    """The ids of the string constants that are docstrings.

    Prose is not a statement: a docstring that mentions ``CREATE TABLE``
    describes SQL, it does not run it.  Excluding the docstrings is what
    keeps the DDL scan from reading the schema module's own commentary
    as if it were a migration.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
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
            ids.add(id(first.value))
    return ids


def _sql_constants(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Every string constant that is not a docstring, with its line."""
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


def _module_bindings(tree: ast.Module) -> dict[str, str]:
    """Map a local name to the dotted path it was imported from.

    ``from routes import scheduler as _routes_scheduler`` binds
    ``_routes_scheduler`` to ``routes.scheduler``.  The gate needs that
    so a renamed alias does not become a way to slip a second
    registration past it.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                bindings[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bindings[local] = alias.name
    return bindings


def _references(node: ast.AST, name: str) -> bool:
    """Does this expression mention ``name``?"""
    return any(
        isinstance(child, ast.Name) and child.id == name
        for child in ast.walk(node)
    )


def _literal_int(node: Optional[ast.AST]) -> Optional[int]:
    """The integer a node is, when it is an integer literal."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    return None


# ---------------------------------------------------------------------------
# The detectors
# ---------------------------------------------------------------------------


def _include_router_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``include_router(...)`` call in a module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
        and node.args
    ]


def _catch_all_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call that declares the frontend catch-all's path."""
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and CATCH_ALL_TEMPLATE.fullmatch(node.args[0].value)
        ):
            calls.append(node)
    return calls


def _is_scheduler_router(argument: str, bindings: dict[str, str]) -> bool:
    """Is this ``include_router`` argument the scheduler's router?

    Two ways to say it, and both are checked: the argument may *name*
    the scheduler (``_routes_scheduler.router``), or it may be a local
    name whose import says so (``from routes.scheduler import router as
    r`` -> ``r``).  The second one is what stops a rename from
    becoming a way to mount the scheduler twice.
    """
    text = argument.strip()
    if "scheduler" in text.lower():
        return True
    if text.isidentifier():
        return "scheduler" in bindings.get(text, "").lower()
    return False


def find_scheduler_registrations(
    sources: Sequence[tuple[object, str]],
) -> list[Site]:
    """Every place the scheduler's router is put on an application."""
    found: list[Site] = []
    for path, source in sources:
        tree = _parse(source)
        bindings = _module_bindings(tree)
        for call in _include_router_calls(tree):
            argument = ast.get_source_segment(source, call.args[0]) or ""
            if _is_scheduler_router(argument, bindings):
                found.append(Site(_label(path), call.lineno, argument))
    return found


def find_catch_all_registrations(
    sources: Sequence[tuple[object, str]],
) -> list[Site]:
    """Every place the frontend catch-all is declared."""
    found: list[Site] = []
    for path, source in sources:
        tree = _parse(source)
        for call in _catch_all_calls(tree):
            template = call.args[0].value  # type: ignore[union-attr]
            found.append(Site(_label(path), call.lineno, template))
    return found


def find_schema_objects(
    source: str,
    pattern: re.Pattern[str],
) -> list[SchemaObject]:
    """Every ``CREATE`` statement in a module, with its guard clause."""
    tree = _parse(source)
    objects: list[SchemaObject] = []
    for lineno, value in _sql_constants(tree):
        for match in pattern.finditer(value):
            offset = value[: match.start()].count("\n")
            objects.append(
                SchemaObject(
                    lineno=lineno + offset,
                    name=match.group("name"),
                    idempotent=bool(match.group("guard")),
                    text=match.group(0).strip(),
                )
            )
    return objects


def find_version_definitions(
    sources: Sequence[tuple[object, str]],
) -> list[tuple[str, int, Optional[int]]]:
    """Every assignment to the schema version constant."""
    found: list[tuple[str, int, Optional[int]]] = []
    for path, source in sources:
        tree = _parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == VERSION_NAME:
                        found.append((_label(path), node.lineno, _literal_int(node.value)))
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name) and target.id == VERSION_NAME:
                    found.append(
                        (_label(path), node.lineno, _literal_int(node.value))
                    )
    return found


def find_version_writes(
    sources: Sequence[tuple[object, str]],
) -> list[VersionWrite]:
    """Every statement that writes the recorded schema version."""
    writes: list[VersionWrite] = []
    for path, source in sources:
        tree = _parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            sql = node.args[0]
            if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                continue
            if not SCHEMA_VERSION_WRITE_PATTERN.search(sql.value):
                continue
            params = node.args[1] if len(node.args) > 1 else None
            writes.append(VersionWrite(_label(path), node.lineno, sql.value, params))
    return writes


def find_rollback_comparisons(
    sources: Sequence[tuple[object, str]],
) -> list[Site]:
    """Comparisons that would treat the current version as the older one.

    ``recorded < CURRENT`` is the upgrade trigger.  ``CURRENT <
    recorded`` is a downgrade -- a database that is ahead of the code
    being told it is behind -- and there must be none.
    """
    found: list[Site] = []
    for path, source in sources:
        tree = _parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not node.ops:
                continue
            if not isinstance(node.ops[0], (ast.Lt, ast.LtE)):
                continue
            if not _references(node.left, VERSION_NAME):
                continue
            text = ast.get_source_segment(source, node) or ""
            found.append(Site(_label(path), node.lineno, text))
    return found


def find_http_client_imports(
    sources: Sequence[tuple[object, str]],
) -> list[Site]:
    """Every import of an HTTP client."""
    found: list[Site] = []
    for path, source in sources:
        tree = _parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden_http_module(alias.name):
                        found.append(Site(_label(path), node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if not module:
                    continue
                if _is_forbidden_http_module(module):
                    found.append(Site(_label(path), node.lineno, module))
                    continue
                # ``from urllib import request`` is the same mistake as
                # ``import urllib.request``, spelled the other way round.
                for alias in node.names:
                    full = f"{module}.{alias.name}"
                    if _is_forbidden_http_module(full):
                        found.append(Site(_label(path), node.lineno, full))
    return found


def _is_forbidden_http_module(module: str) -> bool:
    """Is this import path an HTTP client?"""
    if module in FORBIDDEN_HTTP_MODULES:
        return True
    if module in FORBIDDEN_HTTP_SUBMODULES:
        return True
    root = module.split(".")[0]
    return root in FORBIDDEN_HTTP_MODULES


def scheduler_package_files() -> list[Path]:
    """The files of the scheduler package, as the application sees them.

    Derived from the application's own file list rather than from a
    hand-written path, so the package is found wherever it lives -- and
    found *nowhere* when it is gone, which is what stops the HTTP scan
    below from passing on an empty haystack.
    """
    return [
        path
        for path in app_source_module.app_files()
        if path.parent.name == SCHEDULER_PACKAGE
    ]


def schema_module(
    sources: Sequence[tuple[object, str]],
) -> tuple[str, str]:
    """The module that owns the schema version and the DDL.

    The version constant is the marker.  When more than one module
    defines it -- which is a broken contract in its own right, and one
    the version test reports -- the module that also carries the
    scheduler's DDL is the one the DDL scan reads, so that one broken
    contract does not take a second one down with it.
    """
    candidates: list[tuple[str, str]] = []
    for path, source in sources:
        tree = _parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == VERSION_NAME:
                        candidates.append((_label(path), source))
                        break
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name) and target.id == VERSION_NAME:
                    candidates.append((_label(path), source))
                    break

    if not candidates:
        raise AssertionError(
            f"{VERSION_NAME} is not defined in any application module, so "
            "there is no schema module to read the DDL from."
        )

    for label, source in candidates:
        names = {
            entry.name
            for entry in find_schema_objects(source, CREATE_TABLE_PATTERN)
        }
        if names.intersection(SCHEDULER_TABLES):
            return label, source
    return candidates[0]


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------


def test_scheduler_router_before_catch_all() -> None:
    """The scheduler's router is registered ahead of the frontend catch-all.

    The catch-all is a GET and Starlette matches first-match-wins, so a
    scheduler router registered after it is unreachable: the operator's
    ``GET /api/scheduler/state`` is answered with the catch-all's
    404 -- which reads like a missing asset, not like a route that is
    registered in the wrong place.  The POST commands keep working (the
    catch-all is a GET), which is what makes the failure half-visible.

    The order is asserted on the *source*, because that is where it is
    decided: the registration is a line in a module, and the line is
    the whole contract.
    """
    sources = app_source_module.app_sources()

    registrations = find_scheduler_registrations(sources)
    assert len(registrations) == 1, (
        f"the scheduler router is registered {len(registrations)} times, "
        "expected exactly one. Two registration points means two answers "
        "to 'where is the scheduler mounted' -- and one of them is the "
        "one nobody reads.\n  "
        + "\n  ".join(
            f"{site.path}:{site.lineno}: {site.text}" for site in registrations
        )
        + (
            "\n  (none found -- the scheduler's router is not mounted at all)"
            if not registrations
            else ""
        )
    )

    catch_alls = find_catch_all_registrations(sources)
    assert len(catch_alls) == 1, (
        f"the frontend catch-all is declared {len(catch_alls)} times, "
        "expected exactly one. Without exactly one, the ordering below "
        "is not a comparison of anything."
    )

    registration = registrations[0]
    catch_all = catch_alls[0]

    assert registration.path == catch_all.path, (
        f"the scheduler router is registered in {registration.path} and "
        f"the frontend catch-all in {catch_all.path}. Two registrations "
        "on the same application can only be ordered by their position "
        "inside one module, so this gate cannot compare them: move the "
        "catch-all into the module that registers the scheduler router, "
        "or update this gate to compare the two include sites."
    )

    assert registration.lineno < catch_all.lineno, (
        f"the scheduler router is registered at {registration.path}:"
        f"{registration.lineno}, which is *after* the frontend catch-all "
        f"at {catch_all.path}:{catch_all.lineno}. Route matching is "
        "first-match-wins, so every GET on the scheduler's read model is "
        "answered by the catch-all with a 404 that looks like a missing "
        "asset -- while the POST commands, which the catch-all does not "
        "match, keep working. That half-visible state is the failure."
    )


def test_schema_version_is_seven_and_monotonic() -> None:
    """The schema version is 7, is defined once, and only ever moves up.

    The version is what tells an existing database that it needs the v7
    scheduler tables.  Three ways to break that, all of them quiet:

    * the version goes backwards -- a database that is ahead of the code
      is told it is behind, and the migration is skipped;
    * the version is defined twice -- two answers to "what version is
      this", and the second one wins by import order;
    * the recorded version is written from a literal -- the same
      rollback, spelled as a number instead of as a comparison.
    """
    sources = app_source_module.app_sources()

    definitions = find_version_definitions(sources)
    assert len(definitions) == 1, (
        f"{VERSION_NAME} is defined {len(definitions)} times, expected "
        "exactly one. Two definitions are two answers to 'which schema "
        "version is this', and the one that wins is decided by import "
        "order.\n  "
        + "\n  ".join(
            f"{path}:{lineno}" for path, lineno, _value in definitions
        )
    )

    path, lineno, value = definitions[0]
    assert value == EXPECTED_SCHEMA_VERSION, (
        f"{path}:{lineno} sets {VERSION_NAME} = {value!r}, expected "
        f"{EXPECTED_SCHEMA_VERSION}. The version is what tells an "
        "existing database that it needs the scheduler's three tables; "
        "a version that does not move forward is a database that is "
        "told it is newer than it is."
    )

    assert value > max(RETIRED_SCHEMA_VERSIONS), (
        f"{path}:{lineno} sets {VERSION_NAME} = {value!r}, which is not "
        f"greater than {max(RETIRED_SCHEMA_VERSIONS)} -- the highest "
        f"version this repository has already shipped "
        f"({', '.join(str(v) for v in RETIRED_SCHEMA_VERSIONS)}). A "
        "schema version is a counter: it moves forward, and it moves "
        "once per shape change."
    )

    rollbacks = find_rollback_comparisons(sources)
    assert not rollbacks, (
        "the current schema version is compared as the *older* side of a "
        "`<` comparison, which is a downgrade: a database that is ahead "
        "of the code would be told it is behind.\n  "
        + "\n  ".join(f"{site.path}:{site.lineno}: {site.text}" for site in rollbacks)
    )

    writes = find_version_writes(sources)
    assert writes, (
        "nothing writes the recorded schema version. The version is the "
        "migration logbook -- without a write, every database stays at "
        "the bootstrap version and the v7 tables are never applied."
    )
    for write in writes:
        names = (
            {node.id for node in ast.walk(write.params) if isinstance(node, ast.Name)}
            if write.params is not None
            else set()
        )
        assert VERSION_NAME in names, (
            f"{write.path}:{write.lineno} writes the schema version with "
            f"{write.sql!r} without binding {VERSION_NAME}. The recorded "
            "version must be the constant, never a literal: a literal is "
            "a second copy of the version, and it is the copy that goes "
            "stale."
        )
        literals = [
            node.value
            for node in ast.walk(write.params)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
        ] if write.params is not None else []
        stale = [number for number in literals if number < (value or 0)]
        assert not stale, (
            f"{write.path}:{write.lineno} writes the schema version from "
            f"the literal(s) {stale}, which are older than "
            f"{VERSION_NAME} = {value}. Writing an older version into "
            "the logbook is a rollback."
        )


def test_ddl_uses_if_not_exists() -> None:
    """The v7 DDL is idempotent: every statement carries the guard clause.

    ``migrate`` is called on every connection the request path opens, so
    its DDL runs again and again.  ``CREATE TABLE IF NOT EXISTS`` makes
    the second run a no-op; without it the second run raises -- on a
    request, in production, with the first run long since forgotten.
    """
    sources = app_source_module.app_sources()
    _schema_path, schema_source = schema_module(sources)

    tables = find_schema_objects(schema_source, CREATE_TABLE_PATTERN)
    indexes = find_schema_objects(schema_source, CREATE_INDEX_PATTERN)

    for table in SCHEDULER_TABLES:
        creating = [entry for entry in tables if entry.name == table]
        assert creating, (
            f"no CREATE TABLE statement for {table} in the schema module. "
            "The scheduler keeps three facts of its own -- the queue "
            "order, the slot occupancy and the manual commands -- and "
            "each of them needs a table."
        )
        for entry in creating:
            assert entry.idempotent, (
                f"{_schema_path}:{entry.lineno} creates {table} without "
                f"IF NOT EXISTS ({entry.text!r}). migrate() runs on every "
                "connection, so the second run of this statement raises "
                "on a request rather than on a deploy."
            )

    for index in SCHEDULER_INDEXES:
        creating = [entry for entry in indexes if entry.name == index]
        assert creating, (
            f"no CREATE INDEX statement for {index} in the schema module. "
            "The index is the lookup a reader needs; without it the "
            "reader degrades into a full scan of the table."
        )
        for entry in creating:
            assert entry.idempotent, (
                f"{_schema_path}:{entry.lineno} creates {index} without "
                f"IF NOT EXISTS ({entry.text!r}). The same second-run "
                "failure as the tables, one object later."
            )


def test_scheduler_package_no_http_client() -> None:
    """The scheduler package imports no HTTP client.

    The scheduler's facts live in one process and one database.  A
    component that reaches the API over a socket queues behind the very
    work it is trying to observe -- a shutdown waits on a request that
    is waiting on the shutdown -- and the decision ends up existing
    twice: once in the process, once in the request.  The two copies
    drift, and the drift is invisible until two readers disagree.
    """
    package = scheduler_package_files()
    assert package, (
        f"no module of the {SCHEDULER_PACKAGE} package was found in the "
        "application's file list. The scan below would pass on an empty "
        "haystack, so the package has to be there before it can be "
        "clean."
    )
    assert any(path.name == "__init__.py" for path in package), (
        f"the {SCHEDULER_PACKAGE} package has no __init__.py among the "
        "application's files, so these are not the scheduler's modules "
        "after all."
    )

    sources = [
        (path, path.read_text(encoding="utf-8")) for path in package
    ]
    findings = find_http_client_imports(sources)
    assert not findings, (
        f"the {SCHEDULER_PACKAGE} package imports an HTTP client. The "
        "scheduler's facts live in this process; reaching them over a "
        "socket queues the request behind the work it is trying to "
        "observe, and puts the decision in two places that drift "
        "apart.\n  "
        + "\n  ".join(
            f"{site.path}:{site.lineno}: imports {site.text}" for site in findings
        )
    )


# ---------------------------------------------------------------------------
# Sensitivity -- the detectors have to be able to fail
# ---------------------------------------------------------------------------

#: A synthetic module in which the scheduler's router is registered
#: *after* the catch-all -- the exact accident this gate exists for.
LATE_SCHEDULER_REGISTRATION = '''\
app = FastAPI()


@app.get("/{path:path}")
def serve_static(path: str):
    return path


from routes import scheduler as _routes_scheduler

app.include_router(_routes_scheduler.router)
'''

#: A synthetic schema module whose DDL forgot the guard clause.
NON_IDEMPOTENT_DDL = '''\
_DDL_SCHEDULER_QUEUE = """
CREATE TABLE scheduler_queue (
  plan_id TEXT PRIMARY KEY
)
"""
'''

#: A synthetic module that reaches for an HTTP client.
HTTP_CLIENT_IMPORT = '''\
import requests


def fetch_state():
    return requests.get("http://127.0.0.1:8000/api/scheduler/state")
'''


def test_detector_reports_a_scheduler_router_registered_after_the_catch_all() -> None:
    """The ordering detector has to see the accident it is looking for.

    A detector that finds nothing passes every gate below it, forever.
    """
    sources = [("server.py", LATE_SCHEDULER_REGISTRATION)]

    registrations = find_scheduler_registrations(sources)
    catch_alls = find_catch_all_registrations(sources)

    assert len(registrations) == 1, (
        f"the planted registration was not found ({len(registrations)} "
        "found) -- the detector cannot see the scheduler's router at all, "
        "so the contract test above would pass on an empty haystack."
    )
    assert len(catch_alls) == 1, (
        "the planted catch-all was not found -- the detector cannot see "
        "the frontend catch-all."
    )
    assert registrations[0].lineno > catch_alls[0].lineno, (
        "the planted registration is *after* the catch-all, so the "
        "comparison the contract test makes must report it as late."
    )


def test_detector_reports_ddl_without_the_guard_clause() -> None:
    """The DDL detector has to notice a missing ``IF NOT EXISTS``."""
    tables = find_schema_objects(NON_IDEMPOTENT_DDL, CREATE_TABLE_PATTERN)

    creating = [entry for entry in tables if entry.name == "scheduler_queue"]
    assert len(creating) == 1, (
        f"the planted CREATE TABLE was not found ({len(creating)} found)."
    )
    assert not creating[0].idempotent, (
        "the planted CREATE TABLE has no IF NOT EXISTS, so the detector "
        "has to report it as non-idempotent."
    )


def test_detector_reports_an_http_client_import() -> None:
    """The import detector has to see the forbidden import."""
    findings = find_http_client_imports([("scheduler/commands.py", HTTP_CLIENT_IMPORT)])

    assert findings, (
        "the planted 'import requests' was not reported -- the HTTP scan "
        "would pass on a package that does reach for a client."
    )
    assert findings[0].text == "requests", findings[0]
