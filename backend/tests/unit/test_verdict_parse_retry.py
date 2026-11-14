"""2026-10-22 (P0-1): a verdict-parse failure is a MALFUNCTION of the
attempt, and the L2 retry it earns must be reachable.

The bug this pins down
----------------------
``VerificationSubAgent._execute_attempt`` used to catch
:class:`VerdictParseError` and answer with a locally built
``Verdict(verdict="FAILED", reasons=["verdict parse error: ..."])``.
That construction swallowed the failure: the caller
(``_self_heal_loop``) saw a well-formed verdict, so its ``except
VerdictParseError`` branch — the L2 retry, the one that exists exactly
for a transient protocol garble (truncated stream, stray prose around
the JSON object) — was unreachable. One garbled response failed the
verification point for good, and the verdict it produced claimed the
deliverable was broken when nothing was known about the deliverable at
all.

The contract pinned here
------------------------
  * a payload that cannot be parsed raises out of ``_execute_attempt``
    instead of becoming a verdict (no inline ``FAILED`` verdict);
  * the raise reaches ``_self_heal_loop``, which retries the attempt
    (L2) and logs a ``verdict_parse_retry`` event per retry;
  * a retry that succeeds produces that attempt's verdict — a garble
    followed by a good payload is a PASSED, not a FAILED;
  * a budget that ran out on malfunctions ends as a hard FAILED
    classified ``infra`` (the attempt malfunctioned, the deliverable is
    not implicated) with the source ``deterministic`` (the class was
    derived by rule, not read off a claim).

The negative control (``# NEGCTL-P0-1-BEGIN`` / ``# NEGCTL-P0-1-END``)
lives in the production source: the block between the two markers is
the except arm, and it must not build a ``Verdict`` of its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent as _verification_subagent  # noqa: E402
from verification_subagent import (  # noqa: E402
    VerificationSubAgent,
    VerdictParseError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedCodingTool:
    """A coding tool that answers with a scripted list of payloads.

    ``query_json`` is called on a worker thread (the real coding tool
    blocks), but only one call is ever in flight, so a plain list is
    enough to script the responses.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def query_json(self, prompt=None, system_instruction=None, timeout=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError(
                "the scripted coding tool ran out of responses"
            )
        return self._responses.pop(0)


def _read_events(log_path: Path) -> List[Dict[str, Any]]:
    """The JSON-lines entries the attempt log holds."""
    events: List[Dict[str, Any]] = []
    if not log_path.exists():
        return events
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _count_event(log_path: Path, event: str) -> int:
    return len([e for e in _read_events(log_path) if e.get("event") == event])


def _vp_node() -> Dict[str, Any]:
    return {
        "id": "VP-001",
        "title": "the module exposes its public API",
        "verification_method": "code_review",
        "expected_result": "the public API is reachable",
    }


#: A payload whose ``verdict`` is a list, not a string — rejected by
#: ``parse_verdict`` (the field must be a str).
MALFORMED_PAYLOAD: Dict[str, Any] = {"verdict": ["FAILED"]}

#: A well-formed payload.
VALID_PAYLOAD: Dict[str, Any] = {
    "verdict": "PASSED",
    "reasons": [],
    "evidence": [],
}

#: A well-formed FAILED payload.
FAILED_PAYLOAD: Dict[str, Any] = {
    "verdict": "FAILED",
    "reasons": ["the public API is not reachable"],
    "evidence": [],
}


# ---------------------------------------------------------------------------
# 1. A malformed payload is retried, and the retry's verdict counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_error_retries_then_passes(tmp_path):
    """Garbled response → L2 retry → the second attempt's verdict.

    The final verdict must be the second attempt's PASSED, and the
    attempt log must show exactly one ``verdict_parse_retry``.
    """
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    tool = _ScriptedCodingTool([MALFORMED_PAYLOAD, VALID_PAYLOAD])

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )

    assert tool.calls == 2, (
        f"the garbled attempt must be retried exactly once; the tool was "
        f"called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        f"the retry's verdict is the one that counts; got "
        f"{verdict.verdict!r}"
    )
    assert verdict.failure_class == "", (
        f"a passed verdict has no failure to classify; got "
        f"{verdict.failure_class!r}"
    )
    assert _count_event(log_path, "verdict_parse_retry") == 1, (
        "the L2 retry must be observable in the attempt log"
    )


# ---------------------------------------------------------------------------
# 2. A budget that ran out on malfunctions ends as infra / deterministic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_marks_infra(tmp_path):
    """Every attempt garbled → hard FAILED, classified ``infra``.

    The attempt malfunctioned; the deliverable is not implicated, and
    the class is marked ``deterministic`` because it was derived by rule
    rather than read off a claim.
    """
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    tool = _ScriptedCodingTool(
        [MALFORMED_PAYLOAD, {"verdict": {"wrong": "shape"}}]
    )

    verdict = await sub._self_heal_loop(
        vp_node=_vp_node(),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=None,
    )

    assert tool.calls == 2, (
        f"the budget is max_retries + 1 attempts; the tool was called "
        f"{tool.calls} times"
    )
    assert verdict.verdict == "FAILED", (
        f"an exhausted budget is a hard FAILED, never a SKIPPED; got "
        f"{verdict.verdict!r}"
    )
    assert verdict.failure_class == "infra", (
        f"the attempt malfunctioned, so the class is infra; got "
        f"{verdict.failure_class!r}"
    )
    assert verdict.failure_class_source == "deterministic", (
        f"the class was derived by rule, not claimed; got "
        f"{verdict.failure_class_source!r}"
    )
    assert any(
        "verdict unparseable after 2 attempts" in reason
        for reason in verdict.reasons
    ), f"the budget-exhausted marker is missing: {verdict.reasons}"


# ---------------------------------------------------------------------------
# 3. The except arm does not build a verdict of its own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_inline_failed_verdict(tmp_path):
    """``_execute_attempt`` must raise, not answer with a verdict.

    Two checks, one behavioural and one structural:

      * calling ``_execute_attempt`` directly with a malformed payload
        raises ``VerdictParseError`` — the caller decides what a
        malformed payload means, and the only caller that may answer
        with a verdict is the retry loop;
      * the source block between the two negative-control markers
        (``# NEGCTL-P0-1-BEGIN`` / ``# NEGCTL-P0-1-END``) constructs no
        ``Verdict`` — the marker pair is what the negative-control
        script locates, so the guarantee has to live inside it.
    """
    sub = VerificationSubAgent(method="code_review", max_retries=1)
    tool = _ScriptedCodingTool([MALFORMED_PAYLOAD])

    with pytest.raises(VerdictParseError):
        await sub._execute_attempt(
            vp_node=_vp_node(),
            coding_tool=tool,
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp.log",
            attempt=0,
            project_dir=None,
        )

    source = Path(_verification_subagent.__file__).read_text(encoding="utf-8")
    begin_marker = "# NEGCTL-P0-1-BEGIN"
    end_marker = "# NEGCTL-P0-1-END"
    assert begin_marker in source, (
        f"the negative-control begin marker {begin_marker!r} is missing "
        f"from {Path(_verification_subagent.__file__).name}"
    )
    assert end_marker in source, (
        f"the negative-control end marker {end_marker!r} is missing "
        f"from {Path(_verification_subagent.__file__).name}"
    )
    block = source.split(begin_marker, 1)[1].split(end_marker, 1)[0]
    assert "Verdict(" not in block, (
        "the parse-error arm must not build a verdict of its own:\n"
        f"{block}"
    )
    assert "raise" in block, (
        "the parse-error arm must re-raise so the L2 retry is "
        f"reachable:\n{block}"
    )
    assert "VerdictParseError" in block, (
        "the marked block must be the VerdictParseError handler itself "
        "— that is what the negative-control script locates:\n"
        f"{block}"
    )


# ---------------------------------------------------------------------------
# 4. The classification literals match the failure-triage vocabulary
# ---------------------------------------------------------------------------


def test_the_infra_literals_match_the_failure_triage_vocabulary():
    """The drift guard.

    ``verification_subagent`` cannot import ``verification.failure_triage``
    at module level (the import is circular), so the two values are
    spelled as literals here. This test is what keeps them the same
    values — the same pattern ``tests/unit/test_verdict_fields.py`` uses
    for the default class.
    """
    from verification import failure_triage

    assert (
        _verification_subagent.INFRA_FAILURE_CLASS
        == failure_triage.FailureClass.INFRA
    )
    assert (
        _verification_subagent.DETERMINISTIC_FAILURE_CLASS_SOURCE
        == failure_triage.SOURCE_DETERMINISTIC
    )


# ---------------------------------------------------------------------------
# 5. The evidence contract gets its OWN retry (2026-11-08)
# ---------------------------------------------------------------------------
#
# The bug this pins down
# ----------------------
# ``_self_heal_loop`` only ever retried a ``VerdictParseError``.  A
# verdict that parsed fine but shipped an evidence artifact the framework
# cannot check — the VP-015 round wrote 50 citations and one of them
# carried an empty ``snippet`` — was returned as PASSED on the spot.  The
# check that would have refused it (``verification_evidence.verify_evidence``)
# runs much later, in ``verification_agent._apply_evidence_check``, by which
# time the sub-agent is gone and there is nobody left to ask for the
# missing text.  So a defect that one extra read could have fixed killed
# the whole verification point, and the next round had to redo the review
# from scratch.
#
# The contract pinned here
# ------------------------
#   * a PASSED whose artifact does not hold up is retried ONCE, and the
#     retry carries the framework's own complaints (the issue texts) into
#     the next prompt, so the sub-agent knows WHICH citation to complete
#     and with WHAT text;
#   * the retry is a *separate* budget: it never spends the parse-error
#     retry, and it never spends more than one attempt of its own;
#   * the retry prompt asks for the ARTIFACT, never for a code change —
#     the repository under verification is read-only for the verifier, and
#     "analyse the failure, fix the underlying issue (edit code...)" would
#     send it into the tree it is verifying;
#   * only PASSED is inspected — a FAILED verdict owes no citation and is
#     never retried for a missing one;
#   * the sub-agent never changes the verdict itself: a PASSED that still
#     does not hold up comes back as PASSED and is downgraded downstream,
#     exactly as before.  This adds a retry, not a pass path.


class _ArtifactTool:
    """A coding tool that writes an evidence artifact per response.

    The real sub-agent writes ``citations.json`` into the artifact
    directory it was told about; this stand-in does the same, one
    artifact per scripted response, so a test can hand the first
    attempt a broken artifact and the second a good one.
    """

    def __init__(
        self,
        artifact_dir: Path,
        artifacts: List[Any],
        responses: List[Dict[str, Any]],
        filename: str = "citations.json",
    ) -> None:
        self._artifact_dir = Path(artifact_dir)
        self._artifacts = list(artifacts)
        self._responses = list(responses)
        self._filename = filename
        self.prompts: List[str] = []
        self.calls = 0

    def query_json(self, prompt=None, system_instruction=None, timeout=None):
        self.calls += 1
        self.prompts.append(str(prompt or ""))
        if self._artifacts:
            payload = self._artifacts.pop(0)
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            (self._artifact_dir / self._filename).write_text(
                json.dumps(payload), encoding="utf-8",
            )
        if not self._responses:
            raise AssertionError(
                "the scripted coding tool ran out of responses"
            )
        return self._responses.pop(0)


def _project_tree(tmp_path: Path) -> Path:
    """A project tree with one citable file."""
    project = tmp_path / "project"
    (project / "backend").mkdir(parents=True)
    (project / "backend" / "sample.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8",
    )
    return project


def _evidence_vp(
    plan_dir: Path, project_dir: Path, vp_id: str = "VP-015",
) -> Dict[str, Any]:
    """A code_review VP that carries the artifact directory.

    ``evidence_artifact_dir`` is what the orchestrator sets
    (``verification_agent``: ``<plan_dir>/vp_artifacts/<vp_id>``), and it
    is the only place the plan directory can be derived from.
    """
    return {
        "id": vp_id,
        "title": "the module exposes its public API",
        "verification_method": "code_review",
        "expected_result": "the public API is reachable",
        "evidence_artifact_dir": str(
            Path(plan_dir) / "vp_artifacts" / vp_id
        ),
        "project_root": str(project_dir),
    }


#: A citations file the framework can check.
GOOD_CITATIONS: Dict[str, Any] = {
    "citations": [
        {"file": "backend/sample.py", "line": 2, "snippet": "return 42"},
    ],
}

#: The shape the VP-015 round actually produced: one good citation and
#: one that names a line but quotes nothing.
MALFORMED_CITATIONS: Dict[str, Any] = {
    "citations": [
        {"file": "backend/sample.py", "line": 2, "snippet": "return 42"},
        {"file": "backend/sample.py", "line": 1, "snippet": ""},
    ],
}

#: The ``ui_validation`` / ``e2e`` counterpart: a checkpoint that
#: records no ``actual`` observation is not a checkpoint.
GOOD_CHECKPOINTS: Dict[str, Any] = {
    "checkpoints": [
        {"selector": "#answer", "expected": "42", "actual": "42",
         "passed": True},
    ],
}

MALFORMED_CHECKPOINTS: Dict[str, Any] = {
    "checkpoints": [
        {"selector": "#answer", "expected": "42", "passed": True},
    ],
}


@pytest.mark.asyncio
async def test_evidence_defect_is_retried_and_the_retry_counts(tmp_path):
    """A PASSED whose citations do not hold up gets one more attempt.

    The second attempt ships a complete artifact; its PASSED is the one
    that comes back, and the attempt log shows the evidence retry.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    tool = _ArtifactTool(
        artifact_dir,
        [MALFORMED_CITATIONS, GOOD_CITATIONS],
        [VALID_PAYLOAD, VALID_PAYLOAD],
    )

    verdict = await sub._self_heal_loop(
        vp_node=_evidence_vp(plan_dir, project_dir),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    assert tool.calls == 2, (
        f"the broken artifact must earn exactly one more attempt; the "
        f"tool was called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        f"the retry's verdict is the one that counts; got "
        f"{verdict.verdict!r}"
    )
    assert _count_event(log_path, "evidence_contract_retry") == 1, (
        "the evidence retry must be observable in the attempt log"
    )


@pytest.mark.asyncio
async def test_evidence_retry_does_not_spend_the_parse_error_budget(tmp_path):
    """``max_retries=0`` and the evidence retry still happens.

    The parse-error budget exists for protocol garbles; the evidence
    budget is a different thing and must not be taken from it.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=0)
    tool = _ArtifactTool(
        artifact_dir,
        [MALFORMED_CITATIONS, GOOD_CITATIONS],
        [VALID_PAYLOAD, VALID_PAYLOAD],
    )

    verdict = await sub._self_heal_loop(
        vp_node=_evidence_vp(plan_dir, project_dir),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    assert tool.calls == 2, (
        f"the evidence retry must not be limited by the parse-error "
        f"budget; the tool was called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        f"the retry's verdict is the one that counts; got "
        f"{verdict.verdict!r}"
    )


@pytest.mark.asyncio
async def test_evidence_retry_asks_for_the_artifact_not_for_code_changes(
    tmp_path,
):
    """The retry prompt must not send the verifier into the tree.

    The generic retry wording ("analyse the failure, fix the underlying
    issue (edit code, install a missing dependency, or tweak config)")
    would have the verification agent edit the repository it is
    verifying.  The evidence retry asks for the artifact instead, and
    carries the framework's complaints into the prompt.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    tool = _ArtifactTool(
        artifact_dir,
        [MALFORMED_CITATIONS, GOOD_CITATIONS],
        [VALID_PAYLOAD, VALID_PAYLOAD],
    )

    await sub._self_heal_loop(
        vp_node=_evidence_vp(plan_dir, project_dir),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    retry_prompt = tool.prompts[1]
    assert "fix the underlying issue" not in retry_prompt, (
        "the evidence retry must not reuse the 'fix the code' wording:\n"
        f"{retry_prompt}"
    )
    assert "edit code" not in retry_prompt, (
        "the evidence retry must not ask the verifier to edit code:\n"
        f"{retry_prompt}"
    )
    assert "citations.json" in retry_prompt, (
        "the evidence retry must name the artifact to rewrite:\n"
        f"{retry_prompt}"
    )
    assert "READ-ONLY" in retry_prompt, (
        "the evidence retry must keep the repository read-only:\n"
        f"{retry_prompt}"
    )
    assert "snippet" in retry_prompt, (
        "the framework's complaints must ride along into the retry "
        f"prompt:\n{retry_prompt}"
    )


@pytest.mark.asyncio
async def test_failed_verdict_is_not_retried_for_missing_evidence(tmp_path):
    """Only PASSED is inspected.

    A FAILED verdict owes no citation — a reviewer reporting that
    something is missing often has no line to cite — so it is returned
    as it is, without a retry.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    tool = _ArtifactTool(
        plan_dir / "vp_artifacts" / "VP-015",
        [],
        [FAILED_PAYLOAD],
    )

    verdict = await sub._self_heal_loop(
        vp_node=_evidence_vp(plan_dir, project_dir),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    assert tool.calls == 1, (
        f"a FAILED verdict must not be retried for missing evidence; the "
        f"tool was called {tool.calls} times"
    )
    assert verdict.verdict == "FAILED", (
        f"the FAILED verdict stands on its own; got {verdict.verdict!r}"
    )
    assert _count_event(log_path, "evidence_contract_retry") == 0, (
        "no evidence retry may be logged for a FAILED verdict"
    )


@pytest.mark.asyncio
async def test_evidence_retry_happens_at_most_once(tmp_path):
    """Two broken artifacts → the verdict comes back as it is.

    The sub-agent does not downgrade anything: that is the framework's
    job (``verification_agent._apply_evidence_check``), and this change
    must not add a pass path of its own.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    tool = _ArtifactTool(
        artifact_dir,
        [MALFORMED_CITATIONS, MALFORMED_CITATIONS],
        [VALID_PAYLOAD, VALID_PAYLOAD],
    )

    verdict = await sub._self_heal_loop(
        vp_node=_evidence_vp(plan_dir, project_dir),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    assert tool.calls == 2, (
        f"the evidence retry is granted once, not once per attempt; the "
        f"tool was called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        "the sub-agent must not change the verdict itself — the "
        f"downgrade is the framework's job; got {verdict.verdict!r}"
    )
    assert _count_event(log_path, "evidence_contract_retry") == 1, (
        "the evidence retry must be logged exactly once"
    )

    # The framework's own downgrade is untouched by this change: the same
    # artifact still turns a PASSED into a FAILED downstream.
    from verification_evidence import apply_to_verdict

    checked = apply_to_verdict(
        {"status": "PASSED", "reasons": [], "evidence": {}},
        "code_review", plan_dir, project_dir, "VP-015",
    )
    assert checked["status"] == "FAILED", (
        "the downstream evidence check must still downgrade a PASSED that "
        f"does not hold up; got {checked['status']!r}"
    )


@pytest.mark.asyncio
async def test_a_malfunctioning_evidence_retry_keeps_the_verdict(tmp_path):
    """A garbled retry must not lose the verdict already in hand.

    The evidence retry is one more attempt, and that attempt can
    malfunction like any other.  When it does, the verdict that is
    already in hand is the one to return: the framework downgrades it
    for its evidence exactly as it did before this change, so the
    operator reads WHICH citations were broken instead of a protocol
    garble.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="code_review", max_retries=2)
    tool = _ArtifactTool(
        artifact_dir,
        [MALFORMED_CITATIONS],
        [VALID_PAYLOAD, MALFORMED_PAYLOAD, MALFORMED_PAYLOAD,
         MALFORMED_PAYLOAD],
    )

    verdict = await sub._self_heal_loop(
        vp_node=_evidence_vp(plan_dir, project_dir),
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    assert verdict.verdict == "PASSED", (
        "the verdict already in hand must not be lost to a garbled "
        f"retry; got {verdict.verdict!r}"
    )
    assert not any(
        "verdict unparseable" in reason for reason in verdict.reasons
    ), (
        "the operator must not be sent to the provider for what is "
        f"really an evidence defect; got {verdict.reasons}"
    )


@pytest.mark.asyncio
async def test_the_checkpoint_methods_get_the_same_retry(tmp_path):
    """``ui_validation`` / ``e2e`` owe checkpoints, not citations.

    The retry names the artifact that method owes (``checkpoints.json``)
    and carries the framework's complaint about it — the wording is
    generated per method, so a hard-coded ``citations.json`` would send
    the sub-agent to the wrong file.
    """
    plan_dir = tmp_path / "plan"
    project_dir = _project_tree(tmp_path)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    log_path = tmp_path / "vp.log"
    sub = VerificationSubAgent(method="ui_validation", max_retries=2)
    tool = _ArtifactTool(
        artifact_dir,
        [MALFORMED_CHECKPOINTS, GOOD_CHECKPOINTS],
        [VALID_PAYLOAD, VALID_PAYLOAD],
        filename="checkpoints.json",
    )
    vp_node = _evidence_vp(plan_dir, project_dir)
    vp_node["verification_method"] = "ui_validation"

    verdict = await sub._self_heal_loop(
        vp_node=vp_node,
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=project_dir,
    )

    assert tool.calls == 2, (
        f"a broken checkpoints.json must earn one more attempt; the tool "
        f"was called {tool.calls} times"
    )
    assert verdict.verdict == "PASSED", (
        f"the retry's verdict is the one that counts; got "
        f"{verdict.verdict!r}"
    )
    assert _count_event(log_path, "evidence_contract_retry") == 1, (
        "the evidence retry must be observable in the attempt log"
    )
    retry_prompt = tool.prompts[1]
    assert "checkpoints.json" in retry_prompt, (
        "the retry must name the artifact this method owes:\n"
        f"{retry_prompt}"
    )
    assert "actual" in retry_prompt, (
        "the framework's complaint must ride along into the retry "
        f"prompt:\n{retry_prompt}"
    )
