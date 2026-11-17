"""The records the finishing stage produces, and the judge reads.

What this module is
-------------------
Three dataclasses, and nothing else:

* :class:`EvidenceRecord` — what one command produced: its exit code, its
  two output streams, the product-code diff observed around it and, for an
  item that waits on something outside, the external status that was seen;
* :class:`AuditRecord` — the audit triple of one executed action: what was
  done, the argv verbatim, and the target it acted on;
* :class:`Verdict` — the judge's decision on one item: whether it passed,
  the excerpt of the evidence that decided it, and the reason.

Why these shapes and not other ones
------------------------------------
They are the boundary between the executor and the judge, so they are
deliberately *narrow*.  There is no free-form comment field, no "the
agent believes it worked" field, no summary field: a record carries what a
command printed and what a query observed, and nothing that a runner could
fill in to argue for its own success.  That is the whole point of the
split — the judge reads evidence, and evidence is not an opinion.

The same narrowness shows in :class:`Verdict`: the excerpt is a slice of
the record's own output, so a reader of the report can compare the report
against the command that produced it without trusting anybody's summary.

Why the records are plain dataclasses
-------------------------------------
They are data, they are compared in tests, and they are written into the
report and the progress file.  A dataclass gives equality, a readable
repr and a ``to_dict`` for serialization without any of that behaviour
being something a future change has to remember to keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvidenceRecord:
    """What one command produced — everything the judge is allowed to read.

    Args:
        item_id: The requirement item this evidence belongs to.
        command: The argv that was run, verbatim and unjoined.
        exit_code: The command's exit code, or ``None`` when it never
            reported one — which is itself a fact the judge acts on.
        stdout: The command's standard output.
        stderr: The command's standard error.
        git_diff: The product-code diff observed around the command — the
            **raw** evidence of it (``git status --porcelain`` and
            ``git diff --stat``, verbatim), and an empty string when the
            command touched nothing.  This is the field a
            ``git_diff_empty`` criterion is decided from, so it carries
            evidence rather than a summary: an item with no diff in it
            has not been shown to be harmless, it has merely not been
            shown to be harmful.
        external_status: The status observed for an item that waits on
            something outside this repository, when there is one.
    """

    item_id: str
    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    git_diff: str = ""
    external_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """This record as a plain dict, for the report and the progress file."""
        return {
            "item_id": self.item_id,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "git_diff": self.git_diff,
            "external_status": self.external_status,
        }


@dataclass
class AuditRecord:
    """The audit triple of one executed action.

    Args:
        action: What was done, in words.
        command: The argv that was run, verbatim and unjoined — never a
            shell string, so nothing in it can be re-read as a shell.
        target: What the action acted on (a repository, a branch, a
            release) — enough to point back at the item.
    """

    action: str
    command: List[str] = field(default_factory=list)
    target: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """This record as a plain dict, for the report."""
        return {
            "action": self.action,
            "command": list(self.command),
            "target": self.target,
        }


@dataclass
class Verdict:
    """The judge's decision on one item.

    Args:
        item_id: The requirement item this verdict is about.
        passed: Whether the item passed.
        evidence_excerpt: A verbatim slice of the judged record's own
            stdout/stderr — the evidence a reader can check.  Empty when
            there was no record to quote.
        reason: The judge's sentence: why it decided what it decided.
    """

    item_id: str
    passed: bool
    evidence_excerpt: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """This verdict as a plain dict, for the report."""
        return {
            "item_id": self.item_id,
            "passed": self.passed,
            "evidence_excerpt": self.evidence_excerpt,
            "reason": self.reason,
        }