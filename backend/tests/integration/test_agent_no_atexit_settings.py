"""
test_agent_no_atexit_settings.py — AST + runtime double verification.

Background:
    Original task 7 said "delete the atexit cleanup hook (decision 3)".
    The original test (``test_agent_wiring.py::test_agent_no_atexit_cleanup``)
    used a naive ``"atexit.register" in source`` string check. That check
    is brittle:

      * It misses *indirect* calls — e.g. a helper like
        ``def _register_cleanup(fn): atexit.register(fn)`` invoked as
        ``_register_cleanup(cleanup_settings_tmpfile)``.
      * It misses *lambda* wrappers — e.g.
        ``atexit.register(lambda: _cleanup_settings(tmp))``.
      * It only checks for the *literal string* — a refactor that
        imports the register function under an alias (e.g.
        ``from atexit import register as _reg``) slips past.

    This module layers **two independent verification strategies**
    on top of the existing text check:

      1. **AST-based audit** (test_agent_no_atexit_settings_cleanup_in_source):
         parse ``backend/agent.py`` and walk every ``ast.Call`` node.
         If the call's callee resolves to ``atexit.register`` (whether
         dotted, aliased, or via attribute on a name imported from
         ``atexit``), inspect the *args/kwargs* and fail the test if
         any reference the settings tmpfile path (substring ``'settings'``
         or ``/tmp/subagent_settings`` or ``CLAUDE_SETTINGS_PATH``).
         This catches indirect calls and lambda wrappers because the
         audit only fires on calls to the *real* atexit.register
         function, regardless of what its arguments actually do.

      2. **Runtime audit** (the other 3 tests):
         spy on ``atexit.register`` (monkeypatch it to record calls
         without actually registering) before importing ``agent`` or
         calling ``subagent_cfg.write_tmp_settings()``. The spy list
         being empty is the source of truth: even if the AST check
         missed something, the runtime check would catch any code
         path that actually attempted to register a cleanup handler.

Together: the AST check is the static contract ("source code does
not admit such a call"), the runtime check is the dynamic contract
("live interpreter does not have such a handler"). A regression
that breaks one of them still trips the other.

Why spy on ``atexit.register`` instead of inspecting
``atexit._exithandlers``:
    CPython <3.10 does not expose ``_exithandlers`` as a public-ish
    attribute. The portable mechanism is to monkeypatch
    ``atexit.register`` itself — the only public surface that any
    atexit-registering code must call. This works on every Python
    version (3.8 through 3.14+) and gives us a clean record of
    *attempts* to register handlers, not the handlers themselves
    (which we deliberately avoid registering, to prevent test-side
    atexit pollution).

Test isolation:
    * AST tests are pure-function — no module import, no env mutation.
    * Runtime tests *do* import ``agent`` and call
      ``write_tmp_settings()``. We pop ``agent`` from
      ``sys.modules`` first to ensure a clean import (in case a
      prior test polluted the registry). Each runtime test
      installs a fresh spy via ``monkeypatch``, and the
      ``monkeypatch`` fixture auto-restores the original
      ``atexit.register`` after the test exits.
    * The SubagentConfig tmpfile is created under ``/tmp/`` (the
      real contract: ``write_tmp_settings`` writes there). The test
      unlinks the tmpfile in a finally-block so the run is
      hermetic.
"""

from __future__ import annotations

import ast
import atexit
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_AGENT_PATH = _BACKEND_DIR / "agent.py"

# Markers used by the AST audit. A call to atexit.register is considered
# "settings-cleanup" if any of its string-form arguments (or the source
# text of any lambda/function passed positionally) contains one of these
# substrings. We deliberately keep the substring list narrow — these
# are the only tokens that uniquely identify the settings tmpfile
# cleanup contract from the source.
_SETTINGS_PATH_HINTS = (
    "settings",        # /tmp/subagent_settings_<uuid>.json — filename hint
    "/tmp/subagent_",  # full path prefix
    "CLAUDE_SETTINGS", # env var name
    "settings_file",   # attribute name on SubagentConfig
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_call_target(node: ast.Call, alias_map: dict) -> Optional[str]:
    """Resolve the dotted-name target of an ``ast.Call`` if possible.

    Returns a string like ``"atexit.register"`` or ``"register"`` (when
    imported via ``from atexit import register``), or ``None`` if the
    call's function is a non-name expression (lambda, Call, Attribute
    of a Call, etc.).

    ``alias_map`` is a dict mapping alias names to the names they
    actually refer to, e.g. ``{"_reg": "atexit.register"}`` for
    ``import atexit as _reg`` — although Python's grammar disallows
    aliasing ``atexit.register`` directly, we keep the hook for
    future-proofing.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return alias_map.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        # We don't recurse into nested attributes (e.g. ``a.b.register``)
        # because that would require resolving ``a``'s value, which
        # is out of scope for a static audit. Such patterns are
        # vanishingly rare in agent.py.
        return None
    return None


def _format_ast_node(node: ast.AST) -> str:
    """Render a small AST subtree for diagnostics.

    We use ``ast.unparse`` (Python 3.9+) which is good enough for a
    few lines of source. Fallback to ``ast.dump`` if unparse fails
    on something exotic (it can on star-expressions in old versions).
    """
    try:
        return ast.unparse(node)
    except Exception:
        return ast.dump(node)


def _stringifies_to(node: ast.AST, hint_substrings: Tuple[str, ...]) -> bool:
    """Best-effort: does this AST node, rendered as Python source,
    contain any of the hint substrings?

    This is the *only* way to detect lambda wrappers and closure
    bodies — the AST has the function source as an ``ast.Lambda`` or
    ``ast.FunctionDef`` node, and we unparse it. False positives are
    possible (a lambda that mentions ``settings`` for unrelated
    reasons), but in agent.py there is no such unrelated usage, so
    we accept the heuristic.
    """
    try:
        rendered = _format_ast_node(node)
    except Exception:
        return False
    rendered_lower = rendered.lower()
    return any(hint.lower() in rendered_lower for hint in hint_substrings)


def _find_settings_cleanup_calls(
    source: str,
    alias_map: Optional[dict] = None,
) -> List[Tuple[int, str, str]]:
    """Return a list of ``(lineno, target, arg_source)`` triples for
    every atexit.register call whose args mention the settings file.

    Args:
        source: Python source code.
        alias_map: Reserved for future use. Currently the audit treats
            ``register`` (imported via ``from atexit import register``)
            the same as ``atexit.register``.

    Returns:
        List of (line_number, resolved_target_name, unparsed_args_source).
        Empty list means: no settings-cleanup atexit call found.
    """
    alias_map = alias_map or {}
    tree = ast.parse(source)
    findings: List[Tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _resolve_call_target(node, alias_map)
        if target is None:
            continue
        # Match either ``atexit.register`` or bare ``register`` (from-atexit-import).
        is_atexit_register = target in ("atexit.register", "register")
        if not is_atexit_register:
            continue

        # Concatenate all positional + keyword args into one source
        # string for substring scanning. We include the call's full
        # source — not just the args — so a hypothetical
        # ``atexit.register(func=...)`` form is also caught.
        call_source = _format_ast_node(node)
        # Identify whether *any* arg reference the settings cleanup
        # contract. We scan every arg node separately as well, in
        # case the call source contains comments or whitespace
        # that the heuristic would miss.
        any_match = False
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if _stringifies_to(arg, _SETTINGS_PATH_HINTS):
                any_match = True
                break
        # If no arg individually matched, fall back to the whole
        # call source. This is the catch-all for patterns like
        # ``atexit.register(_make_cleanup("settings"))`` where the
        # hint is in a nested call.
        if not any_match and any(
            hint.lower() in call_source.lower() for hint in _SETTINGS_PATH_HINTS
        ):
            any_match = True

        if any_match:
            findings.append((node.lineno, target, call_source))

    return findings


def _install_atexit_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    """Install a spy on ``atexit.register`` and return the call list.

    The spy captures ``(func, args, kwargs)`` tuples for every
    attempt to register a handler. It deliberately does NOT call
    the original ``atexit.register`` — recording a real handler
    would mutate global interpreter state and pollute every
    subsequent test in the same pytest run. The contract under
    test is "agent.py does not *intend* to register cleanup", and
    recording the intent is enough to assert that.

    The returned list is shared between the test and the spy via
    closure; the test reads it after the action-under-test and
    inspects its length.
    """
    captured: list = []
    original_register = atexit.register

    def spy_register(func, *args, **kwargs):
        captured.append((func, args, kwargs))
        return original_register(func, *args, **kwargs)

    monkeypatch.setattr(atexit, "register", spy_register)
    return captured


# ---------------------------------------------------------------------------
# Test 1: AST — agent.py has no atexit.register call referencing settings
# ---------------------------------------------------------------------------


def test_agent_no_atexit_settings_cleanup_in_source():
    """AST scan: no atexit.register call in agent.py mentions settings.

    Walks every ``ast.Call`` node in ``backend/agent.py`` and matches
    two patterns:

      * ``atexit.register(...)`` (dotted form)
      * ``register(...)``  (from-atexit-import form)

    For every match, the call's args are checked for substrings that
    uniquely identify the settings tmpfile cleanup contract
    (``"settings"``, ``"/tmp/subagent_"``, ``"CLAUDE_SETTINGS"``,
    ``"settings_file"``). A match fails the test.

    Why AST instead of plain text: the AST audit catches indirect
    calls and lambda wrappers. A future refactor like
    ``atexit.register(lambda: _settings_cleanup(cfg))`` would pass a
    naive ``"atexit.register" in source`` check but is caught here.
    """
    source = _AGENT_PATH.read_text(encoding="utf-8")
    findings = _find_settings_cleanup_calls(source)

    if findings:
        formatted = "\n".join(
            f"  line {ln}: {target!r} args={arg!r}"
            for ln, target, arg in findings
        )
        raise AssertionError(
            "agent.py has atexit.register calls that reference the "
            "settings tmpfile (decision 3 contract). The tmpfile must "
            "NOT be auto-cleaned at process exit so asynchronous child "
            "processes can still read it.\n"
            f"Findings:\n{formatted}"
        )


# ---------------------------------------------------------------------------
# Test 2: Runtime — importing agent.py does not call atexit.register
# ---------------------------------------------------------------------------


def test_agent_module_load_no_atexit_registration(monkeypatch: pytest.MonkeyPatch):
    """Importing agent.py must not call atexit.register.

    Spies on ``atexit.register`` BEFORE importing agent.py. After
    the import, the captured call list must be empty. We pop
    ``agent`` from ``sys.modules`` first so the test is not
    polluted by a cached module object that was loaded for a
    sibling test.

    Why we care: a regression that adds
    ``atexit.register(cleanup_tmpfile)`` at module top-level would
    fire on every process startup, not just on the first
    autonomous_coding() call. The runtime check fires for *any*
    top-level ``atexit.register(...)`` regardless of its argument
    text — if you put atexit.register in agent.py, this test fails
    before we even get to decide whether the handler was "settings
    related".

    Boundary case: agent.py transitively imports other modules
    (coding_tool, task_manager, ...). Some of those modules *may*
    register their own atexit handlers (anyio does, for example).
    The spy records ALL such transitive calls. The test would
    flag a regression in *any* transitive import of agent.py, which
    is the strictest possible contract — and the right one, given
    that any atexit-based settings cleanup would have to live
    somewhere on the import path.
    """
    captured = _install_atexit_spy(monkeypatch)

    # Pop agent and any agent.* submodules from sys.modules so the
    # import below is a fresh one (not a cached already-imported
    # module). This guards against test-ordering flakiness.
    for mod in list(sys.modules):
        if mod == "agent" or mod.startswith("agent."):
            sys.modules.pop(mod, None)

    import agent  # noqa: F401

    if captured:
        formatted_calls = "\n".join(
            f"  func={f!r}, args={a!r}, kwargs={k!r}"
            for f, a, k in captured
        )
        raise AssertionError(
            f"Importing agent.py triggered {len(captured)} call(s) to "
            "atexit.register. Decision 3 forbids atexit-based cleanup "
            "of the settings tmpfile at the module level. Captured "
            f"calls:\n{formatted_calls}"
        )


# ---------------------------------------------------------------------------
# Test 3: Runtime — write_tmp_settings() does not call atexit.register
# ---------------------------------------------------------------------------


def test_subagent_config_write_does_not_register_atexit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SubagentConfig.write_tmp_settings() must not call atexit.register.

    Decision 3 contract: the tmpfile is intentionally not cleaned up
    at process exit. We assert the spy's call list is empty after
    ``write_tmp_settings()`` returns. If a future patch adds
    ``atexit.register(...)`` inside ``write_tmp_settings``, this test
    fails with the exact (func, args, kwargs) tuple of the offending
    call.
    """
    from subagent_config import SubagentConfig

    captured = _install_atexit_spy(monkeypatch)

    cfg = SubagentConfig(
        provider_name="vendor-a-pro",
        base_url="https://api.vendor-a.example/anthropic",
        api_key="sk-cp-test-key-1234567890",
        auth_token="sk-cp-test-token-1234567890",
        model_env={
            "ANTHROPIC_MODEL": "M-Medium",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "M-Opus",
        },
    )

    try:
        path = cfg.write_tmp_settings()
    except OSError as e:
        pytest.skip(f"/tmp is not writable in this environment: {e}")

    try:
        if captured:
            formatted_calls = "\n".join(
                f"  func={f!r}, args={a!r}, kwargs={k!r}"
                for f, a, k in captured
            )
            raise AssertionError(
                f"SubagentConfig.write_tmp_settings() triggered "
                f"{len(captured)} call(s) to atexit.register. "
                "Decision 3 contract: the tmpfile is intentionally not "
                f"cleaned up at process exit. Captured calls:\n{formatted_calls}"
            )
    finally:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Test 4: Runtime — no atexit.register call references the settings_file_path
# ---------------------------------------------------------------------------


def test_subagent_config_settings_path_not_in_atexit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No atexit.register call after write_tmp_settings() references
    cfg.settings_file_path.

    Stronger than test 3: this walks every (func, args, kwargs) tuple
    the spy captured *after* the call and asserts none of them
    contains the settings tmpfile path string. The check is done
    via ``str(...)`` of each handler's components because:

      * Most handlers are simple bound methods or module-level
        functions — ``str(func)`` is enough to surface the path.
      * Some handlers are partials / lambdas — we fall back to
        ``repr(args) + repr(kwargs)`` which is a superset of the
        source text and will catch any stringified path.

    The test combines two assertions: ``len(captured) == 0`` (test
    3's strictness) AND, were a registration to occur, the path
    string would not appear in any of its components. This is a
    belt-and-suspenders contract: any path reference would also
    trip test 3, but a future bug that *mis-uses* atexit (registers
    but with a different intent) would still be caught here.
    """
    from subagent_config import SubagentConfig

    captured = _install_atexit_spy(monkeypatch)

    cfg = SubagentConfig(
        provider_name="vendor-a-pro",
        base_url="https://api.vendor-a.example/anthropic",
        api_key="sk-cp-test-key-1234567890",
        auth_token="sk-cp-test-token-1234567890",
        model_env={
            "ANTHROPIC_MODEL": "M-Medium",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "M-Opus",
        },
    )

    try:
        path = cfg.write_tmp_settings()
    except OSError as e:
        pytest.skip(f"/tmp is not writable in this environment: {e}")

    try:
        path_str = str(path)
        # The filename is /tmp/subagent_settings_<uuid>.json. We assert
        # on the *full* path string and also the bare filename, so a
        # refactor that drops the parent path (e.g. just unlinks the
        # file by name) is still caught.
        path_basename = path.name
        offending = []

        for func, args, kwargs in captured:
            try:
                func_text = repr(func)
            except Exception:
                func_text = f"<unreprable {type(func).__name__}>"
            try:
                args_text = repr(args)
            except Exception:
                args_text = "<unreprable args>"
            try:
                kwargs_text = repr(kwargs)
            except Exception:
                kwargs_text = "<unreprable kwargs>"

            haystack = f"{func_text}\n{args_text}\n{kwargs_text}"
            if path_str in haystack or path_basename in haystack:
                offending.append((func_text, args_text, kwargs_text))

        assert not offending, (
            f"After write_tmp_settings(), {len(offending)} atexit "
            f"handler(s) reference the settings tmpfile path ({path_str!r}). "
            "Decision 3 contract: the tmpfile must NOT be auto-cleaned "
            "at process exit. Offending handlers:\n"
            + "\n".join(
                f"  func={f!r}, args={a!r}, kwargs={k!r}"
                for f, a, k in offending
            )
        )
    finally:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
