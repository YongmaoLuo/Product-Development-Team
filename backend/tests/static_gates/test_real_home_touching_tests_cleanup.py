"""Tests that *create* a keychain under the real home must clean up after themselves.

Why this gate exists
--------------------
Two keychain E2E cases need to exercise the disposable-home guard's second
branch — "a home that is disposable while the keychain resolves somewhere
under the real home". The only way to reach that branch is to point a home
at the operator's own directory tree, which means the case's fake
``create-keychain`` really does create a directory and a zero-byte file
there (``temporary_keychain`` checks the file exists before yielding).

For a long time these cases did not clean up. Every run left
``<real home>/not-a-redirect/Library/Keychains/runtime-secrets.keychain-db``
behind — litter in the one directory the whole guard exists to keep its
hands out of. The files were harmless (the real tool never ran, so they
are 0 bytes and never entered any keychain search list), but a test that
writes into the operator's home and calls that acceptable is a test whose
neighbours will do worse.

What this pins
--------------
Any ``test_*`` function that calls one of the keychain-creating helpers
with a home rooted at ``_REAL_HOME`` must also *call* a cleanup helper.

Two things this deliberately does not do, each of which cost a version of
this file:

* **It does not count mentions of the path.** Four neighbouring cases
  pass ``_REAL_HOME / ...`` straight into ``_assert_disposable`` to
  assert the guard refuses them, and they create nothing. Matching the
  bare name flagged all four.
* **It does not read cleanup out of the source text.** A
  ``# shutil.rmtree(...)`` comment and a docstring naming it both leave
  the name in the text without removing anything — exactly the shape a
  temporary "comment it out while I debug" edit takes. This repository
  has already made the string-matching version of that mistake once.

It also resolves the argument rather than assuming the literal: the
guarded cases pass a local name (``temporary_keychain(litter)``) whose
assignment is the real-home path, so a check on the argument text alone
passes vacuously — which it did, until this file was pointed at its own
output.

Scope, stated honestly
----------------------
This reads one attribute of one shape of test. A test that creates a file
under the real home through some other name — not a keychain helper, not
``_REAL_HOME`` — is invisible here. The gate is the cheap one: it catches
the shape that already happened.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Repository layout — this file lives at
# ``backend/tests/static_gates/``, so the root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_E2E_DIR = _REPO_ROOT / "backend" / "tests" / "e2e"

#: The helpers that materialise a keychain. Passing a real-home path to
#: one of these is what writes into the operator's directory tree.
_CREATING_HELPERS = frozenset({"temporary_keychain", "_real_keychain"})

#: The helpers whose *call* counts as cleanup. Matched on the AST, for
#: the reason in the module docstring: a substring check is satisfied by
#: a comment, and a gate that a comment can satisfy is a gate that
#: cannot fail.
_CLEANUP_CALLS = frozenset({"rmtree", "_clean_real_home_litter"})


def _callee(node: ast.Call) -> str:
    """Return the bare name a call is made through, or ``""``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", "")


def _function_sources(path: Path) -> list[tuple[str, str]]:
    """Return ``(name, dedented source)`` for every function defined in *path*.

    The source is re-indented so it can be handed straight back to
    :func:`ast.parse` and :func:`ast.get_source_segment` together — a
    segment extracted from the original file and then re-parsed on its
    own is a different string, and a segment lookup against the wrong one
    returns ``None`` for every node inside it.
    """
    text = path.read_text()
    out: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = ast.get_source_segment(text, node)
        if segment is None:  # pragma: no cover — unparsable already fails
            continue
        lines = segment.splitlines()
        pad = len(lines[0]) - len(lines[0].lstrip()) if lines else 0
        out.append((node.name, "\n".join(ln[pad:] for ln in lines)))
    return out


def _real_home_locals(source: str) -> set[str]:
    """Return names assigned in *source* from a ``_REAL_HOME`` expression.

    The guarded cases bind ``litter = _REAL_HOME / "not-a-redirect"`` and
    then call ``temporary_keychain(litter)``. Reading the argument alone
    finds the word ``litter`` and concludes nothing reaches the real home,
    which is how this file passed while detecting nothing at all.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        value_src = ast.get_source_segment(source, node.value) or ""
        if "_REAL_HOME" not in value_src:
            continue
        names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _creates_under_real_home(source: str) -> bool:
    """True when *source* calls a creating helper rooted at the real home."""
    real_home_locals = _real_home_locals(source)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or _callee(node) not in _CREATING_HELPERS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in real_home_locals:
                return True
            if "_REAL_HOME" in (ast.get_source_segment(source, arg) or ""):
                return True
    return False


def _has_cleanup_call(source: str) -> bool:
    """True when *source* actually *calls* a cleanup helper."""
    return any(
        isinstance(node, ast.Call) and _callee(node) in _CLEANUP_CALLS
        for node in ast.walk(ast.parse(source))
    )


def _test_files() -> list[Path]:
    files = sorted(_E2E_DIR.glob("test_*keychain*.py")) + sorted(
        _E2E_DIR.glob("test_*credential*.py")
    )
    assert files, "no keychain/credential E2E files found — layout moved?"
    return files


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: p.name)
def test_real_home_touching_tests_clean_up(path: Path) -> None:
    """Any test that creates a keychain under the real home plans its return."""
    creating, cleaned = [], []
    for name, source in _function_sources(path):
        if not name.startswith("test_") or not _creates_under_real_home(source):
            continue
        creating.append(name)
        if _has_cleanup_call(source):
            cleaned.append(name)

    unrepentant = sorted(set(creating) - set(cleaned))
    assert not unrepentant, (
        f"{path.name}: these test functions create a keychain under the "
        f"operator's real home directory without calling a cleanup: "
        f"{unrepentant}. A keychain-E2E test may only point a home at the "
        f"real tree in order to exercise the disposable guard, and what "
        f"it creates there has to be removed before the test ends — see "
        f"the neighbouring tests for the pattern."
    )
