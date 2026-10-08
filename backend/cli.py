"""
Command Line Interface
====================

CLI for autonomous coding skill.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

from agent import autonomous_coding
import credentials
from rollback_manager import RollbackManager
from config_registry import ConfigRegistry
from config_loader import load_config_by_name
from execution_logger import get_logger

#: What ``secrets show`` prints in place of a keychain index the
#: environment does not carry. A named state rather than a blank cell:
#: an absent index means there is no item to look up, and an empty
#: column reads as "the value is empty", which is a different problem
#: with a different fix.
INDEX_UNSET = "<unset>"

#: The column headings ``secrets show`` prints. ``service`` is the name
#: this project files the secret under, and it is a display label
#: only: a secret is looked up by the account index beside it and by
#: nothing else, so this column is never turned back into a query.
SHOW_HEADERS = ("service", "account_env_key", "account")

#: The three words ``secrets verify --verify-against`` may print in its
#: comparison column, and the whole of its vocabulary.
#:
#: Three rather than two is the point. A comparison that cannot say
#: "the baseline does not carry this key" has only two answers left, and
#: it gives the second one for a key the file simply does not have —
#: which reads as a finding about a deployment that is in fact fine.
#: The two verdicts mean opposite things to whoever reads them: 不相等
#: is something to go and fix, 基准缺失 is a check that did not finish.
#: An operator who has seen the first alarm raised by an unfinished check
#: has learned not to trust the command.
COMPARE_EQUAL = "相等"
COMPARE_DIFFERENT = "不相等"
COMPARE_BASELINE_ABSENT = "基准缺失"

#: What ``--verify-against`` says when the file cannot be read at all.
#: It names no path: the path is the one string on this code path that
#: the operator did not type into the command and cannot predict — it
#: arrives through a CI job's argv, a shell history, a process listing,
#: and a working directory somebody else chose — and this command has no
#: reason to write any of that to a log it will keep.
BASELINE_UNREADABLE_NOTE = (
    "baseline: the --verify-against file could not be read, so nothing "
    "was compared; no path is printed because this command does not "
    "write the one string it was handed to a log"
)

#: The subcommand names this CLI answers to. Held as a table rather
#: than read back off the parser so the caller of :func:`_leading_subcommand`
#: can be a plain function over a plain argument list — it has to answer
#: before the parser exists, because the answer is what decides the
#: parser's own shape.
SUBCOMMANDS = ("rollback", "configs", "secrets")


def _leading_subcommand(argv) -> Optional[str]:
    """Return the subcommand ``argv`` opens with, or None.

    Only a *leading* token counts. A subcommand named later on the
    line — after ``-w``, say — is left to argparse exactly as it was
    before this existed, so no invocation that parsed one way yesterday
    parses differently today. The documented form puts the subcommand
    first, and the alternative would mean deciding what counts as a
    value for every option on the parser, which is a second grammar to
    keep in step with the first.
    """
    if not argv:
        return None
    first = argv[0]
    return first if first in SUBCOMMANDS else None


def main():
    """Main entry point for CLI."""
    from env_config import load_env

    load_env()

    parser = argparse.ArgumentParser(
        description="Autonomous Coding CLI - Fully autonomous software development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  autonomous-coding "Create a REST API" -w ./myproject
  autonomous-coding "Build a web scraper" -w ./scraper --max-tasks 10
  autonomous-coding --recover -w ./myproject
  autonomous-coding rollback list -w ./myproject
  autonomous-coding rollback to 1-2 -w ./myproject
  autonomous-coding --config harmonyos "Create a UI component" -w ./app
  autonomous-coding secrets verify
  autonomous-coding secrets show
        """
    )

    # Main command arguments
    #
    # ``requirement`` is left out when the line opens with a
    # subcommand. It and the subparser action compete for the same
    # slot, and argparse matches positionals left to right: the
    # ``nargs="?"`` positional swallows the subcommand's own name and
    # the subcommand action is handed its first argument, which is not
    # one of its choices. ``secrets verify`` dies there with "invalid
    # choice: 'verify'", and ``rollback list`` with "invalid choice:
    # 'list'" — the two were unreachable, and a bare ``configs`` fell
    # through to the autonomous path with the requirement "configs".
    # Deciding here, where the leading token is still visible, is what
    # lets the subcommand and the requirement stop overlapping. The
    # attribute is still set, so every reader of the namespace finds
    # it; only the subcommand branch ever sees it as None.
    leading_subcommand = _leading_subcommand(sys.argv[1:])
    if leading_subcommand is None:
        parser.add_argument("requirement", nargs="?", help="The high-level requirement")
    parser.set_defaults(requirement=None)
    parser.add_argument("--workspace", "-w", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--dir", help="Project directory (deprecated, use --workspace)")
    parser.add_argument("--recover", action="store_true", help="Recover from previous crash (skip planning)")
    parser.add_argument("--max-tasks", type=int, default=None, help="Maximum tasks to execute")
    parser.add_argument("--config", "-c", default=None, help="Configuration name (e.g., 'coding', 'harmonyos')")
    parser.add_argument("--tool", "-t", default=None, choices=["claude", "opencode"],
                        help="Coding tool to use: 'claude' (default) or 'opencode'")
    parser.add_argument("--verification-tasks", default=None,
                        help="Path to verification tasks JSON file (for auto-repair loop)")
    parser.add_argument("--tasks-file", default=None,
                        help="Path to tasks.json (default: <workspace>/tasks.json)")

    # Rollback subcommand
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Rollback command
    rollback_parser = subparsers.add_parser('rollback', help='Rollback operations')
    rollback_subparsers = rollback_parser.add_subparsers(dest='rollback_command', help='Rollback commands')

    # List rollback points
    list_parser = rollback_subparsers.add_parser('list', help='List available rollback points')

    # Rollback to specific task
    to_parser = rollback_subparsers.add_parser('to', help='Rollback to a specific task')
    to_parser.add_argument('task_id', help='Task ID to rollback to')
    to_parser.add_argument('--keep', action='store_true', help='Stash changes instead of discarding')

    # Rollback to previous task
    prev_parser = rollback_subparsers.add_parser('prev', help='Rollback to previous task')
    prev_parser.add_argument('--keep', action='store_true', help='Stash changes instead of discarding')

    # Config command
    config_parser = subparsers.add_parser('configs', help='List available configurations')

    # Secrets command
    secrets_parser = subparsers.add_parser(
        'secrets', help='Inspect how provider secrets are configured')
    secrets_subparsers = secrets_parser.add_subparsers(
        dest='secrets_command', help='Secrets commands')

    # Report where each provider secret is read from
    verify_parser = secrets_subparsers.add_parser(
        'verify', help='Report where each provider secret is read from')
    # Compare each secret against a KEY=value file. The comparison is
    # this command's own: the provider is handed a name and answers
    # with a source and a value, and never learns that a baseline file
    # exists.
    verify_parser.add_argument(
        '--verify-against', metavar='PATH', default=None,
        help='Compare each secret with the KEY=value file at PATH and '
             'report equal / not equal / absent from the baseline')

    # List the keychain index each provider secret is filed under
    secrets_subparsers.add_parser(
        'show', help='List the keychain index each provider secret is filed under')

    args = parser.parse_args()

    # Handle rollback commands
    if args.command == 'rollback':
        handle_rollback_command(args)
        return

    # Handle configs command
    if args.command == 'configs':
        handle_configs_command()
        return

    # Handle secrets commands
    if args.command == 'secrets':
        if args.secrets_command == 'verify':
            exit(cmd_secrets_verify(args.verify_against))
        if args.secrets_command == 'show':
            exit(cmd_secrets_show())
        print("Please specify a secrets command: verify or show")
        print("Use 'autonomous-coding secrets --help' for more information")
        exit(1)

    # Handle main autonomous coding
    workspace_dir = args.workspace
    if args.dir:
        workspace_dir = args.dir

    logger = get_logger()

    tasks_file = Path(args.tasks_file) if args.tasks_file else None

    autonomous_coding(
        requirement=args.requirement,
        project_dir=workspace_dir,
        recover=args.recover,
        max_tasks=args.max_tasks,
        config_name=args.config,
        tool=args.tool,
        logger=logger,
        tasks_file=tasks_file,
        # ``args.verification_tasks`` is intentionally NOT forwarded
        # to ``autonomous_coding``: server.py's ``_run_repair_execution``
        # already merges the verification round's repair tasks into the
        # canonical tasks.json before spawning this subprocess (audit
        # 2026-08-26). The flag is accepted on the CLI for backwards
        # compatibility with operators that pass it manually.
    )


def handle_rollback_command(args):
    """Handle rollback subcommands."""
    workspace_dir = args.workspace
    rollback_manager = RollbackManager(workspace_dir)

    if args.rollback_command == 'list':
        print(rollback_manager.list_rollback_points())

    elif args.rollback_command == 'to':
        success = rollback_manager.rollback_to_task(args.task_id, keep_changes=args.keep)
        if success:
            print(f"Successfully rolled back to task {args.task_id}")
        else:
            print(f"Failed to rollback to task {args.task_id}")
            exit(1)

    elif args.rollback_command == 'prev':
        success = rollback_manager.rollback_to_previous(keep_changes=args.keep)
        if success:
            print("Successfully rolled back to previous task")
        else:
            print("Failed to rollback to previous task")
            exit(1)

    else:
        print("Please specify a rollback command: list, to, or prev")
        print("Use 'autonomous-coding rollback --help' for more information")
        exit(1)


def handle_configs_command():
    """Handle configs subcommand."""
    print("Available configurations:")
    print("\nBuilt-in configurations:")
    for name in ConfigRegistry.list_configs():
        print(f"  - {name}")

    print("\nConfig files (in ./configs/ directory):")
    configs_dir = Path(__file__).parent / "configs"
    if configs_dir.exists():
        for config_file in configs_dir.glob("*.yaml"):
            config_name = config_file.stem
            print(f"  - {config_name} (from {config_file.name})")
    else:
        print("  (No config files found)")

    print("\nUsage:")
    print("  autonomous-coding --config <name> \"your requirement\" -w ./project")


def _keychain_tool_path() -> str:
    """Return the keychain tool's path, as the provider defines it.

    Read through the provider rather than written out here. The path is
    what decides whether this platform can serve a keychain-backed
    secret, so a second copy in this file would be a second thing that
    can be right while the read itself runs a different one — and the
    command that reports on the read would then be reporting on
    something else. Reaching the provider's constant also means a test
    that redirects the tool redirects this command with it, which is
    what lets the "no such platform" case be exercised at all on a
    machine that has the tool.

    It is a private name and that is a real cost. The alternative is a
    second literal, and a literal is a fact about one machine's
    filesystem written where a second machine's read will not honour
    it — which is the worse of the two to explain later.
    """
    return credentials._SECURITY_BIN


def _keychain_tool_available() -> bool:
    """Return whether the keychain tool is here and runnable.

    Both halves matter and neither implies the other: a path that
    exists but cannot be executed is a platform that cannot serve the
    secret, and reporting that as available would send the operator
    looking for a configuration problem that is not there.
    """
    path = _keychain_tool_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _unsupported_keychain_note() -> Optional[str]:
    """Return why this platform cannot deliver a keychain, or None.

    The condition is the *tool's absence*, and nothing else. The read
    path cannot say why it found nothing — a missing tool and a missing
    item are both "no value" to it, correctly, since its caller asked
    whether a transport is configured. An operator at a terminal is
    asking a different question, and this is where it gets an answer.

    The tempting condition is ``credentials.keychain_disabled()``, and
    it is wrong in a way that only shows up on the platform it is
    meant to describe. That switch is true in two unrelated situations:
    nobody asked for a keychain, and this platform cannot have one. On
    every machine of the second kind — every Linux runner, every
    container — the platform is what turned the keychain off, so the
    switch is true by the time anything gets to ask, and a note gated
    on it can never fire there. The command would print
    ``source=os.environ`` on every line and exit 0 on the one machine
    whose deployment is guaranteed never to be able to use a keychain:
    a green answer to a question about a facility the box does not
    have. A platform property has to be asked as one.

    So the note is about the platform, and the switch is left out of it
    entirely. That keeps the two situations apart: a box that has the
    tool and was not asked to use it is a working environment-only
    deployment and says nothing (see
    ``test_present_tool_is_not_a_diagnosis``), and a box without the
    tool is told the sentence on every run, because that is true on
    every run.
    """
    if _keychain_tool_available():
        return None
    return (
        "keychain: not supported on this platform "
        "({} is missing or not executable)".format(_keychain_tool_path())
    )


def _unquote(value: str) -> str:
    """Return ``value`` with one layer of matching quotes removed.

    A quoted value is how a credential containing a space, a ``#`` or a
    trailing newline is written down, and the quotes are the writer's
    syntax rather than part of the secret. Unquoting is done here rather
    than by a dotenv library so that the only transformation applied to
    a value before it is compared is one this file documents.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _load_baseline(path: str) -> Optional[Dict[str, str]]:
    """Return ``{key: value}`` read from the ``KEY=value`` file at ``path``.

    ``None`` means the file could not be read at all, which is a
    different failure from any one key being absent from it: the first
    is a file the operator got wrong, the second is a question about
    what the file carries. Reporting them as one thing would send
    someone looking for a credential that was never misconfigured.

    The grammar is deliberately small, and the three limits are the
    reason:

    * **No interpolation.** A value is compared as it is written, not
      as whatever the shell that wrote it would have produced. A
      baseline that expands ``$OTHER_SECRET`` is a baseline whose
      answer depends on a file this command never read.
    * **A line with no ``=`` is skipped.** In this grammar a bare key
      means "inherit from the environment" — and a baseline that
      inherited the environment would compare the value against itself,
      which is a comparison that cannot fail and means nothing.
    * **Bytes that are not valid UTF-8 survive.** A secret is bytes,
      and ``credentials._decode_payload`` already hands back whatever
      the keychain held. A baseline that cannot hold the credential it
      is meant to check reports a mismatch that is not one.
    * **Surrounding whitespace is not part of the value**, on either
      side of the ``=``. That is the reading ``.env`` itself gets, and a
      value compared against a spelling the file's own loader would have
      trimmed is a comparison against a value no process on this
      machine holds.

    Keyed by the spec table's own ``fallback_env_key`` — the spelling
    ``.env`` uses, and the one variable that holds a *value* rather than
    an index. The logical name is not also accepted: two spellings per
    secret is a second grammar, and a file carrying both for one key
    would have to pick one silently.
    """
    try:
        # Read as bytes and decode here rather than through
        # ``read_text``: the same reason ``credentials`` does it, and a
        # baseline is a place a real credential is written down.
        raw = Path(path).read_bytes()
    except OSError:
        # Not one re-raise and not one message carrying the path. The
        # caller prints the note, after the listing it belongs under,
        # and the note is a constant so that a path an operator did not
        # choose — out of a job's argv, a shell history, a CI log —
        # cannot be written by this command.
        return None

    values: Dict[str, str] = {}
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = _unquote(value.strip())
    return values


def _compare_against_baseline(name: str, baseline: Dict[str, str]) -> str:
    """Return the comparison verdict for one secret.

    The comparison happens here, in the command, and not in
    ``credentials`` — the provider is not told a baseline exists. That
    is not tidiness. A provider that took a baseline path would owe its
    every caller an answer to "what is this file for", and the only
    caller that would have a use for the answer is this one.

    ``read_secret`` is called at most once per secret and only after the
    baseline has been asked, so a key the file does not carry costs no
    lookup the command was not going to make anyway. The value it
    returns is a memo hit on the read ``secret_source`` already made
    this loop, which is what keeps a comparison from doubling the cost
    the provider memoises in order not to pay it twice.

    A live secret with no value is 不相等 rather than a fourth state:
    the baseline carries something and this machine does not, which is
    a deployment that will not send as configured, and the source
    column beside it already says ``missing`` — that is where the
    operator is sent to fix it.
    """
    spec = credentials.SECRET_SPECS.get(name)
    if spec is None:
        return COMPARE_BASELINE_ABSENT

    expected = baseline.get(spec.fallback_env_key)
    if expected is None:
        return COMPARE_BASELINE_ABSENT

    actual = credentials.read_secret(name)
    if actual is None:
        return COMPARE_DIFFERENT

    return COMPARE_EQUAL if actual == expected else COMPARE_DIFFERENT


def _print_rows(rows) -> None:
    """Print aligned rows, every column padded but the last.

    The padding is what lets the answer be read down the side: this is
    printed by a person looking at a terminal, and a column that has to
    be re-derived from a varying offset is one they will misread. Every
    column but the last is padded to the widest cell in it plus a gap,
    and the padding *is* the gap — a separator added on top of it would
    make every column's offset a function of the row above it. The last
    column is not padded because nothing follows it, and one of the
    verdicts is written in characters that are wider than they are long
    — a padded trailing column would be a column whose alignment
    depended on the terminal's font.
    """
    column_count = len(rows[0]) if rows else 0
    widths = [
        max([len(row[column]) for row in rows], default=0) + 2
        for column in range(column_count)
    ]
    for row in rows:
        print("".join(
            cell if index == column_count - 1 else cell.ljust(widths[index])
            for index, cell in enumerate(row)
        ).rstrip())


def cmd_secrets_verify(verify_against: Optional[str] = None) -> int:
    """Print where each provider secret is read from; return an exit code.

    One line per secret, and the line carries the *source* and nothing
    else. The value is never written and ``read_secret`` is never
    called: this is the command an operator runs to find out whether a
    deployment is set up, and a command that answers that question by
    writing the credential to a terminal, a CI log, or a screen share
    is not an answer to any question worth asking. The source label is
    the provider's own, so a deployment cannot read as configured here
    and unconfigured at the far end of a notification send.

    ``verify_against`` adds a third column: the file named there is a
    ``KEY=value`` baseline, and each secret is compared with the value
    it holds there. Both values are in this process to be compared and
    the column is one of three words, so the flag adds a finding and
    not a disclosure — the whole acceptance item is "does this machine
    hold the same credential the source side holds", and a command that
    answers it by writing the credential has answered a different
    question. A baseline file that cannot be read leaves the ordinary
    two columns in place and says so, because the comparison is an
    addition to this command rather than a precondition for it.

    The exit code is 0 when every secret resolved, the platform carries
    a keychain tool, and — with a baseline — every secret matched it;
    1 otherwise. Every one of those failures is the same failure to
    whoever is reading it — this deployment will not send as configured,
    or has not been shown to — and a command that reported only the
    first would let the others pass as health. A platform with no
    keychain tool is a nonzero exit on every run, whether or not
    anybody asked for a keychain: the fact is about the machine, and it
    is the fact an operator running this on Linux or in a container came
    for. A comparison that could not finish is a nonzero exit for the
    same reason, since a check that did not run cannot have passed.
    """
    names = list(credentials.SECRET_SPECS)

    if verify_against is None:
        _print_rows([
            (name, "source={}".format(credentials.secret_source(name)))
            for name in names
        ])
        _print_missing_reasons(names)
        note = _unsupported_keychain_note()
        if note is not None:
            print(note)
            return 1
        return 0 if all(credentials.secret_available(name) for name in names) else 1

    baseline = _load_baseline(verify_against)
    if baseline is None:
        # The listing still goes out, and the note goes under it: the
        # operator who named a file that is not there still wants to
        # know where their secrets come from, and a sentence above the
        # table reads as belonging to the table.
        _print_rows([
            (name, "source={}".format(credentials.secret_source(name)))
            for name in names
        ])
        _print_missing_reasons(names)
        print(BASELINE_UNREADABLE_NOTE)
        return 1

    # Each verdict is computed once and used twice — printed, then
    # folded into the exit code. Asking a second time would be a memo
    # hit rather than a second lookup, so it would not show up in the
    # fetch count, and a command that has to be counted to be believed
    # should not contain work it does not need.
    verdicts = {
        name: _compare_against_baseline(name, baseline) for name in names
    }
    _print_rows([
        (name, credentials.secret_source(name), verdicts[name]) for name in names
    ])
    _print_missing_reasons(names)

    note = _unsupported_keychain_note()
    if note is not None:
        print(note)
        return 1

    return 0 if all(
        verdict == COMPARE_EQUAL for verdict in verdicts.values()
    ) else 1


def _print_missing_reasons(names: Iterable[str]) -> None:
    """Print why each unresolved secret has no value.

    ``source=missing`` is one word standing for at least four different
    problems — the keychain is switched off, the index is not set, the
    keychain is locked, or the item was never filed — and the operator
    who reads it has no way to tell which one they have. They fix those
    in four different places, and the locked case additionally cost up to
    a minute of blocking before it could report anything at all.

    Only unresolved secrets produce a line. A resolved one has nothing to
    explain, and a command that narrates the healthy rows teaches the
    reader to skip the output that matters.
    """
    reasons = [
        reason
        for reason in (
            credentials.diagnose_secret(name) for name in names
        )
        if reason is not None
    ]
    if not reasons:
        return
    print()
    for reason in reasons:
        print(reason)


def cmd_secrets_show() -> int:
    """List the keychain metadata each provider secret is filed under.

    The lookup path is not entered, and that is the design rather than
    an omission. A secret is located by the account index beside it and
    by nothing else; this command already has every row that lookup
    would use, so the only thing running it would add is the value —
    pulled into this process to answer a question about naming.

    So the rows come from the spec table and from the environment, and
    the environment is read for the index alone. An index is not a
    secret: it is the key that names the item, which is the reason it
    is allowed to sit in the environment at all, and it is printed here
    because a listing that hid it would answer none of the question it
    was asked. The plaintext fallback is not consulted, not even to
    report whether it is set — ``verify`` is the command that reports
    whether a source exists, and two commands answering overlapping
    questions is how they come to disagree.

    Always 0. Nothing here is configured, nothing is read, and there is
    no deployment state this listing could be wrong about.
    """
    rows = [
        (name, spec.account_env_key, os.environ.get(spec.account_env_key) or INDEX_UNSET)
        for name, spec in credentials.SECRET_SPECS.items()
    ]
    widths = [
        max([len(heading)] + [len(row[column]) for row in rows])
        for column, heading in enumerate(SHOW_HEADERS)
    ]

    def _line(cells):
        return "  ".join(
            cell.ljust(widths[index]) for index, cell in enumerate(cells)
        ).rstrip()

    print(_line(SHOW_HEADERS))
    for row in rows:
        print(_line(row))

    print(
        "\nKeys only. The service column names where a secret is filed "
        "and is not a lookup key;\nitems are found by their account "
        "index, and no keychain item is read here."
    )
    return 0


if __name__ == "__main__":
    main()