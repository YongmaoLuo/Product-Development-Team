"""Telling a locked keychain apart from a credential nobody filed.

Why this suite exists
---------------------
``read_secret`` returns ``None`` for every way of not having a value,
and that is deliberate: the caller asks whether a transport is
configured, not what went wrong, and a notifier that raised on an
unconfigured deployment would take the whole process down over a
notification it was never going to send.

The cost of that collapse is that ``missing`` is one word standing for
at least four different situations:

* the keychain is switched off and the plaintext variable is empty;
* the keychain is on but the account index is not set, so there was
  nothing to look up;
* the keychain is **locked**;
* the keychain is unlocked and holds no such item.

Those are fixed in four different places, and the locked one is the
expensive one — it also costs up to ``_KEYCHAIN_TIMEOUT_SECONDS`` of
blocking on an unlock dialog before it can report anything. An operator
reading only "missing" goes looking for a credential that was never set
up, and finds it, and concludes the message must be wrong.

So two things are pinned here: the WARNING that names a locked keychain
(noisily, and *only* for that case), and :func:`diagnose_secret`, which
answers the same question on demand.

Nothing here runs ``/usr/bin/security``. The read is replaced at
:func:`credentials._run_security`, which is also what makes the count of
subprocesses assertable — the lock probe is a second process, and a
gate that cannot see it cannot tell whether the happy path grew one.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

import credentials

#: The real lock probe, captured at import — before the autouse fixture
#: below replaces it. Writing ``monkeypatch.setattr(credentials,
#: "_keychain_state", credentials._keychain_state)`` inside a test would
#: otherwise capture the stub that fixture installed, and the test would
#: pass while asserting nothing about the real probe.
_REAL_KEYCHAIN_STATE = credentials._keychain_state


@pytest.fixture(autouse=True)
def _no_real_keychain_process(monkeypatch):
    """Neutralise the lock probe for every test in this module.

    :func:`credentials._keychain_state` deliberately bypasses
    ``_run_security`` and calls ``subprocess.run`` itself, because the
    whole point of it is to tell *the tool failed* apart from *the tool
    could not be run* — and ``_run_security`` collapses both into
    ``None``, which is right for the read and useless here.

    The cost of that bypass is that patching ``_run_security`` no longer
    keeps a test off the real binary, and a test that quietly shells out
    to ``/usr/bin/security`` can block on an unlock dialog on a
    contributor's machine. So the probe is stubbed here by default, for
    every test, and the ones that care about a particular state override
    it.
    """
    monkeypatch.setattr(
        credentials, "_keychain_state", lambda: credentials.KEYCHAIN_UNLOCKED
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    """The memo has the process's lifetime unless a test ends it."""
    credentials.reset_cache()
    yield
    credentials.reset_cache()


@pytest.fixture
def keychain_on(monkeypatch):
    """The keychain is consulted, and this platform has one."""
    monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "0")
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)


@pytest.fixture
def keychain_off(monkeypatch):
    """The keychain is switched off: the environment is the only source."""
    monkeypatch.delenv(credentials._SWITCH_ENV_KEY, raising=False)
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)


class _FakeSecurity:
    """Stand-in for :func:`credentials._run_security`.

    Records every argv it is handed, so a test can assert on *how many*
    processes a resolution started and not merely on the answer. The
    lock probe is invisible in the answer and obvious in the count.
    """

    def __init__(self, payload: bytes | None = None, returncode: int = 0):
        self.payload = payload
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.returncode != 0:
            return None
        return self.payload


def _fail_read(monkeypatch, *, state: str):
    """Make the keychain read fail, and pin what the probe reports."""
    monkeypatch.setattr(
        credentials, "_run_security", _FakeSecurity(None, returncode=1)
    )
    monkeypatch.setattr(credentials, "_keychain_state", lambda: state)


# ---------------------------------------------------------------------------
# The warning
# ---------------------------------------------------------------------------


class TestLockedKeychainIsNamed:
    def test_a_locked_keychain_warns_with_the_file_and_the_command(
        self, keychain_on, monkeypatch, caplog
    ):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        _fail_read(monkeypatch, state=credentials.KEYCHAIN_LOCKED)

        with caplog.at_level(logging.WARNING, logger="credentials"):
            assert credentials.read_secret("feishu_app_secret") is None

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
        message = warnings[0].getMessage()
        assert "locked" in message
        assert "feishu_app_secret" in message
        assert "unlock-keychain" in message

    def test_the_warning_never_contains_the_secret(
        self, keychain_on, monkeypatch, caplog
    ):
        """The message is built from the logical name and the keychain
        path. Both are things the operator needs; neither is the value,
        and a diagnostic that leaked the credential would be the worst
        possible place for one."""
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.setenv("FEISHU_APP_SECRET", "the-actual-secret-value")
        fake = _FakeSecurity(None, returncode=1)
        monkeypatch.setattr(credentials, "_run_security", fake)
        monkeypatch.setattr(credentials, "_keychain_state", lambda: credentials.KEYCHAIN_LOCKED)

        with caplog.at_level(logging.WARNING, logger="credentials"):
            credentials.read_secret("feishu_app_secret")

        assert "the-actual-secret-value" not in caplog.text

    def test_an_item_that_is_simply_absent_is_quiet(
        self, keychain_on, monkeypatch, caplog
    ):
        """The expected state must stay quiet.

        A warning that fires on "nobody has filed this yet" is a warning
        the operator learns to ignore, and then the one that matters
        arrives into the same silence.
        """
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        _fail_read(monkeypatch, state=credentials.KEYCHAIN_UNLOCKED)

        with caplog.at_level(logging.WARNING, logger="credentials"):
            assert credentials.read_secret("feishu_app_secret") is None

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_a_missing_index_is_quiet(self, keychain_on, monkeypatch, caplog):
        """No lookup was attempted, so nothing could have been locked."""
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

        with caplog.at_level(logging.WARNING, logger="credentials"):
            assert credentials.read_secret("feishu_app_secret") is None

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_a_successful_read_never_probes_the_lock(
        self, keychain_on, monkeypatch
    ):
        """The probe is a second process started for no information.

        Asserted through the count rather than by patching the probe to
        raise, because a probe that is patched out cannot show that it
        was never called.
        """
        fake = _FakeSecurity(b"value\n")
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.setattr(credentials, "_run_security", fake)
        monkeypatch.setattr(
            credentials,
            "_keychain_state",
            lambda: pytest.fail("the lock was probed on a successful read"),
        )

        assert credentials.read_secret("feishu_app_secret") == "value"
        assert len(fake.calls) == 1

    def test_a_failed_read_probes_the_lock_exactly_once(
        self, keychain_on, monkeypatch
    ):
        """The probe is what turns an unexplained miss into a named
        cause, so it must actually run — once, and memoised with the
        resolution rather than repeated on every call.

        Counted at ``subprocess.run`` rather than at ``_run_security``,
        because that is where the probe goes: it has to bypass the
        wrapper in order to tell "the tool failed" from "the tool could
        not be run", which is the one distinction this module exists to
        keep.
        """
        fake = _FakeSecurity(None, returncode=1)
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.setattr(credentials, "_run_security", fake)
        monkeypatch.setattr(credentials, "_keychain_state", _REAL_KEYCHAIN_STATE)

        probes = []
        real_run = subprocess.run

        def _counting_run(argv, *args, **kwargs):
            if argv[1:2] == ["show-keychain-info"]:
                probes.append(argv)
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(credentials.subprocess, "run", _counting_run)

        assert credentials.read_secret("feishu_app_secret") is None
        assert len(probes) == 1, [p[1] for p in probes]

        credentials.read_secret("feishu_app_secret")
        assert len(probes) == 1, "the probe was re-run on a memoised miss"


# ---------------------------------------------------------------------------
# diagnose_secret
# ---------------------------------------------------------------------------


class TestDiagnoseSecret:
    def test_a_resolved_secret_has_nothing_to_report(
        self, keychain_off, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        assert credentials.diagnose_secret("telegram_bot_token") is None

    def test_an_unregistered_name_names_itself_and_lists_the_real_ones(self):
        reason = credentials.diagnose_secret("nope")
        assert reason is not None
        assert "nope" in reason
        assert "feishu_app_secret" in reason
        assert "telegram_bot_token" in reason

    def test_the_switch_is_off_and_nothing_is_set(self, keychain_off, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        reason = credentials.diagnose_secret("telegram_bot_token")
        assert reason is not None
        assert credentials._SWITCH_ENV_KEY in reason
        assert "TELEGRAM_BOT_TOKEN" in reason

    def test_a_missing_index_names_the_index(self, keychain_on, monkeypatch):
        """Different from an absent item: the lookup was never attempted,
        so an item cannot be the thing that is missing."""
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

        reason = credentials.diagnose_secret("feishu_app_secret")
        assert reason is not None
        assert "FEISHU_APP_ID" in reason
        assert "is locked" not in reason

    def test_a_locked_keychain_is_named_as_such(self, keychain_on, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        _fail_read(monkeypatch, state=credentials.KEYCHAIN_LOCKED)

        reason = credentials.diagnose_secret("feishu_app_secret")
        assert reason is not None
        assert "is locked" in reason
        assert "unlock-keychain" in reason
        assert "60 seconds" in reason

    def test_an_unlocked_keychain_with_no_item_says_so(
        self, keychain_on, monkeypatch
    ):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        _fail_read(monkeypatch, state=credentials.KEYCHAIN_UNLOCKED)

        reason = credentials.diagnose_secret("feishu_app_secret")
        assert reason is not None
        # Matched on the phrase, not the substring: "unlocked" contains
        # "locked", so a bare `not in` would be testing the English.
        assert "is locked" not in reason
        assert "holds no item" in reason

    def test_it_never_prints_the_secret(self, keychain_on, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.setenv("FEISHU_APP_SECRET", "the-actual-secret-value")
        _fail_read(monkeypatch, state=credentials.KEYCHAIN_LOCKED)

        reason = credentials.diagnose_secret("feishu_app_secret")
        assert reason is not None
        assert "the-actual-secret-value" not in reason

    def test_diagnosing_does_not_change_what_read_secret_returns(
        self, keychain_on, monkeypatch
    ):
        """It is a question, not a repair: asking must not resolve
        differently from not asking."""
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.setattr(credentials, "_run_security", _FakeSecurity(b"value\n"))

        assert credentials.read_secret("feishu_app_secret") == "value"
        assert credentials.diagnose_secret("feishu_app_secret") is None


class TestLockedIsNotTheSameAsUnrunnable:
    """The distinction that a boolean threw away.

    ``/usr/bin/security`` cannot be executed from inside some sandboxes
    and agent sessions — it fails with EPERM before the keychain's state
    is ever consulted. A probe that treats that as "locked" tells an
    operator to go and unlock a keychain that is already open, and the
    advice is attached to a real symptom, so it gets believed.
    """

    def test_a_tool_that_cannot_be_executed_is_not_reported_as_locked(
        self, keychain_on, monkeypatch, caplog
    ):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        # The read fails, and the probe cannot even start the binary —
        # the sandbox / agent-session case.
        monkeypatch.setattr(
            credentials, "_run_security", _FakeSecurity(None, returncode=1)
        )
        monkeypatch.setattr(credentials, "_keychain_state", _REAL_KEYCHAIN_STATE)

        def _unrunnable(*args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(credentials.subprocess, "run", _unrunnable)

        with caplog.at_level(logging.WARNING, logger="credentials"):
            assert credentials.read_secret("feishu_app_secret") is None

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
        message = warnings[0].getMessage()
        assert "is locked" not in message, message
        assert "cannot run" in message, message
        assert "Unlocking the keychain will not help" in message, message

    def test_diagnosis_names_the_unrunnable_tool_rather_than_a_lock(
        self, keychain_on, monkeypatch
    ):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_abc")
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

        def _unrunnable(*args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(
            credentials, "_run_security", _FakeSecurity(None, returncode=1)
        )
        monkeypatch.setattr(credentials, "_keychain_state", _REAL_KEYCHAIN_STATE)
        monkeypatch.setattr(credentials.subprocess, "run", _unrunnable)

        reason = credentials.diagnose_secret("feishu_app_secret")
        assert reason is not None
        assert "is locked" not in reason
        assert "/usr/bin/security" in reason
        assert "Unlocking the keychain will not" in reason

    def test_a_non_macos_platform_is_unavailable_not_locked(
        self, monkeypatch
    ):
        """There is no keychain to lock on Linux; saying "locked" would
        send an operator to look for one."""
        monkeypatch.setattr(credentials, "_is_macos", lambda: False)
        assert _REAL_KEYCHAIN_STATE() == credentials.KEYCHAIN_UNAVAILABLE

    def test_the_probe_labels_the_three_outcomes(self, monkeypatch):
        """Exercised against the real probe, so the labels come from the
        subprocess rather than from a stubbed function."""
        monkeypatch.setattr(credentials, "_is_macos", lambda: True)

        class _Completed:
            def __init__(self, code):
                self.returncode = code

        monkeypatch.setattr(
            credentials.subprocess, "run", lambda *a, **k: _Completed(0)
        )
        assert _REAL_KEYCHAIN_STATE() == credentials.KEYCHAIN_UNLOCKED

        monkeypatch.setattr(
            credentials.subprocess, "run", lambda *a, **k: _Completed(1)
        )
        assert _REAL_KEYCHAIN_STATE() == credentials.KEYCHAIN_LOCKED

        def _raises(*a, **k):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(credentials.subprocess, "run", _raises)
        assert _REAL_KEYCHAIN_STATE() == credentials.KEYCHAIN_UNAVAILABLE
