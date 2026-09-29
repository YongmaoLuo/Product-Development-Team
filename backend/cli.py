"""
Command Line Interface
====================

CLI for autonomous coding skill.
"""

import argparse
import os
from pathlib import Path
from typing import Optional

from agent import autonomous_coding
from rollback_manager import RollbackManager
from config_registry import ConfigRegistry
from config_loader import load_config_by_name
from execution_logger import get_logger


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
        """
    )

    # Main command arguments
    parser.add_argument("requirement", nargs="?", help="The high-level requirement")
    parser.add_argument("--workspace", "-w", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--dir", help="Project directory (deprecated, use --workspace)")
    parser.add_argument("--recover", action="store_true", help="Recover from previous crash (skip planning)")
    parser.add_argument("--max-tasks", type=int, default=None, help="Maximum tasks to execute")
    parser.add_argument("--config", "-c", default=None, help="Configuration name (e.g., 'coding', 'harmonyos')")
    parser.add_argument("--tool", "-t", default=None, choices=["claude", "opencode"],
                        help="Coding tool to use: 'claude' (default) or 'opencode'")
    parser.add_argument("--verification-tasks", default=None,
                        help="Path to a verification tasks JSON file (for auto-repair loop)")
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

    args = parser.parse_args()

    # Handle rollback commands
    if args.command == 'rollback':
        handle_rollback_command(args)
        return

    # Handle configs command
    if args.command == 'configs':
        handle_configs_command()
        return

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


if __name__ == "__main__":
    main()
