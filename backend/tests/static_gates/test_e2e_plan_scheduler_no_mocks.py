"""Static gate: the plan-scheduler E2E directory replaces nothing.

Why this gate exists
--------------------
The defects this project exists to remove -- a queued plan that never
reaches the scheduler, an adoption that is lost across a restart, a
scheduling decision taken from two places at once -- only show up across
processes and restarts.  A test that swaps the store, the lifecycle,
the HTTP client or the process model out of the way cannot see any of
them: the double answers the question the test asked, and the question
was never the real one.  A suite of such tests is green and proves
nothing, which is the most expensive kind of green.

So the end-to-end directory is not allowed to install doubles at all.
The one thing it may substitute is the *executor* -- the process that
would otherwise call a model and cost money -- and it substitutes it
through a named, production-side seam (``PDT_EXECUTOR_CMD``) rather
than through a mock.  Everything else has to be real: the SQLite file,
the HTTP port, the child process, the exit code, the process group.

What this gate checks
---------------------
Two rules, both read off the source of the end-to-end directory rather
than out of a convention:

1. **No replacement.**  No ``mock.patch`` / ``mock.patch.object`` /
   ``monkeypatch.setattr`` in that directory may target the scheduler's
   store, the plan lifecycle, the HTTP client or the process model
   (``subprocess`` and ``Popen``).  Naming the environment and the
   configuration paths is allowed: that is how a test points the
   application at its own temporary files.
2. **No manufactured facts.**  The directory may *read* the state
   database; it may not write scheduler rows with its own SQL.  A queue
   row the test wrote itself is a fact the test made up, and a
   scheduler that reads a made-up row has demonstrated nothing.

Why the rules are read off the source
-------------------------------------
A rule that is written down but not checked is a note.  Both rules
above are checked here, on the AST of the files in the directory, so a
double that hides behind an alias, an import, or a locally built module
object is still found.

The gate also proves that it *can* fail.  A scanner that matches
nothing passes forever, and a scanner that cannot see a patched store
is worse than no scanner at all: it reports the directory as clean.
The sensitivity tests at the bottom of this file hand the detector a
known violation and require it to be reported.

Scope
-----
The directory scanned is ``backend/tests/e2e/plan_scheduler/`` -- the
end-to-end tests of the native scheduler -- together with the fixture
directory beside it, ``backend/tests/e2e/fixtures/``, whose
deterministic executor is spawned by those tests.  The older
end-to-end tests elsewhere under ``backend/tests/e2e/`` predate this
project and are not covered here.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Where the gate looks
# ---------------------------------------------------------------------------

#: Repository root.  This file lives at ``backend/tests/static_gates/``,
#: so the root is three parents up -- the same derivation the sibling
#: gates in this directory use.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The end-to-end directory this gate polices.
E2E_DIR: Path = REPO_ROOT / "backend" / "tests" / "e2e" / "plan_scheduler"

#: The fixture directory those tests spawn their stand-in executor from.
E2E_FIXTURES_DIR: Path = REPO_ROOT / "backend" / "tests" / "e2e" / "fixtures"

#: The environment variable that swaps the executor for a stand-in.  It
#: is the *only* substitution the end-to-end directory is allowed to
#: make, and it is a named production seam rather than a mock.
OVERRIDE_ENV: str = "PDT_EXECUTOR_CMD"


# ---------------------------------------------------------------------------
# The contract this gate pins
# ---------------------------------------------------------------------------

#: Modules the end-to-end directory may not replace.  A target is
#: forbidden when it names one of these, or when it names a module
#: *under* one of them (``plan_scheduler.store.SqliteSchedulerStore``
#: names ``plan_scheduler.store``).
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        # The scheduler's own storage and decision modules.
        "plan_scheduler.store",
        "plan_scheduler.queue",
        "plan_scheduler.policy",
        "plan_scheduler.dryrun",
        # The plan lifecycle -- the process boundary the E2E suite
        # claims to exercise.
        "plan_lifecycle_service",
        "routes.plan_lifecycle",
        # The HTTP client, and the libraries whose whole purpose is to
        # stand in for it.
        "httpx",
        "respx",
        "pytest_httpx",
        # The process model.
        "subprocess",
    }
)

#: A module whose own name is one of these is forbidden wherever it
#: lives, so a lifecycle module that is renamed or moved does not
#: become replaceable.
FORBIDDEN_MODULE_NAMES: frozenset[str] = frozenset({"lifecycle"})

#: Symbols that are forbidden however they are reached.
FORBIDDEN_SYMBOLS: frozenset[str] = frozenset({"Popen"})

#: Substrings that make an *opaque* target -- one the detector cannot
#: resolve to a module -- a forbidden one.  An opaque target is a
#: local name the reader cannot follow; when the name says which
#: component it is, the replacement is refused rather than waved
#: through.
FORBIDDEN_NAME_HINTS: tuple[str, ...] = (
    "store",
    "lifecycle",
    "httpx",
    "respx",
    "subprocess",
    "popen",
    "queue",
    "policy",
    "dryrun",
)

#: The call tails that install a double.
REPLACEMENT_TAILS: frozenset[str] = frozenset(
    {"patch", "patch.object", "patch.multiple", "patch.dict", "setattr"}
)

#: The call tails that change the environment rather than the code.
ENVIRONMENT_TAILS: frozenset[str] = frozenset(
    {"setenv", "delenv", "setitem", "delitem"}
)

#: A SQL statement that writes.
SQL_WRITE = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\b",
    re.IGNORECASE,
)

#: The tables whose rows are the scheduler's facts.
SCHEDULER_TABLES: tuple[str, ...] = (
    "scheduler_queue",
    "scheduler_slot",
    "scheduler_command",
)

#: The same tables, as a pattern.
SCHEDULER_TABLE = re.compile(
    r"\b(?:" + "|".join(SCHEDULER_TABLES) + r")\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# What a finding looks like
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One violation, with enough context to act on it."""

    path: Path
    line: int
    kind: str
    detail: str

    def describe(self) -> str:
        """The finding as one line a reader can act on."""
        return f"{self.path}:{self.line}: {self.kind}: {self.detail}"


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


def _scanned_files() -> list[Path]:
    """Every Python file this gate reads."""
    files: list[Path] = []
    for directory in (E2E_DIR, E2E_FIXTURES_DIR):
        if directory.is_dir():
            files.extend(sorted(p for p in directory.rglob("*.py") if p.is_file()))
    return files


# ---------------------------------------------------------------------------
# Reading the source
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.AST) -> Optional[str]:
    """The dotted name of a ``Name``/``Attribute`` chain, or ``None``.

    ``None`` means the expression is not a name at all -- a call, a
    subscript, a literal -- so there is nothing to follow.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _import_bindings(tree: ast.AST) -> dict[str, str]:
    """Map every name an import binds to the dotted path it came from.

    ``import a.b`` binds ``a``; ``from a.b import c`` binds ``c`` to
    ``a.b.c``.  Both spellings are needed, because a target written as
    ``store.SqliteSchedulerStore`` and one written as the string
    ``"plan_scheduler.store.SqliteSchedulerStore"`` name the same
    thing, and only the import map tells the two apart.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    bindings[root] = root
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                source = f"{module}.{alias.name}" if module else alias.name
                bindings[alias.asname or alias.name] = source
    return bindings


def _resolve(node: ast.AST, bindings: dict[str, str]) -> Optional[str]:
    """The dotted path of *node*, with its head resolved through imports.

    A name the module imported resolves to the path it was imported
    from; any other name resolves to itself, which is what makes an
    unreadable local name visible as such rather than as a path.
    """
    dotted = _dotted_name(node)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    if head in bindings:
        return f"{bindings[head]}.{rest}" if rest else bindings[head]
    return dotted


def _tail(path: str) -> str:
    """The last one or two components of *path*.

    ``unittest.mock.patch.object`` ends in ``patch.object`` -- the two
    components together, because ``patch`` alone would not say which
    of the patch functions was called.
    """
    parts = path.split(".")
    if len(parts) >= 2 and parts[-2] == "patch":
        return f"patch.{parts[-1]}"
    return parts[-1]


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The ids of the nodes that are docstrings.

    A docstring is prose about the code, not the code: a sentence that
    mentions a write it does not perform must not be read as one.
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


# ---------------------------------------------------------------------------
# Deciding what a target means
# ---------------------------------------------------------------------------


def _forbidden_module(path: str) -> Optional[str]:
    """The forbidden module *path* that *path* sits under, if any.

    Every prefix of *path* is tried, so ``plan_scheduler.store`` names
    the forbidden module and ``plan_scheduler.store.enqueue`` names it
    too.  A prefix is matched against the forbidden modules both
    exactly and by suffix, so a module reached as
    ``backend.routes.plan_lifecycle`` is caught as the
    ``routes.plan_lifecycle`` it is.
    """
    parts = path.split(".")
    for index in range(1, len(parts) + 1):
        prefix = ".".join(parts[:index])
        for forbidden in FORBIDDEN_MODULES:
            if prefix == forbidden or prefix.endswith("." + forbidden):
                return forbidden
        if prefix.rsplit(".", 1)[-1] in FORBIDDEN_MODULE_NAMES:
            return prefix
    return None


def _forbidden_symbol(path: str) -> Optional[str]:
    """The forbidden symbol *path* names, if any."""
    last = path.rsplit(".", 1)[-1]
    return last if last in FORBIDDEN_SYMBOLS else None


def _forbidden_hint(name: str) -> Optional[str]:
    """The component an unreadable local name says it is, if any."""
    lowered = name.lower()
    for hint in FORBIDDEN_NAME_HINTS:
        if hint in lowered:
            return hint
    return None


def _path_reason(path: str) -> Optional[str]:
    """Why *path* is a forbidden replacement target, if it is."""
    module = _forbidden_module(path)
    if module:
        return f"the target names the {module} module"
    symbol = _forbidden_symbol(path)
    if symbol:
        return f"the target names {symbol}"
    return None


def _target_argument(call: ast.Call) -> Optional[ast.AST]:
    """The expression naming what a replacement call replaces."""
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "target":
            return keyword.value
    return None


def _replacement_reason(
    call: ast.Call, bindings: dict[str, str]
) -> Optional[str]:
    """Why *call* installs a double, or ``None`` when it installs none.

    Three shapes of target, three verdicts:

    * a **string** -- the dotted path of what is replaced, checked
      against the forbidden modules;
    * an **imported name** -- resolved through the import map and
      checked the same way;
    * an **unreadable name** -- a local the reader cannot follow.  When
      the name says which component it is, the replacement is refused;
      when it does not, the environment and the configuration paths
      stay writable.

    A target that is not a name at all -- a call, a subscript -- cannot
    be shown to be anything but the code under test, so it is refused.
    """
    target = _target_argument(call)
    if target is None:
        return None

    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return _path_reason(target.value)

    resolved = _resolve(target, bindings)
    if resolved is None:
        return (
            "the target is an expression, so what it replaces cannot be "
            "shown to be anything other than the code under test"
        )

    head = resolved.split(".")[0]
    if head in bindings:
        return _path_reason(resolved)

    hint = _forbidden_hint(head)
    if hint:
        return f"the target {head!r} names the {hint} component"
    return None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _check_sql(path: Path, line: int, text: str, findings: list[Finding]) -> None:
    """Record a finding when *text* writes a scheduler row."""
    if not SQL_WRITE.search(text) or not SCHEDULER_TABLE.search(text):
        return
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else text
    findings.append(
        Finding(
            path,
            line,
            "manufactured fact",
            f"the test writes its own scheduler row: {first_line[:120]}",
        )
    )


def _sql_write_findings(path: Path, tree: ast.Module) -> list[Finding]:
    """Every string in *tree* that writes a scheduler row with its own SQL."""
    docstrings = _docstring_ids(tree)
    findings: list[Finding] = []
    handled: set[int] = set()

    # An f-string is scanned as the concatenation of its literal parts,
    # so a statement split across a substitution is still seen whole.
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        parts = [
            part
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        handled.update(id(part) for part in parts)
        _check_sql(path, node.lineno, "".join(part.value for part in parts), findings)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or id(node) in handled:
            continue
        _check_sql(path, node.lineno, node.value, findings)

    return findings


def _scan_source(path: Path, source: str) -> list[Finding]:
    """Every violation found in *source*.

    Two rules, one pass over the tree: no replacement of a scheduler
    component, and no scheduler row written by the test's own SQL.
    """
    tree = ast.parse(source, filename=str(path))
    bindings = _import_bindings(tree)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = _resolve(node.func, bindings)
        if function is None:
            continue
        tail = _tail(function)
        if tail in ENVIRONMENT_TAILS:
            continue
        if tail not in REPLACEMENT_TAILS:
            continue
        reason = _replacement_reason(node, bindings)
        if reason:
            findings.append(Finding(path, node.lineno, "replacement", reason))

    findings.extend(_sql_write_findings(path, tree))
    return findings


def _scan_tree() -> list[Finding]:
    """Every violation found in the end-to-end directory."""
    findings: list[Finding] = []
    for path in _scanned_files():
        findings.extend(_scan_source(path, path.read_text(encoding="utf-8")))
    return findings


def _findings_of(kind: str) -> list[Finding]:
    """The findings of one kind in the end-to-end directory."""
    return [finding for finding in _scan_tree() if finding.kind == kind]


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_scan_is_not_empty() -> None:
    """The gate must read something, or it passes without checking.

    A directory that moved, or a walk that stopped matching, would
    leave every rule below satisfied by an empty haystack.
    """
    files = _scanned_files()
    assert files, (
        f"no Python file was found under {E2E_DIR} or {E2E_FIXTURES_DIR}; "
        "the walk is broken and every rule below passes without checking "
        "anything"
    )


def test_no_mock_of_scheduler_components() -> None:
    """No end-to-end test replaces a scheduler component with a double.

    The store, the lifecycle, the HTTP client and the process model are
    the things the end-to-end suite exists to exercise.  A double in
    their place makes the suite measure the double.
    """
    findings = _findings_of("replacement")
    assert findings == [], (
        "the end-to-end directory must not replace what it is testing; "
        f"only {OVERRIDE_ENV} may be substituted.\n  "
        + "\n  ".join(finding.describe() for finding in findings)
    )


def test_override_var_is_allowed() -> None:
    """``PDT_EXECUTOR_CMD`` may appear: it is the one allowed substitution.

    The executor is the only thing the end-to-end suite is allowed to
    stand in for, because the real one calls a model and costs money.
    It is stood in for through a named production seam, so naming the
    variable -- in an environment, in a subprocess environment, in a
    command line -- must never be reported.
    """
    findings = _scan_source(Path("synthetic_module.py"), _override_source())
    assert findings == [], (
        f"naming {OVERRIDE_ENV} must not be reported as a replacement.\n  "
        + "\n  ".join(finding.describe() for finding in findings)
    )

    offenders: list[Finding] = []
    for path in _scanned_files():
        source = path.read_text(encoding="utf-8")
        if OVERRIDE_ENV not in source:
            continue
        offenders.extend(_scan_source(path, source))
    assert offenders == [], (
        f"a file that names {OVERRIDE_ENV} must not trip the gate.\n  "
        + "\n  ".join(finding.describe() for finding in offenders)
    )


def test_no_direct_scheduler_row_writes() -> None:
    """The end-to-end directory may read the state database, not write it.

    Writing a queue row with the test's own SQL manufactures the fact
    the scheduler is supposed to produce.  Reading the row the
    scheduler produced is the whole point.
    """
    findings = _findings_of("manufactured fact")
    assert findings == [], (
        "the end-to-end directory must not write scheduler rows itself; "
        "the rows have to be what the code under test produced.\n  "
        + "\n  ".join(finding.describe() for finding in findings)
    )


# ---------------------------------------------------------------------------
# Sensitivity: the detector has to be able to fail
# ---------------------------------------------------------------------------


def _override_source() -> str:
    """A module that names the override variable the way a test may."""
    return (
        "import os\n"
        "import subprocess\n"
        "\n"
        "\n"
        "def test_override_is_installed(monkeypatch, tmp_path):\n"
        f'    monkeypatch.setenv("{OVERRIDE_ENV}", "python3 -u executor.py")\n'
        "    env = dict(os.environ)\n"
        f'    env["{OVERRIDE_ENV}"] = "python3 -u executor.py"\n'
        '    subprocess.Popen(["python3"], env=env)\n'
    )


def test_the_detector_sees_a_mocked_store() -> None:
    """A patched store class is reported."""
    findings = _scan_source(
        Path("synthetic_module.py"),
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_it():\n"
        '    with mock.patch("plan_scheduler.store.SqliteSchedulerStore"):\n'
        "        pass\n",
    )
    assert findings, "a patched scheduler store must be reported"


def test_the_detector_sees_a_patched_process_model() -> None:
    """A patched ``Popen`` is reported."""
    findings = _scan_source(
        Path("synthetic_module.py"),
        "from unittest.mock import patch\n"
        "import subprocess\n"
        "\n"
        "\n"
        "def test_it():\n"
        '    with patch("subprocess.Popen"):\n'
        "        pass\n",
    )
    assert findings, "a patched process model must be reported"


def test_the_detector_sees_a_replaced_http_client() -> None:
    """A patched HTTP client is reported."""
    findings = _scan_source(
        Path("synthetic_module.py"),
        "import httpx\n"
        "\n"
        "\n"
        "def test_it(monkeypatch):\n"
        '    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)\n',
    )
    assert findings, "a replaced HTTP client must be reported"


def test_the_detector_sees_a_replaced_lifecycle() -> None:
    """A patched lifecycle entry point is reported."""
    findings = _scan_source(
        Path("synthetic_module.py"),
        "from unittest import mock\n"
        "\n"
        "\n"
        "def test_it():\n"
        '    mock.patch("backend.routes.plan_lifecycle.start_plan")\n',
    )
    assert findings, "a replaced lifecycle must be reported"


def test_the_detector_sees_an_opaque_store_target() -> None:
    """A local name that says it is the store is reported.

    The detector cannot follow a local name, so it cannot prove what is
    being replaced.  When the name says which component it is, the
    replacement is refused instead of waved through.
    """
    findings = _scan_source(
        Path("synthetic_module.py"),
        "def test_it(monkeypatch):\n"
        "    store = build_store()\n"
        '    monkeypatch.setattr(store, "enqueue", fake_enqueue)\n',
    )
    assert findings, "an opaque store target must be reported"


def test_the_detector_sees_a_direct_queue_insert() -> None:
    """A queue row written by the test's own SQL is reported."""
    findings = _scan_source(
        Path("synthetic_module.py"),
        "def test_it(conn):\n"
        '    conn.execute(\n'
        '        "INSERT INTO scheduler_queue (plan_id, seq) VALUES (?, ?)"\n'
        "    )\n",
    )
    assert findings, "a directly written scheduler row must be reported"


def test_the_detector_allows_the_environment_and_the_config_paths() -> None:
    """Pointing the application at temporary files is not a replacement."""
    findings = _scan_source(
        Path("synthetic_module.py"),
        "import os\n"
        "from plan_scheduler import config\n"
        "\n"
        "\n"
        "def test_it(monkeypatch, tmp_path):\n"
        '    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "s.db"))\n'
        '    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "c.yaml")\n'
        '    os.environ["PDT_EXECUTOR_CMD"] = "python3 executor.py"\n',
    )
    assert findings == [], (
        "the environment and the configuration paths may be pointed "
        "elsewhere.\n  "
        + "\n  ".join(finding.describe() for finding in findings)
    )
