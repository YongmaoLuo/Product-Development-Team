"""The cutover runbook has to exist — and it has to stay a runbook.

Why this gate exists
--------------------
The native scheduler replaces a scheduler that used to run beside it:
a tick woken by an external timer, and, on the deployments that ran the
resident form, a small service of its own with a read-only page and a
control API on port 8004.

Replacing a scheduler is an operation that is carried out by a person,
on a machine, in the middle of a working day.  The procedure therefore
has to be *written down* — and a written procedure that is not checked
is a procedure that drifts: the port number changes, the flag moves, the
old job is deleted at the wrong moment, and two schedulers run at once
for an afternoon.

So the runbook is pinned here, in the same spirit as the rest of
``static_gates/``: what the document claims to cover is checked against
the document's own text, so that a claim cannot be quietly dropped.

What it pins
------------
1. **The five sections.**  The runbook is a procedure, and a procedure
   has a shape: the pre-flight, the dry run and the judgement of its
   artifacts, the cutover itself, the rollback, and the decommission
   checklist.  All five are level-2 headings, in that order, and there
   are no others — a sixth section is a section nobody pinned.
2. **The rollback flag.**  The rollback is ``scheduler.enabled: false``
   in the configuration file, and the section says so *in a command
   block*: prose about the rollback is not a rollback.
3. **The decommission checklist.**  The old port, the old state file
   and the supervisor's managed set are checked, each of them in a
   command block: a checklist that only names things is not a checklist.
4. **No machine-specific paths.**  The document is read by people whose
   home directories are not this repository's author's, so it reaches
   the home directory through ``$HOME`` and writes down no absolute
   path under anybody's home.
5. **One probe, used twice.**  The pre-flight asks "is the old service
   still up?" and the decommission asks "is it gone?" — and they have
   to ask the *same* question of the *same* path, or the operator is
   comparing two different signals.  The path they use is the old
   face's read-only page, not a path under its ``/api/`` prefix:
   every ``/api/`` path there is behind a guard header, so a probe
   against one is answered 403 while the service is alive, and the
   probe would report a live service as dead.
6. **The Chinese sibling.**  The site publishes two locales and the
   language selector promises that the other one exists; a page with
   no translation silently falls back to English.  The sibling is
   therefore part of this deliverable, and it has to carry the same
   five sections.
7. **The navigation.**  A page no nav entry points at is a page
   nobody finds: it is built, it is served, and it is not in the way
   anyone walks.
8. **The supervisor criterion.**  The decommission checklist judges the
   retired scheduler's *own* supervisor entry — the name the
   supervisor registers it under — and the other services the
   supervisor manages are named as out of scope.  A criterion about the
   size of the managed set cannot be signed off before the cutover or
   after it, and an operator who follows it literally removes the PDT
   backend along with the old scheduler.

Scope, stated honestly
----------------------
This gate checks the **text**.  It cannot check that an operator
followed it, that the old job was really deleted, or that the port is
really closed — those are the checks the checklist in the document
itself is for.  What this gate does is make sure the checklist is
still there, still names the things it has to name, and still carries
the commands that prove them.

The page checked here is the English one, the base of the pair the
documentation site publishes.  Its Chinese sibling carries the same
procedure in the second locale, and the check for that pairing is
below rather than left to the reader: a claim the document makes
about itself is worth the same as any other claim it makes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The cutover runbook — the English page, the base of the pair.
RUNBOOK = _REPO_ROOT / "docs" / "operations" / "scheduler-cutover.md"

#: The five sections, in order.  The order is the procedure's order: you
#: cannot judge the dry run before you have done the pre-flight, and you
#: cannot decommission what you have not cut over.
SECTIONS: tuple[str, ...] = (
    "1. Before you start",
    "2. The dry run and the judgement of its artifacts",
    "3. The cutover, inside one window",
    "4. Rolling back",
    "5. The decommission checklist",
)

#: The heading of the rollback section.
ROLLBACK = SECTIONS[3]

#: The heading of the decommission checklist.
DECOMMISSION = SECTIONS[4]

#: The sentence that says the rollback switch is not a panel control.
#: It is pinned rather than searched for, because "the panel does not
#: expose the switch" is a rule — the switch lives in the configuration
#: and nowhere else — and a rule that is only implied is a rule the
#: operator may talk itself out of.
PANEL_EXPOSES_NOTHING = "The panel does not expose this switch"

#: The name the supervisor registers the retired scheduler under — the
#: entry the decommission checklist judges.  After the cutover that
#: entry must not be in the supervisor's managed set: a supervisor that
#: still manages the retired scheduler starts it again on the next
#: tick, and that tick is the moment the invariant breaks.
RETIRED_SCHEDULER_ENTRY = "ac-scheduler"

#: The same entry, matched as a whole name.  A plain substring search
#: would accept the ``.ac-scheduler`` state directory, which is a path
#: on disk and not a supervisor entry.
RETIRED_SCHEDULER_ENTRY_RE = re.compile(
    r"(?<![\w./-])ac[-_]scheduler(?![\w.-])"
)

#: The header the surviving read-only forwarder carries when it reads
#: PDT.  PDT answers 403 to a request that arrives without it, so a
#: checklist that describes the forwarder as reading PDT has to say how.
FORWARDER_HEADER = "X-PDT-Request: 1"

#: The services the supervisor manages that are none of the retired
#: scheduler's business.  The checklist names them so that an operator
#: reading it does not read "the supervisor manages the forwarder and
#: nothing else" as an instruction to remove the rest of the fleet.
OUT_OF_SCOPE_ENTRIES: tuple[str, ...] = (
    "provider-optimizer",
    "provider-usage-forwarder",
    "perpetual-",
    "pdt-backend",
)

#: The wording that made the check undecidable.  The supervisor's
#: managed set belongs to the supervisor — it holds the PDT backend and
#: the other subsidiaries too — so a criterion that asks it to hold a
#: single name is false before the cutover and false after it, and an
#: operator who follows it literally removes the PDT backend.
SINGLE_NAME_CLAIM = "only name in the list is"

#: The port the retired scheduler served its read-only page and its
#: control API on.
LEGACY_PORT = 8004

#: The port the supervisor's status API answers on.
SUPERVISOR_PORT = 8002

#: The name of the retired scheduler's state file.
LEGACY_STATE_FILE = "state.json"

#: The Chinese sibling of the runbook.  The site publishes two locales
#: and the language selector promises the other one exists; a page with
#: no translation falls back to English without a word about it.
CHINESE_SIBLING = RUNBOOK.with_name("scheduler-cutover.zh.md")

#: The five sections of the Chinese page, in the same order.  The two
#: pages are one procedure in two languages, so a section that exists
#: in one and not the other is a procedure the two languages disagree
#: about.
ZH_SECTIONS: tuple[str, ...] = (
    "1. 开始之前",
    "2. dry-run 与产物判定",
    "3. 单窗口切换",
    "4. 回滚",
    "5. 下线核对清单",
)

#: The site's configuration.  The nav lives here, and the nav is how a
#: reader finds a page.
MKDOCS_YML = _REPO_ROOT / "mkdocs.yml"

#: The path of the runbook in the nav, relative to ``docs/``.
NAV_PATH = "operations/scheduler-cutover.md"

#: The retired scheduler's read-only page — the path both the pre-flight
#: and the decommission checklist use to ask whether it is still there.
LEGACY_BASE = "http://127.0.0.1:8004"
LEGACY_PAGE = f"{LEGACY_BASE}/"

#: A URL of the retired scheduler, matched out of a command block.
_LEGACY_URL = re.compile(
    r"http://127\.0\.0\.1:8004(?P<path>/[^\s\"'\\)]*)?"
)

#: The shapes of a path that names *this* machine's home directory.
#: ``/Users/<name>/`` and ``/home/<name>/`` are the two Unix forms, the
#: Windows form is the third, and ``~/`` is the fourth — a tilde path is
#: home-relative, but it still spells out "somebody's home directory"
#: instead of letting the shell expand it where the command is pasted.
_HOME_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"),
    re.compile(r"~/"),
)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Return the document's text.

    The file is read once per test; it does not change while the suite
    runs, and every test below reads the same bytes.
    """
    assert path.exists(), (
        f"{path.relative_to(_REPO_ROOT)} does not exist. The cutover is "
        "carried out by a person on a machine: a procedure that is not "
        "written down is a procedure that is carried out from memory, "
        "and two schedulers running at once is the failure it is "
        "supposed to prevent."
    )
    return path.read_text(encoding="utf-8")


def _outside_fences(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(index, line)`` for every line that is not inside a fence.

    A ``#`` inside a fenced block is a comment in an example, not a
    heading — so the heading scan has to know about fences, and so does
    the section scan.
    """
    out: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append((index, line))
    return out


def _level2_headings(text: str) -> list[str]:
    """Return the titles of the level-2 headings, in order."""
    return [
        line[3:].strip()
        for _, line in _outside_fences(text.splitlines())
        if line.startswith("## ")
    ]


def _section(text: str, heading: str) -> str:
    """Return the body of the section titled ``heading``.

    A level-2 section owns everything below it until the next level-2
    heading — the level-3 subsections are part of it, not a boundary.
    So a command that belongs to another section cannot satisfy a check
    that is about this one.
    """
    lines = text.splitlines()
    start: int | None = None
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## ") and line[3:].strip() == heading:
            start = index
            break
    assert start is not None, (
        f"the runbook has no section titled {heading!r}. The procedure "
        "has five sections and this one is missing."
    )

    # The body keeps its fences: the command blocks are what the checks
    # below look at, and a body with the fences stripped would have no
    # blocks left in it.
    body: list[str] = []
    in_fence = False
    for line in lines[start + 1 :]:
        if line.startswith("```"):
            in_fence = not in_fence
            body.append(line)
            continue
        if not in_fence and line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _code_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``(info string, body)`` for every fenced code block.

    The info string is the language tag (``bash``, ``yaml``); the body
    is what the block carries.  Checks that look for a command look
    here and not in the prose: a runbook that *describes* a command in
    a sentence has not given the operator anything to paste.
    """
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("```"):
            index += 1
            continue
        info = line[3:].strip()
        body: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].startswith("```"):
            body.append(lines[index])
            index += 1
        blocks.append((info, "\n".join(body)))
        index += 1
    return blocks


def _blocks_containing(text: str, needle: str) -> list[str]:
    """Return the bodies of the code blocks that carry ``needle``."""
    return [body for _, body in _code_blocks(text) if needle in body]


def _legacy_urls(text: str) -> set[str]:
    """Return the retired scheduler's URLs that appear in the commands.

    Only the fenced blocks are read: a URL in the prose is a mention,
    and a mention is not something the operator can run.
    """
    urls: set[str] = set()
    for _, body in _code_blocks(text):
        for match in _LEGACY_URL.finditer(body):
            urls.add(f"{LEGACY_BASE}{match.group('path') or ''}")
    return urls


def _nav_paths(nav: object) -> set[str]:
    """Return every page path the nav points at.

    The nav is a tree of ``{title: path}`` and ``{title: [children]}``
    entries, so the walk is recursive.  A nav entry may also be a bare
    path, which is why the strings are collected too.
    """
    found: set[str] = set()
    stack: list[object] = [nav]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, (str, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return found


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runbook() -> str:
    """The runbook's text."""
    return _read(RUNBOOK)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_runbook_has_five_sections(runbook: str) -> None:
    """Five level-2 sections, in the procedure's order.

    The order is the point: the pre-flight comes before the dry run, the
    dry run before the cutover, the cutover before the decommission.  A
    document with the same five sections in a different order is a
    different procedure.

    "And no others" is checked too.  A sixth section is a section that
    nobody pinned, and the count is what keeps the document a procedure
    rather than an essay that happens to contain one.
    """
    headings = _level2_headings(runbook)
    assert headings == list(SECTIONS), (
        "the runbook's level-2 sections are not the five the procedure "
        f"has.\n  expected: {list(SECTIONS)}\n  found:    {headings}\nThe "
        "sections are pinned because their order is the order the "
        "operator works in: the pre-flight, then the dry run, then the "
        "cutover, then the rollback, then the decommission checklist."
    )


def test_runbook_documents_rollback_flag(runbook: str) -> None:
    """The rollback is ``scheduler.enabled: false``, and it is not in the panel.

    Two rules, in one section, because they are the same rule seen from
    two sides:

    * **The flag.**  The rollback is one setting in the configuration
      file, and the section has to carry it as a command — a YAML block
      the operator can paste over the section in ``config.yaml``.  Prose
      about the rollback is not a rollback.

    * **The panel.**  The switch is a configuration lever and nothing
      else.  There is no button and no endpoint that flips it, because a
      control that stops every plan in the system is one mis-click away
      from an outage.  The section says so in the same breath as the
      flag, so the two cannot be separated by an edit.
    """
    section = _section(runbook, ROLLBACK)

    blocks = [
        body
        for _, body in _code_blocks(section)
        if "scheduler:" in body and "enabled: false" in body
    ]
    assert blocks, (
        "the rollback section has no command block that sets "
        "`scheduler.enabled: false`. The rollback is one setting in the "
        "configuration file, and the operator has to be able to paste it: "
        "the section has to carry the setting as a command block, not "
        "only describe it."
    )

    assert PANEL_EXPOSES_NOTHING in section, (
        "the rollback section does not say that the panel does not expose "
        "the switch. The rollback lever lives in the configuration file "
        "and nowhere else — no button, no endpoint. A runbook that "
        "leaves that out invites the operator to look for a switch in the "
        "panel, and there is none."
    )


def test_runbook_lists_decommission_checks(runbook: str) -> None:
    """The checklist names the old port, the old state file and the supervisor.

    The decommission is the step that is *checked* rather than done, so
    the checks are the content of the section.  Each of them has to
    arrive as a command:

    * **the old port** — the retired scheduler served its read-only page
      and its control API there, and nothing may answer on it any more;
    * **the old state file** — it belongs to the retired scheduler, and
      a file that looks live and is not is how the next operator is
      misled;
    * **the supervisor's managed set** — after the decommission the
      retired scheduler must not be in it.  A supervisor that still
      manages the retired scheduler starts it again on the next tick,
      and that tick is the moment the single-writer invariant breaks.
    """
    section = _section(runbook, DECOMMISSION)

    port_blocks = _blocks_containing(section, str(LEGACY_PORT))
    assert port_blocks, (
        f"the decommission checklist has no command that touches port "
        f"{LEGACY_PORT}. The retired scheduler served its read-only page "
        "and its control API on that port; the checklist has to say how "
        "the operator proves that nothing answers there any more."
    )

    state_blocks = [
        body
        for body in _blocks_containing(section, LEGACY_STATE_FILE)
        if "! -e" in body
    ]
    assert state_blocks, (
        "the decommission checklist has no command that proves the "
        f"retired scheduler's {LEGACY_STATE_FILE} is gone. The check has "
        "to be a command that fails when the file is still there — a "
        "checklist that only names the file is not a check."
    )

    supervisor_blocks = [
        body
        for body in _blocks_containing(section, str(SUPERVISOR_PORT))
        if "subsidiaries" in body
    ]
    assert supervisor_blocks, (
        f"the decommission checklist has no command that reads the "
        f"supervisor's managed set on port {SUPERVISOR_PORT}. After the "
        "decommission the retired scheduler must not be in it; a "
        "supervisor that still manages the retired scheduler starts it "
        "again on the next tick."
    )


def test_the_supervisor_check_judges_the_retired_scheduler_entry(
    runbook: str,
) -> None:
    """The supervisor check is about the retired scheduler's own entry.

    The managed set the supervisor exposes belongs to the supervisor: it
    holds the PDT backend, the provider optimizer, the usage forwarder
    and the perpetual services, before the cutover and after it alike.
    So "the list holds exactly one name" is a criterion nobody can
    sign off — it is false before the cutover and false after it — and
    an operator who follows it literally removes the PDT backend along
    with the old scheduler.

    What the checklist has to judge is the *retired scheduler's* entry:
    the name the supervisor registers it under, ``ac-scheduler``.  That
    entry must not be in the managed set, because a supervisor that
    still manages the retired scheduler starts it again on the next
    tick.  If the entry is kept at all, it is kept as the read-only
    Feishu forwarder and nothing else — a statement about the shape of
    one process, not about the size of the list.

    So the section has to name the entry, say what the surviving
    forwarder is allowed to be, and say that the other managed services
    are none of this item's business.
    """
    section = _section(runbook, DECOMMISSION)

    assert RETIRED_SCHEDULER_ENTRY_RE.search(section), (
        "the decommission checklist does not name the retired scheduler's "
        f"own supervisor entry ({RETIRED_SCHEDULER_ENTRY}). The check has "
        "to be about the retired scheduler's entry: the managed set "
        "belongs to the supervisor and holds the PDT backend and the "
        "other subsidiaries too, so a criterion about the whole list "
        "cannot be signed off before the cutover or after it."
    )

    assert SINGLE_NAME_CLAIM not in section.lower(), (
        "the decommission checklist still asks the supervisor's managed "
        f"set to hold a single name ({SINGLE_NAME_CLAIM!r}). The managed "
        "set is the supervisor's business — it holds the PDT backend and "
        "the other subsidiaries too — so that criterion is false before "
        "the cutover and false after it, and an operator who follows it "
        "literally removes the PDT backend."
    )

    assert FORWARDER_HEADER in section, (
        "the decommission checklist does not say what the surviving "
        f"forwarder carries ({FORWARDER_HEADER}). PDT answers 403 to a "
        "request that arrives without that header, so a checklist that "
        "describes the forwarder as reading PDT has to say how."
    )

    for entry in OUT_OF_SCOPE_ENTRIES:
        assert entry in section, (
            f"the decommission checklist does not say that {entry!r} is "
            "out of this item's scope. The checklist judges the retired "
            "scheduler's entry, not the size of the supervisor's "
            "managed set, and the services that are none of the retired "
            "scheduler's business must not be removed because of it."
        )


def test_no_machine_specific_paths(runbook: str) -> None:
    """The runbook names no path under anybody's home directory.

    The runbook is read on machines that are not this repository's
    author's.  A path that spells out a home directory is a path that
    is wrong everywhere else, so the document reaches the home directory
    through ``$HOME`` — which the shell expands where the command is
    pasted — and writes down no absolute path under it.
    """
    for pattern in _HOME_PATH_PATTERNS:
        match = pattern.search(runbook)
        assert match is None, (
            "the runbook contains a machine-specific home path "
            f"({match.group(0) if match else ''}). The document is read on "
            "other people's machines: reach the home directory through "
            "$HOME and write down no absolute path under it."
        )

    assert "$HOME" in runbook, (
        "the runbook never refers to the home directory through $HOME. "
        "The retired scheduler's state file lives under the home "
        "directory, and the check that proves it is gone has to name the "
        "place without spelling it out."
    )

    # The Chinese page ships the same commands, so it carries the same
    # constraint: a path that is wrong on this machine is wrong in every
    # language.
    if CHINESE_SIBLING.exists():
        sibling = CHINESE_SIBLING.read_text(encoding="utf-8")
        for pattern in _HOME_PATH_PATTERNS:
            match = pattern.search(sibling)
            assert match is None, (
                "the Chinese page contains a machine-specific home path "
                f"({match.group(0) if match else ''}). The document is "
                "read on other people's machines: reach the home "
                "directory through $HOME and write down no absolute path "
                "under it."
            )


def test_dryrun_section_names_the_replay_command(runbook: str) -> None:
    """The dry-run section carries the command that judges the artifacts.

    The judgement is executable — a script that replays the recorded run
    against the criterion — and the runbook has to carry that command,
    with its three inputs and its exit codes.  A section that says
    "check the artifacts" without saying how gives the operator nothing
    to run, and the 24 hour wait is not something that can be repeated
    on a hunch.
    """
    section = _section(runbook, SECTIONS[1])

    blocks = _blocks_containing(section, "scheduler_dryrun_diff.py")
    assert blocks, (
        "the dry-run section has no command that runs the replay script. "
        "The judgement of the dry run's artifacts is executable, and the "
        "section has to carry the command that runs it."
    )

    body = blocks[0]
    for option in ("--ticks", "--windows", "--out"):
        assert option in body, (
            f"the replay command does not pass {option}. The script reads "
            "the recorded ticks and the recorded windows and writes the "
            "report; all three are inputs to the command."
        )

    assert "ticks.jsonl" in body and "windows.json" in body, (
        "the replay command does not name the two artifacts it reads "
        "(ticks.jsonl and windows.json). The names are the contract "
        "between the dry run and the judgement."
    )


def test_cutover_stops_the_old_scheduler_before_enabling_the_new_one(
    runbook: str,
) -> None:
    """The cutover stops the old scheduler *first*.

    This is the invariant the whole procedure exists for: at any moment
    there is exactly one scheduler.  Two schedulers are two writers on
    the same queue, and the operator finds out about the second one
    when the two have already disagreed.

    The order is checked, not just the presence of both steps.  A
    runbook that stops the old scheduler somewhere after it has already
    turned the new one on describes a window in which both were live —
    and it describes it in the present tense, which is how it gets
    followed.
    """
    section = _section(runbook, SECTIONS[2])

    stop_at = section.find("cron edit")
    enable_at = section.find("enabled: true")

    assert stop_at != -1, (
        "the cutover section has no command that stops the old scheduler's "
        "tick. The old scheduler is woken by an external timer, and the "
        "cutover has to stop that timer before the new scheduler is "
        "turned on."
    )
    assert enable_at != -1, (
        "the cutover section never sets `enabled: true`. Turning the "
        "native scheduler on is the second half of the cutover, and it "
        "is written down as a setting in the configuration file."
    )
    assert stop_at < enable_at, (
        "the cutover section turns the new scheduler on before it stops "
        "the old one. The two must never run at the same time: the old "
        "scheduler's tick is stopped first, then the native scheduler is "
        "turned on."
    )


def test_the_legacy_probe_is_the_same_page_in_both_sections(runbook: str) -> None:
    """Both ends of the procedure probe the old face the same way.

    The pre-flight asks whether the old service is still up, and the
    decommission asks whether it is gone.  Those are two ends of one
    question, so they have to be asked of the same path — otherwise the
    operator compares two different signals and cannot tell a change in
    the service from a change in the probe.

    The path is the read-only page, and that is not a matter of taste.
    Every path under the old face's ``/api/`` prefix sits behind its
    guard header, so a probe against one of them is answered 403 while
    the service is alive: ``curl -f`` reports a live service as dead,
    and the operator goes looking for a process that was running all
    along.  The page is the one path there that answers without a
    header, because a browser has to be able to load it before it can
    ask the API anything.
    """
    preflight = _section(runbook, SECTIONS[0])
    decommission = _section(runbook, DECOMMISSION)

    pre_urls = _legacy_urls(preflight)
    assert LEGACY_PAGE in pre_urls, (
        "the pre-flight does not probe the old scheduler's read-only page "
        f"({LEGACY_PAGE}). It probes {sorted(pre_urls)} instead. The "
        "pre-flight and the decommission have to probe the same path, or "
        "the operator is comparing two different signals."
    )

    guarded = sorted(url for url in pre_urls if "/api/" in url)
    assert not guarded, (
        f"the pre-flight probes guarded API paths ({guarded}). The old "
        "face answers 403 for an /api/ path whose guard header is "
        "missing, so the probe would report a live service as dead. "
        "Probe the read-only page, which answers without a header."
    )

    dec_urls = _legacy_urls(decommission)
    assert LEGACY_PAGE in dec_urls, (
        "the decommission checklist does not probe the old scheduler's "
        f"read-only page ({LEGACY_PAGE}); it probes {sorted(dec_urls)}."
    )


def test_the_chinese_sibling_carries_the_same_sections() -> None:
    """The Chinese page exists, and it has the same five sections.

    The site publishes two locales and the header offers a switch
    between them.  The switch is a promise that the page exists in the
    other language too, and nothing in the toolchain checks it: a page
    with no translation builds, passes ``--strict`` and silently hands
    a Chinese reader English.

    So the sibling is part of the deliverable, not a courtesy — and it
    has to carry the same five sections, because the two pages are one
    procedure in two languages.
    """
    assert CHINESE_SIBLING.exists(), (
        f"{CHINESE_SIBLING.relative_to(_REPO_ROOT)} does not exist. The "
        "site's language selector promises the page exists in the other "
        "locale; without the sibling a Chinese reader silently gets "
        "English, and the build says nothing about it."
    )

    headings = _level2_headings(
        CHINESE_SIBLING.read_text(encoding="utf-8")
    )
    assert headings == list(ZH_SECTIONS), (
        "the Chinese page's level-2 sections are not the five the "
        f"procedure has.\n  expected: {list(ZH_SECTIONS)}\n"
        f"  found:    {headings}\nThe two pages are one procedure in two "
        "languages: a section that exists in one and not the other is a "
        "procedure the two languages disagree about."
    )


def test_the_chinese_sibling_judges_the_same_supervisor_entry() -> None:
    """The Chinese page carries the same criterion, about the same entry.

    The two pages are one procedure in two languages, and the Chinese
    reader is the one who would go and remove the PDT backend: a
    criterion that is wrong in one language is wrong in both.  So the
    Chinese checklist names the retired scheduler's own entry, says
    what the surviving forwarder carries, and says that the other
    managed services are not this item's business.
    """
    text = CHINESE_SIBLING.read_text(encoding="utf-8")
    section = _section(text, ZH_SECTIONS[4])

    assert RETIRED_SCHEDULER_ENTRY_RE.search(section), (
        "the Chinese page's decommission checklist does not name the "
        "retired scheduler's own supervisor entry "
        f"({RETIRED_SCHEDULER_ENTRY}). The check has to be about that "
        "entry: the managed set belongs to the supervisor and holds the "
        "PDT backend and the other subsidiaries too."
    )

    assert not re.search(
        r"列表里只有\s*`?provider-usage-forwarder`?", section
    ), (
        "the Chinese page's decommission checklist still asks the "
        "supervisor's managed set to hold a single name. That criterion "
        "is false before the cutover and false after it, and an operator "
        "who follows it literally removes the PDT backend."
    )

    assert FORWARDER_HEADER in section, (
        "the Chinese page's decommission checklist does not say what the "
        f"surviving forwarder carries ({FORWARDER_HEADER})."
    )

    for entry in OUT_OF_SCOPE_ENTRIES:
        assert entry in section, (
            f"the Chinese page's decommission checklist does not say that "
            f"{entry!r} is out of this item's scope."
        )


def test_the_runbook_is_in_the_site_navigation() -> None:
    """The nav points at the runbook.

    A page no nav entry points at is a page nobody finds: it is built,
    it is served, and it is not in the way anyone walks.  The runbook
    is the document an operator reaches for *during* a cutover, so it
    has to be where the operations pages are, not in a corner of the
    site that only a search finds.
    """
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    nav = config.get("nav")
    assert nav, (
        "mkdocs.yml has no nav. The site is built from the nav, so a "
        "runbook that is not in it is a runbook nobody is told about."
    )

    paths = _nav_paths(nav)
    assert NAV_PATH in paths, (
        f"the nav does not point at {NAV_PATH}. The runbook is built and "
        "served, but nothing in the site links to it: the operations "
        "reader who needs it during a cutover never sees it. The nav "
        f"currently points at {sorted(paths)}."
    )
