"""Public security docs describe *defects*, never *incidents* (2026-09-28).

Why this gate exists
--------------------
``SECURITY_AUDIT.md`` ships in a public repository. Its findings are about
the **code**: what shape is wrong, how it was fixed, how to verify the
fix. Anyone can re-derive those by reading the source, so publishing them
adds nothing an attacker did not already have and buys a real credibility
signal — it is the evidence behind README's "every step leaves a
structured, reviewable artifact".

An incident record is a different kind of object. "On the machine that
ran this, *N* such files existed, *M* of them from real runs, the oldest
*D* days old" is **not re-derivable from the source** — it is a statement
about one deployment that no reader of the code could have produced.
Publishing it is a fresh disclosure: it hands over the exact window in
which credentials were exposed and a concrete lead to follow.

(That sentence is spelled with placeholders on purpose. An earlier draft
of this docstring quoted the real figures to explain what it bans, which
republished the very thing the gate exists to keep out — the failure mode
this whole file is about, committed by the file itself.)

The same test catches a second family that reads like diligence rather
than like disclosure: **bookkeeping about the sweep itself** — a dated
wall-clock baseline, a map of which (surface × problem-class) cells the
audit examined and found nothing in, a table of greps and their
dispositions. None of that is a claim about the code, and the negative
half is a hunting guide: it says where nobody found anything, which is
where nobody will look next.

The line, stated so it can be applied without security expertise:

    defects are re-derivable from the code → publishable
    incidents happened on one machine     → not publishable
    sweep bookkeeping describes the sweep → not publishable

Why a gate rather than a note in the template
---------------------------------------------
The template can only ask nicely. Every entry in this document was
written in good faith, and the two that carried incident narrative
(ENTRY-017, ENTRY-018) read perfectly reasonable at the time of writing —
a count and a date read as *evidence*, and evidence feels like the right
thing to include. That is exactly the failure a review pass misses and a
regex does not.

The bookkeeping family is worse, because it does not feel like a mistake
at all. A perf baseline and a coverage grid look like *rigour*. The first
version of this gate missed all of it: it was written from the two
offences already in hand, so it caught those two shapes and nothing else
— and the `## 性能记录` section sat in the same file, three collection
dates and six baseline figures and twenty-four "found nothing" cells
wide, with the gate green. A rule generalised from its examples covers
exactly its examples. Hence the record-vocabulary patterns below.

What this gate can and cannot see
---------------------------------
Stated honestly, because an overstated gate is worse than a narrow one:

**Catches** — the shapes that carry operator facts in practice:

  * a first-person *measurement* ("on my machine we measured …"). The
    measurement verb is the discriminator, not the word "machine": the
    document uses "本机" five times and only one of them is an incident —
    the other four describe a threat model that applies to any
    deployment, which is correct and stays;
  * a **quantified disclosure** — a count attached to an artifact noun
    (``4242 个此类文件``, ``24 个残留文件``);
  * a **collection date** — a date attached to the act of measuring
    (``采集于 <a date>``). A date on a code change is *not* this:
    ``git log`` re-derives it, so a change date stays. The verb is the
    discriminator, not the digits;
  * a **test-suite baseline** — ``用例数`` / ``case count`` / ``N = 1111``
    / ``1000 passed``. Again the *record vocabulary* is the signal, not the
    number: a published timeout (``1800s``) is a contract and stays;
  * the coverage matrix's **negative-result cell** (``无此类发现``);
  * a **quoted secret prefix**.

**Does not catch** — a bare number with no record vocabulary and no
artifact noun, or an incident narrated without a measurement verb. The
gate narrows the opening; the entry template and review close it. Do not
read a green run as proof that a document contains no incident narrative
— read it as proof that the document does not contain the shapes above.

Scope
-----
Public-facing documents at the repository root and under ``docs/``. The
entry template lives in ``SECURITY_AUDIT.md`` itself, so the rule and its
enforcement are in the same file.

The sweep record is deliberately *outside* this gate's reach **and**
outside its text: it is supposed to carry the baseline and the coverage
grid. Naming where it lives would be the disclosure this gate exists to
prevent, so the tests below avoid that term — see
``test_the_operator_private_doc_is_not_named_here``.

The structural half of the same rule is pinned separately by
``test_the_public_doc_carries_no_sweep_bookkeeping``: the four sections
that belong to the sweep record must not reappear in the public document,
because a heading that comes back takes its contents with it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Root-level documents that ship. The repository-root ``CLAUDE.md`` is not
#: here on purpose — it is gitignored, so it is the one place an operator
#: *may* record incident history, and scanning it would be self-defeating.
_PUBLIC_DOCS = (
    Path("SECURITY_AUDIT.md"),
    Path("README.md"),
)

#: Directories whose Markdown is published as a whole. ``docs/`` is the
#: MkDocs source for the developer site (``mkdocs.yml`` →
#: ``.github/workflows/pages.yml``), so every page under it is as public as
#: the README — and it is now the larger surface of the two.
_PUBLIC_DOC_DIRS = (
    Path("docs"),
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
#
# Built from fragments so this file's own source does not contain the
# literal phrases it catches. Same convention as the sibling
# ``test_no_operator_attribution_in_source.py``: a gate that matches its
# own definition is a gate nobody can read.

#: First-person measurement markers. These are what make a sentence a
#: report about one machine rather than a statement about the code.
#:
#: The English entries were added after the gate missed a real one: the
#: contributing guide illustrated the rule with a first-person sentence
#: carrying a count — an incident quoted verbatim in a published page —
#: and none of the Chinese-verb patterns fired on it. The failure was the
#: same shape as the one that produced this gate: the patterns covered the
#: examples in hand, not the category.
_MEASUREMENT_PATTERNS: tuple[str, ...] = (
    "本机" + "实测",
    "我们" + "实测",
    "实测" + "样本",
    "we " + "measured",
    "measured " + "on our",
    "measured " + "on the operator",
    "on my " + "machine",
    "on our " + "machine",
    "on the " + "machine that ran",
)

#: Markdown emphasis, stripped before the quantified-disclosure match so
#: ``**4242** 个`` reads as ``4242 个``.
_EMPHASIS_RE = re.compile(r"[*_`]+")

#: A count attached to an artifact noun — the shape a leaked-incident
#: sentence is built from. Deliberately narrow: a bare ``4.1 MB`` is
#: trivia, not a disclosure, and pinning every numeral would produce the
#: kind of noisy gate that gets deleted rather than fixed.
_QUANTIFIED_DISCLOSURE_RE = re.compile(
    r"\d+\s*(?:个|份|条)\s*(?:此类|该类)?\s*(?:文件|密钥|凭据|残留|记录)"
    # The English form of the same shape. Added for the same reason as the
    # English measurement markers above — a bare count + "such files"
    # carries the disclosure without a Chinese measure word, so the first
    # pattern missed it.
    r"|\d+\s+such\s+(?:files|keys|credentials|records|tokens)"
)

#: A date attached to the act of *collecting* a measurement. The
#: discriminator is the verb, exactly as with the measurement patterns
#: above — a date on a code change is re-derivable from ``git log`` and
#: stays (``test_leaves_correct_defect_language_alone`` pins that case),
#: while a date on a measurement is a claim about one machine at one
#: moment that no reader could reproduce.
_COLLECTION_DATE_RE = re.compile(
    r"(?:采集|取样|记录|统计)"
    + r"于\s*\d{4}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2}\s*日?)?"
    r"|(?:collected|measured|recorded|sampled)\s+on\s+\d{4}-\d{2}"
)

#: Vocabulary that only a *record of a suite run* uses. Kept as phrases
#: rather than figures because the number is not the tell: a documented
#: timeout (``1800s``) is a published contract and must survive.
#:
#: Note what is deliberately absent: ``"tests collected"``. ENTRY-010
#: quotes pytest's own failure message (``"0 tests collected"``) to
#: explain why a bare system-python invocation is dangerous, and that is a
#: statement about the tool, not a baseline — the first draft of this
#: pattern flagged it on the first run. The suite-record figures below
#: catch every actual baseline without it.
_SUITE_RECORD_PHRASES: tuple[str, ...] = (
    "用例" + "数",
    "case " + "count",
)

#: The figure shapes a recorded run leaves behind — ``N = 1111``,
#: ``1000 passed``, ``14 failed``. Anchored on ``\bN`` so ``MAX_PLAN_ID_LEN
#: = 64`` does not match (there is no word boundary inside ``LEN``).
_SUITE_RECORD_RE = re.compile(
    r"\bN\s*=\s*\d{3,}\b"
    r"|\b\d{2,}\s+(?:passed|failed|skipped)\b"
)

#: The coverage matrix's negative-result cell. Built from fragments so
#: this file does not carry the literal it bans — the same convention as
#: every other pattern here.
_EMPTY_CONCLUSION_TERM = "无此类" + "发现"

#: The audit's sweep-bookkeeping sections, by exact level-2 heading. They
#: are contract names (the meta-tests parse them), so pinning them here is
#: cheap and catches a re-introduction that the prose patterns might miss.
_SWEEP_SECTION_HEADINGS: tuple[str, ...] = (
    "## " + "性能记录",
    "## " + "Appendix C — index of findings by tier",
    "## " + "Coverage matrix",
    "## " + "Information-disclosure adjudication",
)

#: Literal secret material. A fragment is enough — these prefixes are
#: recognisable and their presence in prose means a real key was quoted.
_SECRET_FRAGMENTS: tuple[str, ...] = (
    "sk-" + "cp-",
    "sk-" + "ant-",
    "sk-" + "proj-",
)

#: The operator's private state directory. Assembled from fragments for
#: the same reason as every other pattern here: the assertion below greps
#: this file's own text, and a literal would make the gate flag its own
#: definition — which it did, on the first run, in the failure message.
_PRIVATE_DIR_TERM = "." + "config"


def deemphasise(text: str) -> str:
    """Strip markdown emphasis so ``**4242** 个`` matches as ``4242 个``."""
    return _EMPHASIS_RE.sub("", text)


def find_incident_narrative(path: Path, text: str) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, matched)`` for every incident-narrative hit.

    Line numbers are 1-based so the failure message can be pasted into an
    editor. Scanning is line-wise: the shapes this gate looks for are
    single-phrase, and a line-wise scan keeps the reported line honest.
    """
    found: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for phrase in _MEASUREMENT_PATTERNS:
            if phrase in lowered or phrase in line:
                found.append((path, lineno, phrase))
        for match in _QUANTIFIED_DISCLOSURE_RE.finditer(deemphasise(line)):
            found.append((path, lineno, match.group(0)))
        for match in _COLLECTION_DATE_RE.finditer(line):
            found.append((path, lineno, match.group(0)))
        for match in _SUITE_RECORD_RE.finditer(line):
            found.append((path, lineno, match.group(0)))
        for phrase in _SUITE_RECORD_PHRASES:
            if phrase in line:
                found.append((path, lineno, phrase))
        if _EMPTY_CONCLUSION_TERM in line:
            found.append((path, lineno, _EMPTY_CONCLUSION_TERM))
        for fragment in _SECRET_FRAGMENTS:
            if fragment in line:
                found.append((path, lineno, fragment))
    return found


def public_doc_paths() -> list[Path]:
    """Every published Markdown document, repo-relative, sorted.

    Sorted so a failure lists the same offences in the same order on every
    machine — the same reason ``source_scan`` sorts its walk.
    """
    paths = list(_PUBLIC_DOCS)
    for rel_dir in _PUBLIC_DOC_DIRS:
        directory = _REPO_ROOT / rel_dir
        if not directory.is_dir():
            continue
        paths.extend(p.relative_to(_REPO_ROOT) for p in sorted(directory.rglob("*.md")))
    return paths


def scan_public_docs() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for rel in public_doc_paths():
        path = _REPO_ROOT / rel
        if not path.exists():  # pragma: no cover - doc removed or renamed
            continue
        hits.extend(find_incident_narrative(rel, path.read_text(encoding="utf-8")))
    return hits


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_public_docs_carry_no_incident_narrative() -> None:
    hits = scan_public_docs()
    offenders = "\n  ".join(f"{p}:L{n}: {m!r}" for p, n, m in hits)
    assert not hits, (
        "a public document describes what happened on one machine rather than "
        "what is wrong with the code. A defect is re-derivable by any reader "
        "of the source, so publishing it costs nothing; a measured incident "
        "is not, so publishing it hands over an exposure window. Restate the "
        "sentence about the code — the class of defect, the fix, the "
        "verification — with no counts, durations or measurement verbs:\n  "
        + offenders
    )


def test_the_gate_scans_something() -> None:
    """A gate whose document list resolves to nothing passes vacuously."""
    present = [rel for rel in public_doc_paths() if (_REPO_ROOT / rel).exists()]
    assert present, (
        f"no public documents resolved relative to {_REPO_ROOT}; the scanner "
        f"is reading nothing"
    )
    assert (_REPO_ROOT / "SECURITY_AUDIT.md").exists(), (
        "SECURITY_AUDIT.md is the document this rule is about — if it moved, "
        "update _PUBLIC_DOCS rather than dropping it"
    )
    # The docs site is the larger public surface; if its directory is
    # renamed the scan would quietly shrink to the two root documents.
    assert len(present) > len(_PUBLIC_DOCS), (
        f"only {len(present)} public document(s) resolved ({present}); the "
        f"docs/ walk found nothing, so the site is going unpublished without "
        f"being scanned"
    )


def test_the_operator_private_doc_is_not_named_here() -> None:
    """This gate must not disclose what it is protecting.

    Stating *where* incident records live would hand a reader the same
    fact the gate exists to keep out — and a gate that names the private
    path is self-defeating in a way no other rule in this file is.
    """
    text = Path(__file__).read_text(encoding="utf-8")
    assert _PRIVATE_DIR_TERM not in text, (
        "this gate names the operator's private state directory. The rule "
        "can be stated completely without saying where the records live — "
        "see the module docstring's scope note."
    )


def test_the_public_doc_carries_no_sweep_bookkeeping() -> None:
    """The four sweep-record sections must not reappear in the public doc.

    The prose patterns catch the *contents*; this catches the container.
    A heading that comes back takes its contents with it, and the
    meta-tests that parse those headings now read the operator-local
    record — so a copy reappearing here would be an unenforced duplicate,
    which is exactly the half-published state the split exists to prevent.
    """
    text = (_REPO_ROOT / "SECURITY_AUDIT.md").read_text(encoding="utf-8")
    present = [h for h in _SWEEP_SECTION_HEADINGS if h in text]
    assert not present, (
        "SECURITY_AUDIT.md carries a section that belongs to the audit's "
        "operator-local sweep record: "
        + ", ".join(repr(h) for h in present)
        + ". These are statements about the sweep — a dated measurement, a "
        "map of where nothing was found, a grep workpaper — not about the "
        "code, and they are not re-derivable by a reader of the source. "
        "Keep them in the sweep record."
    )


# ---------------------------------------------------------------------------
# Sensitivity — the gate must catch the shapes it exists for, and must not
# fire on the sentences that correctly describe a defect.
# ---------------------------------------------------------------------------


def test_flags_a_first_person_measurement() -> None:
    line = "本" + "机实测（" + "1970-01-01" + "）：4242 个此类文件"
    assert find_incident_narrative(Path("x.md"), line)


def test_flags_a_quantified_disclosure_behind_markdown_emphasis() -> None:
    """The real ENTRY-017 line writes its numbers in bold."""
    line = "**4242** 个此类文件，其中 **2424** 个来自真实运行"
    hits = find_incident_narrative(Path("x.md"), line)
    assert any("4242" in m for _, _, m in hits), (
        "the emphasis-stripping regressed; the gate would miss the exact "
        "line it was written for"
    )


def test_flags_a_quoted_secret_prefix() -> None:
    line = "含非空的 125 字符 sk-cp-… 密钥"
    assert find_incident_narrative(Path("x.md"), line)


# ---------------------------------------------------------------------------
# Regression sample — the text that was in the public document, and was
# waved through by the first version of this gate.
#
# This is deliberately the *shape* of the text that was in the public
# document rather than a shape invented to be caught. The gate's first
# version was written from the two offences already in hand and generalised
# from them, so it covered exactly those two and left the whole
# `## 性能记录` section — collection dates, baseline figures, "found
# nothing" cells — passing green. A negative test built from the real
# section is what makes "the gate catches the category" a claim rather than
# a hope.
#
# One deliberate deviation: the collection dates below are synthetic. The
# real ones are themselves an instance of the banned shape, and a gate
# whose own fixture republishes the date it exists to keep out is a gate
# nobody will trust. The *shapes* are verbatim; the identifying digits are
# not.
# ---------------------------------------------------------------------------


_REGRESSION_SAMPLE = """\
### unit (采集于 1970-01-01)

* 用例数 (case count): `N = 1111` tests collected under
  `-m "unit or integration"` (1000 passed, 35 skipped, 14 failed,
  2 xfailed, 1 xpassed at baseline).
* 墙钟时间 (wall-clock duration): `999.99s` (≈ 6 min 6 s) for the
  combined unit + integration invocation.

### e2e (采集于 1970-01-01)

* 用例数 (case count): `N = 33` collected (30 passed, 3 skipped).

| 入口面 | 无此类发现 | ENTRY-001, ENTRY-006 | 无此类发现 |
"""


@pytest.mark.parametrize("family, expected", [
    ("collection date", "采集于 1970-01-01"),
    ("suite record phrase", "用例" + "数"),
    ("suite record figure", "N = 1111"),
    ("per-layer outcome counts", "1000 passed"),
    ("negative-result cell", "无此类" + "发现"),
])
def test_the_regression_sample_is_caught(family: str, expected: str) -> None:
    """Every family that was sitting in the public document still trips.

    Parametrized so a later narrowing of one pattern reports *which* family
    reopened rather than a single opaque failure — the failure mode this
    guards against is a pattern quietly tightened back to the two original
    shapes, which would leave four of these five green.
    """
    hits = find_incident_narrative(Path("x.md"), _REGRESSION_SAMPLE)
    matched = [m for _, _, m in hits]
    assert any(expected in m for m in matched), (
        f"the gate no longer catches the {family!r} family "
        f"(expected a hit containing {expected!r}); it matched only "
        f"{matched!r}. This text was in the published SECURITY_AUDIT.md "
        f"and the gate passed it."
    )


@pytest.mark.parametrize("line", [
    # The English incident form the gate missed in the wild. The Chinese
    # patterns above did not fire on it, and it was live in the contributing
    # guide — see the note on _MEASUREMENT_PATTERNS.
    'On my machine there were 4242 such files, the oldest three days old',
    "Measured on our runner: 4242 such keys",
])
def test_flags_the_english_incident_form(line: str) -> None:
    assert find_incident_narrative(Path("x.md"), line), (
        f"the gate missed the English incident form on {line!r} — this is "
        f"the shape that was actually published, in a page the Chinese "
        f"patterns could not see"
    )


@pytest.mark.parametrize("line", [
    # A date on a code change — re-derivable from `git log`, so it stays.
    # The counterpart to the collection-date pattern: the *verb* is the
    # discriminator, and losing that distinction would ban every date.
    "搬迁本身在 2026-09-28 执行",
    "ENTRY-015 landed on 2026-09-20 with `MAX_PLAN_ID_LEN = 64`",
])
def test_leaves_a_change_date_alone(line: str) -> None:
    assert not find_incident_narrative(Path("x.md"), line), (
        f"false positive on {line!r}; a change date is re-derivable from "
        f"git history, so banning it would be banning the fix log"
    )


@pytest.mark.parametrize("line", [
    # A published timeout is a contract the reader must know, not a
    # measurement of a run. Same reasoning as the mode constants above.
    "`subprocess.run(..., timeout=1800)` — the cap on a single VP.",
    "the whole process group is killed after 300s, not just the child",
    # ENTRY-010 quotes pytest's own failure message. It reads like a
    # baseline and is not one — the first draft of the suite-record
    # pattern flagged this line, which is why it is pinned here.
    "a 45-minute CI run reported \"0 tests collected\"",
])
def test_leaves_a_published_timeout_alone(line: str) -> None:
    assert not find_incident_narrative(Path("x.md"), line), (
        f"false positive on {line!r}; a documented timeout or a quoted "
        f"failure message is a statement about the tool, and a gate that "
        f"fires on those is a gate that gets deleted rather than fixed"
    )


@pytest.mark.parametrize("line", [
    # Threat-model language: describes any deployment. Must stay.
    "任何本机账户无需任何权限提升即可读取全部存量",
    "该载荷还带有部署方的主机路径，扩大了泄露面",
    # A defect statement with a mode constant — a number, but not a count
    # of anything on anyone's machine.
    "`/tmp` 是 `1777`；`0644` 的文件对机器上任何账户可读",
    "`write_private_json()` 显式设置并强制 `0600`",
    # A change date, not an incident measurement.
    "搬迁本身在 2026-09-28 执行",
    # An operator-facing configuration shape, correctly described.
    "某些配置下 `self.settings` 是操作者自己的 `~/.claude/settings.json`",
])
def test_leaves_correct_defect_language_alone(line: str) -> None:
    assert not find_incident_narrative(Path("x.md"), line), (
        f"false positive on {line!r}; a gate that fires on legitimate "
        f"threat-model prose is a gate that gets deleted"
    )
