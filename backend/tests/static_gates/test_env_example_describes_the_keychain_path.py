"""``.env.example`` must describe the credential path the code has.

Why this gate exists
--------------------
``.env.example`` is the only place a user of this repository is told where
a credential comes from. It is copied, read once, and then believed — so a
sentence in it is an instruction, and an instruction that contradicts the
implementation is a defect the moment it ships. The file already carried
one: it told the reader that bot credentials come from the environment,
which stopped being true when the keychain provider landed.

The failure this gate is shaped against is *drift*, not absence. The four
keys below are examples an operator copies, and a copy skips the prose
around it; the prose is the part that goes stale, and it goes stale silently
because nothing reads it. So each claim the provider makes — which keys stay
here, which do not, which command runs, which switch turns the keychain on,
and what happens on a platform that has no keychain — is asserted against
the module that implements it rather than restated in this file. A
credential read that moves, a switch that is renamed, a keychain that is
repointed, or a truth table that changes all break a test here in the same
commit that changes the code.

What is *not* asserted
----------------------
Nothing about the values an operator types. The four keys are examples and
stay commented out, and the file gains no active setting: a commented key
is the whole contract. A key that became active would be copied verbatim by
a deployment that has no credential to put there, which is a startup
failure the file itself would then be causing.

Where the claims come from
--------------------------
``credentials`` is imported rather than re-typed. A gate that keeps its own
copy of the keychain path, the switch name, or the enabling values is a
second source of truth for the same three facts, and it is the one that
goes wrong: the copy cannot be wrong in a way that fails, because nothing
compares it to the module. Here the module is the fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import credentials

#: ``<repo>/.env.example``, resolved from this file's own location so the
#: gate reads the same file whether pytest was launched from the repository
#: root or from ``backend/`` (which is what the CI gate lane does).
#:
#: One level higher than it used to point. The template documented
#: ``backend/.env`` and therefore sat beside the file it described; it now
#: documents ``<repo>/.env`` and sits beside that. The gate is the reason
#: this move is safe rather than silent — a template that drifted away
#: from its file would go on describing a credential path nobody reads.
_ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"

#: The four keys the example file is about: two that stay here because they
#: are the keychain *index*, two that are credentials and therefore do not.
#: Both halves are listed because "the doc changed" is only a defect when
#: the classification changed with it.
INDEX_KEYS: tuple[str, ...] = ("FEISHU_APP_ID", "TELEGRAM_CHAT_ID")
SECRET_KEYS: tuple[str, ...] = ("FEISHU_APP_SECRET", "TELEGRAM_BOT_TOKEN")

#: The dedicated keychain this project keeps its provider secrets in, and
#: the only one the provider opens. Spelled out here rather than imported,
#: so the assertion below is a comparison between two independently written
#: facts: a value derived from ``credentials._KEYCHAIN_PATH`` would confirm
#: whatever the module happened to say, which is the drift this gate exists
#: to catch rather than the thing it is catching it with.
DEDICATED_KEYCHAIN_NAME = "runtime-secrets.keychain-db"

#: The command the provider builds, minus the machine-specific tail. Pinned
#: so the documented mechanism and the executed one cannot disagree.
SECURITY_SUBCOMMAND = "find-generic-password"


def _text() -> str:
    """Return the example file's contents."""
    assert _ENV_EXAMPLE.is_file(), f"missing {_ENV_EXAMPLE}"
    return _ENV_EXAMPLE.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return _text().splitlines()


def _unwrapped() -> str:
    """Return the file's prose as one whitespace-normalised line.

    A claim in a comment file is reflowed freely — the sentence this gate
    exists for was split across two lines, and a substring search over the
    raw text cannot see it. Matching against unwrapped prose means the check
    is about the *claim* and not about where the line happens to break.
    Comment markers come off first, since a wrapped comment line begins
    with ``# `` and that marker is an artefact of the layout too.
    """
    stripped = (line.lstrip().lstrip("#").strip() for line in _lines())
    return " ".join(" ".join(stripped).split())


def _comment_block_above(key: str, span: int = 6) -> list[str]:
    """Return the comment lines directly above the example line for *key*.

    ``span`` is how far to walk upwards looking for them: a block that ends
    more than a few lines above its key is not documentation *of* that key,
    and reading it as such is exactly the mistake this gate is about. The
    search stops at the first non-comment line, so a key's neighbours in a
    different section cannot be borrowed.
    """
    lines = _lines()
    try:
        idx = next(
            i
            for i, line in enumerate(lines)
            if re.match(rf"^\s*#?\s*{re.escape(key)}\s*=", line)
        )
    except StopIteration:  # pragma: no cover - the failure message is the point
        return []

    block: list[str] = []
    for line in reversed(lines[max(0, idx - span) : idx]):
        if not line.lstrip().startswith("#"):
            break
        block.append(line)
    return block


def _comment_text_above(key: str, span: int = 6) -> str:
    """:func:`_comment_block_above` as one lowercased string, for matching."""
    return "\n".join(_comment_block_above(key, span)).lower()


def _active_assignments() -> set[str]:
    """Return the keys the file actually sets — every ``KEY=value`` uncommented.

    A commented line is a comment; what this returns is the set a reader
    who copies the file would end up exporting.
    """
    pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    keys: set[str] = set()
    for line in _lines():
        match = pattern.match(line)
        if match:
            keys.add(match.group(1))
    return keys


# ---------------------------------------------------------------------------
# The sentence that was wrong
# ---------------------------------------------------------------------------


def test_env_example_no_longer_claims_credentials_come_from_the_environment() -> None:
    """The old claim is gone, and so is the phrasing that made it."""

    # The whole sentence, and then the two fragments that carry the claim.
    # Matched against unwrapped prose: the sentence was already split over
    # two comment lines, and a raw-text search would miss a reintroduction
    # that happens to wrap the same way.
    # "nothing else" is deliberately *not* in this list: three words of
    # ordinary prose, which a future sentence about the lookup could use
    # without making the false claim — a ban wide enough to forbid them is
    # a ban that gets deleted the first time it is inconvenient, and then
    # the list it belonged to is gone too.
    prose = _unwrapped()
    gone = (
        "Bot credentials come from the environment, exactly like the "
        "Feishu ones above — nothing else.",
        "Bot credentials come from the environment",
        "exactly like the Feishu ones above",
    )
    still_there = [phrase for phrase in gone if phrase in prose]
    assert not still_there, (
        "``.env.example`` still tells the reader that credentials come from "
        "the environment. That was true before the keychain provider and is "
        "false now, and a reader who follows it configures a plaintext "
        "variable that the provider no longer reads. Phrases still "
        f"present: {still_there}"
    )


# ---------------------------------------------------------------------------
# The keychain path
# ---------------------------------------------------------------------------


def test_env_example_documents_the_keychain_path() -> None:
    """The example file names the keychain the provider actually opens."""
    text = _text()

    # The dedicated keychain, named as the one this release does NOT search.
    # A user who keeps their items in a keychain of their own reads this
    # sentence instead of debugging a lookup that was never going to match.
    assert DEDICATED_KEYCHAIN_NAME in text, (
        f"``.env.example`` must name ``{DEDICATED_KEYCHAIN_NAME}``. The "
        "provider names one keychain file explicitly, and a reader who "
        "filed their items in a keychain of their own otherwise has no way "
        "to tell which one the lookup uses."
    )

    # The keychain the module opens, taken from the module rather than
    # retyped — a gate holding its own copy is the copy that drifts.
    opened = Path(credentials._KEYCHAIN_PATH).name
    assert opened in text, (
        "``.env.example`` must name the keychain the provider opens "
        f"(``{credentials._KEYCHAIN_PATH}``, i.e. ``{opened}``). The doc "
        "and the module decide the same thing, and they must not decide it "
        "twice."
    )

    # The command that performs the read, so the mechanism is documented
    # rather than left to be inferred from a keychain name.
    assert SECURITY_SUBCOMMAND in text, (
        f"``.env.example`` must name the ``security {SECURITY_SUBCOMMAND}`` "
        "lookup. Naming the keychain without naming how it is read leaves "
        "the reader unable to reproduce a read that is not working."
    )


def test_the_documented_dedicated_keychain_is_the_one_the_module_opens() -> None:
    """The "dedicated keychain" of the prose is the module's default.

    The provider's isolation argument rests entirely on the keychain it
    names holding nothing but this project's items. That is false the
    moment the module points at the login keychain — the one container
    whose purpose is to hold everything at once, and which any process able
    to enumerate it can enumerate this project's credentials with. So the
    name the doc calls dedicated and the file the code opens are asserted
    to be one and the same, from both sides.
    """
    opened = Path(credentials._KEYCHAIN_PATH).name
    assert opened == DEDICATED_KEYCHAIN_NAME, (
        f"the provider opens ``{opened}``, but ``{DEDICATED_KEYCHAIN_NAME}`` "
        "is documented as the dedicated keychain this project keeps its "
        "secrets in. Two different keychains means an operator who filed "
        "their item where the doc says gets a lookup that can never match."
    )
    assert "login" not in str(credentials._KEYCHAIN_PATH).lower(), (
        f"``{credentials._KEYCHAIN_PATH}`` is the login keychain. Items "
        "filed there sit in the same container as every other credential "
        "the user has saved and are visible to anything that can enumerate "
        "it, which is what the dedicated keychain exists to avoid."
    )


def test_env_example_no_longer_dismisses_the_dedicated_keychain() -> None:
    """The prose must not tell a reader their keychain is never searched.

    This file used to say a keychain of the operator's own is "NOT
    searched in this release". Once the module was pointed at the
    dedicated keychain that sentence inverted into its opposite: it now
    warns away from the one file the read actually uses, so a reader who
    follows it files the item in the login keychain and then reports the
    secret missing with a correctly configured keychain in hand.
    """
    prose = _unwrapped().lower()
    still_there = [
        phrase
        for phrase in ("not searched", "is not the keychain the provider opens")
        if phrase in prose
    ]
    assert not still_there, (
        "``.env.example`` still tells the reader the dedicated keychain is "
        "not the one the provider opens. It is the one it opens, so the "
        "sentence now warns them away from the only place their item will "
        f"be found. Phrases still present: {still_there}"
    )


def test_env_example_explains_how_the_item_reaches_the_dedicated_keychain() -> None:
    """The add step must put the item where the read looks for it.

    ``add-generic-password`` without a keychain argument files the item in
    the *default* keychain, so following a doc that names the dedicated
    keychain only on the read line produces an item the read cannot see —
    and the resulting ``secret_source`` is "missing", which reads like a
    credential problem rather than a placement one. The doc therefore has
    to carry the step that makes the default be the dedicated keychain
    while the item is added.
    """
    text = _text()
    assert "default-keychain" in text, (
        "``.env.example`` must name the ``security default-keychain`` step. "
        "``add-generic-password`` files an item in the *default* keychain "
        "when no keychain is given, so without this step an operator who "
        "adds the item the way the doc shows ends up with it in the login "
        "keychain, where the lookup that reads the dedicated keychain "
        "cannot see it — and the symptom is a missing secret rather than "
        "an item in the wrong file."
    )


def test_env_example_documents_the_file_descriptor_handoff() -> None:
    """The doc says how the secret reaches a child process."""
    text = _text().lower()
    assert "descriptor" in text, (
        "``.env.example`` must say that a secret read from the keychain "
        "reaches the processes that need it over a file descriptor, so it "
        "never appears in any child process's environment. That is the "
        "property the migration exists for, and a doc that stops at "
        "'read from the keychain' leaves the reader believing the secret "
        "is exported."
    )


# ---------------------------------------------------------------------------
# Which keys stay in this file
# ---------------------------------------------------------------------------


def test_env_example_marks_index_keys_as_staying_in_env() -> None:
    """Both index keys are still configured here, and say why."""
    for key in INDEX_KEYS:
        assert f"# {key}=" in _text(), (
            f"``{key}`` is the account the keychain item is looked up by, "
            "so it stays in this file as a commented example. It is not a "
            "credential: it names the item rather than being the secret."
        )
        block = _comment_text_above(key)
        assert "index" in block or "account" in block, (
            f"the comment above ``{key}`` must say it is the keychain index "
            "(the account the item is found by). A reader cannot tell an "
            "index from a credential by the key's name alone, and getting "
            "it backwards means either a secret in this file or a lookup "
            f"with no account. Comments directly above ``{key}``: {block}"
        )


def test_env_example_keeps_secrets_out_of_the_file() -> None:
    """Both secret keys are documented as keychain values, not as settings."""
    for key in SECRET_KEYS:
        assert f"# {key}=" in _text(), (
            f"``{key}`` keeps its commented example so the reader knows the "
            "name, and what the comment next to it says is what changes."
        )
        block = _comment_text_above(key)
        assert "keychain" in block, (
            f"the comment above ``{key}`` must say the value is read from "
            "the keychain. Without it, a commented example of a secret "
            "next to a key that stays here reads as 'paste your token "
            f"here'. Comments directly above ``{key}``: {block}"
        )


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_env_example_states_the_disable_switch_defaults_to_off() -> None:
    """The counter-intuitive pairing is spelled out, not left to be inferred.

    ``PDT_DISABLE_KEYCHAIN_SECRETS`` is named for what it disables, and the
    state it defaults to is the disabled one. A reader who reads the name
    and stops there concludes the opposite of what the code does on an
    unset variable, and concludes it silently.
    """
    text = _text()

    # The name, checked against the module: renaming the variable without
    # renaming it here would document a switch that does not exist.
    assert credentials._SWITCH_ENV_KEY in text, (
        f"``.env.example`` must name the switch the code reads "
        f"(``{credentials._SWITCH_ENV_KEY}``)."
    )

    lowered = text.lower()
    assert "disable" in lowered, (
        "the switch's name carries the ``DISABLE`` half of its meaning and "
        "the doc must carry it too, or a reader grepping for 'enable' finds "
        "nothing and a reader grepping for 'disable' finds no explanation"
    )
    assert "default" in lowered and "off" in lowered, (
        "the doc must state that the keychain path defaults to *off*: an "
        "unset ``PDT_DISABLE_KEYCHAIN_SECRETS`` means this file is read, "
        "not that the keychain is."
    )
    assert "unset" in lowered, (
        "the doc must say what an unset switch does. The truth table's most "
        "reached row is the one nobody typed, and a reader cannot derive it "
        "from the variable's name."
    )

    # Every value that actually turns the keychain on, from the module. A
    # doc that says ``1`` enables the keychain, or that omits ``false``,
    # is a doc that tells a reader their deployment is using a keychain it
    # is not using.
    for value in sorted(credentials._SWITCH_ENABLING_VALUES):
        assert value in text, (
            f"``.env.example`` must list ``{value}`` as one of the values "
            f"that enables the keychain read. The module accepts "
            f"{sorted(credentials._SWITCH_ENABLING_VALUES)} and nothing else; "
            "a value the doc does not mention is a value a reader will try."
        )


def test_env_example_documents_the_linux_and_windows_fallback() -> None:
    """The two platforms with no keychain say what they do instead."""
    text = _text()
    lowered = text.lower()

    for platform in ("linux", "windows"):
        assert platform in lowered, (
            f"``.env.example`` must say what a {platform} installation does, "
            "not leave the reader to discover it. On both, the keychain "
            "tool does not exist, so the answer is always this file."
        )

    assert "keychain" in lowered and ".env" in lowered, (
        "the Linux/Windows note must land on a concrete behaviour — the "
        "plaintext variable in this file is what those installations read."
    )


# ---------------------------------------------------------------------------
# Nothing active
# ---------------------------------------------------------------------------


def test_four_keys_stay_commented_out() -> None:
    """The four keys — and the switch — remain examples, not settings."""
    text = _text()
    active = _active_assignments()

    for key in INDEX_KEYS + SECRET_KEYS:
        assert f"# {key}=" in text, (
            f"``{key}`` must stay in the file as a commented example."
        )
        assert key not in active, (
            f"``{key}`` is an active setting in ``.env.example``. Every key "
            "in this file is optional for a deployment, and one that is "
            "active is exported by every reader who copies the file — "
            f"currently active keys: {sorted(active)}"
        )

    # The switch joins them: documented, commented, and absent as a setting,
    # so a deployment that copies the file unchanged keeps the default.
    assert "# PDT_DISABLE_KEYCHAIN_SECRETS=" in text, (
        "the switch must appear as a commented example line so a reader can "
        "copy it, rather than as prose they have to spell out themselves."
    )
    assert "PDT_DISABLE_KEYCHAIN_SECRETS" not in active, (
        "``PDT_DISABLE_KEYCHAIN_SECRETS`` must not be an active setting: "
        "copying the file must leave the keychain path off."
    )