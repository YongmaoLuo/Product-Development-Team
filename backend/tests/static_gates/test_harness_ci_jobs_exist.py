"""The harness CI layers are declared — and declared the way the contract says.

Why this gate exists
--------------------
The harness refactor introduced a second backend behind one registry.
A backend that can be selected is not the same thing as a backend that
is *tested*, and the difference is entirely in the CI file: a suite that
lives in the repository but is not collected by any lane is a suite
nobody runs. The layers below are where the harness suites actually run,
and this file is what stops a layer from quietly disappearing.

The layering is explicit because the harness suites cannot all run
everywhere. The static gates, the unit suite and the contract suite are
hermetic and run on every commit. The integration suite needs a macOS
host. The real end-to-end suite needs a macOS host *and* a real
account, so it lives on a self-hosted runner and on the nightly /
release schedule rather than in the per-commit path. The perf suite
needs a macOS host for the same reason the rest of it does: two of its
cases are macOS-specific and its ceilings were taken there. A suite
that needs a capability the runner does not have must be in its own
layer; a suite that is folded into a layer it does not fit is a suite
whose failures are indistinguishable from an infrastructure outage.

What this pins
--------------
1. **All six jobs exist.** The names are the contract: a layer that is
   renamed without being updated here is a layer that stops being
   checked, and the failure is silent — the lane keeps running, it just
   stops being anybody's responsibility.
2. **The L3 jobs never carry ``continue-on-error``.** L3 is the release
   and nightly layer; a failure there that is allowed to be tolerated
   is a failure that is reported and then ignored, which is the same
   thing wearing a green badge. The rule is checked at both levels —
   the job and the step — because either one is enough to swallow it.
3. **The real end-to-end job runs on macOS.** A seatbelt profile and a
   keychain are macOS facilities; a Linux runner proves nothing about
   either, so a job that claims to cover them and runs on Linux is
   green by construction.
4. **The perf job runs on macOS as well.** Two of the perf cases are
   macOS-specific and the ceilings they check were taken on macOS; a
   Linux lane would skip the cases and publish a budget it never
   measured.

The layering budgets
--------------------
Each layer declares a ceiling, and the ceiling is part of the contract:
L1 has to fit inside the per-commit budget, L2 inside the pre-merge
budget, L3 inside the release budget. A job that declares no ceiling
runs until the runner gives up, and the evidence of that is a cancelled
job with no log.

Scope, stated honestly: this reads the workflow file. It cannot prove a
runner was allocated, that a self-hosted machine with a real account
existed, or that the suites it names were the ones that ran. A green
run here means the layers are wired as declared, not that they passed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: L1 — every commit. Hermetic: no platform, no account, no network.
L1_JOBS = ("harness-static-gates", "harness-unit", "harness-contract")

#: L2 — before the merge. Needs a macOS host.
L2_JOBS = ("harness-integration",)

#: L3 — release and nightly. Needs a macOS host; the real end-to-end
#: layer also needs an account on it.
L3_JOBS = ("harness-e2e-real", "harness-perf")

#: Every job this gate owns.
ALL_HARNESS_JOBS = L1_JOBS + L2_JOBS + L3_JOBS

#: The ceiling each layer's jobs may declare, in minutes. A job that
#: declares more than this has moved itself into a longer layer, which
#: is a decision nobody made on purpose.
LAYER_BUDGET_MINUTES: dict[str, int] = {
    **{name: 10 for name in L1_JOBS},
    **{name: 20 for name in L2_JOBS},
    **{name: 60 for name in L3_JOBS},
}

#: Where the L3 layers archive their evidence. The run id is part of
#: the path so two runs of the same layer never overwrite each other's
#: evidence.
L3_ARCHIVE_TEMPLATE = "artifacts/harness-runs/${{ github.run_id }}"

#: The runner label fragment that means "a macOS host". Matched
#: case-insensitively against every label of the job's ``runs-on``,
#: because the self-hosted form is a list and the hosted form is a
#: string.
_MACOS_LABEL = "macos"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    workflow = _workflow()
    assert isinstance(workflow, dict) and "jobs" in workflow, (
        f"{_WORKFLOW} has no top-level `jobs:` block; the harness layers "
        f"have nothing to live in."
    )
    return workflow["jobs"]


def _job(name: str) -> dict:
    jobs = _jobs()
    assert name in jobs, (
        f"ci.yml has no job named {name!r}. The harness layers are the "
        f"only place the harness suites run — a layer that is renamed "
        f"without being renamed here is a layer nobody checks."
    )
    return jobs[name]


def _run_blocks(job: dict) -> list[str]:
    return [
        step["run"]
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]


def _runner_labels(job: dict) -> list[str]:
    """The runner labels of *job*, as a list.

    ``runs-on`` is either a string (a hosted image) or a list (the
    self-hosted form, where every label must match). Both shapes are
    normalised here so the caller does not have to branch on it.
    """
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, (list, tuple)):
        return [str(label) for label in runs_on]
    return []


# ---------------------------------------------------------------------------
# The layers exist
# ---------------------------------------------------------------------------


def test_ci_has_all_harness_jobs() -> None:
    """Every harness layer is declared in ci.yml.

    The failure this catches is a suite that was written and never
    wired into CI. Such a suite is green forever — it is not run, and
    nothing about it is red — and the only evidence it exists is the
    directory it sits in.
    """
    names = set(_jobs())
    missing = [name for name in ALL_HARNESS_JOBS if name not in names]
    assert not missing, (
        f"ci.yml is missing the harness layer(s) {missing}. The harness "
        f"suites run nowhere else: a suite that no lane collects is a "
        f"suite nobody runs, and it is green for exactly that reason."
    )


# ---------------------------------------------------------------------------
# L3 cannot be tolerated away
# ---------------------------------------------------------------------------


def test_l3_not_continue_on_error() -> None:
    """An L3 job may not declare ``continue-on-error`` — at either level.

    L3 is the release and nightly layer. A failure there is either a
    real defect or a broken runner, and the point of the layer is that
    somebody reads the answer. ``continue-on-error`` on the job makes
    the badge green while the run is red; ``continue-on-error`` on a
    step does the same for the step that failed and lets the rest of
    the job carry on. Both are checked, because either one alone is
    enough to swallow the failure.
    """
    for name in L3_JOBS:
        job = _job(name)
        assert job.get("continue-on-error") is not True, (
            f"the {name!r} job declares `continue-on-error: true`. L3 is "
            f"the release layer: a failure there that is allowed to be "
            f"tolerated is a failure that gets ignored, which is the "
            f"same thing wearing a green badge."
        )
        for step in job.get("steps", []):
            assert step.get("continue-on-error") is not True, (
                f"a step of the {name!r} job declares `continue-on-error: "
                f"true` ({step.get('name')!r}). The job-level flag is not "
                f"the only way to swallow an L3 failure — a step that is "
                f"allowed to fail lets the rest of the job continue past "
                f"it."
            )


# ---------------------------------------------------------------------------
# The real end-to-end layer needs a macOS host
# ---------------------------------------------------------------------------


def test_e2e_job_is_macos() -> None:
    """``harness-e2e-real`` must be scheduled onto a macOS runner.

    A seatbelt profile, a keychain and ``sandbox-exec`` are macOS
    facilities. On a Linux runner the cases that need them *skip*, so
    the job is green without having checked the thing it exists to
    check — and a skip reads exactly like a pass in a run summary.
    """
    job = _job("harness-e2e-real")
    labels = _runner_labels(job)
    assert labels, (
        "the harness-e2e-real job declares no `runs-on`; GitHub would "
        "refuse to parse the workflow, so this fails here first, where "
        "the message names the mistake."
    )
    assert any(_MACOS_LABEL in label.lower() for label in labels), (
        f"the harness-e2e-real job runs on {labels!r}, which names no "
        f"macOS runner. The real end-to-end layer needs a macOS host — "
        f"the seatbelt and keychain cases skip everywhere else, so the "
        f"layer would be green without having run them."
    )


# ---------------------------------------------------------------------------
# The layering itself
# ---------------------------------------------------------------------------


def test_the_perf_layer_runs_on_macos() -> None:
    """``harness-perf`` must be scheduled onto a macOS runner.

    Two of the perf cases are macOS-specific, and one of them — the
    seatbelt wrapper contract — is the one the harness layer exists to
    keep. On a Linux runner it skips, so the lane would report a green
    budget it never measured. The ceilings are stated for macOS too:
    they were taken there, and a ceiling is a statement about a platform,
    not a free-standing number.
    """
    job = _job("harness-perf")
    labels = _runner_labels(job)
    assert labels, (
        "the harness-perf job declares no `runs-on`; GitHub would refuse "
        "to parse the workflow, so this fails here first, where the "
        "message names the mistake."
    )
    assert any(_MACOS_LABEL in label.lower() for label in labels), (
        f"the harness-perf job runs on {labels!r}, which names no macOS "
        f"runner. The macOS-only perf cases skip there, so the lane would "
        f"publish a budget it never measured."
    )


@pytest.mark.parametrize("job_name", ALL_HARNESS_JOBS)
def test_each_layer_declares_its_budget(job_name: str) -> None:
    """Every harness job declares a ceiling, and it is the layer's.

    A job with no ceiling runs until the runner gives up. What survives
    that is nothing: a cancelled job uploads no log, so the failure
    leaves behind a red cross and no evidence of what was running.
    """
    job = _job(job_name)
    budget = LAYER_BUDGET_MINUTES[job_name]
    declared = job.get("timeout-minutes")
    assert declared is not None, (
        f"the {job_name!r} job declares no `timeout-minutes`. A job with "
        f"no ceiling runs until the runner stops reporting, and a "
        f"cancelled job uploads no log — the failure would leave no "
        f"evidence at all."
    )
    assert declared <= budget, (
        f"the {job_name!r} job declares `timeout-minutes: {declared}`, "
        f"which is above the {budget}-minute ceiling of its layer. A job "
        f"that outruns its layer's budget has moved itself into a longer "
        f"layer, and the layer it was supposed to fit in now waits longer "
        f"than it was designed to."
    )


def test_l1_and_l2_run_on_a_pull_request() -> None:
    """L1 and L2 are per-commit and pre-merge layers.

    The repository's only per-commit trigger is the pull request — the
    workflow has no ``push:`` clause — so a layer that is not reachable
    from a pull request does not run before anything is merged.
    """
    for name in L1_JOBS + L2_JOBS:
        condition = str(_job(name).get("if", ""))
        assert "pull_request" in condition, (
            f"the {name!r} job is gated on {condition!r}, which excludes "
            f"pull_request. This workflow has no `push:` trigger, so the "
            f"pull request is the only path to main — a layer that is "
            f"not reachable from it runs after the merge, which is a "
            f"report rather than a gate."
        )


def test_l3_does_not_run_on_a_pull_request() -> None:
    """L3 is the release and nightly layer, not the per-commit one.

    It needs a self-hosted runner and a real account, so putting it in
    the pull-request path would make every pull request wait for a
    machine that may not exist. It also has to stay out of the merge
    gate: a job that is skipped on a pull request reports ``skipped``,
    and the merge gate treats a skip as a failure.
    """
    for name in L3_JOBS:
        condition = str(_job(name).get("if", ""))
        assert "pull_request" not in condition, (
            f"the {name!r} job is gated on {condition!r}, which admits a "
            f"pull request. L3 needs a self-hosted runner and a real "
            f"account; in the per-commit path every pull request would "
            f"wait for a machine that may not be there."
        )
        assert "schedule" in condition or "workflow_dispatch" in condition, (
            f"the {name!r} job is gated on {condition!r}, which reaches "
            f"neither the release schedule nor a manual dispatch. An L3 "
            f"layer with no release trigger never runs at all."
        )


@pytest.mark.parametrize("job_name", L3_JOBS)
def test_l3_archives_its_evidence(job_name: str) -> None:
    """An L3 job uploads its evidence under the run-scoped archive path.

    The path carries the run id, so two runs of the same layer never
    write over each other's evidence — and a layer that produces no
    artifact at all is a layer whose result nobody can inspect after
    the fact.
    """
    job = _job(job_name)
    upload_paths = [
        str((step.get("with") or {}).get("path", ""))
        for step in job.get("steps", [])
        if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert upload_paths, (
        f"the {job_name!r} job uploads no artifact. L3 runs on a schedule "
        f"and on self-hosted runners, where the run summary is the only "
        f"record — an L3 failure with no artifact leaves nothing to read."
    )
    assert any(L3_ARCHIVE_TEMPLATE in path for path in upload_paths), (
        f"none of the {job_name!r} upload paths contains "
        f"{L3_ARCHIVE_TEMPLATE!r} (got {upload_paths!r}). The archive path "
        f"carries the run id so two runs of the same layer cannot "
        f"overwrite each other's evidence."
    )


def test_the_existing_claude_suite_still_has_its_own_lane() -> None:
    """The harness layers are added to the pipeline, not swapped for it.

    The default backend is ``claude`` — the registry resolves to it
    when nothing is configured — so the pre-existing suite runs the
    claude path unless something narrows it. The harness layers must
    not narrow it: the lanes that already collect the default suite
    stay, and no harness job pins ``PDT_HARNESS``, because the harness
    suite exercises both backends itself and a job-level pin would
    silently narrow one of them.
    """
    names = set(_jobs())
    for name in ("unit-tests", "integration-tests", "e2e-on-demand"):
        assert name in names, (
            f"ci.yml has no {name!r} job. The harness layers are added "
            f"alongside the existing ones; a lane that disappears takes "
            f"the default (claude) suite with it."
        )

    unit_lane = "\n".join(_run_blocks(_job("unit-tests")))
    assert 'LANE_PATHS="tests/unit"' in unit_lane, (
        "the unit-tests lane no longer collects `tests/unit`, so the "
        "default (claude) unit suite has no lane of its own. The harness "
        "layers were added next to it, not in place of it."
    )

    for name in ALL_HARNESS_JOBS:
        env = _job(name).get("env") or {}
        assert "PDT_HARNESS" not in env, (
            f"the {name!r} job pins PDT_HARNESS in its job environment. "
            f"The default backend is claude and the harness suite covers "
            f"both backends on its own, so a job-level pin narrows one of "
            f"them without saying so."
        )

    harness_unit = "\n".join(_run_blocks(_job("harness-unit")))
    assert "tests/unit/harness" in harness_unit, (
        "the harness-unit job does not collect `tests/unit/harness`; the "
        "harness unit suite is the one this lane exists to run."
    )
