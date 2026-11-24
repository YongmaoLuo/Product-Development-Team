"""The 失败复盘 waiting row must be readable, answerable — and carry no
延长 / 终止.

What this gate witnesses
------------------------
The scheduler panel's waiting table draws one row per wait.  Until the
failure-review row existed, the panel could only draw two kinds of row:
a **human** wait (somebody has to answer it) and an **external** wait
(the system rechecks it).  A third kind arrived with the failure-review
hook: a wait whose ``wait_type`` is ``failure_review`` and whose
``item_id`` **is** the HITL decision id.

That third kind breaks the panel's two assumptions, so it is pinned
here:

1. **It must be answerable.**  The old rule was "only a *human* wait has
   somebody to answer it", so a failure-review row drew no answer input
   at all — the operator saw a question and no way to answer it.
2. **It must not be extendable or terminable.**  延长 and 终止 act on
   the scheduler's own wait endpoints; a failure-review row is not a
   durable wait the scheduler owns, so those two buttons must not be
   drawn for it.  Offering them would offer a command the server has no
   such wait for.
3. **Its answer must reach the existing answer route.**  The row
   already carries the decision id, so the panel sends it along with the
   answer instead of looking the decision up a second time — and the
   answer still goes through the **existing** HITL answer route, not a
   new endpoint.

Why the shape is checked on the source
--------------------------------------
The repo has no JS toolchain in its test lanes, so this is a **shape
gate**: it reads ``frontend/app.js`` / ``frontend/index.html`` as source
and pins the contract in them (test decision point R8).

Non-vacuity
------------
Every assertion below reads the *current* asset and compares it with a
literal written out here.  The helper that extracts a function body
raises when the declaration is absent, so a rename cannot pass by
matching nothing.  The frozen-pin test compares the **bytes on disk**
with the hash the scope gate pins, so a stale pin fails here too.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


#: The repository root.  ``backend/tests/static_gates/`` → up three
#: levels is the checkout, and the frontend assets live at its top.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"
_SCOPE_GATES = _REPO_ROOT / "backend" / "tests" / "static_gates" / (
    "test_verification_mode_scope_gates.py"
)

#: The wait type the failure-review hook writes.  It is the server's
#: own vocabulary (``backend/routes/scheduler.py`` builds the row), so
#: the panel spells it exactly.
_FAILURE_REVIEW = "failure_review"

#: The cache-busting version ``index.html`` gives ``app.js``.  The
#: browser must not keep the old file after the panel changed.
_APP_JS_VERSION = 20


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

    The body ends at the first line that is exactly ``}`` — the file's
    top-level functions close at column zero, so that line ends the
    function no matter what the body contains.  Brace counting would be
    the alternative, and it miscounts the template literals this file is
    full of.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates this gate with fails loudly instead of
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


def _constant_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``const name = {...}``.

    The literal has no nested braces, so the block ends at the first
    closing brace.  Raises :class:`AssertionError` when the declaration
    is absent.
    """
    match = re.search(
        r"^const\s+" + re.escape(name) + r"\s*=\s*\{(.*?)\}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"const {name} not found in the source"
    return match.group(1)


def _frozen_pin(gate_source: str, rel_path: str) -> str:
    """Return the sha256 the scope gate pins for ``rel_path``.

    Raises :class:`AssertionError` when the file is not pinned there at
    all — a file the gate does not hold is a file nothing keeps still.
    """
    match = re.search(
        r'"' + re.escape(rel_path) + r'":\s*\(\s*"([0-9a-f]{64})"',
        gate_source,
    )
    assert match is not None, f"{rel_path} is not pinned in the scope gate"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. The wait type has a word of its own
# ---------------------------------------------------------------------------


def test_wait_type_text_has_failure_review() -> None:
    """``SCHEDULER_WAIT_TYPE_TEXT`` knows the failure-review wait.

    The vocabulary is the server's: a wait type outside the map is
    drawn as the word itself, never as an empty cell and never as a
    guess.  The map is where that word is chosen, so a new wait type
    has to be added there — otherwise the row reads ``failure_review``
    in English where every other row reads a Chinese word.
    """
    body = _constant_body(_source(_APP_JS), "SCHEDULER_WAIT_TYPE_TEXT")
    assert re.search(rf"\b{_FAILURE_REVIEW}\s*:", body), (
        "SCHEDULER_WAIT_TYPE_TEXT 没有 failure_review 的文案"
    )


# ---------------------------------------------------------------------------
# 2. The row is answerable
# ---------------------------------------------------------------------------


def test_answerable_includes_failure_review() -> None:
    """A failure-review wait is answerable, like a human wait.

    The old rule was "only a **human** wait has somebody to answer it".
    A failure-review row is a question for a person too — it is the
    hook's own question — so it has to be drawn with an answer input.
    Without this the operator reads the question and has no way to
    answer it.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")
    assert re.search(rf"answerable\s*=[^\n]*{_FAILURE_REVIEW}", body), (
        "renderSchedulerWaiting() 没有把 failure_review 判为可回答"
    )


# ---------------------------------------------------------------------------
# 3. The row carries no 延长 / 终止
# ---------------------------------------------------------------------------


def test_extend_terminate_hidden_for_failure_review() -> None:
    """A failure-review wait is neither extended nor terminated.

    延长 and 终止 act on the scheduler's own durable wait endpoints; a
    failure-review row is not such a wait, so those two buttons must not
    be drawn for it.  Both literals stay in the renderer — the other
    wait kinds still carry them — they are only guarded.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")
    assert re.search(rf"wait_type\s*!==\s*[\"']{_FAILURE_REVIEW}[\"']", body), (
        "renderSchedulerWaiting() 没有把 failure_review 行挡在延长/终止之外"
    )
    for action in ("extend", "terminate"):
        assert f'data-action="{action}"' in body, (
            f"等待行没有 {action} 操作"
        )


# ---------------------------------------------------------------------------
# 4. The answer carries the decision id
# ---------------------------------------------------------------------------


def test_answer_passes_decision_id_directly() -> None:
    """The row carries the decision id and the answer uses it.

    A failure-review row's ``item_id`` **is** the HITL decision id, so
    the row hands it to the answer path directly (``data-decision``) and
    ``schedulerAnswerWait()`` takes it as a hint.  The hint is only a
    hint: a wait whose id is not a decision id (the ordinary human
    wait) still falls back to looking the decision up.
    """
    js = _source(_APP_JS)
    waiting_body = _function_body(js, "renderSchedulerWaiting")
    answer_body = _function_body(js, "schedulerAnswerWait")

    assert "data-decision" in waiting_body, (
        "等待行没有把 decision id 带给回答操作"
    )
    assert "decisionIdHint" in answer_body, (
        "schedulerAnswerWait() 没有接收 decision id 提示"
    )


# ---------------------------------------------------------------------------
# 5. The browser gets the new file
# ---------------------------------------------------------------------------


def test_asset_version_bumped() -> None:
    """``index.html`` asks for the new ``app.js``.

    The panel's JavaScript is served with a ``?v=`` cache-buster; a
    browser that keeps the old file would keep the old panel — the
    failure-review row would stay unanswerable in every tab that was
    opened before the change.
    """
    html = _source(_INDEX_HTML)
    assert f"app.js?v={_APP_JS_VERSION}" in html, (
        f"index.html 没有引用 app.js?v={_APP_JS_VERSION}"
    )


# ---------------------------------------------------------------------------
# 6. The frozen pins match the bytes on disk
# ---------------------------------------------------------------------------


def test_frozen_pins_match_disk() -> None:
    """The scope gate's pins are the hashes of the files on disk.

    ``frontend/app.js`` and ``frontend/index.html`` are frozen byte for
    byte by the verification-mode scope gate.  This change is approved,
    so the pins move; what must not happen is a pin that names a hash
    no file has — that is a gate that has stopped guarding anything.
    """
    gate_source = _source(_SCOPE_GATES)
    for rel_path, path in (
        ("frontend/app.js", _APP_JS),
        ("frontend/index.html", _INDEX_HTML),
    ):
        on_disk = hashlib.sha256(path.read_bytes()).hexdigest()
        pinned = _frozen_pin(gate_source, rel_path)
        assert pinned == on_disk, (
            f"{rel_path} 的冻结哈希与磁盘不一致：\n"
            f"  钉住的：{pinned}\n"
            f"  磁盘的：{on_disk}"
        )
