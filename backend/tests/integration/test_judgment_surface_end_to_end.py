"""End-to-end acceptance of the judgment-surface gates.

Why this file exists
--------------------
The judgment surface is described by artifacts that are allowed to drift
apart, and a change to it has to be written down:

* the **manifest** (``docs/judgment_points.json``) — which functions
  decide PASS/FAIL, and what pins each of them;
* the **decision tables** the manifest's Tier A rows name;
* the **behavioural goldens**
  (``backend/tests/fixtures/judgment_snapshots.json``) — what those
  functions actually return, frozen byte for byte;
* the **changelog** — a judgment-surface change has to say what it
  changed.

Four commands make that chain real:

* ``scripts/census_judgment_points.py --check`` — the manifest against
  the census of the tree it was generated from;
* ``scripts/judgment_snapshots.py --check`` — the goldens against what
  the code produces today;
* ``scripts/ci_judgment_surface.sh`` — the three checks behind one
  entry point;
* ``scripts/ci_changelog_gate.py`` — the changelog entry a
  judgment-surface change owes.

Each of them is a *file* until something runs it, and each of them can
report green while the thing it checks has rotted.  The static gates
under ``backend/tests/static_gates/`` read the script and the workflow
file and assert on their structure; this file runs the commands, so the
question answered here is a different one: does the chain still work
when it is *run*, and can each of its checks still go red?

Scope, stated honestly
----------------------
The census check is **advisory** in this version of the work: a manifest
that does not match the code is reported and does not block — that is
the decision ``scripts/ci_judgment_surface.sh`` documents and the
reason its summary line has three forms.  So this file does not assert
that the repository's own manifest is clean; it asserts that the check
*decides*, that its reason is in the log rather than only in the exit
code, and that the entry point keeps such a drift advisory instead of
blocking on it.

Everything below runs a real command line with a real interpreter
against a real tree.  The negative controls change bytes and read them
back — they never assert against a double, because a double cannot
answer the question this file asks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

#: The repository root — three levels up from
#: ``backend/tests/integration/``.  Derived from this file's own
#: location, because a path written down is a path that is wrong on
#: somebody else's machine.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The manifest checker — the manifest's own drift check.
CENSUS = REPO_ROOT / "scripts" / "census_judgment_points.py"

#: The golden checker — the behavioural snapshots' drift check.
SNAPSHOTS = REPO_ROOT / "scripts" / "judgment_snapshots.py"

#: The one entry point that runs the three judgment-surface checks.
SURFACE = REPO_ROOT / "scripts" / "ci_judgment_surface.sh"

#: The changelog gate — a judgment-surface change owes an entry.
CHANGELOG_GATE = REPO_ROOT / "scripts" / "ci_changelog_gate.py"

#: The workflow the two gates are wired into.
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The manifest the repository carries — what ``--check`` compares by
#: default.
MANIFEST = REPO_ROOT / "docs" / "judgment_points.json"

#: The goldens the repository carries.
SNAPSHOT = (
    REPO_ROOT / "backend" / "tests" / "fixtures" / "judgment_snapshots.json"
)

#: How long one command may take.  Every command here is seconds of
#: work against a local tree; the bound is there so that a wedged
#: command fails the test instead of hanging the lane.
TIMEOUT = 180

#: The job that runs the changelog gate.
CHANGELOG_JOB = "changelog-gate"

#: The job that runs the judgment-surface entry point.
SURFACE_JOB = "judgment-surface-consistency"

#: The terminal job that decides whether a pull request may merge.
MERGE_GATE_JOB = "merge-gate"

#: The line the entry point prints when no blocking check failed.  It
#: has three forms — ``OK``, ``WARN`` (an advisory drift) and ``FAILED``
#: — and the first two both mean "this tree is consistent enough to
#: merge".
SUMMARY_LINE = re.compile(
    r"^JUDGMENT SURFACE: (OK|WARN|FAILED) \((\d+)/(\d+) checks", re.MULTILINE
)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command and capture what it said.

    The command is bounded by :data:`TIMEOUT`, and both streams are
    captured rather than inherited: the reason a gate failed is in its
    output, and a test that cannot show that output cannot say why it
    failed.
    """
    return subprocess.run(
        command,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env=env,
    )


def _transcript(completed: subprocess.CompletedProcess[str]) -> str:
    """The command, its exit code and everything it printed."""
    return (
        f"$ {' '.join(str(part) for part in completed.args)}\n"
        f"exit {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}\n"
    )


@pytest.fixture(scope="module")
def golden_groups() -> list[str]:
    """The golden groups the checker has registered.

    The list comes from the checker's own ``--list``, so a group added
    later is covered by the tests below without this file being edited
    — and so that "every group was compared" is a statement about the
    checker's registry rather than about a list copied here.
    """
    listed = _run(
        [sys.executable, str(SNAPSHOTS), "--list", "--root", str(REPO_ROOT)]
    )
    assert listed.returncode == 0, (
        "the golden checker could not list its groups, so the tests "
        "cannot say whether every group was compared.\n"
        f"{_transcript(listed)}"
    )
    groups = json.loads(listed.stdout)
    assert groups, (
        "the golden checker registered no group at all. The goldens are "
        "the only artifact in the chain that says what the judgment "
        "functions return; with no group, nothing is pinned."
    )
    return groups


def _copy_backend(destination: Path) -> Path:
    """A copy of the backend tree, without the virtual environment.

    The copy is what lets the golden checker be run against a tree
    whose goldens differ: the checker reads the modules *and* the
    snapshot file out of one root, and it refuses a module that did not
    come from that root — so the modules have to be real files under
    the root, not links to another checkout's.

    ``.venv`` is excluded on purpose: it is a link to another
    checkout's environment, and copying what it points at would copy
    hundreds of megabytes of an interpreter this test does not use.
    """
    shutil.copytree(
        REPO_ROOT / "backend",
        destination,
        ignore=shutil.ignore_patterns(
            ".venv",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".ruff_cache",
            "logs",
            "e2e-verification-state",
        ),
    )
    return destination


@pytest.mark.integration
def test_manifest_check_end_to_end(tmp_path: Path) -> None:
    """The manifest checker decides, and its reason is in the log.

    Three things have to hold, and each of them is a way the check can
    be present but useless:

    * it **runs** against this repository, and says which manifest it
      compared — the path is in the log, so a reader can tell what was
      checked;
    * it **reports** its verdict in words, not only in an exit code —
      a CI log that carries a number and no reason is a log nobody can
      act on;
    * its exit code is **load-bearing**: a manifest that differs from
      the census has to be reported as a difference, and a manifest
      that matches has to be reported as a match.

    The last one is the negative control.  It runs against a manifest
    the test owns — generated by the census itself, so it matches by
    construction — and then changes one judgment point's tier in it.
    A checker that compared nothing would report the tampered manifest
    as clean, and the test would go red.

    The repository's own manifest is deliberately **not** the file the
    negative control tampers with.  The census drift in this tree is
    advisory by decision, the tree is shared with other work, and
    ``--out`` makes the same comparison against a file this test
    owns.  What the real tree's check is asserted to be is a *verdict*:
    ``0`` for clean, ``1`` for drift, and never the ``2`` that means
    the census could not be run at all.
    """
    completed = _run(
        [sys.executable, str(CENSUS), "--check", "--root", str(REPO_ROOT)]
    )
    assert completed.returncode in (0, 1), (
        "the manifest check did not decide. Exit 0 means the manifest "
        "matches the census, exit 1 means it drifted, and exit 2 means "
        "the census could not be run at all — the third is a broken "
        "checker, not a verdict about this tree.\n"
        f"{_transcript(completed)}"
    )
    verdict = completed.stdout + completed.stderr
    if completed.returncode == 0:
        assert "clean:" in verdict, (
            "the manifest check exited 0 without saying that the manifest "
            "is clean. The reason has to be in the log, not only in the "
            f"exit code.\n{_transcript(completed)}"
        )
    else:
        assert "DRIFT:" in verdict, (
            "the manifest check exited 1 without saying that the "
            "manifest drifted. A CI log that carries a number and no "
            f"reason is a log nobody can act on.\n{_transcript(completed)}"
        )
    assert str(MANIFEST) in verdict, (
        f"the manifest check did not report on {MANIFEST}. The check has "
        "to say which manifest it compared — otherwise a reader cannot "
        "tell whether the file it drifted from is the one in the "
        f"repository.\n{_transcript(completed)}"
    )

    # --- the negative control ---------------------------------------
    manifest = tmp_path / "judgment_points.json"
    written = _run(
        [
            sys.executable,
            str(CENSUS),
            "--write",
            "--out",
            str(manifest),
            "--root",
            str(REPO_ROOT),
        ]
    )
    assert written.returncode == 0, (
        "the census could not write a manifest of its own, so nothing "
        "below can prove that the check compares anything.\n"
        f"{_transcript(written)}"
    )
    assert "wrote" in written.stdout, (
        "the census wrote a manifest without saying that it wrote one.\n"
        f"{_transcript(written)}"
    )
    original = manifest.read_bytes()

    checked = _run(
        [
            sys.executable,
            str(CENSUS),
            "--check",
            "--out",
            str(manifest),
            "--root",
            str(REPO_ROOT),
        ]
    )
    assert checked.returncode == 0, (
        "a manifest the census had just written was reported as "
        "drifted. The check compares the file with the census of the "
        "tree, and those two are the same thing by construction.\n"
        f"{_transcript(checked)}"
    )
    assert "clean:" in checked.stdout, (
        "the manifest check exited 0 without saying that the manifest "
        f"is clean.\n{_transcript(checked)}"
    )

    # One judgment point now claims a different tier.  The text is
    # rendered the way the census renders it — compact separators, one
    # trailing newline — so that the tier is the *only* difference
    # between the two files.
    document = json.loads(original.decode("utf-8"))
    points = document["points"]
    assert points, (
        "the census wrote a manifest with no judgment point in it, so "
        "there is nothing for the check to compare."
    )
    points[0]["tier"] = "X" if points[0]["tier"] != "X" else "Y"
    manifest.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    drifted = _run(
        [
            sys.executable,
            str(CENSUS),
            "--check",
            "--out",
            str(manifest),
            "--root",
            str(REPO_ROOT),
        ]
    )
    assert drifted.returncode == 1, (
        "the manifest check did not go red on a manifest whose judgment "
        "point claims a different tier. A checker that cannot fail is "
        "not a check, and one that prints the success line no matter "
        f"what it found is worse than not running at all.\n"
        f"{_transcript(drifted)}"
    )
    assert "DRIFT:" in drifted.stdout, (
        "the manifest check went red without saying that the manifest "
        f"drifted.\n{_transcript(drifted)}"
    )

    manifest.write_bytes(original)
    restored = _run(
        [
            sys.executable,
            str(CENSUS),
            "--check",
            "--out",
            str(manifest),
            "--root",
            str(REPO_ROOT),
        ]
    )
    assert restored.returncode == 0, (
        "the manifest check stayed red after the manifest's original "
        "bytes were restored. The red had to be the tamper's doing, and "
        "the check has to be able to come back.\n"
        f"{_transcript(restored)}"
    )
    assert "clean:" in restored.stdout, (
        "the manifest check came back green without saying that the "
        f"manifest is clean again.\n{_transcript(restored)}"
    )


@pytest.mark.integration
def test_snapshot_check_end_to_end(tmp_path: Path, golden_groups: list[str]) -> None:
    """The golden checker agrees with this tree, and can disagree.

    The goldens are the only artifact in the chain that says what the
    judgment functions *return*.  A golden that no longer matches the
    code is the one failure a reader cannot notice by reading the
    diff: the code changed, the golden did not, and every other gate
    is still green.

    So this asserts three things:

    * every registered group is checked, and every one of them agrees
      — the group list comes from the checker's own ``--list``, so a
      group added later is covered without this file being edited;
    * the agreement is said out loud, group by group, so a reader of
      the log can see which golden was compared;
    * the check **can go red**: on a copy of the tree whose goldens
      were changed, the checker reports the group that differs, and
      after the original bytes are restored it is green again.

    The negative control runs against a *copy* of the tree, because the
    checker reads the modules and the goldens out of one root and
    refuses a module that came from anywhere else — a copy is what
    makes "the goldens differ" expressible without touching the
    repository this test shares with other work.
    """
    completed = _run(
        [sys.executable, str(SNAPSHOTS), "--check", "--root", str(REPO_ROOT)]
    )
    assert completed.returncode == 0, (
        "the goldens do not match what the code produces today. A golden "
        "that no longer matches the code is the one failure a reader "
        "cannot notice by reading the diff.\n"
        f"{_transcript(completed)}"
    )
    for name in golden_groups:
        assert f"一致：{name}" in completed.stdout, (
            f"the golden checker did not report group {name!r} as "
            "consistent. Every registered group has to be compared, and "
            "the answer has to be in the log.\n"
            f"{_transcript(completed)}"
        )

    # --- the negative control ---------------------------------------
    assert SNAPSHOT.is_file(), (
        f"{SNAPSHOT} does not exist, so there are no goldens for the "
        "checker to compare — and the check above would have said "
        "nothing about this tree."
    )

    tree = tmp_path / "tree"
    tree.mkdir()
    _copy_backend(tree / "backend")

    baseline = _run(
        [sys.executable, str(SNAPSHOTS), "--check", "--root", str(tree)]
    )
    assert baseline.returncode == 0, (
        "the goldens of a copy of this tree do not match the copy. The "
        "copy is the same tree, so this is the checker disagreeing "
        "with itself.\n"
        f"{_transcript(baseline)}"
    )

    snapshot = tree / "backend" / "tests" / "fixtures" / "judgment_snapshots.json"
    original = snapshot.read_bytes()
    assert original == SNAPSHOT.read_bytes(), (
        "the copy's goldens are not the repository's goldens, so the "
        "negative control below would prove something about a file "
        "nobody ships."
    )
    document = json.loads(original.decode("utf-8"))
    group = next(
        (
            name
            for name in golden_groups
            if (document.get(name) or {}).get("cases")
        ),
        None,
    )
    assert group is not None, (
        f"no registered group {golden_groups!r} holds a case, so there "
        "is no golden to change."
    )
    document[group]["cases"][0]["out"] = "TAMPERED"
    snapshot.write_text(
        json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    drifted = _run(
        [sys.executable, str(SNAPSHOTS), "--check", "--root", str(tree)]
    )
    assert drifted.returncode == 1, (
        f"the golden checker did not go red on a tree whose {group!r} "
        "golden had been changed. A checker that cannot fail is not a "
        "check.\n"
        f"{_transcript(drifted)}"
    )
    assert f"不一致：{group}" in drifted.stdout, (
        f"the golden checker went red without naming the group {group!r} "
        "that differs. The reason has to be in the log, not only in the "
        f"exit code.\n{_transcript(drifted)}"
    )
    assert "TAMPERED" in drifted.stdout, (
        "the golden checker reported the difference without printing the "
        "differing line, so a reader still cannot see what changed.\n"
        f"{_transcript(drifted)}"
    )

    snapshot.write_bytes(original)
    restored = _run(
        [sys.executable, str(SNAPSHOTS), "--check", "--root", str(tree)]
    )
    assert restored.returncode == 0, (
        "the golden checker stayed red after the goldens' original bytes "
        "were restored. The red had to be the change's doing, and the "
        "checker has to be able to come back.\n"
        f"{_transcript(restored)}"
    )
    assert f"一致：{group}" in restored.stdout, (
        f"the golden checker came back green without reporting group "
        f"{group!r} as consistent again.\n{_transcript(restored)}"
    )


@pytest.mark.integration
def test_ci_judgment_surface_entrypoint_end_to_end(golden_groups: list[str]) -> None:
    """The one entry point runs the three checks and answers with one number.

    ``scripts/ci_judgment_surface.sh`` exists because three separate
    checkers are three separate ways for the chain to rot: a link can
    stop running while the other two keep reporting green, and the
    reader of a CI log has to know which of the three to believe.  So
    the script runs all three — the census, the three static gates and
    the goldens — in order, and answers with one number.

    This asserts that the answer is a number that means something:

    * the script exits ``0`` — no *blocking* check failed;
    * it ran all three checks, and the log says which is which;
    * each of the three checkers *really ran* — the census's own
      verdict, the static gates' own pytest summary and the goldens'
      own per-group verdicts are all in the log.  A section heading is
      not a run: a script that prints the heading and runs nothing
      under it is the rot this entry point exists to prevent;
    * its summary line says how many of the three passed, so the
      verdict cannot claim a check passed when it did not;
    * an advisory drift stays advisory: the census is reported and
      does not close the gate.

    The last one is why the summary line has three forms.  A manifest
    that does not match the code warns in this version of the work and
    does not block, so the tree's verdict may legitimately be ``WARN``
    rather than ``OK`` — and it may equally be ``OK`` once the manifest
    catches up.  What must never happen is ``FAILED``, or a drift that
    closes the gate anyway.
    """
    completed = _run(
        ["bash", str(SURFACE), "--root", str(REPO_ROOT)],
        env={**os.environ, "PYTHON": sys.executable},
    )
    assert completed.returncode == 0, (
        "the judgment-surface entry point did not exit 0 against this "
        "tree. Exit 0 is the contract: no blocking check failed. An "
        "advisory drift is reported and does not change it.\n"
        f"{_transcript(completed)}"
    )

    for label in (
        "=== judgment-point census ===",
        "=== judgment static gates (manifest / tables / coverage) ===",
        "=== behavioural goldens ===",
    ):
        assert label in completed.stdout, (
            f"the entry point did not run {label!r}. Three checkers are "
            "three ways for the chain to rot: a link can stop running "
            "while the other two keep reporting green.\n"
            f"{_transcript(completed)}"
        )

    # The census really ran: its own verdict is in the log.
    assert re.search(
        r"^(clean|DRIFT):", completed.stdout, re.MULTILINE
    ), (
        "the entry point's log carries no census verdict. The census is "
        "the manifest's own drift check, and it says what it found on "
        "a line of its own — a section heading is not a run.\n"
        f"{_transcript(completed)}"
    )

    # The static gates really ran: their own pytest summary is in the log.
    assert re.search(r"\d+ passed", completed.stdout), (
        "the entry point's log carries no pytest summary, so the "
        "judgment static gates did not run under it. They are test "
        "*files*: nothing but a pytest run executes them.\n"
        f"{_transcript(completed)}"
    )

    # The goldens really ran: every group's own verdict is in the log.
    for name in golden_groups:
        assert f"一致：{name}" in completed.stdout, (
            f"the entry point's log does not carry the golden checker's "
            f"verdict for group {name!r}. The goldens are the check that "
            "says what the judgment functions return, and they did not "
            "run under the entry point.\n"
            f"{_transcript(completed)}"
        )

    summary = SUMMARY_LINE.search(completed.stdout + completed.stderr)
    assert summary is not None, (
        "the entry point's summary line is missing or malformed. It is "
        "the line a reader — the CI log, a developer at a terminal — "
        "takes the verdict from, and it has to say how many of the "
        f"three checks passed.\n{_transcript(completed)}"
    )
    verdict, passed, total = summary.group(1), summary.group(2), summary.group(3)
    assert verdict != "FAILED", (
        "the entry point reported FAILED while exiting 0. The two "
        "answers have to agree.\n"
        f"{_transcript(completed)}"
    )
    assert total == "3", (
        f"the entry point reported {total} checks instead of 3. The "
        "summary line counts the checks that ran, so a count of "
        "something else means a check did not run.\n"
        f"{_transcript(completed)}"
    )
    if verdict == "WARN":
        assert "ADVISORY: reported, not blocking" in completed.stderr, (
            "the entry point reported a warning without saying that the "
            "drift was advisory. A drift says 'the manifest may be out "
            "of date'; a red table gate says 'the manifest names a "
            "table that is not there'. The two are not the same "
            f"thing.\n{_transcript(completed)}"
        )
    assert passed == total or verdict == "WARN", (
        f"the entry point reported {passed}/{total} checks passed while "
        "exiting 0. Only the advisory check may be short of its count, "
        f"and then the verdict says WARN.\n{_transcript(completed)}"
    )


@pytest.mark.integration
def test_ci_wiring_end_to_end(tmp_path: Path) -> None:
    """Both gates run on the pull request, and the merge gate waits for them.

    A gate nobody runs is a file in a repository and nothing else; a
    gate that runs but is outside the merge gate's ``needs`` is a red
    badge that closes no gate.  Both are the failure this test is
    written against, and both are asserted on the workflow file itself:
    ``changelog-gate`` and ``judgment-surface-consistency`` exist, each
    of them runs its script, and the merge gate waits for both.

    And because a workflow file is a *description* of a run, the
    changelog chain is also run for real — in a throw-away repository
    that contains a judgment-surface change and nothing else.  A
    judgment-surface change with no changelog entry has to be refused
    with the reason in the log; the same change with an entry that
    names the file it touched has to pass.  That is the fourth link of
    the chain, and it is the one that cannot be checked by reading
    YAML.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs") or {}

    needs = jobs.get(MERGE_GATE_JOB, {}).get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    for job in (CHANGELOG_JOB, SURFACE_JOB):
        assert job in jobs, (
            f"ci.yml has no `{job}` job, so that gate never runs in CI. "
            "A gate nobody runs is a file in a repository and nothing "
            "else."
        )
        assert job in needs, (
            f"the merge gate needs {needs!r}, which does not include "
            f"`{job}`. The job would run on every pull request and its "
            "failure would block nothing — a red badge that closes no "
            "gate is a badge nobody has to fix."
        )

    for job, script in (
        (CHANGELOG_JOB, "scripts/ci_changelog_gate.py"),
        (SURFACE_JOB, "scripts/ci_judgment_surface.sh"),
    ):
        runs = " ".join(
            str(step.get("run", ""))
            for step in (jobs[job].get("steps") or [])
            if isinstance(step, dict)
        )
        assert script in runs, (
            f"the `{job}` job does not run {script}. The job is the "
            "thing that runs the gate; a job that does not call it gates "
            "nothing, however green it is."
        )

    # --- the changelog chain, run for real ---------------------------
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "verification_evidence.py").write_text(
        "# the judgment, before\n", encoding="utf-8"
    )
    for name in ("CHANGELOG.md", "CHANGELOG.zh.md"):
        (repo / name).write_text(
            "# Changelog\n\n## [Unreleased]\n\n### 判定语义变更\n\n"
            "- 无\n\n## [0.1.0]\n",
            encoding="utf-8",
        )

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "judgment-surface-e2e",
        "GIT_AUTHOR_EMAIL": "judgment-surface-e2e@example.com",
        "GIT_COMMITTER_NAME": "judgment-surface-e2e",
        "GIT_COMMITTER_EMAIL": "judgment-surface-e2e@example.com",
    }

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=git_env,
        )
        assert result.returncode == 0, (
            f"git {' '.join(args)} failed in the throw-away repository.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n"
        )
        return result.stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")

    # A judgment-surface change, and no changelog entry for it.
    (repo / "backend" / "verification_evidence.py").write_text(
        "# the judgment, after\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "a judgment change")

    refused = _run(
        [
            sys.executable,
            str(CHANGELOG_GATE),
            "--base",
            base,
            "--root",
            str(repo),
        ],
        cwd=repo,
        env=git_env,
    )
    assert refused.returncode == 1, (
        "the changelog gate accepted a judgment-surface change that "
        "carries no changelog entry. That is the accident this gate "
        "was written for: a judgment changed, the changelog said "
        "nothing, and four rounds judged by the old rule.\n"
        f"{_transcript(refused)}"
    )
    assert "CHANGELOG GATE: FAIL" in refused.stdout, (
        "the changelog gate refused the change without saying why. The "
        "reason is printed on one line so that a CI log shows what the "
        f"gate wanted.\n{_transcript(refused)}"
    )

    # The same change, with an entry that names the file it touched.
    for name in ("CHANGELOG.md", "CHANGELOG.zh.md"):
        text = (repo / name).read_text(encoding="utf-8")
        (repo / name).write_text(
            text.replace(
                "- 无\n",
                "- `backend/verification_evidence.py`: 判定语义变更\n",
            ),
            encoding="utf-8",
        )
    git("add", "-A")
    git("commit", "-q", "-m", "the entry")

    accepted = _run(
        [
            sys.executable,
            str(CHANGELOG_GATE),
            "--base",
            base,
            "--root",
            str(repo),
        ],
        cwd=repo,
        env=git_env,
    )
    assert accepted.returncode == 0, (
        "the changelog gate refused a judgment-surface change that "
        "carries an entry naming the file it touched. The entry has to "
        "be auditable with `git log` afterwards, and one that names the "
        f"file is.\n{_transcript(accepted)}"
    )
    assert "CHANGELOG GATE: PASS" in accepted.stdout, (
        "the changelog gate accepted the change without saying so in the "
        f"log.\n{_transcript(accepted)}"
    )
