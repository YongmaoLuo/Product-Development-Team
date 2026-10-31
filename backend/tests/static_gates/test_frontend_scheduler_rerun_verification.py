"""The 「重跑验证」 button must reach the verification API, in the right
order, with the right bodies.

Why this gate exists
--------------------
Task 2 put ``rerun-verification`` into the action matrix, so a failed
plan's row now draws a 「重跑验证」 button. But the button only *asks*
for an action: ``schedulerRunAction()`` is what carries it out, and
until the branch exists the button is drawn and does nothing at all —
the operator presses 「重跑验证」 and the page does not even blink.

Worse, the two commands that make up a re-run look alike and are not
interchangeable:

* ``POST /api/verification/{id}/reset_rounds`` rewinds the round
  counter, so the auto-loop may iterate the batch again. Without it a
  re-run has no rounds left to run and the ``/start`` below is a no-op.
* ``POST /api/verification/{id}/start`` actually begins the round.

So the order is the contract: reset first, then start. And the reset
must not happen when the operator's intent was anything else — a
re-run that reaches for a *scheduler* route (``/plan/{id}/scheduler/
start``) re-queues the whole plan, which is the heavier operation the
operator explicitly did not choose when they picked 「重跑验证」.

The rule
--------
* ``schedulerRerunVerification(planId)`` exists and calls **two**
  verification endpoints: ``reset_rounds`` first, ``start`` second.
* Both calls carry a JSON body — ``JSON.stringify({})`` — because
  ``api()`` spreads its options straight into ``fetch()`` and never
  serialises anything itself: a body of ``{}`` would be stringified
  by ``fetch`` into ``"[object Object]"``, which the server's
  ``ResetRoundsRequest`` model rejects.
* The function touches no ``/scheduler/`` route, and issues no bare
  ``fetch(`` — every request goes through ``api()``, which is what
  carries the request guard's ``X-PDT-Request`` header.
* ``schedulerRunAction()`` dispatches ``"rerun-verification"`` to it.

Scan target
-----------
``frontend/app.js``, read as source. This is a shape gate, like the
rest of ``static_gates/``: it runs on a plain Python environment with
no JS toolchain, so it pins the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"

#: The action key the button carries (task 2's action matrix).
_RERUN_ACTION = "rerun-verification"

#: The two endpoints a re-run is made of, in the order they must be
#: called: the counter is rewound first, then the round begins.
_RESET_ENDPOINT = "reset_rounds"
_START_ENDPOINT = "start"


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


#: One ``api(...)`` call against a verification endpoint: the endpoint
#: name, and the options object that follows it.  The options are read
#: as "everything up to the ``}`` that closes the call", which is why
#: the pattern is anchored on the trailing ``)`` followed by ``;`` —
#: a naive brace match would stop inside ``JSON.stringify({})``.
_VERIFICATION_CALL = re.compile(
    r"api\(\s*`/verification/\$\{[^}]*\}/(?P<endpoint>[A-Za-z_]+)`\s*,\s*"
    r"\{(?P<options>[^;]*?)\}\s*\)\s*;",
    re.DOTALL,
)


def _verification_calls(body: str) -> list[tuple[str, str]]:
    """Return ``(endpoint, options_text)`` for every verification call.

    In source order — which is the order the calls happen in, because
    each one is awaited.
    """
    return [
        (match.group("endpoint"), match.group("options"))
        for match in _VERIFICATION_CALL.finditer(body)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rerun_verification_calls_verification_api() -> None:
    """A re-run is two calls: ``reset_rounds`` then ``start``, both with
    a JSON body.

    The order is the whole point — ``/start`` before ``/reset_rounds``
    would begin a round and then rewind the counter under it, and a
    re-run that only calls ``/start`` has no rounds left to run (the
    counter was never rewound), so it does nothing at all.

    The body is not decoration: ``api()`` passes its options straight
    to ``fetch()``, which does not serialise a plain object — ``{}``
    reaches the server as ``"[object Object]"`` and
    ``ResetRoundsRequest`` rejects it. Only ``JSON.stringify({})``
    puts a JSON document on the wire.
    """
    body = _function_body(_source(_APP_JS), "schedulerRerunVerification")

    calls = _verification_calls(body)

    endpoints = [endpoint for endpoint, _ in calls]
    assert endpoints == [_RESET_ENDPOINT, _START_ENDPOINT], (
        "schedulerRerunVerification() must call the verification API "
        f"exactly twice — {_RESET_ENDPOINT!r} then {_START_ENDPOINT!r} — "
        f"in that order. It calls {endpoints!r}. "
        f"{_RESET_ENDPOINT!r} rewinds the round counter; "
        f"{_START_ENDPOINT!r} begins the round. Without the first, the "
        "second has no rounds left to run."
    )

    for endpoint, options in calls:
        normalized = _normalized(options)
        assert 'method:"POST"' in normalized, (
            f"the {endpoint!r} call does not use the POST method. "
            f"It sends {normalized!r}."
        )
        assert "body:JSON.stringify({})" in normalized, (
            f"the {endpoint!r} call does not send a JSON body. "
            "api() spreads its options into fetch() and never "
            "serialises anything itself, so the body must be written "
            f"as JSON.stringify({{}}) — not {{}}. It sends "
            f"{normalized!r}."
        )

    assert _normalized(body).count("awaitapi(") == 2, (
        "both verification calls must be awaited. An un-awaited call "
        "would let the round begin before the counter is rewound."
    )


def test_rerun_verification_is_dispatched() -> None:
    """The button's action key must reach the function that runs it.

    ``schedulerRunAction()`` is the single dispatcher every action
    button goes through. A ``"rerun-verification"`` key it does not
    know is silently ignored — the button is drawn, and pressing it
    does nothing, with no error to show for it.
    """
    body = _normalized(_function_body(_source(_APP_JS), "schedulerRunAction"))

    assert f'action==="{_RERUN_ACTION}"' in body, (
        f"schedulerRunAction() has no branch for {_RERUN_ACTION!r}. The "
        "button is rendered by the action matrix and dispatched here; "
        "a key with no branch is a button that does nothing."
    )

    assert "schedulerRerunVerification(planId)" in body, (
        f"schedulerRunAction() does not call schedulerRerunVerification() "
        f"for {_RERUN_ACTION!r}."
    )


def test_rerun_verification_does_not_call_scheduler_routes() -> None:
    """A re-run is not a re-queue, and it must not issue a bare fetch.

    ``/plan/{id}/scheduler/start`` puts the whole plan back in the
    queue — a strictly heavier operation than re-running the
    verification round, and the one the operator explicitly did not
    choose when they pressed 「重跑验证」 instead of 「重新排队执行」.

    A bare ``fetch(`` would also be wrong: it carries no
    ``X-PDT-Request`` header, and the server answers such requests
    with 403.
    """
    body = _function_body(_source(_APP_JS), "schedulerRerunVerification")

    assert "/scheduler/" not in body, (
        "schedulerRerunVerification() reaches a /scheduler/ route. A "
        "re-run of the verification round must not re-queue the plan — "
        "that is what the separate 「重新排队执行」 button is for."
    )

    assert "fetch(" not in body, (
        "schedulerRerunVerification() issues a bare fetch(). Every "
        "request must go through api(), which carries the request "
        "guard's X-PDT-Request header; without it the server answers "
        "403."
    )
