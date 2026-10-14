"""Scan content against an operator-owned blacklist of forbidden names.

Why the list is not in this repository
--------------------------------------
A blacklist of private proper names cannot be committed: writing the names
down to check for them publishes them, which is the exact thing the list
exists to prevent. So the list lives in ``.config/forbidden-names`` -- a
path ``.gitignore`` already excludes ("Local configuration -- operator-owned,
never committed") -- and this module reads it at run time.

What changed, and why this is not just a move
---------------------------------------------
The list used to live in
``backend/tests/static_gates/test_no_private_names_in_source.py``, as
``PRIVATE_NAME_PHRASES`` / ``PRIVATE_NAME_SHAPES`` behind
``find_private_names()``, and that file was *also* a pytest gate. Two
things were wrong with that arrangement:

1. the names were committed, so they were published on every fetch;
2. the check ran wherever pytest ran, which is not where a leak can still
   be stopped. By the time the suite is green the text is already in a
   commit.

The check therefore moved to the two hooks where refusing is still free --
``pre-commit`` (the text is not in a commit yet) and ``pre-push`` (the
commits are not published yet) -- and the pytest gate went away. That is
this module plus ``scripts/scan_forbidden_names.sh``, wired into
``scripts/pre_commit_gate.sh`` and ``scripts/scan_commit_range.sh``.

Matching is plain case-insensitive substring, on purpose
--------------------------------------------------------
The rule this replaces used a boundary regex::

    (?<![A-Za-z0-9._-])NAME(?![A-Za-z0-9._-])

The character class counts a hyphen as a word character, which means that
pattern cannot match ``lib<SOME-NAME>_rs.dylib`` (the name is glued to a
prefix on the left) and cannot match a plan-id slug such as
``2026xxxx-<name>-something`` (glued on both sides). Those are precisely
the two forms that occur in real leaks, and a rule that cannot match them
is green over exactly the cases it was written for. So: substring, no
boundaries, no ``\\b``. False positives are the right way round -- a
two-second rewrite beats a permanent publication.

List format
-----------
One needle per line. Blank lines, and lines whose first non-space character
is ``#``, are ignored. An entry may be:

``<needle>``
    Matched case-insensitively as a substring of a line.

``re:<pattern>``
    Matched as a case-insensitive regular expression, for the cases a
    literal list cannot express -- a *shape* rather than any one word in it
    (a quote identifier against a real exchange, say). The reported needle
    is the text that actually matched, not the pattern.

Usage
-----
::

    # staged content, the way the pre-commit hook calls it
    python3 scripts/scanners/scan_forbidden_names.py --index

    # the commits about to be pushed, the way the pre-push hook calls it
    python3 scripts/scanners/scan_forbidden_names.py --range origin/main..HEAD

    # a different list, for tests and for a one-off check
    python3 scripts/scanners/scan_forbidden_names.py --list /tmp/list --index

Exit codes
----------
* ``0`` -- the list was read, the content was read, zero needles matched.
* ``1`` -- the list was read, the content was read, at least one needle
  matched. Every hit is reported as ``relative/path:lineno: needle``.
* ``2`` -- **the check did not run,** or did not run over everything it was
  asked to read. The list is missing, holds zero usable needles, or holds a
  pattern that does not compile; the index could not be read; a range
  resolved to no commits; a binary blob was skipped (see *Binary content*
  below), so "no violations in what I read" is not a claim about the commit.

``2`` is load-bearing and is why this module refuses to treat a missing list
as an empty one. An empty list would make the scan a clean pass over a
check nobody configured -- the same "green but never ran" failure that
``scripts/scan_commit_range.sh`` calls out when gitleaks is missing ("A
skipped scan must not read as a passing one"). A hook that cannot tell the
reader "the check did not run" gets bypassed, and the bypass is invisible.

Scope
-----
No path is exempt and no extension is filtered *out of* the read. A private
name is as published inside a test fixture, a docstring, a ``.yaml`` or a
shell script as it is inside a secret, and the only thing that has no
legitimate synthetic stand-in is a name. Both modes therefore read every
text blob -- the index and every commit in the range, tests included. The
extension set in *Binary content* below is not a scope filter: it only ever
decides whether a blob is read despite holding a NUL, never whether a
readable blob is exempt from a needle.

Binary content
--------------
Git calls a blob binary when it holds a NUL byte in the first kilobyte, and
decoding a genuine PNG with ``errors="replace"`` would manufacture findings
out of replacement characters. So the skip is legitimate -- but it used to be
applied to *every* blob, on the reasoning that "a private name is text, and
no encoding turns one into a NUL". That reasoning holds and the skip is still
right for a real binary. It does not survive a ``.py`` whose first byte is
``\0``: the name in it is still text, and this module is the *only* place
the private names are checked at all (it does not run in CI -- a CI runner
has no operator-owned ``.config/forbidden-names`` -- so it is the pre-commit
and pre-push hooks or nothing). One leading NUL byte therefore disabled the
whole check, silently, with ``OK  forbidden-name scan: 0 violations`` in the
output and a ``binary blob(s) skipped`` note in the detail line that changes
no exit code.

The split is by *extension*, not by content, and only for the skip side:

  * a source-suffixed blob (anything in
    :data:`source_scan.SOURCE_EXTENSIONS`) is always read, NUL or not;
  * any other blob with a NUL in the first kilobyte is skipped -- and a
    batch with a skipped blob exits 2, naming the files it did not read.

The degradation is the load-bearing half. Extension-based skipping means a
new file type (``.toml``, a fixture format nobody has met yet) is *not* in
the set, so it is skipped, and without the exit-2 rule that would be this
same bypass with one more step. With it, the worst a wrong extension set can
do is refuse a commit and name the file -- a loud false positive rather than
a silent pass. This is the same split and the same exit contract
``backend/tests/static_gates/staged_content_scan.py`` uses, and the two are
kept worded identically on purpose: a reader who has seen one has seen both.

Content that is not UTF-8
--------------------------
Every blob here is a byte string and every needle is a character string,
and the two cannot be compared until one of them has been decoded. The old
decode was ``errors="replace"``, which settles that question without
looking: an undecodable byte becomes ``U+FFFD``, so a needle holding any
character outside ASCII can never be found in a blob that is not valid
UTF-8 -- not "is unlikely to be found", never -- and the run then printed
``OK  forbidden-name scan: 0 violations`` over bytes it had not actually
read. That is the NUL skip above reached from the other side: there the
file was not read at all; here it was read, and the part of it that could
have answered the question was thrown away before the needles saw it.

ASCII is why this stayed invisible. ASCII is a byte-identical subset of
every ASCII-superset encoding, and ``errors="replace"`` only ever rewrites
bytes *outside* ASCII, so an ASCII needle -- ``zz-scaffold``,
``zz-sample-widgets`` -- is still found verbatim in a latin-1 file. The
bypass is precisely the needles that are not ASCII, and the list is the
operator's, so the trigger surface is theirs to widen at any time.

The decode is strict now, and the failure branch is the whole point:

* **A UTF-8 blob** is decoded with ``errors="strict"``, which for that blob
  is byte-for-byte what the old call produced. A UTF-8 blob cannot hide
  anything from the string pass -- a UTF-8 blob *is* its own UTF-8 view --
  so no byte pass runs over it and the cost is one decode, exactly as
  before.
* **A blob that is not valid UTF-8** is decoded with ``surrogateescape``,
  which is lossless, so line numbering and the report still work, and the
  needles are searched a second time against the **raw bytes**, in every
  encoding :data:`NEEDLE_ENCODINGS` names.

``surrogateescape`` on its own is *not* the fix, and the measurement that
decided that is worth recording: it recovers the bytes but not their
identity. A ``café-project`` needle is ``caf\\xc3\\xa9-project``; in a
latin-1 file the ``\\xe9`` survives decoding as ``U+DCE9`` and still is
not the needle, and across every case in the measurement that chose this
shape the two decoders returned identical verdicts. Searching for the
needle's *own* bytes is what finds it. What closes the gap is a second
encoding, not a better error handler -- :data:`NEEDLE_ENCODINGS` and its
docstring say which and why.

What this does *not* reach, stated rather than left implied: a needle
whose characters are written in a legacy multi-byte encoding -- ``zz-示例`` in
a gb2312 or big5 file -- is still not found. Covering that would mean
guessing the file's encoding from a list that is allowed to hold any
characters at all, and a guessed encoding that fires is a false positive
the operator has to chase down with no way to tell it apart from a real
one. Latin-1 is the one alternative encoding added, because it is the one
that historically holds Western prose and because adding it costs a
second ``find`` over content that is already outside the common path.
Every other legacy encoding remains a named gap, not a solved problem.

Why this module restates the git plumbing
-----------------------------------------
It is stdlib-only and reads git itself rather than importing the plumbing
from ``backend/tests/static_gates/``, matching the other scanners in this
directory. One reason, and it is about the future rather than the present:

thirty of the modules in ``static_gates/`` are pytest gates that
``import pytest`` at module scope, and the plumbing this scanner wants
lives beside them. Today the three non-gate modules there happen to import
under a bare ``python3`` -- the one pytest import in their transitive
closure was removed from
``test_no_operator_attribution_in_source.py`` in the same change that moved
this list out. That is a coincidence of the current import graph, not a
property anybody maintains: the next person to add a ``pytest.mark`` or a
fixture to a rule module would silently give every hook that imports from
that package a dependency on a test framework, and the failure would only
appear on a machine whose ``python3`` is not the venv.

So the plumbing is local. A hook that must always run does not reach into
a directory whose purpose is testing, and it does not depend on an import
graph staying a particular shape.

What *is* imported is the extension vocabulary --
``source_scan.SOURCE_EXTENSIONS``, below. The distinction is deliberate:
restating the plumbing here would be a second implementation of something
that has to agree, while restating the extension set would be a second
*decision* about which files are worth reading, and the two copies would
drift. ``source_scan`` is stdlib-only (``collections.abc`` and ``pathlib``),
it is a walker rather than a pytest gate, and the pre-commit stage already
depends on that directory: ``scripts/scan_staged_privacy.sh`` runs
``staged_content_scan.py`` from it, which imports the same module. So this
import adds no dependency the stage did not already carry.

``scripts/check_commit_msg.py`` reaches into the same directory from a bare
``python3`` commit-msg hook for the same reason, and fails closed with exit
2 if the import does not resolve -- which this module does too.

What is *not* restated here is a privacy rule. The plan-id shape, the
attribution phrases, the home-path patterns and the local-measurement
patterns have exactly one definition each and keep it -- those rules stay
where they are, in the tree gates, and are unchanged by this module.
"""

from __future__ import annotations

import argparse
import bisect
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Repository root -- this file lives at ``scripts/scanners/``, two levels
#: below the checkout.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the list lives, relative to the checkout. Not configurable by
#: default on purpose: one well-known path is one thing to remember, and it
#: is the path the two hooks and the failure message both name.
DEFAULT_LIST_REL = Path(".config") / "forbidden-names"

#: The committed template the failure message points at. Carries no real
#: name, so it can be tracked.
TEMPLATE_REL = Path("example") / "forbidden-names.example"

#: Prefix marking an entry as a regular expression rather than a literal.
REGEX_PREFIX = "re:"

#: Encodings a needle is additionally searched as, on a blob that is not
#: valid UTF-8. The needle's characters are encoded into each of these and
#: the raw bytes are searched for the result.
#:
#: ``utf-8`` is the list's own encoding, so the form is usually a no-op --
#: it is listed to make the set explicit rather than to add coverage.
#: ``latin-1`` is the one that earns its place: it is a total map from
#: bytes 0-255 onto U+0000-U+00FF, so *every* character of a needle that
#: latin-1 can represent is representable, and the file written by a
#: Western-locale editor decades ago is a byte string in exactly that map.
#: It costs one extra ``find`` per needle, and only on content that has
#: already failed a strict UTF-8 decode.
#:
#: Nothing else is listed. ``gb2312``/``big5``/``shift_jis`` would be
#: guessing at a file's encoding from a list that may hold any characters
#: at all, and each guess that fires is a false positive the operator
#: cannot tell apart from a real finding. An operator who needs one can
#: add it here knowing exactly what it will and will not catch.
NEEDLE_ENCODINGS: tuple[str, ...] = ("utf-8", "latin-1")

#: A NUL byte in the first kilobyte is git's own definition of binary.
#: Decoding a genuine binary as UTF-8 with ``errors="replace"`` would
#: manufacture findings out of replacement characters. This decides only
#: *whether a blob may be skipped* -- never whether a blob is scanned; see
#: ``_is_scannable`` and the *Binary content* section of the module
#: docstring for which blobs that actually is.
#:
#: Deliberately a second definition rather than an import:
#: ``backend/tests/static_gates/staged_content_scan.py`` holds the same
#: constant for the same reason, and importing it here would pull this hook
#: -- the only surface that checks private names at all -- through the
#: entire import closure of the plan-id gate, for the sake of sharing an
#: integer. The duplication is instead pinned by
#: ``backend/tests/integration/test_forbidden_names_local_list.py``, which
#: may import both modules because a test is not a hook.
_BINARY_PROBE_BYTES = 1024

# One definition of "a first-party source file", shared with the tree gates
# and with ``staged_content_scan``. Imported rather than restated: the set
# says which files are worth reading, and two copies of a decision is how
# one of them ends up missing a file type the other knows about -- which,
# for this particular set, re-opens the bypass the import exists to close.
# ``source_scan`` is stdlib-only, so the bare-``python3`` fallback in
# ``scripts/scan_forbidden_names.sh`` stays genuinely equivalent.
_GATES_DIR = REPO_ROOT / "backend" / "tests" / "static_gates"
if not _GATES_DIR.is_dir():  # pragma: no cover - broken checkout
    print(
        f"error: static gates directory not found: {_GATES_DIR}\n"
        f"error: without it this scanner cannot tell a source file from a "
        f"binary one, and a binary one is exactly what it must not mistake "
        f"for an unreadable file. Nothing has been cleared.",
        file=sys.stderr,
    )
    raise SystemExit(2)
sys.path.insert(0, str(_GATES_DIR))

try:
    from source_scan import SOURCE_EXTENSIONS  # noqa: E402
except ImportError as exc:  # pragma: no cover - broken checkout
    print(
        f"error: cannot import the source-extension set from "
        f"{_GATES_DIR}: {exc}\n"
        f"error: nothing has been cleared.",
        file=sys.stderr,
    )
    raise SystemExit(2)


class ScanError(RuntimeError):
    """The scan could not run. Never conflated with "found nothing"."""


@dataclass(frozen=True)
class Needle:
    """One entry from the list, parsed."""

    #: ``"literal"`` or ``"regex"``.
    kind: str
    #: The substring to look for, or the pattern to compile.
    pattern: str
    #: Compiled for ``kind == "regex"``, else ``None``.
    compiled: "re.Pattern[str] | None"
    #: The same needle over *bytes*: ``bytes`` for a literal, a compiled
    #: ``re.Pattern[bytes]`` for a shape, one entry per (spelling,
    #: encoding) pair that can represent it and is not pure ASCII. Empty
    #: for a pure-ASCII needle, because then the byte pass could only
    #: repeat what the string pass already found -- ASCII bytes are
    #: byte-identical in every ASCII-superset encoding and no decoder
    #: rewrites them.
    byte_forms: tuple["bytes | re.Pattern[bytes]", ...] = ()

    def search(self, line: str) -> str | None:
        """Return the offending text in *line*, or ``None`` if absent."""
        if self.kind == "literal":
            if self.pattern.lower() in line.lower():
                return self.pattern
            return None
        assert self.compiled is not None
        match = self.compiled.search(line)
        return match.group(0) if match else None


def _spellings(needle: str) -> tuple[str, ...]:
    """Return *needle* in the capitalisations it can plausibly appear in.

    Needed because ``bytes.lower()`` folds ASCII and nothing else: in a
    latin-1 file ``café-project`` is ``caf\\xe9-project`` and
    ``CAFÉ-PROJECT`` is ``CAF\\xc9-PROJECT``, and no amount of ASCII
    case-folding turns the second into the first. Case-insensitivity is a
    pinned part of this scanner's contract -- a name shouted in capitals is
    the same leak -- so a byte pass that found only the exact spelling
    would trade one silent miss for a quieter one.

    Four forms, not every possible capitalisation: the needle as written,
    and its lower / upper / title cases. Those are the shapes that turn up
    in a filename, a heading and a shouted string. An arbitrary mixed-case
    spelling inside a non-UTF-8 file stays out of reach -- the string pass
    is what covers mixed case, and it covers it for every UTF-8 blob, which
    is nearly all of them.
    """
    out: list[str] = []
    for candidate in (needle, needle.lower(), needle.upper(), needle.title()):
        if candidate not in out:
            out.append(candidate)
    return tuple(out)


def _byte_forms(
    needle: str, *, is_regex: bool
) -> tuple["bytes | re.Pattern[bytes]", ...]:
    """Return the raw-byte forms of *needle*: every spelling, every encoding.

    *needle* is the entry with any ``re:`` prefix already stripped -- the
    prefix is list syntax, not pattern, and leaving it in would compile a
    bytes pattern that can never match anything. *is_regex* says which kind
    of form to build: a literal becomes ``bytes``, a shape becomes a
    compiled bytes pattern.

    Forms that come out identical (a pure-ASCII needle is the same bytes in
    every spelling and every encoding) collapse to one entry, so nothing is
    searched twice.

    A pure-ASCII form is dropped outright. ``surrogateescape`` leaves every
    ASCII byte exactly as it was, and the string pass folds case with
    ``str.lower()``, which for ASCII is at least as capable as
    ``bytes.lower()`` -- so an ASCII form could only ever repeat a hit the
    string pass has already reported. For the same reason an ASCII *shape*
    is dropped: compiled from a string it folds case the Unicode way, which
    the bytes version cannot.

    A form that cannot be built is left out rather than raising. That is a
    deliberate exception to this module's "never skip silently" rule, and
    it is bounded on purpose: ``r"\\u1234"`` is a valid pattern in string
    mode and ``b"\\u1234"`` is not a valid pattern in bytes mode, so a
    shape that is perfectly correct can have no byte form. Skipping costs
    that one shape's participation in the byte pass and nothing else -- its
    string form still runs, in full, on every blob -- and raising would
    mean one exotic entry could stop the whole check for everybody.
    """
    forms: list[bytes | re.Pattern[bytes]] = []
    for spelling in _spellings(needle):
        for encoding in NEEDLE_ENCODINGS:
            try:
                raw = spelling.encode(encoding)
            except UnicodeEncodeError:
                continue
            if raw.isascii():
                continue
            if is_regex:
                try:
                    form: bytes | re.Pattern[bytes] = re.compile(
                        raw, re.IGNORECASE
                    )
                except re.error:
                    continue
            else:
                # Lower-cased, because that is the side the content is
                # searched in: ``_scan_bytes`` folds the content once and
                # then matches every form against it. Folding a *spelling*
                # rather than the form would defeat the point -- that is
                # precisely why :func:`_spellings` enumerates the
                # capitalisations instead of relying on this call.
                form = raw.lower()
            if form not in forms:
                forms.append(form)
    return tuple(forms)


@dataclass(frozen=True)
class Violation:
    """One needle found in one line of one blob."""

    path: str
    lineno: int
    needle: str

    def format(self) -> str:
        return f"{self.path}:{self.lineno}: {self.needle}"


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def parse_needles(text: str) -> list[Needle]:
    """Turn list *text* into needles, skipping blanks and comments.

    A malformed ``re:`` entry raises :class:`ScanError` rather than being
    dropped. A pattern that does not compile is a check that is not running
    as the operator configured it, which is the same condition as a missing
    list: reporting it as "0 violations" would be a clean pass over a scan
    nobody is getting.
    """
    needles: list[Needle] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith(REGEX_PREFIX):
            pattern = entry[len(REGEX_PREFIX):].strip()
            if not pattern:
                raise ScanError(
                    f"line {lineno} is '{REGEX_PREFIX}' with no pattern; "
                    f"write a pattern after it, or delete the line"
                )
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ScanError(
                    f"line {lineno}: '{pattern}' is not a valid regular "
                    f"expression: {exc}"
                ) from exc
            needles.append(
                Needle("regex", pattern, compiled, _byte_forms(pattern, is_regex=True))
            )
        else:
            needles.append(
                Needle("literal", entry, None, _byte_forms(entry, is_regex=False))
            )
    return needles


def dedupe(needles: Sequence[Needle]) -> list[Needle]:
    """Drop repeats, keeping the first spelling of each.

    Without this a name listed twice reports the same line twice, and a
    count nobody can read is a count nobody checks. Literals are compared
    case-insensitively, because they match case-insensitively.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Needle] = []
    for needle in needles:
        key = (needle.kind, needle.pattern.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(needle)
    return out


def load_needles(list_path: Path) -> list[Needle]:
    """Read and parse the list, or raise :class:`ScanError`.

    All three failure modes -- absent file, a file with nothing usable in
    it, and a file that is not valid UTF-8 -- raise, and all three are exit
    2 at the CLI. Zero usable needles is not a clean tree; it is an
    unconfigured check.

    The third is here because ``UnicodeDecodeError`` is a ``ValueError``,
    not an ``OSError``, so it used to escape this function uncaught and
    terminate the process out of ``main``'s ``try``. That reported exit 1
    -- which the wrapper in ``scripts/scan_forbidden_names.sh`` reads as
    "the check ran and found violations", and answers with a HOWTO telling
    the reader to go and delete each reported ``path:lineno: needle``.
    The list was never read, so there was no needle and no line, and the
    advice pointed at a leak that does not exist. The commit was still
    refused, so this was never a hole in the check; it was the wrong label
    on a refusal, which is its own kind of lie about what was examined.
    """
    try:
        text = list_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ScanError(
            f"forbidden-name list not found: {list_path}\n"
            f"The pre-commit and pre-push hooks both refuse to pass without "
            f"it, because an absent list and a clean tree are the same thing "
            f"as far as an exit code goes, and a check that silently does "
            f"not run is the failure this repository keeps re-learning.\n"
            f"Create it from the committed template (which contains no real "
            f"name):\n"
            f"    mkdir -p {list_path.parent.as_posix()}\n"
            f"    cp {REPO_ROOT / TEMPLATE_REL} {list_path}\n"
            f"then add one needle per line. The list itself must never be "
            f"committed."
        ) from exc
    except OSError as exc:
        raise ScanError(
            f"forbidden-name list could not be read: {list_path}: {exc}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ScanError(
            f"forbidden-name list is not valid UTF-8: {list_path}: {exc}\n"
            f"The list is read as UTF-8 so that a needle in it names one "
            f"specific string of characters. A list saved in another "
            f"encoding does not: the same bytes are different characters "
            f"under a different decoder, so the needles in it would mean "
            f"something other than what they look like, and this check "
            f"would be running against a rule nobody wrote.\n"
            f"Re-save it as UTF-8, e.g.\n"
            f"    iconv -f <its-current-encoding> -t UTF-8 "
            f"{list_path} > /tmp/list && mv /tmp/list {list_path}\n"
            f"Nothing has been scanned, and nothing has been cleared."
        ) from exc

    needles = dedupe(parse_needles(text))
    if not needles:
        raise ScanError(
            f"forbidden-name list is empty: {list_path}\n"
            f"It parsed to zero usable needles -- every line was blank, a "
            f"'#' comment, or nothing but the '{REGEX_PREFIX}' prefix. That "
            f"is an unconfigured check, not a clean one, so this is reported "
            f"as an error.\n"
            f"Add at least one needle, or restore one from "
            f"{REPO_ROOT / TEMPLATE_REL}."
        )
    return needles


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(repo: Path, args: Sequence[str]) -> bytes:
    """Run a git command, returning stdout, or raise :class:`ScanError`.

    ``check=False`` with an explicit raise rather than ``check=True``, so
    the message can name the command: a bare ``CalledProcessError`` from
    inside a scan is not actionable while somebody is trying to commit.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ScanError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def _parse_batch(out: bytes, expected: int) -> list[bytes | None]:
    """Parse ``git cat-file --batch`` output into blob bodies.

    Measured on this git rather than assumed: ``-z`` changes the *input*
    delimiter to NUL and leaves the *output* shaped the way it is without
    it -- a header line ``<oid> SP <type> SP <size> LF``, then ``<size>``
    raw bytes, then one LF. So the payload is read by length, never by
    splitting on a delimiter, and the terminators are LFs.

    Getting this wrong fails silently in the dangerous direction: a parser
    expecting NULs where git emits LFs reads the header as the whole record
    and every later path desynchronises. A response count that does not
    match the request count is therefore an error, not a short list.
    """
    blobs: list[bytes | None] = []
    pos = 0
    total = len(out)
    while pos < total and len(blobs) < expected:
        newline = out.find(b"\n", pos)
        if newline == -1:
            raise ScanError("git cat-file --batch: unterminated header")
        header = out[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        if header.endswith(" missing"):
            blobs.append(None)
            continue
        fields = header.rsplit(" ", 2)
        if len(fields) != 3 or not fields[2].isdigit():
            raise ScanError(
                f"git cat-file --batch: unrecognised header {header!r}"
            )
        size = int(fields[2])
        end = pos + size
        if end > total:
            raise ScanError(
                f"git cat-file --batch: blob at offset {pos} claims {size} "
                f"bytes but only {total - pos} remain"
            )
        blobs.append(out[pos:end])
        pos = end + 1  # skip the record's trailing LF

    if len(blobs) != expected:
        raise ScanError(
            f"git cat-file --batch returned {len(blobs)} objects for "
            f"{expected} requests; refusing to report a clean scan over a "
            f"partial read"
        )
    return blobs


def _read_blobs(repo: Path, specs: list[str]) -> list[bytes | None]:
    """Read every spec through one ``cat-file --batch``.

    One shell-free batch instead of one subprocess per file. That is the
    difference between a pre-commit hook that is invisible and one that is
    not, and the reason the blob protocol is parsed by length above.
    """
    if not specs:
        return []
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch", "-z", "--buffer"],
        input=("\0".join(specs) + "\0").encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ScanError(
            f"git cat-file --batch failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return _parse_batch(result.stdout, len(specs))


def _is_scannable(rel: Path, content: bytes) -> bool:
    """Return True iff this blob must be read by the scan.

    The extension decides, and the content only ever decides the *skip*
    side. Two halves, and the order is the fix:

    1. A source-suffixed blob (:data:`source_scan.SOURCE_EXTENSIONS`) is
       always read. A NUL byte in it does not stop the private name in it
       from being text; it makes this scanner -- which is the only thing
       that checks these names, since it cannot run in CI -- skippable
       with one byte of the developer's choosing, and reports that as
       ``OK  0 violations``. The replacement characters that decoding
       produces are a cost; skipping the file is a hole.
    2. Anything else with a NUL in the first kilobyte is git's own
       definition of binary, and is skipped -- decoding a PNG with
       ``errors="replace"`` manufactures findings out of replacement
       characters. That half is legitimate, so the cost of getting it
       wrong is paid by the caller instead: a skipped blob makes the whole
       batch exit 2, naming the files it did not read.
    """
    if rel.suffix in SOURCE_EXTENSIONS:
        return True
    return b"\0" not in content[:_BINARY_PROBE_BYTES]


# ---------------------------------------------------------------------------
# The two inputs: the index, and commits in a range
# ---------------------------------------------------------------------------


def iter_index_blobs(repo: Path) -> Iterator[tuple[str, bytes]]:
    """Yield ``(path, content)`` for every staged file.

    The index, not the working tree: they differ exactly when it matters
    most. A file can be edited back to clean after being staged, and the
    commit still carries what was staged.

    ``--diff-filter=ACMR`` drops deletions (no content to scan) and the
    copy/rename *sources* (only the destination is staged). ``-z`` because a
    path may contain a newline, and a newline-separated list would silently
    split one file in two -- one of which may scan clean.
    """
    out = _git(
        repo,
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
    )
    paths = [
        chunk.decode("utf-8", "replace")
        for chunk in out.split(b"\0")
        if chunk
    ]
    if not paths:
        raise ScanError(
            f"nothing is staged in {repo}. That is what a `git commit -a` "
            f"with no changes, or a merge, looks like -- and it is not a "
            f"clean scan. Reporting it as one would be indistinguishable "
            f"from having read the index."
        )
    blobs = _read_blobs(repo, [f":{path}" for path in paths])
    for path, blob in zip(paths, blobs):
        if blob is None:
            raise ScanError(
                f"{path} is in the index but git could not read its blob; "
                f"refusing to report a clean scan over a partial read"
            )
        yield path, blob


def iter_range_commits(repo: Path, range_specs: Sequence[str]) -> list[str]:
    """Return the SHAs across every spec, oldest first, de-duplicated.

    A spec that resolves to no commits raises. That is the "up to date"
    case, and the caller -- ``scripts/scan_commit_range.sh`` -- decides
    whether it is legitimate *before* calling this; by the time a spec
    reaches here it is supposed to hold something. An empty range means the
    scan proved nothing, and reporting that as success is the vacuous pass
    this whole check exists to avoid.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for spec in range_specs:
        args = shlex.split(spec)
        if not args:
            raise ScanError(f"empty range specification: {spec!r}")
        out = _git(repo, ["rev-list", "--reverse", *args])
        for sha in out.decode("utf-8", "replace").splitlines():
            sha = sha.strip()
            if sha and sha not in seen:
                seen.add(sha)
                ordered.append(sha)
    if not ordered:
        raise ScanError(
            f"the range(s) {list(range_specs)} resolve to no commits; "
            f"nothing to scan. That is reported as an error rather than a "
            f"pass, because a scan that inspected nothing must not look "
            f"like one that found nothing."
        )
    return ordered


def iter_commit_blobs(repo: Path, sha: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(path, content)`` for every blob in commit *sha*.

    ``ls-tree -r`` lists every blob, with no extension or path filter: a
    private name is as published in a ``.yaml`` or a ``.md`` as in a
    ``.py``. Non-blob entries -- submodule commits, nested trees -- are
    dropped by *type* rather than being handed to ``cat-file`` and failing.
    """
    tree = _git(repo, ["ls-tree", "-r", sha])
    paths: list[str] = []
    for line in tree.decode("utf-8", "replace").splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or fields[1] != "blob":
            continue
        paths.append(path)

    if not paths:
        return

    blobs = _read_blobs(repo, [f"{sha}:{path}" for path in paths])
    for path, blob in zip(paths, blobs):
        if blob is None:
            raise ScanError(
                f"{sha[:9]}:{path} is listed by ls-tree but has no blob; "
                f"refusing to report a clean scan over a partial read"
            )
        yield path, blob


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


#: A lone surrogate is what ``surrogateescape`` puts where a byte could not
#: be decoded. It is a real code point in ``str`` and it is what makes that
#: decode lossless, but ``print`` cannot encode one -- ``utf-8`` has no
#: representation of it and the default error handler refuses -- so a
#: ``re:`` needle whose match happened to span such a byte would crash the
#: scanner while reporting a finding it had already made. A gate that dies
#: on the way to printing the leak it found is a worse outcome than the
#: mismatched decode it replaced, so every label goes through this first.
#:
#: ``backslashreplace`` rather than ``replace``: it prints the escape
#: (``\udce9``) instead of a substitution character, so the report names the
#: exact byte that could not be read and a reader can tell a mangled match
#: from a clean one.
def _printable(text: str) -> str:
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def scan_text(
    path: str, content: bytes, needles: Sequence[Needle]
) -> tuple[list[Violation], bool]:
    """Apply every needle to one blob.

    Returns ``(violations, scannable)``. ``scannable`` is False only for a
    blob that is binary *and* not source-suffixed; see :func:`_is_scannable`
    and the module docstring for why those two conditions are not the same
    test.

    Two passes, and which ones run is decided by the decode rather than by
    the content:

    * a blob that is valid UTF-8 gets the string pass only. It is decoded
      strictly, which for that blob is what ``errors="replace"`` used to
      produce, and a UTF-8 blob cannot hide a needle from its own UTF-8
      view -- so there is nothing a byte pass would add, and the fast path
      costs exactly what the old code cost.
    * a blob that is not gets the string pass *and* :func:`_scan_bytes`.
      ``surrogateescape`` keeps every byte addressable so lines still
      number correctly, and the raw bytes are searched for the needles'
      own encodings, which is the only thing that can answer "is this
      character in there" for a file that is not UTF-8.

    A violation found by both passes is reported once, at the line the
    string pass named, so the count stays the number of distinct hits a
    reader has to look at rather than the number of ways the scanner found
    the same line.
    """
    if not _is_scannable(Path(path), content):
        return [], False

    try:
        text = content.decode("utf-8")
        raw: bytes | None = None
    except UnicodeDecodeError:
        text = content.decode("utf-8", "surrogateescape")
        raw = content

    violations: list[Violation] = []
    reported: set[tuple[int, str]] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for needle in needles:
            hit = needle.search(line)
            if hit is not None:
                label = _printable(
                    f"re:{hit}" if needle.kind == "regex" else hit
                )
                violations.append(Violation(path, lineno, label))
                reported.add((lineno, label))

    if raw is not None:
        violations.extend(_scan_bytes(path, raw, needles, reported))

    return violations, True


def _scan_bytes(
    path: str,
    raw: bytes,
    needles: Sequence[Needle],
    reported: set[tuple[int, str]],
) -> list[Violation]:
    """Search *raw* for each needle's byte forms; return the new hits.

    Only reached for a blob that is not valid UTF-8. *reported* is the
    string pass's output, both so a line found twice is listed once and so
    this pass adds only what the string pass genuinely missed.

    ``bytes.lower()`` folds ASCII and nothing else, which is what makes the
    case-insensitive literal match work here: it folds the needle and the
    content the same way and both stay byte-aligned. The string pass remains
    the one that understands ``É`` for ``é``; this pass is the one that can
    look at a file where those characters are not UTF-8, and neither can do
    the other's job.

    Line numbers come from a precomputed offset table rather than counting
    newlines per hit, so a file with many hits stays linear.
    """
    if not any(needle.byte_forms for needle in needles):
        return []

    starts = [0]
    newline = raw.find(b"\n")
    while newline != -1:
        starts.append(newline + 1)
        newline = raw.find(b"\n", newline + 1)

    lowered = raw.lower()
    found: list[Violation] = []
    for needle in needles:
        for form in needle.byte_forms:
            spans: Iterator[tuple[int, str]] = _byte_spans(lowered, form, needle.pattern)
            for index, label in spans:
                lineno = bisect.bisect_right(starts, index)
                if (lineno, label) in reported:
                    continue
                reported.add((lineno, label))
                found.append(Violation(path, lineno, label))
    return found


def _byte_spans(
    lowered: bytes, form: "bytes | re.Pattern[bytes]", pattern: str
) -> Iterator[tuple[int, str]]:
    """Yield ``(offset, label)`` for every occurrence of *form*.

    Everything is matched against *lowered*, the lower-cased content, and
    that is correct for both kinds of form. ``bytes.lower()`` rewrites
    ASCII ``A-Z`` and leaves every other byte alone, so a literal folded
    against folded content stays byte-aligned; and a compiled bytes pattern
    already carries ``re.IGNORECASE``, which for a bytes pattern folds
    ASCII and nothing more -- so a non-ASCII byte in the pattern still
    meets the same non-ASCII byte at the same offset.
    """
    if isinstance(form, re.Pattern):
        for match in form.finditer(lowered):
            yield match.start(), f"re:{match.group(0).decode('utf-8', 'replace')}"
        return
    start = 0
    while True:
        index = lowered.find(form, start)
        if index == -1:
            return
        yield index, pattern
        start = index + 1


def scan(
    repo: Path,
    list_path: Path,
    *,
    index: bool,
    range_specs: Sequence[str],
) -> tuple[int, list[str], list[Violation]]:
    """Scan the index or the given ranges.

    Returns ``(scanned, skipped_paths, violations)``. ``skipped_paths``
    names the blobs that were not read, and is part of the result rather
    than a diagnostic count because it changes the exit code: a batch with
    a skipped blob is a batch this scan did not read whole, and reporting
    that as a clean scan is the failure this module exists to avoid.

    Raises :class:`ScanError` when the list cannot be read or the content
    cannot be read, so that "the check did not run" reaches the caller as
    something it must not confuse with "found nothing".
    """
    needles = load_needles(list_path)

    sources: list[Iterator[tuple[str, bytes]]]
    if index:
        sources = [iter_index_blobs(repo)]
    else:
        sources = [
            iter_commit_blobs(repo, sha)
            for sha in iter_range_commits(repo, range_specs)
        ]

    violations: list[Violation] = []
    scanned = 0
    skipped: list[str] = []
    for source in sources:
        for path, content in source:
            found, scannable = scan_text(path, content, needles)
            if not scannable:
                skipped.append(path)
                continue
            scanned += 1
            violations.extend(found)

    if scanned == 0:
        raise ScanError(
            f"{len(skipped)} blob(s) were read and none of them was text; "
            f"refusing to report a clean scan over an empty read"
        )
    return scanned, skipped, violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan_forbidden_names",
        description=(
            "Scan content against the operator-owned forbidden-name list. "
            "Exits 0 clean, 1 on violations, 2 when the check did not run."
        ),
    )
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT),
        help=(
            "repository to read the index or the commits from "
            "(default: this checkout)"
        ),
    )
    parser.add_argument(
        "--list",
        dest="list_path",
        default=None,
        help=(
            f"forbidden-name list to read (default: <repo>/"
            f"{DEFAULT_LIST_REL.as_posix()})"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--index",
        action="store_true",
        help="scan the content staged for the next commit",
    )
    group.add_argument(
        "--range",
        dest="ranges",
        action="append",
        default=[],
        metavar="SPEC",
        help="scan every commit in SPEC (repeatable), e.g. origin/main..HEAD",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(args.repo)
    list_path = (
        Path(args.list_path) if args.list_path else repo / DEFAULT_LIST_REL
    )

    try:
        scanned, skipped, violations = scan(
            repo,
            list_path,
            index=bool(args.index),
            range_specs=args.ranges,
        )
    except ScanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    detail = f"scanned {scanned} file(s) against {list_path}"
    if skipped:
        detail += (
            f" ({len(skipped)} binary blob(s) skipped -- "
            f"the commit was NOT fully read)"
        )
    print(detail)

    if violations:
        print(f"FAIL forbidden-name scan: {len(violations)} violation(s)")
        for violation in violations:
            print(f"  {violation.format()}")

    if skipped:
        # Violations first, then the degradation: the names are real and
        # were found, and a reader who sees only "the check did not run"
        # goes looking for a git problem instead of the name. Same wording
        # and same ordering as ``staged_content_scan``.
        print(
            "ERROR: this scan did not read the whole commit, so it cannot "
            "report a clean result.",
            file=sys.stderr,
        )
        for path in skipped:
            print(
                f"ERROR:   not scanned: {path} "
                f"(binary content, and its extension is not one this scan "
                f"treats as source)",
                file=sys.stderr,
            )
        print(
            "ERROR: Unstage the file(s) above, or run the commit with only "
            "text staged, and commit again. If one of them really is a "
            "binary that must be committed, commit it on its own -- a "
            "batch with nothing else in it still cannot be reported clean, "
            "so `--no-verify` is the honest answer for a deliberate binary "
            "and only for that.",
            file=sys.stderr,
        )
        return 2

    if not violations:
        print("OK  forbidden-name scan: 0 violations")
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the hook
    sys.exit(main())
