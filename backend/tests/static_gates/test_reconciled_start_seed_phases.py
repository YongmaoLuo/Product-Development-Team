"""The start-path test seeds must name the phase the start route accepts.

Why this gate exists
--------------------
The waiting phase used to be called ``ready``. It is now called
``queued`` — one word for one situation, decided in
``backend/phase_vocabulary.py``. The start route enforces the
vocabulary with a CAS on the routing row: the set of phases a start may
come from is ``server._EXECUTION_START_SOURCE_PHASES``, and it lists
``queued`` (plus the terminal re-entry phases) — not ``ready``. A row
parked at the retired word is refused with ``409 stage_mismatch``.

Two test files still parked their routing row at the retired word
before calling the start route:

* ``backend/tests/unit/test_exec_pid_cleared_on_terminal.py`` — the
  spawn-failure case seeds the routing row directly, then posts to
  ``/api/execution/{id}/start``;
* ``backend/tests/integration/api/test_execution_resume_start.py`` — the
  two fresh-start cases seed it through the ``_seed_plan`` helper.

A seed at the retired word is not a slower test; it is a test of a path
the product no longer has. Left alone, the suite would fail at the CAS
for a reason that reads like "the start route is broken", when in fact
the fixture is out of date. The worse failure is the one where somebody
"fixes" it by widening the expectation to "409 or 200": the case then
passes under either vocabulary, so it pins nothing about the start path
at all, and the merge can be undone by the next commit without a single
test noticing.

The rule
--------
Both files seed the routing row at ``queued``, and neither may seed it
at ``ready`` in any shape: positional, keyword, or the "accepts either
word" form that pins nothing.

What is deliberately *not* checked
----------------------------------
``ready`` is still a legal **input** alias: the plan-state JSON mirror
normalises it on the way in, and that normalisation is a compatibility
path somebody has to keep testing. The spawn-failure case writes the
legacy word into that mirror on purpose; rewriting it would change what
the case exercises (the alias path) into something else (the ordinary
path) without changing what it proves about the pid.

The ``_seed_plan`` helper's default is ``"stopped"`` and stays there:
it is what the other cases in that file use, and ``stopped`` is one of
the phases a start may come from, so the default is already a legal
seed.

Scan target
-----------
The two test files, read as source. This is a shape gate, like the rest
of ``static_gates/``: it pins the contract in the text rather than
executing the tests it inspects, so the alignment cannot rot back into a
stale seed without a gate noticing. The behaviour itself is pinned by
those files' own runs.
"""

from __future__ import annotations

import re
from pathlib import Path


#: ``backend/tests/`` — the directory the two reconciled files live in.
_TESTS_DIR = Path(__file__).resolve().parents[1]

#: ``backend/`` — where the start route's whitelist lives.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The files this gate reconciles. The key is what the file is *for*;
#: the value is where it lives relative to ``backend/tests/``.
_SEED_FILES: dict[str, Path] = {
    "the spawn-failure pid test (backend/tests/unit/test_exec_pid_cleared_on_terminal.py)": (
        _TESTS_DIR / "unit" / "test_exec_pid_cleared_on_terminal.py"
    ),
    "the resume-start lane (backend/tests/integration/api/test_execution_resume_start.py)": (
        _TESTS_DIR / "integration" / "api" / "test_execution_resume_start.py"
    ),
}

#: The seed shape each file uses. The spawn-failure case inserts the
#: routing row itself; the resume lane hands the phase to the
#: ``_seed_plan`` helper as a keyword argument.
_SEED_SHAPE: dict[str, re.Pattern[str]] = {
    "the spawn-failure pid test (backend/tests/unit/test_exec_pid_cleared_on_terminal.py)": (
        re.compile(
            r"""RoutingRepository\(\s*[^)]*\)\s*\.\s*insert\(\s*[^,()]+,\s*["']queued["']"""
        )
    ),
    "the resume-start lane (backend/tests/integration/api/test_execution_resume_start.py)": (
        re.compile(r"""\bphase\s*=\s*["']queued["']""")
    ),
}

# ---------------------------------------------------------------------------
# Patterns — the retired word
# ---------------------------------------------------------------------------

#: A routing-row seed that names the retired waiting phase positionally:
#: ``RoutingRepository(conn).insert(plan_id, "ready")``.
_READY_ROUTING_SEED = re.compile(
    r"""RoutingRepository\(\s*[^)]*\)\s*\.\s*insert\(\s*[^,()]+,\s*["']ready["']"""
)

#: A seed that names the retired waiting phase as a keyword:
#: ``_seed_plan(..., phase="ready")``.
_READY_KEYWORD_SEED = re.compile(
    r"""\bphase\s*=\s*["']ready["']"""
)

#: Any ``insert(<row>, "ready")`` — the routing repository is the one
#: that carries the phase, but the *shape* is what matters here, not the
#: receiver: a seed that moved to a helper of its own would otherwise
#: slip past the pattern above.
_READY_POSITIONAL_INSERT = re.compile(
    r"""\.\s*insert\(\s*[^,()]+,\s*["']ready["']"""
)

#: A comparison that accepts *either* word — the "widened" seed that
#: looks like a fix and is not one: it passes under the old vocabulary
#: and the new one, so it pins nothing.
_BOTH_WORDS_ACCEPTED = re.compile(
    r"""\bin\s*\(\s*[^)\]]*["']ready["'][^)\]]*["']queued["'][^)\]]*\)"""
    r"""|\bin\s*\(\s*[^)\]]*["']queued["'][^)\]]*["']ready["'][^)\]]*\)"""
)

# ---------------------------------------------------------------------------
# Patterns — the phase vocabulary the seeds must agree with
# ---------------------------------------------------------------------------

#: Every phase a call site hands to the ``_seed_plan`` helper.
_KEYWORD_PHASE_VALUE = re.compile(r"""\bphase\s*=\s*["']([a-z_]+)["']""")

#: The helper's default phase.
_DEFAULT_PHASE_VALUE = re.compile(
    r"""\bphase\s*:\s*str\s*=\s*["']([a-z_]+)["']"""
)

#: The start route's whitelist, read out of ``backend/server.py``.
_WHITELIST_ASSIGNMENT = re.compile(
    r"""_EXECUTION_START_SOURCE_PHASES\s*=\s*\(([^)]*)\)""", re.DOTALL
)

#: A phase name inside the whitelist tuple.
_PHASE_NAME = re.compile(r"""["']([a-z_]+)["']""")


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


def _matches(pattern: re.Pattern[str], source: str) -> list[str]:
    """Return the source lines that ``pattern`` matches, trimmed.

    Only the line is returned, never the matched span's neighbours: the
    failure message says *where* to look, and the reader opens the file.
    """
    return [
        line.strip()
        for line in source.splitlines()
        if pattern.search(line)
    ]


def _start_source_phases() -> set[str]:
    """Return the phases ``POST /api/execution/{id}/start`` accepts.

    Read out of ``backend/server.py`` rather than imported: this gate
    is a shape gate, and importing the server module to read one tuple
    would turn a fast source scan into a slow module boot.
    """
    source = _source(_BACKEND_DIR / "server.py")
    assignment = _WHITELIST_ASSIGNMENT.search(source)
    assert assignment is not None, (
        "backend/server.py no longer assigns "
        "`_EXECUTION_START_SOURCE_PHASES` to a tuple literal. The "
        "start route's whitelist moved or changed shape, and this gate "
        "can no longer tell which phases a seed may name."
    )
    return set(_PHASE_NAME.findall(assignment.group(1)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_seed_helpers_use_queued_phase() -> None:
    """Both files must seed the routing row at a phase the route accepts.

    Not merely "the word ``queued`` appears somewhere": each file has to
    seed it the way that file seeds, and the word it seeds has to be one
    the start route's whitelist actually contains. A file that seeds
    ``queued`` while the route only accepts ``stopped`` would leave the
    case asserting a path the product does not have — the same defect
    this gate exists to prevent, one level up.
    """
    whitelist = _start_source_phases()

    assert "queued" in whitelist, (
        "the start route's whitelist does not contain `queued`, so a "
        "seed naming it would be refused by the CAS. The waiting phase "
        "is `queued` — the route must accept it."
    )
    assert "ready" not in whitelist, (
        "the start route's whitelist still contains `ready`. The "
        "waiting phase is `queued`; `ready` is an input alias only."
    )

    for description, path in _SEED_FILES.items():
        source = _source(path)

        matched = _matches(_SEED_SHAPE[description], source)
        assert matched, (
            f"{description} seeds no `queued` routing row. The start "
            f"route accepts a row parked at `queued`; a seed at the "
            f"retired word is refused with 409 before the case can "
            f"test what it is about."
        )


def test_no_ready_phase_seed_in_start_path_tests() -> None:
    """Neither file may seed the retired word, in any shape.

    Three nets, because there are three ways to write the same wrong
    seed: the positional routing insert, the keyword argument, and the
    "accepts either word" form that pins nothing. The widened form is
    called out by name, because it is the shape that looks like a fix:
    an assertion (or a seed) that accepts ``ready`` *or* ``queued``
    passes under either vocabulary, so the next commit can undo the
    merge without a single test noticing.

    The plan-state JSON mirror is out of scope, and deliberately so:
    ``ready`` is still a legal *input* there, and the compatibility path
    it exercises is somebody's job to keep testing.
    """
    for description, path in _SEED_FILES.items():
        source = _source(path)

        routing = _matches(_READY_ROUTING_SEED, source)
        assert not routing, (
            f"{description} still seeds a routing row at `ready`. The "
            f"waiting phase is `queued`; the start route refuses a row "
            f"parked at the retired word."
        )

        keyword = _matches(_READY_KEYWORD_SEED, source)
        assert not keyword, (
            f"{description} still seeds a phase at `ready`. The "
            f"waiting phase is `queued`."
        )

        positional = _matches(_READY_POSITIONAL_INSERT, source)
        assert not positional, (
            f"{description} still inserts a row with the phase `ready`. "
            f"The waiting phase is `queued`."
        )

        widened = _matches(_BOTH_WORDS_ACCEPTED, source)
        assert not widened, (
            f"{description} accepts both `ready` and `queued` for the "
            f"same seed. That passes under either vocabulary, so it "
            f"pins nothing."
        )


def test_resume_start_seed_uses_queued() -> None:
    """The resume lane must seed ``queued`` — and only phases it may seed.

    Every phase the lane hands to its ``_seed_plan`` helper is checked
    against the route's whitelist, plus ``executing`` — the one phase
    the lane seeds on purpose to provoke the conflict answer. A phase
    that is neither would be a row the start route refuses before the
    case reaches its own assertion.

    The helper's default is pinned too: it is ``stopped``, and the
    other cases in that file depend on it. Quietly changing the default
    would change what those cases test, which is not a change a gate
    should have to notice by accident.
    """
    description = (
        "the resume-start lane "
        "(backend/tests/integration/api/test_execution_resume_start.py)"
    )
    path = _SEED_FILES[description]
    source = _source(path)

    seeded = set(_KEYWORD_PHASE_VALUE.findall(source))
    assert seeded, (
        f"{description} seeds no explicit phase at all. The fresh-start "
        f"cases have to say which phase they park the routing row at."
    )

    allowed = _start_source_phases() | {"executing"}
    unexpected = sorted(seeded - allowed)
    assert not unexpected, (
        f"{description} seeds phase(s) {unexpected} that the start "
        f"route does not accept. The route accepts "
        f"{sorted(_start_source_phases())} — a row parked anywhere else "
        f"is refused with 409 before the case reaches its assertion."
    )

    assert "ready" not in seeded, (
        f"{description} still seeds `ready`. The waiting phase is "
        f"`queued`."
    )
    assert "queued" in seeded, (
        f"{description} seeds no `queued` phase. The fresh-start cases "
        f"park the routing row at `queued` — the phase the start route "
        f"accepts."
    )

    default = _DEFAULT_PHASE_VALUE.findall(source)
    assert default == ["stopped"], (
        f"{description}'s `_seed_plan` default phase is {default}, "
        f"expected `['stopped']`. The default is what the other cases "
        f"in that file rely on; changing it changes what they test."
    )