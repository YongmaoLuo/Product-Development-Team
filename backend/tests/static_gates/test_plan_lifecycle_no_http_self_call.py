"""Static gate: the plan lifecycle facade must not reach itself over HTTP.

Why this gate exists
--------------------
The unified plan lifecycle facade dispatches on **live in-process state**:
whether an execution round or a verification round is running *right now*.
That state lives only inside the resident daemon process.

If the facade reached its own endpoints over HTTP, the single process
would call itself:

* the request would queue behind the very work it is trying to stop, and
  a shutdown would wait on a request that is waiting on the shutdown;
* every caller would add a request, so the request volume would grow with
  the number of callers instead of staying flat;
* the dispatch decision would exist twice — once in the process, once in
  the request — and the two copies would drift.

So the facade calls the handlers **in process** and never opens a socket
to itself.  This gate pins that shape:

* the facade modules may not import an HTTP client (``httpx``,
  ``requests``, ``urllib.request``);
* they may not spell a URL out (``http://`` / ``https://``);
* they may not build a ``TestClient`` — driving yourself through a test
  client is still driving yourself.

The sleeping scheduler
----------------------
``backend/state_machine/services/scheduler_support.py`` holds
``SchedulerSupport.decide_tick`` — the method that answers "which plans
are due".  It has **no production caller**: the scheduler component is
asleep in this repository, and reviving it here would start plans nobody
asked for.

The tick ordering itself (reconcile → converge → backfill, plus the
minimum-runtime guard) is a **contract** for the consumer repository that
owns the runtime.  This repository ships the contract and the "do not
revive" constraint; it does not ship a tick pipeline.  Hence the two
rules this gate enforces:

* ``decide_tick`` may only be **named** inside ``backend/state_machine/``;
* ``SchedulerSupport.decide_tick`` must have **zero** call sites in
  production code.

Prose is not a violation
------------------------
``scheduler_support.py`` mentions ``decide_tick`` eight times in prose —
the module docstring, the class docstring, three method docstrings and the
comment above ``_SCHEDULABLE_PHASES``.  That prose is the evidence of the
contract, and it must survive.  Every rule here is decided on the **AST**,
so prose is invisible to it: a name written in a docstring is not a name
in the code.

Scan scope
----------
Every ``*.py`` file under ``backend/``, excluding ``tests/`` subtrees and
vendor/cache trees.  This gate file lives under ``backend/tests/``, so it
is out of scope by construction.
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

#: The two modules that make up the facade: the service that dispatches on
#: live state, and the router that is its HTTP door.  Both are scanned —
#: the door is where a stray ``httpx.post`` would be written, and the
#: service is where the dispatch decision lives.
_LIFECYCLE_MODULES: tuple[Path, ...] = (
    _BACKEND_DIR / "plan_lifecycle_service.py",
    _BACKEND_DIR / "routes" / "plan_lifecycle.py",
)

#: Directory names skipped wholesale: test subtrees plus vendor/cache trees.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", ".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"}
)

#: The only subtree allowed to *name* ``decide_tick`` — spelled relative
#: to ``backend/``, the way the scan reports its findings.
_STATE_MACHINE_PREFIX = "state_machine/"

#: The sleeping scheduler's tick method.
_DECIDE_TICK = "decide_tick"

#: HTTP client modules the facade may not import.  ``urllib.request`` is
#: spelled separately because it is the stdlib way of making the same
#: mistake.
_FORBIDDEN_HTTP_MODULES: frozenset[str] = frozenset({"httpx", "requests"})
_FORBIDDEN_URLLIB_REQUEST = "urllib.request"

#: URL schemes the facade may not spell out.
_URL_PREFIXES: tuple[str, ...] = ("http://", "https://")

#: The facade may not build a test client either.
_TEST_CLIENT_NAME = "TestClient"


class HttpViolation(NamedTuple):
    """One forbidden HTTP shape, located inside a facade module."""

    #: Module path relative to ``backend/`` (e.g. ``routes/plan_lifecycle.py``).
    module: str
    #: 1-based line of the offending statement.
    lineno: int
    #: ``"import"`` / ``"url"`` / ``"test_client"``.
    kind: str
    #: What was found (``httpx``, ``http://``, ``TestClient``).
    detail: str

    def render(self) -> str:
        """The one-line report a reader can act on."""
        if self.kind == "import":
            return f"{self.module} imports {self.detail}"
        if self.kind == "url":
            return f"{self.module}:{self.lineno} contains a {self.detail} literal"
        return f"{self.module}:{self.lineno} instantiates {self.detail}"


class DecideTickViolation(NamedTuple):
    """One forbidden ``decide_tick`` reference in production code."""

    #: Module path relative to ``backend/``.
    path: str
    #: 1-based line of the reference.
    lineno: int
    #: What was found.
    detail: str

    def render(self) -> str:
        """The one-line report a reader can act on."""
        return f"{self.path}:{self.lineno} {self.detail}"


class ModuleDecideTickScan(NamedTuple):
    """The ``decide_tick`` findings inside a single module."""

    #: References to ``decide_tick`` outside ``backend/state_machine/``.
    out_of_scope_names: list[DecideTickViolation]
    #: Call sites of ``decide_tick`` — the sleeping scheduler must have none.
    call_sites: list[DecideTickViolation]


def _iter_production_files() -> Iterator[Path]:
    """Every ``*.py`` file under ``backend/`` outside the excluded trees."""
    for dirpath, dirnames, filenames in os.walk(_BACKEND_DIR):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of the string constants that are docstrings.

    A docstring is the first statement of a module, a class or a function
    and nothing else.  Prose is not a violation of this gate, so the URL
    check skips exactly these nodes — and only these.
    """
    ids: set[int] = set()
    bodies: list[list[ast.stmt]] = []
    if isinstance(tree, ast.Module):
        bodies.append(tree.body)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            bodies.append(node.body)
    for body in bodies:
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            ids.add(id(value))
    return ids


def _imported_modules(tree: ast.AST) -> list[tuple[str, str | None, int]]:
    """Every import in the tree as ``(module, imported_name, lineno)``.

    ``import httpx`` gives ``("httpx", None, line)``;
    ``from urllib import request`` gives ``("urllib", "request", line)`` —
    the two spellings of the same mistake have to be told apart.
    """
    found: list[tuple[str, str | None, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, None, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if module:
                    found.append((module, alias.name, node.lineno))
                else:
                    found.append((alias.name, None, node.lineno))
    return found


def _forbidden_import(module: str, imported_name: str | None) -> str | None:
    """The forbidden HTTP module this import names, if it names one."""
    root = module.split(".")[0]
    if root in _FORBIDDEN_HTTP_MODULES:
        return root
    if module == _FORBIDDEN_URLLIB_REQUEST or module.startswith(
        _FORBIDDEN_URLLIB_REQUEST + "."
    ):
        return _FORBIDDEN_URLLIB_REQUEST
    if module == "urllib" and imported_name == "request":
        return _FORBIDDEN_URLLIB_REQUEST
    return None


def _url_literals(
    tree: ast.AST, docstrings: set[int]
) -> list[tuple[int, str]]:
    """Every URL-shaped string constant, with its line and its scheme.

    The constant pieces of an f-string are visited too, so
    ``f"http://host/{plan_id}"`` is caught — the scheme does not have to
    survive to the end of the literal to be a URL.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        for prefix in _URL_PREFIXES:
            if prefix in node.value:
                hits.append((node.lineno, prefix))
                break
    return hits


def _test_client_calls(tree: ast.AST) -> list[int]:
    """Lines where a ``TestClient`` is instantiated.

    ``TestClient(app)`` and ``starlette.testclient.TestClient(app)`` are
    the same move; the receiver does not change what it does.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == _TEST_CLIENT_NAME:
            hits.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == _TEST_CLIENT_NAME:
            hits.append(node.lineno)
    return hits


def _scan_module_source(module: str, source: str) -> list[HttpViolation]:
    """Every forbidden HTTP shape in one facade module."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    violations: list[HttpViolation] = []
    for imported, name, lineno in _imported_modules(tree):
        forbidden = _forbidden_import(imported, name)
        if forbidden is not None:
            violations.append(HttpViolation(module, lineno, "import", forbidden))
    for lineno, prefix in _url_literals(tree, docstrings):
        violations.append(HttpViolation(module, lineno, "url", prefix))
    for lineno in _test_client_calls(tree):
        violations.append(
            HttpViolation(module, lineno, "test_client", _TEST_CLIENT_NAME)
        )
    return violations


def _scan_lifecycle_modules() -> list[HttpViolation]:
    """Every forbidden HTTP shape in the two facade modules."""
    violations: list[HttpViolation] = []
    for path in _LIFECYCLE_MODULES:
        module = path.relative_to(_BACKEND_DIR).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace")
        violations.extend(_scan_module_source(module, source))
    return violations


def _decide_tick_name_lines(tree: ast.AST) -> list[int]:
    """Lines where the identifier ``decide_tick`` appears in code.

    Docstrings and comments are invisible here — they are not AST nodes —
    which is exactly the point: the eight prose mentions in
    ``scheduler_support.py`` are documentation, not references.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == _DECIDE_TICK:
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == _DECIDE_TICK:
            lines.append(node.lineno)
        elif isinstance(node, ast.arg) and node.arg == _DECIDE_TICK:
            lines.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == _DECIDE_TICK:
            lines.append(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == _DECIDE_TICK:
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == _DECIDE_TICK:
                    lines.append(node.lineno)
    return lines


def _decide_tick_call_lines(tree: ast.AST) -> list[int]:
    """Lines where ``decide_tick`` is *called*.

    A definition is not a call — ``def decide_tick(self)`` in
    ``scheduler_support.py`` is the method that must stay uncalled, not a
    call site.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == _DECIDE_TICK:
            lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id == _DECIDE_TICK:
            lines.append(node.lineno)
        elif (
            isinstance(func, ast.Call)
            and isinstance(func.func, ast.Name)
            and func.func.id == "getattr"
        ):
            # ``getattr(scheduler, "decide_tick")()`` — the same call
            # wearing a disguise.
            if any(
                isinstance(arg, ast.Constant) and arg.value == _DECIDE_TICK
                for arg in func.args
            ):
                lines.append(node.lineno)
    return lines


def _scan_module_decide_tick(
    relative: str, source: str, *, in_state_machine: bool
) -> ModuleDecideTickScan:
    """The ``decide_tick`` findings inside one module."""
    tree = ast.parse(source)
    out_of_scope: list[DecideTickViolation] = []
    if not in_state_machine:
        for lineno in _decide_tick_name_lines(tree):
            out_of_scope.append(
                DecideTickViolation(
                    relative,
                    lineno,
                    "names decide_tick outside backend/state_machine/",
                )
            )
    call_sites = [
        DecideTickViolation(
            relative, lineno, "calls the sleeping scheduler's decide_tick"
        )
        for lineno in _decide_tick_call_lines(tree)
    ]
    return ModuleDecideTickScan(out_of_scope, call_sites)


def _scan_decide_tick() -> ModuleDecideTickScan:
    """The ``decide_tick`` findings across the production tree."""
    out_of_scope: list[DecideTickViolation] = []
    call_sites: list[DecideTickViolation] = []
    for path in _iter_production_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(_BACKEND_DIR).as_posix()
        scan = _scan_module_decide_tick(
            relative,
            source,
            in_state_machine=relative.startswith(_STATE_MACHINE_PREFIX),
        )
        out_of_scope.extend(scan.out_of_scope_names)
        call_sites.extend(scan.call_sites)
    return ModuleDecideTickScan(out_of_scope, call_sites)


def test_lifecycle_modules_do_not_self_call_http() -> None:
    """The facade calls its handlers in process; it never calls itself."""
    # Non-vacuity: both facade modules have to be on disk, or the scan
    # below would be reading nothing at all.
    for path in _LIFECYCLE_MODULES:
        assert path.exists(), (
            f"{path.relative_to(_REPO_ROOT)} is missing — the gate is not "
            "looking at the modules it claims to cover"
        )

    violations = _scan_lifecycle_modules()
    assert not violations, (
        "the plan lifecycle facade must not reach itself over HTTP — it "
        "dispatches on live in-process state, and an HTTP round trip to "
        "this same daemon would deadlock against the work it is stopping. "
        "Call the handler in process instead:\n"
        + "\n".join(v.render() for v in violations)
    )


def test_decide_tick_has_no_production_callers() -> None:
    """The sleeping scheduler stays asleep: no production caller, no name."""
    scanned = {
        path.relative_to(_BACKEND_DIR).as_posix()
        for path in _iter_production_files()
    }
    # Non-vacuity first: the definition has to be inside the scanned tree,
    # or the assertions below would pass for the wrong reason.
    assert "state_machine/services/scheduler_support.py" in scanned, (
        "the scan never reached state_machine/services/scheduler_support.py "
        "— the gate is not looking at the production tree it claims to cover"
    )

    scan = _scan_decide_tick()
    assert not scan.out_of_scope_names, (
        "decide_tick belongs to the sleeping scheduler and may only be "
        "named inside backend/state_machine/:\n"
        + "\n".join(v.render() for v in scan.out_of_scope_names)
    )
    assert not scan.call_sites, (
        "SchedulerSupport.decide_tick has no production caller — the "
        "scheduler is asleep in this repository, and a caller would revive "
        "it:\n" + "\n".join(v.render() for v in scan.call_sites)
    )


def test_gate_detects_a_planted_http_call() -> None:
    """The shape the gate exists for is caught."""
    planted = (
        "import httpx\n"
        "\n"
        "\n"
        "def stop(plan_id: str) -> None:\n"
        "    httpx.post(\n"
        "        f'http://127.0.0.1:8000/api/plan/{plan_id}/lifecycle/stop'\n"
        "    )\n"
    )
    violations = _scan_module_source("plan_lifecycle_service.py", planted)
    rendered = [v.render() for v in violations]
    assert "plan_lifecycle_service.py imports httpx" in rendered, rendered
    assert any("http://" in r for r in rendered), rendered

    # The same call written with a plain string URL — the shape the facade
    # would really take, with no f-string to hide behind.
    planted_plain = (
        "def stop() -> None:\n"
        "    client.post(\n"
        "        'http://127.0.0.1:8000/api/plan/p1/lifecycle/stop'\n"
        "    )\n"
    )
    plain_violations = _scan_module_source(
        "plan_lifecycle_service.py", planted_plain
    )
    plain_rendered = [v.render() for v in plain_violations]
    assert any("http://" in r for r in plain_rendered), plain_rendered

    planted_client = (
        "from starlette.testclient import TestClient\n"
        "\n"
        "client = TestClient(app)\n"
    )
    client_violations = _scan_module_source(
        "plan_lifecycle_service.py", planted_client
    )
    client_rendered = [v.render() for v in client_violations]
    assert any("instantiates TestClient" in r for r in client_rendered), (
        client_rendered
    )


def test_scheduler_support_docstring_mentions_are_not_violations() -> None:
    """Prose about ``decide_tick`` is evidence of the contract, not a call."""
    path = _BACKEND_DIR / "state_machine" / "services" / "scheduler_support.py"
    source = path.read_text(encoding="utf-8", errors="replace")
    mentions = source.count(_DECIDE_TICK)
    assert mentions >= 8, (
        "the prose that documents the sleeping scheduler's contract is "
        f"expected to mention decide_tick at least eight times, found "
        f"{mentions} in {path.relative_to(_REPO_ROOT)}"
    )
    scan = _scan_module_decide_tick(
        path.relative_to(_BACKEND_DIR).as_posix(),
        source,
        in_state_machine=True,
    )
    assert scan.out_of_scope_names == [], [
        v.render() for v in scan.out_of_scope_names
    ]
    assert scan.call_sites == [], [v.render() for v in scan.call_sites]
