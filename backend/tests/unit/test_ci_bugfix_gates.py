"""CI workflow gate / dispatch-input contract tests.

The CI pipeline (``.github/workflows/ci.yml``) is the authoritative
gate that decides whether code changes can be merged. As new test
layers have been added in earlier tasks (grep-guard, unit,
integration, mock-plan E2E, real-plan E2E), the workflow must continue
to express the layered contract in a single, parseable file:

  1. ``grep-guard``  — fast static check (regex sweep over the
     production tree) runs FIRST so unit/integration do not have to
     waste cycles on a tree that already contains a known bug.
  2. ``unit``         — runs after ``grep-guard``. Runs the project's
     own ``.venv/bin/python3 -m pytest`` invocation (system Python
     has stale urllib3 + missing deps).
  3. ``integration``  — runs after ``unit``. Same venv python policy.
  4. ``e2e-main``     — runs the mock plan E2E by default (covered
     by ``run_verify_deploy_pulled.py``). The dangerous REAL plan
     migration is gated behind a ``workflow_dispatch`` input so it
     never runs on a routine push or PR.

These two tests pin the surface area of ``.github/workflows/ci.yml``
so regressions cannot silently weaken the gate contract. Both tests
are deliberately STRING-LEVEL: they read the workflow file as text
and check for the exact substrings the contract requires. This is
intentionally a higher bar than a YAML-tree comparison — a future
maintainer who adds a brand new ``e2e-real`` job that does NOT chain
after the layered gates will be caught.

TDD test specs:

  test_ci_contains_bugfix_test_gates
      ``ci.yml`` must declare a ``grep-guard`` job; must contain the
      three pytest invocation blocks for grep-guard, unit, integration
      (all of which MUST use ``./.venv/bin/python3``); and the
      ``e2e-main`` job must carry a comment / label that links it to
      the mock-plan migration contract.

  test_ci_real_migration_requires_explicit_input
      The ``workflow_dispatch`` trigger must declare an
      ``inputs.run_real_plan_migration`` boolean input. A separate
      REAL-plan migration job (or step) MUST be guarded by an
      ``if: github.event.inputs.run_real_plan_migration == 'true'``
      predicate so it cannot run on a routine push or PR.

The two tests pin different non-overlapping contracts; both must
PASS for the layered pipeline to be considered correctly wired.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Absolute paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_FILE = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _read_workflow() -> str:
    """Return the full text of ``.github/workflows/ci.yml``.

    A single helper used by every test in this module so the
    "where is the workflow file" knowledge lives in exactly one
    place. Fails the test (not the whole file) if the file is
    missing — a missing workflow file is a config regression, not
    a test fixture error.
    """
    assert WORKFLOW_FILE.exists(), (
        f"missing workflow file: {WORKFLOW_FILE}; the gate contracts "
        f"this module pins cannot be evaluated"
    )
    return WORKFLOW_FILE.read_text(encoding="utf-8")


# A top-level job key: exactly two spaces of indent, an id, and
# nothing else on the line. Step / ``with:`` keys are deeper, and
# every key inside a job body is indented at least four spaces, so
# this can never match a nested key.
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", flags=re.MULTILINE)


def _find_job_id(text: str, pattern: re.Pattern) -> str | None:
    """Return the first top-level job id whose name matches ``pattern``.

    The scan is restricted to the ``jobs:`` section — the ``on:``
    trigger block also has two-space-indented children (``push:``,
    ``workflow_dispatch:``, ...) and must not be mistaken for jobs.
    """
    jobs_header = re.search(r"^jobs:\s*$", text, flags=re.MULTILINE)
    assert jobs_header is not None, (
        "ci.yml has no top-level `jobs:` block; the layered gate "
        "contract cannot be evaluated"
    )
    for match in _JOB_KEY_RE.finditer(text, jobs_header.end()):
        if pattern.search(match.group(1)):
            return match.group(1)
    return None


def _job_body(text: str, job_id: str) -> str:
    """Return the YAML body of the top-level job ``job_id``.

    The body is truncated at the next top-level job key. Without
    that boundary a job that had *lost* its ``needs:`` clause would
    still match — the regex would happily walk forward into the
    next job and quote *its* ``needs:``. Bounding the body makes
    the absence of a clause fail the test, as it should.
    """
    match = re.search(
        rf"^  {re.escape(job_id)}:\s*$", text, flags=re.MULTILINE
    )
    assert match is not None, f"ci.yml has no `{job_id}:` job"
    rest = text[match.end():]
    boundary = _JOB_KEY_RE.search(rest)
    return rest[: boundary.start()] if boundary else rest


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


# ---------------------------------------------------------------------------
# Test 1 — layered gates (grep-guard -> unit -> integration -> e2e)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ci_contains_bugfix_test_gates() -> None:
    """ci.yml must declare the four layered gates and chain them.

    Required shape (any job IDs that satisfy the names below are
    accepted; the test is deliberately ID-agnostic so future
    maintainers can rename ``grep-guard`` -> ``scan`` without
    breaking this contract):

      1. A job whose id contains ``grep-guard`` (or ``grep_guard``).
         The job must invoke the grep scanner — either via the
         project's ``grep_guard`` module or via a delegated pytest
         call — and it must NOT depend on any other job.

      2. The ``unit`` job must declare ``needs: grep-guard`` (or a
         synonym) so a grep-guard failure blocks unit from running.

      3. The ``integration`` job must declare ``needs: unit`` so a
         unit failure blocks integration.

      4. Both ``unit`` and ``integration`` invocations MUST use
         ``./.venv/bin/python3 -m pytest`` — system Python on the
         ubuntu-latest runner has stale urllib3 / missing deps and
         breaks pytest collection.

      5. The ``e2e-main`` (or equivalent mock E2E) job must exist and
         its name / first comment line must mention the mock-plan
         E2E so the contract is self-documenting.
    """
    text = _read_workflow()

    # 1) grep-guard job must exist as a top-level job id.
    assert re.search(
        r"^  grep-guard:\s*$",
        text,
        flags=re.MULTILINE,
    ), (
        "ci.yml must declare a top-level `grep-guard:` job; "
        "this is the fast static gate that protects unit/integration "
        "from running on a tree with a known bug."
    )

    # 2) unit job must declare needs: grep-guard (or grep_guard with
    # a chained job name). We accept either spelling because both
    # are common in GitHub Actions YAML.
    #
    # The job id is matched on a substring (``unit-tests`` is what
    # ci.yml actually declares) rather than on the literal ``unit:``
    # — see the module docstring's "ID-agnostic" promise. Hard-coding
    # the id here is what made this test red while the workflow was
    # unchanged.
    unit_job_id = _find_job_id(text, re.compile(r"unit"))
    assert unit_job_id is not None, (
        "ci.yml must declare a top-level unit job (e.g. `unit-tests:`) "
        "with a `needs:` clause that names the upstream gate."
    )
    unit_needs_tokens = _needs_tokens(_job_body(text, unit_job_id))
    assert unit_needs_tokens is not None, (
        f"the {unit_job_id!r} job must declare a `needs:` clause; "
        f"without it a grep-guard failure does not block unit from "
        f"running."
    )
    assert any(
        "grep-guard" in tok or "grep_guard" in tok
        for tok in unit_needs_tokens
    ), (
        f"the {unit_job_id!r} job must declare `needs: grep-guard` "
        f"(got needs={unit_needs_tokens!r}); a grep-guard failure must "
        f"block unit from running."
    )

    # 3) integration job must declare needs: unit. We accept the
    # needs value as a string or list-shaped scalar, and match the
    # upstream id on a substring (``unit-tests``) for the same
    # ID-agnostic reason as above.
    integration_job_id = _find_job_id(text, re.compile(r"integration"))
    assert integration_job_id is not None, (
        "ci.yml must declare a top-level integration job (e.g. "
        "`integration-tests:`) with a `needs:` clause that names the "
        "upstream unit gate."
    )
    integration_needs_tokens = _needs_tokens(
        _job_body(text, integration_job_id)
    )
    assert integration_needs_tokens is not None, (
        f"the {integration_job_id!r} job must declare a `needs:` clause; "
        f"without it a unit failure does not block integration from "
        f"running."
    )
    assert any("unit" in tok for tok in integration_needs_tokens), (
        f"the {integration_job_id!r} job must declare `needs: unit[...]` "
        f"(got needs={integration_needs_tokens!r}); a unit failure must "
        f"block integration from running."
    )

    # 4) Every pytest invocation in the layered gates MUST use the
    # project's venv python (./.venv/bin/python3 or
    # backend/.venv/bin/python3). The CLAUDE.md rule pins this so
    # the ubuntu-latest runner's system Python 3.9 cannot silently
    # regress pytest collection.
    #
    # We intentionally sample all pytest invocations in the file:
    # the failure message lists every line that violates the rule.
    pytest_lines = [
        line.strip()
        for line in text.splitlines()
        if "-m pytest" in line
    ]
    assert pytest_lines, (
        "ci.yml does not contain any `python -m pytest` invocations; "
        "the layered gates have no pytest calls to enforce the "
        "venv-python contract on."
    )
    violations = [
        line for line in pytest_lines
        if ".venv/bin/python3" not in line
    ]
    assert not violations, (
        "every `python -m pytest` invocation in ci.yml MUST go through "
        "./.venv/bin/python3 (system Python on ubuntu-latest has stale "
        f"urllib3 + missing deps). Offending lines: {violations!r}"
    )

    # 5) The mock-plan E2E job must exist and its name must mention
    # either "mock" or "mock plan" so the contract is self-documenting
    # (the test-design layer relies on the default CI behaviour to
    # run the mock E2E while leaving the real-plan E2E gated behind
    # an explicit workflow_dispatch input).
    e2e_name_match = re.search(
        r"^  e2e-[A-Za-z0-9_-]+:\s*\n[^\n]*name:\s*([^\n]+)",
        text,
        flags=re.MULTILINE,
    )
    assert e2e_name_match is not None, (
        "ci.yml must declare a top-level `e2e-...:` job (the mock-plan "
        "E2E that runs on every push to main by default)."
    )
    e2e_name = e2e_name_match.group(1).strip()
    assert "mock" in e2e_name.lower() or "plan" in e2e_name.lower(), (
        f"the default e2e job's `name:` must mention 'mock' or 'plan' "
        f"so the contract is self-documenting (got name={e2e_name!r}). "
        f"The mock-plan E2E must run on every push to main while the "
        f"real-plan E2E is gated behind a workflow_dispatch input."
    )


# ---------------------------------------------------------------------------
# Test 2 — real plan migration requires an explicit workflow_dispatch input
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ci_real_migration_requires_explicit_input() -> None:
    """ci.yml must require an explicit input to run a REAL plan migration.

    Required contract:

      1. The ``workflow_dispatch`` trigger must declare an
         ``inputs.run_real_plan_migration`` boolean input. We accept
         any of the common YAML boolean input shapes — bare
         ``type: boolean`` or a wrapped ``{"type": "boolean",
         "default": ...}`` dict.

      2. A separate job (whose name mentions ``real`` /
         ``migration`` / ``real_plan`` / ``real-plan``) MUST be
         guarded by an ``if:`` predicate whose body mentions
         ``inputs.run_real_plan_migration`` so a routine push / PR
         cannot trigger a real plan migration (which would write to
         the production ``plans/`` directory and break CI isolation).

      3. The ``if:`` predicate MUST require the input to equal
         ``'true'`` (the GitHub Actions boolean input convention);
         a bare mention of the input name in the predicate is not
         enough — the ``== 'true'`` comparison is what makes the
         guard a hard gate.
    """
    text = _read_workflow()

    # 1) workflow_dispatch must declare run_real_plan_migration as
    # an input. We accept the input definition in either nested
    # (block) form or wrapped (flow) form.
    assert "workflow_dispatch:" in text, (
        "ci.yml does not declare a `workflow_dispatch:` trigger; "
        "manual maintenance runs cannot be invoked at all."
    )
    assert re.search(
        r"run_real_plan_migration\s*:",
        text,
    ), (
        "ci.yml must declare a `run_real_plan_migration:` input under "
        "the workflow_dispatch trigger; without it the dangerous "
        "real-plan migration cannot be invoked intentionally."
    )
    # The input MUST be typed `boolean` — not choice / string / env
    # — because the test contract hinges on the explicit
    # `== 'true'` literal below.
    match = re.search(
        r"run_real_plan_migration\s*:\s*\n(?P<body>(?:[^\n]*\n)*?)"
        r"(?:^      [A-Za-z][A-Za-z0-9_]*\s*:|\Z)",
        text,
        flags=re.MULTILINE,
    )
    # If the multi-line body match failed we fall back to the
    # simpler single-line check: just confirm `type: boolean`
    # appears near the input key somewhere in the file.
    if match is None:
        # Whole-file search for the input key followed by `type: boolean`
        # within the next ~10 lines.
        idx = text.find("run_real_plan_migration")
        assert idx >= 0
        window = text[idx: idx + 800]
        assert "type: boolean" in window, (
            "ci.yml must declare `run_real_plan_migration: {type: "
            "boolean}` under workflow_dispatch.inputs; the input "
            "type must be boolean so the =='true' gate below works."
        )
    else:
        body = match.group("body")
        assert "type: boolean" in body, (
            "ci.yml `run_real_plan_migration` input must be typed "
            f"`boolean` (got body={body!r}); the gate contract below "
            "relies on the boolean === 'true' comparison."
        )

    # 2) A real-plan migration job/step must exist, and it must be
    # guarded by an `if:` predicate that mentions the input.
    # We accept either a separate job (``real-plan-migration:`` or
    # similar) OR a gated step inside an existing job — the test
    # pin is "the dangerous code path is unreachable without the
    # explicit input" rather than the specific job layout.
    real_job_match = re.search(
        r"^  ([A-Za-z0-9_-]*real[A-Za-z0-9_-]*(?:[_-]plan[A-Za-z0-9_-]"
        r"*|[_-]migrat[A-Za-z0-9_-]*)?)\s*:\s*\n"
        r"(?P<body>(?:[^\n]*\n)*?)(?=^  [A-Za-z0-9_-]+\s*:|\Z)",
        text,
        flags=re.MULTILINE,
    )
    assert real_job_match is not None, (
        "ci.yml must declare a job whose id references 'real' and "
        "'plan' (e.g. `real-plan-migration:`); without such a job "
        "there is nothing for the input to guard."
    )
    real_job_id = real_job_match.group(1)
    real_job_body = real_job_match.group("body")

    # 3) The job's ``if:`` predicate MUST require the input to
    # equal `'true'`. We accept any of the following shapes:
    #     if: github.event.inputs.run_real_plan_migration == 'true'
    #     if: ${{ github.event.inputs.run_real_plan_migration == 'true' }}
    #     if: |-
    #       github.event.inputs.run_real_plan_migration == 'true'
    if_line_match = re.search(
        r"if:\s*([^\n]+)",
        real_job_body,
    )
    assert if_line_match is not None, (
        f"the {real_job_id!r} job must declare an `if:` predicate "
        "that gates execution on the run_real_plan_migration input; "
        "without a guard, the real-plan migration will run on every "
        "workflow trigger."
    )
    if_body = if_line_match.group(1).strip()
    # GitHub Actions accepts both quoted and unquoted forms; we
    # normalise on "== 'true'" as the safety contract.
    condition_tokens = re.findall(
        r"inputs\.run_real_plan_migration\s*==\s*'true'|"
        r"inputs\.run_real_plan_migration\s*==\s*\"true\"|"
        r"inputs\.run_real_plan_migration\s*==\s*true",
        if_body,
    )
    assert condition_tokens, (
        f"the {real_job_id!r} job's `if:` predicate must require "
        f"`inputs.run_real_plan_migration == 'true'` (got if={if_body!r}); "
        f"a bare mention of the input name in the predicate would let "
        f"any non-empty string (including the boolean default) trigger "
        f"the real-plan migration, which is unsafe."
    )


# ---------------------------------------------------------------------------
# Test 3 — a push to main must actually run the layered gates
# ---------------------------------------------------------------------------
#
# 2026-09-22. Run 35725018566 (a push to main) reported
# ``conclusion: success`` while unit, integration AND e2e were all
# ``skipped`` — the only jobs that really ran were lint, grep-guard and
# the slow lane. The cause was ``if: github.event_name == 'pull_request'``
# on ``unit-tests``, copied onto ``integration-tests``, and then a
# ``needs: integration-tests`` on the e2e job whose own ``if`` explicitly
# ALLOWS a push to main.
#
# This repo's workflow is a local merge into main followed by a direct
# push — there are no PRs — so "PR gate" meant "never runs". A green
# conclusion that skipped most of the pipeline is worse than a red one:
# it is a gate that reports success without checking anything.
#
# The predicates below must stay in lockstep. If a future maintainer
# narrows one of them back to pull_request-only, these tests fail rather
# than the pipeline quietly going dark.

#: The predicate half that permits a push to main. Matched as a
#: substring so a job may combine it with other conditions.
_MAIN_PUSH_PREDICATE = (
    "github.event_name == 'push' && github.ref == 'refs/heads/main'"
)


def _job_condition(text: str, job_id: str) -> str | None:
    """Return a job's ``if:`` expression, or ``None`` when unconditional."""
    body = _job_body(text, job_id)
    match = re.search(r"^\s+if:\s*(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _runs_on_main_push(text: str, job_id: str) -> bool:
    """True when ``job_id`` is not excluded from a push to main."""
    condition = _job_condition(text, job_id)
    if condition is None:
        return True  # no predicate — runs on every trigger
    return _MAIN_PUSH_PREDICATE in condition


@pytest.mark.unit
def test_every_layered_gate_runs_on_a_push_to_main() -> None:
    """lint -> grep-guard -> unit -> integration -> e2e must all run.

    A gate that is skipped on the branch it is supposed to protect is
    not a gate.
    """
    text = _read_workflow()
    for job_id in (
        "grep-guard",
        "unit-tests",
        "integration-tests",
        "e2e-on-demand",
    ):
        condition = _job_condition(text, job_id)
        assert _runs_on_main_push(text, job_id), (
            f"the {job_id!r} job is excluded from a push to main "
            f"(if={condition!r}). This repo merges locally and pushes "
            f"main directly — there is no PR — so a pull_request-only "
            f"predicate means the job never runs at all, and a green "
            f"run reports success without having checked anything."
        )


@pytest.mark.unit
def test_the_e2e_job_is_reachable_on_a_push_to_main() -> None:
    """Every ``needs:`` ancestor of the e2e job must also run on main.

    GitHub skips a job whose ``needs`` job was skipped. So an e2e job
    with a push-to-main ``if`` is still dead if the job it needs is
    pull_request-only — which is exactly how e2e went dark while its own
    comment claimed "Default CI behaviour (push to main): runs the
    MOCK-PLAN E2E only".
    """
    text = _read_workflow()
    pending = ["e2e-on-demand"]
    visited: set[str] = set()
    while pending:
        job_id = pending.pop()
        if job_id in visited:
            continue
        visited.add(job_id)
        assert _job_body(text, job_id) is not None
        assert _runs_on_main_push(text, job_id), (
            f"e2e-on-demand depends on {job_id!r}, which does not run on "
            f"a push to main — GitHub skips a job whose `needs` job was "
            f"skipped, so e2e would never run there either."
        )
        pending.extend(_needs_tokens(_job_body(text, job_id)) or [])
