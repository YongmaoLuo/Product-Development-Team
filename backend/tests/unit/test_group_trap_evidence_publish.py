"""The group-signal control's evidence has to reach every face a citation names.

The background
--------------
``code_review`` citations are resolved against ``project_dir``
(``target = (project_dir / rel)``), while the control's product lives in
the plan directory's ``vp_artifacts/<vp_id>/``.  The plan directory is
usually another checkout, so a citation naming the product has no file to
resolve against unless the product is staged into the project dir as well.

The staging that does it was a one-off snapshot taken when a round was
dispatched -- and the dispatcher was a different checkout from the one
holding the current control.  So the citable face stayed a generation
behind: the archive in the plan directory held the current control's
product, while the staged copy the citations named still held the output
of the control as it was before the fix.  A citation that resolves
against a file nobody can reproduce is not evidence, and a snapshot that
is never refreshed is a snapshot that keeps answering for a control that
no longer exists.

What this file pins
-------------------
``scripts/negctl_publish_group_trap_evidence.sh`` is the entry point that
closes the gap, and it closes it by fixing the ORDER: run the current
control first, publish second.  The cases below pin

  * both rounds reach the archive face AND the citable face, and the two
    faces are byte-for-byte equal;
  * a citable face that still holds a superseded product is overwritten,
    not left alone;
  * what lands there is the current control's product -- the current
    census header, the current sentinel lifetime, and none of the
    markers of the superseded control;
  * the two rounds disagree, which is the whole point of the control;
  * the staged tree does not land inside the tree the api service
    serves (a plan directory there makes ``GET /api/plans`` answer 503);
  * the publisher signals no process it did not create.

Nothing here calls a model or starts a service.  The publisher does run
the real control, because a stand-in would not produce the evidence the
assertions read.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


REPO_ROOT = Path(__file__).resolve().parents[3]

#: The publisher: it runs the control and files the product on both faces.
PUBLISH_SCRIPT = REPO_ROOT / "scripts" / "negctl_publish_group_trap_evidence.sh"

#: The control whose product is published.
CONTROL_SCRIPT = REPO_ROOT / "scripts" / "negctl_group_signal_trap.sh"

#: The verification point whose evidence this is.
VP_ID = "VP-014"

#: The plan id the fixture files its staged tree under.  Synthetic: the
#: gate that forbids real plan ids in first-party source exempts test
#: sources, and a fixture has no business naming somebody's plan.
PLAN_ID = "fixture-group-trap-publish"

#: A staged copy that was never refreshed.  It carries the superseded
#: census header and the superseded sentinel lifetime -- the two markers
#: that tell a stale copy apart from the current control's product.
STALE_PRODUCT = (
    "=== 2026-01-01T00:00:00 the control shell was signalled ===\n"
    "mode=group\n"
    "sentinel_alive_before_the_signal=yes\n"
    "sentinel_alive_after_the_signal=no\n"
    "received_by=group\n"
    "--- ps -o pid,ppid,pgid,command -g 1 (the group at the moment of the signal) ---\n"
    "  PID  PPID  PGID COMMAND\n"
    "1 0 1 bash scripts/negctl_group_signal_trap.sh group /tmp/stale/group\n"
    "3 1 1 ps -o pid,ppid,pgid,command -g 1\n"
    "scripts/negctl_group_signal_trap.sh: line 155: 2 Terminated: 15"
    "          sleep 30\n"
    "--- end of the census ---\n"
)

#: The two names the product is filed under -- one per round.
FACES = ("group-trap.txt", "group-trap-single-pid.txt")


def _fact(text: str, key: str) -> str:
    """The value of a ``key=value`` line in an evidence file."""
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    return ""


def _census_header(text: str) -> str:
    """The header line of the group census."""
    for line in text.splitlines():
        if line.startswith("--- ps "):
            return line.strip()
    return ""


def _census_rows(text: str) -> list[list[str]]:
    """The rows of the group census the trap wrote."""
    rows: list[list[str]] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("--- ps "):
            inside = True
            continue
        if line.startswith("--- end of the census"):
            inside = False
            continue
        if not inside:
            continue
        fields = line.split()
        if fields and fields[0].isdigit():
            rows.append(fields)
    return rows


def _sentinel_lifetime() -> int:
    """The sentinel's lifetime, read out of the control's own source."""
    source = CONTROL_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"SENTINEL_LIFETIME_SECONDS:-(\d+)", source)
    assert match, (
        "the control declares no numeric sentinel lifetime, so the "
        "published evidence cannot be checked against one"
    )
    return int(match.group(1))


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One real publishing run, with a stale citable face waiting for it.

    The stale copy is planted before the run, so the run has to overwrite
    it: a publisher that only ever writes a fresh tree would pass the
    same assertions, and would leave the defect in place.
    """
    base = tmp_path_factory.mktemp("group-trap-publish")
    plan_dir = base / "plan" / PLAN_ID
    (plan_dir / "vp_artifacts" / VP_ID).mkdir(parents=True)
    project_root = base / "project"
    staged_dir = (
        project_root / ".pdt" / "evidence" / PLAN_ID / "vp_artifacts" / VP_ID
    )
    staged_dir.mkdir(parents=True)
    for name in FACES:
        (staged_dir / name).write_text(STALE_PRODUCT, encoding="utf-8")
    # The tree the api service serves.  The staged tree must not land
    # inside it: a plan directory there makes GET /api/plans answer 503.
    (project_root / "plans").mkdir(parents=True)

    result = subprocess.run(
        [
            "bash",
            str(PUBLISH_SCRIPT),
            "--plan-dir",
            str(plan_dir),
            "--project-root",
            str(project_root),
            "--vp-id",
            VP_ID,
            "--plans-dir",
            str(project_root / "plans"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        # The publisher's own contract: the shell that runs the control
        # leads a process group of its own, so a group signal can only
        # ever reach a group this run created.  A shell that merely
        # lives in somebody else's group (pytest's, the CI runner's)
        # has no group of its own to lead -- so the run is started in a
        # session of its own, the way the control's own caller starts
        # it (``start_new_session=True``).
        start_new_session=True,
    )

    return {
        "result": result,
        "plan_dir": plan_dir,
        "project_root": project_root,
        "archive_dir": plan_dir / "vp_artifacts" / VP_ID,
        "staged_dir": staged_dir,
    }


# ---------------------------------------------------------------------------
# The publisher exists and runs
# ---------------------------------------------------------------------------


def test_the_publisher_is_in_the_repository() -> None:
    """The entry point has to be a file, not a plan."""
    assert PUBLISH_SCRIPT.is_file(), (
        f"{PUBLISH_SCRIPT.relative_to(REPO_ROOT)} does not exist, so "
        f"nothing publishes the control's product to the citable face"
    )


def test_the_publisher_succeeds(published: dict) -> None:
    """The run has to succeed, and say why when it does not."""
    result = published["result"]
    assert result.returncode == 0, (
        f"the publisher exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Both faces, and the same bytes on both
# ---------------------------------------------------------------------------


def test_both_rounds_reach_the_archive_face_and_the_citable_face(
    published: dict,
) -> None:
    """Each round is filed under its own name, on both faces."""
    archive = published["archive_dir"]
    staged = published["staged_dir"]
    for name in FACES:
        assert (archive / name).is_file(), (
            f"{name} did not reach the archive face ({archive})"
        )
        assert (staged / name).is_file(), (
            f"{name} did not reach the citable face ({staged})"
        )


def test_the_two_faces_hold_the_same_bytes(published: dict) -> None:
    """The citable face is a copy, so it has to be the same copy.

    A citation that names the citable face resolves line numbers in
    THAT file.  If the two faces differ by a byte, the line numbers the
    citation carries point at a different line than the archive's.
    """
    archive = published["archive_dir"]
    staged = published["staged_dir"]
    for name in FACES:
        assert (staged / name).read_bytes() == (archive / name).read_bytes(), (
            f"{name}: the citable face and the archive face differ, so a "
            f"citation's line numbers do not carry over"
        )


def test_a_stale_citable_face_is_overwritten(published: dict) -> None:
    """A staged copy left over from another generation must not survive.

    This is the defect the publisher exists for: the citable face held a
    superseded product while the archive held the current one, so the
    citations resolved against a control that no longer existed.
    """
    staged = published["staged_dir"]
    for name in FACES:
        text = (staged / name).read_text(encoding="utf-8")
        assert "ps -o pid,ppid,pgid,command -g" not in text, (
            f"{name}: the citable face still carries the superseded census "
            f"header -- it was never refreshed"
        )
        assert "sleep 30" not in text, (
            f"{name}: the citable face still carries the superseded "
            f"sentinel -- it was never refreshed"
        )


# ---------------------------------------------------------------------------
# What lands there is the current control's product
# ---------------------------------------------------------------------------


def test_the_group_round_reads_the_group_signal(published: dict) -> None:
    """The group round: the witness dies with the group."""
    text = (published["staged_dir"] / "group-trap.txt").read_text(
        encoding="utf-8"
    )
    assert _fact(text, "mode") == "group", text
    assert _fact(text, "sentinel_alive_before_the_signal") == "yes", text
    assert _fact(text, "sentinel_alive_after_the_signal") == "no", text
    assert _fact(text, "received_by") == "group", text
    assert "Terminated" in text, (
        "the group round shows no terminated witness, so the text does not "
        "say that the group signal is what killed it:\n" + text
    )
    assert "SENTINEL_LIFETIME_SECONDS" in text, (
        "the group round's job message does not name the witness' "
        "lifetime, so the text cannot tell a witness killed by the "
        "signal from one that expired by itself:\n" + text
    )


def test_the_single_pid_round_leaves_the_witness_alone(published: dict) -> None:
    """The single-pid round: the witness in the same group lives."""
    text = (published["staged_dir"] / "group-trap-single-pid.txt").read_text(
        encoding="utf-8"
    )
    assert _fact(text, "mode") == "single-pid", text
    assert _fact(text, "sentinel_alive_before_the_signal") == "yes", text
    assert _fact(text, "sentinel_alive_after_the_signal") == "yes", text
    assert _fact(text, "received_by") == "single-pid", text


def test_the_witness_is_alive_with_the_current_lifetime(published: dict) -> None:
    """The single-pid round's census shows the witness still running.

    A witness that expired by itself cannot be told apart from a witness
    the signal killed, so the lifetime in the census has to be the one
    the control declares -- long enough to outlast the watch window.
    """
    text = (published["staged_dir"] / "group-trap-single-pid.txt").read_text(
        encoding="utf-8"
    )
    lifetime = _sentinel_lifetime()
    sentinel_pid = _fact(text, "sentinel_pid")
    rows = _census_rows(text)
    assert any(
        row[0] == sentinel_pid and row[3:] == ["sleep", str(lifetime)]
        for row in rows
    ), (
        f"the census does not show the witness (pid {sentinel_pid}) alive "
        f"as `sleep {lifetime}`; the census reads:\n"
        + "\n".join(" ".join(row) for row in rows)
    )


def test_the_census_header_is_the_current_one(published: dict) -> None:
    """The census is read with the current control's command.

    The superseded control read the roster with ``ps -o ... -g``, which
    on macOS answers with the whole host -- so a census carrying that
    header belongs to a control that was replaced.
    """
    staged = published["staged_dir"]
    for name in FACES:
        text = (staged / name).read_text(encoding="utf-8")
        header = _census_header(text)
        assert header, f"{name}: no census header"
        assert "ps -axo pid,ppid,pgid,command" in header, (
            f"{name}: the census header {header!r} is not the current one"
        )


def test_the_two_rounds_disagree(published: dict) -> None:
    """The control has to tell the group from the pid.

    A control that reported the same verdict in both rounds would be
    reporting a constant, and a constant answers nothing about where a
    signal went.
    """
    staged = published["staged_dir"]
    group = _fact(
        (staged / "group-trap.txt").read_text(encoding="utf-8"), "received_by"
    )
    single = _fact(
        (staged / "group-trap-single-pid.txt").read_text(encoding="utf-8"),
        "received_by",
    )
    assert group == "group", group
    assert single == "single-pid", single
    assert group != single, (
        "both rounds reported the same verdict; the control cannot tell "
        "the group from the pid"
    )


# ---------------------------------------------------------------------------
# Where the staged tree lands
# ---------------------------------------------------------------------------


def test_the_staged_tree_stays_out_of_the_served_plans_tree(
    published: dict,
) -> None:
    """The staged tree is inside the project dir and outside the served one.

    Inside the project dir because that is what the framework resolves a
    citation against.  Outside the plans tree because a plan directory
    there that the state db does not know makes ``GET /api/plans`` answer
    503 and pushes the UI verification point to failure.
    """
    project_root = published["project_root"]
    staged_root = project_root / ".pdt" / "evidence" / PLAN_ID
    served = project_root / "plans"

    assert staged_root.is_dir(), (
        f"{staged_root} does not exist, so the citations have no tree to "
        f"resolve against"
    )
    assert staged_root == project_root / ".pdt" / "evidence" / PLAN_ID, (
        "the staged tree is not at the documented location, so the tree the "
        "citations resolve against is not the tree the publisher fills"
    )
    staged_root.resolve().relative_to(project_root.resolve())
    with pytest.raises(ValueError):
        staged_root.resolve().relative_to(served.resolve())
    assert not list(served.rglob("group-trap*.txt")), (
        "a copy of the product landed inside the tree the api service "
        "serves, which makes GET /api/plans answer 503"
    )


# ---------------------------------------------------------------------------
# The publisher signals nothing it did not create
# ---------------------------------------------------------------------------


def test_the_publisher_signals_no_process_it_did_not_create() -> None:
    """No kill in the publisher aims at a pid written down as a number.

    The publisher starts the control and copies files.  The only pids the
    control may signal are its own, the group it created, and the
    witness it started -- so a literal pid in a kill command anywhere in
    this path would be a way to reach a process nobody here knows about.
    """
    source = PUBLISH_SCRIPT.read_text(encoding="utf-8")
    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not re.search(r"\bkill\b", stripped):
            continue
        without_redirects = re.sub(r"\d*\s*>>?\s*\S+", " ", stripped)
        if re.search(r"\d", without_redirects):
            offenders.append((number, stripped))
    assert not offenders, offenders

    for sweeper in ("pkill", "killall", "killpg"):
        assert sweeper not in source, (
            f"the publisher must not sweep processes by name ({sweeper}); "
            f"it may only start the control"
        )