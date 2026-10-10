"""Quality checks for a task's ``test_command``.

Why this exists
---------------
A task's completion verdict is a cross-check between the subagent's
``TEST_RESULT`` claim and the command's exit code (the dual-criterion
rule). That second signal is only worth anything if the command can
distinguish a correct repair from an incorrect one. Some commands
cannot, no matter how good the code is.

The one shape this module started with is the **probe chain**: an ``&&``
sequence in which an inspection verb (``ls``, ``grep``, ``find``,
``cat``, …) appears *before* the final link. Those verbs return
non-zero when they find nothing — an ``ls`` on a path that does not
exist exits 1 even with ``2>/dev/null`` (the redirect hides the message,
not the status) — and ``&&`` short-circuits, so the whole chain exits
non-zero. The command is then *structurally* unable to report success,
which means the gate records a failure and scores the subagent's
``TEST_RESULT: PASSED`` as a lie. A generated task can carry exactly
this shape:

    cd … && grep -n 'TODO' … && ls -la …/libnative_ext.dylib 2>/dev/null \\
      && ls -la …/*.so 2>/dev/null && source … && python3 -c "…"

It burned two full retry cycles plus a refiner pass before anyone
noticed the command itself was the problem.

What this module checks
-----------------------
Three shapes, in order of how much they cost when missed:

1. ``no_execution`` — a ``python -c`` program that decides pass/fail
   with ``sys.exit`` over data it read from a file, without starting any
   process. The command compares a string in ``verification_plan.json``
   against ``sys.argv[3]``; the real ``cargo test`` it quotes is an
   argument being compared, never a process being started. These exit 0
   before any repair has run — so every one of those tasks certifies
   work nobody did.
2. ``exit_code_swallowed_by_pipe`` — the pipeline ends in ``tee`` /
   ``grep``, so a failing run reports SUCCESS.
3. ``probe_chain`` — a mid-chain ``grep`` / ``ls`` that exits non-zero
   when it finds nothing, so the chain can never report success.

Quote-awareness is load-bearing for all three: what is inside quotes is
data, not a command. A runner name that appears only inside a string
literal is not "this command runs tests" — see
:func:`_strip_quoted_spans`.

What this module deliberately does NOT flag
-------------------------------------------
**Absolute paths.** An earlier draft rejected ``/Users/<name>/…`` and
``cd ~/…`` as "non-portable". Against the real plan corpus that flags
almost every command — because in this deployment absolute paths are the
*norm and the instruction*: the plan's ``project_dir`` is itself an
absolute path, and ``repair_generator`` explicitly tells the LLM to
"用绝对路径替换未替换的占位符". A rule that fires on nearly all healthy
input is noise, not a gate, so it was removed.

**A bare ``grep``/``ls`` as the whole command.** A single ``grep`` that
exits 1 when the pattern is absent is a legitimate contract check, and
several real repair tasks are exactly that. Inspecting is
fine; inspecting *as a mid-chain gate* is what breaks.

A clean result here is not a guarantee that a command is a good test.
These are *shape* checks, and a well-formed command can still be
vacuous. The complementary property — that the command must **fail**
before the work is done — is checked separately by
:func:`check_falsifiable`, which is a runtime probe and has no false
positives by construction: a command that already passes cannot
certify anything that happens afterwards.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from bounded_subprocess import run_bounded

#: Inspection verbs. These gather evidence — they are not assertions,
#: and they return non-zero when they find nothing.
#:
#: Deliberately excludes ``cd``, ``source``, ``export`` and ``timeout``:
#: those are setup steps, they appear at the front of ordinary chains
#: like ``cd X && source venv/bin/activate && pytest``, and treating
#: them as probes flagged every legitimate multi-step command in the
#: corpus.
PROBE_VERBS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ls", "find", "cat", "head",
    "tail", "stat", "file", "du", "wc", "jq", "diff", "readlink",
    "basename", "dirname", "which", "type", "test",
})

#: Commands that actually run a test suite. Their presence anywhere in
#: the chain is enough to clear it — a chain is allowed to inspect things
#: on the way *to* running tests.
TEST_RUNNERS = (
    "pytest", "py.test", "unittest", "tox", "nox",
    "cargo test", "cargo nextest", "go test", "mvn test", "gradle test",
    "npm test", "npm run test", "pnpm test", "pnpm run test",
    "yarn test", "jest", "vitest", "playwright test", "rspec",
    "make test", "ctest", "dotnet test",
)

#: Minimum ``&&``-joined segments before a command counts as a chain.
#: Two segments is ordinary (``cd x && pytest``); the failure mode needs
#: enough links that one of them is bound to miss.
_MIN_CHAIN_SEGMENTS = 3

#: Process-spawning entry points. A ``python -c`` program that decides
#: pass/fail (``sys.exit``) while reading a file but calling none of
#: these can only be *comparing data*. Its exit status is a function of
#: the file it read, never of the code under test — which is what makes
#: it useless as a task's completion signal.
_PROCESS_SPAWNERS = (
    "subprocess", "os.system", "os.popen", "os.exec", "os.spawn",
    "pty.spawn", "runpy", "pexpect",
)

#: Evidence that the ``-c`` program read something off disk. Paired with
#: ``sys.exit(`` this is the "spec echo" shape: read a file, compare,
#: exit. Requiring the read keeps real one-liner assertions clear —
#: ``python3 -c "import native_ext; sys.exit(0 if p else 1)"`` exercises
#: the module under test and must NOT be flagged.
_FILE_READS = ("open(", "json.load", "read_text", "read_bytes", "Path(")

#: Matches ``python -c <program>``, tolerating a leading ``timeout N``
#: and env assignments. The named group is the program text.
_PYTHON_C_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:timeout\s+\d+\s+)?"
    r"python[\d.]*\s+-c\s+(?P<rest>.*)$",
    re.DOTALL,
)


@dataclass(frozen=True)
class CommandIssue:
    """One defect found in a ``test_command``.

    ``code`` is a stable identifier for log filtering; ``detail`` is the
    operator-facing explanation.
    """

    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.code}: {self.detail}"


def _strip_quoted_spans(command: str) -> str:
    """Return ``command`` with every quoted span removed.

    What is inside quotes is *data* — an argument being passed or
    compared — not a command being executed. A generated repair command
    quotes a genuine ``cargo test …`` invocation as an argument to
    ``sys.argv[3]``; a substring search over the whole command text
    counts that as "this command runs tests", which is what
    let the family through the guard.

    Backslash escapes are honoured inside double quotes (and inside
    single quotes, where a backslash is literal, the escape branch is
    simply never taken because ``quote != '"'``).
    """
    out: List[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            else:
                out.append(ch)
            i += 1
            continue
        if ch == "\\" and quote == '"' and i + 1 < len(command):
            i += 2
            continue
        if ch == quote:
            quote = None
        i += 1
    return "".join(out)


def _split_top_level_chain(command: str) -> List[str]:
    """Split on ``&&``, ignoring separators inside quotes.

    A naive ``split("&&")`` mis-splits
    ``python -c "a and b" && pytest`` and similar. Quote-awareness keeps
    the segment count honest, which is what the probe-chain heuristic
    keys on.
    """
    segments: List[str] = []
    current: List[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and i + 1 < len(command):
                current.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            current.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if command.startswith("&&", i):
            segments.append("".join(current))
            current = []
            i += 2
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return [s.strip() for s in segments if s.strip()]


def _first_word(segment: str) -> str:
    """Leading command word of a segment, ignoring env assignments.

    ``FOO=1 cargo test`` → ``cargo``; ``timeout 600 pytest`` → ``timeout``
    (which is why the runner check looks at the whole segment rather than
    this word alone).
    """
    for token in segment.split():
        if "=" in token and not token.startswith("-"):
            continue
        return token
    return ""


# ---------------------------------------------------------------------------
# Pipeline exit codes (2026-09-17)
# ---------------------------------------------------------------------------
#
# A pipeline exits with the status of its LAST command. So ``… | tee log``
# exits 0 no matter what the tested command did, and ``… | grep X`` exits
# 0 whenever the pattern matched — including when the tested command
# failed and printed nothing matching.
#
# This is the mirror image of ``probe_chain``: that one is structurally
# unable to report success (guaranteeing a FALSE FAILURE), this one is
# structurally unable to report failure (guaranteeing a FALSE PASS — and
# a false pass is worse, because nobody investigates it). A full-CI task
# was skipped as "already done" by the pre-flight because its
# ``ci_local.py … | tee`` command could only ever exit 0.

#: Verbs that exit 0 whenever they run at all. As the final stage of a
#: pipeline they erase the tested command's status.
EXIT_CODE_SINK_VERBS = frozenset({
    "tee", "cat", "head", "tail", "wc", "cut", "tr", "sort", "uniq",
    "column", "awk", "sed", "base64", "xxd", "strings", "nl", "rev",
    "fold", "fmt", "paste", "more", "less", "iconv", "expand",
})

#: Verbs whose exit status reflects only their own match, not the
#: upstream command's. ``pytest … | grep FAILED`` is 0 when pytest failed
#: *and* printed FAILED.
MATCH_FILTER_VERBS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})

#: Ways a command explicitly re-propagates the upstream status. Their
#: presence disables both pipeline checks — the author has handled it.
_PIPE_STATUS_ESCAPES = ("pipefail", "PIPESTATUS")


def _split_top_level(command: str, separators: Tuple[str, ...]) -> List[str]:
    """Split ``command`` on ``separators``, ignoring quoted text.

    Quote-awareness matters here for the same reason it does in
    ``_split_top_level_chain``: ``python3 -c "print('a;b')"`` must not be
    read as two commands.
    """
    parts: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and i + 1 < len(command):
                current.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            current.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        for sep in separators:
            if command.startswith(sep, i):
                parts.append("".join(current))
                current = []
                i += len(sep)
                break
        else:
            current.append(ch)
            i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _last_link(command: str) -> str:
    """The final top-level command — the one whose status is the verdict.

    Handles ``;``, ``&&``, ``||`` and newlines. A trailing standalone
    ``&`` is dropped (backgrounded work is not the verdict).
    """
    links = _split_top_level(command, (";", "&&", "||", "\n"))
    return links[-1] if links else ""


def _pipe_stages(link: str) -> List[str]:
    """Top-level stages of ``link`` (``|`` only, never ``||``)."""
    parts: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(link):
        ch = link[i]
        if quote:
            if ch == "\\" and i + 1 < len(link):
                current.append(link[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            current.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "|" and not link.startswith("||", i):
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def find_semicolon_exit_issue(command: str) -> Optional[CommandIssue]:
    """``a ; b`` — the task is judged by ``b``'s status, not ``a``'s.

    Same family as a pipeline ending in ``tee``: the command the task
    cares about is not the one whose exit status reaches the gate. ``;``
    runs the next segment *whatever* the previous one did, so a failing
    test is masked by any later command that succeeds.

    Caught by the tasks self-review on the 2026-09-21 regeneration, which
    read the command rather than shape-matching it:

        cd native_ext && cargo test --test x ; venv1/bin/pytest tests/y.py -v

    exits 0 whenever pytest passes, however cargo did. Use ``&&``
    (short-circuits on failure) or two separate ``test_commands`` entries.
    """
    segments = _split_top_level(command, (";",))
    if len(segments) < 2:
        return None
    # The "capture and re-raise" idiom — `cmd > log 2>&1; rc=$?; …; exit $rc`
    # — propagates the status deliberately, exactly like `set -o pipefail`
    # does for a pipeline. Repair commands legitimately take this
    # shape; flagging them would have the rewriting agent "fix" commands
    # that are already correct.
    if "$?" in command and re.search(r"\bexit\b", command):
        return None
    return CommandIssue(
        code="exit_code_swallowed_by_semicolon",
        detail=(
            f"`;`-joined chain of {len(segments)} commands: the exit status "
            f"is the LAST one's, so an earlier failure never reaches the "
            f"completion gate — a failing test run masked by any later "
            f"command that succeeds. Use `&&` (short-circuits on failure) "
            f"or split into separate `test_commands` entries."
        ),
    )


def find_pipeline_exit_issue(command: str) -> Optional[CommandIssue]:
    """Flag a final pipeline stage that hides the tested command's status."""
    if any(escape in command for escape in _PIPE_STATUS_ESCAPES):
        # ``set -o pipefail`` or an explicit ``${PIPESTATUS[0]}`` — the
        # author propagated the status deliberately.
        return None

    link = _last_link(command)
    stages = _pipe_stages(link)
    if len(stages) < 2:
        return None

    last_stage = stages[-1]
    verb = _first_word(last_stage).split("/")[-1].lower()
    if verb in EXIT_CODE_SINK_VERBS:
        return CommandIssue(
            code="exit_code_swallowed_by_pipe",
            detail=(
                f"the pipeline ends in `{verb}`, which exits 0 whenever it "
                f"runs — so the command reports SUCCESS no matter what the "
                f"tested command did. Capture the output with a redirect "
                f"instead (`cmd > /tmp/out 2>&1; rc=$?; …; exit $rc`), or "
                f"re-propagate explicitly (`set -o pipefail`, "
                f"`${{PIPESTATUS[0]}}`)"
            ),
        )
    if verb in MATCH_FILTER_VERBS:
        return CommandIssue(
            code="upstream_failure_masked_by_filter",
            detail=(
                f"the pipeline ends in `{verb}`, so the exit status is the "
                f"match result — a failing test run that still prints a "
                f"matching line reports SUCCESS. Assert on the runner's own "
                f"status instead (`cmd > /tmp/out 2>&1; rc=$?; …; exit $rc`)"
            ),
        )
    return None


def has_test_runner(command: str) -> bool:
    """True when the command invokes something that runs tests.

    Quoted spans are stripped first. A runner name that appears only
    inside a string literal is an argument being passed or compared, not
    a command being executed — see :func:`_strip_quoted_spans` for the case that made this necessary.
    """
    return _contains_test_runner(_strip_quoted_spans(command))


def _contains_test_runner(text: str) -> bool:
    lowered = text.lower()
    return any(runner in lowered for runner in TEST_RUNNERS)


def _python_c_program(command: str) -> Optional[str]:
    """Return the ``-c`` program text of a ``python -c`` command.

    ``None`` when ``command`` is not a bare ``python -c`` invocation, or
    when the program is empty. Only the program matters for
    :func:`_no_execution_issue`; the arguments after it are data.
    """
    match = _PYTHON_C_RE.match(command)
    if match is None:
        return None
    rest = match.group("rest")
    if not rest:
        return None
    quote = rest[0]
    if quote not in ("'", '"'):
        # Unquoted program — a single shell word.
        return rest.split()[0] if rest.split() else None
    body: List[str] = []
    i = 1
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and quote == '"' and i + 1 < len(rest):
            body.append(rest[i + 1])
            i += 2
            continue
        if ch == quote:
            break
        body.append(ch)
        i += 1
    return "".join(body)


def _no_execution_issue(command: str) -> Optional[CommandIssue]:
    """A ``python -c`` probe that decides pass/fail without running anything.

    A generated repair task can ship a command shaped like::

        python3 -c "import json,sys; d=json.load(open(sys.argv[1]));
                    cur=[v for v in d['verification_points']
                         if v.get('id')==sys.argv[2]];
                    sys.exit(1 if not cur or
                             cur[0].get('test_command','').strip()==sys.argv[3]
                             else 0)"
                 '<plans>/…/verification_plan.json' 'VP-013' \\
                 'bash -c "… cargo test --test signal_classification …"'

    The program reads the *plan file* and compares two strings. The real
    test command it quotes is an argument being compared, never a process
    being started — so the exit status is a function of the plan file's
    contents and of nothing else. The task can therefore never
    distinguish "the fix worked" from "nothing was changed".

    Most such commands already exit 0 before any repair has run. That is
    precisely what makes the dual-criterion gate's second signal vacuous,
    and what produces empty-diff commits that the executor nevertheless
    records as completions.

    Only the *shape* is detected here. Whether a given command actually
    fails on the pre-repair state is a runtime property and is checked
    separately by :func:`check_falsifiable`.
    """
    program = _python_c_program(command)
    if program is None:
        return None
    if "sys.exit(" not in program:
        return None
    if any(spawner in program for spawner in _PROCESS_SPAWNERS):
        return None
    if not any(reader in program for reader in _FILE_READS):
        return None
    return CommandIssue(
        code="no_execution",
        detail=(
            "the `python -c` program calls sys.exit(…) on data it read "
            "from a file, without spawning any process — so its exit "
            "status is a function of that file's contents, not of the "
            "code under test. A command like this passes whether or not "
            "the task changed anything, which makes the exit-code half "
            "of the completion verdict vacuous. Run the test the task is "
            "supposed to be about instead."
        ),
    )


#: Runner summary lines that say "the runner started and collected
#: nothing to run". Every pattern is anchored to a line the runner
#: itself prints, so a fixture's own text cannot masquerade as one.
#:
#: The cargo entries are the ones this list exists for:
#: ``cargo test --lib no_such_module`` prints ``running 0 tests`` and
#: exits 0, so a module-wide filter naming a module that does not exist
#: is indistinguishable — by exit code alone — from a passing run.
_ZERO_TEST_SIGNATURES: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"^running 0 tests?$", re.MULTILINE), "cargo"),
    (
        re.compile(r"^test result: ok\. 0 passed; 0 failed", re.MULTILINE),
        "cargo",
    ),
    (re.compile(r"^no tests ran in ", re.MULTILINE), "pytest"),
    (re.compile(r"^collected 0 items", re.MULTILINE), "pytest"),
    # go prints this on the package's own line, alongside the package
    # path (`?   example.com/pkg   [no test files]`), so it cannot be
    # anchored to the whole line.
    (re.compile(r"\[no test files\]"), "go"),
    (re.compile(r"^No tests found", re.MULTILINE), "jest"),
    (re.compile(r"^No test files found", re.MULTILINE), "vitest"),
)

#: The counterpart: positive evidence that a test really ran. A zero
#: signature on its own is NOT a verdict — a plain ``cargo test`` prints
#: ``running 0 tests`` for every target that happens to be empty while
#: hundreds of tests run in the others. "Vacuous" means the whole run
#: shows no positive count anywhere. Patterns with a capture group must
#: report a count above zero; those without one are positive by presence.
_POSITIVE_TEST_SIGNATURES: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"^running (\d+) tests?$", re.MULTILINE),          # cargo
    re.compile(r"^test result: .*?(\d+) passed", re.MULTILINE),   # cargo
    re.compile(r"(\d+) passed\b"),                                # pytest / jest
    re.compile(r"(\d+) passing\b"),                               # mocha
    re.compile(r"(\d+) failed\b"),                                # pytest / jest
    re.compile(r"^ok\s+\S+", re.MULTILINE),                       # go
)


def _has_positive_test_evidence(output: str) -> bool:
    """True when ``output`` carries a count proving at least one test ran."""
    for pattern in _POSITIVE_TEST_SIGNATURES:
        for match in pattern.finditer(output):
            if not match.groups():
                return True
            try:
                if int(match.group(1)) > 0:
                    return True
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
    return False


def find_vacuous_pass(output: str) -> Optional[CommandIssue]:
    """A run that exited 0 having executed no tests at all.

    This is the runtime counterpart of the *shape* checks above. Those
    can only see what a command looks like; this one reads what the
    runner actually reported, which is the only way to catch a filter
    that names something absent. The module docstring of
    ``tasks_generator``'s falsifiability probe has recorded the defect
    for a while — "``cargo test --lib no_such_test`` exits 0" — but it
    was a **generation-time** probe, so it never saw a command the
    refiner wrote during execution.

    The cost of missing it is not a wrong exit code, it is a wrong
    ``completed``: in the 20261010-CC-Switch-Remote-Aut plan four tasks
    were recorded completed on exactly this signature, with zero commits
    and the module they were supposed to create never written at all.
    Exit code 0 from a run that executed nothing makes the command half
    of the dual-criterion rule vacuous — the single-signal mode that
    rule exists to prevent.

    Returns ``None`` when the output shows a test really ran, when no
    runner summary is recognisable at all (an unknown runner must not be
    judged by another runner's grammar), or when ``output`` is empty.
    """
    if not output:
        return None
    runner: Optional[str] = None
    for pattern, name in _ZERO_TEST_SIGNATURES:
        if pattern.search(output):
            runner = name
            break
    if runner is None:
        return None
    if _has_positive_test_evidence(output):
        return None
    return CommandIssue(
        code="vacuous_pass",
        detail=(
            f"the {runner} output reports a run that collected no tests, "
            f"so exit code 0 says only that nothing was executed — not "
            f"that this task's work is present. A runner filter naming "
            f"something that does not exist (`cargo test --lib "
            f"no_such_mod`) exits 0 exactly this way. Point the command "
            f"at the test the task actually adds, or drop the filter."
        ),
    )


def commands_of(task: Any) -> List[str]:
    """Every non-empty test command carried by ``task``, either schema form.

    A task carries its commands in one of two shapes:

    * ``test_commands`` — a list, the canonical form the generator is
      instructed to write (``tasks_generator``: "优先使用 test_commands
      （数组），当只有一个命令时才用 test_command（字符串）").
    * ``test_command`` — a single string, the legacy form still present
      throughout the corpus and in every repair task.

    A quality check that reads only one of them silently passes every
    task that uses the other. That is not hypothetical: the 2026-09-20
    regeneration produced 17 tasks, all in list form, and the checks —
    which read only the string form — reported zero problems while two
    of the commands were provably vacuous.

    Accepts a mapping or any object exposing the two attributes, so both
    sides of the ``SubTask`` model boundary can share one helper.
    """
    if isinstance(task, Mapping):
        def _get(key: str, default: Any = None) -> Any:
            return task.get(key, default)
    else:
        def _get(key: str, default: Any = None) -> Any:
            return getattr(task, key, default)

    commands: List[str] = []
    for raw in _get("test_commands") or []:
        text = str(raw or "").strip()
        if text and text not in commands:
            commands.append(text)

    single = str(_get("test_command") or "").strip()
    if single and single not in commands:
        commands.append(single)

    return commands


def inspect_command(command: str) -> List[CommandIssue]:
    """Return every quality issue found in ``command``.

    An empty/whitespace command yields no issues — a missing command is
    the dual-signal gate's problem, not this module's, and reporting it
    twice would only add noise.
    """
    command = (command or "").strip()
    if not command:
        return []

    issues: List[CommandIssue] = []

    # Pipeline exit-code checks run FIRST and independently of the
    # probe-chain heuristic below: that heuristic deliberately bails out
    # when the command runs a test runner, but a test runner piped into
    # ``tee`` is exactly the shape we must not miss (repair-r3-08 ran a
    # full CI suite through ``| tee`` and could never fail).
    pipeline_issue = find_pipeline_exit_issue(command)
    if pipeline_issue is not None:
        issues.append(pipeline_issue)

    semicolon_issue = find_semicolon_exit_issue(command)
    if semicolon_issue is not None:
        issues.append(semicolon_issue)

    no_execution = _no_execution_issue(command)
    if no_execution is not None:
        issues.append(no_execution)

    probe_issue = _probe_chain_issue(command)
    if probe_issue is not None:
        issues.append(probe_issue)

    return issues


def _probe_chain_issue(command: str) -> Optional[CommandIssue]:
    """The mid-chain inspection-verb check (structurally-unpassable chains)."""
    segments = _split_top_level_chain(command)
    if len(segments) < _MIN_CHAIN_SEGMENTS:
        return None
    if has_test_runner(command):
        # The chain runs tests somewhere, so inspection links are on the
        # way to real verification rather than standing in for it.
        return None

    # Only NON-FINAL segments count: a probe in the last position is the
    # command's actual assertion (``grep -q X file`` is a contract check
    # that means something), whereas a probe *before* the end is being
    # used as a gate whose failure kills everything after it.
    gating_probes = [
        segment for segment in segments[:-1]
        if _first_word(segment) in PROBE_VERBS
    ]
    if not gating_probes:
        return None

    return CommandIssue(
        code="probe_chain",
        detail=(
            f"{len(segments)}-segment && chain in which "
            f"{len(gating_probes)} mid-chain link(s) are inspection "
            f"commands ({', '.join(_first_word(s) for s in gating_probes)}); "
            f"those exit non-zero when they find nothing — even with "
            f"2>/dev/null, which hides the message and not the status — so "
            f"the chain can never report success regardless of whether the "
            f"fix is correct"
        ),
    )


def is_usable(command: str) -> bool:
    """True when ``command`` has no quality issues. Convenience wrapper."""
    return not inspect_command(command)


#: ``cd X`` at the head of a command — the directory its relative
#: arguments resolve against.
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?P<dir>[^\s&;|]+)")

#: Path-like token with a filename extension.
_PATH_TOKEN_RE = re.compile(
    r"[\w][\w./-]*\.(?:py|rs|ts|tsx|js|jsx|mjs|cjs|json|toml|ya?ml|css|sh)"
)

#: A filename stem ending in ``-``/``_`` right before the extension —
#: ``tests/e2e/signal-type-.py``. Produced when a name is truncated.
_MALFORMED_TAIL_RE = re.compile(r"[-_]\.[A-Za-z0-9]+$")


def _dedot(path: str) -> str:
    """``.github/workflows/x.yml`` and ``github/workflows/x.yml`` → same key."""
    return "/".join(seg.lstrip(".") for seg in str(path).split("/"))


#: Files that mark a directory as its own project. A runner invoked from
#: above one of these picks up a *different* config — and usually a
#: different file set — so a path into that directory will not be found.
#:
#: The marker set is **per runner family**, and that is load-bearing:
#: ``tests/e2e/`` in a target repo carries a ``package.json`` for
#: its Playwright setup, but a *pytest* command there is unaffected by it
#: and collects normally from the repo root. A single marker list flagged
#: those commands as unreachable — a false positive that would have had
#: the rewriting agent break working commands.
_NODE_MARKERS = (
    "package.json", "vitest.config.ts", "vitest.config.js",
    "vitest.config.mts", "playwright.config.ts", "playwright.config.js",
)
_RUST_MARKERS = ("Cargo.toml",)

#: ``(runner keyword, the manifests that govern where it may run)``.
_RUNNER_MARKERS = (
    ("npx", _NODE_MARKERS),
    ("npm", _NODE_MARKERS),
    ("pnpm", _NODE_MARKERS),
    ("yarn", _NODE_MARKERS),
    ("cargo", _RUST_MARKERS),
)


def _governing_markers(command: str) -> Tuple[str, ...]:
    """Manifests that constrain where ``command`` may run.

    Empty when the command uses no runner we know how to reason about —
    in which case this check stays silent rather than guessing.
    """
    lowered = command.lower()
    for runner, markers in _RUNNER_MARKERS:
        if re.search(r"(?:^|[\s;&|(])" + re.escape(runner) + r"\s", lowered):
            return markers
    return ()


def _leading_subproject(
    token: str, base: Path, markers: Tuple[str, ...],
) -> Optional[str]:
    """Leading directory of ``token`` that is its own project, if any.

    ``frontend-app/lib/x.spec.ts`` → ``frontend-app`` when that
    directory carries one of ``markers``. Returns ``None`` when no prefix
    of the path is a project root for this runner family.
    """
    if not markers:
        return None
    parts = [p for p in str(token).split("/") if p and p not in (".", "..")]
    for depth in range(1, len(parts)):
        candidate = "/".join(parts[:depth])
        directory = base / candidate
        try:
            if not directory.is_dir():
                continue
            if any((directory / m).exists() for m in markers):
                return candidate
        except OSError:
            continue
    return None


def _effective_cwd_prefix(command: str) -> str:
    """The ``cd`` target of ``command``, normalised to a bare relative path.

    ``cd frontend-app && …`` → ``frontend-app``;
    ``cd /abs/path`` or ``cd $HOME/…`` → ``""`` (nothing relative to
    reason about).
    """
    match = _CD_PREFIX_RE.match(command)
    if match is None:
        return ""
    target = match.group("dir").strip("\"'").rstrip("/")
    if not target or target.startswith(("/", "$", "~")):
        return ""
    return target


def find_unreachable_targets(task: Any) -> List[Tuple[str, CommandIssue]]:
    """Commands that can never succeed, and declared paths that are malformed.

    The complement of :func:`check_falsifiable`. RED asks "does this
    command fail *before* the work?" — necessary, but a command that
    fails *forever* satisfies it just as well. Aiming a command at a
    nonsense path passes RED and dooms the task.

    Three mechanical checks catch the shapes that do reach the plan:

    * ``doubled_cd_prefix`` — after ``cd X``, a relative argument that
      starts with ``X/`` resolves to ``X/X/…``, so the path misses now
      *and* after the task writes the file.
    * ``malformed_declared_path`` — a ``files_to_modify`` entry whose
      filename stem ends in ``-``/``_`` (a truncated ``.py`` or ``.yml``).
    * ``duplicate_declared_path`` — two entries that are the same path
      once a leading dot is stripped from each segment.
    """
    if isinstance(task, Mapping):
        def _get(key: str, default: Any = None) -> Any:
            return task.get(key, default)
    else:
        def _get(key: str, default: Any = None) -> Any:
            return getattr(task, key, default)

    found: List[Tuple[str, CommandIssue]] = []

    project_dir_raw = str(_get("project_dir") or "").strip()
    project_dir = Path(project_dir_raw) if project_dir_raw else None

    for command in commands_of(task):
        cd_dir = _effective_cwd_prefix(command)
        markers = _governing_markers(command)

        for token in _PATH_TOKEN_RE.findall(command):
            if cd_dir and token.startswith(cd_dir + "/"):
                found.append((command, CommandIssue(
                    code="doubled_cd_prefix",
                    detail=(
                        f"the command does `cd {cd_dir}` and then names "
                        f"`{token}`, which resolves to "
                        f"`{cd_dir}/{token}` — a path that exists neither "
                        f"before nor after the task. The command can never "
                        f"exit 0, so the task can never be completed."
                    ),
                )))
                break

            if project_dir is None:
                continue
            subproject = _leading_subproject(token, project_dir, markers)
            if subproject is None:
                continue
            if cd_dir == subproject or cd_dir.startswith(subproject + "/"):
                continue
            found.append((command, CommandIssue(
                code="subproject_outside_cwd",
                detail=(
                    f"`{token}` lives under `{subproject}/`, which is its own "
                    f"project (it carries its own manifest/config). The "
                    f"command runs from the project root, where the runner "
                    f"uses a *different* config — measured: vitest from the "
                    f"repo root reports `No test files found` for a path "
                    f"under frontend-app/. Prefix the command with "
                    f"`cd {subproject} && ` and drop the prefix from the path."
                ),
            )))
            break

    declared = [str(p) for p in (_get("files_to_modify") or []) if str(p).strip()]
    for path in declared:
        if _MALFORMED_TAIL_RE.search(path):
            found.append((path, CommandIssue(
                code="malformed_declared_path",
                detail=(
                    f"`files_to_modify` declares `{path}`, whose filename "
                    f"stem ends in a separator. This is a truncated name, "
                    f"not a file the task can create."
                ),
            )))

    seen: dict = {}
    for path in declared:
        seen.setdefault(_dedot(path), []).append(path)
    for variants in seen.values():
        if len(variants) > 1:
            found.append((variants[0], CommandIssue(
                code="duplicate_declared_path",
                detail=(
                    f"`files_to_modify` declares the same path twice, "
                    f"differing only by a leading dot: {variants}. One of "
                    f"them does not exist."
                ),
            )))

    return found


#: Python environment managers that run a command inside an environment
#: *they* own rather than the project's own virtualenv.
#:
#: Kept out of :data:`PROBE_VERBS` / :data:`TEST_RUNNERS` because the
#: command *shape* is legal — a repo that owns no virtualenv may
#: legitimately use ``uv run``. It is the project context that turns it
#: into a defect, which is why the check is a separate function rather
#: than another arm of :func:`inspect_command`.
#:
#: Why it matters (observed in production):
#: the task generator emitted ``uv run pytest tests/...`` for the project,
#: which ships its own ``venv1/``. ``uv`` then resolves its own
#: environment, so the subagent and the framework's independent
#: re-run can end up on *different* interpreters — the command passes
#: for one and fails for the other, and the task is scored as a
#: test-report mismatch. Nothing flagged it: the old ``_needs_venv``
#: treated ``uv run`` as "the env is already handled" and skipped
#: venv injection, and no validator looked at interpreter identity.
FOREIGN_ENV_RUNNERS = (
    "uv run",
    "poetry run",
    "pipenv run",
    "pdm run",
    "hatch run",
    "rye run",
    "conda run",
)


def find_foreign_env_runner(command: str) -> Optional[CommandIssue]:
    """Flag a command that delegates to a foreign Python env manager.

    Context-free: it fires on the runner alone, at the head of the
    command or of any ``&&`` / ``;`` / ``|`` link. Whether that is a
    defect depends on the project — use
    :func:`find_interpreter_mismatch` when the caller knows whether the
    project ships its own virtualenv.
    """
    stripped = _strip_quoted_spans(command or "")
    for runner in FOREIGN_ENV_RUNNERS:
        if re.search(rf"(?:^|[;&|]\s*){re.escape(runner)}\s", stripped):
            return CommandIssue(
                "FOREIGN_PY_ENV_RUNNER",
                f"`{runner}` runs the test in an environment it owns, not the "
                f"project's own virtualenv. The interpreter resolved here can "
                f"differ from the one the framework re-runs with, which "
                f"surfaces as a test-report mismatch. Put the project venv's "
                f"binary path in the command (e.g. `<venv>/bin/pytest …`, with "
                f"`<venv>` the directory that project actually uses) instead.",
            )
    return None


def find_interpreter_mismatch(
    task: Any, venv_path: Optional[str]
) -> List[Tuple[str, CommandIssue]]:
    """Interpreter-contract issues on ``task``'s commands.

    ``venv_path`` is the project's own virtualenv — the ``bin/``
    directory or the activate script. ``None``/empty means the project
    owns no virtualenv, in which case a foreign runner is a legitimate
    choice and nothing is reported.
    """
    if not venv_path:
        return []
    found: List[Tuple[str, CommandIssue]] = []
    for command in commands_of(task):
        issue = find_foreign_env_runner(command)
        if issue is not None:
            found.append((command, issue))
    return found


def inspect_task(task: Any) -> List[Tuple[str, CommandIssue]]:
    """Every issue on every command of ``task``, tagged with its command.

    ``inspect_command`` answers "is this one command good?". A caller
    holding a whole task needs "which of this task's commands are bad?"
    — and has to ask it of *both* schema forms, which is what
    :func:`commands_of` is for. Pairing them here keeps that from being
    re-derived (and half-derived) at each call site.
    """
    found: List[Tuple[str, CommandIssue]] = []
    for command in commands_of(task):
        for issue in inspect_command(command):
            found.append((command, issue))
    return found


# ---------------------------------------------------------------------------
# The falsifiability invariant (2026-09-20)
# ---------------------------------------------------------------------------
#
# Everything above is a *static* check on the command's shape. None of
# them can tell whether a command distinguishes a correct fix from no
# fix at all — a command can be perfectly well-formed and still be
# vacuous.
#
# The one property that cannot be faked is: **the command must fail
# before the work is done.** If it already exits 0 on the pre-repair
# state, then whatever the executor does afterwards, the exit code will
# still be 0, and the dual-criterion gate's second signal carries no
# information. That is the invariant this section enforces.
#
# It is cheap to apply at generation time (one run per task) and has no
# false positives by construction: a command that passes before the work
# starts cannot certify the work.

#: Default wall-clock ceiling for the probe run. Generous enough for a
#: focused test file, short enough that a runaway command (a full
#: nightly suite) is caught as inconclusive rather than burning the
#: generation pass.
DEFAULT_FALSIFIABILITY_TIMEOUT_S = 300

#: The backend checkout itself: ``test_command_quality.py`` lives in
#: ``backend/``, so two levels up is the repository root.
_AC_REPO_ROOT = Path(__file__).resolve().parent.parent


def probe_scope_is_usable(project_dir: Any) -> Tuple[bool, str]:
    """May a falsifiability probe execute inside ``project_dir``?

    Returns ``(usable, reason)``. ``reason`` is ``""`` when usable.

    Both probe call sites — ``TasksGenerator.probe_falsifiability`` and
    ``RepairTaskGenerator._passes_falsifiability`` — must ask this
    question before executing a task-authored shell command, and they
    must get the same answer. They did not: on 2026-09-22 the
    generation-side probe was given a guard and the repair-side probe
    was not, so the same hazard stayed reachable through the other
    door. That drift is the reason this predicate is a shared function
    rather than a check inlined at each site.

    Refused cases, and why each one is dangerous:

    * **No declared workspace.** ``cwd=None`` makes ``subprocess``
      inherit the caller's directory — the backend's own checkout — so the probe
      would run a command that describes nothing, inside the backend's own
      tree.
    * **Not an existing directory.** The command cannot mean anything
      relative to a path that is not there.
    * **Inside the backend's own checkout.** the backend's own unit fixture carries
      ``test_command="pytest tests/ -v"``. Executed at any level of
      the backend's tree that is a genuine nested pytest over the suite, which
      re-enters the codepath that spawned it — an unbounded process
      tree rather than a test result.

    The predicate is deliberately about *where* the command may run,
    not *what* it is: a project's own full-suite command is legitimate
    in that project. Only the backend's tree is off limits, because only there
    does the command re-enter the backend.
    """
    if project_dir is None:
        return False, "no project_dir declared"
    raw = str(project_dir).strip()
    if not raw:
        return False, "no project_dir declared"

    try:
        resolved = Path(raw).resolve()
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
        return False, f"project_dir {raw!r} could not be resolved ({exc})"

    if not resolved.is_dir():
        return False, f"project_dir {resolved} is not an existing directory"

    if resolved == _AC_REPO_ROOT or _AC_REPO_ROOT in resolved.parents:
        return False, (
            f"project_dir {resolved} is inside the backend's own checkout "
            f"({_AC_REPO_ROOT}); a test command run there re-enters the "
            f"the backend suite suite instead of describing a result"
        )

    return True, ""


def check_falsifiable(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_FALSIFIABILITY_TIMEOUT_S,
) -> Tuple[bool, str]:
    """Run ``command`` against the current (pre-repair) state.

    Returns ``(ok, detail)``.

    ``ok=False`` means the command already succeeded — it cannot
    distinguish a correct fix from an unchanged tree, so shipping it
    would make the task's completion verdict vacuous. The caller should
    discard it and regenerate rather than run the task with a signal
    that proves nothing.

    A command that times out is treated as ``ok=True``: something real
    was running and the verdict is simply unknown here, which is a
    different (and much rarer) problem than a command that cannot fail.

    A command that cannot even start is ``ok=False``: it would fail on
    every run regardless of the fix, which is the false-failure mode the
    probe-chain check exists to prevent.

    A command whose *scope* is unusable — no ``cwd``, a ``cwd`` that is
    not a directory, or a ``cwd`` inside the backend's own checkout — is refused
    without being executed, also ``ok=False``. See
    :func:`probe_scope_is_usable`.
    """
    command = (command or "").strip()
    if not command:
        return False, "empty command"

    # The chokepoint. Every probe in the codebase funnels through here
    # (generation: ``TasksGenerator.probe_falsifiability``; repair:
    # ``RepairTaskGenerator._passes_falsifiability``), so the scope
    # question is asked once. Putting it in either caller instead is
    # what let the 2026-09-22 guard cover one door and leave the other
    # open.
    usable, unusable_reason = probe_scope_is_usable(cwd)
    if not usable:
        return False, (
            f"refused to run ({unusable_reason}) — an exit code measured "
            f"there would describe nothing about this task"
        )

    try:
        completed = run_bounded(command, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return True, (
            f"inconclusive: still running after {timeout}s — treated as "
            f"falsifiable because something real is executing"
        )
    except OSError as exc:
        return False, (
            f"cannot start ({exc}) — the command would fail on every run "
            f"regardless of whether the fix is correct"
        )

    if completed.returncode == 0:
        return False, (
            "already exits 0 before the work is done — this command "
            "cannot distinguish a correct fix from no change at all"
        )
    return True, f"fails as required before the work (exit {completed.returncode})"
