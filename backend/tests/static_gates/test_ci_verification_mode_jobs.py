"""The verification-mode CI lanes exist, are serial, and moved nothing else.

Why this gate exists
--------------------

The verification branch is verified by two lanes of its own:
``verification-e2e`` (the repair round) and ``verification-perf`` (the
verification mode's wall-clock budgets). Both were added to ``ci.yml``
in one change, and both are easy to damage in ways no functional test
can see:

* **A lane that stops being serial.** ``needs: unit-tests`` and no
  ``-n`` are what make these lanes measure anything: the shards of
  ``unit-tests`` run in parallel with anything not chained after them,
  so a lane that loses its chain starts while the shards are still
  running, and a lane that gains ``-n 8`` measures the workers rather
  than the code. Neither shows up as a red test — the tests still pass.
* **The unit lane quietly moving.** The same file carries the unit
  shards' ``-n 8`` and the lane paths, and a change to the verification
  lanes has no business touching either. A unit shard that silently
  became serial would cost the whole suite its parallelism, and the
  merge gate would report it as green.
* **The repair round losing its evidence.** The lane uploads the plan
  directory and the git state when it fails. That upload is the only
  thing that can answer "what did the round leave behind, and did it
  leave the repository alone?" — and a lane that stopped uploading
  would fail in exactly the situation where the answer is needed.

What each gate checks
---------------------

``test_ci_has_verification_e2e_job``
    The ``verification-e2e`` job exists, is chained after the unit
    shards, and runs its pytest serially.

``test_ci_has_verification_perf_job``
    The ``verification-perf`` job exists, is chained after the unit
    shards, runs its pytest serially, and pins the measurement to four
    CPUs.

``test_unit_lane_unchanged``
    The unit lane still collects ``tests/unit`` and still runs with
    ``-n 8``.

``test_e2e_uploads_failure_artifacts``
    The repair-round lane still uploads the plan directory and the git
    state when it fails.

Scope, stated honestly
----------------------

This reads the workflow. It cannot execute it, so a green run here
means "the workflow still says what it is supposed to say" — not "the
lanes passed on a runner". The two are different claims, and only the
first one is a static gate's to make.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

#: Repository root. This file lives at ``backend/tests/static_gates/``,
#: so the root is three parents up.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The workflow under test.
WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The two verification-mode lanes.
E2E_JOB = "verification-e2e"
PERF_JOB = "verification-perf"

#: The lane that owns the unit shards.
UNIT_JOB = "unit-tests"

#: The shard parallelism the unit lane runs with. A change to it costs
#: the whole suite its parallelism, and the merge gate reports that as
#: green.
UNIT_WORKERS = "8"

#: The directory the unit lane collects. Everything else in the tree is
#: somebody else's lane.
UNIT_LANE_PATHS = "tests/unit"

#: xdist's worker flag. ``-n 8`` and ``-n=8`` are the same request, and
#: the second spelling is what a careful editor produces.
_XDIST_WORKERS_RE = re.compile(r"(?<!\S)-n[= ]\s*\d+")

#: xdist's other entry points. ``--dist`` selects the scheduling mode
#: and ``-p xdist`` loads the plugin; either one parallelises a run
#: that this gate is here to keep serial.
_XDIST_OTHER_RE = re.compile(r"(?<!\S)(--dist[= ]|--dist\b|-p\s+xdist)")


def _workflow() -> Dict[str, Any]:
    """The parsed workflow."""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job(name: str) -> Dict[str, Any]:
    """The job called *name*, asserted present.

    A missing job is a named failure rather than a ``None`` that the
    next assertion turns into a confusing ``TypeError``: the reader
    wants to know which lane is missing, not which line blew up.
    """
    workflow = _workflow()
    jobs = workflow.get("jobs") or {}
    job = jobs.get(name)
    assert job is not None, (
        f"ci.yml has no job named {name!r}. The verification-mode lanes are "
        f"part of what this repository's CI is: a lane that was renamed or "
        f"removed leaves the repair round and the verification perf budgets "
        f"unrun, and nothing else in the tree would say so."
    )
    return job


def _run_blocks(job: Dict[str, Any]) -> List[str]:
    """The ``run:`` bodies of *job*."""
    return [
        step["run"]
        for step in job.get("steps", []) or []
        if isinstance(step.get("run"), str)
    ]


def _shell_code(job: Dict[str, Any]) -> str:
    """The job's shell code with its comment lines dropped.

    A ``run:`` block is mostly prose, and prose mentions ``-n 8`` and
    ``taskset`` in explanations. Only the commands are the job.
    """
    lines: List[str] = []
    for block in _run_blocks(job):
        for line in block.split("\n"):
            if line.lstrip().startswith("#"):
                continue
            lines.append(line)
    return "\n".join(lines)


def _assert_serial(job_name: str, job: Dict[str, Any]) -> None:
    """Assert *job* runs its pytest serially, and is chained to be so.

    Two halves, and both are load-bearing:

    * **No xdist.** ``-n 8`` in a lane whose budgets are wall-clock
      numbers turns the measurement into a measurement of the workers.
    * **Chained after the shards.** A lane with no ``needs`` starts
      while the unit shards are still running, so the machine it lands
      on is shared — which is the same defect by another route, and the
      one a "just add ``-n 8`` to be quick" edit brings back.
    """
    code = _shell_code(job)
    assert not _XDIST_WORKERS_RE.search(code), (
        f"the {job_name!r} job runs pytest with xdist workers "
        f"({_XDIST_WORKERS_RE.search(code).group(0)!r}). These lanes exist "
        f"because their numbers and their reproductions mean something only "
        f"on a machine that is not being shared; a wall-clock budget measured "
        f"beside other workers is a measurement of the workers."
    )
    assert not _XDIST_OTHER_RE.search(code), (
        f"the {job_name!r} job parallelises its pytest run "
        f"({_XDIST_OTHER_RE.search(code).group(0)!r}). Serial means serial: "
        f"one process, no scheduler, no other worker's noise in the number."
    )
    needs = job.get("needs")
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert UNIT_JOB in needs_list, (
        f"the {job_name!r} job needs {needs_list!r} and not {UNIT_JOB!r}. The "
        f"unit shards run in parallel with anything that is not chained after "
        f"them, so an unchained lane starts on a machine that is still busy — "
        f"the same defect as running it beside other workers, reached by a "
        f"different road."
    )


def test_ci_has_verification_e2e_job() -> None:
    """``verification-e2e`` exists and runs the repair round serially."""
    job = _job(E2E_JOB)

    assert job.get("runs-on"), (
        f"the {E2E_JOB!r} job has no runner. A job without one is a job "
        f"GitHub never schedules."
    )
    _assert_serial(E2E_JOB, job)

    # The repair round has to be able to commit: it is a repair, and a
    # repair that cannot land is a repair that was measured, not made.
    code = _shell_code(job)
    assert "user.email" in code, (
        f"the {E2E_JOB!r} job does not configure a git identity. The repair "
        f"round commits on the worktree branch, and a hosted runner has no "
        f"committer identity by default — the commit fails and the round "
        f"never reaches its second turn."
    )


def test_ci_has_verification_perf_job() -> None:
    """``verification-perf`` exists, runs serially, and pins four CPUs."""
    job = _job(PERF_JOB)

    assert job.get("runs-on"), (
        f"the {PERF_JOB!r} job has no runner. A job without one is a job "
        f"GitHub never schedules."
    )
    _assert_serial(PERF_JOB, job)

    # 4-core fidelity: the budgets are stated for a four-CPU machine, so
    # the measurement is pinned to four CPUs. A runner with more would
    # make every run look faster than the reference, and the number
    # would not be comparable with the last one.
    code = _shell_code(job)
    assert "taskset -c 0-3" in code, (
        f"the {PERF_JOB!r} job does not pin the measurement to four CPUs. The "
        f"verification mode's perf budgets are stated for a four-CPU "
        f"machine; a number read off a machine with more cores is not "
        f"comparable with the last one, and the whole point of the lane is "
        f"that the number can be compared."
    )


def test_unit_lane_unchanged() -> None:
    """The unit lane still collects ``tests/unit`` and still runs ``-n 8``."""
    job = _job(UNIT_JOB)
    code = _shell_code(job)

    assert f'unit) LANE_PATHS="{UNIT_LANE_PATHS}"' in code, (
        f"the unit lane no longer collects {UNIT_LANE_PATHS!r}. The lane "
        f"paths are a partition: a directory that stops being collected is "
        f"not slow, it is unrun, and the merge gate reports the shard as "
        f"green."
    )
    assert re.search(rf"(?<!\S)-n[= ]\s*{UNIT_WORKERS}(?!\S)", code), (
        f"the unit lane no longer runs pytest with -n {UNIT_WORKERS}. The "
        f"shards are sized for that parallelism; a lane that lost it does "
        f"not get slower per test, it gets slower per shard, and the whole "
        f"suite stops fitting in the window the merge gate allows."
    )


def test_e2e_uploads_failure_artifacts() -> None:
    """The repair round still uploads the plan directory and the git state."""
    job = _job(E2E_JOB)

    uploads = [
        step
        for step in job.get("steps", []) or []
        if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert uploads, (
        f"the {E2E_JOB!r} job has no artifact upload step. The repair "
        f"round's failure is answered by evidence — the plan directory it "
        f"wrote and the git state it left behind — and a lane that fails "
        f"without uploading either has taken the only copy of that answer "
        f"with it."
    )

    uploads_on_failure = [
        step
        for step in uploads
        if "failure()" in str(step.get("if", ""))
        or "always()" in str(step.get("if", ""))
    ]
    assert uploads_on_failure, (
        f"the {E2E_JOB!r} job's artifact upload does not run on failure. An "
        f"upload that only happens on a green run has nothing to upload."
    )

    upload = uploads_on_failure[0]
    with_ = upload.get("with") or {}
    path = str(with_.get("path", ""))

    # The plan directory: the job redirects it into the workspace, and
    # the upload has to cover exactly that directory — a path that names
    # a different one uploads the absence of the plans and reads as "the
    # round produced nothing".
    plans_dir = str((job.get("env") or {}).get("PDT_PLANS_DIR", ""))
    assert plans_dir, (
        f"the {E2E_JOB!r} job does not redirect PDT_PLANS_DIR. The hermetic "
        f"test environment puts the plan directory in a per-session scratch "
        f"path that only the pytest process knows, and the upload step "
        f"could not name it."
    )
    relative = plans_dir.replace("${{ github.workspace }}/", "")
    entries = [entry for entry in path.replace("\n", " ").split() if entry]
    covered = any(
        relative == entry.rstrip("/")
        or relative.startswith(entry.rstrip("/") + "/")
        for entry in entries
    )
    assert covered, (
        f"the {E2E_JOB!r} job uploads {path!r}, which does not cover the "
        f"plan directory it configured ({relative!r}). The upload would "
        f"carry the logs and not the plans — the half of the evidence that "
        f"answers what the round did."
    )

    # The git state: the claim the round makes is that it left the
    # repository alone, and the file that shows it has to be in the
    # artifact.
    assert "verification-git-state.txt" in path, (
        f"the {E2E_JOB!r} job uploads {path!r}, which does not include the "
        f"git state. The repair round's central claim is that the "
        f"repository it verified came out unchanged, and the git state is "
        f"the only thing in the artifact that can show it."
    )
    assert any(
        "verification-git-state.txt" in block
        for block in _run_blocks(job)
    ), (
        f"the {E2E_JOB!r} job uploads verification-git-state.txt but no step "
        f"writes it. An artifact entry for a file that is never written is "
        f"an empty directory in the download, and an empty directory reads "
        f"as 'nothing happened'."
    )
