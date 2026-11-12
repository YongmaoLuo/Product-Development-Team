"""The CI artifact contract holds from the producer to the consumer.

Why this file exists
--------------------
2026-11-09 — the contract for the CI artifact
(``plans/<plan_id>/vp_artifacts/<vp_id>/ci_output.json``) was written down
**three times**: once by the producer (the evidence dictionaries
``run_ci_verification`` builds), once by the consumer (the repair loop
that reads the artifact back) and once by the tests that pinned both.
Three copies of one contract drift, and the drift only shows up in the
whole-repository gate — long after the thing that drifted was written.

Tasks 1, 3 and 5 gave the three parties one contract each:
``backend/ci_artifact.py`` states it, the runner checks its own payload
against it and writes a per-round copy beside the artifact, and the
repair generator reads the structured ``failed_cases`` out of the
artifact instead of scraping the run output.

But three parties with one contract each is still three things that can
disagree **with each other**.  Each of them can be individually correct
and the chain can still be broken: a producer that writes a field the
consumer does not read, a consumer that reads a field the producer never
writes, a round copy that quietly grows a key the artifact contract does
not allow.  Every one of those breaks at the moment somebody reads the
artifact back — the worst possible moment.

So this file pins the **chain**, not the three links of it one by one: a
**real runner** writes the artifact and the **real consumer** reads that
very file back.  Nothing here is a stand-in for the contract: the
artifact on disk is the one the runner produced, and the ids the consumer
returns are the ones the runner's junit report carried.

What is pinned here
-------------------
* the real runner writes both files — the artifact and the round copy —
  and the artifact obeys the contract with no complaint at all;
* the round copy is the artifact **plus** the two bookkeeping keys:
  rejected by the contract by default, accepted when the caller says it
  is looking at a copy, and different in nothing else;
* the consumer reads the artifact the runner wrote and prefers the
  structured ids in it over the ids the run output names;
* when the artifact is gone, the consumer falls back to the run output —
  the round copy does not stand in for the artifact;
* the machine's answer stands on its own: a round whose run output names
  no failing case at all still gets the ids the runner read out of the
  junit report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import repair_generator as rg  # noqa: E402
import verification_ci_runner as ci_runner  # noqa: E402
from ci_artifact import validate_ci_artifact  # noqa: E402

#: The point the producer judges on the schema-failure path — a VP with
#: no entry at all.  Its artifact is written by the runner from the
#: evidence dictionary that path builds by hand.
JUDGED_VP = "VP-777"

#: The point the consumer reads back.  Its artifact is written by the
#: **same real runner**, from a junit report the gate's pytest left in
#: the evidence directory.
CONSUMED_VP = "VP-778"

#: The case the machine wrote down: the node id the junit report
#: carried, rebuilt into the id pytest itself prints.
STRUCTURED_ID = "backend/tests/unit/test_alpha.py::test_one"

#: The case the run output names — a **different** case, so a consumer
#: that reads the text cannot pass by accident.
TEXT_ID = "backend/tests/other.py::test_oops"

#: The run output of the round: pytest's short summary, naming the text
#: case.
RUN_OUTPUT = f"FAILED {TEXT_ID} - assert 1 == 2"


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path) -> Path:
    """The plan directory, with its evidence directory under it.

    The runner takes the evidence directory (``<plan>/vp_artifacts``)
    and the consumer takes the plan directory (``<plan>``) — the two are
    the same plan seen from two sides, and the test keeps them that way
    so the chain is the real one.
    """
    plan = tmp_path / "plan"
    (plan / "vp_artifacts").mkdir(parents=True, exist_ok=True)
    return plan


def _artifact_dir(plan: Path) -> Path:
    """Where the evidence of a plan lands."""
    return plan / "vp_artifacts"


#: A fake CI entry.  It publishes the prepared report into the
#: directory the runner named through ``PDT_GATE_JUNIT_DIR`` — the very
#: variable the real gate script reads — so the test exercises the
#: wiring the production path uses rather than reaching into the
#: reader directly.
#:
#: The exit code is the one the test asked for; when the environment
#: variable is missing the entry exits 99 instead, so a test can tell
#: "the gate ran and failed" from "the gate never learned where to
#: write".
_FAKE_GATE_SOURCE = '''\
import os
import shutil
import sys
from pathlib import Path

target = os.environ.get("PDT_GATE_JUNIT_DIR")
if not target:
    sys.exit(99)
destination = Path(target)
destination.mkdir(parents=True, exist_ok=True)
for report in sorted((Path(__file__).parent / "reports").glob("*.xml")):
    shutil.copyfile(report, destination / report.name)
sys.exit(int(sys.argv[1]))
'''


def _junit_report(node_class: str, node_name: str) -> str:
    """The phase report the gate's pytest would have written."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites>'
        '<testsuite name="unit" tests="1" failures="1" errors="0" skipped="0">'
        f'<testcase classname="{node_class}" name="{node_name}" time="0.01">'
        '<failure message="assert 1 == 2">E   assert 1 == 2</failure>'
        '</testcase>'
        '</testsuite>'
        '</testsuites>'
    )


def _gate_entry(tmp_path: Path, node_class: str, node_name: str) -> str:
    """The ``ci_entry`` of a gate that leaves one phase report behind.

    The report is published **while the runner runs**, into the
    directory the runner named through ``PDT_GATE_JUNIT_DIR``: the
    evidence directory of a VP is reused across rounds, so the runner
    takes the reports of an earlier round out of the way before the
    gate starts.  A report laid down before the run is one of those —
    it is deleted as stale, and the round is left with no evidence of
    its own.

    The source report therefore lives **outside** the evidence
    directory; the gate copies it in while the runner is running.  The
    report is still the producer's input, not the producer's output:
    the artifact is produced by the real runner either way.
    """
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase-1.xml").write_text(
        _junit_report(node_class, node_name), encoding="utf-8",
    )
    gate = tmp_path / "fake_gate.py"
    gate.write_text(_FAKE_GATE_SOURCE, encoding="utf-8")
    return f'"{sys.executable}" "{gate}" 1'


def _run_producer(plan: Path, vp_id: str, *, round_no: int = 1) -> Any:
    """Run the **real** CI runner and let it write the evidence.

    The VP carries a real entry — the fake gate that publishes a phase
    report while the runner runs — so the runner takes the normal path:
    it runs the entry, reads the gate's junit report and writes the
    artifact from the evidence it built.

    Every producer in this file runs the same gate.  A report laid down
    before the run is one of the reports the runner takes out of the
    way as stale, so the entry that produces the evidence has to be the
    one that writes the report.
    """
    entry = _gate_entry(
        plan.parent, "backend.tests.unit.test_alpha", "test_one",
    )
    return ci_runner.run_ci_verification(
        {
            "id": vp_id,
            "verification_method": "full_ci",
            "verification_phase": 2,
            "ci_entry": entry,
        },
        project_dir=plan.parent,
        artifact_dir=_artifact_dir(plan),
        round_no=round_no,
    )


def _read(path: Path) -> Dict[str, Any]:
    """The payload of a JSON file on disk."""
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_path(plan: Path, vp_id: str) -> Path:
    """Where the artifact of ``vp_id`` lands."""
    return _artifact_dir(plan) / vp_id / "ci_output.json"


def _round_copy_path(plan: Path, vp_id: str, round_no: int = 1) -> Path:
    """Where the round copy of ``vp_id`` lands."""
    return _artifact_dir(plan) / vp_id / f"ci_output.round-{round_no}.json"


def _round_items(vp_id: str) -> List[Dict[str, Any]]:
    """A round's failed verification point, as the resolver hands it over."""
    return [{
        "id": vp_id,
        "title": "整仓门禁红",
        "actual_result": "AssertionError: assert 1 == 2",
        "run_output": RUN_OUTPUT,
    }]


# ---------------------------------------------------------------------------
# The producer writes the artifact and the round copy
# ---------------------------------------------------------------------------


def test_producer_writes_main_and_round_copy(tmp_path):
    """One run, two files: the artifact and the round's copy of it.

    The producer is the runner itself — not a stand-in for it.  It
    writes the artifact, and it writes the round copy beside it, because
    the evidence directory is reused across rounds and the next round
    would overwrite this round's evidence.

    The artifact the runner wrote must obey the contract with no
    complaint: it is the file every reader of the plan will open.
    """
    plan = _plan_dir(tmp_path)

    result = _run_producer(plan, JUDGED_VP, round_no=1)

    artifact = _artifact_path(plan, JUDGED_VP)
    copy = _round_copy_path(plan, JUDGED_VP, 1)
    assert artifact.exists(), f"the runner wrote no artifact: {artifact}"
    assert copy.exists(), f"the runner wrote no round copy: {copy}"

    main_payload = _read(artifact)
    copy_payload = _read(copy)

    assert validate_ci_artifact(main_payload) == [], (
        "the artifact the runner wrote must obey the contract: "
        f"{validate_ci_artifact(main_payload)}"
    )
    assert result.evidence == main_payload, (
        "the artifact on disk is the evidence the runner built"
    )

    # The copy is the artifact **plus** the round bookkeeping — the
    # evidence is carried verbatim, so a reader of the copy is reading
    # the same run the artifact describes.
    for key, value in main_payload.items():
        assert copy_payload[key] == value, (
            f"the copy changed the evidence under {key!r}: "
            f"{copy_payload[key]!r} != {value!r}"
        )
    assert copy_payload["round"] == 1
    assert copy_payload["round_source"] == "parameter"

    # The artifact carries no bookkeeping: the contract has no round in
    # it, and a runner that wrote one into the artifact would make the
    # artifact a copy of itself.
    assert "round" not in main_payload, (
        f"the artifact the runner wrote carries no round key: {main_payload}"
    )
    assert "round_source" not in main_payload


# ---------------------------------------------------------------------------
# The copy is a copy: the two files differ by the bookkeeping and by nothing else
# ---------------------------------------------------------------------------


def test_round_copy_allows_round_fields_main_rejects(tmp_path):
    """Both files are checked against the **real** files on disk.

    The contract rejects ``round`` / ``round_source`` by default — they
    are bookkeeping a round copy adds on top of the artifact — and it
    accepts them when the caller says it is looking at a copy.  The
    artifact the runner wrote carries neither, so it passes the contract
    as it stands.

    The assertion on the copy's rejection names the **keys** and not
    their order: the contract is allowed to report the two together or
    one after the other, and a test that pinned the order would be
    pinning the wording rather than the contract.
    """
    plan = _plan_dir(tmp_path)

    _run_producer(plan, JUDGED_VP, round_no=1)

    main_payload = _read(_artifact_path(plan, JUDGED_VP))
    copy_payload = _read(_round_copy_path(plan, JUDGED_VP, 1))

    # The artifact: the contract, as it stands, says nothing.
    assert validate_ci_artifact(main_payload) == [], (
        "the artifact must pass the contract unchanged: "
        f"{validate_ci_artifact(main_payload)}"
    )

    # The copy: rejected by default, and the rejection names both keys.
    issues = validate_ci_artifact(copy_payload)
    assert issues, (
        "the copy carries bookkeeping the artifact contract does not allow, "
        "so the default check must have something to say"
    )
    joined = " ".join(issues)
    assert "round" in joined, f"the rejection must name the round keys: {issues}"
    assert "round_source" in joined, (
        f"the rejection must name the round keys: {issues}"
    )

    # And accepted when the caller says it is looking at a copy.
    assert validate_ci_artifact(copy_payload, allow_round_fields=True) == [], (
        "the copy must pass the contract when it is checked as a copy: "
        f"{validate_ci_artifact(copy_payload, allow_round_fields=True)}"
    )

    # The two bookkeeping keys are the **only** difference: take them off
    # and what is left is the artifact, which the contract accepts.
    stripped = {
        key: value for key, value in copy_payload.items()
        if key not in ("round", "round_source")
    }
    assert validate_ci_artifact(stripped) == [], (
        "the copy must differ from the artifact by the bookkeeping and by "
        f"nothing else: {validate_ci_artifact(stripped)}"
    )


# ---------------------------------------------------------------------------
# The consumer reads what the producer wrote
# ---------------------------------------------------------------------------


def test_consumer_prefers_structured_failed_cases(tmp_path):
    """The artifact the runner wrote is the answer; the prose is the fallback.

    The runner's junit report named ``test_alpha.py::test_one``, so the
    artifact it wrote carries that id.  The round's run output names a
    **different** case.  Both are "the case that failed", and taking both
    would tell the repair agent to turn green a case that is not red —
    so the machine's own answer wins.
    """
    plan = _plan_dir(tmp_path)
    _run_producer(plan, CONSUMED_VP, round_no=1)

    # The producer wrote the machine's answer down — this is the file the
    # consumer is about to read, not a fixture of it.
    payload = _read(_artifact_path(plan, CONSUMED_VP))
    assert payload["failed_cases"] == [STRUCTURED_ID], (
        f"the runner wrote the machine's failing set down: {payload}"
    )
    assert validate_ci_artifact(payload) == [], (
        f"the artifact must obey the contract: {validate_ci_artifact(payload)}"
    )

    ids = rg._failing_case_ids_for(_round_items(CONSUMED_VP), plan)

    assert ids == [STRUCTURED_ID], (
        f"the machine's ids win over the text's: {ids}"
    )
    assert TEXT_ID not in ids, (
        f"the text's ids must not be mixed into the machine's: {ids}"
    )


def test_consumer_falls_back_after_artifact_removed(tmp_path):
    """No artifact on disk: the run output is all there is.

    An artifact that cannot answer is not a defect of the code — it is
    the reason the fallback exists.  The round copy does **not** stand in
    for the artifact: the consumer reads the artifact the runner wrote,
    and when that file is gone the answer comes from the text.
    """
    plan = _plan_dir(tmp_path)
    _run_producer(plan, CONSUMED_VP, round_no=1)

    artifact = _artifact_path(plan, CONSUMED_VP)
    assert artifact.exists(), "the producer wrote the artifact"
    artifact.unlink()

    # The round copy is still on disk — it must not answer for the
    # artifact that is not.
    assert _round_copy_path(plan, CONSUMED_VP, 1).exists()

    ids = rg._failing_case_ids_for(_round_items(CONSUMED_VP), plan)

    assert ids == [TEXT_ID], (
        f"without the artifact the text is the fallback: {ids}"
    )


def test_consumer_answers_from_artifact_when_text_names_nothing(tmp_path):
    """The machine's answer does not need the prose to agree with it.

    The round's run output is prose the runner happened to write: a
    killed run prints no summary at all, and the summary it does print
    is reworded every round.  The artifact is the answer that does not
    depend on that prose — the ids the runner read out of the junit
    report — so a round whose text names nothing still gets them.

    This is the case where a drift between the producer and the consumer
    is **silent**: if the producer ever stopped writing the machine's
    failing set down, the consumer would have nothing to fall back to
    either, and the round would simply lose its failing case ids.
    """
    plan = _plan_dir(tmp_path)
    _run_producer(plan, CONSUMED_VP, round_no=1)

    payload = _read(_artifact_path(plan, CONSUMED_VP))
    assert payload["failed_cases"] == [STRUCTURED_ID], (
        f"the runner wrote the machine's failing set down: {payload}"
    )

    # The round's text names no failing case — only the machine does.
    items = [{
        "id": CONSUMED_VP,
        "title": "整仓门禁红",
        "actual_result": "AssertionError: assert 1 == 2",
        "run_output": "1 failed, 0 passed, 2 warnings in 12.34s",
    }]

    ids = rg._failing_case_ids_for(items, plan)

    assert ids == [STRUCTURED_ID], (
        f"the machine's ids stand on their own: {ids}"
    )
