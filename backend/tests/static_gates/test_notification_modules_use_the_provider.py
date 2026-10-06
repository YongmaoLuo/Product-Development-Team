"""All three notification modules take their secret from the provider.

Why this gate exists
--------------------
``backend/credentials.py`` owns the question "where does a secret come from"
— the OS keychain where the machine has one, the plaintext variable only
where it does not. Two of the three notification modules authenticate with a
secret, so both have to ask it, and the notifier's provisioning probes have
to ask it too or they disagree with the transports about whether a channel
is configured.

The failure this gate exists to prevent is arithmetic, not dramatic. The
migration touches three files, and a change that lands in two of them leaves
a deployment in a state nobody can see: the transport resolves the token from
a keychain entry, the probe reads the environment, the probe finds nothing,
and the notifier reports "telegram disabled" for a channel whose token is
working. Nothing throws. The channel is simply dead, and the reason it is
dead is not in any message.

That is why this gate checks the **set**, not each file alone. Per-file
checks pass for a half-finished migration; a check that names all three and
requires each to reach the provider is what makes "changed two, forgot the
third" a red build.

The prose, too
--------------
Each of the three files also carries prose about where its secret comes
from, and prose is where the migration drifts back. A fail-fast message that
still says "not set in environment", a disabled log that tells the operator
to fix the environment, a module docstring that never mentions a keychain —
each of those tells the one person who reads them (an operator, mid-incident)
something false, and each is a one-sentence edit to restore.

So the prose is pinned too, by an explicit list rather than by a sweep for
words. A sweep for "environment" across the tree would fire on
``TELEGRAM_CHAT_ID`` — which *is* an environment variable, on purpose, because
a chat id is an index rather than a secret — and a gate that fires on correct
prose is a gate that gets switched off. Every entry below names the module,
the AST node that carries the prose, and what that node may and may not say.

What is deliberately not asserted
---------------------------------
``FEISHU_APP_ID`` and ``TELEGRAM_CHAT_ID`` still come from the environment,
and should: they are indices, not secrets, and hiding a non-secret behind a
keychain lookup would make the routing unreadable to whatever decides where a
card goes. Nothing here touches those reads, and the sibling gate
``test_notification_secrets_stay_out_of_environ.py`` is what keeps the two
lists from crossing — this one says every module asks the provider, that one
says only the provider reads the secret keys.

Why the gate proves it can fail
-------------------------------
Every other test in this file asserts "this detector found nothing".
:func:`test_the_detectors_fire_on_planted_samples` feeds each detector a
sample carrying the drift the rule names and requires a hit — a detector
that reports nothing would leave the rest of the file green while reading
nothing at all. It carries positive controls too: the neighbouring node that
is *correct* has to survive, so a locator that has widened to match
everything is caught as well.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

# Locate the shared first-party source walker the way the sibling gates in
# this directory do: the package is colocated with this file, and pytest's
# own import-mode insertion is not something to depend on.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan  # noqa: E402  (post-sys.path adjustment)

#: The three modules that authenticate with a secret, plus the notifier
#: whose provisioning probes decide whether a channel counts as configured.
#: Named relative to the repository root and resolved against it, never the
#: working directory: CI runs this lane with ``working-directory: backend``,
#: where a relative ``backend/`` root does not exist.
NOTIFICATION_MODULES: tuple[Path, ...] = (
    Path("backend/notifications/feishu_client.py"),
    Path("backend/notifications/feishu_notifier.py"),
    Path("backend/notifications/telegram_client.py"),
)

FEISHU_CLIENT = Path("backend/notifications/feishu_client.py")
FEISHU_NOTIFIER = Path("backend/notifications/feishu_notifier.py")
TELEGRAM_CLIENT = Path("backend/notifications/telegram_client.py")

#: The provider every one of them has to ask.
PROVIDER_MODULE = "credentials"

#: The one call that means "resolved a secret for me". Two spellings reach
#: it — ``read_secret(...)`` after ``from credentials import read_secret``
#: and ``credentials.read_secret(...)`` — and a gate that knew only the
#: second would report ``feishu_client.py`` as not asking anything, which
#: is exactly backwards: it is the module that got there first.
READ_SECRET = "read_secret"

#: ``env`` and everything built on it — ``environment``, ``environments``,
#: ``Environmental``, ``env``. Both spellings make the same claim, and the
#: migration drifted back in both: the pre-migration disabled log ended
#: "until env is fixed" while the pre-migration fail-fast said "not set in
#: environment". Matching only the long form would have caught one of them.
#: Case-insensitive because ``Environment`` opens a sentence, and word-
#: anchored so ``TELEGRAM_CHAT_ID`` — which is correctly an environment
#: variable — is not one of these.
_ENV_CLAIM = re.compile(r"\benv\w*", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Locating the files
# ---------------------------------------------------------------------------


def module_path(repo_rel: Path) -> Path:
    """Absolute path of a module named relative to the repository root."""
    return source_scan.resolve_root(repo_rel)


def iter_scanned_modules() -> Iterator[Path]:
    """Yield each notification module, as a repository-relative path.

    The scan set is these three files and nothing else. The rule is about
    the notification path, and widening it to the whole tree would let a
    future file join the contract by accident.

    A module that does not exist raises rather than being skipped. A
    silently skipped module is a hole in the contract: the file was renamed
    or moved, the scan reads two files instead of three, and every
    assertion about it still passes.
    """
    for repo_rel in NOTIFICATION_MODULES:
        if not module_path(repo_rel).is_file():
            raise AssertionError(
                f"{repo_rel} is in this gate's scan set but does not exist. "
                "A module that is skipped rather than reported leaves the "
                "contract with a hole in it — the other two files still "
                "pass, and the one that moved is no longer checked."
            )
        yield repo_rel


def parse_module(repo_rel: Path) -> ast.Module:
    """Parse one of the scanned modules."""
    return ast.parse(
        module_path(repo_rel).read_text(encoding="utf-8"),
        filename=str(repo_rel),
    )


def parse_scanned_modules() -> dict[Path, ast.Module]:
    """Every scanned module, parsed once, keyed by repository-relative path."""
    return {repo_rel: parse_module(repo_rel) for repo_rel in iter_scanned_modules()}


# ---------------------------------------------------------------------------
# Structured detectors
# ---------------------------------------------------------------------------


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def imported_roots(tree: ast.Module) -> set[str]:
    """Top-level module names imported by *tree*, from any import spelling.

    ``import credentials`` and ``from credentials import read_secret`` are
    both an import of the provider; the second is recorded under the
    provider's name rather than under ``read_secret``, so one set answers
    "does this module import the provider" whichever spelling was used.

    A relative import contributes nothing: its target is inside the
    importing package, so there is no top-level name to classify against.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def read_secret_calls(tree: ast.Module) -> list[int]:
    """Line numbers of every ``read_secret`` call in *tree*.

    Both spellings count, so "ask the provider" has one shape here however
    the module imported it.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == READ_SECRET:
            lines.append(node.lineno)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == READ_SECRET
            and _is_name(func.value, PROVIDER_MODULE)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def literal_text(node: ast.AST) -> str | None:
    """Return *node*'s value when it is a plain string, else ``None``.

    Implicit concatenation of adjacent literals arrives from the parser as
    one ``Constant``, so the log lines and error messages this gate reads —
    all written as several adjacent strings — are single nodes. An
    f-string, a bare name, or a ``+`` of two names is not a literal and is
    not judged: the rule is about what the source says, and a name says
    nothing about what it will hold at runtime.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def first_argument_text(call: ast.Call) -> str | None:
    """The literal first positional argument of *call*, or ``None``."""
    if not call.args:
        return None
    return literal_text(call.args[0])


@dataclass(frozen=True)
class Message:
    """One ``raise`` or one log call, with the line it sits on."""

    lineno: int
    text: str
    has_cause: bool = False


def raise_messages(tree: ast.Module, exception: str) -> list[Message]:
    """Every ``raise <exception>(...)`` whose message is a plain string.

    ``has_cause`` records whether the statement carried ``from exc``. The
    distinction is structural rather than textual on purpose: the two
    ``FeishuUnavailable`` paths are told apart by what they catch — the
    missing-SDK path raises *from* the ``ImportError`` — so selecting the
    credential path by its cause does not depend on guessing at wording.
    """
    found: list[Message] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        if not _is_name(node.exc.func, exception):
            continue
        text = first_argument_text(node.exc)
        if text is None:
            continue
        found.append(Message(node.lineno, text, node.cause is not None))
    return sorted(found, key=lambda m: m.lineno)


def logger_messages(tree: ast.Module) -> list[Message]:
    """Every ``logger.<level>(<literal>)`` call in *tree*.

    Only the module logger is read, and only when the first argument is a
    literal: the messages an operator reads at 3am are the ones written out
    in full, and a message assembled from variables at runtime is not prose
    this gate can judge.
    """
    found: list[Message] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.args is None:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not _is_name(func.value, "logger"):
            continue
        text = first_argument_text(node)
        if text is None:
            continue
        found.append(Message(node.lineno, text))
    return sorted(found, key=lambda m: m.lineno)


def find_logger_message(messages: list[Message], needle: str) -> Message | None:
    """The first log line whose literal text contains *needle*.

    ``needle`` identifies *which* line is being judged — it is a locator,
    not the rule. The rule is what the caller asserts about the line this
    returns.
    """
    for message in messages:
        if needle in message.text:
            return message
    return None


def credential_fail_fast(tree: ast.Module) -> Message | None:
    """The ``FeishuUnavailable`` raised for missing credentials.

    Selected as the one with no ``from exc``, which is what distinguishes
    it from the missing-SDK path.
    """
    return next(
        (m for m in raise_messages(tree, "FeishuUnavailable") if not m.has_cause),
        None,
    )


def telegram_disabled_log(tree: ast.Module) -> Message | None:
    """The once-per-process log line saying the channel is not provisioned."""
    return find_logger_message(logger_messages(tree), "telegram channel disabled")


def node_docstring(
    tree: ast.Module, kind: str, name: str
) -> tuple[int, str] | None:
    """``(lineno, docstring)`` of a module-level function or class.

    ``kind`` is ``"def"`` or ``"class"`` — matching on a single node type
    rather than on the name alone, so a module-level *assignment* to a name
    that happens to collide is not read as a docstring holder.
    """
    wanted = ast.FunctionDef if kind == "def" else ast.ClassDef
    for node in tree.body:
        if isinstance(node, wanted) and node.name == name:
            doc = ast.get_docstring(node)
            if doc:
                return node.lineno, doc
    return None


def module_docstring(tree: ast.Module) -> tuple[int, str] | None:
    """``(1, docstring)`` for a module that has one."""
    doc = ast.get_docstring(tree)
    return (1, doc) if doc else None


# ---------------------------------------------------------------------------
# The prose checklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProsePoint:
    """One piece of prose that made a claim about a secret's source.

    ``find`` returns ``(lineno, text)`` for the node that carries the prose,
    or ``(None, None)`` when the node cannot be located. ``forbidden`` is
    fixed phrases that must be gone; ``required`` is phrases that must still
    be there, so that "fix it by deleting the sentence" is not a way
    through.

    The third pass in :func:`prose_findings` judges two of these nodes by
    word rather than by phrase, and reads them from ``WORD_JUDGED``
    instead — a reworded sentence has to be caught even though none of the
    phrases in this table match it.
    """

    module: Path
    what: str
    locator: str
    find: Callable[[ast.Module], tuple[int | None, str | None]]
    forbidden: tuple[str, ...] = ()
    required: tuple[str, ...] = ()


def _fail_fast_point() -> ProsePoint:
    return ProsePoint(
        module=FEISHU_CLIENT,
        what="the credential fail-fast message",
        locator=(
            "the raise FeishuUnavailable in FeishuClient.__init__ that "
            "carries no cause"
        ),
        find=lambda tree: _as_pair(credential_fail_fast(tree)),
        forbidden=("FEISHU_APP_SECRET not set",),
        required=("FEISHU_APP_ID", "keychain", "FEISHU_APP_SECRET"),
    )


def _feishu_class_docstring_point() -> ProsePoint:
    return ProsePoint(
        module=FEISHU_CLIENT,
        what="FeishuClient's class docstring",
        locator="the class FeishuClient docstring",
        find=lambda tree: node_docstring(tree, "class", "FeishuClient"),
        # The pre-migration sentence claimed both values were read from the
        # environment. Quoted whole rather than as the bare word
        # ``environment``, because the corrected docstring legitimately
        # uses that word to say the app *id* stays there — so this point is
        # judged on the sentence, not on the word. The newline matches
        # ``inspect.cleandoc``, which is what ``ast.get_docstring`` runs:
        # it strips the common leading indentation, so the wrapped line
        # arrives with no indent at all.
        forbidden=(
            "``FEISHU_APP_ID`` and ``FEISHU_APP_SECRET``\n"
            "from the environment",
        ),
        required=("credentials", "keychain", "index"),
    )


def _disabled_log_point() -> ProsePoint:
    return ProsePoint(
        module=FEISHU_NOTIFIER,
        what="the telegram-disabled log line",
        locator='the logger call containing "telegram channel disabled"',
        find=lambda tree: _as_pair(telegram_disabled_log(tree)),
        # Naming the secret's fallback key is the drift: it sends the
        # operator to set a variable a keychain install leaves empty on
        # purpose. The chat-id variables stay — they *are* environment
        # configuration, so dropping them would leave nothing to set.
        forbidden=("TELEGRAM_BOT_TOKEN",),
        required=("TELEGRAM_CHAT_ID",),
    )


def _telegram_docstring_point() -> ProsePoint:
    return ProsePoint(
        module=TELEGRAM_CLIENT,
        what="the telegram transport's module docstring",
        locator="the module docstring",
        find=module_docstring,
        forbidden=(
            "configured exactly\nlike every other credential in this "
            "project: from the environment",
        ),
        required=("keychain",),
    )


def _telegram_config_docstring_point() -> ProsePoint:
    return ProsePoint(
        module=TELEGRAM_CLIENT,
        what="load_telegram_config's docstring",
        locator="the def load_telegram_config docstring",
        find=lambda tree: node_docstring(tree, "def", "load_telegram_config"),
        forbidden=("for one send, from the environment",),
        required=("provider",),
    )


def _as_pair(message: Message | None) -> tuple[int | None, str | None]:
    """``(lineno, text)`` for an optional :class:`Message`."""
    return (message.lineno, message.text) if message else (None, None)


#: The finite list of prose points, written out one by one on purpose. The
#: set of places that claimed a secret comes from the environment is a fact
#: about this migration, not a property to be rediscovered by grepping for a
#: word: a grep finds every use of the word, and most of the ones still in
#: the tree are about the *index* keys, which are correct.
PROSE_POINTS: tuple[ProsePoint, ...] = (
    _fail_fast_point(),
    _feishu_class_docstring_point(),
    _disabled_log_point(),
    _telegram_docstring_point(),
    _telegram_config_docstring_point(),
)

#: The two points judged by the bare word ``environment`` rather than by
#: fixed phrase. These are the two an operator reads while the system is
#: broken, and the sentence in both has been reworded more than once, so a
#: phrase list would have to be re-extended at every rewrite. Judging by
#: the word keeps the *rule* ("this line makes no environment claim") rather
#: than the transcription.
WORD_JUDGED_POINTS: tuple[ProsePoint, ...] = (
    ProsePoint(
        module=FEISHU_CLIENT,
        what="the credential fail-fast message",
        locator=(
            "the raise FeishuUnavailable in FeishuClient.__init__ that "
            "carries no cause"
        ),
        find=lambda tree: _as_pair(credential_fail_fast(tree)),
    ),
    ProsePoint(
        module=FEISHU_NOTIFIER,
        what="the telegram-disabled log line",
        locator='the logger call containing "telegram channel disabled"',
        find=lambda tree: _as_pair(telegram_disabled_log(tree)),
    ),
)


def prose_findings() -> list[str]:
    """One line per prose rule the tree breaks.

    Both passes run over the same nodes. The fixed-phrase pass catches the
    sentence that was there when the migration was written; the word pass
    catches a rewording of it. A gate that only knew the original phrasing
    would pass on "not configured — check your env", which says the same
    false thing in fewer words.
    """
    findings: list[str] = []
    trees = parse_scanned_modules()
    for point in PROSE_POINTS:
        lineno, text = point.find(trees[point.module])
        if text is None:
            findings.append(f"{point.module}: could not find {point.locator}")
            continue
        at = f"{point.module}:L{lineno}: {point.what}"
        findings.extend(
            f"{at} says {claim!r}" for claim in point.forbidden if claim in text
        )
        findings.extend(
            f"{at} never says {need!r}"
            for need in point.required
            if need not in text
        )
    for point in WORD_JUDGED_POINTS:
        lineno, text = point.find(trees[point.module])
        if text is None:
            findings.append(f"{point.module}: could not find {point.locator}")
            continue
        at = f"{point.module}:L{lineno}: {point.what}"
        findings.extend(
            f"{at} claims the secret comes from the environment "
            f"({match.group(0)!r})"
            for match in _ENV_CLAIM.finditer(text)
        )
    return findings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_gate_scans_all_three_files() -> None:
    """The scan set is non-empty and holds exactly the three modules.

    Every other check here reads through :func:`iter_scanned_modules` or
    through this same constant, so a scan that went to zero would leave the
    rest of the file green while asserting nothing. The count is asserted
    rather than derived, and every member checked for presence: a fourth
    module joins this gate by a deliberate edit to this test, not by turning
    up in the directory.
    """
    scanned = set(iter_scanned_modules())
    assert scanned, (
        "iter_scanned_modules() yielded nothing. The gate would pass on an "
        "empty scan, which is the one failure mode a set-membership check "
        "cannot detect on its own."
    )
    assert scanned == set(NOTIFICATION_MODULES), (
        f"the scan set is {sorted(map(str, scanned))}; it must be the three "
        "notification modules."
    )
    assert len(NOTIFICATION_MODULES) == 3, (
        "the contract is three files: two transports, and the notifier whose "
        "probes decide whether they are configured. A migration that gave "
        "one of them a fourth authentication path changes the contract, and "
        "this count is where that shows up."
    )


def test_all_three_modules_reference_credentials() -> None:
    """Each module imports the provider and calls it at least once.

    Both halves are required, for different reasons. The import makes the
    dependency visible in review; the call makes it real. A module that
    imports the provider and never calls it has not migrated — it has only
    opened the file and moved on.
    """
    offenders: list[str] = []
    for repo_rel, tree in parse_scanned_modules().items():
        imports_provider = PROVIDER_MODULE in imported_roots(tree)
        calls = read_secret_calls(tree)
        if not imports_provider or not calls:
            offenders.append(
                f"{repo_rel}: imports credentials={imports_provider}, "
                f"read_secret calls={calls}"
            )
    assert not offenders, (
        "every module that authenticates with a secret has to take it from "
        "credentials.read_secret. Migrating two of the three is worse than "
        "migrating none: the transport resolves the secret from the "
        "provider, the probe reads the environment, and a channel with a "
        "working keychain entry reports itself disabled.\n  "
        + "\n  ".join(offenders)
    )


def test_fail_fast_message_has_no_environment_claim() -> None:
    """The credential fail-fast message names the keychain, not the env.

    This is the one string an operator sees when the notifier refuses to
    start. Telling them a secret is "not set in environment" is false once
    the provider owns the value: on a keychain install the variable is empty
    *because the secret is not there*, which is the opposite of the
    problem. The message has to name the keychain as well, or the fix for
    "delete the claim" is a sentence that says nothing at all.
    """
    message = credential_fail_fast(parse_module(FEISHU_CLIENT))
    assert message is not None, (
        "FeishuClient.__init__ raises FeishuUnavailable with a literal "
        "message and no cause. The path that reports missing credentials "
        "is the one that has to be right — it is the notifier's only error "
        "output, and without it there is nothing to judge."
    )
    claims = _ENV_CLAIM.findall(message.text)
    assert not claims, (
        f"{FEISHU_CLIENT}:L{message.lineno}: the credential fail-fast "
        f"message claims the secret comes from the environment ({claims!r}). "
        f"It reads: {message.text!r}. The secret is resolved by the "
        "provider, which prefers the OS keychain; an install with an empty "
        "environment is exactly the one that is working. Name the keychain, "
        "and keep naming FEISHU_APP_SECRET as the no-keychain fallback."
    )
    assert "keychain" in message.text, (
        f"{FEISHU_CLIENT}:L{message.lineno}: the fail-fast message dropped "
        "its environment claim without naming the keychain, so an operator "
        f"on a keychain install is told nothing about where to look. It "
        f"reads: {message.text!r}"
    )


def test_notifier_disabled_log_does_not_blame_env() -> None:
    """The disabled log names the chat id, and not the bot token.

    This line is emitted at most once per process, and it is the whole of
    what an operator has when a Telegram mirror silently stops working. It
    used to end "until env is fixed", which is advice that sends the reader
    to a variable a keychain install deliberately leaves empty — the
    failure it describes then persists after every correct action.
    """
    message = telegram_disabled_log(parse_module(FEISHU_NOTIFIER))
    assert message is not None, (
        f"{FEISHU_NOTIFIER} logs a line containing 'telegram channel "
        "disabled' once per process when the channel is not provisioned. "
        "That line is the only description of the failure an operator gets, "
        "and without it there is nothing to judge."
    )
    assert "TELEGRAM_BOT_TOKEN" not in message.text, (
        f"{FEISHU_NOTIFIER}:L{message.lineno}: the disabled log names "
        "TELEGRAM_BOT_TOKEN. Naming the secret's fallback key tells the "
        "operator to go set a variable, which is where a channel already "
        "migrated to the keychain will stay broken. The token is not an "
        "environment problem; the chat id is."
    )
    claims = _ENV_CLAIM.findall(message.text)
    assert not claims, (
        f"{FEISHU_NOTIFIER}:L{message.lineno}: the disabled log blames the "
        f"environment ({claims!r}). It reads: {message.text!r} — an "
        "instruction to fix env sends the reader away from a working "
        "keychain entry."
    )
    assert "TELEGRAM_CHAT_ID" in message.text, (
        f"{FEISHU_NOTIFIER}:L{message.lineno}: the disabled log no longer "
        "names the chat-id variables. They are routing configuration and "
        "really do come from the environment, so dropping them leaves the "
        f"operator with nothing to set. It reads: {message.text!r}"
    )


def test_telegram_module_docstring_lists_keychain_path() -> None:
    """The telegram transport's module docstring names the keychain.

    The transport's token comes from the provider, and a docstring that
    only says "configured from the environment" is the reason the next
    reader adds an environment fallback — the docstring would be telling
    them to. It has to name the keychain, which is where the provider
    looks first.
    """
    found = module_docstring(parse_module(TELEGRAM_CLIENT))
    assert found is not None, (
        "telegram_client.py has no module docstring. Its token comes from "
        "the provider, and the docstring is where that is explained to the "
        "next person to open the file."
    )
    lineno, doc = found
    assert "keychain" in doc, (
        f"{TELEGRAM_CLIENT}:L{lineno}: the module docstring never mentions "
        "a keychain. It used to say the transport was configured like every "
        "other credential 'from the environment', and a docstring that "
        "says that is an instruction to add an environment fallback — the "
        "half-finished migration the sibling gate exists to catch."
    )
    assert "every other credential" not in doc or "from the environment" not in doc.split(
        "every other credential", 1
    )[1].split(".")[0], (
        f"{TELEGRAM_CLIENT}:L{lineno}: the docstring still describes this "
        "transport as configured from the environment. Only its chat id is "
        "an environment read; the token comes from the provider."
    )


def test_prose_checklist_is_fully_pinned() -> None:
    """Every listed prose point is located, and none of them is violated.

    Both halves are the point. A checklist entry whose node cannot be found
    has to fail — otherwise renaming ``load_telegram_config`` would quietly
    retire the rule about its docstring — and an entry that is located and
    violated has to fail too. Together they keep "the checklist is a list
    of real, still-enforced statements" true.
    """
    findings = prose_findings()
    assert not findings, (
        "prose in the notification modules makes a claim about where a "
        "secret comes from that the provider migration invalidated. Each of "
        "these lines is read by an operator at the moment they are stuck, "
        "and each one sends them somewhere that will not help.\n  "
        + "\n  ".join(findings)
    )
    scanned = set(iter_scanned_modules())
    for point in PROSE_POINTS + WORD_JUDGED_POINTS:
        assert point.module in scanned, (
            f"the checklist lists {point.module}, which is not in the scan "
            "set. A prose point outside the scan is never read, and the "
            "checklist would look enforced while saying nothing."
        )


def test_the_scan_scope_cannot_collapse_without_going_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four ways the scan set can go wrong, each required to fail loudly.

    Every assertion in this file reads through :func:`iter_scanned_modules`,
    so a scan set that quietly shrank would leave the file green while
    checking nothing — and shrinking is easy: a path typo, a file moved, a
    refactor that builds the set from a glob that matches one file. This
    test takes each of those apart and requires
    :func:`test_gate_scans_all_three_files` to go red, which is the only
    thing that distinguishes "the gate passed" from "the gate read
    something and found nothing".

    A gate's non-empty assertion is the one assertion that cannot be
    checked by inspecting its own pass — it is only meaningful if it is
    shown to fire, so it is fired here.
    """
    real = NOTIFICATION_MODULES

    monkeypatch.setattr(sys.modules[__name__], "NOTIFICATION_MODULES", ())
    with pytest.raises(AssertionError, match="yielded nothing"):
        test_gate_scans_all_three_files()

    # A fourth file joins the contract only by a deliberate edit here, so
    # the count is what catches the accidental widening.
    monkeypatch.setattr(
        sys.modules[__name__],
        "NOTIFICATION_MODULES",
        real + (Path("backend/notifications/cards.py"),),
    )
    with pytest.raises(AssertionError):
        test_gate_scans_all_three_files()

    # And the same edit in the other direction: two of the three is not
    # the contract, however small the omission looks.
    monkeypatch.setattr(sys.modules[__name__], "NOTIFICATION_MODULES", real[:2])
    with pytest.raises(AssertionError):
        test_gate_scans_all_three_files()

    # A named module that is not on disk must raise from the walk itself,
    # not be skipped: a skip leaves the other two files checked and the
    # contract one file short, which is the exact failure this gate exists
    # to prevent.
    monkeypatch.setattr(sys.modules[__name__], "NOTIFICATION_MODULES", real)
    monkeypatch.setattr(
        sys.modules[__name__], "module_path", lambda rel: Path("/nonexistent") / rel.name
    )
    with pytest.raises(AssertionError, match="does not exist"):
        list(iter_scanned_modules())


def test_the_detectors_fire_on_planted_samples() -> None:
    """Each detector is handed a sample carrying the drift it names.

    Every other test here asserts "this detector found nothing". A detector
    that silently stopped matching — a refactor of the AST walk, a renamed
    logger, an exception class spelled differently — would leave the file
    green while reading nothing at all. These samples are the drift the
    migration removed, verbatim, and each has to be reported.

    The positive controls matter as much: the neighbouring node that is
    *correct* has to survive. The SDK-missing fail-fast raises *from* its
    ``ImportError`` and is not the credential path; the notifier's
    per-plan ``permanently_disabled`` line is not the "telegram channel
    disabled" one.
    """
    planted = ast.parse(
        "raise FeishuUnavailable(\n"
        '    "FEISHU_APP_ID / FEISHU_APP_SECRET not set in environment"\n'
        ")"
    )
    assert [m.text for m in raise_messages(planted, "FeishuUnavailable")] == [
        "FEISHU_APP_ID / FEISHU_APP_SECRET not set in environment"
    ], (
        "the detector reads a raise message written across several "
        "adjacent literals, which is how the real one is written"
    )
    assert _ENV_CLAIM.findall(planted.body[0].exc.args[0].value) == [
        "environment"
    ], (
        "the environment word is what the fail-fast rule turns on, matched "
        "as a word so Environment and environments count too"
    )
    assert _ENV_CLAIM.findall("Fix the Env before retrying") == ["Env"], (
        "the short spelling is the same claim as the long one, and the "
        "pre-migration disabled log used it — a pattern matching only "
        "'environment' would have missed that sentence"
    )
    assert _ENV_CLAIM.findall("TELEGRAM_CHAT_ID") == [], (
        "the word anchor is what keeps the index key out of it: that name "
        "is not an environment claim, it is the variable an operator is "
        "meant to set. Without \\b a substring sweep would flag every line "
        "that correctly names a chat-id variable, and a gate that flags "
        "correct code gets switched off."
    )

    found = _as_pair(credential_fail_fast(planted))
    assert found[0] == 1 and not found[1] is None, (
        "the credential path is selected by the absence of a cause, so this "
        f"sample must be selected as the credential message; got {found!r}"
    )

    sdk_path = ast.parse(
        "try:\n"
        "    import lark_oapi as lark\n"
        "except ImportError as exc:\n"
        '    raise FeishuUnavailable("lark-oapi SDK is required") from exc\n'
    )
    sdk_raises = raise_messages(sdk_path, "FeishuUnavailable")
    assert len(sdk_raises) == 1 and sdk_raises[0].has_cause, (
        "a raise ... from exc is the missing-SDK path. If it were selected "
        "as the credential message, the gate above would be reading the "
        "wrong string."
    )
    assert credential_fail_fast(sdk_path) is None, (
        "a module with only the SDK path has no credential message to judge"
    )

    planted_log = ast.parse(
        "logger.info(\n"
        '    "[feishu_notifier] telegram channel disabled: "\n'
        '    "TELEGRAM_BOT_TOKEN and a chat id are not both set; "\n'
        '    "skipping telegram push until env is fixed"\n'
        ")\n"
    )
    logged = telegram_disabled_log(planted_log)
    assert logged is not None, "the locator finds the line by its own text"
    assert "TELEGRAM_BOT_TOKEN" in logged.text, (
        "the disabled-log rule turns on the secret's fallback key name, and "
        "this sample carries the key the migration removed"
    )
    assert _ENV_CLAIM.findall(logged.text) == ["env"], (
        "the word pass reads the same node the fixed-phrase pass does, and "
        "catches the env-blaming tail this sample ends with"
    )
    assert WORD_JUDGED_POINTS[1].find(planted_log) == (
        logged.lineno,
        logged.text,
    ), (
        "the word-judged point for the notifier locates the same line the "
        "fixed-phrase point does — two passes over one node, not two nodes"
    )

    other_line = ast.parse('logger.info("[feishu_notifier] no chat_id for plan=%s")\n')
    assert telegram_disabled_log(other_line) is None, (
        "the locator must not match every log line in the module — matching "
        "the wrong one would judge a line that says nothing about the "
        "channel being disabled"
    )

    # The pre-migration class docstring, verbatim, wrapped the way it was
    # wrapped in the source. The newline in the forbidden phrase is the one
    # ``cleandoc`` leaves behind, which is why the sample is parsed rather
    # than compared as a raw string: this detector reads a *cleaned*
    # docstring, so the phrase has to be written the way cleaning renders it.
    old_class = ast.parse(
        'class FeishuClient:\n'
        '    """Minimal Feishu IM client.\n'
        "\n"
        "    Construction reads ``FEISHU_APP_ID`` and ``FEISHU_APP_SECRET``\n"
        "    from the environment. If ``lark-oapi`` is not importable, the\n"
        '    constructor raises ``FeishuUnavailable``."""\n'
        "\n"
        "    pass\n"
    )
    found_doc = node_docstring(old_class, "class", "FeishuClient")
    assert found_doc is not None, "a class docstring is located by its node"
    forbidden = _feishu_class_docstring_point().forbidden[0]
    assert forbidden in found_doc[1], (
        "the pinned phrase is the pre-migration sentence as cleandoc renders "
        f"it; if the two have drifted apart the rule has stopped matching its "
        f"own evidence. Phrase: {forbidden!r}"
    )

    old_config = ast.parse(
        'def load_telegram_config(chat_id=None):\n'
        '    """Build the transport config for one send, from the environment.\n'
        "\n"
        "    Returns:\n"
        '        A dict."""\n'
        "\n"
        "    return {}\n"
    )
    found_config = node_docstring(old_config, "def", "load_telegram_config")
    assert found_config is not None, (
        "a function docstring is located by its node"
    )
    assert _telegram_config_docstring_point().forbidden[0] in found_config[1], (
        "the load_telegram_config rule pins the pre-migration opening "
        "sentence, and this sample is that sentence"
    )

    old_telegram = ast.parse(
        '"""Telegram transport.\n'
        "\n"
        "So the transport lives in this package now, and is configured exactly\n"
        "like every other credential in this project: from the environment.\n"
        '"""\n'
    )
    found_module_doc = module_docstring(old_telegram)
    assert found_module_doc is not None
    assert _telegram_docstring_point().forbidden[0] in found_module_doc[1], (
        "the telegram module-docstring rule pins the pre-migration sentence "
        "as cleandoc renders it, across the line it was wrapped at"
    )
    assert _ENV_CLAIM.findall(found_module_doc[1]) == ["environment"], (
        "and the word pass catches the same sample independently of the "
        "pinned phrasing, so a rewrite is caught too"
    )

    # Positive control for the docstring locators: a function whose name is
    # close but not equal is not the one being judged.
    decoy = ast.parse('def load_telegram_configs(chat_id=None):\n    """Doc."""\n')
    assert node_docstring(decoy, "def", "load_telegram_config") is None, (
        "the docstring locator matches the name exactly — a prefix match "
        "would read the wrong function's prose"
    )

    both = ast.parse(
        "import credentials\n"
        "from credentials import read_secret\n"
        'a = credentials.read_secret("x")\n'
        'b = read_secret("y")\n'
        'c = os.environ.get("TELEGRAM_CHAT_ID")\n'
    )
    assert read_secret_calls(both) == [3, 4], (
        "read_secret is detected in both spellings and only in those: the "
        "chat-id read beside them is an index, not a secret, and must not "
        "be counted"
    )
    assert PROVIDER_MODULE in imported_roots(both), (
        "the provider import is detected from both spellings"
    )

    relative_only = ast.parse("from .cards import build_progress_card\n")
    assert imported_roots(relative_only) == set(), (
        "a relative import names nothing at the top level, so it cannot "
        "count as an import of the provider"
    )
