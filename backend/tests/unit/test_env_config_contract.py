"""The dotenv loader must terminate, and must not invent a requirement.

**This file used to pin a different contract and the change is the point
of reading it** (2026-10-08). `env_config.REQUIRED_VARS` demanded three
variables and raised `RuntimeError` naming whichever was absent. The
tests below were parametrized over that list, so the moment it emptied
they would have kept *passing* while testing nothing: an empty
`parametrize` is a skipped test, not a failing one, and a suite full of
skipped tests is green. That is the whole hazard this rewrite exists to
close, so the
contract is restated against properties that hold whether or not anything
is required.

What survives, and why it is still worth a test:

  * **Nothing is required.** A deployment that configures nothing still
    starts. The previous demand was not merely strict, it was wrong: the
    key it demanded is supplied by the provider layer rather than from
    here, so a deployment whose credentials were correctly supplied was
    refused a start for not having a file it does not use. The other two
    names have no reader in this repository at all.
  * **The loader terminates.** This is the half worth keeping
    unconditionally, and it is not hypothetical. The investigation this
    file records started from a CI job that stopped responding with no
    log, no step timeout and no job timeout — and "configuration is
    missing" is exactly the class of condition that must never be the
    thing that hangs. Whatever the loader is eventually asked to do, a
    prompt return is part of its contract, so this is asserted directly
    rather than inherited from the requirement list.
  * **The shell beats the file.** A value already exported wins over the
    same key in the dotenv, which is what makes a one-off override
    possible without editing anything.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config_paths  # noqa: E402
from env_config import DEFAULTS, REQUIRED_VARS, load_env, load_env_config  # noqa: E402

#: An env path that does not exist, so ``load_dotenv`` is skipped entirely
#: and the result depends only on ``os.environ``. Without this the loader
#: would re-read the ``.env`` conftest just wrote and the test would be
#: asserting against the fixture instead of against the loader.
NO_ENV_FILE = "/nonexistent/definitely-not-a-dotenv-file"


# ---------------------------------------------------------------------------
# Nothing is required
# ---------------------------------------------------------------------------


def test_nothing_is_required():
    """The empty list is the contract, so assert it is empty.

    A future addition is a *decision* — a variable somebody actually
    reads has to exist before requiring it can be honest. This test is
    what turns that decision into something the diff has to argue for,
    rather than a name quietly appended to a list.
    """
    assert REQUIRED_VARS == [], (
        "env_config now requires nothing. Before adding a name here, "
        "confirm that some code path in this repository actually reads "
        "the value — no code path read the API key (it is supplied by "
        "the provider layer) and neither of the other two ever had a "
        "reader, which is how three phantom requirements accumulated in "
        "the first place."
    )


def test_a_bare_deployment_starts(tmp_path, monkeypatch):
    """No env file, no exported variables, no exception.

    This is the regression the empty requirement list exists to prevent.
    It used to raise ``KeyError('ANTHROPIC_API_KEY')`` from
    ``load_env_config`` and ``RuntimeError`` from ``load_env``.
    """
    for key in (*REQUIRED_VARS, *DEFAULTS):
        monkeypatch.delenv(key, raising=False)

    config = load_env_config(str(tmp_path / "absent.env"))

    assert config == dict(DEFAULTS)


def test_load_env_also_starts_with_nothing_configured(tmp_path, monkeypatch):
    """The loud wrapper must not be louder than the loader underneath.

    Both CLI entry points call ``load_env()``. If it raised where
    ``load_env_config`` does not, the two would disagree about what a
    bare deployment looks like, and the wrapper would be re-introducing
    exactly the startup failure the empty list removed.
    """
    for key in (*REQUIRED_VARS, *DEFAULTS):
        monkeypatch.delenv(key, raising=False)

    config = load_env(str(tmp_path / "absent.env"))

    assert config == dict(DEFAULTS)


# ---------------------------------------------------------------------------
# It terminates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_path", [NO_ENV_FILE, "placeholder"])
def test_it_terminates_promptly_rather_than_retrying(monkeypatch, env_path, tmp_path):
    """A missing or absent file is not a transient condition.

    There is nothing to wait for, so there must be no waiting: no retry
    loop, no backoff, no provider-style poll. A configuration error that
    hangs holds the process, its file descriptors and its locks while
    reporting nothing — the failure mode this test exists to forbid.
    """
    path = str(tmp_path / "absent.env") if env_path == "placeholder" else env_path
    for key in (*REQUIRED_VARS, *DEFAULTS):
        monkeypatch.delenv(key, raising=False)

    started = time.monotonic()
    load_env(path)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"load_env() took {elapsed:.2f}s to report on a missing file; it "
        f"must return immediately"
    )


# ---------------------------------------------------------------------------
# The path is declared once
# ---------------------------------------------------------------------------


def test_the_default_path_is_the_project_root_dotenv():
    """One file, one declaration.

    ``env_config`` used to derive ``backend/.env`` from its own
    ``__file__`` while ``server`` read ``<repo>/.env``. A deployment
    could satisfy one and starve the other. Asserting the resolved value
    — rather than passing an explicit path — is what keeps a second
    derivation from creeping back in.
    """
    assert config_paths.ENV_FILE == config_paths.PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_the_shell_beats_the_file(tmp_path, monkeypatch):
    """An exported variable wins over the same key in the dotenv.

    This is what makes it possible to point one run at a different
    configuration without editing the file the deployment depends on.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("TIMEZONE=UTC\n", encoding="utf-8")
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")

    config = load_env_config(str(env_file))

    assert config["TIMEZONE"] == "Asia/Tokyo"


def test_the_file_supplies_a_value_the_shell_does_not(tmp_path, monkeypatch):
    monkeypatch.delenv("TIMEZONE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TIMEZONE=UTC\n", encoding="utf-8")

    config = load_env_config(str(env_file))

    assert config["TIMEZONE"] == "UTC"


def test_the_default_applies_when_neither_supplies_one(tmp_path, monkeypatch):
    monkeypatch.delenv("TIMEZONE", raising=False)

    config = load_env_config(str(tmp_path / "absent.env"))

    assert config["TIMEZONE"] == DEFAULTS["TIMEZONE"]