"""The pull-request commit cap is wired, and the cap is still five.

Why this file exists
--------------------
``.github/workflows/ci.yml`` enforces that a pull request carries at most
five commits. The number is a product decision, so it lives as a literal
in the job rather than as a repository variable — which also means an
edit of ``ci.yml`` can raise it, or drop the job, or leave the job out of
the merge gate's ``needs``, and nothing would say so.

Each of those is one case below:

* the job disappears, or stops being ``pull_request``-only — the cap
  stops applying (or starts failing dispatches that have no PR);
* the cap literal changes — a silent widening of the rule;
* the count stops coming from ``pull_request.commits`` — the number the
  pull request itself shows is no longer what is enforced;
* the job stays but leaves the merge gate's ``needs`` — it goes red and
  nothing closes. ``test_merge_gate_contract.py`` also pins this, from
  the other direction; the duplication is deliberate, the failure is
  silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

COMMIT_CAP_JOB = "pr-commit-cap"
MERGE_GATE_JOB = "merge-gate"
EXPECTED_CAP = "5"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


def test_the_job_exists_and_runs_on_pull_requests_only(jobs: dict) -> None:
    job = jobs.get(COMMIT_CAP_JOB)
    assert job is not None, f"no {COMMIT_CAP_JOB} job in ci.yml"
    condition = str(job.get("if") or "")
    assert "pull_request" in condition, (
        "the commit cap has to be pull_request-only: a dispatch has no "
        f"pull_request payload to read. Got if: {condition!r}"
    )


def test_the_cap_is_five_and_reads_the_pull_request_count(jobs: dict) -> None:
    steps = jobs[COMMIT_CAP_JOB]["steps"]
    env = steps[0]["env"]
    assert env["CAP"] == EXPECTED_CAP, (
        f"the cap is {EXPECTED_CAP}; got {env['CAP']!r}. Raising it is a "
        "decision, not an edit."
    )
    assert "pull_request.commits" in str(env["COMMITS"]), (
        "the enforced count must be the pull request's own commit count, "
        f"not something else. Got {env['COMMITS']!r}"
    )


def test_the_merge_gate_needs_it(jobs: dict) -> None:
    needs = jobs[MERGE_GATE_JOB].get("needs") or []
    assert COMMIT_CAP_JOB in needs, (
        f"the merge gate does not need {COMMIT_CAP_JOB}; it would go red "
        "without closing anything"
    )
