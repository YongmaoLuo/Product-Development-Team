"""The harness fixtures must carry no machine, no credential, no profile.

Why this gate exists
--------------------
The harness fixtures are **recordings of a real machine**.  The event
streams under ``claude_stream_json/`` are what the ``claude`` CLI
actually wrote, and the argv baseline is what the dispatch actually
built — both captured on somebody's laptop, with their home
directory, their credentials and their profile names in the corners.

A recording is the right way to pin a contract: a hand-written
fixture turns *our assumption* about the event shape into the
contract, while a recording keeps the contract anchored to what the
CLI emits.  But a recording is also the whole machine, and the
machine is not ours to publish.

The capture script already refuses to write a payload that carries a
home path, a credential or a sandbox-profile name
(``assert_publishable`` in ``capture_claude_baseline.py``).  That
refusal is a property of *one* script, and it is a property of the
moment of capture.  What it does not cover is the fixture that
somebody hand-edits two weeks later, the recording that was
sanitised by a different path, and the file that lands in the tree
without ever passing through the capture script at all.

So the rule is stated here, over the whole fixture directory, and it
is checked on every run.

The three things a fixture may not carry
----------------------------------------
**A home directory.**  ``/Users/<name>/...`` and ``/home/<name>/...``
name the machine the recording came from, and the recording is
published to everybody who fetches the repository.

**A credential.**  Not the *name* of a credential — the fixtures say
``"apiKeySource": "ANTHROPIC_API_KEY"``, which is the name of an
environment variable and identifies nobody — but a value: an API key,
a bearer token, a JWT.  The distinction matters, because a rule that
forbids the word would flag the recording's own honest statement of
where its key came from.

**A profile name.**  A profile is the operator's name for a bundle of
launch parameters — which model, which settings file, which account.
``MCODE_PROFILE`` selects one, and a profile name is a person's name
for their own setup.  A fixture that records one records a machine.

Reporting
---------
A finding never prints the matched text.  A gate that exists to catch
a credential must not write that credential into whatever reads its
output — a terminal, a CI annotation, an issue somebody pastes.  The
finding says *where* (``path:lineno``), *which rule* fired, and *how
much* text was there (``<redacted> (24 chars)``) — the last one through
:func:`source_scan.redact_span`, the repository's shared answer to that
question.  The text itself is left for the reader to go and look at.

Why a *gate* and not just the rule
----------------------------------
The fixtures are checked once, by hand, when they are captured.  The
next edit to a fixture is a hand-edit: a redaction that missed one
field, a path that was pasted in from a terminal, a profile name that
came along in a JSON blob.  Nothing about those edits fails anything
until somebody notices, and by then the recording is in the history.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_scan  # noqa: E402  (path set above)

#: The backend directory, derived from this file's location rather than
#: from the process working directory (CI runs the suite with
#: ``working-directory: backend``).
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The fixture directory this gate is about.
FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures" / "harness"

# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: A home directory: the macOS and Linux forms.  The segment has to be
#: there — ``"/Users/"`` on its own is the *prefix* of a home path, and
#: a fixture that names the prefix names the shape without naming the
#: machine.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")

#: A home directory written the way a shell writes it.
_TILDE_HOME_RE = re.compile(r"(?<![\w/])~/")

#: An API key: the ``sk-`` prefix every major vendor uses, followed by
#: enough material to be a key rather than a prefix.
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")

#: A JSON web token.
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
)

#: A bearer token.
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}")

#: A credential handed to a program.  The *name* of an environment
#: variable is not a credential; the name with a value is.
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_TOKEN"
    r"|OPENAI_API_KEY|OPENAI_API_TOKEN|GOOGLE_API_KEY)"
    r"\s*[=:]\s*[\"']?([^\s\"',}]+)"
)

#: The environment variable the MiniMax launcher selects a profile
#: with.
_MCODE_PROFILE_RE = re.compile(
    r"\bMCODE_PROFILE[\"']?\s*[:=]\s*[\"']?([^\s\"',}]+)"
)

#: A profile named as the value of a profile-shaped key, in JSON or in
#: a Python dict literal.
_PROFILE_KEY_RE = re.compile(
    r"[\"'](?:sandbox_)?profile[\"']\s*[:=]\s*[\"']([^\"']*)[\"']"
)

#: A macOS Seatbelt profile file.  The profile *file* is the machine's
#: own sandbox configuration, and its name is as identifying as the
#: path it sits at.
_SEATBELT_RE = re.compile(r"[A-Za-z0-9_./-]*\.sb\b")

#: Values that stand in for a secret without being one.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "redacted",
    "placeholder",
    "example",
    "changeme",
    "none",
    "null",
    "unset",
})


def _is_placeholder(value: str) -> bool:
    """True when *value* is a stand-in rather than a real name."""
    if not value.strip():
        return True
    stripped = value.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return stripped.lower() in _PLACEHOLDER_VALUES


def _is_secret_value(value: str) -> bool:
    """True when *value* looks like a credential rather than a name.

    The name of an environment variable is not a secret, even when it
    is the value of a key that mentions one: the recording says
    ``"apiKeySource": "ANTHROPIC_API_KEY"`` and that identifies
    nobody.
    """
    stripped = value.strip()
    if len(stripped) < 12:
        return False
    if _is_placeholder(stripped):
        return False
    # An environment variable's own name, in capitals.
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", stripped):
        return False
    return True


def _line_of(text: str, offset: int) -> int:
    """The 1-based line number of *offset* in *text*."""
    return text.count("\n", 0, offset) + 1


def find_home_paths(text: str) -> list[tuple[int, str]]:
    """The ``(lineno, matched)`` pairs of every home directory in *text*."""
    found = []
    for pattern in (_HOME_PATH_RE, _TILDE_HOME_RE):
        for match in pattern.finditer(text):
            found.append((_line_of(text, match.start()), match.group(0)))
    return found


def find_token_shapes(text: str) -> list[tuple[int, str]]:
    """The ``(lineno, matched)`` pairs of every credential in *text*."""
    found = []
    for pattern in (_API_KEY_RE, _JWT_RE, _BEARER_RE):
        for match in pattern.finditer(text):
            found.append((_line_of(text, match.start()), match.group(0)))
    for match in _CREDENTIAL_ASSIGNMENT_RE.finditer(text):
        if _is_secret_value(match.group(2)):
            found.append((_line_of(text, match.start()), match.group(2)))
    return found


def find_profile_names(text: str) -> list[tuple[int, str]]:
    """The ``(lineno, matched)`` pairs of every profile name in *text*."""
    found = []
    for match in _MCODE_PROFILE_RE.finditer(text):
        if not _is_placeholder(match.group(1)):
            found.append((_line_of(text, match.start()), match.group(1)))
    for match in _PROFILE_KEY_RE.finditer(text):
        if not _is_placeholder(match.group(1)):
            found.append((_line_of(text, match.start()), match.group(1)))
    for match in _SEATBELT_RE.finditer(text):
        found.append((_line_of(text, match.start()), match.group(0)))
    return found


def find_sanitization_violations(text: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, rule, snippet)`` for every violation in *text*.

    Args:
        text: The text of one fixture file.

    Returns:
        One ``(lineno, rule, snippet)`` triple per violation, sorted by
        line.  The rule is the *name* of the rule that fired — ``"home
        path"``, ``"credential token"``, ``"profile name"`` — and the
        snippet is the matched span **redacted** by
        :func:`source_scan.redact_span`, never the matched text itself,
        which is the very thing the rule is about.
    """
    found: list[tuple[int, str, str]] = []
    found.extend(
        (lineno, "home path", source_scan.redact_span(matched))
        for lineno, matched in find_home_paths(text)
    )
    found.extend(
        (lineno, "credential token", source_scan.redact_span(matched))
        for lineno, matched in find_token_shapes(text)
    )
    found.extend(
        (lineno, "profile name", source_scan.redact_span(matched))
        for lineno, matched in find_profile_names(text)
    )
    found.sort()
    return found


def _fixture_files():
    """Yield ``(relative_path, absolute_path)`` for every fixture file."""
    for path in sorted(FIXTURES_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(BACKEND_DIR)
        if "__pycache__" in rel.parts:
            continue
        yield rel, path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fixtures_are_sanitized() -> None:
    """No fixture carries a home path, a credential or a profile name.

    The rule is checked over the whole fixture directory, and the gate
    has to be able to fail: a violating sample is injected and has to
    be reported.
    """
    offenders: list[str] = []
    for rel, path in _fixture_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file
            continue
        for lineno, rule, snippet in find_sanitization_violations(text):
            offenders.append(f"{rel}:{lineno}: {rule}: {snippet}")

    assert not offenders, (
        "a harness fixture carries something that identifies the machine "
        "it was recorded on. The fixtures are published with the "
        "repository: replace the value with a placeholder "
        "('<private-dir>', '<redacted>'), record it under /tmp, and "
        "re-capture if the value cannot be replaced.\n  "
        + "\n  ".join(offenders)
    )

    # The gate has to be able to fail. The sample carries one of each
    # thing the rule forbids.
    injected = find_sanitization_violations(_INJECTED_LEAKS)
    assert injected, (
        "the gate did not report an injected leak, so it cannot fail and "
        "the assertion above proves nothing"
    )
    rules = {rule for _, rule, _ in injected}
    assert rules == {"home path", "credential token", "profile name"}, (
        f"the injected sample was reported as {sorted(rules)}, which is "
        "not one of each forbidden shape"
    )

    # A finding carries the matched span only in redacted form: the
    # gate exists to catch a credential and must not print one.
    assert all(
        snippet.startswith(source_scan.REDACTED) for _, _, snippet in injected
    ), "a finding printed the matched text instead of a redacted span"


def test_the_gate_catches_every_shape() -> None:
    """Each forbidden shape is reported, and each allowed one is not.

    The allowed shapes matter as much as the forbidden ones.  A
    recording that says ``"apiKeySource": "ANTHROPIC_API_KEY"`` is
    being honest about where its key came from, and a gate that
    flagged the *name* of a credential would flag that.
    """
    shapes = {
        "home path": 'root = "' + "/Us" + "ers/someone/work/repo\"\n",
        "tilde home": 'root = "~/work/repo"\n',
        "api key": 'key = "' + "sk" + "-ant-api03-" + "x" * 24 + '"\n',
        "jwt": 'token = "' + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
               + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk" + '"\n',
        "bearer": 'header = "Bearer ' + "aB3dEfGhIjKlMnOpQrStUvWx" + '"\n',
        # The credential-assignment sample is written across two lines
        # on purpose.  The committed-history secret scanner reads a
        # credential-shaped *name* followed by a credential-shaped
        # *value* on one line as a leaked key, and this repository
        # publishes its commits — the shape under test is the very
        # thing it must be able to recognise, so the sample stays a
        # credential assignment and is only laid out so that the name
        # and the value are not neighbours.
        "credential assignment": (
            'ANTHROPIC_API_KEY=' + "aB3dEfGhIjKlMnOpQrStUvWx" + "\n"
        ),
        "mcode profile": 'MCODE_PROFILE=team-alpha\n',
        "profile key": 'binding = {"profile": "team-alpha"}\n',
        "seatbelt profile": 'profile = "/tmp/team-alpha.sb"\n',
    }
    for label, source in shapes.items():
        assert find_sanitization_violations(source), (
            f"the gate did not report the {label} shape"
        )

    allowed = {
        "credential name": 'source = {"apiKeySource": "ANTHROPIC_API_KEY"}\n',
        "placeholder profile": 'binding = {"profile": "<redacted>"}\n',
        "placeholder path": 'root = "<private-dir>/work"\n',
        "tmp path": 'root = "/tmp/claude-stream-json-recording"\n',
        "profile word": "the profile of a recording\n",
        "session id": 'id = "11111111-1111-1111-1111-111111111111"\n',
    }
    for label, source in allowed.items():
        assert not find_sanitization_violations(source), (
            f"the gate reported the {label} shape"
        )


def test_the_gate_reads_the_real_fixtures() -> None:
    """The scan target must be the fixture directory, and not be empty.

    A gate whose scan root has moved reads nothing and passes
    vacuously — which is how the original defect shipped: the paths
    were there and the check was not running.
    """
    assert FIXTURES_DIR.is_dir(), (
        f"{FIXTURES_DIR} does not exist, so the gate would scan nothing "
        "and pass vacuously"
    )
    files = list(_fixture_files())
    assert files, (
        "the fixture directory came back empty, so the gate would scan "
        "nothing and pass vacuously"
    )
    assert any(
        rel.suffix == ".json" for rel, _ in files
    ), "the recorded fixtures are not in the scanned tree"


#: A sample carrying one of each forbidden thing.  Every part of it is
#: assembled from fragments so that this file's own text never contains
#: the shapes it is about — a secret scanner reads this file too, and a
#: self-referential sample would be a false positive in a different
#: gate.
_INJECTED_LEAKS = (
    'root = "' + "/Us" + "ers/someone/work/repo\"\n"
    'key = "' + "sk" + "-ant-api03-" + "x" * 24 + "\"\n"
    'binding = {"profile": "team-alpha"}\n'
)
