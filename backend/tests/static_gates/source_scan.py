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


#: Directory names of :data:`SCAN_ROOTS`. Callers that hold a
#: *repository-root-relative* path — a commit tree, a diff — need this
#: explicitly; the on-disk walker gets it for free by starting at the roots.
SCAN_ROOT_NAMES: frozenset[str] = frozenset(p.name for p in SCAN_ROOTS)


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
    """
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:  # pragma: no cover - safety net only
                rel = path
            # ``rel``, not ``root / rel``: the predicate is root-agnostic,
            # and a root may be absolute (``test_scripts_have_no_dangerous_
            # defaults`` passes an absolute ``scripts/`` root), so
            # re-attaching it would produce a path whose first part is
            # ``/`` and confuse any caller that does look at the first part.
            if not is_first_party_source(rel, excluded, extensions):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path