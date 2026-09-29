"""What the docs promise about the bind address matches what the code does.

Regression guard for the README/code disagreement found on 2026-09-25.

``config_paths.resolve_server_host`` defaults to ``127.0.0.1`` and says
why: *"The web UI has no authentication, so a wildcard bind exposes
every endpoint (including the ones that spawn subprocesses and write
files) to anyone who can reach the host. That must not be something an
operator gets by accident."*

The README, meanwhile, stated plainly:

    The server listens on ``http://0.0.0.0:8000``

Nothing checked the two against each other, so the sentence survived
until someone went looking. It is worse than a stale doc: the README is
where an operator decides what to expect, and "it listens on 0.0.0.0" is
an instruction to open a firewall, not a description of what happens.

These tests are narrow on purpose. They do not diff prose — they pin the
two facts an operator acts on: the default bind address, and whether the
docs tell them to expect a wildcard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config_paths import DEFAULT_SERVER_HOST, resolve_server_host  # noqa: E402

REPO_ROOT = _BACKEND_DIR.parent

#: Docs an operator reads before starting the server.
DOCS = ("README.md", "CLAUDE.md", "SKILL.md")

WILDCARD = "0.0.0.0"


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------


def test_the_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("PDT_HOST", raising=False)
    assert DEFAULT_SERVER_HOST == "127.0.0.1"
    assert resolve_server_host() == "127.0.0.1"


def test_an_operator_can_still_opt_in(monkeypatch):
    """The default must be *safe*, not *mandatory*.

    A container or a VM genuinely needs a non-loopback bind; the design
    is that it costs one explicit setting, not that it is impossible.
    """
    monkeypatch.setenv("PDT_HOST", "0.0.0.0")
    assert resolve_server_host() == "0.0.0.0"


def test_the_override_is_re_read_every_call(monkeypatch):
    """Resolution is not cached, so a late ``PDT_HOST`` still takes effect."""
    monkeypatch.delenv("PDT_HOST", raising=False)
    assert resolve_server_host() == "127.0.0.1"
    monkeypatch.setenv("PDT_HOST", "192.168.1.10")
    assert resolve_server_host() == "192.168.1.10"


# ---------------------------------------------------------------------------
# The docs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", DOCS)
def test_no_document_promises_a_wildcard_bind(doc):
    """Every ``0.0.0.0`` in the docs must be the opt-in, spelled out.

    A line may mention the wildcard only as part of a runnable example
    that sets ``PDT_HOST`` — i.e. as something the operator *chooses*.
    Any other appearance is the README claiming a bind the code refuses,
    which is how the original sentence read.
    """
    path = REPO_ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} is not present")

    offenders = [
        (n, line.strip())
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if WILDCARD in line and "PDT_HOST" not in line
    ]
    assert not offenders, (
        f"{doc} mentions {WILDCARD} without naming the PDT_HOST opt-in, "
        "so it reads as the default:\n"
        + "\n".join(f"  {doc}:{n}: {text}" for n, text in offenders)
    )


def test_the_readme_documents_the_loopback_default():
    """The positive half: the docs say what actually happens."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "127.0.0.1" in readme or "loopback" in readme.lower(), (
        "README.md no longer states the loopback default"
    )


def test_the_readme_says_the_api_is_unauthenticated():
    """The reason the bind matters has to be written down.

    Loopback-by-default is only a coherent decision if the reader knows
    what it does and does not buy. Two claims have to survive any rewrite
    of the README:

    * the API is unauthenticated — there is no login, and the guard is not
      one; and
    * loopback is **not** a security boundary, because the browser the
      operator is already running can reach it.

    The second is the one that is easy to lose: "it binds loopback" reads
    like a safety property, and a README that stops at the first claim
    invites exactly the mistake the guard exists to make survivable.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "unauthenticated" in readme, (
        "README.md does not mention that the API is unauthenticated — there "
        "is no login, and the request guard is not one"
    )
    assert "not a security boundary" in readme, (
        "README.md presents the loopback bind without saying that it is not "
        "a security boundary against a browser — the claim that makes the "
        "request guard make sense"
    )
