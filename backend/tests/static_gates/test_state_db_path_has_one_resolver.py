"""The state-machine DB path is resolved in exactly one place (2026-09-28).

Why this gate exists
--------------------
``state.db``'s location was resolved by **nine** independent copies of the
same two-step rule — ``PDT_STATE_DB_PATH`` env var, else a path derived
from the reader's own ``__file__``:

``server._state_db_path``, ``plan_state._state_db_path``,
``task_repository``, ``task_manager`` (twice), ``agent``, ``watchdog``,
``verification.orchestrator`` and ``notifications.plan_dir_resolver``.

Two had already drifted, and neither failed loudly:

* ``verification/orchestrator`` used ``.parent.parent`` and reached
  ``<repo>/backend/state.db`` — a stale 40 KB database from 2026-08-27
  with no ``plan_tasks`` table. Every ``add_task`` call wrote there, so
  the RP-* repair rows the executor's Phase 2 reconcile depends on were
  silently discarded. The bug was found on 2026-09-12 and fixed by
  adding a third ``.parent``;
* ``watchdog`` did not read ``PDT_STATE_DB_PATH`` at all, so a test that
  redirected the database still had the watchdog opening the operator's
  live one.

Both are the shape this gate removes: a copy that is *correct on the day
it is written*, and whose only failure mode is pointing at a different
valid file. Unlike a typo, there is nothing to notice — the process
starts, the query succeeds, the rows go somewhere nobody reads.

The rule
--------
``backend/config_paths.py`` declares ``STATE_DB`` and
``resolve_state_db_path()``. Every other module reaches the database
through one of those two names.

This is the sibling of ``test_extracted_modules_use_app_root.py``, which
covers the *same defect class* for the application root (``routes/``).
That gate is scoped to modules extracted out of ``server.py``, where
``__file__`` arithmetic changes meaning; this one is scoped to one
*path*, because a module that sits at the right depth today is still a
copy that can be relocated tomorrow.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ``static_gates/`` is on sys.path for this directory's modules (see the
# sibling gates, which import ``source_scan`` the same way).
import source_scan

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: Repo root. ``_STATE_DB_CONSUMERS`` is written relative to it, so the
#: list is anchored here rather than against the process's cwd — this
#: gate must give the same verdict whether pytest is invoked from the
#: repository root or from ``backend/``.
_REPO_ROOT = _BACKEND_DIR.parent

#: The one module allowed to name the file and to read the env var.
_RESOLVER_MODULE = Path("backend/config_paths.py")

#: Modules that open the state-machine database. Each must reach it
#: through ``resolve_state_db_path`` / ``STATE_DB`` — this is the list a
#: future tenth caller joins, and joining it is what forces the new
#: module to go through the resolver instead of hand-rolling the path.
_STATE_DB_CONSUMERS = (
    Path("backend/server.py"),
    Path("backend/plan_state.py"),
    Path("backend/task_repository.py"),
    Path("backend/task_manager.py"),
    Path("backend/agent.py"),
    Path("backend/watchdog.py"),
    Path("backend/state_machine/db/connection.py"),
    Path("backend/verification/orchestrator.py"),
    Path("backend/notifications/plan_dir_resolver.py"),
)


def _is_production(path: Path) -> bool:
    """True for first-party production source, not test code.

    Tests legitimately name ``state.db`` when they build a throwaway
    database or assert on a fixture path; they are not the code that runs
    in the server. A typo here would make every rule below pass
    vacuously, so the predicate is itself pinned by a test.
    """
    return "tests" not in path.parts and not path.name.startswith("test_")


def _production_sources() -> list[Path]:
    return [p for p in source_scan.iter_first_party_sources() if _is_production(p)]


def _string_constants(path: Path) -> list[tuple[int, str]]:
    """Every plain string literal in ``path``, with its line number.

    AST rather than a regex on purpose: ``connection.py`` carries the
    text ``open(DATA_DIR / "state.db")`` inside a ``#`` comment, and a
    grep-shaped gate would flag that comment as an offence until someone
    deleted the explanation. Comments and docstrings are not constants.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


# ---------------------------------------------------------------------------
# Rule 1 — the filename literal lives in the resolver module only.
# ---------------------------------------------------------------------------


def test_only_the_resolver_module_names_the_database_file() -> None:
    offenders: list[str] = []
    for path in _production_sources():
        if path == _RESOLVER_MODULE:
            continue
        try:
            constants = _string_constants(path)
        except (OSError, SyntaxError):
            continue
        for lineno, value in constants:
            if value == "state.db":
                offenders.append(f"{path}:L{lineno}")

    assert not offenders, (
        "a production module names 'state.db' directly. Every one of the "
        "nine original copies of that literal was correct when written, and "
        "two still drifted to a different file without anything failing "
        "(see this module's docstring). Import "
        "config_paths.resolve_state_db_path instead:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Rule 2 — the env var is read once.
# ---------------------------------------------------------------------------


def test_only_the_resolver_module_reads_the_env_override() -> None:
    offenders: list[str] = []
    for path in _production_sources():
        if path == _RESOLVER_MODULE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            # The name appearing in prose is fine and often useful; only
            # a lookup is an offence.
            if "PDT_STATE_DB_PATH" in line and "environ" in line:
                offenders.append(f"{path}:L{lineno}: {line.strip()}")

    assert not offenders, (
        "a production module reads PDT_STATE_DB_PATH itself. `watchdog` did "
        "exactly this — it was the one copy that never honoured the override, "
        "so a test that redirected the database still opened the operator's "
        "live one. Call config_paths.resolve_state_db_path():\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Rule 3 — the known consumers go through the resolver.
# ---------------------------------------------------------------------------


def test_every_state_db_consumer_uses_the_resolver() -> None:
    missing: list[str] = []
    for rel in _STATE_DB_CONSUMERS:
        absolute = _REPO_ROOT / rel
        if not absolute.exists():  # pragma: no cover - module moved or renamed
            missing.append(f"{rel} (module is gone — update _STATE_DB_CONSUMERS)")
            continue
        text = absolute.read_text(encoding="utf-8")
        if "resolve_state_db_path" not in text and "STATE_DB" not in text:
            missing.append(f"{rel} references neither resolve_state_db_path nor STATE_DB")

    assert not missing, (
        "every module that opens the state-machine database must resolve it "
        "through config_paths, so the path is wrong in at most one place:\n  "
        + "\n  ".join(missing)
    )


def test_the_consumer_list_is_not_stale() -> None:
    """A renamed module must not silently drop out of rule 3."""
    absent = [rel for rel in _STATE_DB_CONSUMERS if not (_REPO_ROOT / rel).exists()]
    assert not absent, (
        "_STATE_DB_CONSUMERS names file(s) that no longer exist; rule 3 is "
        f"now checking a shorter list than it claims to: {absent}"
    )


def test_scan_is_non_empty() -> None:
    """A gate that scans nothing passes vacuously.

    ``SCAN_ROOTS`` is relative, so running pytest from anywhere other
    than the repository root yields an empty walk.
    """
    production = _production_sources()
    assert production, (
        "the production filter matched nothing, so every rule above passes "
        "vacuously. Run pytest from the repository root, or check "
        "_is_production()."
    )


def test_production_filter_excludes_tests_and_keeps_source() -> None:
    """``_is_production`` is the one line deciding what gets scanned."""
    assert _is_production(Path("backend/coding_tool.py"))
    assert _is_production(Path("backend/config_paths.py"))
    assert not _is_production(Path("backend/tests/unit/test_plan_state.py"))
    assert not _is_production(Path("backend/tests/conftest.py"))
    # A production module whose *name* merely starts with "test_".
    assert not _is_production(Path("backend/test_helpers.py"))


# ---------------------------------------------------------------------------
# Rule 4 — the resolver behaves as the docstring claims.
# ---------------------------------------------------------------------------


def test_resolver_honours_the_override_and_falls_back_to_the_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config_paths import STATE_DB, STATE_DB_ENV, resolve_state_db_path

    monkeypatch.delenv(STATE_DB_ENV, raising=False)
    assert resolve_state_db_path() == STATE_DB

    monkeypatch.setenv(STATE_DB_ENV, "/tmp/throwaway-state.db")
    assert resolve_state_db_path() == Path("/tmp/throwaway-state.db"), (
        "the override stopped winning — a test would then write into the "
        "operator's live database"
    )


def test_resolver_is_not_cached_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docstring promises per-call resolution; a cached result would
    silently ignore an env var set after import, which is how the
    ``plans/`` fixture pollution happened."""
    from config_paths import STATE_DB_ENV, resolve_state_db_path

    monkeypatch.setenv(STATE_DB_ENV, "/tmp/first.db")
    assert resolve_state_db_path() == Path("/tmp/first.db")
    monkeypatch.setenv(STATE_DB_ENV, "/tmp/second.db")
    assert resolve_state_db_path() == Path("/tmp/second.db")


# ---------------------------------------------------------------------------
# Sensitivity — the scanner must flag the shape it exists to catch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    'REAL_DB = Path(__file__).resolve().parents[3] / "state.db"',
    'db = os.path.join(Path(__file__).parent.parent, "state.db")',
    'DEFAULT = PROJECT_ROOT / "state.db"',
])
def test_scanner_flags_every_shape_that_drifted(source: str, tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert [
        v for _, v in _string_constants(probe) if v == "state.db"
    ], f"the scanner no longer matches {source!r}; the gate is decorative"


def test_scanner_ignores_the_same_text_in_a_comment(tmp_path: Path) -> None:
    """``connection.py`` explains the rule in a comment; a gate that
    cannot tell prose from code gets deleted by the next person it annoys."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        '# callers can use ``open(DATA_DIR / "state.db")`` against it\n'
        'x = 1\n',
        encoding="utf-8",
    )
    assert not [v for _, v in _string_constants(probe) if v == "state.db"]
