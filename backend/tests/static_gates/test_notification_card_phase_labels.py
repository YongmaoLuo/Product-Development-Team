"""The notification card's phase vocabulary: ``queued`` is the only
waiting phase.

Why this gate exists
--------------------
``backend/notifications/cards.py`` renders the Feishu card, and the card
carries three phase vocabularies of its own:

  1. ``_PHASE_LABEL`` — the header line ("⏸ 就绪", "🕒 已排队", …).
  2. ``_EXECUTION_PHASE_LABEL`` — the "what is the plan doing right now"
     sub-line ("准备启动", "已排队，等待调度", …).
  3. the inline base vocabulary inside ``_resolve_header`` — the short
     title of a mid-plan card ("就绪", "已排队", …).

All three used to spell the waiting phase ``ready``, and all three also
had a ``queued`` key with slightly different wording ("已排队" in one
place, "已排队，等待调度" in another). Two spellings of one state, in
three tables, is how an operator ends up staring at a card that says
"就绪" while the plan is sitting in the scheduler's inbox.

The waiting phase has a single spelling now — ``queued`` — and it is
spelled the same way everywhere: 「排队中」. ``ready`` is an *input
alias* (something a caller may still POST, normalised upstream), not a
phase the card renders, so it has no entry in any of the three
vocabularies.

The colour rule rides along with the vocabulary: the green "nothing is
wrong, this plan is waiting for a human" branch used to fire on
``phase == "ready"``. If the key goes and the branch does not move with
it, a queued card renders blue — the same colour as a running plan —
and the operator can no longer tell "waiting" from "working" by
looking at it. That is why the branch is part of this gate and not an
implementation detail.

What is deliberately NOT asserted
----------------------------------
``ready`` remains a legal *input* to the state-update API (normalised
to ``queued`` upstream), and the plan state machine keeps it in its
transition table for that reason. This gate is about the card's
vocabulary only — it says nothing about which phases a caller may send.

The verification sub-machine's labels ("🔍 初次验证", "✅ 验证通过", …)
are also untouched: the merge is about the *waiting* phase, and a
verification phase that was renamed by accident would be a different
defect with a different owner.

What this test does
-------------------
1. Asserts that no dict literal in ``cards.py`` carries a ``ready`` key
   (that covers all three vocabularies, including the inline one), and
   that no comparison against the ``ready`` phase remains.
2. Asserts the exact ``queued`` wording in the two module-level tables
   and in the rendered base title.
3. Asserts the green colour branch fires for ``queued`` — by calling
   the pure ``_resolve_header`` and by reading the source, so the branch
   cannot be satisfied by a coincidentally-green outcome.

Scan target
-----------
``backend/notifications/cards.py``, read as source AND imported as a
module. The source read is what proves the *absence* of a key (a
runtime ``.get`` cannot tell "the key is gone" from "the key is there
but empty"); the import is what proves the *wording*.
"""

from __future__ import annotations

import ast
from pathlib import Path

from notifications import cards
from plan_status import PlanStatus


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CARDS_PY = _REPO_ROOT / "backend" / "notifications" / "cards.py"


def _cards_source() -> str:
    return _CARDS_PY.read_text(encoding="utf-8")


def _cards_tree() -> ast.Module:
    return ast.parse(_cards_source())


def _string_keys(node: ast.Dict) -> list[str]:
    """The string-literal keys of a dict literal, in source order."""
    return [
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    ]


def test_phase_label_has_no_ready_key():
    """No vocabulary in the card module may spell the waiting phase
    ``ready``."""
    assert "ready" not in cards._PHASE_LABEL, (
        "the card header vocabulary must not carry the legacy waiting "
        f"phase: {sorted(cards._PHASE_LABEL)}"
    )
    assert "ready" not in cards._EXECUTION_PHASE_LABEL, (
        "the card execution sub-line vocabulary must not carry the "
        f"legacy waiting phase: {sorted(cards._EXECUTION_PHASE_LABEL)}"
    )

    # The inline base vocabulary inside ``_resolve_header`` is a dict
    # literal too, and it is invisible to the two assertions above —
    # this is the walk that covers it (and every future table).
    offenders = [
        node.lineno
        for node in ast.walk(_cards_tree())
        if isinstance(node, ast.Dict) and "ready" in _string_keys(node)
    ]
    assert not offenders, (
        "cards.py must not define a 'ready' key in any vocabulary; "
        f"found one at line(s) {offenders}"
    )

    # A comparison against the legacy phase is the same mistake wearing
    # a different hat: it would colour a card by a phase that no longer
    # exists in any table.
    comparisons = [
        node.lineno
        for node in ast.walk(_cards_tree())
        if isinstance(node, ast.Compare)
        and any(
            isinstance(operand, ast.Constant) and operand.value == "ready"
            for operand in node.comparators
        )
    ]
    assert not comparisons, (
        "cards.py must not compare against the legacy 'ready' phase; "
        f"found one at line(s) {comparisons}"
    )

    # An unknown phase still falls back to the raw value — the card must
    # never raise KeyError on a phase it does not know (legacy rows, a
    # newer vocabulary, a typo). Same for ``ready`` itself now that it
    # is no longer a key.
    assert cards._PHASE_LABEL.get("no_such_phase", "no_such_phase") == (
        "no_such_phase"
    )
    header = cards._resolve_header(
        PlanStatus(plan_id="p-unknown", phase="no_such_phase"),
        {"total": 1},
    )
    assert header["title"] == "no_such_phase · p-unknown", header
    legacy_header = cards._resolve_header(
        PlanStatus(plan_id="p-legacy", phase="ready"),
        {"total": 1},
    )
    assert legacy_header["title"] == "ready · p-legacy", (
        "a phase with no label must fall through to the raw value: "
        f"{legacy_header}"
    )


def test_queued_label_is_chinese():
    """``queued`` is spelled 「排队中」 in every card vocabulary."""
    assert cards._PHASE_LABEL["queued"] == "🕒 排队中", (
        "the card header label for the waiting phase must be 「排队中」: "
        f"{cards._PHASE_LABEL.get('queued')!r}"
    )
    assert cards._EXECUTION_PHASE_LABEL["queued"] == "排队中，等待调度", (
        "the card execution sub-line for the waiting phase must be "
        f"「排队中，等待调度」: {cards._EXECUTION_PHASE_LABEL.get('queued')!r}"
    )

    # The inline base vocabulary is what a mid-plan card actually
    # renders — check it through the pure renderer, not the source.
    header = cards._resolve_header(
        PlanStatus(plan_id="p-queued", phase="queued"),
        {"total": 1},
    )
    assert header["title"] == "排队中 · p-queued", header

    # The verification sub-machine is a different vocabulary and must
    # not move with the waiting phase.
    for phase, label in (
        ("verification", "🔍 初次验证"),
        ("verification_passed", "✅ 验证通过"),
        ("verification_failed", "❌ 验证失败"),
    ):
        assert cards._PHASE_LABEL[phase] == label, (
            f"the verification label for {phase!r} must stay "
            f"{label!r}: {cards._PHASE_LABEL.get(phase)!r}"
        )


def test_color_branch_uses_queued():
    """The green "waiting, nothing is wrong" branch fires on ``queued``."""
    header = cards._resolve_header(
        PlanStatus(plan_id="p-queued", phase="queued"),
        {"total": 1},
    )
    assert header["color"] == "green", (
        "a queued plan must render green, the colour that means "
        f"'waiting, nothing is wrong': {header}"
    )

    source = _cards_source()
    assert 'if phase == "queued":' in source, (
        "the green colour branch must be keyed on the queued phase"
    )
    assert 'if phase == "ready":' not in source, (
        "the green colour branch must no longer be keyed on the legacy "
        "ready phase"
    )
