"""The CI runner keeps one copy of the artifact per round, beside it.

Why this file exists
--------------------
2026-11-09 — the evidence directory of a VP is **reused across rounds**:
``<plan>/vp_artifacts/<vp_id>/`` is the same directory in round 1 and in
round 2.  So the runner writing ``ci_output.json`` in round 2 **overwrites
the file round 1 wrote** — and the round-1 evidence, the thing that says
"what did the gate say when it rejected this delivery", is gone.

The copy has to be somewhere else, and it has to say which round it is
from.  So every run writes **two** files:

* ``ci_output.json`` — the artifact, exactly as the runner produced it.
  It carries **no** round bookkeeping: the artifact the runner writes is
  the artifact the contract describes, and the contract does not have
  ``round`` in it.
* ``ci_output.round-<n>.json`` — the same evidence plus ``round`` and
  ``round_source``, so a reader can tell the rounds apart and can tell
  **where the round number came from** (the caller, or the context pack).

The copy is written with a hard link, not a replace: a copy that already
exists is **evidence from an earlier round** and is kept as it is.  A
round that overwrites its own predecessor's evidence is the very thing
this file exists to prevent.

What is pinned here
-------------------

* the copy carries the round bookkeeping, the main artifact does not;
* the **normal** path writes a copy too, not only the schema-failure path;
* a round number that was not passed in is resolved from the context pack
  (``<plan>/context/round-<n>.json``), and the **largest** ``n`` wins;
* an existing copy is **not** overwritten;
* when no round is known, no copy is written at all;
* a ``bool`` is not a round number either: ``bool`` is a subclass of
  ``int``, so a bool must not be resolved as a round and must not name a
  copy;
* a copy that cannot be written changes **nothing** about the verdict, and
  leaves no ``.tmp`` residue behind.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402

#: The name of the runner's logger — the one the reports go to.
LOGGER_NAME = "verification_ci_runner"


def _artifact_dir(tmp_path: Path) -> Path:
    """The artifact directory of a plan.

    It is a **sub-directory** of the plan directory on purpose: the
    runner derives the plan directory from the artifact directory's
    parent, and the plan directory is where the context packs live.  A
    flat ``tmp_path`` would make the plan directory the pytest base
    directory, which is not this test's to write into.
    """
    artifact_dir = tmp_path / "vp_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _vp(vp_id: str = "VP-901", entry: str = "true") -> dict:
    """A ``full_ci`` VP that runs ``entry`` and exits 0."""
    return {
        "id": vp_id,
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }


def _main_artifact(artifact_dir: Path, vp_id: str = "VP-901") -> Path:
    """Where the artifact of ``vp_id`` lands."""
    return Path(artifact_dir) / vp_id / "ci_output.json"


def _round_copy(
    artifact_dir: Path, round_no: int, vp_id: str = "VP-901",
) -> Path:
    """Where the copy of round ``round_no`` lands."""
    return Path(artifact_dir) / vp_id / f"ci_output.round-{round_no}.json"


def _written_copies(artifact_dir: Path) -> list:
    """Every round copy under ``artifact_dir``."""
    return sorted(Path(artifact_dir).rglob("ci_output.round-*.json"))


def _warnings(caplog) -> list:
    """Every WARNING record the runner's own logger emitted."""
    return [
        record for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]


def _errors(caplog) -> list:
    """Every ERROR record the runner's own logger emitted."""
    return [
        record for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.ERROR
    ]


# --------------------------------------------------------------------------
# The copy carries the round bookkeeping, the artifact does not
# --------------------------------------------------------------------------


def test_round_copy_carries_round_fields_main_does_not(tmp_path):
    """Two files, two jobs: the artifact is the artifact, the copy is dated.

    The artifact the runner writes must obey the contract as it stands —
    and the contract has no ``round`` in it.  The copy is the one that
    says which round it came from, and it says it twice: the number, and
    where the number came from.
    """
    artifact_dir = _artifact_dir(tmp_path)

    ci_runner.run_ci_verification(
        {"id": "VP-901"},
        project_dir=tmp_path,
        artifact_dir=artifact_dir,
        round_no=2,
    )

    main = json.loads(
        _main_artifact(artifact_dir).read_text(encoding="utf-8"),
    )
    assert "round" not in main, (
        f"the artifact the runner wrote carries no round key: {main}"
    )
    assert "round_source" not in main, (
        f"the artifact the runner wrote carries no round_source key: {main}"
    )

    copy = json.loads(
        _round_copy(artifact_dir, 2).read_text(encoding="utf-8"),
    )
    assert copy["round"] == 2
    assert copy["round_source"] == "parameter", (
        f"a round number that was passed in comes from the caller: {copy}"
    )

    # The copy carries the **same evidence** as the artifact — it is a
    # copy, not a summary.
    for key, value in main.items():
        assert copy[key] == value, (
            f"the copy changed the evidence under {key!r}: "
            f"{copy[key]!r} != {value!r}"
        )


def test_round_copy_passes_the_round_field_validation(tmp_path, caplog):
    """The copy is validated as a copy, not as an artifact.

    The contract rejects ``round`` / ``round_source`` by default — they
    are bookkeeping a copy adds on top of the artifact.  So the copy is
    checked with ``allow_round_fields=True``, and a copy that is checked
    that way says nothing at all.
    """
    artifact_dir = _artifact_dir(tmp_path)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        ci_runner.run_ci_verification(
            {"id": "VP-901"},
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
            round_no=1,
        )

    complaints = [
        message for message in
        (record.getMessage() for record in _errors(caplog))
        if "unexpected keys" in message
    ]
    assert complaints == [], (
        f"the round bookkeeping must not be reported as unexpected: {complaints}"
    )


# --------------------------------------------------------------------------
# The normal path writes a copy too
# --------------------------------------------------------------------------


def test_normal_path_also_writes_round_copy(tmp_path):
    """The path that ran the gate is the path that matters.

    The schema-failure path is the rare one; the normal path — the gate
    ran, it said something, the artifact says what — is the one that
    happens every round.  If only the rare path wrote a copy, the
    evidence of every real round would still be overwritten.
    """
    artifact_dir = _artifact_dir(tmp_path)

    result = ci_runner.run_ci_verification(
        _vp(), project_dir=tmp_path, artifact_dir=artifact_dir, round_no=1,
    )

    assert result.status == ci_runner.STATUS_PASSED, (
        f"the gate exited 0, so the verdict is PASSED: {result.reasons}"
    )
    copy = json.loads(
        _round_copy(artifact_dir, 1).read_text(encoding="utf-8"),
    )
    assert copy["round"] == 1
    assert copy["round_source"] == "parameter"
    assert copy["exit_code"] == 0, (
        f"the copy carries the evidence of the run: {copy}"
    )

    main = json.loads(
        _main_artifact(artifact_dir).read_text(encoding="utf-8"),
    )
    assert "round" not in main


# --------------------------------------------------------------------------
# A round number that was not passed in comes from the context pack
# --------------------------------------------------------------------------


def test_round_resolved_from_context_pack(tmp_path):
    """The context pack knows the round; the runner asks it.

    A caller that does not know its round number must not have to guess:
    the plan's own context packs are named after the round
    (``<plan>/context/round-<n>.json``), so the **largest** one is the
    round that is running now.
    """
    artifact_dir = _artifact_dir(tmp_path)
    context_dir = tmp_path / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "round-1.json").write_text(
        json.dumps({"round_no": 1}), encoding="utf-8",
    )
    (context_dir / "round-3.json").write_text(
        json.dumps({"round_no": 3}), encoding="utf-8",
    )

    ci_runner.run_ci_verification(
        {"id": "VP-901"}, project_dir=tmp_path, artifact_dir=artifact_dir,
    )

    copy = json.loads(
        _round_copy(artifact_dir, 3).read_text(encoding="utf-8"),
    )
    assert copy["round"] == 3, (
        f"the largest context pack names the current round: {copy}"
    )
    assert copy["round_source"] == "context_pack", (
        f"a round number that was not passed in comes from the pack: {copy}"
    )
    assert _written_copies(artifact_dir) == [_round_copy(artifact_dir, 3)]


# --------------------------------------------------------------------------
# An existing copy is evidence from an earlier round — it is kept
# --------------------------------------------------------------------------


def test_existing_round_copy_is_not_overwritten(tmp_path, caplog):
    """The copy of an earlier round is not this round's to overwrite.

    The whole point of the copy is that round 1's evidence survives round
    2.  So a copy that is already there is kept **byte for byte** — and
    the runner says so, because a reader who wonders why the file did
    not change has to be able to find the answer.
    """
    artifact_dir = _artifact_dir(tmp_path)
    target = _round_copy(artifact_dir, 2)
    target.parent.mkdir(parents=True, exist_ok=True)
    earlier = json.dumps({"round": 2, "round_source": "earlier"}, indent=2)
    target.write_text(earlier, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        ci_runner.run_ci_verification(
            {"id": "VP-901"},
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
            round_no=2,
        )

    assert target.read_text(encoding="utf-8") == earlier, (
        "an existing round copy must be kept exactly as it is"
    )
    warnings = [record.getMessage() for record in _warnings(caplog)]
    assert any(
        "ci_output.round-2.json" in message for message in warnings
    ), f"the runner must say that it kept the existing copy: {warnings}"


# --------------------------------------------------------------------------
# No round known → no copy
# --------------------------------------------------------------------------


def test_no_round_known_writes_no_copy(tmp_path):
    """Without a round there is nothing to name the copy after.

    Writing ``ci_output.round-0.json`` would be a lie: round 0 is not a
    round.  So when no round number is known, no copy is written — and
    the artifact is still written, because the artifact does not depend
    on the round.
    """
    artifact_dir = _artifact_dir(tmp_path)

    ci_runner.run_ci_verification(
        {"id": "VP-901"}, project_dir=tmp_path, artifact_dir=artifact_dir,
    )

    assert _written_copies(artifact_dir) == [], (
        "no round is known, so no copy may be written"
    )
    assert _main_artifact(artifact_dir).exists(), (
        "the artifact does not depend on the round"
    )


def test_zero_or_negative_round_falls_back_to_the_pack(tmp_path):
    """``0`` and a negative number are not rounds.

    A caller that passes ``0`` — because the counter starts at zero, or
    because it does not know — must not get a ``round-0`` copy.  The
    round is then resolved from the context pack, exactly as if nothing
    had been passed in.
    """
    artifact_dir = _artifact_dir(tmp_path)
    context_dir = tmp_path / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "round-2.json").write_text(
        json.dumps({"round_no": 2}), encoding="utf-8",
    )

    for bad in (0, -1):
        ci_runner.run_ci_verification(
            {"id": "VP-901"},
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
            round_no=bad,
        )
        assert not _round_copy(artifact_dir, bad).exists(), (
            f"{bad!r} is not a round number, so no copy may be named after it"
        )

    copy = json.loads(
        _round_copy(artifact_dir, 2).read_text(encoding="utf-8"),
    )
    assert copy["round"] == 2
    assert copy["round_source"] == "context_pack"


# --------------------------------------------------------------------------
# A bool is not a round number — bool is a subclass of int
# --------------------------------------------------------------------------


def test_bool_round_resolves_to_no_round_without_a_pack(tmp_path):
    """``True`` is not a round, and with no pack to ask there is none.

    ``bool`` is a **subclass** of ``int``, so ``True > 0`` holds and
    ``isinstance(True, int)`` is ``True``: a guard that only asked "is it
    a positive int?" would resolve ``True`` as **round 1** — and then
    write ``ci_output.round-True.json``, a file name no contract
    describes.  So the resolution rejects a bool outright, and with no
    context pack to ask, the answer is "no round at all".
    """
    artifact_dir = _artifact_dir(tmp_path)

    assert ci_runner._resolve_round(True, artifact_dir) == (None, None)
    assert ci_runner._resolve_round(False, artifact_dir) == (None, None)


def test_bool_round_writes_no_round_true_copy(tmp_path):
    """A bool round never names a copy on disk.

    The name would be ``ci_output.round-True.json``: the evidence
    directory is reused across rounds, and a file named after a bool is
    a file nobody ever looks for.  So no copy is written — while the
    artifact still is, because the artifact does not depend on the round.
    """
    artifact_dir = _artifact_dir(tmp_path)

    ci_runner.run_ci_verification(
        {"id": "VP-901"},
        project_dir=tmp_path,
        artifact_dir=artifact_dir,
        round_no=True,
    )

    assert not _round_copy(artifact_dir, True).exists(), (
        "a bool is not a round number, so no copy may be named after it"
    )
    assert _written_copies(artifact_dir) == [], (
        f"a bool round must not write any copy: {_written_copies(artifact_dir)}"
    )
    assert _main_artifact(artifact_dir).exists(), (
        "the artifact does not depend on the round"
    )


def test_bool_round_falls_back_to_the_pack(tmp_path):
    """A bool round falls back to the pack, and says so.

    The pack knows the real round; the bool does not.  So the copy is
    named after the pack's round and says where its number came from —
    ``context_pack``, never ``parameter``: a bool is not a number the
    caller passed in.
    """
    artifact_dir = _artifact_dir(tmp_path)
    context_dir = tmp_path / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "round-2.json").write_text(
        json.dumps({"round_no": 2}), encoding="utf-8",
    )

    assert ci_runner._resolve_round(True, artifact_dir) == (2, "context_pack")

    for bad in (True, False):
        ci_runner.run_ci_verification(
            {"id": "VP-901"},
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
            round_no=bad,
        )

    copy = json.loads(
        _round_copy(artifact_dir, 2).read_text(encoding="utf-8"),
    )
    assert copy["round"] == 2
    assert copy["round_source"] == "context_pack", (
        f"a bool round comes from the pack, not from the caller: {copy}"
    )
    assert _written_copies(artifact_dir) == [_round_copy(artifact_dir, 2)]


# --------------------------------------------------------------------------
# A copy that cannot be written changes nothing but the copy
# --------------------------------------------------------------------------


def test_round_copy_failure_does_not_change_the_verdict(tmp_path, monkeypatch,
                                                          caplog):
    """Losing the copy is a fact about the disk, not about the code.

    The verdict was computed before the copy was written, and it must
    stay the same: a gate that exited 0 is a gate that exited 0.  What
    may not happen is a ``.tmp`` left behind — the residue is invisible
    to the orphan sweep and grows silently across rounds.
    """
    artifact_dir = _artifact_dir(tmp_path)

    def _broken_link(*args, **kwargs):
        raise OSError("the disk said no")

    monkeypatch.setattr(ci_runner.os, "link", _broken_link)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = ci_runner.run_ci_verification(
            _vp(), project_dir=tmp_path, artifact_dir=artifact_dir, round_no=1,
        )

    assert result.status == ci_runner.STATUS_PASSED, (
        f"the verdict must not change because a copy could not be written: "
        f"{result.reasons}"
    )
    assert _written_copies(artifact_dir) == []
    assert not list(Path(artifact_dir).rglob("*.tmp")), (
        "a failed copy must not leave .tmp residue behind"
    )
    assert _warnings(caplog), "the runner must say that the copy failed"


def test_successful_round_copy_leaves_no_tmp_residue(tmp_path):
    """A copy that was written leaves nothing but the copy behind.

    The copy is written by hard-linking a temp file, so the temp file's
    name is still there after the link — it has to be removed, or every
    round leaves a ``.tmp`` behind.
    """
    artifact_dir = _artifact_dir(tmp_path)

    ci_runner.run_ci_verification(
        {"id": "VP-901"},
        project_dir=tmp_path,
        artifact_dir=artifact_dir,
        round_no=1,
    )

    residue = sorted(
        path.relative_to(artifact_dir).as_posix()
        for path in Path(artifact_dir).rglob("*.tmp")
    )
    assert residue == [], f"the copy left .tmp residue behind: {residue}"
