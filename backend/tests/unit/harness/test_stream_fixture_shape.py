"""Shape contract for the recorded Claude Code ``stream-json`` fixtures.

The fixtures under ``tests/fixtures/harness/claude_stream_json/`` are
recordings of the *real* ``claude`` CLI (``--output-format stream-json``).
They exist so that event-identity resolution is written against the field
shape the CLI actually emits instead of against a hand-written object that
merely encodes our assumption of that shape.

The recording commands, the recording date and every edit applied to the
recorded bytes are documented in ``README.md`` next to the fixtures.

One directory below the fixtures is *not* part of that contract:
``malformed/`` holds the negative samples -- streams with a line that was
cut off mid-write and therefore does not parse. The "every line parses"
rule below applies to the fixtures listed in :data:`ALL_FIXTURES`; the
negative samples are exempt from it by construction, and
:func:`test_the_malformed_sample_is_deliberately_unparseable` keeps the
exemption honest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "harness" / "claude_stream_json"
)

SINGLE_TURN = "single_turn.jsonl"
MULTI_TURN_TOOLS = "multi_turn_tools.jsonl"
ERROR_AND_PARTIAL_USAGE = "error_and_partial_usage.jsonl"
SESSION_EDGE_CASES = "session_edge_cases.jsonl"

ALL_FIXTURES = (
    SINGLE_TURN,
    MULTI_TURN_TOOLS,
    ERROR_AND_PARTIAL_USAGE,
    SESSION_EDGE_CASES,
)

#: The directory of the negative samples. Its files are *meant* not to
#: parse -- that is what makes them negative samples -- so they are
#: outside the "every line is a JSON document" rule that covers
#: :data:`ALL_FIXTURES`.
MALFORMED_DIR = FIXTURE_DIR / "malformed"

#: The negative sample: a stream whose assistant line was cut off
#: mid-write.
MALFORMED_FIXTURE = "truncated_tail.jsonl"


def _read_events(name: str) -> List[Dict[str, Any]]:
    """Return the parsed events of one fixture, skipping blank lines."""
    path = FIXTURE_DIR / name
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def _non_empty_lines(name: str) -> List[str]:
    path = FIXTURE_DIR / name
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _non_empty_lines_of(path: Path) -> List[str]:
    """The non-blank lines of one file, wherever it lives."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_fixtures_are_jsonl():
    """Every line of every fixture is a standalone JSON document.

    The rule covers the fixtures in :data:`ALL_FIXTURES` only. The
    negative samples under ``malformed/`` are exempt from it -- by
    construction they hold a line that does not parse -- and they are
    kept out of that set by
    :func:`test_the_malformed_directory_is_outside_the_parseable_set`.
    """
    for name in ALL_FIXTURES:
        path = FIXTURE_DIR / name
        assert path.exists(), f"missing stream-json fixture: {path}"
        lines = _non_empty_lines(name)
        assert lines, f"stream-json fixture is empty: {path}"
        for lineno, line in enumerate(lines, start=1):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - failure path
                raise AssertionError(
                    f"{name}:{lineno} is not valid JSON: {exc}"
                ) from exc
            assert isinstance(parsed, dict), (
                f"{name}:{lineno} must be a JSON object, "
                f"got {type(parsed).__name__}"
            )


def test_each_has_result_event():
    """Each fixture ends with the CLI's terminal ``result`` event."""
    for name in ALL_FIXTURES:
        events = _read_events(name)
        assert events, f"stream-json fixture has no events: {name}"
        last = events[-1]
        assert last.get("type") == "result", (
            f"{name}: last event must be type=result, "
            f"got {last.get('type')!r}"
        )


def test_partial_usage_sample_marks_error():
    """The error fixture is an error result whose usage lost its cache keys."""
    events = _read_events(ERROR_AND_PARTIAL_USAGE)
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"{ERROR_AND_PARTIAL_USAGE}: no result event found"
    result = results[-1]

    assert result.get("is_error") is True, (
        f"{ERROR_AND_PARTIAL_USAGE}: is_error must be True, "
        f"got {result.get('is_error')!r}"
    )

    usage = result.get("usage")
    assert isinstance(usage, dict), (
        f"{ERROR_AND_PARTIAL_USAGE}: result.usage must be an object, "
        f"got {type(usage).__name__}"
    )
    cache_keys = [k for k in usage if "cache_creation" in k]
    assert not cache_keys, (
        f"{ERROR_AND_PARTIAL_USAGE}: result.usage must not carry "
        f"cache_creation keys, found {cache_keys}"
    )


# ---------------------------------------------------------------------------
# The negative samples
# ---------------------------------------------------------------------------


def test_the_malformed_sample_is_deliberately_unparseable():
    """The negative sample has to be broken, or the exemption is empty.

    The ``malformed/`` directory is exempt from the "every line is a
    JSON document" rule. That exemption only means something while the
    samples in it really do not parse: a negative sample that was fixed
    by accident would turn the exemption into a hole in the gate.
    """
    path = MALFORMED_DIR / MALFORMED_FIXTURE
    assert path.exists(), f"missing malformed stream-json fixture: {path}"

    lines = _non_empty_lines_of(path)
    assert lines, f"malformed stream-json fixture is empty: {path}"

    broken = 0
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            broken += 1
    assert broken >= 1, (
        f"{MALFORMED_FIXTURE}: every line parses, so a negative sample "
        "that tests nothing is standing in for the broken one"
    )


def test_the_malformed_directory_is_outside_the_parseable_set():
    """The negative samples are not part of the parseable fixtures.

    The exemption is a decision about a directory, not a hole in the
    gate: a negative sample that was registered in
    :data:`ALL_FIXTURES` would make the "every line parses" rule
    contradict itself.
    """
    for name in ALL_FIXTURES:
        assert not name.startswith("malformed/"), (
            f"{name} is a negative sample and must not be in the set of "
            "fixtures whose every line has to parse"
        )

    malformed = sorted(path.name for path in MALFORMED_DIR.glob("*.jsonl"))
    assert malformed, (
        f"{MALFORMED_DIR} holds no negative sample, so the directory is "
        "an empty exemption"
    )
    for name in malformed:
        assert name not in ALL_FIXTURES, (
            f"{name} is a negative sample and must not be in "
            "ALL_FIXTURES"
        )


def test_every_fixture_in_the_directory_is_registered():
    """A fixture no test knows about is a fixture nobody checks.

    The rule "every line of every fixture parses" only holds for the
    fixtures that are registered. A file that lands in the directory
    without being registered would carry no contract at all, so the
    directory and the registered set have to be the same set.
    """
    on_disk = {path.name for path in FIXTURE_DIR.glob("*.jsonl")}
    assert on_disk == set(ALL_FIXTURES), (
        "the fixtures on disk and the registered fixtures differ: "
        f"on disk {sorted(on_disk)}, registered {sorted(ALL_FIXTURES)}"
    )