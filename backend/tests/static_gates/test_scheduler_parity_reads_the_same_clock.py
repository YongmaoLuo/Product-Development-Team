"""The panel's parity assertion must compare the panel's own clock.

Why this gate exists
--------------------
A running plan is measured twice, and the two numbers answer different
questions:

* ``elapsed_sec`` is the **slot** clock — the time since the slot was
  acquired.  It is measured against the tick, so it restarts at zero
  whenever the scheduler restarts, and it is the number the eviction
  rule is applied to.
* ``running_sec`` is the **plan** clock — the time since the plan
  itself started.  It is what an operator means by 「已运行」, and it is
  what the panel draws (``frontend/app.js``:
  ``item.running_sec ?? item.elapsed_sec``).

The panel parity case compares what the panel drew with what the read
model carries.  It used to compare the panel's **plan** clock with the
read model's **slot** clock — two different facts, one floor apart —
and to allow a fixed tolerance of two seconds for the difference
between two readings of a clock that were never simultaneous.

That combination is what let the failure hide.  On a quiet machine the
two clocks happened to be one second apart and the sampling happened
to be one second apart, so the difference was exactly two and the
assertion sat on its own boundary; on a busy host the sampling grew
and the case went red for a reason that had nothing to do with the
code.  A case that only fails when the machine is busy cannot be
trusted to be green when it is quiet, and it cannot be trusted to be
red when it is not.

The rule
--------
1. The comparison reads the clock the panel draws: the **plan** clock
   (``running_sec``), falling back to the slot clock when the plan
   clock is missing.  The fallback is nullish (``??``), not falsy: a
   plan that started a moment ago has a plan clock of ``0``, and
   ``||`` would throw that number away.
2. The tolerance is derived from the **measured** sampling window — the
   time between the two samples of the read model that bracket the
   panel's own reading — and not from a constant that only holds on a
   quiet machine.
3. The driver takes those two samples.  Without them there is no
   window to measure, and the tolerance would be a constant again.

The sampling order
------------------
A window is only a window if the two samples really do bracket the
panel's own reading.  Three things have to hold in the driver's source,
and all three are about *where* things are, not about how many of them
there are:

1. **The order.**  The first sample comes before the panel reads the
   scheduler view, and the second sample comes after it.  Samples
   taken on one side of the reading measure a window that says nothing
   about the drawing.
2. **The timestamps.**  The window is measured between ``api_before_at``
   and ``api_after_at`` — the first taken before the first sample, the
   second taken after the panel's reading and before the second sample.
   A window measured across the wrong stretch of the driver is a number
   that answers a question nobody asked.
3. **The screenshot.**  The full-page screenshot of the scheduler view
   is slow.  Taken between the two samples it stretches the window
   without adding anything to the reading the window exists for — and
   the tolerance is derived from the window, so it grows with it.

Counting the samples cannot tell any of that apart: a driver that
samples the read model twice, both times on the same side of the
panel's reading, looks exactly like one that brackets it.  So the rule
is written as a single function — ``_sampling_order_problem()`` — that
a driver source is passed through, and that the mutations below are
passed through as well.

Scan target
-----------
``backend/tests/e2e/plan_scheduler/test_scheduler_operator_e2e.py``,
read as source.  This is a shape gate, like the rest of
``static_gates/``: it pins the contract in the source rather than
running the browser, because the browser is exactly what a busy host
makes untrustworthy.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Callable


_REPO_ROOT = Path(__file__).resolve().parents[3]
_PARITY_TEST = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "e2e"
    / "plan_scheduler"
    / "test_scheduler_operator_e2e.py"
)

#: The helper that resolves the clock the panel draws for a running row.
_PANEL_CLOCK = "_panel_clock"

#: The name the browser driver holds its source under.
_DRIVER_SOURCE = "_PANEL_DRIVER_SOURCE"

#: The key the driver reports its sampling window under.
_WINDOW_KEY = "api_window_sec"

#: The read model's own endpoint — what the driver samples, twice.
_STATE_URL = '"/api/scheduler/state"'

#: The panel's own reading of the scheduler view — the DOM read that the
#: two samples of the read model have to bracket.  The anchor is the one
#: the driver really uses, not a marker invented here.
_PANEL_READ_RE = re.compile(
    r'read_capacity\(\s*page\s*,\s*"#scheduler-view-capacity"\s*\)'
)

#: The full-page screenshot of the scheduler view.
_SCREENSHOT_RE = re.compile(
    r'page\.screenshot\([^\n]*screens\s*/\s*"scheduler\.png"[^\n]*full_page=True'
)

#: The screenshot line as the driver writes it — the anchor the mutation
#: below moves around.
_SCREENSHOT_LINE = (
    'page.screenshot(path=str(screens / "scheduler.png"), full_page=True)'
)

#: The two timestamps that measure the sampling window.
_BEFORE_STAMP = "api_before_at = time.time()"
_AFTER_STAMP = "api_after_at = time.time()"

#: The second sample's request — the anchor that tells the two samples
#: apart.
_SECOND_SAMPLE = "api_state_after = page.request.get("


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


def _module(path: Path) -> ast.Module:
    """The parsed module at ``path``."""
    return ast.parse(_source(path))


def _function_body(path: Path, name: str) -> str:
    """Return the source of the top-level function ``name``.

    Raises :class:`AssertionError` when the function is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.
    """
    source = _source(path)
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"{name}() has no source segment"
            return segment
    raise AssertionError(f"function {name}() not found in {path}")


def _module_constant(path: Path, name: str) -> str:
    """Return the value of the top-level constant ``name``.

    Raises :class:`AssertionError` when the constant is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.
    """
    for node in _module(path).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = ast.literal_eval(node.value)
                assert isinstance(value, str), f"{name} is not a string"
                return value
    raise AssertionError(f"constant {name} not found in {path}")


def _constant_in(text: str, name: str) -> str:
    """Return the value of the top-level constant ``name`` in ``text``.

    The text is parsed rather than the file read, so a *mutated* copy
    of the module can be measured with the same rule the real module
    is measured with.
    """
    for node in ast.parse(text).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = ast.literal_eval(node.value)
                assert isinstance(value, str), f"{name} is not a string"
                return value
    raise AssertionError(f"constant {name} not found")


def _line_index(
    lines: list[str],
    matches: Callable[[str], bool],
    start: int = 0,
) -> int:
    """The index of the first line at or after ``start`` that ``matches``.

    Raises :class:`AssertionError` when no line matches, so a driver
    that no longer has the shape this gate is about fails loudly
    instead of matching nothing and passing.
    """
    for index in range(start, len(lines)):
        if matches(lines[index]):
            return index
    raise AssertionError("the driver has no line of the expected shape")


def _is_stripped(line: str, text: str) -> bool:
    """Whether ``line`` is exactly ``text`` once its indent is removed."""
    return line.strip() == text


def _starts_with_stripped(line: str, text: str) -> bool:
    """Whether ``line`` starts with ``text`` once its indent is removed."""
    return line.strip().startswith(text)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _sampling_order_problem(driver: str) -> str:
    """Return what is wrong with the driver's sampling order.

    ``""`` means the driver's two samples really do bracket the panel's
    own reading, and that the window the parity assertion derives its
    tolerance from is the window around that reading.

    Anything else is a sentence saying what is wrong and in which
    direction.  The sentences open with the kind of the defect —
    ``order:``, ``timestamp:``, ``screenshot:`` or ``shape:`` — so a
    caller can say which rule was broken rather than only that
    something was.

    The function is pure: it reads the driver source it is given and
    nothing else, so a mutated driver can be measured with the very
    same rule the real driver is measured with.
    """
    samples = [
        match.start() for match in re.finditer(re.escape(_STATE_URL), driver)
    ]
    if len(samples) < 2:
        return (
            f"shape: the driver samples the read model {len(samples)} time(s); "
            "it has to sample it twice — once before the panel's own reading "
            "and once after — so the window between the two readings can be "
            "measured."
        )
    first, second = samples[0], samples[1]

    panel = _PANEL_READ_RE.search(driver)
    if panel is None:
        return (
            "shape: the driver never reads the panel's own capacity widget "
            '(`read_capacity(page, "#scheduler-view-capacity")`), so there is '
            "no reading for the two samples to bracket."
        )
    panel_at = panel.start()

    shot = _SCREENSHOT_RE.search(driver)
    if shot is None:
        return (
            "shape: the driver never takes the full-page screenshot of the "
            'scheduler view (`screens / "scheduler.png"`), so there is '
            "nothing to place relative to the two samples."
        )
    shot_at = shot.start()

    before_at = driver.find(_BEFORE_STAMP)
    after_at = driver.find(_AFTER_STAMP)
    if before_at < 0 or after_at < 0:
        return (
            "shape: the driver does not stamp both ends of the sampling "
            f"window (`{_BEFORE_STAMP}` and `{_AFTER_STAMP}`), so the window "
            "it reports is not a measurement of anything."
        )

    # 1. The order: the panel's reading has to be *between* the samples.
    if panel_at < first:
        return (
            "order: the panel's own reading happens before the first sample "
            "of the read model, so the two samples do not bracket it — the "
            "window they measure says nothing about the panel's drawing."
        )
    if panel_at > second:
        return (
            "order: the panel's own reading happens after the second sample "
            "of the read model, so the two samples do not bracket it — the "
            "window they measure says nothing about the panel's drawing."
        )

    # 2. The timestamps: the window has to be the stretch around the read.
    if before_at > first:
        return (
            f"timestamp: `{_BEFORE_STAMP}` is taken after the first sample "
            "of the read model, so the window does not start where the "
            "sampling starts."
        )
    if after_at < panel_at:
        return (
            f"timestamp: `{_AFTER_STAMP}` is taken before the panel's own "
            "reading, so the window it measures is not the one around that "
            "reading."
        )
    if after_at > second:
        return (
            f"timestamp: `{_AFTER_STAMP}` is taken after the second sample "
            "of the read model, so the window it measures is not the one "
            "between the two samples."
        )

    # 3. The screenshot: it must not sit between the two samples.
    if first < shot_at < second:
        return (
            "screenshot: the full-page screenshot of the scheduler view sits "
            "between the two samples of the read model. A screenshot is "
            "slow, so it stretches the measured window without adding "
            "anything to the reading the window is there for — and the "
            "tolerance is derived from that window."
        )

    return ""


# ---------------------------------------------------------------------------
# The mutations — the driver, with its sampling order broken
# ---------------------------------------------------------------------------


def _move_screenshot_between_the_samples(text: str) -> str:
    """Move the full-page screenshot back between the two samples.

    This is the shape the driver used to have: the screenshot was taken
    between the two samples of the read model, which stretched the
    measured window — and the tolerance derived from it — without the
    reading the window exists for.
    """
    lines = text.splitlines(keepends=True)
    index = _line_index(lines, lambda line: _is_stripped(line, _SCREENSHOT_LINE))
    moved = lines.pop(index)
    first = _line_index(lines, lambda line: _is_stripped(line, _BEFORE_STAMP))
    end = _line_index(
        lines,
        lambda line: _starts_with_stripped(line, ").json()"),
        start=first,
    )
    lines.insert(end + 1, moved)
    return "".join(lines)


def _move_samples_after_the_panel_read(text: str) -> str:
    """Move the first sample of the read model after the second one.

    The result is the shape that *loosens* the comparison: both samples
    end up after the panel's own reading, so the measured window is one
    that does not contain the drawing at all.
    """
    lines = text.splitlines(keepends=True)
    first = _line_index(lines, lambda line: _is_stripped(line, _BEFORE_STAMP))
    end = _line_index(
        lines,
        lambda line: _starts_with_stripped(line, ").json()"),
        start=first,
    )
    block = lines[first : end + 1]
    rest = lines[:first] + lines[end + 1 :]
    second = _line_index(
        rest, lambda line: _starts_with_stripped(line, _SECOND_SAMPLE)
    )
    second_end = _line_index(
        rest,
        lambda line: _starts_with_stripped(line, ").json()"),
        start=second,
    )
    return "".join(rest[: second_end + 1] + block + rest[second_end + 1 :])


def _move_samples_before_the_panel_read(text: str) -> str:
    """Move both samples of the read model before the panel's own reading.

    The result is the shape that *loosens* the comparison the other way
    round: both samples end up before the panel's own reading, so the
    measured window is one that does not contain the drawing at all.
    """
    lines = text.splitlines(keepends=True)
    first = _line_index(lines, lambda line: _is_stripped(line, _BEFORE_STAMP))
    end = _line_index(
        lines,
        lambda line: _starts_with_stripped(line, ").json()"),
        start=first,
    )
    second = _line_index(lines, lambda line: _is_stripped(line, _AFTER_STAMP))
    second_end = _line_index(
        lines,
        lambda line: _starts_with_stripped(line, ").json()"),
        start=second,
    )
    block = lines[first : end + 1] + lines[second : second_end + 1]
    rest = lines[:first] + lines[end + 1 : second] + lines[second_end + 1 :]
    panel = _line_index(rest, lambda line: _PANEL_READ_RE.search(line) is not None)
    return "".join(rest[:panel] + block + rest[panel:])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_the_panel_clock_is_read_from_the_plan_clock() -> None:
    """The comparison must read the clock the panel actually draws.

    The panel draws the plan clock and falls back to the slot clock
    when the plan clock is missing, so the comparison has to read the
    same thing.  Reading the slot clock instead compares two
    different facts — and the difference between them is a floor, not
    an error, which is exactly why a wrong comparison can sit on a
    boundary for rounds and nobody notices.

    The fallback must be nullish and not falsy: a plan that started a
    moment ago has a plan clock of ``0``, and ``or`` would throw that
    number away and read the slot clock instead.
    """
    body = _function_body(_PARITY_TEST, _PANEL_CLOCK)

    assert "running_sec" in body, (
        f"{_PANEL_CLOCK}() never reads `running_sec`, so the comparison "
        "is not reading the clock the panel draws — the panel draws the "
        "plan clock, and the read model's slot clock is a different "
        "fact."
    )

    assert "elapsed_sec" in body, (
        f"{_PANEL_CLOCK}() never reads `elapsed_sec`, so a listing that "
        "carries no plan clock has no fallback and the comparison has "
        "nothing to read."
    )

    assert "is None" in body, (
        f"{_PANEL_CLOCK}() does not test the plan clock for `None`. The "
        "fallback has to be nullish: a plan that started a moment ago "
        "has a plan clock of `0`, and a falsy test would throw that "
        "number away."
    )

    assert re.search(r"running_sec[^\n]*\bor\b", body) is None, (
        f"{_PANEL_CLOCK}() falls back to the slot clock with a falsy "
        "test. A plan clock of `0` is a real answer, and `or` would "
        "discard it in favour of the slot clock."
    )


def test_the_drawn_duration_is_not_compared_with_the_slot_clock() -> None:
    """The drawn duration must not be compared with ``elapsed_sec``.

    The panel's drawing is the plan clock, so comparing it with the
    read model's slot clock compares two different facts.  The case
    then has to allow for the difference between them, which is what
    turns a small systematic offset into a tolerance big enough to
    hide a real difference — and big enough to go red on a busy host.
    """
    source = _source(_PARITY_TEST)

    assert re.search(r"drawn_elapsed[^\n]*elapsed_sec", source) is None, (
        "the panel's drawn duration is compared with `elapsed_sec` on "
        "the same line. The panel draws the plan clock "
        "(`running_sec ?? elapsed_sec`), so the comparison has to read "
        "the same clock."
    )

    assert re.search(r"elapsed_sec[^\n]*drawn_elapsed", source) is None, (
        "the panel's drawn duration is compared with `elapsed_sec` on "
        "the same line. The panel draws the plan clock "
        "(`running_sec ?? elapsed_sec`), so the comparison has to read "
        "the same clock."
    )


def test_the_tolerance_comes_from_the_measured_window() -> None:
    """The tolerance must be derived from the window, not from a constant.

    A constant tolerance only holds on a host that measures time the
    way the constant was written down.  The window between the two
    samples of the read model is measured on the host that runs the
    case, so it is the only number that can say how far apart the two
    readings really were.
    """
    source = _source(_PARITY_TEST)

    match = re.search(
        r"^[ \t]*tolerance[ \t]*=[ \t]*(?P<expression>[^\n#]+)",
        source,
        re.MULTILINE,
    )
    assert match is not None, (
        "the parity assertion has no `tolerance` at all: the panel's "
        "drawing and the read model's seconds are compared with a "
        "number that is written down somewhere else."
    )

    expression = match.group("expression")
    assert "window" in expression, (
        f"the tolerance is `{expression.strip()}` — it is not derived "
        "from the measured sampling window, so it only holds on a host "
        "that measures time the way it was written down."
    )


def test_the_read_model_is_sampled_around_the_panels_own_reading() -> None:
    """The driver must take the two samples the window is measured from.

    The window is the time between the two readings of the read model
    that bracket the panel's own reading.  With one reading there is
    no window, and the tolerance falls back to being a constant — the
    thing this gate exists to prevent.
    """
    driver = _module_constant(_PARITY_TEST, _DRIVER_SOURCE)

    assert driver.count(_STATE_URL) >= 2, (
        f"the browser driver samples the read model "
        f"{driver.count(_STATE_URL)} time(s). It has to sample it "
        "twice — once before the panel's own reading and once after — "
        "so the window between the two readings can be measured."
    )

    assert _WINDOW_KEY in driver, (
        f"the browser driver never reports `{_WINDOW_KEY}`. The window "
        "between the two samples is what the comparison's tolerance is "
        "derived from, so the driver has to measure it and report it."
    )

    problem = _sampling_order_problem(driver)
    assert problem == "", (
        "the browser driver does not sample the read model around the "
        f"panel's own reading: {problem}"
    )


def test_the_unmutated_driver_is_accepted_by_the_sampling_rule() -> None:
    """The rule must accept the driver as it stands.

    A rule that rejects everything would pass every mutation below and
    pin nothing, so the real driver is measured with it as well — the
    positive control that says the rule is a rule and not a complaint.
    """
    driver = _module_constant(_PARITY_TEST, _DRIVER_SOURCE)

    problem = _sampling_order_problem(driver)

    assert problem == "", (
        f"the sampling rule rejects the driver as it stands: {problem}"
    )


def test_a_screenshot_between_the_two_samples_is_rejected() -> None:
    """The screenshot must not sit between the two samples.

    This is the shape the driver used to have: the full-page
    screenshot was taken between the two samples of the read model.
    The screenshot is slow, so the window it measures was stretched by
    the screenshot rather than by the panel's reading — and the
    tolerance is derived from that window, so the comparison was given
    a slack that had nothing to do with what it was comparing.
    """
    mutated = _move_screenshot_between_the_samples(_source(_PARITY_TEST))
    original_driver = _module_constant(_PARITY_TEST, _DRIVER_SOURCE)
    driver = _constant_in(mutated, _DRIVER_SOURCE)

    assert driver != original_driver or _sampling_order_problem(
        original_driver
    ), (
        "the mutation moved nothing and the driver does not already have the "
        "shape the mutation creates — so the mutation is not testing what it "
        "claims to test."
    )

    problem = _sampling_order_problem(driver)

    assert problem != "", (
        "the full-page screenshot sits between the two samples of the "
        "read model and the sampling rule still accepts it — so the "
        "rule does not pin where the screenshot is taken."
    )
    assert problem.startswith("screenshot:"), (
        f"the rule reports {problem!r} — the position of the screenshot is "
        "not what it complains about."
    )


def test_samples_taken_after_the_panels_own_reading_are_rejected() -> None:
    """Both samples on one side of the panel's reading are not a window.

    This is the shape that *loosens* the comparison: the two samples
    end up after the panel's own reading, so the window they measure
    is one that does not contain the drawing at all.  The reading and
    the samples are then two facts about two different moments, and
    the tolerance derived from the window says nothing about the
    difference between them.
    """
    mutated = _move_samples_after_the_panel_read(_source(_PARITY_TEST))
    original_driver = _module_constant(_PARITY_TEST, _DRIVER_SOURCE)
    driver = _constant_in(mutated, _DRIVER_SOURCE)

    assert driver != original_driver or _sampling_order_problem(
        original_driver
    ), (
        "the mutation moved nothing and the driver does not already have the "
        "shape the mutation creates — so the mutation is not testing what it "
        "claims to test."
    )

    problem = _sampling_order_problem(driver)

    assert problem != "", (
        "both samples of the read model sit after the panel's own reading "
        "and the sampling rule still accepts it — so the rule does not "
        "pin that the reading is bracketed."
    )
    assert problem.startswith("order:"), (
        f"the rule reports {problem!r} — the panel's reading not being "
        "bracketed is not what it complains about."
    )


def test_samples_taken_before_the_panels_own_reading_are_rejected() -> None:
    """Both samples before the panel's reading are not a window either.

    The other direction of the same defect: the two samples of the
    read model are taken before the panel reads the scheduler view, so
    the measured window is again one that does not contain the
    drawing.  The parity case would then compare the panel's drawing
    with readings taken before it, and the tolerance would be derived
    from a window that has nothing to do with the drawing.
    """
    mutated = _move_samples_before_the_panel_read(_source(_PARITY_TEST))
    original_driver = _module_constant(_PARITY_TEST, _DRIVER_SOURCE)
    driver = _constant_in(mutated, _DRIVER_SOURCE)

    assert driver != original_driver or _sampling_order_problem(
        original_driver
    ), (
        "the mutation moved nothing and the driver does not already have the "
        "shape the mutation creates — so the mutation is not testing what it "
        "claims to test."
    )

    problem = _sampling_order_problem(driver)

    assert problem != "", (
        "both samples of the read model sit before the panel's own reading "
        "and the sampling rule still accepts it — so the rule does not "
        "pin that the reading is bracketed."
    )
    assert problem.startswith("order:"), (
        f"the rule reports {problem!r} — the panel's reading not being "
        "bracketed is not what it complains about."
    )
