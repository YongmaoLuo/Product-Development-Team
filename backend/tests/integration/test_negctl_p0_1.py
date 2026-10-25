"""2026-10-22 (P0-1): the negative control that proves the fix was needed.

The bug
-------
``VerificationSubAgent._execute_attempt`` used to catch
:class:`VerdictParseError` and answer with a locally built
``Verdict(verdict="FAILED", ...)``. That swallowed the failure: the
caller (``_self_heal_loop``) saw a well-formed verdict, so its ``except
VerdictParseError`` branch — the L2 retry, the one that exists exactly
for a transient protocol garble — was unreachable. One garbled response
failed the verification point for good, and the verdict claimed the
deliverable was broken when nothing was known about the deliverable.

Two claims, one of which is easy to leave on self-report
---------------------------------------------------------
"the fix works" is half the claim, and the easy half: the behavioural
suite (``tests/unit/test_verdict_parse_retry.py``) shows a garbled
payload is retried and the retry's verdict is the one that counts.

The other half is "the bug was there". Without it, the fix could be a
no-op and the behavioural suite would still be green — a suite that
passes on both the broken and the fixed code proves nothing about the
fix. The negative control is the piece that closes that gap: it reads
the PRE-FIX source out of git and shows that it really did build the
inline FAILED verdict, so the behavioural difference the suite measures
is a difference that existed to begin with.

``scripts/negctl_p0_1.sh`` is that control. This module is its TDD
spec:

  * ``test_negctl_p0_1_passes`` — the control is established: the
    baseline really shows the pre-fix shape, the current source does
    not, the behavioural suite passes, and the script says PASS.
  * ``test_negctl_p0_1_detects_prefix`` — the control is not vacuous:
    pointing the baseline at the POST-fix version makes the script fail.
    A control that cannot fail is not a control.
  * ``test_negctl_p0_1_missing_baseline_is_not_silent`` — an
    unresolvable baseline is a loud non-zero exit, never a skip. A
    silently skipped control is indistinguishable from a passed one.

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
``backend/verification_subagent.py`` is the current file with the P0-1
fix reverted — the pre-fix handler, quoted verbatim from
``origin/main:backend/verification_subagent.py``. The commit is created
with ``git commit-tree`` against a throwaway index, so the repository's
real index, the working tree and every ref are untouched, and the
commit is unreferenced (``git gc`` will collect it).
"""

from __future__ import annotations

import os
import subprocess
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
SCRIPT_REL = "scripts/negctl_p0_1.sh"

#: The file the control reads, relative to the project root — both the
#: pre-fix baseline and the working-tree copy.
TARGET_REL = "backend/verification_subagent.py"

#: The marker pair the fix introduced (see ``backend/verification_subagent.py``).
BEGIN_MARKER = "# NEGCTL-P0-1-BEGIN"
END_MARKER = "# NEGCTL-P0-1-END"

#: The pre-fix handler, quoted verbatim from
#: ``origin/main:backend/verification_subagent.py`` (commit 5109ee0) —
#: the shape the P0-1 fix removed. The indentation matches the block it
#: replaces, so the reverted file stays valid Python.
PRE_FIX_ARM = (
    '            except VerdictParseError as e:\n'
    '                return Verdict(\n'
    '                    verdict="FAILED",\n'
    '                    reasons=[f"verdict parse error: {e}"],\n'
    '                    evidence=[raw[:500]],\n'
    '                    model_complexity=self.model_complexity,\n'
    '                )\n'
)

#: The script's RED line — the pre-fix shape, found in the baseline.
RED_LINE = (
    "RED: pre-fix verification_subagent.py contains inline "
    "Verdict(verdict='FAILED') in VerdictParseError handler"
)

#: The script's PASS line.
PASS_LINE = "PASS: negctl-p0-1"

#: Upper bound on the script's runtime. The script runs a pytest
#: session of its own (~5 s on this tree), so this is generous.
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
    """The current file with the P0-1 fix reverted.

    The block between the two markers is replaced by the pre-fix
    handler and the markers themselves are dropped — the pre-fix source
    did not have them (the fix introduced them), and leaving them in
    would make the synthetic baseline a file the control has never seen.

    The result is compiled before it is handed to git: a baseline that
    is not valid Python would fail the control for the wrong reason
    (a parse error instead of a shape check), and this test should say
    so plainly rather than let the control report a confusing error.
    """
    source = (PROJECT_ROOT / TARGET_REL).read_text(encoding="utf-8")
    head, marker, rest = source.partition(BEGIN_MARKER)
    assert marker, f"{BEGIN_MARKER} is missing from {TARGET_REL}"
    _, marker, tail = rest.partition(END_MARKER)
    assert marker, f"{END_MARKER} is missing from {TARGET_REL}"
    # ``head`` ends with the indentation of the BEGIN marker's line, so it
    # is stripped before the replacement goes in — otherwise the pre-fix
    # arm would be indented twice and the file would not parse.
    reverted = head.rstrip() + "\n" + PRE_FIX_ARM + tail
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
    env["P0_1_PREFIX_REV"] = prefix_rev
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
    """Run the negative-control script with no baseline pinned at all.

    The env var is *removed* rather than set to an empty string: the
    script treats an env var that is set but empty as an error (the
    caller said which revision it meant and named nothing), and the
    path this helper exercises is the one where the caller said nothing
    at all — which is how CI and the local gate run it.
    """
    env = dict(os.environ)
    env.pop("P0_1_PREFIX_REV", None)
    proc = subprocess.run(
        ["bash", str(PROJECT_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=HARD_TIMEOUT_SECONDS,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _lines(output: str, prefix: str) -> List[str]:
    return [line for line in output.splitlines() if line.startswith(prefix)]


# ---------------------------------------------------------------------------
# 1. The control is established
# ---------------------------------------------------------------------------


def test_negctl_p0_1_passes():
    """The script exits 0 and reports RED + three GREENs + one PASS.

    The baseline is the pre-fix source, synthesised by reverting the
    fix in the current file. The script must:

      * confirm the pre-fix shape in the baseline (the RED line) —
        otherwise the control would be proving nothing about a bug that
        was not there;
      * confirm the current source has no inline FAILED verdict;
      * confirm the current source re-raises, so the L2 retry the fix
        was about is reachable;
      * run the behavioural suite for the fix and see it pass;
      * print exactly one PASS line.
    """
    prefix_rev = _synthetic_commit(TARGET_REL, _prefix_source())
    rc, stdout, stderr = _run_script(prefix_rev)

    assert rc == 0, (
        f"the negative control exited {rc} (expected 0).\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    combined = stdout + stderr

    green_lines = _lines(combined, "GREEN:")
    assert len(green_lines) == 3, (
        f"expected exactly three GREEN lines, got {len(green_lines)}: "
        f"{green_lines!r}\n--- combined output ---\n{combined}"
    )

    pass_lines = [line for line in combined.splitlines() if line == PASS_LINE]
    assert len(pass_lines) == 1, (
        f"expected exactly one {PASS_LINE!r} line, got {pass_lines!r}\n"
        f"--- combined output ---\n{combined}"
    )

    assert RED_LINE in combined.splitlines(), (
        f"the RED line is missing — the control did not confirm that the "
        f"baseline carries the pre-fix inline FAILED verdict.\n"
        f"--- combined output ---\n{combined}"
    )

    assert "GREEN: current verification_subagent.py has no inline FAILED verdict" \
        in combined.splitlines(), (
        "the current source was not cleared of the inline FAILED verdict.\n"
        f"--- combined output ---\n{combined}"
    )

    assert "GREEN: tests/unit/test_verdict_parse_retry.py passed" \
        in combined.splitlines(), (
        "the behavioural suite for the fix did not pass.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 2. The control is not vacuous
# ---------------------------------------------------------------------------


def test_negctl_p0_1_detects_prefix():
    """Pointing the baseline at the post-fix version makes the script fail.

    This is the negative control of the negative control. The baseline
    here is the CURRENT source — the fixed version — so the pre-fix
    shape is absent and the RED check cannot be established. The script
    must exit non-zero and must NOT print the PASS line.

    A control that cannot fail is not a control: without this test, a
    script that always printed PASS would satisfy
    ``test_negctl_p0_1_passes`` forever.
    """
    post_fix_source = (PROJECT_ROOT / TARGET_REL).read_text(encoding="utf-8")
    prefix_rev = _synthetic_commit(TARGET_REL, post_fix_source)

    rc, stdout, stderr = _run_script(prefix_rev)
    combined = stdout + stderr

    assert rc != 0, (
        "the control PASSED against a baseline that carries the fix — it "
        "cannot tell the pre-fix source from the post-fix source, so it "
        "proves nothing.\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    assert PASS_LINE not in combined.splitlines(), (
        f"the script printed {PASS_LINE!r} even though the baseline was "
        f"the post-fix source.\n--- combined output ---\n{combined}"
    )

    assert "FAIL" in combined, (
        "the control failed without saying why — the failure has to name "
        "the check that fired.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 3. An unresolvable baseline is loud, never a skip
# ---------------------------------------------------------------------------


def test_negctl_p0_1_missing_baseline_is_not_silent():
    """A baseline revision that does not exist is a non-zero exit.

    The control's whole value is that it is re-runnable and that its
    result means something. If the baseline could not be read and the
    script shrugged (``SKIP`` and exit 0), then a broken invocation
    would read exactly like a passed control — and the PRD acceptance
    would be satisfied by a script that had checked nothing.
    """
    rc, stdout, stderr = _run_script(
        "refs/heads/negctl-p0-1-no-such-revision"
    )
    combined = stdout + stderr

    assert rc != 0, (
        "the script exited 0 with an unresolvable baseline revision — a "
        "skipped control reads exactly like a passed one.\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

    assert PASS_LINE not in combined.splitlines(), (
        f"the script printed {PASS_LINE!r} without ever reading the "
        f"baseline.\n--- combined output ---\n{combined}"
    )

    assert "ERROR" in combined, (
        "the script failed without naming the reason — the caller has to "
        "be able to tell a broken invocation from a fired check.\n"
        f"--- combined output ---\n{combined}"
    )


# ---------------------------------------------------------------------------
# 4. The control finds its own baseline when the caller pins none
# ---------------------------------------------------------------------------


def test_negctl_p0_1_finds_its_own_pre_fix_baseline():
    """With no revision pinned, the control still reaches a real RED.

    This is the path CI and the local gate take: the script is run with
    no environment at all. The baseline must then be one the script
    found for itself — the most recent revision that still carries the
    pre-fix shape, or the checked-in pre-fix fragment.

    What this pins is the absence of a moving default. A baseline of
    ``origin/main`` is a ref that names whatever main happens to be
    when the control runs: it stops holding the pre-fix source the
    moment the fix merges, and a clone that fetched one branch does
    not have it at all. Either way the control stops measuring "the
    bug was there" — it fails on merge day, or it cannot run at all.
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

    assert RED_LINE in combined.splitlines(), (
        "the RED line is missing — the control did not confirm that the "
        f"baseline carries the pre-fix shape.\n--- combined output ---\n{combined}"
    )

    assert PASS_LINE in combined.splitlines(), (
        f"the script did not print {PASS_LINE!r}.\n"
        f"--- combined output ---\n{combined}"
    )
