"""The CI artifact contract has one copy, and it is enforced.

Why this file exists
--------------------

The contract for the CI artifact (``plans/<plan_id>/vp_artifacts/<vp_id>/ci_output.json``)
was written down **three times**: once by the producer (the evidence
dictionaries ``run_ci_verification`` builds), once by the consumer (the
orchestrator that reads the artifact back), and once by the tests that
pinned both.  Three copies of one contract drift — and the drift only
shows up in the whole-repository gate, long after the thing that
drifted was written.

``backend/ci_artifact.py`` is the single copy.  This file pins it.

What is pinned here
-------------------

* the three required keys alone are a valid payload;
* an extra key is reported **by name** — a contract that says "something
  is wrong" without saying what is wrong is not a contract;
* a missing key is reported **by name**, and all the missing keys come
  back in one message;
* the round-copy extras (``round`` / ``round_source``) are rejected by
  default and accepted only when the caller says it is looking at a
  round copy;
* a payload that is not a dict is reported as one issue that names the
  type it got;
* both names of the entry slot (``ci_entry`` / ``entry_command``) stay
  in the allowed set, because they are the same slot at runtime;
* the allowed set is **exactly** the set of keys the producer writes —
  neither a key too many nor a key too few, and the set is derived
  from the producer rather than copied here, so this file does not
  become the third copy of the contract it exists to prevent;
* a real artifact the runner writes obeys the contract.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402
from ci_artifact import (  # noqa: E402
    CI_ARTIFACT_FIELDS,
    CI_ARTIFACT_REQUIRED_FIELDS,
    ROUND_COPY_EXTRA_FIELDS,
    validate_ci_artifact,
)


def _payload() -> dict:
    """The smallest payload that obeys the contract."""
    return {"vp_id": "VP-1", "verifier_source": {}, "failed_cases": []}


def test_valid_payload_passes():
    assert validate_ci_artifact(_payload()) == []


def test_extra_key_is_named():
    issues = validate_ci_artifact({**_payload(), "bogus": 1})
    assert issues
    assert "bogus" in " ".join(issues)


def test_missing_key_is_named():
    payload = _payload()
    payload.pop("failed_cases")
    issues = validate_ci_artifact(payload)
    assert issues
    joined = " ".join(issues)
    assert "missing keys" in joined
    assert "failed_cases" in joined


def test_round_fields_only_when_allowed():
    payload = {**_payload(), "round": 2, "round_source": "copy"}
    issues = validate_ci_artifact(payload)
    assert issues
    # One message, and it names both round extras: they are rejected
    # together, so a reader is not left wondering which one was wrong.
    assert len(issues) == 1
    assert "round" in issues[0]
    assert "round_source" in issues[0]
    assert validate_ci_artifact(payload, allow_round_fields=True) == []


def test_non_dict_payload_is_reported():
    issues = validate_ci_artifact([])
    assert len(issues) == 1
    assert "list" in issues[0]


def test_non_string_key_is_reported_as_an_unexpected_key_not_a_crash():
    """A key that is not a string is a key the contract does not allow.

    The validator used to raise ``TypeError: sequence item 0: expected
    str instance, int found`` here — a validator that crashes on the
    payload it is handed tells the caller nothing and takes the caller
    down with it.  The key is reported instead, with its ``repr`` so
    the message says what the key actually was.
    """
    payload = {1: "x", **_payload()}
    issues = validate_ci_artifact(payload)
    assert issues
    joined = " ".join(issues)
    assert "unexpected keys" in joined
    assert repr(1) in joined


def test_non_string_key_does_not_crash_the_validator():
    """The same thing again, but stated as "it returns, it does not raise".

    ``repr`` of an int is ``1``, so the message and this assertion
    agree on what the reader sees.
    """
    issues = validate_ci_artifact({1: "x", "vp_id": "VP-1"})
    assert isinstance(issues, list)
    assert issues
    assert any("1" in issue for issue in issues)


def test_required_fields_are_a_subset_of_the_allowed_fields():
    assert CI_ARTIFACT_REQUIRED_FIELDS <= CI_ARTIFACT_FIELDS
    assert CI_ARTIFACT_REQUIRED_FIELDS == {
        "vp_id", "verifier_source", "failed_cases",
    }


def test_round_copy_extras_are_not_part_of_the_artifact_contract():
    assert ROUND_COPY_EXTRA_FIELDS == {"round", "round_source"}
    assert not (ROUND_COPY_EXTRA_FIELDS & CI_ARTIFACT_FIELDS)


def test_both_names_of_the_entry_slot_are_allowed():
    """``ci_entry`` and ``entry_command`` are one slot at runtime.

    The runner writes the entry under ``entry_key``, which is one name
    or the other depending on the kind of the entry — so a contract
    that allowed only one of them would reject half of the artifacts
    the runner writes.
    """
    assert ci_runner.CI_ENTRY_FIELD == "ci_entry"
    assert ci_runner.ENTRY_COMMAND_FIELD == "entry_command"
    assert ci_runner.CI_ENTRY_FIELD in CI_ARTIFACT_FIELDS
    assert ci_runner.ENTRY_COMMAND_FIELD in CI_ARTIFACT_FIELDS


def _produced_evidence_keys() -> set:
    """The keys the runner's own evidence dictionaries carry.

    Read out of the runner's **source**, not out of a run: the schema
    failure path writes four of them and the normal path writes the
    rest, and neither path can be reached here without either running
    a gate or copying the list — and copying the list is the thing
    this whole module exists to stop.

    So the keys are collected from the source: the top-level keys of
    every dict literal the runner assigns to an evidence variable, plus
    the keys of every ``evidence["..."] = ...`` assignment.  The
    ``entry_key`` slot contributes both of its names, because the
    runner writes one of them or the other.
    """
    source = (BACKEND_DIR / "verification_ci_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def function(name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} is gone from the runner")

    def is_evidence(variable):
        if isinstance(variable, ast.Name):
            return variable.id == "evidence"
        if isinstance(variable, ast.Attribute):
            return variable.attr == "evidence"
        return False

    def top_level_keys(function_node):
        keys = set()
        for node in ast.walk(function_node):
            value = None
            if isinstance(node, ast.Assign) and is_evidence(node.targets[0]):
                value = node.value
            elif isinstance(node, ast.AnnAssign) and is_evidence(node.target):
                value = node.value
            if isinstance(value, ast.Dict):
                for key in value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and is_evidence(target.value):
                        index = target.slice
                        if isinstance(index, ast.Constant) and isinstance(index.value, str):
                            keys.add(index.value)
        return keys

    produced = (
        top_level_keys(function("run_ci_verification"))
        | top_level_keys(function("_read_junit_evidence"))
    )
    produced |= {ci_runner.CI_ENTRY_FIELD, ci_runner.ENTRY_COMMAND_FIELD}
    return produced


def test_the_allowed_set_is_exactly_what_the_producer_writes():
    """Exact equality — a key too many and a key too few are both drift.

    A subset would pass while the producer grew a key the contract
    never heard of, which is how the three copies of this contract
    drifted in the first place.
    """
    produced = _produced_evidence_keys()
    assert CI_ARTIFACT_FIELDS == produced


def test_a_real_artifact_obeys_the_contract(tmp_path):
    """The artifact the runner writes passes the contract it defines.

    The schema failure path is the one that reaches a verdict without
    running anything, so it is the one that can be exercised here.
    """
    project = tmp_path / "project"
    project.mkdir()
    artifacts = tmp_path / "vp_artifacts"

    ci_runner.run_ci_verification(
        {"id": "VP-1"},
        project_dir=project,
        artifact_dir=artifacts,
    )

    artifact = artifacts / "VP-1" / "ci_output.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert set(payload) <= CI_ARTIFACT_FIELDS
    assert validate_ci_artifact(payload) == []
