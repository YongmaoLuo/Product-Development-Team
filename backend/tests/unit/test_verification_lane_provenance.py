"""A verdict that says "the command exited 0" has not said **which** lane it ran.

Why this file exists
--------------------
2026-11-18 (VP-022): the e2e lane's command really did exit 0 — the
runner ran the entry the plan declared, in the tree the plan declared,
and the gate's exit code was 0.  The framework's subject gate still
downgraded the verdict to FAILED, and the reason it gave was this::

    本验证点点名 scripts/run_tests.sh，而结论只涉及 backend/tests/...

The point had asked: *did the e2e + integration lane run through — the
lane ``scripts/run_tests.sh``'s phase 2 selects?*  The verdict had
answered: *this command exited 0.*  Those are not the same question,
and the framework was right to notice — the verdict had said nothing
about **the lane**, and the only thing in the tree that says which lane
a command selects is the script that declares the selection.

So the verdict says it now: the runner reads the marker selection out
of the entry command, finds the script in this tree that declares the
**same** selection, and names that script — path and line — in the
verdict.

Two rules keep that honest:

* a selection **nobody** declares gets **no** clause at all.  A verdict
  that named a script nobody declared would be a keyword, not
  evidence — the negative control below pins that;
* the clause names the script **with the line** the declaration is on,
  so a reader can open the file and read the line the verdict is
  talking about.  The line is part of the claim: ``scripts/run_tests.sh:519``
  says something a verdict that said only ``scripts/run_tests.sh`` does
  not.

"Declares" means **declares this very selection**: a script that
declares a *different* lane is not a declarer, and neither is one that
declares a selection that merely looks like this one.  And an entry
that names its selection without quotes names no selection at all, so
it selects no lane and gets no clause.  Both rules are pinned below
with their own negative controls.

The clause travels on **two** channels, and both are pinned here:

* the **reasons** — what the verdict says;
* the **evidence** (``lane_summaries``) — what the artifact carries.
  The artifact is how the *next* round reads this verdict back: it is
  written to disk, and the round after this one rebuilds the verdict
  from it.  A clause that reached only the reasons would be gone by
  then, and the round after this one would see a verdict that names no
  lane at all — the very thing this change exists to fix.

These cases pin the behaviour.  None of them runs VP-022's own
entry command — the gate is stubbed out, and the tree is a temporary
one.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402

#: The marker selection the entry command selects.
SELECTION = "(e2e or integration) and not time_sensitive"

#: The entry a command-kind e2e point declares.
ENTRY = 'python3 -m pytest backend/tests -m "%s" --timeout=600 -q' % SELECTION

#: A script that declares the very same selection, the way this
#: repository's own gate script declares it.  The declaration is on
#: line 3.
DECLARING_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "# the phase 2 lane\n"
    'PHASE2_ARGS=(-m "%s" --timeout=600)\n'
) % SELECTION

#: A script that declares nothing at all.
SILENT_SCRIPT = "#!/usr/bin/env bash\nexit 0\n"

#: A script that declares a selection that only **looks** like the one
#: the entry runs: the very same expression with one more clause.  It
#: shares a prefix with the selection and it is a **different** lane.
NEAR_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "# nearly, but not the same, lane\n"
    'NEAR_ARGS=(-m "%s and not perf" --timeout=600)\n'
) % SELECTION

#: A script that declares a lane of its own.
OTHER_LANE_SCRIPT = (
    "#!/usr/bin/env bash\n"
    'OTHER_ARGS=(-m "pytest" --timeout=60)\n'
)

#: An entry that names its selection **without quotes** — so nobody can
#: read which tests it chose, and it is not a selection at all.
UNQUOTED_ENTRY = (
    "python3 -m pytest backend/tests -m %s --timeout=600 -q" % SELECTION
)


def _project(tmp_path: Path, scripts: Optional[Dict[str, str]] = None) -> Path:
    """A project tree: the entry's own paths exist, plus the scripts."""
    project = tmp_path / "project"
    (project / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    for name, body in (scripts or {}).items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return project


def _vp(entry: str = ENTRY) -> Dict:
    """A command-kind e2e point that declares ``entry``."""
    return {
        "id": "VP-022",
        "verification_method": "e2e",
        "e2e_kind": "command",
        "entry_command": entry,
    }


def _green_gate(monkeypatch) -> None:
    """Stub the gate: it ran, and it exited 0."""

    def _fake_run_bounded(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(ci_runner, "run_bounded", _fake_run_bounded)


def test_the_verdict_names_the_script_that_declares_the_selection(
    tmp_path, monkeypatch,
):
    """The clause names the declarer by its repo-relative path."""
    project = _project(tmp_path, {"scripts/run_tests.sh": DECLARING_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    assert "scripts/run_tests.sh" in verdict, verdict


def test_the_clause_carries_the_line_that_declares_the_selection(
    tmp_path, monkeypatch,
):
    """The line number is part of the claim: it must be the right line."""
    project = _project(tmp_path, {"scripts/run_tests.sh": DECLARING_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    verdict = "\n".join(result.reasons)
    match = re.search(r"(scripts/run_tests\.sh):(\d+)", verdict)
    assert match, verdict
    number = int(match.group(2))
    lines = (project / "scripts" / "run_tests.sh").read_text(
        encoding="utf-8",
    ).splitlines()
    assert SELECTION in lines[number - 1], lines[number - 1]


def test_every_script_that_declares_the_selection_is_named(
    tmp_path, monkeypatch,
):
    """Two scripts declare it — the verdict names **both** of them.

    Naming one of two declarers is still not an answer: the reader
    cannot tell whether the framework meant the one it was told about
    or the one it left out.  Every declaration is recorded, so the
    clause names all of them.
    """
    project = _project(
        tmp_path,
        {
            "scripts/run_tests.sh": DECLARING_SCRIPT,
            "scripts/ci_phase2.sh": DECLARING_SCRIPT,
        },
    )
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    verdict = "\n".join(result.reasons)
    assert "scripts/run_tests.sh:3" in verdict, verdict
    assert "scripts/ci_phase2.sh:3" in verdict, verdict


def test_a_script_that_declares_another_selection_is_not_named(
    tmp_path, monkeypatch,
):
    """A script that declares a **different** lane is not a declarer.

    The clause claims "this selection is declared here" — so a script
    that declares some other selection must not be dragged into that
    claim.  A verdict that names a script which declares nothing of
    the kind is a keyword, not evidence.

    And the negative control is not satisfied by silence: the run
    itself is still reported.  A verdict that says nothing at all
    would satisfy "no lane is named" too — and a verdict that says
    nothing at all is not a pass, it is a machine that stopped
    talking.  So the exit code and the duration are pinned here as
    well: the clause is missing, the verdict is not.

    And the **evidence** stays silent too, on both channels: a lane
    that is not declared leaves no declarer in the artifact either.
    """
    project = _project(
        tmp_path,
        {"scripts/unit_lane.sh": 'UNIT_ARGS=(-m "unit" --timeout=60)\n'},
    )
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    assert "unit_lane.sh" not in verdict, verdict
    assert not re.search(r"\.sh:\d+", verdict), verdict
    assert "声明" not in verdict, verdict
    assert "退出码 0" in verdict, verdict
    assert "用时" in verdict, verdict

    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries
    assert not any(re.search(r"\.sh:\d+", line) for line in summaries), summaries
    assert not any("声明" in line for line in summaries), summaries


def test_the_clause_names_the_path_relative_to_the_project_root(
    tmp_path, monkeypatch,
):
    """The path is the repo-relative one — the reader opens it there.

    An absolute path would say which machine ran the lane, not which
    file in the repository declares the selection, and it would not
    survive the move to another checkout.
    """
    project = _project(tmp_path, {"scripts/run_tests.sh": DECLARING_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    verdict = "\n".join(result.reasons)
    match = re.search(r"由\s+(\S+?):(\d+)\s+声明", verdict)
    assert match, verdict
    named = match.group(1)
    assert named == "scripts/run_tests.sh", named
    assert not named.startswith("/"), named
    assert str(project) not in verdict, verdict


def test_the_clause_reaches_the_evidence_the_next_round_reads_back(
    tmp_path, monkeypatch,
):
    """The clause is in the artifact too, not only in the reasons.

    The artifact is the **only** thing the next round has: it reads
    the verdict back from disk, and rebuilds it from what the artifact
    carries.  A clause that stopped at the reasons would be gone by
    then, and the round after this one would see a verdict that names
    no lane at all.
    """
    project = _project(tmp_path, {"scripts/run_tests.sh": DECLARING_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries
    assert any(
        "scripts/run_tests.sh:3" in line for line in summaries
    ), summaries


def test_a_selection_no_script_declares_gets_no_clause(tmp_path, monkeypatch):
    """Nobody declares it — so the verdict says nothing about it.

    The exit code and the duration are still there: the verdict is
    about the run, and the run is what the verdict is for.

    And the **evidence** stays silent as well: a lane nobody declares
    has no declarer, so the artifact carries no declarer either.
    """
    project = _project(tmp_path, {"scripts/other.sh": SILENT_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    assert not re.search(r"\.sh:\d+", verdict), verdict
    assert "声明" not in verdict, verdict
    assert "退出码 0" in verdict, verdict
    assert "用时" in verdict, verdict

    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries
    assert not any(re.search(r"\.sh:\d+", line) for line in summaries), summaries
    assert not any("声明" in line for line in summaries), summaries


def test_the_clause_is_not_written_for_a_gate_entry(tmp_path, monkeypatch):
    """An entry that selects no lane gets no clause.

    ``bash scripts/ci_gate.sh`` names no marker selection, so there is
    no lane to name a declarer for — and the script it happens to run
    is not a declaration of anything.

    The run is still reported: the exit code and the duration are in
    the verdict.  Without them this control would also be satisfied by
    a verdict that says nothing at all, and "nothing was said" is not
    a pass.

    And the **evidence** stays silent too: an entry that selects no
    lane leaves no declarer in the artifact either — the script it
    happens to run is not a declaration of anything.
    """
    project = _project(tmp_path, {"scripts/ci_gate.sh": SILENT_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(
        _vp("bash scripts/ci_gate.sh"), project_dir=project,
    )

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    assert not re.search(r"\.sh:\d+", verdict), verdict
    assert "声明" not in verdict, verdict
    assert "退出码 0" in verdict, verdict
    assert "用时" in verdict, verdict

    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries
    assert not any(re.search(r"\.sh:\d+", line) for line in summaries), summaries
    assert not any("声明" in line for line in summaries), summaries


def test_a_near_miss_spelling_of_the_selection_is_not_named(
    tmp_path, monkeypatch,
):
    """A selection that only *looks* like the declared one is not a declarer.

    ``scripts/near.sh`` spells the lane with one more clause.  It
    shares a prefix with the selection the entry runs, and it is a
    **different** lane: naming it would tell the reader that this
    script declares the lane the command ran, and a reader who opens
    the file would find a selection that is not the one.

    So a near miss is not a declaration — neither in the decision (no
    script is found) nor in the verdict (no script is named) — and the
    run is still reported, so that "no lane is named" is a claim about
    the lane and not about a machine that said nothing.
    """
    project = _project(tmp_path, {"scripts/near.sh": NEAR_SCRIPT})
    _green_gate(monkeypatch)

    assert ci_runner.lane_declarations(project, SELECTION) == []

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    assert "near.sh" not in verdict, verdict
    assert not re.search(r"\.sh:\d+", verdict), verdict
    assert "声明" not in verdict, verdict
    assert "退出码 0" in verdict, verdict
    assert "用时" in verdict, verdict

    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries
    assert not any(re.search(r"\.sh:\d+", line) for line in summaries), summaries
    assert not any("声明" in line for line in summaries), summaries


def test_an_unquoted_marker_selection_produces_no_provenance(
    tmp_path, monkeypatch,
):
    """``-m`` without quotes is not a selection — so no lane is named.

    The quotes are what tells pytest's ``-m`` from the interpreter's
    own ``-m module``: ``python3 -m pytest … -m (e2e or integration)``
    uses both, and only the quoted one says which tests the command
    chose.  An entry that names no readable selection selects no lane
    at all, and a verdict that names a declarer for it names one
    nobody declared.

    The tree still holds scripts that declare lanes — including the
    very selection the entry *meant* to run, and a lane of its own.
    Neither is dragged into the verdict: the entry did not select
    them, and a declarer that was not selected is not evidence.
    """
    project = _project(
        tmp_path,
        {
            "scripts/run_tests.sh": DECLARING_SCRIPT,
            "scripts/other_lane.sh": OTHER_LANE_SCRIPT,
        },
    )
    _green_gate(monkeypatch)

    assert ci_runner.marker_selection(UNQUOTED_ENTRY) is None

    result = ci_runner.run_ci_verification(
        _vp(UNQUOTED_ENTRY), project_dir=project,
    )

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    assert not re.search(r"\.sh:\d+", verdict), verdict
    assert "声明" not in verdict, verdict
    assert "退出码 0" in verdict, verdict
    assert "用时" in verdict, verdict

    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries
    assert not any(re.search(r"\.sh:\d+", line) for line in summaries), summaries
    assert not any("声明" in line for line in summaries), summaries


def test_the_two_channels_name_the_same_declaration(tmp_path, monkeypatch):
    """The verdict and the artifact must make the *same* claim.

    The clause travels on two channels: the **reasons** (what the
    verdict says) and the **evidence** (what the artifact carries,
    and what the next round reads back).  Pinning that each channel
    *has* a clause is not enough — a reader who compares the two must
    find the same declaration on both.  One channel naming the
    declarer and the other naming a different one is a verdict and an
    artifact that disagree about what ran, and the next round reads
    the artifact.

    So this pins the two channels against **each other**: the
    declarations they name are the same ones, in the same order.
    """
    project = _project(tmp_path, {"scripts/run_tests.sh": DECLARING_SCRIPT})
    _green_gate(monkeypatch)

    result = ci_runner.run_ci_verification(_vp(), project_dir=project)

    assert result.status == ci_runner.STATUS_PASSED, result.reasons
    verdict = "\n".join(result.reasons)
    summaries = result.evidence.get("lane_summaries")
    assert isinstance(summaries, list), summaries

    in_verdict = re.findall(r"(\S+\.sh):(\d+)", verdict)
    in_evidence = [
        found
        for line in summaries
        for found in re.findall(r"(\S+\.sh):(\d+)", line)
    ]
    assert in_verdict, verdict
    assert in_evidence, summaries
    assert in_verdict == in_evidence, (in_verdict, in_evidence)
