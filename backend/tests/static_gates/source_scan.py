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
            if path.suffix not in extensions:
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:  # pragma: no cover - safety net only
                rel = path
            if excluded & set(rel.parts):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path