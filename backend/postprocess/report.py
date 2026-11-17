"""The post-processing terminal report — the plan's final word.

What this module owns
----------------------

The finishing stage (收尾) ends in exactly one state, and that state is
read by three consumers that never ran the stage: the dashboard's
terminal panel, the failure notification, and the resume entry point.
All three read **one** file — ``plans/{id}/postprocess_report.json`` —
so this module is the only place that decides what that file contains.

The report's shape is the architecture's data model, written down::

    {
      "spec_sha256": "<the frozen requirement book's fingerprint>",
      "terminal_status": "completed" | "postprocess_failed",
      "items": [
        {
          "id": "<item id>",
          "verdict": "PASS" | "FAIL" | "SKIPPED",
          "evidence_excerpt": "<the evidence, redacted>",
          "audit": {"action": "...", "command": ["git", "push"], "target": "..."}
        }
      ],
      "failure_context_ref": "<path to the progress file>" | null
    }

Why the report is a file of its own
-----------------------------------

The stage keeps two files on purpose (architecture decision point 3).
``postprocess_progress.json`` is rewritten *during* the stage — it is
the retry's memory of which items failed.  The report is written *once*,
at the end, and it is the terminal truth: the panel's terminal panel,
the failure notification and the resume entry point all read it.  Had
the two shared one file, a retry's progress write would overwrite the
terminal verdict, and the report would stop being a report.

Why the write is atomic
-----------------------

The panel reads the report *while* the stage writes it, and an
interruption between the two steps is not exotic: the stage runs
outward-facing commands (``git push``, ``gh pr create``) and those get
killed.  A half-written JSON is not a degraded report — it is an
unparseable one, and the consumer that trips over it is the human
asking "did it work?".

So the write is "temporary file in the same directory + ``os.replace``".
``os.replace`` is atomic on every filesystem this runs on, and a reader
sees either the whole previous report or the whole new one — never a
half of each.  The temporary file is removed when the rename fails, so
a failed write leaves the plan directory exactly as it found it.

Why the report redacts what it carries
-------------------------------------
The report is an audit artefact: it republishes what a command printed
and what was done, so a credential that reached the command's output
would reach the file — and the file is read by the panel's terminal
summary and quoted by the failure notification, so the value would go
on the wire with it.

The excerpt that arrives here has already been redacted by the judge
(the boundary is drawn where the text is **produced**), and the report
redacts once more on the way to the file.  Redacting twice is
idempotent, so the second pass costs nothing and closes the other
half of the boundary: a caller that hands this writer an excerpt it
built itself — and the audit triple, which is text too — still cannot
put a credential into the file.  A credential is replaced with
``<redacted>``, never dropped, so a reader can still see that
something was there.

Why the report is written private
---------------------------------
The report is an audit artefact: it records what was done, as it was
done, and by which command.  It lands in the plans tree, which is the
operator's own home directory — so a mode that leaves it readable by
everything running as that operator hands the audit trail to every
other process on the machine.

0600 is therefore a **contract of the write**, not a default the writer
happens to inherit.  The mode is set on the *open descriptor* of the
temporary file, before the first byte is written, so it does not depend
on the process umask, on a default ACL of the plans directory, or on
what ``mkstemp`` happens to create today — and the file is not readable
by anyone but its owner even for the instant between its creation and
the rename.  The report a resumed run writes over the one the crashed
run left behind goes through the same writer, and is held to the same
contract.

Why ``audit.command`` must be a list
------------------------------------

The audit triple records *what was done, verbatim, and to what* — the
panel shows it and a human re-runs it.  A concatenated string
(``"git push"``) has already thrown away the argument boundaries: it
cannot say whether the target was ``origin`` or ``origin main``, and
it cannot be re-run without guessing.  The argv list keeps the
boundaries, so a string where a list belongs is refused rather than
quietly accepted.

Why ``terminal_status`` is a closed enum
----------------------------------------

Two terminal states exist: the stage completed, or it failed.  The
value is read by the panel to decide which panel to draw and by the
notification to decide whether to send anything at all, so an
unrecognised value is not a "maybe failed" — it is a report nobody can
interpret.  An illegal value is refused at write time, where the caller
can still see what it passed.

The status is **derived** when the caller does not state one: an item
whose verdict is neither ``PASS`` nor ``SKIPPED`` means the stage did
not finish, so the status is ``postprocess_failed``.  The derivation is
fail-closed on purpose — an unrecognised verdict is treated as a
failure, because a verdict nobody recognises must not be reported as a
success.  A caller that knows better states the status outright, and
then the closed enum applies to it.

Why the shapes are closed
-------------------------

The report is a contract with three consumers written against one
document.  A key the architecture does not declare is a key the panel
does not render and the notification does not quote — it looks
supported and is not.  So an unknown key in an item, in the audit
triple, or an illegal value anywhere is refused at write time, naming
the offending field.  Optional items are not an exception to the
shape: they are expressed as the verdict ``SKIPPED``, which the
architecture's four keys already carry.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from config_paths import resolve_plans_dir
from framework.ids import validate_plan_id
from postprocess.notify import redact


__all__ = [
    "AUDIT_KEYS",
    "ITEM_KEYS",
    "NON_FAILURE_VERDICTS",
    "REPORT_FILENAME",
    "REPORT_FILE_MODE",
    "REPORT_KEYS",
    "ReportError",
    "TERMINAL_STATUSES",
    "TERMINAL_STATUS_COMPLETED",
    "TERMINAL_STATUS_FAILED",
    "read_report",
    "report_path",
    "validate_terminal_status",
    "write_report",
]


#: The report's file name inside a plan directory.
REPORT_FILENAME = "postprocess_report.json"

#: The mode the report is written with: readable by its owner only.
#:
#: The report is an audit artefact, so this is a contract of the write
#: rather than a default that happens to hold today.  The writer sets it
#: on the open descriptor of the temporary file — see
#: :func:`_write_json_atomic` — so it does not depend on the process
#: umask, nor on a default ACL of the plans directory, nor on what
#: ``mkstemp`` creates.
REPORT_FILE_MODE = 0o600

#: The stage finished: every required item passed.
TERMINAL_STATUS_COMPLETED = "completed"

#: The stage did not finish: at least one required item failed.
TERMINAL_STATUS_FAILED = "postprocess_failed"

#: The only two terminal statuses.  A report carries one of them, always.
TERMINAL_STATUSES = frozenset(
    {TERMINAL_STATUS_COMPLETED, TERMINAL_STATUS_FAILED}
)

#: The report's four keys — the architecture's data model.
REPORT_KEYS = frozenset(
    {"spec_sha256", "terminal_status", "items", "failure_context_ref"}
)

#: One report item's four keys.
ITEM_KEYS = frozenset({"id", "verdict", "evidence_excerpt", "audit"})

#: The audit triple's three keys.
AUDIT_KEYS = frozenset({"action", "command", "target"})

#: The verdicts that do **not** mean the stage failed.  ``SKIPPED`` is
#: an optional item that was not required to run: the stage still
#: completed.  Every other verdict is a failure — fail-closed.
NON_FAILURE_VERDICTS = frozenset({"PASS", "SKIPPED"})


class ReportError(ValueError):
    """The report is not legal.

    Raised for an illegal ``terminal_status``, a malformed item or
    audit triple, a frozen spec without a fingerprint, and a report
    file on disk that cannot be parsed.  It is a :class:`ValueError`
    because every case it covers is "a value that cannot be right" —
    the same category the state machine uses for an invalid phase.
    """


def report_path(plan_id: str, plans_dir: Optional[Path] = None) -> Path:
    """Where the report of ``plan_id`` lives.

    Args:
        plan_id: The plan's id.  It is validated as a filesystem leaf
            (no separators, no traversal), so a caller cannot make the
            report land outside the plans root.
        plans_dir: The plans root.  Defaults to
            :func:`config_paths.resolve_plans_dir`, which honours the
            ``PDT_PLANS_DIR`` escape hatch — that is how the test
            suite keeps its reports out of the operator's live tree.

    Returns:
        The path of the plan's ``postprocess_report.json``.
    """
    root = Path(plans_dir) if plans_dir is not None else resolve_plans_dir()
    return root / validate_plan_id(plan_id) / REPORT_FILENAME


def validate_terminal_status(value: Any) -> str:
    """Return ``value`` if it is one of the two legal terminal statuses.

    Args:
        value: The candidate status.

    Returns:
        The status, unchanged.

    Raises:
        ReportError: The value is not ``completed`` or
            ``postprocess_failed``.
    """
    if not isinstance(value, str) or value not in TERMINAL_STATUSES:
        raise ReportError(
            f"terminal_status must be one of "
            f"{sorted(TERMINAL_STATUSES)}, got {value!r}"
        )
    return value


def write_report(
    plan_id: str,
    *,
    frozen_spec: Any,
    item_verdicts: Sequence[Mapping[str, Any]],
    failure_context_ref: Optional[str] = None,
    terminal_status: Optional[str] = None,
    plans_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write the terminal report of ``plan_id`` and return what was written.

    Args:
        plan_id: The plan the report belongs to.
        frozen_spec: The frozen requirement book.  Only its
            ``spec_sha256`` is read; anything carrying that attribute
            works (the orchestrator hands over the real ``FrozenSpec``).
        item_verdicts: One record per item, each carrying ``id``,
            ``verdict``, ``evidence_excerpt`` and the ``audit`` triple.
        failure_context_ref: Where a human finds the context of a
            failure — the progress file's path.  ``None`` when the
            stage completed.
        terminal_status: The stage's terminal status.  ``None`` derives
            it from the verdicts (any failing verdict means the stage
            failed); a stated status is checked against the closed
            enum.
        plans_dir: The plans root — see :func:`report_path`.

    Returns:
        The report document, exactly as it was written to disk.

    Raises:
        ReportError: The report would not be legal — see the module
            docstring for what "legal" means here.
    """
    spec_sha256 = _spec_sha256(frozen_spec)
    items = [_build_item(raw, index) for index, raw in enumerate(item_verdicts)]
    status = _resolve_terminal_status(terminal_status, items)

    if failure_context_ref is not None and not isinstance(
        failure_context_ref, str
    ):
        raise ReportError(
            f"failure_context_ref must be a string or None, got "
            f"{type(failure_context_ref).__name__}"
        )

    report: Dict[str, Any] = {
        "spec_sha256": spec_sha256,
        "terminal_status": status,
        "items": items,
        "failure_context_ref": failure_context_ref,
    }

    _write_json_atomic(report_path(plan_id, plans_dir), report)
    return report


def read_report(
    plan_id: str,
    plans_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read the terminal report of ``plan_id``.

    Args:
        plan_id: The plan the report belongs to.
        plans_dir: The plans root — see :func:`report_path`.

    Returns:
        The report document, or ``None`` when the plan has no report —
        it never reached the finishing stage, or the stage has not
        finished yet.  "No report" is an ordinary state, not an error.

    Raises:
        ReportError: A report exists but cannot be read — truncated
            JSON, or a document that is not the report.  A corrupt
            report must never be read as "no report": the difference
            between "the stage never ran" and "the stage's answer was
            destroyed" is the difference between a clean retry and a
            silent one.
    """
    path = report_path(plan_id, plans_dir)

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReportError(
            f"cannot read the report of {plan_id!r}: {exc}"
        ) from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportError(
            f"the report of {plan_id!r} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(raw, Mapping):
        raise ReportError(
            f"the report of {plan_id!r} is not a JSON object, got "
            f"{type(raw).__name__}"
        )

    missing = sorted(REPORT_KEYS - set(raw))
    if missing:
        raise ReportError(
            f"the report of {plan_id!r} is missing key(s): "
            f"{', '.join(missing)}"
        )

    return dict(raw)


def _spec_sha256(frozen_spec: Any) -> str:
    """The fingerprint of the frozen requirement book.

    The report's whole traceability claim rests on this one string: it
    says *which* version of the requirement book the verdicts were
    judged against.  A report without it cannot be tied to anything, so
    a frozen spec that does not carry one is refused.
    """
    sha256 = getattr(frozen_spec, "spec_sha256", None)
    if sha256 is None and isinstance(frozen_spec, Mapping):
        sha256 = frozen_spec.get("spec_sha256")

    if not isinstance(sha256, str) or not sha256:
        raise ReportError(
            "frozen_spec must carry a non-empty string spec_sha256, got "
            f"{frozen_spec!r}"
        )
    return sha256


def _resolve_terminal_status(
    stated: Optional[str],
    items: Sequence[Mapping[str, Any]],
) -> str:
    """The stage's terminal status: the stated one, or the derived one."""
    if stated is not None:
        return validate_terminal_status(stated)

    for item in items:
        if item["verdict"] not in NON_FAILURE_VERDICTS:
            return TERMINAL_STATUS_FAILED
    return TERMINAL_STATUS_COMPLETED


def _build_item(raw: Any, index: int) -> Dict[str, Any]:
    """One validated report item, normalised to the architecture's keys."""
    if not isinstance(raw, Mapping):
        raise ReportError(
            f"item #{index} must be a mapping, got {type(raw).__name__}"
        )

    missing = sorted(ITEM_KEYS - set(raw))
    if missing:
        raise ReportError(
            f"item #{index} is missing key(s): {', '.join(missing)}"
        )

    unknown = sorted(set(raw) - ITEM_KEYS)
    if unknown:
        raise ReportError(
            f"item #{index} carries key(s) the report does not declare: "
            f"{', '.join(unknown)}"
        )

    item_id = raw["id"]
    if not isinstance(item_id, str):
        raise ReportError(
            f"item #{index} has a non-string id: {type(item_id).__name__}"
        )

    verdict = raw["verdict"]
    if not isinstance(verdict, str) or not verdict:
        raise ReportError(
            f"item {item_id!r} has a missing or non-string verdict: "
            f"{verdict!r}"
        )

    evidence_excerpt = raw["evidence_excerpt"]
    if not isinstance(evidence_excerpt, str):
        raise ReportError(
            f"item {item_id!r} has a non-string evidence_excerpt: "
            f"{type(evidence_excerpt).__name__}"
        )

    return {
        "id": item_id,
        "verdict": verdict,
        "evidence_excerpt": redact(evidence_excerpt),
        "audit": _build_audit(raw["audit"], item_id),
    }


def _build_audit(raw: Any, item_id: str) -> Dict[str, Any]:
    """One validated audit triple: what was done, verbatim, and to what."""
    if not isinstance(raw, Mapping):
        raise ReportError(
            f"item {item_id!r} has a non-mapping audit: "
            f"{type(raw).__name__}"
        )

    missing = sorted(AUDIT_KEYS - set(raw))
    if missing:
        raise ReportError(
            f"item {item_id!r} has an audit missing key(s): "
            f"{', '.join(missing)}"
        )

    unknown = sorted(set(raw) - AUDIT_KEYS)
    if unknown:
        raise ReportError(
            f"item {item_id!r} has an audit carrying key(s) the report "
            f"does not declare: {', '.join(unknown)}"
        )

    for key in ("action", "target"):
        if not isinstance(raw[key], str):
            raise ReportError(
                f"item {item_id!r} has a non-string audit {key}: "
                f"{type(raw[key]).__name__}"
            )

    command = raw["command"]
    if isinstance(command, (str, bytes)):
        raise ReportError(
            f"item {item_id!r} has a concatenated audit command "
            f"({command!r}); the audit wants the argv list, so the "
            f"argument boundaries survive"
        )
    if not isinstance(command, (list, tuple)):
        raise ReportError(
            f"item {item_id!r} has a non-list audit command: "
            f"{type(command).__name__}"
        )
    if not all(isinstance(argument, str) for argument in command):
        raise ReportError(
            f"item {item_id!r} has a non-string element in its audit "
            f"command"
        )

    return {
        "action": redact(raw["action"]),
        "command": [redact(argument) for argument in command],
        "target": redact(raw["target"]),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` as JSON, atomically and privately.

    The bytes go to a temporary file **in the same directory** (same
    filesystem, so the rename is atomic), are flushed and fsynced, and
    only then replace the target.  A reader therefore sees the whole
    previous report or the whole new one.

    If anything fails before the rename, the temporary file is removed
    and the exception propagates: the previous report is still the
    report, and the plan directory is left as it was found.

    The mode is set on the temporary file's **open descriptor**, to
    :data:`REPORT_FILE_MODE`, before the first byte is written.  The
    report is an audit artefact, so 0600 is a contract of this write
    and not a default it inherits: the mode does not depend on the
    process umask, nor on a default ACL of the plans directory, nor on
    what ``mkstemp`` happens to create today.  Set on the descriptor,
    it also holds for the instant between the file's creation and the
    rename — the report is never readable by anyone but its owner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            # The mode is the contract, set on the descriptor itself:
            # what mkstemp created is not left to chance.
            os.fchmod(handle_fd, REPORT_FILE_MODE)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise