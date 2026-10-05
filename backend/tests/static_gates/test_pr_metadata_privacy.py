"""The pull request's own text is a fourth privacy surface, and it is checked.

Why this gate exists
--------------------
Three surfaces were already covered and this is the fourth:

  * the tree, by the ``static_gates/`` rules;
  * the commits in a range, by ``commit_range_scan.py``;
  * commit messages, by ``check_commit_msg.py``.

A pull request contributes one more, and on this repository it is the one
that reaches the default branch: GitHub composes the merge commit message
from the **branch name**, the **title**, and the **body**, and that message
is a permanent public commit. None of the three gates above reads any of
those three strings. A branch named after an unrelated project, or a
description that pastes a local path, passes everything and lands verbatim.

Scope — and its limit
---------------------
The rules are the *same* pure functions the other three gates use
(``find_home_paths`` / ``find_attribution`` / ``find_local_measurements``),
imported rather than restated, so the four surfaces cannot disagree about
what a violation is. That is also the honest limit of this gate: it can
find a home path or an attribution phrase in a branch name, and it cannot
decide that a branch name *refers to another project* — the identifiers
that would decide that are themselves the leak. That case is a human
reading the pull request, which is the argument for checking here rather
than after the merge.

What these tests actually do
----------------------------
They **execute** the scanner, the way ``test_merge_gate_contract.py``
executes the merge gate's ``run:`` body. A string-matching test for a
gate is a test that can be satisfied by a gate that does nothing — that
is not hypothetical here: the merge-gate's own first contract test
asserted ``"exit 1" in source`` and stayed green after
``sys.exit(1)`` became ``sys.exit(0)``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import pr_metadata_scan
from pr_metadata_scan import MetadataScanError, fields_from_env, scan_text

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

JOB = "pr-metadata-privacy"

#: Fixture text is assembled from fragments so this file's own source never
#: contains a literal the tree gates forbid — ``/Users/<segment>/`` with a
#: non-placeholder segment, and the attribution phrases. Same reason
#: ``test_no_local_home_path_in_first_party`` builds its sample as
#: ``"/Use" + "rs/kai/work/repo"``: a self-referential scan would either
#: suppress the rule for this file (the gate stops catching its own
#: authors) or fail the suite on the gate's own definition.
_HOME = "/Use" + "rs/jdoe/dev/"
_ATTRIB_CN = "用户" + "原话"


def _home_path(suffix: str) -> str:
    return _HOME + suffix


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run(*env: str) -> subprocess.CompletedProcess:
    """Run the scanner as a subprocess, exactly as the CI job does.

    ``env`` is applied on top of a deliberately minimal environment: the
    point of several tests below is what happens when a variable is
    *absent*, so the ambient environment must not be able to supply one.
    """
    child = {k: v for k, v in os.environ.items() if not k.startswith("PR_")}
    for pair in env:
        key, _, value = pair.partition("=")
        child[key] = value
    return subprocess.run(
        [sys.executable, str(Path(pr_metadata_scan.__file__))],
        capture_output=True,
        text=True,
        env=child,
        check=False,
    )


# ---------------------------------------------------------------------------
# The gate finds things
# ---------------------------------------------------------------------------


class TestItFindsViolations:
    def test_a_home_path_in_the_body_is_a_violation(self) -> None:
        findings = scan_text(
            "pull_request/body",
            "复现步骤见 " + _home_path("widget/main.py"),
        )
        assert findings, (
            "a local home path pasted into a pull request body is "
            "composed into the merge commit message; the body has to be "
            "scanned like any other first-party text"
        )

    def test_an_attribution_phrase_in_the_title_is_a_violation(self) -> None:
        findings = scan_text("pull_request/title", "refactor: " + _ATTRIB_CN + ": 保留该字段")
        assert findings, (
            "the pull request title lands verbatim in the merge commit "
            "message, so the attribution rule has to reach it"
        )

    def test_a_violation_in_the_branch_name_is_reported(self) -> None:
        """The branch name is the half GitHub always puts in the message.

        ``Merge pull request #27 from <owner>/<branch>`` — there is no
        shape of merge where that line is absent.
        """
        findings = scan_text("pull_request/head.ref", "fix/" + _ATTRIB_CN)
        assert findings, (
            "the branch name is unconditionally part of the merge commit "
            "message, so it is the one field that can never be omitted"
        )

    def test_the_field_is_named_in_the_finding(self) -> None:
        """A finding that does not say which field failed is unactionable.

        The fix is to rename the branch or retitle the pull request —
        a message that only says "violation" sends the reader to the
        wrong place.
        """
        findings = scan_text("pull_request/title", "fix: " + _ATTRIB_CN + " 保留")
        assert findings
        assert all(f.field == "pull_request/title" for f in findings)
        assert "pull_request/title" in findings[0].format()


# ---------------------------------------------------------------------------
# Clean input stays clean
# ---------------------------------------------------------------------------


class TestItDoesNotFireOnOrdinaryProse:
    @pytest.mark.parametrize(
        "field,text",
        [
            ("pull_request/head.ref", "docs/audit-state-the-reason"),
            ("pull_request/head.ref", "fix/killpg-sentinel-cleanup-v2"),
            ("pull_request/title", "ci: 加一个真的会拦的门禁"),
            ("pull_request/title", "fix(task_manager): 跳过写入的日志只留 SHA 前缀"),
            ("pull_request/body", "把断言被绕过的经过改成它成立的理由。"),
            ("pull_request/body", ""),
            # Placeholder paths illustrate a shape; they identify no
            # machine, so they are not a leak. The home-path rule has
            # always allowed these, and this gate inherits that — the
            # segment allowlist includes the obvious generic names, so
            # a sample written with `someone` in it is clean by design
            # and a test that assumes otherwise is testing the wrong
            # thing.
            ("pull_request/body", "路径形如 /Users/a/b 与 /home/x/y.py"),
            ("pull_request/body", "路径形如 /Users/someone/dev/x.py"),
            ("pull_request/title", "docs: 复现路径 /home/username/proj"),
        ],
    )
    def test_ordinary_text_produces_no_findings(self, field: str, text: str) -> None:
        assert not scan_text(field, text), (
            f"ordinary text in {field} produced a finding; a gate that "
            f"fires on every pull request is a gate that gets disabled"
        )


# ---------------------------------------------------------------------------
# The rules are shared, not restated
# ---------------------------------------------------------------------------


class TestTheRulesAreTheOnesTheOtherGatesUse:
    def test_it_imports_the_gate_modules_rather_than_rewriting_them(self) -> None:
        for label, rule in pr_metadata_scan.METADATA_RULES:
            module = getattr(rule, "__module__", "")
            assert module.startswith("test_"), (
                f"the {label!r} rule came from {module!r}, not from one of "
                f"the gate modules; a restated rule is a second definition "
                f"that will drift from the first"
            )

    def test_the_rule_set_is_exactly_the_three_existing_ones(self) -> None:
        labels = [label for label, _ in pr_metadata_scan.METADATA_RULES]
        assert labels == ["home path", "operator attribution", "local measurement"], (
            f"the metadata rules changed to {labels}; commit_range_scan "
            f"deliberately mirrors this list, so a change belongs in both"
        )


# ---------------------------------------------------------------------------
# Failure is loud
# ---------------------------------------------------------------------------


class TestAPlumbingFailureIsNotACleanScan:
    def test_a_missing_required_variable_raises(self) -> None:
        with pytest.raises(MetadataScanError):
            fields_from_env({})

    def test_the_error_names_the_variable_and_the_fix(self) -> None:
        try:
            fields_from_env({"PR_TITLE": "x", "PR_BODY": "y"})
        except MetadataScanError as exc:
            message = str(exc)
        else:  # pragma: no cover - the call above must raise
            pytest.fail("fields_from_env accepted a payload with no PR_HEAD_REF")
        assert "PR_HEAD_REF" in message
        assert "head.ref" in message, (
            "the error has to name the GitHub context expression to "
            "insert, not just the variable"
        )

    def test_a_set_but_empty_body_is_not_an_error(self) -> None:
        """An empty pull request body is ordinary, not a broken pipeline.

        Unset means the ``env:`` block is broken; set-and-empty means the
        author wrote nothing. Only the first may be an error.
        """
        fields = fields_from_env(
            {"PR_HEAD_REF": "fix/x", "PR_TITLE": "t", "PR_BODY": ""}
        )
        assert len(fields) == 3

    def test_a_missing_optional_body_is_skipped_not_fatal(self) -> None:
        fields = fields_from_env({"PR_HEAD_REF": "fix/x", "PR_TITLE": "t"})
        assert [label for label, _ in fields] == [
            "pull_request/head.ref",
            "pull_request/title",
        ]


# ---------------------------------------------------------------------------
# Exit contract — 0 clean, 1 violations, 2 could not run
# ---------------------------------------------------------------------------


class TestTheExitContract:
    def test_clean_input_exits_zero(self) -> None:
        result = _run(
            "PR_HEAD_REF=docs/audit",
            "PR_TITLE=docs: 加 CHANGELOG",
            "PR_BODY=补充说明。",
        )
        assert result.returncode == 0, result.stderr

    def test_a_violation_exits_one_and_prints_the_field(self) -> None:
        result = _run(
            "PR_HEAD_REF=fix/x",
            "PR_TITLE=t",
            "PR_BODY=见 " + _home_path("x.py"),
        )
        assert result.returncode == 1, result.stdout
        assert "pull_request/body" in result.stdout

    def test_unset_variables_exit_two_rather_than_zero(self) -> None:
        """The failure mode this whole module is built around.

        A renamed step or a dropped ``env:`` block used to be the shape
        that produced a green gate that had checked nothing. Exit 2 keeps
        "the scan did not run" distinguishable from "the scan found
        nothing" — the same split ``scan_commit_range.sh`` makes.
        """
        result = _run()
        assert result.returncode == 2, (
            f"an unset environment returned {result.returncode}, not 2; "
            f"the caller cannot tell a broken scan from a clean one"
        )
        assert "PR_HEAD_REF" in result.stderr

    def test_a_malformed_text_argument_exits_two(self) -> None:
        result = subprocess.run(
            [sys.executable, str(Path(pr_metadata_scan.__file__)), "--text", "no-equals"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# The CI job is wired the way the module expects
# ---------------------------------------------------------------------------


class TestTheJobIsWiredUp:
    def test_the_job_exists(self) -> None:
        jobs = _workflow()["jobs"]
        assert JOB in jobs, (
            f"ci.yml has no `{JOB}` job; the pull request's branch name "
            f"and title reach main through the merge commit message and "
            f"nothing else scans them"
        )

    def test_it_runs_on_pull_requests(self) -> None:
        condition = str(_workflow()["jobs"][JOB].get("if", ""))
        assert "pull_request" in condition, (
            "without a pull_request trigger the job has no pull request to "
            "read, and the scanner would exit 2 on every run"
        )

    def test_the_merge_gate_waits_for_it(self) -> None:
        """A gate outside the merge gate is unmonitored.

        Same reason every other job is in ``needs``: a red job nothing
        closes on is a job that gets ignored.
        """
        needed = _workflow()["jobs"]["merge-gate"].get("needs") or []
        assert JOB in needed, (
            f"`{JOB}` is not in the merge gate's needs; it can go red "
            f"without closing anything"
        )

    def test_it_runs_before_the_shards(self) -> None:
        """It costs seconds — no checkout of the base, no test run.

        The matrix is twenty-odd runner VMs; starting them and only then
        discovering the pull request title was a leak is the expensive
        ordering.
        """
        needs = _workflow()["jobs"]["unit-tests"].get("needs") or []
        assert JOB in needs

    def test_the_env_is_wired_to_the_pull_request_context(self) -> None:
        """The three variables the module requires, from the right place.

        This is the assertion that would have caught a job that exists,
        runs, and scans nothing.
        """
        env = _workflow()["jobs"][JOB]["steps"][-1]["env"]
        assert env["PR_HEAD_REF"] == "${{ github.event.pull_request.head.ref }}"
        assert env["PR_TITLE"] == "${{ github.event.pull_request.title }}"
        assert env["PR_BODY"] == "${{ github.event.pull_request.body }}"
