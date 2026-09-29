"""Archive-scan module — boundary-value judgement for plan cutoff.

This module implements the archive-scan contract pinned by
architecture decision point 7: a hard 2026-08-05 cutoff between
"active" plans (kept in SQLite) and "archived" plans (rendered
read-only via 410 Gone semantics). Archived plans do NOT enter
SQLite — their existence is captured purely by the directory tree
and the requirement text in ``interview.json``.

The three exported symbols are:

  * :data:`CUTOFF_2026_08_05` — the pinned boundary ``datetime``.
    Module-level so tests can ``monkeypatch`` it for forward
    compatibility.

  * :func:`classify_plan` — pure function that decides
    ``"archived"`` vs ``"new"`` for a single plan directory.
    Dirname (``YYYYMMDD-*`` format) takes priority over mtime; an
    unparsable dirname stays ``"new"`` — archiving requires positive
    evidence of an old creation date, not the absence of a parseable
    one (see the rationale on :func:`classify_plan`).

  * :func:`scan_archived_plans` — directory scan returning only
    directory-derived metadata (``plan_id``, ``requirement_first_line``,
    ``created_at``, ``archived``). Stage / current_phase fields are
    deliberately omitted — archived plans do not enter SQLite.

The companion module :mod:`state_machine.db.artifact_scan` derives
artifact status from file existence (no JSON parsing) so archived
plans can still surface their files without writing to
``plan_artifacts``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

__all__ = [
    "CUTOFF_2026_08_05",
    "classify_plan",
    "scan_archived_plans",
]

#: The pinned cutoff boundary. Plans with dirname date
#: ``<= CUTOFF_2026_08_05`` are classified as "archived".
#:
#: Exposed at module level so tests can ``monkeypatch`` it (e.g.
#: to test forward compatibility at a later cutoff).
#:
#: Note: this is a naive ``datetime`` (no timezone) representing
#: 2026-08-05 00:00:00 **local time** per the spec. The plan-dir
#: ``YYYYMMDD`` date is also interpreted as local-naive, so both
#: sides agree on timezone.
CUTOFF_2026_08_05: datetime = datetime(2026, 8, 5)

#: Regex matching a plan directory name with the canonical
#: ``YYYYMMDD-*`` prefix. The first 8 digits encode the creation
#: date in local-naive time. Anything not matching this prefix is
#: treated as unparsable (safe-downgrade to "archived").
_DIRNAME_DATE_RE = re.compile(r"^(?P<date>\d{8})(?:-.*)?$")

#: Archive classification label returned by :func:`classify_plan`.
Classification = Literal["archived", "new"]


def _parse_dirname_date(plan_dir: Path) -> datetime | None:
    """Return the ``datetime`` encoded in ``plan_dir.name``'s prefix.

    Returns ``None`` if the dirname does not start with 8 digits
    (the ``YYYYMMDD-*`` convention).
    """
    match = _DIRNAME_DATE_RE.match(plan_dir.name)
    if not match:
        return None
    date_str = match.group("date")
    try:
        return datetime(
            int(date_str[0:4]),
            int(date_str[4:6]),
            int(date_str[6:8]),
        )
    except ValueError:
        # E.g. ``20261301`` — month 13 is invalid. Treat as
        # unparsable so the caller can downgrade safely.
        return None


def _read_requirement_first_line(plan_dir: Path) -> str:
    """Return the first non-empty line of ``interview.json``.

    Falls back to ``""`` if the file is missing or unparsable —
    the archive scan must not raise on partial archives.
    """
    interview_path = plan_dir / "interview.json"
    if not interview_path.exists():
        return ""
    try:
        payload = json.loads(interview_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    requirement = payload.get("requirement", "") if isinstance(payload, dict) else ""
    if not isinstance(requirement, str):
        return ""
    # First line of the requirement string, stripped.
    for line in requirement.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def classify_plan(
    plan_dir: Path,
    cutoff: datetime,
) -> Classification:
    """Decide whether ``plan_dir`` is ``"archived"`` or ``"new"``.

    Boundary rule: ``parsed_date <= cutoff`` => ``"archived"``
    (钉死). This includes the exact-cutoff moment.

    Precedence:

      1. If the dirname parses as ``YYYYMMDD-*`` -> use that date.
         The dirname is the user-asserted creation date and is the
         single source of truth for archive decisions.
      2. Otherwise -> ``"new"`` (the plan was created without a
         date prefix, e.g. ``vp001-...`` test fixtures or
         ``migration-plan-...`` work-in-progress dirs that haven't
         been promoted to the canonical naming convention yet).

    The directory's mtime is intentionally NOT consulted when the
    dirname parses cleanly: a hand-edited mtime must never
    resurrect an archived plan.

    Note: the previous implementation treated unparsable dirnames
    as ``"archived"`` on the safe-side argument. That was wrong:
    it conflated "we don't know when this was created" with
    "this was created before 2026-08-05" — the latter requires
    *some* evidence of an old creation date, not the absence of
    a parseable one. The corrected rule downgrades unparsable
    names to ``"new"`` so that legitimate non-conforming plans
    (test fixtures, ad-hoc dirs) are not silently archived. The
    legacy 2026-08-05 archive boundary is still enforced via the
    plan_state column for plans that actually have the canonical
    YYYYMMDD-* prefix.
    """
    parsed = _parse_dirname_date(plan_dir)
    if parsed is None:
        return "new"
    if parsed <= cutoff:
        return "archived"
    return "new"


def scan_archived_plans(
    data_dir: Path,
    cutoff: datetime = CUTOFF_2026_08_05,
) -> list[dict]:
    """Return one row per archived plan under ``data_dir``.

    Each row contains only directory-derived metadata:

        ``plan_id``               str — the directory basename
        ``requirement_first_line``str — first non-empty line of
                                        ``interview.json``'s
                                        ``requirement`` field
                                        (or ``""`` if absent)
        ``created_at``            str — ISO date ``YYYY-MM-DD``
                                        parsed from the dirname
        ``archived``              bool — always ``True``

    Stage / current_phase fields are deliberately OMITTED. Archived
    plans do not enter the SQLite state machine; their existence
    is captured purely by the directory tree and the requirement
    text. Returning those fields would leak SQLite semantics and
    let downstream callers accidentally re-enter the archived plan
    into active routing.

    Edge cases:

      * ``data_dir`` does not exist -> ``[]`` (no exception).
      * Non-directory entries (regular files, symlinks pointing
        outside) -> silently skipped.
      * Dirname not parseable as a date -> classified as ``"new"``
        and therefore EXCLUDED from the result. Archiving requires
        positive evidence of an old creation date (see
        :func:`classify_plan`), so an unparsable name is not swept
        into the archived set.
    """
    if not data_dir.exists() or not data_dir.is_dir():
        return []

    rows: list[dict] = []
    for entry in sorted(data_dir.iterdir()):
        if not entry.is_dir():
            continue
        if classify_plan(entry, cutoff) != "archived":
            continue
        parsed = _parse_dirname_date(entry)
        created_at = parsed.strftime("%Y-%m-%d") if parsed is not None else ""
        rows.append(
            {
                "plan_id": entry.name,
                "requirement_first_line": _read_requirement_first_line(entry),
                "created_at": created_at,
                "archived": True,
            }
        )
    return rows
