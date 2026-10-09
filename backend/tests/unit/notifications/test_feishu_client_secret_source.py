"""Which source the Feishu client reads the app secret from.

Why this suite exists
---------------------
The Feishu app secret used to be read the way every secret used to be
read: ``os.environ.get("FEISHU_APP_SECRET")``. That is the source
``credentials.py`` exists to move away from, because an environment
variable is readable by every process of every user on the machine and
survives into shell history, crash reports and process listings. The
provider now answers "where is this secret, and what is it" in one
place, and a consumer that keeps reading the variable itself is a
consumer that is half-migrated with no way to tell from the outside.

The two values are not treated alike, and that asymmetry is the point:

* the **app id** is an *index* — the account a keychain item is looked
  up by — so it stays in ``os.environ``;
* the **app secret** is the secret, so it comes from the provider.

So this suite pins the half that moved, and pins that the half that
stayed did not move with it. It also pins the fail-fast message, which
is not decoration: the notifier catches ``FeishuUnavailable`` and
disables itself with that string as the only thing the operator sees.
A message that still claims the secret is read from the environment
sends whoever has to fix it to the wrong place, and on a keychain
deployment there is nothing in the environment to set.
"""

from __future__ import annotations

import os
import sys

import pytest

import credentials
from notifications.feishu_client import FeishuClient, FeishuUnavailable


#: The provider's logical name for the Feishu secret, so a test can
#: assert on *which* secret was asked for rather than trusting that the
#: one it arranged is the one that got read.
LOGICAL_NAME = "feishu_app_secret"

_APP_ID_KEY = "FEISHU_APP_ID"
_APP_SECRET_KEY = "FEISHU_APP_SECRET"
_SWITCH_KEY = "PDT_DISABLE_KEYCHAIN_SECRETS"
#: The keychain *reader* override — the fourth thing the module reads out
#: of the environment. Cleared for the same reason the app id is: it is
#: read from the project-root ``.env`` before any test runs, so a
#: workstation that narrowed its ACL with ``setup.sh adopt`` carries it,
#: and ``_keychain_holds`` below would then stub a process the module is
#: no longer starting. A case whose result depends on which machine runs
#: it is a case that proves nothing.
_READER_PATH_KEY = "PDT_SECRET_READER_PATH"

#: What the keychain returns in the cases below. Not a credential and
#: not shaped like one: a suite that needs a real secret to prove a
#: real secret is read has mislaid the boundary it is testing.
SENTINEL_SECRET = "sentinel-app-secret"

#: The app id used wherever the index is needed. An index, not a
#: secret, which is why it can live in the environment at all.
SENTINEL_APP_ID = "cli_sentinel"


@pytest.fixture(autouse=True)
def _isolated_secret_state(monkeypatch):
    """Start every test from an empty environment and an empty cache.

    Three things, each for a specific reason.

    The environment is cleared so a developer's own ``FEISHU_APP_ID``
    cannot make a "the index is missing" assertion pass, and so
    ``PDT_SECRET_READER_PATH`` — which the project-root ``.env`` exports
    into this process on any machine that has migrated its keychain —
    cannot point the read at a binary other than the one a test stubbed.
    The cache is
    cleared because the provider memoises a resolution for the life of
    the process, and a test that resolves a secret leaves an entry
    behind for the next test in the file. The platform is forced to
    macOS so the keychain-enabled cases do not depend on which machine
    runs the suite — the keychain is a platform facility, and a case
    that quietly skipped itself on Linux would be a case nobody ran.
    """
    for key in (_SWITCH_KEY, _APP_ID_KEY, _APP_SECRET_KEY, _READER_PATH_KEY):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    credentials.reset_cache()
    yield
    credentials.reset_cache()


def _keychain_holds(sentinel, monkeypatch) -> None:
    """Put ``sentinel`` where the client will find it, the way it arrives.

    Stubbed at the process boundary rather than above it, so everything
    below the client stays real: the switch is read, the platform is
    read, the spec table is consulted, the payload is decoded. Only the
    one call that would reach the keychain of the machine running the
    suite is replaced. Replacing the provider's *return value* instead
    would let this pass on a client that still read the plaintext
    variable whenever the ambient environment happened to carry one —
    which is the exact regression the suite exists to catch.

    Stubbing the keychain is no longer enough on its own. The process
    the client runs in is the *server*, and the server is handed its
    secrets over a descriptor by the launcher — it does not read the
    keychain. So this helper finishes the journey the way
    :mod:`backend.secret_launcher` does: publish what the keychain
    returned onto a pipe, and name the descriptor in the environment.
    Both halves stay real, so a client that stopped going through the
    provider still fails here.
    """
    def fake_run(argv, timeout):
        assert argv[0].endswith("security"), argv
        assert "-a" in argv and "-w" in argv
        return sentinel.encode("utf-8") + b"\n"

    monkeypatch.setattr(credentials, "_run_security", fake_run)

    fd = credentials.publish_secret_fd("feishu_app_secret")
    assert fd is not None, "the stubbed keychain should have yielded a value"
    monkeypatch.setenv(
        credentials.secret_fd_env_var("feishu_app_secret"), str(fd)
    )
    credentials.reset_cache()


def _keychain_has_nothing(monkeypatch) -> None:
    """Make the keychain read fail the way a missing item does.

    A nonzero exit — the ordinary case for a machine where the switch
    was turned on and the item was never added.
    """
    monkeypatch.setattr(
        credentials,
        "_run_security",
        lambda argv, timeout: None,
    )


def _sdk_absent(monkeypatch) -> None:
    """Make ``import lark_oapi`` fail the way a missing install does.

    ``None`` in ``sys.modules`` is the interpreter's own spelling of
    "this import cannot succeed", which is what the constructor turns
    into ``FeishuUnavailable``. Deleting the entry instead would leave
    the real SDK importable wherever it happens to be installed, and a
    test that depends on the SDK being absent is a test whose result
    depends on the runner.
    """
    monkeypatch.setitem(sys.modules, "lark_oapi", None)


# ---------------------------------------------------------------------------
# The secret comes from the provider
# ---------------------------------------------------------------------------


def test_app_secret_comes_from_provider(monkeypatch):
    """With the keychain enabled, the client takes the provider's value.

    ``FEISHU_APP_SECRET`` is left unset for the whole test, and the
    provider is asked for ``feishu_app_secret`` by logical name. A
    client still reading the variable would find nothing there and
    disable itself, so this fails loudly on a half-migrated client
    instead of passing on whatever the ambient environment happened to
    carry.
    """
    monkeypatch.setenv(_SWITCH_KEY, "0")
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)
    _keychain_holds(SENTINEL_SECRET, monkeypatch)

    client = FeishuClient()

    assert client.app_secret == SENTINEL_SECRET
    assert _APP_SECRET_KEY not in os.environ


def test_app_secret_falls_back_to_the_plaintext_variable_when_disabled(
    monkeypatch,
):
    """With the keychain off, the provider still answers — from the env.

    The migration moved *where the decision is made*, not whether the
    environment is a valid source. On a CI runner, a container, or
    Linux there is no keychain, and refusing to read the variable there
    would disable a transport that is perfectly configured. The test
    goes through the provider rather than setting the variable and
    reading it directly, so it also pins that the client is not the
    thing doing the deciding.
    """
    monkeypatch.setenv(_SWITCH_KEY, "1")  # fail-closed: any value disables
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)
    monkeypatch.setenv(_APP_SECRET_KEY, SENTINEL_SECRET)

    client = FeishuClient()

    assert client.app_secret == SENTINEL_SECRET
    assert credentials.secret_source(LOGICAL_NAME) == "os.environ"


def test_explicit_constructor_arguments_still_win(monkeypatch):
    """A caller that supplies the secret is not overruled by the provider.

    The constructor signature is unchanged, and a caller that already
    resolved the secret some other way must not have it silently
    replaced by a second, different resolution.
    """
    monkeypatch.setenv(_SWITCH_KEY, "0")
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)
    _keychain_holds("from-the-keychain", monkeypatch)

    client = FeishuClient("cli_other", "from-the-caller")

    assert client.app_id == "cli_other"
    assert client.app_secret == "from-the-caller"


# ---------------------------------------------------------------------------
# The app id stays where it was
# ---------------------------------------------------------------------------


def test_app_id_still_reads_os_environ(monkeypatch):
    """The index is not a secret, and stays in the environment.

    The provider's own spec table names ``FEISHU_APP_ID`` as the
    account a keychain item is looked up by — the key that says *which*
    item, not the item. The keychain is unreachable without it, so
    there is nothing for the provider to resolve it from; moving this
    read to the provider would leave the lookup with no index at all.
    """
    monkeypatch.setenv(_SWITCH_KEY, "0")
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)
    _keychain_holds(SENTINEL_SECRET, monkeypatch)

    client = FeishuClient()

    assert client.app_id == SENTINEL_APP_ID
    assert client.app_id == os.environ[_APP_ID_KEY]


# ---------------------------------------------------------------------------
# Fail-fast: three ways to be unavailable, one of them a message that lies
# ---------------------------------------------------------------------------


def test_fail_fast_message_no_longer_claims_environment(monkeypatch):
    """The disabled message must not send the operator to the wrong place.

    The old text was ``FEISHU_APP_ID / FEISHU_APP_SECRET not set in
    environment``. After the migration that sentence is false twice
    over: the secret need not be in the environment at all on a
    keychain deployment, and the string is the single ERROR line the
    operator sees when the notifier disables itself — the one place
    where being wrong is expensive.
    """
    monkeypatch.setenv(_SWITCH_KEY, "1")
    _keychain_has_nothing(monkeypatch)

    with pytest.raises(FeishuUnavailable) as excinfo:
        FeishuClient()

    message = str(excinfo.value)
    assert "environment" not in message.lower()
    # Still actionable: both routes a deployment actually has, named.
    assert _APP_ID_KEY in message
    assert _APP_SECRET_KEY in message


def test_sdk_missing_and_credentials_missing_are_distinguishable(monkeypatch):
    """Two different problems must not print the same sentence.

    A missing SDK is fixed by installing a package; missing credentials
    are fixed by configuring them. Both are "the notifier is disabled",
    and the operator reading the one line has to be able to tell which
    one happened — otherwise the first thing they try is always the
    wrong one. Both cases are armed with everything *other* than their
    own fault, so nothing but the fault itself differs.
    """
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)

    # Credentials missing: no descriptor was handed down and nothing is
    # in the environment. The switch is off, which is what makes the
    # provider willing to look in the environment at all — and there is
    # nothing there to find.
    monkeypatch.setenv(_SWITCH_KEY, "1")
    with pytest.raises(FeishuUnavailable) as credentials_failure:
        FeishuClient()
    credentials_message = str(credentials_failure.value)

    # SDK missing: every credential is present and correct. The
    # descriptor is published here rather than above, because a handed-
    # down secret outranks the switch: the switch decides whether *this*
    # process reads the keychain, and a descriptor means the parent
    # already did. Arming this case before the one above would have
    # given it a credential it is not supposed to have.
    monkeypatch.setenv(_SWITCH_KEY, "0")
    _keychain_holds(SENTINEL_SECRET, monkeypatch)
    _sdk_absent(monkeypatch)
    with pytest.raises(FeishuUnavailable) as sdk_failure:
        FeishuClient()
    sdk_message = str(sdk_failure.value)

    assert sdk_message != credentials_message
    # Each names its own cause, so the message alone identifies it.
    assert "lark-oapi" in sdk_message
    assert "lark-oapi" not in credentials_message
    assert _APP_ID_KEY in credentials_message


def test_missing_app_id_raises_unavailable(monkeypatch):
    """No index: unavailable, whatever the secret says.

    The keychain cannot be consulted without the index — the provider
    does not start a process at all in that case — so a keychain
    deployment with no ``FEISHU_APP_ID`` has no secret either. The
    plain variable is not substituted for it.
    """
    monkeypatch.setenv(_SWITCH_KEY, "0")
    monkeypatch.setenv(_APP_SECRET_KEY, SENTINEL_SECRET)
    _keychain_has_nothing(monkeypatch)

    with pytest.raises(FeishuUnavailable):
        FeishuClient()


def test_missing_app_secret_raises_unavailable(monkeypatch):
    """An unresolvable secret is unavailable, not an empty string.

    The provider answers ``None`` for every way a read can fail, so a
    client that treated that as a value would hand an empty secret to
    the SDK and fail at the far end of a push instead of at startup,
    where the notifier could disable itself cleanly.
    """
    monkeypatch.setenv(_SWITCH_KEY, "0")
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)
    _keychain_has_nothing(monkeypatch)

    with pytest.raises(FeishuUnavailable):
        FeishuClient()


def test_missing_sdk_raises_unavailable_before_reading_anything(monkeypatch):
    """The SDK check stays first, and stays a named failure.

    The order is deliberate and unchanged: a machine without the SDK
    should not shell out to a keychain to discover it cannot build a
    client anyway. The exception type is the contract — the notifier
    catches ``FeishuUnavailable`` specifically, so a bare
    ``ImportError`` escaping here would take the generic ``except
    Exception`` branch and log a traceback instead of a reason.
    """
    monkeypatch.setenv(_SWITCH_KEY, "0")
    monkeypatch.setenv(_APP_ID_KEY, SENTINEL_APP_ID)
    _keychain_has_nothing(monkeypatch)

    def explode(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("the keychain was consulted with no SDK to use")

    monkeypatch.setattr(credentials, "_run_security", explode)
    _sdk_absent(monkeypatch)

    with pytest.raises(FeishuUnavailable):
        FeishuClient()
