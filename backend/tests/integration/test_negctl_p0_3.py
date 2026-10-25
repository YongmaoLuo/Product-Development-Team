"""2026-10-22 (P0-3): the negative control that proves the fix was needed.

The bug
-------
The verification loop used to end the chain the moment the same failure
repeated: ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` was ``1``, and the
same-failure branch returned ``repair_tasks=[]`` — a terminal verdict
with no repair path and no reason a human could act on. The loop could
not tell a broken criterion (a human must fix the check) from a wrong
deliverable (the work must be redone), so it took the former for the
latter and killed the chain.

The fix raised the threshold to ``2`` **and** turned the cap into a
semantic triage: reaching it buys one ``VPTriageJudge`` call, and the
triage's verdict decides between stopping and diverting into the repair
path.

Two claims, one of which is easy to leave on self-report
---------------------------------------------------------
"The fix works" is half the claim, and the easy half: the behavioural
suite (``tests/integration/test_same_failure_triage.py``) pins the four
contracts that decide whether the triage may act at all.

The other half is "the bug was there". Without it the fix could be a
no-op and the behavioural suite would still be green — a suite that
passes on both the broken and the fixed code measures nothing about
the fix. The negative control is the piece that closes that gap: it
reads the PRE-FIX source out of git and shows that it really did
terminate at the cap with an empty ``repair_tasks`` and no triage, so
the behavioural difference the suite measures is a difference that
existed to begin with.

``scripts/negctl_p0_3.sh`` is that control. This module is its TDD
spec:

  * ``test_negctl_p0_3_passes`` — the control is established: the
    baseline really shows the pre-fix shape, the current source shows
    the fixed shape, and the script says PASS.
  * ``test_negctl_p0_3_detects_prefix`` — the control is not vacuous:
    pointing the baseline at the POST-fix version makes the script
    fail. A control that cannot fail is not a control.
  * ``test_negctl_p0_3_missing_baseline_is_not_silent`` — an
    unresolvable baseline is a loud non-zero exit, never a skip. A
    silently skipped control is indistinguishable from a passed one.
  * ``test_negctl_p0_3_detects_an_unguarded_triage`` — a triage the
    stop does not wait for is not the fix. The control's GREEN half
    asks "does the stop wait for the verdict?", not "is there a triage
    call in the file?" — a call that is made and ignored is the
    pre-fix behaviour wearing a triage.
  * ``test_negctl_p0_3_behaviour_suite_passes`` — the behavioural suite
    for the fix passes. The script never runs it (a nested pytest run
    is what the control forbids); the caller runs it.

Why the baseline is synthesised rather than read from history
-------------------------------------------------------------
The pre-fix revision has to exist in the repository, and it does not
always:

  * CI checks out with ``fetch-depth: 1`` (``actions/checkout@v4``'s
    default), so no pre-fix commit is reachable — only the merge commit
    is;
  * ``origin/main`` stops holding the pre-fix source the moment this
    branch merges, so a test pinned to ``origin/main`` would break on
    merge, long after it stopped meaning anything.

So the test builds the baseline it needs: a commit object whose
``backend/verification/orchestrator.py`` is the current file with the
P0-3 fix reverted — the threshold back to ``1`` and the triage call
replaced by a verdict the branch could act on without asking anyone.
The commit is created with ``git commit-tree`` against a throwaway
index, so the repository's real index, the working tree and every ref
are untouched, and the commit is unreferenced (``git gc`` will collect
it).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

pytestmark = pytest.mark.integration


#: The project root — the directory that holds ``scripts/`` and
#: ``backend/``. Resolved from this file's location
#: (``backend/tests/integration/<this file>``), so the test needs no
#: environment variable and works from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: The script under test, relative to the project root.
SCRIPT_REL = "scripts/negctl_p0_3.sh"

#: The file the control reads, relative to the project root — both the
#: pre-fix baseline and the working-tree copy.
TARGET_REL = "backend/verification/orchestrator.py"

#: The behavioural suite for the fix, relative to ``backend/`` (pytest
#: is invoked with ``cwd=backend/``, which is where ``backend/pytest.ini``
#: and the venv live).
BEHAVIOUR_REL = "tests/integration/test_same_failure_triage.py"

#: The script's RED lines — the pre-fix shape, found in the baseline.
RED_THRESHOLD_LINE = (
    "RED: pre-fix orchestrator.py has "
    "_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 1"
)
RED_BRANCH_LINE = "RED: pre-fix same-failure branch returns repair_tasks=[]"

#: The script's GREEN line — the fixed shape, found in the working tree.
GREEN_LINE = "GREEN: current orchestrator.py has threshold 2 and triage dispatch"

#: The script's PASS line.
PASS_LINE = "PASS: negctl-p0-3"

#: The pre-fix threshold, quoted from
#: ``origin/main:backend/verification/orchestrator.py`` (commit 5109ee0)
#: — the value the fix raised.
PRE_FIX_THRESHOLD = "self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 1"

#: The fix's threshold, quoted from the current
#: ``backend/verification/orchestrator.py``.
POST_FIX_THRESHOLD = "self._MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS = 2"

#: The triage call, quoted from the current
#: ``backend/verification/orchestrator.py``. The pre-fix source has no
#: triage at all — it terminated at the cap without asking anyone.
TRIAGE_CALL = (
    "            decision = self.classify_cycle(\n"
    "                stuck_keys[0], report, round_number,\n"
    "            )\n"
)

#: What the triage call is replaced with in the synthetic baseline: a
#: verdict the branch can act on without asking anyone — the pre-fix
#: shape, reached at the cap.
PRE_FIX_DECISION = '            decision = {"should_stop": True}\n'

#: Upper bound on the script's runtime.
HARD_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(
    *args: str,
    stdin: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Run a git command in PROJECT_ROOT and return its stdout.

    Raises ``AssertionError`` on a non-zero exit — a helper that
    returns empty output on failure is how a test ends up asserting
    against nothing.
    """
    proc = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return proc.stdout


def _synthetic_commit(rel_path: str, source: str) -> str:
    """Create a commit object holding ``source`` at ``rel_path``.

    The repository's real index, the working tree and every ref are
    left exactly as they were: the index is redirected to a throwaway
    file via ``GIT_INDEX_FILE``, the blob and tree are written with the
    plumbing commands, and the commit is created with ``git
    commit-tree``, which writes an object and moves no ref. The commit
    is unreferenced — nothing in ``git log`` — and ``git gc`` collects
    it later.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        # ``commit-tree`` refuses to run without an author identity.
        env.update(
            {
                "GIT_AUTHOR_NAME": "negctl",
                "GIT_AUTHOR_EMAIL": "negctl@example.invalid",
                "GIT_COMMITTER_NAME": "negctl",
                "GIT_COMMITTER_EMAIL": "negctl@example.invalid",
            }
        )
        blob = _git("hash-object", "-w", "--stdin", stdin=source, env=env).strip()
        _git("read-tree", "HEAD", env=env)
        _git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},{rel_path}",
            env=env,
        )
        tree = _git("write-tree", env=env).strip()
        commit = _git(
            "commit-tree",
            tree,
            "-p",
            "HEAD",
            "-m",
            "negctl: synthetic baseline (not referenced by any ref)",
            env=env,
        ).strip()
    return commit


def _prefix_source() -> str:
    """The current file with the P0-3 fix reverted.

    Two things changed in the fix, and both are reverted here:

    1. the threshold — ``2`` back to ``1``, so the cap fires on the
       first repeat instead of the second;
    2. the triage — the ``classify_cycle`` call replaced by a verdict
       the branch can act on without asking anyone, so the branch
       terminates at the cap with an empty ``repair_tasks`` and no
       triage, which is the pre-fix shape.

    The result is compiled before it is handed to git: a baseline that
    is not valid Python would fail the control for the wrong reason
    (a parse error instead of a shape check), and this test should say
    so plainly rather than let the control report a confusing error.
    """
    source = (PROJECT_ROOT / TARGET_REL).read_text(encoding="utf-8")

    assert source.count(POST_FIX_THRESHOLD) == 1, (
        f"expected exactly one {POST_FIX_THRESHOLD!r} in {TARGET_REL} — "
        f"the synthetic baseline is built by reverting it"
    )
    reverted = source.replace(POST_FIX_THRESHOLD, PRE_FIX_THRESHOLD)

    assert reverted.count(TRIAGE_CALL) == 1, (
        f"expected exactly one triage call in {TARGET_REL} — "
        f"the synthetic baseline is built by reverting it"
    )
    reverted = reverted.replace(TRIAGE_CALL, PRE_FIX_DECISION)

    compile(reverted, TARGET_REL, "exec")
    return reverted


def _run_script(prefix_rev: str) -> Tuple[int, str, str]:
    """Run the negative-control script with ``prefix_rev`` as the baseline.

    Returns ``(returncode, stdout, stderr)``. The script is invoked by
    path with ``cwd=PROJECT_ROOT`` — it resolves its own paths from
    ``BASH_SOURCE``, but the git commands it runs are anchored on the
    project root, so the cwd is pinned here to keep that explicit.
    """
    env = dict(os.environ)
    env["P0_3_PREFIX_REV"] = prefix_rev
    proc = subprocess.run(
        ["bash", str(PROJECT_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=HARD_TIMEOUT_SECONDS,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_script_without_a_pinned_baseline() -> Tuple[int, str, str]:
    """Run the control with no baseline pinned at all.

    The env var is *removed* rather than set to an empty string: the
    script treats an env var that is set but empty as an error (the
    caller said which revision it meant and named nothing), and the
    path this helper exercises is the one where the caller said nothing
    at all — which is how CI and the local gate run it.
    """
    env = dict(os.environ)
    env.pop("P0_3_PREFIX_REV", None)
    proc = subprocess.run(
        ["bash", str(PROJECT_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=HARD_TIMEOUT_SECONDS,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_behaviour_suite() -> Tuple[int, str]:
    """Run the behavioural suite for the fix and return ``(rc, output)``.

    The control script never runs it: a pytest session inside a pytest
    session is exactly what the control is not allowed to do. The
    caller runs it, and the caller is this test.
    """
    venv_python = PROJECT_ROOT / "backend" / ".venv" / "bin" / "python3"
    assert venv_python.exists(), (
        f"the project venv python is missing: {venv_python} — the "
        f"behavioural suite must not run on the system interpreter"
    )
    env = dict(os.environ)
    # The outer pytest session's environment must not leak into this
    # one: PYTEST_ADDOPTS would inject options, and COVERAGE_FILE
    # would point two pytest runs at one coverage file.
    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_CURRENT_TEST",
        "PYTEST_PLUGINS",
        "PYTEST_VERSION",
        "COVERAGE_FILE",
    ):
        env.pop(name, None)
    proc = subprocess.run(
        [str(venv_python), "-m", "pytest", BEHAVIOUR_REL, "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT / "backend"),
        env=env,
        timeout=HARD_TIMEOUT_SECONDS,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _lines(output: str, prefix: str) -> List[str]:
    return [line for line in output.splitlines() if line.startswith(prefix)]


#: The heredoc the script writes its analyser into — the analyser is
#: lifted out of the script rather than copied here, so this test
#: exercises the control's real analyser and not a copy of it.
ANALYSER_BEGIN = "cat > \"$ANALYSER\" <<'PYEOF'\n"
ANALYSER_END = "\nPYEOF\n"


def _extract_analyser() -> str:
    """The analyser's source, lifted out of the script's heredoc."""
    script = (PROJECT_ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    assert ANALYSER_BEGIN in script, (
        f"the script no longer writes its analyser through a heredoc "
        f"opened by {ANALYSER_BEGIN!r}"
    )
    begin = script.index(ANALYSER_BEGIN) + len(ANALYSER_BEGIN)
    end = script.index(ANALYSER_END, begin)
    return script[begin:end]


def _analyse(source: str) -> Dict[str, str]:
    """Run the script's analyser over a source string.

    Returns the analyser's ``key=value`` facts as a dict.
    """
    with tempfile.TemporaryDirectory() as tmp:
        analyser = Path(tmp) / "analyse_orchestrator.py"
        analyser.write_text(_extract_analyser(), encoding="utf-8")
        target = Path(tmp) / "orchestrator.py"
        target.write_text(source, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(analyser), str(target)],
            capture_output=True,
            text=True,
            timeout=HARD_TIMEOUT_SECONDS,
        )
    assert proc.returncode == 0, (
        f"the analyser exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    facts = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        facts[key] = value
    return facts


# ---------------------------------------------------------------------------
# 1. The control is established
# ---------------------------------------------------------------------------


def test_negctl_p0_3_passes():
    """The script exits 0 and reports two REDs, one GREEN and one PASS.

    The baseline is the pre-fix source, synthesised by reverting the
    fix in the current file. The script must:

      * confirm the pre-fix threshold in the baseline (the first RED
        line) — otherwise the control would be proving nothing about a
        bug that was not there;
      * confirm the pre-fix branch returned an empty ``repair_tasks``
        without dispatching a triage (the second RED line) — that is
        the behaviour the fix replaced;
      * confirm the current source has the threshold at 2 and does
        dispatch a triage (the GREEN line);
      * print exactly one PASS line.
    """
    prefix_rev = _synthetic_commit(TARGET_REL, _prefix_source())
    rc, stdout, stderr = _run_script(prefix_rev)

    assert rc == 0, (
        f"the negative control exited {rc} (expected 0).\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    combined = stdout + stderr

    red_lines = _lines(combined, "RED:")
    assert len(red_lines) == 2, (
        f"expected exactly two RED lines, got {len(red_lines)}: "
        f"{red_lines!r}\n--- combined output ---\n{combined}"
    )

    pass_lines = [line for line in combined.splitlines() if line == PASS_LINE]
    assert len(pass_lines) == 1, (
        f"expected exactly one {PASS_LINE!r} line, got {pass_lines!r}\n"
        f"--- combined output ---\n{combined}"
    )

    assert RED_THRESHOLD_LINE in combined.splitlines(), (
        f"the first RED line is missing — the control did not confirm "
        f"that the baseline carries the pre-fix threshold.\n"
        f"--- combined output ---\n{combined}"
    )

    assert RED_BRANCH_LINE in combined.splitlines(), (
        f"the second RED line is missing — the control did not confirm "
        f"that the baseline terminates with an empty repair_tasks.\n"
        f"--- combined output ---\n{combined}"
    )

    assert GREEN_LINE in combined.splitlines(), (
        f"the GREEN line is missing — the current source was not shown "
        f"to carry the fixed shape.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 2. The control is not vacuous
# ---------------------------------------------------------------------------


def test_negctl_p0_3_detects_prefix():
    """Pointing the baseline at the POST-fix source makes the script fail.

    ``HEAD`` holds the fixed source: the threshold is 2 and the branch
    dispatches a triage. A control that accepted it as a pre-fix
    baseline would be measuring nothing — so the script must exit
    non-zero and say which shape it did not find.
    """
    rc, stdout, stderr = _run_script("HEAD")

    assert rc != 0, (
        f"the negative control exited 0 against the post-fix baseline — "
        f"the control is vacuous.\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    combined = stdout + stderr

    assert PASS_LINE not in combined.splitlines(), (
        f"the script printed {PASS_LINE!r} against the post-fix "
        f"baseline — it must not.\n--- combined output ---\n{combined}"
    )

    assert "FAIL:" in combined, (
        f"the script failed without saying why.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 3. An unresolvable baseline is loud, never silent
# ---------------------------------------------------------------------------


def test_negctl_p0_3_missing_baseline_is_not_silent():
    """A baseline that cannot be read is a non-zero exit with a reason.

    A silently skipped control reads exactly like a passed one, so the
    script has to say what it could not do.
    """
    rc, stdout, stderr = _run_script("refs/heads/negctl-no-such-revision")

    assert rc != 0, (
        f"the negative control exited 0 against a baseline that does not "
        f"exist.\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    combined = stdout + stderr

    assert PASS_LINE not in combined.splitlines(), (
        f"the script printed {PASS_LINE!r} with an unreadable "
        f"baseline — it must not.\n--- combined output ---\n{combined}"
    )

    assert "ERROR:" in combined, (
        f"the script failed without saying why.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 5. The control finds its own baseline when the caller pins none
# ---------------------------------------------------------------------------


def test_negctl_p0_3_finds_its_own_pre_fix_baseline():
    """With no revision pinned, the control still reaches a real RED.

    This is the path CI and the local gate take: the script is run with
    no environment at all. The baseline must then be one the script
    found for itself — the most recent revision that still stops with
    an empty ``repair_tasks``, or the checked-in pre-fix fragment.

    What this pins is the absence of a moving default. A baseline of
    ``origin/main`` is a ref that names whatever main happens to be
    when the control runs: it stops holding the pre-fix source the
    moment the fix merges, and a clone that fetched one branch does
    not have it at all — the control then cannot run, which is a
    failure with no RED and no GREEN in it.
    """
    rc, stdout, stderr = _run_script_without_a_pinned_baseline()
    combined = stdout + stderr

    assert rc == 0, (
        f"the negative control exited {rc} with no baseline pinned "
        f"(expected 0).\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    assert "origin/main" not in combined, (
        "the control read its baseline from `origin/main` — a ref that "
        "moves under the control, and one a single-branch clone does "
        f"not have.\n--- combined output ---\n{combined}"
    )

    assert RED_THRESHOLD_LINE in combined.splitlines(), (
        "the first RED line is missing — the control did not confirm "
        "that the baseline carries the pre-fix threshold.\n"
        f"--- combined output ---\n{combined}"
    )

    assert RED_BRANCH_LINE in combined.splitlines(), (
        "the second RED line is missing — the control did not confirm "
        "that the baseline terminates with an empty repair_tasks.\n"
        f"--- combined output ---\n{combined}"
    )

    assert GREEN_LINE in combined.splitlines(), (
        "the GREEN line is missing — the current source was not shown "
        f"to carry the fixed shape.\n--- combined output ---\n{combined}"
    )

    assert PASS_LINE in combined.splitlines(), (
        f"the script did not print {PASS_LINE!r}.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 4. A triage that does not gate the stop is not the fix
# ---------------------------------------------------------------------------


def test_negctl_p0_3_detects_an_unguarded_triage():
    """A triage the stop does not wait for does not satisfy the control.

    The fixed source has the stop *inside* a branch whose condition
    reads the triage's verdict: the cap buys one triage call, and the
    chain stops only if the triage said stop. Two shapes look similar
    but are the pre-fix behaviour wearing a triage call:

      * the triage is called and its verdict is thrown away — the chain
        stops whatever the triage said;
      * the triage is called after the stop — the chain stops without
        ever asking.

    The control's GREEN half must reject both, so the analyser has to
    answer "does the stop wait for the verdict?", not "is there a
    triage call in the file?".
    """
    source = (PROJECT_ROOT / TARGET_REL).read_text(encoding="utf-8")

    facts = _analyse(source)
    assert facts["threshold"] == "2", (
        f"the current source's threshold is {facts['threshold']!r}, "
        f"expected '2' — the fixture this test mutates is not the fixed one"
    )
    assert facts["triage_dispatch"] == "1"
    assert facts["stop_guarded_by_triage"] == "1", (
        f"the fixed source's stop does not wait for the triage verdict "
        f"(facts: {facts!r})"
    )

    # The verdict is called and thrown away: the chain stops whatever
    # the triage said, which is the pre-fix behaviour.
    ignored = source.replace(
        TRIAGE_CALL,
        "            self.classify_cycle(\n"
        "                stuck_keys[0], report, round_number,\n"
        "            )\n"
        '            decision = {"should_stop": True}\n',
    )
    assert ignored != source, (
        "the triage call was not found in the current source — the "
        "mutation this test applies is not the one it means to apply"
    )
    ignored_facts = _analyse(ignored)
    assert ignored_facts["triage_dispatch"] == "1", (
        f"the mutation removed the triage call instead of its verdict "
        f"(facts: {ignored_facts!r})"
    )
    assert ignored_facts["stop_guarded_by_triage"] == "0", (
        f"the analyser accepted a triage whose verdict is ignored "
        f"(facts: {ignored_facts!r}) — the control would pass on the "
        f"pre-fix behaviour wearing a triage call"
    )

    # The triage is called after the stop: the chain stops without ever
    # asking.
    after = source.replace(TRIAGE_CALL, '            decision = {"should_stop": True}\n')
    assert after != source
    assert after.count("                return stop_payload\n") == 1, (
        "the stop payload was not found in the current source — the "
        "mutation this test applies is not the one it means to apply"
    )
    after = after.replace(
        "                return stop_payload\n",
        "                return stop_payload\n"
        "                self.classify_cycle(stuck_keys[0], report, round_number)\n",
    )
    after_facts = _analyse(after)
    assert after_facts["triage_dispatch"] == "1", (
        f"the mutation removed the triage call instead of moving it "
        f"(facts: {after_facts!r})"
    )
    assert after_facts["stop_guarded_by_triage"] == "0", (
        f"the analyser accepted a triage that runs after the stop "
        f"(facts: {after_facts!r}) — the control would pass on the "
        f"pre-fix behaviour wearing a triage call"
    )


# ---------------------------------------------------------------------------
# 5. The behavioural suite for the fix passes
# ---------------------------------------------------------------------------


def test_negctl_p0_3_behaviour_suite_passes():
    """The fix's own behavioural suite is green.

    This is the "the fix works" half of the claim; the control above is
    the "the bug was there" half. The suite is run by the caller — the
    control script refuses to start a nested pytest session.
    """
    rc, output = _run_behaviour_suite()

    assert rc == 0, (
        f"the behavioural suite for the fix failed (pytest exit {rc}).\n"
        f"--- output ---\n{output}"
    )
