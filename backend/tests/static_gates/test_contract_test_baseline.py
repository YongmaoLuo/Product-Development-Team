"""The cross-repo contract needs a test baseline and a merge gate — and
the writing has to stay pinned.

Why this gate exists
--------------------
Sections 1 and 2 of ``docs/development/cross-repo-contract.md`` freeze
*what* the two repositories promise each other: the endpoints, the
vocabulary, the tick pipeline, the authority. A promise with no test
behind it is a document. The next implementation change rewrites it
quietly, and because the writing is careful and the behaviour is not,
nothing goes red: PDT renames an error code, or the scheduler's client
starts reading a phase the contract never mentioned, and the two
repositories stop agreeing without anybody noticing.

The merge order has the same problem. "PDT first, the scheduler only
consumes what has already been merged" is only enforceable if the thing
being consumed says which tests have to exist and which CI job has to
run. Otherwise it is a sentence in a planning document, and the order
gets inverted by whoever is in a hurry — which is exactly how a consumer
ends up coding against semantics that were never merged.

So this gate pins the last two sections of the contract:

* **section 3 — the test baseline.** Which cases have to exist, who
  writes them, and where they are asserted: the consumer's
  ``PDTClient`` boundary tests (phase whitelist, facade calls, error
  code mapping), the concurrency-convergence cases, and the E2E main
  flow plus fault injection.
* **section 4 — the merge order.** PDT goes first, and the scheduler's
  pull request has to run a contract-test job in its CI.

What it pins
------------
1. **Both sections exist as level-2 headings, with the pinned titles**,
   and they live in the same Markdown document as the two halves that
   came before — a second file is a second contract, and an executable
   one drifts.
2. **The baseline lists the cases by name.** ``PDTClient``, the phase
   whitelist, the unified facade, the error-code mapping, concurrency
   convergence, E2E, the main flow and fault injection. A baseline that
   says "the consumer should test the contract" is not a baseline.
3. **The document does not promise behaviour the consumer has not
   implemented.** The cases are obligations — *"must be implemented by
   the consumer and asserted in its CI"* — not a description of a
   consumer repository that does not exist yet. The gate refuses the
   wording that would turn an obligation into a claim.
4. **The merge order is PDT first, and it is checkable.** The order is
   spelled out, and the scheduler's pull request is required to run a
   contract-test job in CI — the requirement itself, phrased as a
   requirement.

Scope, stated honestly
----------------------
This gate checks the **text**. The cases in section 3 live in the
scheduler's repository; PDT adds no consumer runtime code and no
consumer runtime tests (section 2.4 of the contract says the same about
the tick pipeline). What this gate can do is make sure the baseline and
the merge order are *published* under the project's own name, pinned well
enough that the consumer's own CI can assert against the same strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS_DIR = _REPO_ROOT / "docs"
_DOC_DIR = _DOCS_DIR / "development"
_CONTRACT_DOC = _DOC_DIR / "cross-repo-contract.md"

#: The baseline section's title, pinned verbatim. The consumer's own CI
#: asserts on the same string, so the two repositories cannot drift into
#: calling the same section two different things.
TEST_BASELINE_SECTION_TITLE = "## 3. 测试基线"

#: The merge-order section's title, pinned verbatim, for the same reason.
MERGE_ORDER_SECTION_TITLE = "## 4. 合入顺序"

#: The two halves that came before. They are pinned here so that
#: "both new sections live in the same document as the old ones" is a
#: checkable statement rather than an assumption.
PROVIDER_SECTION_TITLE = "## 1. 提供方（PDT）契约"
CONSUMER_SECTION_TITLE = "## 2. 消费方（调度器）契约"

#: The consumer's client — the only door the scheduler has into PDT.
CLIENT_NAME = "PDTClient"

#: The three groups of ``PDTClient`` boundary cases. Each one is a rule
#: the client would otherwise be free to break: read the wrong phase,
#: bypass the facade, or flatten a conflict into a server error.
BOUNDARY_CASE_GROUPS: tuple[str, ...] = (
    "相位白名单",
    "统一门面",
    "错误码映射",
)

#: The status codes the error-code mapping has to cover. ``409`` is the
#: one that matters most: "you cannot stop this now" and "the server
#: is broken" have to stay distinguishable on the consumer's side.
MAPPED_STATUS_CODES: tuple[str, ...] = ("404", "409", "400")

#: The concurrency-convergence baseline: what happens when two things
#: touch the same plan at the same time.
CONCURRENCY_CASE = "并发收敛"

#: The E2E baseline: the main flow, and the failures injected into it.
E2E_CASE = "E2E"
E2E_FLOW_CASES: tuple[str, ...] = ("主流程", "故障注入")

#: The framing that keeps this document from promising behaviour the
#: consumer has not implemented yet. The cases are obligations; the
#: wording has to say so.
CONSUMER_OWNS = "必须由消费方实现并在其 CI 断言"

#: The merge order, stated twice: once as the rule's name, once as the
#: sentence that spells out which repository goes first.
PDT_FIRST = "PDT 先行"
MERGE_SEQUENCE = "先合 PDT，再合调度器"

#: The gate on the consumer's side: its pull request has to run a
#: contract-test job in CI.
CONTRACT_TEST_JOB = "契约测试 job"

#: Wording that would turn an obligation into a claim about a consumer
#: repository that does not exist yet. Each entry is a shape a careless
#: edit would leave behind.
UNIMPLEMENTED_CLAIMS: tuple[str, ...] = (
    "调度器已实现",
    "消费方已实现",
    "已在消费方仓实现",
    "消费方仓已实现",
)


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
        "repositories can point at — a promise that lives only in a "
        "commit message is renegotiated by the next implementation change."
    )
    return _CONTRACT_DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body under ``heading``, including its subsections.

    A level-2 section owns everything below it until the next heading of
    the *same or a higher* level — the level-3 subsections are part of
    it, not a boundary. The body therefore stops at the next heading of
    the same or a higher level, so a keyword that belongs to a different
    section cannot satisfy a check that is about this one.

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


def test_test_baseline_section_exists(contract_text: str) -> None:
    """Both new sections exist, and they live in the same document.

    The contract is one document with four halves: what PDT serves, what
    the scheduler may do, what has to be tested, and in which order the
    two repositories merge. A baseline that lives in a second file is a
    second contract — and an executable one, because the gate that reads
    it is Python. So the two new sections have to be level-2 headings in
    the same Markdown file as the two that came before, and the docs tree
    must hold no Python at all.
    """
    lines = contract_text.splitlines()

    for title in (
        PROVIDER_SECTION_TITLE,
        CONSUMER_SECTION_TITLE,
        TEST_BASELINE_SECTION_TITLE,
        MERGE_ORDER_SECTION_TITLE,
    ):
        assert any(line.strip() == title for line in lines), (
            f"the contract document does not contain the pinned section "
            f"title {title!r}, as a level-2 heading. The section titles "
            "are the identifiers the consumer's own gate asserts on — "
            "the two repositories have to call the same section the "
            "same thing."
        )

    python_files = sorted(_DOCS_DIR.rglob("*.py"))
    assert not python_files, (
        "the docs tree holds a Python file: "
        + ", ".join(str(p.relative_to(_REPO_ROOT)) for p in python_files)
        + ". The contract is a document; the only code that belongs to "
        "it is the gate itself, and it lives under "
        "backend/tests/static_gates/."
    )


def test_baseline_lists_pdtclient_cases(contract_text: str) -> None:
    """The baseline names the cases: client boundary, concurrency, E2E.

    A baseline that says "the consumer should test the contract" is not
    a baseline — it cannot fail, so it cannot gate anything. The three
    groups have to be named:

    * **The ``PDTClient`` boundary cases** — phase whitelist, facade
      calls, error-code mapping. These are the three rules a client
      would otherwise be free to break: read a phase it must not read,
      bypass the facade, or flatten a conflict into a server error.
    * **The concurrency-convergence cases** — what happens when two
      things touch the same plan at once, and when the scheduler's own
      state and the routing table disagree.
    * **The E2E main flow and fault injection** — the flow against a
      real resident scheduler and a real PDT backend, and the failures
      injected into it.

    And the ownership has to be stated: these are obligations on the
    consumer, not claims about a consumer repository that does not exist
    yet.
    """
    section = _section(contract_text, TEST_BASELINE_SECTION_TITLE)

    assert CLIENT_NAME in section, (
        f"the test baseline does not mention {CLIENT_NAME!r}. The "
        "consumer's client is the only door it has into PDT; a baseline "
        "that does not name the thing under test does not say what has "
        "to be asserted."
    )

    for group in BOUNDARY_CASE_GROUPS:
        assert group in section, (
            f"the test baseline does not list the {group!r} boundary "
            "case. The client's boundary is exactly three rules: which "
            "phases it may read, which door it goes through, and how it "
            "translates the errors it gets back. A case that is not "
            "named is a case nobody has to write."
        )

    for code in MAPPED_STATUS_CODES:
        assert code in section, (
            f"the test baseline does not mention the status code {code!r} "
            "in the error-code mapping. The codes the provider promises "
            "have to be named on both sides: the consumer has to map "
            "them, and 409 in particular has to stay distinguishable "
            "from 500 — 'you cannot stop this now' and 'the server is "
            "broken' are different problems."
        )

    assert CONCURRENCY_CASE in section, (
        f"the test baseline does not list the {CONCURRENCY_CASE!r} "
        "case. Two concurrent requests on one plan, and a scheduler "
        "whose own state has drifted from the routing table, are the "
        "cases where the two repositories would otherwise disagree "
        "quietly."
    )

    assert E2E_CASE in section, (
        f"the test baseline does not mention {E2E_CASE!r}. The main "
        "flow has to run against a real resident scheduler and a real "
        "PDT backend; a stubbed client proves the stub works."
    )

    for case in E2E_FLOW_CASES:
        assert case in section, (
            f"the test baseline does not list the {case!r} case. The "
            "E2E baseline is two things: the main flow, and the "
            "failures injected into it. A baseline with only the happy "
            "path never finds the interesting bug."
        )

    assert CONSUMER_OWNS in section, (
        f"the test baseline does not say that the cases {CONSUMER_OWNS!r}. "
        "The runtime implementation of these cases belongs to the "
        "consumer's repository — PDT adds no consumer runtime code — so "
        "the document has to state the obligation rather than describe "
        "a repository that does not exist yet."
    )

    for claim in UNIMPLEMENTED_CLAIMS:
        assert claim not in section, (
            f"the test baseline claims {claim!r}. The consumer has not "
            "implemented these cases yet; writing it as a fact turns "
            "an obligation into a claim, and the merge gate in section "
            "4 then reads as if the guarantee already exists."
        )


def test_merge_order_section_puts_pdt_first(contract_text: str) -> None:
    """The merge order is PDT first, and the consumer's CI has to prove it.

    Two things have to be written down, and both have to be checkable:

    * **The order.** PDT goes first; the scheduler only consumes what
      has already been merged. The section says so twice — once as the
      rule's name and once as the sequence — so that "who merges first"
      is not a matter of reading comprehension.
    * **The gate on the consumer's side.** The scheduler's pull request
      has to run a contract-test job in CI, and that job has to assert
      the baseline from section 3. Without it, the scheduler's pull
      request is merely *compatible-looking*.

    The job is a requirement on the consumer, phrased as a requirement:
    it must be implemented by the consumer and asserted in its CI.
    """
    section = _section(contract_text, MERGE_ORDER_SECTION_TITLE)

    assert "合入顺序" in section, (
        "the merge-order section does not talk about the merge order. "
        "The section exists to carry one rule; a section that does not "
        "name the thing it is about cannot be asserted on."
    )

    assert PDT_FIRST in section, (
        f"the merge-order section does not say {PDT_FIRST!r}. The order "
        "is not a preference: the scheduler codes against PDT's "
        "endpoints, fields and phase words, so a scheduler that merges "
        "first is merged against semantics nobody shipped yet."
    )

    assert MERGE_SEQUENCE in section, (
        f"the merge-order section does not spell the sequence out "
        f"({MERGE_SEQUENCE!r}). Saying which repository goes first has "
        "to be unambiguous — 'PDT 先行' names the rule, the sentence "
        "names the order, and both are needed for the two repositories "
        "to answer the same question the same way."
    )

    assert CONTRACT_TEST_JOB in section, (
        f"the merge-order section does not require a {CONTRACT_TEST_JOB!r} "
        "in the consumer's CI. The merge order is only enforceable if "
        "the consumer's pull request runs the tests that assert the "
        "contract it is coding against."
    )

    for token in ("调度器", "PR", "CI"):
        assert token in section, (
            f"the merge-order section does not mention {token!r}. The "
            "gate has to be attached to a specific thing: the "
            "scheduler's pull request, running in its CI."
        )

    assert CONSUMER_OWNS in section, (
        f"the merge-order section does not say that the contract-test job "
        f"{CONSUMER_OWNS!r}. The job lives in the consumer's CI, so the "
        "document has to state the obligation rather than describe a "
        "pipeline that does not exist yet."
    )

    for claim in UNIMPLEMENTED_CLAIMS:
        assert claim not in section, (
            f"the merge-order section claims {claim!r}. The consumer has "
            "not implemented the contract-test job yet; writing it as a "
            "fact turns the merge gate into a description of something "
            "that is not there."
        )
