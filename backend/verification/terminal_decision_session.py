"""The sidecar file that carries a terminal decision's session and analysis.

Why this module exists
======================

When a verification round stops and asks a human what to do next, the
answer is produced by an **analysis session** — a conversation with a
model that read the failure scene and wrote a recommendation.  Two
things have to survive the round: the **session id** (which conversation
it was, so the session can be resumed) and the **analysis context**
(what the model was shown, so the answer can be re-read next to the
question it answers).

The decision itself lives in the state database.  But a session id and
a free-form analysis context are not decision fields, and adding
columns for them would change the decision storage schema — which this
work does not do (arch decision point 8).  So they are parked in a
**sidecar**: one JSON file next to the plan,
``<plan_dir>/terminal_decision_session.json``.

A sidecar, not a table
======================

The file is deliberately *not* part of the state database:

* **The decision stays where it is.**  Nothing here writes to
  ``state.db``, nothing here changes a table, nothing here changes the
  schema.  A plan whose sidecar is missing behaves exactly as it did
  before this file existed.
* **It is per-plan, not global.**  It sits in the plan directory next
  to the other plan artefacts, so it is copied, archived and inspected
  with the plan it belongs to.

What this module guarantees
===========================

**The write is atomic.**  The bytes are written to a sibling
``.terminal_decision_session.json.tmp`` file, flushed, and then moved
onto the final path with :func:`os.replace`.  A reader of the final
path therefore sees either the previous record or the complete new one
— never a truncated one.  The staging file is removed if anything goes
wrong, so a failed write does not leave litter next to the plan.

**The read never raises.**  A missing file, a corrupt file, a file that
is not a JSON object, and a file written under a schema version this
module does not know all answer the same way: ``None``.  The reader is
trying to *resume* a session; a session it cannot read is a session it
does not have, and that is a normal state to be in (arch decision
point 5: read it or degrade, never raise).

**The record's shape is fixed.**  :func:`build_record` always produces
the same nine keys, with the same defaults.  A reader that knows the
schema can rely on the keys being there; a file written by a future
version is refused rather than misread.

**A missing session id is not a missing context.**  When no session id
could be obtained, the record carries ``null`` for it and keeps the
analysis context intact — the analysis is still worth having, it just
cannot be resumed.

What this module deliberately does NOT do
=========================================

It imports no orchestrator, no routes and no server — only the standard
library.  The orchestrator will import *this* module; an import the
other way round would be a cycle, and a file helper that pulls the
server in cannot be exercised on its own.

The per-round archive
=====================

The sidecar is **per plan** and the next round overwrites it.  The
analysis it carries is not per-plan though: every terminal round
produces one, and each is a piece of the record of how the plan went.
So the archives live in their own directory — ``<plan_dir>/reports/`` —
one ``.md`` (for a person) and one ``.json`` (for a machine) per write,
and no write ever overwrites another.

The ``.md`` half draws its five sections with the **question's own
renderer** (:func:`verification.terminal_analysis.render_analysis`),
so the archive and the question say the same thing about the same
round.  That renderer is reached from the caller — the orchestrator,
which already imports it — and handed over as the archive's body; see
:func:`save_round_archive`'s ``rendered`` parameter.  This module
itself stays on the standard library (a pinned property — see
``tests/unit/test_terminal_decision_session_file.py``), so it cannot
call that renderer itself; the copy it keeps for the case where no
rendered body was handed over is pinned to the real one by a test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "ARCHIVE_STEM",
    "REPORTS_DIRNAME",
    "SCHEMA_VERSION",
    "SESSION_FILENAME",
    "build_record",
    "load",
    "save",
    "save_round_archive",
    "session_path",
]


#: The sidecar's filename, next to the plan's other artefacts.
SESSION_FILENAME = "terminal_decision_session.json"

#: The record schema this module reads and writes.
#:
#: A file carrying any other version is **refused** rather than read on
#: a best-effort basis: a record whose meaning changed underneath the
#: reader is worse than no record at all.
SCHEMA_VERSION = 1

#: The staging file's suffix.  The staging file lives next to the
#: target and is hidden, so the swap is a rename within one filesystem
#: and the plan directory does not show a second record.
_TMP_SUFFIX = ".tmp"

#: The directory (inside the plan directory) the per-round archives live in.
REPORTS_DIRNAME = "reports"

#: The archive file's stem.  The round number and the UTC timestamp are
#: appended to it: ``terminal-decision-round-3-20261106T173200Z``.
ARCHIVE_STEM = "terminal-decision-round"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def session_path(plan_dir: Any) -> Path:
    """The sidecar's path inside ``plan_dir``."""
    return Path(plan_dir) / SESSION_FILENAME


def _tmp_path(target: Path) -> Path:
    """The staging path for ``target`` — a hidden sibling of it."""
    return target.parent / f".{target.name}{_TMP_SUFFIX}"


def _discard(path: Path) -> None:
    """Remove ``path``, ignoring the fact that it may not be there.

    Used on the failure path of the write: the staging file must not
    survive a failed write, but the failure that is being reported is
    the original one, not a failure to clean up.
    """
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def build_record(
    *,
    decision_id: str,
    plan_id: str,
    analysis: Any = None,
    session_id: Optional[str] = None,
    failed_points: Any = None,
    stop_reason: str = "repair_execution_failed",
    round_number: int = 0,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the record this module writes.

    The nine keys are fixed, so a reader can rely on them:

    ``schema_version``
        always :data:`SCHEMA_VERSION`.
    ``decision_id`` / ``plan_id``
        which decision and which plan this belongs to.
    ``session_id``
        the analysis session to resume, or ``None`` when there is none.
    ``analysis``
        the analysis context, stored **as given** — this module does not
        re-shape it and does not summarise it.
    ``failed_points``
        the failed verification points the analysis was read against.
    ``stop_reason``
        why the round stopped.
    ``round_number``
        which round stopped.
    ``created_at``
        when the record was built; the current local time unless the
        caller stamps its own.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "plan_id": plan_id,
        "session_id": session_id,
        # The analysis is kept verbatim; only its absence is normalised
        # to an empty context, so the key's type never changes.
        "analysis": analysis if analysis is not None else {},
        # The list is copied so a caller mutating its own list cannot
        # change a record that was already built.
        "failed_points": list(failed_points) if failed_points is not None else [],
        "stop_reason": stop_reason,
        "round_number": round_number,
        "created_at": (
            created_at if created_at is not None else datetime.now().isoformat()
        ),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save(plan_dir: Any, record: Mapping[str, Any]) -> Path:
    """Write ``record`` to the sidecar in ``plan_dir``; return its path.

    The write is atomic: the bytes go to a hidden staging file next to
    the target and are then renamed onto it.  A reader never sees half
    a record, and the staging file is gone afterwards — whether the
    write succeeded or failed.

    A failure here is **not** swallowed: a record that could not be
    written is a fact the caller has to hear about.  (Only the *read*
    side degrades.)
    """
    target = session_path(plan_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = dict(record)
    # A record built elsewhere may not carry the version; fill it in.
    # An explicitly wrong version is left alone — the reader refuses it.
    payload.setdefault("schema_version", SCHEMA_VERSION)

    # Serialised before the staging file is created: an unserialisable
    # payload then leaves nothing behind at all.
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    staged = _tmp_path(target)
    try:
        with open(staged, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    except BaseException:
        _discard(staged)
        raise

    return target


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load(plan_dir: Any) -> Optional[Dict[str, Any]]:
    """Read the sidecar in ``plan_dir``; return ``None`` if there is none.

    ``None`` answers every "there is nothing usable here" case: the
    file is missing, it is not readable, it is not JSON, it is not a
    JSON object, or it was written under a schema version this module
    does not know.  Nothing here raises.
    """
    path = session_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, Mapping):
        return None

    if data.get("schema_version") != SCHEMA_VERSION:
        return None

    return dict(data)

# ---------------------------------------------------------------------------
# The per-round archive
# ---------------------------------------------------------------------------
#
# The sidecar above is **per plan** and is **overwritten** by the next
# round: it answers "what did the last terminal round analyse?", and the
# last round is the only one anybody asks about today.  The analysis
# itself is not per-round though — it is the reading of a failure scene,
# and a plan that stopped several times has several of them, each of them
# a piece of the record of how the plan went.  A sidecar cannot keep
# them: one file, one plan, last writer wins.
#
# So the archives live in their own directory, ``<plan_dir>/reports/``,
# one pair of files per write:
#
# * ``terminal-decision-round-<n>-<timestamp>.md`` — the analysis as a
#   person reads it (the five CPEA sections, or the factual sentence when
#   there was no analysis);
# * ``terminal-decision-round-<n>-<timestamp>.json`` — the same round as
#   a machine reads it: the question, the options, the decision, the
#   failed points and the analysis itself.
#
# **Nothing is overwritten.**  The timestamp is to the second, so two
# writes inside the same second would land on the same name; the second
# one takes a ``-2`` suffix, the third ``-3``, and so on.  Every write
# keeps its own files, whatever the round did.


def _utc_now(now: Optional[datetime] = None) -> datetime:
    """``now`` as a timezone-aware UTC reading.

    A naive timestamp is read as UTC — the archive names its files in
    UTC, and a file named in one zone and read in another is a file
    nobody can find.
    """
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _archive_stem(reports_dir: Path, round_number: int, stamp: str) -> str:
    """A file stem no archive in ``reports_dir`` is using yet.

    The round number and the timestamp make the name unique on their
    own — but two writes inside the same second do not.  The second one
    takes ``-2``, the third ``-3``: an archive is never overwritten.
    """
    stem = f"{ARCHIVE_STEM}-{int(round_number)}-{stamp}"
    candidate = stem
    index = 2
    while (
        (reports_dir / f"{candidate}.md").exists()
        or (reports_dir / f"{candidate}.json").exists()
    ):
        candidate = f"{stem}-{index}"
        index += 1
    return candidate


def _clean_body(text: Any) -> str:
    """A section body as one block: no blank lines, no edge whitespace.

    The same rule the question's renderer uses
    (:func:`verification.terminal_analysis.render_analysis`): the
    lines that survive keep their spacing, because the text is the
    judge's and rewriting it would be paraphrasing evidence.
    """
    lines = [line.strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _section(heading: str, body: str) -> str:
    """One ``## heading`` + body block."""
    return f"## {heading}\n{body}"


def _render_cpea(analysis: Mapping[str, Any]) -> str:
    """The five C/P/E/A/U sections of an analysis.

    The same five sections, in the same order and the same layout, as
    the question's renderer
    (:func:`verification.terminal_analysis.render_analysis`) writes
    them — the archive and the question say the same thing, so a reader
    who has seen one recognises the other.

    This is a **copy**, and the copy is why the archive is written the
    way it is: :mod:`verification.terminal_decision_session` imports
    nothing but the standard library (a pinned property — see
    ``tests/unit/test_terminal_decision_session_file.py``), so it
    cannot reach the question's renderer.  The copy is used only when
    the caller handed no rendering over; the caller (the orchestrator)
    draws the body with the question's own renderer and passes it in
    as ``rendered``.  The copy is kept honest by a test that asserts
    the two agree, section for section, on a contract-valid analysis.

    Two differences from the question's renderer, both on purpose:

    * the archive keeps the analysis **whole**.  The question's
      renderer is bounded (it has to fit into a question a person reads
      in a chat); an archive file has no such limit, and the analysis
      is the thing being kept.
    * a section the analysis does not have is left out.  An empty
      section under a heading reads as "the judge said this", which is
      a different claim from "the judge said nothing here".

    The analysis is rendered **as it was given**: this module does not
    re-shape it and does not summarise it, exactly as the sidecar keeps
    it verbatim.
    """
    sections: List[str] = []

    context = _clean_body(analysis.get("context"))
    if context:
        sections.append(_section("背景（C）", context))

    problem = _clean_body(analysis.get("problem"))
    if problem:
        sections.append(_section("问题（P）", problem))

    raw_evidence = analysis.get("evidence")
    if isinstance(raw_evidence, (list, tuple)):
        bullets = [
            str(item).strip() for item in raw_evidence if str(item).strip()
        ]
        if bullets:
            sections.append(
                _section("证据（E）", "\n".join(f"- {item}" for item in bullets))
            )

    recommendation = str(analysis.get("recommendation") or "").strip()
    reason = str(analysis.get("recommendation_reason") or "").strip()
    if recommendation or reason:
        sections.append(
            _section("建议（A）", f"{recommendation} —— {reason}"),
        )

    uncertainty = analysis.get("uncertainty")
    if uncertainty is not None:
        body = _clean_body(uncertainty)
        if body:
            sections.append(_section("不确定（U）", body))

    return "\n\n".join(sections)


def _render_archive_markdown(
    *,
    round_number: int,
    stop_reason: str,
    decision_id: str,
    failed_vp_ids: List[str],
    question: str,
    analysis: Optional[Mapping[str, Any]],
    rendered: Optional[str] = None,
) -> str:
    """The archive's human-readable half.

    The header says **which** round this is — round number, stop reason,
    the decision it produced (empty when there was none) and the points
    that failed.  Then the analysis itself: the five CPEA sections, or
    — when the round had no analysis to render — the factual sentence
    that was put to the human, verbatim.

    ``rendered`` is the analysis as the **question's** renderer drew it,
    handed over by the caller.  It is preferred over anything drawn
    here, because the question's renderer is the one that decided
    whether the analysis *is* an analysis: an analysis it refuses is
    not rendered at all, and the archive then keeps the question's
    factual sentence — the same thing the human was shown.  ``None``
    means "nobody drew one" (a direct call by a test or another
    caller), and the copy kept here is used instead.
    """
    lines = [
        f"# 终局决策分析 · round {int(round_number)}",
        "",
        f"- stop_reason: {stop_reason}",
        f"- decision_id: {decision_id}",
        "- failed points: "
        + (", ".join(str(vp_id) for vp_id in failed_vp_ids) or "none"),
        "",
    ]
    body = ""
    if rendered is not None:
        # The caller's rendering wins, including when it is empty: an
        # empty rendering is the renderer saying "this is not an
        # analysis", and that answer is the answer.
        body = str(rendered)
    elif isinstance(analysis, Mapping):
        body = _render_cpea(analysis)
    if not body:
        # No analysis, or an analysis with nothing in it: what the
        # human was asked is the fact the archive keeps.
        body = str(question or "")
    lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


def _write_archive_pair(
    md_target: Path,
    md_text: str,
    json_target: Path,
    json_text: str,
) -> None:
    """Write the two halves of one archive, atomically.

    Both halves go to staging files first and are then renamed onto
    their names, so a reader never sees half an archive.  If anything
    fails, the staging files are removed and the half that was already
    renamed is removed with them: an archive is a **pair**, and a pair
    with one half is a file nobody knows what to do with.
    """
    staged_md = _tmp_path(md_target)
    staged_json = _tmp_path(json_target)
    renamed: List[Path] = []
    try:
        for staged, text in ((staged_md, md_text), (staged_json, json_text)):
            with open(staged, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(staged_md, md_target)
        renamed.append(md_target)
        os.replace(staged_json, json_target)
        renamed.append(json_target)
    except BaseException:
        _discard(staged_md)
        _discard(staged_json)
        for target in renamed:
            _discard(target)
        raise


def save_round_archive(
    plan_dir: Any,
    *,
    round_number: int,
    stop_reason: str,
    question: str,
    options: List[str],
    default_option: str,
    analysis: Optional[Mapping[str, Any]],
    decision_id: str,
    failed_vp_ids: List[str],
    rendered: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Archive one terminal round's analysis; return the ``.md`` path.

    The round's analysis — what the judge made of the failure scene — is
    the one thing about a terminal round that cannot be reconstructed
    later: it is a model call that already happened.  If it is not
    written down it is gone, and the two ways it used to be gone were
    the round that asked a question nobody was ever asked (the analysis
    was computed and thrown away) and the round that came after it (the
    sidecar was overwritten).  So every terminal round writes **its own**
    pair of files, and no write ever overwrites another.

    The archive says what the round was, what it was asked and what it
    found: the stop reason, the decision (empty when the round asked
    nobody), the failed points, the question and its options, and the
    analysis itself.

    **A write that fails is not a failed round.**  The decision stands
    whatever happens here, so every failure is swallowed and answered
    with ``None`` (arch decision point 5: read it or degrade, never
    raise).  Nothing here touches ``state.db``, adds a table or changes
    a column.

    Parameters
    ----------
    plan_dir:
        The plan directory.  The archives go into its ``reports/``
        subdirectory, which is created when it is missing.
    round_number:
        Which round stopped.
    stop_reason:
        Why the round stopped.
    question:
        The question the round put to a human — the factual sentence,
        which is also what the archive keeps when there is no analysis.
    options:
        The answers the question offered.
    default_option:
        The answer the question falls back to when nobody replies.
    analysis:
        The analysis the round produced, or ``None`` when it had none.
    decision_id:
        The decision the question became — empty when nobody was asked.
    failed_vp_ids:
        The verification points that failed.
    rendered:
        The analysis as the **question's** renderer drew it — the five
        CPEA sections, or ``""`` when the renderer refused the analysis.
        The caller (the orchestrator) draws it, because this module
        imports nothing but the standard library and cannot reach
        :func:`verification.terminal_analysis.render_analysis` itself.
        ``None`` means "nobody drew one", and the copy kept here is
        used instead; see the module docstring.
    now:
        The clock reading the file names and ``written_at`` are stamped
        with; the current UTC time when it is not given.

    Returns
    -------
    The path of the ``.md`` file that was written, or ``None`` when
    nothing could be written.
    """
    try:
        moment = _utc_now(now)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
        reports_dir = Path(plan_dir) / REPORTS_DIRNAME
        reports_dir.mkdir(parents=True, exist_ok=True)
        stem = _archive_stem(reports_dir, round_number, stamp)
        md_target = reports_dir / f"{stem}.md"
        json_target = reports_dir / f"{stem}.json"

        md_text = _render_archive_markdown(
            round_number=round_number,
            stop_reason=str(stop_reason),
            decision_id=str(decision_id or ""),
            failed_vp_ids=list(failed_vp_ids or []),
            question=str(question or ""),
            analysis=analysis,
            rendered=rendered,
        )
        payload = {
            "round": int(round_number),
            "stop_reason": str(stop_reason),
            "decision_id": str(decision_id or ""),
            "failed_points": list(failed_vp_ids or []),
            "options": list(options or []),
            "default_option": str(default_option or ""),
            "analysis": dict(analysis) if isinstance(analysis, Mapping) else None,
            "written_at": moment.isoformat(),
        }
        json_text = json.dumps(payload, ensure_ascii=False, indent=2)

        _write_archive_pair(md_target, md_text, json_target, json_text)
    except Exception:  # noqa: BLE001 — the round does not depend on this
        return None
    return md_target
