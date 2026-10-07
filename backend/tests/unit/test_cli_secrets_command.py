"""The ``secrets`` subcommand: what it prints, and — mostly — what it does not.

Why this suite exists
---------------------
`credentials.py` decides where a provider secret comes from and hands
callers three questions to ask. The CLI is how an operator asks them
without reading source: ``secrets verify`` answers "is this deployment
configured, and out of where", ``secrets show`` answers "which keychain
item is it". Both are display commands, and the contract that makes
them safe to run is that they display *configuration* and never a
credential.

What is pinned here
-------------------
* **Sources, not values.** ``verify`` prints one line per secret
  carrying the label ``secret_source`` returns, and the value is never
  written. A command whose answer to "is this set up?" is the secret
  itself does not belong in a terminal, a CI log, or a screen share.
  The canary values below are what make that checkable: they are in
  the environment for the whole test, so finding either one in the
  output is a failure, not an absence of evidence.
* **The lookup path is not entered by ``show``.** It enumerates the
  spec table's metadata, so it must not start a keychain read — not
  because a read is expensive, but because a read is the one thing
  that puts a secret in this process to answer a question about
  naming. The instrument is ``credentials._run_security``, the single
  funnel through which the whole project starts the keychain tool, and
  the test proves the funnel works by watching ``verify`` go through
  it under identical conditions.
* **A platform without the keychain gets a hint, not a traceback.**
  An operator on a machine with no ``/usr/bin/security`` is owed one
  sentence saying so, and a nonzero exit. A stack trace is not that
  sentence. The condition is the tool's absence and nothing else — see
  the note on the fixture below, and ``test_platform_without_the_tool_is_told_so``
  for why a switch-conditioned note could never have fired on the
  platform it is meant to describe.
* **A platform *with* the keychain tool is not a diagnosis.** The
  other side of the same relationship, and the guard against
  over-correcting: with the tool present and the keychain switched
  off, a deployment whose secrets all come from the environment is
  fully configured and the command has to read as healthy.
* **Both entry points agree.** ``python -m backend.cli`` and
  ``python backend/cli.py`` differ in ``sys.path``, and that is the
  only thing allowed to differ. A subcommand whose output depended on
  how it was spelled would be a subcommand with two answers.

Why unit
--------
Nothing here starts a process, opens a socket, or writes outside
``tmp_path``. The keychain is read through a recording stand-in for
``_run_security`` rather than a real ``/usr/bin/security``, so the
"no read happened" assertion is made without the read being possible
in the first place — which is what keeps a negative assertion
trustworthy. The child processes in the entry-point test are waited on
before the test ends, so nothing outlives it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import cli
import credentials

#: The repository root and the backend directory, both resolved from
#: this file. Written out literally they would name one developer's
#: checkout and resolve to nothing on every other machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

#: Every environment variable a ``secrets`` answer can depend on, named
#: after the spec table rather than written out: a row added to
#: ``SECRET_SPECS`` is covered by this scrubber the day it is added,
#: and no secret key name is spelled in this file.
_CREDENTIAL_ENV_KEYS = tuple(
    key
    for spec in credentials.SECRET_SPECS.values()
    for key in (spec.fallback_env_key, spec.account_env_key)
) + ("PDT_DISABLE_KEYCHAIN_SECRETS",)

#: The three variables ``env_config.load_env()`` demands before the CLI
#: parses anything. They are placeholders: the loader only checks that
#: they exist, and a subprocess that reached the parser with these
#: filled in behaves exactly as one that reached it from a developer's
#: own ``.env``.
_LOADER_PLACEHOLDERS = (
    "ANTHROPIC_API_KEY",
    "NOTION_TOKEN",
    "NOTION_PARENT_PAGE_ID",
)


@pytest.fixture(autouse=True)
def scrub_credential_environment(monkeypatch, tmp_path):
    """Start every test with no provider credential in the environment.

    Two directions, and the second is the one that bites. A developer's
    shell — and the ``.env`` the CLI loads before it parses — may well
    carry a real ``FEISHU_APP_SECRET``, and a test that asserted on
    output it did not fully control would pass on one machine and fail
    on another. Clearing the keys first means every assertion below is
    about what the test set, and the switch starts disabled so no
    test can reach a real keychain by accident.

    The third direction is the same argument about the *platform*
    rather than about the environment. This suite is read on macOS by
    its author and on a Linux container by CI, and only one of those
    has ``/usr/bin/security``. Left pointing at whatever the machine
    has, every assertion below would also be an assertion about the
    machine — and a suite whose answers change with the runner cannot
    tell a regression from a platform. So a present, executable
    stand-in is installed here, and the tests that care about a
    platform without one remove it explicitly.
    """
    for key in _CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    monkeypatch.setattr(credentials, "_SECURITY_BIN", str(_stub_tool(tmp_path)))
    credentials.reset_cache()
    yield
    credentials.reset_cache()


def _stub_tool(tmp_path, name: str = "security") -> Path:
    """Return the path of an executable stand-in for the keychain tool.

    It is never actually run by this module — the one test that lets a
    read start replaces the runner with a recorder first — so what
    matters is only the two properties the availability check asks
    about: the path exists, and it can be executed.
    """
    tool = tmp_path / name
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def _enable_keychain(monkeypatch, binary: str) -> None:
    """Put the deployment in the state where the keychain is consulted.

    The platform, the switch, and the tool's path are all redirected,
    which is the same triple ``tests/integration``'s stand-in builds:
    without all three, ``keychain_disabled()`` answers for the machine
    the suite happens to run on rather than for the case under test.
    """
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    monkeypatch.setattr(credentials, "_SECURITY_BIN", binary)
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    credentials.reset_cache()


# ---------------------------------------------------------------------------
# verify: sources, never values
# ---------------------------------------------------------------------------


def test_verify_prints_sources_without_values(monkeypatch, capsys, canary, tmp_path):
    """One line per secret, the source on it, and neither credential on it.

    The two canaries are exported for the whole test, so the assertion
    that they are absent from the output is a real check rather than a
    vacuous one — a command that printed no source labels and no
    values would pass a suite that only looked for the values.
    """
    secret = canary("feishu_secret", tmp_path)
    token = canary("telegram_token", tmp_path)

    for name, value in (
        ("feishu_app_secret", secret),
        ("telegram_bot_token", token),
    ):
        monkeypatch.setenv(
            credentials.SECRET_SPECS[name].fallback_env_key, value
        )

    exit_code = cli.cmd_secrets_verify()
    out = capsys.readouterr().out

    assert out == (
        "feishu_app_secret   source=os.environ\n"
        "telegram_bot_token  source=os.environ\n"
    )
    assert secret not in out
    assert token not in out
    assert exit_code == 0


def test_verify_reports_a_missing_secret_as_a_failure(
    monkeypatch, capsys, canary, tmp_path
):
    """A secret with no source is the answer the command exists to give.

    Exit 0 here would let a half-migrated deployment look healthy at
    the one place an operator checks, and find out at the far end of a
    notification send instead.
    """
    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].fallback_env_key,
        canary("feishu_secret", tmp_path),
    )

    assert cli.cmd_secrets_verify() == 1
    assert "source=missing" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# show: metadata, and not the read
# ---------------------------------------------------------------------------


def test_show_lists_metadata_only(monkeypatch, capsys, canary, tmp_path):
    """The rows name the service and the account; no row carries a value.

    The index canaries *are* printed, and deliberately: an index is not
    a secret — it is the key that names the item, which is the whole
    reason it is allowed to live in the environment — and a listing
    that hid it would answer none of the question it was asked. The
    secret canaries are the ones that must not appear, and the test
    distinguishes the two kinds rather than banning every canary.
    """
    index = canary("feishu_index", tmp_path)
    secret = canary("feishu_secret", tmp_path)

    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].account_env_key, index
    )
    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].fallback_env_key, secret
    )

    assert cli.cmd_secrets_show() == 0
    out = capsys.readouterr().out

    header = out.splitlines()[0]
    assert "service" in header
    assert "account_env_key" in header
    assert "account" in header

    row = next(
        line for line in out.splitlines() if line.startswith("feishu_app_secret")
    )
    assert credentials.SECRET_SPECS["feishu_app_secret"].account_env_key in row
    assert index in row

    # The other row has no index exported, and an absent index is a
    # named state rather than an empty cell: there is no item to look
    # up, and a blank column would read as "the value is empty".
    other = next(
        line for line in out.splitlines() if line.startswith("telegram_bot_token")
    )
    assert "<unset>" in other

    assert secret not in out


def test_show_does_not_go_through_the_lookup_path(monkeypatch, capsys, canary, tmp_path):
    """``show`` starts no keychain read; ``verify`` under the same
    conditions does, and is watched doing it.

    The second half is what makes the first half mean something. A
    recorder that never fires proves nothing on its own — the fixture
    could be miswired, or the case could have quietly stopped
    describing the situation where a read would happen. So the test
    puts the deployment in exactly the state that makes a read
    possible (platform on, switch on, index exported, tool path
    present) and then asks both commands the same question.
    """
    index = canary("feishu_index", tmp_path)
    _enable_keychain(monkeypatch, str(_stub_tool(tmp_path)))
    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].account_env_key, index
    )

    started = []

    def _recording_run_security(argv, timeout):
        started.append(list(argv))
        return b"a-value-the-cli-must-not-print"

    monkeypatch.setattr(credentials, "_run_security", _recording_run_security)

    cli.cmd_secrets_show()
    show_output = capsys.readouterr().out
    assert started == [], "show entered the read path: {}".format(started)
    assert index in show_output

    cli.cmd_secrets_verify()
    verify_output = capsys.readouterr().out
    assert len(started) == 1
    argv = started[0]
    assert argv[1] == "find-generic-password"
    assert "-a" in argv and "-w" in argv
    assert argv[argv.index("-a") + 1] == index
    assert "a-value-the-cli-must-not-print" not in verify_output


# ---------------------------------------------------------------------------
# The platform that has no keychain
# ---------------------------------------------------------------------------


def test_missing_security_binary_gives_hint_not_traceback(
    monkeypatch, capsys, tmp_path
):
    """The platform has a keychain, the tool is not there: a sentence, a nonzero code.

    The binary is pointed at a path that is not there and the platform
    is told the keychain exists, so the only thing the answer can
    depend on is the tool's absence. The exit code has to be nonzero —
    this deployment cannot be configured as asked, and the one command
    whose job is to say so would be saying so with a zero.
    """
    _enable_keychain(monkeypatch, str(tmp_path / "no-such-security"))
    assert credentials.keychain_disabled() is False

    exit_code = cli.cmd_secrets_verify()
    out = capsys.readouterr().out

    assert exit_code != 0
    assert "not supported" in out
    assert str(tmp_path / "no-such-security") in out
    assert "Traceback" not in out
    assert "Error" not in out


def test_platform_without_the_tool_is_told_so(
    monkeypatch, capsys, canary, tmp_path
):
    """A platform with no keychain tool: the sentence, and a nonzero code.

    This is the shape every Linux runner and every container is in, and
    it is reached with the switch *off* — the platform is what
    disables the keychain there, before any spelling of the switch is
    consulted. So a hint gated on ``keychain_disabled()`` being false
    could never fire here, and the one command whose job is to say what
    a deployment can and cannot do would be silent on the one thing it
    cannot do, reporting ``source=os.environ`` for every secret as
    though the environment were the whole answer.

    Every secret is exported from the environment first, so the exit
    code is 0 by the sources test alone: the platform is the only thing
    left that can move it.
    """
    monkeypatch.setattr(credentials, "_is_macos", lambda: False)
    monkeypatch.setattr(
        credentials, "_SECURITY_BIN", str(tmp_path / "no-such-security")
    )
    credentials.reset_cache()
    assert credentials.keychain_disabled() is True

    for name in credentials.SECRET_SPECS:
        monkeypatch.setenv(
            credentials.SECRET_SPECS[name].fallback_env_key,
            canary("feishu_secret", tmp_path),
        )

    exit_code = cli.cmd_secrets_verify()
    out = capsys.readouterr().out

    assert exit_code != 0
    assert "not supported" in out
    assert str(tmp_path / "no-such-security") in out
    assert out.count("source=os.environ") == len(credentials.SECRET_SPECS)
    assert "Traceback" not in out


def test_present_tool_is_not_a_diagnosis(monkeypatch, capsys, canary, tmp_path):
    """The other half: a platform that *has* the tool, with it switched off.

    The hint is a statement about the platform, and this is what keeps
    it from becoming a statement about the switch. With the tool here
    and the keychain not asked for — the fail-closed default — a
    deployment whose secrets all come from the environment is fully
    configured, and the command has to read as healthy: one sentence
    on a box that could have used a keychain is a diagnosis, and
    without it an environment-only deployment looks like a broken one.
    """
    _enable_keychain(monkeypatch, str(_stub_tool(tmp_path)))
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    credentials.reset_cache()

    for name in credentials.SECRET_SPECS:
        monkeypatch.setenv(
            credentials.SECRET_SPECS[name].fallback_env_key,
            canary("feishu_secret", tmp_path),
        )

    assert cli.cmd_secrets_verify() == 0
    out = capsys.readouterr().out
    assert "not supported" not in out
    assert out.count("source=os.environ") == len(credentials.SECRET_SPECS)


# ---------------------------------------------------------------------------
# Two spellings, one answer
# ---------------------------------------------------------------------------


def test_module_and_script_entrypoints_agree(tmp_path):
    """``-m backend.cli`` and ``backend/cli.py`` print the same bytes.

    They are not interchangeable by accident: the module form imports
    the package (whose init puts ``backend/`` on ``sys.path``), the
    script form gets that from ``sys.path[0]``, and every import in
    ``cli.py`` is a flat one that depends on which of those two ran.
    A subcommand that read the environment differently under the two
    spellings would give an operator two answers to one question.

    The environment is built here rather than inherited, so the child's
    output is this test's and not the developer's shell. Every
    credential key is *set* rather than merely removed: the CLI loads
    ``backend/.env`` before it parses, and a variable that is absent
    from the child is exactly the one that file is allowed to fill.

    The exit code is compared rather than pinned to 0, because it is a
    property of the machine and not of the spelling — and this machine
    is not a constant. It has ``/usr/bin/security`` on disk, so the
    unit tests above can stub the tool and be done, but whether the
    tool can actually be *executed* is a platform policy this suite
    does not control: an endpoint-security profile that denies it
    makes the same box report the keychain as unavailable while a
    neighbouring runner reports it as available. Asserting 0 here would
    have made the test a claim about the runner, and it would have
    failed on the very machine the author wrote it on. What has to hold
    everywhere is that the two spellings do not disagree, that both
    reach the answer rather than dying, and that neither writes a
    credential. The platform's own verdict is exercised where it can be
    controlled, by the tests above.
    """
    child_env = dict(os.environ)
    for key in _CREDENTIAL_ENV_KEYS:
        child_env.pop(key, None)
    for key, value in zip(_LOADER_PLACEHOLDERS, ("placeholder-a", "placeholder-b", "placeholder-c")):
        child_env[key] = value
    child_env["PDT_DISABLE_KEYCHAIN_SECRETS"] = "1"
    for index, name in enumerate(credentials.SECRET_SPECS):
        spec = credentials.SECRET_SPECS[name]
        child_env[spec.fallback_env_key] = "child-secret-{}".format(index)
        child_env[spec.account_env_key] = "child-index-{}".format(index)

    module_form = subprocess.run(
        [sys.executable, "-m", "backend.cli", "secrets", "verify"],
        cwd=str(REPO_ROOT), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    script_form = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "cli.py"), "secrets", "verify"],
        cwd=str(REPO_ROOT), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )

    assert module_form.returncode in (0, 1), module_form.stderr.decode("utf-8", "replace")
    assert module_form.returncode == script_form.returncode
    assert module_form.stdout == script_form.stdout
    assert b"source=os.environ" in module_form.stdout
    assert b"child-secret" not in module_form.stdout
    # "no traceback", asserted through a real entry point rather than
    # through a captured exception: the requirement is that a platform
    # the command cannot serve is answered in a sentence.
    assert b"Traceback" not in module_form.stderr

# ---------------------------------------------------------------------------
# verify: saying *why* a secret is missing
# ---------------------------------------------------------------------------


def test_verify_explains_a_missing_secret_rather_than_only_reporting_it(
    monkeypatch, capsys, tmp_path
):
    """``source=missing`` is one word for at least four situations.

    The operator who reads it has no way to tell which one they have,
    and they are fixed in four different places. The switch being off is
    the most common by far and the least alarming; a locked keychain is
    the one that also cost a minute of blocking first, and sending
    someone to look for a credential that was never set up because the
    message said "missing" is the failure this line removes.
    """
    # The autouse fixture leaves the switch at "1" and both credential
    # keys unset, which is exactly "switched off, nothing in the
    # environment".
    assert cli.cmd_secrets_verify() == 1
    out = capsys.readouterr().out

    assert "source=missing" in out
    assert credentials._SWITCH_ENV_KEY in out
    assert "feishu_app_secret" in out


def test_verify_names_the_missing_index_when_the_keychain_is_on(
    monkeypatch, capsys, tmp_path
):
    """A different cause with a different fix: the lookup was never
    attempted, so an item cannot be what is missing."""
    monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "0")
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    # No FEISHU_APP_ID, so there is no account to look anything up by.
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert cli.cmd_secrets_verify() == 1
    out = capsys.readouterr().out

    assert "FEISHU_APP_ID" in out
    assert "is locked" not in out


def test_verify_says_nothing_extra_when_every_secret_resolves(
    monkeypatch, capsys, tmp_path
):
    """A command that narrates the healthy rows trains the reader to skip
    the output that matters."""
    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].fallback_env_key, "s"
    )
    monkeypatch.setenv(
        credentials.SECRET_SPECS["telegram_bot_token"].fallback_env_key, "t"
    )

    assert cli.cmd_secrets_verify() == 0
    out = capsys.readouterr().out

    assert "source=os.environ" in out
    assert "missing" not in out


def test_the_explanation_never_prints_a_secret(
    monkeypatch, capsys, canary, tmp_path
):
    """The line exists to be read by a person pasting output into an
    issue. A diagnostic that carried the credential would make it the
    worst possible thing to paste."""
    secret = canary("feishu_secret", tmp_path)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
    monkeypatch.setenv(
        credentials.SECRET_SPECS["feishu_app_secret"].fallback_env_key, secret
    )
    monkeypatch.delenv(
        credentials.SECRET_SPECS["telegram_bot_token"].fallback_env_key,
        raising=False,
    )

    cli.cmd_secrets_verify()
    out = capsys.readouterr().out

    assert secret not in out
