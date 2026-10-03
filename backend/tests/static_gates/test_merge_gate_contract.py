"""The merge gate must gate, not merely report.

Why this gate exists
--------------------
GitHub evaluates a required status check as passing when its conclusion is
``success`` **or ``skipped`` or ``neutral``**. That single fact makes the
obvious way to require a test suite wrong:

    unit-tests  (a shard goes red)
      └─ coverage-gate      → skipped → counts as SUCCESS
      └─ integration-tests  → skipped → counts as SUCCESS

Require the downstream jobs and the upstream suite is unprotected. The
``needs:`` chain looks like it closes the gap — it does not, because a job
that depends on a failed job is *skipped* rather than *failed*, and skip
counts as pass. This repository shipped exactly that configuration once:
four required checks whose stated purpose was "unit is covered
transitively", none of which would have blocked a merge on a red unit
shard.

The fix is a terminal job with ``if: always()`` that reads
``needs.*.result`` and fails on anything that is not ``success``. This file
is what stops that from quietly regressing, because every part of it can
be undone by an innocent-looking edit:

* dropping ``always()`` from the gate's ``if:`` — the gate then inherits
  the very skip it exists to detect, and goes green while broken;
* narrowing the gate's ``if:`` so it no longer matches ``pull_request`` —
  same outcome, and it looks like tidying;
* adding a new pull-request job and not adding it to the gate's
  ``needs`` — the new job is unmonitored and nothing says so;
* relaxing the body to tolerate ``skipped`` — the hole reopens, quietly,
  and every dependent job that was skipped looks satisfied.

Scope, stated honestly: this reads the workflow file and asserts on its
structure. It does not run GitHub Actions, so it cannot prove GitHub's
evaluation rules — those are asserted in prose in the job's comment and
pinned here only as far as the YAML can express them. What it does prove is
that the structure which *depends* on those rules is still intact, which
is the part a refactor can silently undo.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

GATE_JOB = "merge-gate"
GATE_DISPLAY_NAME = "Merge gate (all PR jobs green)"

# Jobs that must never appear in the gate's ``needs``. Each is gated to a
# non-pull-request trigger, so requiring it would wedge every merge on a
# check that cannot report on a pull request — a blocker no one can ever
# satisfy, which is its own kind of broken gate.
DISPATCH_ONLY_JOBS = ("unit-staircase", "nightly-regression", "real-plan-migration")


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.fail(f"{WORKFLOW} does not exist; the merge gate has no host")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow.get("jobs") or {}


@pytest.fixture(scope="module")
def gate(jobs: dict) -> dict:
    if GATE_JOB not in jobs:
        pytest.fail(
            f"ci.yml has no `{GATE_JOB}` job. Branch protection is configured "
            f"to require `{GATE_DISPLAY_NAME}`; without that job every merge "
            f"is blocked forever, and if the requirement was dropped instead, "
            f"nothing stands between a red suite and main."
        )
    return jobs[GATE_JOB]


def _pull_request_jobs(jobs: dict) -> set[str]:
    """Every job whose ``if:`` admits a pull-request event.

    Derived from the workflow rather than listed, so a newly added
    pull-request job shows up here as a gate failure instead of quietly
    joining the ungated set.
    """
    found = set()
    for name, body in jobs.items():
        if not isinstance(body, dict):
            continue
        condition = str(body.get("if", ""))
        if "pull_request" in condition:
            found.add(name)
    return found


@pytest.fixture(scope="module")
def gate_source() -> str:
    """The merge-gate block as raw text, straight from the file.

    Reading a re-serialisation would be testing ``yaml.safe_dump``'s
    escaping rather than the workflow: the gate's behaviour lives in a
    ``run:`` body, and the only faithful way to read a ``run:`` body is
    the file it was written in.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = f"\n  {GATE_JOB}:\n"
    if marker not in text:
        # `text.index` would raise a bare ValueError naming an offset and
        # nothing else. A renamed or deleted gate is the single most
        # consequential edit someone can make to this file, and the
        # failure has to say so in words.
        pytest.fail(
            f"ci.yml no longer contains a `{GATE_JOB}:` job. Either the "
            f"merge gate was renamed or removed — in which case branch "
            f"protection's required check {GATE_DISPLAY_NAME!r} either "
            f"blocks every merge forever (renamed) or has silently stopped "
            f"existing (removed). Update both together or neither."
        )
    start = text.index(marker) + 1
    rest = text[start + len(marker) :]
    # The gate is currently the last job; cut at the next top-level job
    # key so appending one does not silently fold it into this block.
    match = re.search(r"\n  [A-Za-z0-9_-]+:\n", rest)
    return rest[: match.start()] if match else rest


class TestTheGateExists:
    def test_it_is_a_job_with_a_stable_display_name(self, gate):
        assert gate.get("name") == GATE_DISPLAY_NAME, (
            f"branch protection requires the check to be called "
            f"{GATE_DISPLAY_NAME!r}; renaming it without updating the "
            f"protection rule makes every merge permanently blocked"
        )

    def test_it_runs_on_pull_requests(self, gate):
        assert "pull_request" in str(gate.get("if", "")), (
            "the gate's `if:` no longer matches pull_request, so on a PR it "
            "is SKIPPED — and a skipped required check counts as a pass. "
            "That is the exact failure this job was added to prevent, "
            "reached by narrowing the condition instead of deleting it."
        )

    def test_it_uses_always_so_a_failure_upstream_cannot_skip_it(self, gate):
        condition = str(gate.get("if", ""))
        assert "always()" in condition, (
            "the merge gate is missing `always()`. Without it the gate is "
            "skipped whenever a dependency fails, and GitHub counts a "
            "skipped required check as a pass — so the gate would open "
            "precisely when it is needed. The skip it inherits is the skip "
            "it was built to catch."
        )


class TestItCoversEveryPullRequestJob:
    def test_needs_covers_every_pull_request_job(self, gate, jobs):
        needed = set(gate.get("needs") or [])
        # The gate itself is trivially "a pull-request job" in a workflow
        # that has one; exclude it so the comparison is meaningful.
        expected = _pull_request_jobs(jobs) - {GATE_JOB}

        missing = expected - needed
        assert not missing, (
            "these jobs run on pull requests but are not in the merge "
            f"gate's needs: {sorted(missing)}. A job outside the gate is "
            "unmonitored — if it goes red nothing closes, and if it is "
            "skipped nothing complains. Add it to `needs`, or give it an "
            "`if:` that stops it matching pull_request."
        )

    def test_it_does_not_require_dispatch_only_jobs(self, gate):
        needed = set(gate.get("needs") or [])
        wedged = sorted(set(DISPATCH_ONLY_JOBS) & needed)
        assert not wedged, (
            f"{wedged} only run on schedule or manual dispatch. Requiring "
            "a check that never reports on a pull request blocks every "
            "merge with no way to satisfy it."
        )

    def test_every_need_is_a_real_job(self, gate, jobs):
        dangling = sorted(set(gate.get("needs") or []) - set(jobs))
        assert not dangling, (
            f"the gate needs {dangling}, which are not jobs in this "
            f"workflow. GitHub would refuse to parse the file, so this "
            f"fails loudly at CI time — but it should fail here first, "
            f"where the error names the mistake."
        )


class TestTheBodyRefusesToBeFooled:
    """Execute the gate rather than read it.

    A string check on the ``run:`` body is the weakest thing that could
    pass here, and it is exactly the check that was tried first: it
    asserted ``"exit 1" in source``, and changing the real
    ``sys.exit(1)`` to ``sys.exit(0)`` left a second ``exit 1`` further
    down in the shell fallback — so the mutation shipped green under a
    test whose stated purpose was to stop the gate from not gating.

    So the body is run. It is a pure function of ``NEEDS_JSON`` — no
    network, no filesystem, no GitHub context — which makes it exactly as
    testable here as it is on the runner. The four cases below are the
    ones that decide whether this job is worth having.
    """

    @pytest.fixture(scope="class")
    def gate_step(self, gate) -> dict:
        steps = gate.get("steps") or []
        assert steps, "the merge gate has no steps to execute"
        return steps[0]

    @pytest.fixture(scope="class")
    def gate_script(self, gate_step) -> str:
        script = gate_step.get("run", "")
        assert script, "the merge gate has no run: body to execute"
        return script

    # Expressions the workflow interpolates from the event context. The
    # harness stands in for GitHub; anything left over is a bug in the
    # gate (a typo'd expression) or a gap in this table, and either way
    # the run must not silently proceed.
    _CONTEXT = {
        "toJSON(needs)": "__NEEDS__",
        "github.server_url": "https://example.invalid",
        "github.repository": "owner/repo",
        "github.run_id": "1",
        "github.run_attempt": "1",
    }

    def _render(self, raw: str, payload: str) -> str:
        out = str(raw)
        for expr, value in self._CONTEXT.items():
            out = out.replace("${{ " + expr + " }}", value)
        return out.replace("__NEEDS__", payload)

    def _run_gate(self, gate_step, gate_script, results: dict):
        """Run the real script with a synthetic ``needs`` context.

        The environment is assembled from the step's own ``env:`` block
        rather than hardcoded here, so adding a variable to the workflow
        does not silently leave the harness running against a shell with
        an unbound variable under ``set -u`` — which would look like a
        gate failure rather than a harness gap.
        """
        payload = json.dumps(
            {name: {"result": result} for name, result in results.items()}
        )
        script = self._render(gate_script, payload)
        assert "${{" not in script, (
            "the gate body has an un-substituted expression: "
            f"{script[script.index('${{'):script.index('${{') + 60]!r}"
        )

        env = {k: self._render(v, payload) for k, v in (gate_step.get("env") or {}).items()}
        env["NEEDS_JSON"] = payload
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            timeout=60,
        )

    def test_it_opens_when_every_job_succeeded(self, gate_step, gate_script):
        proc = self._run_gate(
            gate_step, gate_script,
            {name: "success" for name in ("lint", "unit-tests", "e2e-on-demand")},
        )
        assert proc.returncode == 0, (
            f"the gate closed on an all-green run (rc={proc.returncode}).\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        assert "All 3 upstream jobs succeeded" in proc.stdout, (
            "the gate's own summary must state how many jobs it checked.\n"
            f"stdout:\n{proc.stdout}\n"
            "It used to print `${#NEEDS_JSON}` — the byte length of the "
            "JSON string — and announce 620 jobs for a nine-job gate. A "
            "number a reader takes literally is not a cosmetic problem; "
            "it is the same class of defect this gate exists to prevent, "
            "one level down."
        )

    def test_it_closes_when_a_job_failed(self, gate_step, gate_script):
        """The case the whole job exists for.

        ``unit-tests`` is not special to the script; it is here because
        it is the one whose downstream jobs are *skipped* rather than
        failed, which is what made the previous required-check set
        ineffective.
        """
        proc = self._run_gate(
            gate_step, gate_script,
            {"lint": "success", "unit-tests": "failure", "e2e-on-demand": "skipped"},
        )
        assert proc.returncode != 0, (
            "the gate opened with a red upstream job. A gate that reports "
            "and returns 0 blocks nothing — the failure it just described "
            "is exactly the one it exists to stop."
        )
        assert "unit-tests" in proc.stdout, (
            "the gate must name the job that closed it; an opaque failure "
            f"is a failure nobody can act on. stdout:\n{proc.stdout}"
        )

    def test_it_closes_on_a_skipped_dependency(self, gate_step, gate_script):
        """The load-bearing case, and the one that is easy to write wrong.

        A job skipped because a dependency failed reports ``skipped``.
        GitHub counts a skipped required check as a *pass*. A gate that
        tolerates ``skipped`` therefore passes precisely when the suite
        is broken — accepting it looks like tolerance and behaves like
        the bug.
        """
        proc = self._run_gate(
            gate_step, gate_script,
            {"lint": "success", "unit-tests": "success", "coverage-gate": "skipped"},
        )
        assert proc.returncode != 0, (
            "the gate opened on a skipped dependency. This is the failure "
            "mode branch protection cannot catch for us: the required "
            "check reports `skipped`, GitHub reads that as success, and "
            "the merge goes through. Tolerating `skipped` here reopens "
            "the exact hole this job was added to close."
        )

    @pytest.mark.parametrize("result", ["cancelled", "failure", "skipped"])
    def test_it_closes_on_every_non_success_result(self, gate_step, gate_script, result):
        proc = self._run_gate(gate_step, gate_script, {"lint": "success", "unit-tests": result})
        assert proc.returncode != 0, (
            f"the gate opened when a dependency reported {result!r}; only "
            f"`success` may open it"
        )


class TestNoRequiredCheckCanBeSkippedAway:
    """A required check that never reports is a permanent merge blocker.

    GitHub's rule, from the troubleshooting guide: a workflow excluded by
    path or branch filtering leaves its checks in ``Pending`` — and a
    pending required check blocks every merge, with no action available
    that makes it go away. The failure is not "the gate is bypassed"; it
    is "the gate is unsatisfiable", which is worse, because it looks like
    CI being busy.

    That is not hypothetical here. ``test_json_cleanup.yml`` carried a
    ``paths:`` filter listing four server-side paths, and a pull request
    touching only ``ci.yml`` therefore could never be merged: the
    ``JSON cleanup three-layer defense`` required check stayed pending
    forever. The filter saved about forty seconds of e2e on unrelated
    pull requests and cost the repository its ability to merge them.

    A required check and a path filter are mutually exclusive — one of
    them has to go — and which one goes is a policy decision that lives
    in the repository's settings, not in this file. So the rule pinned
    here is the conservative one: no workflow may path-filter a
    pull-request trigger, because none of them can be required while
    doing so. Dropping a check from ``required_status_checks`` remains
    the escape hatch, and it is a visible one.
    """

    @pytest.fixture(scope="class")
    def workflows_dir(self) -> Path:
        return WORKFLOW.parent

    def test_no_workflow_path_filters_a_pull_request(self, workflows_dir):
        offenders = []
        for path in sorted(workflows_dir.glob("*.yml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:  # pragma: no cover - CI parses first
                pytest.fail(f"{path.name} is not valid YAML: {exc}")
            if not isinstance(document, dict):
                continue

            # PyYAML resolves the bare key `on:` to the boolean True,
            # which is why every workflow reader has this quirk.
            triggers = document.get("on", document.get(True))
            if not isinstance(triggers, dict):
                continue
            pull_request = triggers.get("pull_request")
            if isinstance(pull_request, dict) and pull_request.get("paths"):
                offenders.append(
                    f"{path.name} (pull_request.paths: "
                    f"{pull_request['paths']})"
                )
            # The bare ``pull_request:`` form (no options) has no filter
            # and is fine; only a mapping can carry one.

        assert not offenders, (
            "these workflows path-filter a pull-request trigger: "
            + "; ".join(offenders)
            + ". If any of their jobs is a required status check, a pull "
            "request that touches none of the listed paths leaves the "
            "check pending forever and blocks every merge with no way to "
            "satisfy it. Remove the `paths:` filter, or take the job out "
            "of required_status_checks — but do not leave both."
        )

    def test_the_main_workflow_itself_is_not_path_filtered(self, workflow):
        triggers = workflow.get("on", workflow.get(True))
        pull_request = (triggers or {}).get("pull_request")
        assert not (isinstance(pull_request, dict) and pull_request.get("paths")), (
            "ci.yml path-filters its own pull-request trigger, which would "
            "wedge the merge gate and every other required check the same "
            "way"
        )


class TestStaticGatesRunBeforeTheLanes:
    """A gate that runs alongside the tests it gates is not a gate.

    The static contracts are pytest files, so before the ``static-gates``
    job existed they ran inside the ``root`` lane — sharded across
    ``root-0..root-6``, in parallel with the other nineteen shards. A
    contract failure then arrived at the same moment twenty runners had
    already committed to finishing. The check was in the suite; it was
    not ahead of anything.

    The ordering is the whole value, and ordering is invisible to the
    tests that live inside it: every one of these contracts passes
    whether it runs first or last. So it has to be pinned from outside,
    which is what this class is.

    The invariant is deliberately about the *graph* and not about a
    particular ``needs:`` line: a lane qualifies if it runs anything
    under ``tests/``, and it must reach ``static-gates`` through the
    dependency edges. That way adding a new lane is a gate failure
    rather than an unmonitored way to spend twenty runner-minutes before
    a contract gets its say.
    """

    GATE_JOB = "static-gates"

    @pytest.fixture(scope="class")
    def runs_tests(self, jobs: dict) -> set[str]:
        """Jobs that both run tests and are part of the pull-request gate.

        The PR scope is not a convenience — it is the only correct one.
        ``nightly-regression`` runs on ``schedule``, where ``static-gates``
        is itself skipped, so making the nightly depend on it would leave
        the nightly permanently skipped. A contract about gate ordering
        that wedges the job it is protecting is not a stricter gate.
        """
        found = set()
        for name, body in jobs.items():
            if not isinstance(body, dict):
                continue
            if "pull_request" not in str(body.get("if", "")):
                continue
            steps = body.get("steps") or []
            text = "\n".join(
                str(s.get("run", "")) for s in steps if isinstance(s, dict)
            )
            if re.search(r"pytest[^|;&\n]*tests/", text):
                found.add(name)
        return found

    @staticmethod
    def _needs_of(body) -> list[str]:
        needs = body.get("needs")
        if needs is None:
            return []
        return [needs] if isinstance(needs, str) else list(needs)

    def _reaches_gate(self, jobs: dict, start: str) -> bool:
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            if current == self.GATE_JOB:
                return True
            body = jobs.get(current)
            if isinstance(body, dict):
                frontier.extend(self._needs_of(body))
        return False

    def test_the_gate_job_exists(self, jobs):
        assert self.GATE_JOB in jobs, (
            f"ci.yml has no `{self.GATE_JOB}` job. The static contracts "
            f"go back to running inside the `root` lane, where a failure "
            f"arrives with nineteen shards already in flight."
        )

    def test_every_lane_that_runs_tests_is_downstream_of_it(self, jobs, runs_tests):
        ungated = sorted(
            name for name in runs_tests if not self._reaches_gate(jobs, name)
        )
        assert not ungated, (
            f"these jobs run tests but are not downstream of "
            f"`{self.GATE_JOB}`: {ungated}. Their runners start before the "
            f"contracts have said anything, so a red contract costs the "
            f"full matrix instead of one job. Add `{self.GATE_JOB}` to "
            f"their `needs` (directly, or via a job that has it)."
        )

    def test_the_lanes_that_spend_the_most_are_among_them(self, jobs, runs_tests):
        """The shards are the whole point — a gate upstream of a 3-second
        job buys nothing. This names the expensive ones explicitly so the
        previous assertion cannot be satisfied by covering only cheap
        jobs while the matrix stays ungated."""
        for lane in ("unit-tests", "integration-tests", "e2e-on-demand"):
            if lane in runs_tests:
                assert self._reaches_gate(jobs, lane), (
                    f"`{lane}` runs tests but is not downstream of "
                    f"`{self.GATE_JOB}`"
                )

    def test_the_gate_itself_runs_on_pull_requests(self, jobs):
        """The precondition for the whole ordering.

        Every other assertion here asks "is the gate upstream of the
        lanes". If the gate does not run on a pull request, that question
        has no answer worth having: the lanes either start anyway or sit
        skipped waiting for a check that will never report. Both outcomes
        are the ``skipped`` trap this repository already fell into once.
        """
        body = jobs[self.GATE_JOB]
        assert "pull_request" in str(body.get("if", "")), (
            f"`{self.GATE_JOB}` does not run on pull requests. The lanes "
            f"that depend on it would then start against a job that never "
            f"reported — which is either a wedge or a bypass, depending on "
            f"how the dependent lane's condition evaluates. Neither is what "
            f"an ordering is for."
        )

    def test_the_gated_lanes_do_not_also_run_it_inline(self, workflow):
        """No double-run.

        ``tests/static_gates/`` lives under ``tests/``, so the ``root``
        lane's ``tests/`` sweep picks it up unless it is explicitly
        ignored. Running it in both places costs a redundant pass and,
        worse, means a static failure can arrive twice from two different
        directions — one of which is inside the matrix this job exists to
        keep out of it.
        """
        text = WORKFLOW.read_text(encoding="utf-8")
        for lane, marker in (("root", 'root) LANE_PATHS="'),):
            start = text.find(marker)
            assert start != -1, f"could not find the {lane} lane's path set"
            line = text[start : text.find("\n", start)]
            assert "--ignore=tests/static_gates" in line, (
                f"the `{lane}` lane sweeps `tests/` without ignoring "
                f"`tests/static_gates`, so the contracts run both in the "
                f"dedicated job and inside the shard matrix. Add the "
                f"ignore; the dedicated job is the one the lanes depend on."
            )
