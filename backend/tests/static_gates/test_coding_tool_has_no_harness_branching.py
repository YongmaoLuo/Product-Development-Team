"""The dispatch module must not branch on a backend name.

Why this gate exists
--------------------
``backend/coding_tool.py`` is the module that used to *be* the
harness: it built the argv, wrapped the command in the sandbox,
merged the environment and spawned the child, and it decided which
backend it was driving while it did so.  Two branches in it said
``"claude"``, and both of them were wrong the moment a second backend
existed — the branch that answered "which backend am I?" could not be
answered from inside the module that was already building the
backend's command line.

The backend is now chosen once, at start-up, by
:mod:`harness.registry`, and every dispatch reads the answer from
there.  That is the *selection point*, and it is the only place in the
codebase where a backend name is compared with anything.

This gate makes that structural instead of aspirational.  The rule is
narrow on purpose:

* a **comparison** against a backend-name literal (``==``, ``!=``,
  ``in``, ``not in``) is a branch on the backend, and is forbidden in
  ``coding_tool.py``;
* a **``match`` statement** whose pattern is a backend-name literal is
  the same branch written differently, and is forbidden too;
* everything else that mentions a backend name is **not** a branch and
  is **not** forbidden.  A telemetry field that says
  ``{"tool": "claude"}``, an environment default that says
  ``os.environ.get("PDT_DEFAULT_CODING_TOOL", "claude")`` and a
  docstring that says ``app_type='claude'`` are all *names*, not
  decisions.  A gate that flagged those would be a gate that cries
  wolf, and a gate that cries wolf gets allowlisted, and an
  allowlisted gate protects nothing.

What is deliberately **not** in this gate
-----------------------------------------
``coding_tool.py`` asks the adapter's *type* one question — whether
this backend's command line can carry a settings file — and does it
with ``isinstance(harness, ClaudeHarness)``.  That is a type check,
not a literal comparison, and the rule this gate states is about
literal comparisons; the rule about naming a concrete adapter is
stated where it belongs, in
``test_single_spawn_point.py``, and it applies to the runner.

Why a *gate* and not just the fix
---------------------------------
"Ask the registry instead of branching" is a rule a reviewer applies
while reading a diff, and a reviewer is not in the room for the next
commit.  The branch is one line, it is a line that works perfectly on
the backend it was written for, and nothing about it fails until the
day somebody needs the *other* backend to take a different path and
finds that two places make that decision.  Keeping the branch out is
the part that needs a test.

The behavioural half
--------------------
The other half of the guarantee is that the choice is made in one
place and read everywhere else.  That is what
``backend/tests/unit/harness/test_single_selection.py`` and
``test_resolve_harness.py`` pin; this gate covers the half that a
*literal* branch in the dispatch would break.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The backend directory, derived from this file's location rather than
#: from the process working directory: CI runs the suite with
#: ``working-directory: backend``, where a ``backend/`` root does not
#: exist.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The module this gate is about.
CODING_TOOL = BACKEND_DIR / "coding_tool.py"

#: The backend names this gate refuses to see used as a branch
#: condition.  These are the two names :data:`harness.registry.HARNESS_NAMES`
#: knows; a third backend would be added there and here.
BACKEND_LITERALS: frozenset[str] = frozenset({"mcode", "claude"})

#: The comparison operators that make a comparison a *branch*.
_BRANCH_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def _carries_backend_literal(node: ast.AST) -> bool:
    """True when *node* is, or contains, a backend-name literal.

    A tuple, list or set of names is included: ``backend in ("claude",
    "mcode")`` is one branch written with a container, and the
    container is where the name actually sits.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in BACKEND_LITERALS
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_carries_backend_literal(element) for element in node.elts)
    return False


def _pattern_carries_backend_literal(node: ast.AST) -> bool:
    """True when a ``match`` pattern names a backend.

    ``case "claude":`` is a :class:`ast.MatchValue`; ``case "claude" |
    "mcode":`` is two of them inside a :class:`ast.MatchOr`.  Walking
    the whole pattern subtree covers both, and also the value of a
    mapping pattern.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.MatchValue) and _carries_backend_literal(child.value):
            return True
    return False


def find_literal_backend_branches(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, description)`` for every backend-name branch.

    A *branch* is a comparison whose operands name a backend, or a
    ``match`` case whose pattern names one.  Everything else — a
    dictionary value, a call argument, a default value — is a name,
    not a decision, and is not reported.

    Args:
        source: The text of the module to scan.

    Returns:
        One ``(lineno, description)`` pair per branch, in source
        order.  The description names the shape rather than quoting
        the line, so a failure message never has to carry the matched
        text.
    """
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            # ``node.ops`` holds *instances* of the operator classes,
            # so the name comes from the type, not from the node.
            names = [
                type(op).__name__
                for op in node.ops
                if isinstance(op, _BRANCH_OPS)
            ]
            if not names:
                continue
            operands = [node.left, *node.comparators]
            if any(_carries_backend_literal(operand) for operand in operands):
                found.append(
                    (
                        node.lineno,
                        "comparison on a backend name ("
                        + ", ".join(sorted(set(names)))
                        + ")",
                    )
                )
        elif isinstance(node, ast.Match):
            for case in node.cases:
                if _pattern_carries_backend_literal(case.pattern):
                    found.append(
                        (case.pattern.lineno, "match case on a backend name")
                    )

    found.sort(key=lambda hit: hit[0])
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_literal_backend_branch() -> None:
    """The dispatch module branches on no backend name — and can still fail.

    Two claims in one test, because they are two halves of the same
    guarantee: the tree is clean, and the gate is not a rubber stamp.
    A gate that cannot fail is indistinguishable from a passing one,
    so a violating sample is injected and the gate has to report it.
    """
    source = CODING_TOOL.read_text(encoding="utf-8")
    hits = find_literal_backend_branches(source)
    assert not hits, (
        "backend/coding_tool.py branches on a backend name. The backend is "
        "chosen once, at start-up, by harness.registry; every dispatch "
        "reads the answer from there. A branch here is a second "
        "selection point, and it is the one that grows a new backend's "
        "special case every time a backend is added.\n"
        "  " + "\n  ".join(f"coding_tool.py:{lineno}: {what}" for lineno, what in hits)
    )

    # The gate has to be able to fail. The sample is the shape a
    # regression would take: a comparison against a backend name, a
    # membership test against a tuple of them, and a match case.
    injected = find_literal_backend_branches(_INJECTED_BRANCHES)
    assert injected, (
        "the gate did not report an injected backend branch, so it "
        "cannot fail and the assertion above proves nothing"
    )
    assert len(injected) == 3, (
        "the gate reported "
        f"{len(injected)} of the 3 injected branches: {injected!r}"
    )


def test_the_gate_reports_every_shape_of_backend_branch() -> None:
    """Each forbidden shape is reported, and each allowed one is not.

    The allowed shapes matter as much as the forbidden ones: a gate
    that also flags a telemetry field or an environment default would
    be flagging names rather than decisions, and it would be
    allowlisted within a week.
    """
    hits = find_literal_backend_branches(_INJECTED_BRANCHES)
    reported = {what for _, what in hits}
    assert "comparison on a backend name (Eq)" in reported
    assert "comparison on a backend name (In)" in reported
    assert "match case on a backend name" in reported

    assert find_literal_backend_branches(
        'telemetry = {"tool": "claude"}\n'
    ) == []
    assert find_literal_backend_branches(
        'default = os.environ.get("PDT_DEFAULT_CODING_TOOL", "claude")\n'
    ) == []
    assert find_literal_backend_branches(
        'if tool_type == "vendor-c":\n    pass\n'
    ) == []
    assert find_literal_backend_branches(
        'if name != "claude-code":\n    pass\n'
    ) == []
    assert find_literal_backend_branches(
        'if "claude" in names:\n    pass\n'
    ), "a literal on the left of ``in`` is still a branch"


def test_the_gate_reads_the_real_module() -> None:
    """The scan target must exist and be the module this gate is about.

    A gate whose target has moved passes vacuously: the file is
    renamed, the path stops resolving, the scan reads nothing, and the
    "no branch" assertion stays green.
    """
    assert CODING_TOOL.is_file(), (
        f"{CODING_TOOL} does not exist, so the gate would scan nothing "
        "and pass vacuously"
    )
    source = CODING_TOOL.read_text(encoding="utf-8")
    assert "coding_tool" in source, (
        "the scanned file does not look like the dispatch module"
    )


#: The violating sample used to prove the gate can fail.  It is
#: written out in full rather than assembled from fragments: it holds
#: backend *names*, and a backend name is not private — it is the
#: thing this gate exists to keep out of a branch.
_INJECTED_BRANCHES = '''\
def choose(backend: str) -> str:
    if backend == "mcode":
        return "mcode-command"
    if backend in ("claude", "vendor-c"):
        return "claude-command"
    match backend:
        case "claude":
            return "legacy-command"
    return "default-command"
'''
