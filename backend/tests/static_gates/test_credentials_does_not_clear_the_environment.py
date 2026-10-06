"""The credentials provider may read the environment; it may not clear it.

Why this gate exists
--------------------
A deployment that predates the keychain has its provider secrets exported
by the shell that starts the server, or listed in the plist of a
registered launchd job. Neither of those is reachable from inside the
process. ``os.environ.pop`` removes a key from *this* process's copy of
the environment and from nothing else: the parent shell still holds the
export, the launchd job still holds the key, and the next start puts the
value straight back.

A provider that deletes the variable on the way past therefore looks
like it has done the migration and has not done it at all. Worse, the
look is load-bearing: the secret is gone from the environment a reader
inspects, so the deployment looks migrated, and the plaintext export
keeps working with nothing to notice it. The honest form of the same
decision is to leave the variable alone and say where it came from —
which :func:`credentials.secret_source` already answers with
``"os.environ"``.

So the rule is narrow and mechanical: this one module reads
``os.environ`` and writes to ``os.pipe``, and it removes nothing from
``os.environ``.

The three shapes
----------------
``os.environ.pop(...)``, ``os.environ.popitem(...)`` and
``del os.environ[...]`` are the three ways Python spells the removal.
A shape rather than a substring: this file's own docstring discusses the
discipline at length, and prose about a forbidden call is not a call.

The scan target
---------------
Exactly ``backend/credentials.py``, resolved from this file's own
location, and parsed rather than grepped. This is deliberately not a
whole-tree substring scan. A repo-wide ban on these three characters
would have to exempt this file — the place the shapes are written down
so the gate can be proven — and an exemption that can grow is a rule
that stops being a rule. Pinning the target to the module that owns the
decision keeps the gate a statement about the provider rather than
about spelling.

Why the gate proves it can fail
-------------------------------
:func:`test_the_scanner_flags_an_injected_sample` feeds the scanner the
sample the rule is written about and requires the scanner to report it.
A gate whose detector returns nothing for a real violation is worse than
no gate at all: it is a green check reading "no secret is left in the
environment" on a machine that still exports one.

The read is not what is forbidden
---------------------------------
:func:`test_provider_keeps_reading_the_fallback_env` is the other half.
The plaintext variable is the only source a CI runner, a container or a
Linux host has, so banning the removal must not quietly turn into
banning the read. It is read, it is labelled ``"os.environ"``, and it
is still in the environment when the call returns.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import credentials

#: ``backend/`` — derived from this file's own location, never written
#: out literally: the repository is cloned at a different path on every
#: machine, and an absolute path here would identify one of them.
BACKEND_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_DIR / "credentials.py"

#: The sample the rule is written about, kept verbatim so the
#: self-check and the rule can never drift apart.
SAMPLE_POP = 'os.environ.pop("TELEGRAM_BOT_TOKEN", None)\n'
SAMPLE_POPITEM = 'os.environ.popitem()\n'
SAMPLE_DEL = 'del os.environ["TELEGRAM_BOT_TOKEN"]\n'


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

#: The two methods this rule names. ``os.environ.clear()`` would empty
#: the whole environment and is not among the three forms this gate
#: covers — stated here so the boundary is visible rather than implied,
#: because a reader who assumes the list is exhaustive would be wrong
#: about one call.
_CLEARING_METHODS = frozenset({"pop", "popitem"})


def _is_os_environ(node: ast.AST) -> bool:
    """Return whether ``node`` is the ``os.environ`` mapping itself."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_environ_removal(node: ast.AST) -> bool:
    """Return whether ``node`` is a removal method on ``os.environ``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _CLEARING_METHODS
        and _is_os_environ(node.value)
    )


def env_clearing_hits(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, shape)`` for every removal from ``os.environ``.

    Two node types carry the removal, and both are walked rather than
    matched as text: the call whose receiver is ``os.environ`` and whose
    method is :data:`_CLEARING_METHODS`, and the ``del`` statement whose
    target subscripts ``os.environ``. The shape is reported with an
    ellipsis instead of the arguments, because the arguments name the
    secret being deleted and a gate's failure message should not become
    the one place the plaintext value is still written down.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_environ_removal(node.func):
            hits.append((node.lineno, f"os.environ.{node.func.attr}(...)"))
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_os_environ(
                    target.value
                ):
                    hits.append((node.lineno, "del os.environ[...]"))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_provider_contains_no_env_clearing_calls() -> None:
    """No call in ``credentials.py`` removes a key from ``os.environ``."""
    assert MODULE_PATH.is_file(), (
        f"{MODULE_PATH} is missing — the gate would pass on an empty "
        f"result rather than on a provider that was inspected."
    )
    tree = ast.parse(
        MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH)
    )
    hits = env_clearing_hits(tree)
    assert not hits, (
        "backend/credentials.py removes a key from os.environ. Deleting "
        "the variable clears this process's copy and nothing else: the "
        "exporting shell and any registered launchd job still carry the "
        "secret, and the next start puts it back. A provider that pops "
        "the variable reads as migrated while the plaintext export keeps "
        "working unobserved. Report the source with secret_source() and "
        "leave the removal to the deployment.\n  "
        + "\n  ".join(f"line {lineno}: {shape}" for lineno, shape in hits)
    )


def test_the_scanner_flags_an_injected_sample() -> None:
    """A gate that cannot fail is decoration — feed it the real shape.

    The first case is the sample verbatim, the same one the module
    docstring above rejects. The other two are the remaining spellings
    of the same removal.
    """
    assert env_clearing_hits(ast.parse(SAMPLE_POP)) == [
        (1, "os.environ.pop(...)")
    ]
    assert env_clearing_hits(ast.parse(SAMPLE_POPITEM)) == [
        (1, "os.environ.popitem(...)")
    ]
    assert env_clearing_hits(ast.parse(SAMPLE_DEL)) == [
        (1, "del os.environ[...]")
    ]


def test_provider_keeps_reading_the_fallback_env(monkeypatch) -> None:
    """Banning the removal must not ban the read.

    On a CI runner, in a container, on Linux there is no keychain, and
    the plaintext variable is the only source there is. The value is
    read, it is labelled ``"os.environ"``, and it is still in the
    environment once the call returns — a read, not a consumption.

    The switch is forced to its disabling spelling so the assertion is
    about the fallback read rather than about whatever a workstation
    happens to have exported.
    """
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "the-secret")
    credentials.reset_cache()
    try:
        assert credentials.secret_source("telegram_bot_token") == "os.environ"
        assert credentials.read_secret("telegram_bot_token") == "the-secret"
        assert os.environ.get("TELEGRAM_BOT_TOKEN") == "the-secret"
    finally:
        credentials.reset_cache()