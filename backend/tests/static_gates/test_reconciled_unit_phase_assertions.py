"""The reconciled unit tests must speak the merged waiting phase: ``queued``.

Why this gate exists
--------------------
Three phase-semantics changes landed underneath three existing unit tests
(``ready`` is now spelled ``queued`` on the write path; ``ready`` survives
only as an *input* alias):

  * the plan-state detector now infers ``queued`` for a dev-branch
    directory (``tasks.json``);
  * the dead-end rollback now CAS's the routing row to ``queued``;
  * the hard-timeout auto-split now writes ``transition_to("queued")``.

Each of those changes broke one existing assertion. The assertions were
re-aligned — but a re-aligned assertion is only worth what the reader of
the file can tell from it. The failure mode this gate closes is silent:
a test that still says ``ready`` fails loudly, but a test that says
``queued`` for the *wrong* reason, or a hint message that still tells the
next reader to write ``ready`` back, passes the suite and misleads the
person who has to debug it.

So the gate pins the *text* of the three reconciled files: the assertion
spelling, the test name, and the hint text that describes them. It reads
source and asserts on it; it does **not** import or execute the tests it
checks (that is what the tests themselves are for — a gate that ran them
would be a second copy of the suite with none of the suite's
diagnostics).

Scan target
-----------
  * ``backend/tests/unit/test_plan_state_verification_transitions.py``
  * ``backend/tests/unit/test_startup_reconcile_orphaned_verification.py``
  * ``backend/tests/test_hard_timeout_auto_split.py``
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PLAN_STATE_TRANSITIONS = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "unit"
    / "test_plan_state_verification_transitions.py"
)
_STARTUP_RECONCILE = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "unit"
    / "test_startup_reconcile_orphaned_verification.py"
)
_HARD_TIMEOUT_AUTO_SPLIT = (
    _REPO_ROOT / "backend" / "tests" / "test_hard_timeout_auto_split.py"
)

#: The name the dead-end rollback test must carry. The name is part of
#: the contract: a test called ``..._rolls_back_to_ready`` that asserts
#: ``queued`` is a test whose name lies about what it pins.
_ROLLBACK_TEST_NAME = (
    "test_dead_end_repairing_with_unfinished_tasks_rolls_back_to_queued"
)


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError`` — a gate that dies on a
    malformed byte reports nothing useful about the contract it was
    written to protect.
    """
    assert path.exists(), f"{path} is missing; the gate cannot check it"
    return path.read_text(encoding="utf-8", errors="replace")


def test_reconciled_unit_tests_assert_queued() -> None:
    """The two re-aligned phase assertions must read ``queued``.

    ``test_plan_state_verification_transitions.py`` pins what the
    detector infers for a dev-branch directory: ``queued``. The dead-end
    rollback pins what the routing row is CAS'd to: ``queued``.

    The retired spelling must be gone from these assertions. ``ready``
    still exists as an *input* alias (a row written before the merge
    still reads ``ready``), so the gate checks the assertion lines, not
    the whole file — a file that mentions ``ready`` while *writing* a
    legacy state on purpose is fine.
    """
    plan_state_src = _source(_PLAN_STATE_TRANSITIONS)
    assert 'get_current_phase() == "queued"' in plan_state_src, (
        "the plan-state detector test must assert `queued`: a directory "
        "holding only `tasks.json` is now inferred as `queued`, not "
        "`ready`."
    )
    assert 'get_current_phase() == "ready"' not in plan_state_src, (
        "the plan-state detector test still asserts `ready`. The waiting "
        "phase is `queued`; `ready` is only an input alias."
    )

    reconcile_src = _source(_STARTUP_RECONCILE)
    assert 'row["current_phase"] == "queued"' in reconcile_src, (
        "the dead-end rollback test must assert `queued`: the rollback "
        "CAS now writes `queued`, not `ready`."
    )
    assert 'row["current_phase"] == "ready"' not in reconcile_src, (
        "the dead-end rollback test still asserts `ready`. The rollback "
        "target is `queued` — `ready` is only an input alias."
    )


def test_rollback_test_name_mentions_queued() -> None:
    """The rollback test's *name* must name ``queued``.

    A test named ``..._rolls_back_to_ready`` that asserts ``queued``
    reads as a contradiction, and the contradiction is what sends the
    next person back to ``ready``. The name is part of what this
    reconciled suite pins.
    """
    src = _source(_STARTUP_RECONCILE)

    assert f"def {_ROLLBACK_TEST_NAME}(" in src, (
        f"the dead-end rollback test must be named {_ROLLBACK_TEST_NAME!r}; "
        "a test whose name says `ready` while its body says `queued` "
        "misleads whoever reads it next."
    )
    assert (
        "def test_dead_end_repairing_with_unfinished_tasks_rolls_back_to_ready("
        not in src
    ), (
        "the dead-end rollback test is still named `..._rolls_back_to_ready`. "
        "The rollback target is `queued`; keep the name and the assertion "
        "saying the same thing."
    )


def test_agent_source_assertion_uses_queued() -> None:
    """The hard-timeout source assertion must look for ``transition_to("queued")``.

    ``test_agent_py_has_hard_timeout_branch`` reads ``agent.py`` as text
    and asserts that the auto-split branch transitions the plan. The
    branch now writes ``transition_to("queued")``; an assertion that
    still looks for ``transition_to("ready")`` fails — and the natural
    "fix" for a failing source assertion is to change the string being
    looked for. That is exactly the edit this gate pins: the assertion
    must look for the spelling the source actually has.
    """
    src = _source(_HARD_TIMEOUT_AUTO_SPLIT)

    assert """assert 'transition_to("queued")' in src""" in src, (
        "the hard-timeout source assertion must look for "
        "`transition_to(\"queued\")`: that is what `agent.py` writes after "
        "an auto-split."
    )
    assert """assert 'transition_to("ready")' in src""" not in src, (
        "the hard-timeout source assertion still looks for "
        "`transition_to(\"ready\")`. `agent.py` writes `queued` now, so "
        "this assertion can only fail."
    )


def test_reconciled_files_document_the_queued_phase() -> None:
    """Each reconciled file's own description must name ``queued`` too.

    The hint text is what the next reader trusts: a docstring that
    still says "rolls back to ready" or "transitions to ready" is how an
    assertion gets "corrected" back to the retired spelling months
    later, with a green suite standing there saying nothing.

    This is a text gate on the three reconciled files' module-level
    descriptions — the same places a reader looks first.
    """
    plan_state_src = _source(_PLAN_STATE_TRANSITIONS)
    assert "（queued / 审核阶段 / 原结果）" in plan_state_src, (
        "the plan-state test's module docstring still describes the "
        "inference result as `ready`; the detector infers `queued` now."
    )

    reconcile_src = _source(_STARTUP_RECONCILE)
    assert "roll back to ``queued``" in reconcile_src, (
        "the startup-reconcile module comment still says the unfinished-"
        "work path rolls back to ``ready``; it rolls back to ``queued``."
    )

    hard_timeout_src = _source(_HARD_TIMEOUT_AUTO_SPLIT)
    assert '``plan_state.transition_to("queued")``' in hard_timeout_src, (
        "the hard-timeout module docstring still says the auto-split "
        "transitions to `ready`; it transitions to `queued`."
    )
