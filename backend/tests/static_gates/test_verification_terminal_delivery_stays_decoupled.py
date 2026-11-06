"""Static gate — the terminal branch stays decoupled from the delivery layer.

Why this gate exists
--------------------
The terminal round — the round in which verification has failed and a
human has to decide what happens next — reaches a person through exactly
one door: :func:`hitl.service.create_decision`.  The delivery layer
(:mod:`backend.hooks.session_send`) is the cc-connect protocol, and
requirement 5 of the material says it stays untouched: the terminal
branch must not change the delivery protocol.

Nothing at runtime pinned that.  The terminal branch's own tests check
that a decision *is created*; they say nothing about how the
orchestration layer reaches the human, and nothing about what the
delivery layer knows about the terminal branch.  So both ways the
constraint can break are silent:

* the orchestration layer starts reaching the delivery layer directly
  (or shelling out to ``cc-connect send``), which bypasses the decision
  record — the human sees a message nobody can answer, and the plan
  keeps no trace of the ask;
* the delivery layer starts carrying terminal-branch vocabulary
  (``repair_execution_failed``, ``rerun_this_round``, …), which welds
  one branch's semantics into the shared protocol — the next branch
  that needs a different set of options has to edit the protocol to
  grow.

What this file pins
-------------------
1. **The orchestration layer reaches a human only through
   ``hitl.service.create_decision``.**  ``verification/orchestrator.py``
   must not name the delivery layer at all: no ``session_send``, no
   ``from hooks`` / ``import hooks``, no ``cc-connect send``.
2. **The delivery layer carries no terminal-branch vocabulary.**  The
   six tokens below are the terminal branch's own words; the protocol
   must not learn them.

   The list carries the **retired** option ids as well as the current
   ones, on purpose.  ``accept_as_failed`` and ``manual_repair_tasks``
   are no longer on the menu, but they are still the branch's words —
   and the delivery protocol learning one of them would be just as bad
   as learning a live one.  A token leaves this list when the *branch*
   drops the word for good, not when the menu changes.

What is deliberately *not* pinned
--------------------------------
Generic delivery fields (``question``, ``options``, …) are fine — the
protocol is allowed to grow generic payload.  The gate bans the
terminal branch's *specific* words, not the shape of a message, and it
bans no generic word that an unrelated edit could legitimately introduce.

If the orchestration layer ever genuinely needs to reach the delivery
layer directly, this gate has to be edited on purpose — that is the
whole point of having it.  A constraint that only lives in somebody's
memory is a constraint that gets forgotten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

#: ``backend/tests/static_gates`` is two levels below ``backend/``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The orchestration layer that reaches a human.
_ORCHESTRATOR = _BACKEND_DIR / "verification" / "orchestrator.py"

#: The cc-connect delivery layer that requirement 5 leaves untouched.
_SESSION_SEND = _BACKEND_DIR / "hooks" / "session_send.py"

#: The terminal branch's own vocabulary.  These words belong to the
#: branch that asks the human what to do; the shared delivery protocol
#: has no business knowing them.
_TERMINAL_TOKENS: Tuple[str, ...] = (
    "repair_execution_failed",
    "rerun_this_round",
    "revise_criterion",
    "accept_as_failed",
    "manual_repair_tasks",
    "_terminal_decision_question",
)


def test_orchestrator_reaches_a_human_only_through_create_decision() -> None:
    """The orchestration layer must not talk to the delivery layer."""
    source = _ORCHESTRATOR.read_text(encoding="utf-8")
    assert "session_send" not in source, (
        "verification/orchestrator.py names the delivery layer — a human "
        "is reached through hitl.service.create_decision, not by talking "
        "to the cc-connect protocol directly."
    )
    assert "from hooks" not in source, (
        "verification/orchestrator.py imports from the delivery layer — "
        "the decision record is the only sanctioned way to ask a human."
    )
    assert "import hooks" not in source, (
        "verification/orchestrator.py imports the delivery layer — the "
        "decision record is the only sanctioned way to ask a human."
    )
    assert "cc-connect send" not in source, (
        "verification/orchestrator.py shells out to cc-connect — a message "
        "sent that way leaves no decision behind for anyone to answer."
    )


def test_delivery_layer_carries_no_terminal_branch_vocabulary() -> None:
    """The delivery protocol must not learn the terminal branch's words."""
    source = _SESSION_SEND.read_text(encoding="utf-8")
    for token in _TERMINAL_TOKENS:
        assert token not in source, (
            f"the delivery layer learned terminal-branch vocabulary: {token!r}"
        )