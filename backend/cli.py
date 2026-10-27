"""
Command Line Interface
====================

CLI for autonomous coding skill.
"""

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from agent import autonomous_coding
import credentials
from rollback_manager import RollbackManager
from config_registry import ConfigRegistry
from config_loader import load_config_by_name
from coding_tool import stop_all_live_dispatches
from execution_logger import get_logger
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE

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
#:
#: The verification branch's two names are in here for the same reason
#: ``secrets`` is: a leading token that is not listed would be read as
#: the requirement, and ``autonomous-coding verification verify p1``
#: would start a development run called "verification".
SUBCOMMANDS = (
    "rollback",
    "configs",
    "secrets",
    "verification-plan",
    "verification",
)


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


def build_parser(argv=None) -> argparse.ArgumentParser:
    """Build the CLI's parser.

    Kept apart from :func:`main` so the grammar can be inspected and
    exercised on its own: the shape of this parser — which top-level
    options exist, which subcommand owns which positional — is the
    CLI's public contract, and a contract that can only be reached
    through ``main()`` is a contract that can only be tested by running
    a command.

    ``argv`` is the argument list the leading-subcommand decision is
    made from; it defaults to the process's own. The decision has to be
    made *here* rather than in the subparsers, because it decides
    whether the ``requirement`` positional exists at all (see below),
    and that question is asked before any subparser is created.
    """
    if argv is None:
        argv = sys.argv[1:]

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
  autonomous-coding verification-plan generate p1
  autonomous-coding verification-plan review p1 --item 3 --status skip --note "no service"
  autonomous-coding verification verify p1
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
    leading_subcommand = _leading_subcommand(argv)
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

    # Verification branch: the plan document (任务 17-21)
    #
    # The vocabulary is closed — ``generate``, ``get``, ``items``,
    # ``review``, ``refine`` — and a name outside it is refused by
    # argparse with a nonzero exit. A subcommand that fell through to
    # the autonomous path instead would be read as a requirement, and
    # the operator would be starting a development run by mistyping a
    # verification one.
    verification_plan_parser = subparsers.add_parser(
        'verification-plan',
        help='Verification branch: the verification plan document')
    verification_plan_subparsers = verification_plan_parser.add_subparsers(
        dest='verification_plan_command', help='Verification plan commands')

    vp_generate_parser = verification_plan_subparsers.add_parser(
        'generate', help='Generate the verification plan document')
    vp_generate_parser.add_argument('plan_id', help='Plan id')

    vp_get_parser = verification_plan_subparsers.add_parser(
        'get', help='Read the verification plan document')
    vp_get_parser.add_argument('plan_id', help='Plan id')

    vp_items_parser = verification_plan_subparsers.add_parser(
        'items', help='List the review items of the verification plan')
    vp_items_parser.add_argument('plan_id', help='Plan id')

    vp_review_parser = verification_plan_subparsers.add_parser(
        'review', help='Record one review action on one item')
    vp_review_parser.add_argument('plan_id', help='Plan id')
    vp_review_parser.add_argument(
        '--item', type=int, required=True, dest='item',
        help='Item index, numbered as the API numbers them (from 0)')
    # The flag is ``--status`` because that is what the operator is
    # doing; the body's field is ``action`` because that is what the
    # endpoint reads. ``--action`` is accepted as a spelling of the
    # same thing, because the endpoint's own name for it is the one an
    # operator reading the API docs will reach for.
    vp_review_parser.add_argument(
        '--status', '--action', dest='status', required=True,
        help='Review action for this item (the server decides which are legal)')
    vp_review_parser.add_argument(
        '--note', default='',
        help='Reason for the action; the server requires one for a skip')
    vp_review_parser.add_argument(
        '--doc-sha256', dest='doc_sha256', default=None,
        help='Hash of the document version that was read (the server '
             'refuses an action against a document that changed since)')

    vp_refine_parser = verification_plan_subparsers.add_parser(
        'refine', help='Rewrite the verification plan document from feedback')
    vp_refine_parser.add_argument('plan_id', help='Plan id')
    vp_refine_parser.add_argument(
        '--feedback', required=True,
        help='What to change in the plan document')

    # Verification branch: the run itself (任务 22)
    verification_parser = subparsers.add_parser(
        'verification', help='Verification branch: run the verification')
    verification_subparsers = verification_parser.add_subparsers(
        dest='verification_command', help='Verification commands')

    verification_verify_parser = verification_subparsers.add_parser(
        'verify', help='Start the verification run for a plan')
    verification_verify_parser.add_argument('plan_id', help='Plan id')

    return parser


# ---------------------------------------------------------------------------
# Stopping: what the executor does when the server says "stop"
# ---------------------------------------------------------------------------
#
# The server stops an execution by sending SIGTERM to the executor's
# process group.  Before this existed the executor had **no signal
# handling at all**: the SIGTERM killed it on the spot, the ``finally``
# blocks and ``cleanup()`` never ran, and the ``claude`` child — which
# is spawned with ``start_new_session=True`` and therefore sits in its
# *own* process group, out of reach of the signal the server sent —
# stayed behind as an orphan, still burning tokens on a task nobody was
# waiting for any more.
#
# So the executor has to stop its own children before it goes.  The
# cooperative sequence that does it lives in ``coding_tool``
# (:func:`coding_tool.stop_all_live_dispatches`): it closes the child's
# stdout, escalates to SIGTERM and then to SIGKILL of the child's
# process group, splitting its budget 0.35 / 0.25 / 0.10 — 0.7 of the
# budget in total.
#
# The budget is the server's own grace window
# (``PDT_STOP_GRACE_SECONDS``, default 10s), inherited through the
# environment: the server spawns the executor with
# ``env={**os.environ, ...}``.  The sequence has to finish *inside*
# that window, because a sequence that runs past it gets its process
# group SIGKILLed by the server — and that hard kill is exactly the
# outcome (a forced stop instead of a clean one) this mechanism exists
# to avoid.
#
# This is a deliberate deviation from the upstream wording ("wait the
# grace, then SIGTERM, then wait 5 more seconds, then SIGKILL"): taken
# literally, that sequence cannot fit inside the window it is measured
# against.  The order is kept — the child gets a chance to leave on
# its own terms first — and the total is what shrinks.
#
# The handler has a hard limit of its own (0.9 of the budget): whatever
# the sequence does, the executor is gone by then.  A stop path that can
# hang is a stop path that leaves the orphan behind.

#: The env var carrying the server's grace window into the executor.
_STOP_GRACE_ENV = "PDT_STOP_GRACE_SECONDS"

#: The budget used when the environment says nothing usable.
_STOP_GRACE_DEFAULT_SECONDS = 10.0

#: Bounds for the budget.  The floor keeps each stage of the sequence
#: long enough to mean something; the ceiling keeps a fat-fingered
#: configuration from parking the stop path for a minute.
_STOP_GRACE_MIN_SECONDS = 2.0
_STOP_GRACE_MAX_SECONDS = 60.0

#: How much of the budget the handler waits for the stop sequence.
#: Below 1.0, so the executor is always gone before the server's own
#: window closes — and therefore before the server's SIGKILL.
_STOP_SEQUENCE_WAIT_FRACTION = 0.9

#: The exit code of a SIGTERM-stopped executor: 128 + SIGTERM(15).
_STOP_EXIT_CODE = 143

#: Whether the handler is installed.  Installing twice must not stack a
#: second handler on top of the first — the second would call the first,
#: and the stop sequence would run twice over the same children.
_stop_handler_installed = False

#: Whether a stop sequence is already running.  A second SIGTERM during
#: the sequence must not start another one.
_stop_in_progress = False

#: The logger for this module's own messages.  Named apart from the
#: ``logger`` local in :func:`main` (which is the per-plan execution
#: logger) so the two cannot be confused for one another.
_stop_logger = logging.getLogger(__name__)


def _stop_grace_seconds() -> float:
    """Return the stop budget in seconds: how long the whole stop may take.

    Read from the environment on every call rather than at import time,
    so the executor sees the value the server was configured with and a
    test can change it without reimporting the module.

    A missing, empty, non-numeric or non-positive value falls back to
    the default rather than to something smaller: "don't wait at all" is
    the opposite of what the cooperative stop is for, and a zero budget
    would skip the grace the children need to leave on their own.  Every
    usable value is clamped into ``[2, 60]`` seconds — the same bounds
    the sequence itself applies, so a fat-fingered configuration cannot
    park the executor in a stop path for a minute.
    """
    raw = os.environ.get(_STOP_GRACE_ENV)
    if raw is None or not raw.strip():
        return _STOP_GRACE_DEFAULT_SECONDS
    try:
        budget = float(raw)
    except (TypeError, ValueError):
        return _STOP_GRACE_DEFAULT_SECONDS
    # NaN fails every comparison, so it has to be excluded explicitly.
    if not math.isfinite(budget) or budget <= 0:
        return _STOP_GRACE_DEFAULT_SECONDS
    return min(max(budget, _STOP_GRACE_MIN_SECONDS), _STOP_GRACE_MAX_SECONDS)


def _exit_now(code: int) -> None:
    """Leave the process right now, with ``code``.

    ``os._exit`` and not ``sys.exit``: unwinding the stack would run
    the ``finally`` blocks of whatever the executor was in the middle
    of, and one of those may block on a child that is already gone.
    The process is leaving because it was told to stop; there is
    nothing left to unwind, and a stop that hangs is a stop that
    leaves the orphan behind.
    """
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a broken stream must not stop the exit
        pass
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — ditto
        pass
    os._exit(code)


def _stop_live_dispatches(budget: float) -> None:
    """Run the cooperative stop sequence.  Never raises.

    This runs on the stop thread, where an escaping exception would be
    printed by the threading machinery and the executor would carry on
    as if nothing had happened.  It has to be logged and swallowed: the
    handler's job is to make the executor go away, and children it
    failed to stop are the server's business to reap.
    """
    try:
        stop_all_live_dispatches(budget)
    except Exception:  # noqa: BLE001 — the exit below must still happen
        _stop_logger.warning(
            "[stop] the cooperative stop sequence failed; exiting anyway",
            exc_info=True,
        )


def _on_sigterm(signum, frame) -> None:
    """Stop the children, then leave.  Installed as the SIGTERM handler.

    The sequence runs on its own thread and the handler waits for it
    with a hard limit (0.9 of the budget): the sequence's own budget
    covers the children's exit, but a handler that waited without a
    limit would wait forever if the sequence wedged.

    A second SIGTERM means "stop waiting".  The sequence is already
    running, and running it again would only keep the executor around
    longer — the second signal exits at once.
    """
    global _stop_in_progress

    if _stop_in_progress:
        _exit_now(_STOP_EXIT_CODE)
        return

    _stop_in_progress = True

    try:
        budget = _stop_grace_seconds()
        worker = threading.Thread(
            target=_stop_live_dispatches,
            args=(budget,),
            name="pdt-stop-sequence",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=budget * _STOP_SEQUENCE_WAIT_FRACTION)
    except Exception:  # noqa: BLE001 — the executor must still leave
        _stop_logger.warning(
            "[stop] could not run the stop sequence; exiting anyway",
            exc_info=True,
        )

    _exit_now(_STOP_EXIT_CODE)


def install_stop_handler() -> None:
    """Install the SIGTERM handler.  Idempotent.

    Installing twice would stack a second handler on top of the first,
    and the sequence would run twice over the same children.  Only the
    executor's own run installs this: the subcommands (``rollback``,
    ``secrets``, ``verification``, ...) return long before anything
    could be signalled.
    """
    global _stop_handler_installed

    if _stop_handler_installed:
        return

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError, RuntimeError):
        # Not the main thread, or the platform refuses.  A handler that
        # cannot be installed is no reason to refuse to run.
        _stop_logger.warning(
            "[stop] could not install the SIGTERM handler; continuing",
            exc_info=True,
        )
        return

    _stop_handler_installed = True


def main():
    """Main entry point for CLI."""
    from env_config import load_env

    # Imported here rather than at the top of the module: the harness
    # layer is not needed to parse an argument list, and a CLI that
    # only ever prints a table of secrets should not have to load it.
    from harness.runner import HarnessInterrupted

    load_env()

    # A workspace a run that was killed left holding the adapter's MCP
    # configuration is put back before this process starts anything of
    # its own. The run that wrote the file is gone, and nothing else
    # would ever give the user their file back.
    try:
        from harness.mcode import recover_pending_mcp_restores
        recover_pending_mcp_restores()
    except Exception:
        logging.getLogger(__name__).exception(
            "the pending MCP restores could not be replayed; continuing"
        )

    parser = build_parser()
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

    # Handle the verification branch's plan-document commands
    if args.command == 'verification-plan':
        sys.exit(handle_verification_plan_command(args))

    # Handle the verification branch's run commands
    if args.command == 'verification':
        sys.exit(handle_verification_command(args))

    # Handle main autonomous coding
    workspace_dir = args.workspace
    if args.dir:
        workspace_dir = args.dir

    logger = get_logger()

    tasks_file = Path(args.tasks_file) if args.tasks_file else None

    # Install the stop handler *before* the run starts.  The executor is
    # the process the server signals, and a handler installed after the
    # first task had begun would miss the very signal it exists to
    # answer — leaving the ``claude`` child running with nobody left to
    # stop it.
    install_stop_handler()

    try:
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
    except HarnessInterrupted as exc:
        # A run that a signal cut short is not a crash: it is the run
        # being stopped. The runner turned the signal into an
        # exception so that everything the run prepared — the file the
        # adapter wrote, the settings file with the credential in it,
        # the child itself — was given back before the exception got
        # here. The exit code is the one the shell reports for a
        # process that was killed by that signal, so a caller can tell
        # "the run was stopped" from "the run failed".
        if logger is not None:
            logger.warning(
                "harness_interrupted",
                f"the run was interrupted by signal {exc.signum}; "
                "the workspace was put back before leaving",
                data={"signum": exc.signum},
            )
        else:
            # No plan, so no log to write to. The operator still gets
            # told: a stop is not a crash, and it is not a silence.
            print(
                f"the run was interrupted by signal {exc.signum}; "
                "the workspace was put back before leaving",
                file=sys.stderr,
            )
        sys.exit(128 + exc.signum)


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


def _verdict(name: str, actual: Optional[str], baseline: Dict[str, str]) -> str:
    """Return the comparison verdict for one secret, already read.

    The comparison happens here, in the command, and not in
    ``credentials`` — the provider is not told a baseline exists. That
    is not tidiness. A provider that took a baseline path would owe its
    every caller an answer to "what is this file for", and the only
    caller that would have a use for the answer is this one.

    ``actual`` is handed in rather than read here, and that is load
    bearing. The value compared has to be the value the ``source``
    column beside it was read from — see :func:`_direct_read` — and a
    verdict that fetched its own would be free to compare against a
    source the row does not name.

    The baseline is asked before anything else, so a key the file does
    not carry costs no lookup the command was not going to make anyway.

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

    if actual is None:
        return COMPARE_DIFFERENT

    return COMPARE_EQUAL if actual == expected else COMPARE_DIFFERENT


def _direct_read(name: str) -> Tuple[str, Optional[str]]:
    """Return ``(source, value)`` for a **direct** read of ``name``.

    Not ``credentials.secret_source``. That one answers for a process
    launched by ``backend.secret_launcher``: it reports
    ``"inherited_fd"``, ``"os.environ"`` or ``"missing"``, and in *this*
    process — which no launcher started, so no descriptor was handed
    down — it would report ``"missing"`` for everything and tell the
    operator nothing they could act on.

    The question ``secrets`` exists to answer is "does this machine hold
    the credential", so it asks the machine. That is a different
    question from "will the server resolve it", and the two answers
    differ in exactly the way an operator needs to see: a keychain that
    is configured here can still miss the server if the launcher failed
    to publish.

    Both halves come out of one call, which is the point of the pair
    rather than a small helper per question. The source column and the
    comparison are two readings of the same fact, and the defect they
    can fall into is disagreeing with each other — a row reading
    ``source=keychain`` beside a verdict computed from the environment.
    Handing both out together makes that disagreement unrepresentable,
    and it keeps the command at one read per secret: a second helper
    would be a second ``security`` process, which is the cost moving
    the server's read into a launcher exists to stop paying.

    On a platform with no keychain the environment is the only source
    there is, and the ordinary labels are the honest ones — the same
    answer the server gives there.
    """
    if credentials.keychain_disabled():
        return credentials.secret_source(name), credentials.read_secret(name)

    value = credentials.read_secret_from_keychain(name)
    if value is None:
        return credentials.SOURCE_MISSING, None
    return credentials.SOURCE_KEYCHAIN, value


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

    **This reads the keychain directly, and says so.** Since the server
    moved to a launcher that publishes the secrets over pipes
    (:mod:`backend.secret_launcher`), ``credentials.secret_source``
    answers a question about *that process* — and in this one, which no
    launcher started, it would answer ``"missing"`` for everything. The
    labels printed here are therefore about **this machine's keychain**
    (see :func:`_direct_read`), which is the thing an operator can
    fix. A row reading ``source=keychain`` means the keychain is
    configured; it does not by itself prove the server received it, and
    the server's own startup log is where that second fact appears.

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
        # Read once per name, and the value is discarded: each read is a
        # `security` process, and asking twice would buy a second one
        # rather than a memo hit — the memo belongs to the server's
        # resolver, which this command deliberately does not go through.
        sources = {name: _direct_read(name)[0] for name in names}
        _print_rows([
            (name, "source={}".format(sources[name]))
            for name in names
        ])
        _print_missing_reasons(sources)
        note = _unsupported_keychain_note()
        if note is not None:
            print(note)
            return 1
        return 0 if all(
            source != credentials.SOURCE_MISSING for source in sources.values()
        ) else 1

    baseline = _load_baseline(verify_against)
    if baseline is None:
        # The listing still goes out, and the note goes under it: the
        # operator who named a file that is not there still wants to
        # know where their secrets come from, and a sentence above the
        # table reads as belonging to the table.
        sources = {name: _direct_read(name)[0] for name in names}
        _print_rows([
            (name, "source={}".format(sources[name]))
            for name in names
        ])
        _print_missing_reasons(sources)
        print(BASELINE_UNREADABLE_NOTE)
        return 1

    # One read per name, and both columns come out of it. Reading the
    # source and the verdict separately would be two `security` processes
    # where one will do, and — worse — would let the verdict be computed
    # from a value the source column does not name. The comparison is the
    # expensive path, so a run with a baseline must fetch exactly as many
    # values as a run without one.
    readings = {name: _direct_read(name) for name in names}
    verdicts = {
        name: _verdict(name, readings[name][1], baseline) for name in names
    }
    _print_rows([
        (name, readings[name][0], verdicts[name]) for name in names
    ])
    _print_missing_reasons({name: source for name, (source, _value) in readings.items()})

    note = _unsupported_keychain_note()
    if note is not None:
        print(note)
        return 1

    return 0 if all(
        verdict == COMPARE_EQUAL for verdict in verdicts.values()
    ) else 1


def _print_missing_reasons(sources: Dict[str, str]) -> None:
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

    This takes the caller's own readings rather than the spec table, and
    that is load bearing rather than a convenience.
    :func:`credentials.diagnose_secret` re-derives a resolution for the
    name it is handed, and on a machine with a keychain its last branch
    reads the keychain to rule out the item being filed. Asked about a
    secret this command has just resolved, it would pay a second
    ``security`` process to re-answer a question the table above has
    already answered — and the answer would have to agree with the row,
    which is a property two independent reads are free to lose. Handing
    over the readings is what keeps the table and the notes below it
    derived from one read.
    """
    unresolved = [
        name
        for name, source in sources.items()
        if source == credentials.SOURCE_MISSING
    ]
    reasons = [
        reason
        for reason in (
            credentials.diagnose_secret(name) for name in unresolved
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


# ---------------------------------------------------------------------------
# The verification branch: a thin wrapper over the API
# ---------------------------------------------------------------------------
#
# The verification branch is entered through HTTP endpoints — the plan
# document's own endpoints (任务 17-21) and the run's start endpoint
# (任务 22). The CLI is the E2E-1 entry point into the same branch, and
# it is a **wrapper**: it parses arguments, sends the request, and prints
# what came back.
#
# It holds no rule of its own, and that is the load-bearing decision
# rather than a shorthand. The stage gate, the item vocabulary, the
# baseline freeze and the doc-hash race all live on the server, where
# the API path and the CLI path reach the same code. A second copy of
# any of them here would be a second thing that can be right while the
# server is wrong — and it would be right *only* in the CLI, which is
# the one path nobody tests against the other. So the CLI sends the
# request and reports the answer, and where it does decide something
# (which URL, which body field) the decision is the spelling the
# endpoint already uses.

#: The server this CLI talks to when nothing says otherwise. Loopback,
#: because the server binds loopback by default
#: (:func:`backend.config_paths.resolve_server_host`) and a web UI with
#: no authentication is not something to reach from another machine by
#: accident.
DEFAULT_API_HOST = "127.0.0.1"

#: The port the server binds with when nothing says otherwise. The same
#: default the server itself uses (``PDT_PORT`` or 8000).
DEFAULT_API_PORT = "8000"

#: How long one API call may take.
#:
#: Minutes, not seconds, and the reason is the plan-document endpoints:
#: they call a model before they answer, so a timeout shorter than the
#: server's own would turn a slow generation into a misleading "cannot
#: reach the server" — the one failure an operator cannot act on.
API_TIMEOUT_SECONDS = 900


class ApiCommandError(Exception):
    """An API call that did not succeed.

    Attributes:
        status_code: The HTTP status, or ``None`` when the call never
            reached a server (a refused connection, a timeout).
        error_code: The server's machine-readable code — ``plan_not_found``,
            ``item_not_found``, ``unknown_action`` — when it sent one.
        detail: The server's human-readable detail, when it sent one.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        detail: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


def _api_base_url() -> str:
    """Return the base URL of the PDT server this CLI talks to.

    ``PDT_API_BASE_URL`` wins outright — an operator pointing the CLI at
    a server on another box, or at a tunnel, must not have to also get
    the port right. Otherwise the port comes from ``PDT_PORT``, the same
    variable the server binds with, so a deployment that moved its
    server to another port has one variable to set rather than two.

    The host is loopback and is not read from ``PDT_HOST``: that
    variable says what the server *binds*, and ``0.0.0.0`` — the
    documented way to expose the server to a LAN — is not an address a
    client can connect to.
    """
    explicit = (os.environ.get("PDT_API_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("PDT_PORT") or "").strip() or DEFAULT_API_PORT
    return "http://{}:{}".format(DEFAULT_API_HOST, port)


def _decode_api_body(raw: bytes) -> Dict:
    """Return the JSON object in ``raw``; an empty dict if there is none.

    A body that is not JSON is not an error here: the CLI's job is to
    forward the server's answer, and a body it cannot parse is a body
    it forwards as an empty object rather than a crash. The status code
    is what decides success, and it was decided before this was called.
    """
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {"detail": payload}


def _api_error_from_response(status: int, body: Dict) -> ApiCommandError:
    """Return the error a failed response stands for.

    FastAPI wraps an ``HTTPException``'s detail in ``{"detail": ...}``,
    and the endpoints raise with a dict carrying ``error`` (the
    machine-readable code) and ``detail`` (the sentence). Both are
    lifted out here so the terminal shows the same code the API's own
    documentation names — an operator grepping a CI log for
    ``plan_not_found`` has to find it.
    """
    inner = body.get("detail")
    if not isinstance(inner, dict):
        inner = body
    error_code = inner.get("error")
    text = inner.get("detail")
    if not isinstance(text, str):
        text = None
    label = error_code or "http_{}".format(status)
    message = "{} (HTTP {})".format(label, status)
    if text:
        message = "{}: {}".format(message, text)
    return ApiCommandError(
        message, status_code=status, error_code=error_code, detail=text
    )


def _api_request(method: str, path: str, payload=None) -> Dict:
    """Send one API call and return the JSON object it answered with.

    Args:
        method: The HTTP method.
        path: The path, beginning with ``/``.
        payload: The JSON body, or ``None`` for a request without one.

    Returns:
        The server's JSON body. An empty dict when the body was empty
        or was not JSON — see :func:`_decode_api_body`.

    Raises:
        ApiCommandError: The server answered with a 4xx or a 5xx, or
            the server could not be reached at all. The two are
            different failures and are told apart by
            :attr:`ApiCommandError.status_code`: one is the server
            refusing, the other is nobody answering.
    """
    url = "{}{}".format(_api_base_url(), path)
    body = None
    # The server's request guard refuses every ``/api/*`` call that does
    # not carry its header — a browser page from another origin cannot
    # set one without a preflight the server never grants. This command
    # is a local script, which is exactly what the guard admits, so it
    # sends the header. The names come from the guard itself rather
    # than from a second copy here: a CLI that spelled the header
    # differently from the server would be refused by every call, and
    # the failure would read as "the API is down".
    headers = {
        "Accept": "application/json",
        REQUEST_HEADER: REQUEST_HEADER_VALUE,
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url, data=body, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(
            request, timeout=API_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
            status = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 — a body we cannot read is a body we do not have
            raw = b""
        raise _api_error_from_response(exc.code, _decode_api_body(raw))
    except urllib.error.URLError as exc:
        raise ApiCommandError(
            "cannot reach the PDT server at {}: {}".format(url, exc.reason)
        )
    except OSError as exc:
        # A timeout, a reset connection, a DNS failure that did not
        # arrive as a URLError. None of them is the server's answer, and
        # none of them may be reported as one.
        raise ApiCommandError(
            "cannot reach the PDT server at {}: {}".format(url, exc)
        )

    if status is not None and status >= 400:
        raise _api_error_from_response(status, _decode_api_body(raw))
    return _decode_api_body(raw)


def _emit_result(command: str, plan_id: str, payload: Dict) -> None:
    """Print one command's answer as a single JSON object.

    The command's own name and the plan id go in beside the server's
    answer, so a script driving several plans in a row can tell whose
    answer it is holding — and so a human reading a terminal can tell
    which of two similar lines is the one they asked for. The server's
    fields are forwarded as they came: the CLI does not know what
    ``current_phase`` may be, and must not pretend to.
    """
    body = {"command": command, "plan_id": plan_id}
    for key, value in payload.items():
        if key != "plan_id":
            body[key] = value
    print(json.dumps(body, ensure_ascii=False))


def cmd_verification_plan_generate(plan_id: str) -> Dict:
    """Generate the verification plan document; return the server's answer."""
    payload = _api_request(
        "POST", "/api/verification-plan/{}/generate".format(plan_id), {}
    )
    _emit_result("verification-plan generate", plan_id, payload)
    return payload


def cmd_verification_plan_get(plan_id: str) -> Dict:
    """Read the verification plan document; return the server's answer."""
    payload = _api_request("GET", "/api/verification-plan/{}".format(plan_id))
    _emit_result("verification-plan get", plan_id, payload)
    return payload


def cmd_verification_plan_items(plan_id: str) -> Dict:
    """List the plan's review items; return the server's answer."""
    payload = _api_request(
        "GET", "/api/verification-plan/{}/review/items".format(plan_id)
    )
    _emit_result("verification-plan items", plan_id, payload)
    return payload


def cmd_verification_plan_review(
    plan_id: str,
    item: int,
    status: str,
    note: str = "",
    doc_sha256: Optional[str] = None,
) -> Dict:
    """Record one review action; return the server's answer.

    The item index goes into the path exactly as it was given. The CLI
    does not renumber it, does not check it against the item list, and
    does not know whether it exists — the endpoint answers that, with
    ``item_not_found``, and an answer the CLI made up would be a second
    opinion about a plan the CLI has not read.
    """
    body: Dict[str, object] = {"action": status, "note": note}
    if doc_sha256:
        body["doc_sha256"] = doc_sha256
    payload = _api_request(
        "POST",
        "/api/verification-plan/{}/review/item/{}".format(plan_id, item),
        body,
    )
    _emit_result("verification-plan review", plan_id, payload)
    return payload


def cmd_verification_plan_refine(plan_id: str, feedback: str) -> Dict:
    """Rewrite the plan document from feedback; return the server's answer."""
    payload = _api_request(
        "POST",
        "/api/verification-plan/{}/refine".format(plan_id),
        {"feedback": feedback},
    )
    _emit_result("verification-plan refine", plan_id, payload)
    return payload


def cmd_verification_verify(plan_id: str) -> Dict:
    """Start the verification run; return the server's answer.

    The body is empty on purpose. The endpoint owns its defaults — the
    round budget, whether the run may repair and re-execute — and a CLI
    that filled them in would be pinning values the operator never
    asked for, on the one path where a wrong value costs a whole run.
    """
    payload = _api_request(
        "POST", "/api/verification/{}/start".format(plan_id), {}
    )
    _emit_result("verification verify", plan_id, payload)
    return payload


def handle_verification_plan_command(args) -> int:
    """Dispatch the ``verification-plan`` subcommands; return an exit code.

    An API failure is reported on stderr and exits nonzero. The error
    goes to stderr rather than stdout because stdout carries the answer:
    a caller that pipes this command into a JSON reader gets the
    server's answer and nothing else, and a failure never arrives as a
    line of stdout that looks like one.
    """
    command = getattr(args, "verification_plan_command", None)
    try:
        if command == 'generate':
            cmd_verification_plan_generate(args.plan_id)
            return 0
        if command == 'get':
            cmd_verification_plan_get(args.plan_id)
            return 0
        if command == 'items':
            cmd_verification_plan_items(args.plan_id)
            return 0
        if command == 'review':
            cmd_verification_plan_review(
                args.plan_id,
                args.item,
                args.status,
                args.note,
                args.doc_sha256,
            )
            return 0
        if command == 'refine':
            cmd_verification_plan_refine(args.plan_id, args.feedback)
            return 0
    except ApiCommandError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    print(
        "Please specify a verification-plan command: generate, get, items, "
        "review, or refine",
        file=sys.stderr,
    )
    print(
        "Use 'autonomous-coding verification-plan --help' for more information",
        file=sys.stderr,
    )
    return 1


def handle_verification_command(args) -> int:
    """Dispatch the ``verification`` subcommands; return an exit code."""
    command = getattr(args, "verification_command", None)
    try:
        if command == 'verify':
            cmd_verification_verify(args.plan_id)
            return 0
    except ApiCommandError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    print(
        "Please specify a verification command: verify", file=sys.stderr
    )
    print(
        "Use 'autonomous-coding verification --help' for more information",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    main()