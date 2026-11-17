"""The post-processing terminal report — shape, closed enum, atomic write.

What this file witnesses
------------------------

The finishing stage (收尾) ends in exactly one state, and that state is
read by three consumers that never ran the stage: the dashboard's
terminal panel, the failure notification, and the resume entry point.
All three read **one** file — ``postprocess_report.json`` — so this
module is the only place that decides what that file contains.

Five contracts are pinned here, one test each:

1. the report carries **exactly** the four keys the architecture
   declares — no more, no fewer (``test_write_report_shape_matches_architecture``);
2. ``terminal_status`` is a **closed** enum: ``completed`` or
   ``postprocess_failed``, nothing else
   (``test_terminal_status_closed_enum``);
3. a plan that never reached the finishing stage has **no** report, and
   reading it says so with ``None`` rather than an exception
   (``test_read_report_returns_none_when_absent``);
4. the audit triple records the command as an **argv list**, never as a
   concatenated string (``test_report_rejects_string_command``);
5. the write is **atomic** — a failure between "the bytes are on disk"
   and "the bytes *are* the report" leaves the previous report intact
   and parseable (``test_report_write_stays_atomic``);
6. the report is written **private** — 0600, on every path that puts a
   report on disk, the first write and the rewrite alike
   (``test_report_file_is_0600``).

Why the fifth one is a file contract and not a call contract
--------------------------------------------------------------

The panel reads the report *while* the finishing stage writes it, and a
kill between the two steps is not exotic: the stage runs outward-facing
commands (git push, gh pr create), and those get interrupted.  A
half-written JSON is not a degraded report — it is an unparseable one,
and the consumer that hits it is the human asking "did it work?".

So the write is "temporary file in the same directory + ``os.replace``":
``os.replace`` is atomic on every filesystem this runs on, and a reader
sees either the whole previous report or the whole new one — never a
half of each.  The test injects the failure exactly where the danger
is: after the temporary file exists, before the rename.

Why the report is written private
----------------------------------
The report is the stage's audit artefact: it says what was done, verbatim,
and who did it.  It lands in the plans tree, which is the operator's home
directory and readable by everything running as that operator — so a mode
that leaves it world-readable hands the audit trail to every other
process on the machine.  0600 is therefore a **contract of the write**,
not a default the writer happens to inherit: the mode is set on the open
descriptor of the temporary file, before the first byte is written, so it
does not depend on the process umask, on a default ACL of the plans
directory, or on what ``mkstemp`` happens to create today.  A resumed run
writes the report a second time — over the one the crashed run left
behind — and that rewrite is held to the same contract, which is why the
test below checks both writes and not only the first.

Why the report's shape is pinned *exactly*
------------------------------------------

The report is read by three consumers written against one contract.
A field that appears without the architecture saying so is a field the
panel does not render and the notification does not quote — it looks
supported and is not.  The four keys are therefore written out member
by member below, from the architecture's data model, so that a key
that sneaks in fails here rather than being silently ignored by every
consumer at once.

The sample shapes come from the shared builders (task 2) rather than
from dicts spelled inline, so a change to the architecture's item shape
breaks this file in one place instead of three.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from postprocess.report import (
    ReportError,
    read_report,
    report_path,
    write_report,
)
from tests.fixtures.postprocess.builders import make_report


#: The report's four keys, spelled out member by member — the
#: architecture's data model for ``postprocess_report.json``.
REPORT_KEYS_EXPECTED = {
    "spec_sha256",
    "terminal_status",
    "items",
    "failure_context_ref",
}

#: One item's four keys, spelled out the same way.
ITEM_KEYS_EXPECTED = {"id", "verdict", "evidence_excerpt", "audit"}

#: The audit triple's three keys, spelled out the same way.
AUDIT_KEYS_EXPECTED = {"action", "command", "target"}


@dataclass(frozen=True)
class FakeFrozenSpec:
    """A stand-in for task 4's ``FrozenSpec``.

    The real frozen spec does not exist yet, and this module must not
    depend on it: the report only ever needs the spec's fingerprint.
    A duck-typed stand-in is therefore the honest sample here — the
    orchestrator will hand over the real ``FrozenSpec``, and the report
    reads the same attribute off it either way.
    """

    spec_sha256: str = "c" * 64


def _plans(tmp_path: Path) -> Path:
    """The plans root this test writes into."""
    return tmp_path / "plans"


def _passing_verdicts():
    """One passing item, shaped by the shared builder."""
    return make_report(
        items=[
            {
                "id": "push",
                "verdict": "PASS",
                "evidence_excerpt": "exit=0",
                "audit": {
                    "action": "push",
                    "command": ["git", "push"],
                    "target": "origin",
                },
            }
        ]
    )["items"]


def _failing_verdicts():
    """One failing item, shaped by the shared builder."""
    return make_report(
        items=[
            {
                "id": "pr",
                "verdict": "FAIL",
                "evidence_excerpt": "exit=1",
                "audit": {
                    "action": "pr",
                    "command": ["gh", "pr", "create"],
                    "target": "upstream",
                },
            }
        ]
    )["items"]


def test_write_report_shape_matches_architecture(tmp_path):
    """The report carries exactly the four keys the architecture declares."""
    plans = _plans(tmp_path)

    report = write_report(
        "p1",
        frozen_spec=FakeFrozenSpec(),
        item_verdicts=_passing_verdicts(),
        plans_dir=plans,
    )

    assert set(report) == REPORT_KEYS_EXPECTED, (
        f"expected exactly {sorted(REPORT_KEYS_EXPECTED)}, got "
        f"{sorted(report)}"
    )
    assert len(report) == 4, f"expected 4 keys, got {len(report)}"

    on_disk = json.loads(report_path("p1", plans).read_text(encoding="utf-8"))
    assert set(on_disk) == REPORT_KEYS_EXPECTED, (
        "the file on disk must carry the same four keys as the returned dict"
    )
    assert on_disk == report, (
        "the returned dict and the file on disk must be the same document"
    )

    assert report["spec_sha256"] == FakeFrozenSpec().spec_sha256
    assert report["terminal_status"] == "completed"
    assert report["failure_context_ref"] is None

    item = report["items"][0]
    assert set(item) == ITEM_KEYS_EXPECTED
    assert set(item["audit"]) == AUDIT_KEYS_EXPECTED
    assert item["audit"]["command"] == ["git", "push"]


def test_terminal_status_closed_enum(tmp_path):
    """A terminal status outside the enum is refused, not written."""
    plans = _plans(tmp_path)

    with pytest.raises(ReportError) as excinfo:
        write_report(
            "p1",
            frozen_spec=FakeFrozenSpec(),
            item_verdicts=_passing_verdicts(),
            terminal_status="passed",  # not in {completed, postprocess_failed}
            plans_dir=plans,
        )

    assert "terminal_status" in str(excinfo.value), (
        "the error must name the field that is wrong"
    )
    assert not report_path("p1", plans).exists(), (
        "a refused report must not leave a file behind"
    )


def test_read_report_returns_none_when_absent(tmp_path):
    """A plan that never finished has no report — and reading it is None."""
    plans = _plans(tmp_path)

    assert read_report("p1", plans_dir=plans) is None


def test_report_rejects_string_command(tmp_path):
    """A concatenated command string is refused — the audit wants an argv."""
    plans = _plans(tmp_path)
    verdicts = make_report(
        items=[
            {
                "id": "push",
                "verdict": "PASS",
                "evidence_excerpt": "exit=0",
                "audit": {
                    "action": "push",
                    "command": "git push",
                    "target": "origin",
                },
            }
        ]
    )["items"]

    with pytest.raises(ReportError) as excinfo:
        write_report(
            "p1",
            frozen_spec=FakeFrozenSpec(),
            item_verdicts=verdicts,
            plans_dir=plans,
        )

    assert "command" in str(excinfo.value), (
        "the error must name the field that is wrong"
    )
    assert not report_path("p1", plans).exists(), (
        "a refused report must not leave a file behind"
    )


def test_report_write_stays_atomic(tmp_path, monkeypatch):
    """A failure before the rename leaves the previous report intact."""
    plans = _plans(tmp_path)
    path = report_path("p1", plans)

    write_report(
        "p1",
        frozen_spec=FakeFrozenSpec(),
        item_verdicts=_passing_verdicts(),
        plans_dir=plans,
    )
    before = path.read_text(encoding="utf-8")

    def exploding_replace(src, dst):
        raise OSError("injected failure between write and rename")

    monkeypatch.setattr(os, "replace", exploding_replace)

    with pytest.raises(OSError):
        write_report(
            "p1",
            frozen_spec=FakeFrozenSpec(),
            item_verdicts=_failing_verdicts(),
            plans_dir=plans,
        )

    after = path.read_text(encoding="utf-8")
    assert after == before, "the previous report must survive a failed write"
    assert json.loads(after)["terminal_status"] == "completed", (
        "the previous report must still be parseable and complete"
    )
    assert [p.name for p in path.parent.iterdir()] == [path.name], (
        "a failed write must not leave a temporary file behind"
    )


def test_read_report_round_trips_the_written_report(tmp_path):
    """What was written is what is read back."""
    plans = _plans(tmp_path)

    written = write_report(
        "p1",
        frozen_spec=FakeFrozenSpec(),
        item_verdicts=_failing_verdicts(),
        failure_context_ref="postprocess_progress.json",
        plans_dir=plans,
    )

    assert read_report("p1", plans_dir=plans) == written


def test_terminal_status_is_derived_from_the_verdicts(tmp_path):
    """A failing item makes the stage failed; a passing one makes it completed."""
    plans = _plans(tmp_path)

    failed = write_report(
        "p1",
        frozen_spec=FakeFrozenSpec(),
        item_verdicts=_failing_verdicts(),
        plans_dir=plans,
    )
    assert failed["terminal_status"] == "postprocess_failed"

    completed = write_report(
        "p2",
        frozen_spec=FakeFrozenSpec(),
        item_verdicts=_passing_verdicts(),
        plans_dir=plans,
    )
    assert completed["terminal_status"] == "completed"


def test_report_rejects_unknown_item_key(tmp_path):
    """A key the architecture does not declare is a loud error, not a field."""
    plans = _plans(tmp_path)
    verdicts = _passing_verdicts()
    verdicts[0]["comment"] = "looks fine to me"

    with pytest.raises(ReportError) as excinfo:
        write_report(
            "p1",
            frozen_spec=FakeFrozenSpec(),
            item_verdicts=verdicts,
            plans_dir=plans,
        )

    assert "comment" in str(excinfo.value), (
        "the error must name the key that is not allowed"
    )


def test_report_requires_a_spec_fingerprint(tmp_path):
    """A frozen spec without a fingerprint cannot produce a report."""
    plans = _plans(tmp_path)

    with pytest.raises(ReportError) as excinfo:
        write_report(
            "p1",
            frozen_spec=object(),
            item_verdicts=_passing_verdicts(),
            plans_dir=plans,
        )

    assert "spec_sha256" in str(excinfo.value)


def test_report_rejects_a_corrupt_file(tmp_path):
    """A corrupt report is an error, never silently read as "no report"."""
    plans = _plans(tmp_path)
    path = report_path("p1", plans)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"spec_sha256": "abc", ', encoding="utf-8")

    with pytest.raises(ReportError):
        read_report("p1", plans_dir=plans)


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_report_file_is_0600(tmp_path, umask):
    """The report is 0600 — under either umask, on the first write and the rewrite."""
    plans = _plans(tmp_path)
    path = report_path("p1", plans)

    previous = os.umask(umask)
    try:
        write_report(
            "p1",
            frozen_spec=FakeFrozenSpec(),
            item_verdicts=_passing_verdicts(),
            plans_dir=plans,
        )
    finally:
        os.umask(previous)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, (
        "the report is an audit artefact and is written private under "
        f"umask {oct(umask)} — the mode is {oct(mode)}"
    )

    # The second write is the one a resumed run makes: it writes the
    # report over the one the crashed run left behind.  A rewrite that
    # widened the report would undo the first write's contract.
    previous = os.umask(umask)
    try:
        write_report(
            "p1",
            frozen_spec=FakeFrozenSpec(),
            item_verdicts=_failing_verdicts(),
            plans_dir=plans,
        )
    finally:
        os.umask(previous)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, (
        "the rewritten report is private too — under umask "
        f"{oct(umask)} the mode is {oct(mode)}"
    )


def test_report_file_is_0600_even_when_the_temporary_file_is_wider(
    tmp_path,
    monkeypatch,
):
    """The 0600 is the writer's contract, not a default it inherits."""
    plans = _plans(tmp_path)
    real_mkstemp = tempfile.mkstemp

    def wide_mkstemp(*args, **kwargs):
        """Stand in for a temporary file that is *not* born private."""
        handle_fd, name = real_mkstemp(*args, **kwargs)
        os.fchmod(handle_fd, 0o644)
        return handle_fd, name

    monkeypatch.setattr(tempfile, "mkstemp", wide_mkstemp)

    write_report(
        "p1",
        frozen_spec=FakeFrozenSpec(),
        item_verdicts=_passing_verdicts(),
        plans_dir=plans,
    )

    mode = stat.S_IMODE(report_path("p1", plans).stat().st_mode)
    assert mode == 0o600, (
        "the writer sets the mode on the descriptor itself, so a temporary "
        f"file that is wider cannot leak into the report — the mode is "
        f"{oct(mode)}"
    )
