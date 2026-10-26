"""The consumer-side cross-repo contract must be written down — and the
writing must stay pinned.

Why this gate exists
--------------------
``docs/development/cross-repo-contract.md`` freezes the **provider** half
of the contract: what PDT serves, and what it answers. This gate covers
the other half — the **consumer** (the scheduler) side: what the
scheduler is allowed to read, in which order it is allowed to act, and
to whom it has to hand the decision about *what to stop*.

Why that half needs a gate of its own
--------------------------------------
The merge order (PDT first, the scheduler only consumes what has already
been merged) is only worth anything if the thing being consumed says what
the consumer must do. A provider contract that lists endpoints but leaves
the consumer free to *read* any phase and to *decide* on its own which
layer to stop is a contract with a hole in the middle: the scheduler
keeps reading ``ready``, keeps deciding "execution is running, so I will
stop execution", and the two repositories stop agreeing without anybody
noticing — because nothing in either repository asserts the thing that
actually broke.

The words that matter are small and checkable:

* the scheduler treats **only** ``queued`` as schedulable;
* it **never reads** ``ready`` (the legacy alias — see section 1.2);
* it **does not decide** whether to stop the execution round or the
  verification round: the unified lifecycle facade decides that, in
  process, against live state;
* each tick runs a **fixed order** — reconcile, converge, backfill — with
  a **minimum-runtime guard** protecting a young run from being stopped;
* **adoption** (startup takeover) and **out-of-band convergence** both
  take the routing table as the single authority.

What it pins
------------
1. **The section exists, as a level-2 heading, with the pinned title.**
   ``## 2. 消费方（调度器）契约`` is the identifier the consumer's own
   gate asserts on; a reworded heading breaks the link between the two
   repositories instead of renaming one half of a shared contract.
2. **Four keywords are present in that section** — ``queued``,
   ``ready``, ``tick``, ``routing``. They are the load-bearing words of
   the consumer half: the schedulable phase, the forbidden one, the
   pipeline, and the authority.
3. **The tick order is written down, in order** — reconcile → converge →
   backfill — together with the minimum-runtime guard and the statement
   that the facade, not the scheduler, decides whom to stop.
4. **Reading ``ready`` is forbidden in the same breath** as ``queued``
   being the only schedulable phase, and the unified facade is named as
   the only way to stop anything.

Scope, stated honestly
----------------------
This gate checks the **text**. It cannot check that the consumer's
implementation obeys it — the running implementation and its tests live
in the scheduler's repository, and PDT adds no tick code of its own (see
section 2.4 of the contract). What this gate can do is make sure the
promise is *published* under the project's own name, pinned well enough
that the consumer's gate can assert on the same strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DOC = _REPO_ROOT / "docs" / "development" / "cross-repo-contract.md"

#: The consumer section's title, pinned verbatim. The scheduler's own gate
#: asserts on the same string, so the two repositories cannot drift into
#: calling the same section two different things. The leading ``##`` is
#: part of the pin: this half of the contract is a *level-2* section,
#: sibling to the provider half.
CONTRACT_SECTION_TITLE = "## 2. 消费方（调度器）契约"

#: The four load-bearing keywords of the consumer half. Each one carries a
#: rule that the scheduler would otherwise be free to break:
#:
#: * ``queued`` — the only schedulable phase;
#: * ``ready`` — the legacy alias the scheduler must never read;
#: * ``tick`` — the pipeline whose order is fixed;
#: * ``routing`` — the table that is the authority on current phase.
REQUIRED_KEYWORDS: tuple[str, ...] = ("queued", "ready", "tick", "routing")

#: The tick pipeline's fixed order, as the contract states it. The gate
#: asserts both membership *and* relative order: a list that names the
#: three steps in the wrong order is a different pipeline, not a
#: differently-worded one.
TICK_ORDER: tuple[str, ...] = ("对账", "收敛", "补位")

#: The phrases the consumer half has to carry verbatim, because they are
#: the rules themselves rather than a description of them.
MIN_RUNTIME_GUARD = "最小运行时长"
FACADE_DECIDES = "统一门面"
STOP_EXECUTION = "停执行"
STOP_VERIFICATION = "停验证"
ONLY_SCHEDULABLE = "唯一的可调度相位是 `queued`"
NEVER_READS_READY = "从不读 `ready`"
NO_TICK_CODE_HERE = "PDT 不新增 tick 代码"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def contract_text() -> str:
    """The contract document's text.

    Read once per module: the file does not change while the suite runs,
    and every test below reads the same bytes.
    """
    assert _CONTRACT_DOC.exists(), (
        f"{_CONTRACT_DOC.relative_to(_REPO_ROOT)} does not exist. The "
        "cross-repo contract has to be written down in one place both "
        "repositories can point at — a promise that lives only in a commit "
        "message is renegotiated by the next implementation change."
    )
    return _CONTRACT_DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body under ``heading``, including its subsections.

    A level-2 section owns everything below it until the next level-2
    heading — the level-3 subsections are part of it, not a boundary.
    The body therefore stops at the next heading of the *same or a
    higher* level, so a keyword that belongs to a different section
    cannot satisfy a check that is about this one.

    Fenced code blocks are skipped: a ``#`` inside one is a comment in
    an example, not a heading.
    """
    lines = text.splitlines()
    level = heading.count("#")
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    assert start is not None, (
        f"the contract document has no section titled {heading!r}"
    )
    body: list[str] = []
    in_fence = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            body.append(line)
            continue
        if not in_fence and stripped.startswith("#"):
            if stripped.count("#") <= level:
                break
            body.append(line)
            continue
        body.append(line)
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scheduler_section_exists(contract_text: str) -> None:
    """The consumer section exists as a level-2 heading, with its four keywords.

    The title is an identifier, not a caption: the consumer's own gate
    asserts on the same string. The four keywords are the rules the
    consumer half is made of — the schedulable phase, the forbidden one,
    the pipeline, and the authority — and they are checked *inside* the
    section, so a keyword that only appears in the provider half cannot
    satisfy a check about the consumer half.
    """
    assert any(
        line.strip() == CONTRACT_SECTION_TITLE
        for line in contract_text.splitlines()
    ), (
        "the contract document does not contain the pinned consumer "
        f"section title {CONTRACT_SECTION_TITLE!r}, as a level-2 heading. "
        "That title is the identifier the consumer's gate asserts on — "
        "the two repositories have to call the same section the same "
        "thing."
    )

    section = _section(contract_text, CONTRACT_SECTION_TITLE)

    for keyword in REQUIRED_KEYWORDS:
        assert keyword in section, (
            f"the consumer section does not mention {keyword!r}. The "
            "consumer half of the contract is made of these four rules: "
            "only `queued` is schedulable, `ready` is never read, the "
            "tick pipeline has a fixed order, and the routing table is "
            "the authority. A rule that is not written down is a rule "
            "the consumer is free to break."
        )


def test_scheduler_section_states_tick_order(contract_text: str) -> None:
    """The tick pipeline's order, the minimum-runtime guard, and the facade.

    Three things have to be pinned here, and all three are things a
    scheduler would otherwise be free to get wrong:

    * **The order.** Reconcile → converge → backfill. Backfilling before
      converging hands a slot to a plan whose state has not been
      reconciled yet — the two orders disagree about what the pipeline
      is for.
    * **The minimum-runtime guard.** A young run is not stopped. A
      scheduler that stops anything still running has to wait out the
      guard, or it will start and stop plans in a loop.
    * **Who decides whom to stop.** The unified lifecycle facade does,
      against live in-process state; the scheduler does not, because it
      cannot see that state and its own guess would diverge.
    """
    section = _section(contract_text, CONTRACT_SECTION_TITLE)

    positions: list[int] = []
    for step in TICK_ORDER:
        assert step in section, (
            f"the consumer section does not mention the tick step {step!r}. "
            "The tick pipeline's order is fixed — reconcile, converge, "
            "backfill — and each of the three steps has to be named."
        )
        positions.append(section.index(step))

    assert positions == sorted(positions), (
        "the consumer section does not state the tick order in order. The "
        "pipeline is reconcile → converge → backfill, and the order is "
        "load-bearing: converging after backfilling hands a slot to a "
        "plan whose state has not been reconciled yet."
    )

    assert MIN_RUNTIME_GUARD in section, (
        "the consumer section does not mention the minimum-runtime guard "
        f"({MIN_RUNTIME_GUARD}). A run that has not reached the minimum "
        "runtime is not stopped — otherwise the scheduler starts and "
        "stops plans in a loop."
    )

    assert FACADE_DECIDES in section, (
        f"the consumer section does not name the unified facade "
        f"({FACADE_DECIDES}) as the one that decides. Which layer to stop "
        "— the execution round or the verification round — is decided "
        "in-process, against live state the scheduler cannot see."
    )

    for phrase in (STOP_EXECUTION, STOP_VERIFICATION):
        assert phrase in section, (
            f"the consumer section does not mention {phrase!r}. The rule "
            "is about the choice between stopping the execution round and "
            "stopping the verification round; both have to be named for "
            "the rule to be about anything."
        )


def test_scheduler_section_forbids_reading_ready(contract_text: str) -> None:
    """Only ``queued`` is schedulable, ``ready`` is never read, the facade stops.

    Three rules that belong together, because they are the same rule seen
    from three sides:

    * **Only ``queued`` is schedulable.** The scheduler's inbox is
      ``queued``; a plan in any other phase is advanced by PDT itself.
    * **``ready`` is never read.** It is the legacy alias of ``queued``
      (section 1.2). A scheduler that still reads it schedules on a
      second vocabulary, and the deprecation log that is supposed to
      surface the old data never fires.
    * **Stopping goes through the unified facade.** The scheduler says
      "stop this plan"; the facade says which layer that means.
    """
    section = _section(contract_text, CONTRACT_SECTION_TITLE)

    assert ONLY_SCHEDULABLE in section, (
        "the consumer section does not say that `queued` is the only "
        "schedulable phase. The scheduler picks up plans by their "
        "waiting phase, and there is exactly one such phase."
    )

    assert NEVER_READS_READY in section, (
        "the consumer section does not forbid reading `ready`. The legacy "
        "spelling is an input alias for `queued` — a scheduler that "
        "reads it schedules on a second vocabulary, and the deprecation "
        "log that is meant to surface the old data stays silent."
    )

    assert FACADE_DECIDES in section, (
        f"the consumer section does not name the unified facade "
        f"({FACADE_DECIDES}) as the only way to stop anything."
    )

    for phrase in (STOP_EXECUTION, STOP_VERIFICATION):
        assert phrase in section, (
            f"the consumer section does not mention {phrase!r}. The "
            "scheduler must not decide which layer to stop; the facade "
            "does, and both layers have to be named for that to be a "
            "rule rather than a gesture."
        )

    assert NO_TICK_CODE_HERE in section, (
        "the consumer section does not say that PDT adds no tick code of "
        f"its own ({NO_TICK_CODE_HERE}). The tick pipeline is described "
        "here as a contract and implemented in the scheduler's "
        "repository; a reader who finds a tick implementation in this "
        "repository is looking at code that is not part of the contract."
    )
