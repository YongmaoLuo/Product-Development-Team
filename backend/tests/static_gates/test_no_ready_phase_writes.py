"""Static gate: production code must not write the legacy ``ready`` phase.

Why this gate exists
--------------------
The waiting phase used to be spelled ``ready``. It is now spelled ``queued``,
and ``queued`` is the *only* waiting phase (see
``backend/phase_vocabulary.py``). Reading a legacy ``ready`` is fine — the
vocabulary module normalises it and the startup migration rewrites the rows
that still hold it. **Writing** it is not: a production write puts a plan
back into a phase that no reader of the new vocabulary expects, and the
result is a plan that is neither running nor waiting for anybody — a black
hole nobody claims.

The writes used to be scattered across ``pipeline.py``, ``routes/fast_lane.py``,
``routes/execution.py``, ``agent.py`` and ``verification_loop.py``. A review
catches the ones in the diff in front of it; it does not catch the one added
three months later in a file nobody was looking at. That is the shape of the
problem this gate closes: the *next* ``ready`` write, not the current one.

How the gate decides
--------------------
Only **call shapes** count, and only when the phase argument is a string
*literal* ``'ready'``:

* ``transition_to(...)`` / ``force_set_phase(...)`` / ``reset_to(...)`` —
  first positional argument, or the ``phase=`` keyword;
* ``_seed_plan_routing_phase(...)`` — the ``phase=`` keyword (or the phase
  argument in its second positional slot).

A runtime value that happens to equal ``'ready'`` cannot be detected by a
static gate; the gate pins the *literal* shape, which is the shape every
real write of the legacy vocabulary had.

What is deliberately NOT a hit
------------------------------
* **Comments and docstrings.** Prose about the legacy phase is evidence of
  the migration, not a write. ``backend/verification_loop.py`` keeps a
  comment explaining that the ``update_phase(plan_id, "ready")`` mirror was
  removed — that comment must stay, and the gate reads the AST, so prose is
  invisible to it.
* **Status buckets.** ``status = "ready"`` in ``backend/routes/plans.py`` is
  a *response bucket* for the plan-list API, not a phase write. Assigning a
  string to a variable is not one of the call shapes above.
* **The three vocabulary modules.** ``phase_vocabulary.py`` (the vocabulary
  itself), ``phase_migration.py`` (the startup migration that has to
  *name* ``ready`` in order to migrate away from it) and ``plan_state.py``
  (the state machine that still accepts ``ready`` as an input alias) are
  allowlisted by module name.

Scan scope
----------
Every ``*.py`` file under ``backend/``, excluding ``tests/`` subtrees and
vendor/cache trees. The gate file itself lives under ``backend/tests/``, so
it is out of scope by construction.

The failing shape
-----------------
A violation is reported as ``backend/<path>:<line> <call>``, e.g.::

    backend/foo.py:12 transition_to("ready")
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterator, NamedTuple

#: Repository root — ``backend/tests/static_gates/<this file>`` sits three
#: levels below it.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The tree this gate scans.
_BACKEND_DIR = _REPO_ROOT / "backend"

#: Directory names skipped wholesale: test subtrees plus vendor/cache trees.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", ".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"}
)

#: Modules allowed to name the legacy vocabulary.  The vocabulary itself,
#: the migration that rewrites legacy rows, and the state machine that
#: still accepts the legacy word as an input alias.
WHITELISTED_MODULES: frozenset[str] = frozenset(
    {
        "phase_vocabulary.py",
        "phase_migration.py",
        "plan_state.py",
    }
)

#: The legacy phase literal the gate forbids in production write paths.
LEGACY_PHASE = "ready"

#: Call targets whose phase argument is inspected, mapped to the positional
#: index that argument occupies.  ``transition_to`` / ``force_set_phase`` /
#: ``reset_to`` are plan-state methods taking the phase first;
#: ``_seed_plan_routing_phase`` takes ``(plan_id, phase)``.
_PHASE_ARG_INDEX: dict[str, int] = {
    "transition_to": 0,
    "force_set_phase": 0,
    "reset_to": 0,
    "_seed_plan_routing_phase": 1,
}


class ReadyPhaseWrite(NamedTuple):
    """One forbidden ``ready`` write, located inside a source file."""

    #: 1-based line of the offending call.
    lineno: int
    #: The offending call, e.g. ``transition_to("ready")``.
    text: str


def _call_name(func: ast.expr) -> str | None:
    """The bare name of the callable.

    ``ps.transition_to`` and ``transition_to`` both yield ``transition_to`` —
    the receiver is irrelevant to the question "is this a phase write?".
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_ready_literal(node: ast.expr) -> bool:
    """Is this expression the string literal ``'ready'``?

    An f-string with no placeholders (``f"ready"``) is the same literal
    wearing different syntax, so it counts too.  A name, an attribute or a
    call is not a literal — the gate cannot know its runtime value, and
    guessing would make it a source of false alarms.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == LEGACY_PHASE
    if isinstance(node, ast.JoinedStr):
        return (
            len(node.values) == 1
            and isinstance(node.values[0], ast.Constant)
            and isinstance(node.values[0].value, str)
            and node.values[0].value == LEGACY_PHASE
        )
    return False


def _segment(source: str, node: ast.expr) -> str:
    """The source text of *node*, verbatim when possible."""
    text = ast.get_source_segment(source, node)
    if text:
        return text
    return ast.unparse(node)


def _find_ready_phase_writes(source: str) -> list[ReadyPhaseWrite]:
    """Every call in *source* that writes the legacy ``ready`` phase.

    The scan reads the AST, so comments and docstrings — which is where
    the migration history is recorded — are invisible to it.  Only the
    call shapes listed in :data:`_PHASE_ARG_INDEX` count.
    """
    hits: list[ReadyPhaseWrite] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name is None:
            continue
        index = _PHASE_ARG_INDEX.get(name)
        if index is None:
            continue
        if len(node.args) > index and _is_ready_literal(node.args[index]):
            argument = _segment(source, node.args[index])
            # The phase is the first argument for the plan-state methods and
            # the second for ``_seed_plan_routing_phase``; the report says
            # which one it is, so the line can be read without the source.
            rendered = (
                f"{name}({argument})"
                if index == 0
                else f"{name}(..., {argument})"
            )
            hits.append(ReadyPhaseWrite(node.lineno, rendered))
            continue
        for keyword in node.keywords:
            if keyword.arg == "phase" and _is_ready_literal(keyword.value):
                hits.append(
                    ReadyPhaseWrite(
                        node.lineno,
                        f"{name}(phase={_segment(source, keyword.value)})",
                    )
                )
    return hits


def _iter_production_files() -> Iterator[Path]:
    """Every ``*.py`` file under ``backend/`` outside the excluded trees."""
    for dirpath, dirnames, filenames in os.walk(_BACKEND_DIR):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _scan_production_tree() -> list[str]:
    """The violations in the production tree, one reportable line each.

    A line reads ``backend/<path>:<line> <call>`` — e.g.
    ``backend/foo.py:12 transition_to("ready")`` — so the report points at
    the exact place to fix.
    """
    violations: list[str] = []
    for path in _iter_production_files():
        if path.name in WHITELISTED_MODULES:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for hit in _find_ready_phase_writes(source):
            relative = path.relative_to(_REPO_ROOT)
            violations.append(f"{relative}:{hit.lineno} {hit.text}")
    return violations


def test_no_production_module_writes_ready_phase() -> None:
    """No production module under ``backend/`` writes the legacy phase."""
    # Non-vacuity first: a walk that reaches nothing would make the
    # assertion below pass for the wrong reason.  The modules named here
    # are the ones the legacy vocabulary used to be written from.
    scanned = {path.relative_to(_REPO_ROOT) for path in _iter_production_files()}
    for expected in (
        Path("backend/pipeline.py"),
        Path("backend/verification_loop.py"),
        Path("backend/routes/plans.py"),
    ):
        assert expected in scanned, (
            f"the scan never reached {expected} — the gate is not looking at "
            "the production tree it claims to cover"
        )

    violations = _scan_production_tree()
    assert not violations, (
        "production code writes the legacy 'ready' phase; 'queued' is the "
        "only waiting phase. Use the waiting-phase vocabulary "
        "(backend/phase_vocabulary.py) instead:\n"
        + "\n".join(violations)
    )


def test_comment_mention_is_not_a_hit() -> None:
    """Prose about the legacy phase is evidence, not a write."""
    source = (
        "# ps.transition_to(\"ready\") must not come back.\n"
        "def handler():\n"
        "    \"\"\"ps.transition_to(\"ready\") is forbidden.\"\"\"\n"
        "    return 1\n"
    )
    hits = _find_ready_phase_writes(source)
    assert hits == [], f"comments/docstrings are not phase writes: {hits}"


def test_status_bucket_assignment_is_not_a_hit() -> None:
    """A status bucket is not a phase write."""
    source = (
        "def summarize():\n"
        "    status = \"ready\"\n"
        "    return status\n"
    )
    hits = _find_ready_phase_writes(source)
    assert hits == [], f"a status bucket is not a phase write: {hits}"


def test_gate_catches_the_real_call_shape() -> None:
    """The shape the gate exists for is caught."""
    source = 'ps.transition_to("ready")\n'
    hits = _find_ready_phase_writes(source)
    assert len(hits) == 1, f"expected exactly one hit, got {hits}"
    assert hits[0].text == 'transition_to("ready")', hits[0]
    assert hits[0].lineno == 1, hits[0]