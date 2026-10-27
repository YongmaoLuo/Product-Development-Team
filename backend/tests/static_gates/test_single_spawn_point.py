"""One place starts a harness process, and only that place.

Why this gate exists
--------------------
A harness process used to be started from more than one place, and
each copy was slightly different: one built the argv before it knew
which backend it was driving, another read the child's whole stream
into memory before it looked at it, another merged the environment
*after* the sandbox wrapper had already read it.  None of those
differences was visible, because the code that had them was a long
function that also resolved providers and wrote settings files.

The harness layer now has one spawn point:
:mod:`harness.runner`.  It decides the sandbox, merges the
environment, spawns, reads the stream line by line and reaps the
child — in that order, once.

That is a promise about the *shape* of the code, and a promise about
shape needs a gate.  The failure this prevents is not a crash: it is
the slow, invisible return of a second spawn point, one commit at a
time, until the order of the four steps is decided in two places and
the two places disagree.

Two rules, and they are not the same rule
-----------------------------------------
**The harness layer has exactly one spawn point.**  Every module under
``backend/harness/`` other than ``runner.py`` — the contract, the
registry, the two adapters — may not start a process at all.  There is
no allowlist for this rule and there cannot be one: an adapter that
spawns is the defect, not a legacy site.  (The runner itself is the
designated spawn point and is exempt.)

**The rest of the backend keeps its legacy spawn sites, but names
them.**  Twenty-odd modules outside the harness layer start processes
of their own — ``git``, the OS keychain, the service manager's own
children, the ``pytest --collect-only`` that enumerates the tests to
split.  Those are not harness spawns and this gate does not forbid
them; it requires each of them to be *named*, with a reason, in
:data:`ALLOWED_SPAWN_SITES`.  A new module that starts a process is
therefore a red gate until somebody writes down why it has to.

The allowlist is per *(file, API)* and not per file, and that is
deliberate.  ``backend/coding_tool.py`` is allowed to call
``subprocess.run`` — it runs the two legacy CLIs that are not
harnesses — and it is **not** allowed to call ``subprocess.Popen``.
That single omission is the task-18 outcome written down: the harness
spawn used to be a ``Popen`` in that module, and a file-level
allowlist would have let it back in.

The runner may not know which adapter it drives
-----------------------------------------------
``runner.py`` is handed an adapter and an invocation.  It reads the
adapter's ``parse_stream`` and ``classify_exit`` when they are there
and does without them when they are not.  What it may not do is ask
what the adapter *is*: an ``isinstance`` check against
``ClaudeHarness`` or ``McodeHarness`` is a backend branch in the one
module that is supposed to be backend-blind, and it is the branch
that grows a new backend's special case.  The registry is where the
backend is chosen; the runner is where it is *run*.

Why AST rather than a grep
--------------------------
``subprocess.Popen`` appears in annotations
(``proc: subprocess.Popen``), in prose, and in strings, and none of
those start a process.  A gate that flagged them would be flagging
the runner's own type hints — the very module this gate exists to
protect.  This one looks at *calls*: the node that actually starts a
process.

Why a *gate* and not just the fix
---------------------------------
The fix was four steps in one module, and it reads well.  What it does
not do is stop the next commit from adding a fifth step somewhere
else, and the next commit is written by somebody who has not read
this module's docstring.  A gate is the part that keeps the fix.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The backend directory, derived from this file's location rather than
#: from the process working directory (CI runs the suite with
#: ``working-directory: backend``).
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The harness layer, relative to :data:`BACKEND_DIR`.
HARNESS_DIR = Path("harness")

#: The one module in the harness layer that may start a process.
SPAWN_POINT = Path("harness/runner.py")

#: The adapter classes the runner may not ask about.
ADAPTER_CLASSES: frozenset[str] = frozenset({"ClaudeHarness", "McodeHarness"})

#: ``subprocess`` members that start a process.
SUBPROCESS_APIS: frozenset[str] = frozenset({
    "Popen",
    "run",
    "call",
    "check_call",
    "check_output",
})

#: ``os`` members that start a process.
OS_APIS: frozenset[str] = frozenset({
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
})

#: ``os`` member prefixes that start a process: ``os.execv``,
#: ``os.execlp``, ``os.spawnve`` and their relatives.
OS_API_PREFIXES: tuple[str, ...] = ("exec", "spawn")

#: Directory names that are never production source.
_SKIP_DIRS = frozenset({"tests", "__pycache__", ".venv", "node_modules", "logs"})

#: The legacy spawn sites outside the harness layer, each named with the
#: reason it exists.  A new one is a red gate until it is written down
#: here — which is the point: the reason is the part a reviewer needs.
#:
#: Keys are paths relative to :data:`BACKEND_DIR`; values map the API
#: that file may call to why that file calls it.  A file that calls an
#: API that is not listed for it is a violation, so the granularity is
#: the *(file, API)* pair and not the file.
ALLOWED_SPAWN_SITES: dict[str, dict[str, str]] = {
    "agent.py": {
        "subprocess.run": "the self-review's own git commands (git log, git add -A)",
    },
    "background_manager.py": {
        "subprocess.Popen": "the background manager starts the plan's own child process",
    },
    "binary_freshness.py": {
        "subprocess.run": "the freshness probe that runs the binary to see what it is",
    },
    "bounded_subprocess.py": {
        "subprocess.Popen": "the bounded-subprocess helper is itself the spawn it bounds",
    },
    "ci_process_guard.py": {
        "subprocess.run": "the ps -A probe that looks for processes this run leaked",
    },
    "claude_sandbox.py": {
        "subprocess.run": "the sandbox-exec probes that ask the kernel whether it accepts the profile",
    },
    "coding_tool.py": {
        "subprocess.run": (
            "the two legacy CLIs that are not harnesses (vendor-c, opencode); "
            "the harness spawn belongs to harness/runner.py, so subprocess.Popen "
            "is deliberately NOT allowed here"
        ),
    },
    "credentials.py": {
        "subprocess.run": "the OS keychain commands (security, the keychain helper)",
    },
    "executor.py": {
        "subprocess.Popen": "the executor's own throttled child",
    },
    "framework/task_output_validator.py": {
        "subprocess.run": "the git log that finds when a file was deleted",
    },
    "hooks/dispatcher.py": {
        "subprocess.Popen": (
            "the cmd transport spawns the plan's own configured command to "
            "deliver the hook's JSON body on stdin, in its own session so a "
            "timeout can kill the whole group; it is a hook delivery, not a "
            "harness run"
        ),
    },
    "hooks/session_send.py": {
        "subprocess.Popen": (
            "the CLI spawns the cc-connect transport to deliver a HITL "
            "decision event and reads back its exit code; it is a "
            "delivery, not a harness run"
        ),
    },
    "mcode_profiles.py": {
        "subprocess.run": "the mcode-profile listing that enumerates the operator's profiles",
    },
    "rollback_manager.py": {
        "subprocess.run": "the git commands that roll a change back",
    },
    "routes/execution.py": {
        "subprocess.Popen": "the legacy plan-execution spawn, predating the harness layer",
    },
    "secret_launcher.py": {
        "os.execv": "the launcher replaces this process image with the credential-carrying child",
    },
    "service_freshness.py": {
        "subprocess.run": "the lsof/ps freshness probes",
    },
    "service_manager.py": {
        "subprocess.run": "the service manager's own start/stop commands",
        "subprocess.Popen": "the service manager's own children",
    },
    "service_restart_agent.py": {
        "subprocess.run": "the restart agent's own probes",
        "subprocess.Popen": "the restart agent's own children",
    },
    "utils/secret_sweep.py": {
        "subprocess.run": "the ps probe that finds the live --settings paths to redact",
    },
    "verification_evidence.py": {
        "subprocess.run": (
            "the read-only git probe that fingerprints the working tree "
            "(the HEAD sha plus the hash of the porcelain status); it "
            "reads the project's own git, it is not a harness run"
        ),
    },
    "verification_loop.py": {
        "subprocess.run": "the pgrep probe that finds a verification already running",
    },
    "verification_plan_delta.py": {
        "subprocess.run": (
            "the git rev-parse that records the commit a delta's VP "
            "definitions were written at — a read-only probe of the "
            "project's HEAD, the same kind of git read the rules and the "
            "rollback manager make, and not a harness run"
        ),
    },
    "verification_rules.py": {
        "subprocess.run": "the git log a rule is evaluated against",
    },
    "vp_split.py": {
        "subprocess.run": "the pytest --collect-only that enumerates the tests to split",
    },
}


def _is_os_spawn(name: str) -> bool:
    """True when the ``os`` member *name* starts a process."""
    return name in OS_APIS or name.startswith(OS_API_PREFIXES)


def find_process_starts(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, api)`` for every process-start *call* in *source*.

    A call is what starts a process; a mention is not.  The runner's
    own annotations (``proc: subprocess.Popen``) and the prose in its
    docstring are mentions, and neither is reported.

    Both spellings of the call are recognised: the module attribute
    (``subprocess.Popen(...)``) and the imported name
    (``from subprocess import Popen`` … ``Popen(...)``).  The second
    is not in the tree today, and a gate that only understood the
    first would be a gate that a single import line could switch off.

    Args:
        source: The text of the module to scan.

    Returns:
        One ``(lineno, api)`` pair per call, in source order, where
        ``api`` is ``"subprocess.<name>"`` or ``"os.<name>"``.
    """
    tree = ast.parse(source)
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in ("subprocess", "os"):
            continue
        for alias in node.names:
            # The *original* name decides whether this is a spawn; the
            # local name (the alias, when there is one) is the name the
            # module calls it by.
            original = alias.name
            local = alias.asname or original
            if node.module == "subprocess" and original in SUBPROCESS_APIS:
                imported[local] = f"subprocess.{original}"
            elif node.module == "os" and _is_os_spawn(original):
                imported[local] = f"os.{original}"

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in imported:
            found.append((node.lineno, imported[func.id]))
            continue
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        module, name = func.value.id, func.attr
        if module == "subprocess" and name in SUBPROCESS_APIS:
            found.append((node.lineno, f"subprocess.{name}"))
        elif module == "os" and _is_os_spawn(name):
            found.append((node.lineno, f"os.{name}"))
    found.sort(key=lambda hit: hit[0])
    return found


def _dotted_name(node: ast.AST) -> str:
    """The dotted name of a reference, e.g. ``"ClaudeHarness"``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def find_adapter_type_checks(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for every ``isinstance`` against an adapter.

    Asking what an adapter *is* is a backend branch.  The runner is
    handed an adapter and an invocation and is supposed to use the
    contract; a type check against one of the two concrete adapters is
    the branch this gate forbids there.

    Args:
        source: The text of the module to scan.

    Returns:
        One ``(lineno, name)`` pair per check, in source order.
    """
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "isinstance":
            continue
        if len(node.args) != 2:
            continue
        name = _dotted_name(node.args[1])
        if name.rsplit(".", 1)[-1] in ADAPTER_CLASSES:
            found.append((node.lineno, name))
    found.sort(key=lambda hit: hit[0])
    return found


def _production_sources():
    """Yield ``(relative_path, absolute_path)`` for every production module."""
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        rel = path.relative_to(BACKEND_DIR)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield rel, path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_spawn_point() -> None:
    """The harness layer spawns in one module; every other spawn is named.

    Three claims, all of them structural:

    1. inside ``backend/harness/``, only ``runner.py`` starts a process;
    2. outside it, every process-start call is covered by an explicit
       allowlist entry that names the API as well as the file;
    3. the gate can fail — an injected spawn is reported.

    The third claim is what makes the first two worth anything: a gate
    that cannot fail is indistinguishable from a passing one.
    """
    offenders: list[str] = []

    for rel, path in _production_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        try:
            starts = find_process_starts(source)
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        if not starts:
            continue

        in_harness = HARNESS_DIR.name in rel.parts
        if in_harness:
            # Rule 1: the harness layer has exactly one spawn point.
            if rel == SPAWN_POINT:
                continue
            for lineno, api in starts:
                offenders.append(
                    f"{rel}:{lineno}: {api} — the harness layer spawns in "
                    f"{SPAWN_POINT} and nowhere else"
                )
            continue

        # Rule 2: every other spawn site is named, with its API.
        allowed = ALLOWED_SPAWN_SITES.get(str(rel), {})
        for lineno, api in starts:
            if api not in allowed:
                offenders.append(
                    f"{rel}:{lineno}: {api} — a process start outside the "
                    f"harness layer has to be named in ALLOWED_SPAWN_SITES "
                    f"with the reason it exists"
                )

    assert not offenders, (
        "a process start is not accounted for. The harness layer spawns in "
        "harness/runner.py and nowhere else; every other spawn site is a "
        "legacy one and has to be named in ALLOWED_SPAWN_SITES with the "
        "reason it exists.\n  " + "\n  ".join(offenders)
    )

    # The gate has to be able to fail.
    injected = find_process_starts(
        "import subprocess\n"
        "\n"
        "def start(argv):\n"
        "    return subprocess.Popen(argv)\n"
    )
    assert injected, (
        "the gate did not report an injected process start, so it cannot "
        "fail and the assertion above proves nothing"
    )


def test_the_gate_catches_every_process_start_shape() -> None:
    """Each spelling of a process start is reported, and mentions are not.

    The mentions matter as much as the calls: the runner's own type
    annotations (``proc: subprocess.Popen``) name the spawn without
    performing it, and a gate that flagged those would flag the very
    module this gate exists to protect.
    """
    shapes = {
        "subprocess.Popen": "import subprocess\nsubprocess.Popen(argv)\n",
        "subprocess.run": "import subprocess\nsubprocess.run(argv)\n",
        "os.system": "import os\nos.system('ls')\n",
        "os.execv": "import os\nos.execv(bin, argv)\n",
        "os.spawnv": "import os\nos.spawnv(os.P_WAIT, bin, argv)\n",
        "imported Popen": "from subprocess import Popen\nPopen(argv)\n",
        "aliased Popen": "from subprocess import Popen as spawn\nspawn(argv)\n",
    }
    for label, source in shapes.items():
        assert find_process_starts(source), (
            f"the gate did not report the {label} shape"
        )

    allowed = {
        "annotation": "def f(proc: subprocess.Popen) -> None:\n    pass\n",
        "docstring": '"""The runner calls subprocess.Popen."""\n',
        "attribute read": "spawner = subprocess.Popen\n",
    }
    for label, source in allowed.items():
        assert not find_process_starts(source), (
            f"the gate reported the {label} shape"
        )


def test_the_adapter_modules_do_not_start_a_process() -> None:
    """An adapter builds a command line; the runner starts it.

    The two adapters are the modules that know what a backend's command
    line looks like.  A process start in one of them would put the
    spawn back into the layer the runner owns, and it would be a spawn
    whose ordering the runner cannot see.
    """
    offenders: list[str] = []
    for adapter in ("harness/claude.py", "harness/mcode.py"):
        path = BACKEND_DIR / adapter
        assert path.is_file(), f"{adapter} is missing, so the gate would scan nothing"
        starts = find_process_starts(path.read_text(encoding="utf-8"))
        for lineno, api in starts:
            offenders.append(f"{adapter}:{lineno}: {api}")

    assert not offenders, (
        "an adapter module starts a process. The adapter builds the argv; "
        "harness/runner.py starts the child, so that the sandbox decision, "
        "the environment merge and the spawn stay in one order.\n  "
        + "\n  ".join(offenders)
    )


def test_the_runner_does_not_ask_what_the_adapter_is() -> None:
    """The runner is backend-blind.

    An ``isinstance`` check against ``ClaudeHarness`` or
    ``McodeHarness`` inside the runner is a backend branch in the one
    module that must not have one: the registry chooses the backend,
    the runner runs whatever it is handed.
    """
    runner = BACKEND_DIR / SPAWN_POINT
    assert runner.is_file(), f"{SPAWN_POINT} is missing, so the gate would scan nothing"
    checks = find_adapter_type_checks(runner.read_text(encoding="utf-8"))
    assert not checks, (
        "the runner asks what the adapter is. It is handed an adapter and "
        "an invocation and uses the contract; the backend is chosen by "
        "harness.registry. A type check here is a second selection "
        "point.\n  "
        + "\n  ".join(f"{SPAWN_POINT}:{lineno}: isinstance(..., {name})" for lineno, name in checks)
    )


def test_the_spawn_point_actually_spawns() -> None:
    """The designated spawn point must still spawn.

    Without this, a gate that forbade *every* process start in the
    backend would pass, and the harness would have no way to run at
    all.  The exemption for ``runner.py`` is only worth having while
    the exemption is used.
    """
    runner = BACKEND_DIR / SPAWN_POINT
    starts = find_process_starts(runner.read_text(encoding="utf-8"))
    assert starts, (
        f"{SPAWN_POINT} starts no process, so the harness cannot run; the "
        "exemption this gate grants it is not being used"
    )


def test_the_allowlist_names_no_harness_module() -> None:
    """The allowlist may not excuse the harness layer.

    The harness layer's rule has no exceptions on purpose.  An
    allowlist entry for a harness module would turn "there is exactly
    one spawn point" into "there is one spawn point plus whatever the
    allowlist says", which is the rule this gate exists to keep.
    """
    offenders = [
        f"{name}: {apis}"
        for name, apis in ALLOWED_SPAWN_SITES.items()
        if Path(name).parts and Path(name).parts[0] == HARNESS_DIR.name
    ]
    assert not offenders, (
        "ALLOWED_SPAWN_SITES names a harness module. The harness layer has "
        "exactly one spawn point and no allowlist.\n  "
        + "\n  ".join(offenders)
    )


def test_every_allowlist_entry_is_used_and_explained() -> None:
    """No stale entries, and no entry without a reason.

    A stale entry is a hole: the file stops calling that API, the
    entry stays, and the next person to write the API finds it already
    allowed.  An entry without a reason is a hole with a worse shape,
    because nobody can tell whether it is still true.
    """
    used: set[tuple[str, str]] = set()
    for rel, path in _production_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        try:
            starts = find_process_starts(source)
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        for _, api in starts:
            used.add((str(rel), api))

    stale = [
        f"{name}: {api}"
        for name, apis in ALLOWED_SPAWN_SITES.items()
        for api in apis
        if (name, api) not in used
    ]
    assert not stale, (
        "ALLOWED_SPAWN_SITES names a (file, API) pair that no longer "
        "exists. A stale entry is a hole: the API comes back and finds "
        "itself already allowed.\n  " + "\n  ".join(stale)
    )

    unexplained = [
        f"{name}: {api}"
        for name, apis in ALLOWED_SPAWN_SITES.items()
        for api, reason in apis.items()
        if not reason.strip()
    ]
    assert not unexplained, (
        "ALLOWED_SPAWN_SITES has an entry without a reason. The reason is "
        "the part a reviewer needs; an entry without one cannot be "
        "checked.\n  " + "\n  ".join(unexplained)
    )


def test_the_gate_reads_the_real_tree() -> None:
    """The scan target must be the backend, and it must not be empty.

    A gate whose scan root has moved reads nothing and passes
    vacuously, which is how the original defect shipped: the paths
    were there and the check was not running.
    """
    modules = list(_production_sources())
    assert modules, (
        "the backend production tree came back empty, so the gate would "
        "scan nothing and pass vacuously"
    )
    assert any(rel == SPAWN_POINT for rel, _ in modules), (
        f"{SPAWN_POINT} is not in the scanned tree, so the spawn point "
        "would not be exempted"
    )
