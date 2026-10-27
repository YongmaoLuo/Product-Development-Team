"""Static gate — the deterministic dispatcher must not read VP fields itself.

Why this gate exists
--------------------
The deterministic dispatch table (:data:`verification_subagent.
DETERMINISTIC_RUNNERS`) answers one question: *can this verification point
be graded without a model?*  The answer has to come from the runner that
will execute the point — ``verification_api_runner.validate_vp`` and
``verification_ci_runner.validate_vp`` — because those are the functions
that decide, at execution time, whether the point is runnable at all.

The moment the dispatcher starts reading the VP's fields itself, there are
two answers to the same question:

* the runner says "this VP has no assertions, it cannot be executed";
* the dispatcher says "it has a ``request`` object, so it looks runnable".

The second one is a *guess*.  It is also the guess that drifts: the
dispatcher's copy of the rule is a snapshot, the runner's is the rule.
Every change to a runner's schema silently stops reaching the dispatcher,
and the failure mode is the worst kind — a point that cannot be executed
gets routed to a model anyway, or, worse, a point that the runner would
have rejected is graded by the deterministic path.

So the rule is: **the dispatcher reads the method and nothing else.**  It
asks the runner "can you take this?" and believes the answer.

What this file pins
-------------------
1. **No field reads, anywhere in the module.**  Every read of the two
   fields the boundary names (``request`` and ``assertions``) is a
   violation, wherever it sits: subscript (``vp["request"]``), accessor
   (``vp.get("request")``), membership (``"request" in vp``), or an
   f-string fragment.
2. **No mention, inside the dispatch region.**  The dispatch region (the
   block between the ``DETERMINISTIC-DISPATCH-BEGIN`` / ``-END`` markers)
   must not even *name* those fields — not in a comment, not in a
   docstring, not in a string literal.  A dispatcher that has to talk
   about a field is one step away from branching on it.
3. **The gate is not vacuous.**  The detector is fed a synthetic
   violation and must flag it; the dispatch region must exist and must
   not be empty.  A gate whose target has drifted away (the markers
   renamed, the region emptied) would otherwise pass silently — which is
   how a check that checks nothing survives for years.

The marker pair is what makes rule 2 checkable.  It is also what makes
rule 3 checkable: the region is located by its markers, so a rename
breaks the gate loudly instead of quietly narrowing it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Tuple

#: ``backend/tests/static_gates`` is two levels below ``backend/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The module that owns the dispatch table.
SUBAGENT_SOURCE = BACKEND_DIR / "verification_subagent.py"

#: The markers that delimit the dispatch region.
BEGIN_MARKER = "# DETERMINISTIC-DISPATCH-BEGIN"
END_MARKER = "# DETERMINISTIC-DISPATCH-END"

#: The VP fields the dispatcher must not read.  These are the two the
#: boundary names explicitly: the API request and the assertions.  A
#: dispatcher that reads either of them has re-implemented a piece of
#: the runner's schema check.
BANNED_FIELDS: Tuple[str, ...] = ("request", "assertions")

#: Accessor methods that read a field by name.
_FIELD_ACCESSORS = frozenset({"get", "pop", "setdefault"})


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


def _constant_str(node: ast.AST) -> str | None:
    """The string a node spells out, or ``None`` if it is not a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _banned_string(node: ast.AST) -> str | None:
    """The banned field name a node spells out, if any.

    Handles f-strings too: ``f"{vp['request']}"`` puts the field name in
    a ``JoinedStr`` fragment rather than in a plain constant, and it is
    the same read.
    """
    direct = _constant_str(node)
    if direct is not None:
        return direct if direct in BANNED_FIELDS else None
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            found = _banned_string(part)
            if found is not None:
                return found
    return None


def field_reads(source: str) -> List[Tuple[int, str]]:
    """Every place ``source`` reads a banned VP field.

    Returns ``(line number, offending source line)`` pairs, in source
    order.  An empty list means the source never reads those fields.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    hits: List[Tuple[int, str]] = []

    def record(node: ast.AST, field: str) -> None:
        lineno = getattr(node, "lineno", 1)
        text = lines[lineno - 1].strip() if lineno <= len(lines) else ""
        hits.append((lineno, f"{field}: {text}"))

    for node in ast.walk(tree):
        # vp["request"] / vp["assertions"]
        if isinstance(node, ast.Subscript):
            field = _banned_string(node.slice)
            if field is not None:
                record(node, field)
        # vp.get("request") / vp.pop("assertions") / vp.setdefault(...)
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _FIELD_ACCESSORS
                and node.args
            ):
                field = _banned_string(node.args[0])
                if field is not None:
                    record(node, field)
        # "request" in vp
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    field = _banned_string(comparator)
                    if field is not None:
                        record(node, field)

    return sorted(set(hits))


def dispatch_region(source: str) -> str:
    """The text between the dispatch markers.

    Raises:
        ValueError: when the markers are missing or out of order — the
            region is the gate's target, and a target that cannot be
            located must fail loudly rather than silently shrink to
            nothing.
    """
    begin = source.find(BEGIN_MARKER)
    end = source.find(END_MARKER)
    if begin < 0:
        raise ValueError(f"missing marker {BEGIN_MARKER!r}")
    if end < 0:
        raise ValueError(f"missing marker {END_MARKER!r}")
    if end < begin:
        raise ValueError(
            f"marker {END_MARKER!r} appears before {BEGIN_MARKER!r}"
        )
    return source[begin + len(BEGIN_MARKER):end]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_direct_field_reads() -> None:
    """The dispatcher must not read the VP fields the runners own."""
    source = SUBAGENT_SOURCE.read_text(encoding="utf-8")
    hits = field_reads(source)
    assert not hits, (
        "verification_subagent.py reads the VP fields the runners own. "
        "Executability is decided by the runner's own validator "
        "(verification_api_runner.validate_vp / "
        "verification_ci_runner.validate_vp); a second copy of that rule "
        "in the dispatcher is a guess that drifts from the rule it "
        "copies.\n"
        + "\n".join(f"  line {lineno}: {text}" for lineno, text in hits)
    )


def test_dispatch_region_does_not_mention_the_fields() -> None:
    """Inside the dispatch region, the owned fields are not even named."""
    source = SUBAGENT_SOURCE.read_text(encoding="utf-8")
    region = dispatch_region(source)
    for field in BANNED_FIELDS:
        pattern = re.compile(rf"\b{re.escape(field)}\b", re.IGNORECASE)
        match = pattern.search(region)
        assert match is None, (
            f"the dispatch region mentions {field!r} — a dispatcher that "
            "talks about a field is one step away from branching on it. "
            "The field belongs to the runner's schema, not to the "
            "dispatcher."
        )


def test_dispatch_region_exists_and_is_not_empty() -> None:
    """The gate's target must exist, or the gate checks nothing."""
    source = SUBAGENT_SOURCE.read_text(encoding="utf-8")
    region = dispatch_region(source)
    code_lines = [
        line
        for line in region.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert code_lines, (
        "the dispatch region is empty — the gate would pass on a module "
        "that has no dispatch code at all"
    )
    assert "DETERMINISTIC_RUNNERS" in region, (
        "the dispatch region does not hold the dispatch table — the "
        "markers point at the wrong block"
    )


def test_gate_flags_a_synthetic_violation() -> None:
    """The detector must actually detect — otherwise the gate is a no-op."""
    violating = (
        "def dispatch(vp):\n"
        "    if not vp.get('assertions'):\n"
        "        return None\n"
        "    return vp['request']\n"
    )
    hits = field_reads(violating)
    assert len(hits) == 2, (
        "the detector missed a synthetic violation — the gate cannot be "
        f"trusted; hits: {hits!r}"
    )

    clean = (
        "def dispatch(vp):\n"
        "    method = vp.get('verification_method')\n"
        "    return method\n"
    )
    assert field_reads(clean) == [], (
        "the detector flagged a clean source — it is too broad to be "
        "trusted"
    )
