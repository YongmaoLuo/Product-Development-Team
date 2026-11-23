"""「恢复」 and 「启动」 must each say what they do — in the label, in
the hint and in the result box.

Why this gate exists
--------------------
The panel offers two buttons that look alike and do not do the same
thing.  「恢复」 puts the plan back at the head of the line and waits
for a free slot — it never takes a slot away from anybody.  「启动」
puts the plan at the head of the line *and* takes a slot away from
somebody when every slot is taken.  When a slot is free the two
produce the same picture, so the operator cannot tell them apart by
looking at the result — and a button labelled 「启动」 on a panel that
silently interrupts a long-running plan is a promise the panel does not
keep.

So the two buttons have to carry different words, and the words have to
say which of the two things the button does.  This gate pins:

* the two labels are different, and the start label says it may
  preempt,
* every action carries a one-line hint for the button's tooltip, and
  the resume hint says it does *not* preempt while the start hint says
  it does,
* the button actually draws that hint, and
* the result box speaks in sentences instead of server tokens.

What it does not pin
--------------------
The scheduler's behaviour.  Nothing here says a start may preempt or a
resume may not — that is the server's rule, and it is unchanged.  This
gate only says the panel does not hide it.

Scan target
-----------
``frontend/app.js``, read as source.  This is a shape gate, like the
rest of ``static_gates/``: it runs on a plain Python environment with no
JS toolchain, so it pins the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError``.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _normalized(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed away."""
    return re.sub(r"\s+", "", text)


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    The body ends at the first line that is exactly ``}`` — the file's
    top-level functions close at column zero, so that line ends the
    function no matter what the body contains.

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


def _flat_map(body: str) -> dict[str, str]:
    """Parse a flat ``key: "value"`` object-literal body into a dict."""
    result: dict[str, str] = {}
    for match in _FLAT_ENTRY.finditer(body):
        key = match.group(1) if match.group(1) is not None else match.group(2)
        result[key] = match.group(3)
    return result


def _labels(source: str) -> dict[str, str]:
    """Parse ``SCHEDULER_ACTION_LABELS`` — each action's own label."""
    return _flat_map(_object_body(source, "SCHEDULER_ACTION_LABELS"))


def _hints(source: str) -> dict[str, str]:
    """Parse ``SCHEDULER_ACTION_HINTS`` — each action's one-line hint."""
    return _flat_map(_object_body(source, "SCHEDULER_ACTION_HINTS"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_resume_and_start_labels_are_different() -> None:
    """The two buttons must not carry the same words.

    Two buttons whose labels are the same are one button the operator
    cannot tell apart.  The difference here is the whole point: a
    resume waits for a free slot and takes nothing from anybody, a
    start may take a slot away from a running plan.
    """
    labels = _labels(_source(_APP_JS))

    assert labels.get("resume") == "恢复排队", (
        f"the `resume` action's label reads {labels.get('resume')!r}, not "
        "「恢复排队」. A resume waits for a slot; the label has to say it "
        "queues."
    )

    assert labels.get("start") == "立即启动（可抢占）", (
        f"the `start` action's label reads {labels.get('start')!r}, not "
        "「立即启动（可抢占）」. A start may take a slot away from a "
        "running plan; the label has to say so."
    )

    assert labels["resume"] != labels["start"], (
        "the resume and the start carry the same label, so the operator "
        "cannot tell the button that waits from the button that "
        "preempts."
    )


def test_hints_say_whether_an_action_preempts() -> None:
    """Every action's hint has to say what it does to other people's
    slots.

    The label is what the operator reads before pressing; the hint is
    what they read while they are about to press.  A hint that does not
    mention preemption leaves the one thing that matters unsaid, so
    the resume hint must say it does *not* preempt, the start hint must
    say that it does, and the pause hint must say that it frees a slot.
    """
    hints = _hints(_source(_APP_JS))

    assert "抢占" in hints.get("start", ""), (
        f"the `start` hint reads {hints.get('start')!r} and does not say "
        "that a start may preempt a running plan."
    )

    assert "不抢占" in hints.get("resume", ""), (
        f"the `resume` hint reads {hints.get('resume')!r} and does not say "
        "that a resume never preempts a running plan."
    )

    assert "释放" in hints.get("pause", ""), (
        f"the `pause` hint reads {hints.get('pause')!r} and does not say "
        "that pausing frees the slot the plan was holding."
    )

    for action in ("promote", "resume", "start", "pause", "rerun-verification"):
        assert hints.get(action), (
            f"the `{action}` action has no hint, so its button's tooltip "
            "says nothing about what pressing it does."
        )


def test_button_draws_the_hint() -> None:
    """The hint has to reach the button, not just the source.

    A hint table nobody reads is a comment.  The button must carry the
    hint as its ``title``, and the title must come from the hint of
    the action that button acts on.
    """
    source = _source(_APP_JS)
    buttons = _normalized(_function_body(source, "schedulerActionButtons"))

    assert "title=" in buttons, (
        "schedulerActionButtons() does not draw a `title` attribute, so "
        "the button has no tooltip at all."
    )

    assert "schedulerActionHint(action)" in buttons, (
        "schedulerActionButtons() does not read its tooltip through "
        "schedulerActionHint(), so the button shows no hint — or one "
        "that belongs to some other action."
    )

    resolver = _normalized(_function_body(source, "schedulerActionHint"))
    assert "SCHEDULER_ACTION_HINTS[action]" in resolver, (
        "schedulerActionHint() does not read SCHEDULER_ACTION_HINTS, so "
        "it cannot return the hint of the action it was asked about."
    )


def test_result_box_speaks_in_sentences() -> None:
    """The result box must say what happened in words, not in tokens.

    The server answers in tokens (``applied``, ``blocked_by_capacity``).
    A panel that shows the token shows the operator a word that means
    nothing to them, and — worse — shows the *same* word for a
    promotion, a pause and a resume.  So the outcome line has to go
    through the sentence table, and the sentences have to distinguish
    the cases the operator cannot otherwise tell apart.
    """
    source = _source(_APP_JS)
    outcome = _normalized(_function_body(source, "renderSchedulerOutcome"))

    assert "schedulerOutcomeText(result)" in outcome, (
        "renderSchedulerOutcome() does not write schedulerOutcomeText(result) "
        "into the outcome line, so the panel shows the server's token."
    )

    assert "schedulerDetailText(result)" in outcome, (
        "renderSchedulerOutcome() does not write schedulerDetailText(result) "
        "into the detail line, so the panel shows the server's token."
    )

    for sentence in (
        "已恢复排队",
        "已启动（有空位）",
        "已启动（抢占了名额）",
        "名额已满，排队等待",
    ):
        assert sentence in source, (
            f"the panel never writes {sentence!r} anywhere. The operator "
            "has to be able to read what the command did."
        )


def test_resume_detail_says_it_preempted_nobody() -> None:
    """The resume's own sentence must say that nobody was interrupted.

    A resume that takes a slot away from a running plan is a different
    command, and the operator has to be able to see that it was not
    that.  So the sentence the panel writes for a resume must say that
    no running plan was preempted.
    """
    source = _source(_APP_JS)
    detail = _normalized(_function_body(source, "schedulerDetailText"))

    assert "未抢占" in detail, (
        "schedulerDetailText() never says that a resume preempted nobody. "
        "The sentence it writes for a resume must say so."
    )

    assert "resumed" in detail, (
        "schedulerDetailText() does not read the `resumed` detail at all, "
        "so the resume's own sentence is never written."
    )
