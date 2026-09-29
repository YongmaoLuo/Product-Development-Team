"""Artifact-scan module — file-existence derived artifact status.

This module is the read-side companion to :mod:`state_machine.db.archive_scan`.
For archived plans (which do NOT enter the SQLite state machine
and therefore never write to ``plan_artifacts``), the artifact
status must be derived purely from directory contents — no JSON
parsing, no schema awareness.

The four canonical artifact types come straight from the
state-machine refactor's plan-directory contract:

    interview.json  — requirement text (always present in healthy plans)
    prd.json        — PRD document
    review.json     — PRD review annotations
    tasks.json      — task list

The shape of each row is::

    {
      "artifact_type": str,   # one of the four above
      "file_path":     str,   # basename inside the plan dir
      "status":        str,   # "present" | "missing"
    }

A future expansion (e.g. ``arch-design.md``,
``test-design.md``) only needs to add a new
entry to :data:`_KNOWN_ARTIFACTS` — the row shape stays identical
so consumers don't break.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["scan_artifacts"]

#: Canonical plan-directory artifact files. The order is
#: deterministic so ``scan_artifacts`` always returns rows in the
#: same order (which keeps snapshot tests stable).
_KNOWN_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("interview", "interview.json"),
    ("prd", "prd.json"),
    ("review", "review.json"),
    ("tasks", "tasks.json"),
)


def scan_artifacts(plan_dir: Path) -> list[dict]:
    """Return one row per known artifact type, derived from file existence.

    Each row's ``status`` is ``"present"`` if the corresponding
    file exists inside ``plan_dir``, else ``"missing"``. No JSON
    parsing, no schema awareness — purely a directory listing
    against :data:`_KNOWN_ARTIFACTS`.

    If ``plan_dir`` does not exist, every artifact reports
    ``"missing"`` — the function never raises on a missing
    directory (callers treat archives as optional by definition).
    """
    rows: list[dict] = []
    for artifact_type, filename in _KNOWN_ARTIFACTS:
        file_path = plan_dir / filename
        rows.append(
            {
                "artifact_type": artifact_type,
                "file_path": filename,
                "status": "present" if file_path.exists() else "missing",
            }
        )
    return rows
