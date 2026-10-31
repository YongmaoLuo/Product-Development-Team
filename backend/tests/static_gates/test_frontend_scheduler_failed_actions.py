"""A failed plan must offer two different buttons, and they must say so.

Why this gate exists
--------------------
A plan that failed carries one button: 「启动」. That single button covers
two different intentions, and its label names neither of them:

* putting the plan back in the queue and running it again, and
* running the *verification* round again.

Both are wanted, and they are different commands. With one button, the
operator who means "re-verify this" presses 「启动」, which re-queues the
whole plan — a strictly heavier operation than re-running the
verification round — and the label gives them no way to tell that before
they press it. 「启动」 on a failed plan is a promise the button does not
keep.

The rule
--------
* ``SCHEDULER_ACTIONS_BY_PHASE['failed']`` offers **both** ``start`` and
  ``rerun-verification``.
* The two are labelled differently: 「重新排队执行」 for ``start`` (a
  phase-specific label, because 「启动」 on a failed plan would read as
  "run the whole thing again") and 「重跑验证」 for
  ``rerun-verification`` (the action's own label).
* A button's label is decided by ``schedulerActionLabel(action, phase)``:
  the phase-specific label when there is one, the action's own label
  otherwise. A phase that is not covered — including *no* phase at all —
  falls back to the action's own label, so ``start`` outside ``failed``
  still reads 「启动」, and the queue row's start button (which passes no
  phase) is unchanged.
* The two terminal verification phases offer the re-run: a plan whose
  verification stopped or failed is exactly the plan the operator wants
  to verify again.

Scan target
-----------
``frontend/app.js``, read as source. This is a shape gate, like the rest
of ``static_gates/``: it runs on a plain Python environment with no JS
toolchain, so it pins the contract in the source rather than executing
the page. The label chain is checked both ways — the tables are parsed
out of the source and resolved here, and the source is required to spell
the same chain out — so a table that says one thing and a resolver that
says another cannot both pass.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The label a failed plan's start button must carry. The action's own
#: label is 「启动」; on a failed plan that reads as "run the whole thing
#: again", which is not what the button does.
_FAILED_START_LABEL = "重新排队执行"

#: The label the re-run-verification button carries, everywhere.
_RERUN_LABEL = "重跑验证"

#: The action key the re-run-verification button carries.
_RERUN_ACTION = "rerun-verification"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError`` — a gate that dies on a
    malformed byte reports nothing useful about the contract it was
    written to protect.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _normalized(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed away.

    The gates here read the source as text, so the exact line breaks and
    indentation a change happens to use must not decide whether the
    contract holds.
    """
    return re.sub(r"\s+", "", text)


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    The body ends at the first line that is exactly ``}`` — the file's
    top-level functions close at column zero, so that line ends the
    function no matter what the body contains. Brace counting would be
    the alternative, and it miscounts the template literals this file is
    full of.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name}() not found in the source"
    lines = source[match.end() :].splitlines()
    collected: list[str] = []
    for line in lines:
        if line.rstrip() == "}":
            return "\n".join(collected)
        collected.append(line)
    raise AssertionError(f"unterminated function {name}()")


def _object_body(source: str, name: str) -> str:
    """Return the body of the object literal ``const name = { ... }``.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename fails loudly instead of matching nothing and passing.
    """
    match = re.search(r"\bconst\s+" + re.escape(name) + r"\s*=\s*\{", source)
    assert match is not None, f"const {name} not found in the source"
    start = match.end() - 1  # index of the opening '{'
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : index]
    raise AssertionError(f"unbalanced braces in const {name}")


#: ``key: "value"`` inside an object literal — the key is either a bare
#: identifier or a quoted string (the action key ``rerun-verification``
#: is not a valid identifier and has to be quoted).
_FLAT_ENTRY = re.compile(
    r'(?:"([^"]+)"|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*"([^"]*)"'
)

#: ``key: { ... }`` — one level of nesting, which is all the
#: phase-specific label map needs.
_NESTED_ENTRY = re.compile(
    r'(?:"([^"]+)"|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*\{'
)

#: ``key: [ ... ]`` — the action matrix's row.
_ACTION_ROW = re.compile(
    r'(?:"([^"]+)"|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*\[([^\]]*)\]'
)


def _flat_map(body: str) -> dict[str, str]:
    """Parse a flat ``key: "value"`` object-literal body into a dict."""
    result: dict[str, str] = {}
    for match in _FLAT_ENTRY.finditer(body):
        key = match.group(1) if match.group(1) is not None else match.group(2)
        result[key] = match.group(3)
    return result


def _nested_map(body: str) -> dict[str, dict[str, str]]:
    """Parse a ``key: { ... }`` object-literal body into a dict of dicts."""
    result: dict[str, dict[str, str]] = {}
    for match in _NESTED_ENTRY.finditer(body):
        key = match.group(1) if match.group(1) is not None else match.group(2)
        start = match.end() - 1  # index of the opening '{'
        depth = 0
        for index in range(start, len(body)):
            if body[index] == "{":
                depth += 1
            elif body[index] == "}":
                depth -= 1
                if depth == 0:
                    result[key] = _flat_map(body[start + 1 : index])
                    break
        else:
            raise AssertionError(f"unbalanced braces in the {key} entry")
    return result


def _actions_by_phase(source: str) -> dict[str, list[str]]:
    """Parse ``SCHEDULER_ACTIONS_BY_PHASE`` into ``{phase: [action, ...]}``."""
    body = _object_body(source, "SCHEDULER_ACTIONS_BY_PHASE")
    result: dict[str, list[str]] = {}
    for match in _ACTION_ROW.finditer(body):
        key = match.group(1) if match.group(1) is not None else match.group(2)
        result[key] = re.findall(r'"([^"]*)"', match.group(3))
    return result


def _labels(source: str) -> dict[str, str]:
    """Parse ``SCHEDULER_ACTION_LABELS`` — each action's own label."""
    return _flat_map(_object_body(source, "SCHEDULER_ACTION_LABELS"))


def _labels_by_phase(source: str) -> dict[str, dict[str, str]]:
    """Parse ``SCHEDULER_ACTION_LABEL_BY_PHASE`` — the per-phase labels."""
    return _nested_map(_object_body(source, "SCHEDULER_ACTION_LABEL_BY_PHASE"))


def _resolve_label(
    action: str,
    phase: str | None,
    labels: dict[str, str],
    by_phase: dict[str, dict[str, str]],
) -> str:
    """The label ``schedulerActionLabel(action, phase)`` must return.

    Mirrors the chain the source is required to spell out: the
    phase-specific label, else the action's own label, else the action's
    own name — so an action nobody labelled is rendered as its name and
    never as the word "undefined".
    """
    phase_labels = by_phase.get(phase)
    specific = phase_labels.get(action) if phase_labels else None
    if specific is not None:
        return specific
    default = labels.get(action)
    return default if default is not None else action


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_failed_offers_start_and_rerun() -> None:
    """A failed plan must offer both the re-queue and the re-verify.

    One button cannot be both: 「重新排队执行」 puts the whole plan back
    in the queue, 「重跑验证」 runs the verification round again. An
    operator who wants to re-verify a failed plan needs the second one
    to exist — with only the first, the only way to re-verify is to
    re-run everything, which is the operation they were trying to avoid.
    """
    actions = _actions_by_phase(_source(_APP_JS))

    assert "failed" in actions, (
        "SCHEDULER_ACTIONS_BY_PHASE has no `failed` row, so a failed "
        "plan is offered no action at all."
    )

    failed = actions["failed"]

    assert "start" in failed, (
        "the `failed` row dropped `start`. A failed plan must still be "
        "offered the re-queue, or it can never run again."
    )

    assert _RERUN_ACTION in failed, (
        f"the `failed` row does not offer `{_RERUN_ACTION}`. Re-running "
        "the verification of a failed plan needs its own button: the "
        "only other button re-queues the whole plan."
    )


def test_failed_labels_are_distinct() -> None:
    """The two buttons on a failed plan must read differently.

    Two buttons whose labels are the same are one button the operator
    cannot tell apart — and the difference here is the whole point:
    one re-queues the plan, the other re-runs the verification.

    The labels are checked on the resolved chain (the phase-specific
    label for ``start``, the action's own label for the re-run) *and*
    on the source, so that the table and the resolver cannot disagree:
    a resolver that read the wrong map would show a distinct pair of
    words here and the wrong words on the page.
    """
    source = _source(_APP_JS)
    labels = _labels(source)
    by_phase = _labels_by_phase(source)

    start_label = _resolve_label("start", "failed", labels, by_phase)
    rerun_label = _resolve_label(_RERUN_ACTION, "failed", labels, by_phase)

    assert start_label == _FAILED_START_LABEL, (
        f"the failed plan's `start` button reads {start_label!r}, not "
        f"{_FAILED_START_LABEL!r}. 「启动」 on a failed plan tells the "
        "operator nothing about what pressing it does."
    )

    assert rerun_label == _RERUN_LABEL, (
        f"the re-run-verification button reads {rerun_label!r}, not "
        f"{_RERUN_LABEL!r}."
    )

    assert start_label != rerun_label, (
        "both buttons on a failed plan carry the same text, so the "
        "operator cannot tell the re-queue from the re-verify."
    )

    buttons = _normalized(_function_body(source, "schedulerActionButtons"))
    assert "schedulerActionLabel(action,phase)" in buttons, (
        "schedulerActionButtons() does not read its text through "
        "schedulerActionLabel(), so the phase-specific label never "
        "reaches the button."
    )

    plans = _normalized(_function_body(source, "renderSchedulerTerminalPlans"))
    assert "schedulerActionButtons(planId,actions,phase)" in plans, (
        "renderSchedulerTerminalPlans() does not pass the plan's phase to "
        "schedulerActionButtons(), so every plan row would be labelled "
        "as if it had no phase."
    )


def test_verification_terminal_phases_offer_rerun() -> None:
    """A plan whose verification stopped or failed must offer the re-run.

    These are the two phases that exist precisely because the
    verification round did not pass. They are the plans an operator
    wants to verify again — and with no button for it, the only way to
    try again is to re-run the whole plan.
    """
    actions = _actions_by_phase(_source(_APP_JS))

    for phase in ("verification_loop_stopped", "verification_failed"):
        assert phase in actions, (
            f"SCHEDULER_ACTIONS_BY_PHASE has no `{phase}` row, so a plan "
            "in that phase is offered no action at all."
        )

        assert _RERUN_ACTION in actions[phase], (
            f"the `{phase}` row does not offer `{_RERUN_ACTION}`. That "
            "phase exists because the verification round did not pass, "
            "so it is exactly the phase that needs the re-run button."
        )


def test_start_label_default_unchanged() -> None:
    """``start`` still reads 「启动」 everywhere the failed label does not
    apply — including where no phase is passed at all.

    The failed plan's label is an exception, not a new default: the
    queue row and the running row pass no phase, and their buttons must
    keep the labels they have always had. A resolver that threw on a
    missing phase would take the whole scheduler panel down over one
    row, and a resolver that returned ``undefined`` would print the
    word "undefined" on a button.
    """
    source = _source(_APP_JS)
    labels = _labels(source)
    by_phase = _labels_by_phase(source)

    assert labels.get("start") == "启动", (
        "the `start` action's own label is no longer 「启动」. The failed "
        "plan's 「重新排队执行」 is a phase-specific label; the action's "
        "own label must stay where every other phase uses it."
    )

    for phase in (None, ""):
        assert _resolve_label("start", phase, labels, by_phase) == "启动", (
            f"the `start` label with phase {phase!r} is not 「启动」. A "
            "missing phase must fall back to the action's own label."
        )
        assert _resolve_label(_RERUN_ACTION, phase, labels, by_phase) == _RERUN_LABEL, (
            f"the `{_RERUN_ACTION}` label with phase {phase!r} is not "
            f"{_RERUN_LABEL!r}."
        )

    resolver = _normalized(_function_body(source, "schedulerActionLabel"))

    assert "SCHEDULER_ACTION_LABEL_BY_PHASE[phase]?.[action]" in resolver, (
        "schedulerActionLabel() does not read the phase-specific label "
        "through an optional lookup. A missing phase must not throw."
    )

    assert re.search(
        r"SCHEDULER_ACTION_LABEL_BY_PHASE\[phase\]\?\.\[action\]\s*(?:\?\?|\|\|)\s*"
        r"SCHEDULER_ACTION_LABELS\[action\]",
        resolver,
    ), (
        "schedulerActionLabel() does not fall back from the "
        "phase-specific label to the action's own label. A phase with no "
        "override — or no phase at all — must render the action's own "
        "label."
    )

    assert resolver.index("SCHEDULER_ACTION_LABEL_BY_PHASE") < resolver.index(
        "SCHEDULER_ACTION_LABELS[action]"
    ), (
        "schedulerActionLabel() consults the default label before the "
        "phase-specific one, so the phase-specific label never wins."
    )

    queue = _normalized(_function_body(source, "renderSchedulerQueue"))
    assert 'schedulerActionButtons(planId,["promote","start"])' in queue, (
        "the queue row no longer calls schedulerActionButtons() with two "
        "arguments. It passes no phase, so its start button must keep "
        "the default 「启动」."
    )

    running = _normalized(_function_body(source, "renderSchedulerRunning"))
    assert 'schedulerActionButtons(planId,["pause"])' in running, (
        "the running row no longer calls schedulerActionButtons() with "
        "two arguments. It passes no phase, so its button must keep the "
        "default label."
    )
