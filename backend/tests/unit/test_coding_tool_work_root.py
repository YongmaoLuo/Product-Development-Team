"""2026-10-29: the coding tool must name — and publish — the checkout
it runs in.

The incident
------------
The PRD acceptance criteria for this plan ask the code-review round to
quote ``backend/capacity_wait.py`` and ``backend/agent.py`` *verbatim*.
The round answered with citations whose line numbers matched the
**main checkout** while the round was verifying the **worktree**: the
framework listed 17 ``snippet_mismatch`` (``agent.py:5804`` for
``_CAPACITY_WAIT_REASONS``, ``coding_tool.py:2333`` for
``wait = resolve_quota_wait(``; the same statements sit at
``agent.py:5566`` and ``coding_tool.py:2082`` in the tree under
verification).  Both sides were byte-for-byte correct for the tree
they were read from, and nothing in the attempt said which tree the
reviewer had opened.

The repairs before this one closed the *review* side of that gap: the
attempt runs in ``project_dir``, the report names the tree it read,
and the framework compares the two.  What was still open is the
**tool** side.  ``ClaudeCodingTool`` took a ``cwd`` that defaulted to
``None``, and ``subprocess.Popen(cwd=None)`` runs the child in the
backend server's working directory — the checkout the server was
started from, which is exactly the checkout the incident was read
from.  A tool built without a root is a tool that can read the wrong
tree, and the only thing that said so was the *absence* of a name.

The contract pinned here
------------------------
  * the working root is a **required keyword** (``cwd`` has no
    default): a tool cannot be born without one, and a call site that
    forgets it fails at once instead of running somewhere else;
  * an explicit ``cwd=None`` is refused with ``ValueError`` — the
    caller *meant* to name a checkout and named nothing;
  * a named tree is recorded in ``work_root`` as its **realpath** — the
    canonical name, so two spellings of one checkout can never be
    confused with two checkouts;
  * the realpath travels with the child process (``PDT_WORK_ROOT``),
    so anything running inside the child can assert which tree it is
    in, and it is written into the ``--settings`` payload too — the
    CLI folds its settings scopes into ``process.env`` last, so only
    the payload can be trusted against the machine's own
    ``~/.claude/settings.json``;
  * the working root never falls back to the process's own directory:
    the module reads it in exactly one place, the dotenv credential
    lookup, and that exception is pinned by a test;
  * the scan below is the gate that keeps the contract from eroding
    one call site at a time.  It scans the whole backend tree — not
    only the files a repair happens to declare — and every construction
    site of ``ClaudeCodingTool`` or ``create_coding_tool`` in it has
    to name a root.  A file that arrives with a merge can carry one,
    so the scan is a function of the tree it is pointed at: it is
    exercised against files it has never seen, because "the gate is
    green" must not be able to mean "the gate only knows about the
    offenders it already saw";
  * the two factory branches that build a tool without carrying the
    root onwards (vendor-c, opencode) are written down in the module
    and pinned by the gate, so that gap is visible instead of silent;
  * the review tool and the executor's tool are built with an explicit
    root, and that stays true.

What is deliberately *not* changed here
---------------------------------------
The answers.  This repair tightens the working root; it does not
change what the tool answers, and it does not move the waiting
decision that the PRD's criteria are about.  The last section of this
file pins that decision where it lives, because the two are easy to
confuse: the repair is about *which tree* a tool runs in, not about
*when* a provider is waited out.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, patch

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
from coding_tool import ClaudeCodingTool, create_coding_tool  # noqa: E402


#: The environment variable that carries the working root into the child.
WORK_ROOT_ENV = "PDT_WORK_ROOT"

#: The phrase the refusal has to carry.
EXPLICIT_CWD_REQUIRED = "explicit cwd required"

#: This file is the negative control for the rule "every construction
#: site names a root": it builds tools *on purpose* without one, so the
#: scan below must not read it as an offender.
_THIS_FILE = Path(__file__).resolve()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_process() -> MagicMock:
    """A stand-in for the Claude CLI process.

    Interactive mode reads the init event and then the result event;
    without them the dispatch would block on an empty stdout.
    """
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter([
        json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "00000000-0000-0000-0000-000000000000",
        }),
        json.dumps({"type": "result", "result": "ok", "is_error": False}),
    ])
    proc.stderr.read.return_value = ""
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    return proc


def _dispatch(
    settings: Path,
    *,
    cwd: str,
    ambient: Optional[Dict[str, str]] = None,
):
    """Run one dispatch and return ``(child env, written settings)``.

    The provider walk is switched off (``PDT_PROVIDER_MODE=inherit``)
    so the dispatch stays offline and deterministic: what is under test
    is the environment the child is handed, not which provider answered.
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("ANTHROPIC_")
    }
    env["PDT_PROVIDER_MODE"] = "inherit"
    env.update(ambient or {})

    with patch.dict(os.environ, env, clear=True), patch(
        "coding_tool.subprocess.Popen"
    ) as popen:
        popen.return_value = _mock_process()
        tool = ClaudeCodingTool(cwd=cwd, settings=settings)
        tool._run_claude_interactive("do something")

        child_env = popen.call_args[1]["env"]
        cmd = popen.call_args[1].get("args") or popen.call_args[0][0]
        written: Optional[Dict[str, Any]] = None
        if "--settings" in cmd:
            path = Path(cmd[cmd.index("--settings") + 1])
            written = json.loads(path.read_text(encoding="utf-8"))
            try:
                path.unlink()
            except OSError:
                pass

    return child_env, written


def _settings_template(tmp_path: Path) -> Path:
    """A minimal ``--settings`` template for the dispatch."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"env": {}, "hooks": {}}), encoding="utf-8",
    )
    return path


def _local_module_path(name: str) -> Optional[Path]:
    """The file a top-level backend module name resolves to."""
    module = _BACKEND_DIR / f"{name}.py"
    if module.exists():
        return module
    package = _BACKEND_DIR / name / "__init__.py"
    if package.exists():
        return package
    return None


def _imported_top_level_names(path: Path) -> Set[str]:
    """The top-level module names ``path`` imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import reaches into the module's own
                # package, never into another backend module.
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _import_closure(entry: Path) -> Set[str]:
    """Every backend module reachable from ``entry`` through imports."""
    seen: Set[str] = set()
    queue: List[Path] = [entry]
    while queue:
        current = queue.pop()
        for name in _imported_top_level_names(current):
            if name in seen:
                continue
            target = _local_module_path(name)
            if target is None:
                continue
            seen.add(name)
            queue.append(target)
    return seen


def _call_name(call: ast.Call) -> Optional[str]:
    """The name of the function a call calls, however it is reached."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_coding_tool_call(node: Any) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ClaudeCodingTool"
    )


def _coding_tool_calls(path: Path, function: str) -> List[ast.Call]:
    """The ``ClaudeCodingTool(...)`` calls inside one function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: List[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function:
            continue
        calls.extend(
            call for call in ast.walk(node) if _is_coding_tool_call(call)
        )
    return calls


def _names_cwd(call: ast.Call) -> bool:
    """Does this construction name a working root?"""
    return any(keyword.arg == "cwd" for keyword in call.keywords)


#: The constructions the scan watches.  ``create_coding_tool`` is the
#: factory the routes build their tools with, and ``ClaudeCodingTool``
#: is the class it builds; both take a required ``cwd``, so every call
#: site of either has to name a root.
_WATCHED_CONSTRUCTIONS = ("ClaudeCodingTool", "create_coding_tool")


def _unnamed_construction_sites(
    root: Path,
    *,
    skip: Optional[Set[Path]] = None,
) -> List[str]:
    """Every construction site under ``root`` that names no working root.

    The scan is a function of the tree it is pointed at, not of the tree
    it happens to live in: the gate below points it at the whole backend
    tree, and the test that proves the gate works points it at files the
    gate has never seen.  A leak is a leak wherever it is written — and
    the file that carries one does not have to exist today, because a
    merge can bring it in.

    The paths are reported relative to ``root``, so the message names the
    file the way a reader would name it.
    """
    skipped = {path.resolve() for path in (skip or set())}
    offenders: List[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() in skipped:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in _WATCHED_CONSTRUCTIONS:
                continue
            if not _names_cwd(node):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    return offenders


def _own_calls(node: ast.AST):
    """The calls inside a function, excluding nested functions."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            continue
        if isinstance(current, ast.Call):
            yield current
        stack.extend(ast.iter_child_nodes(current))


def _process_directory_read_sites(path: Path) -> List[str]:
    """Every read of the process's own directory, with its function.

    ``os.getcwd()`` and ``Path.cwd()`` are the two spellings; the
    function each one sits in is reported too, so an exception can be
    pinned to the place it is allowed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in _own_calls(node):
            func = call.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr in ("getcwd", "cwd"):
                name = f"{func.attr}()"
            elif isinstance(func, ast.Name) and func.id == "getcwd":
                name = "getcwd()"
            if name is not None:
                sites.append(f"{node.name}: {name}")
    return sorted(sites)


# ---------------------------------------------------------------------------
# 1. The working root is required
# ---------------------------------------------------------------------------


def test_the_working_root_is_a_required_keyword():
    """``cwd`` has no default: a tool cannot be born without a root.

    The whole point of the repair is that "no root named" is not a
    state a tool can be in.  A default would make it one — and the
    child would then run in the backend server's working directory,
    the checkout the incident was read from.
    """
    parameter = inspect.signature(ClaudeCodingTool.__init__).parameters["cwd"]

    assert parameter.default is inspect.Parameter.empty, (
        f"cwd must have no default; got {parameter.default!r}"
    )
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"cwd must be a keyword; got {parameter.kind!r}"
    )


def test_a_forgotten_cwd_fails_at_once():
    """A call site that omits ``cwd`` gets no tool at all.

    ``TypeError`` is Python's own answer to a missing argument, and it
    is the right one: the call site is broken, not the tree.  The point
    is that the failure happens *here*, at construction — the child
    process never starts, so it never runs in a directory nobody named.
    """
    with pytest.raises(TypeError):
        ClaudeCodingTool()


def test_coding_tool_rejects_none_cwd():
    """``ClaudeCodingTool(cwd=None)`` must not build a tool.

    ``None`` here means "the child inherits the backend server's own
    working directory" — the checkout the server was started from,
    which is the tree this whole repair keeps the reviewer out of.  An
    explicit ``None`` is a caller who *meant* to name a root and named
    nothing, so the constructor refuses instead of quietly running
    somewhere else.
    """
    with pytest.raises(ValueError) as excinfo:
        ClaudeCodingTool(cwd=None)

    assert str(excinfo.value) == EXPLICIT_CWD_REQUIRED, (
        f"the refusal must be the contract's own words; got {excinfo.value!r}"
    )
    assert excinfo.value.__notes__, (
        "the refusal must carry the explanation as a note, so the traceback "
        "says what to do about it"
    )


def test_an_empty_cwd_is_refused():
    """An empty path is the same mistake spelled differently.

    ``Path("").resolve()`` is the process's own directory, so an empty
    ``cwd`` is the silent fallback the explicit ``None`` is refused
    for.
    """
    with pytest.raises(ValueError) as excinfo:
        ClaudeCodingTool(cwd="   ")

    assert str(excinfo.value) == EXPLICIT_CWD_REQUIRED, (
        f"the refusal must be the contract's own words; got {excinfo.value!r}"
    )
    assert excinfo.value.__notes__, (
        "the refusal must carry the explanation as a note, so the traceback "
        "says what to do about it"
    )


def test_the_factory_rejects_none_cwd():
    """The factory refuses too — it is where the routes build tools.

    The factory used to translate ``None`` into "no root was named"
    and hand the class a tool that would run in the server's own
    directory.  A factory that cannot produce a rootless tool is the
    only way the routes are protected.
    """
    with pytest.raises(ValueError) as excinfo:
        create_coding_tool(cwd=None)

    assert str(excinfo.value) == EXPLICIT_CWD_REQUIRED, (
        f"the refusal must be the contract's own words; got {excinfo.value!r}"
    )
    assert excinfo.value.__notes__, (
        "the refusal must carry the explanation as a note, so the traceback "
        "says what to do about it"
    )


def test_the_factory_refuses_to_build_a_tool_without_a_root():
    """``create_coding_tool()`` with no arguments builds nothing.

    The factory's ``cwd`` is required, so the call fails before any
    tool exists.  What must never happen is a tool that silently
    inherits the server's directory.
    """
    with pytest.raises(TypeError):
        create_coding_tool()


def test_every_construction_site_names_a_root():
    """No call site in the tree builds a tool without naming a root.

    This is the gate that keeps the contract from eroding one call
    site at a time: a new tool built without ``cwd=`` fails here
    rather than in production, where the child would run in the
    backend server's working directory.

    (This file is exempt: it builds tools *on purpose* without a root
    to prove that the refusal works.)
    """
    offenders = _unnamed_construction_sites(_BACKEND_DIR, skip={_THIS_FILE})

    assert offenders == [], (
        f"every tool must be built with an explicit cwd; offenders: "
        f"{offenders}.  Every construction site in the tree names a root "
        f"today — the migration that got them there is finished, so a new "
        f"one that does not is a regression and not a pending repair."
    )


def test_the_gate_reports_a_new_unnamed_construction_site(tmp_path):
    """The gate finds a leak in a file it has never seen.

    The tree the gate scans grows every time the base branch is merged
    in, and a file that did not exist yesterday can arrive today
    carrying a construction site that names no root.  A gate that only
    knew the offenders it had already seen would stay green through
    that merge — which is what happened once already: the leak came in
    with the merge, and the merge was the thing that had to be undone
    before anyone noticed.

    So the scan is exercised here against files written for the
    occasion, in a directory that is not the backend tree: one that
    names no root has to be reported, and one that names its root has
    not to be.
    """
    leaked = tmp_path / "a_file_that_arrived_with_the_merge.py"
    leaked.write_text(
        "from coding_tool import ClaudeCodingTool\n"
        "\n"
        "\n"
        "def build():\n"
        "    return ClaudeCodingTool()\n",
        encoding="utf-8",
    )
    clean = tmp_path / "a_file_that_names_its_root.py"
    clean.write_text(
        "from coding_tool import ClaudeCodingTool\n"
        "\n"
        "\n"
        "def build(root):\n"
        "    return ClaudeCodingTool(cwd=root)\n",
        encoding="utf-8",
    )

    offenders = _unnamed_construction_sites(tmp_path)

    assert offenders == [f"{leaked.name}:5"], (
        "the scan must report a construction site that names no root, "
        "wherever the file lives, and must not report one that names "
        f"it; got {offenders}"
    )


def test_every_factory_branch_either_carries_the_root_or_is_pinned():
    """No branch of the factory may drop the working root quietly.

    ``create_coding_tool`` checks the root before it builds anything,
    but two of its branches — vendor-c and opencode — build a tool that
    does not *carry* that root onwards: those adapters spawn their CLI
    without ``cwd=``, so their child runs in the backend server's
    working directory.  The gap is written down in
    ``coding_tool.FACTORY_TOOLS_WITHOUT_A_WORKING_ROOT`` instead of
    being left to be discovered, and this gate is what keeps that
    written-down list honest in both directions:

      * a branch that drops the root without being listed fails here —
        a new leak cannot join the list quietly;
      * a listed tool that no longer drops the root fails here too —
        the pin has to be moved, not left behind.

    (Closing the gap means giving those two adapters a root, which in
    turn means threading one into the executor path that builds them.
    That is a separate change; until it lands the gap is pinned rather
    than left silent.)
    """
    source = (_BACKEND_DIR / "coding_tool.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    factories = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "create_coding_tool"
    ]
    assert factories, "create_coding_tool is not in coding_tool.py"

    factory = factories[0]
    built: List[Tuple[str, bool]] = []
    for node in ast.walk(factory):
        if not isinstance(node, ast.Return):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        name = _call_name(value)
        if name is None:
            continue
        built.append((name, _names_cwd(value)))

    assert built, (
        "the factory builds no tool at all — the checks below would pass "
        "on an empty set and prove nothing"
    )

    pinned = tuple(coding_tool.FACTORY_TOOLS_WITHOUT_A_WORKING_ROOT)

    unpinned = [
        name for name, carries in built
        if not carries and name not in pinned
    ]
    assert unpinned == [], (
        "a branch of the factory builds a tool without handing it the "
        f"working root and is not pinned: {unpinned}.  Either the "
        "branch hands the root over, or the gap is written down in "
        "coding_tool.FACTORY_TOOLS_WITHOUT_A_WORKING_ROOT."
    )

    stale = [
        name for name in pinned
        if not any(n == name and not carries for n, carries in built)
    ]
    assert stale == [], (
        "the pin names a tool the factory no longer builds without a "
        f"root: {stale}.  Move the pin — a pin that outlives the gap it "
        "names is a hole in the gate."
    )

    factory_source = ast.get_source_segment(source, factory) or ""
    assert "FACTORY_TOOLS_WITHOUT_A_WORKING_ROOT" in factory_source, (
        "the factory must name the pin that lists its rootless "
        "branches, so the gap is written down where the gap is"
    )


# ---------------------------------------------------------------------------
# 2. The working root never falls back to the process directory
# ---------------------------------------------------------------------------


def test_no_getcwd_fallback():
    """The module never falls back to the process's own directory.

    A working root that falls back to the process directory *is* the
    server's directory — the tree the incident was read from — and it
    is indistinguishable from a root somebody named on purpose.
    """
    sites = _process_directory_read_sites(_BACKEND_DIR / "coding_tool.py")
    offenders = [site for site in sites if site.endswith("getcwd()")]

    assert offenders == [], (
        f"the coding tool must not fall back to the process's own "
        f"directory; found {offenders}"
    )


def test_the_only_process_directory_read_is_the_dotenv_lookup():
    """One exception, pinned to the place it is allowed.

    ``_load_api_key_from_env`` reads the ``.env`` file next to the
    process — a credential lookup, deliberately (the docstring there
    explains why it does not walk up the parent chain), and it has
    nothing to do with the tree a dispatch runs in.  Any *other* read
    of the process directory would be a working-root fallback, which
    is what this repair forbids, so a new one has to be justified
    here rather than slipped in.
    """
    sites = _process_directory_read_sites(_BACKEND_DIR / "coding_tool.py")

    assert sites == ["_load_api_key_from_env: cwd()"], (
        f"the module must read the process directory in exactly one "
        f"place — the dotenv lookup; found {sites}"
    )


def test_the_published_name_is_the_name_the_module_writes():
    """The two sides agree on the name.

    A rename on one side only would leave both sides green otherwise:
    the test would keep asserting an environment variable nobody
    writes, and the module would keep writing one nobody reads.
    """
    assert coding_tool.WORK_ROOT_ENV == WORK_ROOT_ENV, (
        f"the module publishes {coding_tool.WORK_ROOT_ENV!r} but the "
        f"contract names {WORK_ROOT_ENV!r}"
    )


# ---------------------------------------------------------------------------
# 3. The realpath is recorded, and it travels with the child
# ---------------------------------------------------------------------------


def test_the_work_root_is_the_realpath_of_the_named_tree(tmp_path):
    """The recorded root is the tree's canonical name.

    Two spellings of one checkout (``/tmp`` and ``/private/tmp``) must
    not read as two checkouts, and the tool has to be able to say which
    tree it is in without resolving anything again at dispatch time.
    """
    tree = tmp_path / "checkout"
    tree.mkdir()

    tool = ClaudeCodingTool(cwd=str(tree))

    assert tool.work_root == str(tree.resolve()), (
        f"the work root must be the realpath of the named tree; got "
        f"{tool.work_root!r}, expected {str(tree.resolve())!r}"
    )
    assert tool.work_root == os.path.realpath(str(tree)), (
        f"the work root must be the realpath; got {tool.work_root!r}"
    )
    assert os.path.isabs(tool.work_root), (
        f"the work root must be absolute; got {tool.work_root!r}"
    )


def test_the_raw_cwd_is_left_alone(tmp_path):
    """The raw value still drives the child and the lock broker.

    ``self.cwd`` is what ``Popen(cwd=...)`` and
    ``socket_path(self.cwd)`` read; resolving it in place would move
    the file-lock socket of a tree whose path is a symlink.  The
    canonical name is a *new* attribute, not a rewrite of the old one.
    """
    tree = tmp_path / "checkout"
    tree.mkdir()

    tool = ClaudeCodingTool(cwd=str(tree))

    assert tool.cwd == str(tree), (
        f"the raw cwd must be left as the caller named it; got "
        f"{tool.cwd!r}"
    )


def test_the_work_root_travels_with_the_subprocess_env(tmp_path):
    """The child process is told which tree it is in.

    ``PDT_WORK_ROOT`` goes into the child's environment *and* into the
    settings payload: the CLI folds its settings scopes into the
    process environment last, so a value that only lived in the
    environment could be clobbered by the machine's own
    ``~/.claude/settings.json``.
    """
    tree = tmp_path / "checkout"
    tree.mkdir()
    settings = _settings_template(tmp_path)

    child_env, written = _dispatch(settings, cwd=str(tree))

    expected = str(tree.resolve())
    assert child_env.get(WORK_ROOT_ENV) == expected, (
        f"the child must be told which tree it runs in; got "
        f"{child_env.get(WORK_ROOT_ENV)!r}, expected {expected!r}"
    )
    assert written is not None, "the dispatch must have written a settings file"
    assert written["env"].get(WORK_ROOT_ENV) == expected, (
        f"the settings payload must carry the working root too; got "
        f"{written['env'].get(WORK_ROOT_ENV)!r}"
    )


def test_a_stale_root_is_replaced_by_this_tools_root(tmp_path):
    """The published root is this tool's root — never somebody else's.

    The server's own environment may carry a root from an earlier
    dispatch.  A tool that inherited it would name a checkout it is
    not running in, which is the exact confusion this repair removes.
    """
    tree = tmp_path / "checkout"
    tree.mkdir()
    settings = _settings_template(tmp_path)

    child_env, written = _dispatch(
        settings,
        cwd=str(tree),
        ambient={WORK_ROOT_ENV: "/some/other/checkout"},
    )

    expected = str(tree.resolve())
    assert child_env.get(WORK_ROOT_ENV) == expected, (
        f"the child must be told THIS tool's tree; got "
        f"{child_env.get(WORK_ROOT_ENV)!r}, expected {expected!r}"
    )
    assert written is not None
    assert written["env"].get(WORK_ROOT_ENV) == expected, (
        f"the settings payload must carry THIS tool's root; got "
        f"{written['env'].get(WORK_ROOT_ENV)!r}"
    )


# ---------------------------------------------------------------------------
# 4. The construction points name their root
# ---------------------------------------------------------------------------


def test_the_review_tool_is_built_with_an_explicit_root():
    """The review attempt's tools name the tree they review.

    These are the two constructions in
    ``VerificationSubAgent._execute_attempt`` — the review tool and
    the scoped tool it hands down.  A tool built without a root runs
    in the backend server's directory, which is the checkout the
    citations were read from in the incident.

    (The summarizer's tool, built in ``_summarize_vp_result``, is
    deliberately not in this set: it is dispatched with
    ``allowed_tools=[]``, so it cannot open a file at all — it only
    summarizes the log tail the caller pasted into its prompt.)
    """
    calls = _coding_tool_calls(
        _BACKEND_DIR / "verification_subagent.py", "_execute_attempt",
    )

    assert calls, "the review attempt must build a coding tool"
    for call in calls:
        assert _names_cwd(call), (
            "the review tool must be built with an explicit cwd — a tool "
            "without one inherits the backend server's working directory"
        )


def test_the_executor_tool_is_built_with_an_explicit_root():
    """The task executor's tool names the tree it works in.

    ``autonomous_coding`` builds the tool the executor drives; it must
    name the project tree explicitly, for the same reason.
    """
    calls = _coding_tool_calls(
        _BACKEND_DIR / "agent.py", "autonomous_coding",
    )

    assert calls, "the executor must build a coding tool"
    for call in calls:
        assert _names_cwd(call), (
            "the executor's tool must be built with an explicit cwd — a "
            "tool without one inherits the backend server's working "
            "directory"
        )


def test_the_route_resolves_its_coding_tool_cwd_to_a_named_tree(monkeypatch, tmp_path):
    """The routes' root rule: the plan's checkout, else its own directory.

    ``routes.phases._coding_tool_cwd`` is where the phase routes decide
    which tree their tool runs in, and the decision has exactly three
    answers:

    * the plan's ``project_dir`` — the checkout the plan's work happens
      in, and the tree the review's citations have to be read from;
    * the plan's own directory — a plan that owns no checkout owns only
      this;
    * never the backend server's own directory.  That one is the tree
      the incident was read from, and a route that lands there is a
      route that reads the wrong checkout.

    The rule was implemented before anything pinned it, which is how it
    could erode one call site at a time without a test going red.
    """
    # ``server`` is imported first on purpose: it seeds ``sys.modules``
    # and imports ``routes.phases`` itself at the bottom, so the other
    # order would re-enter a half-initialised module.
    import server  # noqa: F401
    from routes import phases

    plan_dir = tmp_path / "plan"
    checkout = tmp_path / "checkout"

    # A plan with a checkout of its own: that is the tree.  It is named
    # twice over — by the caller and by the plan's own registration —
    # and both spellings have to land on the same checkout.
    monkeypatch.setattr(server, "_get_project_dir", lambda plan_id: checkout)

    assert phases._coding_tool_cwd("plan-x", plan_dir, checkout) == str(checkout), (
        "a named project_dir is the tree the tool runs in; got "
        f"{phases._coding_tool_cwd('plan-x', plan_dir, checkout)!r}"
    )
    assert phases._coding_tool_cwd("plan-x", plan_dir) == str(checkout), (
        "the plan's own project_dir is preferred over its plan directory; "
        f"got {phases._coding_tool_cwd('plan-x', plan_dir)!r}"
    )

    # A plan with no checkout owns only its plan directory.
    monkeypatch.setattr(server, "_get_project_dir", lambda plan_id: None)

    assert phases._coding_tool_cwd("plan-x", plan_dir) == str(plan_dir), (
        "a plan without a project_dir runs in its own directory; got "
        f"{phases._coding_tool_cwd('plan-x', plan_dir)!r}"
    )

    # And never in the server's own directory, and never nowhere.
    resolved = phases._coding_tool_cwd("plan-x", plan_dir)

    assert resolved != os.getcwd(), (
        "the tool's root must never be the backend server's working "
        f"directory; got {resolved!r}"
    )
    assert resolved.strip(), (
        "the tool's root must name a tree — an empty root is the server's "
        "directory spelled differently"
    )


# ---------------------------------------------------------------------------
# 5. The waiting decision the PRD's criteria are about
# ---------------------------------------------------------------------------


def test_capacity_wait_imports_only_stdlib_and_two_leaf_modules():
    """``capacity_wait`` stands on its own — that is why there is no cycle.

    The waiting decision lives in ``backend/capacity_wait.py``.  Its
    import region names the standard library and exactly two leaf
    modules: ``dynamic_provider_concurrency`` (which classifies a
    capacity refusal) and ``provider_order`` (which knows the tiers).
    Neither imports ``coding_tool`` and neither imports ``agent``, so
    importing ``capacity_wait`` from either of them cannot close a
    loop.
    """
    imported = _imported_top_level_names(_BACKEND_DIR / "capacity_wait.py")

    leaf_modules = {
        name for name in imported
        if _local_module_path(name) is not None
    }
    assert leaf_modules == {
        "dynamic_provider_concurrency", "provider_order",
    }, (
        f"capacity_wait must import exactly the two leaf modules; got "
        f"{sorted(leaf_modules)}"
    )


def test_capacity_wait_publishes_its_public_names():
    """The module says what it offers.

    ``__all__`` is the module's own statement of its surface: the wait
    kinds, the budgets, the resolver and the exception the agent
    catches.  It is the list a reader checks to see where the waiting
    decision is allowed to live.
    """
    capacity_wait = __import__("capacity_wait")
    published = set(getattr(capacity_wait, "__all__", []))

    for name in (
        "resolve_quota_wait",
        "QuotaWait",
        "QuotaWaitExhausted",
        "capacity_wait_budget_sec",
        "capacity_wait_poll_sec",
        "WAIT_WINDOW",
        "WAIT_RATE_LIMITED",
        "WAIT_MANUAL_TOPUP",
        "WAIT_UNKNOWN",
    ):
        assert name in published, (
            f"capacity_wait must publish {name!r}; got {sorted(published)}"
        )


def test_agent_imports_the_wait_decision_from_its_own_home():
    """``agent.py`` imports the wait types from ``capacity_wait`` itself.

    Not through ``coding_tool``'s re-exports: the type the agent
    catches is then provably the type the wait raises.
    """
    source = (_BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_from_capacity_wait: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "capacity_wait":
            imported_from_capacity_wait.update(
                alias.name for alias in node.names
            )

    assert "QuotaWaitExhausted" in imported_from_capacity_wait, (
        f"agent.py must import QuotaWaitExhausted from capacity_wait; "
        f"got {sorted(imported_from_capacity_wait)}"
    )
    assert "capacity_wait_budget_sec" in imported_from_capacity_wait, (
        f"agent.py must import the wait budget from capacity_wait; got "
        f"{sorted(imported_from_capacity_wait)}"
    )


def test_both_dispatches_go_through_the_wait():
    """The two dispatches that ask a provider are wrapped in the wait.

    ``_query_with_capacity_wait`` is the single place where a capacity
    refusal turns into a wait.  Both dispatch sites go through it, so
    no other path can park a provider behind a decision of its own.
    """
    source = (_BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    wraps = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_query_with_capacity_wait"
    ]

    assert len(wraps) == 2, (
        f"the agent must wrap both dispatches in the capacity wait; "
        f"found {len(wraps)}"
    )


def test_the_wait_decision_lives_in_one_module():
    """The wait kinds are defined once — in ``capacity_wait``.

    A second definition of a wait kind anywhere else would be a second
    decision point, which is exactly what the wait's single home is
    there to prevent.
    """
    kinds = (
        "WAIT_WINDOW",
        "WAIT_RATE_LIMITED",
        "WAIT_MANUAL_TOPUP",
        "WAIT_UNKNOWN",
    )

    defined_elsewhere: List[str] = []
    for path in sorted(_BACKEND_DIR.rglob("*.py")):
        if path.name == "capacity_wait.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [
                    t.id for t in node.targets
                    if isinstance(t, ast.Name)
                ]
            elif isinstance(node, ast.AnnAssign):
                targets = [
                    node.target.id
                    if isinstance(node.target, ast.Name) else ""
                ]
            else:
                continue
            for name in targets:
                if name in kinds:
                    defined_elsewhere.append(
                        f"{path.relative_to(_BACKEND_DIR)}:{node.lineno}"
                    )

    assert defined_elsewhere == [], (
        f"the wait kinds must be defined only in capacity_wait.py; found "
        f"{defined_elsewhere}"
    )


def test_capacity_wait_still_does_not_depend_on_coding_tool_or_agent():
    """``capacity_wait`` remains free of the modules it is imported by.

    That is what makes "the capacity wait has a single decision point"
    a statement about the import graph instead of a statement about the
    reader's memory.
    """
    closure = _import_closure(_BACKEND_DIR / "capacity_wait.py")

    # The walk itself has to work, or the assertions below are vacuous.
    assert "dynamic_provider_concurrency" in closure, (
        f"the import walk found nothing; closure={closure}"
    )
    assert "provider_order" in closure, (
        f"the import walk found nothing; closure={closure}"
    )

    assert "coding_tool" not in closure, (
        f"capacity_wait must not depend on coding_tool; closure={closure}"
    )
    assert "agent" not in closure, (
        f"capacity_wait must not depend on agent; closure={closure}"
    )
