"""Shared sample shapes for the post-processing (收尾) stage.

What this file witnesses
------------------------
The twenty-odd tasks that follow this one all need the same handful of
sample shapes: a requirement item, a requirement book, a progress file,
a report, an evidence record, a clock they can push forward, and a
``gh`` they can call without touching the network.  Written inline in
every test file, those shapes drift: one case spells the item with six
keys, the next with seven, and a red test then means "my sample was
wrong", not "the code is wrong".

So the samples live in ONE place — ``tests/fixtures/postprocess/`` —
and this file pins the contract that place makes:

1. a requirement item has exactly the seven keys the architecture
   declares — no more, no fewer;
2. a builder refuses a key it does not know, so a typo in a test fails
   loudly instead of silently producing a sample nobody asked for;
3. the fake clock moves only where it is injected — the wall clock
   behind it never jumps;
4. the ``gh`` stub records the argv it was called with, verbatim;
5. the zero-intrusion baseline names no file the finishing stage would
   write.

Why the seven keys are written out here
---------------------------------------
"the item has the keys the builder produces" is checkable but
vacuous — a builder that lost ``external`` would still agree with
itself.  The seven names are therefore spelled out member by member
below, from the architecture's data model, so that losing one is a
failure rather than a silent shrink.  This is the same trick the phase
vocabulary test uses for ``VALID_PHASES``.

Where the samples live
----------------------
``backend/tests/fixtures/postprocess/`` — a **namespace** directory:
``backend/tests/`` is a regular package (it has ``__init__.py``) but
``backend/tests/fixtures/`` deliberately has none, so
``tests.fixtures.postprocess.builders`` resolves without an
``__init__.py`` being written.  Nothing in this file imports the
not-yet-implemented business modules; the builders are plain dict
factories.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.postprocess.builders import (
    FakeClock,
    fake_notifier,
    make_evidence,
    make_item,
    make_progress,
    make_report,
    make_spec,
)


#: The fixture directory, resolved from this file — never from a literal
#: path, so the suite keeps working on any checkout.
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "postprocess"

#: The ``gh`` stub: an executable script, not a Python module.
GH_STUB = FIXTURES_DIR / "bin" / "gh"

#: The zero-intrusion baseline snapshot.
BASELINE_FILE = FIXTURES_DIR / "plan_dir_baseline.json"


#: The keys the architecture declares for one requirement item
#: (``postprocess.json``'s ``items[]`` element).
ARCHITECTURE_ITEM_KEYS = {
    "id",
    "action",
    "acceptance_predicate",
    "evidence_requirement",
    "external",
    "required",
    "wait",
}


def _all_json_keys(node):
    """Every key found anywhere in a decoded JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _all_json_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_json_keys(value)


def test_make_item_defaults_match_architecture():
    """The item carries the architecture's seven keys — and only those."""
    item = make_item()

    assert set(item) == ARCHITECTURE_ITEM_KEYS, (
        f"expected exactly {sorted(ARCHITECTURE_ITEM_KEYS)}, got "
        f"{sorted(item)}"
    )
    assert len(item) == 7, f"expected 7 keys, got {len(item)}"


def test_make_item_defaults_are_the_documented_ones():
    """The defaults are the ones the architecture's example spells out."""
    item = make_item()

    assert item["external"] is False
    assert item["required"] is True
    assert item["wait"] == {"type": "none"}
    assert item["acceptance_predicate"] == {"kind": "exit_code"}


def test_make_item_does_not_share_mutable_defaults():
    """Two items never share one mutable default."""
    first = make_item()
    second = make_item()

    first["wait"]["type"] = "human"

    assert second["wait"] == {"type": "none"}, (
        "a mutated default must not leak into the next item"
    )


def test_unknown_key_is_rejected():
    """A key the builder does not know is an error, not a silent extra."""
    with pytest.raises(KeyError, match="no_such_key"):
        make_item(no_such_key=1)


def test_other_builders_reject_unknown_keys():
    """The drift guard covers every builder, not just the item one."""
    for builder in (make_spec, make_progress, make_report, make_evidence):
        with pytest.raises(KeyError):
            builder(no_such_key=1)


def test_make_spec_wraps_the_items():
    """The requirement book is ``{"items": [...]}``."""
    spec = make_spec([make_item(id="push"), make_item(id="pr", external=True)])

    assert list(spec) == ["items"]
    assert [item["id"] for item in spec["items"]] == ["push", "pr"]
    assert spec["items"][1]["external"] is True


def test_make_spec_accepts_an_empty_item_list():
    """An empty book is constructible — illegal samples need it too."""
    assert make_spec([]) == {"items": []}


def test_make_progress_defaults_include_spec_sha256():
    """The progress file always carries the frozen spec's fingerprint."""
    progress = make_progress()

    assert set(progress) == {"spec_sha256", "started_at", "items"}
    assert len(progress["spec_sha256"]) == 64, (
        "the placeholder fingerprint must still look like a sha256 digest"
    )
    assert progress["items"] == {}


def test_make_report_defaults_match_the_architecture():
    """The report is the four keys the architecture declares."""
    report = make_report()

    assert set(report) == {
        "spec_sha256",
        "terminal_status",
        "items",
        "failure_context_ref",
    }
    assert report["items"] == []


def test_make_evidence_defaults_match_the_architecture():
    """An evidence record holds what the judge is allowed to read."""
    evidence = make_evidence()

    assert set(evidence) == {
        "exit_code",
        "stdout",
        "stderr",
        "git_diff",
        "external_status",
        "ref",
    }
    assert evidence["exit_code"] == 0


def test_fake_notifier_captures_calls_without_sending():
    """The notifier sink records ``{plan_id, kind, payload}`` per call."""
    notifier = fake_notifier()

    notifier.notify("plan-1", "postprocess_failed", {"items": ["push"]})

    assert notifier.count == 1
    assert notifier.calls == [
        {
            "plan_id": "plan-1",
            "kind": "postprocess_failed",
            "payload": {"items": ["push"]},
        }
    ]


def test_fake_clock_advances_only_injected_now():
    """``advance`` moves the injected clock, and only the injected clock."""
    clock = FakeClock()
    before = clock.now()
    wall_before = datetime.now(timezone.utc)

    clock.advance(60)

    after = clock.now()
    wall_after = datetime.now(timezone.utc)

    assert after - before == timedelta(seconds=60), (
        "the injected clock must move by exactly the requested amount"
    )
    assert wall_after - wall_before < timedelta(seconds=5), (
        "advancing the fake clock must not move the wall clock"
    )
    assert clock.now() == after, (
        "the fake clock must stay frozen between advances — it must not "
        "read the wall clock"
    )


def test_gh_stub_records_argv(tmp_path):
    """The stub writes the argv it was called with, verbatim, one per line."""
    calls_file = tmp_path / "GH_CALLS.jsonl"
    argv = ["pr", "create", "--title", "release 1.0", "--body", "note"]
    env = dict(os.environ)
    env["GH_CALLS_JSONL"] = str(calls_file)

    assert os.access(GH_STUB, os.X_OK), f"{GH_STUB} must be executable"

    result = subprocess.run(
        [str(GH_STUB), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"stub failed: {result.stderr}"
    lines = calls_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"expected one recorded call, got {lines}"
    assert json.loads(lines[0]) == argv, (
        "the recorded argv must be the caller's argv, unchanged"
    )
    for argument in argv:
        assert argument in lines[0], (
            f"{argument!r} must appear in the recorded call line"
        )


def test_gh_stub_appends_one_line_per_call(tmp_path):
    """Two calls are two lines — the log is a JSONL stream."""
    calls_file = tmp_path / "GH_CALLS.jsonl"
    env = dict(os.environ)
    env["GH_CALLS_JSONL"] = str(calls_file)

    for argv in (["pr", "view", "1"], ["pr", "merge", "2"]):
        subprocess.run(
            [str(GH_STUB), *argv],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    lines = calls_file.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        ["pr", "view", "1"],
        ["pr", "merge", "2"],
    ]


def test_baseline_lists_no_postprocess_artifacts():
    """The baseline names no file the finishing stage would write."""
    raw = BASELINE_FILE.read_text(encoding="utf-8")
    baseline = json.loads(raw)

    assert baseline["files"], "the baseline must not be empty"

    offenders = [
        key
        for key in _all_json_keys(baseline)
        if key.startswith("postprocess")
    ]
    assert not offenders, (
        f"the zero-intrusion baseline must not name finishing-stage "
        f"artifacts: {offenders}"
    )


def test_baseline_locks_no_mtime():
    """The snapshot pins names and size magnitudes — never a timestamp."""
    raw = BASELINE_FILE.read_text(encoding="utf-8")

    assert "mtime" not in raw, "the baseline must not lock modification times"

    baseline = json.loads(raw)
    for name, magnitude in baseline["files"].items():
        assert isinstance(name, str)
        assert isinstance(magnitude, str), (
            "the baseline records a size magnitude, not a byte count"
        )
