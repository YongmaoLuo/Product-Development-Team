"""Remove stale ``.venv.bak-python39-*`` backups.

The repo's ``backend/.venv`` was rebuilt as a Homebrew Python 3.11 venv
(see the ``project_venv_xcode_to_homebrew_311`` memory). The migration
left behind ``.venv.bak-python39-<timestamp>/`` snapshots from the
previous Xcode-Python 3.9 venv. Those backups are now stale and must
not re-appear in the repo. This module provides one function —
``cleanup_venv_bak_dirs`` — that globs ``.venv.bak-python39-*`` under a
given root and removes every match via ``shutil.rmtree``.

The live-state contract (no such dir exists in ``backend/``) is
verified by ``tests/test_venv_backup_cleanup.py``. The behaviour
contract (which dirs are removed, in what order, return value) is
verified by ``tests/test_cleanup_venv_bak.py``.

Design notes
------------
* The pattern is hard-coded to ``.venv.bak-python39-*`` — never to a
  bare ``.venv.bak-*``. Python 3.11 backups (if any ever exist) have
  different semantics and must NOT be removed by this helper.
* The function never recurses into ``.venv`` itself (the literal
  pattern ``.venv.bak-python39-*`` does not match ``.venv``).
* ``shutil.rmtree`` runs without ``ignore_errors`` so the caller can
  observe and react to a real failure (e.g. permission denied). The
  backend runner that wraps this helper prints a deterministic
  ``TEST_RESULT`` line based on whether the post-condition
  (no ``.venv.bak-python39-*`` matches remain) holds.
"""

from __future__ import annotations

import shutil
from pathlib import Path


# Literal glob pattern. Single source of truth — imported by tests
# via ``from scripts.cleanup_venv_bak import VENV_BAK_GLOB``.
VENV_BAK_GLOB = ".venv.bak-python39-*"


def cleanup_venv_bak_dirs(root: Path | str) -> list[Path]:
    """Glob ``root/.venv.bak-python39-*`` and ``shutil.rmtree`` each match.

    Parameters
    ----------
    root
        Directory under which to search. Typically the ``backend/``
        directory. Must exist (a missing root is treated as zero
        matches, not an error — this matches the lazy discovery
        semantics of ``pathlib.Path.glob``).

    Returns
    -------
    list[Path]
        The absolute paths that were removed, sorted by name for
        deterministic output. Empty if no matches were found.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    removed: list[Path] = []
    # ``Path.glob`` is lazy; materialise once. Sort for determinism.
    for path in sorted(root_path.glob(VENV_BAK_GLOB)):
        if not path.is_dir():
            continue
        shutil.rmtree(path)
        removed.append(path.resolve())

    return removed


__all__ = ["VENV_BAK_GLOB", "cleanup_venv_bak_dirs"]


if __name__ == "__main__":
    # CLI entry point: ``backend/.venv/bin/python3 -m scripts.cleanup_venv_bak``.
    # Useful for ad-hoc operator cleanup. The backend has its own
    # wrapper (``scripts/runners/run_venv_bak_cleanup.py``) that calls
    # this function and prints the canonical ``TEST_RESULT`` line.
    import sys

    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <root>", file=sys.stderr)
        raise SystemExit(2)
    removed_paths = cleanup_venv_bak_dirs(sys.argv[1])
    for p in removed_paths:
        print(f"removed: {p}")
    if not removed_paths:
        print(f"no matches for {VENV_BAK_GLOB!r} under {sys.argv[1]}")