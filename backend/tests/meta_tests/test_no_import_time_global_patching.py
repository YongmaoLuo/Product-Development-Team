"""Meta-test: no test module may rebind a production function at import time.

Background
----------
Three test modules used to neutralise the mandatory second-pass
self-review by assigning over the generator modules' names *at import
time*:

    _arch_gen.run_doc_self_review = _passthrough_self_review

Python has no "restore on module teardown", so that assignment lasted
for the entire pytest process.  Whichever module was imported first
won, and every module collected afterwards silently received the no-op
instead of the real ``self_review.run_doc_self_review``.  The damage is
silent because the stub returns a well-formed report: generation still
succeeds, it just stops dispatching anything.

``tests/unit/test_session_reuse_planning_phases.py`` was the measured
victim.  It defends itself by re-binding
``arch_generator.run_doc_self_review`` from ``self_review``, but
``tests/test_skip_propagation.py`` had also replaced
``self_review.run_doc_self_review`` itself, so the "put the real one
back" step read the stub back out:

    tests/unit/test_session_reuse_planning_phases.py::test_the_arch_self_review_reuses_the_generation_session
    E   AssertionError: expected one arch self-review dispatch, got 0

The scoped replacement is a ``monkeypatch`` fixture, which restores the
originals when the test ends.  These two tests pin that shape so it
cannot come back under a different symbol name:

  1. ``test_no_test_module_assigns_a_global_at_import_time`` — an AST
     sweep of every test file: no module-level ``obj.attr = ...``
     assignment outside any function or class body.  This is the
     general shape, so it catches the next one whatever it patches.
  2. ``test_importing_the_polluters_leaves_self_review_intact`` — the
     behavioural check, in a subprocess: import the three former
     polluters and assert ``run_doc_self_review`` is still the same
     function object on all four production modules that hold a
     reference to it.

The subprocess matters.  In-process, pytest has already imported these
modules (at collection) and already applied whatever scoped patching
they do, so the check would only ever re-observe the fixed state; a
fresh interpreter is the only way to observe what *import* alone does.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_TESTS_DIR = _BACKEND_DIR / "tests"

#: The modules that used to rebind ``run_doc_self_review`` at import
#: time.  Listed explicitly so a failure names the culprit; the AST
#: sweep above is what generalises the rule.
_FORMER_POLLUTERS = (
    "test_skip_propagation",
    "test_arch_generator_hard_gate",
    "test_preflight_review",
)

#: Every production module that holds a reference to the function.  All
#: four matter: ``tasks_generator`` in particular binds it into its own
#: namespace via ``from self_review import run_doc_self_review``, so
#: patching the source module alone would not reach it.
_CONSUMERS = (
    "arch_generator",
    "test_design_generator",
    "tasks_generator",
    "self_review",
)


def test_no_test_module_assigns_a_global_at_import_time():
    """No test file may mutate another object's attribute at module scope.

    Scans the AST of every ``*.py`` under ``backend/tests`` and flags an
    assignment to an attribute (``mod.attr = ...``) sitting directly in
    the module body.  Such a statement runs once, at import, and is
    never undone -- exactly the shape that let the self-review stub
    outlive the file that installed it.
    """
    offenders: list[str] = []

    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:  # pragma: no cover - collection would fail first
            continue

        lines = source.splitlines()
        for node in tree.body:  # module scope only, never a def/class body
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Attribute):
                    continue
                rel = path.relative_to(_BACKEND_DIR)
                offenders.append(
                    f"{rel}:{node.lineno}: {ast.unparse(target)} = ...\n"
                    f"      | {lines[node.lineno - 1].strip()}"
                )

    assert not offenders, (
        "these test modules rebind a global at import time and never "
        "restore it, so the leak reaches every module collected later "
        "in the same process -- scope it with a monkeypatch fixture "
        "(or a yield fixture that restores) instead:\n"
        + "\n".join(offenders)
    )


def test_importing_the_polluters_leaves_self_review_intact():
    """Importing the former polluters must not change any binding.

    Runs in a fresh interpreter: import the three modules that used to
    do the import-time rebinding, then assert that ``run_doc_self_review``
    is still the very same function object on all four consumers.
    """
    script = f"""
import importlib, sys
sys.path[:0] = [
    {str(_BACKEND_DIR)!r},
    {str(_TESTS_DIR)!r},
    {str(_TESTS_DIR / "unit")!r},
]
for name in {_FORMER_POLLUTERS!r}:
    importlib.import_module(name)

import self_review
real = self_review.run_doc_self_review

problems = []
for mod_name in {_CONSUMERS!r}:
    mod = importlib.import_module(mod_name)
    if getattr(mod, "run_doc_self_review", None) is not real:
        problems.append(
            "{{}}.{{}} = {{!r}} (expected {{!r}})".format(
                mod_name,
                "run_doc_self_review",
                getattr(mod, "run_doc_self_review", None),
                real,
            )
        )
print("\\n".join(problems))
sys.exit(1 if problems else 0)
"""

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, (
        "importing these test modules replaced run_doc_self_review with "
        "something else; the pollution outlives the module that installed "
        f"it.\\nstdout:\\n{proc.stdout}\\nstderr:\\n{proc.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
