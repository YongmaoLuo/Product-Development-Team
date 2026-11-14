"""The panel's reading and the read model's reading must be one moment apart.

Why this gate exists
--------------------
The operator's case used to read the panel, take a screenshot, and
**then** read the read model — with the screenshot in between.  The
screenshot is a round trip to the browser and back, and the panel
redraws itself every ``SCHEDULER_POLL_MS`` milliseconds, so the two
readings were two readings of two different moments: the panel had
drawn a model that was up to one poll period old, and the case
compared it with a model it had just fetched.  The case's own
docstring claimed the two were read "at the same moment"; the code
said otherwise.

That claim was not decoration.  The case's tolerance is computed from
it: how far the panel's clock and the read model's clock may differ
is one poll period, plus the gap between the two readings, plus a
second for the rounding.  If the gap between the readings is not
measured — or is measured across a screenshot — the tolerance is a
number nobody can defend.

So the order of the two readings is pinned here, where a plain Python
environment can see it, and the read model the **panel** was fed is
required to be recorded: the rows the panel drew have to be comparable
with the model they were drawn from, not with a model read at some
other moment.

The rules
---------
* The read model is read **before** the screenshot.  The screenshot is
  evidence and not an input: nothing that costs time may come between
  the two readings.
* The driver captures the body the server answered the panel's own
  poll with — the read model the panel was fed.
* The panel's poll period is one number, shared by the case and the
  page: ``PANEL_POLL_SECONDS * 1000`` is the page's
  ``SCHEDULER_POLL_MS``.

Scan target
-----------
``backend/tests/e2e/plan_scheduler/test_scheduler_operator_e2e.py`` and
``frontend/app.js``, read as source.  These are shape gates like the
rest of ``static_gates/``: they run on a plain Python environment with
no browser and no JS toolchain, so they pin the contract in the source
rather than executing the page.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Tuple


_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The operator's end-to-end case: the one that drives the panel and
#: compares it with the read model.
_OPERATOR_E2E = (
    _REPO_ROOT
    / "backend"
    / "tests"
    / "e2e"
    / "plan_scheduler"
    / "test_scheduler_operator_e2e.py"
)

#: The page: the panel's own poll period lives here.
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The read model's own endpoint.  The panel polls it and the case
#: reads it, so it is the one URL both sides answer on.
_STATE_PATH = "/api/scheduler/state"

#: A handler registered on the page's responses.
_RESPONSE_HANDLER = re.compile(
    r'page\.on\(\s*"response"\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)'
)

#: The page's poll period, as a named constant in the page's source.
_POLL_CONSTANT = re.compile(
    r"const\s+SCHEDULER_POLL_MS\s*=\s*(\d+)\s*;"
)


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


def _module_ast(path: Path) -> ast.Module:
    """The parsed module of ``path``."""
    return ast.parse(_source(path), filename=str(path))


def _driver_source() -> str:
    """The browser driver's own source, read out of the case's module.

    The driver is a string inside the case's module, so a source scan
    is the only way to see what it does.  It is read as a module of its
    own afterwards, so the functions inside it can be looked at one by
    one.
    """
    for node in _module_ast(_OPERATOR_E2E).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_PANEL_DRIVER_SOURCE":
                return str(ast.literal_eval(node.value))
    raise AssertionError(
        "the operator's end-to-end case has no _PANEL_DRIVER_SOURCE. The "
        "driver is the panel, as an operator uses it; a case without one "
        "cannot compare the panel with anything."
    )


def _functions(source: str) -> Dict[str, str]:
    """Every function of *source*, by name, with its own text.

    The walk is over the whole tree and not only over the top level:
    the driver's handlers are defined inside the ``with`` block that
    drives the browser, not beside it.
    """
    bodies: Dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                bodies[node.name] = segment
    return bodies


def _constants(source: str) -> Dict[str, str]:
    """Every module-level assignment of *source*, by name, with its text."""
    constants: Dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        segment = ast.get_source_segment(source, node)
        if segment is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = segment
    return constants


def _response_handlers(driver: str, functions: Dict[str, str]) -> List[Tuple[str, str]]:
    """Every function the driver registered on the page's responses.

    A response that nobody listens to is a response nobody reads: the
    panel's own poll is answered by the server, and the answer is
    gone unless something is there to catch it.
    """
    handlers: List[Tuple[str, str]] = []
    for name in _RESPONSE_HANDLER.findall(driver):
        body = functions.get(name)
        if body is not None:
            handlers.append((name, body))
    return handlers


def _names_the_state(body: str, known: Dict[str, str]) -> bool:
    """Whether *body* singles out the read model's endpoint.

    The recognition may live one call — or one name — away: a body may
    ask a function that asks a constant that carries the endpoint.
    Where the recognition lives is not the contract, so the whole chain
    is followed.
    """
    seen = set()
    pending = [body]
    while pending:
        text = pending.pop()
        if _STATE_PATH in text:
            return True
        for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", text):
            if name in seen:
                continue
            seen.add(name)
            definition = known.get(name)
            if definition is not None:
                pending.append(definition)
    return False


def _module_constant(name: str) -> float:
    """The value of the module-level constant *name* of the case."""
    for node in _module_ast(_OPERATOR_E2E).body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            value = node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == name:
            assert value is not None, f"{name} has no value"
            return float(ast.literal_eval(value))
    raise AssertionError(
        f"the operator's end-to-end case has no {name}. The panel's poll "
        "period is a property of the panel; a case that does not name it "
        "cannot say how old the panel's drawing may be."
    )


def _page_poll_millis() -> int:
    """The panel's own poll period, in milliseconds, as the page says it."""
    found = _POLL_CONSTANT.search(_source(_APP_JS))
    assert found is not None, (
        "frontend/app.js has no `const SCHEDULER_POLL_MS = …;`. The "
        "panel's poll period must be a named constant: it is a property "
        "of the panel, and a literal inside the setInterval call cannot "
        "be found or compared with the case's own number."
    )
    return int(found.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_api_read_precedes_screenshot() -> None:
    """The read model must be read before the screenshot is taken.

    A screenshot costs a round trip to the browser and back, and it is
    evidence, not an input: it proves what the panel looked like, and
    it feeds nothing into the comparison.  Put between the two
    readings, it does one thing only — it moves the clock.  The panel
    has redrawn itself by then, and the two readings are two readings
    of two different moments, which is exactly the thing the case
    claims they are not.
    """
    driver = _driver_source()

    read_at = driver.find("page.request.get(")
    assert read_at != -1, (
        "the driver never reads the read model through the browser's own "
        "session (page.request.get). The panel and the read model would "
        "then be two different servers' answers."
    )

    shot_at = driver.find('screens / "scheduler.png"')
    assert shot_at != -1, (
        "the driver takes no screenshot of the scheduler view. The "
        "screenshot is the evidence the case keeps; without it the "
        "failure has nothing to show."
    )

    assert read_at < shot_at, (
        "the driver takes the screenshot before it reads the read model. "
        "The screenshot is evidence and not an input — it costs a round "
        "trip to the browser, and nothing that costs time may come "
        "between the two readings, or the clock moves between them."
    )


def test_served_state_is_captured() -> None:
    """The read model the panel was fed must be captured.

    The panel draws the model it read at its last poll, so the rows on
    the screen belong to *that* model and to no other.  A case that
    only ever reads a fresh model from the API compares the panel with
    a moment it was not showing: the two answers are both correct, and
    the comparison between them is meaningless.

    So the driver has to catch the body the server answered the
    panel's own poll with, and keep it.
    """
    driver = _driver_source()
    functions = _functions(driver)
    constants = _constants(driver)
    known = dict(constants)
    known.update(functions)

    assert _STATE_PATH in driver, (
        f"the driver never names {_STATE_PATH!r}. That is the endpoint "
        "the panel polls and the case reads: without it the driver "
        "cannot tell the panel's own answer from any other answer."
    )

    handlers = _response_handlers(driver, functions)
    assert handlers, (
        "the driver registers no handler on the page's responses. The "
        "panel's poll is answered by the server, and the answer is gone "
        "unless something is there to catch it."
    )

    capturing = [
        (name, body) for name, body in handlers if ".json()" in body
    ]
    assert capturing, (
        "no handler on the page's responses reads a response body. The "
        "read model the panel was fed is the body the server answered "
        "the panel's own poll with, and that body is gone unless "
        "something reads it."
    )

    matched = [
        name
        for name, body in capturing
        if _names_the_state(body, known)
    ]
    assert matched, (
        "the handler that reads a response body does not single out the "
        f"read model's endpoint ({_STATE_PATH!r}). A body captured "
        "without saying which response it belongs to is not the panel's "
        "read model."
    )


def test_poll_constant_matches_panel() -> None:
    """The case's poll period must be the panel's own.

    The tolerance that lets the panel's clock and the read model's
    clock differ is built from the panel's poll period: the panel
    draws the model it read at its last poll, so a reading of the panel
    may be one period old.  A case that carries its own number is
    carrying a second answer to the question "how old may the panel's
    drawing be" — and a second answer here is a tolerance nobody can
    defend.
    """
    seconds = _module_constant("PANEL_POLL_SECONDS")
    millis = _page_poll_millis()

    assert int(seconds * 1000) == millis, (
        f"the case's PANEL_POLL_SECONDS is {seconds!r} and the page's "
        f"SCHEDULER_POLL_MS is {millis}: the case says the panel polls "
        f"every {seconds * 1000:.0f} ms and the page says every {millis} "
        "ms. The tolerance the case computes from that number is the "
        "panel's tolerance only if it is the panel's own number."
    )
