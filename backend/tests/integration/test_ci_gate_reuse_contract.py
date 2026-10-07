"""TDD verification for the CI gate-reuse contract.

Background
----------
``pytest 全绿`` is meaningful only if every CI gate that the audit
sweep relies on actually exists in ``.github/workflows/ci.yml`` AND
runs on the trigger path the workflow is supposed to protect. Two
historical regressions made this concrete:

  * On 2026-09-22, push run 35725018566 reported ``conclusion: success``
    while ``unit``, ``integration`` and ``e2e`` were all ``skipped``
    (only lint + grep-guard + slow actually ran). The e2e job
    declared it would run on push to main, but its ``needs:
    integration-tests`` chained to a job that was itself gated to
    ``pull_request`` only — so GitHub skipped the chain and reported
    green. The same shape recurred on 2026-10-02 when the e2e job's
    own ``if`` was narrowed.

    Gate 5 below now roots that check at ``pull_request`` rather than
    ``push``. ci.yml lost its ``push:`` trigger on 2026-10-05, so the
    pull request is the only path to ``main`` and a chain that dies
    there is dead outright.

  * Earlier the same year, the install / lint layer diverged from
    the local rule that pytest must go through ``backend/.venv``:
    a contributor added a bare ``python3 -m pytest`` invocation on
    the ubuntu-latest runner. The runner's system Python 3.9 had
    stale urllib3 + missing deps, collection failed silently, and
    the gate reported success without having collected anything.

This module pins five contracts so neither regression returns under
a different shape. The tests are deliberately string-level rather
than AST-level — the audit's job is to make the gate *obvious*, not
to allow future maintainers to rename ``uv sync --project backend
--locked`` and still pass.

Public surface
--------------
``ci_yml_text()`` — single source of truth for the workflow file
text. Task 17 imports it rather than re-resolving the path so the
"where is the workflow file" knowledge lives in exactly one place.

TDD spec (5 gates):

  Gate 1: ``test_ci_definition_parses``
          YAML parses; top-level ``jobs:`` block exists.

  Gate 2: ``test_install_steps_are_unchanged``
          every install is ``uv sync --project backend --locked``,
          and nothing passes ``--no-dev``.

  Gate 3: ``test_grep_guard_step_still_present``
          ``bash ../scripts/grep_guard.sh`` is still present.

  Gate 4: ``test_every_pytest_invocation_uses_project_venv``
          Every ``python -m pytest`` invocation goes through
          ``backend/.venv`` or bootstrap's ``./.venv``; a bare
          ``python3 -m pytest`` line fails the gate.

  Gate 5: ``test_e2e_job_runs_on_a_pull_request``
          A job invoking ``-m e2e`` exists AND it is reachable
          from ``on.pull_request`` (its own ``if`` allows the pull
          request AND no ``needs:`` ancestor is excluded).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List

import pytest
import yaml

# ---------------------------------------------------------------------------
# Absolute paths
# ---------------------------------------------------------------------------
#
# The audit doc owns the "where is the workflow file" knowledge and pins it
# via Path(__file__).resolve().parents[N] rather than via an env var: a
# contract that depends on a developer's environment is one that quietly
# breaks the moment someone clones the repo to a different layout.

PROJECT_ROOT = (
    Path(__file__).resolve().parents[3]
    if not os.environ.get("PDT_DEV_REPO")
    else Path(os.environ["PDT_DEV_REPO"])
)
WORKFLOW_FILE = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


# ---------------------------------------------------------------------------
# Public helper — exported so task 17 (and any later cross-cutting CI audit)
# can reuse the same single source of truth without re-resolving the path.
# ---------------------------------------------------------------------------


def ci_yml_text() -> str:
    """Return the full text of ``.github/workflows/ci.yml``.

    A single helper used by every test in this module so the "where
    is the workflow file" knowledge lives in exactly one place.
    Fails the test (not the whole file) if the file is missing —
    a missing workflow file is a config regression, not a test
    fixture error.
    """
    assert WORKFLOW_FILE.exists(), (
        f"missing workflow file: {WORKFLOW_FILE}; the CI gate-reuse "
        f"contract cannot be evaluated against an absent workflow"
    )
    return WORKFLOW_FILE.read_text(encoding="utf-8")


def ci_yml_config() -> dict:
    """Return the YAML-parsed workflow file.

    YAML parse failures surface here as ``yaml.YAMLError`` so the
    caller can fail with a clean assertion message rather than an
    opaque parser traceback.
    """
    try:
        return yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        pytest.fail(
            f"could not parse {WORKFLOW_FILE} as YAML: {exc}; "
            f"a CI workflow that does not parse is a CI workflow "
            f"that does not run"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# A top-level job key: exactly two spaces of indent, an id, and
# nothing else on the line. Step / ``with:`` keys are deeper, and
# every key inside a job body is indented at least four spaces, so
# this can never match a nested key.
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", flags=re.MULTILINE)


def _job_body(text: str, job_id: str) -> str:
    """Return the YAML body of the top-level job ``job_id``.

    The body is truncated at the next top-level job key. Without
    that boundary a job that had *lost* its ``needs:`` clause would
    still match — the regex would happily walk forward into the
    next job and quote *its* ``needs:``. Bounding the body makes
    the absence of a clause fail the test, as it should.
    """
    match = re.search(
        rf"^  {re.escape(job_id)}\s*:\s*$", text, flags=re.MULTILINE
    )
    assert match is not None, f"ci.yml has no `{job_id}:` job"
    rest = text[match.end():]
    boundary = _JOB_KEY_RE.search(rest)
    return rest[: boundary.start()] if boundary else rest


def _job_condition(text: str, job_id: str) -> str | None:
    """Return a job's ``if:`` expression, or ``None`` when unconditional."""
    body = _job_body(text, job_id)
    match = re.search(r"^\s+if:\s*(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _needs_tokens(body: str) -> list[str] | None:
    """Tokens of a job body's ``needs:`` clause, or ``None`` if absent.

    GitHub Actions accepts both a scalar (``needs: grep-guard``) and
    a sequence, so both shapes are normalised to a flat token list.
    """
    match = re.search(r"^\s+needs:\s*(.*)$", body, flags=re.MULTILINE)
    if match is None:
        return None
    raw = match.group(1).strip()
    if not raw:
        # Sequence shape — the ids are the following ``- <id>`` lines.
        collected: list[str] = []
        for line in body[match.end():].splitlines():
            if not line.strip():
                continue
            if line.lstrip().startswith("-"):
                collected.append(line)
                continue
            break
        raw = " ".join(collected)
    return re.findall(r"[A-Za-z0-9_:-]+", raw)


#: A predicate fragment that permits a pull request. Matched as a
#: substring so a job may combine it with other conditions (the e2e
#: and time-sensitive lanes also allow ``workflow_dispatch``).
_PULL_REQUEST_PREDICATE = "github.event_name == 'pull_request'"


def _runs_on_pull_request(text: str, job_id: str) -> bool:
    """True when ``job_id`` is not excluded from a pull request.

    Since 2026-10-05 the merge trigger is the ONLY trigger: ci.yml has
    no ``push:`` branch any more, so ``main`` is protected only if this
    workflow runs green on the pull request. A ``schedule``-only or
    ``workflow_dispatch``-only predicate therefore means the lane never
    runs before a merge — the same dead-chain failure this helper
    exists to catch, one trigger over. Mirrors the predicate logic in
    ``test_ci_bugfix_gates.py`` so a future refactor must update both
    gates together.
    """
    condition = _job_condition(text, job_id)
    if condition is None:
        return True  # no predicate — runs on every trigger
    return _PULL_REQUEST_PREDICATE in condition


# ---------------------------------------------------------------------------
# TDD spec 1 — YAML parses and contains ``jobs:``
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ci_definition_parses() -> None:
    """``ci.yml`` must be parseable YAML with a top-level ``jobs:`` block.

    A CI workflow that does not parse is a CI workflow that does not
    run. The pytest_lines / grep-guard-string assertions below would
    both pass vacuously against a YAML file GitHub rejects at
    evaluation time, so this gate surfaces that failure first.
    """
    cfg = ci_yml_config()
    assert isinstance(cfg, dict), (
        f"top-level ci.yml parsed as {type(cfg).__name__}; expected a "
        f"mapping (jobs/on/name/...)"
    )
    assert "jobs" in cfg, (
        "ci.yml has no top-level `jobs:` block; GitHub Actions will "
        "refuse to schedule any work"
    )
    assert isinstance(cfg["jobs"], dict) and cfg["jobs"], (
        "ci.yml `jobs:` block is empty or not a mapping"
    )


# ---------------------------------------------------------------------------
# TDD spec 2 — install steps unchanged
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_install_steps_are_unchanged() -> None:
    """Every ``uv sync`` the workflow runs must be ``--locked``, and none may be ``--no-dev``.

    Pinned at the audit level: a contributor who narrows the install
    to ``pip install .`` (omitting the dev/test extras) would let
    pytest collection fail silently on CI without any one test
    noticing.

    The spelling changed on 2026-10-06 when ``backend/requirements.txt``
    gave way to ``backend/pyproject.toml`` + ``backend/uv.lock``, but the
    contract underneath did not — and the replacement is strictly
    stronger, so this is the same gate pointed at a better mechanism:

    * ``--locked`` makes uv *fail* when ``uv.lock`` and
      ``pyproject.toml`` disagree. The old ``pip install -r`` resolved
      the whole graph afresh on every job, so a routine upgrade could
      change what CI tested with no edit to blame. That is not a
      narrower gate, it is a different kind: it catches a *silent*
      change the substring check could never have seen.
    * ``--no-dev`` is the uv spelling of dropping the test extras, so
      it is rejected by name. ``uv sync`` installs the ``dev`` group by
      default and nothing in the repository may turn that off.

    Scanned through the YAML rather than as text, and only over ``run:``
    payloads. The first version of this gate matched any line
    mentioning ``uv sync`` — which meant the explanatory comment above
    each install step (``# `uv sync` installs backend/uv.lock…``) read
    as an install missing ``--locked``. A gate that trips on prose
    about itself gets deleted the first time someone improves the prose.
    """
    text = ci_yml_text()

    assert "pip install -r backend/requirements.txt" not in text, (
        "ci.yml still installs from backend/requirements.txt, which no "
        "longer exists and is not covered by uv.lock. Route every "
        "install through `uv sync --project backend --locked`."
    )

    workflow = yaml.safe_load(text)
    offenders: List[str] = []
    seen = 0

    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for raw in run.splitlines():
                line = raw.strip()
                if not line.startswith("uv sync"):
                    continue
                seen += 1
                where = f"job {job_name!r}, step {step.get('name', '<unnamed>')!r}"
                if "--locked" not in line:
                    offenders.append(
                        f"  {where}: {line!r} — no --locked. Without the "
                        f"flag uv silently re-resolves instead of "
                        f"installing backend/uv.lock, which is the exact "
                        f"drift this manifest exists to prevent."
                    )
                if "--no-dev" in line:
                    offenders.append(
                        f"  {where}: {line!r} — --no-dev drops the dev "
                        f"group, which carries pytest, pytest-asyncio, "
                        f"pytest-timeout, pytest-cov, pytest-xdist, "
                        f"coverage, bandit and the rest of what the "
                        f"suite is run with. Collection then fails with "
                        f"no test to notice."
                    )

    assert seen, (
        "no `uv sync` invocation was found in any `run:` block of ci.yml; "
        "the install step was changed and no longer routes through the "
        "lockfile. Restore `uv sync --project backend --locked`."
    )
    assert not offenders, (
        "these install steps in ci.yml are not the locked sync:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# TDD spec 3 — grep-guard step still present
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_grep_guard_step_still_present() -> None:
    """The grep-guard step must still invoke ``bash ../scripts/grep_guard.sh``.

    Pinned at the audit level: a contributor who inlines a
    ``python3 -c`` invocation in the ``grep-guard`` job — or who
    drops the job entirely — silently disables the
    forbidden-filename sweep on CI while it still passes locally.
    The same wrapper is consumed by ``.pre-commit-config.yaml``;
    keeping both callers pointing at the same shell entry point is
    what makes the contract auditable.
    """
    text = ci_yml_text()
    assert "bash ../scripts/grep_guard.sh" in text, (
        "ci.yml no longer contains `bash ../scripts/grep_guard.sh`; "
        "the grep-guard step was either removed or inlined. "
        "Restore `bash ../scripts/grep_guard.sh` so the CI gate and "
        "the pre-commit hook share the same wrapper."
    )


# ---------------------------------------------------------------------------
# TDD spec 4 — every pytest invocation goes through the project venv
# ---------------------------------------------------------------------------


#: Lines that count as a "pytest invocation". We deliberately match
#: on the substring ``-m pytest`` (rather than ``python3 -m pytest``
#: alone) so a future ``uv run pytest`` or similar wrapper still
#: appears in the audit surface — the failure message lists every
#: line that violates the rule.
_PYTEST_LINE_RE = re.compile(r"^.*-m pytest.*$", flags=re.MULTILINE)


@pytest.mark.integration
def test_every_pytest_invocation_uses_project_venv() -> None:
    """Every ``python -m pytest`` line in ci.yml MUST go through the project venv.

    The ubuntu-latest runner's system Python 3.9 has stale urllib3
    and missing dependencies, so a bare ``python3 -m pytest``
    invocation silently fails pytest collection. CLAUDE.md pins the
    rule: pytest must run through ``backend/.venv/bin/python3`` or
    the bootstrap step's ``./.venv/bin/python3``.

    The gate fails loudly here rather than in a stale-urllib3
    traceback at the bottom of a 45-minute run.
    """
    text = ci_yml_text()
    pytest_lines = [
        line.strip() for line in text.splitlines() if "-m pytest" in line
    ]
    assert pytest_lines, (
        "ci.yml does not contain any `-m pytest` invocations; the "
        "layered gates have no pytest calls to enforce the "
        "venv-python contract on"
    )
    violations = [
        line for line in pytest_lines
        if ".venv/bin/python3" not in line
        and ".venv/bin/python " not in line
    ]
    assert not violations, (
        "every `-m pytest` invocation in ci.yml MUST go through "
        "`./.venv/bin/python3` (system Python on ubuntu-latest has "
        f"stale urllib3 + missing deps). Offending lines: {violations!r}"
    )


# ---------------------------------------------------------------------------
# TDD spec 5 — an e2e job runs on a pull request
# ---------------------------------------------------------------------------


def _find_e2e_job(text: str) -> str | None:
    """Return the first top-level job whose steps invoke ``-m e2e``.

    The search accepts the marker in either unquoted (``-m e2e``)
    or quoted (``-m "e2e"`` / ``-m 'e2e'``) shape — the ci.yml
    recipe uses the quoted form, but we accept both so a future
    maintainer that re-quotes the marker does not silently break
    the audit. The match is anchored on the substring ``e2e`` so a
    future rename of ``e2e-on-demand`` (or the introduction of
    ``e2e-mock-plan``) does not silently remove the gate; what
    matters is that *some* job on the push-to-main path actually
    runs ``-m e2e``.
    """
    # Match `-m e2e`, `-m "e2e"`, `-m 'e2e'`. The pattern requires
    # whitespace before the marker (so `-m foo` does not match) and
    # allows an optional surrounding quote pair.
    e2e_marker_re = re.compile(r"-m\s+['\"]?e2e['\"]?\b")
    jobs_header = re.search(r"^jobs:\s*$", text, flags=re.MULTILINE)
    assert jobs_header is not None, (
        "ci.yml has no top-level `jobs:` block; cannot search for "
        "the e2e job"
    )
    for match in _JOB_KEY_RE.finditer(text, jobs_header.end()):
        job_id = match.group(1)
        body = _job_body(text, job_id)
        if e2e_marker_re.search(body):
            return job_id
    return None


@pytest.mark.integration
def test_e2e_job_runs_on_a_pull_request() -> None:
    """A job invoking ``-m e2e`` exists AND is reachable from a pull request.

    Two halves:

      1. **Existence**: ci.yml must declare at least one job whose
         step shell line carries ``-m e2e``. Without it the
         pytest-e2e layer is invisible to CI.

      2. **Reachability**: the e2e job AND every job in its
         ``needs:`` chain must NOT be excluded from a pull request
         (a schedule-only or dispatch-only predicate silently skips
         the whole chain — the 2026-09-22 regression that this test
         was written to pin, which recurred in 2026-10-02 on the e2e
         job and is now what this half guards).

    This used to be rooted at ``push`` to main, back when the repo
    merged locally and pushed main directly with no pull request.
    The ``main: pull request required`` ruleset (2026-09-29) made
    every commit on main arrive through a PR, and ci.yml lost its
    ``push:`` trigger on 2026-10-05 — so the pull request is now the
    only path to main, and a chain that dies there never runs at all.
    """
    text = ci_yml_text()

    e2e_job_id = _find_e2e_job(text)
    assert e2e_job_id is not None, (
        "ci.yml has no job whose step shell line invokes `-m e2e`; "
        "the e2e layer has been removed from CI. Restore the e2e "
        "job (the standard id is `e2e-on-demand`) with a step that "
        "calls `./.venv/bin/python3 -m pytest ... -m e2e`."
    )

    # Walk the ``needs:`` graph from the e2e job to its roots. Every
    # ancestor must also be reachable on a pull request — GitHub
    # skips a job whose needs-job was skipped, so an e2e job with a
    # pull_request ``if`` is still dead if the job it needs is
    # schedule-only.
    pending = [e2e_job_id]
    visited: set[str] = set()
    while pending:
        job_id = pending.pop()
        if job_id in visited:
            continue
        visited.add(job_id)
        body = _job_body(text, job_id)
        assert _runs_on_pull_request(text, job_id), (
            f"the {job_id!r} job is in the `-m e2e` chain but is "
            f"excluded from a pull request "
            f"(if={_job_condition(text, job_id)!r}). ci.yml has no "
            f"`push:` trigger, so the pull request is the only path to "
            f"main — a schedule- or dispatch-only predicate means the "
            f"e2e layer never runs before a merge."
        )
        pending.extend(_needs_tokens(body) or [])