"""First-party source walker shared by the static gates.

Why this module exists
----------------------
Three static gates (this commit, plus the ones referenced as
"tasks 3, 4, 16") all need to iterate over the same set of
first-party source files, with the same exclusions, and produce the
same answer on every machine. Repeating that walk in each gate is
where the drift lives: one gate excludes ``.venv``, another does
not, a third one forgets ``__pycache__``, and a fourth forgets that
``backend/scripts/`` is itself part of the tree.

This module pins the single definition.

What "first party" means here
-----------------------------
* Top-level directories checked into the repository that hold
  application source: ``backend/`` (Python backend), ``frontend/``
  (web UI), ``scripts/`` (operator helpers), ``example/`` (config
  templates).
* ``.config/`` is **not** included — it is gitignored, holds
  per-deployment secrets, and is intentionally never scanned.
* ``tests/`` directories are *included*: unit tests, fixture
  samples, and the static gates themselves are all first-party code
  and the home-path rule applies to them. The exclusion of
  ``backend/tests/`` from "production" is a separate concern, owned
  by the gates that need it.

Exclusions
----------
``EXCLUDED_DIRS`` names directories that look like first-party
source but never should be scanned — vendor caches, build outputs,
the plans the application writes at runtime. Names, not paths: a
match anywhere in the relative path's parts removes the file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

#: Top-level first-party source roots. Every directory here is
#: checked into the repository; ``.config/`` is intentionally absent
#: because it is gitignored and holds per-deployment secrets.
SCAN_ROOTS: tuple[Path, ...] = (
    Path("backend"),
    Path("frontend"),
    Path("scripts"),
    Path("example"),
)

#: Directory names that look like first-party source but must never
#: be scanned. Matched anywhere in a file's relative path's parts.
EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".venv",
    "node_modules",
    "__pycache__",
    "plans",
    "backups",
    ".git",
})

#: File extensions considered first-party source by the walker. The
#: set is deliberately narrow: a name like ``*.cfg`` would sweep up
#: pytest's own coverage cache the moment the directory structure
#: moves. The home-path gate only needs to read text; adding a new
#: extension here is the place to do that.
SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".html",
    ".css",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".sh",
})


#: Repository root, derived from this file's location rather than from the
#: process working directory. ``backend/tests/static_gates/source_scan.py``
#: is four parents deep from the root, the same way every gate in this
#: directory already computes it.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: Directory names of :data:`SCAN_ROOTS`. Callers that hold a
#: *repository-root-relative* path — a commit tree, a diff — need this
#: explicitly; the on-disk walker expresses it by resolving its roots.
SCAN_ROOT_NAMES: frozenset[str] = frozenset(p.name for p in SCAN_ROOTS)


def resolve_root(root: Path) -> Path:
    """Absolute form of a scan root.

    A relative root is resolved against :data:`REPO_ROOT`, **not** against
    the process working directory. That distinction is the whole point of
    this function: ``SCAN_ROOTS`` are relative names like ``backend``, and
    resolving them against cwd made the scan depend on how pytest was
    launched. CI runs the suite with ``working-directory: backend``, where
    no ``backend/`` root exists — the walker then fell back to whatever
    relative root happened to exist there (``backend/scripts/``) and
    returned a handful of unrelated files, which is enough for a
    non-empty assertion to pass while the tree the gates exist to scan is
    never read.

    Absolute roots are returned unchanged, so a caller that resolves its
    own root (``test_scripts_have_no_dangerous_defaults`` passes
    ``<project>/scripts``) keeps working.
    """
    return root if root.is_absolute() else REPO_ROOT / root


def repo_relative(path: Path) -> Path:
    """*path* relative to :data:`REPO_ROOT`, or *path* unchanged if outside.

    For display, and for comparing against paths that are already
    repository-root-relative (a ``git ls-tree`` listing).

    Symlinks are **not** resolved on the first attempt, and that ordering
    matters: several files under ``backend/tests/`` are symlinks into
    ``unit/``, and git records the *link* name. Resolving would turn
    ``backend/tests/test_grep_guard.py`` into
    ``backend/tests/unit/test_grep_guard.py`` — a path no commit contains,
    which then compares unequal to the tree listing for no useful reason.
    The resolved form is only tried as a fallback, for a path that reaches
    the repository through a different prefix.
    """
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(REPO_ROOT)
    except (ValueError, OSError):
        return path


def is_first_party_source(
    rel: Path,
    excluded: frozenset[str] = EXCLUDED_DIRS,
    extensions: frozenset[str] = SOURCE_EXTENSIONS,
) -> bool:
    """Return True iff *rel* passes the suffix and excluded-directory tests.

    **Root-agnostic by design.** *rel* may be relative to a scan root
    (``server.py``, as the on-disk walker sees it) or to the repository
    root (``backend/server.py``, as a commit tree sees it) — neither test
    below cares which, because both look only at the suffix and at the
    path's parts. Directory parts are matched anywhere in the path, so
    ``backups/`` excludes a file wherever it appears.

    This deliberately does **not** check that the path sits under a scan
    root. That check is meaningful only for repository-root-relative
    paths, and :func:`is_first_party_path` is the function that applies
    both. Folding it in here would silently mis-judge every caller that
    passes a root-relative path or an absolute one — which is exactly what
    happened: the root membership test was added here, and the
    ``scripts/`` gate (whose root is absolute) and the on-disk walker
    (whose paths are root-relative) both went to zero files while their
    "non-empty" assertions stayed green.
    """
    if rel.suffix not in extensions:
        return False
    return not (excluded & set(rel.parts))


def is_under_scan_root(repo_rel: Path) -> bool:
    """Return True iff a repository-root-relative path is under a scan root.

    ``backend/server.py`` is; ``SECURITY_AUDIT.md``, ``docs/index.md`` and
    ``mkdocs.yml`` are not — no gate has ever scanned those, and a scan
    that starts to would report violations the rest of the suite does not
    know about.
    """
    return bool(repo_rel.parts) and repo_rel.parts[0] in SCAN_ROOT_NAMES


def is_first_party_path(repo_rel: Path) -> bool:
    """``is_first_party_source`` for a repository-root-relative path.

    This is the predicate a caller holding paths from somewhere other than
    a filesystem walk wants — a commit tree (``git ls-tree``), a diff. It
    is the composition the on-disk walker expresses implicitly by starting
    its iteration at the scan roots.
    """
    return is_under_scan_root(repo_rel) and is_first_party_source(repo_rel)


def iter_first_party_sources(
    roots: tuple[Path, ...] = SCAN_ROOTS,
    excluded: frozenset[str] = EXCLUDED_DIRS,
    extensions: frozenset[str] = SOURCE_EXTENSIONS,
) -> Iterator[Path]:
    """Yield every first-party source file under ``roots``.

    Files are returned in sorted order so that gate output is
    deterministic and the same offence list comes out of every run
    on every machine. A file whose path contains any
    ``excluded`` directory name as a path part is skipped. Files
    whose suffix is not in ``extensions`` are skipped; this is the
    single point that decides which file types count as "source".

    Yields **absolute** paths, and each root is resolved against
    :data:`REPO_ROOT` rather than against cwd (see :func:`resolve_root`).
    Both halves matter for the same reason: the result must not depend on
    where the process was started. Absolute paths also keep the callers
    that merely ``read_text()`` working from any working directory, and
    the callers that classify a path by its parts
    (``"tests" not in path.parts``) are unaffected — they never depended
    on the path being relative. A caller that needs the
    repository-root-relative form passes it through
    :func:`repo_relative`.
    """
    seen: set[Path] = set()
    for root in roots:
        resolved = resolve_root(root)
        if not resolved.exists():
            continue
        for path in sorted(resolved.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(resolved)
            except ValueError:  # pragma: no cover - safety net only
                rel = path
            if not is_first_party_source(rel, excluded, extensions):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path