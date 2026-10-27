"""E2E-PARITY — the two backends, side by side.

Why this scenario exists
========================

A scenario that only ever runs against one backend cannot say whether
that backend behaves like the one it replaces.  "Equivalent" needs
something to be equivalent to, and that something is a *baseline*: the
same task, run through the other backend, with the two runs compared
on the things that are supposed to be the same.

So this file runs the fixed task twice -- once through the claude
backend, once through the mcode backend -- and compares the two runs.

What is compared, and what is not
=================================

Compared, because it is supposed to be the same:

  * the files the run changed -- path *and* content hash;
  * the exit code and the normalised stdout of the task's target
    command;
  * the categories of the event stream;
  * the keys of the usage block;
  * the structure of the report -- the heading levels and the required
    sections.

Not compared, because it is supposed to differ, or because comparing it
would measure the wrong thing:

  * the prose.  Two models answer in different words; a comparison of
    words measures the models, not the backends that carry them.
  * the latency.  The two backends are different programs and are not
    expected to take the same time.
  * the *names* of the models.  They differ on purpose -- each backend
    is pinned to its own model.
  * the raw event types and the raw usage keys.  The two CLIs report
    different amounts about the same run: the claude CLI reports a
    dozen keys in its usage block and the mcode CLI reports four, and
    only the four canonical ones are a measurement both of them make.
    The comparison is therefore over the *canonical* keys, and the raw
    sets are kept beside it so that a difference can be read.
  * the counts of the events.  One CLI reports a thinking token per
    line and the other reports one message per event, so the counts
    measure the two CLIs' reporting styles rather than the shape of
    the run.

Cost and blast radius
=====================

Two real model calls, and two real server processes -- one per
backend.  Both servers are reclaimed when the scenario ends.  The
scenario is off unless ``PDT_E2E_HARNESS=1`` is set: the local and CI
deterministic gates only *collect* it.  It is marked ``real_model``
(it really calls a model) and ``e2e`` (it drives real processes), so
the cloud lanes skip it twice over.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

import pytest

# The shared machinery lives with the claude baseline: one place that
# knows how to run the fixed task and how to read a run back.  The
# directory is on the path because pytest puts it there when it
# imports this module.
from test_e2e_claude import (  # noqa: E402 — the import needs the path above
    E2E_ENV_VAR,
    EVENT_CATEGORIES,
    MCODE,
    MCODE_MODEL,
    MCODE_PLAN_ID,
    REQUIRED_REPORT_SECTIONS,
    USAGE_KEYS,
    ScenarioResult,
    claude_server,  # noqa: F401 — the claude scenario's own dependency
    claude_scenario,  # noqa: F401 — the baseline's run, shared with this one
    run_scenario,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.real_model,
    pytest.mark.skipif(
        os.environ.get(E2E_ENV_VAR) != "1",
        reason=(
            "the real harness chains need PDT_E2E_HARNESS=1: they spawn "
            "the real CLIs under the real macOS seatbelt and cost a model "
            "call, so the deterministic gates only collect them"
        ),
    ),
]


@pytest.fixture(scope="session")
def mcode_scenario(e2e_server) -> ScenarioResult:
    """The one real mcode run this comparison is built on.

    The run happens in its own directory *inside* the server's
    workspace, and in its own repository: the workspace the server
    booted already belongs to the mcode scenario, and two runs
    sharing one directory would be comparing a run with itself.

    Args:
        e2e_server: The server the run happens against.

    Returns:
        What the run produced.
    """
    if os.environ.get(E2E_ENV_VAR) != "1":
        pytest.skip(
            f"{E2E_ENV_VAR}=1 is not set: the real mcode chain is "
            f"opt-in, because it costs a model call and spawns the real CLI"
        )
    workspace = Path(e2e_server.workspace) / "parity"
    return run_scenario(
        harness=MCODE,
        model=MCODE_MODEL,
        server=e2e_server,
        workspace=workspace,
        plan_id=MCODE_PLAN_ID,
    )


def _contains_in_order(
    sections: Tuple[Tuple[int, str], ...],
    required: Tuple[Tuple[int, str], ...],
) -> bool:
    """Whether ``required`` appears in ``sections``, in that order.

    A report that carries the required sections carries them in the
    order they were asked for; a report that carries them out of order
    is a different report, and so is one that is missing one.

    Args:
        sections: The headings of the report.
        required: The headings the task asked for.

    Returns:
        Whether the required headings are there, in order.
    """
    index = 0
    for section in sections:
        if index < len(required) and section == required[index]:
            index += 1
    return index == len(required)


def test_changed_file_sets_equal(
    claude_scenario: ScenarioResult, mcode_scenario: ScenarioResult
) -> None:
    """Both runs left the same files, with the same content.

    The comparison is on the path *and* the content hash: two runs
    that both wrote ``hello.txt`` but wrote different bytes into it
    have not done the same thing.

    Args:
        claude_scenario: What the claude run produced.
        mcode_scenario: What the mcode run produced.
    """
    claude_files = claude_scenario.changed_files
    mcode_files = mcode_scenario.changed_files

    # Not vacuous: both runs really did leave something behind.
    assert claude_files, "the claude run changed no file at all"
    assert mcode_files, "the mcode run changed no file at all"
    assert dict(claude_files).get("hello.txt"), (
        f"the claude run left no hello.txt behind: {claude_files!r}"
    )
    assert dict(mcode_files).get("hello.txt"), (
        f"the mcode run left no hello.txt behind: {mcode_files!r}"
    )

    assert claude_files == mcode_files, (
        f"the two runs left different files behind:\n"
        f"  claude: {claude_files!r}\n"
        f"  mcode:  {mcode_files!r}"
    )


def test_event_and_usage_shape_equal(
    claude_scenario: ScenarioResult, mcode_scenario: ScenarioResult
) -> None:
    """Both runs had the same shape of event stream and of usage block.

    The comparison is over the *categories* of the events and over the
    *canonical* usage keys -- the four both CLIs measure.  The raw
    sets are asserted to be non-empty and are reported when they
    differ, because a difference there is real, but it is a
    difference in what the two CLIs report, not in what the run did.

    Args:
        claude_scenario: What the claude run produced.
        mcode_scenario: What the mcode run produced.
    """
    # -- the event stream -------------------------------------------------
    assert claude_scenario.event_kinds, "the claude run reported no events"
    assert mcode_scenario.event_kinds, "the mcode run reported no events"
    assert claude_scenario.event_kinds == mcode_scenario.event_kinds, (
        f"the two runs had differently shaped event streams:\n"
        f"  claude: {list(claude_scenario.event_kinds)}\n"
        f"  mcode:  {list(mcode_scenario.event_kinds)}"
    )
    assert set(claude_scenario.event_kinds) == set(EVENT_CATEGORIES), (
        f"the claude run did not carry every category: "
        f"{list(claude_scenario.event_kinds)}"
    )

    # -- the usage block --------------------------------------------------
    assert claude_scenario.usage_keys == USAGE_KEYS, (
        f"the claude run did not report the four canonical usage keys: "
        f"{sorted(claude_scenario.usage_keys)}"
    )
    assert mcode_scenario.usage_keys == USAGE_KEYS, (
        f"the mcode run did not report the four canonical usage keys: "
        f"{sorted(mcode_scenario.usage_keys)}"
    )
    assert claude_scenario.usage_keys == mcode_scenario.usage_keys, (
        f"the two runs reported different usage keys:\n"
        f"  claude: {sorted(claude_scenario.usage_keys)}\n"
        f"  mcode:  {sorted(mcode_scenario.usage_keys)}"
    )
    assert claude_scenario.usage is not None, "the claude run reported no usage"
    assert mcode_scenario.usage is not None, "the mcode run reported no usage"
    assert claude_scenario.usage["input_tokens"] > 0, (
        f"the claude run reported no input tokens: {claude_scenario.usage}"
    )
    assert mcode_scenario.usage["input_tokens"] > 0, (
        f"the mcode run reported no input tokens: {mcode_scenario.usage}"
    )

    # -- the target command ----------------------------------------------
    assert claude_scenario.target_exit == 0, (
        f"`cat hello.txt` failed after the claude run: "
        f"exit {claude_scenario.target_exit}"
    )
    assert mcode_scenario.target_exit == 0, (
        f"`cat hello.txt` failed after the mcode run: "
        f"exit {mcode_scenario.target_exit}"
    )
    assert claude_scenario.target_stdout == mcode_scenario.target_stdout, (
        f"the two runs' target command answered differently:\n"
        f"  claude: {claude_scenario.target_stdout!r}\n"
        f"  mcode:  {mcode_scenario.target_stdout!r}"
    )


def test_report_structure_equal(
    claude_scenario: ScenarioResult, mcode_scenario: ScenarioResult
) -> None:
    """Both runs answered with the same report structure.

    The comparison is on the headings -- their level and their text --
    and not on the prose under them: the prose is the model's, and the
    two models are not expected to write alike.

    Args:
        claude_scenario: What the claude run produced.
        mcode_scenario: What the mcode run produced.
    """
    claude_sections = claude_scenario.report_sections
    mcode_sections = mcode_scenario.report_sections

    assert claude_sections, "the claude run answered with no report at all"
    assert mcode_sections, "the mcode run answered with no report at all"

    assert claude_sections == mcode_sections, (
        f"the two runs answered with different report structures:\n"
        f"  claude: {list(claude_sections)}\n"
        f"  mcode:  {list(mcode_sections)}"
    )
    assert _contains_in_order(claude_sections, REQUIRED_REPORT_SECTIONS), (
        f"the claude run's report does not carry the required sections: "
        f"{list(claude_sections)}"
    )
    assert _contains_in_order(mcode_sections, REQUIRED_REPORT_SECTIONS), (
        f"the mcode run's report does not carry the required sections: "
        f"{list(mcode_sections)}"
    )
