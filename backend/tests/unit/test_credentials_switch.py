"""Provider secret resolution: the spec table, the switch, and the cache.

Why this suite exists
---------------------
Provider credentials are read from two different places depending on
how the deployment is set up. On a workstation the secret belongs in
the OS keychain, because an environment variable is readable by every
process of every user and lands in shell history, crash reports and
`ps` output. On a CI runner, in a container, on Linux, there is no
keychain at all, and the environment is the only place a secret can
come from.

`credentials.py` is the one place that decides between them, so the
decision itself is what this suite pins:

* **The spec table** names, for each provider secret, the environment
  variable that holds the secret and the environment variable that
  holds the *index* used to look the item up. A keychain lookup needs
  a key; that key is not a secret and stays in the environment.
  Two entries, no more: the table is a property of *this* project's
  providers, so every row added to it is a decision someone has to
  read and agree with.
* **The switch** decides whether the keychain is consulted at all, and
  it is fail-closed: anything other than the two spellings that mean
  "do not disable" leaves the keychain switched off. A typo in a
  variable name must not silently turn a keychain deployment into one
  that shells out per read.
* **The switch is re-read on every call.** It is an environment
  variable, and a process that decides once at import time cannot be
  corrected without a restart — which is exactly the situation a test
  that patches the environment after import creates.
* **The cache is memoisation with a public invalidation entry.**
  `reset_cache()` exists because the module memoises its resolution
  for the life of the process; a test, or an operator who re-keys a
  secret, needs a way to say "look again" without a restart.

The module is stdlib-only and imports nothing from the notifier
package: it sits below that layer, and a credentials module that
drags in an HTTP client cannot be imported by the code that
*populates* the credentials.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

import credentials

#: Resolved from this file, never written out literally: the module
#: under test lives in ``backend/`` and the repository is cloned at a
#: different path on every machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_DIR / "credentials.py"

#: Every environment variable the module reads. A test that sets one of
#: these must undo it; the autouse fixture below guarantees that even
#: for a test that fails halfway through.
_ENV_KEYS = (
    "PDT_DISABLE_KEYCHAIN_SECRETS",
    "PDT_KEYCHAIN_PATH",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_BOT_TOKEN",
)


#: The module's own platform check, captured at import — before any
#: test patches it. ``test_platform_is_read_on_every_call`` needs the
#: real one back, because it is the test that covers it.
_REAL_IS_MACOS = credentials._is_macos


@pytest.fixture(autouse=True)
def _isolated_credentials_state(monkeypatch):
    """Start every test from an empty environment and an empty cache.

    Both halves are needed. The environment is cleared so that a
    developer's own ``FEISHU_APP_SECRET`` cannot make a "missing"
    assertion pass. The cache is cleared because the module memoises
    across calls: a test that resolves a secret leaves an entry behind
    for the next test in the file.

    The platform is forced to macOS so that the grid does not depend
    on which machine runs the suite. Forcing it is what most tests
    want; the one test that is about the platform check restores the
    real function for itself.
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)
    credentials.reset_cache()
    yield
    credentials.reset_cache()


def _stdlib_module_names() -> frozenset:
    """Return the top-level standard-library module names.

    ``sys.stdlib_module_names`` is the source of truth but arrived in
    3.10; the project's venv is 3.9, so fall back to the builtins plus
    every module file in the stdlib directory. Over-inclusion is the
    safe direction here — the set is only used to *exclude* names from
    the "not standard library" list.
    """
    names = set(sys.builtin_module_names)
    stdlib_attr = getattr(sys, "stdlib_module_names", None)
    if stdlib_attr is not None:
        return frozenset(stdlib_attr) | names
    stdlib_dir = Path(sysconfig.get_path("stdlib"))
    if stdlib_dir.is_dir():
        for entry in stdlib_dir.iterdir():
            if entry.suffix == ".py":
                names.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                names.add(entry.name)
    return frozenset(names)


# ---------------------------------------------------------------------------
# The spec table
# ---------------------------------------------------------------------------


def test_spec_table_has_exactly_two_entries():
    """Two provider secrets, three fields each, and no keychain service.

    The count is part of the contract: a row here is a decision about
    which of this project's secrets may live in a keychain and under
    which index, and a table that grows by accident stops being
    reviewable. The field set is pinned for the same reason — a field
    named ``service`` would be a per-deployment keychain layout baked
    into source, which is the shape the project rule forbids.
    """
    assert set(credentials.SECRET_SPECS) == {
        "feishu_app_secret",
        "telegram_bot_token",
    }

    for name, spec in credentials.SECRET_SPECS.items():
        field_names = {field.name for field in dataclasses.fields(spec)}
        assert field_names == {
            "logical_name",
            "fallback_env_key",
            "account_env_key",
        }, f"{name} carries fields beyond the three that are its own"
        assert "service" not in field_names
        assert spec.logical_name == name
        assert spec.fallback_env_key, f"{name} has no environment fallback"
        assert spec.account_env_key, f"{name} has no keychain index key"
        assert spec.account_env_key != spec.fallback_env_key


def test_spec_table_index_keys_match_the_notifier_configuration():
    """The index key is the identifier the notifier already reads.

    The keychain item is looked up by an account name. For Feishu that
    is the app id, for Telegram the chat id — the two non-secret
    identifiers the notifiers resolve from the environment today. If
    these drift from what the notifiers read, the keychain would be
    searched under a key nothing else in the project ever produces.
    """
    assert (
        credentials.SECRET_SPECS["feishu_app_secret"].fallback_env_key
        == "FEISHU_APP_SECRET"
    )
    assert (
        credentials.SECRET_SPECS["feishu_app_secret"].account_env_key
        == "FEISHU_APP_ID"
    )
    assert (
        credentials.SECRET_SPECS["telegram_bot_token"].fallback_env_key
        == "TELEGRAM_BOT_TOKEN"
    )
    assert (
        credentials.SECRET_SPECS["telegram_bot_token"].account_env_key
        == "TELEGRAM_CHAT_ID"
    )


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

#: (raw value, disabled on macOS, disabled off macOS). Five value
#: classes x two platforms is the whole grid.
_SWITCH_CASES = [
    pytest.param(None, True, True, id="unset"),
    pytest.param("0", False, True, id="zero-enables"),
    pytest.param("false", False, True, id="false-enables"),
    pytest.param("1", True, True, id="one-disables"),
    pytest.param("True", True, True, id="true-disables"),
]


@pytest.mark.parametrize("is_macos", [True, False], ids=["macos", "non-macos"])
@pytest.mark.parametrize(
    "raw,disabled_on_macos,disabled_off_macos", _SWITCH_CASES
)
def test_switch_truth_table_10_cells(
    monkeypatch, is_macos, raw, disabled_on_macos, disabled_off_macos
):
    """Ten cells: five value classes on each of two platforms.

    The non-macOS column is the interesting one. The keychain is a
    macOS facility; on any other platform "disabled" is not a
    configuration choice but a fact, and it holds for every spelling
    of the switch, including the two that enable it elsewhere.
    """
    if raw is not None:
        monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", raw)
    monkeypatch.setattr(credentials, "_is_macos", lambda: is_macos)

    expected = disabled_on_macos if is_macos else disabled_off_macos
    assert credentials.keychain_disabled() is expected


@pytest.mark.parametrize(
    "raw",
    ["", "True", "TRUE", "Yes", " 0 ", "0 ", "false ", "no", "maybe", "-1"],
    ids=[
        "empty",
        "true",
        "true-upper",
        "yes",
        "zero-padded",
        "zero-trailing-space",
        "false-trailing-space",
        "no",
        "nonsense",
        "negative",
    ],
)
def test_switch_variants_fail_closed(monkeypatch, raw):
    """Anything not read as "do not disable" leaves the keychain off.

    The spellings listed here are the ones a real deployment produces:
    a variable exported by a shell as empty, a boolean rendered by a
    tool, a value with a stray space from a ``.env`` file. Each one
    must fall the same way — off. A switch that guesses is a switch
    that one day guesses wrong in the direction of shelling out.
    """
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", raw)
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)

    assert credentials.keychain_disabled() is True


def test_switch_reread_on_every_call(monkeypatch):
    """No import-time snapshot of the switch.

    A module that evaluates the environment once at import binds the
    answer to the process, and a test — or an operator who re-exports
    the variable in a running shell — can no longer change the
    behaviour. Reading on every call costs one dict lookup and removes
    a whole class of "it worked before the restart" defect.
    """
    monkeypatch.setattr(credentials, "_is_macos", lambda: True)

    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    assert credentials.keychain_disabled() is False

    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    assert credentials.keychain_disabled() is True

    monkeypatch.delenv("PDT_DISABLE_KEYCHAIN_SECRETS")
    assert credentials.keychain_disabled() is True


def test_platform_is_read_on_every_call(monkeypatch):
    """The platform comes from ``sys`` at call time, not from a constant.

    The suite above forces the platform through the module's own seam
    so that the grid does not depend on which machine runs it. This
    test covers the seam itself: the real check has to consult
    ``sys.platform`` each time, or patching the seam would be testing
    nothing but the patch.
    """
    monkeypatch.setattr(credentials, "_is_macos", _REAL_IS_MACOS)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    assert credentials.keychain_disabled() is False

    monkeypatch.setattr(sys, "platform", "linux")
    assert credentials.keychain_disabled() is True

    monkeypatch.setattr(sys, "platform", "win32")
    assert credentials.keychain_disabled() is True


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["feishu_app_secret", "telegram_bot_token"])
def test_missing_index_key_resolves_to_missing_without_io(
    monkeypatch, name
):
    """An empty environment resolves to "missing" and reads as None.

    Both halves of the boundary: the label callers branch on, and the
    value they get. A caller that sees ``"missing"`` must be able to
    report a configuration problem, and a caller that gets None must
    not get an exception from a lookup it expected to be total.
    """
    monkeypatch.setenv(credentials.SECRET_SPECS[name].account_env_key, "")
    monkeypatch.setenv(credentials.SECRET_SPECS[name].fallback_env_key, "")

    assert credentials.secret_source(name) == "missing"
    assert credentials.read_secret(name) is None
    assert credentials.secret_available(name) is False


@pytest.mark.parametrize("name", ["notion_token", "smtp_password", ""])
def test_unknown_name_resolves_to_missing(monkeypatch, name):
    """A name that is not in the table is missing, not an exception.

    The table is the set of secrets this project knows how to look up.
    Anything else has no lookup, so it is absent — which is what
    ``secret_source`` already means. Raising would push the "is this
    secret configured" question onto every caller, and one of them
    would answer it differently.
    """
    assert name not in credentials.SECRET_SPECS
    assert credentials.secret_source(name) == "missing"
    assert credentials.read_secret(name) is None
    assert credentials.secret_available(name) is False


@pytest.mark.parametrize(
    "name,fallback_key",
    [
        ("feishu_app_secret", "FEISHU_APP_SECRET"),
        ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
    ],
)
def test_keychain_disabled_falls_back_to_the_environment(
    monkeypatch, name, fallback_key
):
    """With the keychain off, a populated fallback variable is the source.

    This is the CI case: no keychain, secret in the environment. The
    label says so, so a caller can log where a value came from
    without having to know which platform it is running on.
    """
    monkeypatch.setenv(fallback_key, "the-secret")

    assert credentials.secret_source(name) == "os.environ"
    assert credentials.read_secret(name) == "the-secret"
    assert credentials.secret_available(name) is True


@pytest.mark.parametrize(
    "name,index_key",
    [
        ("feishu_app_secret", "FEISHU_APP_ID"),
        ("telegram_bot_token", "TELEGRAM_CHAT_ID"),
    ],
)
def test_keychain_enabled_with_index_key_selects_the_keychain(
    monkeypatch, name, index_key
):
    """The keychain is the source whenever it is enabled and the index is set.

    The index key is a non-secret identifier, so leaving it in the
    environment is what makes a keychain deployment possible at all.
    When both the switch and the index are in place the secret comes
    from the keychain and *not* from the environment variable, even
    when that variable also happens to hold a value — otherwise the
    keychain would be decorative on exactly the machines that
    configured it.

    The read is stubbed at the process boundary, which is where the
    only part of this that belongs to a machine rather than to a
    project begins: the account below is a synthetic value that no
    keychain has an item filed under, so an unstubbed read would exit
    nonzero and report the lookup as missing — a result that says
    something about the runner and nothing about the switch. Everything
    above the stub stays real, which is what makes the recorded
    ``argv`` and the returned value worth asserting: the command line
    says which tool was asked, for which key, in which keychain file,
    and the value that comes back is the stub's rather than the
    ``"plaintext"`` sitting in the environment variable beside it.
    """
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    monkeypatch.setenv(index_key, "account-value")
    monkeypatch.setenv(
        credentials.SECRET_SPECS[name].fallback_env_key, "plaintext"
    )

    started = []

    def _recording_run_security(argv, timeout):
        started.append(list(argv))
        return b"the-keychain-value\n"

    monkeypatch.setattr(credentials, "_run_security", _recording_run_security)

    assert credentials.secret_source(name) == "keychain"
    assert credentials.secret_available(name) is True
    assert credentials.read_secret(name) == "the-keychain-value"

    # One read, and it is the one this test describes: the keychain
    # tool, asked for the item named by the index variable, in the
    # dedicated keychain file.
    #
    # The last argument is spelled out rather than read back from
    # ``credentials._keychain_file()``. That call would make this the
    # strongest-looking assertion in the file and the weakest: it
    # compares the module's answer with itself, so it passes identically
    # whether the module opened this project's keychain or the login
    # keychain, and the recorded command line — the one artefact a
    # reviewer can read without running anything — would then be the
    # proof of the opposite of what it looks like. Naming the file here
    # makes the ``argv`` the evidence: point the module at the login
    # keychain and this line goes red.
    assert len(started) == 1, f"expected one keychain read, got {started}"
    assert started[0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "account-value",
        "-w",
        str(Path.home() / DEDICATED_KEYCHAIN_FILE),
    ]


@pytest.mark.parametrize(
    "name,index_key",
    [
        ("feishu_app_secret", "FEISHU_APP_ID"),
        ("telegram_bot_token", "TELEGRAM_CHAT_ID"),
    ],
)
def test_keychain_enabled_without_index_key_is_missing(
    monkeypatch, name, index_key
):
    """Enabled but unable to name the item: missing, with no fallback.

    The keychain is a lookup by key. With no key there is no lookup,
    and the plaintext environment variable is not substituted for it —
    an operator who asked for a keychain gets a report that the
    keychain is not configured, not a quiet downgrade to a secret that
    every process on the machine can read.
    """
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    monkeypatch.setenv(index_key, "")
    monkeypatch.setenv(
        credentials.SECRET_SPECS[name].fallback_env_key, "plaintext"
    )

    assert credentials.secret_source(name) == "missing"
    assert credentials.read_secret(name) is None
    assert credentials.secret_available(name) is False


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def test_reset_cache_clears_module_cache(monkeypatch):
    """``reset_cache()`` empties the module-level memo.

    The module memoises because a secret is a property of the
    deployment, not of the call site: re-resolving it per call would
    mean re-reading the environment on every push, and re-running the
    keychain lookup with it. The memo is therefore a cache with a
    lifetime the process does not control, and the only way to end
    that lifetime is this function.
    """
    monkeypatch.setenv("FEISHU_APP_SECRET", "the-secret")
    assert credentials.secret_source("feishu_app_secret") == "os.environ"
    assert credentials._CACHE, "resolution did not memoise anything"

    credentials.reset_cache()

    assert credentials._CACHE == {}


def test_cache_memoises_until_reset(monkeypatch):
    """A cached resolution survives an environment change, not a reset.

    This is the behaviour that makes the cache worth having, and the
    reason the invalidation entry has to be public. If a resolution
    were recomputed on every call, ``reset_cache`` would be dead code
    and the keychain would be consulted per read.
    """
    monkeypatch.setenv("FEISHU_APP_SECRET", "first")
    assert credentials.read_secret("feishu_app_secret") == "first"

    monkeypatch.setenv("FEISHU_APP_SECRET", "second")
    assert credentials.read_secret("feishu_app_secret") == "first"
    assert credentials.secret_source("feishu_app_secret") == "os.environ"

    credentials.reset_cache()

    assert credentials.read_secret("feishu_app_secret") == "second"


def test_cache_is_consistent_across_the_three_entry_points(monkeypatch):
    """The three readers agree with each other, cached or not.

    ``secret_source``, ``read_secret`` and ``secret_available`` answer
    three questions about one secret. If they each resolved
    independently they could disagree — a caller that asked "is it
    available" and then read it would see two different worlds, and
    the disagreement would depend on which of them ran first.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "the-secret")

    assert credentials.secret_source("telegram_bot_token") == "os.environ"
    assert credentials.secret_available("telegram_bot_token") is True
    assert credentials.read_secret("telegram_bot_token") == "the-secret"

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert credentials.secret_source("telegram_bot_token") == "os.environ"
    assert credentials.secret_available("telegram_bot_token") is True
    assert credentials.read_secret("telegram_bot_token") == "the-secret"


# ---------------------------------------------------------------------------
# Module shape
# ---------------------------------------------------------------------------


def test_module_imports_only_the_standard_library():
    """The module's imports are stdlib, and none of them is the notifier.

    Two reasons, one rule. First, this module sits *below* the
    notifier layer — a credentials lookup that imported a notifier
    would make the package circular, and would mean a process that
    only needs a secret had to construct a notification client to get
    one. Second, a non-stdlib import is a dependency the module
    cannot be reasoned about in isolation, which is the property that
    lets the whole switch be a pure function.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pytest.fail("credentials.py uses a relative import")
            if node.module:
                roots.add(node.module.split(".")[0])

    assert roots, "no imports found — the parse is not reading the module"
    assert "notifications" not in roots
    assert roots <= _stdlib_module_names(), (
        f"non-stdlib imports in credentials.py: "
        f"{sorted(roots - _stdlib_module_names())}"
    )


def test_module_constants_are_typed_strings():
    """The keychain tool and keychain path are named once, as strings.

    A path assembled at the call site from a home directory is a
    literal that differs on every machine, which is what the
    repository's own path rule exists to prevent. Naming the binary and
    the keychain file once, in this form, keeps the reader's command
    line reviewable in one place.
    """
    assert isinstance(credentials._SECURITY_BIN, str)
    assert credentials._SECURITY_BIN.endswith("/security")
    assert isinstance(credentials._KEYCHAIN_PATH, str)
    assert "keychain" in credentials._KEYCHAIN_PATH.lower()
    assert isinstance(credentials._CACHE, dict)


# ---------------------------------------------------------------------------
# Which keychain the default names
# ---------------------------------------------------------------------------

#: The keychain this project opens, spelled out here rather than derived
#: from the constant under test. A test that computed the expected value
#: from ``credentials._KEYCHAIN_PATH`` would compare the constant with
#: itself and pass against any value at all, which is the shape of the
#: drift this file is pinned against.
DEDICATED_KEYCHAIN_FILE = "Library/Keychains/runtime-secrets.keychain-db"


def test_default_keychain_is_a_dedicated_one_and_never_the_login_keychain():
    """The named keychain holds this project's secrets and nothing else.

    The login keychain is where an operating system keeps *everything*
    the user has ever saved, and any process that can enumerate it can
    enumerate this project's credentials with it. Pointing the read there
    puts the provider's items in the one container whose whole property
    is that it is shared, so a dedicated keychain is named instead.

    The value is relative to the home directory and is joined on at call
    time: an absolute path spelled in source is a path that is wrong on
    every machine but this one.
    """
    path = credentials._KEYCHAIN_PATH

    assert path == DEDICATED_KEYCHAIN_FILE, (
        "the provider must open this project's dedicated keychain. "
        f"Got {path!r}, expected {DEDICATED_KEYCHAIN_FILE!r}."
    )
    assert "login" not in path.lower(), (
        f"{path!r} is the login keychain. Items filed in the login keychain "
        "are visible to every process that can enumerate it, which is the "
        "exposure this project's own keychain exists to avoid."
    )
    assert not Path(path).is_absolute(), (
        f"{path!r} is absolute, so it names one machine's checkout of a "
        "path every other installation resolves differently. Keep it "
        "relative to the home directory."
    )


def test_keychain_path_override_is_read_and_falls_back_to_the_default(
    monkeypatch, tmp_path
):
    """``PDT_KEYCHAIN_PATH`` redirects the read; unset means the default.

    Which file an installation keeps its secrets in is a property of that
    installation, not of this repository, so it is read at runtime rather
    than compiled in. The default still has to be the dedicated keychain:
    the override is an escape hatch, and an escape hatch that silently
    became the fallback would repoint every deployment that never set it.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PDT_KEYCHAIN_PATH", raising=False)

    # Unset: the default, resolved against the home directory in effect.
    assert credentials._keychain_file() == str(
        Path(tmp_path / "home") / DEDICATED_KEYCHAIN_FILE
    )

    # Set: the override wins, resolved the same way. An empty value is the
    # same as an unset one — there is no such keychain as "".
    monkeypatch.setenv("PDT_KEYCHAIN_PATH", "Library/Keychains/elsewhere.keychain-db")
    assert credentials._keychain_file() == str(
        Path(tmp_path / "home") / "Library/Keychains/elsewhere.keychain-db"
    )

    monkeypatch.setenv("PDT_KEYCHAIN_PATH", "")
    assert credentials._keychain_file() == str(
        Path(tmp_path / "home") / DEDICATED_KEYCHAIN_FILE
    )

    # Absolute: used as written. An operator naming a file outside their
    # home directory is stating a fact about their machine, and re-joining
    # it onto the home directory would resolve somewhere else entirely.
    outside = tmp_path / "elsewhere" / "secrets.keychain-db"
    monkeypatch.setenv("PDT_KEYCHAIN_PATH", str(outside))
    assert credentials._keychain_file() == str(outside)


def test_keychain_path_is_resolved_on_every_call(monkeypatch, tmp_path):
    """Neither the home directory nor the override is bound at import.

    Both are environment, and a process that decided once at import cannot
    be corrected without a restart — which is exactly the situation a test
    that redirects the environment after import creates, and the situation
    an operator who exports the override in a later shell finds
    themselves in.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"

    monkeypatch.setenv("HOME", str(first))
    monkeypatch.delenv("PDT_KEYCHAIN_PATH", raising=False)
    assert credentials._keychain_file() == str(first / DEDICATED_KEYCHAIN_FILE)

    monkeypatch.setenv("HOME", str(second))
    assert credentials._keychain_file() == str(second / DEDICATED_KEYCHAIN_FILE)

    monkeypatch.setenv("PDT_KEYCHAIN_PATH", "Library/Keychains/later.keychain-db")
    assert credentials._keychain_file() == str(
        second / "Library/Keychains/later.keychain-db"
    )


def test_override_branch_reaches_the_command_line_as_spelled(
    monkeypatch, tmp_path
):
    """Both keychain branches are pinned on the recorded ``argv``, not on the helper.

    The two tests above assert what ``_keychain_file()`` *returns*. That
    is one step short of the property: the file a deployment opens is the
    one named after ``-w`` on the command line, and a command line is the
    only one of these three facts — constant, helper return, executed
    argv — that can be read off a record without importing anything. This
    test therefore drives a real read and asserts the whole recorded
    argv, for each branch, spelled the same way the default branch is
    spelled above.

    Both halves of the escape hatch are covered, because either one alone
    is a hole. Unset and empty must still open the dedicated keychain, or
    a deployment that never configured the override is silently reading
    somewhere else. Set must open the override, or the variable is
    decorative. A regression in either direction now moves an ``argv``,
    which is the artefact this suite is willing to call evidence.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "0")
    monkeypatch.setenv("FEISHU_APP_ID", "account-value")

    started = []

    def _recording_run_security(argv, timeout):
        started.append(list(argv))
        return b"the-keychain-value\n"

    monkeypatch.setattr(credentials, "_run_security", _recording_run_security)

    def _read_and_return_the_command_line():
        # The module memoises its resolution, so each branch below needs
        # a look again; without this the second read would replay the
        # first one's answer and the override would never reach a process.
        credentials.reset_cache()
        assert credentials.read_secret("feishu_app_secret") == "the-keychain-value"
        return started[-1]

    # Spelled here rather than read from the module, for the reason given
    # in the test above this one.
    expected_prefix = [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "account-value",
        "-w",
    ]

    # Unset: the dedicated keychain, under the home directory in effect.
    assert _read_and_return_the_command_line() == expected_prefix + [
        str(home / DEDICATED_KEYCHAIN_FILE)
    ]

    # Set: the override, and none of the default left in the command.
    monkeypatch.setenv(
        "PDT_KEYCHAIN_PATH", "Library/Keychains/elsewhere.keychain-db"
    )
    assert _read_and_return_the_command_line() == expected_prefix + [
        str(home / "Library/Keychains/elsewhere.keychain-db")
    ]

    # Empty is unset. There is no such keychain as "", and treating the
    # empty value as a path would hand the read to a name that resolves
    # to the home directory itself.
    monkeypatch.setenv("PDT_KEYCHAIN_PATH", "")
    assert _read_and_return_the_command_line() == expected_prefix + [
        str(home / DEDICATED_KEYCHAIN_FILE)
    ]

    # Absolute overrides are used as written, with no home directory
    # prepended — the last spelling of "set", and the one an installation
    # outside its own home tree has to be able to use.
    outside = tmp_path / "elsewhere" / "secrets.keychain-db"
    monkeypatch.setenv("PDT_KEYCHAIN_PATH", str(outside))
    assert _read_and_return_the_command_line() == expected_prefix + [str(outside)]

    # Every command line this test produced named a keychain of its own.
    # The equality assertions above already imply it; stating it over the
    # recorded list is what a reader of a failed run actually looks at.
    for argv in started:
        assert "login" not in argv[-1].lower(), (
            f"the read was pointed at the login keychain: {argv[-1]!r}"
        )


def test_no_subprocess_on_a_non_macos_platform(monkeypatch):
    """Off macOS the module never starts a process.

    The keychain is reached through a system binary. On a platform
    that has no such binary, running it is a guaranteed failure with
    an ugly message, and a machine that is neither macOS nor Linux —
    a Windows box, a build agent — would pay that on every read. The
    tripwire below turns a stray call into a test failure instead of
    a slow path nobody notices.
    """
    def _tripwire(*args, **kwargs):
        raise AssertionError(f"credentials.py started a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", _tripwire)
    monkeypatch.setattr(subprocess, "Popen", _tripwire)
    monkeypatch.setattr(subprocess, "check_output", _tripwire)
    monkeypatch.setattr(credentials, "_is_macos", lambda: False)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "the-secret")

    assert credentials.secret_source("feishu_app_secret") == "os.environ"
    assert credentials.read_secret("feishu_app_secret") == "the-secret"
    assert credentials.secret_available("feishu_app_secret") is True
