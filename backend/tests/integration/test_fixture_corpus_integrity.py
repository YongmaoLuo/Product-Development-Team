"""The HITL failure-sample fixture corpus: schema, anchors and privacy.

Background
----------
A verification failure is filed under one of four classes
(:mod:`verification.failure_triage`) and each class implies a different
disposition -- retry it without counting it, rework it and count it,
escalate it to a human, or skip it.  Those rules are pure, and they are
unit-tested on their own.  What nothing else covers is the thing the
rules are actually pointed at: the *recorded* failures they have to act
on, taken from history rather than invented inside a test body.

This module is that corpus.  Four samples, one JSON file each, under
``backend/tests/fixtures/hitl/cases/``:

``infra_429``
    A provider answered ``429`` in the middle of a verification round.
    The environment malfunctioned; the deliverable was never implicated.
``infra_watchdog``
    The verification sub-agent stopped making progress and the watchdog
    killed its subprocess.  Also an environment malfunction.
``criterion_schema_rejected``
    Four rounds of the same ``api_test`` verification point rejected at
    schema time: the criterion cannot be run as written, and no amount
    of reworking the deliverable will ever change that.
``criterion_weaker_replacement``
    A broken criterion whose proposed replacement verifies it with a
    *weaker* method than the one it replaces.

Why one file per case
----------------------
The corpus is read by more than one lane and rewritten by more than one
hand.  A single file holding all four would be a file two tasks edit at
once, and a merge of two such edits loses one of them silently.  One
case per file makes that conflict impossible and makes "which sample is
this" answerable from the path alone.

The directory is shared with the sibling corpora the other HITL tasks
land, and that is deliberate: one file per case is what keeps two tasks
from writing over each other.  It is also why this module reads the
four files it names and not the directory: a sibling case carries its
own schema, and this corpus's readers are not this corpus's contract.

What this module checks
-----------------------
Three properties, one test each:

``test_case_schema``
    Every case carries the required keys, the file name and the ``case``
    field agree, and the class a case claims is the class the production
    disposition table says it is -- the corpus is checked against the
    implementation, not against a copy of it.
``test_quote_substring_invariant``
    A non-passed record's ``evidence_quote`` is a **verbatim** fragment
    of its ``actual_result``, or the case says out loud that the
    classification was defaulted and never earned.
``test_no_private_identifiers``
    No case carries a token, an absolute path or an external project
    identifier.  A sample taken from a real run carries real strings
    unless they are taken out on purpose, and this corpus is committed.

Test command::

    pytest backend/tests/integration/test_fixture_corpus_integrity.py -v
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest


# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Where the corpus lives and what it must contain
# ---------------------------------------------------------------------------

#: The corpus directory.  Derived from this file's own location rather
#: than from the process working directory, so the test reads the same
#: four files no matter where pytest was started from.
CASES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "hitl" / "cases"

#: The cases the corpus is required to hold.  The list is written out
#: rather than discovered, because a corpus that is defined by whatever
#: happens to be on disk cannot tell a deleted sample from a corpus that
#: never had one: both read as "one file, all well".
EXPECTED_CASES: Tuple[str, ...] = (
    "criterion_schema_rejected",
    "criterion_weaker_replacement",
    "infra_429",
    "infra_watchdog",
)

#: The keys every case file carries.
REQUIRED_CASE_KEYS = frozenset({
    "case",
    "verdict",
    "failure_history",
    "verification_plan",
    "expected_disposition",
})

#: The keys every verdict record carries -- the top-level one and every
#: entry of ``failure_history`` alike.  ``failure_class_source`` is not
#: in the set: a record that does not say where its class came from is
#: read as the model having claimed it, which is the case the anchor
#: invariant below is about.
REQUIRED_VERDICT_KEYS = frozenset({
    "verdict",
    "failure_class",
    "actual_result",
    "evidence_quote",
})

#: The keys a history entry carries beyond the verdict ones: which round
#: it was, and where its class came from.
REQUIRED_HISTORY_KEYS = frozenset({
    "round",
    "failure_class_source",
})

#: The keys every ``expected_disposition`` carries.  ``defaulted`` is
#: not among them -- it is the escape hatch for a record whose quote
#: does not verify, and a case that does not need it must not carry it.
REQUIRED_DISPOSITION_KEYS = frozenset({"disposition"})

#: The verdict literals a record may carry.
VERDICT_TOKENS = frozenset({"PASSED", "FAILED"})


# ---------------------------------------------------------------------------
# The privacy scan
# ---------------------------------------------------------------------------
#
# Three families, because "private" is three different mistakes:
# a credential, a filesystem path that names one machine, and an
# identifier that names somebody else's project.  A scan that only knew
# one of them would let the other two through, and the corpus is
# committed to a public repository.
#
# The patterns report a *family and a rule*, never the matched text: a
# gate that prints what it found has published the thing it exists to
# remove.  The same rule the repository's other privacy gates follow.

#: Credential shapes.  Each is a real format, so each is matched by
#: shape rather than by a list of this project's own secrets -- which
#: cannot be written down here anyway.
_CREDENTIAL_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{16,}")),
    ("anthropic-style key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer header", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{12,}")),
    (
        "secret assignment",
        re.compile(
            r"\b(?:api[_-]?key|secret|password|passwd|access[_-]?token"
            r"|auth[_-]?token|private[_-]?key)\b\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9._\-]{8,}"
        ),
    ),
)

#: Filesystem paths that name a machine.  The home-directory forms are
#: the ones that actually leak (they carry the account name); the
#: system-directory forms catch a path that survived with the home part
#: stripped off.
_ABSOLUTE_PATH_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("posix home path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    ("home-relative path", re.compile(r"~(?:/[A-Za-z0-9._-]+)+")),
    ("windows drive path", re.compile(r"[A-Za-z]:\\\\")),
    (
        "system directory path",
        re.compile(
            r"/(?:etc|var|opt|root|private|usr|sbin|bin|srv|mnt|media"
            r"|Volumes|Library|Applications|tmp|proc|sys|dev)(?:/|\b)"
        ),
    ),
)

#: Identifiers of a project that is not this one.  A plan id is a
#: private directory the run actually created, a forge URL names a
#: repository, and a public host names a service the plan was written
#: against -- none of them belong in a sample that is meant to be
#: readable by anyone.
_EXTERNAL_IDENTIFIER_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("plan id", re.compile(r"\b20\d{6}-[A-Za-z0-9][A-Za-z0-9._\-]*")),
    (
        "forge repository",
        re.compile(
            r"\b(?:github|gitlab|bitbucket|gitee)\.com/"
            r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+"
        ),
    ),
    (
        "public host",
        re.compile(
            r"\b[A-Za-z0-9\-]{2,}\.(?:com|net|org|io|cn|ai|dev|co|app|xyz)\b"
        ),
    ),
    (
        "private checkout directory",
        re.compile(r"(?:Documents|GitHub|Desktop|Projects)/[A-Za-z0-9._\-]+"),
    ),
)

#: The three families, in the order they are reported.
_PRIVATE_FAMILIES: Tuple[Tuple[str, Tuple[Tuple[str, "re.Pattern[str]"], ...]], ...] = (
    ("credential", _CREDENTIAL_PATTERNS),
    ("absolute path", _ABSOLUTE_PATH_PATTERNS),
    ("external identifier", _EXTERNAL_IDENTIFIER_PATTERNS),
)

#: A synthetic leak, used to prove the scan is not vacuous.  It is
#: assembled from fragments so that this file's own text never carries
#: the shape the scan forbids -- a gate that matched its own source
#: would either have to be exempted (and then it stops watching
#: anything of that shape) or fail on its own definition.
_SYNTHETIC_LEAK = 'cd "/Use' + 'rs/example/dev/plans"'


def find_private_identifiers(path: Path, text: str) -> List[Tuple[str, int, str]]:
    """Return ``(family, lineno, rule)`` for every private hit in ``text``.

    The matched text is deliberately **not** returned: a finding that
    prints the secret has published it.  What survives is enough to
    find the line and enough to know which rule fired.

    Args:
        path: The file the text came from.  Carried through so a caller
            can report the finding without re-deriving it.
        text: The text to scan.

    Returns:
        One ``(family, lineno, rule)`` triple per hit, in the order the
        families and the lines are visited.
    """
    hits: List[Tuple[str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for family, patterns in _PRIVATE_FAMILIES:
            for rule, pattern in patterns:
                if pattern.search(line):
                    hits.append((family, lineno, rule))
    return hits


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _triage():
    """The module under test.

    Imported inside the test bodies on purpose: a module that does not
    exist (or does not export the contract) then shows up as a test
    FAILURE carrying the real reason, instead of a collection ERROR
    that hides which contract broke.
    """
    try:
        from verification import failure_triage
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"verification.failure_triage is not importable: {exc}")
    return failure_triage


def _corpus_files() -> List[Path]:
    """This module's corpus files, in a fixed order.

    The corpus directory is **shared**: one JSON file per case, so that
    two tasks editing the corpus at the same time cannot lose one
    another's case to a merge.  The sibling corpora that live beside
    these four are therefore not this module's business -- they carry
    their own schema, and a reader of *this* corpus is a reader of the
    four cases named in :data:`EXPECTED_CASES` and of nothing else.

    What is checked is that those four are all there.  A case that was
    deleted is a hole in the corpus, and a hole is invisible to a scan
    that only looks at what it finds.
    """
    assert CASES_DIR.is_dir(), (
        f"the fixture corpus directory is missing: {CASES_DIR.name}. "
        f"It must hold one JSON file per case, under "
        f"backend/tests/fixtures/hitl/cases/."
    )
    paths = [CASES_DIR / f"{name}.json" for name in EXPECTED_CASES]
    missing = [path.name for path in paths if not path.is_file()]
    assert not missing, (
        f"the fixture corpus is missing {missing}. Every case this module "
        f"replays must be on disk: a case that was deleted is a hole in "
        f"the corpus, and a hole is invisible to a scan that only looks "
        f"at what it finds."
    )
    return paths


def _load(path: Path) -> Dict[str, Any]:
    """The parsed contents of one case file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        pytest.fail(f"{path.name}: not readable as JSON: {exc}")


def _verdict_records(case: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Every verdict record in a case: the current one, then the history.

    The history entries are verdict records in the same sense the
    current one is -- a round that failed, with a class and a raw
    result -- so the anchor invariant applies to them exactly as it
    applies to the verdict being replayed.
    """
    yield case["verdict"]
    for entry in case["failure_history"]:
        yield entry


def _is_passed(record: Dict[str, Any]) -> bool:
    """Whether a verdict record says the verification point passed."""
    return str(record.get("verdict", "")).strip().upper() == "PASSED"


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_case_schema() -> None:
    """Every case carries its required keys, and its class is the real one.

    The class/disposition agreement is checked against
    :mod:`verification.failure_triage` itself rather than against a
    second copy of the table written into this file: a corpus that
    agreed with a copy of the rules would keep agreeing with the copy
    after the rules moved.
    """
    triage = _triage()

    case_key_sets: List[frozenset] = []
    history_key_sets: List[frozenset] = []
    plan_key_sets: List[frozenset] = []
    vp_key_sets: List[frozenset] = []

    for path in _corpus_files():
        case = _load(path)

        missing = REQUIRED_CASE_KEYS - set(case)
        assert not missing, (
            f"{path.name}: missing required case keys {sorted(missing)}"
        )
        case_key_sets.append(frozenset(case))
        assert case["case"] == path.stem, (
            f"{path.name}: the 'case' field says {case['case']!r}, so the "
            f"file and the record name different samples"
        )

        # -- the verdict record -------------------------------------------
        verdict = case["verdict"]
        assert isinstance(verdict, dict), f"{path.name}: verdict is not an object"
        vmissing = REQUIRED_VERDICT_KEYS - set(verdict)
        assert not vmissing, (
            f"{path.name}: the verdict record is missing {sorted(vmissing)}"
        )
        assert str(verdict["verdict"]).strip().upper() in VERDICT_TOKENS, (
            f"{path.name}: verdict {verdict['verdict']!r} is not a verdict "
            f"literal"
        )
        assert verdict["failure_class"] in triage.ALL_FAILURE_CLASSES, (
            f"{path.name}: failure_class {verdict['failure_class']!r} is not "
            f"one of the four classes"
        )
        assert isinstance(verdict["actual_result"], str) and (
            verdict["actual_result"].strip()
        ), f"{path.name}: actual_result is empty"
        assert isinstance(verdict["evidence_quote"], str), (
            f"{path.name}: evidence_quote is not a string"
        )

        source = verdict.get("failure_class_source")
        if source is not None:
            assert source in {
                triage.SOURCE_DETERMINISTIC,
                triage.SOURCE_LLM,
                triage.SOURCE_DEFAULTED,
            }, f"{path.name}: failure_class_source {source!r} is not a source"

        # -- the history --------------------------------------------------
        history = case["failure_history"]
        assert isinstance(history, list), f"{path.name}: failure_history is not a list"
        for entry in history:
            assert isinstance(entry, dict), (
                f"{path.name}: a failure_history entry is not an object"
            )
            hmissing = (REQUIRED_VERDICT_KEYS | REQUIRED_HISTORY_KEYS) - set(entry)
            assert not hmissing, (
                f"{path.name}: a failure_history entry is missing "
                f"{sorted(hmissing)}"
            )
            assert entry["failure_class"] in triage.ALL_FAILURE_CLASSES, (
                f"{path.name}: a history entry carries the unknown class "
                f"{entry['failure_class']!r}"
            )
            history_key_sets.append(frozenset(entry))

        # -- the plan -----------------------------------------------------
        plan = case["verification_plan"]
        assert isinstance(plan, dict), (
            f"{path.name}: verification_plan is not an object"
        )
        if plan:
            plan_key_sets.append(frozenset(plan))
            points = plan.get("verification_points")
            assert isinstance(points, list) and points, (
                f"{path.name}: a non-empty verification_plan carries no "
                f"verification_points"
            )
            for point in points:
                assert isinstance(point, dict), (
                    f"{path.name}: a verification point is not an object"
                )
                assert point.get("verification_method"), (
                    f"{path.name}: a verification point names no method"
                )
                vp_key_sets.append(frozenset(point))
                replacement = point.get("replacement")
                if replacement is not None:
                    assert isinstance(replacement, dict), (
                        f"{path.name}: a replacement proposal is not an object"
                    )
                    assert replacement.get("verification_method"), (
                        f"{path.name}: a replacement proposal names no method"
                    )
                    assert "expected_not_looser" in replacement, (
                        f"{path.name}: a replacement proposal does not say "
                        f"what the strength guard is expected to answer"
                    )
                    answered = triage.is_not_looser(
                        point["verification_method"],
                        replacement["verification_method"],
                    )
                    assert answered == bool(replacement["expected_not_looser"]), (
                        f"{path.name}: the strength guard answers "
                        f"{answered} for {point['verification_method']!r} -> "
                        f"{replacement['verification_method']!r}, but the "
                        f"case says {replacement['expected_not_looser']!r}"
                    )

        # -- the expected disposition --------------------------------------
        expected = case["expected_disposition"]
        assert isinstance(expected, dict), (
            f"{path.name}: expected_disposition is not an object"
        )
        dmissing = REQUIRED_DISPOSITION_KEYS - set(expected)
        assert not dmissing, (
            f"{path.name}: expected_disposition is missing {sorted(dmissing)}"
        )
        disposition = expected["disposition"]
        assert disposition == triage.disposition_for(verdict["failure_class"]), (
            f"{path.name}: the expected disposition {disposition!r} is not "
            f"the one the disposition table gives {verdict['failure_class']!r}"
        )
        assert triage.is_counted(verdict["failure_class"]) is (
            not disposition.endswith("_not_counted")
        ), (
            f"{path.name}: the disposition {disposition!r} and the counting "
            f"rule for {verdict['failure_class']!r} disagree"
        )

        # -- the classification survives the real classifier ----------------
        classified, classified_source = triage.classify_verdict(verdict)
        assert classified == verdict["failure_class"], (
            f"{path.name}: the classifier answers {classified!r} for a record "
            f"that claims {verdict['failure_class']!r}"
        )
        if source is not None:
            assert classified_source == source, (
                f"{path.name}: the classifier answers {classified_source!r} "
                f"for a record that says its class came from {source!r}"
            )

    # One shape across the whole corpus: a case that quietly grows a key
    # the others do not have is a case the shared readers no longer
    # share.
    assert len(set(case_key_sets)) <= 1, (
        "the cases of the corpus do not share one shape: a reader that "
        "knows one case does not know the next"
    )
    assert len(set(history_key_sets)) <= 1, (
        "the failure_history entries of the corpus do not share one shape"
    )
    assert len(set(plan_key_sets)) <= 1, (
        "the non-empty verification_plan objects of the corpus do not share "
        "one shape"
    )
    assert len(set(vp_key_sets)) <= 1, (
        "the verification points of the corpus do not share one shape"
    )


def test_quote_substring_invariant() -> None:
    """A non-passed record's quote is verbatim -- or the case says so.

    The quote is the anchor the classification rests on: it has to
    appear in the raw output character for character, or the class was
    a guess.  A corpus that carried a guess would let the production
    rules be exercised on records that production would have degraded,
    and the replay would prove nothing.
    """
    triage = _triage()

    anchored = 0
    for path in _corpus_files():
        case = _load(path)
        declared_defaulted = bool(
            case["expected_disposition"].get("defaulted", False)
        )
        for record in _verdict_records(case):
            if _is_passed(record):
                continue
            quote = record.get("evidence_quote", "")
            actual = record.get("actual_result", "")
            if triage.validate_evidence_quote(quote, actual):
                anchored += 1
                continue
            assert declared_defaulted, (
                f"{path.name}: the evidence_quote is not a verbatim fragment "
                f"of its actual_result, and the case does not declare "
                f"expected_disposition.defaulted -- so the class it claims "
                f"was never earned"
            )

    assert anchored >= len(EXPECTED_CASES), (
        "the corpus carries fewer anchored records than it has cases: a "
        "corpus that is entirely defaulted proves nothing about anchoring"
    )

    # The rule is not vacuous: a paraphrase of the raw output is a
    # near-miss, and a near-miss has to be refused.
    assert triage.validate_evidence_quote(
        "schema rejected the identifier", "schema rejected id"
    ) is False, (
        "a paraphrase of the raw output was accepted as an anchor; the "
        "quote check has stopped distinguishing a quotation from a "
        "description of one"
    )


def test_no_private_identifiers() -> None:
    """No case carries a token, an absolute path or an external identifier.

    The corpus is committed, and a sample taken from a real run carries
    real strings -- an account name, a private plan directory, a
    provider key that was pasted into a log line -- unless they are
    taken out on purpose.  The samples here are deliberately synthetic
    for that reason, and this test is what keeps them that way.
    """
    offenders: List[str] = []
    for path in _corpus_files():
        text = path.read_text(encoding="utf-8")
        for family, lineno, rule in find_private_identifiers(path, text):
            offenders.append(f"{path.name}:{lineno}: {family} ({rule})")

    assert not offenders, (
        "the fixture corpus carries private identifiers. The corpus is "
        "committed to a public repository: replace the real account name, "
        "the real plan id and the real host with synthetic ones.\n  "
        + "\n  ".join(offenders)
    )

    # The scan is not vacuous: a synthetic leak is caught.
    assert find_private_identifiers(Path("synthetic"), _SYNTHETIC_LEAK), (
        "the privacy scan found nothing in a synthetic leak -- the patterns "
        "have stopped matching and the zero above proves nothing"
    )
