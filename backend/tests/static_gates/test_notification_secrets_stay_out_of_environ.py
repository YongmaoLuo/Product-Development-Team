"""Only the credentials provider may read a secret out of ``os.environ``.

Why this gate exists
--------------------
Two of the notification transports authenticate with a secret, and
``backend/credentials.py`` exists to decide where that secret comes from:
the OS keychain where the machine has one, the environment only where it
does not. The environment is the weaker of the two for reasons that are
not fussiness — a variable is readable by every process of every user on
the machine, it is inherited by every child, and it turns up in shell
history, in process listings and in crash reports.

"The secret does not live in ``os.environ``" is therefore the property
the provider was written to establish, and a property that lives in a
module docstring is held by nothing. The way it decays is quiet: a call
site adds one fallback read, the read works, notifications keep working,
and the migration is half finished with no signal that anything changed.
This gate is the signal.

The rule
--------
Exactly one module in ``backend/`` may read either secret key out of the
environment: the provider. Every other module reaches a secret through
``credentials.read_secret`` / ``credentials.secret_source``.

The judgement is made on the key that is **read**, parsed out of the
AST, and never on a substring of the file. That distinction is the test
of the rule, in both directions:

* ``os.environ.get("TELEGRAM_BOT_TOKEN")`` is the secret, and is flagged.
* ``os.environ.get("TELEGRAM_CHAT_ID")`` is the keychain *index* — a
  value with no power of its own — and is not.
* ``os.environ.get(f"TELEGRAM_CHAT_ID_{plan_id}")`` is per-plan routing.
  A key that is only a prefix at compile time is not the secret, and a
  gate that flagged it would be flagging the deployment's own
  configuration.
* ``os.environ.get("PDT_SECRET_FD_FEISHU_APP_SECRET")`` names a pipe
  descriptor rather than a credential, and is not — even though the
  secret's name sits inside the string. A substring gate fires on that,
  and a gate that fires on correct code gets switched off.

Two conditions come from the same table and are asserted here rather
than left to review. The provider imports nothing outside the standard
library, and it imports nothing from the notifier package: a
credentials lookup that pulled in an HTTP client could not be used by
the code that *populates* the credentials, so the dependency has to run
one way.

Whitelist
---------
Two files are exempt, and each exemption carries a reason that
:func:`test_whitelist_entries_carry_a_reason` requires to be non-empty.
An exemption with no stated reason is a rule nobody decided, and it
grows: the second one added is always the first one that was not
explained.

The scan target
---------------
``backend/``, Python files only, ``tests/`` excluded. Excluding the
tests is not leniency — this file holds the five forbidden samples
verbatim, and a scan that read them would flag its own fixtures, which
is how a gate comes to need an allowlist for itself.

Why the gate proves it can fail
-------------------------------
:func:`test_the_five_forms_are_flagged` feeds the detector the five
spellings the rule names and requires a hit on each;
:func:`test_the_five_allowed_forms_are_not_flagged` does the same for
the five near misses. A detector that reports nothing is worse than no
gate at all: it is a green check reading "no secret is left in the
environment" on a machine that still exports one.

What this gate cannot see
-------------------------
Two shapes, stated so a reader does not assume more than is there. A
mapping bound to a local name first — ``env = os.environ`` followed by
``env.get("TELEGRAM_BOT_TOKEN")`` — is not recognised, because the
binding is a statement about a name and the rule is a statement about a
key. And a key assembled at runtime is not a key the source names, so
neither form is judged; the point of the rule is that the *call site*
does not reach for the environment at all, and both of these are
evidence that somebody decided to.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import sysconfig
from collections.abc import Iterator
from pathlib import Path

import credentials

# Locate the shared first-party source walker, the way the sibling gates
# in this directory do: the package is colocated with this file, and
# pytest's own import-mode insertion is not something to depend on.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan  # noqa: E402  (post-sys.path adjustment)

#: ``backend/``, derived from this file's own location — never written
#: out literally, because the checkout path is a different one on every
#: machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The module the rule exempts, and the modules the migration touched.
#: Both are resolved from this file's own location for the same reason.
CREDENTIALS_PATH = BACKEND_DIR / "credentials.py"

#: The three notifier modules the provider was introduced for. They are
#: the files most likely to grow a "just read the variable here" line
#: back, and their presence in the scan set is asserted rather than
#: assumed: a scan that quietly stopped covering them would still be
#: green.
NOTIFICATION_MODULES: tuple[Path, ...] = (
    Path("backend/notifications/feishu_client.py"),
    Path("backend/notifications/feishu_notifier.py"),
    Path("backend/notifications/telegram_client.py"),
)


# ---------------------------------------------------------------------------
# The keys
# ---------------------------------------------------------------------------


#: The two environment variables that hold a *secret* rather than an
#: index, read from the provider's own spec table instead of being typed
#: here. A second copy of the key names is a second source of truth for
#: the same two facts, and it is the copy that goes wrong quietly: a row
#: is renamed in the table and the copy keeps matching the old spelling.
#: The set comprehension is why adding a secret to the table extends this
#: gate without anyone remembering to extend it.
def _secret_env_keys() -> frozenset[str]:
    return frozenset(
        spec.fallback_env_key for spec in credentials.SECRET_SPECS.values()
    )


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

#: The two methods that name a single key. Both take it as the first
#: positional argument, so one extraction serves them.
_KEYED_METHODS = frozenset({"get", "setdefault"})

#: The package the provider must not depend on. Named, not derived from a
#: path, because the rule is about the direction of the dependency.
_NOTIFICATIONS_PACKAGE = "notifications"


def _constant_str(node: ast.AST) -> str | None:
    """Return *node*'s value when it is a string literal, else None.

    A bare name, an attribute, an f-string or a concatenation is not a
    constant, and none of them is a match. That is deliberate rather than
    a limitation to apologise for: the only module allowed to read a
    secret key indirectly is the provider, and the exemption is spelled
    by path rather than guessed at by trying to fold expressions.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_os_name(node: ast.AST) -> bool:
    """Return whether *node* is the name ``os`` itself."""
    return isinstance(node, ast.Name) and node.id == "os"


def _is_os_environ(node: ast.AST) -> bool:
    """Return whether *node* is the ``os.environ`` mapping itself."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and _is_os_name(node.value)
    )


def _is_os_call(node: ast.AST, name: str) -> bool:
    """Return whether *node* is ``os.<name>`` — the attribute form only."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and _is_os_name(node.value)
    )


def _first_key_argument(node: ast.Call) -> str | None:
    """Return the first positional argument of *node* as a string literal."""
    if not node.args:
        return None
    return _constant_str(node.args[0])


def _literal_keys(node: ast.AST) -> Iterator[str]:
    """Yield every string-literal key in a mapping-literal *node*.

    Handles the two spellings a mapping literal arrives in: as the
    argument itself (``update({...})``) and behind a star
    (``update(**{...})``, which the parser hands over as a keyword with
    no name — the caller in :func:`secret_env_hits` is what looks inside
    that one). A mapping that arrives as a variable yields nothing: the
    rule is judged on what the source says, and a name says nothing
    about which keys it holds.
    """
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if key is not None:
                literal = _constant_str(key)
                if literal is not None:
                    yield literal
    elif isinstance(node, ast.Starred):
        yield from _literal_keys(node.value)


def secret_env_hits(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Return ``(lineno, shape, key)`` for every secret read or write.

    The five shapes the rule names, and nothing else:

    * ``os.environ.get(KEY)`` — read one key by name;
    * ``os.environ[KEY] = value`` — write one key by name. The
      subscript is matched whether it is stored to or loaded from, so a
      read written as a subscript is the same node and the same finding;
    * ``os.environ.setdefault(KEY, default)`` — read-or-write one key;
    * ``os.environ.update({KEY: value})`` — write a mapping whose keys
      are literals;
    * ``os.getenv(KEY)`` — the module-level spelling of ``get``.

    The key is reported next to the shape rather than as the whole
    expression: a failing gate has to name the variable to be fixable,
    and a variable's *name* is not its secret. The value never reaches
    this function.
    """
    secret_keys = _secret_env_keys()
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and _is_os_environ(func.value):
                method = func.attr
                if method in _KEYED_METHODS:
                    key = _first_key_argument(node)
                    if key in secret_keys:
                        hits.append(
                            (node.lineno, f"os.environ.{method}", key)
                        )
                elif method == "update":
                    # ``os._Environ.update`` takes a mapping, ``**kwargs``
                    # and a starred mapping, so all three are read: the
                    # starred one reaches the parser as a keyword with no
                    # name, and a caller who wrote it that way has not
                    # hidden anything.
                    keys = [
                        key for arg in node.args for key in _literal_keys(arg)
                    ]
                    for keyword in node.keywords:
                        if keyword.arg is not None:
                            keys.append(keyword.arg)
                        else:
                            keys.extend(_literal_keys(keyword.value))
                    hits.extend(
                        (node.lineno, "os.environ.update", key)
                        for key in keys
                        if key in secret_keys
                    )
            elif _is_os_call(func, "getenv"):
                key = _first_key_argument(node)
                if key in secret_keys:
                    hits.append((node.lineno, "os.getenv", key))
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            key = _constant_str(node.slice)
            if key in secret_keys:
                hits.append((node.lineno, "os.environ[...]", key))
    return hits


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

#: The one root this gate owns. ``backend/`` rather than the whole
#: first-party tree: the rule is about a Python-level API, and widening
#: the root to a directory the rule has no statement about would let a
#: future root quietly become part of the contract.
SCAN_ROOTS: tuple[Path, ...] = (Path("backend"),)

#: Python only. The other first-party suffixes are styles this rule has
#: no opinion about, and a file that is not parseable as Python cannot be
#: judged by an AST.
PY_SUFFIXES: frozenset[str] = frozenset({".py"})


def iter_scanned_sources() -> Iterator[Path]:
    """Yield every first-party ``.py`` file the gate reads.

    Re-uses :func:`source_scan.iter_first_party_sources` for the walk,
    so the same exclusions apply everywhere (``no .venv``, no
    ``__pycache__``, no plans), and the root resolves against the
    repository rather than against the working directory — CI runs this
    lane with ``working-directory: backend``, where a relative
    ``backend/`` root does not exist.

    ``tests/`` is dropped on top of the shared exclusions. The gates
    are first-party code and the home-path rules apply to them; *this*
    rule is a statement about the modules that ship, and this file holds
    the forbidden shapes verbatim as its fixtures.
    """
    for path in source_scan.iter_first_party_sources(
        roots=SCAN_ROOTS, extensions=PY_SUFFIXES
    ):
        if "tests" in source_scan.repo_relative(path).parts:
            continue
        yield path


def scan_findings() -> list[str]:
    """Return one ``<rel>:L<line>: <shape> reads <key>`` string per offence.

    A file that does not parse raises rather than being skipped. A
    skipped file is a hole the gate cannot report, and the only thing
    that can create one is a future Python version the tree has moved
    ahead of — which is worth a loud failure rather than a quiet pass.
    """
    findings: list[str] = []
    for path in iter_scanned_sources():
        rel = source_scan.repo_relative(path)
        if rel in WHITELIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        findings.extend(
            f"{rel}:L{lineno}: {shape} reads {key}"
            for lineno, shape, key in secret_env_hits(tree)
        )
    return findings


# ---------------------------------------------------------------------------
# The provider's own imports
# ---------------------------------------------------------------------------


def absolute_import_roots(tree: ast.AST) -> set[str]:
    """Top-level module names imported by *tree*.

    A relative import contributes nothing: its target is inside the
    importing package, so there is no top-level name to classify against
    the standard library.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and node.module
        ):
            roots.add(node.module.split(".")[0])
    return roots


def notifications_imports(tree: ast.AST) -> list[str]:
    """Every import in *tree* that reaches the notifier package.

    All four spellings are covered, because a dependency edge that can be
    spelled four ways is one that can be re-introduced four ways:
    ``import notifications.x``, ``from notifications import x``,
    ``from .notifications import x`` and ``from . import notifications``.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == _NOTIFICATIONS_PACKAGE:
                    offenders.append(
                        f"line {node.lineno}: import {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            names = [alias.name for alias in node.names]
            if node.level:
                # Relative: the target is a sibling of this package.
                reached = (
                    module == _NOTIFICATIONS_PACKAGE
                    or _NOTIFICATIONS_PACKAGE in names
                )
                spelling = "." * node.level + (node.module or "")
            else:
                reached = module == _NOTIFICATIONS_PACKAGE
                spelling = node.module or ""
            if reached:
                detail = ", ".join(names) if names else ""
                offenders.append(
                    f"line {node.lineno}: from {spelling} import {detail}"
                )
    return offenders


def _stdlib_names() -> frozenset[str]:
    """The standard library's top-level module names, where the runtime
    publishes them (3.10 and later)."""
    return frozenset(getattr(sys, "stdlib_module_names", ()))


#: Directory names that mark a module as installed rather than
#: standard, checked separately from the stdlib path because some
#: distributions put ``site-packages`` *inside* the interpreter's own
#: library directory, which would make the path test alone call ``pytest``
#: standard library.
_INSTALLED_DIR_NAMES = frozenset({"site-packages", "dist-packages"})


def _origin_is_stdlib(name: str) -> bool:
    """Whether *name* resolves inside the standard library by path.

    The fallback for interpreters older than 3.10, which do not publish
    the module list. It is deliberately unwilling to guess: a module it
    cannot place returns False, so the check fails loudly rather than
    waving a third-party import through.
    """
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    if spec.origin is None or _INSTALLED_DIR_NAMES & set(
        Path(spec.origin).parts
    ):
        return False
    stdlib_dir = sysconfig.get_paths().get("stdlib")
    if not stdlib_dir:
        return False
    try:
        Path(spec.origin).resolve().relative_to(Path(stdlib_dir).resolve())
    except (ValueError, OSError):
        return False
    return True


def is_stdlib_module(name: str) -> bool:
    """Return whether *name* is part of the standard library.

    Two answers, one per interpreter generation, and no table in this
    file: a list of standard-library names is a maintenance burden that
    goes stale silently, and a stale entry in a *permission* list fails
    open.
    """
    published = _stdlib_names()
    if published:
        return name in published
    return _origin_is_stdlib(name)


#: Exempted files, each with the reason it is exempt. An empty reason
#: fails :func:`test_whitelist_entries_carry_a_reason`.
WHITELIST: dict[Path, str] = {
    Path("backend/credentials.py"): (
        "The provider is the module that decides where a secret comes "
        "from, so reading these two keys here is the decision every other "
        "module is routed through. The exemption is for the module, not "
        "for a spelling: it reads the keys out of SECRET_SPECS rather "
        "than out of a literal, which is why the gate does not flag its "
        "own reads."
    ),
    Path(".env.example"): (
        "The deployment-facing example. It has to name both keys — a "
        "keychain deployment is configured by the index keys it lists and "
        "the fallback keys it comments out — and an example that could "
        "not name them could not be copied into a working .env. It is not "
        "Python, so the AST scan never reaches it; the entry is here so "
        "that the set of files allowed to name a secret key is one list "
        "rather than one list plus a different answer in someone's head."
    ),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_the_five_forms_are_flagged() -> None:
    """The five spellings the rule names, each reported once."""
    samples = (
        (
            'os.environ.get("TELEGRAM_BOT_TOKEN")',
            "os.environ.get",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            'os.environ["FEISHU_APP_SECRET"] = token',
            "os.environ[...]",
            "FEISHU_APP_SECRET",
        ),
        (
            'os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")',
            "os.environ.setdefault",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            'os.environ.update({"FEISHU_APP_SECRET": token})',
            "os.environ.update",
            "FEISHU_APP_SECRET",
        ),
        (
            'os.getenv("TELEGRAM_BOT_TOKEN")',
            "os.getenv",
            "TELEGRAM_BOT_TOKEN",
        ),
    )
    for source, shape, key in samples:
        hits = secret_env_hits(ast.parse(source))
        assert hits == [(1, shape, key)], (
            f"{source!r} should be reported once as {shape} reading "
            f"{key}; got {hits!r}. The five forms are the whole rule — if "
            "the detector cannot see one of them it cannot see the "
            "violation it exists to catch."
        )


def test_the_five_allowed_forms_are_not_flagged() -> None:
    """Five reads of the environment that are not the secret."""
    samples = (
        # The keychain index: a value with no power on its own.
        'os.environ.get("TELEGRAM_CHAT_ID")',
        # Per-plan routing. A key that is a prefix at compile time is not
        # the secret, and this is the project's own configuration.
        'os.environ.get(f"TELEGRAM_CHAT_ID_{plan_id}")',
        # A pipe descriptor. The string *contains* the secret's name, and
        # a substring gate would fire on it here.
        'os.environ.get("PDT_SECRET_FD_FEISHU_APP_SECRET")',
        # A near miss: the same word, a different variable.
        'os.environ.get("TELEGRAM_BOT_TOKEN_FILE")',
        # An indirect key. This is the provider's own shape, and the only
        # module allowed to use it.
        "os.environ.get(spec.fallback_env_key)",
    )
    for source in samples:
        hits = secret_env_hits(ast.parse(source))
        assert hits == [], (
            f"{source!r} reads the environment but not a secret; got "
            f"{hits!r}. A gate that flags correct code is a gate that "
            "gets disabled — the index keys and the descriptor variable "
            "are what the whole design rests on."
        )


def test_the_adjacent_spellings_of_a_shape_are_not_a_hole() -> None:
    """A shape with more than one spelling is a shape with more than one hole.

    The five samples above are the spellings the rule names. Each also
    has a neighbour that means the same thing and arrives differently:
    ``update`` takes a mapping, keyword arguments and a starred mapping;
    a subscript is a write when it is stored to and a read when it is
    loaded, and ``del`` is a third thing. A detector that only knows one
    of them is bypassed by choosing another, which is a one-character
    edit and needs no intent at all — the reason each neighbour is
    pinned here rather than left to the reader to infer.
    """
    samples = (
        (
            'os.environ.update(TELEGRAM_BOT_TOKEN=token)',
            "os.environ.update",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            'os.environ.update(**{"TELEGRAM_BOT_TOKEN": token})',
            "os.environ.update",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            'del os.environ["TELEGRAM_BOT_TOKEN"]',
            "os.environ[...]",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            'os.environ["TELEGRAM_BOT_TOKEN"]',
            "os.environ[...]",
            "TELEGRAM_BOT_TOKEN",
        ),
    )
    for source, shape, key in samples:
        hits = secret_env_hits(ast.parse(source))
        assert hits == [(1, shape, key)], (
            f"{source!r} is another spelling of a shape this rule already "
            f"names; it should be reported the same way. Got {hits!r}."
        )


def test_the_secret_keys_are_the_ones_the_provider_declares() -> None:
    """The gate follows the spec table instead of re-typing the keys.

    A gate that keeps its own copy of the key names is a second source
    of truth for the same two facts, and it is the one that goes wrong
    silently: rename a row in the spec table and the copy keeps matching
    the old spelling.
    """
    assert _secret_env_keys() == frozenset(
        {"FEISHU_APP_SECRET", "TELEGRAM_BOT_TOKEN"}
    ), (
        "the two secret env keys are the two fallback_env_key entries in "
        "credentials.SECRET_SPECS. If that table gained a row, the gate "
        "picks it up automatically — say so here rather than leaving the "
        "count pinned to a number nobody reads."
    )


def test_scan_set_is_not_empty_and_contains_notification_modules() -> None:
    """The scan must read something, and it must read the notifier tree."""
    scanned = {
        source_scan.repo_relative(path) for path in iter_scanned_sources()
    }
    assert scanned, (
        "iter_scanned_sources() returned nothing — the gate would pass on "
        "an empty result, which is the failure mode a scan-only gate has "
        "no other way to detect."
    )
    missing = [
        module for module in NOTIFICATION_MODULES if module not in scanned
    ]
    assert not missing, (
        f"{missing} are no longer in the scan set. They are the modules "
        "the provider was introduced for, so a scan that stopped covering "
        "them is not narrower — it is broken."
    )


def test_whitelist_entries_carry_a_reason() -> None:
    """Two exemptions, two reasons, both non-empty."""
    assert set(WHITELIST) == {
        Path("backend/credentials.py"),
        Path(".env.example"),
    }, (
        "the whitelist is the provider and the deployment-facing example "
        "file, and nothing else. An exemption that is not one of these two "
        "is a module reading a secret that nothing decided it may."
    )
    for path, reason in WHITELIST.items():
        assert reason.strip(), (
            f"{path} is exempt with no reason. An unexplained exemption is "
            "a rule nobody agreed to, and it is how the next one arrives."
        )
        assert (source_scan.REPO_ROOT / path).is_file(), (
            f"{path} is exempt but does not exist. A stale entry protects "
            "nothing and reads as though the file were still allowed."
        )
    assert source_scan.repo_relative(CREDENTIALS_PATH) in WHITELIST, (
        "the whitelist key for the provider has to be the path the scan "
        "actually produces, or the exemption silently stops applying and "
        "the one file allowed to read these keys starts failing the gate."
    )


def test_credentials_imports_are_stdlib_only() -> None:
    """The provider may not grow a third-party dependency."""
    tree = ast.parse(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    roots = absolute_import_roots(tree)
    assert roots, (
        "no import was found in the provider — the walk is broken, and a "
        "gate that inspects nothing reports nothing."
    )
    third_party = sorted(name for name in roots if not is_stdlib_module(name))
    assert not third_party, (
        "backend/credentials.py imports outside the standard library. The "
        "provider is the module a deployment's own credential-loading code "
        "imports; a third-party dependency there drags that loader into "
        "installing something it never asked for. Move the dependency to "
        "the caller that needs it.\n  "
        + "\n  ".join(third_party)
    )


def test_credentials_does_not_import_notifications() -> None:
    """The dependency between the two packages runs one way."""
    tree = ast.parse(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    offenders = notifications_imports(tree)
    assert not offenders, (
        "backend/credentials.py imports from the notifier package. The "
        "dependency has to point one way — the provider supplies the "
        "secret, the notifier asks for it — and the reverse edge makes "
        "the provider unusable by whatever populates a deployment's "
        "credentials.\n  " + "\n  ".join(offenders)
    )


def test_no_module_outside_the_provider_reads_a_secret_env_key() -> None:
    """The whole ``backend/`` tree, minus ``tests/``: zero findings."""
    findings = scan_findings()
    assert not findings, (
        "a secret was read from or written to os.environ outside the "
        "credentials provider. An environment variable is readable by "
        "every process of every user on the machine and is inherited by "
        "every child, which is the reason the keychain provider exists. "
        "Call credentials.read_secret() or credentials.secret_source() "
        "instead — the deployment decides where the value comes from, "
        "and the call site does not need to know.\n  " + "\n  ".join(findings)
    )
