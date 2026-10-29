"""Static gate: the executor override is a test-only seam, and the default
path is the one the tests think they are exercising.

Background
----------
``PDT_EXECUTOR_CMD`` (task-26) lets a test put a deterministic stand-in where
the real executor would run.  It is a *production-side* seam, and that is
exactly what makes it dangerous: the same knob a test uses to substitute the
executor can, if it leaks into configuration, replace the executor in
production — and the E2E suite, the thing supposed to notice, would then be
measuring the stand-in it installed instead of the code it claims to cover.

The failure this gate prevents is therefore not "the override is broken".  It
is "the override quietly stopped being test-only", or "the default path was
rewritten while nobody was looking":

  * the knob is read in more than one place, so a later edit can add a second
    read that no test covers;
  * the default command line drifted away from the template the E2E suite
    assumes, so the tests spawn something other than what they think;
  * the knob's *name* leaked into ``config.yaml`` / ``.env.example`` /
    ``example/`` and became a supported production setting;
  * a second ``Popen`` started the executor somewhere else, bypassing the one
    spawn point that honours the override.

What the test does
------------------
Four static assertions.  The application's own source is read through
:mod:`tests.app_source` — never by opening a hand-picked file — so the gate
follows the code when the module moves instead of failing as "the code is
gone":

  1. :func:`test_single_read_point` — the variable is read exactly once in
     the whole production tree, that read is inside the helper, it happens
     *before* the default command line is built, and the branch it selects
     returns the split value verbatim.
  2. :func:`test_default_argv_matches_template` — the default command line is
     compared to the template **element by element on the AST**, so a
     reordered or dropped argument fails.  A substring search cannot do this:
     ``"--recover"`` still appears in a command line that lost ``-w``.
  3. :func:`test_literal_only_in_tests` — the knob's name appears only in the
     module that owns the helper and in files under the test tree.  A hit in
     ``config.y*ml`` or under ``example/`` is a failure by itself.
  4. :func:`test_no_other_executor_popen` — no ``subprocess.Popen`` outside
     the shared spawn helper builds the executor's command line, the helper
     that builds that command is called from nowhere else, and
     ``verification_loop.py`` routes its spawn through the shared helper.

Scope note
----------
The literal scan (3) walks the repository's first-party surface: the source
roots below plus every regular file at the repository root.  Local runtime
state (``tmp/``, ``.pdt/``, ``.playwright-mcp/``) is excluded on purpose —
its content is machine-dependent, so scanning it would make the gate
non-deterministic on a developer machine and green in CI for the wrong
reason.  What remains is exactly the surface an operator could configure the
product with, which is where a leaked knob would do its damage.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The repository root, one level above ``backend/``.
REPO_ROOT = BACKEND_DIR.parent

from tests import app_source as app_source_module  # noqa: E402


# ---------------------------------------------------------------------------
# The contract this gate pins
# ---------------------------------------------------------------------------

#: The environment variable that names the executor's whole command line.
EXECUTOR_CMD_ENV = "PDT_EXECUTOR_CMD"

#: The constant the helper reads that variable through.  The name is bound
#: once and read once; the gate checks both halves.
ENV_CONST_NAME = "_EXECUTOR_CMD_ENV"

#: The helper that builds the executor's command line.
HELPER_NAME = "_executor_command"

#: The shared spawn helper — the one place that starts the executor.
SPAWN_NAME = "_spawn_executor_subprocess"

#: The one production file allowed to *name* the knob: the module that owns
#: the helper reading it.
HELPER_MODULE = "backend/routes/execution.py"

#: The test tree, where naming the knob is the entire point.
TESTS_ROOT = "backend/tests"

#: The executor's entry point.  A command line that names it is the
#: executor's command line, whichever module built it.
EXECUTOR_ENTRY = "cli.py"

#: The flag that makes ``cli.py`` resume a crashed run — the executor's
#: own mode, and therefore part of the same marker.
EXECUTOR_FLAG = "--recover"

#: The default command line, written as source and parsed, so the comparison
#: is between two ASTs rather than between a list and a hand-written
#: expectation that could drift from the code without anyone noticing.
DEFAULT_ARGV_TEMPLATE_SOURCE = """
[
    sys.executable,
    "-u",
    str(backend_dir / "cli.py"),
    "--recover",
    "-w",
    str(project_dir),
    "--tasks-file",
    str(tasks_file),
]
"""

#: The ``--tool`` extension the default path appends when a tool is named.
TOOL_ARG_TEMPLATE_SOURCE = """["--tool", tool]"""


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The ids of the nodes that are docstrings.

    A docstring is prose about the code; it is not the code.  The gates
    below count *occurrences of a name in executable code*, and a docstring
    that mentions the knob is documentation, not a second read point.
    """
    ids: set[int] = set()
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _code_string_constants(tree: ast.AST) -> list[ast.Constant]:
    """Every string constant in the tree that is not a docstring."""
    docstrings = _docstring_ids(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _is_environ_read(node: ast.Call) -> bool:
    """True for ``os.environ.get(...)`` and ``os.getenv(...)``."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "get":
        owner = func.value
        return (
            isinstance(owner, ast.Attribute)
            and owner.attr == "environ"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "os"
        )
    return isinstance(func, ast.Name) and func.id == "getenv"


def _names_the_env_var(node: ast.Call) -> bool:
    """True when the call's first argument names the override variable.

    Both spellings count: through the module-level constant (what the code
    does) and as the bare literal (what a careless edit would write).
    """
    if not node.args:
        return False
    first = node.args[0]
    if isinstance(first, ast.Name) and first.id == ENV_CONST_NAME:
        return True
    return isinstance(first, ast.Constant) and first.value == EXECUTOR_CMD_ENV


def _environ_reads(tree: ast.AST) -> list[ast.Call]:
    """Every call in the tree that reads the override variable."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_environ_read(node)
        and _names_the_env_var(node)
    ]


def _env_bindings(tree: ast.AST) -> list[ast.Assign]:
    """Every assignment that binds the override's constant."""
    out: list[ast.Assign] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == ENV_CONST_NAME:
                out.append(node)
    return out


def _is_popen(node: ast.Call) -> bool:
    """True for a ``Popen`` construction, however it was imported."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "Popen"
    return isinstance(func, ast.Name) and func.id == "Popen"


def _popen_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``Popen`` construction in the tree."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_popen(node)]


def _executor_markers(call: ast.Call) -> list[str]:
    """The executor markers in a call's arguments.

    A call that names ``cli.py`` or passes ``--recover`` is building the
    executor's command line.  The markers are looked for as *constants*, so
    a comment that happens to mention ``cli.py`` cannot trip the gate.
    """
    found: list[str] = []
    for node in ast.walk(call):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        value = node.value
        if value == EXECUTOR_ENTRY or value.endswith("/" + EXECUTOR_ENTRY):
            found.append(value)
        elif value == EXECUTOR_FLAG:
            found.append(value)
    return found


def _same_node(left: ast.AST, right: ast.AST) -> bool:
    """True when two nodes denote the same place in the source.

    Compared by position, not by identity: the gate reads the module once
    through :func:`tests.app_source.find_def` and again through
    :func:`tests.app_source.app_sources`, and those are two separate
    parses of the same file — the nodes are equal but they are not the
    same object.
    """
    return (
        getattr(left, "lineno", None),
        getattr(left, "col_offset", None),
        getattr(left, "end_lineno", None),
        getattr(left, "end_col_offset", None),
    ) == (
        getattr(right, "lineno", None),
        getattr(right, "col_offset", None),
        getattr(right, "end_lineno", None),
        getattr(right, "end_col_offset", None),
    )


def _within(outer: ast.AST, inner: ast.AST) -> bool:
    """True when ``inner`` lives inside ``outer``.

    Compared by *source position*, not by object identity: the gate reads
    the module once through :func:`tests.app_source.find_def` and again
    through :func:`tests.app_source.app_sources`, and those are two
    separate parses of the same file — the nodes are equal but they are
    not the same object.  An identity check would report "the read is not
    inside the helper" for a read that plainly is.
    """
    outer_start = getattr(outer, "lineno", None)
    outer_end = getattr(outer, "end_lineno", None)
    inner_start = getattr(inner, "lineno", None)
    inner_end = getattr(inner, "end_lineno", None)
    if None in (outer_start, outer_end, inner_start, inner_end):
        return False
    return (outer_start, outer.col_offset) <= (inner_start, inner.col_offset) and (
        inner_end,
        inner.end_col_offset,
    ) <= (outer_end, outer.end_col_offset)


def _cmd_assignments(func_node: ast.AST) -> list[ast.Assign]:
    """The assignments that bind ``cmd``, inside a function."""
    out: list[ast.Assign] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "cmd":
                out.append(node)
    return out


def _cmd_list_assignments(func_node: ast.AST) -> list[ast.Assign]:
    """The assignments that give ``cmd`` a list literal, inside a function."""
    return [a for a in _cmd_assignments(func_node) if isinstance(a.value, ast.List)]


def _cmd_extend_calls(func_node: ast.AST) -> list[ast.Call]:
    """The ``cmd.extend(...)`` calls inside a function."""
    out: list[ast.Call] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "extend"):
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id == "cmd":
            out.append(node)
    return out


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    """Every call in the tree whose function is called ``name``."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            out.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            out.append(node)
    return out


def _parse_list_template(source: str) -> ast.List:
    """Parse a list literal written as source into its AST."""
    node = ast.parse(source, mode="eval").body
    assert isinstance(node, ast.List), "template must be a list literal"
    return node


def _describe(node: ast.AST) -> str:
    """A short, comparable rendering of an AST node."""
    return ast.dump(node)


# ---------------------------------------------------------------------------
# The repository-wide literal scan
# ---------------------------------------------------------------------------

#: The first-party roots the knob's name is scanned in.  Everything an
#: operator could point the product at is here: the application, the docs,
#: the example project, the CI workflows, the repo's own scripts.
_SCAN_ROOTS: tuple[str, ...] = (
    "backend",
    "docs",
    "example",
    "frontend",
    "scripts",
    "tests",
    "tools",
    ".github",
    ".claude",
)

#: Directory names that never hold first-party source: the interpreter's
#: caches, the virtualenv, ``node_modules``, and the git directory.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
)

#: Files larger than this are not scanned: a knob's name does not live in a
#: megabyte-scale blob, and reading one would make the gate slow for no
#: possible gain.
_MAX_SCAN_BYTES = 1_000_000


def _scan_files() -> list[Path]:
    """Every first-party file the knob's name is scanned in."""
    files: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
            for name in sorted(filenames):
                files.append(Path(dirpath) / name)
    for entry in sorted(REPO_ROOT.iterdir()):
        if entry.is_file():
            files.append(entry)
    return files


def _literal_hits(path: Path, needle: str) -> list[tuple[int, str]]:
    """The lines of ``path`` that contain ``needle``."""
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "\x00" in text:
        return []
    hits: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            hits.append((line_no, line.strip()))
    return hits


def _is_config_yaml(path: Path) -> bool:
    """True for a ``config.yaml`` / ``config.yml`` file."""
    name = path.name
    return name.startswith("config.") and name.endswith((".yaml", ".yml"))


def _is_under(path: Path, root_name: str) -> bool:
    """True when ``path`` lives under the repository's ``root_name``."""
    root = REPO_ROOT / root_name
    return root == path or root in path.parents


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_single_read_point() -> None:
    """The override variable is read exactly once, inside the helper, and
    the read happens before the default command line is built.

    One read point is the whole safety argument.  Two reads mean one of
    them is not covered by the tests that were written against the first,
    and a knob that is read in two places is a knob whose behaviour no
    single test can pin down.
    """
    helper = app_source_module.find_def(HELPER_NAME)
    helper_module = helper.path.relative_to(REPO_ROOT).as_posix()
    assert helper_module == HELPER_MODULE, (
        f"{HELPER_NAME} now lives in {helper_module}, not {HELPER_MODULE}. "
        "This gate names the module that owns the override; update "
        "HELPER_MODULE if the code genuinely moved."
    )

    # (1) The variable's name is bound exactly once in the production tree.
    bindings: list[tuple[Path, ast.Assign]] = []
    for path, source in app_source_module.app_sources():
        for node in _env_bindings(ast.parse(source)):
            bindings.append((path, node))
    assert len(bindings) == 1, (
        f"{ENV_CONST_NAME} is bound {len(bindings)} times in the production "
        f"tree; it must be bound exactly once:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}:{n.lineno}" for p, n in bindings)
    )
    bind_path, bind = bindings[0]
    assert bind_path == helper.path, (
        f"{ENV_CONST_NAME} is bound in {bind_path.relative_to(REPO_ROOT)}, "
        f"not in the module that owns {HELPER_NAME}."
    )
    assert isinstance(bind.value, ast.Constant) and bind.value.value == EXECUTOR_CMD_ENV, (
        f"{ENV_CONST_NAME} must be bound to the literal {EXECUTOR_CMD_ENV!r}, "
        f"got {bind.value!r}."
    )

    # (2) The variable is read exactly once in the production tree, and
    #     that read is inside the helper.
    reads: list[tuple[Path, ast.Call]] = []
    for path, source in app_source_module.app_sources():
        for node in _environ_reads(ast.parse(source)):
            reads.append((path, node))
    assert len(reads) == 1, (
        f"{EXECUTOR_CMD_ENV} is read {len(reads)} times in the production "
        f"tree; it must be read exactly once, at spawn time:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}:{n.lineno}" for p, n in reads)
    )
    read_path, read = reads[0]
    assert read_path == helper.path, (
        f"{EXECUTOR_CMD_ENV} is read in {read_path.relative_to(REPO_ROOT)}; "
        f"the only read belongs to {HELPER_NAME}."
    )
    assert _within(helper.node, read), (
        f"the read of {EXECUTOR_CMD_ENV} at line {read.lineno} is not inside "
        f"{HELPER_NAME} — a module-level read would freeze the value at "
        "import time, and a read in another function would be a second "
        "read point."
    )

    # (3) The read happens before the default command line is built.
    cmd_assignments = _cmd_list_assignments(helper.node)
    assert len(cmd_assignments) == 1, (
        f"{HELPER_NAME} must build its default command line exactly once, "
        f"found {len(cmd_assignments)} list assignments."
    )
    cmd_assignment = cmd_assignments[0]
    assert read.lineno < cmd_assignment.lineno, (
        f"the read of {EXECUTOR_CMD_ENV} (line {read.lineno}) happens after "
        f"the default command line is built (line {cmd_assignment.lineno}); "
        "the read must come first or the override cannot take effect."
    )

    # (4) The override branch returns the split value and nothing else.
    #     A stand-in for the executor is handed the arguments it needs in
    #     the value itself; appending the production arguments behind its
    #     back would hand it flags it does not understand — and the E2E
    #     run would fail on a flag rather than on the code it meant to
    #     cover.
    override_names = [
        target.id
        for node in ast.walk(helper.node)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and any(_same_node(part, read) for part in ast.walk(node.value))
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert len(override_names) == 1, (
        f"the read of {EXECUTOR_CMD_ENV} must be bound to exactly one name, "
        f"found {len(override_names)}."
    )
    override_name = override_names[0]
    branches = [
        node
        for node in ast.walk(helper.node)
        if isinstance(node, ast.If)
        and any(
            isinstance(part, ast.Name) and part.id == override_name
            for part in ast.walk(node.test)
        )
    ]
    assert len(branches) == 1, (
        f"the override must be selected by exactly one branch, found "
        f"{len(branches)}."
    )
    branch = branches[0]
    assert not [
        part
        for part in ast.walk(branch)
        if isinstance(part, ast.Name) and part.id == "cmd"
    ], (
        "the override branch must not touch 'cmd': the overridden command "
        "replaces the production one verbatim, and appending to it would "
        "hand the stand-in flags it does not understand."
    )
    returns = [node for node in ast.walk(branch) if isinstance(node, ast.Return)]
    assert len(returns) == 1, (
        f"the override branch must return exactly once, found "
        f"{len(returns)} returns."
    )
    returned = returns[0].value
    assert (
        isinstance(returned, ast.Call)
        and isinstance(returned.func, ast.Attribute)
        and returned.func.attr == "split"
        and isinstance(returned.func.value, ast.Name)
        and returned.func.value.id == "shlex"
        and len(returned.args) == 1
        and isinstance(returned.args[0], ast.Name)
        and returned.args[0].id == override_name
    ), (
        f"the override branch must return shlex.split({override_name}) "
        f"unchanged, got {ast.dump(returned)}."
    )

    # (5) The knob's *name* appears in executable code exactly once in the
    #     production tree — the binding above.  A second occurrence is a
    #     second place that knows the knob's name.
    code_literals: list[tuple[Path, ast.Constant]] = []
    for path, source in app_source_module.app_sources():
        for node in _code_string_constants(ast.parse(source)):
            if node.value == EXECUTOR_CMD_ENV:
                code_literals.append((path, node))
    assert len(code_literals) == 1, (
        f"{EXECUTOR_CMD_ENV!r} appears in executable code "
        f"{len(code_literals)} times in the production tree; it must appear "
        "once — the constant that binds it:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}:{n.lineno}" for p, n in code_literals)
    )


def test_default_argv_matches_template() -> None:
    """The default command line equals the template element by element.

    The comparison is on the AST, not on a rendered string: an element that
    was dropped, reordered or renamed has to fail.  A substring search for
    ``"--recover"`` would still pass on a command line that lost ``-w`` or
    lost the ``--tasks-file`` argument — and the executor would then be
    started with the wrong working directory or without its task file.
    """
    helper = app_source_module.find_def(HELPER_NAME)

    cmd_assignments = _cmd_list_assignments(helper.node)
    assert len(cmd_assignments) == 1, (
        f"{HELPER_NAME} must build its default command line exactly once, "
        f"found {len(cmd_assignments)} list assignments."
    )
    actual = cmd_assignments[0].value
    assert isinstance(actual, ast.List)

    expected = _parse_list_template(DEFAULT_ARGV_TEMPLATE_SOURCE)

    assert len(actual.elts) == len(expected.elts), (
        f"the default command line has {len(actual.elts)} elements, the "
        f"template has {len(expected.elts)}:\n"
        f"  actual:   {[ast.dump(e) for e in actual.elts]}\n"
        f"  template: {[ast.dump(e) for e in expected.elts]}"
    )
    for index, (got, want) in enumerate(zip(actual.elts, expected.elts)):
        assert _describe(got) == _describe(want), (
            f"element {index} of the default command line drifted.\n"
            f"  actual:   {_describe(got)}\n"
            f"  template: {_describe(want)}\n"
            f"  (line {cmd_assignments[0].lineno})"
        )

    # The default path's ``--tool`` extension is part of the same command
    # line, and is pinned the same way.
    extend_calls = _cmd_extend_calls(helper.node)
    tool_lists = [
        call.args[0]
        for call in extend_calls
        if call.args and isinstance(call.args[0], ast.List)
    ]
    assert len(tool_lists) == 1, (
        f"the default command line must append exactly one literal list "
        f"(the --tool extension), found {len(tool_lists)}."
    )
    expected_tool = _parse_list_template(TOOL_ARG_TEMPLATE_SOURCE)
    assert len(tool_lists[0].elts) == len(expected_tool.elts), (
        f"the --tool extension has {len(tool_lists[0].elts)} elements, the "
        f"template has {len(expected_tool.elts)}."
    )
    for index, (got, want) in enumerate(zip(tool_lists[0].elts, expected_tool.elts)):
        assert _describe(got) == _describe(want), (
            f"element {index} of the --tool extension drifted.\n"
            f"  actual:   {_describe(got)}\n"
            f"  template: {_describe(want)}"
        )

    # And the only other extension is the caller's own extra arguments.
    other_extensions = [
        call for call in extend_calls if not (call.args and isinstance(call.args[0], ast.List))
    ]
    assert len(other_extensions) == 1, (
        f"the default command line must extend with the caller's extra "
        f"arguments exactly once, found {len(other_extensions)}."
    )
    extra_arg = other_extensions[0].args[0]
    assert isinstance(extra_arg, ast.Name) and extra_arg.id == "extra_args", (
        "the default command line must extend with the caller's "
        f"'extra_args', got {ast.dump(extra_arg)}."
    )


def test_literal_only_in_tests() -> None:
    """The knob's name appears only in the module that owns the helper and
    in the test tree.

    A name that reaches ``config.yaml``, ``.env.example`` or ``example/``
    is a name an operator can set — and then the override is not a test
    seam any more, it is a production setting that quietly replaces the
    executor.
    """
    hits: list[tuple[Path, int, str]] = []
    scanned = _scan_files()
    for path in scanned:
        for line_no, line in _literal_hits(path, EXECUTOR_CMD_ENV):
            hits.append((path, line_no, line))

    # The scan must actually reach the surfaces this gate names.  A scan
    # that silently matched nothing would pass vacuously — and a gate that
    # cannot fail is not a gate.
    assert any(_is_config_yaml(path) for path in scanned), (
        "no config.y*ml file was scanned — the literal scan would pass "
        "vacuously."
    )
    assert any(_is_under(path, "example") for path in scanned), (
        "nothing under example/ was scanned — the literal scan would pass "
        "vacuously."
    )

    violations = [
        f"{path.relative_to(REPO_ROOT)}:{line_no}: {line}"
        for path, line_no, line in hits
        if not (
            path.relative_to(REPO_ROOT).as_posix() == HELPER_MODULE
            or path.relative_to(REPO_ROOT).as_posix().startswith(TESTS_ROOT + "/")
        )
    ]
    assert not violations, (
        f"{EXECUTOR_CMD_ENV!r} appears outside the module that owns "
        f"{HELPER_NAME} ({HELPER_MODULE}) and the test tree ({TESTS_ROOT}/). "
        "The override is a test-only seam: naming it in production code, "
        "configuration or documentation turns it into a setting an operator "
        "can use to replace the executor.\n" + "\n".join(violations)
    )

    # The two surfaces the brief names explicitly, each with its own
    # message — they are the ones an operator would actually reach for.
    config_hits = [
        f"{path.relative_to(REPO_ROOT)}:{line_no}: {line}"
        for path, line_no, line in hits
        if _is_config_yaml(path)
    ]
    assert not config_hits, (
        f"{EXECUTOR_CMD_ENV!r} must not appear in a config.y*ml file — that "
        "would make the executor's command line a supported setting.\n"
        + "\n".join(config_hits)
    )

    example_hits = [
        f"{path.relative_to(REPO_ROOT)}:{line_no}: {line}"
        for path, line_no, line in hits
        if _is_under(path, "example")
    ]
    assert not example_hits, (
        f"{EXECUTOR_CMD_ENV!r} must not appear under example/ — the example "
        "project is what an operator copies, and a knob named there is a "
        "knob that ships.\n" + "\n".join(example_hits)
    )


def test_no_other_executor_popen() -> None:
    """The executor is started in exactly one place.

    The override only covers the spawn that goes through the shared helper.
    A second ``Popen`` that builds the executor's command line would start
    a process the override cannot reach — and the E2E suite would report a
    green run of the real executor while believing it was running its
    stand-in.
    """
    spawn = app_source_module.find_def(SPAWN_NAME)
    spawn_module = spawn.path.relative_to(REPO_ROOT).as_posix()
    assert spawn_module == HELPER_MODULE, (
        f"{SPAWN_NAME} now lives in {spawn_module}, not {HELPER_MODULE}. "
        "This gate names the module that owns the spawn; update "
        "HELPER_MODULE if the code genuinely moved."
    )

    # The spawn helper constructs exactly one process...
    spawn_popens = [call for call in _popen_calls(spawn.tree) if _within(spawn.node, call)]
    assert len(spawn_popens) == 1, (
        f"{SPAWN_NAME} must construct exactly one process, found "
        f"{len(spawn_popens)}."
    )

    # ...and it is the only Popen in the module that owns it.
    module_popens = _popen_calls(spawn.tree)
    assert len(module_popens) == 1, (
        f"{spawn_module} constructs {len(module_popens)} processes; the "
        "executor is started in exactly one place."
    )

    # The process it starts is the command the helper built.  Without this,
    # the override could be read and then ignored: a hardcoded argv in the
    # Popen call would start the real executor while the tests believed
    # they had replaced it.  That is the quietest failure this gate exists
    # to prevent, and nothing else below would catch it.
    popen = spawn_popens[0]
    assert popen.args, (
        f"{SPAWN_NAME} must give its Popen a command to run."
    )
    argv = popen.args[0]
    assert isinstance(argv, ast.Name) and argv.id == "cmd", (
        f"{SPAWN_NAME} must spawn the command it built ('cmd'), got "
        f"{ast.dump(argv)} — the override would be read and then ignored."
    )
    spawn_cmd_assignments = _cmd_assignments(spawn.node)
    assert len(spawn_cmd_assignments) == 1, (
        f"{SPAWN_NAME} must bind 'cmd' exactly once, found "
        f"{len(spawn_cmd_assignments)}."
    )
    built = spawn_cmd_assignments[0].value
    assert (
        isinstance(built, ast.Call)
        and isinstance(built.func, ast.Name)
        and built.func.id == HELPER_NAME
    ), (
        f"the spawned command must come from {HELPER_NAME}(...), got "
        f"{ast.dump(built)} — the override's result would never be spawned."
    )

    # No other module builds the executor's command line.
    violations: list[str] = []
    for path, source in app_source_module.app_sources():
        if path == spawn.path:
            continue
        for call in _popen_calls(ast.parse(source)):
            markers = _executor_markers(call)
            if markers:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{call.lineno}: "
                    f"Popen with {markers!r} — the executor's command line "
                    "must be built by "
                    f"{HELPER_NAME} and started by {SPAWN_NAME} only"
                )
    assert not violations, (
        "another module starts the executor outside the shared spawn "
        "helper; the override does not reach it.\n" + "\n".join(violations)
    )

    # The helper that builds the command line is called from the spawn
    # helper and nowhere else — this is what closes the hole where a
    # second spawn point builds its argv from the helper's *result*
    # (no literal to grep for, but the same bypass).
    callers: list[tuple[Path, ast.Call]] = []
    for path, source in app_source_module.app_sources():
        for call in _calls_named(ast.parse(source), HELPER_NAME):
            if _within(spawn.node, call):
                continue
            callers.append((path, call))
    assert not callers, (
        f"{HELPER_NAME} is called from outside {SPAWN_NAME}; the executor's "
        "command line must be built in one place only:\n"
        + "\n".join(f"  {p.relative_to(REPO_ROOT)}:{c.lineno}" for p, c in callers)
    )

    # The background verification loop's spawn is one of the two call
    # sites of the shared helper, so the override covers it too.
    loop_sources = [
        (path, source)
        for path, source in app_source_module.app_sources()
        if path.name == "verification_loop.py"
    ]
    assert len(loop_sources) == 1, (
        "verification_loop.py must be one of the application's modules; it "
        f"was found {len(loop_sources)} times."
    )
    loop_path, loop_source = loop_sources[0]
    loop_tree = ast.parse(loop_source)
    assert not _popen_calls(loop_tree), (
        f"{loop_path.relative_to(REPO_ROOT)} constructs a process itself "
        f"({len(_popen_calls(loop_tree))} Popen calls); its executor spawn "
        f"must go through {SPAWN_NAME}, which is the only place the "
        "override takes effect."
    )
    assert _calls_named(loop_tree, SPAWN_NAME), (
        f"{loop_path.relative_to(REPO_ROOT)} does not call {SPAWN_NAME}; "
        "the verification loop's executor spawn must route through the "
        "shared helper."
    )
