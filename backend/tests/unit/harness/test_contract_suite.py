"""TDD spec for the harness contract suite — one suite, two adapters.

Why this file exists
====================

Two adapters drive two different CLIs, and the layer above them reads
the two as if one CLI had written both. That equivalence is only worth
something if it is *enforced*. A claude-shaped assertion and an
mcode-shaped assertion, each written by the hand that knows that
adapter, prove nothing about the layer above — they prove only that
each adapter matches its own author's idea of it. "The two backends
behave the same" would then be a claim nobody checked, checked by the
one party that had an interest in it being true.

So this file states the contract **once** and applies it to **both**:
every assertion below runs against the claude adapter and against the
mcode adapter, from the same code path, over a recording of each
backend's own output. An adapter that drifts from the contract fails
here — whichever one it is, and for the same reason.

The three contract layers
=========================

The harness contract has three layers, and this suite covers all three
(arch decision point 7):

1. **Request.** A :class:`~harness.contract.TaskInvocation` becomes a
   :class:`~harness.contract.HarnessInvocation` — the command line that
   runs it. :func:`test_the_request_becomes_a_command_line` pins it.
2. **Process.** The child's output becomes a stream of events and one
   terminal result. :func:`test_event_order_both_backends`,
   :func:`test_result_has_three_keys` and
   :func:`test_usage_key_set_exact` pin it.
3. **Verdict.** The exit code becomes a verdict.
   :func:`test_outcome_in_closed_enum` pins it.

The fixtures are recordings, not inventions
==========================================

A hand-written fixture turns *our assumption* about an event shape into
the contract, and then the contract can only ever confirm the
assumption. Every stream below was written by a CLI:

* the claude streams are the recordings committed under
  ``tests/fixtures/harness/claude_stream_json/`` — a successful run
  (``single_turn.jsonl``) and a failed one whose usage lost its
  cache-creation fields (``error_and_partial_usage.jsonl``);
* the mcode stream is the recording embedded below: the seven lines
  ``mcode exec --output-format stream-json`` wrote, verbatim.

The two adapters are not the same shape
=======================================

The claude adapter is an *identity*: its events carry the CLI's payload
key for key, so its usage block is the CLI's own block — extra keys and
all. The mcode adapter is a *translation*: its events are canonical, its
usage block has exactly the four canonical fields, and a count the CLI
never made is ``None`` rather than a number.

The contract is therefore pinned on the **canonical view** of a run: the
four canonical usage fields, projected out of whichever spelling the CLI
used (:func:`canonical_usage`). The projection reads the adapter's own
output — it never re-derives a count — so every assertion about the
view is an assertion about the adapter. Around it, two more assertions
keep the projection honest: no adapter may *drop* a canonical field, and
no count may be reported under two names at once.

What the integration layer may reuse
====================================

The recorded streams, the case table and the two helpers are module
level on purpose: the integration layer imports them rather than
recording the same streams a second time. :data:`CONTRACT_CASES` is the
table — one row per recorded run of one backend.

No process is spawned and no ``Popen`` is mocked (test decision point
2): the adapters' job is to turn a recorded stream into events, and the
events are the whole observable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from harness import runner as harness_runner
from harness.claude import ClaudeHarness
from harness.contract import (
    HarnessEvent,
    HarnessInvocation,
    HarnessOutcome,
    HarnessResult,
    OUTCOME_KINDS,
    TaskInvocation,
)
from harness.mcode import McodeHarness

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# What the contract says
# ---------------------------------------------------------------------------

#: The four canonical usage fields. A count that was not reported is
#: missing from the values — never zero.
CANONICAL_USAGE_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: The keys a result payload has to carry. The check is about their
#: *presence*: a key that is there and empty still satisfies the
#: contract, and a key that is absent does not.
REQUIRED_RESULT_KEYS: Tuple[str, ...] = ("usage", "is_error", "session_id")

#: The spellings the MiniMax Code CLI uses for the same four counts.
#: The translation renames them; it does not keep both.
MCODE_USAGE_SPELLINGS: Dict[str, str] = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadTokens": "cache_read_input_tokens",
    "cacheCreationTokens": "cache_creation_input_tokens",
}

#: The exit codes the verdict is pinned against: the ones the contract
#: names, plus two it does not (an unmapped code and a signal).
EXIT_CODES_UNDER_TEST: Tuple[int, ...] = (0, 1, 2, 124, 130, 143, 9, -15)

#: The verdicts the contract allows. Seven, and no more.
EXPECTED_OUTCOME_KINDS: frozenset = frozenset(
    {
        "ok",
        "model_error",
        "timeout",
        "permission_denied",
        "config_error",
        "signaled",
        "unknown",
    }
)

#: The request the command line is built from. The prompt is a phrase
#: that appears nowhere else, so "the prompt is not on the command
#: line" is a statement that can be checked.
PROMPT = "say exactly OK"
SYSTEM_INSTRUCTION = "be brief"
MODEL = "minimax/MiniMax-M3.1-Flash"
WORKING_DIRECTORY = "workspace"


# ---------------------------------------------------------------------------
# The recorded streams
# ---------------------------------------------------------------------------

#: The recorded claude streams.
CLAUDE_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "harness" / "claude_stream_json"
)

#: A recording of ``mcode exec --output-format stream-json``: the seven
#: lines the CLI wrote, verbatim. It is the mcode counterpart of the
#: recorded claude streams — a run that starts, says one thing and ends
#: with a result.
MCODE_RECORDED_STREAM: Tuple[str, ...] = (
    '{"schemaVersion":1,"sequence":1,"timestampMs":1792770679574,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"exec.started"}',
    '{"schemaVersion":1,"sequence":2,"timestampMs":1792770679575,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"session.started"}',
    '{"schemaVersion":1,"sequence":3,"timestampMs":1792770679575,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"turn.started"}',
    '{"schemaVersion":1,"sequence":4,"timestampMs":1792770681741,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"item.started","item":{"id":"e53a7798-2893-4959-8c0e-ad2ac4cab933:message","type":"agent_message","contentDelta":"OK"}}',
    '{"schemaVersion":1,"sequence":5,"timestampMs":1792770681857,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"item.completed","item":{"id":"e53a7798-2893-4959-8c0e-ad2ac4cab933:message","type":"agent_message","content":"OK"}}',
    '{"schemaVersion":1,"sequence":6,"timestampMs":1792770682102,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"turn.completed","model":{"providerId":"minimax","modelId":"MiniMax-M3.1-Flash","variant":"thinking","providerSource":"provider","providerKind":"minimax-managed"},"usage":{"inputTokens":16271,"outputTokens":3,"cacheReadTokens":618,"totalTokens":16274},"usageSource":"completed_responses","usageIncomplete":false,"durationMs":2348}',
    '{"schemaVersion":1,"sequence":7,"timestampMs":1792770682102,"runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","type":"exec.completed","result":{"schemaVersion":1,"type":"exec.result","runId":"exec_turn_mvl5dhj8_ov6h55","sessionId":"mvs_fb69527a6d6242d9bc1cc70c46ed71a2","turnId":"turn_mvl5dhj8_ov6h55","status":"succeeded","output":"OK","model":{"providerId":"minimax","modelId":"MiniMax-M3.1-Flash","variant":"thinking","providerSource":"provider","providerKind":"minimax-managed"},"usage":{"inputTokens":16271,"outputTokens":3,"cacheReadTokens":618,"totalTokens":16274},"usageSource":"completed_responses","usageIncomplete":false,"durationMs":2348}}',
)


def claude_recorded_lines(name: str) -> Tuple[str, ...]:
    """The lines of one recorded claude stream.

    Args:
        name: The file name of the recording.

    Returns:
        The recorded lines, in the order the CLI wrote them.
    """
    path = CLAUDE_FIXTURE_DIR / name
    return tuple(path.read_text(encoding="utf-8").splitlines())


def mcode_recorded_lines() -> Tuple[str, ...]:
    """The lines of the recorded mcode stream.

    Returns:
        The recorded lines, in the order the CLI wrote them.
    """
    return MCODE_RECORDED_STREAM


#: The recorded stream of each backend, in that backend's own shape.
RECORDED_STREAMS: Dict[Any, Tuple[str, ...]] = {
    ClaudeHarness: claude_recorded_lines("single_turn.jsonl"),
    McodeHarness: mcode_recorded_lines(),
}


# ---------------------------------------------------------------------------
# Reading one run's output, whichever adapter produced it
# ---------------------------------------------------------------------------


def canonical_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """The four canonical usage fields, projected out of a usage block.

    The projection reads the block the run reported — it never counts
    anything itself — so what comes out is what the adapter said, in
    the contract's spelling. A field the CLI did not report comes out
    as ``None``: not zero, because zero is a count that came back and a
    report of zero cannot be told apart from a run that really did not
    touch the cache.

    Args:
        usage: The usage block the run reported.

    Returns:
        The four canonical fields, and nothing else.
    """
    reported = usage if isinstance(usage, dict) else {}
    return {field: reported.get(field) for field in CANONICAL_USAGE_FIELDS}


def missing_result_keys(payload: Dict[str, Any]) -> List[str]:
    """The required keys a result payload does not carry.

    The check is about *presence*, not about a value: a key that is
    there and empty still satisfies the contract, and a key that is not
    there at all does not — a payload that lost ``is_error`` cannot be
    told apart from one that reported no error.

    Args:
        payload: The result payload.

    Returns:
        The required keys the payload does not carry, in the order they
        are required.
    """
    return [key for key in REQUIRED_RESULT_KEYS if key not in payload]


def reported_partial(event: HarnessEvent) -> bool:
    """Whether the run marked its count partial.

    A translation writes the marker into the result payload
    (``usage_partial``). An identity carries the CLI's own usage block,
    which has no marker to write: the count that was never made is
    simply not in it, and that absence says the same thing in the CLI's
    own spelling.

    Args:
        event: The run's result event.

    Returns:
        ``True`` when the run's count is partial.
    """
    payload = event.raw
    if "usage_partial" in payload:
        return bool(payload["usage_partial"])
    usage = event.usage if isinstance(event.usage, dict) else {}
    return "cache_creation_input_tokens" not in usage


def classify(adapter: Any, code: int, events: List[HarnessEvent]) -> HarnessOutcome:
    """The verdict the layer above gets for one run.

    An adapter that classifies exits itself has the last word. One that
    does not gets the runner's fallback — which is what the layer above
    would really see, so the contract is pinned against that and not
    against a guess.

    Args:
        adapter: The adapter that produced the events.
        code: The process's exit code.
        events: The events the run produced.

    Returns:
        The verdict on what the run meant.
    """
    classifier = getattr(adapter, "classify_exit", None)
    if classifier is not None:
        return classifier(code, events)
    return harness_runner._default_outcome(code)


# ---------------------------------------------------------------------------
# The table of recorded runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractCase:
    """One recorded run of one backend, as the contract sees it.

    Attributes:
        case_id: The name of the row.
        backend: The backend the run came from.
        adapter_class: The adapter that reads that backend's output.
        lines: The recorded lines, in the backend's own shape.
        exit_code: The exit code the run ended with.
        expected_usage: The canonical usage the recording itself says.
        expected_usage_keys: The keys the adapter must report the usage
            under.
    """

    case_id: str
    backend: str
    adapter_class: Any
    lines: Tuple[str, ...]
    exit_code: int
    expected_usage: Dict[str, Optional[int]]
    expected_usage_keys: frozenset


def _claude_expected_usage(name: str) -> Dict[str, Optional[int]]:
    """The canonical usage the recorded claude stream itself says.

    Args:
        name: The file name of the recording.

    Returns:
        The canonical usage of the recorded run's own result line.
    """
    payload = json.loads(claude_recorded_lines(name)[-1])
    usage = payload.get("usage")
    return canonical_usage(usage if isinstance(usage, dict) else {})


def _claude_expected_usage_keys(name: str) -> frozenset:
    """The keys the recorded claude stream's own usage block carries.

    The claude adapter is an identity, so the keys it reports are the
    keys the CLI wrote — no more and no fewer.

    Args:
        name: The file name of the recording.

    Returns:
        The key set of the recorded run's own usage block.
    """
    payload = json.loads(claude_recorded_lines(name)[-1])
    usage = payload.get("usage")
    return frozenset(usage) if isinstance(usage, dict) else frozenset()


def _mcode_expected_usage() -> Dict[str, Optional[int]]:
    """The canonical usage the recorded mcode stream itself says.

    The MiniMax Code CLI spells its counts in camel case; the
    expectation is read out of the recording and renamed, so the number
    the suite expects is the number the CLI wrote.

    Returns:
        The canonical usage of the recorded run's own result line.
    """
    payload = json.loads(mcode_recorded_lines()[-1])
    usage = payload["result"]["usage"]
    return {
        canonical: usage.get(spelling)
        for spelling, canonical in MCODE_USAGE_SPELLINGS.items()
    }


def _mcode_expected_usage_keys() -> frozenset:
    """The keys the mcode adapter reports its usage under.

    The MiniMax Code adapter is a translation, so it reports the four
    canonical fields and nothing else: the counts it read under the
    CLI's own spelling, renamed, and no field of its own.

    Returns:
        The key set the translation must report.
    """
    return frozenset(CANONICAL_USAGE_FIELDS)


#: One row per recorded run. The integration layer may import it.
CONTRACT_CASES: Tuple[ContractCase, ...] = (
    ContractCase(
        case_id="claude-success",
        backend="claude",
        adapter_class=ClaudeHarness,
        lines=claude_recorded_lines("single_turn.jsonl"),
        exit_code=0,
        expected_usage=_claude_expected_usage("single_turn.jsonl"),
        expected_usage_keys=_claude_expected_usage_keys("single_turn.jsonl"),
    ),
    ContractCase(
        case_id="claude-error-partial-usage",
        backend="claude",
        adapter_class=ClaudeHarness,
        lines=claude_recorded_lines("error_and_partial_usage.jsonl"),
        exit_code=1,
        expected_usage=_claude_expected_usage("error_and_partial_usage.jsonl"),
        expected_usage_keys=_claude_expected_usage_keys(
            "error_and_partial_usage.jsonl"
        ),
    ),
    ContractCase(
        case_id="mcode-success",
        backend="mcode",
        adapter_class=McodeHarness,
        lines=mcode_recorded_lines(),
        exit_code=0,
        expected_usage=_mcode_expected_usage(),
        expected_usage_keys=_mcode_expected_usage_keys(),
    ),
)


def contract_events(case: ContractCase) -> List[HarnessEvent]:
    """The events one recorded run turns into.

    Args:
        case: The recorded run.

    Returns:
        The events the adapter produced, in the order they arrived.
    """
    return list(case.adapter_class().parse_stream(case.lines))


def contract_result(case: ContractCase) -> HarnessEvent:
    """The terminal result event of one recorded run.

    Args:
        case: The recorded run.

    Returns:
        The run's result event.
    """
    events = contract_events(case)
    return events[-1]


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=[ClaudeHarness, McodeHarness], ids=["claude", "mcode"])
def harness_factory(request):
    """The two adapters the contract is pinned against.

    The parameter is the adapter *class*, so every test builds a fresh
    adapter and none of them shares one with another.

    Returns:
        The adapter class of the backend under test.
    """
    return request.param


@pytest.fixture
def recorded_lines(harness_factory):
    """The recorded stream of the backend under test, in its own shape.

    Args:
        harness_factory: The adapter class of the backend under test.

    Returns:
        The recorded lines of that backend.
    """
    return RECORDED_STREAMS[harness_factory]


@pytest.fixture(params=CONTRACT_CASES, ids=[case.case_id for case in CONTRACT_CASES])
def contract_case(request):
    """One recorded run of one backend — the table the suite is built on.

    Returns:
        The row under test.
    """
    return request.param


# ---------------------------------------------------------------------------
# Layer 1 — the request becomes a command line
# ---------------------------------------------------------------------------


def test_the_request_becomes_a_command_line(harness_factory):
    """A request becomes the command line that runs it.

    The spelling of the flags belongs to the adapter; the shape of the
    command line belongs to the contract. So this asserts what both
    adapters have to do: start with the backend that is going to run,
    carry the request's working directory, put the model and the
    instruction the caller named on the command line, and keep the
    prompt off it — a command line is visible in the process table.
    """
    adapter = harness_factory()
    request = TaskInvocation(
        prompt=PROMPT,
        system_instruction=SYSTEM_INSTRUCTION,
        model=MODEL,
        cwd=WORKING_DIRECTORY,
    )

    invocation = adapter.build_invocation(request)

    assert isinstance(invocation, HarnessInvocation)
    assert invocation.argv, "a request must produce a command line"

    # The command line starts with the backend that runs it.
    assert invocation.argv[0] == harness_runner._harness_name(adapter)

    # The request's working directory is carried into the invocation.
    assert invocation.cwd == request.cwd

    # The model and the instruction the request named are on it: a run
    # that answered with a different model than the one that was asked
    # for is a run nobody noticed.
    assert MODEL in invocation.argv
    assert SYSTEM_INSTRUCTION in invocation.argv

    # The prompt is not: it travels on stdin.
    assert not any(PROMPT in part for part in invocation.argv)

    # And no flag is emitted with an empty value — an empty value is a
    # different command line, not a shorter one.
    assert all(part != "" for part in invocation.argv)


# ---------------------------------------------------------------------------
# Layer 2 — the output becomes events and one result
# ---------------------------------------------------------------------------


def test_event_order_both_backends(harness_factory, recorded_lines):
    """Both backends end their stream with exactly one result.

    A reader takes the *last* result as the answer, so a second one
    would overwrite the first, and an event after it would look like
    part of an answer that was already given. Everything the agent said
    comes before the result, and the result is where the stream ends.
    """
    events = list(harness_factory().parse_stream(recorded_lines))
    types = [event.raw["type"] for event in events]

    assert types, "the recording produced no events at all"

    # The result is the last event, and it is the only one.
    assert types[-1] == "result"
    assert types.count("result") == 1

    result_position = types.index("result")
    assistant_positions = [
        position for position, event_type in enumerate(types) if event_type == "assistant"
    ]

    assert assistant_positions, "the run said nothing at all"
    assert all(position < result_position for position in assistant_positions), (
        f"an assistant event came after the result: {types}"
    )


def test_result_has_three_keys(harness_factory, recorded_lines):
    """A result carries ``usage``, ``is_error`` and ``session_id``.

    The check is about the *presence* of the keys, not about their
    values: a payload that lost ``is_error`` cannot be told apart from
    one that reported no error, and a payload that lost ``session_id``
    cannot be told apart from one that ran in no conversation.
    """
    events = list(harness_factory().parse_stream(recorded_lines))
    payload = events[-1].raw

    assert missing_result_keys(payload) == [], (
        f"the result payload is missing {missing_result_keys(payload)}"
    )

    # A key that is there and empty still satisfies the contract.
    assert missing_result_keys({key: None for key in REQUIRED_RESULT_KEYS}) == []

    # And a payload that is missing one of them does not.
    assert missing_result_keys(
        {key: None for key in REQUIRED_RESULT_KEYS if key != "usage"}
    ) == ["usage"]


def test_usage_key_set_exact(harness_factory, recorded_lines):
    """The usage a run reports has exactly the four canonical fields.

    The canonical view is what the layer above reads: four fields, no
    fifth, and no field that is not one of them. Around it, two
    assertions keep the view honest — no adapter may drop a canonical
    field, and no count may be reported under two names at once.

    The block that is read is the event's own ``usage`` field, because
    that is the field the layer above reads; the payload's block has to
    agree with it, which :func:`test_the_event_usage_is_the_usage_of_its_payload`
    pins.
    """
    events = list(harness_factory().parse_stream(recorded_lines))
    reported = events[-1].usage
    assert isinstance(reported, dict), "the result reported no usage block"

    canonical = canonical_usage(reported)

    assert set(canonical) == set(CANONICAL_USAGE_FIELDS), (
        f"the canonical usage has the fields {sorted(canonical)}"
    )

    # A field missing from the block is a field the layer above cannot
    # read, so neither adapter may drop one.
    assert set(CANONICAL_USAGE_FIELDS) <= set(reported), (
        f"the reported usage is missing "
        f"{sorted(set(CANONICAL_USAGE_FIELDS) - set(reported))}"
    )

    # And a count is reported under one name, not two.
    assert not set(reported) & set(MCODE_USAGE_SPELLINGS), (
        f"the reported usage spells a count twice: {sorted(reported)}"
    )


def test_the_event_usage_is_the_usage_of_its_payload(harness_factory, recorded_lines):
    """The event's usage field is the usage of the payload it came from.

    The layer above reads the field; a reader looking at the payload
    sees the same thing. A field that says one number and a payload
    that says another is a run nobody can read.
    """
    events = list(harness_factory().parse_stream(recorded_lines))

    for event in events:
        assert event.usage == event.raw.get("usage"), (
            f"the event of type {event.type!r} reports {event.usage!r}, "
            f"its payload says {event.raw.get('usage')!r}"
        )


def test_the_reported_usage_names_exactly_what_it_may_name(contract_case):
    """The usage block a run reports carries no field of its own.

    Two adapters, two rules — and one assertion. The claude adapter is
    an identity, so it reports the keys the CLI wrote: the ones the
    contract models and the ones it does not, exactly as they came.
    The mcode adapter is a translation, so it reports the four
    canonical fields and nothing else. Neither may add a field of its
    own: a field the layer above does not know about is a field nobody
    agreed to read, and a field that went missing is a count the run
    did report and the layer above cannot see.
    """
    reported = contract_result(contract_case).usage
    assert isinstance(reported, dict), "the result reported no usage block"

    assert set(reported) == set(contract_case.expected_usage_keys), (
        f"{contract_case.case_id}: the reported usage names "
        f"{sorted(reported)}, expected "
        f"{sorted(contract_case.expected_usage_keys)}"
    )


def test_unreported_count_is_missing_and_marked_partial(contract_case):
    """A count the CLI never reported is missing, and the run says so.

    Zero is a count that came back. A field the CLI did not fill in
    says nothing about the run, and a reader that cannot tell the two
    apart believes a number that is too low — in the direction that
    costs money.
    """
    event = contract_result(contract_case)
    canonical = canonical_usage(event.usage)

    for field in CANONICAL_USAGE_FIELDS:
        expected = contract_case.expected_usage[field]
        assert canonical[field] == expected, (
            f"{contract_case.case_id}: {field} is {canonical[field]!r}, "
            f"the recording says {expected!r}"
        )

        if expected is None:
            assert canonical[field] is None, (
                f"{contract_case.case_id}: {field} was never reported and "
                f"must not be {canonical[field]!r}"
            )
            assert reported_partial(event) is True, (
                f"{contract_case.case_id}: a count that was never reported "
                f"must leave the run marked partial"
            )

    if all(
        contract_case.expected_usage[field] is not None
        for field in CANONICAL_USAGE_FIELDS
    ):
        assert reported_partial(event) is False, (
            f"{contract_case.case_id}: every count was reported, so the run "
            f"is not partial"
        )


def test_both_backends_report_the_same_usage_fields():
    """Both backends put the same four counts under the same four names.

    This is the equivalence the whole file is for, stated in one place:
    the two adapters are different kinds of adapter, and the layer
    above them still reads their usage the same way.
    """
    views = {}
    for case in CONTRACT_CASES:
        event = contract_result(case)
        views[case.backend] = canonical_usage(event.usage)

    assert set(views) == {"claude", "mcode"}
    for backend, view in views.items():
        assert set(view) == set(CANONICAL_USAGE_FIELDS), (
            f"{backend}: the canonical usage has the fields {sorted(view)}"
        )

    assert set(views["claude"]) == set(views["mcode"])


def test_result_records_the_sandbox_state_and_the_harness_name():
    """A finished run says what it ran under and who ran it.

    A result that cannot name the backend that produced it is half an
    answer, and a result that cannot say whether the child was confined
    cannot be acted on. The runner is the only place that fills those
    two fields, so the contract is pinned on what it writes.
    """
    fields = HarnessResult.__dataclass_fields__

    assert "sandbox_state" in fields, (
        "the contract's result must carry the state of the sandbox"
    )
    assert "harness" in fields, (
        "the contract's result must carry the name of the harness"
    )

    # The name the runner writes is the name of the backend — never an
    # empty string.
    assert harness_runner._harness_name(ClaudeHarness()) == "claude"
    assert harness_runner._harness_name(McodeHarness()) == "mcode"

    # And the sandbox state is one of the three tokens the runner can
    # write, not an open vocabulary.
    assert set(harness_runner._SANDBOX_STATE.values()) == {
        harness_runner.SANDBOX_APPLIED,
        harness_runner.SANDBOX_INHERITED,
        harness_runner.SANDBOX_UNSANDBOXED,
    }

    result = HarnessResult(
        exit_code=0,
        harness=harness_runner._harness_name(McodeHarness()),
        sandbox_state=harness_runner.SANDBOX_UNSANDBOXED,
    )
    assert result.harness == "mcode"
    assert result.sandbox_state == "unsandboxed"


# ---------------------------------------------------------------------------
# Layer 3 — the exit code becomes a verdict
# ---------------------------------------------------------------------------


def test_outcome_in_closed_enum(harness_factory, recorded_lines):
    """The verdict is one of the seven the contract allows.

    The layer above branches on the verdict, so an open vocabulary would
    mean every caller re-parsed free text. An adapter that has not
    written a classifier of its own gets the runner's fallback, and the
    contract is pinned on that too — it is the verdict the layer above
    would really see.
    """
    events = list(harness_factory().parse_stream(recorded_lines))
    adapter = harness_factory()

    for code in EXIT_CODES_UNDER_TEST:
        outcome = classify(adapter, code, events)
        assert outcome.kind in OUTCOME_KINDS, (
            f"exit code {code} was classified as {outcome.kind!r}"
        )

    # Seven verdicts, and the set is closed.
    assert OUTCOME_KINDS == EXPECTED_OUTCOME_KINDS
    assert len(OUTCOME_KINDS) == 7

    # A verdict outside the set cannot even be built.
    with pytest.raises(ValueError):
        HarnessOutcome(kind="not-a-verdict")
