"""The frontend must be able to say "paused", and must let the operator
continue without re-typing the project directory.

Why this gate exists
--------------------
Stopping an execution used to be final: ``stop`` killed the process and
left nothing behind, so the plan sat at ``stopped`` forever unless the
operator re-ran the whole thing. Stopping is now a *pause* — it writes
a resume point that carries the interrupted task list **and** the
``project_dir`` the work was happening in, and the backend reports it as
``paused: true`` alongside ``status`` (task 7).

Two things in ``frontend/`` therefore became wrong the moment that
contract landed, and neither fails loudly:

1. ``setExecBadge`` has no ``paused`` label, so a paused plan renders
   as the raw string ``paused`` — or, worse, is silently indistinguishable
   from a plan that was stopped after finishing. The operator cannot
   tell "wait for me, I will continue this" from "this is over", which
   is the one distinction the pause feature exists to create.
2. ``startExecution`` refuses to send anything when the project
   directory box is empty. The resume point *already knows* the
   directory — the backend reads it from the checkpoint and only falls
   back to the request body — so requiring the operator to type a path
   they typed once before is a gate on the exact action the feature is
   meant to make easier.

Neither is a build error or a test failure. The page simply lies about
the plan's state until someone reads the source.

The rule
--------
* ``setExecBadge`` must carry a ``paused`` label, and the badge state
  must be derived from ``data.paused`` — which outranks ``status``,
  because a paused plan that was never resumed reports ``status:
  "stopped"``, and one that survived a backend restart reports
  ``status: "not_started"`` (both confirmed in
  ``backend/routes/execution.py::_pause_fields`` and the ``not_started``
  branch of ``get_execution_status``).
* Both status-reading call sites — the poller and the panel-resume —
  must go through that one derivation, so the two cannot drift.
* ``startExecution`` must have no empty-directory guard, and must fall
  back to an empty request body so the backend can supply the directory
  from the resume point. An empty body with no resume point is answered
  with a 400 by the backend, which the existing ``catch`` already shows
  in an ``alert`` — the correct place for that message.

Scan target
-----------
``frontend/app.js`` and ``frontend/style.css``, read as source. These are
shape gates, like the rest of ``static_gates/``: they run on a plain
Python environment with no JS toolchain, so they pin the contract in
the source rather than executing the page. A new ``.paused`` badge
variant without a CSS rule renders as an unstyled pill, which is why
the stylesheet is checked too.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_STYLE_CSS = _REPO_ROOT / "frontend" / "style.css"


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


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.

    Brace counting is naive — it does not know about strings or
    comments — which is adequate for the handful of short, uniformly
    indented functions this file inspects.
    """
    pattern = re.compile(
        r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name}() not found in the source"
    body_start = match.end() - 1  # index of the opening '{'
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"unbalanced braces in function {name}()")


#: ``: {}`` followed by a statement terminator — the empty-object branch
#: of a ternary that builds the start request body. Whitespace-tolerant
#: so reformatting the file does not break the gate, but specific
#: enough that an unrelated ``{}`` (an options object, a reset) does not
#: satisfy it.
_EMPTY_OBJECT_BRANCH = re.compile(r":\s*\{\s*\}\s*[;,)]")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_app_js_has_paused_badge() -> None:
    """A paused plan must render "已暂停", and ``paused`` must outrank
    ``status``.

    Both call sites that read ``GET /execution/{id}/status`` go through
    one derivation, checked here at both of them. Letting them drift is
    the failure this pins: the panel would show 已暂停 while the poller
    kept overwriting it with 已停止, so the badge would flicker between
    the two every two seconds and tell the operator neither.
    """
    source = _source(_APP_JS)

    assert 'paused: "已暂停"' in source, (
        'setExecBadge has no "paused" label. A paused plan would render '
        "the raw status string instead of telling the operator that the "
        "plan is waiting to be continued."
    )

    assert "data.paused" in source, (
        "the badge state is never derived from the backend's `paused` "
        "field, so `status` alone decides what the operator sees. That "
        "is wrong twice over: a paused plan that was not resumed is "
        "`stopped`, and one that outlived a backend restart is "
        "`not_started`."
    )

    for caller in ("startExecutionPolling", "resumeExecutionPanel"):
        body = _function_body(source, caller)
        assert "execBadgeState(" in body, (
            f"{caller}() does not route its badge through "
            "execBadgeState(); it renders `data.status` directly, so it "
            "cannot show a paused plan as paused."
        )


def test_start_execution_allows_empty_project_dir() -> None:
    """An empty project directory must still send the start request.

    The backend reads ``project_dir`` off the resume point first and
    only falls back to the request body
    (``backend/routes/execution.py::start_execution``), so an empty
    body is the correct thing to send when continuing a paused plan.
    The 400 for "no resume point and no directory" is the backend's
    answer and the existing ``catch`` already puts it in an ``alert`` —
    which is where a message about a field belongs, and where the
    operator can act on it.
    """
    body = _function_body(_source(_APP_JS), "startExecution")

    assert "请输入目标项目目录路径" not in body, (
        "startExecution() still refuses to send when the project "
        "directory box is empty. That makes continuing a paused plan "
        "require typing a path the resume point already carries."
    )

    assert "/start" in body, (
        "startExecution() no longer issues the start request — the "
        "empty-directory guard was replaced rather than removed."
    )

    assert _EMPTY_OBJECT_BRANCH.search(body), (
        "the request body has no empty-object branch, so an empty "
        "project directory is sent as something other than `{}`."
    )


def test_css_has_paused_badge() -> None:
    """``.exec-badge.paused`` must be styled.

    ``setExecBadge`` sets ``className = `exec-badge ${status}``, so the
    paused state lands on an element whose only variant rules are the
    ones already in the sheet. Without a ``.paused`` rule the pill falls
    back to the base ``--surface3`` background — the same grey as 未开始
    — and the pause reads as "nothing has happened yet".
    """
    assert ".exec-badge.paused" in _source(_STYLE_CSS), (
        "style.css has no .exec-badge.paused rule; the paused badge "
        "renders with the base (grey) styling shared with not_started."
    )
