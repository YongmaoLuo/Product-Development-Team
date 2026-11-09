"""The producer checks its own payload before it writes it down.

Why this file exists
--------------------

2026-11-09: the runner wrote the CI artifact straight from the evidence
dictionary it had just built — ``_write_artifact(artifact_dir, vp_id,
payload)`` — and nobody ever looked at the payload again.  So the
contract for ``ci_output.json`` was copied into three places (the
producer, the consumer, the tests), and when the producer's copy
drifted (``round`` / ``round_source`` appeared in one of them) the
drift was found by the whole-repository gate, long after the thing
that drifted had been written.

``backend/ci_artifact.py`` is now the single copy of the contract.
This file pins the **producer side** of it: the runner says what is
wrong with a payload *before* it writes the payload down, and it says
it loudly enough that a human reading the log can see it.

What is pinned here
-------------------

* an extra key is reported **by name**, at ERROR level — a contract
  that says "something is wrong" without saying what is wrong is not a
  contract;
* a payload that obeys the contract says nothing at all;
* the report **does not change the verdict**: the artifact is still
  written, and it is written byte-for-byte as the payload was — a
  broken contract must not cost the operator the evidence;
* both construction points (the schema-failure path and the normal
  path) go through the validation, not just one of them;
* the writer underneath stays a **pure writer**: it does not know the
  contract exists, it does not add a key, and it does not drop one.

The last one matters for the layer above: a round copy carries two
extra bookkeeping keys (``round`` / ``round_source``), and the writer
has to keep carrying them verbatim — the contract is checked at the
boundary where the runner writes, not by the thing that writes.
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402

#: The name of the runner's logger — the one the contract report goes to.
LOGGER_NAME = "verification_ci_runner"


def _errors(caplog) -> list:
    """Every ERROR record the runner's own logger emitted."""
    return [
        record for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.ERROR
    ]


def _payload() -> dict:
    """The smallest payload that obeys the contract."""
    return {"vp_id": "VP-1", "verifier_source": {}, "failed_cases": []}


def _written(artifact_dir: Path, vp_id: str) -> Path:
    """Where the artifact of ``vp_id`` lands."""
    return Path(artifact_dir) / vp_id / "ci_output.json"


# --------------------------------------------------------------------------
# A payload that breaks the contract says so — and is still written
# --------------------------------------------------------------------------


def test_extra_key_logs_error_and_still_writes(tmp_path, caplog):
    """The extra key is named, and the evidence is still on disk.

    Two things have to be true at once.  The key that is not in the
    contract is **named** — "unexpected keys: bogus" — because a reader
    who has to go and find out which key is wrong has lost the point of
    the report.  And the artifact is still written: the contract being
    broken is a fact about the *producer*, and losing the evidence
    because of it would make the incident unreproducible.
    """
    payload = {**_payload(), "bogus": 1}

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        ci_runner._write_ci_artifacts(tmp_path, "VP-1", payload)

    messages = [record.getMessage() for record in _errors(caplog)]
    assert any(
        "unexpected keys" in message and "bogus" in message
        for message in messages
    ), f"the extra key must be named, got: {messages}"

    target = _written(tmp_path, "VP-1")
    assert target.exists(), "a broken contract must not cost the evidence"
    assert target.read_bytes() == json.dumps(
        payload, indent=2, ensure_ascii=False,
    ).encode("utf-8"), "the artifact must be the payload, verbatim"


def test_valid_payload_logs_no_error(tmp_path, caplog):
    """A payload that obeys the contract says nothing at all.

    A contract report that fires on every run is noise, and noise is
    how a real report gets missed.
    """
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        ci_runner._write_ci_artifacts(tmp_path, "VP-1", _payload())

    assert _errors(caplog) == [], (
        f"a compliant payload must be silent, got: "
        f"{[r.getMessage() for r in _errors(caplog)]}"
    )
    assert _written(tmp_path, "VP-1").exists()


def test_no_artifact_dir_validates_without_writing(tmp_path, caplog):
    """No directory means no file — but the check still happens.

    The runner is called with ``artifact_dir=None`` when the caller
    does not want an audit copy.  The contract is still worth checking
    there: the report is about the payload, not about the disk.
    """
    payload = {**_payload(), "bogus": 1}

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        ci_runner._write_ci_artifacts(None, "VP-1", payload)

    messages = [record.getMessage() for record in _errors(caplog)]
    assert any("bogus" in message for message in messages), (
        f"the payload is still checked, got: {messages}"
    )
    assert list(tmp_path.iterdir()) == [], "nothing may be written"


# --------------------------------------------------------------------------
# Both construction points go through the check
# --------------------------------------------------------------------------


def test_schema_failure_path_writes_artifact(tmp_path):
    """The path that never ran a case still produces a contract-shaped file.

    This is the path the 2026-11-09 incident lived on: the evidence
    dictionary is built by hand on the schema-failure path, so it is
    exactly the kind of payload that drifts from the contract.  The
    artifact it writes must obey the contract and must not carry the
    round bookkeeping keys — those belong to a round *copy*, never to
    the artifact the runner wrote.
    """
    result = ci_runner.run_ci_verification(
        {"id": "VP-901"}, project_dir=tmp_path, artifact_dir=tmp_path,
    )

    target = _written(tmp_path, "VP-901")
    assert target.exists(), "the schema-failure path must write an artifact"

    data = json.loads(target.read_text(encoding="utf-8"))
    assert "round" not in data, (
        f"the artifact the runner wrote carries no round key: {data}"
    )
    assert "round_source" not in data
    assert data.get("vp_id") == "VP-901"
    assert result.evidence == data, "the artifact is the evidence"


def test_both_construction_points_route_through_validation(tmp_path, monkeypatch):
    """Both places the runner builds evidence go through the check.

    Two construction points exist: the schema-failure path and the
    normal path.  Validating one of them is not validating the runner —
    a check that covers half the write sites covers the half nobody
    happened to trip over.
    """
    calls = []

    def spy(*args, **kwargs):
        """A stand-in for the writer.

        ``*args`` / ``**kwargs`` on purpose: the writer grows keyword
        arguments over time, and a spy that names them would break the
        moment it does.
        """
        calls.append((args, kwargs))

    monkeypatch.setattr(ci_runner, "_write_ci_artifacts", spy)

    # The schema-failure path: a VP with no entry at all.
    ci_runner.run_ci_verification(
        {"id": "VP-901"}, project_dir=tmp_path, artifact_dir=tmp_path,
    )
    # The normal path: an entry that runs and exits 0.
    ci_runner.run_ci_verification(
        {
            "id": "VP-902",
            "verification_method": "full_ci",
            "verification_phase": 2,
            "ci_entry": "true",
        },
        project_dir=tmp_path,
        artifact_dir=tmp_path,
    )

    assert len(calls) == 2, (
        f"both construction points must go through the writer, got {len(calls)}"
    )
    payloads = [args[2] for args, _ in calls]
    assert "schema_issues" in payloads[0], "the first call is the schema path"
    assert "exit_code" in payloads[1], "the second call is the normal path"


# --------------------------------------------------------------------------
# The writer underneath stays a pure writer
# --------------------------------------------------------------------------


def test_write_artifact_keeps_payload_verbatim(tmp_path):
    """The writer does not know the contract, so it keeps every key.

    A round copy carries two bookkeeping keys on top of the artifact
    (``round`` / ``round_source``).  The writer has to carry them
    verbatim: the contract is checked where the runner builds the
    payload, not by the thing that puts it on disk.  If the writer
    started dropping unknown keys, the round copy would lose its
    provenance on the way through.
    """
    payload = {**_payload(), "round": 2, "round_source": "previous"}

    ci_runner._write_artifact(tmp_path, "VP-1", payload)

    target = _written(tmp_path, "VP-1")
    assert target.read_bytes() == json.dumps(
        payload, indent=2, ensure_ascii=False,
    ).encode("utf-8"), "the payload must land byte-for-byte"


def test_write_artifact_does_not_reference_schema():
    """The writer does not know the contract exists.

    One writer, one job: put the payload on disk.  The moment it also
    decides what a legal payload is, the contract has two copies again
    — and the copy that decides is the copy nobody reads.
    """
    source = inspect.getsource(ci_runner._write_artifact)

    assert "ci_artifact" not in source, (
        "the writer must not reference the contract module:\n" + source
    )
